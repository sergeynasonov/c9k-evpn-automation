# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Catch task-local vars being read by a LATER task.

Why this exists
---------------
A key under a task's ``vars:`` is scoped to that task. A key under ``set_fact:``
persists for the host. The two look almost identical in YAML, and consolidating
tasks makes it easy to move a key from one to the other by accident:

    - name: Inventory objects
      ansible.builtin.set_fact:
        _dev_fabric_vrfs: "..."
      vars:
        _new_fabric_vrfs: "..."     # <-- task-local, gone by the next task

    - name: Find tenant SVIs
      ansible.builtin.set_fact:
        _dev_fabric_svis: "{{ _dev_fabric_vrfs + _new_fabric_vrfs }}"   # Undefined

``ansible-playbook --syntax-check`` does not catch this -- it only parses YAML
and module names. It surfaces at run time, against live devices, as
'can only concatenate list (not "UndefinedMarker") to list'. That is exactly how
it was found, mid-render, on a seven-switch run.

The check is deliberately conservative: a name is only reported when it is a
task-local var somewhere, is referenced by a DIFFERENT task in the same file,
and is never set by set_fact or register anywhere in that file.
"""

import glob
import os
import re
import unittest

import yaml

ROLES = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
VAR_RE = re.compile(r"[\{\%]\{?\s*.*?\b([a-zA-Z_][a-zA-Z0-9_]*)\b", re.S)
NAME_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")


def _task_files():
    for role in ("acm_evpn", "underlay_readiness"):
        yield from sorted(glob.glob(os.path.join(ROLES, role, "tasks", "*.yml")))


def _referenced(node):
    """Every bare identifier appearing inside a template in this structure."""
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "vars":
                continue  # a task's own vars may reference themselves freely
            out |= _referenced(v)
    elif isinstance(node, list):
        for v in node:
            out |= _referenced(v)
    elif isinstance(node, str) and ("{{" in node or "{%" in node):
        out |= set(NAME_RE.findall(node))
    return out


class TestTaskVarScoping(unittest.TestCase):
    def test_no_task_reads_another_tasks_local_vars(self):
        problems = []
        for path in _task_files():
            with open(path) as fh:
                tasks = yaml.safe_load(fh) or []
            if not isinstance(tasks, list):
                continue

            persistent = set()
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                for key in ("ansible.builtin.set_fact", "set_fact"):
                    if isinstance(t.get(key), dict):
                        persistent |= set(t[key])
                if isinstance(t.get("register"), str):
                    persistent.add(t["register"])

            local_owner = {}
            for i, t in enumerate(tasks):
                if isinstance(t, dict) and isinstance(t.get("vars"), dict):
                    for name in t["vars"]:
                        local_owner.setdefault(name, i)

            for i, t in enumerate(tasks):
                if not isinstance(t, dict):
                    continue
                for name in _referenced(t):
                    if name in persistent:
                        continue
                    owner = local_owner.get(name)
                    if owner is not None and owner != i:
                        problems.append(
                            "%s: task %r reads %r, which is a task-local var of "
                            "task %r and is never set_fact'd"
                            % (os.path.basename(path), tasks[i].get("name"),
                               name, tasks[owner].get("name")))
        self.assertEqual(problems, [], "\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main()
