# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Each fabric's underlay anycast RP, resolved from NetBox.

The address every spine carries on a dedicated loopback, and that every switch
points 'ip pim rp-address' at. It used to be one global in fabric.yml, which a
multi-fabric estate does not have: each fabric runs its own underlay with its
own RP.

The NetBox model needs two features at once, and both are load-bearing:

  role Anycast   PERMITS the duplicate address. NetBox rejects duplicates for
                 every other role, so an object without it is not this model.
  the tag        SELECTS it. Role alone is far too broad -- an anycast gateway
                 SVI on a border carries the same role and is not an RP.

The check this makes possible is the one worth having: within a fabric every
spine's RP address must be IDENTICAL. Differing addresses configure cleanly on
every switch and then silently fail to forward, which is close to undiagnosable
from any single device.

The GraphQL round trip is the one thing that cannot run offline, so the test
strips that task and feeds the transform a fixture in the shape 'uri' registers.
Everything else is the shipped file, lifted at run time.
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

SOURCE = os.path.join(ROLE, "tasks", "fabric_anycast_rp.yml")
HARNESS = os.path.join(FIXTURES, "anycast_rp_harness.yml")
INVENTORY = os.path.join(FIXTURES, "msdp_two_fabrics.ini")

ANSIBLE = shutil.which("ansible-playbook")

def rp_tasks():
    """fabric_anycast_rp.yml with the NetBox query removed, verbatim otherwise."""
    with open(SOURCE) as fh:
        tasks = yaml.safe_load(fh)
    kept = [t for t in tasks if "ansible.builtin.uri" not in t]
    if len(kept) != len(tasks) - 1:
        raise AssertionError(
            "expected exactly one 'uri' task in {}, found {}"
            .format(SOURCE, len(tasks) - len(kept))
        )
    return kept


def row(address, device, interface="Loopback60000", role="anycast"):
    """One IPAddress as the GraphQL query returns it."""
    assigned = None
    if device:
        assigned = {"name": interface, "device": {"name": device}}
    return {"address": address, "role": role, "assigned_object": assigned}


