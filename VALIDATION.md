# Dataplane validation

The unit suite proves the automation renders what it means to; CI proves it stays
that way. Neither proves a packet crosses the fabric. This is the plan for that.

It is built to be **run, not read**. The forwarding matrix is derived from NetBox
rather than written out by hand, so it stays correct as the fabric changes and
can be re-run after any deploy as a regression check. Only the parts that
genuinely need a human are manual.

**Status: designed, not yet built or executed.**

---

## Part 1 — Test data to create in NetBox

The current lab cannot exercise the matrix: it has one tenant VRF and no
segment pairs that isolate a single variable. Six VLANs across two VRFs cover
every path.

### VRFs

| VRF | `l3vni` | Purpose |
|---|---|---|
| `VAL_A` | 20011 | Primary test tenant |
| `VAL_B` | 20012 | Inter-VRF peer — traffic between the two must leave the fabric |

Set `mcast_base_group` on both from the existing carve — run
`scripts/netbox_multicast_carve.py` rather than picking addresses by hand, so
the 239.x allocations stay consistent with what is already deployed.

### VLANs

| VID | Name | VRF | Role | Tags | Prefix | Isolates |
|---|---|---|---|---|---|---|
| 201 | `VAL_A_BR1` | `VAL_A` | `edge` | `multicast_enabled` | `10.201.0.0/24` | Bridged segment, present on both leaves |
| 202 | `VAL_A_RT1` | `VAL_A` | `edge` | `dag_routed` | `10.202.0.0/24` | Routed-only — ingress replication, `advertise mac disable` |
| 203 | `VAL_A_BR2` | `VAL_A` | `edge` | `multicast_enabled` | `10.203.0.0/24` | Second bridged segment — intra-VRF inter-segment routing |
| 211 | `VAL_B_BR1` | `VAL_B` | `edge` | `multicast_enabled` | `10.211.0.0/24` | Inter-VRF peer, bridged |
| 212 | `VAL_B_RT1` | `VAL_B` | `edge` | `dag_routed` | `10.212.0.0/24` | Inter-VRF peer, routed-only |
| 220 | `VAL_L2` | *none* | — | `l2vni` set, `mcast_group` set | *none* | L2-only overlay — no VRF, no SVI, no gateway |

Why these six and not fewer: 201 vs 203 isolates *routing between segments of
one VRF* from *bridging within one segment*; 201 vs 202 isolates the two BUM
replication paths and the MAC-advertisement asymmetry; 201 vs 211 forces the
external hairpin; 220 is the only shape with no gateway at all.

Keep the existing `vrf_l3_handoff_vlan` per VRF per border, and tag 220 for both
borders so the dual-homed L2 handoff is exercised.

### Endpoints — three VMs, not nine

Give each endpoint VM a **trunk** port to its leaf and put every test VLAN on it
as an 802.1Q sub-interface. Three VMs cover the whole matrix:

| VM | Attached to | Interfaces |
|---|---|---|
| `ep-leaf1` | leaf1, trunk | `.201`, `.202`, `.211`, `.220` |
| `ep-leaf2` | leaf2, trunk | `.201`, `.203`, `.212`, `.220` |
| `ep-ext` | outside the fabric, past the handoff | single interface |

`ep-leaf1.201` and `ep-leaf2.201` are the same VLAN on different leaves — the
VXLAN-bridged case. Everything else pairs across VLANs, VRFs or the fabric edge.

**Ubuntu for everything automated.** Add one Windows VM on an *access* port
later for spot-checks only — its value is real client behaviour (DHCP option
lists, NetBIOS/mDNS broadcast, SMB), not coverage, and automating it via WinRM
costs more than it returns.

Model the VMs in NetBox as virtual machines with their interfaces, IPs and VLAN
assignments, tagged `validation_endpoint`. That is what lets the automation
derive expectations instead of being told them.

### Prerequisite: keep virtual machines out of the fabric plays

**`nb_inventory` fetches virtual machines unconditionally**, alongside devices —
`fetch_hosts()` calls both `/api/dcim/devices/` and
`/api/virtualization/virtual-machines/`, and `netbox_inv.yml` sets nothing that
would stop it. So the moment these endpoints exist in NetBox with a site and
`status: active`, they land in `sites_<slug>`, `fabric_resolve.yml` resolves them
into the fabric, and `readiness.yml -e deploy_target=<site>` tries to SSH each
one as an IOS switch. Readiness deliberately checks every device in the transit
path rather than only fabric nodes, so nothing downstream drops them.

