#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Enrol VRF CLMEL: the VRF, its multicast carve, five VLANs and their prefixes.

A new tenant VRF with two campus-wide anycast-gateway segments and a handoff on
each border, laid out the way CORP, MEDICAL and SECURITY are:

    VRF CLMEL          l3vni 20006, multicast index 6, supernet 172.16.0.0/16
    VLAN 400           bridged DAG, 172.16.1.0/24, multicast group 239.6.1.1
    VLAN 401           routed DAG ('dag_routed'), 172.16.2.0/24, ingress repl.
    VLAN 497 / 498     L3 handoffs on core1 / core2, 172.16.0.0/31, .2/31
    VLAN 499           core VLAN (L3VNI transit), bound by 169.254.255.0/30

NO TRM. Unlike SECURITY this VRF carries no tenant routed multicast: no RP, no
register-source pool, no 'multicast_enabled' VLAN, and the default/data MDT
ranges stay RESERVED -- an Active default_mdt is what switches TRM on, so
leaving it Reserved is the decision, not an omission. trm_allocate.yml has
nothing to do for CLMEL.

The carve is STILL needed. VLAN 400 is bridged, so its BUM traffic is flooded
through an underlay multicast group, and the render refuses a bridged segment
in a VRF with no 'mcast_base_group'. That group has to sit inside an Active
l2vni_replication range in CLMEL's own 239.6.0.0/16. Turning TRM on later is a
status change on 239.6.2.0/24 plus an RP, not a re-carve.

WHY THIS IS A ONE-OFF SCRIPT -- see netbox_enrol_vrf_security.py, whose
reasoning applies unchanged: every value is a reviewed decision, declared as a
constant so the whole change reads in one screen before it is applied. The HTTP
client and branch handling are imported from that script rather than copied.

Two values are NOT constants, because they belong to objects that already
exist and copying them would be a second source of truth:

  * the DHCP relay destination is read from VLAN 100's prefix, so CLMEL points
    at whatever VLAN 100 points at -- and the run refuses if VLAN 100 has none,
    or more than one prefix disagreeing about it;
  * l3vni 20006 is checked against every other VRF, since two VRFs on one
    L3VNI share an RD suffix, route-targets and a multicast block.

EVERYTHING LANDS IN A NETBOX BRANCH. Nothing is visible to generate_evpn.yml
or deploy_evpn.yml until the branch is merged, and deleting the branch undoes
the whole change.

CREATE-ONLY, except that an object which already exists is RECONCILED rather
than skipped -- the lesson of the SECURITY enrolment, where skipping existing
VLANs left both handoffs pinned to no border. A value already set to something
different is a CONFLICT and is never overwritten.

    export NETBOX_API=https://netbox.example.com NETBOX_TOKEN=...
    scripts/netbox_enrol_vrf_clmel.py            # dry run, prints the plan
    scripts/netbox_enrol_vrf_clmel.py --apply    # create the branch and write

What has to happen OUTSIDE NetBox for the VRF to work end to end is listed in
scripts/README.md under "Enrolling a new VRF".
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netbox_enrol_vrf_security import NetBox, wait_for_ready  # noqa: E402

# --- the change, declared -----------------------------------------------------

VRF_NAME = "CLMEL"
L3VNI = 20006                      # CORP 20003, MEDICAL 20004, SECURITY 20005
L3VNI_BASE = 20000
MCAST_INDEX = L3VNI - L3VNI_BASE   # the carve is indexed by this, not by name
MCAST_BASE_GROUP = "239.%d.1.1" % MCAST_INDEX

BRANCH_NAME = "vrf-clmel-enrolment"
VLAN_GROUP = "evpn_campus_vlans"   # campus-wide: both edge VLANs span buildings

# Global table, never in a VRF -- see netbox_enrol_vrf_security.py. Only the
# replication range is Active. The MDT ranges are Reserved, which is what
# "no TRM" means to the render; see the module docstring.
CARVE = [
    ("239.%d.0.0/16" % MCAST_INDEX, None, "container",
     "VRF %s - multicast" % VRF_NAME),
    ("239.%d.1.0/24" % MCAST_INDEX, "l2vni_replication", "active",
     "L2VNI replication"),
    ("239.%d.2.0/24" % MCAST_INDEX, "default_mdt", "reserved",
     "default MDT (Reserved = TRM off; Active turns it on)"),
    ("239.%d.3.0/24" % MCAST_INDEX, "data_mdt", "reserved",
     "data MDT"),
    ("239.%d.4.0/22" % MCAST_INDEX, None, "reserved",
     "reserved for future use (L2 TRM, IPv6, DCI)"),
]

