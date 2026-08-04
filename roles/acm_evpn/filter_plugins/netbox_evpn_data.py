# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Transform raw NetBox GraphQL VLAN/prefix/VRF data into the per-fabric
structure consumed by the acm_evpn config templates.

This logic began as a Jinja template and was ported to Python wholesale.
It exists as a plugin, not a template, for three reasons:

  1. It is dense, stateful, branchy logic (grouping, scope-matching, IP
     math) — exactly the kind of code that is nearly impossible to unit
     test as a Jinja template, and easy to unit test as Python.
  2. It is on the hot path for scale: this transform used to run once
     *per host* (500+ times for a full fabric) even though its result is
     ~99% identical across hosts (only the per-site filtering differs).
     Callers should now compute this once with ``run_once: true`` and
     reuse it, rather than re-running it per host.
  3. It is the natural place to grow IPv6 address-family and multicast
     support without another rewrite — see ``build_evpn_data`` docstring.

Business rules encoded here (confirmed with the project owner):

  * Edge VLAN gateways (SVIs) take the FIRST usable host address in their
    prefix. This is applied at template-render time from ``prefix_ipv4``/
    ``prefix_ipv6`` (unchanged, not this module's concern) — noted here
    only for contrast with the next rule.
  * Layer-3 handoff / point-to-point links (role ``vrf_l3_handoff_vlan``)
    follow the OPPOSITE convention: the LOWEST usable address in the
    prefix belongs to the remote/upstream peer, and the HIGHEST usable
    address belongs to the local switch SVI. This is computed here, in
    ``handoff_addresses()``.
  * /31 (IPv4, RFC 3021) and /127 (IPv6, RFC 6164) are the point-to-point
    special case: both addresses in the prefix are valid host addresses
    (there is no network/broadcast address), so "usable" there means the
    two addresses themselves, in address order. This falls out of
    ``first_last_usable()`` automatically and needs no special-casing by
    callers.

Extension points for future work (intentionally not implemented yet,
since the concrete field names must come from what is actually modelled
in NetBox, not guessed):

  * IPv6 handoff math (``local_ip_v6``/``neighbor_ip_v6``) IS computed
    below, mirroring the v4 rule exactly, using ``handoff_addresses()``.
    It is populated in the data layer now; no template consumes it yet.
  * IPv6 DHCP relay server resolution IS computed below
    (``dhcp_servers_ipv6``), mirroring v4. Previously always empty.
  * Multicast: VRF-level ``mcast_base_group`` extraction is unchanged.
    All VRF custom-field reads are centralised in ``_vrf_fields()`` — a
    future field (e.g. an IPv6 multicast group, or an MDT range) is a
    one-line addition there, and does NOT require any change to the
    GraphQL query in 01_gather_data.yml: NetBox exposes custom fields as
    a single catch-all ``custom_field_data: JSON!`` blob, so any custom
    field defined in NetBox is already present in the fetched data the
    moment it is created — only the Python-side read needs to catch up.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # pragma: no cover - only missing outside an Ansible venv
    class AnsibleFilterError(Exception):
        pass


# ---------------------------------------------------------------------------
# IP math helpers
# ---------------------------------------------------------------------------

def _parse_network(prefix: str):
    """Parse a NetBox prefix string into an ipaddress network object."""
    try:
        return ipaddress.ip_network(prefix, strict=False)
    except ValueError as exc:
        raise AnsibleFilterError(f"netbox_evpn_data: invalid prefix {prefix!r}: {exc}") from exc


def network_and_mask(prefix: str) -> Tuple[str, str]:
    """Return (network_address, netmask) as strings for a v4 or v6 prefix.

    Mirrors ``prefix | ansible.utils.ipaddr('network')`` /
    ``prefix | ansible.utils.ipaddr('netmask')``.
    """
    net = _parse_network(prefix)
    return str(net.network_address), str(net.netmask)


def first_last_usable(prefix: str) -> Tuple[str, str]:
    """Return (first_usable, last_usable) host addresses for a prefix.

    /31 (v4) and /127 (v6) point-to-point links have no network/broadcast
    address — both addresses are usable, in order. Every other prefix
    excludes the network and broadcast/all-ones addresses.

    Computed with O(1) integer arithmetic on the network/broadcast
    address, NOT by enumerating the address range: a naive
    ``list(net.hosts())`` is fine for a /27 but never finishes for a /64
    or larger (2**64 addresses), which is exactly the prefix length used
    for the IPv6 VLAN subnets in this fabric.
    """
    net = _parse_network(prefix)
    if net.num_addresses == 1:
        addr = str(net.network_address)
        return addr, addr
    if (net.version == 4 and net.prefixlen == 31) or (net.version == 6 and net.prefixlen == 127):
        # RFC 3021 / RFC 6164: exactly two addresses, both are hosts.
        return str(net.network_address), str(net.broadcast_address)
    return str(net.network_address + 1), str(net.broadcast_address - 1)


def handoff_addresses(prefix: str) -> Tuple[str, str]:
    """Return (local_cidr, neighbor_addr) for an L3 handoff / P2P prefix.

    Rule: the lowest usable address is the remote/upstream neighbor; the
    highest usable address is the local switch SVI. ``local_cidr`` keeps
    the prefix length attached (e.g. ``10.67.118.7/31``) because it is
    rendered directly as an ``ip address`` / ``ipv6 address`` line;
    ``neighbor_addr`` is a bare address because it is used directly as a
    BGP neighbor identifier.
    """
    net = _parse_network(prefix)
    lo, hi = first_last_usable(prefix)
    local_cidr = f"{hi}/{net.prefixlen}"
    return local_cidr, lo


def _prefix_version(prefix: str) -> int:
    return _parse_network(prefix).version


# ---------------------------------------------------------------------------
# Core transform
# ---------------------------------------------------------------------------

_HANDOFF_ROLE = "vrf_l3_handoff_vlan"
_CORE_ROLE = "core_vlan"

# Overlay enrolment is OPT-IN: a VLAN becomes an EVPN segment only when NetBox
# says so explicitly. The empty string used to be a member of this tuple, which
# meant "no role set" defaulted to "stretch this VLAN across every leaf in
# scope" — so a VLAN added to NetBox purely to document IPAM (the Core-N-Underlay
# point-to-point VLANs, for instance) was silently deployed fabric-wide as an
# L2 overlay segment with its own EVPN instance and VNI. Additions are not
# caught by the deploy guard, which only scores removals, so nothing flagged it.
#
# This mirrors VRF enrolment, where the presence of an 'l3vni' custom field is
# what opts a VRF into the fabric (see _is_evpn_vrf): unmarked means not ours.
_EDGE_ROLES = ("edge",)

# Every role that produces configuration. A VLAN whose role is outside this set
# is reported in the 'skipped_vlans' result rather than silently ignored.
_KNOWN_ROLES = _EDGE_ROLES + (_CORE_ROLE, _HANDOFF_ROLE)

# A VRF's multicast index is derived as l3vni - DEFAULT_L3VNI_BASE, and selects
# its /16 inside the administratively scoped block: 239.<index>.0.0/16. See the
# address plan in the role README.
#
# Deriving it means no separate index field has to be kept in step with the
# L3VNI, and one number then identifies a VRF everywhere — l3vni 20003 gives RD
# and route-targets ":20003" and multicast block 239.3.0.0/16. The cost is that
# the usable L3VNI range narrows to base+1 .. base+254, since the index has to
# fit in an octet. That is 254 VRFs, against a campus needing tens.
#
# Deliberately NOT read from NetBox's VRF id: that is a database surrogate key
# which changes when an object is deleted and recreated or when an instance is
# rebuilt through the API, and it grows past 254 as objects churn. l3vni is
# operator-owned data and has none of those properties.
DEFAULT_L3VNI_BASE = 20000
MCAST_INDEX_MAX = 254
MCAST_VRF_BLOCK = "239.{index}.0.0/16"


def _mcast_index(l3vni: Optional[int], l3vni_base: int) -> Optional[int]:
    """A VRF's multicast index, or None if its L3VNI cannot yield a valid one."""
    if l3vni is None:
        return None
    index = l3vni - l3vni_base
    return index if 1 <= index <= MCAST_INDEX_MAX else None


# Tag marking a ROUTED-ONLY distributed anycast gateway. Such a segment gets
# EVPN proxy-ARP (ip proxy-arp / ip local-proxy-arp on the SVI), has the EVPN
# ARP-cache flooding suppression disabled on its EVI, and replicates BUM
# traffic head-end (ingress-replication) — so it needs no multicast group.
#
# Everything untagged is a BRIDGED DAG, which replicates via the underlay
# multicast tree and therefore DOES need a group.
_MCAST_TAG = "dag_routed"

# Administratively scoped IPv4 multicast, RFC 2365. Underlay replication groups
# must live here: outside it they collide with well-known/link-local groups
# (224.0.0.0/24), SSM (232.0.0.0/8, which this fabric enables via 'ip pim ssm
# default' and which cannot be used for ASM (*,G) replication), or GLOP space.
_ADMIN_SCOPED_MCAST = "239.0.0.0/8"


def _resolve_mcast_group(vlan: Dict[str, Any], vrf_group: str) -> str:
    """Effective underlay multicast group for one VLAN.

    A VLAN-level ``mcast_group`` custom field overrides the VRF-level
    ``mcast_base_group``, which otherwise applies to every bridged segment in
    that VRF.
    """
    cfd = vlan.get("custom_field_data") or {}
    override = cfd.get("mcast_group")
    if override not in (None, ""):
        return str(override).strip()
    return vrf_group or ""


def _replication_mode(tags: Iterable[str], mcast_group: str, vrf_name: str) -> str:
    """BUM replication mode for one edge VLAN: ``ingress`` or ``static``.

    Decided here rather than in Jinja so the EVI ``replication-type`` and the
    NVE member line cannot disagree — they were previously chosen by two
    separate template branches and ended up paired back to front.

      * routed-only DAG (``dag_routed``): head-end replication, no group.
      * a group is available (VLAN override, or the VRF's base): multicast.
      * no group but the VLAN IS in a VRF: still multicast, and validation
        rejects it — a VRF that carries bridged segments must define a base
        group, and silently downgrading to ingress would hide that.
      * no group and no VRF: a VRF-less L2-only segment has nothing to inherit
        from, so head-end replication is the only sensible default.
    """
    if _MCAST_TAG in (tags or []):
        return "ingress"
    if mcast_group:
        return "static"
    if vrf_name:
        return "static"
    return "ingress"


def _mcast_group_problem(group: str) -> Optional[str]:
    """Why ``group`` is unusable as an underlay replication group, or None."""
    try:
        addr = ipaddress.ip_address(group)
    except ValueError:
        return "is not a valid IP address"
    if addr.version != 4:
        return "is not IPv4 (underlay replication groups must be IPv4)"
    if not addr.is_multicast:
        return "is not a multicast address"
    if addr not in ipaddress.ip_network(_ADMIN_SCOPED_MCAST):
        return (
            "is outside the administratively scoped range {} (RFC 2365) — "
            "well-known, SSM and GLOP ranges cannot be used for ASM underlay "
            "replication".format(_ADMIN_SCOPED_MCAST)
        )
    return None


def _containing_prefix(group: str, prefixes: Iterable[Dict[str, Any]]) -> Optional[str]:
    """First prefix in ``prefixes`` that contains ``group``, or None."""
    try:
        addr = ipaddress.ip_address(group)
    except ValueError:
        return None
    for entry in prefixes or []:
        cidr = entry.get("prefix")
        if not cidr:
            continue
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return cidr
        except ValueError:
            continue
    return None


def _validate_multicast_groups(
    all_edge_vlans: Dict[str, Any],
    reserved_prefixes: Optional[Iterable[Dict[str, Any]]] = None,
    allowed_prefixes: Optional[Iterable[Dict[str, Any]]] = None,
    l3vni_base: int = DEFAULT_L3VNI_BASE,
) -> None:
    """Validate underlay multicast replication groups.

    Rules, all of which fail the render rather than the switch:

      * every BRIDGED segment (no ``dag_routed`` tag) resolves to a group —
        otherwise the template emits a bare ``member vni <n> mcast-group`` and
        IOS-XE rejects it as ``% Incomplete command``;
      * every group is an administratively scoped IPv4 multicast address;
      * no two distinct owners claim the same group. VLANs inheriting one VRF's
        base group legitimately share it, so ownership is per-VRF for inherited
        groups and per-VLAN for overrides;
      * no group falls inside a prefix reserved for multicast infrastructure
        (``default_mdt`` / ``data_mdt``), which lives in the same shared
        underlay irrespective of VRF.

    Checked against the in-scope VLANs: unlike VNI uniqueness, a missing group
    is a local problem that matters exactly where the segment is configured.
    """
    errors: List[str] = []
    owners: Dict[str, str] = {}

    for vid in sorted(all_edge_vlans, key=lambda v: int(v)):
        v_data = all_edge_vlans[vid]
        name = v_data.get("name")
        group = v_data.get("mcast_group") or ""

        if v_data.get("replication_mode") != "static":
            # Head-end replication: routed-only DAG, or a VRF-less L2-only
            # segment with nothing to inherit. Neither needs a group.
            continue

        if not group:
            errors.append(
                "VLAN {} '{}' is a bridged segment in VRF '{}', so it replicates via the "
                "underlay multicast tree, but that VRF has no 'mcast_base_group' set and "
                "the VLAN has no 'mcast_group' override. Set a base group on the VRF, "
                "override it on this VLAN, or tag the VLAN '{}' to use ingress "
                "replication".format(vid, name, v_data.get("vrf_name"), _MCAST_TAG)
            )
            continue

        problem = _mcast_group_problem(group)
        if problem:
            errors.append(
                "VLAN {} '{}' resolves to multicast group '{}', which {}".format(
                    vid, name, group, problem)
            )
            continue

        # Reserved ranges are tested BEFORE containment: both would reject a
        # group sitting in an MDT range, but "that is the default_mdt range"
        # tells the operator what to do, while "not inside an allowed prefix"
        # only tells them something is wrong.
        reserved_hit = _containing_prefix(group, reserved_prefixes or [])
        if reserved_hit:
            role = next((p.get("role") or "reserved"
                         for p in reserved_prefixes if p.get("prefix") == reserved_hit),
                        "reserved")
            errors.append(
                "VLAN {} '{}' resolves to multicast group {}, which falls inside {}, a "
                "prefix reserved for '{}'. These ranges share one underlay, so they must "
                "not overlap regardless of VRF".format(
                    vid, name, group, reserved_hit, role)
            )
            continue

        # Containment against the carve held in NetBox IPAM. Skipped entirely
        # when no prefix carries an allowed role — otherwise a fabric that has
        # not adopted the carve yet would fail every VLAN. The 239/8 floor
        # check above still applies in that case.
        if allowed_prefixes and not _containing_prefix(group, allowed_prefixes):
            errors.append(
                "VLAN {} '{}' resolves to multicast group {}, which is not inside any "
                "prefix reserved for L2VNI replication in NetBox IPAM ({}). Allocate it "
                "from the carve, or add the range to IPAM".format(
                    vid, name, group,
                    ", ".join(sorted(p["prefix"] for p in allowed_prefixes if p.get("prefix")))
                    or "none defined")
            )
            continue

        # Each VRF owns one /16, selected by its multicast index, so a group
        # must sit in its OWN block rather than merely somewhere in the carve.
        # Without this a VRF could quietly allocate out of another's range: the
        # uniqueness check below would still pass, and only the per-VRF
        # legibility the address plan exists for would be lost.
        #
        # Gated on the carve being present in IPAM, exactly as the containment
        # check above is. Both enforce the same address plan, so a fabric that
        # has not adopted it should not be held to half of it.
        index = _mcast_index(v_data.get("vrf_l3vni"), l3vni_base)
        if allowed_prefixes and index is not None:
            block = MCAST_VRF_BLOCK.format(index=index)
            if not _containing_prefix(group, [{"prefix": block}]):
                errors.append(
                    "VLAN {} '{}' resolves to multicast group {}, which is outside {}, "
                    "the block belonging to VRF '{}' (L3VNI {} -> index {})".format(
                        vid, name, group, block, v_data.get("vrf_name"),
                        v_data.get("vrf_l3vni"), index)
                )
                continue

        # Inherited groups are owned by the VRF (many VLANs legitimately share
        # one); an override is owned by the VLAN itself.
        owner = ("VLAN {} '{}'".format(vid, name)
                 if v_data.get("mcast_group_is_override")
                 else "VRF '{}'".format(v_data.get("vrf_name") or "<none>"))
        previous = owners.get(group)
        if previous is not None and previous != owner:
            errors.append(
                "multicast group {} is claimed by both {} and {} — distinct segments "
                "must replicate to distinct groups".format(group, previous, owner)
            )
        else:
            owners[group] = owner

    if errors:
        raise AnsibleFilterError(
            "EVPN multicast validation failed ({} problem{}):\n  - {}".format(
                len(errors), "" if len(errors) == 1 else "s", "\n  - ".join(errors)
            )
        )


# ---------------------------------------------------------------------------
# VNI allocation
# ---------------------------------------------------------------------------
#
# The VNI namespace is 24-bit. The floor of 4096 exists so a VNI can never be
# confused with an 802.1Q VLAN ID (1..4094) when eyeballing a config.
VNI_MIN = 4096
VNI_MAX = 16777215

# A Type-1 route distinguisher ("rd <ipv4-address>:<nn>") carries nn in a
# 16-bit field. common_base.j2 renders "rd {{ loopback0_ip }}:{{ l3vni }}",
# so an L3VNI drives the RD as well as the NVE member VNI and must therefore
# fit in 65535 — a tighter bound than VNI_MAX. L2VNIs never appear in an RD
# and so get the full 24-bit range.
RD_TYPE1_MAX = 65535

# L2VNI = DEFAULT_L2VNI_BASE + VLAN ID, giving 10001..14094 for the full
# 1..4094 VLAN space. Overridable per-role via evpn_l2vni_base.
DEFAULT_L2VNI_BASE = 10000


# ---------------------------------------------------------------------------
# TRM (tenant routed multicast)
# ---------------------------------------------------------------------------
#
# A fabric VRF is multicast-enabled by DATA, not a flag: an ACTIVE prefix with
# role 'default_mdt' inside that VRF's own 239.<index>.0.0/16 block. The carve
# reserves those ranges for every VRF, so status is what separates "planned" from
# "in use" -- creating the range is a plan, activating it is the decision.
#
# RP mode is derived the same way, from IPAM objects tagged in the tenant VRF:
#
#   trm_anycast_rp      -> anycast RP shared by every VTEP; ASM stays inside the
#                          fabric, and BGP MVPN source-active routes do the job
#                          MSDP would otherwise do, so no MSDP is configured.
#   trm_external_rp     -> an RP outside the fabric; each VTEP needs a UNIQUE
#                          register source, allocated by trm_allocate.yml.
#
# Both present is a data error: they are mutually exclusive per VRF.
TRM_TAG_ANYCAST_RP = "trm_anycast_rp"
TRM_TAG_EXTERNAL_RP = "trm_external_rp"
TRM_TAG_REGISTER_SOURCE = "trm_register_source"

# Tag on an 'edge' VLAN whose SVI needs 'ip pim sparse-mode' + 'ip igmp
# version 3'. Independent of dag_routed: a routed-only DAG segment still has
# hosts that can be multicast receivers, and its L3 multicast rides the MDT
# rather than the L2VNI's ingress replication.
TRM_TAG_MCAST_VLAN = "multicast_enabled"

MDT_DEFAULT_ROLE = "default_mdt"
MDT_DATA_ROLE = "data_mdt"
STATUS_ACTIVE = "active"


def _mdt_prefixes_for_index(prefixes, index, role):
    """ACTIVE prefixes with ``role`` inside 239.<index>.0.0/16."""
    if index is None:
        return []
    try:
        block = ipaddress.ip_network(MCAST_VRF_BLOCK.format(index=index))
    except ValueError:
        return []
    out = []
    for p in prefixes or []:
        if (p.get("role") or "") != role:
            continue
        if (p.get("status") or "").lower() != STATUS_ACTIVE:
            continue
        try:
            net = ipaddress.ip_network(p.get("prefix", ""), strict=False)
        except ValueError:
            continue
        if net.version != 4 or not net.subnet_of(block):
            continue
        out.append(net)
    return sorted(out, key=lambda n: (n.network_address, n.prefixlen))


def _trm_objects_for_vrf(trm_objects, vrf_name, tag):
    objs = (trm_objects or {}).get("addresses") or []
    return [o for o in objs
            if (o.get("vrf") or "") == vrf_name and tag in (o.get("tags") or [])]


def _fabric_mcast_vlan_vrfs(vlan_list) -> set:
    """VRF names having at least one edge VLAN tagged multicast_enabled,
    across the WHOLE fabric rather than the rendering host's scope."""
    out = set()
    for vlan in vlan_list or []:
        tags = {t.get("slug") for t in (vlan.get("tags") or []) if t}
        if TRM_TAG_MCAST_VLAN not in tags:
            continue
        role = ((vlan.get("role") or {}).get("slug") or "")
        if role not in _EDGE_ROLES:
            continue
        for pfx in (vlan.get("prefixes") or []):
            vrf = (pfx.get("vrf") or {}).get("name")
            if vrf:
                out.add(vrf)
    return out


def _build_trm(vrf_name, l3vni, l3vni_base, mdt_prefixes, trm_objects,
               hostname, has_mcast_vlans, errors, require_register_source=True):
    """Resolve one VRF's TRM configuration, appending any problems to errors.

    Returns a dict merged into the VRF entry. ``enabled`` False means the
    templates render nothing multicast for this VRF.
    """
    index = _mcast_index(l3vni, l3vni_base)
    defaults = _mdt_prefixes_for_index(mdt_prefixes, index, MDT_DEFAULT_ROLE)
    off = {"enabled": False}

    if not defaults:
        if has_mcast_vlans:
            errors.append(
                f"VRF '{vrf_name}' has VLAN(s) tagged '{TRM_TAG_MCAST_VLAN}' but is "
                f"not multicast-enabled: no ACTIVE prefix with role "
                f"'{MDT_DEFAULT_ROLE}' inside {MCAST_VRF_BLOCK.format(index=index)}. "
                f"Set that prefix to Active in NetBox, or remove the tag.")
        return off

    if len(defaults) > 1:
        errors.append(
            f"VRF '{vrf_name}' has {len(defaults)} ACTIVE '{MDT_DEFAULT_ROLE}' "
            f"prefixes ({', '.join(str(n) for n in defaults)}); exactly one is "
            f"required. Set the others to Reserved.")
        return off

    default_net = defaults[0]
    trm = {
        "enabled": True,
        # The group is the first address of the range, matching how edge SVIs
        # take the first usable address of their prefix.
        "mdt_default_group": str(default_net.network_address + 1),
        "has_mcast_vlans": has_mcast_vlans,
    }

    data = _mdt_prefixes_for_index(mdt_prefixes, index, MDT_DATA_ROLE)
    if len(data) > 1:
        errors.append(
            f"VRF '{vrf_name}' has {len(data)} ACTIVE '{MDT_DATA_ROLE}' prefixes; "
            f"at most one is allowed.")
    elif data:
        trm["mdt_data_network"] = str(data[0].network_address)
        trm["mdt_data_wildcard"] = str(ipaddress.ip_address(
            int(data[0].hostmask)))

    anycast = _trm_objects_for_vrf(trm_objects, vrf_name, TRM_TAG_ANYCAST_RP)
    external = _trm_objects_for_vrf(trm_objects, vrf_name, TRM_TAG_EXTERNAL_RP)

    if anycast and external:
        errors.append(
            f"VRF '{vrf_name}' has BOTH an '{TRM_TAG_ANYCAST_RP}' and an "
            f"'{TRM_TAG_EXTERNAL_RP}' address; the modes are mutually exclusive. "
            f"Anycast keeps ASM inside the fabric; external extends it outward.")
        return off

    if len(anycast) > 1:
        errors.append(
            f"VRF '{vrf_name}' has {len(anycast)} addresses tagged "
            f"'{TRM_TAG_ANYCAST_RP}'; exactly one is required.")
        return off

    if anycast:
        trm["rp_mode"] = "anycast"
        trm["anycast_rp"] = anycast[0]["address"].split("/")[0]
    elif external:
        trm["rp_mode"] = "external"
        trm["external_rps"] = sorted(o["address"].split("/")[0] for o in external)
        # Ownership is the interface the address is assigned to, which is how
        # NetBox natively binds an address to a device. Tags stay what they are
        # meant to be -- classification ('this is a register source') -- rather
        # than 500 per-device tags standing in for identity.
        mine = [o for o in _trm_objects_for_vrf(trm_objects, vrf_name,
                                                TRM_TAG_REGISTER_SOURCE)
                if hostname and o.get("device") == hostname]
        if len(mine) > 1:
            errors.append(
                f"VRF '{vrf_name}': {hostname} has {len(mine)} addresses tagged "
                f"'{TRM_TAG_REGISTER_SOURCE}'; exactly one is required.")
            return off
        if not mine:
            # trm_allocate.yml is the one caller that legitimately sees this
            # state -- it exists to fix it, so it must not be blocked by it.
            # Everything else fails: rendering 'register-source' for an address
            # that does not exist would silently break PIM registration.
            if require_register_source:
                errors.append(
                    f"VRF '{vrf_name}' uses an external RP, so "
                    f"{hostname or 'this VTEP'} needs a unique PIM register "
                    f"source, and none is allocated to it. Run 'ansible-playbook "
                    f"trm_allocate.yml' (it allocates from the prefix tagged "
                    f"'{TRM_TAG_REGISTER_SOURCE}' in that VRF, borders first). "
                    f"If no such pool exists in NetBox, create one first.")
                return off
            trm["needs_register_source"] = True
        else:
            trm["register_source"] = mine[0]["address"].split("/")[0]
    else:
        # SSM only: 'ip pim vrf X ssm default' still renders, so 232/8 works
        # without any RP. Only ASM needs one.
        trm["rp_mode"] = "ssm-only"

    return trm


def _coerce_vni(value: Any) -> Optional[int]:
    """Normalise a NetBox custom-field value to int|None.

    An Integer custom field arrives as an int, but the same field typed as
    text (or left blank, or absent entirely) can arrive as ""/None/a string.
    Anything non-numeric becomes None and is reported by validation rather
    than silently coerced to a wrong number.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _vlan_l2vni(vlan: Dict[str, Any], vid: Any, l2vni_base: int) -> Optional[int]:
    """L2VNI for a VLAN: derived from the VLAN ID, or an explicit override.

    Derivation is safe because VLAN IDs are unique across this fabric, so a
    function of the VLAN ID is unique too. The override exists for brownfield
    segments that already carry a VNI which does not fit the formula; when
    set it wins, and validation still range-checks it and tests it for
    collisions against every other VNI in the fabric.
    """
    cfd = vlan.get("custom_field_data") or {}
    raw = cfd.get("l2vni")
    if raw is None or raw == "":
        return l2vni_base + int(vid)
    return _coerce_vni(raw)


def _l2vni_explicit(vlan: Dict[str, Any]) -> bool:
    """True when the VLAN carries an explicit ``l2vni`` custom field.

    The field means two different things depending on context, which is why
    this is asked separately from ``_vlan_l2vni``:

      * inside a VRF it is a brownfield OVERRIDE of the derived VNI;
      * with no VRF it is the ENROLMENT MARKER -- it is what makes the segment
        an L2-only overlay rather than a VLAN NetBox merely happens to hold.

    Enrolment is deliberately an act, not the absence of one. A VRF-less edge
    VLAN with no l2vni used to render anyway on a derived VNI, so a
    half-modelled VLAN reached a switch looking finished.
    """
    raw = (vlan.get("custom_field_data") or {}).get("l2vni")
    return raw is not None and raw != ""


def _validate_l2_only(candidates: List[Dict[str, Any]]) -> None:
    """Validate L2-only overlay segments. Every failure here fails the render.

    An L2-only segment has no VRF, so nothing upstream supplies the two things
    it still needs -- a VNI and a BUM replication group. Both must be stated on
    the VLAN itself, and neither has a safe default:

      * no ``l2vni`` -> not enrolled. Rendering it on a derived VNI is how a
        VLAN that was never meant to be an overlay reaches a switch.
      * no ``mcast_group`` -> there is no VRF base group to inherit, so the
        segment would silently fall back to head-end replication. That is a
        different forwarding design arrived at by omission.
      * a prefix -> an L2-only segment is bridged, with no SVI and no gateway.
        The address renders nowhere, so carrying one means the VLAN was modelled
        as routed and the operator will look for a gateway that does not exist.
    """
    errors: List[str] = []
    for c in sorted(candidates, key=lambda x: int(x["vid"])):
        vid, name, why = c["vid"], c["name"], c["why"]
        if why == "unenrolled":
            errors.append(
                "VLAN {} '{}' has role 'edge' and no VRF, but no 'l2vni' custom "
                "field. Set 'l2vni' to enrol it as an L2-only overlay, or give it "
                "a VRF, or change its role -- it is currently neither."
                .format(vid, name))
        elif why == "no_mcast_group":
            errors.append(
                "VLAN {} '{}' is an L2-only overlay, so it has no VRF base group "
                "to inherit: set the 'mcast_group' custom field to its BUM "
                "replication group."
                .format(vid, name))
        elif why == "has_prefix":
            errors.append(
                "VLAN {} '{}' is an L2-only overlay but has prefix {} assigned. "
                "L2-only segments are bridged and render no SVI, so the address "
                "would be silently ignored. Remove the prefix, or give the VLAN a "
                "VRF so it becomes a routed segment."
                .format(vid, name, c.get("prefix")))
    if errors:
        raise AnsibleFilterError(
            "netbox_evpn_data: L2-only overlay validation failed:\n  - "
            + "\n  - ".join(errors))


# Longest VLAN name this fabric permits. IOS-XE itself accepts 32, so this
# leaves a little headroom rather than sitting on the platform ceiling. The
# limit is quoted verbatim in the error below, so the number and the message
# cannot drift apart.
MAX_VLAN_NAME_LEN = 30


def _vlan_name_problem(name: str) -> Optional[str]:
    """Why ``name`` cannot be an IOS-XE VLAN name, or None.

    NetBox accepts anything, IOS-XE does not, and the two disagree silently.
    A name containing a space renders as ``name L2 only overlay for IoT``, which
    the parser reads as ``name L2`` before rejecting the remainder -- so the
    candidate fails at ``acm replace ... validate``, after being copied to every
    switch, with a parser error that never mentions NetBox or the VLAN.
    """
    if name is None or name == "":
        return "is empty"
    if any(c.isspace() for c in name):
        return ("contains whitespace. IOS-XE cannot parse it: 'name {}' is read "
                "as 'name {}' and the rest is rejected".format(name, name.split()[0]))
    if len(name) > MAX_VLAN_NAME_LEN:
        return "is {} characters; the maximum is {}".format(len(name), MAX_VLAN_NAME_LEN)
    return None


def _validate_vlan_names(candidates: List[Dict[str, Any]]) -> None:
    """Reject VLAN names IOS-XE cannot accept, before anything is rendered.

    Checked here rather than in the template because a template cannot fail
    usefully -- it would emit the bad line and leave the switch to complain.
    """
    errors: List[str] = []
    for c in sorted(candidates, key=lambda x: int(x["vid"])):
        problem = _vlan_name_problem(c["name"])
        if problem:
            errors.append(
                "VLAN {} '{}' {}. Rename it in NetBox: VLAN names must be at most "
                "{} characters and contain no whitespace."
                .format(c["vid"], c["name"], problem, MAX_VLAN_NAME_LEN))
    if errors:
        raise AnsibleFilterError(
            "netbox_evpn_data: VLAN name validation failed ({} problem{}):\n  - ".format(
                len(errors), "" if len(errors) == 1 else "s")
            + "\n  - ".join(errors))


def _validate_fabric_vnis(
    l3_by_vrf: Dict[str, int],
    l3_bad_by_vrf: Dict[str, Any],
    l2_by_vid: Dict[int, Tuple[Optional[int], str]],
    l3vni_base: int = DEFAULT_L3VNI_BASE,
) -> None:
    """Validate the whole fabric's VNI plan, raising on any problem.

    This deliberately runs against every VRF/VLAN in the NetBox dataset, not
    just the subset in scope for the rendering host: a collision between two
    buildings must fail every host's render, not only the render of a host
    unlucky enough to see both halves of it.

    A VRF with no l3vni at all is not an error — it is simply not part of the
    EVPN fabric (see _is_evpn_vrf). Only a VRF whose l3vni is *set but
    unusable* is reported, via l3_bad_by_vrf.

    All problems are collected and reported together — finding out about six
    bad VNIs one deploy at a time is not a useful workflow.
    """
    errors: List[str] = []
    owner: Dict[int, str] = {}

    def claim(vni: int, what: str) -> None:
        if vni in owner:
            errors.append(f"VNI {vni} is claimed by both {owner[vni]} and {what}")
        else:
            owner[vni] = what

    for vrf_name in sorted(l3_bad_by_vrf):
        errors.append(
            f"VRF '{vrf_name}' has an 'l3vni' custom field set to "
            f"{l3_bad_by_vrf[vrf_name]!r}, which is not a valid integer. Set a number "
            f"to enrol this VRF in the EVPN fabric, or clear the field to exclude it"
        )

    for vrf_name in sorted(l3_by_vrf):
        vni = l3_by_vrf[vrf_name]
        if not (VNI_MIN <= vni <= RD_TYPE1_MAX):
            errors.append(
                f"VRF '{vrf_name}' has L3VNI {vni}, outside the permitted range "
                f"{VNI_MIN}..{RD_TYPE1_MAX} (the ceiling is {RD_TYPE1_MAX} rather than "
                f"{VNI_MAX} because the L3VNI also renders into 'rd <loopback>:<nn>', "
                f"whose nn field is 16-bit)"
            )
            continue
        if _mcast_index(vni, l3vni_base) is None:
            errors.append(
                "VRF '{}' has L3VNI {}, which gives multicast index {} — outside "
                "1..{}. The index selects the VRF's multicast block "
                "({}), so L3VNIs must be allocated from {}..{}".format(
                    vrf_name, vni, vni - l3vni_base, MCAST_INDEX_MAX,
                    MCAST_VRF_BLOCK.format(index="<index>"),
                    l3vni_base + 1, l3vni_base + MCAST_INDEX_MAX)
            )
            continue
        claim(vni, f"VRF '{vrf_name}' (L3VNI)")

    for vid in sorted(l2_by_vid):
        vni, name = l2_by_vid[vid]
        what = f"VLAN {vid} '{name}' (L2VNI)"
        if vni is None:
            errors.append(
                f"VLAN {vid} '{name}' has an 'l2vni' override in NetBox that is not a "
                f"valid integer. Clear it to use the derived value, or set a number"
            )
            continue
        if not (VNI_MIN <= vni <= VNI_MAX):
            errors.append(
                f"VLAN {vid} '{name}' resolves to L2VNI {vni}, outside the valid VNI "
                f"range {VNI_MIN}..{VNI_MAX}"
            )
            continue
        claim(vni, what)

    if errors:
        raise AnsibleFilterError(
            "EVPN VNI validation failed ({} problem{}):\n  - {}".format(
                len(errors), "" if len(errors) == 1 else "s", "\n  - ".join(errors)
            )
        )


def _build_ip_map(ip_list: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """id (str) -> bare host address (str), stripped of any /prefixlen.

    NetBox always returns IPAddress.address with a prefix length attached
    (e.g. "10.66.119.56/32"); downstream consumers (DHCP helper-address /
    relay-destination lines) want the bare address only.
    """
    ip_map: Dict[str, str] = {}
    for ip in ip_list or []:
        addr = ip.get("address")
        if not addr:
            continue
        ip_map[str(ip["id"])] = str(ipaddress.ip_interface(addr).ip)
    return ip_map


def _resolve_dhcp_ips(cfd: Optional[Dict[str, Any]], ip_map: Dict[str, str], key: str) -> List[str]:
    """Resolve a list of NetBox IPAddress object IDs (from a prefix custom
    field) to their bare addresses, de-duplicated, order preserved, and
    silently skipping any ID that doesn't resolve (e.g. a stale reference
    to a deleted IP object) — matches prior template behaviour.
    """
    ids = (cfd or {}).get(key) or []
    out: List[str] = []
    for ip_id in ids:
        addr = ip_map.get(str(ip_id))
        if addr and addr not in out:
            out.append(addr)
    return out


def _new_vlan_record(vlan: Dict[str, Any], role: str, scope_type: Optional[str], scope_id: Optional[str],
                      site_id: Optional[str]) -> Dict[str, Any]:
    return {
        "vid": vlan["vid"],
        "name": vlan["name"],
        "role": role,
        "tags": [t["slug"] for t in (vlan.get("tags") or [])],
        # scope_type/scope_id are informational only (not used for scope
        # matching below, which uses the raw, non-defaulted values) — kept
        # for parity with the previous template's output shape.
        "scope_type": scope_type if scope_type else ("site" if site_id else "global"),
        "scope_id": scope_id if scope_id else (site_id if site_id else "none"),
        "prefix_ipv4": "", "network_ipv4": "", "netmask_ipv4": "",
        "local_ip_v4": "", "neighbor_ip_v4": "",
        "prefix_ipv6": "", "network_ipv6": "", "netmask_ipv6": "",
        "local_ip_v6": "", "neighbor_ip_v6": "",
        "vrf_name": "", "vrf_rd": "", "vrf_mcast_group": "",
        "vrf_l3vni": None, "vrf_l3vni_raw": None,
        # Effective underlay replication group: the VLAN's own 'mcast_group'
        # custom field if set, otherwise the VRF's 'mcast_base_group'.
        "mcast_group": "", "mcast_group_is_override": False,
        # "ingress" or "static"; drives both the EVI replication-type and the
        # NVE member line so the two can never disagree.
        "replication_mode": "ingress",
        # Populated for edge VLANs only — core VLANs carry their VRF's L3VNI
        # and handoff VLANs carry no VNI at all.
        "l2vni": None,
        "dhcp_servers_ipv4": [],
        "dhcp_servers_ipv6": [],
    }


def _vrf_fields(vrf: Dict[str, Any]) -> Dict[str, str]:
    """Centralised read of VRF-level data (name, RD, custom fields).

    All VRF custom-field extraction should happen here so future fields
    (e.g. a v6 multicast group, an MDT range) are a one-line addition.
    """
    cfd = vrf.get("custom_field_data") or {}
    return {
        "name": vrf.get("name") or "",
        "rd": vrf.get("rd") or "",
        "mcast_group": cfd.get("mcast_base_group") or "",
        # L3VNI is explicit (never derived): it is unique per VRF, must be
        # identical on every VTEP carrying that VRF, and drives the RD and
        # route-targets as well as the NVE member VNI. Its presence is also
        # what enrols the VRF in the fabric at all -- see _is_evpn_vrf.
        "l3vni": _coerce_vni(cfd.get("l3vni")),
        # Raw value retained so a set-but-unparseable l3vni ("2O199") can be
        # reported as an error instead of being silently read as "not enrolled".
        "l3vni_raw": cfd.get("l3vni"),
    }


def _apply_prefix(v_data: Dict[str, Any], p: Dict[str, Any], role: str, ip_map: Dict[str, str]) -> None:
    """Fold one prefix's data into a VLAN record (v_data), in place."""
    prefix = p.get("prefix")
    if not prefix:
        return
    version = _prefix_version(prefix)
    cfd = p.get("custom_field_data") or {}

    if version == 4:
        network, netmask = network_and_mask(prefix)
        v_data["prefix_ipv4"] = prefix
        v_data["network_ipv4"] = network
        v_data["netmask_ipv4"] = netmask
        if role == _HANDOFF_ROLE:
            local_cidr, neighbor = handoff_addresses(prefix)
            v_data["local_ip_v4"] = local_cidr
            v_data["neighbor_ip_v4"] = neighbor
        v_data["dhcp_servers_ipv4"] = _merge_unique(
            v_data["dhcp_servers_ipv4"], _resolve_dhcp_ips(cfd, ip_map, "dhcp_servers_ipv4")
        )
    elif version == 6:
        network, netmask = network_and_mask(prefix)
        v_data["prefix_ipv6"] = prefix
        v_data["network_ipv6"] = network
        v_data["netmask_ipv6"] = netmask
        if role == _HANDOFF_ROLE:
            local_cidr, neighbor = handoff_addresses(prefix)
            v_data["local_ip_v6"] = local_cidr
            v_data["neighbor_ip_v6"] = neighbor
        v_data["dhcp_servers_ipv6"] = _merge_unique(
            v_data["dhcp_servers_ipv6"], _resolve_dhcp_ips(cfd, ip_map, "dhcp_servers_ipv6")
        )

    vrf = p.get("vrf")
    if vrf and not v_data["vrf_name"]:
        fields = _vrf_fields(vrf)
        v_data["vrf_name"] = fields["name"]
        v_data["vrf_rd"] = fields["rd"]
        v_data["vrf_mcast_group"] = fields["mcast_group"]
        v_data["vrf_l3vni"] = fields["l3vni"]
        v_data["vrf_l3vni_raw"] = fields["l3vni_raw"]


