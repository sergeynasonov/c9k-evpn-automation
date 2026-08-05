# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""netbox_inv.yml's variable renames, driven through the real plugin code.

NetBox exposes a device's 'serial' and 'tags' as host variables and Ansible
reserves both names, so every run opened with two "Found variable using reserved
name" warnings. They are renamed rather than suppressed: 'serial' is the
batching keyword this project's backlog intends to use for canary deploys, and a
host variable shadowing it would be debugged twice.

The subtle part is the anchoring. nb_inventory matches with re.match, which
anchors at the start only, so a pattern without a trailing '$' would also rename
any variable merely BEGINNING with those names. That is asserted below rather
than reasoned about.

This drives nb_inventory's OWN parse_rename_variables and _set_variable against
the config that ships, with a stub standing in for the inventory object. No
NetBox: the rename is pure string handling, and it is the only part of the
inventory config that has logic in it at all.

It lives in acm_evpn's suite because that is where inventory semantics are
already tested (see test_fabric_resolve.py) and because CI discovers tests per
role; netbox_inv.yml itself belongs to no role.
"""

import os
import sys
import unittest

import yaml

# unit -> tests -> acm_evpn -> roles -> repo root
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
INVENTORY_CONFIG = os.path.join(REPO, "netbox_inv.yml")

try:
    from ansible_collections.netbox.netbox.plugins.inventory.nb_inventory import (
        InventoryModule,
    )
except ImportError:  # pragma: no cover - collection not installed
    InventoryModule = None

try:
    from ansible.vars.reserved import get_reserved_names
except ImportError:  # pragma: no cover
    get_reserved_names = None


class _StubInventory:
    """Stands in for Ansible's inventory; records what the plugin would set."""

    def __init__(self):
        self.variables = {}

    def set_variable(self, hostname, key, value):
        self.variables.setdefault(hostname, {})[key] = value


def _config():
    with open(INVENTORY_CONFIG) as fh:
        return yaml.safe_load(fh)


@unittest.skipIf(InventoryModule is None, "netbox.netbox collection is not installed")
class TestReservedNamesAreRenamed(unittest.TestCase):
    """The rename runs through the shipped plugin, not a reimplementation."""

    @classmethod
    def setUpClass(cls):
        cls.config = _config()

    def plugin(self):
        plugin = InventoryModule()
        plugin.rename_variables = plugin.parse_rename_variables(
            self.config.get("rename_variables")
        )
        plugin.inventory = _StubInventory()
        return plugin

    def set_var(self, key, value="x"):
        """The key nb_inventory would actually store, given our config."""
        plugin = self.plugin()
        plugin._set_variable("core1", key, value)
        return list(plugin.inventory.variables["core1"])[0]

    def test_serial_is_renamed(self):
        self.assertEqual(self.set_var("serial"), "netbox_serial")

    def test_tags_is_renamed(self):
        self.assertEqual(self.set_var("tags"), "netbox_tags")

    def test_the_value_survives_the_rename(self):
        plugin = self.plugin()
        plugin._set_variable("core1", "tags", ["fabric_spine"])
        self.assertEqual(plugin.inventory.variables["core1"]["netbox_tags"],
                         ["fabric_spine"])

    def test_a_variable_merely_starting_with_tags_is_untouched(self):
        """re.match anchors at the start only, so without a trailing '$' this
        would be renamed to 'netbox_tags_fabric_spine'."""
        self.assertEqual(self.set_var("tags_fabric_spine"), "tags_fabric_spine")

    def test_a_variable_merely_starting_with_serial_is_untouched(self):
        self.assertEqual(self.set_var("serial_number"), "serial_number")

    def test_unrelated_variables_are_untouched(self):
        for key in ("ansible_host", "sites", "primary_ip4", "device_roles"):
            self.assertEqual(self.set_var(key), key)


@unittest.skipIf(get_reserved_names is None, "ansible is not importable")
class TestTheRenamesTargetRealCollisions(unittest.TestCase):
    """Guards the premise, not the mechanism.

    If a future Ansible stopped reserving one of these, the rename would be
    churn; if it started reserving another variable nb_inventory sets, the
    warnings would come back. Both are worth being told about.
    """

    def setUp(self):
        self.renamed = {item["pattern"].strip("^$")
                        for item in _config().get("rename_variables", [])}

    def test_every_renamed_name_is_actually_reserved(self):
        for name in self.renamed:
            self.assertIn(name, get_reserved_names(),
                          "{} is renamed but Ansible does not reserve it".format(name))

    def test_no_other_variable_nb_inventory_sets_is_reserved(self):
        """The host variables nb_inventory can set, from its own docs and
        extractors. Kept explicit: a new collision should fail here rather than
        reappear as a warning nobody reads."""
        emitted = {
            "ansible_host", "primary_ip4", "primary_ip6", "oob_ip", "serial",
            "tags", "sites", "device_roles", "device_types", "manufacturers",
            "platforms", "racks", "tenants", "locations", "regions",
            "site_groups", "cluster", "config_context", "custom_fields",
            "services", "interfaces", "dns_name", "status", "rack_face",
            "rack_position", "virtual_chassis", "vcpus", "memory", "disk",
        }
        collisions = sorted((emitted & set(get_reserved_names())) - self.renamed)
        self.assertEqual(collisions, [],
                         "nb_inventory sets reserved name(s) {} that are not "
                         "renamed in netbox_inv.yml".format(collisions))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
