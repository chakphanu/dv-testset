# SPDX-License-Identifier: MIT
"""dvtest/isolation.py: the mode table, the activity plan, the statistics from synthetic samples, the activity checks, the
document and the offline summarizer. No rig."""
import json

import pytest

from dvtest import isolation as iso
from dvtest import soak

pytestmark = pytest.mark.unit

LANES = ["usrp-a", "usrp-b", "dstar", "dmr", "ysf", "m17", "nxdn", "p25"]


# ------------------------------------------------------------------ mode table

def test_the_five_modes_and_their_names():
    assert list(iso.MODES) == ["A", "B", "C", "D", "E"]
    assert [m.name for m in iso.MODES.values()] == ["idle", "webrtc-churn", "voice", "proto-churn", "webrtc-voice"]
    for text, letter in (("A", "A"), ("a", "A"), ("idle", "A"), ("webrtc-churn", "B"), ("Voice", "C"), ("proto-churn", "D"), ("webrtc-voice", "E")):
        assert iso.resolve(text).letter == letter
    for bad in ("", "F", "all", "soak", None):
        with pytest.raises(ValueError, match="not a mode"):
            iso.resolve(bad)


def test_every_mode_runs_exactly_one_activity():
    a, b, c, d, e = (iso.MODES[x] for x in "ABCDE")
    assert (a.listeners, a.churn_per_min, a.voice, a.proto_churn) == (0, 0, "none", False)
    assert (b.listeners, b.churn_per_min, b.voice, b.proto_churn) == (100, 5, "none", False)
    assert (c.listeners, c.churn_per_min, c.voice, c.proto_churn) == (0, 0, "all", False)
    assert (d.listeners, d.churn_per_min, d.voice, d.proto_churn) == (0, 0, "none", True)
    assert (e.listeners, e.churn_per_min, e.voice, e.proto_churn) == (100, 0, "one", False)
    assert [m.needs_generator for m in iso.MODES.values()] == [False, True, False, False, True]
    assert [m.needs_lanes for m in iso.MODES.values()] == [False, False, True, True, True]
    for m in iso.MODES.values():                              # one activity: listeners, or voice, or login cycles (E = listeners + voice in one room)
        assert (m.voice != "none") + m.proto_churn + (m.listeners > 0) <= (2 if m.letter == "E" else 1)
        assert not (m.proto_churn and m.voice != "none")


def test_lanes_per_mode():
    assert iso.lanes_for(iso.MODES["A"], LANES) == [] and iso.lanes_for(iso.MODES["B"], LANES) == []
    assert iso.lanes_for(iso.MODES["C"], LANES) == LANES and iso.lanes_for(iso.MODES["D"], LANES) == LANES
    assert iso.lanes_for(iso.MODES["E"], LANES) == ["usrp-a"]
    assert iso.lanes_for(iso.MODES["E"], LANES, "usrp-b") == ["usrp-b"]
    assert iso.e_lane_of({}) == "usrp-a" and iso.e_lane_of({"DVT_ISOLATION_E_LANE": "usrp-b"}) == "usrp-b"
    with pytest.raises(ValueError):
        iso.e_lane_of({"DVT_ISOLATION_E_LANE": "dmr"})


# ------------------------------------------------------------------ timing

def test_timing_default_short_and_overrides():
    tm = iso.timing({})
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"], tm["smoke"], tm["short"]) == (300.0, 600.0, 30.0, False, False)
    tm = iso.timing({"DVT_ISOLATION_SHORT": "1"})
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"], tm["smoke"], tm["short"]) == (120.0, 180.0, 15.0, True, True)
    tm = iso.timing({"DVT_ISOLATION_WARMUP_S": "10", "DVT_ISOLATION_MEASURE_S": "60", "DVT_ISOLATION_SAMPLE_S": "5"})
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"], tm["smoke"]) == (10.0, 60.0, 5.0, True)
    with pytest.raises(ValueError):
        iso.timing({"DVT_ISOLATION_MEASURE_S": "60", "DVT_ISOLATION_SAMPLE_S": "30"})            # fewer than 6 samples
    with pytest.raises(ValueError):
        iso.timing({"DVT_ISOLATION_WARMUP_S": "abc"})


# ------------------------------------------------------------------ the plan

