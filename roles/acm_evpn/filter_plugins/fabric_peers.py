# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Work out which EVPN overlay peers a fabric node should have.

Why this is inventory-driven
----------------------------
Peer addresses used to be read from a fact each device set for itself at run
time (``loopback0_ip``, scraped from its own running config), then collected
across hosts with ``map('extract', hostvars, 'loopback0_ip') | select('defined')``.
That has two failure modes, both silent and both catastrophic under
``acm replace``:

  * Any peer NOT in the current play has no such fact, so ``select('defined')``
    quietly drops it. Deploying a single leaf with ``--limit`` rendered a
    ``router bgp`` with ZERO EVPN neighbours; adding the spines to the play
    only moved the problem, because a spine's peer list needs every leaf and
    the limited ones were still missing.
  * Even in a full run the result depends on execution order: with
    ``strategy: free`` a host can render before its peers have set the fact.

Inventory data has neither problem. ``hostvars[<host>]['ansible_host']`` comes
from NetBox (``primary_ip4``) and is populated for every device in the
inventory whether or not it is in the play, and before any task runs. The
fabric topology likewise comes from ``groups``, which is always complete.

The assumption this rests on — that a device's NetBox primary IP is its VTEP
loopback — is not assumed silently: ``01_gather_data.yml`` asserts the scraped
Loopback0 matches ``ansible_host`` on every host it visits.

Peering model: spines are the route reflectors and peer with every client;
clients peer with every spine. A border that is also tagged ``fabric_spine``
is a route reflector, not a client, so it is not peered with as one.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # pragma: no cover - only missing outside an Ansible venv
    class AnsibleFilterError(Exception):
        pass


def evpn_peer_ips(
    inventory_hostname: str,
    spines: Optional[Iterable[str]],
    leaves: Optional[Iterable[str]],
    borders: Optional[Iterable[str]],
    addresses: Optional[Dict[str, str]],
) -> List[str]:
    """Overlay peer addresses for ``inventory_hostname``.

    Args:
        inventory_hostname: the host being rendered.
        spines: fabric spines (route reflectors).
        leaves: fabric leaves.
        borders: fabric borders; any that are also spines act as reflectors.
        addresses: {hostname: address} for every fabric member, from inventory.

    Raises:
        AnsibleFilterError: if a peer has no address. Dropping it silently is
            exactly the bug this function exists to remove — a missing address
            would render a config that deletes a live BGP session.
    """
    spine_set = set(spines or ())
    leaf_set = set(leaves or ())
    border_set = set(borders or ())

    clients = leaf_set | (border_set - spine_set)

    if inventory_hostname in spine_set:
        peers = clients - {inventory_hostname}
    elif inventory_hostname in clients:
        peers = spine_set - {inventory_hostname}
    else:
        # Not a fabric overlay node (e.g. a pure distribution switch).
        return []

    addr_map = addresses or {}
    missing = sorted(host for host in peers if not addr_map.get(host))
    if missing:
        raise AnsibleFilterError(
            "Cannot build the EVPN peer list for {}: no address in inventory for {}. "
            "Every fabric device needs a primary IPv4 address in NetBox — without "
            "one the rendered config would silently omit that BGP neighbour, and "
            "'acm replace' would tear down the session.".format(
                inventory_hostname, ", ".join(missing)
            )
        )

    return sorted((addr_map[host] for host in peers),
                  key=lambda a: [int(p) for p in a.split(".")] if a.count(".") == 3 else [])


class FilterModule(object):
    """Ansible filter plugin entry point."""

    def filters(self):
        return {"evpn_peer_ips": evpn_peer_ips}
