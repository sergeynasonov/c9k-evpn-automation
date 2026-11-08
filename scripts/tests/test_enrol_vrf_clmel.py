# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""The CLMEL enrolment, end to end without NetBox.

Two kinds of test.

The first kind is what the SECURITY enrolment taught: an object that already
exists is reconciled, not skipped, and anything that would render something
nobody declared is refused.

The second kind is the one that matters most and that the SECURITY script never
had. Every object the script would POST is turned into the GraphQL shape
netbox_facts.yml fetches, and fed through the role's REAL transform,
build_evpn_data -- the code generate_evpn.yml runs. If an object is missing
(the core VLAN's binding prefix, an upstream_asn, a device tag), the transform
refuses or silently drops something, and these tests say which. That is the
check that "the branch is enough to provision the VRF" actually holds, short of
a live generate.

There is no live NetBox here, so the client is faked.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "roles", "acm_evpn", "filter_plugins"))

import netbox_enrol_vrf_clmel as clmel  # noqa: E402

from netbox_evpn_data import build_evpn_data  # noqa: E402

SG = "3"               # the campus site group the VLAN group is scoped to
VLAN_GROUP_ID = 1
DHCP_IDS = [71, 72]    # VLAN 100's relay destinations, as in the lab


class Ids(dict):
    """Auto-vivifying id map, plus the reverse lookup the GraphQL fake needs."""

    def __missing__(self, key):
        self[key] = 600 + len(self)
        return self[key]

    def slug(self, id_):
        return next(k for k, v in self.items() if v == id_)


class FakeNetBox:
    """Reads answer from fixtures; writes are recorded and given ids."""

    def __init__(self, vlans=(), vrfs=(), dhcp=True, carve=()):
        self.vlans = list(vlans)
        self.vrfs = list(vrfs)
        self.dhcp = dhcp
        self.carve = {c["prefix"]: c for c in carve}
        self.posted = []
        self.patched = {}

    # --- reads ---------------------------------------------------------------
    def one(self, path, branch=True, **params):
        rows = self.get(path, branch=branch, **params)
        return rows[0] if rows else None

    def get(self, path, branch=True, **params):
        if path == "ipam/vlans/":
            out = [v for v in self.vlans if v["vid"] == params.get("vid")]
            if "group_id" in params:
                out = [v for v in out
                       if (v.get("group") or {}).get("id") == params["group_id"]]
            if params.get("vid") == clmel.DHCP_SOURCE_VID and not out:
                out = [{"id": 100, "vid": 100, "name": "CORP_DATA_CAMPUS",
                        "group": {"id": VLAN_GROUP_ID}}]
            return out
        if path == "ipam/prefixes/":
            if "vlan_id" in params:        # VLAN 100's prefixes
                servers = ([{"id": i, "address": "10.66.119.%d/32" % (i - 15)}
                            for i in DHCP_IDS] if self.dhcp else [])
                return [{"prefix": "10.67.118.128/27",
                         "custom_fields": {"dhcp_servers_ipv4": servers}},
                        {"prefix": "2001:db8:0:100::/64", "custom_fields": {}}]
            if params.get("vrf_id") == "null":
                c = self.carve.get(params.get("prefix"))
                return [c] if c else []
            return []
        if path == "ipam/vrfs/":
            if "cf_l3vni" in params:
                return [v for v in self.vrfs
                        if (v.get("custom_fields") or {}).get("l3vni") == params["cf_l3vni"]]
            return [v for v in self.vrfs if v["name"] == params.get("name")]
        if path == "ipam/vlan-groups/":
            return [{"id": VLAN_GROUP_ID}]
        return [{"id": 1}]                 # roles and tags all exist

    # --- writes --------------------------------------------------------------
    def post(self, path, data, branch=True):
        obj = dict(data, id=9000 + len(self.posted))
        self.posted.append((path, obj))
        return obj

    def patch(self, path, data, branch=True):
        self.patched[path] = data
        return {}


def run(nb=None, apply=True):
    nb = nb or FakeNetBox()
    plan = []
    ids = clmel.resolve(nb, plan)
    ids["role"], ids["tag"] = Ids(), Ids()
    clmel.enrol(nb, ids, plan, apply)
    return nb, plan, ids


def entries(plan, kind):
    return [what for k, what in plan if k == kind]


