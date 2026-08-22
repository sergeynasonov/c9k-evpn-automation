#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Enrol VRF SECURITY: custom fields, multicast carve, VLANs, prefixes, RP.

SECURITY already exists in NetBox with no custom fields at all, which is what
"un-enrolled" means here -- acm_evpn keys on l3vni, so a VRF without one renders
nothing and reports no error. This script supplies everything that turns it into
a fabric VRF with TRM in EXTERNAL RP mode, modelled on MEDICAL.

WHY THIS IS A ONE-OFF SCRIPT AND NOT A GENERAL TOOL
---------------------------------------------------
netbox_provision_schema.py deliberately does not create VRFs, VLANs or prefixes:
"those are your network, and a script inventing them would be guessing". Nothing
here is invented -- every address, VID and name is a reviewed decision, written
out as a constant below so the whole change can be read in one screen before it
is applied. That is the difference, and it is the reason this exists as declared
data rather than as arguments.

WHY THE CARVE IS REPRODUCED HERE
--------------------------------
netbox_multicast_carve.py knows nothing about branches and writes to main. Half
this change in a branch and half on main would mean a revert leaves the 239.5
prefixes behind -- the same reasoning that made the validation repo reproduce
the carve rather than call that script. The layout below is that script's, with
default_mdt and data_mdt ACTIVE to match MEDICAL rather than left Reserved.

EVERYTHING LANDS IN A NETBOX BRANCH, so nothing is visible to a deploy until the
branch is merged, and the whole change is revertible by deleting it. Provisioning
a branch is a background job, so the script polls for 'ready' rather than
assuming; writes are scoped with 'X-NetBox-Branch: <schema_id>'.

CREATE-ONLY, with exactly one exception: the VRF custom fields, which are a PATCH
because the VRF already exists. That patch refuses to overwrite a value that is
already set to something different -- enrolling a VRF twice with different
numbers is a mistake worth stopping, not merging.

    export NETBOX_API=https://netbox.example.com NETBOX_TOKEN=...
    scripts/netbox_enrol_vrf_security.py            # dry run, prints the plan
    scripts/netbox_enrol_vrf_security.py --apply    # create the branch and write

To undo before merging, delete the branch in the NetBox UI. After merging, the
objects are on main and deleting the branch undoes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- the change, declared -----------------------------------------------------

VRF_NAME = "SECURITY"
L3VNI = 20005                      # CORP 20003, MEDICAL 20004, next is 20005
MCAST_INDEX = L3VNI - 20000        # the carve is indexed by this, not by name
MCAST_BASE_GROUP = "239.5.1.1"     # first address of the l2vni_replication /24

BRANCH_NAME = "vrf-security-enrolment"
VLAN_GROUP = "evpn_campus_vlans"   # campus-wide, so the edge VLAN spans buildings

# Global table, never in a VRF: Prefix.vrf means "exists in that VRF's routing
# table", which is false for an underlay multicast group.
CARVE = [
    ("239.%d.0.0/16" % MCAST_INDEX, None, "container",
     "VRF %s - multicast" % VRF_NAME),
    ("239.%d.1.0/24" % MCAST_INDEX, "l2vni_replication", "active",
     "L2VNI replication"),
    ("239.%d.2.0/24" % MCAST_INDEX, "default_mdt", "active",
     "default MDT (Active = TRM enabled)"),
    ("239.%d.3.0/24" % MCAST_INDEX, "data_mdt", "active",
     "data MDT"),
    ("239.%d.4.0/22" % MCAST_INDEX, None, "reserved",
     "reserved for future use (L2 TRM, IPv6, DCI)"),
]

# 397/398 are the handoffs and 399 the core VLAN, following CORP's 197/198/199
# and MEDICAL's 297/298/299.
VLANS = [
    (300, "SECURITY_DATA_CAMPUS", "edge",
     ["dag_routed", "multicast_enabled"]),
    (397, "VRF_SECURITY_HANDOFF397", "vrf_l3_handoff_vlan", ["core1"]),
    (398, "VRF_SECURITY_HANDOFF398", "vrf_l3_handoff_vlan", ["core2"]),
    (399, "VRF_SECURITY_CORE_VLAN", "core_vlan", []),
]

