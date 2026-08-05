# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""The MSDP mesh check, and the fabric it is scoped to.

The mesh is checked against INVENTORY rather than against the device, because a
one-way mesh is invisible from either end -- each switch's own config looks
complete. That is also what made it a single-fabric assumption: it demanded a
session to every device tagged fabric_spine anywhere in NetBox, so the day a
second fabric appeared, every correctly-built spine would have been reported
unready and handed a remediation file telling an operator to peer across two
underlays that share neither an RP nor a route to each other.

The check is Jinja inside a set_fact, where --syntax-check evaluates nothing, so
the '_spine' expression is lifted out of the shipped 02_check.yml at run time
and evaluated under a real Ansible against a static inventory and a hand-written
running config. Nothing is copied, so the test cannot drift from the role.
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
FIXTURES = os.path.join(TESTS, "fixtures")

CHECK = os.path.join(ROLE, "tasks", "02_check.yml")
HARNESS = os.path.join(FIXTURES, "msdp_harness.yml")
INVENTORY = os.path.join(FIXTURES, "msdp_two_fabrics.ini")

ANSIBLE = shutil.which("ansible-playbook")

RP = "192.0.2.1"


def spine_check_tasks():
    """The shipped '_spine' expression, wrapped in a task of its own.

    02_check.yml computes every category in one set_fact so the play prints one
    line per host. Only this category is fabric-scoped, and the others need OSPF
    and SNMP state from a device, so the expression is evaluated on its own --
    verbatim, never retyped.
    """
    with open(CHECK) as fh:
        tasks = yaml.safe_load(fh)
    for task in tasks:
        spine = (task.get("vars") or {}).get("_spine")
        if spine:
            return [{
                "name": "Spine check",
                "ansible.builtin.set_fact": {"spine_findings": "{{ _spine | from_json }}"},
                "vars": {"_spine": spine},
                "when": "_stanzas is defined",
            }]
    raise AssertionError("no '_spine' var found in " + CHECK)


def stanzas(*lines, rp=True, originator=True):
    """A running config as 01_gather.yml's indexer would have keyed it."""
    out = {line: [] for line in lines}
    if originator:
        out["ip msdp originator-id Loopback0"] = []
    if rp:
        out["interface Loopback60000"] = ["ip address %s 255.255.255.255" % RP]
    return out


def msdp_peer(addr):
    return "ip msdp peer %s connect-source Loopback0" % addr


def run_check(spine_stanzas):
    """Run the real spine check over a {hostname: stanzas} fixture."""
    workdir = tempfile.mkdtemp(prefix="msdp-scope-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        with open(os.path.join(workdir, "spine_check.yml"), "w") as fh:
            yaml.safe_dump(spine_check_tasks(), fh, default_flow_style=False,
                           sort_keys=False)

        result_file = os.path.join(workdir, "result.json")
        env = dict(os.environ, ANSIBLE_ROLES_PATH=ROLES, ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [ANSIBLE, "-i", INVENTORY, "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", json.dumps({"spine_stanzas": spine_stanzas})],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        if not os.path.exists(result_file):
            raise AssertionError("harness produced no result:\n" + output)
        with open(result_file) as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# A correctly built pair of fabrics: the campus spines mesh with each other and
# with nobody else, and the single remote spine has nobody to mesh with.
HEALTHY = {
    "core1": stanzas(msdp_peer("10.0.0.2")),
    "core2": stanzas(msdp_peer("10.0.0.1")),
    "rcore1": stanzas(),
    "access1-1": {},
}


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestMeshIsFabricScoped(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_check(HEALTHY)

    def msgs(self, host):
        return self.result[host]["msgs"]

    def test_two_correctly_built_fabrics_are_clean(self):
        for host in ("core1", "core2", "rcore1", "access1-1"):
            self.assertEqual(self.msgs(host), [], host)

    def test_a_campus_spine_is_not_asked_to_peer_with_the_remote_fabric(self):
        """The regression. Unscoped, core1 would be told to add an MSDP peer to
        rcore1 -- a session across two underlays, to an RP it does not share."""
        self.assertNotIn("rcore1", " ".join(self.msgs("core1")))

    def test_a_lone_spine_in_its_own_fabric_needs_no_peers(self):
        self.assertEqual(self.msgs("rcore1"), [])

    def test_fabrics_resolved_as_expected(self):
        """Guards the fixture itself: if these ever collapsed into one fabric
        the scoping assertions above would pass for the wrong reason."""
        self.assertEqual(self.result["core1"]["fabric_id"], "site_group_evpn_campus")
        self.assertEqual(self.result["rcore1"]["fabric_id"], "sites_evpn_remote")


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestOneWayMeshStillCaught(unittest.TestCase):
    """Scoping must not blunt the check that justified writing it.

    core2 pointed at core1 and core1 did not point back; both configs looked
    complete on the device. This fabric was bitten by exactly that.
    """

    @classmethod
    def setUpClass(cls):
        broken = dict(HEALTHY, core1=stanzas())     # core1 no longer points back
        cls.result = run_check(broken)

    def test_the_missing_direction_is_reported(self):
        msgs = " ".join(self.result["core1"]["msgs"])
        self.assertIn("missing MSDP peer to core2", msgs)
        self.assertIn("10.0.0.2", msgs)

    def test_the_direction_that_is_configured_is_not_reported(self):
        self.assertEqual(self.result["core2"]["msgs"], [])

    def test_the_other_fabric_is_unaffected(self):
        self.assertEqual(self.result["rcore1"]["msgs"], [])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSpineOnlyChecksSurvive(unittest.TestCase):
    """The non-mesh half of the same block, which scoping must leave alone."""

    @classmethod
    def setUpClass(cls):
        broken = dict(
            HEALTHY,
            core1=stanzas(msdp_peer("10.0.0.2"), originator=False),
            core2=stanzas(msdp_peer("10.0.0.1"), rp=False),
        )
        cls.result = run_check(broken)

    def test_missing_originator_id_is_reported(self):
        self.assertIn("ip msdp originator-id Loopback0",
                      " ".join(self.result["core1"]["msgs"]))

    def test_spine_not_carrying_the_anycast_rp_is_reported(self):
        self.assertIn("no interface carries the anycast RP address",
                      " ".join(self.result["core2"]["msgs"]))

    def test_a_leaf_is_never_checked_as_a_spine(self):
        self.assertEqual(self.result["access1-1"]["msgs"], [])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
