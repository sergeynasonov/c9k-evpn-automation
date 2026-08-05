# c9k-evpn-automation

[![CI](https://github.com/sergeynasonov/c9k-evpn-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/sergeynasonov/c9k-evpn-automation/actions/workflows/ci.yml)

Ansible automation for a campus EVPN/VXLAN fabric on Cisco Catalyst 9000 running
IOS-XE, with **NetBox** as the source of truth and secrets supplied from the
environment at run time.

Configuration is rendered in full and applied with `acm replace` (Atomic
Configuration Manager), which validates the candidate on the device, applies it
under a rollback timer, and only makes it permanent once the automation confirms
the commit.

## What this automates

**Today:**

- **EVPN overlays, IPv4 address-family** — VRFs, anycast-gateway SVIs, EVPN
  instances, the NVE interface and overlay BGP, rendered from NetBox.
- **Per-VRF BGP IPv4 handoffs on border switches** — the handoff SVI and its eBGP
  neighbour, so a tenant VRF reaches the outside world.
- **IPv4 TRM** (tenant routed multicast) — MVPN auto-discovery, default and data
  MDTs, anycast or external RP.
- **L2-only overlays** — a VLAN bridged across the fabric with no VRF and no
  gateway. Enrolled with the `l2vni` custom field, replicated via a multicast
  group, and opt-in on border switches.

**Not functional today. Near-term roadmap:**

- **ESI multihoming**
- **IPv6** — no `address-family ipv6` in the overlay, and no IPv6 TRM.

**The fabric it builds** is **iBGP EVPN**: one ASN across the fabric, spine
switches as route reflectors for both `l2vpn evpn` and `ipv4 mvpn`.
Reachability between VTEPs comes from an **IGP — OSPFv2** — not from BGP.
That IGP, and everything
else in the underlay, is **built by hand** and never touched by Ansible; see
[Phase 1](#phase-1--day-0-hand-build-the-underlay) for exactly what it must
contain.

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
| 2 | `scripts/netbox_provision_schema.py --apply` | Create the custom fields, tags and roles NetBox must carry. Once per NetBox |
| 3 | *(in NetBox)* | Model the device, then the tenants. **[The role README is the data contract](roles/acm_evpn/README.md)** |
| 4 | `ansible-playbook readiness.yml` | Assert the underlay is fit to provision; report what is not |
| 5 | `ansible-playbook generate_evpn.yml` | Render candidates, then ask each switch what it would change. **Nothing is applied** |
| 6 | *(read the diff)* | `commit-results/<host>-proposed-diff.cfg` — see [Rendering](#rendering-safe--no-configuration-changes) |
| 7 | `ansible-playbook deploy_evpn.yml` | Apply under a rollback timer, confirm, save |

**Step 5 is the one to get into the habit of.** It changes no configuration,
and the diff it produces is the switch's own answer to the candidate a deploy
would push — so it is how you find out what a change actually does before it
does it.

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
| `scripts/` | One-off NetBox provisioning helpers — `netbox_provision_schema.py` (the schema) and `netbox_multicast_carve.py` (the multicast carve) |
| `switch-configuration/` | Rendered candidate configs (regenerated every run) |
| `commit-results/` | `<host>-proposed-diff.cfg` from a generate; net-diffs and validation failures from a deploy |
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

Everything below is what `readiness.yml` checks for, so a switch built from
these blocks passes. **Substitute your own addressing**; the values shown are
examples, and every line that is not addressing is verbatim.

`readiness.yml` additionally *installs* the platform baseline (NTP, AAA, VTY
hardening, CoPP, CEF load-sharing and so on) with `ios_config`, so you do not
have to paste those. The CEF lines are shown below anyway because they matter
from the moment the underlay carries traffic, and re-applying them is a no-op.

### Every node — globals

```
system mtu 9100
!
ip routing
!
ip multicast-routing
ip pim register-source Loopback0
ip pim ssm default
ip pim rp-address 10.0.0.100 override
!
! ECMP hashing. Without include-ports, every flow between the same pair of
! VTEPs hashes to one uplink, because the outer VXLAN header is identical.
ip cef load-sharing algorithm include-ports source destination
ipv6 cef load-sharing algorithm include-ports source destination
```

`ip routing` prints on a C9300 and is a silent default on the C9400/9500/9600 —
harmless to paste on any of them. `10.0.0.100` is the fabric anycast RP, which
lives on the spine switches; it must match `underlay_rp_address` in
`group_vars/all/fabric.yml`.

### Every node — Loopback0

The VTEP source, the OSPF and BGP router-id, and the PIM register source. **Must
be a /32**, and must match the device's primary IPv4 in NetBox.

```
interface Loopback0
 ip address 10.0.0.11 255.255.255.255
 ip pim sparse-mode
 ip ospf 1 area 0
```

### Every node — routed uplinks

Every fabric-facing routed interface. Uplinks are **discovered**, not named, so
nothing depends on an interface-naming convention — any non-loopback, non-SVI
interface in OSPF process 1 is checked against this list.

```
interface TenGigabitEthernet1/1/1
 no switchport
 ip address 10.1.1.0 255.255.255.254
 no ip redirects
 no ip unreachables
 no ip proxy-arp
 ip pim sparse-mode
 ip ospf network point-to-point
 ip ospf 1 area 0
 load-interval 30
 carrier-delay msec 0
 bfd interval 250 min_rx 250 multiplier 3
 hold-queue 4094 in
 hold-queue 4094 out
```

`bfd interval` is what makes BFD real. `bfd all-interfaces` under `router ospf`
arms nothing on its own — an interface without per-interface timers falls back
to OSPF dead-interval detection, and you get 40-second failover while the
configuration looks correct.

### Every node — OSPF

```
router ospf 1
 router-id 10.0.0.11
 max-metric router-lsa include-stub summary-lsa external-lsa on-startup 60
 nsf cisco
 fast-reroute per-prefix enable prefix-priority low
 bfd all-interfaces
```

`router-id` must equal the `Loopback0` address — BGP takes its router-id from the
same interface, and a disagreement makes route dumps unreadable. `max-metric
on-startup` holds a rebooting node out of the transit topology until it has
converged.

### Spine switches only — anycast RP and MSDP

The RP loopback carries the **same address on every spine switch**, which is what makes
it anycast.

```
interface Loopback60000
 ip address 10.0.0.100 255.255.255.255
 ip pim sparse-mode
 ip ospf 1 area 0
!
ip msdp originator-id Loopback0
ip msdp peer 10.0.0.12 connect-source Loopback0
```

One `ip msdp peer` per **other** spine switch, addressed to that switch's
`Loopback0`.
It is a full mesh and **must be configured on both ends** — a one-way
configuration looks complete on each switch individually, so `readiness.yml`
checks every pair rather than each device in isolation.

### Border switches only — underlay handoff and default route

The handoff SVI lives in the **global** table (no `vrf forwarding`) and carries
the underlay eBGP session to upstream. Allow its VLAN on the trunk to the
upstream device.

```
interface Vlan98
 ip address 10.2.0.1 255.255.255.254
!
router bgp 64513
 bgp router-id interface Loopback0
 bgp log-neighbor-changes
 bgp graceful-restart
 no bgp default ipv4-unicast
 neighbor 10.2.0.0 remote-as 64512
 !
 address-family ipv4
  neighbor 10.2.0.0 activate
  network 10.0.0.11 mask 255.255.255.255
  aggregate-address 10.0.0.0 255.255.255.192 summary-only
  maximum-paths ibgp 4
 exit-address-family
!
router ospf 1
 default-information originate
```

`64513` is the fabric ASN (`evpn_bgp_asn`); `64512` is upstream's. **Ownership
of this stanza is by ASN**: the automation regenerates neighbours in the fabric
ASN — the EVPN peer group and the border-to-border session — and preserves
everything in any other ASN verbatim. So this eBGP session survives every
deploy untouched, and you never re-declare it anywhere.

`default-information originate` is **border-only**. It injects the default the
border switch learned from upstream into the fabric's OSPF, which is what gives
every leaf and spine switch a path out. A leaf switch running it would advertise
a default it cannot
honour.

Once this is done the node is reachable and self-sufficient; everything from here
is additive.

### Checking Day 0: the underlay_readiness role

Day-0 config is hand-built and then preserved verbatim, which means nothing
would otherwise check it. `roles/underlay_readiness/` asserts a switch is fit
for fabric provisioning:

```bash
ansible-playbook readiness.yml                            # whole fabric
ansible-playbook readiness.yml -e deploy_target=access1-1 # one switch
```

It also runs automatically as a **pre-flight inside `acm_evpn`**, so a switch
that fails it is never deployed to. `generate_evpn.yml` reports the same
findings without failing, so you can still inspect a candidate for an unready
switch.

Everything below is a hard failure, because each one produces a fabric that
configures cleanly and then silently fails to forward:

| Check | Why |
|---|---|
| `system mtu 9100`, `ip multicast-routing`, PIM RP / register-source / SSM | VXLAN needs the headroom; no multicast routing means no BUM replication |
| CEF load-sharing with `include-ports` | The default hashes on the outer header, which is identical for every flow between a pair of VTEPs — so the whole tunnel pins to one uplink |
| IP routing enabled — **derived, not text-matched** | It is a silent platform default on the C9400/9500/9600, so absence proves nothing |
| `Loopback0` is a /32, with `ip pim sparse-mode` and in OSPF | It is the VTEP source, the router-id and the PIM register source |
| `router ospf` with `max-metric on-startup`, `nsf cisco`, `fast-reroute`, `bfd all-interfaces` | Convergence behaviour — a node must not attract traffic before it has converged |
| OSPF `router-id` equals the `Loopback0` address | BGP takes its router-id from the same interface; disagreement makes route dumps unreadable |
| Border switches additionally originate the default route | `default-information originate` |
| Every routed uplink carries the full command set, **including `bfd interval 250 min_rx 250 multiplier 3`** | `bfd all-interfaces` does nothing without per-interface timers |
| SNMPv3 user and group present | Checked with `show snmp user` / `show snmp group`, never by reading passwords |
| **Spine switches:** anycast RP address carried, `ip msdp originator-id`, and an `ip msdp peer` to *every* other spine switch | A one-way MSDP mesh looks complete on each switch individually |

The one **warning** is OSPF area size: above 200 routers in an area it
recommends splitting, since the C9K supports 250. Nothing is wrong — the area
has simply grown.

#### What it writes

- **`readiness-results/<hostname>-readiness.cfg`** per unready switch —
  paste-ready CLI grouped by config context. Findings needing a human decision
  (a router-id change that would reset adjacencies, say) appear as comments
  rather than commands. Nothing is ever applied from these files.
- **`readiness-results/readiness-summary.csv`** — one row per switch including
  the ready ones, so a remediation campaign across hundreds of switches is a
  spreadsheet, and diffing two runs shows what regressed.

A switch that passes loses its file, so the directory always lists exactly what
is outstanding. Results are managed per switch, so a scoped run never deletes
results for switches it did not evaluate.

The run reports **one summary and one failure at the end**, not per host —
failing per-host would drop switches out of the play and under-report exactly
the ones you care about. An unreachable switch is neither ready nor unready: it
gets its own line and does not abort the run.

#### What it changes

Unlike everything else here, this role does configure two things — applied with
`ios_config` in merge mode, so they add what is missing and remove nothing:

- **The platform baseline** (`templates/platform_baseline.j2`) — local admin
  user, AAA, NTP, VTY hardening, spanning-tree, errdisable, UDLD, CoPP, CEF
  load-sharing and the routing/multicast globals. Set once at onboarding rather
  than rendered on every overlay change.
- **SNMPv3** — IOS-XE never reports `snmp-server user` back, so a full-replace
  candidate could never converge on it. It is applied here and checked from
  operational state instead.

Two consequences worth knowing: **rotating the local admin password is a
`readiness.yml` run, not a deploy**, and on an established switch readiness
should report **no baseline change at all** — anything it does report is real
drift.

Set `readiness_apply_baseline: false` or `readiness_apply_snmp: false` to check
without configuring anything.

#### Quieter output

```bash
ANSIBLE_DISPLAY_OK_HOSTS=false ANSIBLE_DISPLAY_SKIPPED_HOSTS=false \
  ansible-playbook readiness.yml
```

Note this hides every `debug` summary too, since those report `ok`.

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

## Rendering (safe — no configuration changes)

```bash
ansible-playbook generate_evpn.yml
```

Renders each switch's candidate to `switch-configuration/`, then **asks the
switch itself what it would change** and writes that to
`commit-results/<host>-proposed-diff.cfg`. Nothing is applied and no rollback
timer is armed.

The run ends with a line per switch:

```
core2         5 stanza(s) would change -- commit-results/core2-proposed-diff.cfg
access1-1     3 stanza(s) would change -- commit-results/access1-1-proposed-diff.cfg
dist3-1       already converged -- nothing to apply
access2-2     CANDIDATE REJECTED by the switch -- see commit-results/access2-2-validate-failed.cfg
```

**Read the proposed diff, not a text diff of the rendered files.** It is the
device's own answer, so it accounts for IOS-XE normalisation — VLAN ranges
collapsing, line reordering, platform defaults — and shows removals in
dependency order, which is what the switch will actually do:

```
Configuration Net-Diff:
interface nve1
 no member vni 10104 mcast-group 239.0.0.1
vlan configuration 104
 no member evpn-instance 104 vni 10104
no vlan configuration 104
...
```

It also needs no prior state, so changing `deploy_target` between runs cannot
mislead it — which is exactly where comparing against a previous render used to
fall down.

**"No configuration changes", precisely.** The candidate is copied to bootflash
and ACM is asked to validate and to diff. Neither applies anything. Generate has
always contacted devices — it reads the running config to build the candidate —
so this is not new contact, but it does leave a file on flash.

## Deploying

```bash
ansible-playbook deploy_evpn.yml                             # whole fabric
ansible-playbook deploy_evpn.yml -e deploy_target=access1-1  # one leaf switch
ansible-playbook deploy_evpn.yml -e deploy_target=sites_evpn_building_a
```

**Scope with `deploy_target`, never `--limit`.** `--limit` is refused by the
role's first task. It can drop spine switches out of the play, and a spine
switch that is not in the play keeps a stale EVPN peer list on the device;
`deploy_target` goes through the playbook's host pattern, which always unions
the spine switches back in.

Per device the deploy: uploads the candidate over SCP → `acm replace … validate`
→ `acm replace … timeout <n>` → waits → `acm confirm-commit` →
`acm save startup-config` → `acm save flash:acm-evpn/<MMDD-HHMMSS>.cfg` →
downloads the net-diff to `commit-results/`. If the
change breaks the management path the confirm never lands and the switch rolls
itself back when the timer expires.

**A switch already in the desired state is a success, not a failure.** ACM
reports `Config diff empty. No Diff to Validate/Apply` for a converged switch.
The role detects this, reports it, and skips the apply, confirm, save and
net-diff download, because there is nothing to do.

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

The `Loopback<l3vni>` interface is the ownership record — NetBox expresses
"this address belongs to this device" as IPAddress → Interface → Device. This is
the only interface this project models, and only because NetBox chooses the
address, so something has to record which switch got it.

Before the first run:

- **Border switches are allocated first, then leaf switches**, as two plays.
- **The pool is size-checked before anything is written**, against *free*
  capacity. A pool too small for every VTEP fails the run with nothing
  allocated.
- **`--check` reports which switches need an address and how many, but not
  which address** — NetBox picks it at allocation time. It also cannot simulate
  the write; see the netbox.netbox note in `requirements.yml`.

A final play prints every register source with the switch and interface holding
it. An address whose owner has left the inventory is reported as **orphaned and
never reclaimed**, since a switch can be temporarily out of the play and
freeing its address would hand it to another switch.

## Rolling back on the switch itself

Each successful deploy leaves a checkpoint on the device, so a switch can be
returned to its last automation-verified state without the control node, NetBox
or this repository:

```
switch# acm rollback ?
  <1-2>
Sno  Event   Result   Username   Time(M/D H:M:S)   Target-config
1    SAVE    Success  admin      08/05 11:28:12    flash:acm-evpn/0805-112812.cfg
```

The checkpoint is taken **after** the change is applied, confirmed and saved, so
it captures the state the automation just verified — which is what you want back
when a later hand-edit goes wrong.

**Filenames are unique per deploy and never reused.** `acm save` refuses to
overwrite, and deleting the file first leaves the old rollback entry in place
still naming that path — so rolling back to it would silently restore the newer
configuration. Every switch in one deploy shares the same filename, so "roll the
fabric back to the 11:28 deploy" is one identifiable set.

ACM keeps roughly 50 checkpoints and rotates the oldest out, but does not delete
the evicted files. Set `acm_checkpoint: false` to disable, or point
`acm_checkpoint_dir` elsewhere.

## Changing something

| Change | What to do |
|---|---|
| New tenant VLAN | Add it in NetBox with role `edge`; deploy |
| New VRF | Set `l3vni` and `mcast_base_group`; create its multicast carve; deploy |
| New VRF handoff | Add the handoff VLAN with role `vrf_l3_handoff_vlan`, tagged with the border switch's hostname. The trunk list updates itself |
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
| Anycast RP assertions | RP loopback disagreeing with `underlay_rp_address`, or a spine switch missing it |
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

---

## Author

Sergey Nasonov, Solutions Engineer, Cisco Systems — <snasonov@cisco.com>

Copyright © 2026 Sergey Nasonov, Cisco Systems.
Licensed under the [GNU General Public License v3.0 or later](LICENSE).
