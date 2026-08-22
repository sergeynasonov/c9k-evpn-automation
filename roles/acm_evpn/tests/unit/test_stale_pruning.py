# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""What the render prunes when an object stops being enrolled.

Under preserve-by-default, anything the role stops rendering is carried forward
from the device. That makes disabling a feature a ONE-WAY DOOR unless the render
also says what to remove, which is what 'stale_headers' in 02_render_configs.yml
is for. Nothing tested it.

The gap it missed: 'vlan <id>' was pruned only when the VLAN had a fabric SVI.
An L2-only overlay has no SVI by definition, so un-tagging one from a border
removed its EVPN instance, its 'vlan configuration' and its NVE member while
leaving 'vlan 104' behind as a preserved orphan. Found by reading a real
'acm replace ... diff' rather than by any test, hence this file.

The expression is LIFTED FROM THE SHIPPED TASK FILE, not copied here -- a copy
would drift from what runs the first time someone edits it. Three filters need
shimming: 'difference' and 'unique' are pure list operations, and
'regex_findall' is re.findall, which is exactly what Ansible's is. None has
Ansible semantics to get wrong.
"""

import ast
import json
import os
import re
import unittest

import jinja2
import yaml

_ROLE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TASKS = os.path.join(_ROLE, "tasks", "02_render_configs.yml")


def _stale_expression():
    with open(_TASKS) as fh:
        tasks = yaml.safe_load(fh)
    for t in tasks:
        if t.get("name", "").startswith("Work out what to prune"):
            return t["ansible.builtin.set_fact"]["stale_headers"]
    raise AssertionError("stale_headers task not found in 02_render_configs.yml")


def prune(**kwargs):
    """Return the list of stanza headers the render would strip."""
    env = jinja2.Environment()
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    env.filters["unique"] = lambda a: list(dict.fromkeys(a))
    env.filters["regex_findall"] = lambda s, p: re.findall(p, s, re.M)
    ctx = dict(_dev_evi_vlans=[], _dev_cfg_vlans=[], _dev_fabric_svis=[],
               _new_evi_vlans=[], _new_cfg_vlans=[],
               _dev_fabric_vrfs=[], _new_fabric_vrfs=[], trm_stale_loopbacks=[],
               fabric_config="")
    ctx.update(kwargs)
    return re.findall(r"'([^']+)'", env.from_string(_stale_expression()).render(**ctx))


class TestAHandoffVlanIsNotStale(unittest.TestCase):
    """A handoff renders 'vlan 197' and 'interface Vlan197' but never an EVPN
    instance or a 'vlan configuration' -- it carries no VNI.

    Measuring "gone" by EVI/vlan-configuration membership alone therefore
    declared every handoff VLAN stale on every single run. It changed no
    candidate, because the managed set drops those two headers anyway and the
    template re-emits them -- ACM reported no diff throughout -- but the
    pre-deploy summary said 'core1 PRUNE interface Vlan197, vlan 197' before a
    deploy that was going to change nothing. A summary that cries wolf is worse
    than no summary.
    """

    HANDOFF_RENDER = "vlan 197\n name VRF_CORP_HANDOFF197\ninterface Vlan197\n vrf forwarding CORP\n"

    def test_a_rendered_handoff_vlan_is_not_pruned(self):
        self.assertEqual([], prune(_dev_fabric_svis=["197"],
                                   fabric_config=self.HANDOFF_RENDER))

    def test_its_vlan_definition_is_not_pruned_either(self):
        out = prune(_dev_cfg_vlans=[], _dev_fabric_svis=["197"],
                    fabric_config=self.HANDOFF_RENDER)
        self.assertNotIn("vlan 197", out)
        self.assertNotIn("interface Vlan197", out)

    def test_a_handoff_the_render_has_DROPPED_is_still_pruned(self):
        """The half that must keep working: un-tag a handoff from this border
        and its SVI and VLAN must go, or they are preserved forever."""
        out = prune(_dev_fabric_svis=["197"], fabric_config="")
        self.assertIn("interface Vlan197", out)
        self.assertIn("vlan 197", out)


class TestStalePruning(unittest.TestCase):

    def test_l2_only_segment_leaves_no_orphan_vlan(self):
        """The bug this file exists for. No SVI, so the 'vlan <id>' definition
        has to be pruned on its own evidence."""
        out = prune(_dev_evi_vlans=["104"], _dev_cfg_vlans=["104"])
        self.assertIn("vlan 104", out)
        self.assertIn("l2vpn evpn instance 104 vlan-based", out)
        self.assertIn("vlan configuration 104", out)

    def test_l2_only_segment_does_not_prune_an_svi_it_never_had(self):
        """Pruning 'interface Vlan104' where none exists would put a removal for
        a non-existent object into every candidate."""
        self.assertNotIn("interface Vlan104",
                         prune(_dev_evi_vlans=["104"], _dev_cfg_vlans=["104"]))

    def test_routed_vlan_prunes_both_svi_and_definition(self):
        out = prune(_dev_evi_vlans=["200"], _dev_cfg_vlans=["200"],
                    _dev_fabric_svis=["200"])
        for expected in ("interface Vlan200", "vlan 200",
                         "l2vpn evpn instance 200 vlan-based", "vlan configuration 200"):
            self.assertIn(expected, out)

    def test_a_vlan_still_enrolled_is_not_pruned(self):
        self.assertEqual([], prune(_dev_evi_vlans=["104"], _dev_cfg_vlans=["104"],
                                   _new_evi_vlans=["104"], _new_cfg_vlans=["104"]))

    def test_a_vlan_the_fabric_never_owned_is_never_touched(self):
        """The guard that must stay narrow. An access VLAN, an underlay SVI's
        VLAN, a trunk member -- none carry an EVPN instance, a 'vlan
        configuration' or a fabric SVI, so none enter the candidate set at all.
        Widening this is how a full-replace deploy strips a site's own config."""
        self.assertEqual([], prune())

    def test_a_removed_vrf_is_pruned(self):
        self.assertIn("vrf definition CORP",
                      prune(_dev_fabric_vrfs=["CORP"], _new_fabric_vrfs=[]))

    def test_trm_loopbacks_are_carried_through(self):
        """VRFs that lost TRM contribute their RP loopback separately."""
        self.assertIn("interface Loopback20003",
                      prune(trm_stale_loopbacks=["interface Loopback20003"]))


