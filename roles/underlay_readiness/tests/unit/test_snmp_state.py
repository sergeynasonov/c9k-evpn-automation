# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Reading SNMPv3 state off a switch that has no SNMP.

The regression, found on a freshly built fabric: a switch that has never had
SNMP configured answers 'show snmp user' with '%SNMP agent not enabled', and
cisco.ios treats a leading '%' as a device error. Every one of seven switches
died in 01_gather.yml before a single check ran -- on precisely the condition
this role exists to fix, and which 03_apply_snmp.yml would have fixed two tasks
later.

Two properties keep it fixed, and both are tested here:

  the read is NON-FATAL and gets its own task    ios_command raises on the first
      command that errors and returns no stdout at all, so sharing a task with
      the OSPF read would have lost the OSPF output too -- every switch silently
      reporting zero routers in its area.

  a read that produced nothing means "not seen"  01_gather.yml normalises the
      missing output to an empty string, so the check reports the user and group
      missing and the apply task installs them. Nothing downstream has to know
      the read is allowed to fail.

The device calls cannot run offline, so they are supplied as data in the shape
ios_command registers them. Everything else is the shipped file, lifted at run
time.
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

GATHER = os.path.join(ROLE, "tasks", "01_gather.yml")
CHECK = os.path.join(ROLE, "tasks", "02_check.yml")
HARNESS = os.path.join(FIXTURES, "snmp_state_harness.yml")
INVENTORY = os.path.join(FIXTURES, "msdp_two_fabrics.ini")

ANSIBLE = shutil.which("ansible-playbook")

USER = "example-snmpv3"
GROUP = "AuthPrivGroup"

# What ios_command registers when the command errored and 'failed_when: false'
# swallowed it: a result carrying a message and NO stdout key whatsoever.
AGENT_OFF = {"failed": False, "msg": "%SNMP agent not enabled"}


def derive_task():
    """The whole 'derive underlay facts' task from 01_gather.yml, verbatim."""
    with open(GATHER) as fh:
        tasks = yaml.safe_load(fh)
    for task in tasks:
        if task.get("name", "").startswith("Readiness — derive underlay facts"):
            return [task]
    raise AssertionError("no derive task found in " + GATHER)


def snmp_check_task():
    """The shipped '_snmp' expression, wrapped in a task of its own.

    02_check.yml computes every category in one set_fact so the play prints one
    line per host. The others need a running config this test does not model.
    """
    with open(CHECK) as fh:
        tasks = yaml.safe_load(fh)
    for task in tasks:
        snmp = (task.get("vars") or {}).get("_snmp")
        if snmp:
            return [{
                "name": "SNMP check",
                "ansible.builtin.set_fact": {"snmp_findings": "{{ _snmp | from_json }}"},
                "vars": {"_snmp": snmp},
            }]
    raise AssertionError("no '_snmp' var found in " + CHECK)


def snmp_ok(user=USER, group=GROUP):
    """'show snmp user' / 'show snmp group' from a switch that has both."""
    return {"stdout": [
        "\nUser name: %s\nEngine ID: 800000090300AABBCCDDEEFF\n"
        "storage-type: nonvolatile        active\n"
        "Authentication Protocol: SHA\nPrivacy Protocol: AES128\n"
        "Group-name: %s\n" % (user, group),
        "\ngroupname: %s                        security model:v3 priv \n"
        "readview : v1default                   writeview: <no writeview specified>\n"
        "notifyview: <no notifyview specified>\nrow status: active\n" % group,
    ]}


def ospf_ok(routers=7):
    return {"stdout": [
        "\n            OSPF Router with ID (10.0.0.1) (Process ID 1)\n\n"
        "                Area 0 database summary\n"
        "  LSA Type      Count    Delete   Maxage\n"
        "  Router        %d        0        0\n"
        "  Network       6        0        0\n" % routers,
    ]}


