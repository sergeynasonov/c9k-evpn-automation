# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Unit tests for the acm_evpn deploy blast-radius guard.

Run from the repo root:
    venv/bin/python -m unittest discover -s roles/acm_evpn/tests/unit
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/filter_plugins")

from ios_config_census import (  # noqa: E402
    UNDERLAY_CLASSES,
    ios_config_census,
    ios_config_census_delta,
    ios_config_census_violations,
)


# A representative slice of a rendered fabric config: two tenant VRFs, the
# always-present Mgmt-vrf, SVIs, EVPN instances and NVE members.
FABRIC = """\
hostname access1-1
!
vrf definition CORP
 rd 10.67.118.71:20199
!
vrf definition GUEST
 rd 10.67.118.71:20250
!
vrf definition Mgmt-vrf
 !
!
vlan configuration 199
 member vni 20199
!
l2vpn evpn instance 101 vlan-based
 encapsulation vxlan
!
l2vpn evpn instance 250 vlan-based
 encapsulation vxlan
!
interface Vlan1
 shutdown
!
interface Vlan101
 vrf forwarding CORP
!
interface Vlan250
 vrf forwarding GUEST
!
interface nve1
 member vni 20199 vrf CORP
 member vni 20250 vrf GUEST
 member vni 10101 ingress-replication
!
end
"""

# What a NetBox failure produces: baseline config, no fabric objects at all.
EMPTY = """\
hostname access1-1
!
vrf definition Mgmt-vrf
 !
!
interface Vlan1
 shutdown
!
interface nve1
 no shutdown
!
end
"""


class TestCensus(unittest.TestCase):
    def test_counts_tenant_vrfs(self):
        self.assertEqual(ios_config_census(FABRIC)["vrfs"], ["CORP", "GUEST"])

    def test_mgmt_vrf_is_excluded(self):
        """Mgmt-vrf is always present and not fabric-managed; counting it
        would dilute every percentage."""
        self.assertNotIn("Mgmt-vrf", ios_config_census(FABRIC)["vrfs"])

    def test_counts_svis(self):
        self.assertEqual(ios_config_census(FABRIC)["svis"], ["Vlan1", "Vlan101", "Vlan250"])

    def test_counts_evpn_instances(self):
        self.assertEqual(ios_config_census(FABRIC)["evpn_instances"], ["101", "250"])

    def test_vni_members_are_deduplicated_across_stanzas(self):
        """20199 appears under both 'vlan configuration' and 'interface nve1';
        the census is a set of identifiers, not a line count."""
        self.assertEqual(ios_config_census(FABRIC)["vni_members"], ["10101", "20199", "20250"])

    def test_member_evpn_instance_form_not_counted_as_vni_member(self):
        cfg = "vlan configuration 101\n member evpn-instance 101 vni 10101\n!\n"
        self.assertEqual(ios_config_census(cfg)["vni_members"], [])

    def test_empty_and_none_are_safe(self):
        for text in ("", None):
            census = ios_config_census(text)
            self.assertEqual(census["vrfs"], [])
            self.assertEqual(census["svis"], [])


