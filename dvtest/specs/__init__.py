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


def m17_fn0_relayed(target):
    """Does the target's sut relay M17 packet 0 (frame number 0)? One
    source for B17 and the M17 passthrough cells: the profile's
    test_m17_frame0_relayed entry; False under the xlxd profile."""
    return (rule(target, "test_m17_frame0_relayed") or {}).get("fn0") == "relay"


RADIO_ID_MODES = ("drop", "fallback", "local")


def rule_mode(target, proto, nn):
    """The profile's spec entry for item nn of a protocol dict (e.g.
    "mmdvm", "P-DMR-03"), honouring DVT_NO_RADIO_ID: under "fallback" or
    "local" the entry "<nn>@<mode>" wins when present; "drop" (the default)
    uses "<nn>". None under the xlxd profile or when absent."""
    mode = target.no_radio_id or "drop"
    if mode not in RADIO_ID_MODES:
        raise ValueError("DVT_NO_RADIO_ID=%r; known: %s" % (mode, ", ".join(RADIO_ID_MODES)))
    entry = rule(target, proto) or {}
    if mode != "drop" and "%s@%s" % (nn, mode) in entry:
        return entry["%s@%s" % (nn, mode)]
    return entry.get(nn)


def rule(target, case):
    """Expectation of the target's profile for a behaviour case: None under
    the xlxd profile (differential against xlxd-ref), else the spec entry
    (None while pending)."""
    return expectation(target.sut_profile, case)
