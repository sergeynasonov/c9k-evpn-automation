# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""The system-MTU floor, and the fabric consistency that goes with it.

This role used to demand 'system mtu 9100' as an exact line, which failed every
fabric built on any other value. The requirement is a floor plus agreement:

  FLOOR         1550, which is 1500 + 50 rather than a number of its own: VXLAN
                adds 50 bytes of outer header to a standard 1500-byte tenant
                frame. Above it the value is the fabric's own choice, and 9100
                is a common one rather than a requirement.

  CONSISTENCY   OSPF carries the interface MTU in its DBD packets and refuses
                the adjacency when the two ends disagree. Checked, but secondary
                to the floor because of how it presents: a leaf or distribution
                switch out of step forms no adjacency, so its loopback never
                reaches the underlay and Ansible sees it as UNREACHABLE rather
                than as an MTU finding. A BORDER stays reachable over its BGP
                handoff, and is what this branch actually catches.

Only the floor proposes a command, and it is 9100 flat -- the same literal
platform_baseline.j2 installs, which one test here exists to keep in step.

Everything under test is Jinja inside a set_fact or a template, where
--syntax-check evaluates nothing, so each piece is lifted out of the shipped
file at run time and evaluated for real. Nothing is copied, so the tests cannot
drift from the role.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE = os.path.dirname(TESTS)
ROLES = os.path.dirname(ROLE)
FIXTURES = os.path.join(TESTS, "fixtures")

GATHER = os.path.join(ROLE, "tasks", "01_gather.yml")
CHECK = os.path.join(ROLE, "tasks", "02_check.yml")
BASELINE = os.path.join(ROLE, "templates", "platform_baseline.j2")
HARNESS = os.path.join(FIXTURES, "system_mtu_harness.yml")
# Reused rather than duplicated: two fabrics is what proves the consensus is
# fabric-scoped, which is the same property the MSDP mesh needed.
INVENTORY = os.path.join(FIXTURES, "msdp_two_fabrics.ini")

ANSIBLE = shutil.which("ansible-playbook")

FLOOR = 1550
CAMPUS = ("core1", "core2", "access1-1")


def _var_task(path, key, sets, when="_stanzas is defined"):
    """Lift one 'vars' expression out of a shipped file, verbatim.

    Both files compute several categories in one set_fact so the play prints one
    line per host instead of one per category. Only the MTU expressions are
    wanted here, and the others need OSPF and SNMP state from a device, so each
    is evaluated on its own -- never retyped.
    """
    with open(path) as fh:
        tasks = yaml.safe_load(fh)
    for task in tasks:
        expr = (task.get("vars") or {}).get(key)
        if expr:
            return [{
                "name": "Lifted %s" % key,
                "ansible.builtin.set_fact": sets,
                "vars": {key: expr},
                "when": when,
            }]
    raise AssertionError("no %r var found in %s" % (key, path))


def _named_task(path, prefix):
    """Lift a whole shipped task by name. Used for the consensus, which is a
    task of its own precisely because it is cross-device."""
    with open(path) as fh:
        tasks = yaml.safe_load(fh)
    for task in tasks:
        if task.get("name", "").startswith(prefix):
            return [task]
    raise AssertionError("no task named %r in %s" % (prefix, path))


def stanzas(mtu=None, extra=()):
    """A running config as 01_gather.yml's indexer would have keyed it.

    'mtu=None' is a switch with no 'system mtu' line -- the platform default of
    1500, which IOS-XE does not render.
    """
    out = {"ip multicast-routing": []}
    if mtu is not None:
        out["system mtu %d" % mtu] = []
    for line in extra:
        out[line] = []
    return out