HANDOFF_ROLE = "vrf_l3_handoff_vlan"

# The eBGP peer ASN behind both handoffs. Not derivable -- it is the remote-as
# of a session with a router this project does not manage -- and the render
# refuses a handoff without one. 64512 is what CORP, MEDICAL and SECURITY peer
# with on the same two borders.
UPSTREAM_ASN = 64512

# 497/498 handoffs and 499 core VLAN, after CORP's 197/198/199, MEDICAL's
# 297/298/299 and SECURITY's 397/398/399.
#
# VLAN 400 is BRIDGED: no 'dag_routed', so it advertises MACs and replicates
# via 239.6.1.1. VLAN 401 is ROUTED-ONLY: 'dag_routed' gives it proxy-ARP and
# ingress replication, so it needs no group. Neither is 'multicast_enabled' --
# that tag requires TRM on the VRF, and the render refuses it otherwise.
#
# Names: no whitespace and at most 30 characters, or the render refuses them.
VLANS = [
    (400, "CLMEL_BRIDGED_CAMPUS", "edge", []),
    (401, "CLMEL_ROUTED_CAMPUS", "edge", ["dag_routed"]),
    (497, "VRF_CLMEL_HANDOFF497", HANDOFF_ROLE, ["core1"]),
    (498, "VRF_CLMEL_HANDOFF498", HANDOFF_ROLE, ["core2"]),
    (499, "VRF_CLMEL_CORE_VLAN", "core_vlan", []),
]

# (prefix, vlan vid or None, tags, description, dhcp)
#
# A handoff /31 renders its LOWEST address as the upstream peer and its HIGHEST
# as the border's own SVI: core1 is 172.16.0.1 peering with 172.16.0.0, core2
# is 172.16.0.3 peering with 172.16.0.2. The upstream router needs the mirror
# image -- see scripts/README.md.
PREFIXES = [
    ("172.16.0.0/16", None, [], "VRF %s" % VRF_NAME, False),
    ("172.16.0.0/31", 497, [], "VRF %s handoff, core1" % VRF_NAME, False),
    ("172.16.0.2/31", 498, [], "VRF %s handoff, core2" % VRF_NAME, False),
    ("172.16.1.0/24", 400, [], "VRF %s bridged DAG" % VRF_NAME, True),
    ("172.16.2.0/24", 401, [], "VRF %s routed DAG" % VRF_NAME, True),
]

# The core VLAN's "dummy" prefix. Its value is never configured -- the SVI is
# 'ip unnumbered' -- but NetBox has no VRF field on a VLAN, so a prefix inside
# the VRF is the ONLY thing that says VLAN 499 belongs to CLMEL. Without it the
# VLAN is read as VRF-less, dropped as unenrolled, and the fabric bridges while
# routing nothing. Every core VLAN on this fabric uses this same /30 in its own
# VRF; that is legal, because VRFs are separate address spaces.
CORE_VLAN_BINDING = ("169.254.255.0/30", 499, [],
                     "binds the core VLAN to VRF %s -- NetBox has no VRF field "
                     "on a VLAN" % VRF_NAME, False)

# Where the DHCP relay destination is copied from.
DHCP_SOURCE_VID = 100

VLAN_NAME_MAX = 30


def check_declarations() -> list:
    """The render's own rules, applied to the constants above before NetBox is
    touched, so a typo here fails in the dry run rather than in a generate."""
    problems = []
    if not 20001 <= L3VNI <= 20254:
        problems.append("l3vni %d is outside 20001-20254" % L3VNI)
    for vid, name, _role, _tags in VLANS:
        if len(name) > VLAN_NAME_MAX or any(c.isspace() for c in name):
            problems.append("VLAN %d name %r is not a valid IOS-XE VLAN name"
                            % (vid, name))
    supernet = ipaddress.ip_network(PREFIXES[0][0])
    for prefix, vid, _t, _d, _h in PREFIXES[1:]:
        if not ipaddress.ip_network(prefix).subnet_of(supernet):
            problems.append("%s (VLAN %s) is outside the VRF supernet %s"
                            % (prefix, vid, supernet))
    for prefix, vid, _t, _d, _h in PREFIXES:
        if vid is not None and vid not in {v[0] for v in VLANS}:
            problems.append("%s names VLAN %d, which is not declared" % (prefix, vid))
    for _p, vid, _t, _d, _h in PREFIXES:
        role = {v[0]: v[2] for v in VLANS}.get(vid)
        if role == HANDOFF_ROLE and ipaddress.ip_network(_p).prefixlen != 31:
            problems.append("handoff VLAN %d is not a /31" % vid)
    return problems


