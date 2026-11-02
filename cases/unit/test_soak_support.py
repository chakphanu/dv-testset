# SPDX-License-Identifier: MIT
"""cases/soak_support.py: the pieces of the web soak case that need no rig (sampling, discovery, the over runner)."""
import threading
import time

import pytest

import soak_support as ss
from dvtest import control, soak

pytestmark = pytest.mark.unit


class FakeSink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(row)


REPLY = ("pid=%d\ncomm=%s\nutime=10\nstime=5\nclk_tck=100\nvmhwm_kb=2000\nvmrss_kb=1500\nthreads=7\nfds=21\nstarttime=900\nt=%.3f\n")
METRICS = "# TYPE dv_edge_webrtc_listeners gauge\ndv_edge_webrtc_listeners 3\ndv_hub_dropped_total{r=\"x\"} 4\n"


class FakeCtl:
    def __init__(self, procs=None, missing=(), broken=()):
        self.procs = procs or [(1, 0, "dv-router"), (7, 1, "dv-edge-xlx"), (8, 1, "dv-svc-web"), (9, 1, "dv-newthing"), (10, 7, "worker")]
        self.missing, self.broken, self.calls = set(missing), set(broken), []

    def proc_list(self):
        return list(self.procs)

    def proc_stat(self, service, process, timeout=60.0):
        self.calls.append(process)
        if process in self.missing:
            raise RuntimeError("proc-stat xlxd-sut %s: error 3" % process)
        if process in self.broken:
            raise TimeoutError("no ack")
        pid = {c: p for p, _pp, c in self.procs}.get(process, 99)
        return REPLY % (pid, process, 1000.0)

    def metrics(self, service="xlxd-sut", timeout=30.0):
        return METRICS


def test_discover_children_of_the_launcher_and_the_unlisted_ones():
    names, others = ss.discover(FakeCtl())
    assert names == ["dv-edge-xlx", "dv-router", "dv-svc-web"]                 # the launcher is sampled too
    assert others == ["dv-newthing"]                                           # a child outside the allow-list is reported
    with pytest.raises(ValueError):
        ss.discover(FakeCtl(procs=[(7, 1, "dv-edge-xlx")]))


def test_discovery_names_agree_with_the_allow_list():
    assert set(ss.discover(FakeCtl())[0]) <= set(control.PROC_STAT_NAMES)


def test_sampler_tick_writes_one_row_per_child_a_gauge_and_marks_failures():
    sink = FakeSink()
    sampler = ss.ChildSampler(FakeCtl(missing={"dv-room"}, broken={"dv-svc-web"}), ["dv-edge-xlx", "dv-room", "dv-svc-web"], sink)
    sampler.phase = "warmup"
    sampler.tick()
    kinds = [(r["kind"], r.get("name"), r.get("absent")) for r in sink.rows]
    assert ("proc", "dv-edge-xlx", None) in kinds and ("proc", "dv-room", True) in kinds
    assert not any(r.get("name") == "dv-svc-web" for r in sink.rows if r["kind"] == "proc")
    gauge = [r for r in sink.rows if r["kind"] == "gauge"]
    assert gauge and gauge[0]["listeners"] == 3 and gauge[0]["phase"] == "warmup"
    assert len(sampler.errors) == 1 and "dv-svc-web" in sampler.errors[0]
    assert any(r["kind"] == "mark" and "sampling error" in r["note"] for r in sink.rows)
    proc = next(r for r in sink.rows if r["kind"] == "proc" and r.get("name") == "dv-edge-xlx")
    assert proc["fds"] == 21 and proc["start"] == 900 and proc["rss_kib"] == 1500 and proc["seq"] == 1
    sampler.counters("idle0")
    assert sink.rows[-1] == soak.counters_row(sink.rows[-1]["t"], "idle0", {"dv_hub_dropped_total": 4.0})


def test_sampler_thread_ticks_on_a_schedule_and_stops():
    sink = FakeSink()
    sampler = ss.ChildSampler(FakeCtl(), ["dv-edge-xlx"], sink, period_s=0.05)
    sampler.start()
    time.sleep(0.4)
    sampler.halt(5.0)
    n = sum(1 for r in sink.rows if r["kind"] == "proc")
    assert 3 <= n <= 10 and not sampler.thread.is_alive()
    seen = len(sink.rows)
    time.sleep(0.15)
    assert len(sink.rows) == seen


