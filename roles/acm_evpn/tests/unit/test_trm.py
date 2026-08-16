# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Unit tests for TRM (tenant routed multicast) resolution.

Run from the repo root:
    venv/bin/python -m unittest discover -s roles/acm_evpn/tests/unit
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/filter_plugins")

from ansible.errors import AnsibleFilterError  # noqa: E402
from netbox_evpn_data import (  # noqa: E402
    build_evpn_data,
    _build_trm,
)

# Mirrors the lab: CORP l3vni 20003 -> 239.3.x anycast, MEDICAL 20004 -> 239.4.x
# external. Both carve ranges Active; a third VRF's range left Reserved.
MDT = [
    {"prefix": "239.3.2.0/24", "role": "default_mdt", "status": "active"},
    {"prefix": "239.3.3.0/24", "role": "data_mdt", "status": "active"},
    {"prefix": "239.4.2.0/24", "role": "default_mdt", "status": "active"},
    {"prefix": "239.4.3.0/24", "role": "data_mdt", "status": "active"},
    {"prefix": "239.5.2.0/24", "role": "default_mdt", "status": "reserved"},
]
TRM_OBJECTS = {
    "pools": [{"prefix": "10.67.118.32/28", "status": "active",
               "vrf": "MEDICAL", "tags": ["trm_register_source"]}],
    "addresses": [
        {"address": "10.67.118.31/32", "vrf": "CORP",
         "tags": ["trm_anycast_rp"], "description": "", "device": ""},
        {"address": "10.67.118.0/32", "vrf": "MEDICAL",
         "tags": ["trm_external_rp"], "description": "", "device": ""},
        {"address": "10.67.118.33/32", "vrf": "MEDICAL",
         "tags": ["trm_register_source"], "description": "",
         "device": "access1-1"},
    ],
}


def trm(vrf, l3vni, host="access1-1", has_vlans=True, mdt=None, objs=None):
    errors = []
    out = _build_trm(vrf, l3vni, 20000, mdt if mdt is not None else MDT,
                     objs if objs is not None else TRM_OBJECTS,
                     host, has_vlans, errors)
    return out, errors


class TestEnablement(unittest.TestCase):
    def test_active_default_mdt_enables_the_vrf(self):
        out, errors = trm("CORP", 20003)
        self.assertTrue(out["enabled"])
        self.assertEqual(errors, [])

    def test_reserved_default_mdt_does_not_enable(self):
        """The carve reserves a range for every VRF; status is what separates a
        plan from a decision. Reserved must not switch TRM on."""
        out, errors = trm("SPARE", 20005, has_vlans=False)
        self.assertFalse(out["enabled"])
        self.assertEqual(errors, [])

    def test_group_is_the_first_address_of_the_range(self):
        self.assertEqual(trm("CORP", 20003)[0]["mdt_default_group"], "239.3.2.1")

    def test_data_mdt_yields_network_and_wildcard(self):
        out, _ = trm("CORP", 20003)
        self.assertEqual(out["mdt_data_network"], "239.3.3.0")
        self.assertEqual(out["mdt_data_wildcard"], "0.0.0.255")

    def test_data_mdt_is_optional(self):
        mdt = [p for p in MDT if p["role"] != "data_mdt"]
        out, errors = trm("CORP", 20003, mdt=mdt)
        self.assertTrue(out["enabled"])
        self.assertNotIn("mdt_data_network", out)
        self.assertEqual(errors, [])

    def test_two_active_default_mdt_prefixes_fail(self):
        mdt = MDT + [{"prefix": "239.3.2.0/25", "role": "default_mdt",
                      "status": "active"}]
        out, errors = trm("CORP", 20003, mdt=mdt)
        self.assertFalse(out["enabled"])
        self.assertIn("exactly one is required", errors[0])

    def test_default_mdt_outside_the_vrfs_own_block_is_ignored(self):
        """239.9.x cannot belong to the VRF whose index is 3."""
        mdt = [{"prefix": "239.9.2.0/24", "role": "default_mdt", "status": "active"}]
        out, _ = trm("CORP", 20003, has_vlans=False, mdt=mdt)
        self.assertFalse(out["enabled"])

    def test_ipv6_mdt_prefix_is_ignored(self):
        """The MDT is an underlay construct and stays IPv4 even once the
        overlay carries IPv6."""
        mdt = [{"prefix": "ff05::/16", "role": "default_mdt", "status": "active"}]
        out, _ = trm("CORP", 20003, has_vlans=False, mdt=mdt)
        self.assertFalse(out["enabled"])