def resolve(nb: NetBox, plan: list) -> dict:
    """Look up every id the change refers to, and fail loudly on anything absent.

    Roles, tags and the VLAN group are pre-existing vocabulary; a missing one
    means the schema was never provisioned or a slug is wrong.
    """
    ids = {"role": {}, "tag": {}}
    need_roles = {r for _, r, _, _ in CARVE if r} | {v[2] for v in VLANS}
    need_tags = {t for v in VLANS for t in v[3]} | {t for p in PREFIXES for t in p[2]}

    for slug in sorted(need_roles):
        r = nb.one("ipam/roles/", slug=slug)
        if not r:
            plan.append(("MISSING", "role %s" % slug))
        else:
            ids["role"][slug] = r["id"]
    for slug in sorted(need_tags):
        t = nb.one("extras/tags/", slug=slug)
        if not t:
            plan.append(("MISSING", "tag %s" % slug))
        else:
            ids["tag"][slug] = t["id"]

    grp = nb.one("ipam/vlan-groups/", slug=VLAN_GROUP)
    if not grp:
        plan.append(("MISSING", "vlan group %s" % VLAN_GROUP))
    ids["vlan_group"] = grp["id"] if grp else None

    ids["vrf"] = nb.one("ipam/vrfs/", name=VRF_NAME)

    # Another VRF already on this L3VNI would share its route-targets and its
    # multicast block -- silently merging two tenants' routing tables.
    for other in nb.get("ipam/vrfs/", cf_l3vni=L3VNI):
        if other.get("name") != VRF_NAME:
            plan.append(("BLOCKED", "l3vni %d is already held by VRF %s"
                         % (L3VNI, other.get("name"))))

    # A VID already used OUTSIDE the campus group -- site-scoped in one
    # building, say -- derives the same L2VNI (10000 + vid) as the one created
    # here, and the render fails the whole fabric on the duplicate. Better to
    # find out before a branch exists.
    for vid, _name, _role, _tags in VLANS:
        for v in nb.get("ipam/vlans/", vid=vid):
            if (v.get("group") or {}).get("id") != ids["vlan_group"]:
                where = ((v.get("group") or {}).get("name")
                         or (v.get("site") or {}).get("name") or "no scope")
                plan.append(("BLOCKED", "VLAN %d already exists as %r (%s), "
                                        "outside %s" % (vid, v.get("name"), where,
                                                        VLAN_GROUP)))

    ids["dhcp"] = _dhcp_from_vlan(nb, plan)
    return ids


def _dhcp_from_vlan(nb: NetBox, plan: list):
    """The relay destination(s) VLAN 100 uses, as IP-address ids."""
    vlans = nb.get("ipam/vlans/", vid=DHCP_SOURCE_VID)
    if len(vlans) != 1:
        plan.append(("MISSING", "exactly one VLAN %d to copy the DHCP server "
                                "from (found %d)" % (DHCP_SOURCE_VID, len(vlans))))
        return None
    found = {}
    for p in nb.get("ipam/prefixes/", vlan_id=vlans[0]["id"]):
        servers = (p.get("custom_fields") or {}).get("dhcp_servers_ipv4") or []
        key = tuple(sorted(s["id"] for s in servers))
        if key:
            found[key] = [s.get("address") for s in servers]
    if len(found) != 1:
        plan.append(("MISSING", "one DHCP server set on VLAN %d's prefixes "
                                "(found %s)" % (DHCP_SOURCE_VID,
                                                list(found.values()) or "none")))
        return None
    key, addresses = next(iter(found.items()))
    plan.append(("source", "DHCP relay from VLAN %d: %s"
                 % (DHCP_SOURCE_VID, ", ".join(addresses))))
    return list(key)


