# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""readiness.yml accepts a switch's new SSH host key.

A switch wiped and re-added presents a new host key, and readiness.yml -- the
onboarding step -- failed on first contact:

    fatal: [dist3-2]: FAILED! => {"msg": "host key mismatch for 10.67.118.73"}

'host_key_checking = False' in ansible.cfg was already set and did nothing:
with ansible.netcommon 8.5.3 over paramiko, network_cli merges its paramiko
layer's built-in default (True) over the configured value. So readiness forgets
each targeted switch's recorded key before connecting, and the connection
re-records whatever the switch presents.

These tests run the SHIPPED task from 00_host_keys.yml under real
ansible-playbook against a throwaway HOME, so the real ~/.ssh/known_hosts is
never touched. Verified separately end to end against a paramiko SSH server: a
stale key failed with the mismatch, and after this task the connection
succeeded and the new key was written back.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE = os.path.dirname(TESTS)
TASKS = os.path.join(ROLE, "tasks")
ANSIBLE = shutil.which("ansible-playbook")
SSH_KEYGEN = shutil.which("ssh-keygen")

# Three hosts recorded; the run targets the first two.
KEY = ("AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")
KNOWN_HOSTS = (
    "10.0.0.1 ssh-ed25519 %s\n"
    "[10.0.0.2]:2222 ssh-ed25519 %s\n"
    "10.0.0.9 ssh-ed25519 %s\n" % (KEY, KEY, KEY))

INVENTORY = """\
sw1    ansible_host=10.0.0.1
sw2    ansible_host=10.0.0.2 ansible_port=2222
other  ansible_host=10.0.0.9

[all:vars]
ansible_connection=local
"""


def run(**extra_vars):
    workdir = tempfile.mkdtemp(prefix="host-keys-")
    try:
        home = os.path.join(workdir, "home")
        os.makedirs(os.path.join(home, ".ssh"))
        known = os.path.join(home, ".ssh", "known_hosts")
        with open(known, "w") as fh:
            fh.write(KNOWN_HOSTS)
        shutil.copy(os.path.join(TASKS, "00_host_keys.yml"), workdir)
        with open(os.path.join(workdir, "hosts.ini"), "w") as fh:
            fh.write(INVENTORY)
        play = [{"name": "harness", "hosts": "sw1:sw2", "gather_facts": False,
                 "vars": {"readiness_refresh_host_keys": True},
                 "tasks": [{"name": "the task under test",
                            "ansible.builtin.import_tasks": "00_host_keys.yml"}]}]
        with open(os.path.join(workdir, "harness.yml"), "w") as fh:
            yaml.safe_dump(play, fh, sort_keys=False)
        cmd = [ANSIBLE, "-i", "hosts.ini", "harness.yml"]
        for k, v in extra_vars.items():
            cmd += ["-e", "%s=%s" % (k, v)]
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                              env=dict(os.environ, HOME=home, ANSIBLE_NOCOLOR="1"))
        with open(known) as fh:
            remaining = [line.split()[0] for line in fh if line.strip()]
        return proc.returncode, proc.stdout + proc.stderr, remaining
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None or SSH_KEYGEN is None,
                 "ansible-playbook or ssh-keygen is not on PATH")
class TestRecordedKeysAreForgotten(unittest.TestCase):

    def test_targeted_switches_lose_their_keys_and_others_keep_theirs(self):
        rc, out, remaining = run()
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(remaining, ["10.0.0.9"])

    def test_a_non_standard_port_is_matched_in_bracket_form(self):
        _, _, remaining = run()
        self.assertNotIn("[10.0.0.2]:2222", remaining)

    def test_switching_it_off_keeps_every_key(self):
        rc, out, remaining = run(readiness_refresh_host_keys="false")
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(remaining, ["10.0.0.1", "[10.0.0.2]:2222", "10.0.0.9"])


class TestOnlyReadinessForgetsKeys(unittest.TestCase):
    """generate and deploy must keep checking against the recorded key, so the
    task belongs to the readiness entry point and not to the shared gather."""

    def imports(self, name):
        with open(os.path.join(TASKS, name)) as fh:
            return [t.get("ansible.builtin.import_tasks") for t in yaml.safe_load(fh)]

    def test_main_forgets_after_resolving_and_before_connecting(self):
        order = self.imports("main.yml")
        self.assertLess(order.index("fabric_resolve.yml"), order.index("00_host_keys.yml"))
        self.assertLess(order.index("00_host_keys.yml"), order.index("01_gather.yml"))

    def test_the_acm_evpn_preflight_does_not(self):
        self.assertNotIn("00_host_keys.yml", self.imports("assert_only.yml"))

    def test_the_shared_gather_does_not(self):
        with open(os.path.join(TASKS, "01_gather.yml")) as fh:
            self.assertNotIn("00_host_keys", fh.read())


if __name__ == "__main__":
    unittest.main()
