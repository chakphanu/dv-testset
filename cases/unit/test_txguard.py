# SPDX-License-Identifier: MIT
"""dvtest/txguard.py and cases/txguard_support.py: the RF transmit-guard wait. Fake clocks and readers, offline."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import txguard_support  # noqa: E402

from dvtest import txguard  # noqa: E402

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.t = 100.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def state(tx_state="idle", fence=None, guard_ms=None, extra=True, name="ic705"):
    port = {"name": name, "kind": "icom-terminal", "tx": True}
    if extra:
        port.update({"tx_state": tx_state, "fence": fence, "guard_ms": guard_ms})
    return {"summary": {}, "procs": [{"kind": "edge", "proc": "dv-edge-radio", "up": True, "radio": {"callsign": "N0CALL", "ports": [port]}}]}


def body(fence=None, guard=None, name="rf-a", nested=True):
    modem = {"name": name, "adapter": "kiss-tcp", "connected": True}
    if fence:
        modem.update({"tx_fence": fence, "tx_guard_ms": guard})
    return {"total": 1, "items": [{"kind": "modem", "modem": modem}]} if nested else {"x": [{"y": modem}]}


def run(seq, kind, observe, **kw):
    clock = Clock()
    it = iter(seq)
    last = [None]

    def read():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return observe(last[0])
    return txguard.wait_unfenced(read, kind=kind, clock=clock.now, sleep=clock.sleep, **kw)


def test_radio_observation_reads_the_documented_fields():
    obs = txguard.radio_observation(state("fenced", "startup_guard", 12000), "ic705")
    assert (obs.found, obs.present, obs.fenced, obs.fence, obs.guard_ms) == (True, True, True, "startup_guard", 12000)
    assert any("ports[ic705]" in k and "tx_state" in k for k in obs.keys)
    assert txguard.radio_observation(state("idle"), "ic705").fenced is False
    assert txguard.radio_observation(state("idle"), "other").found is False
    assert txguard.radio_observation({"summary": {}}, "ic705").found is False
    assert txguard.radio_observation([], "ic705").found is False


def test_ax25_observation_reads_nested_and_loose_shapes():
    obs = txguard.ax25_observation(body("startup_guard", 9000), "rf-a")
    assert (obs.found, obs.present, obs.fenced, obs.fence, obs.guard_ms) == (True, True, True, "startup_guard", 9000)
    assert txguard.ax25_observation(body(), "rf-a").present is False
    assert txguard.ax25_observation(body("session_loss", 100, nested=False), "rf-a").fence == "session_loss"
    assert txguard.ax25_observation(body(), "rf-b").found is False
    assert txguard.ax25_observation({"items": []}, "rf-a").found is False


def test_radio_fenced_then_unfenced_waits_without_a_fixed_sleep():
    seq = [state("fenced", "startup_guard", 3000)] * 4 + [state("idle")]
    res = run(seq, "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "unfenced" and res.fence == "startup_guard" and res.guard_ms == 3000
    assert res.polls == 5 and res.waited_ms == 4 * int(txguard.POLL_S * 1000)


def test_ax25_fenced_then_unfenced():
    seq = [body("startup_guard", 2000)] * 3 + [body()]
    res = run(seq, "ax25", lambda b: txguard.ax25_observation(b, "rf-a"))
    assert res.state == "unfenced" and res.fence == "startup_guard" and res.polls == 4


def test_not_fenced_at_the_first_read_returns_at_once():
    res = run([state("idle")], "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "clear" and res.polls == 1 and res.waited_ms == 0


def test_absent_field_returns_at_once_on_an_old_image():
    res = run([state(extra=False)], "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "absent" and res.polls == 1 and res.waited_ms == 0
    res = run([body()], "ax25", lambda b: txguard.ax25_observation(b, "rf-a"))
    assert res.state == "absent" and res.polls == 1
    text = txguard.describe(res, "ax25", "rf-a")
    assert "present without tx_fence" in text and "kind=modem items[].modem" in text


def test_timeout_when_the_fence_never_ends_and_the_bound_is_capped():
    res = run([state("fenced", "recovery_probe", None)], "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "timeout" and txguard.STARTUP_GUARD_S * 1000 <= res.waited_ms <= txguard.MAX_WAIT_S * 1000
    # a huge reported guard raises the bound only up to MAX_WAIT_S
    res = run([state("fenced", "startup_guard", 600000)], "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "timeout" and res.waited_ms <= txguard.MAX_WAIT_S * 1000 + 500


def test_a_reported_guard_extends_the_default_bound():
    seq = [state("fenced", "startup_guard", 17000)] * 33 + [state("idle")]          # 16 s of polls: above 13 s + 2 s, below 20 s
    res = run(seq, "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "unfenced"


def test_not_found_port_is_reported_after_the_bound():
    res = run([state(name="other")], "radio", lambda s: txguard.radio_observation(s, "ic705"))
    assert res.state == "not_found" and res.keys


def test_describe_names_state_fence_guard_and_keys():
    res = run([state("fenced", "startup_guard", 500), state("idle")], "radio", lambda s: txguard.radio_observation(s, "ic705"))
    text = txguard.describe(res, "radio", "ic705")
    assert "the fence ended" in text and "startup_guard" in text and "500" in text and "tx_state" in text


def test_make_guard_records_and_handles_a_missing_session():
    lines = []
    note = lambda case, tag, text: lines.append((case, tag, text))
    # no admin session: the fixture skips; the guard records and returns no_reader
    request = SimpleNamespace(getfixturevalue=lambda name: (_ for _ in ()).throw(pytest.skip.Exception("no admin logins")))
    guard = txguard_support.make_guard(request, note)
    assert guard("DP15-6c").state == "no_reader"
    assert guard("DP15-6c").state == "no_reader" and sum("no status session" in t for _c, _g, t in lines) == 1
    # a session whose clients route reports a fence, then none
    replies = iter([body("startup_guard", 1000), body()])

    class Admin:
        def path(self, suffix):
            return "/api/v1/admin" + suffix

        def get(self, path, expect=None):
            assert path.endswith("/clients")
            return SimpleNamespace(status=200, json=lambda b=next(replies): b)

    request = SimpleNamespace(getfixturevalue=lambda name: (lambda role: Admin()) if name == "admin_session" else None)
    lines.clear()
    res = txguard_support.make_guard(request, note)("DP15-6c")
    assert res.state == "unfenced" and lines[-1][1] == "RECORD" and "startup_guard" in lines[-1][2]


def test_ax25_reader_opens_the_step_up_window_once_on_403():
    calls = []

    class Admin:
        def path(self, suffix):
            return suffix

        def step_up(self):
            calls.append("step_up")

        def get(self, path, expect=None):
            calls.append("get")
            status = 403 if calls.count("get") == 1 else 200
            return SimpleNamespace(status=status, json=lambda: body("startup_guard", 10))

    obs = txguard.ax25_reader(Admin(), "rf-a")()
    assert calls == ["get", "step_up", "get"] and obs.fence == "startup_guard"
    other = SimpleNamespace(path=lambda s: s, get=lambda p, expect=None: SimpleNamespace(status=500, json=lambda: {}))
    assert txguard.ax25_reader(other, "rf-a")().found is False


def viewer_body():
    """What a viewer or observer credential gets: the modem session row without the `modem` object."""
    return {"total": 1, "items": [{"kind": "modem", "protocol": "ax25", "circuit": "c1"}]}


def test_ax25_rows_are_matched_by_kind_modem_and_the_port_name():
    mixed = {"items": [{"kind": "dstar", "modem": {"name": "rf-a", "tx_fence": "startup_guard"}},      # not a modem row: ignored
                       {"kind": "modem", "modem": {"name": "rf-b", "adapter": "kiss-tcp"}},
                       {"kind": "modem", "modem": {"name": "rf-a", "adapter": "kiss-tcp", "tx_fence": "session_loss", "tx_guard_ms": 700}}]}
    obs = txguard.ax25_observation(mixed, "rf-a")
    assert (obs.found, obs.fenced, obs.fence, obs.guard_ms, obs.via) == (True, True, "session_loss", 700, "kind=modem items[].modem")
    other = txguard.ax25_observation(mixed, "rf-c")
    assert other.found is False and other.scope is None and "port not among them" in other.via
    # the loose walker is a fallback only and says so
    loose = txguard.ax25_observation({"x": [{"y": {"name": "rf-a", "adapter": "kiss-tcp", "tx_fence": "foreign_holder"}}]}, "rf-a")
    assert loose.found and loose.via == "fallback walker" and loose.fence == "foreign_holder"


def test_modem_row_without_a_modem_object_is_a_scope_problem_not_unfenced():
    obs = txguard.ax25_observation(viewer_body(), "rf-a")
    assert obs.found is False and obs.scope == "no_modem_object"
    res = run([viewer_body()], "ax25", lambda b: txguard.ax25_observation(b, "rf-a"))
    assert res.state == "scope" and res.polls == 1 and res.waited_ms == 0
    assert "CREDENTIAL/SCOPE" in txguard.describe(res, "ax25", "rf-a")


def test_make_guard_fails_with_a_clear_message_on_a_scope_problem():
    lines = []

    class Viewer:
        def path(self, suffix):
            return suffix

        def get(self, path, expect=None):
            return SimpleNamespace(status=200, json=viewer_body)

    request = SimpleNamespace(getfixturevalue=lambda name: (lambda role: Viewer()))
    guard = txguard_support.make_guard(request, lambda case, tag, text: lines.append((case, tag, text)))
    with pytest.raises(pytest.fail.Exception, match="CREDENTIAL/SCOPE"):
        guard("DP15-6c")
    assert lines and lines[-1][1] == "FINDING"


def test_no_modem_row_at_all_returns_at_once_and_says_which_case_it_was():
    res = run([{"total": 1, "items": [{"kind": "dstar"}]}], "ax25", lambda b: txguard.ax25_observation(b, "rf-a"))
    assert res.state == "no_modem" and res.polls == 1 and res.waited_ms == 0 and res.via == "no modem rows"
    assert "not a modem session" in txguard.describe(res, "ax25", "rf-a")
    # and the other quiet return: the modem object is present without a fence
    res = run([body()], "ax25", lambda b: txguard.ax25_observation(b, "rf-a"))
    assert res.state == "absent" and "present without tx_fence" in txguard.describe(res, "ax25", "rf-a")
