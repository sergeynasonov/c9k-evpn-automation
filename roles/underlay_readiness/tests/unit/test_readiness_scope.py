# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Which devices readiness.yml checks.

Two problems, one cause: readiness.yml used deploy_target as its host pattern
directly.

  * A site-scoped target selects everything in the site, so the servers sharing
    a campus building entered the play, sat in the recap and were written to
    readiness-summary.csv as 'ready', although nothing was ever checked.
  * A target matching nothing -- a typo, or an inventory that failed to load
    because NETBOX_API was unset -- ended as 'no hosts matched' and exit 0,
    indistinguishable from a clean run.

The scope is now resolved first, on localhost. These tests run that play,
LIFTED FROM THE SHIPPED readiness.yml at run time, against the static
inventories acm_evpn's scope tests use, and record what the main play would
receive. Unlike generate and deploy, readiness adds no spines.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE = os.path.dirname(TESTS)
ROLES = os.path.dirname(ROLE)
REPO = os.path.dirname(ROLES)
FIXTURES = os.path.join(ROLES, "acm_evpn", "tests", "fixtures")
PLAYBOOK = os.path.join(REPO, "readiness.yml")
ANSIBLE = shutil.which("ansible-playbook")


def _scope_play():
    with open(PLAYBOOK) as fh:
        plays = yaml.safe_load(fh)
    wanted = [p for p in plays if any(
        "readiness_scope" in ((t.get("ansible.builtin.set_fact") or {}))
        for t in p.get("tasks") or [])]
    if len(wanted) != 1:
        raise AssertionError("expected one scope-resolving play in readiness.yml")
    return wanted[0]


def resolve(deploy_target=None, inventory="fabric_servers.ini"):
    workdir = tempfile.mkdtemp(prefix="readiness-scope-")
    try:
        result_file = os.path.join(workdir, "result.json")
        probe = {"name": "probe", "hosts": "localhost", "gather_facts": False,
                 "tasks": [{"name": "record", "ansible.builtin.copy": {
                     "dest": "{{ result_file }}",
                     "content": "{{ groups.get('readiness_scope', []) | sort | to_json }}"}}]}
        with open(os.path.join(workdir, "scope.yml"), "w") as fh:
            yaml.safe_dump([_scope_play(), probe], fh, sort_keys=False)
        cmd = [ANSIBLE, "-i", os.path.join(FIXTURES, inventory), "scope.yml",
               "-e", "result_file=" + result_file]
        if deploy_target is not None:
            cmd += ["-e", "deploy_target=" + deploy_target]
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                              env=dict(os.environ, ANSIBLE_ROLES_PATH=ROLES,
                                       ANSIBLE_NOCOLOR="1"))
        result = None
        if os.path.exists(result_file):
            with open(result_file) as fh:
                result = json.load(fh)
        return proc.returncode, proc.stdout + proc.stderr, result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestReadinessScope(unittest.TestCase):

    def scope(self, target, inventory="fabric_servers.ini"):
        rc, out, result = resolve(target, inventory)
        self.assertEqual(rc, 0, out[-3000:])
        return result

    def test_servers_sharing_the_campus_are_not_checked(self):
        self.assertEqual(self.scope("site_group_evpn_campus"),
                         ["access1-1", "access2-1", "core1", "core2", "dist1"])

    def test_the_skipped_devices_are_named_once(self):
        _, out, _ = resolve("site_group_evpn_campus")
        self.assertIn("scope: skipped 3 device(s) not in a fabric role", out)

    def test_no_spines_are_added(self):
        """Unlike generate and deploy: nothing readiness checks depends on
        fabric-wide state, so a narrow run stays narrow."""
        self.assertEqual(self.scope("access1-1"), ["access1-1"])

    def test_a_distribution_switch_is_still_checked(self):
        """The transit path: nothing is rendered for it, but its underlay is."""
        self.assertEqual(self.scope("dist1"), ["dist1"])

    def test_every_switch_the_old_pattern_selected_is_still_selected(self):
        """The filter removes non-switches and nothing else."""
        for target in ("site_group_evpn_campus", "sites_building_a",
                       "sites_evpn_remote", "access1-1:raccess1"):
            with self.subTest(target=target):
                self.assertEqual(self.scope(target, "fabric_servers.ini"),
                                 self.scope(target, "fabric_multi.ini"))

    def test_a_target_of_only_servers_ends_quietly(self):
        """A real target with nothing to check is not a typo."""
        self.assertEqual(self.scope("sites_wan_edge"), [])

    def test_a_target_that_matches_nothing_is_refused(self):
        rc, out, result = resolve("acess1-1")
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIsNone(result)
        self.assertIn("matches no device in the inventory", out)

    def test_an_unscoped_run_still_reaches_the_main_plays_refusal(self):
        """No deploy_target: the default pattern resolves to the fabric roles,
        and fabric_resolve.yml task 2 in the main play refuses the run, exactly
        as before. The scope play must not swallow that."""
        rc, out, result = resolve(None)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("access1-1", result)
        self.assertNotIn("ubuntu1", result)


if __name__ == "__main__":
    unittest.main()
