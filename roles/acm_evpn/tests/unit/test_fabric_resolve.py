# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Fabric resolution and per-fabric EVPN peer selection.

Unlike its neighbours here this is not a plugin test: the logic lives in Jinja,
in roles/underlay_readiness/tasks/fabric_resolve.yml and in tasks 16-17 of
roles/acm_evpn/tasks/01_gather_data.yml. ``--syntax-check`` does not evaluate
Jinja, so the only honest way to test it is to run it -- which is possible
offline because fabric membership is decided entirely from inventory groups,
with no NetBox and no device in the path.

Each case runs the REAL task files under a real ansible-playbook against a
static inventory in ../fixtures. Nothing is copied by hand: the peer-selection
tasks are lifted out of 01_gather_data.yml at run time, so the test cannot drift
from the role.

What it is guarding
-------------------
A peer list is the highest-risk value this project computes. Too short a list
renders a perfectly valid ``router bgp`` that ``acm replace`` applies as a
teardown of live sessions, on a switch that was working a moment earlier. Every
assertion below is ultimately about a peer that must be there, or one that must
not.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE = os.path.dirname(TESTS)
ROLES = os.path.dirname(ROLE)
REPO = os.path.dirname(ROLES)
FIXTURES = os.path.join(TESTS, "fixtures")

GATHER = os.path.join(ROLE, "tasks", "01_gather_data.yml")
HARNESS = os.path.join(FIXTURES, "fabric_harness.yml")

ANSIBLE = shutil.which("ansible-playbook")


def _peer_tasks():
    """The inventory-only tasks of 01_gather_data.yml, verbatim.

    Everything else in that file needs NetBox or a live switch. Selecting by the
    numeric prefix keeps the harness honest: renumber or rename a task and this
    raises rather than silently testing less than it claims to.
    """
    with open(GATHER) as fh:
        tasks = yaml.safe_load(fh)
    wanted = [t for t in tasks if t.get("name", "").startswith(("16.", "17."))]
    if len(wanted) != 2:
        raise AssertionError(
            "expected tasks 16 and 17 of 01_gather_data.yml to be the peer-selection "
            "tasks, found {}".format([t.get("name") for t in wanted])
        )
    return wanted


def _target_tasks():
    """Tasks 13-15 of 01_gather_data.yml: deploy_target -> targeted fabrics."""
    with open(GATHER) as fh:
        tasks = yaml.safe_load(fh)
    wanted = [t for t in tasks if t.get("name", "").startswith(("13.", "14.", "15."))]
    if len(wanted) != 3:
        raise AssertionError(
            "expected tasks 13-15 of 01_gather_data.yml to be the target-scoping "
            "tasks, found {}".format([t.get("name") for t in wanted])
        )
    return wanted