class TestAVrfTeardownIsComplete(unittest.TestCase):
    """Removing a VRF must leave NOTHING behind, and the objects are tested
    together rather than one at a time.

    Each piece already had a test. What none of them asserted is that a whole
    VRF leaves cleanly, and "one object left behind" is exactly how the last
    three defects here escaped: TRM loopbacks that made disabling TRM a one-way
    door, a DHCP snooping list that kept a deleted VLAN for two days, and a
    handoff VLAN that stayed permitted on a border trunk forever. Every one was
    a render that stopped emitting something while preserve-by-default kept it.

    Modelled on VRF SECURITY as actually deployed on 2026-08-22: edge VLAN 300,
    handoffs 397 and 398, core VLAN 399, and Loopback20005 for TRM.
    """

    DEV_VLANS = ["300", "397", "398", "399"]

    def teardown(self):
        return prune(
            _dev_fabric_vrfs=["CORP", "SECURITY"], _new_fabric_vrfs=["CORP"],
            _dev_evi_vlans=["300"], _dev_cfg_vlans=["300"],
            _dev_fabric_svis=self.DEV_VLANS,
            _new_evi_vlans=[], _new_cfg_vlans=[],
            trm_stale_loopbacks=["interface Loopback20005"],
            fabric_config="",
        )

    def test_the_vrf_definition_goes(self):
        self.assertIn("vrf definition SECURITY", self.teardown())

    def test_every_svi_goes_edge_handoff_and_core(self):
        out = self.teardown()
        for vid in self.DEV_VLANS:
            self.assertIn("interface Vlan" + vid, out, vid)

    def test_every_vlan_definition_goes(self):
        out = self.teardown()
        for vid in self.DEV_VLANS:
            self.assertIn("vlan " + vid, out, vid)

    def test_the_edge_vlans_evpn_instance_goes(self):
        """Only the edge VLAN carries one -- a handoff has no VNI and a core
        VLAN carries the L3VNI rather than an EVI."""
        out = self.teardown()
        self.assertIn("l2vpn evpn instance 300 vlan-based", out)
        self.assertIn("vlan configuration 300", out)

    def test_the_trm_loopback_goes(self):
        """The documented one-way door: preserve-by-default would keep
        Loopback20005, 'ip multicast-routing vrf' and 'ip pim vrf' forever."""
        self.assertIn("interface Loopback20005", self.teardown())

    def test_a_surviving_vrf_loses_nothing(self):
        """CORP stays enrolled, so nothing of its may appear in the teardown."""
        out = self.teardown()
        self.assertNotIn("vrf definition CORP", out)
        for vid in ("100", "101", "197", "199"):
            self.assertNotIn("interface Vlan" + vid, out, vid)




