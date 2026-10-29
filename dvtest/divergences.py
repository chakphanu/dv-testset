# SPDX-License-Identifier: MIT
"""Declared divergences of the system under test from xlxd-ref (legacy).

Legacy: superseded by DVT_SUT_PROFILE and dvtest/specs/ (a sut that is not
an xlxd clone gets its own behaviour spec). Kept working for existing runs;
no new names are added here.

A reflector under test may differ from the reference on purpose. It
declares that with DVT_SUT_DIVERGENCES (comma separated names, spaces
ignored) or the TOML key sut_divergences (list or comma string); cases
then expect the declared behaviour on the sut side only. Everything not
declared must equal xlxd-ref. An unknown name is an error.

TABLE: name -> (sut behaviour when declared, xlxd-ref reference).
"""
import os

TABLE = {
    "late_entry_transcoded": (
        "a late entrant (header inside the previous stream's close window) is relayed with "
        "transcoding, cross-mode listeners hear it",
        "native only: creflector.cpp:660-745"),
    "immediate_revoke": (
        "egress to a client stops as soon as it leaves or disconnects, no 1.0 s tail",
        "client cache: cdextraprotocol.h:43, cdmrmmdvmprotocol.h:52"),
    "no_zero_fill": (
        "a transcoded output that misses its deadline is dropped, never sent as all-zero codec frames",
        "zero fill: ccodecstream.cpp:867-889"),
    "tot_180s": (
        "continuous transmission is capped at 180 s, then the stream is closed",
        "xlxd has no time-out timer"),
    "synthesized_terminator": (
        "a stream closed by the 1.6 s silence timeout gets a terminator / last frame on egress",
        "no last frame: cpacketstream.h:38, cprotocol.cpp:391-404"),
    "ambed_extended_open_always": (
        "24 B extended OPEN for every stream once CAPS was answered",
        "extended OPEN for PCM streams only: ctranscoder (PR #272)"),
    "no_load_loss": (
        "no frame loss under multi-module load",
        "frames dropped at 26 modules (measured by C9)"),
}


# names a DVT_SUT_PROFILE implies (its behaviour spec covers them); an
# unknown profile is an error
PROFILES = {
    "xlxd": (),
    "dvrouter": ("ambed_extended_open_always", "immediate_revoke", "no_zero_fill", "synthesized_terminator",
                 "no_load_loss"),
}


def implied(profile):
    """Names a profile implies; ValueError for an unknown profile."""
    if profile not in PROFILES:
        raise ValueError("unknown DVT_SUT_PROFILE %r; known: %s" % (profile, ", ".join(sorted(PROFILES))))
    return set(PROFILES[profile])


def parse(value):
    """Set of names from a comma string or a list; ValueError on unknown."""
    if isinstance(value, str):
        value = value.split(",")
    names = {str(v).strip() for v in value or ()} - {""}
    unknown = sorted(names - set(TABLE))
    if unknown:
        raise ValueError("unknown divergence(s) %s; known: %s" % (", ".join(unknown), ", ".join(sorted(TABLE))))
    return names


def load(env=None):
    """Declared names from DVT_SUT_DIVERGENCES (default os.environ)."""
    env = os.environ if env is None else env
    return parse(env.get("DVT_SUT_DIVERGENCES", ""))


def effective(env=None):
    """Declared names plus the names DVT_SUT_PROFILE implies."""
    env = os.environ if env is None else env
    return load(env) | implied(env.get("DVT_SUT_PROFILE") or "xlxd")
