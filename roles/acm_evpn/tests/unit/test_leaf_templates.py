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


if __name__ == "__main__":
    unittest.main()
