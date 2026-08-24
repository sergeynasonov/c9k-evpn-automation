# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Enrolling a VLAN that already exists in NetBox.

The script skipped it. That is how the SECURITY enrolment shipped half-done:
397, 398, 399 and 300 were already present un-enrolled, so the script recorded
their ids and moved on, and every tag in its table was written only on CREATE.

The consequences were silent and different per VLAN. Without a device tag a
handoff renders on NO border -- 'inventory_hostname in vlan.tags' is false
everywhere -- so core1 came up with no VLAN 397, no interface Vlan397 and no
eBGP peering, and the teardown diff afterwards had nothing to remove. Without
'dag_routed' and 'multicast_enabled' VLAN 300 is neither an anycast-gateway
segment nor part of the TRM carve.

Nothing failed, nothing warned, and the plan said 'exists'.

There is no live NetBox here, so the client is faked: 'one' answers with the
un-enrolled objects and 'patch' records what would be written.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import netbox_enrol_vrf_security as enrol_mod  # noqa: E402


class FakeNetBox:
    """Answers reads from a fixture and records writes."""

    def __init__(self, vlans):
        self._vlans = {v["vid"]: v for v in vlans}
        self.patched = {}
        self.posted = []

    def one(self, path, branch=True, **params):
        if path == "ipam/vlans/":
            return self._vlans.get(params.get("vid"))
        # NetBox reads a missing vrf_id as "the GLOBAL table", so a query built
        # with vrf_id=None answers about global objects. 169.254.255.0/30 and
        # 10.67.118.0/32 both exist there -- other VRFs' core bindings and the
        # underlay RP -- so an unguarded lookup finds them and reports the VRF's
        # own objects as already present. Modelled here, or the guard against it
        # cannot be tested: without this the fake answers None either way and a
        # missing guard looks identical to a working one.
        if path in ("ipam/prefixes/", "ipam/ip-addresses/"):
            return {"id": 1} if params.get("vrf_id") is None else None
        return None          # nothing else exists, so it is planned as CREATE

    def post(self, path, data, branch=True):
        self.posted.append((path, data))
        return {"id": 9000 + len(self.posted)}

    def patch(self, path, data, branch=True):
        self.patched[path] = data
        return {}


def vlan(vid, name, tags=(), role="vrf_l3_handoff_vlan", asn=None):
    return {"id": vid, "vid": vid, "name": name,
            "role": {"slug": role} if role else None,
            "custom_fields": {"upstream_asn": asn},
            "tags": [{"id": 500 + i, "slug": t} for i, t in enumerate(tags)]}


class Ids(dict):
    """Auto-vivifying id map: the script resolves roles and tags from several
    tables, and a fixture that lists them by hand goes stale silently."""

    def __missing__(self, key):
        self[key] = 600 + len(self)
        return self[key]


def run(vlans, apply=True, vrf={"id": 3, "name": "SECURITY"}):
    ids = {"role": Ids(), "tag": Ids(), "vlan_group": 1, "dhcp": 2,
           "vrf": vrf}
    nb, plan = FakeNetBox(vlans), []
    enrol_mod.enrol(nb, ids, plan, apply)
    return nb, plan, ids


def entries(plan, kind):
    return [what for k, what in plan if k == kind]