class TestRpMode(unittest.TestCase):
    def test_anycast_mode(self):
        out, errors = trm("CORP", 20003)
        self.assertEqual(out["rp_mode"], "anycast")
        self.assertEqual(out["anycast_rp"], "10.67.118.31")
        self.assertNotIn("register_source", out)
        self.assertEqual(errors, [])

    def test_external_mode_uses_the_allocated_register_source(self):
        out, errors = trm("MEDICAL", 20004)
        self.assertEqual(out["rp_mode"], "external")
        self.assertEqual(out["external_rps"], ["10.67.118.0"])
        self.assertEqual(out["register_source"], "10.67.118.33")
        self.assertEqual(errors, [])

    def test_external_mode_without_an_allocation_fails_and_names_the_playbook(self):
        out, errors = trm("MEDICAL", 20004, host="dist3-2")
        self.assertFalse(out["enabled"])
        self.assertIn("trm_allocate.yml", errors[0])

    def test_register_source_of_another_host_is_not_used(self):
        """Ownership is the interface the address is assigned to -- NetBox's
        own way of binding an address to a device. An allocation belonging to
        access1-1 must never be rendered on core1."""
        _, errors = trm("MEDICAL", 20004, host="core1")
        self.assertTrue(errors and "register source" in errors[0])

    def test_both_rp_modes_is_a_data_error(self):
        objs = dict(TRM_OBJECTS)
        objs["addresses"] = TRM_OBJECTS["addresses"] + [
            {"address": "10.9.9.9/32", "vrf": "CORP",
             "tags": ["trm_external_rp"], "description": ""}]
        out, errors = trm("CORP", 20003, objs=objs)
        self.assertFalse(out["enabled"])
        self.assertIn("mutually exclusive", errors[0])

    def test_no_rp_is_ssm_only_not_an_error(self):
        """'ip pim vrf X ssm default' renders regardless, so 232/8 works with
        no RP at all. Only ASM needs one."""
        objs = {"pools": [], "addresses": []}
        out, errors = trm("CORP", 20003, objs=objs)
        self.assertEqual(out["rp_mode"], "ssm-only")
        self.assertTrue(out["enabled"])
        self.assertEqual(errors, [])

    def test_multiple_external_rps_are_all_returned(self):
        objs = dict(TRM_OBJECTS)
        objs["addresses"] = TRM_OBJECTS["addresses"] + [
            {"address": "10.8.8.8/32", "vrf": "MEDICAL",
             "tags": ["trm_external_rp"], "description": ""}]
        out, _ = trm("MEDICAL", 20004, objs=objs)
        self.assertEqual(out["external_rps"], ["10.67.118.0", "10.8.8.8"])


