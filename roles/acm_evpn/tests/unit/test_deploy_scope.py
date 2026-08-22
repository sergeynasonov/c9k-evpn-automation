# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Which devices a deploy actually touches, and which spines come with them.

deploy_evpn.yml resolves its own scope before the play that opens a session to
anything. That resolution is the whole safety boundary of a scoped run: get it
wrong in one direction and a fabric's spines keep a stale EVPN peer list after a
leaf is added; wrong in the other and a deploy scoped to one building contacts
switches in a different lab.

It used to be a host pattern -- ``{{ deploy_target }}:tags_fabric_spine`` -- and
``tags_fabric_spine`` is ESTATE-WIDE, so every campus run selected the remote
fabric's spines and then dropped them in a task. Selecting devices in order to
discard them is not a boundary, it is a filter with an off-by-one waiting to
happen, and it cannot be narrowed in the pattern because ':&' consumes
everything to its left.

Like test_fabric_resolve.py this runs the REAL play out of deploy_evpn.yml,
lifted at run time so the test cannot drift from the playbook, against a static
two-fabric inventory. No NetBox and no device is in the path: scope is decided
entirely from inventory groups.
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
FIXTURES = os.path.join(TESTS, "fixtures")
PLAYBOOK = os.path.join(REPO, "deploy_evpn.yml")
ANSIBLE = shutil.which("ansible-playbook")


def _scope_play():
    """The scope-resolution play, lifted from deploy_evpn.yml.

    Identified by the fact it sets, not by position: a play added above it must
    not silently make this test check the wrong one.
    """
    with open(PLAYBOOK) as fh:
        plays = yaml.safe_load(fh)

    def sets_the_scope(play):
        for task in play.get("tasks") or []:
            fact = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
            if "acm_deploy_scope" in fact:
                return True
        return False

    wanted = [p for p in plays if sets_the_scope(p)]
    if len(wanted) != 1:
        raise AssertionError(
            "expected exactly one scope-resolving play in deploy_evpn.yml, "
            "found {}".format([p.get("name") for p in wanted])
        )
    return wanted[0]


def resolve(deploy_target, inventory="fabric_multi.ini"):
    """Run the real play and return the hosts the deploy play would receive."""
    workdir = tempfile.mkdtemp(prefix="deploy-scope-")
    try:
        result_file = os.path.join(workdir, "result.json")
        probe = {
            "name": "probe",
            "hosts": "acm_deploy_scope",
            "gather_facts": False,
            "strategy": "linear",
            "tasks": [{
                "name": "record",
                "run_once": True,
                "delegate_to": "localhost",
                "ansible.builtin.copy": {
                    "dest": "{{ result_file }}",
                    "content": ("{{ {'scope': ansible_play_hosts_all | sort,"
                                " 'addresses': ansible_play_hosts_all | sort"
                                " | map('extract', hostvars, 'ansible_host')"
                                " | list} | to_json }}"),
                },
            }],
        }
        with open(os.path.join(workdir, "scope.yml"), "w") as fh:
            yaml.safe_dump([_scope_play(), probe], fh, sort_keys=False)

        env = dict(
            os.environ,
            ANSIBLE_ROLES_PATH=ROLES,
            ANSIBLE_FILTER_PLUGINS=os.path.join(ROLE, "filter_plugins"),
            ANSIBLE_NOCOLOR="1",
        )
        proc = subprocess.run(
            [ANSIBLE, "-i", os.path.join(FIXTURES, inventory), "scope.yml",
             "-e", "result_file=" + result_file,
             "-e", "deploy_target=" + deploy_target],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        result = None
        if os.path.exists(result_file):
            with open(result_file) as fh:
                result = json.load(fh)
        return proc.returncode, proc.stdout + proc.stderr, result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestDeployScope(unittest.TestCase):
    """fabric A = site_group_evpn_campus, spines core1/core2.
    fabric B = sites_evpn_remote, spine rcore1."""

    def scope(self, target):
        rc, out, result = resolve(target)
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIsNotNone(result, out[-3000:])
        return result["scope"]

    def test_a_site_group_brings_its_own_spines_and_no_others(self):
        scope = self.scope("site_group_evpn_campus")
        self.assertIn("core1", scope)
        self.assertIn("core2", scope)
        self.assertNotIn("rcore1", scope)

    def test_one_leaf_pulls_in_its_own_fabrics_spines(self):
        """The case the estate-wide union existed for. A leaf on its own does
        not select its spines, and their peer list would go stale."""
        self.assertEqual(self.scope("access1-1"), ["access1-1", "core1", "core2"])

    def test_one_leaf_pulls_in_no_other_fabrics_spines(self):
        """The reason the union had to go: a campus-scoped run must never open
        a session to the remote fabric."""
        self.assertEqual(self.scope("raccess1"), ["raccess1", "rcore1"])

    def test_a_site_that_is_not_a_fabric_contributes_no_spines(self):
        """sites_wan_edge holds WAN kit and a server. It is in fabric_map but
        not in fabric_ids, so it names no fabric and must pull in nothing."""
        scope = self.scope("sites_wan_edge")
        self.assertEqual(scope, ["fileserver", "wan-rtr-1"])

    def test_a_spine_is_not_added_twice_when_already_targeted(self):
        scope = self.scope("core1")
        self.assertEqual(scope, ["core1", "core2"])

    def test_inventory_host_vars_survive_the_regrouping(self):
        """add_host names hosts that already exist, so it adds group membership
        and nothing else. If it replaced them, ansible_host would be lost and
        every connection would fall back to resolving the device NAME."""
        rc, out, result = resolve("access1-1")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(result["addresses"], ["10.0.0.11", "10.0.0.1", "10.0.0.2"])


if __name__ == "__main__":
    unittest.main()
