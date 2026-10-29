# SPDX-License-Identifier: MIT
"""Expected behaviour of a system under test that is not an xlxd clone.

DVT_SUT_PROFILE (default "xlxd") picks the rule for cases marked
`behaviour` (floor, late entry, stream timeout/terminators, revoke tail,
zero fill, TOT, load loss). With "xlxd" they stay differential: the sut
must behave as xlxd-ref. With another name they read the expectation from
dvtest/specs/<name>.py: a module with SPEC = {case name: expectation},
where the case name is the test function name; a behaviour case without
an entry skips with "spec pending". Cases marked `interop` (wire format,
handshakes, keepalives, framing, codec routing and bytes, stream ids, no
re-forwarding, no echo) are always judged against xlxd-ref.
"""
import importlib

DEFAULT = "xlxd"


def load(name):
    """The spec module of a profile; ValueError for an unknown one."""
    if name == DEFAULT:
        return None
    if not name.isidentifier():
        raise ValueError("invalid DVT_SUT_PROFILE %r" % name)
    try:
        return importlib.import_module("dvtest.specs." + name)
    except ModuleNotFoundError as exc:
        raise ValueError("no spec module dvtest/specs/%s.py for DVT_SUT_PROFILE=%s" % (name, name)) from exc


def expectation(name, case):
    """The profile's expectation for a behaviour case, or None (pending)."""
    module = load(name)
    return None if module is None else getattr(module, "SPEC", {}).get(case)
