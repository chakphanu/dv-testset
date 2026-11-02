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


REAL_CHILDREN = [
    "dv-edge-dextra", "dv-edge-dplus", "dv-edge-dcs", "dv-edge-mmdvm", "dv-edge-dmrplus", "dv-edge-ysf", "dv-edge-m17", "dv-edge-g3",
    "dv-edge-imrs", "dv-edge-xlx", "dv-edge-nxdn", "dv-edge-p25", "dv-edge-usrp", "dv-edge-webrtc",
    "dv-room", "dv-data-worker", "dv-codec-ambed", "dv-svc-status", "dv-svc-web", "dv-svc-admin", "dv-svc-iddb", "dv-svc-idsync",
    "dv-svc-callhome", "dv-svc-recorder"]


def test_discovery_finds_every_edge_as_its_own_child_and_reports_unknown_ones():
    procs = [(1, 0, "dv-router")] + [(10 + i, 1, n) for i, n in enumerate(REAL_CHILDREN)] + [(90, 1, "dv-edge-newone"), (91, 1, "dv-edge-ax25x")]
    names, others = ss.discover(FakeCtl(procs=procs))
    assert names == sorted(REAL_CHILDREN + ["dv-router"])                      # one entry per binary, nothing grouped
    assert others == ["dv-edge-ax25x", "dv-edge-newone"]
    # not running = not listed = not an error: a launch without a recorder, codec worker and the optional edges
    few = [(1, 0, "dv-router"), (2, 1, "dv-edge-dextra"), (3, 1, "dv-room")]
    assert ss.discover(FakeCtl(procs=few)) == (["dv-edge-dextra", "dv-room", "dv-router"], [])


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
    last = sink.rows[-1]
    assert last["counters"] == {"dv_hub_dropped_total": 4.0} and last["phase"] == "idle0" and last["families"] == ["dv_edge_webrtc_listeners", "dv_hub_dropped_total"]
    assert set(last["flat"]) == set(soak.SNAPSHOT_FAMILIES) and set(last["flat"].values()) == {None}
    assert last["reasons"] == {} and "gauges" in last


def test_the_tick_records_the_attribution_series_and_the_scrape_size_next_to_the_gauge():
    sink = FakeSink()
    text = ('dv_edge_webrtc_listeners 3\ndv_status_events_len{proc="status"} 17\ndv_status_inputs_total{kind="x"} 9\n'
            'dv_pkt_seen_entries{proc="data"} 4\n')

    class Ctl(FakeCtl):
        def metrics(self, service="xlxd-sut", timeout=30.0):
            return text

    sampler = ss.ChildSampler(Ctl(), ["dv-edge-xlx"], sink)
    sampler.tick("measured")
    attr = [r for r in sink.rows if r["kind"] == "attr"]
    assert len(attr) == 1 and attr[0]["phase"] == "measured" and attr[0]["series_total"] == 4 and attr[0]["truncated"] == 0
    assert attr[0]["values"] == {"dv_status_events_len{proc=status}": 17.0, "dv_pkt_seen_entries{proc=data}": 4.0, "dv_edge_webrtc_listeners": 3.0}


def test_sampler_tells_the_processes_of_a_shared_name_apart():
    sink = FakeSink()

    class Ctl(FakeCtl):
        def proc_stat(self, service, process, timeout=60.0):
            return REPLY % (21, process, 1000.0) + REPLY % (22, process, 1000.5) if process == "dv-room" else super().proc_stat(service, process, timeout)

    sampler = ss.ChildSampler(Ctl(), ["dv-room", "dv-svc-web"], sink)
    sampler.tick("warmup")
    rows = [(r["name"], r["pid"]) for r in sink.rows if r["kind"] == "proc"]
    assert rows == [("dv-room.01", 21), ("dv-room.02", 22), ("dv-svc-web", 8)]


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


class Tgt:
    xlxd_sut = "192.0.2.11"
    xlxd_peer_sut = ""
    sut_profile = "dvrouter"


def test_make_lanes_talk_to_the_suts_own_nxdn_and_p25_ports():
    lanes, skipped = ss.make_lanes(Tgt(), {}, ss.parse_modules("", None, None))
    assert not skipped and set(lanes) == set(ss.DEFAULT_MODULES)
    assert (lanes["nxdn"].ip, lanes["nxdn"].tg, lanes["nxdn"].module) == ("192.0.2.11", 31672, "F")
    assert (lanes["p25"].ip, lanes["p25"].tg, lanes["p25"].module) == ("192.0.2.11", 31672, "G")
    lanes, _ = ss.make_lanes(Tgt(), {"DVT_NXDN_TG": "20", "DVT_P25_TG": "9", "DVT_NXDN": "off"}, ss.parse_modules("", None, None))
    assert (lanes["nxdn"].tg, lanes["p25"].tg) == (20, 9)                       # the rig's TG settings, no reflector needed


def test_a_counters_row_has_the_flat_families_and_the_family_names_once():
    sink = FakeSink()
    text = 'dv_edge_drops_total{proc="p",reason="x"} 2\ndv_codec_dropped_total{kind="data"} 0\ndv_up 1\n'

    class Ctl(FakeCtl):
        def metrics(self, service="xlxd-sut", timeout=30.0):
            return text

    sampler = ss.ChildSampler(Ctl(), ["dv-router"], sink)
    sampler.counters("idle0")
    sampler.counters("warmup_end")
    first, second = [r for r in sink.rows if r["kind"] == "metrics"]
    assert first["flat"]["dv_edge_drops_total"] == 2.0 and first["flat"]["dv_codec_dropped_total"] == 0.0
    assert first["flat"]["dv_edge_tx_refused_total"] is None and first["series"]["dv_edge_drops_total"] == {"reason=x": 2.0}
    assert first["reasons"] == {"x": 2.0} and first["gauges"] == {"dv_up": {"n": 1, "sum": 1.0}} and "gauges" not in second
    assert first["families"] == ["dv_codec_dropped_total", "dv_edge_drops_total", "dv_up"] and "families" not in second


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
