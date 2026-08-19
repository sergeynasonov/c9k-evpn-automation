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
- **Multiple fabrics from one NetBox** — a fabric is a **site group**, or a
  **site** where that site has no site group. Each has its own iBGP ASN, its own
  underlay anycast RP and its own set of route reflectors, and they are fully
  independent: two fabrics may reuse the same VRFs, L3VNIs, VLAN IDs and
  multicast groups, because they never exchange them. Peers, meshes, VNI
  uniqueness and deploy scoping are all resolved per fabric. See
  [the data contract](roles/acm_evpn/README.md#fabrics).

**Not functional today. Near-term roadmap:**

- **ESI multihoming**
- **IPv6** — no `address-family ipv6` in the overlay, and no IPv6 TRM.

**The fabric it builds** is **iBGP EVPN**: one ASN across each fabric, spine
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

cp group_vars/all/deployment.yml.example group_vars/all/deployment.yml
$EDITOR group_vars/all/deployment.yml     # your NTP / VTP identity, and
                                          # optionally SNMPv3 and syslog
```

Export `NETBOX_API` and `NETBOX_TOKEN`, plus the switch credentials
(`ios_username`, `ios_password`) — see [SETUP.md](SETUP.md). Nothing in this
repository contains secrets, and `deployment.yml` is gitignored so your NTP,
SNMP, syslog and VTP values never get committed.

SNMPv3 and syslog in that file are **optional and enrolled by presence**: leave
the keys set and `readiness.yml` configures and checks them, comment them out
and it ignores them entirely. There is no flag to pass. Both ship uncommented in
`deployment.yml.example` with RFC 5737 placeholder addresses, so replace them or
comment them out before the first run.

`deployment.yml` is needed only by `readiness.yml`, which applies the platform
baseline. **`generate_evpn.yml` and `deploy_evpn.yml` need no configuration at
all** — everything the fabric is comes from NetBox.

Then, per switch, in order:

**Every run must be scoped.** `-e deploy_target=<fabric>` is required, on all
four playbooks — an unscoped run is refused, and the refusal lists the fabrics
in your inventory. Without it the host pattern means *every fabric node in the
estate*, which nobody intends by omission. `-e deploy_target=all` is accepted,
because that is a deliberate act rather than a missing flag.

| Step | Command | What it does |
|---|---|---|
| 1 | *(by hand)* | Build the underlay — see [Phase 1](#phase-1--day-0-hand-build-the-underlay). Ansible never touches it |
| 2 | `scripts/netbox_provision_schema.py --apply` | Create the custom fields, tags and roles NetBox must carry. Once per NetBox |
| 3 | *(in NetBox)* | Model the **fabric** (an ASN on each of its sites, an anycast-RP IPAddress per spine), then the devices, then the tenants. **[The role README is the data contract](roles/acm_evpn/README.md)** |
| 4 | `ansible-playbook readiness.yml -e deploy_target=<fabric>` | Assert the underlay is fit to provision; report what is not |
| 5 | `ansible-playbook generate_evpn.yml -e deploy_target=<fabric>` | Render candidates, then ask each switch what it would change. **Nothing is applied** |
| 6 | *(read the diff)* | `commit-results/<host>-proposed-diff.cfg` — see [Rendering](#rendering-safe--no-configuration-changes) |
| 7 | `ansible-playbook deploy_evpn.yml -e deploy_target=<fabric>` | Apply under a rollback timer, confirm, save |

**Step 5 is the one to get into the habit of.** It changes no configuration,
and the diff it produces is the switch's own answer to the candidate a deploy
would push — so it is how you find out what a change actually does before it
does it.

Scope with `-e deploy_target=<host-or-group>` — **never `--limit`**, which the
role also refuses, for a different reason. See [Deploying](#deploying) for both.

## Layout

| Path | What it is |
|---|---|
| `deploy_evpn.yml` | Render and deploy to the fabric |
| `generate_evpn.yml` | Render only — no device changes |
| `readiness.yml` | Check a switch's hand-built underlay is fit to provision |
| `trm_allocate.yml` | Allocate TRM PIM register sources in NetBox — the only playbook that writes to it |
| `netbox_inv.yml` | NetBox dynamic inventory |
| `group_vars/all/main.yml` | Connection wiring and the two project-wide conventions. Tracked — describes nobody's network |
| `group_vars/all/deployment.yml` | **Gitignored.** This deployment's NTP and VTP identity, plus optional SNMPv3 and syslog, from `deployment.yml.example`. Read only by `readiness.yml` |
| `roles/acm_evpn/` | The fabric role. **[Its README](roles/acm_evpn/README.md) is the NetBox data-model contract** |
| `roles/underlay_readiness/` | Day-0 assertions, platform baseline, and optional SNMPv3 and syslog. Run at onboarding, and as an `acm_evpn` pre-flight |
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

`system mtu` is checked as a floor of **1550** — 1500 plus VXLAN's 50 bytes of
outer header — not as the exact value below. Any value at or above the floor is
accepted, so a fabric standardised on 1600 or 9216 is left alone; 9100 is shown
because it also covers jumbo tenant frames, and it is what both the remediation
and the platform baseline propose for a switch below the floor.

The fabric still has to agree on whatever it picks — OSPF carries the MTU in
its DBD packets and will not form an adjacency across a mismatch — and that is
checked too. Expect it to surface indirectly, though: a leaf or distribution
switch out of step forms no adjacency, so its loopback never reaches the
underlay and it shows up as **unreachable** rather than as an MTU finding. A
border stays reachable over its BGP handoff and is reported properly. An
otherwise healthy switch that has gone unreachable is worth checking for this.

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
lives on the spine switches. Model it in NetBox as one IPAddress per spine —
same address, role **Anycast** (which is what lets NetBox hold the duplicates),
tagged `underlay_anycast_rp`, each assigned to that spine's RP loopback. NetBox
is the only source: a fabric with none, or a spine missing its own object, fails
the run.

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

One `ip msdp peer` per **other** spine switch **in the same fabric**, addressed
to that switch's `Loopback0`.
It is a full mesh and **must be configured on both ends** — a one-way
configuration looks complete on each switch individually, so `readiness.yml`
checks every pair rather than each device in isolation.

The mesh stops at the fabric boundary. MSDP carries source-active state for the
underlay anycast RP, and another fabric has a different RP on an underlay this
switch has no route into, so a session between them would be wrong even if it
came up.

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

`64513` is the fabric ASN, read from the ASN object on the fabric's NetBox
sites. `64512` is upstream's, from the handoff VLAN's `upstream_asn`. Both come
from NetBox only — there is no global fallback anywhere, because one global
value cannot describe more than one fabric. **Ownership
of this stanza is by ASN**: the automation regenerates neighbours in the fabric
ASN — the EVPN peer group and the border-to-border session — and preserves
everything in any other ASN verbatim. So this eBGP session survives every
deploy untouched, and you never re-declare it anywhere.

`default-information originate` is **border-only**. It injects the default the
border switch learned from upstream into the fabric's OSPF, which is what gives
every leaf and spine switch a path out. A leaf switch running it would advertise
a default it cannot
honour.

### Every node — SNMPv3 *(optional)*

The one block on this page you do **not** paste: `readiness.yml` configures it
for you, in merge mode. It is here because it is *checked*, and because it is
the only check a switch built exactly to this page can still fail.

```
ip access-list standard SNMP-ACL
 10 permit 198.51.100.20
!
snmp-server group AuthPrivGroup v3 priv access SNMP-ACL
snmp-server user example-snmpv3 AuthPrivGroup v3 auth sha <auth> priv aes 128 <priv>
```

**Enrolled by presence.** `snmp_v3_user`, `snmp_v3_group` and `snmp_acl_name` in
`group_vars/all/deployment.yml` are the switch. Set them and the role reads
SNMPv3 state off every switch, configures what is missing, and reports what it
cannot fix. Comment all three out and it does none of that — no read, no push,
no check, no finding. A deployment with no SNMP collector says so by not naming
one, and is not told forever that its fabric is unready for a collector that
does not exist.

**All three, or none.** Half a set fails the run. Once absence means "no", a
mistyped key name reads exactly like a deliberate opt-out and would silently
stop configuring every switch in the fabric — so it is rejected instead.

The two passwords come from the environment (`ios_snmp_auth`, `ios_snmp_priv`)
and are never written to a file. See [SETUP.md](SETUP.md).

`snmp_acl` — the permitted collectors — is optional *within* that: leave it out
and the group carries no `access` clause. It is also the first thing to check
when a poll times out, because a source that is not in the ACL is dropped
silently: no auth error, no report PDU, no answer at all.

### Every node — syslog *(optional)*

Same rule, one variable. `syslog_hosts` in `group_vars/all/deployment.yml` is a
list of collectors, shaped exactly like `snmp_acl`:

```yaml
syslog_hosts:
  - "198.51.100.30"
  - "198.51.100.31"
```

and every switch gets:

```
logging trap notifications
logging facility syslog
logging host 198.51.100.30
logging host 198.51.100.31
```

one `logging host` per entry. Comment the key out — list items included — and
nothing syslog-related is pushed, checked or reported.

Each entry must be a bare IPv4 address: no mask, no hostname, no port. Anything
else fails the run rather than being quietly dropped, for the same reason half
an SNMP triple does.

Unlike SNMPv3 this rides in the platform baseline rather than a task of its own,
because all three lines *are* rendered back into `show running-config`
(`notifications` is not the default severity, `syslog` is not the default
facility), so `ios_config` converges on them normally. The CLI is defined once,
in `readiness_syslog_lines`, and both the push and the check read that same
variable — so a switch cannot pass the check against a different set of
collectors than the baseline was told to install.

`logging source-interface Loopback0` is applied either way. It is part of the
baseline's source-interface hygiene, not part of this enrolment.

**Run `readiness.yml` on a new switch before `generate_evpn.yml` or
`deploy_evpn.yml`.** The readiness role also runs as a pre-flight inside
`acm_evpn`, but that path only *checks* — it never configures. So a switch that
has never had a standalone readiness run reports its SNMPv3 user and group
missing, which looks like a Day-0 mistake and is really just an ordering one:

```bash
ansible-playbook readiness.yml -e deploy_target=<new switch>   # configures, then checks
```

If it still reports the user missing after an enrolled run, the passwords are
not reaching the play — the line is pushed with empty credentials, which the device accepts
in a form that never matches. Check `ios_snmp_auth` and `ios_snmp_priv` are in
Infisical, then `direnv reload`.

Both checks read **operational state**, `show snmp user` and `show snmp group`,
rather than the running config. IOS-XE hides `snmp-server user` from `show
running-config` entirely, which is also why SNMPv3 is installed here in merge
mode instead of being rendered into the `acm_evpn` candidate — a full-replace
candidate could never converge on a line the device will not read back.

### Every node — one default to leave alone

`readiness.yml` fails a switch for this being *present*, which is why it does
not appear in the blocks above. A default is not rendered into the running
config, so a value that **is** rendered was set deliberately by someone.

| Do not set | Why, and how to clear it |
|---|---|
| `no hidekeys`, under `archive` → `log config` | Archived configurations would store credentials in clear text. `hidekeys` is the default, so the baseline never pushes it — but an explicit `no hidekeys` overrides it. `archive` / `log config` / `hidekeys` |

Nothing else about platform tuning is checked, and `ip tcp window-size` is the
worked example of why. It was checked against one fabric-wide value until a real
fabric reported all seven switches at once for a setting that was correct on
each of them — the default is platform-specific. A check earns its place here
only if the thing it finds is wrong on *any* platform, not merely different
between them.

Once this is done the node is reachable and self-sufficient; everything from here
is additive.

### Checking Day 0: the underlay_readiness role

Day-0 config is hand-built and then preserved verbatim, which means nothing
would otherwise check it. `roles/underlay_readiness/` asserts a switch is fit
for fabric provisioning:

```bash
ansible-playbook readiness.yml -e deploy_target=site_group_evpn_campus # one fabric
ansible-playbook readiness.yml -e deploy_target=sites_evpn_building_a  # one building
ansible-playbook readiness.yml -e deploy_target=access1-1              # one switch
ansible-playbook readiness.yml -e deploy_target=all                    # every fabric
```

It also runs automatically as a **pre-flight inside `acm_evpn`**, so a switch
that fails it is never deployed to. `generate_evpn.yml` reports the same
findings without failing, so you can still inspect a candidate for an unready
switch.

**It checks every switch in the fabric's transit path, not only the VTEPs.** A
distribution switch between a fabric leaf and a fabric border carries fabric
traffic, so its underlay has to be right even though `acm_evpn` renders nothing
for it. The three checks that look VTEP-specific all still apply to one:

| Check | Why it applies to a non-VTEP |
|---|---|
| `Loopback0` present, /32, in OSPF | It is the OSPF router-id and the management address, whether or not it sources VXLAN |
| `ip pim register-source Loopback0` | PIM transits the switch, so it still registers |
| `ip pim rp-address <anycast RP>` | Same reason — it needs to know the fabric's RP to forward multicast |

**And nothing else.** Fabric leaves, fabric borders, spines and distribution
switches are the whole of what this project contacts. A run scoped by site or
site group selects everything in that site — servers, printers, wireless
controllers — because `deploy_target` replaces the playbook's role-scoped
default pattern rather than intersecting with it. Anything outside those roles
is dropped before a single device is contacted, and **named while it is
dropped**:

```
scope: 2 device(s) selected by the target but skipped — not a fabric role:
  fileserver, wan-rtr-1. A switch in the fabric transit path listed here needs
  its device role adding to fabric_managed_groups.
```

That report is the point. A transit switch modelled under a role name
`fabric_managed_groups` does not list is indistinguishable from a printer here,
and silently skipping it would leave a hole in exactly the path this check
exists to cover. Widen `fabric_managed_groups` when the report names one.

Only the spine-specific block is skipped for them: carrying the anycast RP
*address* on a loopback, and the MSDP mesh, belong to route reflectors.

Everything below is a hard failure, because each one produces a fabric that
configures cleanly and then silently fails to forward:

| Check | Why |
|---|---|
| `system mtu` at or above **1550** | 1500 + 50 bytes of VXLAN header. The exact value is yours. The fabric must also agree on it, or OSPF refuses the adjacency — but a switch that disagrees usually presents as unreachable, not as this finding |
| `ip multicast-routing`, PIM RP / register-source / SSM | No multicast routing means no BUM replication |
| CEF load-sharing with `include-ports` | The default hashes on the outer header, which is identical for every flow between a pair of VTEPs — so the whole tunnel pins to one uplink |
| IP routing enabled — **derived, not text-matched** | It is a silent platform default on the C9400/9500/9600, so absence proves nothing |
| `Loopback0` is a /32, with `ip pim sparse-mode` and in OSPF | It is the VTEP source, the router-id and the PIM register source |
| `router ospf` with `max-metric on-startup`, `nsf cisco`, `fast-reroute`, `bfd all-interfaces` | Convergence behaviour — a node must not attract traffic before it has converged |
| OSPF `router-id` equals the `Loopback0` address | BGP takes its router-id from the same interface; disagreement makes route dumps unreadable |
| Border switches additionally originate the default route | `default-information originate` |
| Every routed uplink carries the full command set, **including `bfd interval 250 min_rx 250 multiplier 3`** | `bfd all-interfaces` does nothing without per-interface timers |
| SNMPv3 user and group present — **only if enrolled** | Checked with `show snmp user` / `show snmp group`, never by reading passwords. `readiness.yml` installs these; the `acm_evpn` pre-flight only checks them. A deployment that declares no SNMP identity is not checked, and the device read is skipped too |
| `logging trap` / `logging facility` / `logging host` — **only if enrolled** | Present only when `syslog_hosts` is declared. Read from the running config, unlike SNMPv3, because all three lines are rendered back |
| No `no hidekeys` under `archive log config` | Checked *negatively* — a platform default is never rendered, so a value that is rendered was set deliberately |
| **Spine switches:** anycast RP address carried, `ip msdp originator-id`, and an `ip msdp peer` to *every* other spine switch **in the same fabric** | A one-way MSDP mesh looks complete on each switch individually |

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
- **SNMPv3**, when enrolled — IOS-XE never reports `snmp-server user` back, so a
  full-replace candidate could never converge on it. It is applied here and
  checked from operational state instead.
- **Syslog**, when enrolled — part of the baseline above rather than a step of
  its own, because these lines *do* come back in the running config.

Both are optional and are enrolled by what `group_vars/all/deployment.yml`
declares, not by a command-line flag — see the Day 0 sections above.

Two consequences worth knowing: **rotating the local admin password is a
`readiness.yml` run, not a deploy**, and on an established switch readiness
should report **no baseline change at all** — anything it does report is real
drift.

Set `readiness_apply_baseline: false` or `readiness_apply_snmp: false` to check
without configuring anything. Neither decides *enrolment* — an unenrolled
deployment is not checked either, whereas these leave the checks running against
something the role was told not to configure.

#### Quieter output

```bash
ANSIBLE_DISPLAY_OK_HOSTS=false ANSIBLE_DISPLAY_SKIPPED_HOSTS=false \
  ansible-playbook readiness.yml -e deploy_target=site_group_evpn_campus
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

### Doing it by CSV

Past two or three switches that is a lot of clicking. [`scripts/`](scripts/)
ships import templates for exactly these objects, which NetBox takes at
**Devices → Devices → Import** and the equivalent page for each type:

| | |
|---|---|
| [`device_import.csv`](scripts/device_import.csv) | name, role, manufacturer and device type, platform, serial, site, rack position, and the `fabric_spine` tag |
| [`interface_import.csv`](scripts/interface_import.csv) | device, name, type — including the `Loopback0` every fabric switch needs |
| [`ip_address_import.csv`](scripts/ip_address_import.csv) | address, role, its device and interface, `is_primary`, tags |

**Import in that order.** Each file resolves its references by name, so the
devices have to exist before their interfaces, and both before their addresses.

Fill in the address `role` column even though NetBox does not require it. A
column of loopbacks read side by side is where a wrong digit in a network
address is obvious — on a per-object form it is not. `anycast` is the one value
that changes behaviour: it is what allows NetBox to hold the underlay RP's
duplicate address across the spines, and
[`ip_address_import.csv`](scripts/ip_address_import.csv) carries that as a
worked example, `underlay_anycast_rp` tag included, so the Day 0 RP model above
comes out of the same import.

[`scripts/README.md`](scripts/README.md) documents every column, what must exist
before the first import, and the traps — chiefly that everything matches by
*name* except `tags`, which matches by *slug*.

### Then model the tenants

VRFs, VLANs, prefixes and the custom fields that enrol them, specified in full
in the [role README](roles/acm_evpn/README.md#netbox-data-model-contract).
Those are not covered by the templates above: they are a data model rather than
a per-device form, so the clicking does not scale the same way.

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
ansible-playbook generate_evpn.yml -e deploy_target=site_group_evpn_campus
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
ansible-playbook deploy_evpn.yml -e deploy_target=site_group_evpn_campus  # one fabric
ansible-playbook deploy_evpn.yml -e deploy_target=sites_evpn_building_a  # one building
ansible-playbook deploy_evpn.yml -e deploy_target=access1-1              # one leaf switch
ansible-playbook deploy_evpn.yml -e deploy_target=all                    # every fabric
```

**`deploy_target` is required — an unscoped run is refused**, before anything is
contacted, with the fabrics in your inventory listed. Without it the pattern
means every fabric node in the estate, which nobody intends by omitting a flag;
`all` says it deliberately.

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
ansible-playbook trm_allocate.yml -e deploy_target=site_group_evpn_campus --check --diff   # dry run
ansible-playbook trm_allocate.yml -e deploy_target=site_group_evpn_campus                  # allocate
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
| New switch | Day 0 by hand, then onboard in NetBox, then `readiness.yml`, then deploy — both scoped to its fabric |
| Enable multicast on a VRF | Set its `239.<index>.2.0/24` (`default_mdt`) to **Active**, tag its edge VLANs `multicast_enabled`, add an RP address. External RP also needs a pool and a scoped `trm_allocate.yml` run |
| Underlay change | On the device. Ansible will pick it up and carry it forward |
| Admin or SNMPv3 password | Change it in your secret store — they are read from the environment, never stored here — then re-run `readiness.yml` |
| SNMP / syslog / NTP / VTP identity | Edit `group_vars/all/deployment.yml`, then re-run `readiness.yml` |
| Turn SNMPv3 or syslog off entirely | Comment its keys out of `group_vars/all/deployment.yml`. The role then stops checking, pushing and reporting it — it does **not** remove what is already on the switches |
| Fabric ASN, anycast RP, upstream ASN | Edit NetBox — see [the data contract](roles/acm_evpn/README.md). Nothing to edit here |
| What the baseline or the assertions contain | Edit `roles/underlay_readiness/defaults/main.yml` (behaviour, not site values), then re-run `readiness.yml` |

---

# Safety model

`acm replace` deletes anything absent from the candidate config, so the pipeline
front-loads its checks. All of these run **before a switch is touched**:

| Guard | Blocks on |
|---|---|
| Underlay readiness | A hand-built underlay that cannot carry the overlay — see [Phase 1](#checking-day-0-the-underlay_readiness-role) |
| VNI validation | Out-of-range, duplicate or L2/L3-colliding VNIs — across the whole of one fabric, not just the switch being rendered |
| Multicast validation | A missing, malformed or out-of-carve replication group; two segments of one fabric claiming the same group |
| Anycast RP assertions | RP loopback disagreeing with the fabric's anycast RP, a spine switch missing it, or two spine switches in one fabric carrying **different** addresses |
| Peer addressing assertion | `Loopback0` disagreeing with the device's NetBox primary IP |
| Blast-radius guard | Removing >50% of any tracked object class, or **any** underlay object (zero tolerance). Strictly greater: one of two members passes, the last member of a class does not |

The blast-radius guard is the backstop for bad input: if NetBox returns nothing,
the candidate config is small and valid, and without the guard `acm replace`
would strip the switch. Override a legitimate large change with:

```bash
ansible-playbook deploy_evpn.yml -e deploy_target=<fabric> -e acm_safety_force=true
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

Stdlib `unittest` only: no pytest, no live NetBox, no devices. One test
(`test_fabric_resolve.py`) drives the real fabric-resolution tasks under
`ansible-playbook` against static inventories, because that logic is Jinja
rather than Python; it skips itself if `ansible-playbook` is not on `PATH`.
The dense logic — the NetBox transform, underlay extraction, VNI and multicast
allocation, peer selection, the deploy guard — lives in
`roles/acm_evpn/filter_plugins/` as plain Python precisely so it can be tested
this way.

---

## Author

Sergey Nasonov, Solutions Engineer, Cisco Systems — <snasonov@cisco.com>

Copyright © 2026 Sergey Nasonov, Cisco Systems.
Licensed under the [GNU General Public License v3.0 or later](LICENSE).