def test_plan_of_each_mode_in_the_full_run():
    run_s = 900.0
    plans = {m: iso.build_plan(iso.MODES[m], "1", run_s, LANES) for m in "ABCDE"}
    assert plans["A"] == {"sessions": [], "overs": [], "cycles": [], "lanes": []}
    b = plans["B"]
    assert len(b["sessions"]) > 100 and b["overs"] == [] and b["cycles"] == [] and soak.validate_sessions(b["sessions"]) == []
    sb = iso.summarize_plan(b)
    assert 50 <= sb["leaves"] <= 75 and sb["cycles"] == 0                                  # about 5 a minute from 135 s to the end
    c = plans["C"]
    assert c["sessions"] == [] and c["cycles"] == [] and {o["lane"] for o in c["overs"]} == set(LANES)
    assert iso.summarize_plan(c)["overs_per_lane"]["dstar"] == 15                          # one over a minute for 15 minutes
    d = plans["D"]
    assert d["sessions"] == [] and d["overs"] == [] and {x["lane"] for x in d["cycles"]} == set(LANES)
    e = plans["E"]
    assert len(e["sessions"]) == 100 and all(s["leave"] is None for s in e["sessions"]) and soak.validate_sessions(e["sessions"]) == []
    assert {o["lane"] for o in e["overs"]} == {"usrp-a"} and e["lanes"] == ["usrp-a"] and e["cycles"] == []
    assert plans["B"] == iso.build_plan(iso.MODES["B"], "1", run_s, LANES)                 # deterministic
    assert plans["B"]["sessions"] != iso.build_plan(iso.MODES["B"], "2", run_s, LANES)["sessions"]


def test_cycles_every_few_seconds_some_lanes_faster_than_others():
    cycles = iso.build_cycles("1", LANES, 900.0)
    per = {}
    for c in cycles:
        per.setdefault(c["lane"], []).append(c)
    assert set(per) == set(LANES)
    for lane, lst in per.items():
        gaps = [b["t"] - a["t"] for a, b in zip(lst, lst[1:])]
        assert 2.9 <= min(gaps) and max(gaps) <= 10.1, (lane, min(gaps), max(gaps))
        assert all(1.0 <= c["dur_s"] <= 2.5 for c in lst)
        assert all(g > c["dur_s"] for g, c in zip(gaps, lst))                              # a lane never overlaps itself
        assert lst[-1]["t"] < 890.0
    counts = sorted(len(v) for v in per.values())
    assert counts[0] < counts[-1]                                                          # some lanes cycle more often than others
    assert 60 * len(cycles) / 900.0 / len(LANES) > 6                                       # more than 6 cycles a lane and minute
    assert cycles == sorted(cycles, key=lambda c: (c["t"], c["lane"]))


def test_schedule_of_picks_overs_or_cycles():
    assert iso.schedule_of(iso.build_plan(iso.MODES["C"], "1", 300.0, LANES))[1] == "over"
    items, method = iso.schedule_of(iso.build_plan(iso.MODES["D"], "1", 300.0, LANES))
    assert method == "cycle" and items
    assert iso.schedule_of(iso.build_plan(iso.MODES["A"], "1", 300.0, LANES)) == ([], "over")


# ------------------------------------------------------------------ statistics

def proc(t, name, phase, vm, seq=1, pid=10, start=5, threads=8, fds=30, **smaps):
    row = {"kind": "proc", "t": t, "seq": seq, "phase": phase, "name": name, "pid": pid, "start": start,
           "rss_kib": vm, "hwm_kib": vm, "threads": threads, "fds": fds, "ticks": 1}
    for k, v in smaps.items():
        row["smaps_%s_kib" % k] = v
    return row


def synth(slope_router=2.0, slope_status=1.0, minutes=10, step=30):
    """Rows of two focus children (per-second slopes), one flat child, snapshots and attr / gauge rows."""
    rows = [{"kind": "metrics", "t": 1000.0 + 300.0, "phase": "warmup_end", "counters": {}},
            {"kind": "metrics", "t": 1000.0 + 300.0 + minutes * 60, "phase": "measured_end", "counters": {}}]
    t = 1000.0
    seq = 0
    while t <= 1000.0 + 300.0 + minutes * 60:
        seq += 1
        ph = "warmup" if t < 1300.0 else "measured"
        dt = t - 1300.0
        rows.append(proc(t, "dv-router", ph, 20000 + slope_router * dt, seq, rss=20100 + slope_router * dt, anon=9000 + slope_router * dt * 0.5,
                          private_dirty=8000 + dt * 0.25, pss_anon=8900))
        rows.append(proc(t, "dv-svc-status", ph, 14000 + slope_status * dt, seq, rss=14100 + slope_status * dt, anon=5000, private_dirty=4000, pss_anon=4900))
        rows.append(proc(t, "dv-edge-xlx", ph, 12000, seq, pid=11))
        rows.append({"kind": "attr", "t": t, "phase": ph, "series_total": 3000 + int(dt / 30), "truncated": 0, "values": {}})
        rows.append({"kind": "gauge", "t": t, "phase": ph, "listeners": 100})
        t += step
    return rows


