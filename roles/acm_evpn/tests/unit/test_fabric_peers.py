# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Unit tests for EVPN overlay peer selection.

The bug these guard against was silent: a short peer list renders a valid
'router bgp' that 'acm replace' applies as a session teardown. So the tests
that matter most are the ones asserting a peer is never quietly omitted.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + "/filter_plugins")

from fabric_peers import evpn_peer_ips  # noqa: E402

try:
    from ansible.errors import AnsibleFilterError
except ImportError:  # pragma: no cover
    from fabric_peers import AnsibleFilterError  # noqa: F401


SPINES = ["core1", "core2"]
LEAVES = ["access1-1", "access2-2", "dist3-1"]
BORDERS = ["core1", "core2"]          # both borders are also spines, as in the lab

ADDR = {
    "core1": "10.67.118.64",
    "core2": "10.67.118.65",
    "access1-1": "10.67.118.71",
    "access2-2": "10.67.118.72",
    "dist3-1": "10.67.118.73",
}


def peers(host, spines=SPINES, leaves=LEAVES, borders=BORDERS, addresses=ADDR):
    return evpn_peer_ips(host, spines, leaves, borders, addresses)


class TestPeeringModel(unittest.TestCase):
    def test_leaf_peers_with_every_spine(self):
        self.assertEqual(peers("access1-1"), ["10.67.118.64", "10.67.118.65"])

    def test_spine_peers_with_every_client(self):
        self.assertEqual(peers("core1"),
                         ["10.67.118.71", "10.67.118.72", "10.67.118.73"])

    def test_spine_does_not_peer_with_itself(self):
        self.assertNotIn("10.67.118.64", peers("core1"))

    def test_border_that_is_also_a_spine_is_a_reflector_not_a_client(self):
        """core1/core2 are borders AND spines. They must be reflectors, and
        must not appear in another spine's client list."""
        self.assertNotIn("10.67.118.65", peers("core1"))

    def test_pure_border_is_a_client(self):
        result = evpn_peer_ips("border9", SPINES, LEAVES, BORDERS + ["border9"],
                               {**ADDR, "border9": "10.67.118.80"})
        self.assertEqual(result, ["10.67.118.64", "10.67.118.65"])

    def test_pure_border_appears_in_spine_client_list(self):
        result = evpn_peer_ips("core1", SPINES, LEAVES, BORDERS + ["border9"],
                               {**ADDR, "border9": "10.67.118.80"})
        self.assertIn("10.67.118.80", result)

    def test_non_fabric_node_has_no_peers(self):
        self.assertEqual(peers("dist1-1"), [])

    def test_output_is_sorted_numerically_for_stable_diffs(self):
        """A reordered peer list is a spurious config diff on every run."""
        shuffled = {k: ADDR[k] for k in reversed(list(ADDR))}
        self.assertEqual(peers("core1", addresses=shuffled),
                         ["10.67.118.71", "10.67.118.72", "10.67.118.73"])

    def test_address_ordering_is_numeric_not_lexicographic(self):
        addrs = {"core1": "10.0.0.1", "a": "10.0.0.9", "b": "10.0.0.10"}
        result = evpn_peer_ips("core1", ["core1"], ["a", "b"], [], addrs)
        self.assertEqual(result, ["10.0.0.9", "10.0.0.10"])


class TestNoSilentOmission(unittest.TestCase):
    """The regression suite for the --limit bug."""

    def test_peer_list_is_independent_of_who_is_in_the_play(self):
        """The whole point: this function sees inventory, not play membership,
        so the answer is the same either way."""
        full = peers("access1-1")
        self.assertEqual(full, ["10.67.118.64", "10.67.118.65"])
        self.assertEqual(len(full), len(SPINES))

    def test_spine_still_sees_all_leaves(self):
        """Forcing spines into the play did not fix the old bug: a spine's
        list needs every leaf, not just the limited one."""
        self.assertEqual(len(peers("core1")), len(LEAVES))

    def test_missing_address_raises_instead_of_dropping(self):
        incomplete = {k: v for k, v in ADDR.items() if k != "access2-2"}
        with self.assertRaises(AnsibleFilterError) as ctx:
            peers("core1", addresses=incomplete)
        self.assertIn("access2-2", str(ctx.exception))

    def test_empty_address_raises(self):
        with self.assertRaises(AnsibleFilterError):
            peers("core1", addresses={**ADDR, "dist3-1": ""})

    def test_no_addresses_at_all_raises(self):
        with self.assertRaises(AnsibleFilterError):
            peers("access1-1", addresses={})

    def test_missing_address_for_a_non_peer_is_irrelevant(self):
        """Only the peers this host actually needs are required."""
        incomplete = {k: v for k, v in ADDR.items() if k != "dist3-1"}
        self.assertEqual(peers("access1-1", addresses=incomplete),
                         ["10.67.118.64", "10.67.118.65"])


class TestDegenerateFabrics(unittest.TestCase):
    def test_single_spine(self):
        self.assertEqual(evpn_peer_ips("access1-1", ["core1"], LEAVES, [], ADDR),
                         ["10.67.118.64"])

    def test_no_spines_yields_no_peers_for_a_leaf(self):
        self.assertEqual(evpn_peer_ips("access1-1", [], LEAVES, [], ADDR), [])

    def test_no_leaves_yields_no_peers_for_a_spine(self):
        self.assertEqual(evpn_peer_ips("core1", SPINES, [], BORDERS, ADDR), [])

    def test_none_arguments_are_tolerated(self):
        self.assertEqual(evpn_peer_ips("core1", None, None, None, None), [])


if __name__ == "__main__":
    unittest.main()