def _trunk_state_expression():
    with open(_TASKS) as fh:
        tasks = yaml.safe_load(fh)
    for t in tasks:
        if t.get("name", "").startswith("Work out what to prune"):
            return t["ansible.builtin.set_fact"]["_trunk_state"]
    raise AssertionError("_trunk_state not found in 02_render_configs.yml")


def trunk_state(running, handoff_vids, fabric_svis, fabric_config="",
                host="core1"):
    """What the render decides the handoff trunk should carry.

    Returns the parsed per-interface dict: 'now', 'desired', 'add', 'drop'.
    """
    env = jinja2.Environment()
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    env.filters["intersect"] = lambda a, b: [x for x in a if x in b]
    env.filters["unique"] = lambda a: list(dict.fromkeys(a))
    env.filters["regex_findall"] = lambda s, p: re.findall(p, s, re.M)
    # Ansible provides these; plain jinja2 does not.
    env.tests["match"] = lambda v, p: bool(re.match(p, str(v)))
    vrfs = {"CORP": {"handoff_vlans": {
        v: {"tags": [host]} for v in handoff_vids}}}
    out = env.from_string(_trunk_state_expression()).render(
        netbox_vlan_groups={"vrfs": vrfs},
        inventory_hostname=host,
        _dev_fabric_svis=fabric_svis,
        fabric_config=fabric_config,
        current_run_config={"stdout": [running]},
    )
    return ast.literal_eval(out.strip())


def trunk_desired(*a, **kw):
    """Just the allowed-VLAN list, for the one trunk in these fixtures."""
    st = trunk_state(*a, **kw)
    if not st:
        return []
    return list(st.values())[0]["desired"]