class TestAnExistingVlanIsReconciled(unittest.TestCase):

    def test_an_untagged_handoff_is_pinned_to_its_border(self):
        nb, plan, ids = run([vlan(397, "VRF_SECURITY_HANDOFF397")])
        self.assertIn("core1", " ".join(entries(plan, "UPDATE")))
        body = nb.patched["ipam/vlans/397/"]
        self.assertEqual(body["tags"], [{"id": ids["tag"]["core1"]}])

    def test_both_handoffs_are_pinned_to_different_borders(self):
        nb, _, ids = run([vlan(397, "H397"), vlan(398, "H398")])
        self.assertEqual(nb.patched["ipam/vlans/397/"]["tags"],
                         [{"id": ids["tag"]["core1"]}])
        self.assertEqual(nb.patched["ipam/vlans/398/"]["tags"],
                         [{"id": ids["tag"]["core2"]}])

    def test_vlan_300_gets_routed_and_multicast_tags(self):
        """Not only the handoffs. Without these 300 is an L2 segment outside
        the TRM carve, which is not what was enrolled."""
        nb, plan, ids = run([vlan(300, "SECURITY_DATA_CAMPUS", role="edge")])
        added = " ".join(entries(plan, "UPDATE"))
        self.assertIn("dag_routed", added)
        self.assertIn("multicast_enabled", added)

    def test_tags_outside_this_script_are_carried_over(self):
        """PATCH replaces the whole list, so anything already on the VLAN has
        to be re-sent or the reconciliation quietly deletes it."""
        nb, _, ids = run([vlan(397, "H397", tags=("someone_elses_tag",))])
        sent = {t["id"] for t in nb.patched["ipam/vlans/397/"]["tags"]}
        self.assertIn(500, sent)                      # the pre-existing tag
        self.assertIn(ids["tag"]["core1"], sent)

    def test_a_fully_enrolled_vlan_is_left_alone(self):
        """Tag AND role AND upstream_asn. A handoff missing any one of them is
        incomplete, and each of the three has broken a render in turn."""
        nb, plan, ids = run([vlan(397, "H397", tags=("core1",),
                                  asn=enrol_mod.UPSTREAM_ASN)])
        self.assertNotIn("ipam/vlans/397/", nb.patched)
        self.assertTrue(any("already correct" in w
                            for w in entries(plan, "exists")))

    def test_a_handoff_pinned_to_the_wrong_border_is_a_conflict(self):
        """Adding core1 to a VLAN already tagged core2 would render the handoff
        on both, so this is refused rather than merged."""
        nb, plan, ids = run([vlan(397, "H397", tags=("core2",))])
        self.assertTrue(any("core2" in c for c in entries(plan, "CONFLICT")))
        self.assertNotIn("ipam/vlans/397/", nb.patched)
        self.assertEqual(entries(plan, "UPDATE"), [])

    def test_a_wrong_role_is_a_conflict_and_is_not_written(self):
        """The fabric classifies a VLAN by role. Enrolling a handoff onto a
        VLAN NetBox calls an edge would create the wrong object."""
        nb, plan, ids = run([vlan(397, "H397", role="edge")])
        self.assertTrue(entries(plan, "CONFLICT"))
        self.assertNotIn("ipam/vlans/397/", nb.patched)

    def test_a_dry_run_writes_nothing(self):
        nb, plan, ids = run([vlan(397, "VRF_SECURITY_HANDOFF397")], apply=False)
        self.assertNotIn("ipam/vlans/397/", nb.patched)
        self.assertTrue(entries(plan, "UPDATE"))


class TestAHandoffCarriesItsUpstreamAsn(unittest.TestCase):
    """'upstream_asn' is the remote-as of a real eBGP session.

    There is nothing to derive it from -- the peer is a router this project does
    not manage -- so the render REFUSES a handoff without it rather than
    guessing. The enrolment did not set it, which stopped the first render after
    VRF SECURITY was provisioned: 18 objects created, both handoffs pinned to
    the right border, and generate_evpn.yml still failed on access2-2 with
    'VLAN 397 ... is an L3 handoff in VRF SECURITY with no upstream_asn'.

    The failure is loud, which is the one good thing about it. It is also the
    third object this enrolment has been incomplete about -- after the device
    tags and the core VLAN binding -- so what the render REQUIRES of a handoff
    is what these tests pin down.
    """

    def test_a_new_handoff_is_created_with_it(self):
        nb, plan, _ids = run([])
        created = [d for p, d in nb.posted if p == "ipam/vlans/"]
        for vid in (397, 398):
            body = next(b for b in created if b["vid"] == vid)
            self.assertEqual(body["custom_fields"]["upstream_asn"],
                             enrol_mod.UPSTREAM_ASN)

    def test_a_non_handoff_does_not_get_one(self):
        """VLAN 300 is an edge segment with no eBGP session. The field is
        scoped to ipam.vlan, so nothing stops it being set on the wrong VLAN."""
        nb, _plan, _ids = run([])
        body = next(b for p, b in nb.posted
                    if p == "ipam/vlans/" and b["vid"] == 300)
        self.assertNotIn("custom_fields", body)

    def test_an_existing_handoff_without_it_is_updated(self):
        nb, plan, _ids = run([vlan(397, "H397", tags=("core1",))])
        self.assertEqual(nb.patched["ipam/vlans/397/"]["custom_fields"],
                         {"upstream_asn": enrol_mod.UPSTREAM_ASN})
        self.assertTrue(any("upstream_asn" in w for w in entries(plan, "UPDATE")))

    def test_a_different_asn_is_a_conflict_and_is_not_overwritten(self):
        """Someone else's session. Changing the remote-as of a live peering to
        a router nobody here controls is worse than leaving it un-enrolled."""
        nb, plan, _ids = run([vlan(397, "H397", tags=("core1",), asn=65000)])
        self.assertTrue(any("65000" in c for c in entries(plan, "CONFLICT")))
        self.assertNotIn("ipam/vlans/397/", nb.patched)

    def test_a_correct_asn_is_left_alone(self):
        nb, plan, _ids = run([vlan(397, "H397", tags=("core1",),
                                   asn=enrol_mod.UPSTREAM_ASN)])
        self.assertNotIn("ipam/vlans/397/", nb.patched)
        self.assertTrue(any("already correct" in w
                            for w in entries(plan, "exists")))