class TestCensusDelta(unittest.TestCase):
    def test_identical_config_removes_nothing(self):
        delta = ios_config_census_delta(FABRIC, FABRIC)
        for name, d in delta.items():
            self.assertEqual(d["removed_count"], 0, name)
            self.assertEqual(d["removed_pct"], 0.0, name)

    def test_total_wipe_is_scored_100_percent(self):
        """The exact scenario the guard exists for: NetBox returns nothing, so
        the candidate keeps only the baseline and every fabric object goes."""
        delta = ios_config_census_delta(FABRIC, EMPTY)
        self.assertEqual(delta["vrfs"]["before"], 2)
        self.assertEqual(delta["vrfs"]["after"], 0)
        self.assertEqual(delta["vrfs"]["removed"], ["CORP", "GUEST"])
        self.assertEqual(delta["vrfs"]["removed_pct"], 100.0)
        self.assertEqual(delta["evpn_instances"]["removed_pct"], 100.0)
        self.assertEqual(delta["vni_members"]["removed_pct"], 100.0)

    def test_partial_removal_percentage(self):
        one_vrf = FABRIC.replace("vrf definition GUEST\n rd 10.67.118.71:20250\n!\n", "")
        delta = ios_config_census_delta(FABRIC, one_vrf)
        self.assertEqual(delta["vrfs"]["removed"], ["GUEST"])
        self.assertEqual(delta["vrfs"]["removed_pct"], 50.0)

    def test_growth_is_never_scored_as_removal(self):
        """Adding VRFs/VLANs is the normal case and must never trip the guard."""
        delta = ios_config_census_delta(EMPTY, FABRIC)
        for name, d in delta.items():
            self.assertEqual(d["removed_count"], 0, name)
            self.assertEqual(d["removed_pct"], 0.0, name)

    def test_greenfield_switch_does_not_trip_guard(self):
        """Nothing to lose: every class is empty beforehand, so no division by
        zero and no false positive on a brand new switch."""
        delta = ios_config_census_delta("", FABRIC)
        for name, d in delta.items():
            self.assertEqual(d["removed_pct"], 0.0, name)

    def test_svi_removal_detected(self):
        fewer = FABRIC.replace("interface Vlan250\n vrf forwarding GUEST\n!\n", "")
        delta = ios_config_census_delta(FABRIC, fewer)
        self.assertIn("Vlan250", delta["svis"]["removed"])

    def test_delta_shape_is_stable_for_the_task_layer(self):
        """The task layer renders v.klass/v.before/v.after/v.removed*, so every
        class must always be present and carry these keys."""
        delta = ios_config_census_delta(FABRIC, EMPTY)
        self.assertEqual(
            set(delta),
            {"vrfs", "svis", "evpn_instances", "vni_members", *UNDERLAY_CLASSES},
        )
        for d in delta.values():
            self.assertEqual(
                set(d), {"before", "after", "removed", "removed_count", "removed_pct"}
            )
            self.assertIsInstance(d["removed_pct"], float)


class TestThresholdSemantics(unittest.TestCase):
    """Mirrors the comparison the task layer performs, so the default policy
    is covered by a test rather than only by a Jinja expression."""

    DEFAULT_PCT = 50.0

    def _violations(self, before, after, threshold=DEFAULT_PCT):
        delta = ios_config_census_delta(before, after)
        return sorted(k for k, d in delta.items() if d["removed_pct"] > threshold)

    def test_wipe_violates_every_class(self):
        # svis included: Vlan101/Vlan250 go, only the baseline Vlan1 survives
        # (2 of 3 removed = 66.7%).
        self.assertEqual(
            self._violations(FABRIC, EMPTY),
            ["evpn_instances", "svis", "vni_members", "vrfs"],
        )

    def test_fifty_percent_exactly_does_not_violate(self):
        """Threshold is strictly greater-than, so losing one of two VRFs is
        allowed at the default setting."""
        one_vrf = FABRIC.replace("vrf definition GUEST\n rd 10.67.118.71:20250\n!\n", "")
        self.assertNotIn("vrfs", self._violations(FABRIC, one_vrf))

    def test_no_change_never_violates(self):
        self.assertEqual(self._violations(FABRIC, FABRIC), [])



# ---------------------------------------------------------------------------
# Underlay: zero tolerance
# ---------------------------------------------------------------------------

UNDERLAY_RUNNING = """\
interface Loopback0
 ip address 10.67.118.71 255.255.255.255
!
interface Loopback1
 ip address 10.67.118.90 255.255.255.255
!
interface TenGigabitEthernet1/1/1
 ip address 10.67.118.130 255.255.255.252
!
interface TenGigabitEthernet1/1/2
 ip address 10.67.118.134 255.255.255.252
!
router ospf 1
 router-id 10.67.118.71
!
"""


