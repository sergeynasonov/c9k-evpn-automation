# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Fabric resolution and per-fabric EVPN peer selection.

Unlike its neighbours here this is not a plugin test: the logic lives in Jinja,
in roles/underlay_readiness/tasks/fabric_resolve.yml and in tasks 15-16 of
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
    wanted = [t for t in tasks if t.get("name", "").startswith(("15.", "16."))]
    if len(wanted) != 2:
        raise AssertionError(
            "expected tasks 15 and 16 of 01_gather_data.yml to be the peer-selection "
            "tasks, found {}".format([t.get("name") for t in wanted])
        )
    return wanted


def run_fabric(inventory, reentry=False):
    """Run the harness against a fixture inventory.

    ``reentry`` imports the resolver a second time, the way acm_evpn does when
    it resolves in 01_gather_data.yml and then runs underlay_readiness as a
    pre-flight.

    Returns (returncode, output, result) where result is the parsed facts, or
    None when the play failed before writing them.
    """
    workdir = tempfile.mkdtemp(prefix="fabric-resolve-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        with open(os.path.join(workdir, "peer_tasks.yml"), "w") as fh:
            yaml.safe_dump(_peer_tasks(), fh, default_flow_style=False, sort_keys=False)

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
            [ANSIBLE, "-i", os.path.join(FIXTURES, inventory), "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", "reentry=" + ("true" if reentry else "false")],
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
        """A distribution switch is site-scoped like anything else and renders
        nothing, so it must resolve a fabric but claim no sessions."""
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

    def test_everyone_else_survives(self):
        self.assertEqual(
            self.result["survivors"],
            ["access1-1", "access2-1", "core1", "core2", "dist1", "raccess1", "rcore1"])


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


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