class TestTheVrfItselfIsCreatedWhenAbsent(unittest.TestCase):
    """The teardown deletes the VRF, so the enrolment has to be able to make it.

    This used to refuse -- "create it first, this script enrols an existing
    VRF" -- which was true of the original hand-made SECURITY VRF and false of
    every run after the teardown script existed. It broke the delete-and-restore
    round trip the pair is for: the teardown removes the VRF, and nothing could
    put it back. netbox_provision_schema.py still refuses to create VRFs and
    should; it does not know which VRF you mean, whereas this script is about
    exactly one.
    """

    def test_an_absent_vrf_is_created_with_its_l3vni(self):
        """'l3vni' is what ENROLS a VRF in the fabric. A VRF created without it
        renders nothing and reports no error, which is the failure mode this
        whole script exists to avoid."""
        nb, plan, _ids = run([], vrf=None)
        posted = dict(nb.posted)["ipam/vrfs/"]
        self.assertEqual(posted["name"], "SECURITY")
        self.assertEqual(posted["custom_fields"]["l3vni"], enrol_mod.L3VNI)
        self.assertEqual(posted["custom_fields"]["mcast_base_group"],
                         enrol_mod.MCAST_BASE_GROUP)

    def test_no_rd_is_invented(self):
        """The switch-side RD is derived per device as <loopback0>:<l3vni>, so
        NetBox's rd field is not the source. Writing one would look
        authoritative and be read by nothing."""
        nb, _plan, _ids = run([], vrf=None)
        self.assertNotIn("rd", dict(nb.posted)["ipam/vrfs/"])

    def test_the_vrf_is_named_in_the_plan(self):
        _nb, plan, _ids = run([], vrf=None)
        self.assertTrue(any("VRF SECURITY" in w and "l3vni" in w
                            for k, w in plan if k == "CREATE"), plan)

    def test_a_dry_run_creates_nothing_but_still_plans_the_vrf(self):
        nb, plan, _ids = run([], apply=False, vrf=None)
        self.assertEqual(nb.posted, [])
        self.assertTrue(any(k == "CREATE" and "VRF SECURITY" in w
                            for k, w in plan))

    def test_a_dry_run_never_reports_vrf_objects_as_already_present(self):
        """With no VRF yet there is no vrf_id, and querying NetBox with
        vrf_id=None asks about the GLOBAL table rather than this VRF. Issuing
        those lookups would report the VRF's own prefixes and its external RP as
        'exists' inside a VRF that does not exist, and the plan would understate
        what an --apply is about to write."""
        _nb, plan, _ids = run([], apply=False, vrf=None)
        wrong = [w for k, w in plan
                 if k == "exists" and ("(SECURITY)" in w or "prefix" in w)]
        self.assertEqual(wrong, [], plan)

    def test_an_existing_vrf_is_not_recreated(self):
        nb, _plan, _ids = run([])
        self.assertNotIn("ipam/vrfs/", dict(nb.posted))


if __name__ == "__main__":
    unittest.main()