def run(snmp_reads, ospf_reads):
    workdir = tempfile.mkdtemp(prefix="snmp-state-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        for name, tasks in (("derive.yml", derive_task()),
                            ("snmp_check.yml", snmp_check_task())):
            with open(os.path.join(workdir, name), "w") as fh:
                yaml.safe_dump(tasks, fh, default_flow_style=False,
                               sort_keys=False, allow_unicode=True)

        result_file = os.path.join(workdir, "result.json")
        env = dict(os.environ, ANSIBLE_ROLES_PATH=ROLES, ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [ANSIBLE, "-i", INVENTORY, "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", json.dumps({"snmp_reads": snmp_reads, "ospf_reads": ospf_reads})],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        if not os.path.exists(result_file):
            raise AssertionError("harness produced no result:\n" + output)
        with open(result_file) as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFreshSwitchWithNoSnmp(unittest.TestCase):
    """The switches from the failing run: nothing configured, agent off."""

    @classmethod
    def setUpClass(cls):
        cls.result = run({"core1": AGENT_OFF}, {"core1": ospf_ok()})
        cls.host = cls.result["core1"]

    def test_the_run_survives_a_read_that_produced_nothing(self):
        """It did not, which is the whole bug. Reaching this assertion at all
        means the derive task templated without an undefined-variable error."""
        self.assertNotEqual(self.host["users"], "__unset__")
        self.assertNotEqual(self.host["groups"], "__unset__")

    def test_missing_output_normalises_to_an_empty_string(self):
        self.assertEqual(self.host["users"], "")
        self.assertEqual(self.host["groups"], "")

    def test_the_user_and_group_are_reported_missing(self):
        msgs = " ".join(self.host["msgs"])
        self.assertIn(USER, msgs)
        self.assertIn(GROUP, msgs)
        self.assertEqual(len(self.host["msgs"]), 2, self.host["msgs"])

    def test_the_ospf_read_is_unaffected(self):
        """The reason the two reads are separate tasks: one shared task loses
        the OSPF output as well, and every switch reports an empty area."""
        self.assertEqual(int(self.host["ospf_routers"]), 7)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSwitchWithSnmpConfigured(unittest.TestCase):
    """Tolerating a failed read must not blunt the check it exists to serve."""

    @classmethod
    def setUpClass(cls):
        cls.result = run(
            {"core1": snmp_ok(),
             "core2": snmp_ok(user="somebody-else"),
             "rcore1": snmp_ok(group="SomeOtherGroup")},
            {h: ospf_ok() for h in ("core1", "core2", "rcore1")})

    def test_a_configured_switch_is_clean(self):
        self.assertEqual(self.result["core1"]["msgs"], [])

    def test_the_wrong_user_is_still_caught(self):
        msgs = " ".join(self.result["core2"]["msgs"])
        self.assertIn(USER, msgs)
        self.assertNotIn(GROUP + '"', msgs)

    def test_the_wrong_group_is_still_caught(self):
        self.assertIn(GROUP, " ".join(self.result["rcore1"]["msgs"]))


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSiblingFactsAreNotReadAcrossOneSetFact(unittest.TestCase):
    """A set_fact key cannot read a sibling key of the same task.

    '_readiness_routing_enabled' used to read '_readiness_ospf_routers', which
    is set beside it. That is an undefined-variable crash, and it survived
    because Jinja's 'or' short-circuits: every switch in service carries either
    'ip routing' or 'router ospf 1', so the term reaching the sibling was never
    evaluated. A C9400/9500/9600 with OSPF not yet configured carries neither --
    'ip routing' is that platform's silent default and is never rendered -- and
    it is exactly the switch this role should report on.

    The harness models it directly: every case here runs with an EMPTY indexed
    config, so nothing short-circuits and the sibling is always reached.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run({"core1": snmp_ok()}, {"core1": ospf_ok(routers=0)})
        cls.host = cls.result["core1"]

    def test_a_config_with_neither_marker_derives_rather_than_crashes(self):
        self.assertNotEqual(self.host["routing_enabled"], "__unset__")

    def test_and_the_verdict_is_the_one_the_check_reports_on(self):
        """False, so 02_check.yml emits 'IP routing does not appear to be
        enabled' -- a finding, which is the point. Crashing told the operator
        nothing and took the other six switches with it."""
        self.assertEqual(self.host["routing_enabled"], False)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestOspfReadThatProducedNothing(unittest.TestCase):
    """Same treatment, same reasoning: a switch with no OSPF process is
    something the checks REPORT, not something to abort a fabric run on."""

    @classmethod
    def setUpClass(cls):
        cls.result = run({"core1": snmp_ok()},
                         {"core1": {"failed": False, "msg": "no such process"}})

    def test_the_run_survives(self):
        self.assertNotEqual(self.result["core1"]["ospf_routers"], "__unset__")

    def test_the_area_reads_as_empty_rather_than_erroring(self):
        self.assertEqual(int(self.result["core1"]["ospf_routers"]), 0)

    def test_snmp_is_unaffected(self):
        self.assertEqual(self.result["core1"]["msgs"], [])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
