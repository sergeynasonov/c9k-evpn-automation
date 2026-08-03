# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Read the two things the fabric templates need out of a running configuration.

This module used to CLASSIFY the running config -- deciding which stanzas were
"underlay" and had to be carried forward so ``acm replace`` would not delete
them. That approach needed an ever-growing allowlist, and the allowlist kept
falling behind reality: it silently dropped every switchport, L3 port-channel
members, SVIs in non-fabric VRFs, and each IOS-XE platform default the
templates did not model. Every one of those was found by inspection rather than
by any guard.

Ownership is now inverted (see templates/candidate_config.j2): the candidate is
the rendered fabric block plus everything on the device it does not contain, so
config survives by default and there is nothing to classify. What remains here
are two genuine transforms that the inversion does not subsume:

  * ``router bgp`` splitting. The stanza is a hybrid -- the overlay half is
    rendered from NetBox, the underlay half belongs to the device -- and the
    templates emit one merged stanza, so the device's half has to be extracted.
    Ownership is BY ASN: a neighbour whose remote-as is the fabric ASN is an
    iBGP construct the automation renders (the EVPN peer group, its members,
    the border-to-border mesh); anything in another ASN is the device's.

  * An interface -> address map, read by the anycast-RP assertions to check the
    generated ``ip pim rp-address`` points at an address the fabric owns.

Plus ``render_vlan_spec``, exposed as the ``ios_vlan_range`` filter, because IOS
re-renders every VLAN list sorted and range-collapsed -- a template emitting
'101,100' is rewritten to '100-101' on the device and never converges.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

_ADDRESS_MARKER = "ip address "


def _iter_blocks(config_text: str) -> Iterator[Tuple[str, List[str]]]:
    """Yield ``(header, child_lines)`` for every top-level stanza.

    IOS-XE configuration is indentation-structured: a non-indented line opens a
    stanza and every following indented line belongs to it.

    Indentation is tested before the ``!`` check, because an INDENTED bang is a
    separator *within* a stanza — ``router bgp`` uses one between each address
    family — while only a non-indented bang closes the stanza. Treating the two
    alike truncates every multi-address-family BGP block at its first separator.
    """
    header: Optional[str] = None
    children: List[str] = []

    for line in (config_text or "").splitlines():
        if line.startswith((" ", "\t")):
            if header is not None:
                children.append(line)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            if header is not None:
                yield header, children
                header, children = None, []
            continue
        if header is not None:
            yield header, children
        header, children = line, []

    if header is not None:
        yield header, children


def _split_bgp(children: Sequence[str]) -> Tuple[List[str], Dict[str, List[str]]]:
    """Split a ``router bgp`` body into direct lines and address-family bodies.

    Address families are keyed by the text following ``address-family``, so the
    global table is ``"ipv4"`` while a tenant family is ``"ipv4 vrf CORP"`` —
    letting the caller keep the former and discard the latter.
    """
    direct: List[str] = []
    families: Dict[str, List[str]] = {}
    current: Optional[str] = None

    for line in children:
        stripped = line.strip()
        if stripped.startswith("address-family "):
            current = stripped[len("address-family "):].strip()
            families.setdefault(current, [])
        elif stripped == "exit-address-family":
            current = None
        elif current is not None:
            families[current].append(line)
        elif stripped != "!":
            direct.append(line)

    return direct, families


def _bgp_neighbour_asn(direct: Sequence[str]) -> Dict[str, str]:
    """Map each BGP neighbour (or peer-group) to its remote-as.

    A neighbour that only says 'peer-group X' inherits X's ASN, so peer-group
    membership is resolved after the direct declarations are collected.
    """
    remote_as: Dict[str, str] = {}
    inherits: Dict[str, str] = {}
    for line in direct:
        stripped = line.strip()
        match = re.match(r"neighbor (\S+) remote-as (\d+)\b", stripped)
        if match:
            remote_as[match.group(1)] = match.group(2)
            continue
        match = re.match(r"neighbor (\S+) peer-group (\S+)\s*$", stripped)
        if match:
            inherits[match.group(1)] = match.group(2)
    for name, group in inherits.items():
        if group in remote_as:
            remote_as.setdefault(name, remote_as[group])
    return remote_as


