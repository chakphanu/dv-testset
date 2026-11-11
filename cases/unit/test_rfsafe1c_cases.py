# SPDX-License-Identifier: MIT
"""cases/test_rfsafe1c.py against fakes: the restart flow (status, events, public feed, log), the judging cases and the skips. No rig, no sleeps."""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_rfsafe1c as t1c  # noqa: E402

from dvtest import txguard  # noqa: E402

pytestmark = pytest.mark.unit

OLD, NEW = 100, 200


class Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def json(self):
        return self._body


class FakeAdmin:
    """operator + admin in one: /status answers the scripted bodies in turn, /events the scripted history."""

    def __init__(self, statuses, events, events_status=200):
        self.statuses, self.events, self.events_status = list(statuses), events, events_status
        self.posts, self.steps = [], 0

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def get(self, path, expect=None):
        if path.endswith("/status"):
            body = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return Resp(200, body)
        if "/events?limit=1000" in path:
            return Resp(self.events_status, {"boot": "a", "last_id": 20, "items": self.events})
        if "/events?limit=1" in path:
            return Resp(200, {"boot": "a", "last_id": 10, "items": []})
        raise AssertionError(path)

    def post(self, path, body, expect=None):
        self.posts.append(path)
        return Resp(200, {})

    def step_up(self):
        self.steps += 1


def status(pid, since, row_pid, row_since):
    return {"launcher": {"children": [{"name": "dv-edge-ax25", "pid": pid, "restarts": 1, "since_ms": since, "state": "up"}]},
            "procs": [{"kind": "edge", "proc": "ax25", "up": True, "source": "token", "restarts": 1, "pid": row_pid, "since_ms": row_since}]}


class FakeSse:
    frames = []
    error = None

    def __init__(self, client, path, seconds=120.0):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ready(self, timeout=10.0):
        return True

    def snapshot(self):
        return [(0.0, f) for f in self.frames]


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setattr(t1c.ds, "SseWatch", FakeSse)
    monkeypatch.setattr(t1c.txguard, "wait_unfenced", lambda read, **kw: txguard.Result("unfenced", 1000, "startup_guard", 0, [], 3))
    monkeypatch.setattr(t1c.txguard, "ax25_reader", lambda admin, port: (lambda: None))
    monkeypatch.setattr(t1c, "note", lambda *a: notes.append(a))
    notes = []
    target = types.SimpleNamespace(out=str(tmp_path))
    edge = types.SimpleNamespace(physicals={"radio1"})
    return target, edge, notes, tmp_path


class Clock:
    t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(flow, statuses, events, frames=(), events_status=200):
    target, edge, notes, _ = flow
    clk = Clock()
    FakeSse.frames = list(frames)
    admin = FakeAdmin(statuses, events, events_status)
    edge.operator = admin
    out = t1c.run_restart(target, edge, admin, object(), sleep=lambda s: None, settle_s=0, wait_kw={"clock": clk, "sleep": clk.sleep})
    return out, admin


def ev(i, typ, name):
    return {"id": i, "type": typ, "ev": name, "ts_ms": i}


def test_restart_flow_collects_the_three_observations(flow):
    stale = status(NEW, 2000, OLD, 1000)
    statuses = [status(OLD, 1000, OLD, 1000), stale, stale, status(NEW, 2000, NEW, 2000)]
    out, admin = run(flow, statuses, [ev(12, "session", "snapshot"), ev(11, "status", "edge_up"), ev(9, "session", "update")],
                     [{"event": "hello", "data": "{}"}, {"event": "session", "id": "a-11", "data": json.dumps({"ev": "snapshot"})}])
    assert admin.posts == ["/api/v1/admin/children/dv-edge-ax25/restart"] and admin.steps == 1 and out.restart_status == 200
    assert out.origin.problems == [] and len(out.origin.stale) == 1
    assert out.baseline == 10 and out.events.seen == 2 and out.events.hints == [] and out.public.seen == 1 and out.public.hints == []
    assert out.log is None                                                           # no SUT log yet: the needle file is left for the host
    assert (Path(flow[3]) / "logscan" / "RF1c-probe-failed.json").is_file()


def test_restart_flow_sees_a_hint_in_either_feed(flow):
    s = [status(OLD, 1000, OLD, 1000), status(NEW, 2000, NEW, 2000)]
    out, _ = run(flow, s, [ev(15, "session", "update")], [{"event": "session", "id": "a-16", "data": json.dumps({"ev": "update", "edge": "ax25"})}])
    assert [h["id"] for h in out.events.hints] == [15] and [h["id"] for h in out.public.hints] == ["a-16"]
    with pytest.raises(AssertionError, match="admin event history"):
        t1c.test_rf1c_2_no_session_update_hint_in_the_feeds(None, flow[0], out)
    out.events.hints.clear()
    with pytest.raises(AssertionError, match="public change feed"):
        t1c.test_rf1c_2_no_session_update_hint_in_the_feeds(None, flow[0], out)
    out.public.hints.clear()
    t1c.test_rf1c_2_no_session_update_hint_in_the_feeds(None, flow[0], out)


