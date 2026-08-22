# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""SNMPv3 and syslog are optional, and deployment.yml is what enrols them.

Both used to be unconditional. SNMPv3 was worse than merely unconditional: the
identity keys were REQUIRED, so a deployment with no SNMP collector could not
run the role at all without inventing one -- and having invented one, every
switch in the fabric then reported two findings, permanently and unfixably, for
a collector that does not exist.

The rule now, for both blocks:

  ENROLLED BY PRESENCE  the keys in deployment.yml are the switch. Declared, and
      the role reads, pushes, checks and reports. Commented out, and it does
      none of those -- including the operational-state read, which is a device
      round-trip bought for nothing on a fabric with no SNMP.

  ALL OR NOTHING        half an SNMP triple is an error, not an opt-out. This is
      the failure mode the mechanism itself introduces: once absence means
      "no", a mistyped key name reads exactly like a deliberate decision and
      would silently stop configuring every switch in the fabric.

  A DECLARED VALUE IS VALIDATED   a syslog entry that is not a bare IPv4 address
      fails the run. Same reasoning: dropping it quietly would turn a typo into
      a silent opt-out, and '10.0.0.1/24' is the case that matters because the
      ipv4 filter accepts it and hands back the host part.

The syslog CLI is defined once, in the role defaults, and consumed by both the
template that pushes it and the check that verifies it. TestPushAndCheckAgree
is what that single definition buys, and is the test to keep if any other here
is ever dropped.

Everything under test is Jinja inside a set_fact, a template or an assert, where
--syntax-check evaluates nothing, so each piece is lifted out of the shipped
file at run time. Nothing is copied.
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

DEFAULTS = os.path.join(ROLE, "defaults", "main.yml")
CHECK = os.path.join(ROLE, "tasks", "02_check.yml")
SITE_ASSERT = os.path.join(ROLE, "tasks", "assert_site_config.yml")
BASELINE = os.path.join(ROLE, "templates", "platform_baseline.j2")
HARNESS = os.path.join(FIXTURES, "optional_enrolment_harness.yml")
INVENTORY = os.path.join(FIXTURES, "msdp_two_fabrics.ini")

ANSIBLE = shutil.which("ansible-playbook")

HOST = "core1"
USER = "example-snmpv3"
GROUP = "AuthPrivGroup"
# RFC 5737 documentation range, matching deployment.yml.example. A test that
# hard-codes a real collector reads as one, and this file is the worked
# example an operator copies from.
COLLECTOR = "198.51.100.30"
COLLECTOR_2 = "198.51.100.31"

# A deployment.yml with SNMPv3 enrolled and syslog not.
SNMP_ONLY = {"snmp_v3_user": USER, "snmp_v3_group": GROUP,
             "snmp_acl_name": "SNMP-ACL"}
# The keys that are required whatever else is or is not declared.
ALWAYS = {"ntp_servers": ["198.51.100.10", "198.51.100.11"],
          "vtp_domain": "EXAMPLE"}


