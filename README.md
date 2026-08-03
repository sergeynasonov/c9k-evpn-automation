# c9k-evpn-automation

[![CI](https://github.com/sergeynasonov/c9k-evpn-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/sergeynasonov/c9k-evpn-automation/actions/workflows/ci.yml)

Ansible automation for a campus EVPN/VXLAN fabric on Cisco Catalyst 9000 running
IOS-XE, with **NetBox** as the source of truth and secrets supplied from the
environment at run time.

Configuration is rendered in full and applied with `acm replace` (Atomic
Configuration Manager), which validates the candidate on the device, applies it
under a rollback timer, and only makes it permanent once the automation confirms
the commit.

## Requirements

Three of these are hard prerequisites that will not fail gracefully if unmet.

| | Requirement | Why |
|---|---|---|
| Platform | **Cisco Catalyst 9000 family** | Templates render IOS-XE EVPN/VXLAN syntax |
| Software | **IOS-XE 17.18.3 or later** | Earlier releases have no Atomic Configuration Manager |
| Source of truth | **NetBox 4.x** (developed against 4.6.4) | GraphQL filter syntax used here is 4.x |
| Control node | Python 3, Ansible — see [SETUP.md](SETUP.md) | `requirements.txt` / `requirements.yml` |

**The IOS-XE floor is not negotiable.** The whole safety model rests on
`acm replace`: validate the candidate on the device, apply it under a rollback
timer, and confirm only once the automation is still able to reach the switch.
Without Atomic Configuration Manager there is no validation step, no rollback
timer, and no safe way to push a full-configuration replacement — so on an
earlier release this project will not work, and should not be made to.

Secrets come from the environment (Infisical here, but anything that exports
variables will do). Nothing in this repository contains credentials.

## Quick start

```bash
git clone https://github.com/sergeynasonov/c9k-evpn-automation.git
cd c9k-evpn-automation

python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/ansible-galaxy collection install -r requirements.yml

cp group_vars/all/fabric.yml.example group_vars/all/fabric.yml
$EDITOR group_vars/all/fabric.yml          # your ASNs, addressing, NTP/SNMP/VTP
```

Export `NETBOX_API` and `NETBOX_TOKEN`, plus the switch credentials
(`ios_username`, `ios_password`) — see [SETUP.md](SETUP.md). Nothing in this
repository contains secrets, and `fabric.yml` is gitignored so your fabric's
addressing never gets committed.

Then, per switch, in order:

| Step | Command | What it does |
|---|---|---|
| 1 | *(by hand)* | Build the underlay — see [Phase 1](#phase-1--day-0-hand-build-the-underlay). Ansible never touches it |
| 2 | *(in NetBox)* | Model the device, then the tenants. **[The role README is the data contract](roles/acm_evpn/README.md)** |
| 3 | `ansible-playbook readiness.yml` | Assert the underlay is fit to provision; report what is not |
| 4 | `ansible-playbook generate_evpn.yml` | Render candidates to `switch-configuration/`. **No device changes** |
| 5 | *(read the diff)* | Compare against the previous render — see [Rendering](#rendering-safe--no-device-changes) |
| 6 | `ansible-playbook deploy_evpn.yml` | Apply under a rollback timer, confirm, save |

**Step 4 is the one to get into the habit of.** It touches nothing, and the
candidate it produces is exactly what a deploy would push — so reading that diff
is how you find out what a change actually does before it reaches a switch.

Scope any run with `-e deploy_target=<host-or-group>`; **never `--limit`**, which
the role refuses. See [Deploying](#deploying) for why.

## Layout

| Path | What it is |
|---|---|
| `deploy_evpn.yml` | Render and deploy to the fabric |
| `generate_evpn.yml` | Render only — no device changes |
| `readiness.yml` | Check a switch's hand-built underlay is fit to provision |
| `trm_allocate.yml` | Allocate TRM PIM register sources in NetBox — the only playbook that writes to it |
| `netbox_inv.yml` | NetBox dynamic inventory |
| `roles/acm_evpn/` | The fabric role. **[Its README](roles/acm_evpn/README.md) is the NetBox data-model contract** |
| `roles/underlay_readiness/` | Day-0 assertions, platform baseline and SNMPv3. Run at onboarding, and as an `acm_evpn` pre-flight |
| `scripts/` | One-off NetBox provisioning helpers |
| `switch-configuration/` | Rendered candidate configs (regenerated every run) |
| `commit-results/` | ACM net-diffs and validation failures from the last deploy |
| `readiness-results/` | One `<hostname>-readiness.cfg` per unready switch — paste-ready remediation |

Environment setup (Infisical, direnv, virtualenv, collections) is in
**[SETUP.md](SETUP.md)**.

---

# Device lifecycle

A switch passes through three phases. **Ansible owns only the third.**

```
   Day 0                    Onboarding                Day N
   hand-built underlay  →   modelled in NetBox   →   Ansible provisions tenants
   (reachability)           (device, IP, role)       (VRFs, VLANs, EVPN)
```

## Phase 1 — Day 0: hand-build the underlay

The underlay is **not** modelled in NetBox and **not** generated. It is built by
hand once, before the node is automated, and from then on the automation reads it
off the device and carries it forward verbatim.

This is deliberate. The underlay changes only when switches are added or removed,
and a mistake in it costs the management plane — the path the automation itself
arrives on. Modelling it would put that path at the mercy of a NetBox edit.

**On a border:**

1. Configure the `Loopback0` interface.
2. Configure the underlay handoff SVI, and allow that VLAN on the trunk to the
   upstream device.
3. Configure BGP with the fabric ASN, establish peering with upstream, and
   receive a default route into the global routing table — this is what gives
   the node reachability to the Ansible control node.
4. Configure the underlay routed interfaces with IP addressing.
5. Enable OSPF on `Loopback0` and the underlay routed interfaces.
6. Originate the default route into OSPF (`default-information originate`), so
   the rest of the fabric learns its way out through this node.

**On a leaf or spine:** steps 1, 4 and 5. The handoff SVI, trunk, external BGP
peering and default-route origination (2, 3 and 6) are border-only — the border
is what connects the fabric to the outside world, which is why it is the node
that originates the default.

**On a spine, additionally:** the anycast RP loopback carrying
`underlay_rp_address`, and the **MSDP mesh to every other spine**, sourced from
`Loopback0`:

```
ip msdp peer <other-spine-Loopback0> connect-source Loopback0
ip msdp originator-id Loopback0
```

MSDP must be configured on **both** ends. It is a full mesh, and a one-way
configuration is silent — each switch's own config looks complete, so nothing
reveals the gap until multicast misbehaves. This has already happened once in
this fabric: core2 pointed at core1 and core1 did not point back.

Because this is day-0 topology the automation deliberately does not own it, so
nothing reconciles the two ends. That is what the readiness role is for — see
below.

Once this is done the node is reachable and self-sufficient; everything from here
is additive.

### Checking Day 0: the underlay_readiness role

Day-0 config is hand-built and then preserved verbatim, which means nothing
would otherwise check it. `roles/underlay_readiness/` asserts a switch is
actually fit for fabric provisioning:

```bash
ansible-playbook readiness.yml -e deploy_target=access1-1
```

#### Output at fabric scale

A first fabric-wide run may find problems on dozens of switches, so the role is
built to report all of them rather than stop at the first:

- **One summary**, not per-host chatter — counts of ready / not ready /
  unreachable, then each unready switch with its findings.
- **One `<hostname>-readiness.cfg` per unready switch** in `readiness-results/`,
  containing paste-ready CLI grouped by config context (an interface missing
  five commands produces one stanza, not five). Findings needing a human
  decision — a router-id change that would reset adjacencies, a missing anycast
  RP loopback — appear as comments rather than commands. Nothing is ever
  applied; the file is for you to review and paste.
- **A switch that passes loses its file**, so the directory always lists
  exactly what is still outstanding.
- **One failure at the end**, not per host. Failing per-host would drop switches
  out of the play and the summary would under-report exactly the ones you care
  about.
- **An unreachable switch is neither ready nor unready.** It gets its own line
  in the summary and does not abort the run — at 500 switches some will be down.
- **`readiness-summary.csv`** alongside the per-switch files: one row per switch
  including the ready ones, with status, finding count, OSPF area size and the
  findings joined. Tracking a remediation campaign across hundreds of switches
  is a spreadsheet problem, and diffing two runs shows what regressed.

Files are managed **per switch**, not by wiping the directory: a scoped run
(`-e deploy_target=access1-1`) never deletes results for switches it did not
evaluate, and a switch that has since been fixed loses its file, so the
directory always lists exactly what is outstanding.

### Quieter output

The run is still one console line per host per task. To see only what changed
and failed — roughly a third of the output, task names intact:

```bash
ANSIBLE_DISPLAY_OK_HOSTS=false ANSIBLE_DISPLAY_SKIPPED_HOSTS=false \
  ansible-playbook readiness.yml
```

`community.general.dense` looks like the obvious answer and is not: it prints
`task 1.task 2.` with no task names, and throws callback dispatch errors against
this inventory. `community.general.yaml` was removed in that collection's v12 —
`result_format = yaml`, already set in `ansible.cfg`, replaces it.

It also runs automatically as a **pre-flight inside `acm_evpn`**, after the
running config is fetched and before anything is rendered — so it costs one
extra round-trip and no extra config fetch. A switch that fails it is not
deployed to. `generate_evpn.yml` reports the same findings without failing, so
you can still inspect a candidate for an unready switch.

Everything is a hard failure, because every item is something that produces a
fabric which configures cleanly and then silently fails to forward:

| Check | Why |
|---|---|
| `system mtu 9100`, `ip multicast-routing`, PIM RP / register-source / SSM | VXLAN needs the headroom; no multicast routing means no BUM replication |
| IP routing enabled — **derived, not text-matched** | See below |
| `Loopback0` is a /32, with `ip pim sparse-mode` and in OSPF | It is the VTEP source, the router-id and the PIM register source |
| `router ospf` with `max-metric on-startup`, `nsf cisco`, `fast-reroute`, `bfd all-interfaces` | Convergence behaviour — a node must not attract traffic before it has converged |
| OSPF `router-id` equals the `Loopback0` address | BGP takes its router-id from the same interface; disagreement makes route dumps unreadable |
| Borders additionally originate the default route | `default-information originate` |
| Every routed uplink carries the full command set, **including `bfd interval 250 min_rx 250 multiplier 3`** | `bfd all-interfaces` does nothing without per-interface timers |
| SNMPv3 user and group present | Checked with `show snmp user` / `show snmp group` — see below |
| **Spines:** anycast RP address carried, `ip msdp originator-id`, and an `ip msdp peer` to *every* other spine | A one-way MSDP mesh is invisible per-device. This fabric has already been bitten by it once |

The one **warning** is the OSPF area size: at more than 200 routers in an area
it recommends splitting, since the C9K supports 250. That is topology-driven
rather than configuration-driven — nothing is wrong, the area has just grown.

#### Platform defaults, and why `ip routing` is derived

Absence from `show running-config` means one of two things — not configured, or
configured as the **platform default** — and a text match cannot tell them
apart.

`ip routing` is the worked example. It is **default-on for the C9400, C9500 and
C9600**, so those never print it; the **C9300 has it off by default** and does.
Because the access layer is C9300 — 30-40 of them per pair of distribution
switches — most devices in a real fabric print the line, which is exactly why a
text match looks correct right up until it meets a distribution or core switch.

Asserting the literal line made access2-2 (a C9400) report it missing while
passing every OSPF check on the same run, which is impossible: IOS rejects
`router ospf` when routing is disabled, and `no ip routing` tears down protocol
configuration rather than leaving it stranded.

The check is therefore **asymmetric**. Absence of `ip routing` proves nothing,
but presence of `no ip routing` proves everything — a running config states
deviations from the default, so a C9400/9500/9600 with routing switched off by
hand says so explicitly. That is a hard failure on its own, and it overrides the
positive evidence below (someone can disable routing while OSPF config is still
in the file).

Otherwise it is satisfied by any of three things, all from data already
gathered, so the check costs no extra device interaction:

- the literal `ip routing` line, on platforms that print it;
- `router ospf <pid>` being configured, which cannot exist without it;
- a non-empty OSPF database — proof the switch is actually routing, not merely
  configured to.

**Before adding a line to `readiness_global_lines`, check it is not a default on
every platform in the fabric.** If it might be, derive it from something
observable instead. This is the mirror image of the SNMPv3 problem below: those
are lines the device will not report *even when set*; this is a line it will not
report *because it is default*.

Uplinks are **discovered, not named**: any routed (non-loopback, non-SVI)
interface enrolled in the fabric OSPF process. Nothing depends on an interface
naming convention, and an uplink placed in the wrong area is still found — so it
gets reported rather than silently dropping out of the check.

#### The platform baseline lives here too

`platform_baseline.j2` — local admin user, AAA, NTP, VTY hardening, clock, VTP,
spanning-tree, errdisable, UDLD, CoPP, licensing, `Vlan1`, the management VRF,
and the routing/multicast globals — moved out of `acm_evpn` into this role. It
is **applied** once at onboarding with `ios_config` (a merge: it installs what
is missing and removes nothing), rather than rendered on every overlay change.

Two things were wrong with rendering it. It is set once for the life of a
switch, so re-rendering it every deploy was pure churn — it was where nearly all
the residual ACM diff came from, because IOS-XE renders back platform defaults
the template did not model. And the checks above assert those same lines: while
`acm_evpn` still rendered them, a readiness failure blocked the very deploy that
would have installed them.

Running switches are unaffected — the config stops being template-owned, so the
candidate preserves it from the device, the same mechanism that carries the
underlay forward.

Two consequences worth knowing:

- **Rotating the local admin password is now a `readiness.yml` run, not a
  deploy.** `acm_evpn` no longer renders a `username` line.
- Anything IOS-XE hides from `show running-config` **must** live in this role.
  A full-replace candidate can never converge on a line the device will not read
  back. `snmp-server user` is the one that matters, and it is applied here by
  merge rather than rendered into a candidate.

#### Two kinds of invisible config, handled opposite ways

`show running-config` omits config for two quite different reasons, and they
need opposite treatment. Telling them apart is what keeps the apply count low.

**Set but never reported.** The device accepts the line and will not read it
back — `snmp-server user` is the one that matters. A full-replace candidate can
never converge on these, which is why they live in this role and are applied by
merge, and they cannot be asserted from the running config either, so
operational state is used instead (`show snmp user`).

The corollary is worth stating, because it removed more lines than any clever
handling did: **if an invisible line is also pointless, drop it.** The VTP
password and `vtp version 3` were both removed on that basis — neither is a
prerequisite for `vtp mode transparent` or `vtp domain`, and in transparent mode
neither does anything.

**Not reported because it is already the default.** `ip routing` on the
C9400/9500/9600, `ip tcp window-size 131072`, `hidekeys`. Pushing these achieves
nothing — the default is already in effect — and the device still will not
render them, so they reported changed forever while buying nothing.

The fix is to **stop pushing them and assert the negative instead**. A
non-default value *is* rendered, so `ip tcp window-size 65536` or `no hidekeys`
shows up and is caught. `ip routing` is the exception that stays in the
template, because the C9300 genuinely needs it.

Between the two, `readiness.yml` on an established switch should now report
**no baseline change at all**. That is a far more useful signal than a stable
non-zero count you have to remember the expected value of: anything it reports
is real drift. The report lists the lines, withholding only credential-bearing
ones.

#### The local admin hash is verified before it is pushed

A wrong `ios_password_secret_hash` is the one failure mode that defeats every
other safeguard here, so it is checked on the control node before anything
reaches a device:

- the switch **accepts** the command — a type-9 hash is opaque, so there is
  nothing to reject — and `ios_config` reports `changed` with no error;
- the existing SSH session stays up, so nothing fails during the run;
- IOS never renders the plaintext back, so no assertion against the running
  config can catch it;
- the lockout appears on the **next** connection, by which point every switch in
  the run has it.

Batching would not help, because nothing fails at a batch boundary; neither
would a post-apply reconnect probe, which contains the blast radius but still
loses a switch. The hash and the plaintext both come from Infisical, so
`filter_plugins/ios_secret.py` simply checks one against the other —
type 9 is scrypt with the salt embedded in the hash, so it is directly
verifiable with no device interaction.

Blocked: a valid hash of the wrong password, a plaintext pasted into the hash
variable, and any other hash type. Allowed: a correct hash, or an empty one —
which makes the template fall back to `secret 0 <plaintext>` for the device to
hash itself.

#### SNMPv3 lives here, not in the render

`snmp-server user` is hidden from `show running-config`. A full-replace
candidate could therefore never converge on it: the line was pushed, accepted,
and appeared in the next deploy's diff again because the device would not read
it back. Rendering it also wrote the auth and priv passwords in plaintext into
every candidate file on disk.

So this role owns SNMPv3 — credentials, ACL and configuration — and applies it
with `ios_config` in merge mode (it adds, it never removes). Presence is
verified from operational state; the passwords are never compared, only set.
Credentials come from `ios_snmp_auth` / `ios_snmp_priv` in the environment.

Existing switches are unaffected: SNMP is simply no longer template-owned, so
the candidate preserves whatever the device already has.

## Phase 2 — Onboarding: model the device in NetBox

Create the device in NetBox with:

- its **name**, matching the configured hostname;
- its **primary IPv4 address**, which **must be the `Loopback0` address** — EVPN
  peers are addressed from it, and the role asserts the two agree on every run;
- its **device role** (`fabric_leaf`, `fabric_border`) and, for spines, the
  `fabric_spine` tag;
- its **site**, inside the fabric's site group.

Then model the tenants — VRFs, VLANs, prefixes and the custom fields that enrol
them. Those are specified in full in the
[role README](roles/acm_evpn/README.md#netbox-data-model-contract).

If the fabric carries **tenant multicast**, the VRF also needs its MDT range set
Active and an RP; where the RP is external, each VTEP needs a unique PIM
register source, which `trm_allocate.yml` allocates from a pool and writes back
to NetBox. See [TRM](roles/acm_evpn/README.md#trm--tenant-routed-multicast).
That playbook is the one place this project writes to NetBox, and it runs at
onboarding — never as part of a deploy, so `generate_evpn.yml` stays a pure
function of NetBox and the candidate you review is the one that ships.

## Phase 3 — Day N: Ansible provisions the tenants

From here the automation owns the overlay: VRFs, tenant SVIs, EVPN instances,
the NVE interface and overlay BGP. It reads the underlay off each device and
re-emits it unchanged, so a hand-built uplink, access port or handoff trunk
survives every run.

The one exception is the **handoff trunk's allowed-VLAN list**, which grows as
NetBox gains VRF handoff VLANs — a new handoff SVI is useless if its VLAN cannot
reach the upstream. The trunk is found by already carrying an underlay handoff
VLAN, so nothing has to be modelled or named by convention.

---

# Operations

## Rendering (safe — no device changes)

```bash
ansible-playbook generate_evpn.yml
```

Writes candidate configs to `switch-configuration/` and reports anything
suspicious without failing. Always run this and read the diff before deploying:

```bash
rm -rf switch-configuration.before && cp -r switch-configuration switch-configuration.before
ansible-playbook generate_evpn.yml
diff -ru switch-configuration.before switch-configuration
```

## Deploying

```bash
ansible-playbook deploy_evpn.yml                             # whole fabric
ansible-playbook deploy_evpn.yml -e deploy_target=access1-1  # one leaf
ansible-playbook deploy_evpn.yml -e deploy_target=sites_evpn_building_a
```

**Scope with `deploy_target`, never `--limit`.** `--limit` is refused by the
role's first task. It can drop spines out of the play, and a spine that is not in
the play keeps a stale EVPN peer list on the device; `deploy_target` goes through
the playbook's host pattern, which always unions the spines back in.

Per device the deploy: uploads the candidate over SCP → `acm replace … validate`
→ `acm replace … timeout <n>` → waits → `acm confirm-commit` →
`acm save startup-config` → downloads the net-diff to `commit-results/`. If the
change breaks the management path the confirm never lands and the switch rolls
itself back when the timer expires.

**A switch already in the desired state is a success, not a failure.** ACM
reports `Config diff empty. No Diff to Validate/Apply` and does *not* report
`Status : Success`, so a naive not-Success test reads a perfectly converged
switch as a validation error — which is exactly what happened on the first fully
converged deploy. The role detects the empty diff, reports it, and skips the
apply, confirm, save and net-diff download, because there is nothing to do.

## Allocating TRM register sources

```bash
ansible-playbook trm_allocate.yml --check --diff   # dry run: who needs one, how many
ansible-playbook trm_allocate.yml                  # allocate
```

**This is the only playbook that writes to NetBox, and it touches no device.**
Everything else in this project treats NetBox as read-only; this one runs the
other way, so it is deliberately separate and never part of a deploy.

It is needed only by VRFs using an **external RP**. Those switches must register
sources from an address inside the tenant VRF, and every VTEP needs its own —
which means addresses have to be allocated per switch and recorded, or the next
run would hand out the same one twice. Anycast-RP VRFs need none of this: the RP
address is shared by every switch, so there is nothing to allocate.

Run it at **onboarding, before `deploy_evpn.yml`**. `acm_evpn` is read-only and
refuses to render a VTEP that needs a register source and has none, naming this
playbook in the failure.

For each switch and external-RP VRF it creates `Loopback<l3vni>` on the device
**in NetBox**, then allocates the next free address from that VRF's pool — the
prefix tagged `trm_register_source` — and attaches it to that interface with the
built-in `Loopback` role.

**The interface is the ownership record.** NetBox expresses "this address belongs
to this device" as IPAddress → Interface → Device, so the loopback has to exist
before an address can be attached to it. This is the only interface this project
models, and only because NetBox chooses the address: something has to record
which switch got it. The `trm_register_source` tag classifies *what* the address
is; the interface says *whose* it is.

Three things worth knowing before the first run:

- **Borders are allocated first, then leaves.** That is two plays rather than a
  sort key — "borders first" is not expressible as an ordering across parallel
  hosts, only as one play finishing before the next starts.
- **The pool is sized before anything is written.** A pool that cannot cover
  every VTEP that needs it fails the run with nothing allocated. Discovering it
  at switch 340 of 500 would be considerably worse. The check counts *free*
  capacity, so the borders play consuming addresses before the leaves play runs
  is handled correctly.
- **`--check` reports which switches need an address and how many, but not
  which address.** NetBox picks it at allocation time, so there is nothing to
  predict. (It also cannot simulate the write: netbox.netbox 3.23.0 has a
  check-mode defect, documented in `requirements.yml`.)

A final play prints every register source in NetBox with the switch and
interface holding it. An address whose owner is no longer in inventory is
reported as **orphaned and never reclaimed** — a switch can be temporarily out
of the play, and automatically freeing its address would hand it to another
switch while the first still has it configured.

## Changing something

| Change | What to do |
|---|---|
| New tenant VLAN | Add it in NetBox with role `edge`; deploy |
| New VRF | Set `l3vni` and `mcast_base_group`; create its multicast carve; deploy |
| New VRF handoff | Add the handoff VLAN with role `vrf_l3_handoff_vlan`, tagged with the border's hostname. The trunk list updates itself |
| New switch | Day 0 by hand, then onboard in NetBox, then `readiness.yml`, then deploy |
| Enable multicast on a VRF | Set its `239.<index>.2.0/24` (`default_mdt`) to **Active**, tag its edge VLANs `multicast_enabled`, add an RP address. External RP also needs a pool and a `trm_allocate.yml` run |
| Underlay change | On the device. Ansible will pick it up and carry it forward |
| Admin or SNMPv3 password | Change it in your secret store — they are read from the environment, never stored here — then `ansible-playbook readiness.yml` |
| SNMP/NTP/VTP identity, ASNs, addressing | Edit `group_vars/all/fabric.yml`, then `ansible-playbook readiness.yml` |
| What the baseline or the assertions contain | Edit `roles/underlay_readiness/defaults/main.yml` (behaviour, not site values), then `ansible-playbook readiness.yml` |

---

# Safety model

`acm replace` deletes anything absent from the candidate config, so the pipeline
front-loads its checks. All of these run **before a switch is touched**:

| Guard | Blocks on |
|---|---|
| Underlay readiness | A hand-built underlay that cannot carry the overlay — see [Phase 1](#checking-day-0-the-underlay_readiness-role) |
| VNI validation | Out-of-range, duplicate or L2/L3-colliding VNIs — fabric-wide |
| Multicast validation | A missing, malformed, duplicated or out-of-carve replication group |
| Anycast RP assertions | RP loopback disagreeing with `underlay_rp_address`, or a spine missing it |
| Peer addressing assertion | `Loopback0` disagreeing with the device's NetBox primary IP |
| Blast-radius guard | Removing >50% of any tracked object class, or **any** underlay object (zero tolerance) |

The blast-radius guard is the backstop for bad input: if NetBox returns nothing,
the candidate config is small and valid, and without the guard `acm replace`
would strip the switch. Override a legitimate large change with:

```bash
ansible-playbook deploy_evpn.yml -e acm_safety_force=true
```

Expect to need that flag on a first cutover from a legacy config, and after a
renumbering. Read the reported diff first — that is what it is for.

## Configuration is saved explicitly

`acm confirm-commit` cancels the rollback timer and makes a change permanent in
the **running** config — it does **not** write `startup-config`. The deploy
therefore runs `acm save startup-config` after confirming, and the readiness
role applies with `save_when: changed`. Without both, every switch would revert
its entire fabric configuration at the next power event, and nothing in the
pipeline would have reported a problem.

The save comes **after** the confirm, never before: the point of the rollback
timer is that a change which breaks the management path is undone when the
confirm never arrives, and saving first would let such a change survive the
reboot meant to recover from it.

---

# Tests

```bash
venv/bin/python -m unittest discover -s roles/acm_evpn/tests/unit
venv/bin/python -m unittest discover -s roles/underlay_readiness/tests/unit
```

Before changing anything, read **[CONTRIBUTING.md](CONTRIBUTING.md)** — it lists
the failure modes that have actually cost outages here, and the Ansible
mechanics that behave differently from how they read.

Stdlib `unittest` only: no pytest, no live NetBox, no Ansible runtime, no devices.
The dense logic — the NetBox transform, underlay extraction, VNI and multicast
allocation, peer selection, the deploy guard — lives in
`roles/acm_evpn/filter_plugins/` as plain Python precisely so it can be tested
this way.