def run_rp(rows):
    workdir = tempfile.mkdtemp(prefix="anycast-rp-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        with open(os.path.join(workdir, "rp_tasks.yml"), "w") as fh:
            yaml.safe_dump(rp_tasks(), fh, default_flow_style=False, sort_keys=False)

        result_file = os.path.join(workdir, "result.json")
        env = dict(os.environ, ANSIBLE_ROLES_PATH=ROLES, ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [ANSIBLE, "-i", INVENTORY, "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", json.dumps({"rp_rows": rows})],
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


# One object per spine, same address within a fabric, each on its own loopback.
HEALTHY = [
    row("10.0.0.100/32", "core1"),
    row("10.0.0.100/32", "core2"),
    row("10.1.0.100/32", "rcore1"),
]


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestPerFabricAnycastRp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_rp(HEALTHY)
        if cls.result is None:  # pragma: no cover - only on a broken harness
            raise AssertionError("harness produced no result:\n" + cls.out)

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_each_fabric_gets_its_own_rp(self):
        self.assertEqual(self.result["rp_map"],
                         {"site_group_evpn_campus": "10.0.0.100",
                          "sites_evpn_remote": "10.1.0.100"})

    def test_the_prefix_length_is_stripped(self):
        """NetBox stores it as CIDR; 'ip pim rp-address' takes a bare address."""
        self.assertEqual(self.result["hosts"]["core1"]["rp_address"], "10.0.0.100")

    def test_every_device_in_a_fabric_shares_its_rp(self):
        """Not just the spines -- every switch points at the RP."""
        for host in ("core1", "core2", "access1-1"):
            self.assertEqual(self.result["hosts"][host]["rp_address"], "10.0.0.100", host)

    def test_the_other_fabric_is_not_pulled_in(self):
        self.assertEqual(self.result["hosts"]["rcore1"]["rp_address"], "10.1.0.100")

    def test_the_interface_comes_from_the_same_object_as_the_address(self):
        """Which is what stops the two drifting apart -- the failure the old
        pair of hand-maintained variables allowed."""
        self.assertEqual(self.result["hosts"]["core1"]["rp_interface"], "Loopback60000")

    def test_spines_may_use_differently_named_loopbacks(self):
        rows = [row("10.0.0.100/32", "core1", interface="Loopback60000"),
                row("10.0.0.100/32", "core2", interface="Loopback600"),
                row("10.1.0.100/32", "rcore1")]
        rc, out, result = run_rp(rows)
        self.assertEqual(rc, 0, out)
        self.assertEqual(result["hosts"]["core2"]["rp_interface"], "Loopback600")
        self.assertEqual(result["rp_map"]["site_group_evpn_campus"], "10.0.0.100")


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestAnycastRpIsRefusedWhenBroken(unittest.TestCase):
    def test_spines_of_one_fabric_on_different_addresses_is_fatal(self):
        """The validation the whole NetBox model exists to make possible."""
        rows = [row("10.0.0.100/32", "core1"),
                row("10.0.0.101/32", "core2"),
                row("10.1.0.100/32", "rcore1")]
        rc, out, _ = run_rp(rows)
        self.assertNotEqual(rc, 0)
        self.assertIn("DIFFERENT anycast RP", out)
        self.assertIn("10.0.0.101", out)

    def test_a_tagged_address_without_role_anycast_is_reported(self):
        """Selected by tag, but NetBox would not have permitted the duplicate
        without the role -- so the model is wrong and saying so beats falling
        back, which looks identical to never having modelled it."""
        rows = [row("10.0.0.100/32", "core1", role="loopback"),
                row("10.0.0.100/32", "core2"),
                row("10.1.0.100/32", "rcore1")]
        rc, out, _ = run_rp(rows)
        self.assertNotEqual(rc, 0)
        self.assertIn("not Anycast", out)
        self.assertIn("core1", out)

    def test_the_graphql_enum_spelling_is_also_accepted(self):
        """The schema filters on ROLE_ANYCAST while the field returns 'anycast'.
        Accepting both means a NetBox that returns the enum name does not read
        as a fabric-wide misconfiguration."""
        rows = [row("10.0.0.100/32", "core1", role="ROLE_ANYCAST"),
                row("10.0.0.100/32", "core2", role="ROLE_ANYCAST"),
                row("10.1.0.100/32", "rcore1")]
        rc, out, result = run_rp(rows)
        self.assertEqual(rc, 0, out)
        self.assertEqual(result["rp_map"]["site_group_evpn_campus"], "10.0.0.100")

    def test_an_unassigned_address_is_reported(self):
        """With no interface there is no device, and with no device there is
        nothing to say which fabric it belongs to."""
        rc, out, _ = run_rp(HEALTHY + [row("10.9.9.9/32", None)])
        self.assertNotEqual(rc, 0)
        self.assertIn("not assigned to an interface", out)

    def test_the_failure_says_how_to_model_it(self):
        rows = [row("10.0.0.100/32", "core1"), row("10.0.0.101/32", "core2")]
        _, out, _ = run_rp(rows)
        self.assertIn("ONE IPAddress per spine", out)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestAFabricWithNoAnycastRpIsRefused(unittest.TestCase):
    """There is no underlay_rp_address in fabric.yml to fall back to any more.

    Falling back used to be the migration path; now NetBox is the only source,
    so a fabric with nothing modelled is a hard failure rather than a silent
    substitution of somebody else's address.
    """

    def test_a_fabric_with_no_rp_is_refused(self):
        rc, out, _ = run_rp([row("10.0.0.100/32", "core1"),
                             row("10.0.0.100/32", "core2")])
        self.assertNotEqual(rc, 0)
        self.assertIn("sites_evpn_remote", out)
        self.assertIn("no anycast RP is modelled", out)

    def test_a_spine_without_its_own_object_is_refused(self):
        """Every spine needs one: the interface name comes from it, which is
        what stops the address and the interface drifting apart."""
        rc, out, _ = run_rp([row("10.0.0.100/32", "core1"),
                             row("10.1.0.100/32", "rcore1")])
        self.assertNotEqual(rc, 0)
        self.assertIn("core2", out)
        self.assertIn("carries no anycast RP IPAddress of its own", out)

    def test_the_failure_says_there_is_no_fallback(self):
        rc, out, _ = run_rp([row("10.0.0.100/32", "core1"),
                             row("10.0.0.100/32", "core2")])
        self.assertIn("NetBox is the only source", out)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
