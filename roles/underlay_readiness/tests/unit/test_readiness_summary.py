# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Which switches the readiness summary counts, and as what.

This exists because the classification broke twice in one day, both times on
hardware and both times silently — the run looked clean and the numbers were
wrong. It is set arithmetic over facts, which is exactly the kind of thing that
reads correctly and behaves otherwise.

  1. Devices deliberately dropped before this role ran had no readiness result,
     and every host without a result was counted UNREACHABLE. A correct run
     reported four connectivity problems that did not exist.
  2. Fixing that by counting only FABRIC NODES was worse. A distribution switch
     between a leaf and a border IS evaluated — it carries fabric traffic — so
     four genuinely unready switches were reported as "0 NOT ready" while
     remediation files were being written for them. Silent under-reporting of
     unready switches is the worst failure this summary has.

The rule is: count everything this run intended to evaluate. A host is excluded
only for the two reasons it is deliberately dropped beforehand — no site at all,
or a spine unioned in from a fabric this run does not target. Anything else with
no result really was unreachable.
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

REPORT = os.path.join(ROLE, "tasks", "05_report.yml")
HARNESS = os.path.join(FIXTURES, "classify_harness.yml")
INVENTORY = os.path.join(FIXTURES, "classify_hosts.yml")

ANSIBLE = shutil.which("ansible-playbook")


def classify_task():
    """The real classify task from 05_report.yml, lifted not copied."""
    with open(REPORT) as fh:
        tasks = yaml.safe_load(fh)
    wanted = [t for t in tasks if "classify" in t.get("name", "").lower()]
    if len(wanted) != 1:
        raise AssertionError(
            "expected exactly one classify task in {}, found {}"
            .format(REPORT, [t.get("name") for t in wanted])
        )
    return wanted


def run_classify(targeted_fabrics=None):
    workdir = tempfile.mkdtemp(prefix="readiness-summary-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        shutil.copy(os.path.join(ROLE, "defaults", "main.yml"),
                    os.path.join(workdir, "defaults.yml"))
        with open(os.path.join(workdir, "classify_tasks.yml"), "w") as fh:
            yaml.safe_dump(classify_task(), fh, default_flow_style=False, sort_keys=False)

        result_file = os.path.join(workdir, "result.json")
        extra = {"result_file": result_file}
        if targeted_fabrics is not None:
            extra["targeted_fabrics"] = targeted_fabrics
        proc = subprocess.run(
            [ANSIBLE, "-i", INVENTORY, "harness.yml", "-e", json.dumps(extra)],
            cwd=workdir, env=dict(os.environ, ANSIBLE_ROLES_PATH=ROLES,
                                  ANSIBLE_NOCOLOR="1"),
            capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        if not os.path.exists(result_file):
            raise AssertionError("harness produced no result:\n" + output)
        with open(result_file) as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestClassificationOnTheAcmEvpnPath(unittest.TestCase):
    """targeted_fabrics is set, so an untargeted fabric's spine is excluded."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_classify(targeted_fabrics=["site_group_evpn_campus"])

    def test_an_unready_distribution_switch_is_counted_unready(self):
        """The regression. It was reported as '0 NOT ready' while a remediation
        file was being written for it."""
        self.assertEqual(self.result["unready"], ["dist1-1"])

    def test_ready_switches_are_counted_ready(self):
        self.assertEqual(self.result["ready"], ["access1-1", "core1"])

    def test_a_switch_meant_to_be_checked_with_no_result_is_unreachable(self):
        self.assertEqual(self.result["unreachable"], ["downswitch"])

    def test_a_device_with_no_site_is_not_counted_at_all(self):
        """Dropped by the resolver and reported there. Counting it as
        unreachable sends an operator after a problem that does not exist."""
        for bucket in ("ready", "unready", "unreachable"):
            self.assertNotIn("orphan1", self.result[bucket], bucket)

    def test_an_untargeted_fabrics_spine_is_not_counted_at_all(self):
        for bucket in ("ready", "unready", "unreachable"):
            self.assertNotIn("rcore1", self.result[bucket], bucket)

    def test_the_total_covers_every_evaluated_switch(self):
        self.assertEqual(self.result["counted"], 4)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestClassificationOnTheStandalonePath(unittest.TestCase):
    """readiness.yml unions nothing, so targeted_fabrics does not exist and
    every device with a fabric is in scope — including the remote spine."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_classify()

    def test_the_remote_spine_is_now_counted(self):
        self.assertIn("rcore1", self.result["ready"])

    def test_the_unready_distribution_switch_is_still_counted(self):
        self.assertEqual(self.result["unready"], ["dist1-1"])

    def test_the_device_with_no_site_is_still_excluded(self):
        for bucket in ("ready", "unready", "unreachable"):
            self.assertNotIn("orphan1", self.result[bucket], bucket)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
