# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Unit tests for roles/acm_evpn/filter_plugins/netbox_evpn_data.py.

Run with:
    venv/bin/python -m unittest discover -s roles/acm_evpn/tests/unit -v

No pytest / no live NetBox / no Ansible runtime required — these exercise
the pure Python transform directly with synthetic (and, where noted,
repo-derived real-world) GraphQL-shaped fixtures.

The VLAN 100/103/197/198/199 fixtures below are not invented: they
reproduce values captured from a live lab NetBox instance, and this
module's output is asserted to match them exactly. That is what makes
them a regression net rather than a restatement of the code.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/filter_plugins")

from netbox_evpn_data import (  # noqa: E402
    DEFAULT_L2VNI_BASE,
    DEFAULT_L3VNI_BASE,
    RD_TYPE1_MAX,
    VNI_MAX,
    VNI_MIN,
    build_evpn_data,
    first_last_usable,
    handoff_addresses,
    network_and_mask,
)

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # pragma: no cover
    from netbox_evpn_data import AnsibleFilterError  # noqa: F401


# ---------------------------------------------------------------------------
# Fixture builders — keep raw GraphQL shapes close to what NetBox actually
# returns (see the query in roles/acm_evpn/tasks/01_gather_data.yml).
# ---------------------------------------------------------------------------

def vlan(vid, name, role="edge", tags=None, site_id=None, scope_type=None, scope_id=None,
         prefixes=None, cfd=None):
    scope = None
    if scope_type is not None:
        scope = {"__typename": f"{scope_type}Type", "id": scope_id}
    return {
        "vid": vid,
        "name": name,
        # NetBox returns role=None when unassigned; role="" models that.
        "role": {"slug": role} if role else None,
        "tags": [{"slug": t} for t in (tags or [])],
        "site": {"id": site_id} if site_id else None,
        "group": {"scope": scope} if scope is not None else None,
        "prefixes": prefixes or [],
        # VLAN-level custom fields; carries the optional 'l2vni' override.
        "custom_field_data": cfd or {},
    }


def prefix(cidr, vrf=None, cfd=None):
    return {"prefix": cidr, "custom_field_data": cfd or {}, "vrf": vrf}


def vrf(name, rd, mcast_group="239.17.10.1", l3vni=20199):
    # l3vni defaults to a valid in-block value so that tests unrelated to VNI
    # allocation are not forced to care about it; VNI-specific tests pass it
    # explicitly (including l3vni=None to model a VRF missing the field).
    # A bridged segment needs a group, so fixtures default to a valid one;
    # multicast tests pass mcast_group=None to model the field being unset.
    cfd = {}
    if mcast_group is not None:
        cfd["mcast_base_group"] = mcast_group
    if l3vni is not None:
        cfd["l3vni"] = l3vni
    return {"name": name, "rd": rd, "custom_field_data": cfd}


def ip(id_, address):
    return {"id": id_, "address": address}


CORP_VRF = vrf("CORP", "199", "239.17.10.1")


# ---------------------------------------------------------------------------
# IP math helpers
# ---------------------------------------------------------------------------

class TestNetworkAndMask(unittest.TestCase):
    def test_v4_slash27(self):
        self.assertEqual(network_and_mask("10.67.118.128/27"), ("10.67.118.128", "255.255.255.224"))

    def test_v4_slash30(self):
        self.assertEqual(network_and_mask("169.254.255.0/30"), ("169.254.255.0", "255.255.255.252"))

    def test_v6_slash64(self):
        net, mask = network_and_mask("2001:db8:0:100::/64")
        self.assertEqual(net, "2001:db8:0:100::")
        self.assertEqual(mask, "ffff:ffff:ffff:ffff::")


class TestFirstLastUsable(unittest.TestCase):
    # Ground truth captured directly from ansible.utils.ipaddr against a
    # live Ansible run (the filter this module replaces), not re-derived.
    def test_v4_slash27(self):
        self.assertEqual(first_last_usable("10.67.118.128/27"), ("10.67.118.129", "10.67.118.158"))

    def test_v4_slash31_both_addresses_usable(self):
        # RFC 3021: no network/broadcast address, both addresses are hosts.
        self.assertEqual(first_last_usable("10.67.118.4/31"), ("10.67.118.4", "10.67.118.5"))

    def test_v6_slash64(self):
        first, _ = first_last_usable("2001:db8:0:100::/64")
        self.assertEqual(first, "2001:db8:0:100::1")

    def test_v6_slash127_both_addresses_usable(self):
        # RFC 6164: the IPv6 equivalent of /31.
        self.assertEqual(first_last_usable("2001:db8:ee::/127"), ("2001:db8:ee::", "2001:db8:ee::1"))

    def test_v4_slash30(self):
        self.assertEqual(first_last_usable("10.0.0.0/30"), ("10.0.0.1", "10.0.0.2"))

    def test_v4_slash32_single_address_degenerate_case(self):
        self.assertEqual(first_last_usable("10.0.0.5/32"), ("10.0.0.5", "10.0.0.5"))


class TestHandoffAddresses(unittest.TestCase):
    """Business rule: lowest usable address -> remote/upstream neighbor;
    highest usable address -> local switch SVI. /31 and /127 fall out of
    this automatically since first_last_usable() already treats both
    addresses in a P2P prefix as usable."""

    def test_v4_slash31_matches_repo_fixture(self):
        # testing/netbox_facts_visualized.json, VLAN 197 (VRF_CORP_HANDOFF197):
        # prefix 10.67.118.6/31 -> local_ip_v4=10.67.118.7/31, neighbor_ip_v4=10.67.118.6
        local, neighbor = handoff_addresses("10.67.118.6/31")
        self.assertEqual(local, "10.67.118.7/31")
        self.assertEqual(neighbor, "10.67.118.6")

    def test_v4_slash31_second_pair_matches_repo_fixture(self):
        # VLAN 198 (VRF_CORP_HANDOFF198): prefix 10.67.118.8/31
        local, neighbor = handoff_addresses("10.67.118.8/31")
        self.assertEqual(local, "10.67.118.9/31")
        self.assertEqual(neighbor, "10.67.118.8")

    def test_v4_larger_than_slash31_uses_usable_bounds(self):
        # Non-/31 handoff: lower usable (.1) -> neighbor, higher usable (.2) -> local.
        local, neighbor = handoff_addresses("10.0.0.0/30")
        self.assertEqual(local, "10.0.0.2/30")
        self.assertEqual(neighbor, "10.0.0.1")

    def test_v6_slash127(self):
        local, neighbor = handoff_addresses("2001:db8:ee::/127")
        self.assertEqual(local, "2001:db8:ee::1/127")
        self.assertEqual(neighbor, "2001:db8:ee::")

    def test_v6_larger_than_slash127_mirrors_v4_rule(self):
        local, neighbor = handoff_addresses("2001:db8:ee::/124")
        self.assertEqual(neighbor, "2001:db8:ee::1")   # first usable
        self.assertEqual(local, "2001:db8:ee::e/124")  # last usable