class TestTheHandoffTrunkIsRenderedAsAFinalList(unittest.TestCase):
    """The trunk carries a computed LIST, not 'add'/'remove'.

    Both of those are imperative -- they describe a transition, so they only do
    anything if the render happens to be present at the instant the change
    occurs. Tearing VRF SECURITY down proved the cost: the removal did not exist
    yet, 397 stayed permitted on core1's trunk, and by the next deploy
    'interface Vlan397' was gone, so an SVI-based test could never match it
    again. A full list is state, so it reconciles whatever it finds.

    98 is underlay and shares this trunk. It is the path the automation reaches
    a border by, and it must survive every one of these.
    """

    TRUNK = ("interface GigabitEthernet1/0/1\n"
             " switchport trunk allowed vlan 98,197,297,397\n"
             " switchport mode trunk\n"
             "!\n"
             "interface Vlan98\n"
             " ip address 10.0.0.1 255.255.255.0\n")

    def _running(self, vlan_db):
        return "".join("vlan %d\n" % v for v in vlan_db) + "!\n" + self.TRUNK

    def test_a_handoff_whose_vrf_is_gone_is_dropped(self):
        """The VRF is being torn down in THIS run: 397's SVI is still on the
        device but the render no longer emits it."""
        out = trunk_desired(self._running([98, 197, 297, 397]),
                            handoff_vids=[197, 297],
                            fabric_svis=[197, 297, 397],
                            fabric_config="vlan 197\nvlan 297\n")
        self.assertEqual(out, [98, 197, 297])

    def test_a_handoff_orphaned_by_an_earlier_run_is_still_dropped(self):
        """The exact core1 state after the SECURITY teardown: 397 is permitted
        on the trunk, but its VLAN and SVI are already gone, so nothing about
        the fabric objects can identify it. This is what 'add'/'remove' could
        not repair and a final list fixes on the next deploy."""
        out = trunk_desired(self._running([98, 197, 297]),
                            handoff_vids=[197, 297],
                            fabric_svis=[197, 297],
                            fabric_config="vlan 197\nvlan 297\n")
        self.assertEqual(out, [98, 197, 297])

    def test_a_handoff_still_rendered_is_kept(self):
        out = trunk_desired(self._running([98, 197, 297, 397]),
                            handoff_vids=[197, 297, 397],
                            fabric_svis=[197, 297, 397],
                            fabric_config="vlan 197\nvlan 297\nvlan 397\n")
        self.assertEqual(out, [98, 197, 297, 397])

    def test_a_new_handoff_is_added_without_disturbing_the_rest(self):
        running = ("vlan 98\nvlan 197\n!\n"
                   "interface GigabitEthernet1/0/1\n"
                   " switchport trunk allowed vlan 98,197\n"
                   "!\n"
                   "interface Vlan98\n ip address 10.0.0.1 255.255.255.0\n")
        st = trunk_state(running, handoff_vids=[197, 297],
                         fabric_svis=[197],
                         fabric_config="vlan 197\nvlan 297\n")
        entry = st["GigabitEthernet1/0/1"]
        self.assertEqual(entry["desired"], [98, 197, 297])
        self.assertEqual(entry["add"], [297])
        self.assertEqual(entry["drop"], [])

    def test_an_unrecognised_vlan_on_the_trunk_is_never_dropped(self):
        """Preserve-by-default applies here too. A VLAN somebody put on this
        trunk by hand is not a fabric object and must not be swept up."""
        running = ("vlan 98\nvlan 197\nvlan 800\n!\n"
                   "interface GigabitEthernet1/0/1\n"
                   " switchport trunk allowed vlan 98,197,800\n"
                   "!\n"
                   "interface Vlan98\n ip address 10.0.0.1 255.255.255.0\n")
        out = trunk_desired(running, handoff_vids=[197], fabric_svis=[197],
                            fabric_config="vlan 197\n")
        self.assertEqual(out, [98, 197, 800])

    def test_a_wrapped_allowed_list_is_read_in_full(self):
        """IOS splits a long list across continuation lines. Reading only the
        last one would see a fraction of the trunk and delete the remainder."""
        running = ("vlan 98\nvlan 197\nvlan 800\nvlan 801\n!\n"
                   "interface GigabitEthernet1/0/1\n"
                   " switchport trunk allowed vlan 98,197\n"
                   " switchport trunk allowed vlan add 800,801\n"
                   "!\n"
                   "interface Vlan98\n ip address 10.0.0.1 255.255.255.0\n")
        out = trunk_desired(running, handoff_vids=[197], fabric_svis=[197],
                            fabric_config="vlan 197\n")
        self.assertEqual(out, [98, 197, 800, 801])

    def test_a_trunk_with_no_underlay_vlan_is_left_alone(self):
        """Only a trunk carrying an underlay SVI's VLAN is a handoff trunk.
        Every other trunk on the box belongs to the operator."""
        running = ("vlan 98\nvlan 700\n!\n"
                   "interface GigabitEthernet1/0/9\n"
                   " switchport trunk allowed vlan 700\n"
                   "!\n"
                   "interface Vlan98\n ip address 10.0.0.1 255.255.255.0\n")
        self.assertEqual(trunk_state(running, handoff_vids=[197],
                                     fabric_svis=[197],
                                     fabric_config="vlan 197\n"), {})


if __name__ == "__main__":
    unittest.main()