def run_check(mtu_stanzas):
    """Run the real MTU logic over a {hostname: stanzas} fixture.

    A host absent from the mapping gets no '_stanzas' at all, which is how an
    unreachable switch reaches these tasks.
    """
    workdir = tempfile.mkdtemp(prefix="system-mtu-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        lifted = {
            "mtu_parse.yml": _var_task(
                GATHER, "_sys_mtu", {"_readiness_system_mtu": "{{ _sys_mtu }}"}),
            "mtu_consensus.yml": _named_task(
                GATHER, "Readiness — resolve the fabric's system MTU"),
            "mtu_check.yml": _var_task(
                CHECK, "_mtu", {"mtu_findings": "{{ _mtu | from_json }}"}),
        }
        for name, tasks in lifted.items():
            with open(os.path.join(workdir, name), "w") as fh:
                yaml.safe_dump(tasks, fh, default_flow_style=False,
                               sort_keys=False, allow_unicode=True)

        result_file = os.path.join(workdir, "result.json")
        env = dict(os.environ, ANSIBLE_ROLES_PATH=ROLES, ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [ANSIBLE, "-i", INVENTORY, "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", json.dumps({"mtu_stanzas": mtu_stanzas})],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        if not os.path.exists(result_file):
            raise AssertionError("harness produced no result:\n" + output)
        with open(result_file) as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def fabric(campus_mtus, remote_mtu=9100):
    """A {host: stanzas} fixture from a {host: mtu} mapping for the campus."""
    out = {h: stanzas(m) for h, m in campus_mtus.items()}
    out["rcore1"] = stanzas(remote_mtu)
    return out


class MtuTestCase(unittest.TestCase):
    def msgs(self, host):
        return self.result[host]["msgs"]

    def lines(self, host):
        return self.result[host]["lines"]

    def assertClean(self, *hosts):
        for host in hosts:
            self.assertEqual(self.msgs(host), [], host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFloorIsAFloorNotAValue(MtuTestCase):
    """The change itself: 9100 was never the requirement."""

    @classmethod
    def setUpClass(cls):
        # Every value here is above the floor, and each fabric is internally
        # consistent -- so every switch is ready.
        cls.result = run_check(fabric(
            {"core1": 1600, "core2": 1600, "access1-1": 1600}, remote_mtu=9216))

    def test_a_fabric_standardised_below_9100_is_ready(self):
        """The regression this change exists for: the old exact-match on
        'system mtu 9100' failed all three of these switches."""
        self.assertClean(*CAMPUS)

    def test_a_fabric_standardised_above_9100_is_ready(self):
        self.assertClean("rcore1")

    def test_the_fabric_value_is_left_alone_not_rewritten(self):
        """Nothing is emitted, so nothing drags 1600 towards 9100."""
        for host in CAMPUS:
            self.assertEqual(self.lines(host), [], host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFloorBoundary(MtuTestCase):
    """'at or above 1550', so both sides of the boundary are pinned down. One
    byte below is a fabric that carries its own traffic and silently mangles a
    full-size tenant frame."""

    @classmethod
    def setUpClass(cls):
        cls.at = run_check(fabric(dict.fromkeys(CAMPUS, FLOOR),
                                  remote_mtu=FLOOR))
        cls.below = run_check(fabric(dict.fromkeys(CAMPUS, FLOOR - 1),
                                     remote_mtu=FLOOR - 1))

    def test_exactly_at_the_floor_is_accepted(self):
        for host in CAMPUS:
            self.assertEqual(self.at[host]["msgs"], [], host)

    def test_one_byte_below_the_floor_is_rejected(self):
        for host in CAMPUS:
            self.assertIn("is below the 1550", " ".join(self.below[host]["msgs"]),
                          host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestGreenfieldFabric(MtuTestCase):
    """No switch has a 'system mtu' line, so every one of them reads as the
    1500 platform default -- consistent with each other, and 50 bytes short of
    what VXLAN needs. Bringing them up is platform_baseline.j2's job; see
    TestBaselineOnlyMovesAnUnconfiguredSwitch."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_check(fabric(dict.fromkeys(CAMPUS, None),
                                      remote_mtu=None))

    def test_an_absent_line_reads_as_the_platform_default(self):
        self.assertEqual(int(self.result["core1"]["mtu"]), 1500)

    def test_the_whole_fabric_is_below_the_floor_and_reported(self):
        for host in CAMPUS:
            self.assertIn("is below the 1550", " ".join(self.msgs(host)), host)

    def test_the_finding_says_the_line_is_absent_rather_than_quoting_it(self):
        self.assertIn('no "system mtu" line is configured',
                      " ".join(self.msgs("core1")))

    def test_consistent_but_too_low_is_the_floor_finding_only(self):
        """They agree with each other, so the mismatch branch must stay quiet:
        one fault, one finding."""
        for host in CAMPUS:
            self.assertEqual(len(self.msgs(host)), 1, host)
            self.assertNotIn("does not agree", " ".join(self.msgs(host)), host)

    def test_every_switch_is_told_to_use_the_build_value(self):
        for host in CAMPUS:
            self.assertEqual(self.lines(host), ["system mtu 9100"], host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestRemediationProposesTheBuildValue(MtuTestCase):
    """A command is emitted for the floor and nothing else, and it is 9100 flat.

    A switch below the floor cannot hold an adjacency with the fabric anyway,
    so it is not joining an established one -- it is being built, and 9100 is
    the build value.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run_check(fabric(
            {"core1": 1600, "core2": 1600, "access1-1": 1400}))
        # A below-floor switch whose fabric has not settled on anything: core1
        # and core2 disagree, so there is nothing to inherit either way.
        cls.no_consensus = run_check(fabric(
            {"core1": 9100, "core2": 1600, "access1-1": 1400}))

    def test_the_below_floor_switch_is_told_to_use_9100(self):
        self.assertEqual(self.lines("access1-1"), ["system mtu 9100"])

    def test_the_proposal_does_not_vary_with_the_fabric(self):
        """Same command whether the fabric sits at 1600 or disagrees entirely,
        so a remediation file never depends on who else answered this run."""
        self.assertEqual(self.no_consensus["access1-1"]["lines"],
                         ["system mtu 9100"])

    def test_the_established_switches_are_untouched(self):
        self.assertClean("core1", "core2")

    def test_one_finding_not_two_for_the_same_fault(self):
        """Below the floor AND out of step with the fabric is one problem with
        one fix; reporting it twice is how a summary stops being readable."""
        self.assertEqual(len(self.msgs("access1-1")), 1, self.msgs("access1-1"))

    def test_the_proposal_matches_what_the_baseline_installs(self):
        """The literal 9100 lives in both 02_check.yml and platform_baseline.j2
        rather than in a variable, so this is what stops them drifting: an
        operator who pastes the remediation must land where the standalone
        readiness run would have put the switch anyway."""
        rendered = [line.strip() for line in render_baseline(1500).splitlines()
                    if line.strip().startswith("system mtu ")]
        self.assertEqual(rendered, self.lines("access1-1"))


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestInconsistentFabricIsCaught(MtuTestCase):
    """Both values are above the floor, so nothing is wrong with either switch
    on its own. OSPF still will not form the adjacency."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_check(fabric(
            {"core1": 9100, "core2": 9100, "access1-1": 1600}))

    def test_the_odd_one_out_is_reported(self):
        self.assertIn("does not agree", " ".join(self.msgs("access1-1")))

    def test_the_majority_is_not_reported(self):
        """Flagging all three would point remediation at the wrong switches."""
        self.assertClean("core1", "core2")

    def test_the_finding_names_the_split(self):
        msg = " ".join(self.msgs("access1-1"))
        self.assertIn("1600 on access1-1", msg)
        self.assertIn("9100 on core1, core2", msg)

    def test_no_command_is_emitted_for_a_mismatch(self):
        """Changing system MTU bounces every interface on the box, and which
        end moves is a judgement about the fabric, not about this switch."""
        self.assertEqual(self.lines("access1-1"), [])

    def test_the_other_fabric_is_unaffected(self):
        self.assertClean("rcore1")


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNoMajorityFlagsEveryone(MtuTestCase):
    """A two-way split with nothing to break the tie. There is no value to
    defer to, so neither switch may be assumed correct."""

    @classmethod
    def setUpClass(cls):
        # access1-1 is unreachable, leaving core1 and core2 tied 1-1.
        cls.result = run_check({"core1": stanzas(9100),
                                "core2": stanzas(1600),
                                "rcore1": stanzas(9100)})

    def test_the_fabric_reports_no_consensus(self):
        self.assertEqual(int(self.result["core1"]["fabric_mtu"]), 0)

    def test_both_sides_of_the_tie_are_reported(self):
        for host in ("core1", "core2"):
            self.assertIn("does not agree", " ".join(self.msgs(host)), host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestScopeAndReachability(MtuTestCase):
    """What the consensus is allowed to count, which is what keeps it honest."""

    @classmethod
    def setUpClass(cls):
        # Only core1 was read. core2 and access1-1 are unreachable; the remote
        # fabric is at a different MTU entirely.
        cls.result = run_check({"core1": stanzas(9100), "rcore1": stanzas(1600)})

    def test_a_second_fabric_at_a_different_mtu_is_not_a_mismatch(self):
        """Separate underlays, no adjacency between them, nothing to agree on."""
        self.assertClean("core1", "rcore1")

    def test_consensus_is_scoped_to_the_fabric(self):
        self.assertEqual(int(self.result["core1"]["fabric_mtu"]), 9100)
        self.assertEqual(int(self.result["rcore1"]["fabric_mtu"]), 1600)

    def test_unreachable_members_do_not_manufacture_a_mismatch(self):
        """A switch we never read has no MTU to disagree with. Counting it as
        anything -- including 0 -- would report a fault that does not exist."""
        self.assertClean("core1")

    def test_a_switch_alone_in_the_run_agrees_with_itself(self):
        """A run scoped to one switch can only check the floor. It must not
        claim to have verified consistency, and must not invent a mismatch."""
        self.assertEqual(int(self.result["rcore1"]["fabric_mtu"]),
                         int(self.result["rcore1"]["mtu"]))

    def test_fabrics_resolved_as_expected(self):
        """Guards the fixture: if these collapsed into one fabric the scoping
        assertions above would pass for the wrong reason."""
        self.assertEqual(self.result["core1"]["fabric_id"], "site_group_evpn_campus")
        self.assertEqual(self.result["rcore1"]["fabric_id"], "sites_evpn_remote")


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestParsingRejectsSiblingCommands(MtuTestCase):
    """IOS has sibling commands in this space. A prefix match on 'system mtu '
    reads 'routing' as the number and yields 0 -- silently, and on a switch
    whose real MTU is the 1500 default, so the fault surfaces as a fabric that
    disagrees rather than as a parsing bug."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_check({
            # A sibling command and nothing else: this switch has no system MTU
            # set, and must be read as the 1500 default rather than as 9100.
            "core1": stanzas(None, extra=["system mtu routing 9100"]),
            "core2": stanzas(9100),
            "rcore1": stanzas(9100),
        })

    def test_a_sibling_command_is_not_read_as_the_system_mtu(self):
        self.assertEqual(int(self.result["core1"]["mtu"]), 1500)

    def test_the_finding_quotes_the_default_rather_than_a_parse_failure(self):
        """A prefix match yields 0, and 'system mtu 0' in a remediation file is
        the kind of number an operator pastes at three in the morning."""
        self.assertIn("system mtu 1500", " ".join(self.msgs("core1")))
        self.assertNotIn("system mtu 0", " ".join(self.msgs("core1")))


def render_baseline(mtu):
    """Render the shipped platform baseline for a switch at this MTU.

    'mtu=None' stands in for an unreachable switch, which never reached the
    gather and so has no fact at all.
    """
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined,
                             keep_trailing_newline=True)
    # The template is rendered by Ansible, which has filters bare Jinja does
    # not. 'bool' for the routing conditionals, 'from_json' for the optional
    # syslog block -- and 'from_json' is not optional to register: Jinja
    # resolves filters when it COMPILES, so an unknown one raises here rather
    # than at render time, whatever context is passed.
    env.filters["bool"] = lambda v: str(v).lower() in ("true", "yes", "1", "on")
    env.filters["from_json"] = json.loads
    with open(BASELINE) as fh:
        template = env.from_string(fh.read())
    # An empty list is a deployment that has not enrolled syslog, which is the
    # right neutral for an MTU test. The enrolled case is covered in
    # test_optional_enrolment.py.
    ctx = {"readiness_system_mtu_min": FLOOR, "readiness_syslog_lines": "[]"}
    if mtu is not None:
        ctx["_readiness_system_mtu"] = mtu
    return template.render(**ctx)


class TestBaselineOnlyMovesAnUnconfiguredSwitch(unittest.TestCase):
    """The baseline still suggests 9100, but 'ios_config' MERGES: an
    unconditional 'system mtu 9100' is missing from the running config of a
    switch at 1600, so every readiness run would push it and bounce every
    interface on the box. Invisible while the check demanded 9100 anyway.
    """

    @staticmethod
    def render(mtu):
        return render_baseline(mtu)

    def test_a_switch_still_on_the_platform_default_is_brought_to_9100(self):
        self.assertIn("system mtu 9100", self.render(1500))

    def test_a_fabric_deliberately_at_1600_is_not_stomped(self):
        """The regression. 1600 is valid and deliberate, and re-pushing 9100
        over it every run would be both wrong and disruptive."""
        self.assertNotIn("system mtu", self.render(1600))

    def test_a_switch_already_at_9100_renders_nothing(self):
        self.assertNotIn("system mtu", self.render(9100))

    def test_a_switch_exactly_at_the_floor_is_left_alone(self):
        """The floor is inclusive here as it is in the check, so the two cannot
        disagree about whether 1550 is acceptable."""
        self.assertNotIn("system mtu", self.render(FLOOR))

    def test_an_unreachable_switch_renders_without_raising(self):
        """The fact is only set once the device has been read. Rendering must
        not fail before the connection does, or the failure names the wrong
        cause."""
        self.assertNotIn("system mtu", self.render(None))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
