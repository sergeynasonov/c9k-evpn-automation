# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Decommissioning a leaf must not block its route reflectors.

Leaf 10.67.118.73 was deleted from NetBox. Both spines stopped rendering it as
an EVPN client -- correctly -- and then refused to render at all, because the
check that guards the border-to-border mesh treated every unrendered
fabric-ASN neighbour as a session about to be deleted by mistake:

    core1: BGP neighbour(s) 10.67.118.73 are in the fabric ASN 64513, so they
    were removed from the preserved configuration on the assumption a template
    renders them - but they do not appear in the candidate.

The classification itself is plain Python, tested in test_ios_underlay.py. What
this file covers is the part that is Jinja in a task file: where the inventory
addresses come from (every host's ansible_host, read through hostvars), the
candidate read off disk, and the assert. Those three tasks are LIFTED FROM THE
SHIPPED 03_safety_check.yml at run time and run under real ansible-playbook
against a static inventory, so a fixture cannot drift from the role.
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
TASKS = os.path.join(ROLE, "tasks", "03_safety_check.yml")
ANSIBLE = shutil.which("ansible-playbook")

WANTED = (
    "Classify fabric-ASN BGP neighbours",
    "Report EVPN peers removed",
    "Verify every fabric-ASN BGP neighbour",
)

DEVICE = """\
router bgp 64513
 neighbor EVPN-PEERS peer-group
 neighbor EVPN-PEERS remote-as 64513
 neighbor 10.67.118.71 peer-group EVPN-PEERS
 neighbor 10.67.118.72 peer-group EVPN-PEERS
 neighbor 10.67.118.73 peer-group EVPN-PEERS
 neighbor 10.67.118.65 remote-as 64513
 neighbor 10.67.118.2 remote-as 64512
!
"""

# The render once .73 is gone: two clients and the border mesh.
CANDIDATE = """\
router bgp 64513
 neighbor EVPN-PEERS peer-group
 neighbor EVPN-PEERS remote-as 64513
 neighbor 10.67.118.71 peer-group EVPN-PEERS
 neighbor 10.67.118.72 peer-group EVPN-PEERS
 neighbor 10.67.118.65 remote-as 64513
 neighbor 10.67.118.2 remote-as 64512
!
"""

INVENTORY = """\
core1      ansible_host=10.67.118.64
core2      ansible_host=10.67.118.65
access1-1  ansible_host=10.67.118.71
access2-1  ansible_host=10.67.118.72
{extra}
[all:vars]
ansible_connection=local
"""


def _lifted_tasks():
    with open(TASKS) as fh:
        tasks = yaml.safe_load(fh)
    out = [t for t in tasks if t.get("name", "").startswith(WANTED)]
    if len(out) != len(WANTED):
        raise AssertionError("expected %d BGP tasks in 03_safety_check.yml, found %s"
                             % (len(WANTED), [t.get("name") for t in out]))
    return out


def run(extra_inventory="", candidate=CANDIDATE):
    workdir = tempfile.mkdtemp(prefix="bgp-departed-")
    try:
        os.makedirs(os.path.join(workdir, "switch-configuration"))
        with open(os.path.join(workdir, "switch-configuration",
                               "core1-candidate-config.cfg"), "w") as fh:
            fh.write(candidate)
        with open(os.path.join(workdir, "lifted.yml"), "w") as fh:
            yaml.safe_dump(_lifted_tasks(), fh, sort_keys=False)
        with open(os.path.join(workdir, "hosts.ini"), "w") as fh:
            fh.write(INVENTORY.format(extra=extra_inventory))
        result_file = os.path.join(workdir, "result.json")
        play = [{
            "name": "harness", "hosts": "core1", "gather_facts": False,
            "vars": {"fabric_asn": 64513, "evpn_peer_group_name": "EVPN-PEERS"},
            "tasks": [
                {"name": "underlay as 01_gather_data.yml extracts it",
                 "ansible.builtin.set_fact": {
                     "underlay": "{{ device | ios_underlay_config("
                                 "evpn_peer_group_name, fabric_asn) }}"},
                 "vars": {"device": DEVICE}},
                {"name": "the tasks under test",
                 "ansible.builtin.include_tasks": "lifted.yml"},
                {"name": "record",
                 "ansible.builtin.copy": {
                     "dest": result_file,
                     "content": "{{ acm_bgp_unrendered | to_json }}"},
                 "delegate_to": "localhost"},
            ],
        }]
        with open(os.path.join(workdir, "harness.yml"), "w") as fh:
            yaml.safe_dump(play, fh, sort_keys=False)
        env = dict(os.environ,
                   ANSIBLE_FILTER_PLUGINS=os.path.join(ROLE, "filter_plugins"),
                   ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [ANSIBLE, "-i", "hosts.ini", "harness.yml"],
            cwd=workdir, env=env, capture_output=True, text=True)
        result = None
        if os.path.exists(result_file):
            with open(result_file) as fh:
                result = json.load(fh)
        return proc.returncode, proc.stdout + proc.stderr, result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestADecommissionedLeafDoesNotBlockItsSpines(unittest.TestCase):

    def test_the_spine_renders_and_the_departure_is_reported(self):
        rc, out, result = run()
        self.assertEqual(rc, 0, out[-3000:])
        self.assertEqual(result, {"departed": ["10.67.118.73"], "missing": []})
        self.assertIn("removing BGP session(s) to 10.67.118.73", out)

    def test_a_leaf_still_in_netbox_that_did_not_render_still_blocks(self):
        """The leaf is back in the inventory, so its absence from the render is
        a render problem, not a decommission."""
        rc, out, _ = run(extra_inventory="access3-1  ansible_host=10.67.118.73")
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("10.67.118.73 are in the fabric ASN 64513", out)

    def test_a_dropped_border_mesh_peer_still_blocks(self):
        rc, out, _ = run(candidate=CANDIDATE.replace(
            " neighbor 10.67.118.65 remote-as 64513\n", ""))
        self.assertNotEqual(rc, 0, out[-3000:])
        self.assertIn("10.67.118.65", out)


if __name__ == "__main__":
    unittest.main()
