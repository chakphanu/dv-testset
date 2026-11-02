# SPDX-License-Identifier: MIT
"""A rehearsal of cases/test_web_rss_isolation.py without a rig (the real test function against fakes, seconds instead of
minutes), plus the login cycle of the lanes and the Traffic method switch of cases/soak_support.py."""
import json
import threading
import time

import pytest

import soak_support as ss
import test_web_rss_isolation as ti
import test_web_soak as tws
from dvtest import isolation as iso
from dvtest import soak
from test_soak import gen_rows
from test_soak_case import Ctl as BaseCtl
from test_soak_case import Gen as BaseGen
from test_soak_case import Target

pytestmark = pytest.mark.unit

CHILDREN = [(1, 0, "dv-router"), (7, 1, "dv-edge-xlx"), (8, 1, "dv-svc-status"), (9, 1, "dv-edge-webrtc")]


class Ctl(BaseCtl):
    """As the SOAK1 fake, with dv-svc-status and the smaps keys; dv-router grows `leak` KiB a second."""

    def __init__(self, leak=0.0, **kw):
        super().__init__(**kw)
        self.leak = leak

    def proc_list(self):
        return list(CHILDREN)

    def proc_stat(self, service, process, timeout=60.0):
        pid = {c: p for p, _pp, c in CHILDREN}[process]
        now = time.time()
        rss = 20000 + (self.leak * (now - self.t0) if process == "dv-router" else 0)
        return ("pid=%d\ncomm=%s\nutime=1\nstime=1\nclk_tck=100\nvmhwm_kb=%d\nvmrss_kb=%d\nthreads=8\nfds=30\nstarttime=900\nt=%.3f\n"
                "smaps_rss_kb=%d\nsmaps_anon_kb=%d\nsmaps_private_dirty_kb=%d\nsmaps_pss_anon_kb=%d\n"
                % (pid, process, rss + 10, rss, now, rss + 100, rss / 2, rss / 4, rss / 2 - 5))


class Gen(BaseGen):
    def __init__(self, ctl):
        super().__init__(ctl)
        self.seats = 100

    def finish(self, timeout=180.0):
        self.finished = True
        self.ctl.listeners = 0
        return {"rows": gen_rows(100)}


class Lane(ss.Lane):
    def __init__(self, name, fail=False):
        super().__init__(name, "D")
        self.overs, self.cycles, self.fail = 0, 0, fail

    def _over(self, dur_s, seed, result):
        self.overs += 1
        result.update(ok=True, sent=500, heard={"x": 500})

    def _cycle(self, hold_s, result):
        self.cycles += 1
        if self.fail and self.cycles > 1:                                            # the probe cycle works, the later ones do not
            raise AssertionError("link failed")
        result["ok"] = True


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    monkeypatch.setenv("DVT_OUT", str(tmp_path / "20261103T000000Z"))
    monkeypatch.setenv("DVT_ISOLATION_WARMUP_S", "0.5")
    monkeypatch.setenv("DVT_ISOLATION_MEASURE_S", "1.6")
    monkeypatch.setenv("DVT_ISOLATION_SAMPLE_S", "0.25")
    monkeypatch.setattr(soak, "build_overs", lambda seed, lanes, dur: [{"t": 0.2 * i, "lane": lane, "dur_s": 1.0} for i in range(10) for lane in lanes])
    monkeypatch.setattr(iso, "build_cycles", lambda seed, lanes, dur: [{"t": 0.2 * i, "lane": lane, "dur_s": 0.1} for i in range(10) for lane in lanes])
    real = soak.build_sessions
    monkeypatch.setattr(soak, "build_sessions", lambda seed, dur, seats=100, churn_per_min=5: real(seed, 400.0, seats=seats, churn_per_min=churn_per_min))
    state = {"lanes": {}, "fail": False}

    def lanes(target, env, modules):
        state["lanes"] = {n: Lane(n, state["fail"]) for n in ss.REQUIRED_LANES + ss.OPTIONAL_LANES}
        return state["lanes"], {}

    monkeypatch.setattr(ss, "make_lanes", lanes)
    state["web"] = tmp_path / "20261103T000000Z" / "web"
    return state


def run(mode, ctl, gen, monkeypatch, gen_allowed=True):
    monkeypatch.setenv("DVT_ISOLATION", mode)

    def access():
        if not gen_allowed:
            raise AssertionError("this mode must not touch the load generator")
        return gen, lambda n: [("N0LD%02d" % i, "x") for i in range(n)]

    ti.test_rss_isolation(iso.resolve(mode), access, True, ctl, Target(), "https://dvt-web", "/x/ca.pem")


