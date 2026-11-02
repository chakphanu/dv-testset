# SPDX-License-Identifier: MIT
"""A rehearsal of cases/test_web_soak.py without a rig: the real test function runs against fakes (control channel,
generator, lanes, event stream) with seconds instead of minutes, so that the orchestration (phases, sampling, the
stop file, the wall clock cap, the teardown, the report files) is checked before the 70 minute run."""
import json
import threading
import time

import pytest

import soak_support as ss
import test_web_soak as ts
from dvtest import soak
from test_soak import gen_rows

pytestmark = pytest.mark.unit

CHILDREN = [(1, 0, "dv-router"), (7, 1, "dv-edge-xlx"), (8, 1, "dv-svc-web"), (9, 1, "dv-edge-webrtc")]


class Ctl:
    def __init__(self, leak=0.0, restart_at=None):
        self.listeners, self.leak, self.restart_at, self.t0, self.lock = 0, leak, restart_at, time.time(), threading.Lock()

    def proc_list(self):
        return list(CHILDREN)

    def proc_stat(self, service, process, timeout=60.0):
        pid = {c: p for p, _pp, c in CHILDREN}[process]
        now = time.time()
        rss = 20000 + self.leak * (now - self.t0)
        start = 900 if self.restart_at is None or now - self.t0 < self.restart_at else 901
        return ("pid=%d\ncomm=%s\nutime=1\nstime=1\nclk_tck=100\nvmhwm_kb=%d\nvmrss_kb=%d\nthreads=8\nfds=30\nstarttime=%d\nt=%.3f\n"
                % (pid, process, rss + 10, rss, start, now))

    def metrics(self, service="xlxd-sut", timeout=30.0):
        return "dv_edge_webrtc_listeners %d\ndv_hub_dropped_total 5\ndv_panics_total 0\n" % self.listeners


class Gen:
    def __init__(self, ctl):
        self.ctl, self.requests, self.aborted, self.finished = ctl, [], False, False

    def run(self, req):
        self.requests.append(req)
        self.ctl.listeners = 100
        return {"workers": 2}

    def status(self):
        return {"state": "connecting", "connected": 100, "failed": 0, "left": 3, "live": 97}

    def finish(self, timeout=180.0):
        self.finished = True
        self.ctl.listeners = 0
        return {"rows": gen_rows(100)}

    def abort(self):
        self.aborted = True
        self.ctl.listeners = 0


class Lane(ss.Lane):
    def __init__(self, name):
        super().__init__(name, "D")
        self.n = 0

    def _over(self, dur_s, seed, result):
        self.n += 1
        result.update(ok=True, sent=500, heard={"x": 500})


class Client:
    def sse(self, path, max_events=1, timeout=15.0, until=None):
        time.sleep(0.2)
        return 200, "text/event-stream", [{"event": "hello", "id": "e-1"}]


class Target:
    xlxd_sut = "192.0.2.11"
    xlxd_peer_sut = ""


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    monkeypatch.setenv("DVT_OUT", str(tmp_path))
    monkeypatch.setenv("DVT_SOAK_WARMUP_S", "1")
    monkeypatch.setenv("DVT_SOAK_MEASURED_S", "2.5")
    monkeypatch.setattr(ts, "IDLE0_TICKS", 1)
    monkeypatch.setattr(ts, "IDLE0_GAP_S", 0.1)
    monkeypatch.setattr(ts, "SETTLE_S", 0.1)
    monkeypatch.setattr(ts, "SAMPLE_PERIOD_S", 0.25)
    monkeypatch.setattr(soak, "IDLE_S", 1.0)
    monkeypatch.setattr(ts.soak, "build_overs", lambda seed, lanes, dur: [{"t": 0.2 * i, "lane": lane, "dur_s": 1.0}
                                                                           for i in range(10) for lane in lanes])

    def lanes(target, env, modules):
        return {n: Lane(n) for n in ss.REQUIRED_LANES}, {"nxdn": "no NXDN reflector", "p25": "no P25 reflector"}

    monkeypatch.setattr(ss, "make_lanes", lanes)
    web = tmp_path / "web"
    return web


def run(ctl, gen, **fx):
    ts.test_soak_sixty_minutes(True, gen, True, ctl, Target(), "https://dvt-web", "/x/ca.pem", lambda: Client(),
                               lambda n: [("N0LD%02d" % i, "x") for i in range(n)])