def enrol(nb: NetBox, ids: dict, plan: list, apply: bool) -> None:
    vrf = ids["vrf"]
    want = {"l3vni": L3VNI, "mcast_base_group": MCAST_BASE_GROUP}

    # --- 0. the VRF ----------------------------------------------------------
    # No 'rd': the switch derives <loopback0>:<l3vni> per device. vrf_id stays
    # None on a dry run and every lookup needing it is skipped, because NetBox
    # reads vrf_id=None as "the global table", not "this VRF".
    if not vrf:
        plan.append(("CREATE", "VRF %s  l3vni=%d  mcast_base_group=%s"
                     % (VRF_NAME, L3VNI, MCAST_BASE_GROUP)))
        vrf = nb.post("ipam/vrfs/", {
            "name": VRF_NAME,
            "description": "EVPN tenant VRF, L3VNI %d" % L3VNI,
            "custom_fields": want,
        }) if apply else None
        ids["vrf"] = vrf
    else:
        cf = vrf.get("custom_fields") or {}
        change = {}
        for k, v in want.items():
            cur = cf.get(k)
            if cur in (None, "", []):
                change[k] = v
            elif str(cur) != str(v):
                plan.append(("CONFLICT", "VRF %s.%s is %r, refusing to overwrite "
                                         "with %r" % (VRF_NAME, k, cur, v)))
            else:
                plan.append(("exists", "VRF %s.%s = %r" % (VRF_NAME, k, cur)))
        if change:
            plan.append(("UPDATE", "VRF %s custom fields %s" % (VRF_NAME, change)))
            if apply:
                nb.patch("ipam/vrfs/%d/" % vrf["id"], {"custom_fields": change})
    vrf_id = vrf["id"] if vrf else None

    # --- 1. the multicast carve, global table --------------------------------
    for prefix, role, status, descr in CARVE:
        cur = nb.one("ipam/prefixes/", prefix=prefix, vrf_id="null")
        if cur:
            have = (cur.get("status") or {}).get("value")
            if have != status:
                # Most likely a TRM decision someone has already taken -- an
                # Active default_mdt. Not this script's to reverse.
                plan.append(("CONFLICT", "prefix %s (global) is %s, this "
                                         "enrolment expects %s" % (prefix, have, status)))
            else:
                plan.append(("exists", "prefix %s (global) %s" % (prefix, have)))
            continue
        body = {"prefix": prefix, "status": status, "description": descr}
        if role:
            body["role"] = ids["role"][role]
        plan.append(("CREATE", "prefix %s (global) %s %s"
                     % (prefix, status, role or "")))
        if apply:
            nb.post("ipam/prefixes/", body)

    # --- 2. VLANs: created, or reconciled if they exist ----------------------
    pinned = {t for _v, _n, r, tg in VLANS if r == HANDOFF_ROLE for t in tg}
    vlan_ids = {}
    for vid, name, role, tags in VLANS:
        existing = nb.one("ipam/vlans/", vid=vid, group_id=ids["vlan_group"])
        if existing:
            vlan_ids[vid] = existing["id"]
            _reconcile_vlan(nb, ids, plan, apply, existing, vid, role, tags, pinned)
            continue
        body = {"vid": vid, "name": name, "status": "active",
                "group": ids["vlan_group"], "role": ids["role"][role],
                "tags": [{"id": ids["tag"][t]} for t in tags]}
        if role == HANDOFF_ROLE:
            body["custom_fields"] = {"upstream_asn": UPSTREAM_ASN}
        plan.append(("CREATE", "vlan %d %s role=%s tags=%s%s"
                     % (vid, name, role, ",".join(tags) or "-",
                        " upstream_asn=%d" % UPSTREAM_ASN
                        if role == HANDOFF_ROLE else "")))
        vlan_ids[vid] = nb.post("ipam/vlans/", body)["id"] if apply else None

    # --- 3. prefixes in the VRF ----------------------------------------------
    for prefix, vid, tags, descr, dhcp in PREFIXES + [CORE_VLAN_BINDING]:
        if vrf_id and nb.one("ipam/prefixes/", prefix=prefix, vrf_id=vrf_id):
            plan.append(("exists", "prefix %s (%s)" % (prefix, VRF_NAME)))
            continue
        body = {"prefix": prefix, "status": "active", "vrf": vrf_id,
                "description": descr,
                "tags": [{"id": ids["tag"][t]} for t in tags]}
        if vid is not None and apply:
            body["vlan"] = vlan_ids[vid]
        if dhcp:
            body["custom_fields"] = {"dhcp_servers_ipv4": ids["dhcp"]}
        plan.append(("CREATE", "prefix %s (%s)%s%s"
                     % (prefix, VRF_NAME,
                        " vlan=%d" % vid if vid else "",
                        " +dhcp relay" if dhcp else "")))
        if apply:
            nb.post("ipam/prefixes/", body)