# ---------------------------------------------------------------------------
# build_evpn_data — core grouping / scope-matching behaviour
# ---------------------------------------------------------------------------

class TestEdgeVlanWithVrf(unittest.TestCase):
    """VLAN 100 (CORP_DATA_CAMPUS) from testing/netbox_facts_visualized.json."""

    def setUp(self):
        self.vlan_list = [
            vlan(
                100, "CORP_DATA_CAMPUS", role="edge", tags=["dag_routed"],
                scope_type="SiteGroup", scope_id="3",
                prefixes=[
                    prefix("10.67.118.128/27", vrf=CORP_VRF, cfd={"dhcp_servers_ipv4": ["d1", "d2"]}),
                    prefix("2001:db8:0:100::/64", vrf=CORP_VRF),
                ],
            )
        ]
        self.ip_list = [ip("d1", "10.66.119.56/32"), ip("d2", "10.66.119.57/32")]

    def test_included_when_site_group_matches(self):
        result = build_evpn_data(self.vlan_list, self.ip_list, my_site=None, my_sg="3")
        v = result["vrfs"]["CORP"]["edge_vlans"]["100"]
        self.assertEqual(v["prefix_ipv4"], "10.67.118.128/27")
        self.assertEqual(v["network_ipv4"], "10.67.118.128")
        self.assertEqual(v["netmask_ipv4"], "255.255.255.224")
        self.assertEqual(v["prefix_ipv6"], "2001:db8:0:100::/64")
        self.assertEqual(v["network_ipv6"], "2001:db8:0:100::")
        self.assertEqual(v["dhcp_servers_ipv4"], ["10.66.119.56", "10.66.119.57"])
        self.assertEqual(v["vrf_name"], "CORP")
        self.assertEqual(v["vrf_rd"], "199")
        self.assertEqual(v["vrf_mcast_group"], "239.17.10.1")
        self.assertEqual(v["tags"], ["dag_routed"])
        # also present in the flat all_edge_vlans view
        self.assertIn("100", result["all_edge_vlans"])
        self.assertEqual(result["vrfs"]["CORP"]["rd"], "199")
        self.assertEqual(result["vrfs"]["CORP"]["mcast_group"], "239.17.10.1")

    def test_excluded_when_site_group_does_not_match(self):
        result = build_evpn_data(self.vlan_list, self.ip_list, my_site="99", my_sg="4")
        self.assertNotIn("CORP", result["vrfs"])
        self.assertNotIn("100", result["all_edge_vlans"])

    def test_fabric_edge_subnets_populated_regardless_of_scope_match(self):
        # A host OUT of scope for this VLAN must still see its subnet in
        # fabric_edge_subnets (borders redistribute every VRF subnet
        # fabric-wide, not just their own site's).
        result = build_evpn_data(self.vlan_list, self.ip_list, my_site="99", my_sg="4")
        self.assertEqual(
            result["fabric_edge_subnets"]["CORP"]["100"],
            {"network": "10.67.118.128", "mask": "255.255.255.224"},
        )


class TestSiteScopedEdgeVlan(unittest.TestCase):
    """VLAN 103 (CORP_DATA) from testing/netbox_facts_visualized.json — its
    VLAN GROUP is scoped directly to a Site (not a SiteGroup)."""

    def setUp(self):
        self.vlan_list = [
            vlan(
                103, "CORP_DATA", role="edge",
                scope_type="Site", scope_id="53",
                prefixes=[prefix("10.67.118.224/27", vrf=CORP_VRF, cfd={"dhcp_servers_ipv4": ["d1"]})],
            )
        ]
        self.ip_list = [ip("d1", "10.66.119.56/32")]

    def test_match_sg_site_branch(self):
        result = build_evpn_data(self.vlan_list, self.ip_list, my_site="53", my_sg="3")
        self.assertIn("103", result["vrfs"]["CORP"]["edge_vlans"])

    def test_excluded_for_different_site(self):
        result = build_evpn_data(self.vlan_list, self.ip_list, my_site="51", my_sg="3")
        self.assertNotIn("CORP", result["vrfs"])


class TestCoreVlan(unittest.TestCase):
    """VLAN 199 (VRF_CORP_CORE_VLAN) — 30-bit anycast core subnet, no
    handoff math (core_vlan role, not vrf_l3_handoff_vlan)."""

    def test_core_vlan_no_handoff_math(self):
        vlan_list = [
            vlan(199, "VRF_CORP_CORE_VLAN", role="core_vlan", scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("169.254.255.0/30", vrf=CORP_VRF)])
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg="3")
        v = result["vrfs"]["CORP"]["core_vlans"]["199"]
        self.assertEqual(v["network_ipv4"], "169.254.255.0")
        self.assertEqual(v["netmask_ipv4"], "255.255.255.252")
        self.assertEqual(v["local_ip_v4"], "")   # core VLANs never get handoff addressing
        self.assertEqual(v["neighbor_ip_v4"], "")
        # core VLANs are not edge, so they don't leak into all_edge_vlans
        self.assertNotIn("199", result["all_edge_vlans"])


class TestHandoffVlan(unittest.TestCase):
    """VLANs 197/198 (VRF_CORP_HANDOFF197/198), tagged per-border."""

    def test_handoff_local_and_neighbor(self):
        vlan_list = [
            vlan(197, "VRF_CORP_HANDOFF197", role="vrf_l3_handoff_vlan", tags=["core1"],
                 scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("10.67.118.6/31", vrf=CORP_VRF)]),
            vlan(198, "VRF_CORP_HANDOFF198", role="vrf_l3_handoff_vlan", tags=["core2"],
                 scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("10.67.118.8/31", vrf=CORP_VRF)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg="3")
        handoffs = result["vrfs"]["CORP"]["handoff_vlans"]
        self.assertEqual(handoffs["197"]["local_ip_v4"], "10.67.118.7/31")
        self.assertEqual(handoffs["197"]["neighbor_ip_v4"], "10.67.118.6")
        self.assertEqual(handoffs["197"]["tags"], ["core1"])
        self.assertEqual(handoffs["198"]["local_ip_v4"], "10.67.118.9/31")
        self.assertEqual(handoffs["198"]["neighbor_ip_v4"], "10.67.118.8")
        # handoff VLANs are not edge VLANs
        self.assertNotIn("197", result["all_edge_vlans"])

    def test_non_slash31_handoff_uses_usable_bounds(self):
        vlan_list = [
            vlan(250, "VRF_CORP_HANDOFF_WIDE", role="vrf_l3_handoff_vlan", scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("10.0.0.0/30", vrf=CORP_VRF)])
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg="3")
        v = result["vrfs"]["CORP"]["handoff_vlans"]["250"]
        self.assertEqual(v["neighbor_ip_v4"], "10.0.0.1")   # lowest usable
        self.assertEqual(v["local_ip_v4"], "10.0.0.2/30")   # highest usable

    def test_ipv6_handoff_populated(self):
        vlan_list = [
            vlan(251, "VRF_CORP_HANDOFF_V6", role="vrf_l3_handoff_vlan", scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("2001:db8:ee::/127", vrf=CORP_VRF)])
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg="3")
        v = result["vrfs"]["CORP"]["handoff_vlans"]["251"]
        self.assertEqual(v["neighbor_ip_v6"], "2001:db8:ee::")
        self.assertEqual(v["local_ip_v6"], "2001:db8:ee::1/127")


