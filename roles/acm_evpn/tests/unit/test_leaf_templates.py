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


def render_leaf(edge_vlans):
    """Render the real leaf_config.j2 with the given edge VLANs."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(_ROLE, "templates")),
        undefined=jinja2.ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ios_vlan_range"] = render_vlan_spec
    env.filters["ansible.utils.ipaddr"] = lambda value, query=None: value
    return env.get_template("leaf_config.j2").render(
        all_edge_vlans=edge_vlans,
        netbox_vlan_groups={"vrfs": {}},
        vtep_source_intf="Loopback0",
        evpn_bgp_asn=64513,
        evpn_peer_group_name="EVPN-PEERS",
    )


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


if __name__ == "__main__":
    unittest.main()
