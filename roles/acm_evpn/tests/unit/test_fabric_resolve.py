# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Fabric resolution and per-fabric EVPN peer selection.

Unlike its neighbours here this is not a plugin test: the logic lives in Jinja,
in roles/underlay_readiness/tasks/fabric_resolve.yml and in tasks 16-17 of
roles/acm_evpn/tasks/01_gather_data.yml. ``--syntax-check`` does not evaluate
Jinja, so the only honest way to test it is to run it -- which is possible
offline because fabric membership is decided entirely from inventory groups,
with no NetBox and no device in the path.

Each case runs the REAL task files under a real ansible-playbook against a
static inventory in ../fixtures. Nothing is copied by hand: the peer-selection
tasks are lifted out of 01_gather_data.yml at run time, so the test cannot drift
from the role.

What it is guarding
-------------------
A peer list is the highest-risk value this project computes. Too short a list
renders a perfectly valid ``router bgp`` that ``acm replace`` applies as a
teardown of live sessions, on a switch that was working a moment earlier. Every
assertion below is ultimately about a peer that must be there, or one that must
not.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE = os.path.dirname(TESTS)
ROLES = os.path.dirname(ROLE)
REPO = os.path.dirname(ROLES)
FIXTURES = os.path.join(TESTS, "fixtures")

GATHER = os.path.join(ROLE, "tasks", "01_gather_data.yml")
HARNESS = os.path.join(FIXTURES, "fabric_harness.yml")

ANSIBLE = shutil.which("ansible-playbook")


def _peer_tasks():
    """The inventory-only tasks of 01_gather_data.yml, verbatim.

    Everything else in that file needs NetBox or a live switch. Selecting by the
    numeric prefix keeps the harness honest: renumber or rename a task and this
    raises rather than silently testing less than it claims to.
    """
    with open(GATHER) as fh:
        tasks = yaml.safe_load(fh)
    wanted = [t for t in tasks if t.get("name", "").startswith(("16.", "17."))]
    if len(wanted) != 2:
        raise AssertionError(
            "expected tasks 16 and 17 of 01_gather_data.yml to be the peer-selection "
            "tasks, found {}".format([t.get("name") for t in wanted])
        )
    return wanted


def _target_tasks():
    """Tasks 13-15a of 01_gather_data.yml: deploy_target -> targeted fabrics.

    Four, not three: the drop of an out-of-fabric spine is now two tasks, one
    recording WHY and one ending the host, so the pre-deploy summary can tell a
    deliberate skip from a failure.
    """
    with open(GATHER) as fh:
        tasks = yaml.safe_load(fh)
    wanted = [t for t in tasks
              if t.get("name", "").startswith(("13.", "14.", "15.", "15a."))]
    if len(wanted) != 4:
        raise AssertionError(
            "expected tasks 13-15a of 01_gather_data.yml to be the target-scoping "
            "tasks, found {}".format([t.get("name") for t in wanted])
        )
    return wanted


RENDER = os.path.join(ROLE, "tasks", "02_render_configs.yml")


def _render_gate_tasks():
    """The two tasks of 02_render_configs.yml that drop a non-fabric device.

    They live there, not in the resolver, because underlay_readiness must have
    checked the device first -- a distribution switch between a leaf and a
    border carries fabric traffic even though nothing is rendered for it.
    """
    with open(RENDER) as fh:
        tasks = yaml.safe_load(fh)
    wanted = [t for t in tasks if t.get("name", "").startswith(("R1.", "R2."))]
    if len(wanted) != 2:
        raise AssertionError(
            "expected the R1/R2 render-gate tasks in 02_render_configs.yml, "
            "found {}".format([t.get("name") for t in wanted])
        )
    return wanted


UNSCOPED = object()