This is not specific to validation — it is true today for anyone whose NetBox
holds virtual machines. Two changes make it safe, and both are small:

1. Add `is_virtual` to `group_by` in `netbox_inv.yml`. It is a supported choice
   and the plugin already computes the value, so this only surfaces what is
   there.
2. Have `fabric_resolve.yml` end the play for a virtual host, the same way it
   already does for a device belonging to no fabric — reported once, never
   fatal.

The endpoints then stay in NetBox as the source of truth for the test topology,
addressable by the validation playbooks and invisible to the fabric ones.

---

## Part 2 — Automation design

```
roles/dataplane_validation/
  tasks/
    01_discover.yml     endpoints + their VLAN/VRF from NetBox
    02_expect.yml       derive the expected matrix (below)
    03_configure.yml    sub-interfaces and addresses on the endpoint VMs
    04_forward.yml      run every ordered pair, assert reachability AND path
    05_failover.yml     one scenario: probe, fail, measure, restore
    06_report.yml       validation-results/*.csv + one summary
```

Playbooks: `validate_forwarding.yml` (safe, repeatable, non-disruptive) and
`validate_failover.yml -e scenario=<name>` (disruptive, gated).

Inventory: the existing `netbox_inv.yml` already returns the switches; endpoints
come from the same NetBox as virtual machines, so one inventory serves both.

### The expectation matrix is derived, not written

For every ordered pair of endpoint interfaces, NetBox already says what the
answer should be:

| Pair | Reachable | TTL delta | Path being proven |
|---|---|---|---|
| Same VLAN, same leaf | yes | 0 | Local switching |
| Same VLAN, different leaf | yes | 0 | VXLAN bridged, no routing |
| Different VLAN, same VRF | yes | 1 | Anycast gateway routes once |
| Different VRF | yes | **≥3** | Out via handoff, through external, back in |
| L2-only (220) to same VLAN | yes | 0 | Bridged with no gateway anywhere |
| L2-only to anything else | **no** | — | There is no gateway to route it |
| Anything to `ep-ext` | yes | ≥2 | Leaf → border → outside |

**Assert the TTL delta, not just reachability.** Linux starts at 64, so
`64 − observed` is the hop count. This is what separates "a packet arrived" from
"a packet took the path the design intends" — an inter-VRF flow that shows a
delta of 1 has found a shortcut inside the fabric, which is a VRF leak, and a
plain ping test would call it a pass.

Two negative expectations matter as much as the positives: L2-only must reach
nothing off its segment, and its gateway address must not answer ARP.

### Failure injection, safely

Each `validate_failover.yml` scenario runs the same five steps:

1. Confirm the baseline matrix passes — never measure failover on a fabric that
   was already broken.
2. Start continuous probes on every affected pair:
   `ping -i 0.02 -c 3000 -D <dst>` — 60 seconds at 50 pps, timestamped. Each
   lost packet is 20 ms, which is the resolution needed to hold BFD to its
   claimed sub-second.
3. Inject the failure.
4. Restore, in an `always:` block so a failed assertion cannot leave the fabric
   down.
5. Parse the probe output for loss count and the longest gap; assert against the
   scenario's threshold.

Three safety rules, following the project's existing posture:

- **`reload in 15` before touching any device you reach through the thing you
  are about to break**, cancelled after restore. A lost session then costs
  fifteen minutes, not a lab rebuild.
- Disruptive playbooks are gated on `validation_allow_disruptive=true`, the same
  shape as `acm_safety_force`.
- `deploy_target` stays required, so a scenario cannot escape its fabric.

### Reporting

`validation-results/` mirroring `readiness-results/`: per-scenario detail files,
one `validation-summary.csv`, one console summary as a list of strings. Same
conventions, so the output is already familiar.

---

## Part 3 — What runs automatically

Once the above exists, one command covers most of the original plan.

### `validate_forwarding.yml` — non-disruptive, run after any deploy