def test_restart_flow_a_row_that_never_catches_up_fails_4b(flow):
    stuck = status(NEW, 2000, OLD, 1000)
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), stuck], [])
    with pytest.raises(AssertionError, match="did not reach the launcher child's pid within 15 s"):
        t1c.test_rf1c_4b_a_restarted_edge_row_catches_up_to_the_launcher_child(None, flow[0], out)
    assert any("PREVIOUS child" in n[3] for n in flow[2])


def test_restart_flow_a_wrong_since_ms_fails_4b(flow):
    bad = status(NEW, 2000, NEW, 1000)
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), bad], [])
    with pytest.raises(AssertionError, match="since_ms"):
        t1c.test_rf1c_4b_a_restarted_edge_row_catches_up_to_the_launcher_child(None, flow[0], out)


def test_2_reports_a_failed_history_read_and_an_empty_history(flow):
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), status(NEW, 2000, NEW, 2000)], [], events_status=503)
    with pytest.raises(BaseException, match="HTTP 503"):
        t1c.test_rf1c_2_no_session_update_hint_in_the_feeds(None, flow[0], out)
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), status(NEW, 2000, NEW, 2000)], [])
    t1c.test_rf1c_2_no_session_update_hint_in_the_feeds(None, flow[0], out)
    assert any(n[2] == "FINDING" and "proves little" in n[3] for n in flow[2])


GOOD = "RF TX fenced on radio1: probe failed part=link_check; recovery is automatic"


def test_3_skips_without_logs_and_judges_with_them(flow):
    target, edge, notes, tmp = flow
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), status(NEW, 2000, NEW, 2000)], [])
    with pytest.raises(BaseException, match="python3 -m dvtest.rfsafe1c"):
        t1c.test_rf1c_3_the_probe_failed_line_has_its_shape(None, target, out)
    (tmp / "logs").mkdir()
    (tmp / "logs" / "dvt-xlxd-sut.log").write_text("boot\n" + GOOD + "\n")
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), status(NEW, 2000, NEW, 2000)], [])
    t1c.test_rf1c_3_the_probe_failed_line_has_its_shape(None, target, out)             # a well-formed line is a FINDING after a healthy restart, not a failure
    assert any(n[2] == "FINDING" and "1 probe failed line" in n[3] for n in notes)
    (tmp / "logs" / "dvt-xlxd-sut.log").write_text(GOOD.replace("radio1", "radio9") + "\n")
    out, _ = run(flow, [status(OLD, 1000, OLD, 1000), status(NEW, 2000, NEW, 2000)], [])
    with pytest.raises(AssertionError, match="not a configured physical"):
        t1c.test_rf1c_3_the_probe_failed_line_has_its_shape(None, target, out)


def test_gate_and_gap(monkeypatch):
    monkeypatch.delenv("DVT_RFSAFE1C", raising=False)
    fx = t1c.strict
    fn = getattr(fx, "__wrapped__", None) or getattr(fx, "_get_wrapped_function", lambda: None)()
    assert fn is not None, "cannot reach the fixture function of `strict`"
    with pytest.raises(BaseException, match="DVT_RFSAFE1C is not on"):
        fn()
    monkeypatch.setenv("DVT_RFSAFE1C", "on")
    assert fn() is True
    with pytest.raises(BaseException, match="GAP"):
        t1c.test_rf1c_1_a_midrun_fence_shows_within_2_s_and_tx_is_refused(None, types.SimpleNamespace(out="/nonexistent-dir-for-test"), None)


def test_the_case_file_writes_only_its_note_log():
    text = (Path(t1c.__file__)).read_text()
    assert text.count("open(path") == 1 and "write_text" not in text and ".write(" not in text and '"w"' not in text      # the SUT log is only read


def test_dp18_procs_origin_follows_the_switch(monkeypatch):
    import test_dp18_rf as trf
    notes = []
    monkeypatch.setattr(trf, "note", lambda *a: notes.append(a))
    sim = types.SimpleNamespace(admin=FakeAdmin([status(NEW, 2000, NEW, 2000)], []))
    monkeypatch.delenv("DVT_RFSAFE1C", raising=False)
    assert trf.procs_origin(sim, None, "DP18-r1", {"pid": OLD}) is None and notes == []          # off: nothing read, nothing asserted
    monkeypatch.setenv("DVT_RFSAFE1C", "on")
    res = trf.procs_origin(sim, None, "DP18-r1", {"pid": OLD})
    assert res.problems == [] and any("RFSAFE1c procs[]" in n[3] for n in notes)
    sim = types.SimpleNamespace(admin=FakeAdmin([status(NEW, 2000, NEW, 1999)], []))
    with pytest.raises(AssertionError, match="RFSAFE1c procs"):
        trf.procs_origin(sim, None, "DP18-r1", None)
