#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Delete every trace of VRF SECURITY from NetBox, reversibly.

The counterpart to netbox_enrol_vrf_security.py, and written to make a VRF
decommission testable on hardware: NetBox is emptied of the VRF, deploy_evpn
renders its removal, and the switch is checked for anything left behind.

WHY IT DISCOVERS INSTEAD OF DELETING A LIST
-------------------------------------------
A hand-written list can only remove what its author remembered creating, and the
whole point of this exercise is finding what nobody remembered. Run against the
live fabric it turned up 169.254.255.0/30 in VRF SECURITY attached to core VLAN
399, which the enrolment script never created and which is not optional: it is
what BINDS the core VLAN to the VRF. NetBox has no VRF field on a VLAN, so a
prefix inside the VRF is the only way to express the relationship, and every
core VLAN on this fabric carries the same 169.254.255.0/30 in its own VRF.
Everything below is found by following references from the VRF outwards.

WHAT IT FINDS, and every one of these is a way a teardown can be incomplete:

  the VRF                its custom fields go with it: l3vni, mcast_base_group
  prefixes IN the VRF    including any added later by anything at all
  IP addresses IN it     the external RP, and every trm_register_source
  Loopback<l3vni>        the INTERFACES on each device, created by
                         trm_allocate.yml. Deleting the addresses is not enough;
                         the interfaces are separate objects and outlive them
  the multicast carve    239.<index>.x, and it is the easy one to miss because
                         it lives in the GLOBAL table -- Prefix.vrf is null, so
                         a query filtered by VRF never returns it. The index is
                         derived from the VRF's own l3vni, not hardcoded
  VLANs                  found two ways and reported by which rule: any VLAN a
                         prefix in the VRF points at, plus any whose name
                         carries the VRF's. The first rule is the load-bearing
                         one, because a prefix IS how a VLAN belongs to a VRF --
                         NetBox has no VRF field on a VLAN, so every core and
                         handoff VLAN carries one for no other purpose. The
                         second rule is belt and braces for a VLAN named after
                         the VRF that somehow has no prefix, which would mean
                         the VRF was mis-enrolled rather than that the VLAN is
                         unnumbered by design

RESTORING
---------
Everything happens inside a NetBox branch, so the delete is a proposal until it
is merged and reversible after. Two different situations, and confusing them
loses the data:

  BEFORE merging   the objects are untouched on main. Delete the branch, or
                   simply never merge it.
  AFTER merging    REVERT the branch in NetBox. Deleting a merged branch undoes
                   nothing AND destroys the only way back, because a deleted
                   branch cannot be reverted.

    export NETBOX_API=https://netbox.example.com NETBOX_TOKEN=...
    scripts/netbox_teardown_vrf_security.py            # what it would delete
    scripts/netbox_teardown_vrf_security.py --apply    # into a branch
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

VRF_NAME = "SECURITY"
BRANCH_NAME = "vrf-security-teardown"
BRANCH_HEADER = "X-NetBox-Branch"

# The multicast index is l3vni - this, matching netbox_multicast_carve.py and
# the enrolment script. Derived so the carve cannot be looked for in the wrong
# block if the VRF is ever renumbered.
L3VNI_BASE = 20000


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
        self.url, self.token, self.branch_value = url.rstrip("/"), token, None

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
                return None if r.status == 204 else json.load(r)
        except urllib.error.HTTPError as exc:
            raise SystemExit("%s %s failed: %s\n%s" % (method, url, exc, _explain(exc)))

    def get(self, path, branch=True, **params):
        out, url = [], "%s/api/%s?%s" % (self.url, path, urllib.parse.urlencode(params))
        while url:
            page = self._request("GET", url, branch=branch)
            out += page["results"]
            url = page.get("next")
        return out

    def delete(self, path, branch=True):
        return self._request("DELETE", path, branch=branch)

    def post(self, path, data, branch=True):
        return self._request("POST", path, data, branch=branch)