def run_fabric(inventory, reentry=False, mutate=None, deploy_target=None):
    """Run the harness against a fixture inventory.

    ``reentry`` imports the resolver a second time, the way acm_evpn does when
    it resolves in 01_gather_data.yml and then runs underlay_readiness as a
    pre-flight.

    ``mutate`` is called with the parsed YAML inventory and may edit it in
    place; the edited copy is written into the temp dir and used instead. The
    broken-ASN cases are one small edit away from the working fixture, so
    expressing them as edits keeps the difference visible in the test rather
    than buried in a near-identical second file.

    Returns (returncode, output, result) where result is the parsed facts, or
    None when the play failed before writing them.
    """
    workdir = tempfile.mkdtemp(prefix="fabric-resolve-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        inv_path = os.path.join(FIXTURES, inventory)
        if mutate is not None:
            with open(inv_path) as fh:
                data = yaml.safe_load(fh)
            mutate(data)
            inv_path = os.path.join(workdir, "inventory.yml")
            with open(inv_path, "w") as fh:
                yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        with open(os.path.join(workdir, "peer_tasks.yml"), "w") as fh:
            yaml.safe_dump(_peer_tasks(), fh, default_flow_style=False, sort_keys=False)
        with open(os.path.join(workdir, "target_tasks.yml"), "w") as fh:
            yaml.safe_dump(_target_tasks(), fh, default_flow_style=False, sort_keys=False)

        result_file = os.path.join(workdir, "result.json")
        env = dict(
            os.environ,
            ANSIBLE_ROLES_PATH=ROLES,
            ANSIBLE_FILTER_PLUGINS=os.path.join(ROLE, "filter_plugins"),
            # The repo's ansible.cfg would point the run at netbox_inv.yml and
            # at the 'free' strategy. Running from a temp cwd leaves it unfound,
            # so everything this needs is passed explicitly above.
            ANSIBLE_NOCOLOR="1",
        )
        proc = subprocess.run(
            [ANSIBLE, "-i", inv_path, "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", "reentry=" + ("true" if reentry else "false")]
            + (["-e", "deploy_target=" + deploy_target] if deploy_target else []),
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        result = None
        if os.path.exists(result_file):
            with open(result_file) as fh:
                result = json.load(fh)
        return proc.returncode, output, result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestTwoFabrics(unittest.TestCase):
    """One campus site group beside one standalone site."""

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini")
        if cls.result is None:  # pragma: no cover - only on a broken harness
            raise AssertionError("harness produced no result:\n" + cls.out)

    def peers(self, host):
        return self.result["hosts"][host]["peer_ips"]

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_site_group_is_the_fabric_when_there_is_one(self):
        """Two buildings under one site group are ONE fabric, not two."""
        self.assertEqual(self.result["fabric_map"]["access1-1"], "site_group_evpn_campus")
        self.assertEqual(self.result["fabric_map"]["access2-1"], "site_group_evpn_campus")

    def test_bare_site_is_the_fabric_when_there_is_no_site_group(self):
        self.assertEqual(self.result["fabric_map"]["rcore1"], "sites_evpn_remote")

    def test_spine_reflects_for_its_own_fabric_only(self):
        """core1 must see both buildings of its campus and neither remote box."""
        self.assertEqual(self.peers("core1"), ["10.0.0.11", "10.0.0.21"])

    def test_leaf_peers_only_with_its_own_spines(self):
        self.assertEqual(self.peers("access1-1"), ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(self.peers("raccess1"), ["10.1.0.1"])

    def test_remote_fabric_does_not_pull_in_the_campus(self):
        self.assertEqual(self.peers("rcore1"), ["10.1.0.11"])

    def test_no_address_from_another_fabric_appears_anywhere(self):
        """The whole point. A cross-fabric neighbour is a session to a device
        that shares neither underlay nor RP."""
        campus = {"core1", "core2", "access1-1", "access2-1", "dist1"}
        for host, data in self.result["hosts"].items():
            remote = [p for p in data["peer_ips"] if p.startswith("10.1.")]
            if host in campus:
                self.assertEqual(remote, [], "{} peers into the remote fabric".format(host))
            else:
                self.assertTrue(all(p.startswith("10.1.") for p in data["peer_ips"]),
                                "{} peers into the campus: {}".format(host, data["peer_ips"]))

    def test_a_second_fabric_does_not_shorten_the_first(self):
        """Regression on the mechanism itself: adding fabric B must not remove a
        peer from fabric A, which is the shape a teardown takes."""
        self.assertEqual(len(self.peers("core1")), 2)
        self.assertEqual(len(self.peers("core2")), 2)

    def test_non_fabric_node_in_a_fabric_gets_no_peers(self):
        """A distribution switch is site-scoped like anything else, so it
        resolves a fabric -- but claims no sessions and, below, is not rendered
        for at all."""
        self.assertEqual(self.result["fabric_map"]["dist1"], "site_group_evpn_campus")
        self.assertEqual(self.peers("dist1"), [])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestDeviceWithNoSite(unittest.TestCase):
    """Ignored and reported, never fatal -- but never rendered either."""

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini")

    def test_run_still_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_orphan_is_reported(self):
        self.assertEqual(self.result["orphans"], ["orphan1"])
        self.assertIn("orphan1", self.out)

    def test_orphan_resolves_to_no_fabric(self):
        self.assertNotIn("orphan1", self.result["fabric_map"])
        self.assertEqual(self.result["hosts"]["orphan1"]["fabric_id"], "")

    def test_orphan_drops_out_of_the_play(self):
        """It was targeted, so silence would mean a config rendered against an
        empty fabric -- an empty peer list is a teardown."""
        self.assertIn("orphan1", self.result["targeted"])
        self.assertNotIn("orphan1", self.result["survivors"])

    def test_every_fabric_node_survives(self):
        """dist1 is absent for a different reason -- see TestNonFabricNodes."""
        self.assertEqual(
            self.result["survivors"],
            ["access1-1", "access2-1", "core1", "core2", "raccess1", "rcore1"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNonFabricNodesAreDropped(unittest.TestCase):
    """A host pattern can select devices this role renders nothing for.

    Not hypothetical: ':' is a UNION, so '-e deploy_target=<site>' selects every
    device in that site, distribution switches included. The render step picks
    its template as border / spine / else-leaf, with no "not ours" branch, so
    such a device used to be handed a full EVPN LEAF configuration and have it
    applied by 'acm replace'.

    It cannot be fixed in the host pattern: Ansible applies ':&' intersections
    after every union regardless of position, so intersecting with the fabric
    roles would also intersect away the spines the pattern deliberately unions
    in -- leaving their peer lists stale, which is the exact bug that union
    exists to prevent. Verified against real ansible, not assumed.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini")

    def test_run_still_succeeds(self):
        """One targeted device being none of our business is not an error."""
        self.assertEqual(self.rc, 0, self.out)

    def test_the_distribution_switch_was_targeted(self):
        """Guards the fixture: if it stopped being selected, the assertions
        below would pass for the wrong reason."""
        self.assertIn("dist1", self.result["targeted"])

    def test_it_drops_out_before_anything_is_rendered(self):
        self.assertNotIn("dist1", self.result["survivors"])

    def test_it_is_reported_by_name(self):
        """An operator who targeted a site deserves to know which of its
        devices were left alone."""
        self.assertIn("are not fabric nodes: dist1", self.out)

    def test_fabric_nodes_are_not_dropped_with_it(self):
        for host in ("core1", "core2", "access1-1", "access2-1", "raccess1", "rcore1"):
            self.assertIn(host, self.result["survivors"], host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestResolvingTwiceIsFree(unittest.TestCase):
    """acm_evpn resolves, then imports underlay_readiness, which resolves again.

    The second pass must change nothing and say nothing. It is guarded on
    ``fabric_id``, which the resolver sets LAST -- guarding on ``fabric_map``,
    which it sets first, would have skipped the orphan report on the FIRST pass
    instead of the second.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini", reentry=True)
        cls.once_rc, cls.once_out, cls.once = run_fabric("fabric_multi.ini")

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_the_orphan_is_reported_exactly_once(self):
        self.assertEqual(self.out.count("belong to no fabric and are skipped"), 1)

    def test_resolution_is_identical_to_a_single_pass(self):
        self.assertEqual(self.result["fabric_map"], self.once["fabric_map"])
        self.assertEqual(self.result["hosts"], self.once["hosts"])

    def test_the_orphan_still_drops_out_on_re_entry(self):
        """The failure mode a whole-file guard would have introduced: by the
        second pass fabric_id is set, so a file-level condition would have
        skipped the end_host and let the orphan through."""
        self.assertNotIn("orphan1", self.result["survivors"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNestedSiteGroups(unittest.TestCase):
    """A site-group tree has no single answer, so it is refused outright."""

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_nested.ini")

    def test_run_fails(self):
        self.assertNotEqual(self.rc, 0, self.out)

    def test_nothing_is_rendered(self):
        self.assertIsNone(self.result)

    def test_failure_names_the_devices_and_both_groups(self):
        self.assertIn("nested site groups", self.out)
        self.assertIn("site_group_emea", self.out)
        self.assertIn("site_group_evpn_campus", self.out)

    def test_failure_says_how_to_fix_it(self):
        self.assertIn("Flatten the tree", self.out)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricWithNoReflector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_no_reflector.ini")

    def test_run_fails(self):
        self.assertNotEqual(self.rc, 0, self.out)

    def test_the_stranded_leaf_is_named(self):
        self.assertIn("stranded1", self.out)
        self.assertIn("route reflector", self.out)

    def test_a_healthy_fabric_in_the_same_run_is_unaffected(self):
        """Per device, not fabric-wide: one mis-sited leaf must not stop the
        buildings that are fine."""
        self.assertIsNotNone(self.result, self.out)
        self.assertEqual(self.result["hosts"]["access1-1"]["peer_ips"], ["10.0.0.1"])
        self.assertEqual(self.result["hosts"]["core1"]["peer_ips"], ["10.0.0.11"])
        self.assertNotIn("stranded1", self.result["survivors"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSingleFabricUnchanged(unittest.TestCase):
    """The fabric that is in production today.

    Before this change the peer list was built from a hardcoded
    ``groups['site_group_evpn_campus']``. On an inventory with one site group
    the resolver must arrive at exactly the same answer -- anything else is a
    config change pushed to seven working switches.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_single.ini")

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_every_switch_stays_in_the_play(self):
        self.assertEqual(self.result["survivors"], self.result["targeted"])
        self.assertEqual(len(self.result["survivors"]), 7)

    def test_spines_see_all_five_clients(self):
        expected = ["10.67.118.71", "10.67.118.72", "10.67.118.73",
                    "10.67.118.74", "10.67.118.75"]
        self.assertEqual(self.result["hosts"]["core1"]["peer_ips"], expected)
        self.assertEqual(self.result["hosts"]["core2"]["peer_ips"], expected)

    def test_every_leaf_sees_both_spines(self):
        for host in ("access1-1", "access2-1", "access2-2", "dist3-1", "dist3-2"):
            self.assertEqual(self.result["hosts"][host]["peer_ips"],
                             ["10.67.118.64", "10.67.118.65"], host)

    def test_borders_that_are_spines_are_still_reflectors(self):
        """core1/core2 are both. Each must not appear in the other's client
        list, or a reflector becomes its own client."""
        self.assertNotIn("10.67.118.65", self.result["hosts"]["core1"]["peer_ips"])
        self.assertNotIn("10.67.118.64", self.result["hosts"]["core2"]["peer_ips"])


def _sites(data, *hosts):
    """The 'sites' hostvar of each named host in a parsed YAML inventory.

    Starts at data['all'], not at data: the top level is keyed by group name, so
    walking from there finds neither 'hosts' nor 'children' and would silently
    return nothing -- a mutation that changes no ASN and a test that passes
    against a fixture it never edited.
    """
    out = []
    stack = [data["all"]]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for host, hv in (node.get("hosts") or {}).items():
            if host in hosts and isinstance(hv, dict):
                out.append(hv["sites"][0])
        stack.extend((node.get("children") or {}).values())
    if not out:
        raise AssertionError("no 'sites' hostvar found for {}".format(hosts))
    return out


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricAsn(unittest.TestCase):
    """The fabric's iBGP ASN, from the ASN objects on its NetBox sites.

    It comes from INVENTORY, not a GraphQL query: nb_inventory fetches sites
    with the full serializer, so 'asns' already rides along in the hostvar for
    every device in or out of the play.

    One ASN per fabric is definitional -- every EVPN session in the fabric is
    iBGP inside it -- so the interesting cases are the ones with no single
    answer, and all of them are refused rather than resolved.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_asns.yml")
        if cls.result is None:  # pragma: no cover - only on a broken harness
            raise AssertionError("harness produced no result:\n" + cls.out)

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_each_fabric_resolves_its_own_asn(self):
        self.assertEqual(self.result["fabric_asn_map"],
                         {"site_group_evpn_campus": 64513, "sites_evpn_remote": 65001})

    def test_two_buildings_of_one_campus_share_it(self):
        """Both sites carry 64513, and agreeing is not a conflict."""
        for host in ("core1", "access1-1", "access2-1"):
            self.assertEqual(self.result["hosts"][host]["fabric_asn"], 64513, host)

    def test_the_other_fabric_gets_its_own(self):
        self.assertEqual(self.result["hosts"]["rcore1"]["fabric_asn"], 65001)

    def test_netbox_wins_over_fabric_yml(self):
        """The fixture sets evpn_bgp_asn to 64999. Seeing it anywhere would mean
        the NetBox value was not picked up."""
        for host, data in self.result["hosts"].items():
            self.assertNotEqual(data["fabric_asn"], 64999, host)

    def test_nothing_is_reported_as_missing(self):
        self.assertEqual(self.result["fabric_asn_missing"], [])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricAsnIsRefusedWhenAmbiguous(unittest.TestCase):
    def test_sites_of_one_fabric_disagreeing_is_fatal(self):
        """The dangerous case: building_b on a different ASN would render half
        the campus into an ASN the other half does not peer in."""
        def mutate(data):
            for site in _sites(data, "access2-1"):
                site["asns"] = [{"id": 9, "asn": 64514}]
        rc, out, _ = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertNotEqual(rc, 0)
        self.assertIn("its sites disagree", out)
        self.assertIn("building_b", out)

    def test_one_site_carrying_two_asns_is_fatal(self):
        def mutate(data):
            for site in _sites(data, "core1", "access1-1"):
                site["asns"] = [{"id": 1, "asn": 64513}, {"id": 9, "asn": 64514}]
        rc, out, _ = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertNotEqual(rc, 0)
        self.assertIn("exactly one ASN", out)
        self.assertIn("building_a", out)

    def test_the_failure_says_what_to_do_about_it(self):
        def mutate(data):
            for site in _sites(data, "access2-1"):
                site["asns"] = [{"id": 9, "asn": 64514}]
        _, out, _ = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertIn("assign ONE ASN object to every site", out)

    def test_one_broken_fabric_does_not_silently_render_the_others(self):
        """Fatal for the run, not per device. A site tree that does not give one
        ASN per fabric is not trustworthy for the fabrics that happen to work."""
        def mutate(data):
            for site in _sites(data, "access2-1"):
                site["asns"] = [{"id": 9, "asn": 64514}]
        _, _, result = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertIsNone(result)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricAsnFallsBackWhileMigrating(unittest.TestCase):
    """NetBox is populated fabric by fabric, so an un-migrated one must still
    render exactly as before -- loudly, but not fatally."""

    @classmethod
    def setUpClass(cls):
        def mutate(data):
            for site in _sites(data, "rcore1", "raccess1"):
                site["asns"] = []
        cls.rc, cls.out, cls.result = run_fabric("fabric_asns.yml", mutate=mutate)

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_the_unmigrated_fabric_uses_fabric_yml(self):
        self.assertEqual(self.result["hosts"]["rcore1"]["fabric_asn"], 64999)

    def test_the_migrated_fabric_is_unaffected(self):
        self.assertEqual(self.result["hosts"]["core1"]["fabric_asn"], 64513)

    def test_it_is_reported_as_a_deprecation(self):
        self.assertIn("sites_evpn_remote", self.result["fabric_asn_missing"])
        self.assertIn("the fallback is deprecated", self.out)

    def test_the_migrated_fabric_is_not_reported(self):
        self.assertNotIn("site_group_evpn_campus", self.result["fabric_asn_missing"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSpineUnionIsScopedToTheTargetedFabric(unittest.TestCase):
    """The playbooks union tags_fabric_spine into the host pattern so that a
    scoped run always re-renders its spines' peer lists. That union is
    unscoped, so it also drags in every OTHER fabric's spines.

    The rule is deliberately conservative: a host is dropped only when its
    fabric contains none of the devices deploy_target actually matched. The
    targeted fabric's own spines therefore always stay, which is what keeps the
    peer-list protection the union exists for.
    """

    def test_a_scoped_run_targets_only_that_fabric(self):
        _, _, result = run_fabric("fabric_multi.ini", deploy_target="sites_building_a")
        self.assertEqual(result["targeted_fabrics"], ["site_group_evpn_campus"])

    def test_the_targeted_fabrics_own_spines_stay(self):
        """The load-bearing half. Dropping these is the stale-peer-list bug."""
        _, _, result = run_fabric("fabric_multi.ini", deploy_target="sites_building_a")
        for spine in ("core1", "core2"):
            self.assertIn(spine, result["survivors"], spine)

    def test_another_fabrics_spine_is_dropped(self):
        _, _, result = run_fabric("fabric_multi.ini", deploy_target="sites_building_a")
        self.assertNotIn("rcore1", result["survivors"])

    def test_scoping_to_a_single_leaf_keeps_its_fabrics_spines(self):
        """The case the union exists for: deploy one leaf, and its spines must
        still be re-rendered so their peer lists include it."""
        _, _, result = run_fabric("fabric_multi.ini", deploy_target="access1-1")
        for spine in ("core1", "core2"):
            self.assertIn(spine, result["survivors"], spine)
        self.assertNotIn("rcore1", result["survivors"])

    def test_targeting_the_other_fabric_inverts_it(self):
        _, _, result = run_fabric("fabric_multi.ini", deploy_target="raccess1")
        self.assertEqual(result["targeted_fabrics"], ["sites_evpn_remote"])
        self.assertIn("rcore1", result["survivors"])
        for spine in ("core1", "core2"):
            self.assertNotIn(spine, result["survivors"], spine)

    def test_an_unscoped_run_targets_every_fabric(self):
        _, _, result = run_fabric("fabric_multi.ini")
        self.assertEqual(result["targeted_fabrics"],
                         ["site_group_evpn_campus", "sites_evpn_remote"])
        for host in ("core1", "core2", "rcore1"):
            self.assertIn(host, result["survivors"], host)

    def test_a_deploy_target_matching_nothing_is_refused(self):
        """Without this a typo becomes 'deploy to every spine in the estate',
        since the union would be all that is left in the play."""
        rc, out, _ = run_fabric("fabric_multi.ini", deploy_target="no_such_group")
        self.assertNotEqual(rc, 0)
        self.assertIn("matches no device that belongs to a fabric", out)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