| # | Test | Expected |
|---|---|---|
| 1.1 | DHCP on every endpoint sub-interface | Address from the VLAN's prefix; lease renews |
| 1.2 | Two clients, same VLAN, different leaves, simultaneously | Both get addresses; **each reply reaches the originating leaf**. Relay sources from `Loopback0` with RFC 3527 `link-selection`/`server-override` because `giaddr` is identical on every leaf — this fails if the server ignores those sub-options |
| 2.1 | Full pair matrix, all directions | Reachability **and TTL delta** per the table above |
| 2.2 | Every endpoint to `ep-ext` | Reachable, delta ≥2 |
| 2.3 | `ep-ext` to a tenant subnet and to a loopback | Reachable — proves no component prefix black-holes into the border's aggregate |
| 3.1 | Multicast intra-VRF, bridged source → routed receiver and reverse | Traffic received; anycast RP |
| 3.2 | Multicast inter-VRF, `VAL_A` source → `VAL_B` receiver | Traffic received via the external RP |
| 4.1 | ARP flood in 201 (bridged, `mcast-group`) | Reaches the far leaf |
| 4.2 | ARP flood in 202 (`dag_routed`, `ingress-replication`) | Reaches the far leaf — a different replication mechanism, same result |
| 4.3 | MAC table on both leaves for 201 vs 202 | 201 learns remote MACs, 202 does not — `advertise mac disable` is global and only bridged DAG overrides it. Both correct |
| 5.1 | 1500-byte payload, DF set, across VXLAN | Passes. Validates the 1550 floor |
| 5.2 | 200 flows between one VTEP pair, varied ports | Spread across uplinks — the whole point of `include-ports` hashing |
| 6.1 | 220 to 220 across leaves | Reachable |
| 6.2 | 220 to any other VLAN | **Unreachable**, and no ARP reply from the gateway address |
| 6.3 | `VAL_A` to `VAL_B` with the handoff shut | **Unreachable** — proves there is no internal leak |

Multicast tooling: `iperf -s -u -B <group>` / `iperf -u -c <group> -T 5`
(iperf2 handles multicast; iperf3 does not). `omping` is a faster first check.

### `validate_failover.yml -e scenario=<name>` — disruptive

Every scenario is a **cycle**: fail, verify the alternate path, restore, verify
recovery, then repeat against the partner. A test that fails only one side
proves half a design — this fabric was bitten by a one-way MSDP mesh that looked
complete from both ends.

| Scenario | Injection | Expected |
|---|---|---|
| `border_a` / `border_b` | Shut both fabric uplinks on that border | Forwarding continues via the other; loss < 1 s. Run with the dual-homed 220 handoff active |
| `spine_a` / `spine_b` | Shut spine uplinks, **with multicast flowing** | Unicast and multicast both survive. This is the anycast RP and MSDP mesh's entire reason for existing and has never been exercised |
| `collapsed` | Same, on the physical lab | Worse case: one node is RP, route reflector and border at once |
| `border_external` | Shut the handoff interface only, border still up | Default route withdraws; other border takes over |
| `underlay_link` | Shut one link on the border↔leaf transit path | ECMP reroutes; loss < 1 s |
| `underlay_device` | Shut all `transit` uplinks | Everything crossing the distribution switch reroutes. `acm_evpn` renders nothing for it, but every packet crosses it |
| `leaf_uplink` | Shut one leaf uplink, leaf still up | ECMP reconvergence |
| `node_reload` | `reload` a transit node | `max-metric router-lsa on-startup 60` keeps traffic off it while it converges — no black hole through a booting switch |
| `reload_persistence` | Reload, no deploy afterwards | Fabric returns on its own. Exercises `acm save startup-config` |

### Operational impact — run these early

| Scenario | Injection | Expected |
|---|---|---|
| `deploy_live` | `deploy_evpn.yml` on a switch carrying traffic | Measured loss. **Nobody has measured what full config replacement costs a live switch, and it is the first thing an evaluator will ask** |
| `deploy_nodiff` | Same, on a converged switch (empty diff) | Zero impact — prove it |
| `rollback_timer` | Let confirm-commit lapse deliberately | Switch reverts; traffic recovers |
| `vlan_churn` | Add and remove a VLAN while traffic flows on another | No impact to the unrelated VLAN |

---

## Part 4 — What stays manual

- **Physical link pulls.** Admin-down is the automated proxy and covers
  reconvergence, but it does not exercise carrier-delay, link-flap detection or
  optics failure. Pull a cable at least once per topology and compare the number.
- **Windows endpoint behaviour** — DHCP option handling, broadcast-heavy
  protocols. A spot-check, not coverage.
- **Judging a marginal result.** The automation reports loss and duration; only
  a human decides whether 800 ms on a spine failure is acceptable for this
  network.

---

## Part 5 — Record the numbers

For each failure scenario: what was failed, packets lost, time to recover, time
to restore on the way back. For each deploy scenario: loss during the replace.

These belong in the repo. Convergence behaviour and deploy impact are what
someone evaluating this project needs and cannot get from reading the source,
and they are worth more than another feature.