class TestRegisterSourceOptOut(unittest.TestCase):
    """trm_allocate.yml is the one caller that legitimately runs against
    un-allocated VRFs -- it exists to fix that state. Everything else must
    refuse, because rendering 'register-source' for an address that does not
    exist would silently break PIM registration rather than failing loudly."""

    def _run(self, require):
        errors = []
        out = _build_trm("MEDICAL", 20004, 20000, MDT, TRM_OBJECTS,
                         "dist3-2", True, errors, require)
        return out, errors

    def test_rendering_refuses_when_no_source_is_allocated(self):
        out, errors = self._run(True)
        self.assertFalse(out["enabled"])
        self.assertEqual(len(errors), 1)
        self.assertIn("trm_allocate.yml", errors[0])

    def test_allocator_sees_the_vrf_and_is_told_what_is_missing(self):
        out, errors = self._run(False)
        self.assertEqual(errors, [])
        self.assertTrue(out["enabled"])
        self.assertEqual(out["rp_mode"], "external")
        self.assertTrue(out["needs_register_source"])
        self.assertNotIn("register_source", out)

    def test_an_allocated_host_never_reports_needing_one(self):
        errors = []
        out = _build_trm("MEDICAL", 20004, 20000, MDT, TRM_OBJECTS,
                         "access1-1", True, errors, False)
        self.assertEqual(out["register_source"], "10.67.118.33")
        self.assertFalse(out.get("needs_register_source", False))
        self.assertEqual(errors, [])

    def test_the_opt_out_does_not_weaken_other_validation(self):
        """Only the register-source rule is relaxed. A VRF with both RP modes is
        still a data error even for the allocator."""
        objs = dict(TRM_OBJECTS)
        objs["addresses"] = TRM_OBJECTS["addresses"] + [
            {"address": "10.9.9.9/32", "vrf": "MEDICAL",
             "tags": ["trm_anycast_rp"], "description": ""}]
        errors = []
        _build_trm("MEDICAL", 20004, 20000, MDT, objs, "dist3-2", True,
                   errors, False)
        self.assertTrue(errors and "mutually exclusive" in errors[0])


class TestMulticastVlanTagging(unittest.TestCase):
    def test_tag_in_a_non_enabled_vrf_is_a_hard_failure(self):
        out, errors = trm("SPARE", 20005, has_vlans=True)
        self.assertFalse(out["enabled"])
        self.assertIn("not multicast-enabled", errors[0])

    def test_fabric_wide_scan_finds_the_vrf(self):
        """Handoff PIM is decided fabric-wide: a border carries VRF traffic for
        VLANs that are never local to it."""
        vlans = [{
            "vid": 200, "role": {"slug": "edge"},
            "tags": [{"slug": "multicast_enabled"}],
            "prefixes": [{"prefix": "10.66.119.128/27", "vrf": {"name": "MEDICAL"}}],
        }]
        self.assertEqual(_mcast_vrfs(vlans), {"MEDICAL"})

    def test_tag_on_a_non_edge_vlan_is_ignored(self):
        vlans = [{
            "vid": 98, "role": {"slug": "vrf_l3_handoff_vlan"},
            "tags": [{"slug": "multicast_enabled"}],
            "prefixes": [{"prefix": "10.0.0.0/31", "vrf": {"name": "CORP"}}],
        }]
        self.assertEqual(_mcast_vrfs(vlans), set())

    def test_dag_routed_and_multicast_enabled_coexist(self):
        """The lab's VLAN 100 carries both. They do not interact: BUM uses
        ingress replication while L3 multicast rides the MDT."""
        vlans = [{
            "vid": 100, "role": {"slug": "edge"},
            "tags": [{"slug": "dag_routed"}, {"slug": "multicast_enabled"}],
            "prefixes": [{"prefix": "10.67.118.128/27", "vrf": {"name": "CORP"}}],
        }]
        self.assertEqual(_mcast_vrfs(vlans), {"CORP"})


# A VLAN must be scoped to be enrolled at all, so these fixtures carry a site
# and the rendering host belongs to it.
SITE = "51"
SG = "3"


