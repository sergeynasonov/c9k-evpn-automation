#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Create the custom fields, tags and roles acm_evpn reads from NetBox.

This is the schema, not the data. It creates the vocabulary the automation keys
on -- every slug in the "NetBox data-model contract" section of
roles/acm_evpn/README.md -- so a new NetBox can be brought up to the contract in
one command instead of twenty UI forms where a single mistyped slug produces a
switch that renders nothing and reports no error.

It deliberately does NOT create sites, site groups, devices, VLANs, VRFs or
prefixes. Those are your network, and a script inventing them would be guessing.
For the multicast carve, which IS derivable, see netbox_multicast_carve.py.

CREATE-ONLY, BY DESIGN. An object whose slug already exists is left completely
alone -- not updated, not renamed, not re-described. This is not laziness: a
run of netbox_tag with state=present would have renamed a pre-existing 'Core1'
tag to 'core1' because the slug matched, silently altering data the automation
does not own. A setup script must never damage the NetBox it is being pointed
at. If an existing object is wrong, the report tells you and you fix it by hand.

Dry run unless --apply is given.

    export NETBOX_API=https://netbox.example.com NETBOX_TOKEN=...
    scripts/netbox_provision_schema.py            # show what is missing
    scripts/netbox_provision_schema.py --apply    # create it
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import pynetbox
except ImportError:
    sys.exit("pynetbox is required:  venv/bin/pip install pynetbox")


# --- Device identity -------------------------------------------------------
#
# SLUGS USE UNDERSCORES. Ansible's TRANSFORM_INVALID_GROUP_CHARS defaults to
# 'never', so a hyphen is NOT rewritten: slug 'fabric-border' produces the group
# 'device_roles_fabric-border' while the playbooks look for
# 'device_roles_fabric_border'. Nothing matches, nothing renders, nothing errors.
# Do not "tidy" these into kebab-case.
#
# Borders and leaves are DEVICE ROLES; spines are a TAG. A spine is a control-
# plane role rather than a hardware class -- often the same platform as a
# distribution switch -- so a tag composes with whatever device role a site's
# own conventions already use. netbox_inv.yml turns these into the groups the
# playbooks branch on: device_roles_fabric_border, device_roles_fabric_leaf,
# tags_fabric_spine.
DEVICE_ROLES = {
    "fabric_border": ("Fabric Border",
                      "EVPN border: VRF handoff SVIs, eBGP to upstream, border-to-border mesh"),
    "fabric_leaf": ("Fabric Leaf",
                    "EVPN leaf: tenant SVIs, EVPN instances, NVE interface"),
}

# --- Tags ------------------------------------------------------------------
#
# Tags CLASSIFY. They never encode identity -- ownership of an allocated address
# is expressed as IPAddress -> Interface -> Device, not by tagging it with a
# hostname. The one exception is the <hostname> tag on a vrf_l3_handoff_vlan,
# which selects which border renders it; those are per-fabric and are NOT
# created here because they depend on your device names.
TAGS = {
    "fabric_spine": ("Fabric Spine",
                     "EVPN/MVPN route reflector and anycast RP. Identifies spines to the inventory."),
    "dag_routed": ("DAG Routed",
                   "Routed-only distributed anycast gateway: ingress replication, no multicast group needed"),
    "multicast_enabled": ("Multicast Enabled",
                          "TRM: this edge VLAN's SVI gets PIM sparse-mode and IGMPv3"),
    "trm_anycast_rp": ("TRM Anycast RP",
                       "TRM: the shared fabric-internal RP address for a VRF (one per VRF)"),
    "trm_external_rp": ("TRM External RP",
                        "TRM: an RP outside the fabric for a VRF"),
    "trm_register_source": ("TRM Register Source",
                            "TRM: the allocation pool prefix, and each VTEP's PIM register-source address"),
    # Selects the UNDERLAY anycast RP. The IPAddress must ALSO carry NetBox's
    # built-in Anycast role -- that is what permits the duplicate address across
    # the spines, and the resolver reports an object that has the tag without
    # it. Role alone would be far too broad a selector: an anycast gateway SVI
    # carries the same role and is not an RP.
    "underlay_anycast_rp": ("Underlay Anycast RP",
                            "The fabric's underlay anycast RP: one IPAddress per spine, all the "
                            "same address, role Anycast, each assigned to that spine's RP loopback"),
}

