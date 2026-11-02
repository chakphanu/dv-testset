# SPDX-License-Identifier: MIT
"""A rehearsal of cases/test_web_soak.py without a rig: the real test function runs against fakes (control channel,
generator, lanes, event stream) with seconds instead of minutes, so that the orchestration (phases, sampling, the
stop file, the wall clock cap, the teardown, the report files) is checked before the 70 minute run."""
import json
import os
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
    def __init__(self, leak=0.0, restart_at=None, drops_per_s=0.0, bad_reason=None, queue_per_s=0.0):
        self.listeners, self.leak, self.restart_at, self.t0, self.lock = 0, leak, restart_at, time.time(), threading.Lock()
        self.drops_per_s, self.bad_reason, self.queue_per_s = drops_per_s, bad_reason, queue_per_s

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
        now = time.time()
        if self.listeners:
            self.started = True
            self.started_t = getattr(self, "started_t", None) or now
        elif getattr(self, "started", False) and getattr(self, "stop_t", None) is None:
            self.stop_t = now                                       # the counters stop moving when the traffic is over
        age = (getattr(self, "stop_t", None) or now) - self.t0
        text = ("dv_edge_webrtc_listeners %d\ndv_hub_dropped_total 5\ndv_panics_total 0\ndv_edge_drops_total{reason=\"no_session\"} %d\n"
                "dv_edge_queue_drops_total{queue=\"q\"} %d\ndv_status_events_len{proc=\"status\"} %d\n"
                % (self.listeners, 4 + self.drops_per_s * age, self.queue_per_s * age, 100 + 10 * age))
        if self.bad_reason and getattr(self, "started_t", None) and now - self.started_t > 3.8:        # inside the measured part (traffic clock starts 2 s after run, 1 s warmup)
            text += "dv_edge_drops_total{reason=\"%s\"} 1\n" % self.bad_reason
        return text


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
    monkeypatch.setenv("DVT_SOAK_MEASURE_S", "2.5")
    monkeypatch.setenv("DVT_SOAK_SAMPLE_S", "0.25")
    monkeypatch.setattr(ts, "IDLE0_TICKS", 1)
    monkeypatch.setattr(ts, "IDLE0_GAP_S", 0.1)
    monkeypatch.setattr(ts, "SETTLE_S", 0.1)
    monkeypatch.setattr(soak, "IDLE_S", 1.0)
    monkeypatch.setattr(ts.soak, "build_overs", lambda seed, lanes, dur: [{"t": 0.2 * i, "lane": lane, "dur_s": 1.0}
                                                                           for i in range(10) for lane in lanes])

    def lanes(target, env, modules):
        return {n: Lane(n) for n in ss.REQUIRED_LANES}, {"nxdn": "no NXDN edge", "p25": "no P25 edge"}

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
    flat = report["result"]["counters"]["flat"]
    assert flat["dv_edge_queue_drops_total"]["status"] == "flat" and flat["dv_edge_tx_refused_total"]["status"] == "absent"
    assert "dv_edge_drops_total" not in flat and report["result"]["counters"]["drops"]["outside"] == {}
    assert report["counters_table"][0].startswith("flat family") and "dv_edge_drops_total" in report["metric_families"]
    att = report["result"]["attribution"]
    assert "dv_status_events_len{proc=status}" in att["series"] and att["series_total"]["n"] > 0 and att["raw"]       # raw series in soak.json
    assert report["attribution_table"][0].startswith("attribution series") and report["warnings"] == []
    assert "idle0" in report["gauge_families"] and report["gauge_families"]["idle0"]["dv_status_events_len"]["n"] == 1
    assert report["short"] is False and report["sample_s"] == 0.25
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


def test_a_growing_flat_counter_fails_the_case_naming_the_family(rehearsal):
    ctl = Ctl(queue_per_s=50.0)
    with pytest.raises(AssertionError, match="flat:dv_edge_queue_drops_total"):
        run(ctl, Gen(ctl))
    report = json.loads((rehearsal / "soak.json").read_text())
    assert report["result"]["counters"]["flat"]["dv_edge_queue_drops_total"]["status"] == "grew"


def test_legit_drop_growth_passes_but_is_flagged_when_fast(rehearsal):
    ctl = Ctl(drops_per_s=50.0)                                          # no_session grows during the traffic: legit; the rate is far above 1/s
    run(ctl, Gen(ctl))
    report = json.loads((rehearsal / "soak.json").read_text())
    drops = report["result"]["counters"]["drops"]
    assert report["result"]["ok"] and drops["flagged"] and drops["reasons"]["no_session"]["delta"] > 0
    assert any("FLAG dv_edge_drops_total" in w for w in report["warnings"])


