#!/usr/bin/env python3
"""Create the fabric's multicast carve in NetBox IPAM.

The carve is VRF-major: one /16 per VRF inside 239.0.0.0/8, subdivided by
function, so that `show ip mroute | include 239.<index>.` returns everything for
one VRF. See roles/acm_evpn/README.md for the full rationale.

    239.<index>.0.0/16      Container   "VRF <NAME> - multicast"    no role
    ├── 239.<index>.1.0/24  Active      role: l2vni_replication
    ├── 239.<index>.2.0/24  Reserved    role: default_mdt   <- Active enables TRM
    ├── 239.<index>.3.0/24  Reserved    role: data_mdt      <- optional
    └── 239.<index>.4.0/22  Reserved    future (L2 TRM, IPv6, DCI)

Every prefix is created in the GLOBAL table. NetBox's Prefix.vrf means "exists in
that VRF's routing table", which is false for an underlay multicast group, and
assigning one to a VRF breaks the prefix hierarchy (VRFs are separate address
spaces). The VRF association lives in the description; the machine-readable link
is the INDEX -- 239.<index>.x belongs to the VRF whose l3vni is
index + evpn_l3vni_base. The 'mdt_default_group' / 'mdt_data_range' VRF custom
fields this once referred to were never created: acm_evpn derives the default
MDT group from the first address of the default_mdt prefix, and the data MDT
range from the data_mdt prefix itself, so there is nothing to keep in step.

Idempotent: existing objects are left alone. Dry-run unless --apply is given.

    export NETBOX_API=https://netbox.example.com NETBOX_TOKEN=...
    scripts/netbox_multicast_carve.py --vrf CORP=1 --vrf MEDICAL=2
    scripts/netbox_multicast_carve.py --vrf CORP=1 --vrf MEDICAL=2 --apply
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    import pynetbox
except ImportError:
    sys.exit("pynetbox is required:  venv/bin/pip install pynetbox")

ROOT = "239.0.0.0/8"

# NetBox prefix roles the acm_evpn validator keys on. Slugs must match
# mcast_allowed_prefix_roles / mcast_reserved_prefix_roles in the role defaults.
ROLES = {
    "l2vni_replication": "L2VNI BUM replication groups (underlay)",
    "default_mdt": "MVPN/TRM default MDT groups (underlay)",
    "data_mdt": "MVPN/TRM data MDT ranges (underlay)",
}

# (third octet, prefix length, role slug or None, status, label)
#
# The MDT ranges are created RESERVED, not active. A carve is a PLAN; status is
# what turns a plan into a decision. acm_evpn treats an ACTIVE 'default_mdt'
# prefix as the marker that a VRF is multicast-enabled, so creating them active
# would switch TRM on for every VRF the moment the carve ran. Flip a VRF's
# 239.<index>.2.0/24 to Active in NetBox to enable TRM on it; the data MDT is
# optional and enabled the same way.
FUNCTIONS = [
    (1, 24, "l2vni_replication", "active", "L2VNI replication"),
    (2, 24, "default_mdt", "reserved", "default MDT (Active enables TRM)"),
    (3, 24, "data_mdt", "reserved", "data MDT (optional; Active to use)"),
    (4, 22, None, "reserved", "reserved for future use (L2 TRM, IPv6, DCI)"),
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vrf", action="append", default=[], metavar="NAME=INDEX",
                    help="fabric VRF and its multicast index (1-254); repeatable")
    ap.add_argument("--apply", action="store_true",
                    help="actually create objects (default: dry run)")
    ap.add_argument("--url", default=os.environ.get("NETBOX_API"))
    ap.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"))
    ap.add_argument("--insecure", action="store_true", default=True,
                    help="skip TLS verification (matches the playbook's behaviour)")
    return ap.parse_args()


def parse_vrfs(pairs: list) -> dict:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"--vrf expects NAME=INDEX, got {pair!r}")
        name, _, raw = pair.partition("=")
        try:
            index = int(raw)
        except ValueError:
            sys.exit(f"--vrf index must be an integer, got {raw!r}")
        if not 1 <= index <= 254:
            # 239.0.0.0/16 is left unused; 239.255.0.0/16 is IPv4 Local Scope.
            sys.exit(f"VRF {name}: index must be 1-254, got {index}")
        if index in out.values():
            sys.exit(f"VRF {name}: index {index} is already used by "
                     f"{[k for k, v in out.items() if v == index][0]}")
        out[name.strip()] = index
    return out


def main() -> int:
    args = parse_args()
    if not args.url or not args.token:
        sys.exit("set NETBOX_API and NETBOX_TOKEN (or pass --url/--token)")
    vrfs = parse_vrfs(args.vrf)
    if not vrfs:
        sys.exit("give at least one --vrf NAME=INDEX")

    nb = pynetbox.api(args.url, token=args.token)
    if args.insecure:
        import requests
        session = requests.Session()
        session.verify = False
        nb.http_session = session
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    verb = "CREATE" if args.apply else "would create"
    created = existing = 0

    def ensure_role(slug: str, description: str):
        nonlocal created, existing
        role = nb.ipam.roles.get(slug=slug)
        if role:
            print(f"  exists   role   {slug}")
            existing += 1
            return role
        print(f"  {verb} role   {slug}  ({description})")
        created += 1
        if not args.apply:
            return None
        return nb.ipam.roles.create(name=slug.replace("_", " ").title(),
                                    slug=slug, description=description)

    def ensure_prefix(prefix: str, status: str, role_slug, description: str):
        nonlocal created, existing
        # vrf_id='null' scopes the lookup to the global table, so a same-named
        # prefix inside a VRF is not mistaken for this one.
        found = nb.ipam.prefixes.get(prefix=prefix, vrf_id="null")
        if found:
            print(f"  exists   prefix {prefix:<20} {status:<9} "
                  f"{role_slug or '-':<18} {description}")
            existing += 1
            return found
        print(f"  {verb} prefix {prefix:<20} {status:<9} "
              f"{role_slug or '-':<18} {description}")
        created += 1
        if not args.apply:
            return None
        payload = {"prefix": prefix, "status": status, "description": description}
        if role_slug:
            role = nb.ipam.roles.get(slug=role_slug)
            if not role:
                sys.exit(f"role {role_slug} missing; run without --apply first")
            payload["role"] = role.id
        return nb.ipam.prefixes.create(**payload)

    print("\n== prefix roles ==")
    for slug, desc in ROLES.items():
        ensure_role(slug, desc)

    print("\n== container ==")
    ensure_prefix(ROOT, "container", None,
                  "Administratively scoped multicast (RFC 2365) - underlay")

    for name, index in sorted(vrfs.items(), key=lambda kv: kv[1]):
        print(f"\n== VRF {name} (index {index}) ==")
        ensure_prefix(f"239.{index}.0.0/16", "container", None,
                      f"VRF {name} - multicast")
        for octet, length, role_slug, status, label in FUNCTIONS:
            ensure_prefix(f"239.{index}.{octet}.0/{length}", status, role_slug,
                          f"VRF {name} - {label}")

    print(f"\n{created} to create, {existing} already present.")
    if not args.apply:
        print("Dry run - nothing was written. Re-run with --apply to commit.")
    print("\nNext: set each VRF's mcast_base_group custom field to "
          "239.<index>.1.1 (see roles/acm_evpn/README.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