def test_sample_file_is_one_flushed_line_per_row(tmp_path):
    f = ss.SampleFile(str(tmp_path / "s.jsonl"))
    threads = [threading.Thread(target=lambda i=i: [f.write(soak.gauge_row(i, "idle", 0)) for _ in range(50)]) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rows, bad = soak.parse_jsonl((tmp_path / "s.jsonl").read_text())        # readable before close: flushed per row
    f.close()
    assert len(rows) == 200 and bad == 0 and f.rows == 200


def test_modules_defaults_overrides_and_collisions():
    mods = ss.parse_modules("", None, None)
    assert mods["usrp-a"] == "A" and mods["usrp-b"] == "B" and len(set(mods.values())) == len(mods) == 8
    assert ss.parse_modules("dmr=H, ysf=K", "L", "M")["nxdn"] == "L"
    assert ss.parse_modules("nxdn=Q", "L", None)["nxdn"] == "Q"
    for bad in ("dmr", "dmr=", "dmr=de", "nosuch=D", "dmr=D"):
        with pytest.raises(ValueError):
            ss.parse_modules(bad)


class FakeLane:
    def __init__(self, name, ok=True, boom=False):
        self.name, self.ok, self.boom, self.calls = name, ok, boom, []

    def over(self, dur_s, seed):
        self.calls.append((time.monotonic(), dur_s, seed))
        return {"lane": self.name, "ok": self.ok, "sent": 500, "heard": {"x": 480 if self.ok else 0}, "error": None if self.ok else "link failed"}


def test_traffic_runs_the_schedule_and_summarises():
    lanes = {"a": FakeLane("a"), "b": FakeLane("b", ok=False)}
    overs = [{"t": 0.0, "lane": "a", "dur_s": 1.0}, {"t": 0.05, "lane": "b", "dur_s": 1.0}, {"t": 0.1, "lane": "a", "dur_s": 1.0},
             {"t": 0.1, "lane": "zz", "dur_s": 1.0}]
    t0 = time.monotonic()
    traffic = ss.Traffic(lanes, overs, t0, seed=5)
    traffic.start()
    time.sleep(0.4)
    assert traffic.finish(5.0)
    got = traffic.summary()
    assert got["a"]["overs"] == 2 and got["a"]["ok"] == 2 and got["a"]["heard_min"] == 480
    assert got["b"] == {"overs": 1, "ok": 0, "errors": {"link failed": 1}, "heard_min": 0, "heard_mean": 0.0}
    assert "zz" not in got and lanes["a"].calls[0][2] == "5/0" and lanes["a"].calls[1][2] == "5/2"
    assert all(r["t_end"] >= r["t_start"] for r in traffic.results)


def test_traffic_stops_before_the_next_over():
    lanes = {"a": FakeLane("a")}
    traffic = ss.Traffic(lanes, [{"t": 0.0, "lane": "a", "dur_s": 1}, {"t": 30.0, "lane": "a", "dur_s": 1}], time.monotonic(), seed=1)
    traffic.start()
    time.sleep(0.2)
    began = time.monotonic()
    assert traffic.finish(5.0) and time.monotonic() - began < 2.0
    assert len(lanes["a"].calls) == 1


def test_lane_checks():
    summary = {n: {"overs": 60, "ok": 58, "errors": {}} for n in ss.REQUIRED_LANES}
    assert all(c.ok for c in ss.lane_checks(summary))
    summary["dmr"] = {"overs": 60, "ok": 3, "errors": {"link failed": 57}}
    del summary["m17"]
    bad = {c.name: c.detail for c in ss.lane_checks(summary) if not c.ok}
    assert set(bad) == {"traffic:dmr", "traffic:m17"} and "link failed" in bad["traffic:dmr"]


def test_lane_over_never_raises():
    class Boom(ss.Lane):
        def _over(self, dur_s, seed, result):
            raise OSError("socket gone")
    res = Boom("x", "D").over(10, "s")
    assert res["ok"] is False and "OSError: socket gone" in res["error"]