def _bgp_regenerated_matchers(peer_group: str) -> List[Any]:
    """Non-neighbour BGP lines the templates render themselves.

    Everything NOT matched here is preserved, so an unfamiliar global knob
    (``timers bgp``, say) survives rather than being silently dropped. Neighbour
    lines are handled separately, by ASN.
    """
    pg = re.escape(peer_group)
    return [
        re.compile(p)
        for p in (
            r"^bgp router-id\b",
            r"^bgp log-neighbor-changes\b",
            r"^bgp graceful-restart\b",
            r"^no bgp default ipv4-unicast\b",
            rf"^neighbor {pg}\b",
            rf"^neighbor \S+ peer-group {pg}\b",
        )
    ]


def underlay_config(
    config_text: str,
    bgp_peer_group: str = "EVPN-PEERS",
    fabric_asn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Extract the device-owned half of ``router bgp``, and an address map.

    Returns::

        {
          "bgp": {"asn": "64513",
                   "direct":  [...],   # neighbours outside the fabric ASN
                   "af_ipv4": [...],   # global address-family, same filter
                   "fabric_asn_stripped": [...]},
          "addresses": {"Loopback0": "10.67.118.64", ...},
        }

    ``fabric_asn_stripped`` lists the neighbours removed on the assumption a
    template renders them. That assumption is load-bearing -- if one is stripped
    and NOT rendered the session is silently deleted, which is how the
    border-to-border iBGP mesh would have been lost -- so 03_safety_check.yml
    verifies each of them appears in the candidate.
    """
    addresses: Dict[str, str] = {}
    bgp: Dict[str, Any] = {"asn": "", "direct": [], "af_ipv4": [],
                           "fabric_asn_stripped": []}

    for header, children in _iter_blocks(config_text):
        if header.startswith("interface "):
            name = header[len("interface "):].strip()
            for line in children:
                stripped = line.strip()
                if stripped.startswith(_ADDRESS_MARKER):
                    addresses[name] = stripped.split()[2]
                    break

        elif header.startswith("router bgp "):
            bgp["asn"] = header[len("router bgp "):].strip()
            direct, families = _split_bgp(children)
            families_ipv4 = families.get("ipv4", [])
            skip = _bgp_regenerated_matchers(bgp_peer_group)

            asn_of = _bgp_neighbour_asn(direct)
            fabric = str(fabric_asn) if fabric_asn is not None else bgp["asn"]

            def _fabric_neighbour(line: str) -> bool:
                match = re.match(r"neighbor (\S+)\b", line.strip())
                return bool(match) and asn_of.get(match.group(1)) == fabric

            bgp["direct"] = [
                line for line in direct
                if not any(m.search(line.strip()) for m in skip)
                and not _fabric_neighbour(line)
            ]
            bgp["af_ipv4"] = [line for line in families_ipv4
                              if not _fabric_neighbour(line)]
            bgp["fabric_asn_stripped"] = sorted({
                re.match(r"neighbor (\S+)\b", line.strip()).group(1)
                for line in list(direct) + list(families_ipv4)
                if _fabric_neighbour(line)
            })

    return {"bgp": bgp, "addresses": addresses}


def render_vlan_spec(vids: Sequence[Any]) -> str:
    """Collapse a VLAN id list to sorted IOS range notation, e.g. '100-101,150'.

    Exposed as the ``ios_vlan_range`` filter because IOS re-renders any VLAN
    list this way. A template emitting '101,100' is rewritten by the device to
    '100-101' on every deploy, so the config never converges.
    """
    ordered = sorted({int(v) for v in vids})
    if not ordered:
        return ""
    parts: List[str] = []
    start = previous = ordered[0]
    for vid in ordered[1:]:
        if vid == previous + 1:
            previous = vid
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = vid
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


class FilterModule(object):
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "ios_underlay_config": underlay_config,
            "ios_vlan_range": render_vlan_spec,
        }