# One contiguous /25, which is a DEVIATION from MEDICAL and a forced one:
# MEDICAL keeps its infrastructure in 10.67.118.0/24 and its edge in .119, but
# 10.67.118.0/24 has only .4-.15 left against the 32 addresses that shape needs.
# The internal layout -- /28 register pool, then the two handoff /31s at .48 and
# .50 -- is MEDICAL's exactly.
#
# (prefix, vlan vid or None, tags, description, dhcp)
PREFIXES = [
    ("10.67.119.0/25", None, [], "VRF %s" % VRF_NAME, False),
    ("10.67.119.0/27", 300, [], "", True),
    ("10.67.119.32/27", None, [], "VRF %s - infrastructure" % VRF_NAME, False),
    ("10.67.119.32/28", None, ["trm_register_source"],
     "TRM PIM Register Loopback Range for VRF %s" % VRF_NAME, False),
    ("10.67.119.48/31", 397, [], "", False),
    ("10.67.119.50/31", 398, [], "", False),
]

# THE CORE VLAN NEEDS A PREFIX, and the first version of this script omitted it
# on the belief that a core VLAN is unnumbered. It is unnumbered on the SWITCH
# -- the template renders 'ip unnumbered Loopback0' -- but that is not what this
# prefix is for. NetBox has no VRF field on a VLAN, so a prefix inside the VRF
# is the ONLY way to express that VLAN 399 belongs to SECURITY, and the render
# reads the relationship back through it.
#
# Omitting it enrolled a VRF whose core VLAN belonged to nothing, and it had to
# be added by hand afterwards. Every core VLAN on this fabric carries the same
# 169.254.255.0/30 in its own VRF: CORP's 199 since 2026-07-18, MEDICAL's 299
# since 2026-08-02, SECURITY's 399 since 2026-08-21. The same prefix three times
# is legal and intended -- VRFs are separate address spaces, and the address is
# never routed.
CORE_VLAN_BINDING = ("169.254.255.0/30", 399, [],
                     "binds the core VLAN to VRF %s -- NetBox has no VRF field "
                     "on a VLAN" % VRF_NAME, False)

# The same relay destination VLAN 200 carries.
DHCP_SERVER = "10.66.119.56/32"

# The SAME address MEDICAL points at, and that is the whole point: two VRFs with
# two different RPs are two isolated multicast domains. It is a valid underlay
# address serving as a shared anycast RP for both VRFs.
#
# Note what this does NOT do. The fabric registers sources and sends joins
# towards the RP through each VRF's own handoff; anything that crosses BETWEEN
# MEDICAL and SECURITY happens outside the fabric, on AS 64512. If those two
# handoffs land in external contexts that cannot reach each other, every object
# below is still correct and nothing will cross.
EXTERNAL_RP = "10.67.118.0/32"

# The per-VTEP Loopback20005 addresses are NOT created here. trm_allocate.yml
# creates the interface on the device in NetBox and attaches an address out of
# the pool above, which is how NetBox natively expresses ownership. Creating
# them here would be a second writer for the same objects.

BRANCH_HEADER = "X-NetBox-Branch"


def _explain(exc) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        return str(exc)
    try:
        return json.dumps(json.loads(body))[:400]
    except ValueError:
        return body[:400]


class NetBox:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.branch_value = None

    def _request(self, method: str, path: str, data=None, branch=True):
        url = path if path.startswith("http") else "%s/api/%s" % (self.url, path)
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", "Token %s" % self.token)
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/json")
        if branch and self.branch_value:
            req.add_header(BRANCH_HEADER, self.branch_value)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 204:
                    return None
                return json.load(r)
        except urllib.error.HTTPError as exc:
            raise SystemExit("%s %s failed: %s\n%s"
                             % (method, url, exc, _explain(exc)))

    def get(self, path, branch=True, **params):
        out, url = [], "%s/api/%s?%s" % (self.url, path,
                                         urllib.parse.urlencode(params))
        while url:
            page = self._request("GET", url, branch=branch)
            out += page["results"]
            url = page.get("next")
        return out

    def one(self, path, branch=True, **params):
        rows = self.get(path, branch=branch, **params)
        return rows[0] if rows else None

    def post(self, path, data, branch=True):
        return self._request("POST", path, data, branch=branch)

    def patch(self, path, data, branch=True):
        return self._request("PATCH", path, data, branch=branch)