def as_graphql(nb, ids):
    """What netbox_facts.yml would fetch after the branch is merged."""
    by_kind = {}
    for path, obj in nb.posted:
        by_kind.setdefault(path, []).append(obj)
    (vrf_obj,) = by_kind["ipam/vrfs/"]
    vrf = {"name": vrf_obj["name"], "rd": None,
           "custom_field_data": vrf_obj["custom_fields"]}

    vlans = {}
    for v in by_kind["ipam/vlans/"]:
        vlans[v["id"]] = {
            "vid": v["vid"], "name": v["name"],
            "role": {"slug": ids["role"].slug(v["role"])},
            "tags": [{"slug": ids["tag"].slug(t["id"])} for t in v["tags"]],
            "site": None,
            "group": {"scope": {"__typename": "SiteGroupType", "id": SG}},
            "custom_field_data": v.get("custom_fields") or {},
            "prefixes": [],
        }
    carve = []
    for p in by_kind["ipam/prefixes/"]:
        if p.get("vrf") is None:
            if p.get("role"):
                carve.append({"prefix": p["prefix"],
                              "role": ids["role"].slug(p["role"]),
                              "status": p["status"]})
            continue
        if p.get("vlan"):
            vlans[p["vlan"]]["prefixes"].append(
                {"prefix": p["prefix"], "vrf": vrf,
                 "custom_field_data": p.get("custom_fields") or {}})
    ips = [{"id": i, "address": "10.66.119.%d/32" % (i - 15)} for i in DHCP_IDS]
    return list(vlans.values()), ips, carve


def transform(nb, ids, hostname):
    vlan_list, ips, carve = as_graphql(nb, ids)
    return build_evpn_data(
        vlan_list, ips, my_site="51", my_sg=SG,
        reserved_mcast_prefixes=[c for c in carve
                                 if c["role"] in ("default_mdt", "data_mdt")],
        allowed_mcast_prefixes=[c for c in carve
                                if c["role"] == "l2vni_replication"],
        trm_objects={}, hostname=hostname)


class TestTheDeclaredChange(unittest.TestCase):

    def test_the_constants_pass_the_renders_own_rules(self):
        self.assertEqual(clmel.check_declarations(), [])

    def test_a_bad_vlan_name_is_caught_before_netbox(self):
        orig = clmel.VLANS
        try:
            clmel.VLANS = orig + [(402, "NAME WITH SPACES", "edge", [])]
            self.assertTrue(any("402" in p for p in clmel.check_declarations()))
        finally:
            clmel.VLANS = orig

    def test_the_multicast_block_follows_the_l3vni(self):
        self.assertEqual(clmel.MCAST_INDEX, 6)
        self.assertEqual(clmel.MCAST_BASE_GROUP, "239.6.1.1")

    def test_no_trm_the_mdt_ranges_stay_reserved(self):
        """Active default_mdt is what turns TRM on. CLMEL has none."""
        status = {r: s for _p, r, s, _d in clmel.CARVE if r}
        self.assertEqual(status["l2vni_replication"], "active")
        self.assertEqual(status["default_mdt"], "reserved")
        self.assertEqual(status["data_mdt"], "reserved")

    def test_no_vlan_is_multicast_enabled(self):
        for vid, _n, _r, tags in clmel.VLANS:
            self.assertNotIn("multicast_enabled", tags, vid)


class TestPrerequisitesAreResolvedOrRefused(unittest.TestCase):

    def test_dhcp_is_copied_from_vlan_100(self):
        _, plan, ids = run()
        self.assertEqual(sorted(ids["dhcp"]), DHCP_IDS)
        self.assertTrue(any("VLAN 100" in s for s in entries(plan, "source")))

    def test_vlan_100_without_dhcp_blocks(self):
        plan = []
        clmel.resolve(FakeNetBox(dhcp=False), plan)
        self.assertTrue(any("DHCP" in m for m in entries(plan, "MISSING")))

    def test_an_l3vni_held_by_another_vrf_blocks(self):
        nb = FakeNetBox(vrfs=[{"id": 5, "name": "OTHER",
                               "custom_fields": {"l3vni": clmel.L3VNI}}])
        plan = []
        clmel.resolve(nb, plan)
        self.assertTrue(any("OTHER" in b for b in entries(plan, "BLOCKED")))

    def test_a_vid_used_outside_the_campus_group_blocks(self):
        """Same VID, same derived L2VNI: the render would fail the fabric."""
        nb = FakeNetBox(vlans=[{"id": 40, "vid": 400, "name": "LEGACY",
                                "group": None, "site": {"name": "building-a"}}])
        plan = []
        clmel.resolve(nb, plan)
        self.assertTrue(any("400" in b for b in entries(plan, "BLOCKED")))

    def test_an_existing_active_default_mdt_is_a_conflict(self):
        """Someone has already turned TRM on for index 6; not ours to undo."""
        nb = FakeNetBox(carve=[{"prefix": "239.6.2.0/24",
                                "status": {"value": "active"}}])
        _, plan, _ = run(nb)
        self.assertTrue(any("239.6.2.0/24" in c for c in entries(plan, "CONFLICT")))


