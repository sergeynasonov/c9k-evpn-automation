# acm_evpn

Renders and deploys the complete IOS-XE configuration for a campus EVPN/VXLAN
fabric, driven by NetBox, and applies it with `acm replace`.

- **Overlay** (VRFs, SVIs, EVPN instances, NVE, overlay BGP) is **generated** from NetBox.
- **Underlay** (global-table interfaces, OSPF, MSDP, static routes, border GRT BGP)
  is **preserved verbatim** from the device — see [Underlay](#underlay).

## Running it

Scope a run with `deploy_target`, **never `--limit`**:

```bash
ansible-playbook generate_evpn.yml -e deploy_target=site_group_evpn_campus # render + preview, no changes
ansible-playbook deploy_evpn.yml   -e deploy_target=site_group_evpn_campus # one fabric
ansible-playbook deploy_evpn.yml   -e deploy_target=access1-1              # one leaf switch
ansible-playbook deploy_evpn.yml   -e deploy_target=all                    # every fabric, deliberately
```

`generate_evpn.yml` renders each candidate, then asks the switch what it would
change via `acm replace … diff`, writing the answer to
`commit-results/<host>-proposed-diff.cfg`. Nothing is applied.

**`deploy_target` is required.** An unscoped run is refused before anything is
contacted, and the refusal lists the fabrics in the inventory. The host pattern
without it means *every fabric node in the estate*, and nobody intends that by
omitting a flag — `-e deploy_target=all` says it deliberately. A run that names
one leaf still re-renders that leaf's own fabric's spines, so their peer lists
cannot go stale; see below.

`--limit` is refused too, by task 0, and for a different reason. It can drop
spine switches out of the play, leaving their EVPN peer lists stale on the
device. `deploy_target` cannot: both playbooks import
`resolve_deploy_scope.yml`, which resolves the target through the inventory and
adds the spines of exactly the fabrics it names — no others — before the play
that renders forms. A generate therefore previews precisely the switches the
matching deploy would touch. Peer *addresses* come from inventory
(`fabric_peers.py`), so a narrowly scoped run still renders correct peer lists.

`deploy_target` is still a **host pattern**, where `:` is a union, so a target
can select devices this role must not render for. That cannot be fixed in the
pattern itself — Ansible applies `:&` intersections *after* every union
regardless of where they appear — so it is enforced as tasks, before anything
touches a device:

| What the pattern selects | What happens |
|---|---|
| `deploy_target=<site>` also selects that site's **distribution switches**, which have the site like any other device | Their underlay **is** checked — see below. Whether anything is *rendered* depends on the `fabric_spine` tag: an untagged one gets nothing and is named in the output, a tagged one is a fabric node and is rendered as a spine. The render step picks its template as border / spine / *else leaf*, so without this an untagged distribution switch would be handed a full EVPN **leaf** configuration |
| A target that matches **no device at all** — a typo | Refused by `resolve_deploy_scope.yml`, listing the fabrics. Otherwise the render play would have no hosts, which Ansible reports as a warning and exit 0 |
| A hand-written pattern that reaches **another fabric's spine** | Dropped by `01_gather_data.yml` task 15. The playbooks no longer produce one — the scope play adds only the targeted fabrics' spines — so this is a backstop |

---

## NetBox data-model contract

Everything below is read from NetBox. Objects that do not carry the required
marker are **not part of the fabric** and are silently ignored — enrolment is
opt-in throughout.

`scripts/netbox_provision_schema.py` creates every custom field, tag and VLAN /
prefix role named below. Run it once against a new NetBox rather than clicking
twenty objects into the UI and hoping the slugs match.

It creates **schema, not data**. Two things a fabric needs are neither: the ASN
object assigned to its sites, and the anycast-RP IPAddresses assigned to its
spines, are per-fabric data you model yourself. NetBox's **Anycast** IP role is
built in and needs no creating either.

### Fabrics

The fabric's own convention, and the part with no substitute in code:

- **Site = building.** Each switch belongs to one.
- **Site group = campus.** Sites are grouped into it.

**A fabric is a site group — or a site, where that site has no site group.**
This is the boundary of one underlay, one iBGP ASN and one set of EVPN route
reflectors. One resolver decides it for every device
(`roles/underlay_readiness/tasks/fabric_resolve.yml`), so no two lookups can
disagree about where a fabric ends. No extra NetBox data expresses membership:
the inventory plugin already builds `site_group_<slug>` and `sites_<slug>`
groups, so a device's own groups carry its fabric.

**Site groups must not be nested.** A parent group spans buildings that do not
share an underlay, and there is no safe way to guess which level was meant to be
the fabric, so a nested tree is refused outright rather than resolved to one end
of it. Model anything above a fabric as a **region** instead — regions are not
used for scoping.

A device with a fabric role but **no site** belongs to no fabric: it is reported
and skipped, never rendered.

**A site is a fabric because it holds a fabric node.** A fabric node is:

```
  fabric_leaf
  OR fabric_border
  OR (distribution_switch AND tagged fabric_spine)
```

The third branch is a **conjunction**, and that matters more than it looks. The
tag on its own qualifies nothing: a WAN router or a printer carrying
`fabric_spine` is not a fabric node, and this role renders nothing for it. That
was not always true — the rule used to be a flat list of groups, which cannot
express an *and*, so the tag alone was enough and a mis-tagged device would have
had its entire configuration replaced by `acm replace`.

Because it is a conjunction it is **derived once**, in
`underlay_readiness/tasks/fabric_resolve.yml`, and published as `fabric_nodes`
for every call site to read. There is no group list to intersect with, and
adding one back would reintroduce the same defect.

Note the two questions are separate, and the answers differ:

| Question | Answer |
|---|---|
| Which devices does this role **render** for? | Fabric nodes only — the rule above. |
| Which devices does `underlay_readiness` **check**? | `fabric_leaf`, `fabric_border` or `distribution_switch` — the whole transit path, tag or no tag. A distribution switch between a leaf and a border carries fabric traffic, so a wrong MTU or a missing PIM on it breaks the overlay exactly as it would on a leaf; it simply has no overlay configuration of its own. The `Loopback0`, `ip pim register-source` and `ip pim rp-address` checks all apply to it. See the root README. |

A site holding **no** fabric node is not a fabric. A NetBox holds plenty of
ordinary sites — WAN kit, servers, campuses this automation does not manage —
and they are not candidate fabrics waiting to be configured, so they are passed
over in silence rather than asked for an ASN and an anycast RP they will never
have.

#### What a fabric owns in NetBox

**NetBox is the only source for all of these. There is no global fallback** —
a single global value cannot describe more than one fabric, which is the whole
point. A fabric missing any of them fails the run and names what is missing.

| Value | Where it lives | If NetBox does not carry it |
|---|---|---|
| **iBGP ASN** | One NetBox **ASN object on every site** in the fabric — the same one on all of them. Exactly one per site is definitional, not policy: every EVPN session in the fabric is iBGP inside it, so a site carrying two, or two sites disagreeing, is refused rather than resolved by picking a winner. Needs no GraphQL query — the inventory plugin fetches sites with the full serializer, so it already rides along in each device's `sites` hostvar. | The run is refused, naming the fabric. |
| **Underlay anycast RP** | One **IPAddress per spine**, all the same address, each with NetBox's built-in **Anycast** role — which is what permits the duplicate — each tagged `underlay_anycast_rp`, and each assigned to that spine's RP loopback. The tag selects; the role permits. Scoping is free: IPAddress → Interface → Device → its fabric. | The run is refused — for a fabric with none at all, and for any single spine missing its own object, since the interface name comes from it. |
| **Upstream ASN** | `upstream_asn` on each `vrf_l3_handoff_vlan` — per handoff, since a border can face a different upstream per tenant. | The render is refused, naming every offending VLAN. Rendering `remote-as` with nothing after it is a parser error on the switch, after the candidate has reached every border. |
| **A spine and a border** | At least one device tagged `fabric_spine`, and at least one with the `fabric_border` role. Not a separate object to create — a property of the devices already modelled. | The run is refused, naming the fabric and what it does have. No spine means no anycast RP and no route reflector; no border means no path out of the fabric. |

Because the RP interface comes from the same object as the address, the two can
no longer drift apart — which is what two independent global values allowed. Within a fabric every spine's RP address must be **identical**;
differing addresses configure cleanly on every switch and then silently fail to
forward.

#### What is scoped to the fabric

| Scoped to the fabric | What an unscoped version would do |
|---|---|
| **EVPN overlay peers** — a spine reflects only for clients in its own fabric, a client peers only with its own fabric's spines | Too short a peer list renders a valid `router bgp` that `acm replace` applies as a session teardown |
| **The border-to-border iBGP mesh** (`border_config.j2`) | Render a neighbour in another fabric: a device in an ASN it is not in, reachable over an underlay this switch has no route into |
| **The MSDP mesh check** (`underlay_readiness`) | Demand a session across two underlays, reporting every correctly-built spine as unready |
| **The border's edge-subnet `network` statements** | Advertise another fabric's prefixes. Inert while they are unreachable — `network` needs a matching route in the RIB — so the visible cost is dead configuration that grows with every fabric. It becomes a real advertisement, competing with that fabric's own border, the moment a shared upstream or a DCI makes them resolvable |
| **VNI uniqueness** and **multicast group collisions** | See below |

Note what is scoped to the fabric but **not** to the site: a border is the exit
for its *whole* fabric, so it advertises the edge subnets of every building in
it, not only its own. That is why `fabric_edge_subnets` ignores the per-host
site filter that `all_edge_vlans` obeys.

#### Uniqueness, and what two fabrics may share

Two fabrics are fully independent: they may reuse the same VRFs, the same
L3VNIs, the same VLAN IDs and the same multicast groups, because they never
exchange them. **VNI uniqueness and multicast group ownership are therefore
scoped to one fabric**, and both halves of that matter:

- *Within* a fabric nothing is relaxed. Validation runs against every VRF and
  VLAN in the NetBox dataset rather than only those in scope for the rendering
  host, so a clash between two buildings of one campus fails **every** host in
  it — including hosts that see only half of it. Two segments in different
  buildings replicating to the same underlay group is exactly that case: no
  single switch sees both, while on the wire each receives the other's flooded
  traffic.
- *Across* fabrics, reuse is the design. Carrying the same VRF at the same
  `l3vni` everywhere is what keeps the RD, the route-targets and the multicast
  index consistent between them. And since a run is always scoped to one
  fabric, a clash modelled in another building must not block this one's
  deploy — it fails the run that actually concerns it.

### Devices

**Slugs must match exactly.** A device whose role is spelled differently is not
in the group, produces no configuration, and raises no error — the run simply
does not mention it. This is the single most common way to get a fabric that
looks provisioned and is not.

| What | NetBox object | Slug | Effect |
|---|---|---|---|
| Border | Device role | `fabric_border` | Renders the handoff SVIs, the eBGP handoff, the border-to-border iBGP mesh, and a `network` statement per edge subnet — all scoped to its own fabric. Allocated first by `trm_allocate.yml`. |
| Leaf | Device role | `fabric_leaf` | Renders tenant SVIs, EVPN instances and the NVE interface. |
| Spine | **Tag**, on a `distribution_switch` | `fabric_spine` | Route reflector: EVPN and MVPN clients, anycast RP. The tag is only half of it — see below. |

A device matching none of these is **not a fabric node**. It still belongs to a
fabric (it has a site) but nothing is rendered or deployed for it, even when a
host pattern selects it — see [Running it](#running-it).

**Every fabric needs at least one of each.** A fabric with no `fabric_spine`
tag has no anycast RP to replicate BUM traffic from; one with no `fabric_border`
has no path out of itself. Both are refused before any device is contacted —
see [What it refuses](#what-it-refuses).

**Underscores, not hyphens.** Ansible's `TRANSFORM_INVALID_GROUP_CHARS`
defaults to `never`, so the inventory plugin does not rewrite a hyphen: a slug
`fabric-border` yields the group `device_roles_fabric-border`, while the
playbooks look for `device_roles_fabric_border`. The two never meet, and the
symptom is silence. Every slug in this contract uses underscores.

Note the asymmetry: border and leaf switches are identified by **device roles**,
spine switches by a **tag on top of the `distribution_switch` role**. A spine is
a role a device plays in the control plane rather than a distinct hardware class
— often literally the same platform as a distribution switch — so it is
expressed as a tag composing with the role the site's own conventions already
use. It is deliberate, and it is not guessable, hence this table.

**The tag composes with `distribution_switch`, and only with it.** Tagging any
other role does not create a fabric node. A campus spine that is also its border
is a fabric node through the *border role*, not through the tag; the tag still
matters there, because it is what selects the spine-only underlay checks
(anycast RP address carried, `ip msdp originator-id`, the MSDP mesh) in
`underlay_readiness`. One tag, two jobs, and they are independent.

Every managed device additionally needs:

| Requirement | Why |
|---|---|
| Status **Active** | `netbox_inv.yml` filters on it; anything else is invisible to the inventory. |
| **Primary IPv4 = the `Loopback0`/VTEP address** | EVPN peers are addressed from each device's primary IP, so a peer outside the current play still gets a correct address. Asserted on every host — see [Safety guards](#safety-guards). |
| A **site** | Decides which fabric the device is in, and drives VLAN scope — both below. A fabric device with no site belongs to no fabric: it is reported and skipped, and nothing is rendered or deployed for it. |

### Sites, site groups and VLAN scope

**Scope is required.** A VLAN with no site and no VLAN group scope is enrolled
**nowhere** — enrolment is opt-in throughout this project, and scope is how a
VLAN says where it belongs. One that has not said is not a decision to honour.

This is **silent, never fatal**. A NetBox legitimately holds hundreds of VLANs
that are not fabric segments — other platforms, other teams, decommissioned
ranges, documentation-only IPAM entries — and neither failing the render nor
listing them on every run is useful. A VLAN that has not opted in is not this
automation's business.

| VLAN in NetBox | Renders on |
|---|---|
| **No site and no group scope** | **Nowhere** — silently ignored |
| `site` set | Leaf switches in that site (building) |
| Group whose scope is a **site group** | Leaf switches whose site is in that campus |
| Group whose scope is a **site** | Leaf switches in that site |

So a tenant VLAN that exists in one building gets a site; one stretched across
the campus gets a group scoped to the site group. There is no way to say
"everywhere" by omission — scope the group at the widest site group you mean.

Leaf switches, not all switches: `leaf_config.j2` is the only template that renders
edge VLANs. Border and spine switches never carry them, except an L2-only
overlay explicitly tagged for that border switch.

Scope decides **where** a VLAN is rendered — and, through the site it lands in,
which [fabric](#fabrics) it belongs to. Its **role**, below, decides **what** is
rendered. A VLAN in scope with no recognised role produces nothing, and nothing
is said about it — enrolment is opt-in, so not opting in is the normal case, not
a finding.

### VLAN names

Rendered verbatim as `vlan <id>` / `name <name>`, so they must be names IOS-XE
can accept. NetBox enforces neither rule, and the disagreement is silent until a
switch rejects the candidate.

| Rule | Why |
|---|---|
| **No whitespace** | `name L2 only overlay for IoT` is parsed as `name L2`, and the remainder is rejected. |
| **At most 30 characters** | IOS-XE accepts 32; this leaves a little headroom rather than sitting on the ceiling. |

Both are checked during the render, so `generate_evpn.yml` fails naming the
VLAN — before a candidate reaches a switch and fails at
`acm replace … validate` with a parser error that mentions neither NetBox nor
the VLAN. Every offending name is reported in one run, so a bulk rename is not
a guessing game.

### VRF custom fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `l3vni` | Integer | no | **Enrols the VRF in the fabric.** Blank ⇒ not a fabric VRF (WAN, out-of-band, other platforms) and ignored entirely. Drives the L3VNI, the RD (`<loopback>:<l3vni>`) and all route-targets (`<asn>:<l3vni>`). Allocate from **20001–20254**. The floor keeps it clear of the derived L2VNI range; the ceiling is set by the multicast index (`l3vni - 20000`), which must fit in an octet. A Type-1 RD would independently cap it at 65535, since its second field is 16 bits. |
| `mcast_base_group` | Text (IPv4) | no | Underlay multicast group used by every **bridged** segment in this VRF. Required if the VRF has any bridged segment; see [Multicast](#multicast). |

### VLAN custom fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `l2vni` | Integer | **conditional** | Two meanings, decided by whether the VLAN has a VRF. **In a VRF:** an override for the derived L2VNI (`evpn_l2vni_base + vid`, 10001–14094) — leave blank unless pinning a brownfield VNI. **With no VRF:** the marker that **enrols the VLAN as an L2-only overlay**; see below. |
| `mcast_group` | Text (IPv4) | **conditional** | Overrides the VRF's `mcast_base_group` for this VLAN alone — an escape hatch, most VLANs in a VRF inherit. **Required on an L2-only overlay**, which has no VRF to inherit from. |
| `upstream_asn` | Integer | **yes, on a `vrf_l3_handoff_vlan`** | The eBGP peer ASN for that handoff, rendered as its `remote-as`. Set it per handoff: a border can face a different upstream per tenant, and two fabrics almost certainly do. There is no global to fall back on, so a handoff without one **fails the render**, naming the VLAN — otherwise it renders `remote-as` with nothing after it, which is a parser error on the switch after the candidate has reached every border. A value that is present but not a valid ASN (1–4294967295) fails the same way. Ignored on any other VLAN role. |

### VLAN roles

Overlay enrolment is **opt-in**: a VLAN is configured only if its role is one of
these. A VLAN with no role produces nothing and is listed in the run output.

| Role | Renders as |
|---|---|
| `edge` | An EVPN segment: `vlan`, `vlan configuration`, `l2vpn evpn instance`, NVE member, and (if in a VRF with a prefix) an anycast-gateway SVI. |
| `core_vlan` | The VRF's L3VNI transit VLAN + `ip unnumbered` SVI. Needs a placeholder prefix to reach its VRF at all — see below. |
| `vrf_l3_handoff_vlan` | A border handoff SVI + eBGP neighbour. Lowest usable address ⇒ remote peer, highest ⇒ local SVI. |

### How a VLAN joins a VRF

**Through a prefix, and only through a prefix.** NetBox has no VRF field on a
VLAN object. The link is: *prefix assigned to the VLAN* → *that prefix's `vrf`*.
Nothing else is read — a VLAN with no prefix has no VRF, whatever its role or
name.

Usually that is invisible, because an `edge` VLAN needs a prefix anyway for its
gateway. It bites on **`core_vlan`**, whose SVI is `ip unnumbered` and therefore
appears to need no addressing at all. Give it a **placeholder prefix in the
VRF** regardless. Without one it is read as a VRF-less VLAN, and — having no
`l2vni` either — it is dropped as unenrolled. Nothing is rendered for it, no
error is raised, and the fabric bridges perfectly while routing nothing.

The placeholder's value is never used: the SVI is unnumbered, and `core_vlan` is
excluded from the border's edge-subnet advertisement. Choose something clearly
not a host subnet, outside the edge ranges — a `/31` is enough.

> This is the opposite of the rule for **underlay multicast** prefixes, which
> stay in the global table and record their VRF in the description. That rule is
> about groups the tenant data plane never routes to; this one is about a VLAN
> that must land in a tenant VRF. Do not carry either across to the other.

### VLAN tags

| Tag | Effect |
|---|---|
| `dag_routed` | **Routed-only** distributed anycast gateway: `flooding-suppression address-resolution disable` on the EVI, `ip proxy-arp` + `ip local-proxy-arp` on the SVI, and **ingress replication** (no multicast group needed). Untagged ⇒ **bridged** DAG using multicast replication. |
| `<hostname>` | On a `vrf_l3_handoff_vlan`, selects which border switch renders it. On an **L2-only overlay**, opts that border switch in — see below. |

### L2-only overlays

A tenant segment that is **bridged across the fabric with no gateway**: no VRF,
no SVI, no anycast gateway. It is a VLAN with role `edge` and **no VRF**, and
`l2vni` is what separates a deliberate L2 overlay from a VLAN whose VRF was
simply never set.

| Requirement | Why |
|---|---|
| `l2vni` set | The enrolment marker. Without it the VLAN is not an overlay and the render **fails**, naming it — it used to render on a derived VNI, so a VLAN nobody had finished modelling reached a switch looking complete. |
| `mcast_group` set | There is no VRF base group to inherit. Absent, the segment would silently fall back to head-end replication — a different forwarding design arrived at by omission, so it is an error instead. |
| **No prefix** | It is bridged and renders no SVI, so an address would be silently ignored while an operator looked for a gateway that does not exist. Assigning one **fails** the render. |

Scope works exactly as it does for any other VLAN — site or site group. There
is no unscoped option: an unscoped VLAN is enrolled **nowhere**, here as
everywhere else.

**Border switches are opt-in.** A border switch has no local edge ports, so an
L2 segment belongs there only when it must be bridged out of the fabric. Tag the
VLAN with that border switch's hostname — the same convention a
`vrf_l3_handoff_vlan` uses. Untagged, border switches ignore it entirely while
every in-scope leaf switch still carries it.

**One border is recommended, and nothing enforces it.** Tagging two bridges the
segment out of the fabric twice, which puts the external L2 domain in parallel
with the fabric's own VXLAN bridging between those borders. That is the
*possibility* of a forwarding loop rather than a loop: spanning tree, an MLAG or
stacked pair, or a single upstream switch all break it, and a dual-homed L2
handoff built on one of those is sound.

The reason to prefer one is narrower than "it loops". Whatever breaks the loop
is configuration **this project does not own** — outside the fabric, absent from
NetBox, not rendered here and not checked by `underlay_readiness`. One border
keeps the guarantee inside the boundary this automation controls; two moves it
outside, to something no run can see or assert. That is a legitimate design an
operator may choose deliberately, so the render allows it and says nothing.

An L3 handoff is the opposite case, and the contrast is the point: two borders
are exactly what you want there, because those are routed and BGP arbitrates the
duplication inside the fabric's own configuration.

Renders as:

```
vlan 900
 name GUEST_L2
!
l2vpn evpn instance 900 vlan-based
 encapsulation vxlan
 replication-type static
 advertise mac enable
!
vlan configuration 900
 member evpn-instance 900 vni 10900
!
interface nve1
 member vni 10900 mcast-group 239.0.1.1
```

`advertise mac enable` is present because the segment **is** bridged — contrast
a `dag_routed` VLAN, which is routed-only and must not advertise MAC routes.

### Prefix custom fields

| Field | Type | Meaning |
|---|---|---|
| `dhcp_servers_ipv4` / `dhcp_servers_ipv6` | Object list → IP addresses | Relay destinations rendered as `ip helper-address global` / `ipv6 dhcp relay destination global`. |

---

## Underlay

The underlay is deliberately **not** modelled in NetBox. It changes only when
switches are added or removed, and getting it wrong costs the management plane —
so the device stays its own source of truth and its underlay is carried forward
verbatim into each candidate config. Rules live in
`filter_plugins/ios_underlay.py`.

An interface is underlay when it is in the **global routing table** (no
`vrf forwarding`) **and** carries an address (`ip address`, `ipv6 address` or
`ip unnumbered`). Tenant SVIs are excluded automatically, since they all sit in
a VRF and are rendered from NetBox instead.

Also preserved: the `vlan <id>` definition backing any underlay SVI (without it
the VLAN does not exist in the VLAN database and the SVI never comes up);
`router ospf` / `router ospfv3`; global statements (`ip msdp …`, `ip route …`);
the global-routing-table half of `router bgp` on border switches; and the
definitions of
any route-map, prefix-list or ACL that preserved config references, resolved
transitively so nothing is left dangling.

Global multicast (`ip multicast-routing`, `ip pim ssm default`,
`ip pim rp-address`, `ip pim register-source`) is *not* extracted — it is static
and rendered unconditionally by `common_base.j2`, so extracting it too would
emit it twice.

Nothing is special-cased: the anycast RP loopback and the MSDP mesh are
ordinary underlay, preserved like everything else in the global table.

---

## Multicast

### Carve vs allocation

Two distinct things, deliberately kept in different places:

| | What it says | Where it lives |
|---|---|---|
| **The carve** | "this *range* serves this *function*" | IPAM prefix + role, in the **global table** |
| **The allocation** | "this *VRF* uses this *value*" | Custom field on the VRF |

The validator joins them: an allocated value must fall **inside** a range roled
for its function, and **outside** every reserved range. Re-carving is therefore a
NetBox data change, not a code edit.

Prefixes stay in the **global table**. `Prefix.vrf` in NetBox means "this prefix
exists in that VRF's routing table", which is false for an underlay multicast
group — the group lives in the underlay and the tenant's data plane never routes
to it. Assigning one to a VRF also breaks NetBox's prefix hierarchy, since VRFs
are separate address spaces. Record the VRF association in the prefix
*description* instead; the machine-readable link is the VRF custom field.

### Address plan

`239.0.0.0/8` (administratively scoped, RFC 2365), **VRF-major** — one `/16` per
VRF so that a single `show ip mroute | include 239.<index>.` returns everything
for that VRF:

```
239.<index>.0.0/16      Container   "VRF <NAME> — multicast"      (no role)
├── 239.<index>.1.0/24  role: l2vni_replication   L2VNI BUM replication
├── 239.<index>.2.0/24  role: default_mdt         MVPN/TRM default MDT
├── 239.<index>.3.0/24  role: data_mdt            MVPN/TRM data MDT
└── 239.<index>.4.0/22  status: Reserved          future (L2 TRM, IPv6, DCI)
```

`<index>` is **derived**, not stored: `l3vni - evpn_l3vni_base` (default 20000),
so `l3vni 20003` owns `239.3.0.0/16`. One number therefore identifies a VRF
everywhere — RD and route-targets `:20003`, multicast block `239.3.x`.

**The carve is shared by every fabric and needs no per-fabric run.** It is one
address plan inside `239.0.0.0/8`, and the index is derived from the L3VNI — so
two fabrics carrying the same VRF at the same `l3vni` land on the same prefixes,
which is the intent rather than a clash. Separate underlays never exchange these
groups, so there is nothing to keep apart, and re-running
`netbox_multicast_carve.py` for a second fabric is idempotent. Where two fabrics
reuse an L3VNI for *different* VRFs, give the index both names
(`--vrf CORP=3 --vrf GUEST=3`); the description then lists both, and the
machine-readable link remains the index itself.

Group **collisions** are policed per fabric, not per switch: two segments in
different buildings of one campus that replicate to the same group would flood
BUM traffic into each other's tree, and no single switch sees both. Across
fabrics the same group is fine.

Valid indexes are 1–254, which is what bounds `l3vni` to 20001–20254. Do not
derive the index from NetBox's VRF `id`: it is a surrogate key that changes on
delete-and-recreate and climbs past 254 as objects churn.

**`239.0.0.0/16` (index 0) is the L2-only block, and it is FLAT.** An L2-only
overlay belongs to no VRF, so it has no per-VRF `/16` to sit in; index 0 was
already left unused by the per-VRF carve, which is exactly where it fits.

It is deliberately *not* subdivided the way a VRF block is. That subdivision
exists to separate L2VNI replication from the MDT ranges, and MDTs are per-VRF
TRM constructs — at index 0 they could never be used, so carving them would
reserve address space against a case that cannot arise and confine L2-only
groups to a `/24` for no reason. The whole `/16` carries the same
`l2vni_replication` role used per-VRF: an L2-only segment's BUM group is an
L2VNI replication group like any other. Create it with
`netbox_multicast_carve.py --l2-only`. `239.0.0.0/16` and `239.255.0.0/16` are left unused
(the latter is IPv4 Local Scope).

Conventional allocations within a VRF's block:

| Value | Held in | Example (l3vni 20003 → index 3) |
|---|---|---|
| Base replication group | VRF `mcast_base_group` | `239.3.1.1` |
| Per-VLAN overrides | VLAN `mcast_group` | `239.3.1.2`, `.3`, … |
| Default MDT | first address of the **Active** `default_mdt` prefix | `239.3.2.1` |
| Data MDT range | the **Active** `data_mdt` prefix | `239.3.3.0/24` |

### Replication modes

| Case | EVI `replication-type` | NVE member |
|---|---|---|
| `dag_routed` | `ingress` | `ingress-replication` |
| bridged, group available | `static` | `mcast-group <group>` |
| bridged, in a VRF with **no** group | — | **render fails** |
| L2-only, no VRF to inherit from | `ingress` | `ingress-replication` |

Both columns are driven by one `replication_mode` value computed in
`filter_plugins/netbox_evpn_data.py`, so they cannot disagree.

---

## TRM — tenant routed multicast

Optional per VRF, and enabled by **data rather than a flag**.

### What enables it

A VRF is multicast-enabled when a prefix with role `default_mdt` inside its own
`239.<index>.0.0/16` has status **Active**. The carve creates that range for
every VRF as **Reserved** — a carve is a plan, and status is what turns a plan
into a decision. Exactly one Active `default_mdt` per VRF; two fails the render.

`data_mdt` works the same way and is optional. Both must be IPv4: the MDT is an
underlay construct and stays IPv4 even once the overlay carries IPv6.

MDT prefixes live in the **global table**, associated by index, not assigned to
the NetBox VRF — see the note in `scripts/netbox_multicast_carve.py` for why.

### RP mode, also derived

Tagged IPAM objects **inside the tenant VRF**:

| Object | Tag | Count | Effect |
|---|---|---|---|
| IP address | `trm_anycast_rp` | 1 | anycast RP; ASM stays inside the fabric |
| IP address | `trm_external_rp` | 1..n | RP outside the fabric |
| Prefix | `trm_register_source` | 1, external only | allocation pool |
| IP address | `trm_register_source` | 1 per VTEP | that switch's register source |

Both RP tags on one VRF is a data error. **Neither is legal** — `ip pim vrf X ssm
default` renders regardless, so SSM (232/8) works with no RP at all; only ASM
needs one.

**No MSDP is configured, deliberately.** The usual reason anycast RP needs it is
that each RP instance only learns sources that registered to it — in TRM, MVPN
Source-Active routes carry that over the EVPN control plane instead, which is
what `mdt overlay use-bgp` enables. If multicast must leave the fabric, use
external RP mode rather than bridging anycast RPs with MSDP.

### Register sources are allocated, not hand-entered

External RP mode needs a **unique** address per VTEP. Those are allocated by
`trm_allocate.yml` from the pool — border switches first, then leaf switches —
and written back
to NetBox, so the allocation is durable rather than re-derived.

Ownership is the **interface the address is assigned to**: the allocator creates
`Loopback<l3vni>` on the device in NetBox and attaches the address to it, which
is how NetBox natively binds an address to a device. Tags stay classification
(*what* it is); the interface is identity (*whose* it is).

This is the only interface this role models, and only because NetBox allocates
the address so NetBox must know what holds it. `acm_evpn` itself stays
read-only: it refuses to render a VTEP that needs a register source and has
none, naming the playbook.

### VLANs

Tag an `edge` VLAN `multicast_enabled` and its SVI gets `ip pim sparse-mode` and
`ip igmp version 3`. The VLAN's VRF **must** be multicast-enabled — tagging one
in a VRF with no Active `default_mdt` fails the render rather than producing
silently non-functional multicast.

Independent of `dag_routed`: a routed-only DAG segment still has hosts that can
be receivers, and its L3 multicast rides the MDT rather than the L2VNI's ingress
replication, so the two do not interact.

Handoff SVIs (`vrf_l3_handoff_vlan`) get PIM automatically when their VRF has
any `multicast_enabled` edge VLAN **anywhere in the fabric** — a border switch has no
local edge VLANs, so a per-device test would never fire.

### What renders

```
vrf definition CORP
 address-family ipv4
  mdt auto-discovery vxlan
  mdt default vxlan 239.3.2.1
  mdt data vxlan 239.3.3.0 0.0.0.255      # only with an Active data_mdt
  mdt overlay use-bgp
!
ip multicast-routing vrf CORP             # note the trailing space IOS-XE keeps
ip pim vrf CORP ssm default
!
interface Loopback20003                   # Loopback<l3vni>, leaf and border switches
 description Fabric Anycast RP for VRF CORP
 vrf forwarding CORP
 ip address 10.67.118.31 255.255.255.255
 ip pim sparse-mode
!
ip pim vrf CORP rp-address 10.67.118.31
```

External mode instead renders `description PIM Register Source for VRF <X>`, the
switch's own allocated address, `ip pim vrf X register-source Loopback<l3vni>`
and one `rp-address` per external RP.

BGP gains `address-family ipv4 mvpn`, mirroring `l2vpn evpn` including
`route-reflector-client` on spine switches, **only** on devices carrying at least one
multicast-enabled VRF.

### Turning it off

Un-marking a VRF prunes its RP loopback, `ip multicast-routing vrf X` and every
`ip pim vrf X` line. Without that, preserve-by-default would carry them forward
for ever and disabling TRM would be a one-way door.


## Rolling back

Two independent undo mechanisms, at different timescales.

### During a deploy — automatic, and the reason for the IOS-XE floor

`acm replace … timeout <n>` applies the candidate under a rollback timer. If the
change breaks the path the automation arrives on, `acm confirm-commit` never
reaches the switch and it restores itself when the timer expires. Nothing has to
be reachable for that to work, which is the whole point.

### Afterwards — manual, from the switch itself

Every successful deploy leaves a checkpoint on the device, so a switch can be
returned to its last automation-verified state with **no control node, no
NetBox, and no access to this repository**:

```
switch# acm rollback ?
  <1-2>
Sno  Event   Result   Username   Time(M/D H:M:S)   Target-config
1    SAVE    Success  admin      08/05 11:39:30    flash:acm-evpn/0805-113928.cfg
2    SAVE    Success  admin      08/05 10:14:49    flash:acm2.cfg

switch# acm rollback 1
```

Pick the entry by its `Sno`, not by filename. Checkpoints written by this role
live in `flash:acm-evpn/` and are named `<MMDD-HHMMSS>.cfg` in the fabric's local
time, so the filename agrees with the `Time` column beside it. Anything in the
flash root — `acm1.cfg` above — was made by hand and is not the automation's.

**Every switch in one deploy shares the same filename**, so rolling a group back
to the same point is a matter of matching that name rather than reconciling 500
timestamps.

Three things worth knowing before relying on it:

- The checkpoint is taken **after** the change is applied, confirmed and saved,
  so it captures what the automation just verified — the state you want back
  after a later hand-edit goes wrong, not the state before this deploy.
- **Filenames are never reused.** `acm save` refuses to overwrite, and deleting
  the file first leaves the old rollback entry naming a path whose contents have
  changed — it would silently restore the newer configuration.
- ACM keeps roughly 50 checkpoints and rotates the oldest out, **without
  deleting the evicted files**. The directory is worth an occasional look.

A rollback puts the switch back to a configuration NetBox no longer describes.
The next `deploy_evpn.yml` will move it forward again, so treat a rollback as
buying time to fix the data, not as a durable state.

Set `acm_checkpoint: false` to disable, or point `acm_checkpoint_dir` elsewhere.

## What it refuses

Two refusals stop the whole run rather than one device, both decided from
inventory before anything is contacted. They are data faults — the fabric is
described wrongly in NetBox — not a switch being unfit, and a render built on
either would be wrong for every member.

| Refusal | Why it is fabric-wide |
|---|---|
| A fabric with no `fabric_spine` **and/or** no `fabric_border` | No spine means no anycast RP to replicate BUM traffic from and no route reflector to peer with; no border means no path out of the fabric. Reported once per fabric, naming what it does have, rather than once per member — a thirty-switch fabric would otherwise produce thirty copies of one NetBox mistake. |
| A site tree that does not partition into fabrics | Nested site groups make every peer list suspect, not only the ambiguous device's. |

Both are scoped to the fabrics a run actually reaches, so a broken fabric
elsewhere in the estate never blocks a run aimed at a healthy one.

## Safety guards

Because `acm replace` deletes anything absent from the candidate config, several
checks run before a switch is touched:

| Guard | Behaviour |
|---|---|
| VNI validation | Fails on out-of-range, duplicate, or L2/L3-colliding VNIs. Scoped to the rendering host's own fabric — see [Sites, site groups and VLAN scope](#sites-site-groups-and-vlan-scope). |
| Multicast validation | Fails on a missing, malformed, or duplicated group, one outside the carve, or one outside the owning VRF's own `/16`. |
| Anycast RP assertions | The RP loopback must match the fabric's anycast RP, every spine switch must carry it, and every spine switch in a fabric must carry the **same** address — differing addresses configure cleanly and then silently fail to forward. |
| Peer addressing assertion | `Loopback0` must equal the device's NetBox primary IP. |
| Fabric partition | Refuses the whole run if the NetBox site tree does not partition into fabrics — today, if any device sits under nested site groups. Fabric-wide: an ambiguous tree makes every peer list suspect, not just the ambiguous device's. |
| Incomplete fabric | Refuses a run reaching a fabric with no `fabric_spine` tag or no `fabric_border`. Without a spine there is no route reflector, so the render is a `router bgp` with no overlay neighbours — which `acm replace` applies as a teardown of every EVPN session on the switch; without a border the fabric has no path out of itself. Refused in the resolver, before any device is contacted, and named by fabric rather than once per stranded member. **Scoped to the fabrics the run actually reaches**, so an incomplete lab fabric cannot block a campus deploy. |
| Route reflector assertion | The same spine requirement, per device, on the render path. Superseded by the refusal above and kept as a second line of defence: it can no longer fire from a missing tag. |
| Blast-radius guard | Blocks a deploy removing >`acm_safety_max_removal_pct` of any tracked object class, and **any** underlay removal (zero tolerance). **Strictly** greater, so a single removal blocks only when it was the last member of its class — the reasoning is in `defaults/main.yml`. Override with `-e acm_safety_force=true`. Generate runs warn instead of failing. |

## Tests

```bash
venv/bin/python -m unittest discover -s roles/acm_evpn/tests/unit
```

Stdlib `unittest` only — no pytest and no live NetBox.

Most of it is plain Python against the filter plugins, and
`test_leaf_templates.py` renders the shipped `.j2` files with plain Jinja2.

The rest of the logic lives in **Jinja inside task files**, where
`--syntax-check` evaluates nothing at all. Those tests run the real task files
under `ansible-playbook` against static inventories in `tests/fixtures/`:

| Test | Covers |
|---|---|
| `test_fabric_resolve.py` | Fabric resolution, per-fabric peer selection, the ASN, and which devices a scoped run may touch |
| `test_inventory_config.py` | `netbox_inv.yml`'s reserved-name renames, driven through the inventory plugin's own code |
| `../../underlay_readiness/tests/unit/test_msdp_scope.py` | The MSDP mesh check and its fabric scoping |
| `../../underlay_readiness/tests/unit/test_anycast_rp.py` | Per-fabric anycast RP resolution |

In every case the tasks under test are lifted out of the shipped file at run
time rather than copied, so a fixture cannot drift from the code. They skip
themselves if `ansible-playbook` is not on `PATH`. See CONTRIBUTING.md for the
recipe.
