# SPDX-License-Identifier: MIT
"""Which combinations of cases/test_web_nat.py the short NAT profile (DVT_NAT_SHORT=1) runs. Pure functions, no I/O,
so the plan is unit tested (cases/unit/test_nat.py); the case module only applies it. The whole matrix takes about
an hour with TURN on; the short profile is meant to finish in 20 minutes (docs/web-nat-cases.md "Short profile")."""

FULL_MATRIX = {"chromium": "port_restricted", "webkit": "symmetric"}   # engine -> the one NAT type that gets every break
SHORT_BREAKS = (2, 20)                       # all other combinations: the shortest break and one clearly lost
RELAY = {("chromium", "symmetric")}          # test_turn_relay
EXPIRY = {("chromium", "symmetric")}         # test_turn_credential_expiry
LEFT_OUT = ("test_direct_listen", "test_talk_is_released_at_disconnected_and_needs_a_fresh_press")


def breaks(engine, nat_type, every, short):
    """The blackhole lengths (s) for one engine / NAT type: `every` (all of them), or in the short profile `every`
    only for the engine's one NAT type of FULL_MATRIX and SHORT_BREAKS for the other combinations."""
    if short and FULL_MATRIX.get(engine) != nat_type:
        return tuple(SHORT_BREAKS)
    return tuple(every)


def wanted(test, engine, nat_type, short):
    """False for a case the short profile leaves out (always True when `short` is false)."""
    if not short:
        return True
    if test in LEFT_OUT:
        return False
    if test == "test_turn_relay":
        return (engine, nat_type) in RELAY
    if test == "test_turn_credential_expiry":
        return (engine, nat_type) in EXPIRY
    return True
