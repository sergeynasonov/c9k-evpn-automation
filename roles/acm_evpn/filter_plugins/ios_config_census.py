# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Count the high-blast-radius objects in an IOS-XE configuration, and diff
two configurations by those counts.

Why this exists
---------------
``acm replace`` is a FULL configuration replacement: anything absent from the
candidate config is removed from the device. That makes an upstream data
problem — NetBox unreachable, no VRF enrolled via ``l3vni``, an over-tight
scope filter, a bad GraphQL response — indistinguishable from "the intended
configuration is genuinely small", right up until a switch loses every VRF,
SVI and EVPN instance it had.

Comparing the rendered candidate against the device's own running config
turns that silent catastrophe into a loud refusal. The parsing lives here,
in Python, so it can be unit tested; the policy (thresholds, override flag)
lives in ``tasks/03_safety_check.yml`` where an operator can see and tune it.

This is a blast-radius guard, not a config differ: it deliberately tracks a
few coarse object classes rather than trying to diff the configuration
semantically. A false negative (a dangerous change it fails to spot) is
possible; the goal is to catch the catastrophic, data-driven case.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# Object classes tracked by the guard. Each pattern captures one identifier
# per object; results are de-duplicated, so a class is a SET of identifiers
# rather than a raw line count.
_PATTERNS = {
    # Tenant VRFs. Mgmt-vrf is filtered out below: it is always present, is
    # not fabric-managed, and would dilute the percentages.
    "vrfs": r"(?m)^vrf definition (\S+)",
    # SVIs — every routed interface the fabric provides.
    "svis": r"(?m)^interface (Vlan\d+)",
    # L2 EVPN instances (MAC-VRFs), one per edge VLAN.
    "evpn_instances": r"(?m)^l2vpn evpn instance (\d+)",
    # Distinct VNIs referenced anywhere ("member vni N", under both
    # 'vlan configuration' and 'interface nve1'). Note this intentionally does
    # NOT match the "member evpn-instance N vni M" form, which is counted by
    # evpn_instances instead.
    "vni_members": r"(?m)^\s+member vni (\d+)",
}

_ALWAYS_PRESENT = {"vrfs": {"Mgmt-vrf"}}

# Classes where ANY removal is a violation, regardless of percentage.
#
# Overlay objects can be lost and rebuilt: the fabric drops a service and the
# next run puts it back. Underlay objects cannot. Losing a routed uplink, a
# loopback or an OSPF process takes the management plane with it, so the
# automation loses its own way back in and the fix becomes a console cable.
# A percentage threshold is the wrong instrument for that: one interface out
# of forty is only 2.5%, and it can still be the one carrying the session.
#
# THAT ARGUMENT IS ONLY TRUE OF THE NARROW SET, which is why the set is narrow
# again. For a while 'underlay_interfaces' meant every 'interface' line on the
# box, widened so that a vanishing access port would also be caught. The
# reasoning was that an access port had been invisible to the guard -- true,
# but it was invisible because an access port is not an underlay interface, so
# the definition was doing its job rather than failing at it. The cost was
# real: a tenant SVI inside a VRF then inherited management-plane severity and
# blocked five switches over a deliberately decommissioned VLAN. The access
# port concern is legitimate and is kept, in 'other_interfaces', judged by
# proportion like every other class that can be rebuilt by a later run.
UNDERLAY_CLASSES = ("underlay_interfaces", "ospf_processes")

# What makes an interface underlay: it forwards in the GLOBAL table and it has
# an address. A 'vrf forwarding' line disqualifies it -- a tenant SVI cannot
# carry the management session by definition, because the session does not live
# in that VRF.
_VRF_MARKER = "vrf forwarding "
# 'ip unnumbered' counts. A routed uplink borrowing a loopback address is as
# load-bearing as one with its own, and the old rule matched only 'ip address ',
# so it would have missed exactly that case. 'no ip address' does not match:
# the line starts with 'no '.
_ADDRESS_MARKERS = ("ip address ", "ip unnumbered ")


def _normalise_exclusions(names: Optional[Sequence[str]]) -> set:
    """Bare interface names from either 'Vlan106' or 'interface Vlan106'.

    Callers supply both forms: the role's own defaults list bare names, while a
    prune list is built from configuration headers and carries the keyword.
    """
    out = set()
    for raw in names or []:
        name = str(raw).strip()
        if name.startswith("interface "):
            name = name[len("interface "):].strip()
        if name:
            out.add(name)
    return out


def _interface_blocks(config_text: str):
    """(name, body) per 'interface X' block, body stripped of indentation.

    The census used to match only the header line, which is why 'vrf
    forwarding' was never seen: the classification could not read it. IOS
    indents children by one space and closes a block with an unindented '!',
    so an unindented line ends the block.
    """
    blocks, name, body = [], None, []
    for line in (config_text or "").splitlines():
        if line.startswith("interface "):
            if name is not None:
                blocks.append((name, body))
            name, body = line[len("interface "):].strip(), []
        elif name is not None:
            if line.startswith((" ", "\t")):
                body.append(line.strip())
            else:
                blocks.append((name, body))
                name, body = None, []
    if name is not None:
        blocks.append((name, body))
    return blocks