# --- VLAN roles ------------------------------------------------------------
#
# Overlay enrolment is opt-in: a VLAN with no role, or an unrecognised one,
# renders nothing and is reported in the run output.
VLAN_ROLES = {
    "edge": ("Edge", "Tenant EVPN segment: EVI, NVE member, anycast-gateway SVI"),
    "core_vlan": ("Core VLAN", "Per-VRF L3VNI transit VLAN with an ip unnumbered SVI"),
    "vrf_l3_handoff_vlan": ("VRF L3 Handoff VLAN", "Border handoff SVI and eBGP neighbour"),
}

# --- Prefix roles ----------------------------------------------------------
#
# Shared with netbox_multicast_carve.py, which creates the prefixes themselves.
# Created here too so the schema is complete before any carve is run.
PREFIX_ROLES = {
    "l2vni_replication": ("L2VNI Replication", "L2VNI BUM replication groups (underlay)"),
    "default_mdt": ("Default MDT", "MVPN/TRM default MDT groups (underlay)"),
    "data_mdt": ("Data MDT", "MVPN/TRM data MDT ranges (underlay)"),
}

# --- Custom fields ---------------------------------------------------------
#
# Any address in 239.0.0.0/8. Deliberately the WHOLE administratively scoped
# range (RFC 2365), matching what acm_evpn's own validator enforces. A regex
# encoding the per-VRF carve (239.<index>.1.x) would reject 239.0.x, which is
# where L2-only overlay groups live -- index 0 belongs to no VRF.
MCAST_REGEX = r"^239\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){2}$"