def test_a_drop_reason_outside_the_legit_set_fails_the_case(rehearsal):
    ctl = Ctl(bad_reason="hold_drop")
    with pytest.raises(AssertionError, match="drops-reasons"):
        run(ctl, Gen(ctl))


def test_the_short_mode_sets_the_timing_and_scales_the_rss_bound(monkeypatch):
    monkeypatch.setenv("DVT_SOAK_SHORT", "1")
    monkeypatch.delenv("DVT_SOAK_WARMUP_S", raising=False)
    monkeypatch.delenv("DVT_SOAK_MEASURE_S", raising=False)
    monkeypatch.delenv("DVT_SOAK_SAMPLE_S", raising=False)
    tm = soak.timing(os.environ)
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"]) == (180.0, 240.0, 15.0)
    plan = soak.build_overs("1", ["dstar", "dmr", "ysf", "m17", "usrp-a", "usrp-b", "nxdn", "p25"], tm["warmup_s"] + tm["measured_s"])
    assert soak.summarize_plan(plan, [])["overs_per_lane"]["dstar"] == 7                  # 7 minutes: at least 5 overs a lane
    assert soak.validate_sessions(soak.build_sessions("1", tm["warmup_s"] + tm["measured_s"], seats=100)) == []


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


def test_the_real_settle_covers_the_longest_link_timeout():
    """DV-Router ruling: 170 s (imrs link timeout 150 s + sweep tick) between the last client and the idle window."""
    assert ts.SETTLE_S >= soak.SETTLE_MIN_S == 170


class EventClient:
    """A public event stream that sends two events per connection (and nothing to idle_events, which passes no on_frame)."""

    def __init__(self):
        self.calls = 0

    def sse(self, path, max_events=1, timeout=15.0, until=None, on_frame=None):
        time.sleep(0.2)
        self.calls += 1
        if on_frame is not None:
            for i in range(2):
                on_frame({"event": "room", "id": "s-%d-%d" % (self.calls, i)})
        return 200, "text/event-stream", [{"event": "hello", "id": "e-1"}]


def test_the_report_carries_hub_events_and_streams(rehearsal):
    ctl = Ctl(leak=0.5)
    gen = Gen(ctl)
    ts.test_soak_sixty_minutes(True, gen, True, ctl, Target(), "https://dvt-web", "/x/ca.pem", lambda: EventClient(),
                               lambda n: [("N0LD%02d" % i, "x") for i in range(n)])
    report = json.loads((rehearsal / "soak.json").read_text())
    hub, streams = report["hub_events"], report["streams"]
    assert report["state"] == "done" and report["events_note"] == hub["events_note"] and report["events"]["ok"] is True      # the idle verdict is unchanged
    assert hub["events_total"] >= 2 and hub["by_kind"] == {"room": hub["events_total"]} and hub["measured"]["mean_per_min"] > 0
    assert hub["events_kib_per_min"] == pytest.approx(hub["measured"]["mean_per_min"] * 1.16, abs=0.01) and hub["ring"] == 10000
    assert hub["status_rss_slope_kib_per_min"] is None and hub["ratio_to_rss"] is None                                       # the fake has no dv-svc-status
    assert hub["events_per_min_series"] and hub["hub_metric"]["names_seen"] == ["dv_status_events_len"]
    assert streams["streams_total"] > 0 and streams["streams_per_min"] > 0 and set(streams["per_lane"]) <= set(ss.REQUIRED_LANES)
    router = streams["slope_per_stream"]["dv-router"]
    assert router["rss_kib_per_min"] > 0 and router["rss_kib_per_stream"] == pytest.approx(router["rss_kib_per_min"] / streams["streams_per_min"], abs=0.01)


def test_a_plain_stream_client_without_on_frame_still_gives_a_record(rehearsal):
    ctl = Ctl()
    gen = Gen(ctl)
    run(ctl, gen)                                                                                     # the Client of this file has no on_frame and sends only hello
    report = json.loads((rehearsal / "soak.json").read_text())
    assert report["state"] == "done" and report["hub_events"]["events_total"] == 0 and report["hub_events"]["reconnects"] >= 1
    assert "events_note" in report and report["streams"]["streams_total"] > 0