def read(state, letter):
    return json.loads((state["web"] / ("isolation-%s.json" % letter)).read_text())


def test_idle_mode_runs_nothing_and_writes_the_data(rehearsal, monkeypatch):
    ctl = Ctl(leak=3.0)
    run("A", ctl, None, monkeypatch, gen_allowed=False)
    doc = read(rehearsal, "A")
    assert doc["state"] == "done" and doc["ok"] and doc["mode"] == "A" and doc["name"] == "idle" and doc["run"] == "20261103T000000Z"
    assert [c["name"] for c in doc["checks"]] == ["activity:sampled", "activity:sampling", "activity:ended"]
    router = doc["focus"]["dv-router"]
    assert router["vmrss"]["slope_per_min"] > 100 and router["smaps"]["rss"]["slope_per_min"] > 100          # 3 KiB/s = 180 KiB/min, noisy at 2.5 s
    assert doc["focus"]["dv-svc-status"]["vmrss"]["slope_per_min"] == 0.0
    assert doc["series_total"]["n"] >= 5 and set(doc["children"]) == {"dv-router", "dv-edge-xlx", "dv-svc-status", "dv-edge-webrtc"}
    assert doc["traffic"] == {} and doc["generator"] is None and doc["plan"]["sessions"] == 0
    assert (rehearsal["web"] / "isolation-A-samples.jsonl").exists()
    assert all(n.overs == 0 and n.cycles == 0 for n in rehearsal["lanes"].values())                          # no lane was even built
    assert iso.main([str(rehearsal["web"].parent)]) == 0


def test_webrtc_churn_mode_holds_listeners_and_no_voice(rehearsal, monkeypatch):
    ctl = Ctl()
    gen = Gen(ctl)
    run("B", ctl, gen, monkeypatch)
    doc = read(rehearsal, "B")
    assert doc["ok"] and gen.finished and not gen.aborted
    req = gen.requests[0]
    assert req["listeners"] == 100 and len(req["accounts"]) == 5 and soak.validate_sessions(req["soak"]["sessions"]) == []
    assert any(s["leave"] is not None for s in req["soak"]["sessions"])
    assert {c["name"] for c in doc["checks"]} >= {"activity:listeners-connected", "activity:listeners-gauge", "activity:churn"}
    assert doc["plan"]["overs"] == 0 and doc["plan"]["cycles"] == 0 and doc["traffic"] == {}
    assert all(n.overs == 0 and n.cycles == 0 for n in rehearsal["lanes"].values())


def test_voice_mode_runs_the_overs_of_every_lane_and_no_listeners(rehearsal, monkeypatch):
    ctl = Ctl()
    run("C", ctl, None, monkeypatch, gen_allowed=False)
    doc = read(rehearsal, "C")
    assert doc["ok"] and doc["generator"] is None
    assert {k for k in doc["traffic"]} == set(ss.REQUIRED_LANES + ss.OPTIONAL_LANES)
    assert all(n.overs > 0 and n.cycles == 0 for n in rehearsal["lanes"].values())
    assert {c["name"] for c in doc["checks"]} >= {"activity:voice:dstar", "activity:voice:usrp-b", "activity:voice:p25"}


def test_proto_churn_mode_cycles_the_lanes_without_voice(rehearsal, monkeypatch):
    ctl = Ctl()
    run("D", ctl, None, monkeypatch, gen_allowed=False)
    doc = read(rehearsal, "D")
    assert doc["ok"] and all(n.cycles > 0 and n.overs == 0 for n in rehearsal["lanes"].values())
    assert "activity:cycles:ysf" in [c["name"] for c in doc["checks"]] and doc["plan"]["cycles"] == 80


def test_proto_churn_that_cannot_log_in_fails_the_activity_check(rehearsal, monkeypatch):
    rehearsal["fail"] = True
    ctl = Ctl()
    with pytest.raises(AssertionError, match="activity:cycles"):
        run("D", ctl, None, monkeypatch, gen_allowed=False)
    doc = read(rehearsal, "D")
    assert doc["state"] == "done" and not doc["ok"]                                    # the data is still written


