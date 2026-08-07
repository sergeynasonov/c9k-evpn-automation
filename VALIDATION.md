# Dataplane validation plan

The unit suite proves the automation renders what it means to; CI proves it stays
that way. Neither proves a packet crosses the fabric. This is the plan for that —
written to be worked through against a lab and to leave a record behind, since
the numbers here (convergence times, deploy impact) are the ones nobody can
derive from reading the code.

**Status: not yet executed.** Tick items as they pass and record what was
measured, not just that it worked.

---

## Method

**Every failure test is a cycle, not an event.** Fail the component, verify
forwarding continues by the alternate path, restore it, verify forwarding
returns — then repeat against the *other* member of the pair. A test that only
fails one side proves half a design, and the halves are rarely symmetric: a
one-way MSDP mesh looked complete on both switches individually and this fabric
was bitten by it.

**Measure loss, not liveness.** "Traffic still works" a minute later says
nothing. Run continuous traffic and record packets lost per event. The underlay
runs `bfd interval 250 min_rx 250 multiplier 3`, so the target is sub-second; if
a failure costs 40 seconds, BFD is not doing what the config claims.

**Test both topologies.** The physical lab collapses spine and border onto the
same switches (`core1`/`core2` carry the `fabric_spine` tag *and* the
`fabric_border` role); the CML lab keeps them separate. Failure semantics
differ — losing a collapsed node takes an RP, a route reflector and a border
handoff simultaneously — and both shapes exist in the wild.

## Prerequisites

- Endpoints on at least two different leaves, in at least two VRFs, in both
  bridged and routed-only (`dag_routed`) segments.
- One endpoint in an L2-only overlay (no VRF, no SVI).
- A DHCP server reachable in the global table.
- A device outside the fabric, beyond the L3 handoff.
- An external RP for the inter-VRF multicast case.
- Continuous traffic generation with per-flow loss counting.

---

## 1. Address assignment

- [ ] DHCP in a bridged segment inside a VRF.
- [ ] DHCP in a routed-only (`dag_routed`) segment.
- [ ] DHCP in segments belonging to *different* VRFs.
- [ ] **Two clients, same VLAN, different leaves, simultaneously.** *Why:* the
      anycast gateway means `giaddr` is identical on every leaf. The relay
      sources from `Loopback0` and sets the RFC 3527 `link-selection` and
      `server-override` sub-options to disambiguate — which only works if your
      DHCP server honours them. Confirm each reply reaches the *originating*
      leaf.
- [ ] **DHCP behind a downstream switch that inserts option 82.** *Why:*
      `ip dhcp snooping` is enabled on relay VLANs and no interface is rendered
      as trusted anywhere. Directly-attached clients should be fine; an
      upstream-inserted option 82 arriving on an untrusted port is dropped.
- [ ] IPv6 address assignment where `dhcp_servers_ipv6` is set.

## 2. Unicast forwarding

- [ ] Within a bridged segment, same leaf.
- [ ] Within a bridged segment, across leaves (VXLAN bridged).
- [ ] Within a routed segment.
- [ ] Bridged → routed, and routed → bridged.
- [ ] **Between VRFs** — must leave the fabric and return via the L3 handoff.
      Verify the return path takes the same route rather than short-cutting.
- [ ] Any fabric segment → external destination.
- [ ] **External → fabric**, including reaching a tenant subnet and a loopback.
      *Why:* the border advertises an aggregate; confirm no component prefix
      black-holes into the aggregate's discard route.

## 3. Multicast

- [ ] Intra-VRF, source and receiver both in bridged segments — anycast RP.
- [ ] Intra-VRF, source and receiver in routed segments — anycast RP.
- [ ] Intra-VRF, source bridged / receiver routed, and the reverse.
- [ ] Inter-VRF, source in one VRF and receivers in another — external RP.
- [ ] Fabric ↔ external: source outside with receivers inside, and the reverse.
- [ ] IGMP snooping within a bridged segment — receivers on the same leaf.

## 4. Feature paths the forwarding matrix skips

- [ ] **L2-only overlay** (`VLAN 104 L2_IOT`): stretch between two leaves. No
      VRF, no SVI, and dual-homed to *both* borders — a different code path from
      a bridged segment inside a VRF, and the topology most likely to loop.