class TestExistingObjectsAreReconciled(unittest.TestCase):

    def vlan(self, vid, tags=(), role="vrf_l3_handoff_vlan", asn=None):
        return {"id": vid, "vid": vid, "name": "V%d" % vid,
                "group": {"id": VLAN_GROUP_ID},
                "role": {"slug": role} if role else None,
                "custom_fields": {"upstream_asn": asn},
                "tags": [{"id": 500 + i, "slug": t} for i, t in enumerate(tags)]}

    def test_an_untagged_handoff_is_pinned_and_given_its_asn(self):
        nb, plan, ids = run(FakeNetBox(vlans=[self.vlan(497)]))
        body = nb.patched["ipam/vlans/497/"]
        self.assertEqual(body["tags"], [{"id": ids["tag"]["core1"]}])
        self.assertEqual(body["custom_fields"], {"upstream_asn": 64512})

    def test_a_handoff_pinned_to_the_other_border_is_a_conflict(self):
        nb, plan, _ = run(FakeNetBox(vlans=[self.vlan(497, tags=("core2",))]))
        self.assertTrue(any("497" in c for c in entries(plan, "CONFLICT")))
        self.assertNotIn("ipam/vlans/497/", nb.patched)

    def test_an_existing_401_gets_dag_routed(self):
        nb, plan, ids = run(FakeNetBox(vlans=[self.vlan(401, role="edge")]))
        self.assertIn({"id": ids["tag"]["dag_routed"]},
                      nb.patched["ipam/vlans/401/"]["tags"])

    def test_a_dry_run_writes_nothing(self):
        nb, plan, _ = run(apply=False)
        self.assertEqual(nb.posted, [])
        self.assertEqual(nb.patched, {})
        self.assertTrue(entries(plan, "CREATE"))


class TestTheBranchProvisionsTheVrf(unittest.TestCase):
    """Everything the script creates, through the transform generate runs."""

    @classmethod
    def setUpClass(cls):
        cls.nb, cls.plan, cls.ids = run()

    def vrf(self, hostname="access1-1"):
        return transform(self.nb, self.ids, hostname)["vrfs"]["CLMEL"]

    def test_the_vrf_is_enrolled(self):
        self.assertIn("CLMEL", transform(self.nb, self.ids, "access1-1")["vrfs"])

    def test_the_core_vlan_belongs_to_the_vrf(self):
        """Without the 169.254.255.0/30 binding, 499 would be VRF-less and
        dropped as unenrolled -- bridging fine, routing nothing."""
        self.assertEqual(list(self.vrf()["core_vlans"]), ["499"])

    def test_400_is_a_bridged_segment_on_the_vrfs_group(self):
        v = self.vrf()["edge_vlans"]["400"]
        self.assertEqual(v["replication_mode"], "static")
        self.assertEqual(v["mcast_group"], "239.6.1.1")
        self.assertNotIn("dag_routed", v["tags"])

    def test_401_is_a_routed_only_segment(self):
        v = self.vrf()["edge_vlans"]["401"]
        self.assertEqual(v["replication_mode"], "ingress")
        self.assertIn("dag_routed", v["tags"])

    def test_both_edge_vlans_relay_to_vlan_100s_dhcp_servers(self):
        for vid in ("400", "401"):
            self.assertEqual(sorted(self.vrf()["edge_vlans"][vid]["dhcp_servers_ipv4"]),
                             ["10.66.119.56", "10.66.119.57"])

    def test_each_handoff_is_addressed_lowest_upstream_highest_local(self):
        h = self.vrf()["handoff_vlans"]
        self.assertEqual((h["497"]["neighbor_ip_v4"], h["497"]["local_ip_v4"]),
                         ("172.16.0.0", "172.16.0.1/31"))
        self.assertEqual((h["498"]["neighbor_ip_v4"], h["498"]["local_ip_v4"]),
                         ("172.16.0.2", "172.16.0.3/31"))
        self.assertEqual({h[v]["upstream_asn"] for v in h}, {64512})

    def test_each_handoff_is_pinned_to_its_own_border(self):
        h = self.vrf()["handoff_vlans"]
        self.assertIn("core1", h["497"]["tags"])
        self.assertIn("core2", h["498"]["tags"])
        self.assertNotIn("core2", h["497"]["tags"])

    def test_no_trm_is_rendered(self):
        trm = self.vrf().get("trm") or {}
        self.assertFalse(trm.get("enabled"), trm)

    def test_the_border_advertises_both_edge_subnets(self):
        subnets = transform(self.nb, self.ids, "core1")["fabric_edge_subnets"]["CLMEL"]
        self.assertEqual(sorted(s["network"] for s in subnets.values()),
                         ["172.16.1.0", "172.16.2.0"])

    def test_dropping_the_core_binding_is_caught(self):
        """Mutation check: the test above must depend on the binding."""
        orig = clmel.CORE_VLAN_BINDING
        try:
            clmel.CORE_VLAN_BINDING = ("169.254.255.0/30", 499, [], "", False)
            nb, _, ids = run()
            nb.posted = [(p, o) for p, o in nb.posted
                         if o.get("prefix") != "169.254.255.0/30"]
            self.assertNotIn("499", transform(nb, ids, "access1-1")["vrfs"]["CLMEL"]
                             .get("core_vlans", {}))
        finally:
            clmel.CORE_VLAN_BINDING = orig


if __name__ == "__main__":
    unittest.main()
