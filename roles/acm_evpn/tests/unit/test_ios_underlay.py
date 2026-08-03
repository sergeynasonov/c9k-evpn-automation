# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Unit tests for what survives of ios_underlay.py after the ownership inversion.

The module used to classify the whole running config, deciding what to carry
forward so 'acm replace' would not delete it. candidate_config.j2 now preserves
everything the fabric block does not render, so there is nothing to classify and
that code is gone, along with the tests that covered it.

What remains are two transforms the inversion does not subsume: splitting the
hybrid 'router bgp' stanza by ASN, and reading an interface->address map for the
anycast-RP assertions.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/filter_plugins")

from ios_underlay import (  # noqa: E402
    render_vlan_spec,
    underlay_config,
)


# ---------------------------------------------------------------------------
# BGP ownership by ASN
#
# A neighbour whose remote-as is the fabric ASN is an iBGP construct the
# automation renders, so it is stripped here or it would appear twice. A
# neighbour in any other ASN is the device's.
#
# The border-to-border mesh is the case that makes this sharp: it sits in the
# fabric ASN, so "preserve everything not matching the peer group" kept it and
# "strip everything in the fabric ASN" deleted it. It is stripped AND rendered.
# ---------------------------------------------------------------------------

BORDER_BGP = """\
router bgp 64513
 bgp router-id interface Loopback0
 no bgp default ipv4-unicast
 neighbor EVPN-PEERS peer-group
 neighbor EVPN-PEERS remote-as 64513
 neighbor 10.67.118.71 peer-group EVPN-PEERS
 neighbor 10.67.118.65 remote-as 64513
 neighbor 10.67.118.65 update-source Loopback0
 neighbor 10.67.118.2 remote-as 64512
 neighbor 10.67.118.2 fall-over bfd
 timers bgp 10 30
 !
 address-family ipv4
  network 10.67.118.64 mask 255.255.255.255
  neighbor 10.67.118.65 activate
  neighbor 10.67.118.2 activate
  maximum-paths ibgp 4
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor EVPN-PEERS send-community both
 exit-address-family
 !
 address-family ipv4 vrf CORP
  advertise l2vpn evpn
 exit-address-family
!
"""


class TestBgpOwnershipByAsn(unittest.TestCase):
    def setUp(self):
        self.bgp = underlay_config(BORDER_BGP, fabric_asn=64513)["bgp"]
        self.direct = "\n".join(self.bgp["direct"])
        self.af = "\n".join(self.bgp["af_ipv4"])

    def test_asn_captured(self):
        self.assertEqual(self.bgp["asn"], "64513")

    def test_external_asn_neighbour_preserved(self):
        self.assertIn("neighbor 10.67.118.2 remote-as 64512", self.direct)
        self.assertIn("neighbor 10.67.118.2 fall-over bfd", self.direct)

    def test_external_asn_neighbour_preserved_in_address_family(self):
        self.assertIn("neighbor 10.67.118.2 activate", self.af)

    def test_border_mesh_neighbour_stripped(self):
        """Fabric ASN, so border_config.j2 renders it — keeping it here too
        would emit the session twice."""
        self.assertNotIn("10.67.118.65", self.direct)
        self.assertNotIn("10.67.118.65", self.af)

    def test_peer_group_member_stripped(self):
        self.assertNotIn("10.67.118.71", self.direct)

    def test_peer_group_asn_inherited_by_members(self):
        """A member says only 'peer-group X'; its ASN comes from X."""
        self.assertIn("10.67.118.71", self.bgp["fabric_asn_stripped"])

    def test_unrecognised_global_setting_preserved(self):
        """Preserve-unless-known-regenerated still applies to non-neighbour
        lines, so an unfamiliar knob is not silently dropped."""
        self.assertIn("timers bgp 10 30", self.direct)

    def test_regenerated_boilerplate_stripped(self):
        for line in ("bgp router-id", "no bgp default ipv4-unicast"):
            self.assertNotIn(line, self.direct)

    def test_device_values_in_address_family_preserved(self):
        """'maximum-paths ibgp 4' is the device's; the template's hardcoded 2
        must not overwrite it."""
        self.assertIn("maximum-paths ibgp 4", self.af)
        self.assertIn("network 10.67.118.64 mask 255.255.255.255", self.af)

    def test_overlay_address_families_excluded(self):
        self.assertNotIn("send-community", self.af)
        self.assertNotIn("advertise l2vpn evpn", self.af)

    def test_stripped_neighbours_reported_for_verification(self):
        """03_safety_check.yml asserts each of these appears in the candidate;
        without that, stripping deletes the session."""
        self.assertEqual(self.bgp["fabric_asn_stripped"],
                         ["10.67.118.65", "10.67.118.71", "EVPN-PEERS"])

    def test_stanza_asn_used_when_fabric_asn_not_supplied(self):
        bgp = underlay_config(BORDER_BGP)["bgp"]
        self.assertIn("10.67.118.65", bgp["fabric_asn_stripped"])
        self.assertIn("neighbor 10.67.118.2 remote-as 64512", "\n".join(bgp["direct"]))

    def test_different_fabric_asn_flips_ownership(self):
        bgp = underlay_config(BORDER_BGP, fabric_asn=64512)["bgp"]
        self.assertIn("10.67.118.2", bgp["fabric_asn_stripped"])
        self.assertIn("neighbor 10.67.118.65 remote-as 64513", "\n".join(bgp["direct"]))

    def test_custom_peer_group_name(self):
        cfg = ("router bgp 65000\n neighbor FABRIC peer-group\n"
               " neighbor FABRIC remote-as 65000\n neighbor 1.1.1.1 remote-as 2\n!\n")
        bgp = underlay_config(cfg, bgp_peer_group="FABRIC")["bgp"]
        self.assertEqual([l.strip() for l in bgp["direct"]],
                         ["neighbor 1.1.1.1 remote-as 2"])

    def test_no_bgp_at_all(self):
        bgp = underlay_config("hostname x\n!\n")["bgp"]
        self.assertEqual(bgp, {"asn": "", "direct": [], "af_ipv4": [],
                               "fabric_asn_stripped": []})

    def test_indented_separator_does_not_truncate_the_stanza(self):
        """'router bgp' uses an indented '!' between address families; treating
        it as a stanza terminator loses every family after the first."""
        self.assertIn("maximum-paths ibgp 4", self.af)