def _mcast_vrfs(vlans):
    """Which VRFs the transform counts as holding a multicast-enabled edge VLAN.

    This was a standalone function taking the raw VLAN list. It is now derived
    inside build_evpn_data's main loop, because that is the only place the
    VLAN's FABRIC is known -- and scanning the raw list meant a second fabric's
    VRFs were judged against this run. Driving the transform is therefore the
    only honest way to test it.

    Read back from the per-VRF TRM block rather than asserted directly: a VRF
    whose 'has_mcast_vlans' is true is exactly one the scan found.
    """
    scoped = []
    for v in vlans:
        v = dict(v, site={"id": SITE}, custom_field_data=v.get("custom_field_data", {}),
                 group=None, name=v.get("name", "VLAN%s" % v["vid"]))
        v["prefixes"] = [dict(p, custom_field_data=p.get("custom_field_data", {}),
                              vrf=dict(p["vrf"], rd="1:1",
                                       custom_field_data={"l3vni": 20003,
                                                          "mcast_base_group": "239.3.1.1"}))
                         for p in v.get("prefixes", [])]
        # A handoff VLAN needs an upstream ASN or a different validator fires
        # first and the case under test never runs.
        if (v.get("role") or {}).get("slug") == "vrf_l3_handoff_vlan":
            v["custom_field_data"] = dict(v["custom_field_data"],
                                          upstream_asn=65000)
        scoped.append(v)
    data = build_evpn_data(scoped, [], SITE, SG, reserved_mcast_prefixes=MDT,
                           trm_objects=TRM_OBJECTS, hostname="access1-1")
    return {name for name, entry in data["vrfs"].items()
            if (entry.get("trm") or {}).get("has_mcast_vlans")}


class TestEndToEnd(unittest.TestCase):
    """Through build_evpn_data, so the wiring is covered as well as the helper."""

    VLANS = [
        {"vid": 100, "name": "CORP_DATA_CAMPUS", "role": {"slug": "edge"},
         "tags": [{"slug": "dag_routed"}, {"slug": "multicast_enabled"}],
         "site": {"id": SITE}, "custom_field_data": {}, "group": None,
         "prefixes": [{"prefix": "10.67.118.128/27", "custom_field_data": {},
                       "vrf": {"name": "CORP", "rd": "1:1",
                               "custom_field_data": {"l3vni": 20003,
                                                     "mcast_base_group": "239.3.1.1"}}}]},
    ]

    def test_vrf_entry_carries_a_trm_block(self):
        data = build_evpn_data(self.VLANS, [], SITE, SG,
                               reserved_mcast_prefixes=MDT,
                               trm_objects=TRM_OBJECTS, hostname="access1-1")
        trm_block = data["vrfs"]["CORP"]["trm"]
        self.assertTrue(trm_block["enabled"])
        self.assertEqual(trm_block["rp_mode"], "anycast")
        self.assertEqual(trm_block["mdt_default_group"], "239.3.2.1")
        self.assertTrue(trm_block["has_mcast_vlans"])

    def test_fabric_that_has_not_adopted_trm_renders_as_before(self):
        """No MDT prefixes, no tags: every VRF comes back disabled and nothing
        fails. This is the state every existing fabric is in."""
        vlans = [dict(self.VLANS[0], tags=[{"slug": "dag_routed"}])]
        data = build_evpn_data(vlans, [], SITE, SG)
        self.assertFalse(data["vrfs"]["CORP"]["trm"]["enabled"])

    def test_multicast_tag_without_an_active_mdt_fails_the_render(self):
        """The tag is a promise the data has to keep -- silently non-functional
        multicast is worse than a blocked render. The VLAN keeps its
        multicast_enabled tag here but no MDT prefix is supplied."""
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data(self.VLANS, [], SITE, SG)
        self.assertIn("not multicast-enabled", str(ctx.exception))

    def test_multicast_tag_in_an_unenrolled_vrf_fails_the_render(self):
        vlans = [dict(self.VLANS[0])]
        vlans[0] = dict(vlans[0])
        vlans[0]["prefixes"] = [{"prefix": "10.0.0.0/27", "custom_field_data": {},
                                 "vrf": {"name": "NOTFABRIC", "rd": "1:1",
                                         "custom_field_data": {"l3vni": None}}}]
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data(vlans, [], SITE, SG, reserved_mcast_prefixes=MDT,
                            trm_objects=TRM_OBJECTS, hostname="access1-1")
        self.assertIn("not enrolled in the fabric", str(ctx.exception))