def _merge_unique(existing: List[str], new: List[str]) -> List[str]:
    out = list(existing)
    for item in new:
        if item not in out:
            out.append(item)
    return out


def _is_evpn_vrf(v_data: Dict[str, Any]) -> bool:
    """Is this VLAN's VRF enrolled in the EVPN fabric?

    Enrolment is opt-in and marked solely by the presence of a valid ``l3vni``
    custom field on the VRF in NetBox. NetBox routinely holds VRFs that have
    nothing to do with this fabric (WAN, out-of-band, other platforms); those
    simply have no l3vni and are ignored here rather than treated as an error.

    Note this deliberately does NOT consider the VRF's RD. The RD used on the
    device is generated as ``<loopback>:<l3vni>``, so NetBox's rd field has no
    bearing on whether or how a VRF is provisioned.
    """
    return v_data.get("vrf_l3vni") is not None


def build_evpn_data(
    vlan_list: List[Dict[str, Any]],
    ip_list: List[Dict[str, Any]],
    my_site: Optional[str],
    my_sg: Optional[str],
    l2vni_base: int = DEFAULT_L2VNI_BASE,
    reserved_mcast_prefixes: Optional[List[Dict[str, Any]]] = None,
    allowed_mcast_prefixes: Optional[List[Dict[str, Any]]] = None,
    l3vni_base: int = DEFAULT_L3VNI_BASE,
    trm_objects: Optional[Dict[str, Any]] = None,
    hostname: Optional[str] = None,
    require_register_source: bool = True,
) -> Dict[str, Any]:
    """Transform NetBox VLAN/prefix/VRF data into the acm_evpn fabric structure.

    Args:
        vlan_list: ``graphql.data.vlan_list`` — every VLAN in the fabric,
            each with its role, tags, site, group/scope, and prefixes
            (each prefix carrying its own custom_field_data and VRF).
        ip_list: IP-address objects referenced as DHCP relay targets,
            each ``{"id": ..., "address": "x.x.x.x/nn"}``. Callers should
            fetch only the IDs actually referenced (see
            ``01_gather_data.yml``) rather than the whole IPAM table.
        my_site: string ID of the rendering host's site, or None.
        my_sg: string ID of the rendering host's site group, or None.

    Returns:
        {
          "vrfs": {vrf_name: {"rd", "mcast_group",
                               "core_vlans", "edge_vlans", "handoff_vlans"}},
          "l2_vlans": {vid: v_data},        # VLANs with no VRF at all
          "l2_only_vlans": {vid: v_data},   # of those, the ENROLLED L2-only
                                            # overlays (explicit l2vni, a
                                            # mcast_group, and no prefix)
          "all_edge_vlans": {vid: v_data},  # edge VLANs, VRF or not, in scope for this host
          "skipped_vlans": {vid: {"vid", "name", "role", "vrf_name"}},
                                             # in scope for this host but
                                             # carrying no recognised role, so
                                             # producing no configuration at
                                             # all. Reported rather than
                                             # silently dropped.
          "fabric_edge_subnets": {vrf_name: {vid: {"network", "mask"}}},
                                             # ALL edge subnets fabric-wide,
                                             # ignoring site scope — used by
                                             # border switches to redistribute
                                             # every VRF subnet regardless of
                                             # which building it lives in.
        }

    Every VLAN is included in ``fabric_edge_subnets`` fabric-wide
    (independent of site scope) but only included in ``vrfs``/``l2_vlans``/
    ``all_edge_vlans`` if it is in scope for this host: globally scoped
    (no site and no group scope at all), directly assigned to this site,
    or reachable via its VLAN group's scope (a site-group scope matching
    this host's site group, or a site scope matching this host's site).

    Note: a VLAN group scoped to a Region/Location/Rack/Cluster/
    ClusterGroup (rather than a Site or SiteGroup) is not matched by any
    of the site/site-group checks above and about will not appear for any
    host unless the VLAN also has a direct ``site`` assignment. This is
    pre-existing behaviour carried over unchanged from the prior
    template; flagged here rather than silently changed.
    """
    ip_map = _build_ip_map(ip_list)

    vrfs: Dict[str, Any] = {}
    l2_vlans: Dict[str, Any] = {}
    # Enrolled L2-only overlays. Kept separate from l2_vlans, which stays
    # 'every in-scope VLAN with no VRF' -- borders render from this one, so it
    # must contain only segments that are actually fabric overlays.
    l2_only_vlans: Dict[str, Any] = {}
    l2_only_bad: List[Dict[str, Any]] = []
    # Every in-scope VLAN with a recognised role has its name rendered as
    # 'vlan <id> / name <name>', so every one of them is checked.
    named_vlans: List[Dict[str, Any]] = []
    all_edge_vlans: Dict[str, Any] = {}
    fabric_edge_subnets: Dict[str, Any] = {}
    skipped_vlans: Dict[str, Any] = {}

    # Fabric-wide VNI registries, populated before the per-host scope filter
    # so validation sees the whole fabric on every render (see
    # _validate_fabric_vnis).
    fabric_l3: Dict[str, int] = {}
    fabric_l3_bad: Dict[str, Any] = {}
    fabric_l2: Dict[int, Tuple[Optional[int], str]] = {}

    for vlan in vlan_list:
        site = vlan.get("site")
        site_id = str(site["id"]) if site else None

        group = vlan.get("group")
        scope = group.get("scope") if group else None
        scope_type = scope["__typename"].replace("Type", "").lower() if scope else None
        scope_id = str(scope["id"]) if scope else None

        role = vlan["role"]["slug"] if vlan.get("role") else ""
        vid_str = str(vlan["vid"])

        v_data = _new_vlan_record(vlan, role, scope_type, scope_id, site_id)

        for p in vlan.get("prefixes") or []:
            _apply_prefix(v_data, p, role, ip_map)

        # Resolved after prefixes, since the VRF-level base group only becomes
        # known once a prefix has contributed its VRF.
        _vlan_override = ((vlan.get("custom_field_data") or {}).get("mcast_group")
                          not in (None, ""))
        v_data["mcast_group"] = _resolve_mcast_group(vlan, v_data["vrf_mcast_group"])
        v_data["mcast_group_is_override"] = _vlan_override
        v_data["replication_mode"] = _replication_mode(
            v_data["tags"], v_data["mcast_group"], v_data["vrf_name"])

        # --- Fabric-wide VNI registry (ignores site scope entirely) ---
        if role in _EDGE_ROLES:
            v_data["l2vni"] = _vlan_l2vni(vlan, vlan["vid"], l2vni_base)
            fabric_l2[int(vlan["vid"])] = (v_data["l2vni"], v_data["name"])
        if v_data["vrf_name"]:
            if _is_evpn_vrf(v_data):
                fabric_l3.setdefault(v_data["vrf_name"], v_data["vrf_l3vni"])
            elif v_data["vrf_l3vni_raw"] not in (None, ""):
                # l3vni was set but is not a usable integer: the operator meant
                # to enrol this VRF, so fail rather than silently skip it.
                fabric_l3_bad.setdefault(v_data["vrf_name"], v_data["vrf_l3vni_raw"])

        # --- Fabric-wide edge subnets (ignores site scope entirely) ---
        if v_data["vrf_name"] and _is_evpn_vrf(v_data) and role in _EDGE_ROLES and v_data["network_ipv4"]:
            vrf_name = v_data["vrf_name"]
            fabric_edge_subnets.setdefault(vrf_name, {})
            fabric_edge_subnets[vrf_name][vid_str] = {
                "network": v_data["network_ipv4"],
                "mask": v_data["netmask_ipv4"],
            }

        # --- Local-scope filter: does this VLAN apply to the rendering host? ---
        match_site = (site_id == my_site) if site_id else False
        match_sg = (scope_type == "sitegroup" and scope_id == my_sg) if scope_type else False
        match_sg_site = (scope_type == "site" and scope_id == my_site) if scope_type else False
        no_scope = not site_id and not scope_id

        # A VLAN with NO scope at all is not eligible for the fabric. Enrolment
        # is opt-in throughout this project, and scope is how a VLAN says where
        # it belongs -- an unscoped VLAN has not said, so it is not a decision
        # to honour. It used to be treated as fabric-wide, which inverted the
        # rule: forgetting to scope a VLAN pushed it to every leaf rather than
        # to none. Reported rather than dropped, since it is a NetBox data
        # problem the operator needs to see.
        if no_scope:
            skipped_vlans[vid_str] = {
                "vid": vlan["vid"],
                "name": v_data["name"],
                "role": role,
                "vrf_name": v_data["vrf_name"],
                "reason": "unscoped",
            }
            continue

        if not (match_site or match_sg or match_sg_site):
            continue

        # A VLAN with no recognised role produces no configuration. Surfaced in
        # skipped_vlans so this is a visible, actionable list rather than a
        # silent omission — the usual cause is a NetBox VLAN that never had a
        # role assigned.
        if role not in _KNOWN_ROLES:
            skipped_vlans[vid_str] = {
                "vid": vlan["vid"],
                "name": v_data["name"],
                "role": role,
                "vrf_name": v_data["vrf_name"],
                "reason": "no_role",
            }
            continue

        named_vlans.append({"vid": vlan["vid"], "name": v_data["name"]})

        if v_data["vrf_name"] and _is_evpn_vrf(v_data):
            vrf_name = v_data["vrf_name"]
            if vrf_name not in vrfs:
                vrfs[vrf_name] = {
                    "rd": v_data["vrf_rd"],
                    "l3vni": v_data["vrf_l3vni"],
                    "mcast_group": v_data["vrf_mcast_group"],
                    "core_vlans": {},
                    "edge_vlans": {},
                    "handoff_vlans": {},
                }
            if role == _CORE_ROLE:
                vrfs[vrf_name]["core_vlans"][vid_str] = v_data
            elif role in _EDGE_ROLES:
                vrfs[vrf_name]["edge_vlans"][vid_str] = v_data
                all_edge_vlans[vid_str] = v_data
            elif role == _HANDOFF_ROLE:
                vrfs[vrf_name]["handoff_vlans"][vid_str] = v_data
        elif not v_data["vrf_name"]:
            l2_vlans[vid_str] = v_data
            if role in _EDGE_ROLES:
                if not _l2vni_explicit(vlan):
                    l2_only_bad.append({"vid": vlan["vid"], "name": v_data["name"],
                                        "why": "unenrolled"})
                elif not v_data["mcast_group"]:
                    l2_only_bad.append({"vid": vlan["vid"], "name": v_data["name"],
                                        "why": "no_mcast_group"})
                elif v_data["prefix_ipv4"] or v_data["prefix_ipv6"]:
                    l2_only_bad.append({
                        "vid": vlan["vid"], "name": v_data["name"], "why": "has_prefix",
                        "prefix": v_data["prefix_ipv4"] or v_data["prefix_ipv6"]})
                else:
                    v_data["l2_only"] = True
                    l2_only_vlans[vid_str] = v_data
                    all_edge_vlans[vid_str] = v_data

    _validate_vlan_names(named_vlans)
    _validate_l2_only(l2_only_bad)
    _validate_fabric_vnis(fabric_l3, fabric_l3_bad, fabric_l2, l3vni_base)
    _validate_multicast_groups(all_edge_vlans, reserved_mcast_prefixes,
                               allowed_mcast_prefixes, l3vni_base)

    # TRM. 'has_mcast_vlans' is FABRIC-WIDE, not per-device: it decides whether
    # a border's handoff SVI needs PIM, and a border carries VRF traffic for
    # VLANs that are never local to it. Computed from the unfiltered VLAN list
    # for that reason.
    trm_errors: List[str] = []
    mcast_vlan_vrfs = _fabric_mcast_vlan_vrfs(vlan_list)
    for vrf_name, vrf_entry in vrfs.items():
        vrf_entry["trm"] = _build_trm(
            vrf_name, vrf_entry.get("l3vni"), l3vni_base,
            reserved_mcast_prefixes, trm_objects, hostname,
            vrf_name in mcast_vlan_vrfs, trm_errors, require_register_source)
    # A VLAN tagged for multicast in a VRF that is not enrolled at all cannot be
    # caught above, because no VRF entry exists for it.
    for vrf_name in sorted(mcast_vlan_vrfs - set(vrfs)):
        trm_errors.append(
            f"VLAN(s) tagged '{TRM_TAG_MCAST_VLAN}' are in VRF '{vrf_name}', "
            f"which is not enrolled in the fabric (no 'l3vni' custom field).")
    if trm_errors:
        raise AnsibleFilterError(
            "netbox_evpn_data: TRM configuration is invalid:\n  - "
            + "\n  - ".join(trm_errors))

    return {
        "vrfs": vrfs,
        "l2_vlans": l2_vlans,
        "l2_only_vlans": l2_only_vlans,
        "all_edge_vlans": all_edge_vlans,
        "fabric_edge_subnets": fabric_edge_subnets,
        "skipped_vlans": skipped_vlans,
    }


# ---------------------------------------------------------------------------
# Ansible filter registration
# ---------------------------------------------------------------------------

class FilterModule(object):
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "netbox_evpn_data": build_evpn_data,
            "first_last_usable": first_last_usable,
            "handoff_addresses": handoff_addresses,
        }