def test_series_stats_slope_per_minute():
    s = iso.series_stats([(0, 100), (60, 130), (120, 160)])
    assert s == {"n": 3, "first": 100, "last": 160, "min": 100, "max": 160, "slope_per_min": 30.0}
    assert iso.series_stats([]) == {"n": 0}
    assert iso.series_stats([(5, 7)])["slope_per_min"] == 0.0


def test_summarize_measures_the_measured_part_only():
    rows = synth(slope_router=2.0, slope_status=1.0)
    s = iso.summarize(rows)
    router, status = s["focus"]["dv-router"], s["focus"]["dv-svc-status"]
    assert router["vmrss"]["slope_per_min"] == pytest.approx(120.0, abs=0.1)                # 2 KiB/s
    assert status["vmrss"]["slope_per_min"] == pytest.approx(60.0, abs=0.1)
    assert router["vmrss"]["first"] == 20000 and router["n"] == 21                          # the first measured sample (t = 1300 is warmup_end)
    sm = router["smaps"]
    assert set(sm) == {"rss", "anon", "private_dirty", "pss_anon"}
    assert sm["rss"]["slope_per_min"] == pytest.approx(120.0, abs=0.1) and sm["anon"]["slope_per_min"] == pytest.approx(60.0, abs=0.1)
    assert sm["private_dirty"]["slope_per_min"] == pytest.approx(15.0, abs=0.1) and sm["pss_anon"]["slope_per_min"] == 0.0
    assert s["children"]["dv-edge-xlx"]["vmrss"]["slope_per_min"] == 0.0 and s["children"]["dv-edge-xlx"]["smaps"] == {}
    st = s["series_total"]
    assert st["first"] == 3000 and st["last"] == 3020 and st["slope_per_min"] == pytest.approx(2.0, abs=0.01)
    assert s["gauge"] == {"n": 21, "min": 100, "max": 100}


def test_summarize_assigns_phases_by_the_snapshots_and_counts_restarts():
    rows = synth()
    rows.append(proc(1000.0 + 300.0 + 700.0, "dv-router", "measured", 999, 99))              # a late tick of phase measured: after measured_end
    rows.append(proc(1000.0 + 300.0 + 300.0, "dv-router", "measured", 20000, 98, pid=77, start=9))    # a restarted router
    s = iso.summarize(rows)
    assert s["focus"]["dv-router"]["n"] == 22 and s["focus"]["dv-router"]["restarts"] == 1


def test_summarize_without_smaps_or_a_child():
    rows = [r for r in synth() if r.get("name") != "dv-svc-status"]
    for r in rows:
        for k in [k for k in r if k.startswith("smaps_")]:
            del r[k]
    s = iso.summarize(rows)
    assert s["focus"]["dv-svc-status"] is None and s["focus"]["dv-router"]["smaps"] == {}


def test_children_table_orders_by_the_largest_slope():
    lines = iso.children_table(iso.summarize(synth())["children"])
    assert lines[0].startswith("child") and lines[1].startswith("dv-router") and lines[2].startswith("dv-svc-status") and lines[3].startswith("dv-edge-xlx")
    assert "120.0" in lines[1]


# ------------------------------------------------------------------ the activity checks

def gensum(connected=100, failed=0, sessions=100, left=30, rooms=None):
    return {"connected": connected, "failed": failed, "sessions": sessions, "left": left,
            "rooms": rooms if rooms is not None else {"A": {"packets": 9000}, "B": {"packets": 0}}}


def traffic(n=10, ok=10, lanes=LANES):
    return {lane: {"overs": n, "ok": ok, "errors": {}, "heard_min": 100, "heard_mean": 100.0} for lane in lanes}


def names(checks, ok=None):
    return sorted(c.name for c in checks if ok is None or c.ok == ok)


def test_activity_idle_only_needs_samples():
    mode = iso.MODES["A"]
    plan = iso.summarize_plan(iso.build_plan(mode, "1", 900.0, []))
    checks = iso.activity_checks(mode, plan, iso.summarize(synth()))
    assert names(checks) == ["activity:sampled"] and checks[0].ok
    short = [r for r in synth() if r.get("name") != "dv-svc-status"]
    assert not iso.activity_checks(mode, plan, iso.summarize(short))[0].ok                  # the focus child is missing