def wait_for_ready(nb: NetBox, branch: dict, timeout=300) -> dict:
    """A new branch is not writable until provisioning finishes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = nb._request("GET", "plugins/branching/branches/%d/" % branch["id"],
                          branch=False)
        status = cur.get("status") or {}
        value = status.get("value") if isinstance(status, dict) else status
        if value == "ready":
            return cur
        if value in ("failed", "errored"):
            raise SystemExit("branch %r provisioning %s" % (cur["name"], value))
        print("   branch status %s, waiting..." % value)
        time.sleep(5)
    raise SystemExit("branch did not become ready within %ds" % timeout)


def discover(nb: NetBox) -> list:
    """Everything that must go, in an order safe to delete in.

    Order is by reference, not by importance: an IP address points at an
    interface and a VRF, a prefix points at a VLAN and a VRF, so the pointers go
    before the things they point at. Getting this backwards does not corrupt
    anything -- NetBox refuses -- but it fails halfway through, which on a
    teardown is the worst outcome.
    """
    vrfs = nb.get("ipam/vrfs/", name=VRF_NAME)
    if not vrfs:
        raise SystemExit("VRF %s does not exist -- nothing to tear down" % VRF_NAME)
    vrf = vrfs[0]
    vid = vrf["id"]
    l3vni = (vrf.get("custom_fields") or {}).get("l3vni")

    plan = []

    # 1. IP addresses in the VRF: the external RP and every register source.
    for ip in nb.get("ipam/ip-addresses/", vrf_id=vid, limit=500):
        ao = ip.get("assigned_object") or {}
        where = "%s on %s" % (ao.get("name"), (ao.get("device") or {}).get("name")) \
            if ao else "unassigned"
        plan.append(("ip address", "ipam/ip-addresses/%d/" % ip["id"],
                     "%s  %s  [%s]" % (ip["address"], where,
                                       ",".join(t["slug"] for t in ip["tags"]) or "-")))

    # 2. The Loopback<l3vni> INTERFACES themselves. trm_allocate.yml creates
    #    these; they are separate objects from their addresses and survive them.
    if l3vni:
        for iface in nb.get("dcim/interfaces/", name="Loopback%d" % l3vni, limit=500):
            plan.append(("interface", "dcim/interfaces/%d/" % iface["id"],
                         "Loopback%d on %s" % (l3vni, iface["device"]["name"])))

    # 3. Prefixes in the VRF. Collect the VLANs they point at before deleting.
    vlan_ids, vlan_why = {}, {}
    for pfx in nb.get("ipam/prefixes/", vrf_id=vid, limit=500):
        vlan = pfx.get("vlan")
        if vlan:
            vlan_ids[vlan["id"]] = vlan
            vlan_why[vlan["id"]] = "prefix %s points at it" % pfx["prefix"]
        plan.append(("prefix (vrf)", "ipam/prefixes/%d/" % pfx["id"],
                     "%s  vlan=%s" % (pfx["prefix"], (vlan or {}).get("vid", "-"))))

    # 4. The multicast carve, in the GLOBAL table. Filtering by VRF never
    #    returns these: an underlay multicast group does not live in a tenant's
    #    routing table, so Prefix.vrf is null by design.
    if l3vni:
        block = "239.%d.0.0/16" % (l3vni - L3VNI_BASE)
        for pfx in nb.get("ipam/prefixes/", within_include=block, limit=100):
            if pfx.get("vrf"):
                continue
            plan.append(("prefix (carve)", "ipam/prefixes/%d/" % pfx["id"],
                         "%s  %s" % (pfx["prefix"], (pfx.get("role") or {}).get("slug") or "-")))

    # 5. VLANs, by two rules. Rule one does the work: a prefix in the VRF is
    #    the ONLY way a VLAN belongs to a VRF, since NetBox has no VRF field on
    #    a VLAN. Every core VLAN carries 169.254.255.0/30 in its own VRF for
    #    exactly that reason -- the same prefix three times over, which is legal
    #    because VRFs are separate address spaces. Rule two catches a VLAN named
    #    for the VRF that has no prefix, which means a mis-enrolment rather than
    #    a VLAN that is unnumbered on purpose.
    for vlan in nb.get("ipam/vlans/", limit=1000):
        if VRF_NAME in (vlan.get("name") or "") and vlan["id"] not in vlan_ids:
            vlan_ids[vlan["id"]] = vlan
            vlan_why[vlan["id"]] = "name carries %s" % VRF_NAME
    for vid_, vlan in sorted(vlan_ids.items(), key=lambda kv: kv[1]["vid"]):
        plan.append(("vlan", "ipam/vlans/%d/" % vid_,
                     "%s %s  (%s)" % (vlan["vid"], vlan.get("name") or "", vlan_why[vid_])))

    # 6. The VRF last. Its custom fields -- l3vni, mcast_base_group -- go with it.
    plan.append(("vrf", "ipam/vrfs/%d/" % vid,
                 "%s  l3vni=%s" % (VRF_NAME, l3vni)))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="create the branch and delete; otherwise print the plan")
    ap.add_argument("--branch", default=BRANCH_NAME)
    ap.add_argument("--url", default=os.environ.get("NETBOX_API"))
    ap.add_argument("--token", default=os.environ.get("NETBOX_TOKEN"))
    args = ap.parse_args()

    if not args.url or not args.token:
        print("NETBOX_API and NETBOX_TOKEN must be set", file=sys.stderr)
        return 2

    nb = NetBox(args.url, args.token)

    if args.apply:
        existing = nb.get("plugins/branching/branches/", branch=False, name=args.branch)
        if existing:
            print("Reusing branch %r" % args.branch)
            branch = wait_for_ready(nb, existing[0])
        else:
            print("Creating branch %r..." % args.branch)
            branch = wait_for_ready(nb, nb.post(
                "plugins/branching/branches/",
                {"name": args.branch,
                 "description": "Tear down VRF %s: every object, reversibly" % VRF_NAME},
                branch=False))
        schema = branch.get("schema_id")
        if not schema:
            raise SystemExit("branch %r has no schema_id" % args.branch)
        nb.branch_value = str(schema)
        print("Branch ready, schema_id=%s\n" % schema)

    plan = discover(nb)

    print("PLAN" if not args.apply else "DELETED (branch %r)" % args.branch)
    width = max(len(k) for k, _, _ in plan)
    for kind, path, what in plan:
        print("  %-*s  %s" % (width, kind, what))
        if args.apply:
            nb.delete(path)

    counts = {}
    for kind, _, _ in plan:
        counts[kind] = counts.get(kind, 0) + 1
    print("\n  %d object(s): %s" % (
        len(plan), ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items()))))

    if not args.apply:
        print("\nDry run. Re-run with --apply to create the branch and delete.")
    else:
        print("""
Nothing on main has changed yet. To APPLY the teardown, merge branch %r in
NetBox, then run deploy_evpn.yml so the fabric renders the removal.

TO RESTORE, and the two cases are not the same:
  before merging   the objects are untouched on main -- delete the branch, or
                   just never merge it
  after merging    REVERT the branch. Deleting a merged branch undoes nothing
                   and destroys the only way back: a deleted branch cannot be
                   reverted.""" % args.branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
