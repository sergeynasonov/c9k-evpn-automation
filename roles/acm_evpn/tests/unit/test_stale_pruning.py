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


if __name__ == "__main__":
    unittest.main()