def _is_underlay_interface(body: Sequence[str]) -> bool:
    if any(line.startswith(_VRF_MARKER) for line in body):
        return False
    return any(line.startswith(marker)
               for line in body for marker in _ADDRESS_MARKERS)


def ios_config_census(config_text: str,
                      exclude_interfaces: Optional[Sequence[str]] = None) -> Dict[str, List[str]]:
    """Identifiers of each tracked object class present in a configuration.

    ``exclude_interfaces`` drops interfaces from every class that counts them.
    Two callers need it and they mean the same thing by it -- do not score a
    removal here -- for different reasons: interfaces a template renders itself,
    and interfaces this deploy prunes ON PURPOSE. Without the second, every
    routine VLAN decommission reads as an unintended loss. The parameter was
    declared and silently ignored before, so both callers were passing values
    into a function that discarded them.
    """
    skip = _normalise_exclusions(exclude_interfaces)

    census: Dict[str, List[str]] = {}
    for name, pattern in _PATTERNS.items():
        found = set(re.findall(pattern, config_text or ""))
        found -= _ALWAYS_PRESENT.get(name, set())
        census[name] = sorted(found)
    # svis are named by interface, so an exclusion applies there too. Otherwise
    # a deliberately pruned SVI is exempt from the interface classes and still
    # scored as a loss under 'svis'.
    census["svis"] = [s for s in census["svis"] if s not in skip]

    underlay, other = [], []
    for name, body in _interface_blocks(config_text):
        if name in skip:
            continue
        (underlay if _is_underlay_interface(body) else other).append(name)

    census["underlay_interfaces"] = sorted(set(underlay))
    # Switchports, VRF-bound SVIs, unaddressed and shutdown interfaces. Losing
    # one silently is still worth catching -- that concern is what widened the
    # underlay class in the first place -- but it cannot strand a switch behind
    # a console cable, so it is scored by proportion rather than zero tolerance.
    census["other_interfaces"] = sorted(set(other))
    census["ospf_processes"] = sorted(
        set(re.findall(r"(?m)^router (ospfv?3? \S+)", config_text or ""))
    )
    return census


def ios_config_census_delta(before_text: str, after_text: str,
                            exclude_interfaces: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Per-class removal report going from ``before_text`` to ``after_text``.

    Only removals are scored. Additions are unbounded on purpose — growing the
    fabric is the normal case and carries no risk of silently wiping a device.

    ``removed_pct`` is 0.0 when a class is empty beforehand, so a greenfield
    switch (nothing to lose) never trips the guard.
    """
    before = ios_config_census(before_text, exclude_interfaces)
    after = ios_config_census(after_text, exclude_interfaces)

    delta: Dict[str, Any] = {}
    for name in before:
        after_set = set(after[name])
        removed = [item for item in before[name] if item not in after_set]
        prior = len(before[name])
        delta[name] = {
            "before": prior,
            "after": len(after[name]),
            "removed": removed,
            "removed_count": len(removed),
            "removed_pct": round(100.0 * len(removed) / prior, 1) if prior else 0.0,
        }
    return delta


def ios_config_census_violations(
    before_text: str,
    after_text: str,
    max_removal_pct: float = 50.0,
    exclude_interfaces: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Object classes whose removals breach policy, worst first.

    Two policies apply. Underlay classes (UNDERLAY_CLASSES) are zero
    tolerance: any removal at all is a violation, because losing a single
    routed uplink can cost the management plane and no later run can repair
    it. Every other class is judged on the proportion removed.

    Returns [] when the change is acceptable, so the task layer is a single
    call and a length check rather than a Jinja expression.
    """
    delta = ios_config_census_delta(before_text, after_text, exclude_interfaces)

    violations: List[Dict[str, Any]] = []
    for name, d in delta.items():
        if not d["removed_count"]:
            continue
        underlay = name in UNDERLAY_CLASSES
        if underlay or d["removed_pct"] > float(max_removal_pct):
            violations.append({
                "klass": name,
                "underlay": underlay,
                "reason": "underlay removal (zero tolerance)" if underlay
                          else f"exceeds {max_removal_pct}% removal threshold",
                **d,
            })

    # Underlay first, then largest proportional loss: the operator should read
    # the most dangerous finding at the top.
    violations.sort(key=lambda v: (not v["underlay"], -v["removed_pct"]))
    return violations


class FilterModule(object):
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "ios_config_census": ios_config_census,
            "ios_config_census_delta": ios_config_census_delta,
            "ios_config_census_violations": ios_config_census_violations,
        }
