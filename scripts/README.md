# `scripts/`

| | |
|---|---|
| `netbox_provision_schema.py` | Creates the NetBox *vocabulary* this automation reads — device roles, tags, VLAN and prefix roles, custom fields. Run once, before anything below. |
| `netbox_multicast_carve.py` | Allocates the underlay multicast prefixes (L2VNI replication, default/data MDT). |
| `netbox_enrol_vrf_security.py` | One-off: enrolled VRF SECURITY (TRM, external RP) into a NetBox branch. Also the shared NetBox client the CLMEL script imports. |
| `netbox_enrol_vrf_clmel.py` | One-off: enrols VRF CLMEL (no TRM) into a NetBox branch. See [Enrolling a new VRF](#enrolling-a-new-vrf). |
| `*_import.csv` | Bulk-import templates. |

---

## CSV import templates

Modelling a fabric through the NetBox UI is one form per device, one per
interface and one per address. These are the same objects as a spreadsheet you
fill in once and paste in three times.

They are **templates, not data**. Every row below the header is a real import —
CSV has no comment syntax, so NetBox will happily create `core1` in Building A
if you leave the examples in. Replace them, don't append to them.

### Import order, and why

NetBox resolves every reference by name at import time, so each file depends on
the one before it:

1. **`device_import.csv`** — needs its site, rack, device type and role to exist.
2. **`interface_import.csv`** — needs the devices.
3. **`ip_address_import.csv`** — needs the devices *and* their interfaces.

Import each at **Devices → Devices → Import** (and the matching page for
interfaces and IP addresses), or the generic `/dcim/devices/import/` URL. Choose
*Direct import* and paste, or upload the file.

### What has to exist first

`netbox_provision_schema.py --apply` creates the device roles (`Fabric Border`,
`Fabric Leaf`) and the tags. It deliberately does **not** create your network,
so before the first import you still need, by hand or by their own CSV import:

- **Manufacturer** and **Device Type** — the `manufacturer` and `device_type`
  columns match a manufacturer *name* and a device type *model*.
- **Site**, and **Location** / **Rack** if you use those columns.
- **Platform**, if you use that column.

A site is required. It is what decides which fabric a device belongs to and
which VLANs reach it — see the [data-model contract](../roles/acm_evpn/README.md).

The example rows include a `dist1-1` with the role `Distribution Switch`, which
is **not** one of the roles the schema script creates — you would have to add
it. That is the point of including it: a switch between a leaf and a border
carries fabric traffic and is checked by `readiness.yml` even though nothing is
rendered for it, so it belongs in NetBox under whatever role you already use.

---

## The columns

### `device_import.csv`

`name`, `role`, `manufacturer`, `device_type`, `platform`, `serial`, `site`,
`location`, `rack`, `position`, `face`, `status`, `tags`

Required by NetBox: **`role`, `manufacturer`, `device_type`, `site`, `status`**.
Everything else is optional to NetBox — but `name` is not optional *here*,
because the Ansible inventory keys on it.

Two that bite:

- **`position` and `face` travel together.** NetBox rejects a device with a rack
  position and no face ("Must specify rack face with rack position"). Leave both
  blank for anything not rack-mounted at a known U.
- **`tags` takes slugs, not names**, comma-separated inside one pair of double
  quotes: `"fabric_spine"`. It is in this template because **spines are
  identified by a tag, not by a device role** — a spine is a control-plane role
  rather than a hardware class, so it composes with whatever device role the
  site already uses. Without this column every spine needs a second visit in the
  UI, which is the clicking this file exists to avoid.

### `interface_import.csv`

`device`, `name`, `type` — NetBox requires all three and this template carries
nothing else.

`type` is a NetBox slug, not a Cisco name: `virtual` for loopbacks and SVIs,
`lag` for port-channels, then the physical media — `1000base-t`,
`10gbase-x-sfpp`, `25gbase-x-sfp28`, `40gbase-x-qsfpp`, `100gbase-x-qsfp28`.

**Every fabric switch needs `Loopback0`**, including distribution switches that
render no fabric configuration. It is the VTEP source, the OSPF router-id and
the PIM register source.

### `ip_address_import.csv`

`address`, `status`, `role`, `device`, `interface`, `is_primary`, `tags`

Required by NetBox: **`address`, `status`**. The rest is the assignment.

- `address` carries its mask: `192.0.2.1/32`, not `192.0.2.1`.
- `is_primary` is `true` / `false`, and only does anything when `device` **and**
  `interface` are both set.
- **The primary IPv4 must be the `Loopback0` address.** The automation asserts
  it on every run: it is what Ansible connects to and what the fabric uses as
  the VTEP source. Exactly one `true` per device.

`role` is optional to NetBox and only one value changes behaviour — but filling
it in is most of why importing beats clicking. Every fabric address in one
column, sorted, is where a `10.67.` among a column of `10.66.` is obvious; in
the UI it is one field on one form you will never see beside the others.

**`anycast` is the value that does something.** NetBox rejects a duplicate
address for every other role, and the underlay anycast RP is deliberately the
same address on every spine — so the role is what permits the model to exist at
all. The two example rows show it: one object per spine, identical address,
each assigned to that spine's own RP loopback.

`tags` is here for the same reason, and the anycast rows are the only ones that
need it. The readiness role finds the RP by querying the
**`underlay_anycast_rp`** tag and then checks the role locally, so an object
with the role and no tag is invisible to it, and one with the tag and the wrong
role is reported rather than silently skipped. Slugs, quoted, as in
`device_import.csv`.

---

## Names, slugs, and the failure that makes no noise

These CSVs match objects by **name** (`Fabric Border`, `Building A`), but the
Ansible inventory groups on the **slug** (`fabric_border` → group
`device_roles_fabric_border`).

Slugs in this project **use underscores**. Ansible's
`TRANSFORM_INVALID_GROUP_CHARS` defaults to `never`, so a hyphen is not
rewritten: a role slugged `fabric-border` produces `device_roles_fabric-border`,
the playbooks look for `device_roles_fabric_border`, and nothing matches.
Nothing renders, and nothing errors.

Let `netbox_provision_schema.py` create the roles and tags and this cannot
happen. If you created any of them by hand, check the slug before importing —
a correct name with a hyphenated slug imports cleanly and then does nothing.

## What these templates do not cover

Deliberately narrow: devices, interfaces, addresses. Still to model, and none of
it is per-device drudgery in the same way:

- **VLANs, VRFs and prefixes**, with the `l2vni` / `l3vni` custom fields that
  enrol them in the fabric. See [Enrolling a new VRF](#enrolling-a-new-vrf).

## Checking the columns against your own NetBox

Field names in NetBox's importers move between major versions. These templates
target **NetBox 4.x**. Your instance is the authority: every import page has a
**Field Options** table listing each accepted column, whether it is required,
and the accepted values — worth a glance before a large import, and the fastest
way to see what else you could add.

---

## Enrolling a new VRF

A tenant VRF is data, not code: nothing in the repository changes when one is
added. What it takes is a set of NetBox objects that must **all** be present —
most omissions do not fail, they render a fabric that looks provisioned and is
not — plus three things outside NetBox that no run can see. The enrolment
scripts (`netbox_enrol_vrf_<name>.py`) write the NetBox half into a branch, so
the whole change can be reviewed and thrown away before anything reaches a
switch.

`netbox_enrol_vrf_clmel.py` is the worked example used below.

### What NetBox must hold

| Object | CLMEL | What goes wrong without it |
|---|---|---|
| VRF with `l3vni` | `CLMEL`, `l3vni` 20006 — next after SECURITY's 20005, range 20001–20254 | No `l3vni`: not a fabric VRF, ignored silently. A reused one: two tenants share route-targets — the script refuses it |
| VRF `mcast_base_group` | `239.6.1.1` | Required as soon as the VRF has a **bridged** segment; the render refuses one without it |
| Multicast carve, global table | `239.6.0.0/16`; `.1.0/24` `l2vni_replication` **Active**; `.2.0/24` `default_mdt` and `.3.0/24` `data_mdt` **Reserved** | The base group must sit in an `l2vni_replication` range in the VRF's own `/16` (index = `l3vni − 20000`) |
| Edge VLANs, role `edge`, in the campus VLAN group | 400 `CLMEL_BRIDGED_CAMPUS`; 401 `CLMEL_ROUTED_CAMPUS` tagged `dag_routed` | Unscoped: enrolled nowhere. Bridged vs routed is decided **only** by `dag_routed` |
| An edge prefix in the VRF, on each edge VLAN | `172.16.1.0/24` → 400, `172.16.2.0/24` → 401 | The prefix is the **only** link from a VLAN to its VRF. The gateway is the first usable address — `.1` |
| `dhcp_servers_ipv4` on each edge prefix | copied from VLAN 100's prefix at run time | No relay: hosts get no address |
| Core VLAN, role `core_vlan` | 499 `VRF_CLMEL_CORE_VLAN` | No L3VNI transit VLAN: no routing between leaves |
| A prefix in the VRF on the core VLAN | `169.254.255.0/30` → 499, the same dummy every VRF uses | **Silent.** The VLAN reads as VRF-less, is dropped, and the fabric bridges while routing nothing |
| Handoff VLANs, role `vrf_l3_handoff_vlan`, each tagged with its border's hostname | 497 → `core1`, 498 → `core2` | Untagged: rendered on **no** border, silently |
| `upstream_asn` on each handoff | 64512 | The render refuses the handoff |
| A `/31` in the VRF on each handoff | `172.16.0.0/31` → 497, `172.16.0.2/31` → 498 | Lowest address is the upstream peer, highest the border: core1 is `.1`, core2 is `.3` |
| VRF supernet (optional) | `172.16.0.0/16` | Nothing renders from it; it is there so IPAM shows the allocation |

Deliberately **absent** for CLMEL: no `multicast_enabled` VLAN, no Active
`default_mdt`, no RP, no `trm_register_source` pool — so no TRM, and
`trm_allocate.yml` has nothing to do. Turning TRM on later is setting
`239.6.2.0/24` (and optionally `.3.0/24`) Active, adding an RP address
(`trm_anycast_rp` or `trm_external_rp`) in the VRF, and tagging the VLANs
`multicast_enabled` — see the
[TRM section of the role README](../roles/acm_evpn/README.md#trm--tenant-routed-multicast).

### What has to exist outside NetBox

None of these are configured, checked or visible to any playbook here.

1. **The upstream router (AS 64512)** needs the mirror image of each handoff:
   VLANs 497 and 498 on its trunks to core1 and core2, VRF CLMEL on its side,
   `172.16.0.0/31` and `172.16.0.2/31` as its addresses, and eBGP neighbours
   `172.16.0.1` and `172.16.0.3` with `remote-as 64513`. The borders run BFD on
   the handoff (`bfd interval 750 min_rx 750 multiplier 3`, `fall-over bfd`) —
   match it.
2. **A default route from upstream, into the VRF.** Each border filters what it
   accepts on a handoff with `WAN-INBOUND-FILTER`, which permits `0.0.0.0/0`
   and nothing else. An upstream that sends specifics and no default leaves
   CLMEL with no way out of the fabric while every session shows Established.
   In the other direction the borders advertise `172.16.1.0/24` and
   `172.16.2.0/24`.
3. **The DHCP server** needs scopes for `172.16.1.0/24` and `172.16.2.0/24`
   (gateways `172.16.1.1` and `172.16.2.1`) and a route back to them. Relay is
   `ip helper-address global`, the same path VLAN 100's clients already use.

### Running it

```bash
scripts/netbox_enrol_vrf_clmel.py           # dry run: prints the plan, writes nothing
scripts/netbox_enrol_vrf_clmel.py --apply   # creates branch 'vrf-clmel-enrolment'
```

The dry run resolves everything against main first and **refuses** — before a
branch exists — if a prerequisite is missing (a role, a tag, the VLAN group,
VLAN 100's DHCP server) or something is in the way (`l3vni` 20006 held by
another VRF, a VID 400/401/497/498/499 already used outside the campus group).
An object that already exists is reconciled, never skipped, and a value that
already disagrees is a `CONFLICT`, not overwritten. Re-running against the same
branch is safe.

Then:

1. **Review the branch in NetBox** — its diff against main.
   About 17 objects, all creations.
2. **Merge it.** generate and deploy read main; nothing in the branch is visible
   to them until then. Deleting an unmerged branch undoes everything.
3. `ansible-playbook generate_evpn.yml -e deploy_target=site_group_evpn_campus`,
   and read each `commit-results/<host>-proposed-diff.cfg`. Expect additions
   only: on the leaves VRF CLMEL, VLANs 400/401/499 and their SVIs, three NVE
   members; on each border VLAN 499, its own handoff, the eBGP session and the
   trunk's allowed list growing by 497 or 498. A removal anywhere means
   something else changed in NetBox — stop and find out what.
4. `ansible-playbook deploy_evpn.yml -e deploy_target=site_group_evpn_campus`.
   The change is additive, so the blast-radius guard should not fire and
   `acm_safety_force` is not needed.
5. **Deploy again.** Done only when every switch reports `Config diff empty`.