def test_webrtc_voice_mode_talks_from_one_lane_only(rehearsal, monkeypatch):
    ctl = Ctl()
    gen = Gen(ctl)
    run("E", ctl, gen, monkeypatch)
    doc = read(rehearsal, "E")
    assert doc["ok"] and set(doc["traffic"]) == {"usrp-a"}
    assert [n for n, lane in rehearsal["lanes"].items() if lane.overs] == ["usrp-a"]
    assert all(s["leave"] is None for s in gen.requests[0]["soak"]["sessions"][:100]) and doc["plan"]["leaves"] == 0
    assert "activity:room-heard:A" in [c["name"] for c in doc["checks"]]


def test_the_stop_file_aborts_cleanly_and_tears_down(rehearsal, monkeypatch):
    ctl = Ctl()
    gen = Gen(ctl)
    rehearsal["web"].mkdir(parents=True, exist_ok=True)
    threading.Timer(0.8, lambda: (rehearsal["web"] / "isolation.stop").write_text("")).start()
    with pytest.raises(pytest.fail.Exception) as err:
        run("B", ctl, gen, monkeypatch)
    assert "stopped early" in str(err.value.msg) and gen.aborted and not gen.finished and ctl.listeners == 0
    assert read(rehearsal, "B")["state"] == "stopped early"


def test_the_case_is_skipped_without_the_switch_and_fails_on_a_bad_mode(monkeypatch):
    monkeypatch.delenv("DVT_ISOLATION", raising=False)
    with pytest.raises(pytest.skip.Exception):
        ti.isolation_mode.__wrapped__()
    monkeypatch.setenv("DVT_ISOLATION", "voice")
    assert ti.isolation_mode.__wrapped__().letter == "C"
    monkeypatch.setenv("DVT_ISOLATION", "Z")
    with pytest.raises(pytest.fail.Exception):
        ti.isolation_mode.__wrapped__()


def test_the_case_reuses_the_soak_machinery():
    assert ti.tws is tws and ti.ss is ss and iso.soak is soak


# ------------------------------------------------------------------ lanes and Traffic

def test_a_lane_cycle_never_raises_and_reports_the_error():
    class Bad(ss.Lane):
        def _cycle(self, hold_s, result):
            raise OSError("no route")

    res = Bad("x", "D").cycle(0.1, "s")
    assert res["ok"] is False and "OSError: no route" in res["error"]
    assert ss.Lane("y", "D").cycle(0.1, "s")["error"].startswith("NotImplementedError")


def test_traffic_runs_the_cycle_method_when_told_to():
    lane = Lane("dstar")
    traffic = ss.Traffic({"dstar": lane}, [{"t": 0.0, "lane": "dstar", "dur_s": 0.1}, {"t": 0.1, "lane": "dstar", "dur_s": 0.1}],
                         time.monotonic(), "1", method="cycle")
    traffic.start()
    time.sleep(0.5)
    assert traffic.finish(5.0)
    assert lane.cycles == 2 and lane.overs == 0 and traffic.summary()["dstar"]["ok"] == 2
    again = ss.Traffic({"dstar": lane}, [{"t": 0.0, "lane": "dstar", "dur_s": 0.1}], time.monotonic(), "1")
    again.start()
    time.sleep(0.3)
    again.finish(5.0)
    assert lane.overs == 1


def test_digital_lane_cycle_links_holds_and_unlinks(monkeypatch):
    made = []

    class Inner:
        def pump(self, s):
            time.sleep(0.01)

    class Fake:
        def __init__(self, kind, index):
            self.kind, self.index, self.dest, self.client, self.closed = kind, index, None, Inner(), False
            made.append(self)

        def link(self, ip):
            self.dest = ip
            return True

        def unlink(self):
            self.dest = None

        def close(self):
            self.closed = True

    monkeypatch.setattr(ss.clients, "make", lambda kind, module, index=0: Fake(kind, index))
    lane = ss.DigitalLane("dstar", "D", "192.0.2.11", "dstar", ("dplus", "dcs"), "dstar")
    res = lane.cycle(0.1, "s")
    assert res["ok"] and res["error"] is None and [m.kind for m in made] == ["dplus", "dcs", "dstar"]
    assert all(m.closed and m.dest is None for m in made)
    made.clear()
    monkeypatch.setattr(Fake, "link", lambda self, ip: self.kind != "dcs")
    res = lane.cycle(0.1, "s")
    assert not res["ok"] and "dcs listener link" in res["error"] and all(m.closed for m in made)
