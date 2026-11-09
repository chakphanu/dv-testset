# SPDX-License-Identifier: MIT
"""The RF transmit-guard wait for the cases (dvtest/txguard.py, radio.md 10.4 rule 4): `make_guard` returns a callable
`guard(case)` that a case calls before its first expected transmission (and after any reload or edge restart); it waits until the
AX.25 port is not fenced, records one [RECORD] line, and never sleeps for a fixed time. The admin session (operator) is taken
lazily from the `admin_session` fixture; without one the guard records that and returns (no wait is possible)."""
import pytest

from dvtest import txguard


def make_guard(request, note, port="rf-a", kind="ax25"):
    """note(case, tag, text) writes a line; the first call opens the status session."""
    state = {"read": None, "tried": False}

    def reader():
        if not state["tried"]:
            state["tried"] = True
            try:
                if kind == "radio":
                    state["read"] = txguard.radio_reader(request.getfixturevalue("web_client"), port)
                else:
                    state["read"] = txguard.ax25_reader(request.getfixturevalue("admin_session")("operator"), port)
            except pytest.skip.Exception as exc:
                note("txguard", "RECORD", "no status session for the TX guard wait (%s)" % exc)
        return state["read"]

    def guard(case):
        read = reader()
        if read is None:
            return txguard.Result("no_reader", 0, None, None, [], 0)
        result = txguard.wait_unfenced(read, kind=kind)
        note(case, "FINDING" if result.state in ("timeout", "not_found") else "RECORD", txguard.describe(result, kind, port))
        return result

    return guard