def test_activity_webrtc_churn_needs_listeners_and_leaves():
    mode = iso.MODES["B"]
    plan = iso.summarize_plan(iso.build_plan(mode, "1", 900.0, []))
    summ = iso.summarize(synth())
    assert all(c.ok for c in iso.activity_checks(mode, plan, summ, {}, gensum(left=plan["leaves"])))
    assert "activity:churn" in names(iso.activity_checks(mode, plan, summ, {}, gensum(left=2)), ok=False)
    assert "activity:listeners-connected" in names(iso.activity_checks(mode, plan, summ, {}, gensum(connected=50)), ok=False)
    assert "activity:listeners-connected" in names(iso.activity_checks(mode, plan, summ, {}, gensum(failed=20)), ok=False)
    low = iso.summarize([dict(r, listeners=10) if r.get("kind") == "gauge" else r for r in synth()])
    assert "activity:listeners-gauge" in names(iso.activity_checks(mode, plan, low, {}, gensum(left=plan["leaves"])), ok=False)
    assert "activity:churn" not in names(iso.activity_checks(iso.MODES["E"], plan, summ, {}, gensum()))


def test_activity_voice_and_cycles_per_lane():
    c = iso.MODES["C"]
    plan = iso.summarize_plan(iso.build_plan(c, "1", 900.0, LANES))
    checks = iso.activity_checks(c, plan, iso.summarize(synth()), traffic(n=15, ok=15))
    assert all(x.ok for x in checks) and len(checks) == 1 + len(LANES)
    bad = traffic(n=15, ok=15)
    bad["dmr"]["ok"] = 3
    del bad["ysf"]
    assert names(iso.activity_checks(c, plan, iso.summarize(synth()), bad), ok=False) == ["activity:voice:dmr", "activity:voice:ysf"]
    d = iso.MODES["D"]
    dplan = iso.summarize_plan(iso.build_plan(d, "1", 900.0, LANES))
    ran = {lane: {"overs": n, "ok": n, "errors": {}} for lane, n in dplan["cycles_per_lane"].items()}
    assert all(x.ok for x in iso.activity_checks(d, dplan, iso.summarize(synth()), ran))
    few = dict(ran, p25={"overs": 2, "ok": 2, "errors": {}})
    assert names(iso.activity_checks(d, dplan, iso.summarize(synth()), few), ok=False) == ["activity:cycles:p25"]


def test_activity_webrtc_voice_needs_the_room_to_hear_it():
    e = iso.MODES["E"]
    plan = iso.summarize_plan(iso.build_plan(e, "1", 900.0, LANES))
    summ = iso.summarize(synth())
    good = iso.activity_checks(e, plan, summ, traffic(n=15, ok=15, lanes=["usrp-a"]), gensum())
    assert all(x.ok for x in good) and "activity:room-heard:A" in names(good)
    deaf = iso.activity_checks(e, plan, summ, traffic(n=15, ok=15, lanes=["usrp-a"]), gensum(rooms={"A": {"packets": 0}, "B": {"packets": 0}}))
    assert names(deaf, ok=False) == ["activity:room-heard:A"]


# ------------------------------------------------------------------ the document and the summarizer

def document(letter="A", router=2.0, status=1.0, run="20261103T000000Z"):
    mode = iso.MODES[letter]
    plan = iso.summarize_plan(iso.build_plan(mode, "1", 900.0, []))
    rows = synth(router, status)
    checks = iso.activity_checks(mode, plan, iso.summarize(rows)) if letter == "A" else [soak.Check("x", True, "")]
    doc = iso.assemble(mode, iso.timing({}), plan, rows, checks, {"run": run, "state": "done"})
    return doc


def test_assemble_has_the_keys_a_reader_needs():
    doc = document("A")
    assert doc["case"] == "ISOLATION" and doc["mode"] == "A" and doc["name"] == "idle" and doc["ok"] is True
    assert set(doc["focus"]) == {"dv-router", "dv-svc-status"} and doc["focus"]["dv-router"]["smaps"]["anon"]["first"] > 0
    assert doc["series_total"]["n"] == 21 and doc["timing"]["measured_s"] == 600.0 and doc["children_table"][0].startswith("child")
    json.dumps(doc)                                                                         # serialisable as is