- [ ] **BUM via multicast**: ARP flood and unknown-unicast flood in a bridged
      segment (`member vni … mcast-group`).
- [ ] **BUM via ingress replication**: the same, in a `dag_routed` segment
      (`member vni … ingress-replication`). *Why:* two entirely different
      replication mechanisms; the multicast tests above exercise TRM, not BUM.
- [ ] **MAC advertisement asymmetry**: `advertise mac disable` is global, with
      bridged DAG segments overriding it to enable. Confirm MAC learning and
      mobility differ between routed-only and bridged segments — that is
      intended, not a fault.
- [ ] **Endpoint mobility**: move a host from leaf1 to leaf2 and measure
      recovery. `anycast-gateway mac auto` puts the same gateway MAC on every
      leaf, so nothing about the move should be visible to the host.
- [ ] **Multi-fabric**: a segment in fabric A to a segment in fabric B — must
      exit and re-enter through both L3 handoffs.
- [ ] **Multi-fabric negative**: MSDP source-active state must not leak between
      fabrics.

## 5. Config decisions never verified in the dataplane

- [ ] **MTU end to end**: 1500-byte payload with DF set, across a VXLAN segment.
      *Why:* validates the 1550 floor (1500 + 50 bytes of outer header). A
      fabric at 1500 forwards small packets perfectly and mangles full-size ones.
- [ ] **ECMP spread**: many flows between one pair of VTEPs, confirm they spread
      across uplinks. *Why:*
      `ip cef load-sharing algorithm include-ports source destination` exists
      solely because every VXLAN flow between a VTEP pair has an identical outer
      header. Without it the whole tunnel pins to one uplink — and the default
      hashes on the outer header, so this is the one line standing between you
      and that.

## 6. Failure and convergence

Apply the cycle from **Method** to each: fail, verify, restore, verify, then
repeat on the partner.

- [ ] **Border A, then border B.** With the L2-only overlay's dual-homed handoff
      active.
- [ ] **Spine A, then spine B** — where spines are separate devices. *Why:* this
      is the anycast RP and MSDP mesh's entire reason for existing, and the part
      of `underlay_readiness` with the most code behind it. Run it with
      multicast flowing.
- [ ] **Collapsed spine+border**, on the physical lab. Losing one node takes the
      RP, a route reflector and a handoff at once.
- [ ] **Border-to-external link failure** — the handoff itself, with the border
      still up.
- [ ] **Underlay link failure in the transit path** between a border and a leaf.
- [ ] **Underlay device failure**: the distribution switch (`transit`). *Why:*
      `acm_evpn` renders nothing for it, but everything crosses it.
- [ ] **Leaf uplink failure** — ECMP reconvergence with the leaf still up.
- [ ] **Full reload of a transit node**: confirm
      `max-metric router-lsa … on-startup 60` really does keep traffic off it
      while it converges, rather than black-holing through a booting switch.
- [ ] **Reload persistence**: after a reload, the fabric returns without a
      deploy. Exercises `acm save startup-config`.

## 7. Operational impact — run this early

- [ ] **Deploy while traffic flows.** Run `deploy_evpn.yml` against a switch
      carrying traffic and measure the loss. *Why:* the tool's whole model is
      full configuration replacement via `acm replace`. Nobody has measured what
      that does to forwarding, and it is the first thing an evaluator will ask.
- [ ] **Deploy with an empty diff**, on a converged switch. Should be zero
      impact; prove it.
- [ ] **Rollback timer expiry under traffic**: let confirm-commit lapse
      deliberately, confirm the switch reverts and traffic recovers.
- [ ] **Add and remove a VLAN while traffic flows** on a different VLAN of the
      same switch.

## 8. Isolation — prove it rather than assume it

- [ ] VRF A cannot reach VRF B inside the fabric. Traffic between them must go
      out and back via the handoff, and there must be no direct path.
- [ ] An L2-only overlay has no gateway responding, on any leaf.
- [ ] A VLAN that never opted in (no role, no `l2vni`) is present on no switch.

## 9. Record the results

For each failure test: what was failed, packets lost, time to recover, time to
restore. For each deploy test: loss during the replace.

These numbers belong in the repo. Convergence behaviour and deploy impact are
what someone evaluating this will want and cannot get from the source, and they
are worth more than another feature.