def _reconcile_vlan(nb, ids, plan, apply, existing, vid, role, tags, pinned):
    """Bring an existing VLAN up to this enrolment, or refuse and say why.

    Tags are added, never removed. A handoff pinned to a border this table does
    not give it, a different role, or a different upstream_asn is a CONFLICT:
    each would otherwise render something nobody declared.
    """
    have = {t["slug"] for t in existing.get("tags", [])}
    missing = [t for t in tags if t not in have]
    wrong = sorted((have & pinned) - set(tags)) if role == HANDOFF_ROLE else []
    have_role = (existing.get("role") or {}).get("slug")

    if wrong:
        plan.append(("CONFLICT", "vlan %d %s is pinned to %s, which this "
                                 "enrolment does not claim" % (vid, existing["name"],
                                                               ",".join(wrong))))
        return
    if have_role and have_role != role:
        plan.append(("CONFLICT", "vlan %d %s has role %s, expected %s"
                     % (vid, existing["name"], have_role, role)))
        return

    body = {}
    if missing:
        body["tags"] = ([{"id": t["id"]} for t in existing.get("tags", [])]
                        + [{"id": ids["tag"][t]} for t in missing])
        plan.append(("UPDATE", "vlan %d %s += tags %s"
                     % (vid, existing["name"], ",".join(missing))))
    if not have_role:
        body["role"] = ids["role"][role]
        plan.append(("UPDATE", "vlan %d %s := role %s" % (vid, existing["name"], role)))
    if role == HANDOFF_ROLE:
        cur = (existing.get("custom_fields") or {}).get("upstream_asn")
        if cur in (None, "", 0):
            body["custom_fields"] = {"upstream_asn": UPSTREAM_ASN}
            plan.append(("UPDATE", "vlan %d %s := upstream_asn %d"
                         % (vid, existing["name"], UPSTREAM_ASN)))
        elif int(cur) != UPSTREAM_ASN:
            plan.append(("CONFLICT", "vlan %d %s has upstream_asn %s, this "
                                     "enrolment expects %d"
                         % (vid, existing["name"], cur, UPSTREAM_ASN)))
            return
    if not body:
        plan.append(("exists", "vlan %d %s (already enrolled)" % (vid, existing["name"])))
    elif apply:
        nb.patch("ipam/vlans/%d/" % existing["id"], body)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="create the branch and write; otherwise print the plan")
    ap.add_argument("--branch", default=BRANCH_NAME)
    ap.add_argument("--url", default=os.environ.get("NETBOX_API"))
    ap.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"))
    args = ap.parse_args()

    problems = check_declarations()
    if problems:
        print("The declared change is invalid:\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 1
    if not args.url or not args.token:
        print("NETBOX_API and NETBOX_TOKEN must be set", file=sys.stderr)
        return 2

    nb = NetBox(args.url, args.token)
    plan: list = []

    # Resolve against MAIN before any branch exists, so a dry run and a refused
    # apply both leave NetBox exactly as they found it.
    ids = resolve(nb, plan)
    if any(k in ("MISSING", "BLOCKED") for k, _ in plan):
        for kind, what in plan:
            print("  %-9s %s" % (kind, what))
        print("\nRefusing to continue: resolve the MISSING / BLOCKED lines above. "
              "Nothing has been written.", file=sys.stderr)
        return 1

    if args.apply:
        existing = nb.one("plugins/branching/branches/", name=args.branch,
                          branch=False)
        if existing:
            print("Reusing branch %r" % args.branch)
            branch = wait_for_ready(nb, existing)
        else:
            print("Creating branch %r..." % args.branch)
            branch = wait_for_ready(nb, nb.post(
                "plugins/branching/branches/",
                {"name": args.branch,
                 "description": "Enrol VRF %s: bridged VLAN 400, routed VLAN 401, "
                                "handoffs 497/498, core VLAN 499, no TRM"
                                % VRF_NAME}, branch=False))
        schema = branch.get("schema_id")
        if not schema:
            raise SystemExit("branch %r has no schema_id" % args.branch)
        nb.branch_value = str(schema)
        print("Branch ready, schema_id=%s\n" % schema)
        # Re-read inside the branch: a reused branch may already hold the VRF.
        ids["vrf"] = nb.one("ipam/vrfs/", name=VRF_NAME)

    enrol(nb, ids, plan, args.apply)

    print("PLAN" if not args.apply else "APPLIED (branch %r)" % args.branch)
    for kind, what in plan:
        print("  %-9s %s" % (kind, what))
    conflicts = [p for p in plan if p[0] == "CONFLICT"]
    created = [p for p in plan if p[0] in ("CREATE", "UPDATE")]
    print("\n  %d to write, %d already present, %d conflict(s)"
          % (len(created), len([p for p in plan if p[0] == "exists"]),
             len(conflicts)))
    if not args.apply:
        print("\nDry run. Re-run with --apply to create the branch and write.")
    elif created:
        print("\nWritten into branch %r. Review it in NetBox, then see "
              "scripts/README.md 'Enrolling a new VRF' for the rest." % args.branch)
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