class TestAnotherFabricsVrfIsNotJudgedAgainstThisRun(unittest.TestCase):
    """The regression, straight off a live run.

    The multicast-VRF scan read the RAW VLAN list -- every VLAN in NetBox, not
    every VLAN in this fabric -- while the VRF registry it was compared against
    was scoped to the rendering host's fabric. With one fabric those are the
    same set. With two they are not, so the other fabric's VRFs came out of the
    subtraction and were reported against this run as unenrolled.

    The diagnosis was wrong as well as the finding: the VRF was enrolled, with a
    perfectly good l3vni, just in the fabric that was not being rendered.
    """

    OTHER_SITE = "77"

    # This run's fabric: one multicast-enabled VLAN in an enrolled VRF.
    MINE = {
        "vid": 201, "name": "VAL_A_BR1", "role": {"slug": "edge"},
        "tags": [{"slug": "multicast_enabled"}],
        "site": {"id": SITE}, "custom_field_data": {}, "group": None,
        "prefixes": [{"prefix": "10.201.0.0/24", "custom_field_data": {},
                      "vrf": {"name": "VAL_A", "rd": "1:1",
                              "custom_field_data": {"l3vni": 20003,
                                                    "mcast_base_group": "239.3.1.1"}}}],
    }
    # Another fabric entirely, with its own enrolled VRF and its own tagged VLAN.
    THEIRS = {
        "vid": 100, "name": "CORP_DATA", "role": {"slug": "edge"},
        "tags": [{"slug": "multicast_enabled"}],
        "site": {"id": OTHER_SITE}, "custom_field_data": {}, "group": None,
        "prefixes": [{"prefix": "10.100.0.0/24", "custom_field_data": {},
                      "vrf": {"name": "CORP", "rd": "1:1",
                              "custom_field_data": {"l3vni": 20009,
                                                    "mcast_base_group": "239.9.1.1"}}}],
    }
    SCOPE_MAP = {"site:%s" % SITE: "fabric_mine",
                 "site:%s" % OTHER_SITE: "fabric_theirs"}

    def test_the_other_fabrics_vrf_does_not_fail_this_render(self):
        data = build_evpn_data(
            [self.MINE, self.THEIRS], [], SITE, SG,
            reserved_mcast_prefixes=MDT + [{"prefix": "239.9.2.0/24",
                                            "role": "default_mdt",
                                            "status": "active"}],
            trm_objects={"pools": [], "addresses": [
                {"address": "10.67.118.31/32", "vrf": "VAL_A",
                 "tags": ["trm_anycast_rp"], "description": "", "device": ""}]},
            hostname="leaf1", fabric_scope_map=self.SCOPE_MAP,
            my_fabric="fabric_mine")
        self.assertIn("VAL_A", data["vrfs"])
        self.assertNotIn("CORP", data["vrfs"],
                         "the other fabric's VRF is out of scope for this run")

    def test_an_unenrolled_vrf_in_THIS_fabric_still_fails(self):
        """Scoping the scan must not blunt the check it exists for."""
        bad = dict(self.MINE, vid=202, name="VAL_A_BAD")
        bad["prefixes"] = [{"prefix": "10.202.0.0/24", "custom_field_data": {},
                            "vrf": {"name": "NOTFABRIC", "rd": "1:1",
                                    "custom_field_data": {"l3vni": None}}}]
        with self.assertRaises(AnsibleFilterError) as ctx:
            build_evpn_data([self.MINE, bad], [], SITE, SG,
                            reserved_mcast_prefixes=MDT,
                            trm_objects=TRM_OBJECTS, hostname="leaf1",
                            fabric_scope_map=self.SCOPE_MAP,
                            my_fabric="fabric_mine")
        self.assertIn("NOTFABRIC", str(ctx.exception))
        self.assertIn("not enrolled in the fabric", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