def wait_for_ready(nb: NetBox, branch: dict, timeout=300) -> dict:
    """A new branch is not writable until provisioning finishes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = nb._request("GET", "plugins/branching/branches/%d/" % branch["id"],
                          branch=False)
        status = (cur.get("status") or {})
        value = status.get("value") if isinstance(status, dict) else status
        if value == "ready":
            return cur
        if value in ("failed", "errored"):
            raise SystemExit("branch %r provisioning %s" % (cur["name"], value))
        print("   branch status %s, waiting..." % value)
        time.sleep(5)
    raise SystemExit("branch did not become ready within %ds" % timeout)


def resolve(nb: NetBox, plan: list) -> dict:
    """Look up every id the change refers to, and fail loudly on anything absent.

    Every one of these is pre-existing vocabulary -- roles, tags, the VLAN group,
    the DHCP server address. A missing one means the schema was never provisioned
    or a slug is wrong, and creating objects that silently reference nothing is
    worse than stopping.
    """
    ids = {"role": {}, "tag": {}}
    need_roles = {r for _, r, _, _ in CARVE if r} | {v[2] for v in VLANS}
    need_tags = {t for v in VLANS for t in v[3]} | {t for p in PREFIXES for t in p[2]}
    need_tags.add("trm_external_rp")

    for slug in sorted(need_roles):
        # Prefix roles and VLAN roles are different models with the same slugs.
        pr = nb.one("ipam/roles/", slug=slug)
        if pr:
            ids["role"][slug] = pr["id"]
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

    vrf = nb.one("ipam/vrfs/", name=VRF_NAME)
    if not vrf:
        plan.append(("MISSING", "VRF %s -- create it first, this script enrols "
                                "an existing VRF" % VRF_NAME))
    ids["vrf"] = vrf

    dh = nb.one("ipam/ip-addresses/", address=DHCP_SERVER)
    if not dh:
        plan.append(("MISSING", "DHCP server address %s" % DHCP_SERVER))
    ids["dhcp"] = dh["id"] if dh else None
    return ids


def enrol(nb: NetBox, ids: dict, plan: list, apply: bool) -> None:
    vrf = ids["vrf"]
    vrf_id = vrf["id"]

    # --- 1. the VRF's own custom fields -- the only PATCH in the script -------
    cf = vrf.get("custom_fields") or {}
    want = {"l3vni": L3VNI, "mcast_base_group": MCAST_BASE_GROUP}
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
        plan.append(("CREATE", "VRF %s custom fields %s" % (VRF_NAME, change)))
        if apply:
            nb.patch("ipam/vrfs/%d/" % vrf_id, {"custom_fields": change})

    # --- 2. the multicast carve, global table --------------------------------
    for prefix, role, status, descr in CARVE:
        if nb.one("ipam/prefixes/", prefix=prefix, vrf_id="null"):
            plan.append(("exists", "prefix %s (global)" % prefix))
            continue
        body = {"prefix": prefix, "status": status, "description": descr}
        if role:
            body["role"] = ids["role"][role]
        plan.append(("CREATE", "prefix %s (global) %s %s"
                     % (prefix, status, role or "")))
        if apply:
            nb.post("ipam/prefixes/", body)

    # --- 3. VLANs ------------------------------------------------------------
    vlan_ids = {}
    for vid, name, role, tags in VLANS:
        existing = nb.one("ipam/vlans/", vid=vid, group_id=ids["vlan_group"])
        if existing:
            vlan_ids[vid] = existing["id"]
            plan.append(("exists", "vlan %d %s" % (vid, existing["name"])))
            continue
        body = {"vid": vid, "name": name, "status": "active",
                "group": ids["vlan_group"], "role": ids["role"][role],
                "tags": [{"id": ids["tag"][t]} for t in tags]}
        plan.append(("CREATE", "vlan %d %s role=%s tags=%s"
                     % (vid, name, role, ",".join(tags) or "-")))
        # None on a dry run: the VLAN is not created, so it has no id, but the
        # prefixes that reference it must still appear in the plan. A dry run
        # that silently drops three objects is not the plan that would run.
        vlan_ids[vid] = nb.post("ipam/vlans/", body)["id"] if apply else None

    # --- 4. prefixes in the VRF ----------------------------------------------
    for prefix, vid, tags, descr, dhcp in PREFIXES + [CORE_VLAN_BINDING]:
        if nb.one("ipam/prefixes/", prefix=prefix, vrf_id=vrf_id):
            plan.append(("exists", "prefix %s (%s)" % (prefix, VRF_NAME)))
            continue
        body = {"prefix": prefix, "status": "active", "vrf": vrf_id,
                "description": descr,
                "tags": [{"id": ids["tag"][t]} for t in tags]}
        if vid is not None:
            if vid not in vlan_ids:
                plan.append(("SKIP", "prefix %s needs vlan %d, which is neither "
                                     "present nor being created" % (prefix, vid)))
                continue
            if apply:
                body["vlan"] = vlan_ids[vid]
        if dhcp:
            body["custom_fields"] = {"dhcp_servers_ipv4": [ids["dhcp"]]}
        plan.append(("CREATE", "prefix %s (%s)%s%s"
                     % (prefix, VRF_NAME,
                        " vlan=%d" % vid if vid else "",
                        " dhcp=%s" % DHCP_SERVER if dhcp else "")))
        if apply:
            nb.post("ipam/prefixes/", body)

    # --- 5. the external RP --------------------------------------------------
    if nb.one("ipam/ip-addresses/", address=EXTERNAL_RP, vrf_id=vrf_id):
        plan.append(("exists", "ip %s (%s)" % (EXTERNAL_RP, VRF_NAME)))
    else:
        plan.append(("CREATE", "ip %s (%s) tag=trm_external_rp"
                     % (EXTERNAL_RP, VRF_NAME)))
        if apply:
            nb.post("ipam/ip-addresses/",
                    {"address": EXTERNAL_RP, "vrf": vrf_id, "status": "active",
                     "tags": [{"id": ids["tag"]["trm_external_rp"]}]})


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="create the branch and write; otherwise print the plan")
    ap.add_argument("--branch", default=BRANCH_NAME)
    ap.add_argument("--url", default=os.environ.get("NETBOX_API"))
    ap.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"))
    args = ap.parse_args()

    if not args.url or not args.token:
        print("NETBOX_API and NETBOX_TOKEN must be set", file=sys.stderr)
        return 2

    nb = NetBox(args.url, args.token)
    plan: list = []

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
                 "description": "Enrol VRF %s: TRM external RP, VLAN 300, "
                                "handoffs 397/398" % VRF_NAME}, branch=False))
        schema = branch.get("schema_id")
        if not schema:
            raise SystemExit("branch %r has no schema_id" % args.branch)
        nb.branch_value = str(schema)
        print("Branch ready, schema_id=%s\n" % schema)

    ids = resolve(nb, plan)
    blocking = [p for p in plan if p[0] in ("MISSING",)]
    if blocking:
        for kind, what in plan:
            print("  %-9s %s" % (kind, what))
        print("\nRefusing to continue: the vocabulary above is missing. Run "
              "netbox_provision_schema.py first.", file=sys.stderr)
        return 1

    enrol(nb, ids, plan, args.apply)

    print("PLAN" if not args.apply else "APPLIED (branch %r)" % args.branch)
    for kind, what in plan:
        print("  %-9s %s" % (kind, what))
    conflicts = [p for p in plan if p[0] == "CONFLICT"]
    created = [p for p in plan if p[0] == "CREATE"]
    print("\n  %d to create, %d already present, %d conflict(s)"
          % (len(created), len([p for p in plan if p[0] == "exists"]),
             len(conflicts)))
    if not args.apply:
        print("\nDry run. Re-run with --apply to create the branch and write.")
    elif created:
        print("\nWritten into branch %r. Review it in NetBox and merge when "
              "satisfied; deleting the branch undoes the whole change."
              % args.branch)
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
