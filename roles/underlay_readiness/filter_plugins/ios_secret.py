# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sergey Nasonov, Cisco Systems <snasonov@cisco.com>
"""Verify a Cisco IOS type-9 secret against its plaintext, on the control node.

Why this exists
---------------
The platform baseline renders ``username <admin> privilege 15 secret 9 <hash>``
from ``local_admin_secret_hash``. A WRONG hash is the one failure mode that
defeats every other safeguard in this project:

* the device ACCEPTS the command -- a type-9 hash is opaque, so there is nothing
  to reject -- and ``ios_config`` reports ``changed`` with no error;
* the existing SSH session stays up, so nothing fails during the run;
* IOS never renders the plaintext back, so no assertion against the running
  config can catch it;
* the lockout only appears on the NEXT connection, by which point every switch
  in the run has it.

Batching does not help, because nothing fails at a batch boundary. The only
place this can be caught is BEFORE anything is pushed, by checking the hash
against the plaintext that the automation itself logs in with -- both of which
are already available from Infisical. No device interaction at all.

Algorithm
---------
Type 9 is scrypt(N=16384, r=1, p=1, dkLen=32). The 14-character salt is used as
raw ASCII rather than being decoded, and the digest is re-encoded with Cisco's
own base64 alphabet, which orders ``./`` first instead of putting ``+/`` last.

Format: ``$9$<14-char salt>$<43-char digest>``
"""

import base64
import hashlib
import hmac

__metaclass__ = type

# Cisco's alphabet differs from RFC 4648 only in ordering, so a translation of
# the standard output is exact -- no bit-twiddling, nothing to get wrong.
_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CISCO_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_TO_CISCO = str.maketrans(_STD_B64, _CISCO_B64)

SCRYPT_N = 16384
SCRYPT_R = 1
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_LEN = 14
DIGEST_LEN = 43
# 128 * N * r is the working set (2 MiB here); give scrypt room without letting
# a malformed cost parameter allocate unbounded memory.
SCRYPT_MAXMEM = 4 * 1024 * 1024


def ios_type9_hash(plaintext, salt):
    """Return the type-9 digest of ``plaintext`` under ``salt``."""
    if plaintext is None:
        raise ValueError("plaintext is required")
    if not salt or len(salt) != SALT_LEN:
        raise ValueError(
            "salt must be exactly %d characters, got %r" % (SALT_LEN, salt))
    derived = hashlib.scrypt(
        plaintext.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM,
    )
    return base64.b64encode(derived).decode("ascii").rstrip("=").translate(_TO_CISCO)


def ios_type9_parse(secret):
    """Split ``$9$salt$digest`` into (salt, digest), or return None if it is not
    a well-formed type-9 secret.

    Returning None rather than raising lets the caller distinguish 'this is not
    a type-9 hash at all' (e.g. someone pasted a type-8, or a plaintext) from
    'this is a type-9 hash of a different password'. They need different advice.
    """
    if not secret or not isinstance(secret, str):
        return None
    parts = secret.split("$")
    # '$9$salt$digest' -> ['', '9', salt, digest]
    if len(parts) != 4 or parts[0] != "" or parts[1] != "9":
        return None
    _, _, salt, digest = parts
    if len(salt) != SALT_LEN or len(digest) != DIGEST_LEN:
        return None
    return salt, digest


def ios_type9_matches(secret, plaintext):
    """True if ``secret`` is the type-9 hash of ``plaintext``.

    False for a malformed secret as well as a mismatched one -- either way the
    line must not be pushed. Use ios_type9_parse() when the two cases need to be
    reported differently.
    """
    parsed = ios_type9_parse(secret)
    if parsed is None:
        return False
    salt, digest = parsed
    try:
        return hmac.compare_digest(ios_type9_hash(plaintext or "", salt), digest)
    except ValueError:
        return False


def ios_type9_problem(secret, plaintext):
    """Return a human explanation of why ``secret`` is unusable, or '' if it is
    fine. Kept separate from the boolean so the task layer decides policy and
    this module only reports facts -- the same split the rest of the project
    uses for its filter plugins.
    """
    if not secret:
        return ""  # not set: the template falls back to 'secret 0 <plaintext>'
    if ios_type9_parse(secret) is None:
        return (
            "is not a well-formed type-9 secret (expected '$9$<%d-char salt>$"
            "<%d-char digest>'). A type-5 or type-8 hash, or a plaintext "
            "password, will be pushed verbatim and lock the automation out."
            % (SALT_LEN, DIGEST_LEN)
        )
    if not ios_type9_matches(secret, plaintext):
        return (
            "is a valid type-9 hash but does NOT match the plaintext password "
            "the automation logs in with. Pushing it would change the local "
            "admin password on every switch to something unknown."
        )
    return ""


class FilterModule(object):
    def filters(self):
        return {
            "ios_type9_hash": ios_type9_hash,
            "ios_type9_matches": ios_type9_matches,
            "ios_type9_problem": ios_type9_problem,
        }
