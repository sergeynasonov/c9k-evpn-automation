# Contributing

Thanks for looking. This document is less about process than about the handful
of things that will bite you — most of them learned the expensive way, against
real switches.

Contributions are accepted under **GPL-3.0-or-later**, the same licence as the
project. New source files should carry the SPDX header and a copyright line.

---

## Run the tests

Install the dev dependencies once — `requirements.txt` deliberately omits the
linter so an operator running playbooks does not install one:

```bash
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Then the tests. Stdlib `unittest` only — no pytest, no live NetBox, no devices.
`test_fabric_resolve.py` is the one case that needs `ansible-playbook` on
`PATH`: fabric resolution is Jinja, not Python, so it is tested by running the
real tasks against a static inventory. It skips itself if Ansible is absent:

```bash
venv/bin/python -m unittest discover -s roles/acm_evpn/tests/unit
venv/bin/python -m unittest discover -s roles/underlay_readiness/tests/unit
```

Then check nothing broke structurally:

```bash
for pb in readiness.yml generate_evpn.yml deploy_evpn.yml trm_allocate.yml; do
  venv/bin/ansible-playbook --syntax-check "$pb"
done
```

`--syntax-check` parses YAML and module names. **It does not evaluate Jinja**, so
it proves far less than it appears to. Most of the logic here lives in templates
and task-layer Jinja that it never touches.

And lint, which the tree currently passes clean at ansible-lint's strictest
(`production`) profile — keep it that way:

```bash
venv/bin/ansible-lint
```

`.ansible-lint` carries exactly one `skip_list` entry and the tree carries one
inline `# noqa`, each with its reasoning written next to it. If you need
another, write down why rather than adding it silently.

All of the above runs in CI on every push and pull request
(`.github/workflows/ci.yml`), on Python 3.12 and 3.14 — the floor ansible-core
requires and the version this is developed on. **CI needs no secrets**, because
nothing it runs touches NetBox or a switch; keep it that way. A green tick means
nothing broke that can be checked without hardware — it says nothing about the
device-level failures listed further down.

Note ansible-lint only sees Jinja **embedded in YAML** — it does not open
`.j2` files at all, which is where most of the logic actually lives. Those are
covered by the offline render described next.

## Testing task-layer logic offline

The unit tests only reach `filter_plugins/*.py`. For task and template logic,
run the **real task files** under real Ansible with the device calls removed:

1. `switch-configuration/*.cfg` are full post-render candidates, so they read
   like a device's `show running-config` — use them as fixtures. The directory
   is gitignored (it describes live devices), so a fresh clone has none: produce
   them with `generate_evpn.yml`, or drop in a saved `show running-config` from
   any switch. Sanitise before committing anything derived from one.
2. Load the task file with `yaml.safe_load`, filter out the `cisco.ios.*` tasks,
   dump the rest to a scratch file. Keeping the remaining tasks byte-identical to
   what ships means the test cannot drift from the role.
3. Drive it from a scratch playbook with `connection: local`, a static
   `hosts.ini` supplying `ansible_host` and the group memberships the logic reads
   (`tags_fabric_spine`, `device_roles_fabric_border`), and `vars_files:` pointing
   at the role's own `defaults/main.yml`.
4. **Mutate a known-good config to prove each check fires.** A check that never
   fails is not a check.

## Things that have caused real outages or near-misses

**`acm replace` is a full configuration replacement.** Anything absent from the
candidate is removed from the switch. An upstream data failure looks exactly
like "a small intended config" right up until a device loses every VRF — which
is why the blast-radius guard exists and why underlay object classes are zero
tolerance. Do not weaken it.

**Deploy twice.** A first successful deploy proves nothing about convergence.
Both of the worst idempotency bugs here were invisible on the first run and only
appeared on the second. A change is not finished until a re-deploy reports
`Config diff empty`.

**`show running-config` is not complete.** Three classes, needing opposite
handling:

- *rendered normally* — safe to assert by text and to own in a candidate;
- *set but never reported* (`snmp-server user`) — a full-replace candidate can
  never converge on it; own it in `underlay_readiness` and apply by merge;
- *not reported because it is the platform default* (`ip routing` on the
  C9400/9500/9600) — **do not push it**; assert the negative instead, because a
  non-default value *is* rendered.

Before asserting a global line by text, check it is not a default on **every**
platform in the fabric. C9300/9400/9500/9600 differ.

**A default-ON setting you want off must be stated explicitly, and in IOS-XE's
own position within the block.** Omitting `no ip proxy-arp` makes every deploy
re-apply it; putting it in the wrong position churns just as reliably, because
the candidate is compared against the running config verbatim.

**Anything the role stops rendering is preserved from the device.** If you add a
feature that can be turned off, you must also prune what it left behind —
otherwise disabling it becomes a one-way door. See the TRM pruning in
`02_render_configs.yml`.

## Ansible mechanics that bite here

- A key under `vars:` is **task-local**; under `set_fact:` it persists. Mixing
  them up surfaces at run time as `'dict' object has no attribute …`, far from
  the task that caused it. `tests/unit/test_task_var_scoping.py` catches the
  whole class — **run it before trusting any task consolidation.**
- A task with `register:` that is **skipped** still overwrites its target with a
  skip stub. Register to a separate name and copy across.
- `--check` skips `uri` and `ios_command`. Read-only queries need
  `check_mode: false` so they run; only genuine writes stay simulated.
- `run_once` is unsupported under the `free` strategy that `ansible.cfg` sets
  globally, and fires per-host inside a dynamic `include_role`. Playbooks
  override with `strategy: linear` and use `import_role` — both load-bearing.
- `debug: msg` should be a **list of strings**. A multi-line string renders as
  one JSON blob full of `\n` escapes.

## Style

**Prefer Ansible-native.** Jinja and tasks over Python filter plugins; a plugin
is a last resort for things Jinja genuinely cannot do (scrypt, regex-heavy
config parsing). Policy — thresholds, flags, enforcement — belongs in the task
layer where an operator can see it.

**Comments explain *why*, not what.** The code says what it does. The valuable
part is the constraint that shaped it: which failure it prevents, which
alternative was rejected and on what grounds. Several comments here name the
outage that caused them; that is deliberate, keep it.

**Fail loudly, never drop silently.** Most bugs found in this project were
silent omissions that rendered a valid-looking config which then deleted live
state. Prefer raising to filtering. Accumulate problems into one list and report
them together rather than stopping at the first.

## Never commit

- **Secrets.** Everything comes from the environment. Nothing in this repo
  contains credentials, and it should stay that way.
- **Site-specific values.** ASNs, addressing, NTP/SNMP/VTP identity live in
  `group_vars/all/fabric.yml`, which is gitignored, with placeholders in
  `fabric.yml.example`. **Role defaults carry none of it** — a plausible default
  pointing at somebody else's network is worse than no default, so the roles
  assert instead.
- Rendered candidates, ACM results, readiness output. All gitignored; they
  describe live devices.
