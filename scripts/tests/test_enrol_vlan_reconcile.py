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
        return None          # nothing else exists, so it is planned as CREATE

    def post(self, path, data, branch=True):
        self.posted.append((path, data))
        return {"id": 9000 + len(self.posted)}

    def patch(self, path, data, branch=True):
        self.patched[path] = data
        return {}


def vlan(vid, name, tags=(), role="vrf_l3_handoff_vlan"):
    return {"id": vid, "vid": vid, "name": name,
            "role": {"slug": role} if role else None,
            "tags": [{"id": 500 + i, "slug": t} for i, t in enumerate(tags)]}


class Ids(dict):
    """Auto-vivifying id map: the script resolves roles and tags from several
    tables, and a fixture that lists them by hand goes stale silently."""

    def __missing__(self, key):
        self[key] = 600 + len(self)
        return self[key]


def run(vlans, apply=True):
    ids = {"role": Ids(), "tag": Ids(), "vlan_group": 1, "dhcp": 2,
           "vrf": {"id": 3, "name": "SECURITY"}}
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

    def test_a_correctly_tagged_vlan_is_left_alone(self):
        nb, plan, ids = run([vlan(397, "H397", tags=("core1",))])
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


if __name__ == "__main__":
    unittest.main()