def test_a_healthy_rehearsal_passes_and_writes_the_files(rehearsal):
    ctl = Ctl()
    gen = Gen(ctl)
    run(ctl, gen)
    assert gen.finished and not gen.aborted
    req = gen.requests[0]
    assert req["listeners"] == 100 and req["workers"] == 2 and len(req["accounts"]) == 5 and req["ca"] == "/webout/ca.pem"
    assert soak.validate_sessions(req["soak"]["sessions"]) == []
    report = json.loads((rehearsal / "soak.json").read_text())
    assert report["state"] == "done" and report["result"]["ok"] and report["smoke"] is True
    assert {c["name"] for c in report["extra_checks"]} >= {"generator-sessions", "sampling", "drained", "traffic:dstar", "traffic:m17"}
    assert report["lanes_skipped"].keys() == {"nxdn", "p25"} and report["table"][0].startswith("child")
    assert set(report["result"]["children"]) == {"dv-router", "dv-edge-xlx", "dv-svc-web", "dv-edge-webrtc"}
    rows, bad = soak.parse_jsonl((rehearsal / "soak-samples.jsonl").read_text())
    assert bad == 0
    phases = {r["phase"] for r in rows if r["kind"] == "proc"}
    assert phases == {"idle0", "warmup", "measured", "stopping", "idle"} - ({"stopping"} if "stopping" not in phases else set())
    assert {r["phase"] for r in rows if r["kind"] == "metrics"} == {"idle0", "warmup_end", "measured_end", "idle_start", "idle_end"}
    assert {r["name"] for r in rows if r["kind"] == "proc"} == {"dv-router", "dv-edge-xlx", "dv-svc-web", "dv-edge-webrtc"}


def test_a_leaking_child_fails_the_case(rehearsal):
    ctl = Ctl(leak=4000.0)                                             # 4 MB a second: far over the limit in seconds
    gen = Gen(ctl)
    with pytest.raises(AssertionError) as err:
        run(ctl, gen)
    assert "rss:" in str(err.value)
    report = json.loads((rehearsal / "soak.json").read_text())
    assert report["state"] == "done" and not report["result"]["ok"]


def test_a_restarting_child_fails_the_case(rehearsal):
    ctl = Ctl(restart_at=2.0)
    with pytest.raises(AssertionError) as err:
        run(ctl, Gen(ctl))
    assert "no-restart:" in str(err.value)


def test_the_stop_file_aborts_cleanly(rehearsal):
    ctl = Ctl()
    gen = Gen(ctl)
    (rehearsal).mkdir(parents=True, exist_ok=True)
    threading.Timer(0.8, lambda: (rehearsal / "soak.stop").write_text("")).start()
    with pytest.raises(pytest.fail.Exception) as err:
        run(ctl, gen)
    assert "stopped early" in str(err.value.msg)
    assert gen.aborted and not gen.finished and ctl.listeners == 0
    assert json.loads((rehearsal / "soak.json").read_text())["state"] == "stopped early"


def test_the_wall_clock_cap_aborts(rehearsal, monkeypatch):
    monkeypatch.setattr(soak, "WALL_CAP_S", 1.2)
    ctl = Ctl()
    gen = Gen(ctl)
    with pytest.raises(pytest.fail.Exception) as err:
        run(ctl, gen)
    assert "wall clock cap" in str(err.value.msg) and gen.aborted


def test_an_exception_still_tears_down(rehearsal):
    ctl = Ctl()
    gen = Gen(ctl)
    gen.status = lambda: {"state": "error", "error": "worker died"}
    ts.os.environ["DVT_SOAK_WARMUP_S"] = "400"                          # long enough to reach the 5 minute note: patch the clock instead
    try:
        orig = time.monotonic
        base = orig()
        calls = {"n": 0}

        def fast():
            calls["n"] += 1
            return base + (orig() - base) * 400.0
        ts.time.monotonic, ss.time.monotonic = fast, fast
        try:
            with pytest.raises(pytest.fail.Exception) as err:
                run(ctl, gen)
        finally:
            ts.time.monotonic = ss.time.monotonic = orig
    finally:
        ts.os.environ["DVT_SOAK_WARMUP_S"] = "1"
    assert "worker died" in str(err.value.msg) and gen.aborted


def test_soak_is_skipped_without_the_switch(monkeypatch):
    monkeypatch.delenv("DVT_SOAK", raising=False)
    with pytest.raises(pytest.skip.Exception):
        ts.soak_on.__wrapped__()
