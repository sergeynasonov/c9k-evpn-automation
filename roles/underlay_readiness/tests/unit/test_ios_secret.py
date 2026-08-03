# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Unit tests for Cisco type-9 secret verification.

Run from the repo root:
    venv/bin/python -m unittest discover -s roles/underlay_readiness/tests/unit
"""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    + "/filter_plugins",
)

from ios_secret import (  # noqa: E402
    DIGEST_LEN,
    SALT_LEN,
    ios_type9_hash,
    ios_type9_matches,
    ios_type9_parse,
    ios_type9_problem,
)

# A real hash from this fabric, with its known plaintext. This is the anchor:
# if the scrypt parameters or the base64 alphabet were wrong, this fails.
REAL_HASH = "$9$MLxnNDtApO4k5k$2JqQump1bnExqwiX6g6v9E3gc2lwOJBPkSja2HzMXE2"
REAL_PLAINTEXT = "C!sco123"
REAL_SALT = "MLxnNDtApO4k5k"


class TestAlgorithm(unittest.TestCase):
    def test_matches_a_known_device_hash(self):
        self.assertTrue(ios_type9_matches(REAL_HASH, REAL_PLAINTEXT))

    def test_reproduces_the_digest_exactly(self):
        self.assertEqual(
            ios_type9_hash(REAL_PLAINTEXT, REAL_SALT), REAL_HASH.split("$")[3])

    def test_digest_length_is_43(self):
        self.assertEqual(len(ios_type9_hash("anything", REAL_SALT)), DIGEST_LEN)

    def test_salt_changes_the_digest(self):
        """Type 9 is salted, which is the whole reason the candidate config
        could never be compared against the device's own username line."""
        other = ios_type9_hash(REAL_PLAINTEXT, "AAAAAAAAAAAAAA")
        self.assertNotEqual(other, REAL_HASH.split("$")[3])

    def test_uses_the_cisco_alphabet_not_rfc4648(self):
        """'+' and '=' never appear in a Cisco digest; '.' and '/' do."""
        digest = ios_type9_hash(REAL_PLAINTEXT, REAL_SALT)
        self.assertNotIn("+", digest)
        self.assertNotIn("=", digest)

    def test_wrong_salt_length_is_rejected(self):
        with self.assertRaises(ValueError):
            ios_type9_hash("x", "tooshort")


class TestMatching(unittest.TestCase):
    def test_wrong_password_does_not_match(self):
        self.assertFalse(ios_type9_matches(REAL_HASH, "nope"))

    def test_empty_password_does_not_match(self):
        for pw in ("", None):
            self.assertFalse(ios_type9_matches(REAL_HASH, pw))

    def test_malformed_secrets_never_match(self):
        """Fail closed: anything unparseable must not be pushed either."""
        for bad in (
            None, "", "notahash", "$5$salt$digest", "$8$salt$digest",
            "$9$tooshort$" + "x" * DIGEST_LEN,
            "$9$" + "a" * SALT_LEN + "$short",
            "$9$" + "a" * SALT_LEN,
        ):
            self.assertFalse(ios_type9_matches(bad, REAL_PLAINTEXT), bad)

    def test_plaintext_password_is_not_mistaken_for_a_hash(self):
        """The trap this guards: someone puts the password itself into
        ios_password_secret_hash, and it gets pushed verbatim as the hash."""
        self.assertFalse(ios_type9_matches(REAL_PLAINTEXT, REAL_PLAINTEXT))


class TestParse(unittest.TestCase):
    def test_parses_a_valid_secret(self):
        self.assertEqual(
            ios_type9_parse(REAL_HASH), (REAL_SALT, REAL_HASH.split("$")[3]))

    def test_returns_none_for_other_hash_types(self):
        self.assertIsNone(ios_type9_parse("$8$" + "a" * SALT_LEN + "$" + "b" * DIGEST_LEN))
        self.assertIsNone(ios_type9_parse("$5$abc$def"))


class TestProblemReporting(unittest.TestCase):
    """The task layer decides policy; this module only reports facts. The two
    failure modes need different advice, so they must be distinguishable."""

    def test_correct_hash_reports_no_problem(self):
        self.assertEqual(ios_type9_problem(REAL_HASH, REAL_PLAINTEXT), "")

    def test_unset_hash_is_not_a_problem(self):
        """Empty means the template falls back to 'secret 0 <plaintext>', which
        the device re-hashes itself -- valid, just noisier."""
        for empty in (None, ""):
            self.assertEqual(ios_type9_problem(empty, REAL_PLAINTEXT), "")

    def test_malformed_and_mismatched_report_differently(self):
        malformed = ios_type9_problem("$8$abc$def", REAL_PLAINTEXT)
        mismatched = ios_type9_problem(REAL_HASH, "wrong")
        self.assertIn("well-formed", malformed)
        self.assertIn("does NOT match", mismatched)
        self.assertNotEqual(malformed, mismatched)

    def test_no_problem_message_leaks_the_secret(self):
        for msg in (ios_type9_problem("$8$abc$def", REAL_PLAINTEXT),
                    ios_type9_problem(REAL_HASH, "wrong")):
            self.assertNotIn(REAL_PLAINTEXT, msg)
            self.assertNotIn(REAL_HASH, msg)


if __name__ == "__main__":
    unittest.main()