def _var_task(path, key, sets):
    """Lift one 'vars' expression out of a shipped file, verbatim.

    02_check.yml computes every category in one set_fact so the play prints one
    line per host; the others need a running config and OSPF state this test
    does not model, so each wanted expression is evaluated on its own.
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
            }]
    raise AssertionError("no %r var found in %s" % (key, path))


def _site_assert_task():
    """The whole assert task from assert_site_config.yml.

    'register' and 'ignore_errors' are added -- and nothing else -- so that a
    rejected deployment is something to assert on rather than an aborted play.
    """
    with open(SITE_ASSERT) as fh:
        tasks = yaml.safe_load(fh)
    task = tasks[0]
    task["register"] = "site_assert"
    task["ignore_errors"] = True
    return [task]


def run(deployment, stanzas=None, snmp_users="", snmp_groups="", trap_seen=""):
    """Run the real enrolment logic against one deployment.yml.

    'deployment' is the file's contents as a dict: a key absent from it is a
    key commented out, which is the whole subject of this test. ALWAYS is
    merged in, since NTP and VTP are required either way -- a case that wants
    them missing passes them as None.
    """
    workdir = tempfile.mkdtemp(prefix="enrolment-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        # Verbatim, so the enrolment expressions under test are the shipped
        # ones. vars_files sits below extra vars, which is what lets each case
        # supply its own deployment.yml on the command line.
        shutil.copy(DEFAULTS, os.path.join(workdir, "defaults.yml"))
        lifted = {
            "site_assert.yml": _site_assert_task(),
            "snmp_check.yml": _var_task(
                CHECK, "_snmp", {"snmp_findings": "{{ _snmp | from_json }}"}),
            "syslog_check.yml": _var_task(
                CHECK, "_syslog", {"syslog_findings": "{{ _syslog | from_json }}"}),
            "ntp_check.yml": _var_task(
                CHECK, "_ntp", {"ntp_findings": "{{ _ntp | from_json }}"}),
        }
        for name, tasks in lifted.items():
            with open(os.path.join(workdir, name), "w") as fh:
                yaml.safe_dump(tasks, fh, default_flow_style=False,
                               sort_keys=False, allow_unicode=True)

        extra = dict(ALWAYS)
        extra.update(deployment)
        # None means "not in the file at all"; extra vars cannot express
        # absence, so the key is dropped instead.
        extra = {k: v for k, v in extra.items() if v is not None}
        extra.update({"stanzas": stanzas or {},
                      "snmp_users": snmp_users, "snmp_groups": snmp_groups,
                      "trap_seen": trap_seen})

        result_file = os.path.join(workdir, "result.json")
        env = dict(os.environ, ANSIBLE_ROLES_PATH=ROLES, ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [ANSIBLE, "-i", INVENTORY, "harness.yml",
             "-e", "result_file=" + result_file, "-e", json.dumps(extra)],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        if not os.path.exists(result_file):
            raise AssertionError("harness produced no result:\n" + output)
        with open(result_file) as fh:
            return json.load(fh)[HOST]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def render_baseline(syslog_cli, ntp_cli='["ntp server 198.51.100.10"]'):
    """Render the shipped platform baseline for a given syslog enrolment.

    Both arguments are as Ansible hands them over: the JSON strings the
    defaults produce. NTP is required, so its default here is a populated list
    rather than an empty one -- an empty one is a deployment the assert refuses.
    """
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined,
                             keep_trailing_newline=True)
    # The template is rendered by Ansible, which has filters bare Jinja does
    # not. Only these two are used in it.
    env.filters["bool"] = lambda v: str(v).lower() in ("true", "yes", "1", "on")
    env.filters["from_json"] = json.loads
    with open(BASELINE) as fh:
        template = env.from_string(fh.read())
    return template.render(readiness_syslog_lines=syslog_cli,
                           readiness_ntp_lines=ntp_cli,
                           readiness_system_mtu_min=1550,
                           _readiness_system_mtu=9100,
                           vtep_source_intf="Loopback0")


def baseline_ntp(ntp_cli):
    """The 'ntp server' lines the baseline would push, in order."""
    return [line.rstrip()
            for line in render_baseline("[]", ntp_cli).splitlines()
            if line.startswith("ntp server ")]


def baseline_logging(syslog_cli):
    """The GLOBAL logging lines the baseline would push, in order.

    Column 0 only, deliberately: 'logging synchronous' under each vty line and
    'logging enable' under archive are children of other stanzas, and stripping
    the indent before matching would pull them in and make this assert nothing.
    """
    return [line.rstrip() for line in render_baseline(syslog_cli).splitlines()
            if line.startswith("logging ")]


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNothingDeclared(unittest.TestCase):
    """A deployment.yml with both optional blocks commented out.

    The switch is a fresh one: no SNMP user, no syslog, nothing in the config.
    Before this change that was two permanent findings on every host.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run({})

    def test_neither_block_is_enrolled(self):
        self.assertFalse(self.result["snmp_enrolled"])
        self.assertFalse(self.result["syslog_enrolled"])

    def test_the_run_is_accepted(self):
        """NTP and VTP alone are a complete deployment.yml."""
        self.assertFalse(self.result["assert_failed"], self.result["assert_msg"])

    def test_no_snmp_finding_for_a_collector_that_does_not_exist(self):
        self.assertEqual(self.result["snmp_msgs"], [])

    def test_no_syslog_finding_either(self):
        self.assertEqual(self.result["syslog_msgs"], [])

    def test_and_nothing_is_pushed(self):
        self.assertEqual(self.result["syslog_cli"], [])
        self.assertEqual(baseline_logging("[]"),
                         ["logging buffered 1000000",
                          "logging source-interface Loopback0"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSnmpDeclaredIsStillChecked(unittest.TestCase):
    """Making it optional must not blunt it for a deployment that wants it."""

    @classmethod
    def setUpClass(cls):
        cls.fresh = run(SNMP_ONLY)
        cls.configured = run(SNMP_ONLY,
                             snmp_users="User name: %s\n" % USER,
                             snmp_groups="groupname: %s\n" % GROUP)

    def test_declaring_the_triple_enrols_it(self):
        self.assertTrue(self.fresh["snmp_enrolled"])

    def test_a_switch_without_it_still_reports_both(self):
        msgs = " ".join(self.fresh["snmp_msgs"])
        self.assertEqual(len(self.fresh["snmp_msgs"]), 2, self.fresh["snmp_msgs"])
        self.assertIn(USER, msgs)
        self.assertIn(GROUP, msgs)

    def test_a_switch_with_it_is_clean(self):
        self.assertEqual(self.configured["snmp_msgs"], [])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestHalfAnSnmpTripleIsRejected(unittest.TestCase):
    """The failure mode enrolment-by-presence introduces, and the reason the
    triple is all-or-none: 'snmp_v3_gruop' reads exactly like an opt-out, and
    would quietly stop configuring SNMP on every switch in the fabric."""

    @classmethod
    def setUpClass(cls):
        partial = dict(SNMP_ONLY)
        del partial["snmp_acl_name"]
        cls.result = run(partial)
        # Only one key left set, which is the other half of the sentence below.
        cls.lonely = run({"snmp_v3_user": USER})

    def test_the_run_is_refused(self):
        self.assertTrue(self.result["assert_failed"])

    def test_the_message_names_the_missing_key(self):
        self.assertIn("snmp_acl_name", self.result["assert_msg"])

    def test_and_says_it_is_all_or_none(self):
        self.assertIn("all three", self.result["assert_msg"])

    def test_the_sentence_agrees_with_how_many_keys_are_set(self):
        """Two survivors take 'are', one takes 'is'. Petty on its own, except
        that this message is the entire user interface of the all-or-none rule
        -- an operator who stumbles on the grammar re-reads the sentence
        instead of the key name it is pointing at."""
        self.assertIn("snmp_v3_user and snmp_v3_group are", self.result["assert_msg"])
        self.assertNotIn("snmp_v3_group is --", self.result["assert_msg"])

    def test_and_reads_correctly_with_a_single_survivor(self):
        self.assertIn("snmp_v3_user is --", self.lonely["assert_msg"])

    def test_every_missing_key_gets_its_own_line(self):
        """Two keys missing, two findings -- not one naming both, which is
        harder to scan and harder to grep for."""
        self.assertIn("snmp_v3_group is not set", self.lonely["assert_msg"])
        self.assertIn("snmp_acl_name is not set", self.lonely["assert_msg"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestRequiredKeysAreStillRequired(unittest.TestCase):
    """Optional means SNMPv3 and syslog. NTP and VTP are not negotiable: the
    platform baseline cannot be rendered without them, and a fabric with no
    time source cannot be read from its own logs."""

    def test_a_missing_vtp_domain_fails(self):
        result = run({"vtp_domain": None})
        self.assertTrue(result["assert_failed"])
        self.assertIn("vtp_domain", result["assert_msg"])

    def test_an_absent_ntp_list_fails(self):
        result = run({"ntp_servers": None})
        self.assertTrue(result["assert_failed"])
        self.assertIn("ntp_servers", result["assert_msg"])

    def test_an_empty_ntp_list_fails_where_an_empty_syslog_list_does_not(self):
        """The one place the two address lists differ. Everything else about
        them -- shape, validation, error wording -- is one shared loop."""
        empty_ntp = run({"ntp_servers": []})
        empty_syslog = run({"syslog_hosts": []})
        self.assertTrue(empty_ntp["assert_failed"])
        self.assertFalse(empty_syslog["assert_failed"], empty_syslog["assert_msg"])

    def test_the_ntp_list_is_validated_like_the_syslog_one(self):
        for bad in ("198.51.100.10/24", "ntp.example.com", "300.1.1.1"):
            result = run({"ntp_servers": [bad]})
            self.assertTrue(result["assert_failed"], bad)
            self.assertIn("bare IPv4", result["assert_msg"])

    def test_one_ntp_server_is_accepted(self):
        """Two or more is better and the docs say so, but 'at least two' is a
        fabric-wide policy this role has no business enforcing."""
        result = run({"ntp_servers": ["198.51.100.10"]})
        self.assertFalse(result["assert_failed"], result["assert_msg"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSupersededNtpKeysAreNamed(unittest.TestCase):
    """ntp_server_1 / ntp_server_2 became ntp_servers.

    Without this the un-migrated file gets 'ntp_servers is required and names
    no NTP server' while two NTP servers sit in plain sight three lines above
    -- technically true, and useless.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run({"ntp_servers": None,
                          "ntp_server_1": "198.51.100.10",
                          "ntp_server_2": "198.51.100.11"})

    def test_the_run_is_refused(self):
        self.assertTrue(self.result["assert_failed"])

    def test_both_old_keys_are_named(self):
        self.assertIn("ntp_server_1 is no longer read", self.result["assert_msg"])
        self.assertIn("ntp_server_2 is no longer read", self.result["assert_msg"])

    def test_the_message_says_what_to_rename_them_to(self):
        self.assertIn("ntp_servers", self.result["assert_msg"])

    def test_a_migrated_file_says_nothing_about_them(self):
        clean = run({})
        self.assertNotIn("no longer read", clean["assert_msg"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSyslogDeclared(unittest.TestCase):
    """One uncommented variable, and the switch gets the lines."""

    @classmethod
    def setUpClass(cls):
        cls.fresh = run({"syslog_hosts": [COLLECTOR]})
        # A configured switch needs BOTH halves now: the two rendered lines in
        # its running config, and the trap level in its operational state.
        cls.configured = run(
            {"syslog_hosts": [COLLECTOR]},
            stanzas={"logging facility syslog": [],
                     "logging host " + COLLECTOR: []},
            trap_seen="notifications")
        cls.two = run({"syslog_hosts": [COLLECTOR, COLLECTOR_2]})

    def test_declaring_a_collector_enrols_it(self):
        self.assertTrue(self.fresh["syslog_enrolled"])

    def test_the_cli_is_the_rendered_lines_only(self):
        """'logging trap' is NOT here. It does not render into the running
        config when it matches the platform default, so it is checked against
        operational state and pushed by 03b_apply_syslog.yml instead."""
        self.assertEqual(self.fresh["syslog_cli"],
                         ["logging facility syslog",
                          "logging host " + COLLECTOR])

    def test_the_trap_line_is_carried_separately(self):
        self.assertEqual(self.fresh["syslog_trap_line"],
                         "logging trap notifications")

    def test_a_switch_without_them_reports_all_three(self):
        """Two rendered lines missing, plus the unreadable trap level."""
        self.assertEqual(len(self.fresh["syslog_msgs"]), 3,
                         self.fresh["syslog_msgs"])

    def test_each_finding_carries_its_command(self):
        """Unlike the SNMP findings, which carry none -- there is no credential
        in a syslog line, so the remediation file can be paste-ready."""
        self.assertEqual(self.fresh["syslog_lines"],
                         self.fresh["syslog_cli"]
                         + [self.fresh["syslog_trap_line"]])

    def test_a_switch_with_them_is_clean(self):
        self.assertEqual(self.configured["syslog_msgs"], [])

    def test_a_second_collector_needs_no_second_variable(self):
        self.assertEqual(self.two["syslog_cli"][1:],
                         ["logging host " + COLLECTOR,
                          "logging host " + COLLECTOR_2])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSyslogValueIsValidated(unittest.TestCase):
    """A declared collector that is not an address is a mistake, not an
    opt-out, so it fails rather than being dropped."""

    def test_a_masked_address_is_rejected(self):
        """The case the ipv4 filter alone would let through: it accepts a
        prefix and returns the host part, and 'logging host 10.0.0.1/24' is a
        device error. Only comparing the result with the input catches it."""
        result = run({"syslog_hosts": [COLLECTOR + "/24"]})
        self.assertTrue(result["assert_failed"])
        self.assertIn("bare IPv4", result["assert_msg"])

    def test_a_hostname_is_rejected(self):
        result = run({"syslog_hosts": ["syslog.example.com"]})
        self.assertTrue(result["assert_failed"])
        self.assertIn("syslog.example.com", result["assert_msg"])

    def test_one_bad_entry_among_good_ones_is_caught(self):
        result = run({"syslog_hosts": [COLLECTOR, "300.1.1.1"]})
        self.assertTrue(result["assert_failed"])
        self.assertIn("300.1.1.1", result["assert_msg"])

    def test_a_bare_string_is_named_as_the_mistake_it_is(self):
        """Iterating a string yields characters, so without this branch the
        operator would get one finding per letter of their address."""
        result = run({"syslog_hosts": COLLECTOR})
        self.assertTrue(result["assert_failed"])
        self.assertIn("must be a LIST", result["assert_msg"])

    def test_an_empty_list_is_not_an_error_it_is_an_opt_out(self):
        result = run({"syslog_hosts": []})
        self.assertFalse(result["assert_failed"], result["assert_msg"])
        self.assertFalse(result["syslog_enrolled"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestPushAndCheckAgree(unittest.TestCase):
    """The reason readiness_syslog_lines exists as one variable.

    'system mtu 9100' is the counter-example in this role: the literal is in
    both 02_check.yml and platform_baseline.j2, and test_system_mtu.py exists
    partly to stop the two drifting. Syslog is defined once instead, and this
    is what that buys -- a switch cannot pass the check against a different set
    of collectors than the baseline was told to push.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = run({"syslog_hosts": [COLLECTOR, COLLECTOR_2]})

    def test_the_baseline_pushes_exactly_what_the_check_demands(self):
        pushed = baseline_logging(json.dumps(self.result["syslog_cli"]))
        for line in self.result["syslog_cli"]:
            self.assertIn(line, pushed)

    def test_and_a_switch_carrying_them_passes(self):
        pushed = baseline_logging(json.dumps(self.result["syslog_cli"]))
        configured = run({"syslog_hosts": [COLLECTOR, COLLECTOR_2]},
                         stanzas={line: [] for line in pushed},
                         trap_seen="notifications")
        self.assertEqual(configured["syslog_msgs"], [])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNtpIsAListLikeTheOthers(unittest.TestCase):
    """ntp_server_1 / ntp_server_2 became one list.

    The two fixed keys were pushed by the baseline and never checked, so a time
    source removed from a switch by hand went unreported. 'ntp server' renders
    back into the running config, so checking it is plain membership -- no
    operational state needed, unlike SNMPv3.
    """

    @classmethod
    def setUpClass(cls):
        cls.three = run({"ntp_servers": ["198.51.100.10", "198.51.100.11",
                                         "198.51.100.12"]})
        cls.configured = run(
            {"ntp_servers": ["198.51.100.10", "198.51.100.11"]},
            stanzas={"ntp server 198.51.100.10": [],
                     "ntp server 198.51.100.11": []})

    def test_a_third_server_needs_no_third_variable(self):
        """The reason for the change: ntp_server_1/_2 had nowhere to put it."""
        self.assertEqual(self.three["ntp_cli"],
                         ["ntp server 198.51.100.10",
                          "ntp server 198.51.100.11",
                          "ntp server 198.51.100.12"])

    def test_a_switch_missing_them_reports_one_finding_each(self):
        self.assertEqual(len(self.three["ntp_msgs"]), 3, self.three["ntp_msgs"])

    def test_each_finding_carries_its_command(self):
        self.assertEqual(self.three["ntp_lines"], self.three["ntp_cli"])

    def test_a_switch_carrying_them_is_clean(self):
        self.assertEqual(self.configured["ntp_msgs"], [])

    def test_the_baseline_pushes_exactly_what_the_check_demands(self):
        """One definition, two consumers -- the same property syslog has, and
        the reason neither duplicates its CLI the way 'system mtu 9100' does."""
        pushed = baseline_ntp(json.dumps(self.three["ntp_cli"]))
        self.assertEqual(pushed, self.three["ntp_cli"])


_ABSENT = object()

# The eight IOS-XE severities in order. Restated here ON PURPOSE, as the thing
# the shipped list is checked against -- lifting it from defaults/main.yml would
# make test_the_list_in_the_defaults_is_the_ios_order compare the file with
# itself and pass whatever the file said.
SEVERITIES = ["emergencies", "alerts", "critical", "errors",
              "warnings", "notifications", "informational", "debugging"]


def resolve_severity(value=_ABSENT):
    """Resolve syslog_severity through the SHIPPED expression, offline.

    The exhaustive mapping is sixteen inputs, and sixteen ansible-playbook runs
    would roughly double this file's wall clock to prove something that is pure
    Jinja with no Ansible in it. The expression is lifted from the defaults
    rather than restated, and TestSyslogSeverity re-runs a sample through real
    Ansible and compares, so the two paths cannot quietly disagree -- which is
    the risk this shortcut buys and the reason that cross-check exists.
    """
    with open(DEFAULTS) as fh:
        defaults = yaml.safe_load(fh)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    template = env.from_string(defaults["readiness_syslog_severity"])
    context = {
        "readiness_syslog_severities": defaults["readiness_syslog_severities"],
    }
    if value is not _ABSENT:
        context["syslog_severity"] = value
    return template.render(**context).strip()


class TestSyslogSeverityMapping(unittest.TestCase):
    """Both forms of syslog_severity, exhaustively, offline."""

    def test_the_list_in_the_defaults_is_the_ios_order(self):
        """The order IS the mapping, so reordering it silently changes what
        every numeric deployment renders."""
        with open(DEFAULTS) as fh:
            shipped = yaml.safe_load(fh)["readiness_syslog_severities"]
        self.assertEqual(shipped, SEVERITIES)

    def test_every_keyword_resolves_to_itself(self):
        for keyword in SEVERITIES:
            with self.subTest(severity=keyword):
                self.assertEqual(resolve_severity(keyword), keyword)

    def test_every_number_resolves_to_its_keyword(self):
        for number, keyword in enumerate(SEVERITIES):
            with self.subTest(severity=number):
                self.assertEqual(resolve_severity(number), keyword)
                self.assertEqual(resolve_severity(str(number)), keyword)

    def test_absent_empty_and_null_all_mean_notifications(self):
        """Three ways a deployment declines to choose, and the answer has to be
        what the role pushed when the line was a literal."""
        self.assertEqual(resolve_severity(), "notifications")
        self.assertEqual(resolve_severity(""), "notifications")
        self.assertEqual(resolve_severity(None), "notifications")

    def test_case_and_surrounding_space_are_forgiven(self):
        """Rejecting 'Notifications' would produce an error message whose list
        of allowed values contains what the operator just typed."""
        self.assertEqual(resolve_severity("Notifications"), "notifications")
        self.assertEqual(resolve_severity("  DEBUGGING  "), "debugging")

    def test_anything_else_resolves_to_nothing(self):
        """Empty is how an unusable value reaches the assert, which turns it
        into a named problem instead of a crash on whatever touched it first."""
        for bad in (8, 99, -1, 1.5, True, "5.0", "warning", "sev5", "trap"):
            with self.subTest(severity=bad):
                self.assertEqual(resolve_severity(bad), "")


class TestSyslogSeverity(unittest.TestCase):
    """The same key through real Ansible, and the CLI it ends up producing."""

    def test_the_default_is_unchanged_for_a_file_that_never_heard_of_it(self):
        result = run({"syslog_hosts": [COLLECTOR]})
        self.assertEqual(result["syslog_trap_line"],
                         "logging trap notifications")
        self.assertFalse(result["assert_failed"], result["assert_msg"])

    def test_a_keyword_reaches_the_trap_line(self):
        result = run({"syslog_hosts": [COLLECTOR],
                      "syslog_severity": "debugging"})
        self.assertEqual(result["syslog_trap_line"], "logging trap debugging")
        self.assertFalse(result["assert_failed"], result["assert_msg"])

    def test_zero_is_emergencies_and_not_the_default(self):
        """0 is FALSY. Written as default('notifications', true) -- the form
        every other optional key in this role uses -- severity 0 would silently
        become severity 5, and nothing downstream would look wrong."""
        result = run({"syslog_hosts": [COLLECTOR], "syslog_severity": 0})
        self.assertEqual(result["syslog_trap_line"], "logging trap emergencies")

    def test_a_number_never_reaches_the_device_as_a_number(self):
        """The reason the conversion happens in the role at all: this project
        has no verified answer for how IOS-XE renders 'logging trap 6' back."""
        result = run({"syslog_hosts": [COLLECTOR], "syslog_severity": 6})
        self.assertEqual(result["syslog_trap_line"],
                         "logging trap informational")

    def test_ansible_agrees_with_the_offline_resolver(self):
        """What licenses TestSyslogSeverityMapping to skip Ansible.

        8 is in the sample deliberately. Bare Jinja resolves an unusable value
        to '' and Ansible's native templating resolves the same expression to
        None, and that divergence is exactly what let every invalid severity
        pass the assert until this cross-check was widened to cover one."""
        for value in ("debugging", 0, 8):
            with self.subTest(severity=value):
                result = run({"syslog_hosts": [COLLECTOR],
                              "syslog_severity": value})
                self.assertEqual(result["syslog_severity"],
                                 resolve_severity(value))

    def test_a_value_that_is_not_a_severity_is_refused(self):
        result = run({"syslog_hosts": [COLLECTOR], "syslog_severity": 8})
        self.assertTrue(result["assert_failed"], result["assert_msg"])

    def test_the_refusal_names_both_forms(self):
        result = run({"syslog_hosts": [COLLECTOR],
                      "syslog_severity": "verbose"})
        message = result["assert_msg"]
        self.assertIn("syslog_severity", message)
        self.assertIn("0-7", message)
        for keyword in SEVERITIES:
            self.assertIn(keyword, message, message)

    def test_the_baseline_does_not_push_the_trap_line(self):
        """The fix for what a live fabric found on 2026-08-22.

        With syslog_severity 6 the baseline pushed 'logging trap
        informational', IOS-XE declined to render it back because it is the
        platform default, and all eleven switches then reported it missing on
        every run while 'show logging' confirmed it set. The trap line is out
        of the baseline entirely now; 03b_apply_syslog.yml pushes it against
        operational state instead."""
        result = run({"syslog_hosts": [COLLECTOR],
                      "syslog_severity": "critical"})
        pushed = baseline_logging(json.dumps(result["syslog_cli"]))
        self.assertNotIn("logging trap critical", pushed)
        for line in ("logging facility syslog", "logging host " + COLLECTOR):
            self.assertIn(line, pushed)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestTheTrapLevelIsCheckedAgainstOperationalState(unittest.TestCase):
    """Because the running config does not always contain it.

    Measured on a C9300, a C9400 and a c9kv on 2026-08-22: with the severity
    set to 'informational' -- the IOS-XE default -- 'show running-config |
    include ^logging' has no 'logging trap' line at all, while 'show logging'
    reports "Trap logging: level informational". Any check that reads the
    running config for this is asserting on a line the device chose not to
    print, and it fails a correctly-configured switch for ever.
    """

    RENDERED = {"logging facility syslog": [],
                "logging host " + COLLECTOR: []}

    def _findings(self, severity, seen):
        return run({"syslog_hosts": [COLLECTOR], "syslog_severity": severity},
                   stanzas=dict(self.RENDERED), trap_seen=seen)["syslog_msgs"]

    def test_a_matching_level_is_clean(self):
        self.assertEqual(self._findings("informational", "informational"), [])

    def test_the_default_case_that_broke_the_fabric_is_clean(self):
        """The exact combination that failed 11/11: severity 6 asked for,
        nothing in the running config, informational in operational state."""
        self.assertEqual(self._findings(6, "informational"), [])

    def test_a_different_level_is_one_finding_naming_both(self):
        findings = self._findings("informational", "warnings")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("warnings", findings[0])
        self.assertIn("informational", findings[0])

    def test_an_unreadable_level_says_so_rather_than_guessing(self):
        """'expected informational, got nothing' and 'expected informational,
        got warnings' send an operator to two different places."""
        findings = self._findings("informational", "")
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("not readable", findings[0])

    def test_the_finding_carries_the_command_that_fixes_it(self):
        result = run({"syslog_hosts": [COLLECTOR], "syslog_severity": 2},
                     stanzas=dict(self.RENDERED), trap_seen="warnings")
        self.assertEqual(result["syslog_lines"], ["logging trap critical"])

    def test_an_unenrolled_deployment_is_not_asked_about_its_trap_level(self):
        """No collector means no trap level to be wrong about, and an empty
        reading must not become a finding on a fabric that opted out."""
        result = run({}, trap_seen="")
        self.assertFalse(result["syslog_enrolled"])
        self.assertEqual(result["syslog_msgs"], [])
        self.assertEqual(result["syslog_trap_line"], "")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
