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

Most of the suite is plain Python against `filter_plugins/*.py`. The rest of the
logic lives in Jinja — in task files and templates — where `--syntax-check`
evaluates nothing at all. That is covered by running the **real task files under
real Ansible** with only the device calls removed.

This is no longer a manual exercise: eight committed tests already do it, and CI
runs them. Copy whichever is closest rather than starting from scratch.

| Test | What it drives | How the device is removed |
|---|---|---|
| `acm_evpn/tests/unit/test_fabric_resolve.py` | The real fabric resolver, peer selection and target scoping | Nothing to remove — fabric membership is decided entirely from inventory groups |
| `underlay_readiness/tests/unit/test_msdp_scope.py` | The real spine check from `02_check.yml` | The `_spine` expression is lifted out and fed a hand-written running config |
| `underlay_readiness/tests/unit/test_anycast_rp.py` | The real anycast-RP resolution | The one `uri` task is stripped; a fixture stands in for the GraphQL result |
| `acm_evpn/tests/unit/test_inventory_config.py` | `netbox_inv.yml`'s variable renames | Drives the inventory plugin's own methods with a stub inventory |
| `underlay_readiness/tests/unit/test_optional_enrolment.py` | Whether SNMPv3 and syslog are enrolled at all, from the real `defaults/main.yml`, the real `assert_site_config.yml` and the real `_snmp` / `_syslog` checks | The whole defaults file is copied in as `vars_files` and each case's `deployment.yml` supplied as extra vars, which outrank it — so "this key is commented out" is expressible |
| `underlay_readiness/tests/unit/test_snmp_state.py` | The real derive task from `01_gather.yml` and the real `_snmp` check, over an operational read that produced nothing | The two `ios_command` reads are supplied as data in the exact shape the module registers them — **including the shape it registers when the command errored**, which is a result with no `stdout` key at all |
| `underlay_readiness/tests/unit/test_system_mtu.py` | The real MTU parse, the real cross-device consensus task and the real `_mtu` check, plus the shipped `platform_baseline.j2` rendered offline | Running configs are supplied as the stanza dict `01_gather.yml`'s indexer would have produced; the template is rendered with plain Jinja2 and filter shims |
| `underlay_readiness/tests/unit/test_readiness_summary.py` | The real classify task from `05_report.yml` — ready / unready / unreachable, and the three ways a host is deliberately dropped | Pure set arithmetic over facts `02_check.yml` would have produced, supplied directly by a fixture inventory that also carries the **role groups**, without which a server cannot be told from a switch |

The mechanics worth reusing:

1. **Extract the tasks under test from the shipped file at run time** —
   `yaml.safe_load`, select by name prefix or pull one `vars:` key, write them
   into a `tempfile.mkdtemp()` beside a copy of the harness playbook. Nothing is
   copied by hand, so a fixture cannot drift from the role. **Assert the
   extraction found what it expected**: a rename should fail loudly, not quietly
   test less than it claims.
2. **Run from the temp directory** so the repo's `ansible.cfg` is not picked up
   — it would force `netbox_inv.yml` and `strategy = free`. Pass
   `ANSIBLE_ROLES_PATH` / `ANSIBLE_FILTER_PLUGINS` explicitly, and set
   `strategy: linear` in the harness, which `run_once` depends on.
3. **Get results out as JSON** — a final `run_once` + `delegate_to: localhost`
   `copy:` task writing `hostvars` to a file named by `-e result_file=…`.
   Collect from `hostvars`, not per host, so hosts that ended the play still
   appear.
4. **Fixtures are static inventories** in `tests/fixtures/`, shaped like what
   `nb_inventory` builds (`sites_<slug>` holding devices, `site_group_<slug>`
   holding sites as children). Use YAML rather than ini when a host var is a
   nested structure. Add the fixtures directory to `.ansible-lint`'s
   `exclude_paths` — a harness importing a file generated at test time cannot
   lint standalone.
5. **Guard on `shutil.which("ansible-playbook")`** with `@unittest.skipIf`, so a
   contributor without Ansible still gets the plugin tests.
6. **Mutate the code and confirm the tests fail.** A check that never fails is
   not a check. Every fabric-scoping change in this repo was reverted in place
   to prove its tests caught it, and that is what found a guard keyed on the
   wrong variable.

Where a real running config is wanted as a fixture, `switch-configuration/*.cfg`
are full post-render candidates and read like `show running-config`. That
directory is gitignored (it describes live devices), so a fresh clone has none:
produce them with `generate_evpn.yml`, or drop in a saved config from any
switch. Sanitise before committing anything derived from one.

Note also that a plain-Jinja harness is a trap: Ansible registers `match` and
`search` as tests, `regex_search` / `to_json` / `regex_escape` as filters, and
`lookup()` as a global — none of which exist in bare Jinja2. Running under real
Ansible avoids all of it and has higher fidelity. The one exception is
`test_leaf_templates.py`, which renders `.j2` files with plain Jinja2 on
purpose; prefer a plain expression over an Ansible-only filter in a template so
it stays renderable there.

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
- **This deployment's management identity.** NTP, SNMPv3 and VTP live in
  `group_vars/all/deployment.yml`, which is gitignored, with placeholders in
  `deployment.yml.example`. **Role defaults carry none of it** — a plausible
  default pointing at somebody else's network is worse than no default, so the
  role asserts instead.
- **Anything describing a fabric.** It does not belong in a file at all: the
  ASN, the anycast RP and each handoff's upstream ASN come from NetBox, per
  fabric. A global value cannot describe more than one fabric, which is why
  none of them survived.
- Rendered candidates, ACM results, readiness output. All gitignored; they
  describe live devices.
