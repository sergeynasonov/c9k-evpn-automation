# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Which devices a generate or deploy actually touches, and which spines come
with them.

resolve_deploy_scope.yml resolves the scope before the play that opens a
session to anything, and both generate_evpn.yml and deploy_evpn.yml import it.
That resolution is the whole safety boundary of a scoped run: get it wrong in
one direction and a fabric's spines keep a stale EVPN peer list after a
leaf is added; wrong in the other and a deploy scoped to one building contacts
switches in a different lab.

It used to be a host pattern -- ``{{ deploy_target }}:tags_fabric_spine`` -- and
``tags_fabric_spine`` is ESTATE-WIDE, so every campus run selected the remote
fabric's spines and then dropped them in a task. Selecting devices in order to
discard them is not a boundary, it is a filter with an off-by-one waiting to
happen, and it cannot be narrowed in the pattern because ':&' consumes
everything to its left.

generate_evpn.yml kept that union after deploy_evpn.yml dropped it, so a
preview contacted different switches from the deploy it previewed. The tests
at the bottom pin both playbooks to the one shared play.

Like test_fabric_resolve.py this runs the REAL play out of
resolve_deploy_scope.yml, lifted at run time so the test cannot drift from the
playbook, against a static two-fabric inventory. No NetBox and no device is in the path: scope is decided
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
PLAYBOOK = os.path.join(REPO, "resolve_deploy_scope.yml")
# Every playbook that renders for the fabric. Each must take its scope from the
# shared play and nowhere else.
SCOPED_PLAYBOOKS = ("generate_evpn.yml", "deploy_evpn.yml")
ANSIBLE = shutil.which("ansible-playbook")