# (name, type, object types, label, description[, validation_regex])
#
# 'l3vni' ENROLS a VRF in the fabric and 'l2vni' enrols a VRF-less VLAN as an
# L2-only overlay. A VRF or VLAN without its marker is ignored entirely, which
# is how NetBox can hold WAN, out-of-band and non-fabric objects alongside.
CUSTOM_FIELDS = [
    ("l3vni", "integer", ["ipam.vrf"], "L3VNI",
     "Enrols this VRF in the EVPN fabric. Allocate 20001-20254. Drives the RD, "
     "route-targets and the VRF's multicast index (l3vni - 20000). Blank = not a fabric VRF."),
    ("mcast_base_group", "text", ["ipam.vrf"], "Multicast Base Group",
     "Underlay multicast group for every bridged segment in this VRF. "
     "Required if the VRF has any bridged (non-dag_routed) segment.",
     MCAST_REGEX),
    ("l2vni", "integer", ["ipam.vlan"], "L2VNI",
     "Two meanings. On a VLAN in a VRF: an override for the derived L2VNI "
     "(10000 + vid), for brownfield segments only. On a VLAN with NO VRF: the "
     "marker that ENROLS it as an L2-only overlay -- without it the VLAN is not "
     "part of the fabric."),
    ("mcast_group", "text", ["ipam.vlan"], "Multicast Group",
     "Overrides the VRF's mcast_base_group for this VLAN alone, and is REQUIRED "
     "on an L2-only overlay, which has no VRF to inherit from.",
     MCAST_REGEX),
    ("upstream_asn", "integer", ["ipam.vlan"], "Upstream ASN",
     "eBGP peer ASN for a vrf_l3_handoff_vlan, rendered as its remote-as. Per "
     "handoff, since a border can face a different upstream per tenant and two "
     "fabrics almost certainly do. Blank falls back to evpn_ext_bgp_asn in "
     "a global, which no longer exists."),
    # On the PREFIX, not the VLAN: _apply_prefix() reads these from the
    # prefix's custom_field_data. A VLAN can carry an IPv4 and an IPv6 prefix
    # with different relay destinations, which a VLAN-level field could not
    # express.
    ("dhcp_servers_ipv4", "multiobject", ["ipam.prefix"], "DHCP Servers IPv4",
     "Relay destinations rendered as 'ip helper-address <addr> global'."),
    ("dhcp_servers_ipv6", "multiobject", ["ipam.prefix"], "DHCP Servers IPv6",
     "Relay destinations rendered as 'ipv6 dhcp relay destination <addr> global'."),
]
# Object type backing the two multiobject fields above.
DHCP_OBJECT_TYPE = "ipam.ipaddress"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually create the missing objects (default is a dry run)")
    ap.add_argument("--url", default=os.environ.get("NETBOX_API"))
    ap.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"))
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (self-signed NetBox only)")
    args = ap.parse_args()

    if not args.url or not args.token:
        return int(bool(sys.stderr.write(
            "NETBOX_API and NETBOX_TOKEN must be set (or passed as --url/--token).\n")))

    nb = pynetbox.api(args.url, token=args.token)
    if args.insecure:
        import requests
        s = requests.Session()
        s.verify = False
        nb.http_session = s

    created, existing, failed = [], [], []

    def ensure(endpoint, slug_field, slug, payload, kind):
        """Create only if absent. Never modifies an existing object."""
        try:
            found = endpoint.get(**{slug_field: slug})
        except Exception as exc:                       # noqa: BLE001 - report, keep going
            failed.append((kind, slug, f"lookup failed: {exc}"))
            return
        if found:
            existing.append((kind, slug))
            return
        if not args.apply:
            created.append((kind, slug))
            return
        try:
            endpoint.create(**payload)
            created.append((kind, slug))
        except Exception as exc:                       # noqa: BLE001
            failed.append((kind, slug, str(exc)))

    for slug, (name, desc) in DEVICE_ROLES.items():
        ensure(nb.dcim.device_roles, "slug", slug,
               {"name": name, "slug": slug, "description": desc, "color": "2196f3"},
               "device role")

    for slug, (name, desc) in TAGS.items():
        ensure(nb.extras.tags, "slug", slug,
               {"name": name, "slug": slug, "description": desc}, "tag")

    # VLAN roles and prefix roles share ipam.roles in NetBox.
    for slug, (name, desc) in {**VLAN_ROLES, **PREFIX_ROLES}.items():
        ensure(nb.ipam.roles, "slug", slug,
               {"name": name, "slug": slug, "description": desc}, "role")

    for cf_name, cf_type, obj_types, label, desc, *extra in CUSTOM_FIELDS:
        payload = {
            "name": cf_name,
            "label": label,
            "type": cf_type,
            "object_types": obj_types,
            "description": desc,
            "required": False,
        }
        if cf_type == "multiobject":
            payload["related_object_type"] = DHCP_OBJECT_TYPE
        if extra and extra[0]:
            payload["validation_regex"] = extra[0]
        ensure(nb.extras.custom_fields, "name", cf_name, payload, "custom field")

    verb = "created" if args.apply else "would create"
    print(f"\n{verb.upper()}: {len(created)}")
    for kind, slug in created:
        print(f"  + {kind:<14} {slug}")
    print(f"\nALREADY PRESENT (left untouched): {len(existing)}")
    for kind, slug in existing:
        print(f"  = {kind:<14} {slug}")
    if failed:
        print(f"\nFAILED: {len(failed)}")
        for kind, slug, err in failed:
            print(f"  ! {kind:<14} {slug}: {err}")

    if not args.apply and created:
        print("\nDry run -- nothing was written. Re-run with --apply to create the above.")
    if existing:
        print("\nExisting objects were NOT modified. If one is wrong (a mismatched slug\n"
              "or a description from another project), fix it in NetBox by hand -- this\n"
              "script will not overwrite data it does not own.")

    print("\nNext: model your sites (buildings) and site groups (campuses), then devices\n"
          "with role fabric_border / fabric_leaf or the fabric_spine tag, status Active,\n"
          "and primary IPv4 set to the Loopback0 address. See the data-model contract in\n"
          "roles/acm_evpn/README.md.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
