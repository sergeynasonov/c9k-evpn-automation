# `scripts/`

| | |
|---|---|
| `netbox_provision_schema.py` | Creates the NetBox *vocabulary* this automation reads — device roles, tags, VLAN and prefix roles, custom fields. Run once, before anything below. |
| `netbox_multicast_carve.py` | Allocates the underlay multicast prefixes (L2VNI replication, default/data MDT). |
| `*_import.csv` | Bulk-import templates. The rest of this file. |

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

`address`, `status`, `device`, `interface`, `is_primary`

Required by NetBox: **`address`, `status`**. The rest is the assignment.

- `address` carries its mask: `192.0.2.1/32`, not `192.0.2.1`.
- `is_primary` is `true` / `false`, and only does anything when `device` **and**
  `interface` are both set.
- **The primary IPv4 must be the `Loopback0` address.** The automation asserts
  it on every run: it is what Ansible connects to and what the fabric uses as
  the VTEP source. Exactly one `true` per device.

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
  enrol them in the fabric.
- **The underlay anycast RP** — one IPAddress per spine, all the same address,
  each on that spine's RP loopback. Those rows need two more columns,
  `role` (`anycast`) and `tags` (`"underlay_anycast_rp"`); the address is a
  legal duplicate only because of the role.

## Checking the columns against your own NetBox

Field names in NetBox's importers move between major versions. These templates
target **NetBox 4.x**. Your instance is the authority: every import page has a
**Field Options** table listing each accepted column, whether it is required,
and the accepted values — worth a glance before a large import, and the fastest
way to see what else you could add.