def run_fabric(inventory, reentry=False, mutate=None, deploy_target="all",
               render_gate=False, target_scoping=True, play_pattern=None):
    """Run the harness against a fixture inventory.

    ``reentry`` imports the resolver a second time, the way acm_evpn does when
    it resolves in 01_gather_data.yml and then runs underlay_readiness as a
    pre-flight.

    ``deploy_target`` defaults to "all" because every run must now be scoped;
    pass ``UNSCOPED`` to exercise the refusal.

    ``mutate`` is called with the parsed YAML inventory and may edit it in
    place; the edited copy is written into the temp dir and used instead. The
    broken-ASN cases are one small edit away from the working fixture, so
    expressing them as edits keeps the difference visible in the test rather
    than buried in a near-identical second file.

    Returns (returncode, output, result) where result is the parsed facts, or
    None when the play failed before writing them.
    """
    workdir = tempfile.mkdtemp(prefix="fabric-resolve-")
    try:
        shutil.copy(HARNESS, os.path.join(workdir, "harness.yml"))
        inv_path = os.path.join(FIXTURES, inventory)
        if mutate is not None:
            with open(inv_path) as fh:
                data = yaml.safe_load(fh)
            mutate(data)
            inv_path = os.path.join(workdir, "inventory.yml")
            with open(inv_path, "w") as fh:
                yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        with open(os.path.join(workdir, "peer_tasks.yml"), "w") as fh:
            yaml.safe_dump(_peer_tasks(), fh, default_flow_style=False, sort_keys=False)
        with open(os.path.join(workdir, "target_tasks.yml"), "w") as fh:
            yaml.safe_dump(_target_tasks(), fh, default_flow_style=False, sort_keys=False)
        with open(os.path.join(workdir, "render_gate_tasks.yml"), "w") as fh:
            yaml.safe_dump(_render_gate_tasks(), fh, default_flow_style=False,
                           sort_keys=False)

        result_file = os.path.join(workdir, "result.json")
        env = dict(
            os.environ,
            ANSIBLE_ROLES_PATH=ROLES,
            ANSIBLE_FILTER_PLUGINS=os.path.join(ROLE, "filter_plugins"),
            # The repo's ansible.cfg would point the run at netbox_inv.yml and
            # at the 'free' strategy. Running from a temp cwd leaves it unfound,
            # so everything this needs is passed explicitly above.
            ANSIBLE_NOCOLOR="1",
        )
        proc = subprocess.run(
            [ANSIBLE, "-i", inv_path, "harness.yml",
             "-e", "result_file=" + result_file,
             "-e", "reentry=" + ("true" if reentry else "false"),
             "-e", "render_gate=" + ("true" if render_gate else "false"),
             "-e", "target_scoping=" + ("true" if target_scoping else "false")]
            + ([] if play_pattern is None
               else ["-e", "play_pattern=" + play_pattern])
            + ([] if deploy_target is UNSCOPED
               else ["-e", "deploy_target=" + deploy_target]),
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        output = proc.stdout + proc.stderr
        result = None
        if os.path.exists(result_file):
            with open(result_file) as fh:
                result = json.load(fh)
        return proc.returncode, output, result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestTwoFabrics(unittest.TestCase):
    """One campus site group beside one standalone site."""

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini")
        if cls.result is None:  # pragma: no cover - only on a broken harness
            raise AssertionError("harness produced no result:\n" + cls.out)

    def peers(self, host):
        return self.result["hosts"][host]["peer_ips"]

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_site_group_is_the_fabric_when_there_is_one(self):
        """Two buildings under one site group are ONE fabric, not two."""
        self.assertEqual(self.result["fabric_map"]["access1-1"], "site_group_evpn_campus")
        self.assertEqual(self.result["fabric_map"]["access2-1"], "site_group_evpn_campus")

    def test_bare_site_is_the_fabric_when_there_is_no_site_group(self):
        self.assertEqual(self.result["fabric_map"]["rcore1"], "sites_evpn_remote")

    def test_spine_reflects_for_its_own_fabric_only(self):
        """core1 must see both buildings of its campus and neither remote box."""
        self.assertEqual(self.peers("core1"), ["10.0.0.11", "10.0.0.21"])

    def test_leaf_peers_only_with_its_own_spines(self):
        self.assertEqual(self.peers("access1-1"), ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(self.peers("raccess1"), ["10.1.0.1"])

    def test_remote_fabric_does_not_pull_in_the_campus(self):
        self.assertEqual(self.peers("rcore1"), ["10.1.0.11"])

    def test_no_address_from_another_fabric_appears_anywhere(self):
        """The whole point. A cross-fabric neighbour is a session to a device
        that shares neither underlay nor RP."""
        campus = {"core1", "core2", "access1-1", "access2-1", "dist1"}
        for host, data in self.result["hosts"].items():
            remote = [p for p in data["peer_ips"] if p.startswith("10.1.")]
            if host in campus:
                self.assertEqual(remote, [], "{} peers into the remote fabric".format(host))
            else:
                self.assertTrue(all(p.startswith("10.1.") for p in data["peer_ips"]),
                                "{} peers into the campus: {}".format(host, data["peer_ips"]))

    def test_a_second_fabric_does_not_shorten_the_first(self):
        """Regression on the mechanism itself: adding fabric B must not remove a
        peer from fabric A, which is the shape a teardown takes."""
        self.assertEqual(len(self.peers("core1")), 2)
        self.assertEqual(len(self.peers("core2")), 2)

    def test_non_fabric_node_in_a_fabric_gets_no_peers(self):
        """A distribution switch is site-scoped like anything else, so it
        resolves a fabric -- but claims no sessions and, below, is not rendered
        for at all."""
        self.assertEqual(self.result["fabric_map"]["dist1"], "site_group_evpn_campus")
        self.assertEqual(self.peers("dist1"), [])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestDeviceWithNoSite(unittest.TestCase):
    """Ignored and reported, never fatal -- but never rendered either."""

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini")

    def test_run_still_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_orphan_is_reported(self):
        self.assertEqual(self.result["orphans"], ["orphan1"])
        self.assertIn("orphan1", self.out)

    def test_orphan_resolves_to_no_fabric(self):
        self.assertNotIn("orphan1", self.result["fabric_map"])
        self.assertEqual(self.result["hosts"]["orphan1"]["fabric_id"], "")

    def test_orphan_drops_out_of_the_play(self):
        """It was targeted, so silence would mean a config rendered against an
        empty fabric -- an empty peer list is a teardown."""
        self.assertIn("orphan1", self.result["targeted"])
        self.assertNotIn("orphan1", self.result["survivors"])

    def test_everyone_else_survives(self):
        """dist1 included: the resolver keeps non-fabric devices so that
        underlay_readiness can check them. See TestNonFabricNodesAreDropped."""
        self.assertEqual(
            self.result["survivors"],
            ["access1-1", "access2-1", "core1", "core2", "dist1", "raccess1", "rcore1"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestASiteWithNoFabricNodeIsNotAFabric(unittest.TestCase):
    """A NetBox holds plenty of ordinary sites — WAN kit, servers, campuses this
    automation does not manage. They are not candidate fabrics waiting to be
    configured, and asking them for an ASN and an anycast RP produced a
    deprecation notice on every run for a site that will never have either.

    A site is a fabric because it holds a fabric node, full stop. The fixture's
    sites_wan_edge holds a WAN router and a file server and nothing else.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini")

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_its_devices_still_resolve_to_their_site(self):
        """Guards the fixture: they ARE in the site, which is why the site shows
        up as a candidate at all."""
        self.assertEqual(self.result["fabric_map"]["wan-rtr-1"], "sites_wan_edge")

    def test_but_the_site_is_not_a_fabric(self):
        self.assertNotIn("sites_wan_edge", self.result["fabric_ids"])

    def test_the_real_fabrics_still_are(self):
        self.assertEqual(self.result["fabric_ids"],
                         ["site_group_evpn_campus", "sites_evpn_remote"])

    def test_it_is_never_reported_for_missing_an_asn(self):
        """The regression, straight from a real run."""
        self.assertNotIn("sites_wan_edge", self.out)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestScopedToDeviceRolesThisAutomationActsOn(unittest.TestCase):
    """This project acts on fabric leaves, fabric borders and the distribution
    switches in the transit path. Nothing else.

    Each playbook's DEFAULT host pattern already says so, but 'deploy_target'
    REPLACES that pattern rather than intersecting with it, and the documented
    usage scopes by site or site group — so a site-scoped run selects every
    server, printer and WAN router that happens to share the site.

    It cannot be fixed in the host pattern. Ansible applies ':&' after every
    union, so '<roles>:&<target>' works only while the target is a single term;
    a multi-term deploy_target silently turns the trailing terms back into
    unions, and 'query(inventory_hostnames, deploy_target)' accepts any pattern.
    So it is a task, alongside the two 'meta: end_host' rules already there.

    The fixture's sites_wan_edge holds a WAN router and a file server, neither
    of which is in fabric_managed_groups.
    """

    @classmethod
    def setUpClass(cls):
        # WITHOUT acm_evpn's target scoping. That drops the same two hosts for
        # an unrelated reason -- their site is not a fabric, so it is never a
        # targeted one -- and with it on, deleting the resolver's scope rule
        # changed nothing here. readiness.yml runs the resolver alone, so this
        # is the path that has to be tested.
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini",
                                                 target_scoping=False)

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_a_device_outside_the_managed_roles_is_dropped(self):
        for host in ("wan-rtr-1", "fileserver"):
            self.assertIn(host, self.result["targeted"], host)
            self.assertNotIn(host, self.result["survivors"], host)

    def test_it_is_reported_rather_than_silently_skipped(self):
        """The whole reason the report exists: a transit switch modelled under
        an unlisted role name looks exactly like a printer from here, and
        silently dropping it would leave a hole in the path readiness covers."""
        self.assertIn("not a fabric role", self.out)
        self.assertIn("wan-rtr-1", self.out)
        self.assertIn("fabric_managed_groups", self.out)

    def test_the_distribution_switch_survives(self):
        """The line that matters. fabric_managed_groups is wider than
        fabric_node_groups by exactly the transit path — nothing is rendered for
        a distribution switch, but its underlay is still checked."""
        self.assertIn("dist1", self.result["survivors"])

    def test_every_fabric_node_survives(self):
        for host in ("core1", "core2", "access1-1", "access2-1",
                     "rcore1", "raccess1"):
            self.assertIn(host, self.result["survivors"], host)

    def test_a_fabricless_node_is_still_reported_as_an_orphan(self):
        """orphan1 is a fabric leaf, so it is in scope by role. The new rule
        must not take over its reporting: it is dropped for having no site, and
        that is the message an operator needs."""
        self.assertNotIn("orphan1", self.result["survivors"])
        self.assertIn("orphan1", self.result["orphans"])
        self.assertIn("belong to no fabric", self.out)

    def test_the_scope_report_does_not_claim_a_fabric_node(self):
        """The two reports must not blur: only non-fabric roles belong in the
        scope one."""
        scope_line = [ln for ln in self.out.splitlines()
                      if "not a fabric role" in ln]
        self.assertTrue(scope_line, self.out)
        for host in ("core1", "access1-1", "dist1", "orphan1"):
            self.assertNotIn(host, " ".join(scope_line), host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNonFabricNodesAreDropped(unittest.TestCase):
    """A host pattern can select devices this role renders nothing for.

    Not hypothetical: ':' is a UNION, so '-e deploy_target=<site>' selects every
    device in that site, distribution switches included. The render step picks
    its template as border / spine / else-leaf, with no "not ours" branch, so
    such a device used to be handed a full EVPN LEAF configuration and have it
    applied by 'acm replace'.

    It cannot be fixed in the host pattern: Ansible applies ':&' intersections
    after every union regardless of position, so intersecting with the fabric
    roles would also intersect away the spines the pattern deliberately unions
    in -- leaving their peer lists stale, which is the exact bug that union
    exists to prevent. Verified against real ansible, not assumed.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini", render_gate=True)
        cls.no_gate = run_fabric("fabric_multi.ini")[2]

    def test_run_still_succeeds(self):
        """One targeted device being none of our business is not an error."""
        self.assertEqual(self.rc, 0, self.out)

    def test_the_distribution_switch_was_targeted(self):
        """Guards the fixture: if it stopped being selected, the assertions
        below would pass for the wrong reason."""
        self.assertIn("dist1", self.result["targeted"])

    def test_the_resolver_keeps_it_so_readiness_can_check_it(self):
        """The division of responsibility. A distribution switch between a leaf
        and a border carries fabric traffic, so its underlay must be checked --
        it just gets no overlay config."""
        self.assertIn("dist1", self.no_gate["survivors"])

    def test_the_render_gate_drops_it(self):
        self.assertNotIn("dist1", self.result["survivors"])

    def test_it_is_reported_by_name(self):
        """An operator who targeted a site deserves to know which of its
        devices got no configuration."""
        self.assertIn("are not fabric nodes: dist1", self.out)

    def test_fabric_nodes_are_not_dropped_with_it(self):
        for host in ("core1", "core2", "access1-1", "access2-1", "raccess1", "rcore1"):
            self.assertIn(host, self.result["survivors"], host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestResolvingTwiceIsFree(unittest.TestCase):
    """acm_evpn resolves, then imports underlay_readiness, which resolves again.

    The second pass must change nothing and say nothing. It is guarded on
    ``fabric_id``, which the resolver sets LAST -- guarding on ``fabric_map``,
    which it sets first, would have skipped the orphan report on the FIRST pass
    instead of the second.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini", reentry=True)
        cls.once_rc, cls.once_out, cls.once = run_fabric("fabric_multi.ini")

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_the_orphan_is_reported_exactly_once(self):
        self.assertEqual(self.out.count("belong to no fabric and are skipped"), 1)

    def test_resolution_is_identical_to_a_single_pass(self):
        self.assertEqual(self.result["fabric_map"], self.once["fabric_map"])
        self.assertEqual(self.result["hosts"], self.once["hosts"])

    def test_the_orphan_still_drops_out_on_re_entry(self):
        """The failure mode a whole-file guard would have introduced: by the
        second pass fabric_id is set, so a file-level condition would have
        skipped the end_host and let the orphan through."""
        self.assertNotIn("orphan1", self.result["survivors"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestNestedSiteGroups(unittest.TestCase):
    """A site-group tree has no single answer, so it is refused outright."""

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_nested.ini")

    def test_run_fails(self):
        self.assertNotEqual(self.rc, 0, self.out)

    def test_nothing_is_rendered(self):
        self.assertIsNone(self.result)

    def test_failure_names_the_devices_and_both_groups(self):
        self.assertIn("nested site groups", self.out)
        self.assertIn("site_group_emea", self.out)
        self.assertIn("site_group_evpn_campus", self.out)

    def test_failure_says_how_to_fix_it(self):
        self.assertIn("Flatten the tree", self.out)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricWithNoReflector(unittest.TestCase):
    """A leaf alone in its own site: a fabric with neither a spine nor a border.

    THIS NOW FAILS EARLIER THAN IT USED TO, and the change is deliberate. The
    fixture was written for acm_evpn task 17, which asserted the spine half PER
    DEVICE on the render path -- so the stranded leaf dropped out and the
    healthy campus in the same inventory carried on. Task 13 of the resolver
    now refuses the whole run instead, before any device is contacted, because
    an incomplete fabric is a NetBox data problem like a missing ASN (task 10)
    rather than one switch being unfit.

    The isolation that behaviour bought is not lost, it moved: task 13 only
    considers fabrics this run actually reaches, so a scoped run against a
    healthy fabric is unaffected by a broken one elsewhere in the estate. That
    is the case test_a_scoped_run_is_not_blocked_by_another_fabric covers. What
    no longer happens is a run that INCLUDES the broken fabric proceeding for
    the others -- deliberately, since the render it would produce was never
    going to be applied.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_no_reflector.ini")

    def test_run_fails(self):
        self.assertNotEqual(self.rc, 0, self.out)

    def test_the_incomplete_fabric_is_named(self):
        """The fabric, not each stranded switch in turn. One line for one
        NetBox fault beats one per member once a fabric has thirty of them."""
        self.assertIn("sites_evpn_lonely", self.out)
        self.assertIn("no spine and no border", self.out)

    def test_it_fails_before_any_device_is_contacted(self):
        """The resolver runs entirely on inventory. Failing here rather than on
        the render path means nothing has opened an SSH session yet."""
        self.assertIn("13. Refuse a fabric without a spine and a border", self.out)
        self.assertNotIn("Fetch current running configuration", self.out)

    def test_a_scoped_run_is_not_blocked_by_another_fabric(self):
        """Where the old per-device isolation went. The campus is healthy, so a
        run scoped to it must not care that sites_evpn_lonely is broken."""
        rc, out, result = run_fabric("fabric_no_reflector.ini",
                                     deploy_target="site_group_evpn_campus",
                                     play_pattern="site_group_evpn_campus")
        self.assertEqual(rc, 0, out)
        self.assertEqual(result["hosts"]["access1-1"]["peer_ips"], ["10.0.0.1"])
        self.assertEqual(result["hosts"]["core1"]["peer_ips"], ["10.0.0.11"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestSingleFabricUnchanged(unittest.TestCase):
    """The fabric that is in production today.

    Before this change the peer list was built from a hardcoded
    ``groups['site_group_evpn_campus']``. On an inventory with one site group
    the resolver must arrive at exactly the same answer -- anything else is a
    config change pushed to seven working switches.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_single.ini")

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_every_switch_stays_in_the_play(self):
        self.assertEqual(self.result["survivors"], self.result["targeted"])
        self.assertEqual(len(self.result["survivors"]), 7)

    def test_spines_see_all_five_clients(self):
        expected = ["10.67.118.71", "10.67.118.72", "10.67.118.73",
                    "10.67.118.74", "10.67.118.75"]
        self.assertEqual(self.result["hosts"]["core1"]["peer_ips"], expected)
        self.assertEqual(self.result["hosts"]["core2"]["peer_ips"], expected)

    def test_every_leaf_sees_both_spines(self):
        for host in ("access1-1", "access2-1", "access2-2", "dist3-1", "dist3-2"):
            self.assertEqual(self.result["hosts"][host]["peer_ips"],
                             ["10.67.118.64", "10.67.118.65"], host)

    def test_borders_that_are_spines_are_still_reflectors(self):
        """core1/core2 are both. Each must not appear in the other's client
        list, or a reflector becomes its own client."""
        self.assertNotIn("10.67.118.65", self.result["hosts"]["core1"]["peer_ips"])
        self.assertNotIn("10.67.118.64", self.result["hosts"]["core2"]["peer_ips"])


def _sites(data, *hosts):
    """The 'sites' hostvar of each named host in a parsed YAML inventory.

    Starts at data['all'], not at data: the top level is keyed by group name, so
    walking from there finds neither 'hosts' nor 'children' and would silently
    return nothing -- a mutation that changes no ASN and a test that passes
    against a fixture it never edited.
    """
    out = []
    stack = [data["all"]]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for host, hv in (node.get("hosts") or {}).items():
            if host in hosts and isinstance(hv, dict):
                out.append(hv["sites"][0])
        stack.extend((node.get("children") or {}).values())
    if not out:
        raise AssertionError("no 'sites' hostvar found for {}".format(hosts))
    return out


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricAsn(unittest.TestCase):
    """The fabric's iBGP ASN, from the ASN objects on its NetBox sites.

    It comes from INVENTORY, not a GraphQL query: nb_inventory fetches sites
    with the full serializer, so 'asns' already rides along in the hostvar for
    every device in or out of the play.

    One ASN per fabric is definitional -- every EVPN session in the fabric is
    iBGP inside it -- so the interesting cases are the ones with no single
    answer, and all of them are refused rather than resolved.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_asns.yml")
        if cls.result is None:  # pragma: no cover - only on a broken harness
            raise AssertionError("harness produced no result:\n" + cls.out)

    def test_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_each_fabric_resolves_its_own_asn(self):
        self.assertEqual(self.result["fabric_asn_map"],
                         {"site_group_evpn_campus": 64513, "sites_evpn_remote": 65001})

    def test_two_buildings_of_one_campus_share_it(self):
        """Both sites carry 64513, and agreeing is not a conflict."""
        for host in ("core1", "access1-1", "access2-1"):
            self.assertEqual(self.result["hosts"][host]["fabric_asn"], 64513, host)

    def test_the_other_fabric_gets_its_own(self):
        self.assertEqual(self.result["hosts"]["rcore1"]["fabric_asn"], 65001)

    def test_every_device_gets_its_own_fabrics_asn(self):
        """No global to fall back to, so every value here came from NetBox."""
        for host, data in self.result["hosts"].items():
            self.assertIn(data["fabric_asn"], (64513, 65001), host)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestFabricAsnIsRefusedWhenAmbiguous(unittest.TestCase):
    def test_sites_of_one_fabric_disagreeing_is_fatal(self):
        """The dangerous case: building_b on a different ASN would render half
        the campus into an ASN the other half does not peer in."""
        def mutate(data):
            for site in _sites(data, "access2-1"):
                site["asns"] = [{"id": 9, "asn": 64514}]
        rc, out, _ = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertNotEqual(rc, 0)
        self.assertIn("its sites disagree", out)
        self.assertIn("building_b", out)

    def test_one_site_carrying_two_asns_is_fatal(self):
        def mutate(data):
            for site in _sites(data, "core1", "access1-1"):
                site["asns"] = [{"id": 1, "asn": 64513}, {"id": 9, "asn": 64514}]
        rc, out, _ = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertNotEqual(rc, 0)
        self.assertIn("exactly one ASN", out)
        self.assertIn("building_a", out)

    def test_the_failure_says_what_to_do_about_it(self):
        def mutate(data):
            for site in _sites(data, "access2-1"):
                site["asns"] = [{"id": 9, "asn": 64514}]
        _, out, _ = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertIn("nothing to fall back to", out)

    def test_one_broken_fabric_does_not_silently_render_the_others(self):
        """Fatal for the run, not per device. A site tree that does not give one
        ASN per fabric is not trustworthy for the fabrics that happen to work."""
        def mutate(data):
            for site in _sites(data, "access2-1"):
                site["asns"] = [{"id": 9, "asn": 64514}]
        _, _, result = run_fabric("fabric_asns.yml", mutate=mutate)
        self.assertIsNone(result)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestAFabricWithNoAsnIsRefused(unittest.TestCase):
    """There is no global evpn_bgp_asn to fall back to any more.

    Falling back was the migration path; NetBox is now the only source, so a
    fabric whose sites carry no ASN fails rather than quietly rendering every
    BGP session in somebody else's number.
    """

    @classmethod
    def setUpClass(cls):
        def mutate(data):
            for site in _sites(data, "rcore1", "raccess1"):
                site["asns"] = []
        cls.rc, cls.out, cls.result = run_fabric("fabric_asns.yml", mutate=mutate)

    def test_the_run_is_refused(self):
        self.assertNotEqual(self.rc, 0)

    def test_it_names_the_fabric(self):
        self.assertIn("sites_evpn_remote", self.out)
        self.assertIn("no site in it carries an ASN object", self.out)

    def test_it_says_there_is_no_fallback(self):
        self.assertIn("no global evpn_bgp_asn to stand in", self.out)

    def test_nothing_renders(self):
        """Fatal for the run, not a per-fabric skip: a site tree that cannot
        answer this is not trustworthy for the fabrics that happen to work."""
        self.assertIsNone(self.result)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestAnUnscopedRunIsRefused(unittest.TestCase):
    """Every run must say which fabric it is for.

    Unscoped, the host pattern defaults to every fabric node in the inventory --
    with more than one fabric that is the whole estate, opening a session to
    every switch in every building and, on a deploy, applying a candidate to all
    of them. Nobody means that by omission.

    Enforced in the shared resolver, so readiness.yml, generate_evpn.yml,
    deploy_evpn.yml and trm_allocate.yml all get it from one place.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_multi.ini",
                                                 deploy_target=UNSCOPED)

    def test_the_run_is_refused(self):
        self.assertNotEqual(self.rc, 0)

    def test_nothing_is_rendered(self):
        self.assertIsNone(self.result)

    def test_it_says_what_is_wrong(self):
        self.assertIn("This run is not scoped", self.out)

    def test_it_lists_the_fabrics_to_choose_from(self):
        self.assertIn("site_group_evpn_campus", self.out)
        self.assertIn("sites_evpn_remote", self.out)

    def test_it_shows_how_to_scope(self):
        self.assertIn("-e deploy_target=", self.out)

    def test_an_empty_deploy_target_is_refused_too(self):
        """'-e deploy_target=' defines the variable as an empty string, which
        would otherwise pass an is-defined check and match nothing."""
        rc, out, _ = run_fabric("fabric_multi.ini", deploy_target="")
        self.assertNotEqual(rc, 0)
        self.assertIn("This run is not scoped", out)

    def test_targeting_everything_deliberately_is_allowed(self):
        """The point is that the estate cannot be reached by ACCIDENT."""
        rc, out, result = run_fabric("fabric_multi.ini", deploy_target="all")
        self.assertEqual(rc, 0, out)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestTheFabricNodeRule(unittest.TestCase):
    """fabric node = leaf OR border OR (distribution_switch AND tagged spine).

    It used to be a flat list -- leaf, border, tags_fabric_spine -- intersected
    with group_names at five call sites. That made the TAG ALONE sufficient, so
    tagging a WAN router 'fabric_spine' got a full EVPN candidate rendered for
    it. The rule has a conjunction in it, which no list of groups can express,
    so the set is derived once in fabric_resolve.yml task 1 and read everywhere.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, cls.out, cls.result = run_fabric("fabric_scope_rule.yml",
                                                 target_scoping=False)

    def test_the_run_succeeds(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_a_leaf_and_a_border_are_nodes_on_their_own(self):
        self.assertIn("core1", self.result["fabric_nodes"])
        self.assertIn("leaf1", self.result["fabric_nodes"])

    def test_a_tagged_distribution_switch_is_a_node(self):
        """The conjunction's positive half. dspine1 has no fabric role -- it
        qualifies only by being a distribution switch that is also tagged."""
        self.assertIn("dspine1", self.result["fabric_nodes"])

    def test_an_untagged_distribution_switch_is_not_a_node(self):
        """Still in the fabric and still checked -- it carries traffic between
        a leaf and a border -- but acm_evpn renders nothing for it."""
        self.assertNotIn("dist1", self.result["fabric_nodes"])
        self.assertEqual(self.result["hosts"]["dist1"]["fabric_id"],
                         "sites_building_a")

    def test_the_tag_alone_does_not_make_a_node(self):
        """The defect the rule closes. wan1 is tagged fabric_spine and carries
        the WAN role; under the old flat list it was a fabric node and this
        role would have replaced its entire configuration."""
        self.assertNotIn("wan1", self.result["fabric_nodes"])

    def test_a_device_with_no_role_is_not_a_node(self):
        self.assertNotIn("server1", self.result["fabric_nodes"])

    def test_the_exact_set(self):
        """Spelled out, because every assertion above is a membership test and
        a rule that admitted something extra would pass all of them."""
        self.assertEqual(sorted(self.result["fabric_nodes"]),
                         ["core1", "dspine1", "leaf1"])


@unittest.skipIf(ANSIBLE is None, "ansible-playbook is not on PATH")
class TestAFabricNeedsASpineAndABorder(unittest.TestCase):
    """Task 13. Both halves are needed and neither implies the other.

    A fabric with no spine has no anycast RP to replicate BUM traffic from; one
    with no border has no path out of itself. Neither is a switch being
    unready -- it is the fabric being incompletely described in NetBox -- so it
    is refused for the whole run rather than reported per device.
    """

    @staticmethod
    def _drop(group, host):
        def mutate(data):
            del data["all"]["children"][group]["hosts"][host]
        return mutate

    def test_a_complete_fabric_is_accepted(self):
        rc, out, _ = run_fabric("fabric_scope_rule.yml", target_scoping=False)
        self.assertEqual(rc, 0, out)

    def test_a_fabric_with_no_spine_is_refused(self):
        """core1 and dspine1 both lose the tag, so nothing is left tagged.
        core1 keeps the border role, which is what makes this the no-SPINE
        case rather than the no-anything case."""
        def mutate(data):
            data["all"]["children"]["tags_fabric_spine"]["hosts"] = {}
        rc, out, _ = run_fabric("fabric_scope_rule.yml", target_scoping=False,
                                mutate=mutate)
        self.assertNotEqual(rc, 0)
        self.assertIn("no switch is tagged fabric_spine", out)
        self.assertIn("sites_building_a", out)

    def test_a_fabric_with_no_border_is_refused(self):
        rc, out, _ = run_fabric("fabric_scope_rule.yml", target_scoping=False,
                                mutate=self._drop("device_roles_fabric_border",
                                                  "core1"))
        self.assertNotEqual(rc, 0)
        self.assertIn("no switch has the fabric border role", out)

    def test_the_message_names_what_the_fabric_does_have(self):
        """So the operator can see it is a gap rather than an empty site."""
        rc, out, _ = run_fabric("fabric_scope_rule.yml", target_scoping=False,
                                mutate=self._drop("device_roles_fabric_border",
                                                  "core1"))
        self.assertIn("spine(s): core1", out)

    def test_a_site_holding_no_fabric_node_is_not_asked_for_either(self):
        """An ordinary NetBox site -- WAN kit, servers -- is not a candidate
        fabric waiting to be built, so it must not be refused for lacking a
        spine. fabric_multi.ini carries exactly that case in sites_wan_edge."""
        rc, out, _ = run_fabric("fabric_multi.ini")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("sites_wan_edge", out)