class TestUnderlayZeroTolerance(unittest.TestCase):
    def test_underlay_interfaces_are_censused(self):
        census = ios_config_census(UNDERLAY_RUNNING)
        self.assertEqual(
            census["underlay_interfaces"],
            ["Loopback0", "Loopback1", "TenGigabitEthernet1/1/1", "TenGigabitEthernet1/1/2"],
        )
        self.assertEqual(census["ospf_processes"], ["ospf 1"])

    def test_single_underlay_interface_loss_blocks_despite_tiny_percentage(self):
        """25% loss is far under the 50% threshold, but one uplink can be the
        one carrying the management session."""
        after = UNDERLAY_RUNNING.replace(
            "interface TenGigabitEthernet1/1/2\n ip address 10.67.118.134 255.255.255.252\n!\n", "")
        v = ios_config_census_violations(UNDERLAY_RUNNING, after, max_removal_pct=50.0)
        klasses = [x["klass"] for x in v]
        self.assertIn("underlay_interfaces", klasses)
        entry = next(x for x in v if x["klass"] == "underlay_interfaces")
        self.assertEqual(entry["removed"], ["TenGigabitEthernet1/1/2"])
        self.assertTrue(entry["underlay"])
        self.assertLess(entry["removed_pct"], 50.0)

    def test_losing_ospf_process_blocks(self):
        after = UNDERLAY_RUNNING.replace("router ospf 1\n router-id 10.67.118.71\n!\n", "")
        klasses = [x["klass"] for x in ios_config_census_violations(UNDERLAY_RUNNING, after)]
        self.assertIn("ospf_processes", klasses)

    def test_underlay_preserved_means_no_violation(self):
        self.assertEqual(ios_config_census_violations(UNDERLAY_RUNNING, UNDERLAY_RUNNING), [])

    def test_adding_underlay_is_not_a_violation(self):
        self.assertEqual(ios_config_census_violations("", UNDERLAY_RUNNING), [])

    def test_underlay_violations_sort_first(self):
        after = "interface Loopback0\n ip address 10.67.118.71 255.255.255.255\n!\n"
        v = ios_config_census_violations(UNDERLAY_RUNNING, after)
        self.assertTrue(v[0]["underlay"], "underlay must be reported first")

    def test_every_interface_is_tracked_now(self):
        """Post-inversion the candidate contains everything the device has, so
        there is no exclusion concept: ANY interface disappearing is a bug in
        the strip rather than an intended change, and blocks the deploy."""
        running = UNDERLAY_RUNNING + "interface Loopback60000\n ip address 10.67.118.66 255.255.255.255\n!\n"
        v = ios_config_census_violations(running, UNDERLAY_RUNNING)
        entry = next(x for x in v if x["klass"] == "underlay_interfaces")
        self.assertEqual(entry["removed"], ["Loopback60000"])
        self.assertTrue(entry["underlay"])

    def test_switchports_are_tracked_too(self):
        """Previously underlay_interfaces meant 'global table + addressed', so
        an access port vanishing was invisible to the guard."""
        running = UNDERLAY_RUNNING + "interface GigabitEthernet1/0/5\n switchport mode access\n!\n"
        v = ios_config_census_violations(running, UNDERLAY_RUNNING)
        entry = next(x for x in v if x["klass"] == "underlay_interfaces")
        self.assertIn("GigabitEthernet1/0/5", entry["removed"])

    def test_overlay_loss_still_uses_percentage(self):
        v = ios_config_census_violations(FABRIC, EMPTY)
        vrfs = next(x for x in v if x["klass"] == "vrfs")
        self.assertFalse(vrfs["underlay"])
        self.assertIn("threshold", vrfs["reason"])


if __name__ == "__main__":
    unittest.main()