def _scope_play():
    """The scope-resolution play, lifted from resolve_deploy_scope.yml.

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
            "expected exactly one scope-resolving play in resolve_deploy_scope.yml, "
            "found {}".format([p.get("name") for p in wanted])
        )
    return wanted[0]


def resolve(deploy_target, inventory="fabric_multi.ini"):
    """Run the real play and return the hosts the deploy play would receive."""
    workdir = tempfile.mkdtemp(prefix="deploy-scope-")
    try:
        result_file = os.path.join(workdir, "result.json")
        # On localhost and reading the GROUP, not a play over the group: a
        # scope can legitimately be empty (a target naming only servers), and
        # a play over an empty group never runs a task to record that.
        probe = {
            "name": "probe",
            "hosts": "localhost",
            "gather_facts": False,
            "strategy": "linear",
            "tasks": [{
                "name": "record",
                "ansible.builtin.copy": {
                    "dest": "{{ result_file }}",
                    # .get: a siteless fixture device has no ansible_host.
                    "content": ("{% set addrs = [] %}"
                                "{% for h in groups.get('acm_deploy_scope', []) | sort %}"
                                "{% set _ = addrs.append(hostvars[h].get('ansible_host')) %}"
                                "{% endfor %}"
                                "{{ {'scope': groups.get('acm_deploy_scope', []) | sort,"
                                " 'addresses': addrs} | to_json }}"),
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

    def scope(self, target, inventory="fabric_multi.ini"):
        rc, out, result = resolve(target, inventory)
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

    def test_a_site_that_is_not_a_fabric_contributes_nothing(self):
        """sites_wan_edge holds WAN kit and a server. It names no fabric, so
        it pulls in no spines -- and since neither device is in a fabric role,
        neither reaches the device play. It is a real target, not a typo, so
        it ends quietly with an empty scope rather than being refused."""
        self.assertEqual(self.scope("sites_wan_edge"), [])

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

    def test_servers_sharing_a_fabric_site_do_not_reach_the_device_play(self):
        """The ubuntu hosts in a real deploy recap: a site-scoped target
        selects everything in the site. Only fabric roles go on."""
        scope = self.scope("site_group_evpn_campus", "fabric_servers.ini")
        for host in ("ubuntu1", "printer1", "edge-wan"):
            self.assertNotIn(host, scope)
        self.assertEqual(scope, ["access1-1", "access2-1", "core1", "core2", "dist1"])

    def test_a_wan_router_tagged_fabric_spine_is_not_added_as_a_spine(self):
        """The spine union adds tagged devices of the targeted fabric. The tag
        alone makes nothing a fabric node, so the role filter must apply to
        what the union adds, not only to what the target matched."""
        self.assertEqual(self.scope("access1-1", "fabric_servers.ini"),
                         ["access1-1", "core1", "core2"])

    def test_the_skipped_devices_are_named_once(self):
        rc, out, _ = resolve("site_group_evpn_campus", "fabric_servers.ini")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("scope: skipped 3 device(s) not in a fabric role", out)
        self.assertIn("edge-wan, printer1, ubuntu1", out)

    def test_switches_are_untouched_by_the_role_filter(self):
        """Every fabric-role device the target selected is still in scope:
        the filter removes non-switches and nothing else."""
        for target in ("site_group_evpn_campus", "sites_building_a", "sites_building_b"):
            with self.subTest(target=target):
                self.assertEqual(
                    [h for h in self.scope(target, "fabric_servers.ini")],
                    [h for h in self.scope(target, "fabric_multi.ini")
                     if h not in ("fileserver", "wan-rtr-1")])

    def test_all_is_resolved_even_with_a_siteless_device(self):
        """deploy_target=all used to fail before the run began --
        "object of type 'dict' has no attribute 'orphan1'" -- because a device
        with no site has no fabric_map entry and map('extract') raised on it.
        Now it resolves: orphan1 is a fabric leaf, so it is in scope, and the
        resolver reports and skips it like any other siteless device."""
        rc, out, result = resolve("all")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("orphan1", result["scope"])
        for spine in ("core1", "core2", "rcore1"):
            self.assertIn(spine, result["scope"])
        self.assertNotIn("fileserver", result["scope"])
        # The report names the fabrics and does not choke on orphan1 either.
        self.assertIn("in site_group_evpn_campus, sites_evpn_remote", out)

    def test_a_siteless_device_named_alone_contributes_no_spines(self):
        rc, out, result = resolve("orphan1")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(result["scope"], ["orphan1"])
        self.assertIn("resolves to 1 device(s) in no fabric", out)

    def test_a_target_that_matches_nothing_is_refused(self):
        """A typo used to reach task 14 by way of the unioned spines. With the
        union gone it would leave the next play empty, which Ansible reports as
        a warning and exit 0 -- a typo indistinguishable from 'nothing to do'."""
        rc, out, result = resolve("acess1-1")
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIsNone(result)
        self.assertIn("matches no device", out)
        self.assertIn("site_group_evpn_campus", out)


class TestBothPlaybooksShareTheScope(unittest.TestCase):
    """generate_evpn.yml is a preview of deploy_evpn.yml, so the two must act on
    the same switches. Asserted structurally rather than by running both:
    'the same play' is a stronger guarantee than 'the same answer today'."""

    def plays(self, name):
        with open(os.path.join(REPO, name)) as fh:
            return yaml.safe_load(fh)

    def test_each_imports_the_shared_scope_play_first(self):
        for name in SCOPED_PLAYBOOKS:
            with self.subTest(playbook=name):
                first = self.plays(name)[0]
                imported = (first.get("ansible.builtin.import_playbook")
                            or first.get("import_playbook"))
                self.assertEqual(imported, "resolve_deploy_scope.yml")

    def test_each_renders_only_for_the_resolved_group(self):
        """A host pattern of its own -- the old ':tags_fabric_spine' union, or
        anything else -- would put back a second definition of scope."""
        for name in SCOPED_PLAYBOOKS:
            with self.subTest(playbook=name):
                rest = self.plays(name)[1:]
                self.assertTrue(rest)
                for play in rest:
                    self.assertEqual(play.get("hosts"), "acm_deploy_scope",
                                     play.get("name"))

    def test_no_playbook_resolves_scope_on_its_own(self):
        for name in SCOPED_PLAYBOOKS:
            with self.subTest(playbook=name):
                for play in self.plays(name):
                    for task in play.get("tasks") or []:
                        fact = (task.get("ansible.builtin.set_fact")
                                or task.get("set_fact") or {})
                        self.assertNotIn("acm_deploy_scope", fact)


if __name__ == "__main__":
    unittest.main()
