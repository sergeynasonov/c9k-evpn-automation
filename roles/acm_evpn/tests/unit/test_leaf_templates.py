# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Render the SHIPPED leaf template and assert what lands in each EVPN instance.

The rest of this suite tests filter_plugins/*.py. Nothing tested the templates,
which is how 'advertise mac enable' came to be emitted on routed-only segments:
the output was valid IOS-XE and contradicted the design, so neither the linter
nor a syntax check had anything to object to.

This renders roles/acm_evpn/templates/leaf_config.j2 itself -- the real file,
resolved through a FileSystemLoader so its '{% include common_base.j2 %}' loads
too -- rather than a copy of the interesting block. A copy would drift from what
ships the first time someone edits the template.

Plain jinja2, no Ansible runtime. Only two non-standard filters appear in these
templates: 'ios_vlan_range' is the project's own, imported here so it is the
real implementation, and 'ansible.utils.ipaddr' is stubbed because nothing
asserted below depends on it. ChainableUndefined lets the fixture stay small --
unrelated variables render empty instead of raising -- which is safe because
every assertion names an exact line.
"""

import os
import re
import sys
import unittest

import jinja2
import yaml

_ROLE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROLE, "filter_plugins"))

from ios_underlay import render_vlan_spec  # noqa: E402


def _render(template, edge_vlans, l2_only=None, hostname="access1-1",
            fabric_members=None, groups=None, vrfs=None):
    """Render a real shipped template with the given VLANs.

    ``fabric_members`` is the campus fabric by default. border_config.j2 scopes
    its border-to-border mesh to it, so leaving it out would render no mesh at
    all and quietly weaken every assertion about that block.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(_ROLE, "templates")),
        undefined=jinja2.ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ios_vlan_range"] = render_vlan_spec
    env.filters["ansible.utils.ipaddr"] = lambda value, query=None: value
    return env.get_template(template).render(
        all_edge_vlans=edge_vlans,
        netbox_vlan_groups={"vrfs": vrfs or {}, "l2_vlans": l2_only or {},
                            "l2_only_vlans": l2_only or {}},
        inventory_hostname=hostname,
        vtep_source_intf="Loopback0",
        evpn_bgp_asn=64513,
        evpn_ext_bgp_asn=64512,
        evpn_peer_group_name="EVPN-PEERS",
        fabric_edge_subnets={},
        underlay={"bgp": {"direct": [], "af_ipv4": []}},
        peer_ips=[], border_mesh=[],
        fabric_members=(["core1", "core2"] if fabric_members is None
                        else fabric_members),
        groups=(groups or {"device_roles_fabric_border": ["core1", "core2"]}),
        hostvars={"core1": {"ansible_host": "10.0.0.64"},
                  "core2": {"ansible_host": "10.0.0.65"},
                  "rcore1": {"ansible_host": "10.1.0.1"}},
    )


def render_leaf(edge_vlans, l2_only=None):
    return _render("leaf_config.j2", edge_vlans, l2_only)


def render_border(l2_only, hostname):
    return _render("border_config.j2", {}, l2_only, hostname)


def l2_only_vlan(vid=900, tags=()):
    """An enrolled L2-only overlay as the transform emits it."""
    return {str(vid): {"name": "L2_ONLY_%d" % vid, "l2vni": 10000 + vid,
                       "replication_mode": "static", "mcast_group": "239.0.1.1",
                       "tags": list(tags), "l2_only": True}}


def evpn_instances(rendered):
    """{vid: [child lines]} for every 'l2vpn evpn instance' stanza."""
    out = {}
    for m in re.finditer(r"^l2vpn evpn instance (\d+) vlan-based$((?:\n .*)*)",
                         rendered, re.M):
        out[int(m.group(1))] = [l.strip() for l in m.group(2).splitlines() if l.strip()]
    return out


def vlan(tags=(), replication="static", l2vni=None, vid=100):
    return {vid: {"name": "TEST_%d" % vid, "l2vni": l2vni or (10000 + vid),
                  "replication_mode": replication, "tags": list(tags)}}


class TestAdvertiseMac(unittest.TestCase):
    """'advertise mac' is disabled globally in common_base.j2; only BRIDGED
    segments override it. A routed-only DAG does not bridge, so advertising its
    MACs into EVPN contradicts the mode it is configured for."""

    def test_routed_dag_does_not_advertise_mac(self):
        inst = evpn_instances(render_leaf(vlan(tags=["dag_routed"], replication="ingress")))
        self.assertNotIn("advertise mac enable", inst[100])

    def test_routed_dag_disables_flooding_suppression(self):
        inst = evpn_instances(render_leaf(vlan(tags=["dag_routed"], replication="ingress")))
        self.assertIn("flooding-suppression address-resolution disable", inst[100])

    def test_bridged_dag_advertises_mac(self):
        inst = evpn_instances(render_leaf(vlan(tags=[])))
        self.assertIn("advertise mac enable", inst[100])

    def test_bridged_dag_keeps_default_flooding_suppression(self):
        """The suppression line is an override too -- bridged segments must not
        carry it, or they lose the ARP suppression that is the point of EVPN."""
        inst = evpn_instances(render_leaf(vlan(tags=[])))
        self.assertNotIn("flooding-suppression address-resolution disable", inst[100])

    def test_other_tags_do_not_make_a_segment_routed(self):
        """Only 'dag_routed' selects routed mode. A multicast-enabled bridged
        segment is still bridged."""
        inst = evpn_instances(render_leaf(vlan(tags=["multicast_enabled"])))
        self.assertIn("advertise mac enable", inst[100])
        self.assertNotIn("flooding-suppression address-resolution disable", inst[100])

    def test_the_two_modes_are_mutually_exclusive(self):
        """Whatever else changes, no instance may carry both markers: they
        describe opposite forwarding modes."""
        edge = {}
        edge.update(vlan(tags=["dag_routed"], replication="ingress", vid=100))
        edge.update(vlan(tags=[], vid=101))
        edge.update(vlan(tags=["multicast_enabled"], vid=200))
        for vid, lines in evpn_instances(render_leaf(edge)).items():
            both = ("advertise mac enable" in lines
                    and "flooding-suppression address-resolution disable" in lines)
            self.assertFalse(both, "instance %d carries both mode markers" % vid)


class TestGlobalDefault(unittest.TestCase):
    def test_common_base_disables_advertise_mac_globally(self):
        """The per-instance logic only makes sense against a global disable. If
        this default ever flips, every assertion above changes meaning."""
        with open(os.path.join(_ROLE, "templates", "common_base.j2")) as fh:
            body = fh.read()
        self.assertIn("advertise mac disable", body)
        self.assertNotIn("advertise mac enable", body)



class TestL2OnlyOverlay(unittest.TestCase):
    """An L2-only overlay has no VRF, so it is bridged with no gateway: the
    four blocks below and, critically, NO SVI."""

    def test_leaf_renders_the_four_blocks(self):
        out = render_leaf(l2_only_vlan(), l2_only_vlan())
        for expected in ("vlan 900",
                         "l2vpn evpn instance 900 vlan-based",
                         "vlan configuration 900",
                         " member evpn-instance 900 vni 10900",
                         " member vni 10900 mcast-group 239.0.1.1"):
            self.assertIn(expected, out, expected)

    def test_leaf_renders_no_svi(self):
        """The distinguishing property. An SVI would imply a gateway that does
        not exist, and the transform rejects a prefix for the same reason."""
        self.assertNotIn("interface Vlan900", render_leaf(l2_only_vlan(), l2_only_vlan()))

    def test_l2_only_is_bridged_so_it_advertises_mac(self):
        inst = evpn_instances(render_leaf(l2_only_vlan(), l2_only_vlan()))
        self.assertIn("advertise mac enable", inst[900])
        self.assertIn("replication-type static", inst[900])

    def test_border_renders_it_only_when_tagged_for_that_border(self):
        tagged = l2_only_vlan(900, tags=["core1"])
        self.assertIn("vlan 900", render_border(tagged, "core1"))
        self.assertNotIn("vlan 900", render_border(tagged, "core2"))

    def test_border_ignores_an_untagged_l2_only_vlan(self):
        """Default off on borders: a border has no local edge ports, so an L2
        segment belongs there only when it must be bridged out of the fabric."""
        untagged = l2_only_vlan(900, tags=[])
        for host in ("core1", "core2"):
            self.assertNotIn("vlan 900", render_border(untagged, host))

    def test_border_nve_member_carries_the_group(self):
        out = render_border(l2_only_vlan(900, tags=["core1"]), "core1")
        self.assertIn(" member vni 10900 mcast-group 239.0.1.1", out)


class TestHandoffUpstreamAsn(unittest.TestCase):
    """The eBGP handoff's remote-as comes from the VLAN, not from a global.

    The global ``evpn_ext_bgp_asn`` had one value for every fabric and every
    tenant, and is gone. netbox_evpn_data.py refuses a handoff that carries no
    ``upstream_asn``, so the template can render it unconditionally.
    """

    def handoff(self, upstream_asn, vid="197"):
        vlan = {"name": "VRF_CORP_HANDOFF", "tags": ["core1"],
                "neighbor_ip_v4": "10.67.118.6", "local_ip_v4": "10.67.118.7/31",
                "upstream_asn": upstream_asn}
        vrfs = {"CORP": {"l3vni": 20199, "rd": "199", "handoff_vlans": {vid: vlan},
                         "core_vlans": {}, "edge_vlans": {}}}
        return _render("border_config.j2", {}, None, "core1", vrfs=vrfs)

    def remote_as_for(self, rendered, neighbour="10.67.118.6"):
        return re.findall(r"^  neighbor %s remote-as (\d+)$" % re.escape(neighbour),
                          rendered, re.M)

    def test_the_vlans_own_asn_is_used(self):
        self.assertEqual(self.remote_as_for(self.handoff(64500)), ["64500"])

    def test_nothing_but_the_vlans_own_asn_is_rendered(self):
        """The fixture still defines evpn_ext_bgp_asn=64512, so seeing it would
        mean a fallback had crept back in."""
        self.assertNotIn("64512", self.remote_as_for(self.handoff(64500)))

    def test_a_four_byte_asn_renders_whole(self):
        self.assertEqual(self.remote_as_for(self.handoff(4200000000)), ["4200000000"])

    def test_the_whole_neighbour_block_is_rendered(self):
        out = self.handoff(64500)
        for suffix in ("fall-over bfd", "activate", "route-map WAN-INBOUND-FILTER in"):
            self.assertIn("  neighbor 10.67.118.6 " + suffix, out)


class TestBorderMeshIsFabricScoped(unittest.TestCase):
    """Borders full-mesh iBGP with each other over Loopback0.

    The mesh used to be built from every device in
    ``groups['device_roles_fabric_border']``, fabric or not. Its remote-as is
    the fabric ASN, so a second fabric's border would have been rendered as an
    iBGP neighbour in an ASN it is not in, over an underlay this switch has no
    route into. Unlike the peer list this is a session that would be ADDED, so
    the blast-radius guard -- which watches removals -- would not have caught it.
    """

    # Two fabrics: core1/core2 are the campus borders, rcore1 is another
    # fabric's border. All three carry the fabric_border role.
    GROUPS = {"device_roles_fabric_border": ["core1", "core2", "rcore1"]}
    CAMPUS = ["core1", "core2"]

    def render(self, hostname, fabric_members):
        return _render("border_config.j2", {}, None, hostname,
                       fabric_members=fabric_members, groups=self.GROUPS)

    def mesh_neighbours(self, rendered):
        """Just the mesh: a dotted quad with an explicit remote-as.

        The peer-group lines name EVPN-PEERS rather than an address, and the
        overlay peers render 'peer-group' rather than 'remote-as', so this
        matches the border mesh and nothing else.
        """
        return re.findall(r"^ neighbor (\d+\.\d+\.\d+\.\d+) remote-as", rendered, re.M)

    def test_mesh_reaches_the_other_border_in_the_same_fabric(self):
        self.assertEqual(self.mesh_neighbours(self.render("core1", self.CAMPUS)),
                         ["10.0.0.65"])

    def test_mesh_does_not_reach_another_fabrics_border(self):
        self.assertNotIn("10.1.0.1", self.render("core1", self.CAMPUS))

    def test_a_border_never_meshes_with_itself(self):
        self.assertNotIn("10.0.0.64", self.mesh_neighbours(self.render("core1", self.CAMPUS)))

    def test_the_other_fabric_sees_no_campus_border(self):
        out = self.render("rcore1", ["rcore1"])
        for addr in ("10.0.0.64", "10.0.0.65"):
            self.assertNotIn(addr, out)

    def test_a_lone_border_renders_no_mesh_neighbours(self):
        """A single-border fabric is legitimate and must not render a mesh to
        borders it does not have."""
        self.assertEqual(self.mesh_neighbours(self.render("rcore1", ["rcore1"])), [])


class TestDhcpSnoopingVlanList(unittest.TestCase):
    """A VLAN is snooped if and only if its SVI got a relay.

    Both come from the same field -- 'ip helper-address global' from
    dhcp_servers_ipv4 on the VLAN, and the snooping list from the presence of
    that same key -- so the two cannot drift. Asserted rather than assumed,
    because the pair is what makes snooping correct: snooping a VLAN with no
    relay inspects traffic that will never carry a reply, and NOT snooping one
    that has a relay drops the binding table the fabric relies on.
    """

    VLANS = {
        100: {"name": "CORP_DATA", "prefix_ipv4": "10.67.118.128/27",
              "vrf": "CORP", "dhcp_servers_ipv4": ["10.66.119.56", "10.66.119.57"]},
        105: {"name": "L2-TestACM", "vrf": None},          # L2-only: no relay
        200: {"name": "MEDICAL_DATA", "prefix_ipv4": "10.67.119.128/27",
              "vrf": "MEDICAL", "dhcp_servers_ipv4": ["10.66.119.56"]},
    }

    # The SVIs are rendered from netbox_vlan_groups.vrfs, the snooping list from
    # all_edge_vlans. Both are supplied here from the SAME dicts, so the test
    # sees the pair the way a real render does.
    VRFS = {
        "CORP": {"l3vni": 20003, "core_vlans": {},
                 "edge_vlans": {100: VLANS[100]}},
        "MEDICAL": {"l3vni": 20004, "core_vlans": {},
                    "edge_vlans": {200: VLANS[200]}},
    }

    def out(self, vlans=None, vrfs=None):
        return _render("leaf_config.j2",
                       self.VLANS if vlans is None else vlans,
                       vrfs=self.VRFS if vrfs is None else vrfs)

    def snooping_line(self, text):
        for line in text.splitlines():
            if line.startswith("ip dhcp snooping vlan "):
                return line
        return None

    def test_only_vlans_with_a_relay_are_snooped(self):
        self.assertEqual(self.snooping_line(self.out()),
                         "ip dhcp snooping vlan 100,200")

    def test_an_l2_only_vlan_is_never_snooped(self):
        """It has no SVI and therefore no relay, so there is nothing to snoop."""
        self.assertNotIn("105", self.snooping_line(self.out()))

    def test_every_snooped_vlan_also_rendered_a_helper_address(self):
        """The pair is the point: a snooped VLAN whose SVI has no relay is
        inspecting traffic that can never carry a reply, and a relayed VLAN
        left unsnooped loses the binding table."""
        text = self.out()
        listed = self.snooping_line(text).split("vlan ")[1]
        for vid, vlan in self.VLANS.items():
            has_relay = bool(vlan.get("dhcp_servers_ipv4"))
            self.assertEqual(str(vid) in listed, has_relay, "vlan %d" % vid)
            for ip in vlan.get("dhcp_servers_ipv4", []):
                self.assertIn("ip helper-address global %s" % ip, text)

    def test_no_relay_anywhere_renders_no_snooping_list(self):
        """An empty list would render 'ip dhcp snooping vlan ' with no argument,
        which IOS rejects."""
        self.assertIsNone(
            self.snooping_line(self.out({105: self.VLANS[105]}, vrfs={})))


class TestTrmMdtBlock(unittest.TestCase):
    """The MDT lines, which nothing rendered-side asserted before.

    test_trm.py checks the DATA -- which VRF is enabled, which group, which data
    range -- and stops there. What actually reaches the switch was untested, and
    that is where a word went missing.
    """

    VRFS = {
        "CORP": {
            "l3vni": 20003, "core_vlans": {}, "edge_vlans": {},
            "trm": {"enabled": True, "mdt_default_group": "239.3.2.1",
                    "mdt_data_network": "239.3.3.0",
                    "mdt_data_wildcard": "0.0.0.255"},
        },
    }

    def block(self):
        out = _render("leaf_config.j2", {}, vrfs=self.VRFS)
        return [ln.strip() for ln in out.splitlines() if "mdt " in ln]

    def test_overlay_use_bgp_carries_spt_only(self):
        """'spt-only' selects the MVPN profile in which receivers move to the
        shortest-path tree. Without it the shared-tree state an RP hands out is
        never completed, so an ASM (*,G) join is accepted and delivers nothing
        while SSM -- which consults no RP -- works perfectly.

        Part 6 measured exactly that in CORP on 2026-08-22: control passing, SSM
        routed at 10/10, ASM 0/10, and the conclusion drawn at the time was that
        the anycast RP had failed. The RP was fine. This word was missing, and
        no test looked at the rendered line.
        """
        self.assertIn("mdt overlay use-bgp spt-only", self.block())
        self.assertNotIn("mdt overlay use-bgp", self.block())

    def test_the_rest_of_the_mdt_block_is_intact(self):
        block = self.block()
        self.assertIn("mdt auto-discovery vxlan", block)
        self.assertIn("mdt default vxlan 239.3.2.1", block)
        self.assertIn("mdt data vxlan 239.3.3.0 0.0.0.255", block)

    def test_a_vrf_without_trm_renders_no_mdt_at_all(self):
        vrfs = {"CORP": {"l3vni": 20003, "core_vlans": {}, "edge_vlans": {}}}
        out = _render("leaf_config.j2", {}, vrfs=vrfs)
        self.assertNotIn("mdt ", out)


class TestARemovedVrfStopsBeingRendered(unittest.TestCase):
    """The half of a VRF teardown that is NOT a prune.

    stale_headers strips what the device has and the render no longer emits.
    But 'interface nve1' and 'router bgp' are regenerated wholesale, so a
    removed VRF leaves them by simply not being rendered -- and nothing checked
    that. A tenant address-family or an NVE member surviving a decommission
    would be invisible to any traffic test, which is precisely how the last
    three defects here went unnoticed.
    """

    VRFS = {
        "CORP": {"l3vni": 20003, "core_vlans": {199: {}}, "edge_vlans": {},
                 "handoff_vlans": {}},
        "SECURITY": {"l3vni": 20005, "core_vlans": {399: {}}, "edge_vlans": {},
                     "handoff_vlans": {}},
    }

    def render(self, vrfs):
        return _render("leaf_config.j2", {}, vrfs=vrfs)

    def test_both_vrfs_render_while_both_are_enrolled(self):
        out = self.render(self.VRFS)
        self.assertIn("address-family ipv4 vrf SECURITY", out)
        self.assertIn("vrf SECURITY", out)

    def test_a_removed_vrf_renders_no_bgp_address_family(self):
        out = self.render({"CORP": self.VRFS["CORP"]})
        self.assertNotIn("address-family ipv4 vrf SECURITY", out)
        self.assertIn("address-family ipv4 vrf CORP", out)

    def test_a_removed_vrf_renders_no_nve_member(self):
        """'member vni <l3vni> vrf SECURITY' under interface nve1."""
        out = self.render({"CORP": self.VRFS["CORP"]})
        self.assertNotIn("vrf SECURITY", out)

    def test_a_removed_vrf_renders_no_core_vlan_svi(self):
        out = self.render({"CORP": self.VRFS["CORP"]})
        self.assertNotIn("interface Vlan399", out)
        self.assertIn("interface Vlan199", out)


class TestCandidateOwnsTheTrunkAllowedList(unittest.TestCase):
    """A rendered CHILD line must replace the device's copy, not join it.

    Ownership everywhere else in the candidate is decided per STANZA, on the
    unindented header, and the whole stanza is then either ours or preserved.
    The handoff trunk breaks that: we own exactly one line inside an interface
    whose mode, description and port security belong to the operator.

    Emitting a full allowed list without stripping the device's would put two
    'switchport trunk allowed vlan' statements in the same interface and leave
    the result to whichever IOS reads last -- the identical failure mode as the
    DHCP snooping list above, which took two non-converging deploys to spot.

    The strip is scoped to the interfaces the render actually re-emits into.
    Any other trunk on the box keeps its allowed list untouched.
    """

    DEVICE = "\n".join([
        "hostname core1",
        "interface GigabitEthernet1/0/1",
        " description handoff",
        " switchport trunk allowed vlan 98,197,297,397",
        " switchport mode trunk",
        "!",
        "interface GigabitEthernet1/0/9",
        " description operator trunk, not ours",
        " switchport trunk allowed vlan 700,701",
        " switchport mode trunk",
        "!",
        "end",
    ])

    def candidate(self, final):
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.join(_ROLE, "templates")),
            undefined=jinja2.ChainableUndefined,
            trim_blocks=True, lstrip_blocks=True,
        )
        env.filters["regex_findall"] = lambda v, p: re.findall(p, v or "")
        env.filters["regex_search"] = lambda v, p: (
            re.search(p, v or "").group(0) if re.search(p, v or "") else None)
        env.tests["match"] = lambda v, p: bool(re.match(p, v or ""))
        env.filters["ios_vlan_range"] = render_vlan_spec
        return env.get_template("candidate_config.j2").render(
            fabric_config="hostname core1\n",
            current_run_config={"stdout": [self.DEVICE]},
            candidate_prefix_managed=[], candidate_omit_regex="",
            stale_headers=[], trunk_vlan_final=final,
        )

    def _allowed(self, out, intf):
        lines, cur = [], ""
        for ln in out.splitlines():
            if ln.startswith("interface "):
                cur = ln[10:].strip()
            elif not ln.startswith(" "):
                cur = ""
            elif cur == intf and ln.strip().startswith(
                    "switchport trunk allowed vlan "):
                lines.append(ln.strip())
        return lines

    def test_exactly_one_allowed_list_survives(self):
        out = self.candidate({"GigabitEthernet1/0/1": [98, 197, 297]})
        self.assertEqual(self._allowed(out, "GigabitEthernet1/0/1"),
                         ["switchport trunk allowed vlan 98,197,297"])

    def test_the_rest_of_the_interface_is_preserved(self):
        out = self.candidate({"GigabitEthernet1/0/1": [98, 197, 297]})
        self.assertIn(" description handoff", out)
        self.assertIn(" switchport mode trunk", out)

    def test_a_trunk_we_do_not_render_keeps_its_list(self):
        out = self.candidate({"GigabitEthernet1/0/1": [98, 197, 297]})
        self.assertEqual(self._allowed(out, "GigabitEthernet1/0/9"),
                         ["switchport trunk allowed vlan 700,701"])

    def test_nothing_is_stripped_when_nothing_is_rendered(self):
        """If the fact is empty -- a non-border, or a device whose trunk could
        not be identified -- the device's list must be carried through intact.
        Stripping without re-emitting would clear the trunk."""
        out = self.candidate({})
        self.assertEqual(self._allowed(out, "GigabitEthernet1/0/1"),
                         ["switchport trunk allowed vlan 98,197,297,397"])


class TestCandidateOwnsTheSnoopingList(unittest.TestCase):
    """The candidate must never carry two 'ip dhcp snooping vlan' lines.

    Ownership of a preserved line is decided by an exact match against what the
    render emits. That can never work for this one, because the VLAN list IS the
    value: the render says '100-101,200,300' while a device that has since lost
    a VLAN still says '100-101,106,200,300', so the device's copy is preserved
    ALONGSIDE ours and IOS applies whichever comes last.

    Measured on access1-1 on 2026-08-22 after VLAN 106 was deleted from NetBox:
    the device kept 106 in its snooping list through two consecutive deploys,
    and every run reported a change that never converged. The per-VLAN stanzas
    pruned correctly -- this is a GLOBAL line that merely names VLAN ids, so no
    VLAN 106 prune matched it.

    The default is loaded from defaults/main.yml rather than restated here, so
    this fails if the entry is removed, not merely if the mechanism breaks.
    """

    DEVICE = "\n".join([
        "hostname access1-1",
        "ip dhcp snooping",
        "ip dhcp snooping vlan 100-101,106,200,300",
        "interface GigabitEthernet1/0/5",
        " switchport mode access",
        "!",
        "end",
    ])
    RENDERED = "\n".join([
        "hostname access1-1",
        "ip dhcp snooping",
        "ip dhcp snooping vlan 100-101,200,300",
    ])

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROLE, "defaults", "main.yml")) as fh:
            cls.defaults = yaml.safe_load(fh)

    def candidate(self, prefixes=None):
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.join(_ROLE, "templates")),
            undefined=jinja2.ChainableUndefined,
            trim_blocks=True, lstrip_blocks=True,
        )
        # Ansible's, not jinja2's, and implemented rather than stubbed: the
        # ownership decision IS these three, so a stub would test nothing.
        env.filters["regex_findall"] = lambda v, p: re.findall(p, v or "")
        env.filters["regex_search"] = lambda v, p: (
            (re.search(p, v or "") or [None]) and
            (re.search(p, v or "").group(0) if re.search(p, v or "") else None))
        env.tests["match"] = lambda v, p: bool(re.match(p, v or ""))
        env.filters["ios_vlan_range"] = render_vlan_spec
        return env.get_template("candidate_config.j2").render(
            fabric_config=self.RENDERED,
            current_run_config={"stdout": [self.DEVICE]},
            candidate_prefix_managed=(self.defaults["candidate_prefix_managed"]
                                      if prefixes is None else prefixes),
            candidate_omit_regex=self.defaults.get("candidate_omit_regex", ""),
            stale_headers=[], trunk_vlan_final={},
        )

    def test_the_shipped_default_owns_the_line(self):
        self.assertIn("ip dhcp snooping vlan ",
                      "\n".join(self.defaults["candidate_prefix_managed"]))

    def test_only_one_snooping_list_survives(self):
        lines = [ln for ln in self.candidate().splitlines()
                 if ln.startswith("ip dhcp snooping vlan ")]
        self.assertEqual(lines, ["ip dhcp snooping vlan 100-101,200,300"])

    def test_the_stale_vlan_does_not_come_back(self):
        self.assertNotIn("106", self.candidate())

    def test_without_the_prefix_rule_both_lines_survive(self):
        """The failure this prevents, demonstrated rather than described."""
        lines = [ln for ln in self.candidate(prefixes=[]).splitlines()
                 if ln.startswith("ip dhcp snooping vlan ")]
        self.assertEqual(len(lines), 2)

    def test_unrelated_device_config_is_still_preserved(self):
        """Owning the line must not widen into owning the interface below it."""
        out = self.candidate()
        self.assertIn("interface GigabitEthernet1/0/5", out)
        self.assertIn(" switchport mode access", out)


if __name__ == "__main__":
    unittest.main()