def test_the_summary_table_over_several_runs(tmp_path, capsys):
    for letter, router in (("B", 3.0), ("A", 0.5), ("E", 1.5)):
        web = tmp_path / ("run-%s" % letter) / "web"
        web.mkdir(parents=True)
        (web / ("isolation-%s.json" % letter)).write_text(json.dumps(document(letter, router=router, run="run-" + letter)))
        (web / ("isolation-%s-samples.jsonl" % letter)).write_text("{}\n")               # the raw samples are not summarized
    other = tmp_path / "run-x"
    other.mkdir()
    (other / "isolation-D.json").write_text("not json")
    (other / "soak.json").write_text(json.dumps({"case": "SOAK1"}))
    paths = [str(tmp_path / "run-A"), str(tmp_path / "run-B"), str(tmp_path / "run-E" / "web"), str(other)]
    docs = iso.find_documents(paths)
    assert [d["mode"] for d in docs] == ["A", "B", "E"]
    assert iso.main(paths) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("mode") and "dv-router" in out[0] and "dv-svc-status" in out[0]
    rows = {line.split()[0]: line for line in out if line[:1] in "ABE" and line[1:2] == " "}
    assert set(rows) == {"A", "B", "E"}
    assert "30.0" in rows["A"] and "180.0" in rows["B"] and "90.0" in rows["E"]             # Rss slopes of dv-router: 0.5 / 3 / 1.5 KiB/s
    assert rows["A"].split("|")[2].split()[0] == "60.0"                                     # dv-svc-status Rss slope, KiB/min
    assert "Rss = smaps_rollup" in out[-1]


def test_the_summarizer_exit_codes_and_children_option(tmp_path, capsys):
    assert iso.main([str(tmp_path)]) == 1
    assert "no isolation-*.json" in capsys.readouterr().err
    f = tmp_path / "isolation-A.json"
    f.write_text(json.dumps(document("A")))
    assert iso.main([str(f), "--children"]) == 0
    text = capsys.readouterr().out
    assert "mode A idle" in text and "dv-edge-xlx" in text


def test_the_table_falls_back_to_vmrss_without_smaps_and_prints_dashes():
    doc = document("A")
    for rec in doc["focus"].values():
        rec["smaps"] = {}
    line = [x for x in iso.table([doc]).splitlines() if x.startswith("A ")][0]
    cells = line.split("|")[1].split()
    assert cells[0] == "120.0" and cells[1:] == ["-", "-"]
    doc["focus"]["dv-svc-status"] = None
    assert iso.table([doc]).splitlines()[3].split("|")[2].split() == ["-", "-", "-"]


# ------------------------------------------------------------------ the pytest options (rig/compose.yml does not forward DVT_ISOLATION*)

def test_plugin_options_set_the_environment():
    from dvtest import isolation_plugin as plug
    env = {"DVT_ISOLATION": "old"}
    got = plug.apply_options(env, "B", True, ["DVT_ISOLATION_SEED=7", "DVT_SOAK_MODULES=dstar=D,dmr=E"])
    assert env == got == {"DVT_ISOLATION": "B", "DVT_ISOLATION_SHORT": "1", "DVT_ISOLATION_SEED": "7", "DVT_SOAK_MODULES": "dstar=D,dmr=E"}
    env = {"DVT_ISOLATION": "keep"}
    assert plug.apply_options(env) == {} and env == {"DVT_ISOLATION": "keep"}
    for bad in ("PATH=/x", "DVT_ISOLATION_SEED", "DVT_WEB=off", ""):
        with pytest.raises(ValueError):
            plug.parse_env_option(bad)


def test_plugin_loads_in_a_real_pytest_run(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    test = tmp_path / "test_probe.py"
    test.write_text("import os\ndef test_env():\n    assert os.environ['DVT_ISOLATION'] == 'C' and os.environ['DVT_ISOLATION_SHORT'] == '1'\n"
                    "    assert os.environ['DVT_ISOLATION_SEED'] == '3'\n")
    cmd = [sys.executable, "-m", "pytest", "-p", "dvtest.isolation_plugin", "--isolation=C", "--isolation-short", "--isolation-env", "DVT_ISOLATION_SEED=3",
           "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path), str(test)]
    res = subprocess.run(cmd, cwd=str(tmp_path), env={**__import__("os").environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout[-600:] + res.stderr[-300:]
    bad = subprocess.run(cmd[:5] + ["--isolation-env", "HOME=/x", str(test)], cwd=str(tmp_path), env={**__import__("os").environ, "PYTHONPATH": str(root)},
                         capture_output=True, text=True, timeout=120)
    assert bad.returncode == 4 and "DVT_ISOLATION_" in bad.stderr