# ---------------------------------------------------------------------------
# Interface address map — read by the anycast-RP assertions
# ---------------------------------------------------------------------------

class TestInterfaceAddresses(unittest.TestCase):
    CFG = """\
interface Loopback0
 ip address 10.67.118.64 255.255.255.255
!
interface Loopback60000
 ip address 10.67.118.66 255.255.255.255
!
interface GigabitEthernet1/0/5
 switchport mode access
!
"""

    def test_addresses_indexed_by_interface(self):
        addrs = underlay_config(self.CFG)["addresses"]
        self.assertEqual(addrs["Loopback0"], "10.67.118.64")
        self.assertEqual(addrs["Loopback60000"], "10.67.118.66")

    def test_interface_without_an_address_is_absent(self):
        self.assertNotIn("GigabitEthernet1/0/5", underlay_config(self.CFG)["addresses"])

    def test_empty_and_none_inputs(self):
        for text in ("", None):
            result = underlay_config(text)
            self.assertEqual(result["addresses"], {})
            self.assertEqual(result["bgp"]["direct"], [])


# ---------------------------------------------------------------------------
# VLAN list rendering (the ios_vlan_range filter)
#
# IOS re-renders every VLAN list sorted and range-collapsed, so a template
# emitting '101,100' is rewritten to '100-101' on the device and the config
# never converges.
# ---------------------------------------------------------------------------

class TestVlanSpecRendering(unittest.TestCase):
    def test_sorted(self):
        self.assertEqual(render_vlan_spec([101, 100]), "100-101")

    def test_contiguous_collapsed_to_range(self):
        self.assertEqual(render_vlan_spec([100, 101, 102]), "100-102")

    def test_mixed_ranges_and_singles(self):
        self.assertEqual(render_vlan_spec([100, 101, 150, 200, 201, 202]),
                         "100-101,150,200-202")

    def test_strings_are_coerced(self):
        self.assertEqual(render_vlan_spec(["101", "100"]), "100-101")

    def test_duplicates_removed(self):
        self.assertEqual(render_vlan_spec([100, 100, 101]), "100-101")

    def test_single_vlan(self):
        self.assertEqual(render_vlan_spec([100]), "100")

    def test_empty_list(self):
        self.assertEqual(render_vlan_spec([]), "")


if __name__ == "__main__":
    unittest.main()