class TestPureL2Vlan(unittest.TestCase):
    def test_l2_vlan_no_vrf(self):
        vlan_list = [vlan(50, "GUEST_WIFI", cfd={"mcast_group": "239.17.50.1"},
                          prefixes=[prefix("172.16.50.0/24")])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("50", result["l2_vlans"])
        self.assertIn("50", result["all_edge_vlans"])  # role '' counts as edge
        self.assertEqual(result["vrfs"], {})

    def test_vlan_with_unrecognised_role_produces_nothing_and_is_reported(self):
        vlan_list = [vlan(51, "SOMETHING", role="voice", prefixes=[prefix("172.16.51.0/24")])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertNotIn("51", result["all_edge_vlans"])
        self.assertNotIn("51", result["l2_vlans"])
        self.assertIn("51", result["skipped_vlans"])
        self.assertEqual(result["skipped_vlans"]["51"]["role"], "voice")


class TestNoScopeIsGlobal(unittest.TestCase):
    def test_vlan_with_no_site_and_no_group_is_always_included(self):
        vlan_list = [vlan(60, "GLOBAL_VLAN", cfd={"mcast_group": "239.17.60.1"},
                             prefixes=[prefix("172.16.60.0/24")])]
        for my_site, my_sg in [(None, None), ("51", "3"), ("999", "999")]:
            with self.subTest(my_site=my_site, my_sg=my_sg):
                result = build_evpn_data(vlan_list, [], my_site=my_site, my_sg=my_sg)
                self.assertIn("60", result["l2_vlans"])


class TestUnsupportedScopeType(unittest.TestCase):
    """Documents a pre-existing gap (carried over, not fixed here): a VLAN
    group scoped to a Region/Location/Rack/Cluster/ClusterGroup is not
    matched by either the site or site-group checks, so it is excluded
    everywhere unless the VLAN also has a direct site assignment."""

    def test_region_scoped_group_excluded_without_direct_site(self):
        vlan_list = [
            vlan(70, "REGION_SCOPED", scope_type="Region", scope_id="9",
                 prefixes=[prefix("172.16.70.0/24", vrf=CORP_VRF)])
        ]
        result = build_evpn_data(vlan_list, [], my_site="9", my_sg="9")
        self.assertEqual(result["l2_vlans"], {})


class TestEvpnEnrolmentIsByL3vni(unittest.TestCase):
    """Fabric membership is opt-in, marked solely by the presence of an
    'l3vni' custom field on the VRF. NetBox legitimately holds VRFs that are
    nothing to do with this fabric (WAN, out-of-band, other platforms)."""

    def test_vrf_without_l3vni_is_silently_excluded(self):
        non_fabric = vrf("WAN_MPLS", rd="65000:1", l3vni=None)
        vlan_list = [vlan(80, "WAN_LINK", prefixes=[prefix("172.16.80.0/24", vrf=non_fabric)])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertEqual(result["vrfs"], {})
        self.assertEqual(result["l2_vlans"], {})
        self.assertEqual(result["fabric_edge_subnets"], {})

    def test_absent_l3vni_is_not_an_error(self):
        """The whole point of the marker: a non-fabric VRF must not fail the
        render the way a genuinely broken value does."""
        non_fabric = vrf("OOB", rd="65000:2", l3vni=None)
        fabric = vrf("CORP", rd="199", l3vni=20199)
        vlan_list = [
            vlan(80, "OOB_MGMT", prefixes=[prefix("172.16.80.0/24", vrf=non_fabric)]),
            vlan(101, "CORP_DATA", prefixes=[prefix("10.0.1.0/24", vrf=fabric)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertEqual(set(result["vrfs"]), {"CORP"})

    def test_rd_is_irrelevant_to_enrolment(self):
        """RD no longer gates anything — the device RD is <loopback>:<l3vni>."""
        no_rd = vrf("CORP", rd=None, l3vni=20199)
        vlan_list = [vlan(101, "CORP_DATA", prefixes=[prefix("10.0.1.0/24", vrf=no_rd)])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertEqual(set(result["vrfs"]), {"CORP"})
        self.assertEqual(result["vrfs"]["CORP"]["l3vni"], 20199)

    def test_set_but_unparseable_l3vni_is_an_error(self):
        """Distinct from absent: the operator clearly meant to enrol it."""
        typo = vrf("CORP", rd="199", l3vni="2O199")  # letter O
        vlan_list = [vlan(101, "CORP_DATA", prefixes=[prefix("10.0.1.0/24", vrf=typo)])]
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("CORP", str(ctx.exception))
        self.assertIn("2O199", str(ctx.exception))


class TestVrfProvisionedOnlyWhereItsVlansAreInScope(unittest.TestCase):
    """An enrolled VRF is still only provisioned on switches that actually
    have one of its VLANs in scope — a Building B tenant VRF must not appear
    on Building A switches."""

    def test_vrf_absent_when_none_of_its_vlans_are_in_scope(self):
        bldg_b = vrf("TENANT_B", rd="300", l3vni=20030)
        vlan_list = [
            vlan(300, "B_ONLY", role="edge", site_id="52", prefixes=[prefix("10.3.0.0/24", vrf=bldg_b)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site="51", my_sg="3")
        self.assertEqual(result["vrfs"], {})

    def test_vrf_present_when_one_of_its_vlans_is_in_scope(self):
        bldg_a = vrf("TENANT_A", rd="301", l3vni=20031)
        vlan_list = [
            vlan(301, "A_ONLY", site_id="51", prefixes=[prefix("10.4.0.0/24", vrf=bldg_a)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site="51", my_sg="3")
        self.assertEqual(set(result["vrfs"]), {"TENANT_A"})

    def test_site_group_scoped_vlan_pulls_vrf_in(self):
        campus = vrf("CAMPUS_WIDE", rd="302", l3vni=20032)
        vlan_list = [
            vlan(302, "EVERYWHERE", scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("10.5.0.0/24", vrf=campus)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site="51", my_sg="3")
        self.assertEqual(set(result["vrfs"]), {"CAMPUS_WIDE"})

    def test_out_of_scope_vrf_still_advertised_fabric_wide_by_borders(self):
        """Scope gates per-switch provisioning, not the border redistribution
        list — borders must advertise every fabric VRF subnet."""
        bldg_b = vrf("TENANT_B", rd="300", l3vni=20030)
        vlan_list = [
            vlan(300, "B_ONLY", role="edge", site_id="52", prefixes=[prefix("10.3.0.0/24", vrf=bldg_b)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site="51", my_sg="3")
        self.assertIn("TENANT_B", result["fabric_edge_subnets"])


class TestDhcpResolution(unittest.TestCase):
    def test_dedup_across_multiple_prefixes_in_same_vlan(self):
        vlan_list = [
            vlan(90, "MULTI_PREFIX", cfd={"mcast_group": "239.17.90.1"}, prefixes=[
                prefix("10.1.1.0/24", cfd={"dhcp_servers_ipv4": ["a", "b"]}),
                prefix("10.1.2.0/24", cfd={"dhcp_servers_ipv4": ["b", "c"]}),  # 'b' repeated
            ])
        ]
        ip_list = [ip("a", "10.9.9.1/32"), ip("b", "10.9.9.2/32"), ip("c", "10.9.9.3/32")]
        result = build_evpn_data(vlan_list, ip_list, my_site=None, my_sg=None)
        self.assertEqual(result["l2_vlans"]["90"]["dhcp_servers_ipv4"], ["10.9.9.1", "10.9.9.2", "10.9.9.3"])

    def test_unresolvable_id_is_silently_skipped(self):
        vlan_list = [vlan(91, "STALE_REF", cfd={"mcast_group": "239.17.91.1"},
                          prefixes=[prefix("10.1.3.0/24", cfd={"dhcp_servers_ipv4": ["ghost"]})])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertEqual(result["l2_vlans"]["91"]["dhcp_servers_ipv4"], [])

    def test_ipv6_dhcp_resolution(self):
        vlan_list = [vlan(92, "V6_DHCP", cfd={"mcast_group": "239.17.92.1"}, prefixes=[
            prefix("2001:db8:9::/64", cfd={"dhcp_servers_ipv6": ["r1"]})
        ])]
        ip_list = [ip("r1", "2001:db8:9::53/128")]
        result = build_evpn_data(vlan_list, ip_list, my_site=None, my_sg=None)
        self.assertEqual(result["l2_vlans"]["92"]["dhcp_servers_ipv6"], ["2001:db8:9::53"])


class TestVrfFirstPrefixWins(unittest.TestCase):
    def test_second_prefix_vrf_data_ignored(self):
        vlan_list = [
            vlan(93, "MULTI_VRF_PREFIX", prefixes=[
                prefix("10.1.4.0/24", vrf=vrf("FIRST", "1", "239.0.0.1")),
                prefix("10.1.5.0/24", vrf=vrf("SECOND", "2", "239.0.0.2")),
            ])
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("FIRST", result["vrfs"])
        self.assertNotIn("SECOND", result["vrfs"])


class TestFullFabricIntegration(unittest.TestCase):
    """One combined pass over a small multi-VRF, multi-role, multi-scope
    fabric slice, asserting the whole returned shape at once."""

    def test_end_to_end(self):
        vlan_list = [
            vlan(100, "CORP_DATA_CAMPUS", tags=["dag_routed"], scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("10.67.118.128/27", vrf=CORP_VRF, cfd={"dhcp_servers_ipv4": ["d1"]})]),
            vlan(199, "VRF_CORP_CORE_VLAN", role="core_vlan", scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("169.254.255.0/30", vrf=CORP_VRF)]),
            vlan(197, "VRF_CORP_HANDOFF197", role="vrf_l3_handoff_vlan", tags=["core1"],
                 scope_type="SiteGroup", scope_id="3",
                 prefixes=[prefix("10.67.118.6/31", vrf=CORP_VRF)]),
            vlan(500, "GLOBAL_GUEST", cfd={"mcast_group": "239.17.5.1"},
                 prefixes=[prefix("172.16.5.0/24")]),
            vlan(600, "OTHER_SITE_ONLY", site_id="99", prefixes=[prefix("172.16.6.0/24", vrf=CORP_VRF)]),
        ]
        ip_list = [ip("d1", "10.66.119.56/32")]

        result = build_evpn_data(vlan_list, ip_list, my_site="51", my_sg="3")

        self.assertEqual(set(result["vrfs"]["CORP"]["edge_vlans"]), {"100"})
        self.assertEqual(set(result["vrfs"]["CORP"]["core_vlans"]), {"199"})
        self.assertEqual(set(result["vrfs"]["CORP"]["handoff_vlans"]), {"197"})
        self.assertEqual(set(result["l2_vlans"]), {"500"})
        self.assertEqual(set(result["all_edge_vlans"]), {"100", "500"})
        # VLAN 600 is scoped to a different site outright -> excluded locally...
        self.assertNotIn("600", result["vrfs"]["CORP"]["edge_vlans"])
        # ...but its subnet still shows up fabric-wide for border redistribution.
        self.assertIn("600", result["fabric_edge_subnets"]["CORP"])


# ---------------------------------------------------------------------------
# VNI allocation
#
# L2VNI is derived (evpn_l2vni_base + VLAN ID) and safe because VLAN IDs are
# unique fabric-wide. L3VNI is explicit per VRF (NetBox custom field) and also
# drives the RD and route-targets. Every VNI problem must fail the render.
# ---------------------------------------------------------------------------

class TestL2VniDerivation(unittest.TestCase):
    def _build(self, vlan_list, **kw):
        return build_evpn_data(vlan_list, [], my_site=None, my_sg=None, **kw)

    def test_derived_from_vlan_id(self):
        result = self._build([vlan(101, "CORP_DATA", prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["101"]["l2vni"], 10101)

    def test_derivation_is_arithmetic_not_string_concat(self):
        """VLAN 1000 must be 11000, not the old concat result of 101000."""
        result = self._build([vlan(1000, "BIG", prefixes=[prefix("10.0.2.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["1000"]["l2vni"], 11000)

    def test_low_vlan_ids_stay_in_valid_vni_range(self):
        """The old concat scheme produced VNI 1010 for VLAN 10 - below the
        4096 floor and rejected by IOS-XE. Arithmetic derivation cannot."""
        result = self._build([vlan(10, "MGMT", prefixes=[prefix("10.0.3.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["10"]["l2vni"], 10010)
        self.assertGreaterEqual(result["all_edge_vlans"]["10"]["l2vni"], VNI_MIN)

    def test_whole_vlan_space_lands_in_range(self):
        vlans = [vlan(v, f"V{v}", prefixes=[prefix("10.0.0.0/24", vrf=CORP_VRF)]) for v in range(1, 4095)]
        result = self._build(vlans)
        vnis = [r["l2vni"] for r in result["all_edge_vlans"].values()]
        self.assertEqual(len(vnis), 4094)
        self.assertTrue(all(VNI_MIN <= v <= VNI_MAX for v in vnis))
        self.assertEqual(len(set(vnis)), 4094, "derived L2VNIs must be unique")

    def test_custom_base_is_honoured(self):
        result = self._build([vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])], l2vni_base=50000)
        self.assertEqual(result["all_edge_vlans"]["101"]["l2vni"], 50101)

    def test_default_base_matches_documented_constant(self):
        result = self._build([vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["101"]["l2vni"], DEFAULT_L2VNI_BASE + 101)

    def test_backwards_compatible_for_three_digit_vlans(self):
        """Migration guard: for VLAN 100-999 the new arithmetic value equals
        the old concat value ('10' ~ vid), so existing fabrics do not churn."""
        vlans = [vlan(v, f"V{v}", prefixes=[prefix("10.0.0.0/24", vrf=CORP_VRF)]) for v in range(100, 1000)]
        result = self._build(vlans)
        for v in range(100, 1000):
            self.assertEqual(result["all_edge_vlans"][str(v)]["l2vni"], int("10" + str(v)))

    def test_override_wins_over_derivation(self):
        result = self._build([vlan(101, "LEGACY", cfd={"l2vni": 55555},
                                   prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["101"]["l2vni"], 55555)

    def test_blank_override_falls_back_to_derivation(self):
        result = self._build([vlan(101, "X", cfd={"l2vni": ""}, prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["101"]["l2vni"], 10101)

    def test_string_override_is_coerced(self):
        result = self._build([vlan(101, "X", cfd={"l2vni": "55555"}, prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])])
        self.assertEqual(result["all_edge_vlans"]["101"]["l2vni"], 55555)

    def test_non_edge_vlans_get_no_l2vni(self):
        vlans = [
            vlan(199, "CORE", role="core_vlan", prefixes=[prefix("169.254.1.0/30", vrf=CORP_VRF)]),
            vlan(197, "HANDOFF", role="vrf_l3_handoff_vlan",
                 prefixes=[prefix("10.67.118.4/31", vrf=CORP_VRF)]),
        ]
        result = self._build(vlans)
        self.assertIsNone(result["vrfs"]["CORP"]["core_vlans"]["199"]["l2vni"])
        self.assertIsNone(result["vrfs"]["CORP"]["handoff_vlans"]["197"]["l2vni"])


class TestL3VniExtraction(unittest.TestCase):
    def test_l3vni_exposed_on_vrf(self):
        v = vrf("CORP", "199", l3vni=20199)
        result = build_evpn_data(
            [vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])], [], None, None
        )
        self.assertEqual(result["vrfs"]["CORP"]["l3vni"], 20199)

    def test_string_l3vni_is_coerced(self):
        v = vrf("CORP", "199", l3vni="20199")
        result = build_evpn_data(
            [vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])], [], None, None
        )
        self.assertEqual(result["vrfs"]["CORP"]["l3vni"], 20199)


class TestVniValidation(unittest.TestCase):
    def _expect_failure(self, vlan_list, *fragments, **kw):
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data(vlan_list, [], my_site=None, my_sg=None, **kw)
        msg = str(ctx.exception)
        for fragment in fragments:
            self.assertIn(fragment, msg)
        return msg

    def test_vrf_without_l3vni_does_not_fail(self):
        """Absent l3vni means "not in the fabric", not "misconfigured".
        Enrolment semantics are covered in TestEvpnEnrolmentIsByL3vni."""
        v = vrf("CORP", "199", l3vni=None)
        result = build_evpn_data(
            [vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])], [], None, None
        )
        self.assertEqual(result["vrfs"], {})

    def test_unparseable_l3vni_fails(self):
        v = vrf("CORP", "199", l3vni="twenty-thousand")
        self._expect_failure(
            [vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            "CORP", "not a valid integer",
        )

    def test_l3vni_below_floor_fails(self):
        v = vrf("CORP", "199", l3vni=999)
        self._expect_failure([vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])], "999")

    def test_l3vni_above_rd_16bit_ceiling_fails(self):
        """A valid VNI (<=16777215) can still be an invalid RD: 'rd <ip>:<nn>'
        carries nn in 16 bits, so L3VNI is capped at 65535, not VNI_MAX."""
        v = vrf("CORP", "199", l3vni=100000)
        msg = self._expect_failure([vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])], "100000")
        self.assertIn(str(RD_TYPE1_MAX), msg)

    def test_l3vni_at_the_multicast_index_ceiling_is_accepted(self):
        """base + 254 is the highest L3VNI whose derived multicast index still
        fits an octet, and is therefore the real ceiling — tighter than the
        16-bit RD limit, which is why RD_TYPE1_MAX alone is no longer it."""
        top = DEFAULT_L3VNI_BASE + 254
        v = vrf("CORP", "199", l3vni=top)
        result = build_evpn_data(
            [vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])], [], None, None
        )
        self.assertEqual(result["vrfs"]["CORP"]["l3vni"], top)
        self.assertLess(top, RD_TYPE1_MAX)

    def test_l3vni_above_the_multicast_index_ceiling_fails(self):
        v = vrf("CORP", "199", l3vni=DEFAULT_L3VNI_BASE + 255)
        self._expect_failure(
            [vlan(101, "X", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            "multicast index 255", "20001..20254")

    def test_duplicate_l3vni_across_vrfs_fails(self):
        a = vrf("CORP", "199", l3vni=20199)
        b = vrf("GUEST", "200", l3vni=20199)
        self._expect_failure(
            [
                vlan(101, "A", prefixes=[prefix("10.0.1.0/24", vrf=a)]),
                vlan(102, "B", prefixes=[prefix("10.0.2.0/24", vrf=b)]),
            ],
            "20199", "CORP", "GUEST",
        )

    def test_l2_l3_collision_fails(self):
        """The exact class of bug the old scheme allowed: a VLAN's L2VNI
        landing on a VRF's L3VNI."""
        v = vrf("CORP", "199", l3vni=10101)
        self._expect_failure(
            [vlan(101, "CLASH", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            "10101", "CORP", "101",
        )

    def test_duplicate_l2vni_via_override_fails(self):
        self._expect_failure(
            [
                vlan(101, "A", prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)]),
                vlan(102, "B", cfd={"l2vni": 10101}, prefixes=[prefix("10.0.2.0/24", vrf=CORP_VRF)]),
            ],
            "10101",
        )

    def test_out_of_range_override_fails(self):
        self._expect_failure(
            [vlan(101, "A", cfd={"l2vni": 99}, prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])],
            "99",
        )

    def test_override_above_vni_ceiling_fails(self):
        self._expect_failure(
            [vlan(101, "A", cfd={"l2vni": VNI_MAX + 1}, prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])],
            str(VNI_MAX + 1),
        )

    def test_non_numeric_override_fails(self):
        self._expect_failure(
            [vlan(101, "A", cfd={"l2vni": "not-a-number"}, prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)])],
            "101", "not a valid integer",
        )

    def test_all_problems_reported_together(self):
        """One deploy should surface every VNI problem, not just the first."""
        a = vrf("CORP", "199", l3vni="bad")
        b = vrf("GUEST", "200", l3vni=99)
        msg = self._expect_failure(
            [
                vlan(101, "A", prefixes=[prefix("10.0.1.0/24", vrf=a)]),
                vlan(102, "B", prefixes=[prefix("10.0.2.0/24", vrf=b)]),
                vlan(103, "C", cfd={"l2vni": "junk"}, prefixes=[prefix("10.0.3.0/24", vrf=CORP_VRF)]),
            ],
            "CORP", "GUEST", "103",
        )
        self.assertIn("3 problems", msg)


class TestVniValidationIsFabricWide(unittest.TestCase):
    """Validation runs against the whole NetBox dataset, not just the VLANs in
    scope for the rendering host — a clash between two other buildings must
    fail every host's render, not hide until someone renders there."""

    def test_fabric_wide_collision_detected_from_a_different_site(self):
        other = vrf("OTHER", "300", l3vni=20030)
        vlans = [
            vlan(101, "LOCAL", site_id="51", prefixes=[prefix("10.0.1.0/24", vrf=CORP_VRF)]),
            vlan(700, "REMOTE", site_id="99", cfd={"l2vni": 20030},
                 prefixes=[prefix("10.0.7.0/24", vrf=other)]),
        ]
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data(vlans, [], my_site="51", my_sg=None)
        self.assertIn("20030", str(ctx.exception))


# ---------------------------------------------------------------------------
# Overlay enrolment is opt-in: role must be 'edge'
#
# Regression suite for the Core-N-Underlay incident. VLAN 98/99 are
# point-to-point underlay VLANs that exist only on core1/core2. They were
# added to NetBox to document their IPAM, with no role and no VRF, and were
# consequently deployed to EVERY leaf as L2 overlay segments with their own
# EVPN instance and VNI. The deploy guard did not catch it because it only
# scores removals, and this was an addition.
# ---------------------------------------------------------------------------

class TestOverlayEnrolmentIsOptIn(unittest.TestCase):
    def _underlay_vlans(self):
        """VLAN 98/99 as NetBox actually held them: no role, no VRF."""
        return [
            vlan(98, "Core-1-Underlay", role="", prefixes=[prefix("10.67.118.2/31")]),
            vlan(99, "Core-2-Underlay", role="", prefixes=[prefix("10.67.118.4/31")]),
        ]

    def test_unrolled_vlan_is_not_deployed_as_an_overlay_segment(self):
        result = build_evpn_data(self._underlay_vlans(), [], my_site=None, my_sg=None)
        self.assertEqual(result["all_edge_vlans"], {})

    def test_unrolled_vlan_gets_no_l2vni(self):
        """No VNI burned, and nothing for the NVE to join."""
        result = build_evpn_data(self._underlay_vlans(), [], my_site=None, my_sg=None)
        self.assertEqual(result["all_edge_vlans"], {})
        self.assertNotIn("98", result["l2_vlans"])

    def test_unrolled_vlans_are_reported_not_silently_dropped(self):
        result = build_evpn_data(self._underlay_vlans(), [], my_site=None, my_sg=None)
        self.assertEqual(set(result["skipped_vlans"]), {"98", "99"})
        self.assertEqual(result["skipped_vlans"]["98"]["name"], "Core-1-Underlay")
        self.assertEqual(result["skipped_vlans"]["98"]["role"], "")

    def test_explicit_edge_role_is_deployed(self):
        vlan_list = [vlan(101, "CORP_DATA", role="edge",
                          prefixes=[prefix("10.67.118.160/27", vrf=CORP_VRF)])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("101", result["all_edge_vlans"])
        self.assertEqual(result["skipped_vlans"], {})

    def test_pure_l2_vlan_still_supported_when_explicitly_marked(self):
        """A genuine L2-only tenant segment (no VRF) is still valid — it just
        has to say so now."""
        vlan_list = [vlan(500, "GUEST_L2", role="edge", cfd={"mcast_group": "239.17.5.1"},
                          prefixes=[prefix("172.16.5.0/24")])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("500", result["all_edge_vlans"])
        self.assertEqual(result["all_edge_vlans"]["500"]["l2vni"], 10500)

    def test_core_and_handoff_roles_are_unaffected(self):
        vlan_list = [
            vlan(199, "CORE", role="core_vlan",
                 prefixes=[prefix("169.254.255.0/30", vrf=CORP_VRF)]),
            vlan(197, "HANDOFF", role="vrf_l3_handoff_vlan", tags=["core1"],
                 prefixes=[prefix("10.67.118.4/31", vrf=CORP_VRF)]),
        ]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("199", result["vrfs"]["CORP"]["core_vlans"])
        self.assertIn("197", result["vrfs"]["CORP"]["handoff_vlans"])
        self.assertEqual(result["skipped_vlans"], {})

    def test_unrolled_vlan_in_a_fabric_vrf_is_also_skipped(self):
        """The rule is about the VLAN's role, not its VRF's enrolment."""
        vlan_list = [vlan(102, "NO_ROLE", role="",
                          prefixes=[prefix("10.0.2.0/24", vrf=CORP_VRF)])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertIn("102", result["skipped_vlans"])
        self.assertEqual(result["skipped_vlans"]["102"]["vrf_name"], "CORP")
        self.assertNotIn("102", result["all_edge_vlans"])

    def test_unrolled_vlan_excluded_from_border_redistribution(self):
        """fabric_edge_subnets feeds 'network' statements on the borders; an
        underlay P2P prefix must not be advertised as a tenant subnet."""
        vlan_list = [vlan(98, "Core-1-Underlay", role="",
                          prefixes=[prefix("10.67.118.2/31", vrf=CORP_VRF)])]
        result = build_evpn_data(vlan_list, [], my_site=None, my_sg=None)
        self.assertEqual(result["fabric_edge_subnets"], {})

    def test_out_of_scope_vlans_are_not_reported_as_skipped(self):
        """The report is about this host's scope, so it stays actionable."""
        vlan_list = [vlan(700, "ELSEWHERE", role="", site_id="99",
                          prefixes=[prefix("10.0.7.0/24")])]
        result = build_evpn_data(vlan_list, [], my_site="51", my_sg="3")
        self.assertEqual(result["skipped_vlans"], {})


# ---------------------------------------------------------------------------
# Underlay multicast replication groups
#
# Corrected semantics (confirmed with the project owner):
#   dag_routed  -> ROUTED-ONLY DAG: proxy-ARP, flooding-suppression disabled,
#                  ingress-replication. Needs NO multicast group.
#   untagged    -> BRIDGED DAG: replication-type static + mcast-group. REQUIRES
#                  a group, resolved as VLAN 'mcast_group' override, else the
#                  VRF's 'mcast_base_group'.
#
# The original templates paired each EVI replication-type with the opposite NVE
# mode, and the missing-group case rendered a bare "member vni N mcast-group"
# that IOS-XE rejected as "% Incomplete command" on every leaf.
# ---------------------------------------------------------------------------

class TestMulticastGroupResolution(unittest.TestCase):
    def test_bridged_vlan_inherits_vrf_base_group(self):
        v = vrf("CORP", "199", mcast_group="239.17.10.1")
        result = build_evpn_data(
            [vlan(101, "CORP_DATA", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            [], None, None)
        rec = result["all_edge_vlans"]["101"]
        self.assertEqual(rec["mcast_group"], "239.17.10.1")
        self.assertFalse(rec["mcast_group_is_override"])

    def test_vlan_override_wins_over_vrf_base(self):
        v = vrf("CORP", "199", mcast_group="239.17.10.1")
        result = build_evpn_data(
            [vlan(101, "CORP_DATA", role="edge", cfd={"mcast_group": "239.17.99.9"},
                  prefixes=[prefix("10.0.1.0/24", vrf=v)])], [], None, None)
        rec = result["all_edge_vlans"]["101"]
        self.assertEqual(rec["mcast_group"], "239.17.99.9")
        self.assertTrue(rec["mcast_group_is_override"])

    def test_blank_override_falls_back_to_vrf_base(self):
        v = vrf("CORP", "199", mcast_group="239.17.10.1")
        result = build_evpn_data(
            [vlan(101, "X", role="edge", cfd={"mcast_group": ""},
                  prefixes=[prefix("10.0.1.0/24", vrf=v)])], [], None, None)
        self.assertEqual(result["all_edge_vlans"]["101"]["mcast_group"], "239.17.10.1")

    def test_many_vlans_may_share_one_vrf_base_group(self):
        """Inheritance is the normal case and must not read as a collision."""
        v = vrf("CORP", "199", mcast_group="239.17.10.1")
        vlans = [vlan(n, f"V{n}", role="edge", prefixes=[prefix(f"10.0.{n}.0/24", vrf=v)])
                 for n in (101, 102, 103)]
        result = build_evpn_data(vlans, [], None, None)
        self.assertEqual({r["mcast_group"] for r in result["all_edge_vlans"].values()},
                         {"239.17.10.1"})

    def test_routed_dag_needs_no_group(self):
        v = vrf("CORP", "199", mcast_group=None)
        result = build_evpn_data(
            [vlan(100, "CORP_DATA_CAMPUS", role="edge", tags=["dag_routed"],
                  prefixes=[prefix("10.67.118.128/27", vrf=v)])], [], None, None)
        self.assertIn("100", result["all_edge_vlans"])


class TestMulticastGroupValidation(unittest.TestCase):
    def _fails(self, vlan_list, *fragments, reserved=None):
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data(vlan_list, [], None, None, reserved_mcast_prefixes=reserved)
        msg = str(ctx.exception)
        for f in fragments:
            self.assertIn(f, msg)
        return msg

    def test_bridged_vlan_without_any_group_fails(self):
        """The exact ACM failure: bare 'member vni N mcast-group'."""
        v = vrf("CORP", "199", mcast_group=None)
        self._fails([vlan(101, "CORP_DATA", role="edge",
                          prefixes=[prefix("10.0.1.0/24", vrf=v)])],
                    "101", "bridged", "mcast_base_group")

    def test_vrf_less_l2_only_segment_falls_back_to_ingress_replication(self):
        """A VRF-less L2-only overlay has nothing to inherit a group from, so
        head-end replication is the default rather than an error. Niche case,
        deliberately low-ceremony."""
        result = build_evpn_data(
            [vlan(500, "GUEST", role="edge", prefixes=[prefix("172.16.5.0/24")])],
            [], None, None)
        rec = result["all_edge_vlans"]["500"]
        self.assertEqual(rec["replication_mode"], "ingress")
        self.assertEqual(rec["mcast_group"], "")

    def test_vlan_in_a_vrf_without_a_base_group_still_fails(self):
        """Being in a VRF means multicast is expected; silently downgrading to
        ingress would hide a missing mcast_base_group."""
        v = vrf("CORP", "199", mcast_group=None)
        self._fails([vlan(101, "CORP_DATA", role="edge",
                          prefixes=[prefix("10.0.1.0/24", vrf=v)])],
                    "101", "mcast_base_group")

    def test_non_multicast_address_rejected(self):
        v = vrf("CORP", "199", mcast_group="10.1.1.1")
        self._fails([vlan(101, "X", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
                    "not a multicast address")

    def test_group_outside_admin_scoped_range_rejected(self):
        """232/8 is SSM here ('ip pim ssm default') and cannot serve ASM (*,G)."""
        v = vrf("CORP", "199", mcast_group="232.1.1.1")
        self._fails([vlan(101, "X", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
                    "239.0.0.0/8")

    def test_link_local_multicast_rejected(self):
        v = vrf("CORP", "199", mcast_group="224.0.0.5")
        self._fails([vlan(101, "X", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
                    "239.0.0.0/8")

    def test_garbage_value_rejected(self):
        v = vrf("CORP", "199", mcast_group="not-an-ip")
        self._fails([vlan(101, "X", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
                    "not a valid IP address")

    def test_two_vrfs_sharing_a_base_group_collide(self):
        a = vrf("CORP", "199", mcast_group="239.17.10.1", l3vni=20199)
        b = vrf("GUEST", "250", mcast_group="239.17.10.1", l3vni=20250)
        self._fails([
            vlan(101, "A", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=a)]),
            vlan(102, "B", role="edge", prefixes=[prefix("10.0.2.0/24", vrf=b)]),
        ], "239.17.10.1", "claimed by both")

    def test_vlan_override_colliding_with_another_vrf_base_fails(self):
        a = vrf("CORP", "199", mcast_group="239.17.10.1", l3vni=20199)
        b = vrf("GUEST", "250", mcast_group="239.17.20.1", l3vni=20250)
        self._fails([
            vlan(101, "A", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=a)]),
            vlan(102, "B", role="edge", cfd={"mcast_group": "239.17.10.1"},
                 prefixes=[prefix("10.0.2.0/24", vrf=b)]),
        ], "claimed by both")

    def test_group_inside_a_reserved_mdt_prefix_fails(self):
        v = vrf("CORP", "199", mcast_group="239.100.0.5")
        self._fails(
            [vlan(101, "X", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            "239.100.0.5", "239.100.0.0/16", "default_mdt",
            reserved=[{"prefix": "239.100.0.0/16", "role": "default_mdt"}])

    def test_group_outside_reserved_prefixes_is_accepted(self):
        v = vrf("CORP", "199", mcast_group="239.17.10.1")
        result = build_evpn_data(
            [vlan(101, "X", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            [], None, None,
            reserved_mcast_prefixes=[{"prefix": "239.100.0.0/16", "role": "default_mdt"}])
        self.assertEqual(result["all_edge_vlans"]["101"]["mcast_group"], "239.17.10.1")

    def test_routed_dag_group_not_checked_against_reserved_ranges(self):
        """It is never rendered, so it cannot conflict."""
        v = vrf("CORP", "199", mcast_group="239.100.0.5")
        result = build_evpn_data(
            [vlan(100, "R", role="edge", tags=["dag_routed"],
                  prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            [], None, None,
            reserved_mcast_prefixes=[{"prefix": "239.100.0.0/16", "role": "default_mdt"}])
        self.assertIn("100", result["all_edge_vlans"])

    def test_all_problems_reported_together(self):
        a = vrf("CORP", "199", mcast_group=None, l3vni=20199)
        b = vrf("GUEST", "250", mcast_group="10.0.0.1", l3vni=20250)
        msg = self._fails([
            vlan(101, "A", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=a)]),
            vlan(102, "B", role="edge", prefixes=[prefix("10.0.2.0/24", vrf=b)]),
        ], "101", "102")
        self.assertIn("2 problems", msg)


# ---------------------------------------------------------------------------
# IPAM-driven multicast carve
#
# The allowed/reserved ranges come from NetBox prefix roles rather than being
# hard-coded, so re-carving is a data change. Fixtures below mirror the lab's
# actual IPAM as fetched on 2026-07-26.
# ---------------------------------------------------------------------------

# VRF-major carve: 239.<vrf>.0.0/16 per VRF, functions carved inside it.
LAB_ALLOWED = [{"prefix": "239.3.1.0/24", "role": "l2vni_replication"}]
LAB_RESERVED = [
    {"prefix": "239.3.2.0/24", "role": "default_mdt"},
    {"prefix": "239.3.3.0/24", "role": "data_mdt"},
]


def _build_with_carve(group, allowed=LAB_ALLOWED, reserved=LAB_RESERVED):
    # l3vni 20003 -> multicast index 3 -> block 239.3.0.0/16,
    # matching LAB_ALLOWED / LAB_RESERVED above.
    v = vrf("CORP", "199", mcast_group=group, l3vni=20003)
    return build_evpn_data(
        [vlan(101, "CORP_DATA", role="edge", prefixes=[prefix("10.0.1.0/24", vrf=v)])],
        [], None, None,
        reserved_mcast_prefixes=reserved, allowed_mcast_prefixes=allowed)


class TestIpamDrivenMulticastCarve(unittest.TestCase):
    def test_group_inside_the_allowed_range_is_accepted(self):
        result = _build_with_carve("239.3.1.1")
        self.assertEqual(result["all_edge_vlans"]["101"]["mcast_group"], "239.3.1.1")

    def test_group_outside_every_allowed_range_is_rejected(self):
        """Admin-scoped but not allocated from the carve."""
        with self.assertRaises(AnsibleFilterError) as ctx:
            _build_with_carve("239.9.9.9")
        msg = str(ctx.exception)
        self.assertIn("239.9.9.9", msg)
        self.assertIn("239.3.1.0/24", msg)

    def test_group_inside_a_reserved_mdt_range_is_rejected(self):
        with self.assertRaises(AnsibleFilterError) as ctx:
            _build_with_carve("239.3.3.5", allowed=[])
        self.assertIn("239.3.3.0/24", str(ctx.exception))

    def test_default_mdt_address_itself_is_rejected(self):
        with self.assertRaises(AnsibleFilterError) as ctx:
            _build_with_carve("239.3.2.1", allowed=[])
        self.assertIn("default_mdt", str(ctx.exception))

    def test_containment_skipped_when_no_allowed_prefixes_defined(self):
        """A fabric that has not adopted the carve still renders; only the
        239/8 floor applies."""
        result = _build_with_carve("239.9.9.9", allowed=[])
        self.assertEqual(result["all_edge_vlans"]["101"]["mcast_group"], "239.9.9.9")

    def test_floor_check_still_applies_without_a_carve(self):
        with self.assertRaises(AnsibleFilterError) as ctx:
            _build_with_carve("232.1.1.1", allowed=[], reserved=[])
        self.assertIn("239.0.0.0/8", str(ctx.exception))

    def test_overlapping_roles_are_caught_by_the_reserved_check(self):
        """If an l2vni_replication range is carved so wide that it swallows an
        MDT range, containment passes but the reserved check must still fire —
        which is why both checks exist."""
        wide = [{"prefix": "239.3.0.0/16", "role": "l2vni_replication"}]
        with self.assertRaises(AnsibleFilterError) as ctx:
            _build_with_carve("239.3.3.5", allowed=wide)
        self.assertIn("data_mdt", str(ctx.exception))

    def test_routed_dag_is_exempt_from_the_carve(self):
        """Never rendered, so it cannot conflict."""
        v = vrf("CORP", "199", mcast_group="239.9.9.9", l3vni=20003)
        result = build_evpn_data(
            [vlan(100, "R", role="edge", tags=["dag_routed"],
                  prefixes=[prefix("10.0.1.0/24", vrf=v)])],
            [], None, None,
            reserved_mcast_prefixes=LAB_RESERVED, allowed_mcast_prefixes=LAB_ALLOWED)
        self.assertEqual(result["all_edge_vlans"]["100"]["replication_mode"], "ingress")


if __name__ == "__main__":
    unittest.main()
