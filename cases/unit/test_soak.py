# SPDX-License-Identifier: MIT
"""dvtest/soak.py: the verdict of the web soak case on synthetic series, the sample file reader and the
deterministic traffic mix."""
import pytest

from dvtest import metrics, soak
from dvtest import webload as wl

pytestmark = pytest.mark.unit

T0 = 1_000_000.0


def proc(name, t, phase, *, pid=10, start=500, rss=20000, threads=8, fds=30, seq=0):
    return {"kind": "proc", "t": t, "seq": seq, "phase": phase, "name": name, "pid": pid, "start": start, "rss_kib": rss,
            "hwm_kib": rss + 100, "threads": threads, "fds": fds, "ticks": 1}


def series(name="dv-edge-webrtc", *, rss=lambda t: 20000, threads=lambda t: 8, fds=lambda t: 30, idle_fds=30, idle0_fds=30,
           restart_at=None, gone_at=None, warm=600, measured=3000, step=30):
    """Rows of one child: idle0 (2 samples), warmup, measured, idle (3 samples)."""
    rows = [proc(name, T0 - 40 + 10 * i, "idle0", fds=idle0_fds) for i in range(2)]
    t = 0
    while t < warm + measured:
        phase = "warmup" if t < warm else "measured"
        pid, start = (10, 500) if restart_at is None or t < restart_at else (11, 9000)
        if gone_at is not None and t >= gone_at:
            rows.append({"kind": "proc", "t": T0 + t, "seq": 0, "phase": phase, "name": name, "absent": True})
        else:
            rows.append(proc(name, T0 + t, phase, pid=pid, start=start, rss=rss(t), threads=threads(t), fds=fds(t)))
        t += step
    end = T0 + warm + measured + 200
    pid, start = (10, 500) if restart_at is None else (11, 9000)
    rows += [proc(name, end + 40 * i, "idle", pid=pid, start=start, fds=idle_fds) for i in range(3)]
    return rows


def events_ok():
    return {"ok": True, "worst_minute": 0, "after_warmup": 0}


def gauge_rows(values=(0, 0, 0)):
    return [soak.gauge_row(T0 + 4000 + i, "idle", v) for i, v in enumerate(values)]


def counter_rows(growth=None, idle_growth=None):
    base = {"dv_hub_dropped_total": 5.0, "dv_panics_total": 0.0, "dv_edge_refused_total": 2.0}
    snap = {}
    for label in ("idle0", "warmup_end", "measured_end", "idle_start", "idle_end"):
        snap[label] = dict(base)
    for fam, v in (growth or {}).items():
        for label in ("measured_end", "idle_start", "idle_end"):
            snap[label][fam] += v
    for fam, v in (idle_growth or {}).items():
        snap["idle_end"][fam] += v
    return [soak.counters_row(T0 + i, label, c) for i, (label, c) in enumerate(snap.items())]


def verdict(rows, **kw):
    return soak.evaluate(rows + kw.pop("extra", gauge_rows() + counter_rows()), events=kw.pop("events", events_ok()), **kw)


def failed(res):
    return sorted(c.name for c in res.checks if not c.ok)


# ---- the criteria on synthetic series

def test_flat_series_passes_and_reports_a_table():
    res = verdict(series())
    assert res.ok, failed(res)
    rec = res.children["dv-edge-webrtc"]
    assert rec["restarts"] == 0 and rec["rss_kib"]["slope_kib_per_min"] == 0.0 and rec["fds"]["idle_end"] == 30
    text = soak.table(res)
    assert "dv-edge-webrtc" in text and text.splitlines()[0].startswith("child")


def test_noise_inside_the_limits_passes():
    res = verdict(series(rss=lambda t: 20000 + (37 * (t // 30) % 11) * 40, threads=lambda t: 8 + (t // 30) % 2, fds=lambda t: 30 + (t // 30) % 3))
    assert res.ok, failed(res)


def test_a_leak_fails_on_the_slope_not_on_the_last_sample():
    # 10 KiB per minute = 500 KiB over 50 minutes on 20000 KiB: under max(5 MiB, 10 percent)
    assert verdict(series(rss=lambda t: 20000 + t / 6.0)).ok
    # 200 KiB per minute = 10 MB over 50 minutes: over the 5 MiB floor and over 10 percent of 20 MB
    res = verdict(series(rss=lambda t: 20000 + max(0, t - 600) * 200 / 60.0))
    assert "rss:dv-edge-webrtc" in failed(res) and not res.ok


def test_one_late_spike_does_not_fail_when_the_slope_is_flat():
    rows = series()
    rows[-4]["rss_kib"] += 9000           # the last measured sample jumps (a burst), the line stays flat
    assert verdict(rows).ok


def test_the_limit_is_ten_percent_for_a_big_process():
    # 200 MB at the end of warmup: 10 percent = 20 MB, so 15 MB of growth passes where 5 MiB would fail
    big = verdict(series(rss=lambda t: 200000 + max(0, t - 600) * 15000 / 3000.0))
    assert big.ok, failed(big)
    assert not verdict(series(rss=lambda t: 200000 + max(0, t - 600) * 30000 / 3000.0)).ok


def test_a_restart_fails():
    res = verdict(series(restart_at=1500))
    assert "no-restart:dv-edge-webrtc" in failed(res)
    assert res.children["dv-edge-webrtc"]["restarts"] == 1


def test_same_pid_new_start_time_is_a_restart():
    rows = series()
    for r in rows:
        if r["phase"] == "measured" and r["t"] > T0 + 2000:
            r["start"] = 7777
    assert "no-restart:dv-edge-webrtc" in failed(verdict(rows))


def test_a_vanished_child_fails():
    res = verdict(series(gone_at=2000))
    assert "no-restart:dv-edge-webrtc" in failed(res)


def test_a_child_that_appears_late_fails():
    rows = [r for r in series("dv-recorder") if r["phase"] != "warmup" and r["phase"] != "idle0"]
    assert "no-restart:dv-recorder" in failed(verdict(rows))


def test_an_fd_leak_fails():
    res = verdict(series(fds=lambda t: 30 + max(0, t - 600) // 60))      # +50 over the measured part
    assert "fds:dv-edge-webrtc" in failed(res)


def test_fds_that_stay_after_the_traffic_fail_the_idle_check():
    assert verdict(series(idle_fds=35)).ok                                 # +5 is allowed
    res = verdict(series(idle_fds=36))
    assert failed(res) == ["fds-idle:dv-edge-webrtc"]


def test_a_thread_leak_fails_and_a_small_wobble_does_not():
    assert verdict(series(threads=lambda t: 8 if t < 1800 else 9)).ok      # floor of 2 threads
    res = verdict(series(threads=lambda t: 8 + max(0, t - 600) // 300))
    assert "threads:dv-edge-webrtc" in failed(res)


def test_unreadable_fds_are_unmeasured_not_passed_silently():
    res = verdict(series(fds=lambda t: -1, idle_fds=-1, idle0_fds=-1))
    assert res.ok and res.unmeasured and "fds unreadable" in res.unmeasured[0]
    assert res.children["dv-edge-webrtc"]["fds"] == {"available": False}


def test_too_few_samples_fail():
    rows = [r for r in series() if r["phase"] != "measured"] + [proc("dv-edge-webrtc", T0 + 700, "measured")]
    assert "samples:dv-edge-webrtc" in failed(verdict(rows))


def test_an_expected_child_that_was_never_sampled_fails():
    res = verdict(series(), expected=("dv-edge-webrtc", "dv-room"))
    assert failed(res) == ["present:dv-room"]


def test_an_allow_listed_child_that_does_not_run_is_ignored():
    rows = series() + [soak.absent_row("dv-recorder", T0 + 30 * i, "measured", 0) for i in range(5)]
    assert verdict(rows).ok


def test_several_children_are_judged_one_by_one():
    rows = series("dv-edge-xlx") + series("dv-svc-web", rss=lambda t: 20000 + max(0, t - 600) * 400 / 60.0)
    res = verdict(rows)
    assert failed(res) == ["rss:dv-svc-web"] and set(res.children) == {"dv-edge-xlx", "dv-svc-web"}


# ---- counters, events, idle

def test_counters_panic_must_not_grow_and_drops_must_not_grow_idle():
    assert verdict(series(), extra=gauge_rows() + counter_rows(growth={"dv_hub_dropped_total": 400.0})).ok     # recorded only
    res = verdict(series(), extra=gauge_rows() + counter_rows(growth={"dv_panics_total": 1.0}))
    assert failed(res) == ["counters-panic"]
    res = verdict(series(), extra=gauge_rows() + counter_rows(idle_growth={"dv_edge_refused_total": 3.0}))
    assert failed(res) == ["counters-idle"]
    res = verdict(series(), extra=gauge_rows())
    assert set(failed(res)) == {"counters-panic", "counters-idle"}


def test_events_and_idle_gauge():
    assert failed(verdict(series(), events={"ok": False, "worst_minute": 12, "after_warmup": 40})) == ["events-quiet"]
    assert failed(verdict(series(), events=None)) == ["events-quiet"]
    assert failed(verdict(series(), extra=gauge_rows((0, 2, 0)) + counter_rows())) == ["idle-listeners-zero"]
    assert failed(verdict(series(), extra=counter_rows())) == ["idle-listeners-zero"]


def test_error_counters_from_hub_samples():
    text = ('dv_hub_dropped_total{reason="a"} 3\ndv_hub_dropped_total{reason="b"} 4\ndv_up 1\n'
            'dv_edge_refused_total 2\ndv_room_panics_total 0\ndv_rx_total 99\n')
    assert soak.error_counters(metrics.parse(text)) == {"dv_hub_dropped_total": 7.0, "dv_edge_refused_total": 2.0, "dv_room_panics_total": 0.0}


# ---- maths and the sample file

def test_least_squares():
    slope, icpt = soak.least_squares([0, 1, 2, 3], [1, 3, 5, 7])
    assert slope == pytest.approx(2.0) and icpt == pytest.approx(1.0)
    assert soak.least_squares([5], [9]) == (0.0, 9.0)
    assert soak.least_squares([2, 2], [1, 3]) == (0.0, 2.0)
    with pytest.raises(ValueError):
        soak.least_squares([], [])
    assert soak.median([3, 1, 2]) == 2 and soak.median([1, 2, 3, 4]) == 2.5 and soak.median([]) is None


PROC_REPLY = ("pid=17\ncomm=dv-edge-webrtc\nutime=612\nstime=340\nclk_tck=100\nvmhwm_kb=34400\nvmrss_kb=33000\nthreads=9\n"
              "fds=41\nstarttime=123456\nt=1000.5\n")


def test_sample_rows_round_trip_through_the_file():
    sample = wl.parse_procstat_out(PROC_REPLY)
    rows = [soak.proc_row(sample, "warmup", 3), soak.absent_row("dv-room", 1001.0, "warmup", 3),
            soak.counters_row(1002.0, "idle0", {"b": 2.0, "a": 1.0}), soak.gauge_row(1003.0, "idle", 0), soak.mark_row(1004.0, "idle", "start")]
    text = "".join(soak.dump_line(r) + "\n" for r in rows)
    back, bad = soak.parse_jsonl(text + "\n")
    assert bad == 0 and back == rows
    assert back[0]["pid"] == 17 and back[0]["start"] == 123456 and back[0]["fds"] == 41 and back[0]["rss_kib"] == 33000 and back[0]["ticks"] == 952
    assert text.count("\n") == len(rows) and all("\n" not in soak.dump_line(r) for r in rows)


def test_parser_counts_damaged_lines_and_keeps_going():
    good = soak.dump_line(soak.gauge_row(5.0, "idle", 0))
    text = "\n".join([good, '{"kind":"proc","t":', "[1,2]", '{"kind":"nope","t":1}', '{"kind":"gauge"}', "", good])
    rows, bad = soak.parse_jsonl(text)
    assert len(rows) == 2 and bad == 4


# ---- the traffic mix

LANES = ("usrp-a", "usrp-b", "dstar", "dmr", "ysf", "m17", "nxdn", "p25")


def test_overs_are_deterministic_from_the_seed():
    a = soak.build_overs(7, LANES, 3600)
    assert a == soak.build_overs(7, LANES, 3600)
    assert a != soak.build_overs(8, LANES, 3600)


def test_overs_one_per_lane_and_minute_ten_seconds_inside_the_minute():
    overs = soak.build_overs(1, LANES, 3600)
    assert len(overs) == 60 * len(LANES)
    seen = set()
    for o in overs:
        minute = int(o["t"] // 60)
        assert (minute, o["lane"]) not in seen
        seen.add((minute, o["lane"]))
        assert 9.0 <= o["dur_s"] <= 11.0
        assert o["t"] % 60 + o["dur_s"] <= 58.0
    assert [o["t"] for o in overs] == sorted(o["t"] for o in overs)


def concurrent(overs, t):
    return {o["lane"] for o in overs if o["t"] <= t < o["t"] + o["dur_s"]}


def test_some_overs_run_at_the_same_time_on_different_modules():
    overs = soak.build_overs(1, LANES, 3600)
    peak = max(len(concurrent(overs, o["t"] + 0.5)) for o in overs)
    assert peak >= 2
    storm = [o for o in overs if int(o["t"] // 60) == 4]                       # every 5th minute they all start together
    assert len(concurrent(storm, max(o["t"] for o in storm) + 0.1)) == len(LANES)
    quiet = [o for o in overs if int(o["t"] // 60) == 0]
    assert max(len(concurrent(quiet, o["t"] + 0.5)) for o in quiet) < len(LANES)


def test_overs_with_few_lanes_and_short_runs():
    assert soak.build_overs(1, (), 3600) == []
    assert soak.build_overs(1, ("dmr",), 59) == []
    one = soak.build_overs(1, ("dmr",), 120)
    assert [o["lane"] for o in one] == ["dmr", "dmr"]


def test_sessions_are_deterministic_and_respect_the_budgets():
    s = soak.build_sessions(3, 3600)
    assert s == soak.build_sessions(3, 3600) and s != soak.build_sessions(4, 3600)
    assert soak.validate_sessions(s) == []
    plan = soak.summarize_plan(soak.build_overs(3, LANES, 3600), s)
    assert plan["sessions"] == 100 + plan["leaves"]
    assert 280 <= plan["leaves"] <= 320                                       # about 5 a minute for the 58 minutes after the first 2


def test_sessions_hold_one_hundred_listeners_and_churn_about_five_a_minute():
    s = soak.build_sessions(3, 3600)
    def live(t):
        return sum(1 for x in s if x["offset"] <= t and (x["leave"] is None or x["leave"] > t))

    assert all(live(t) >= 95 for t in range(200, 3550, 50))
    assert max(live(t) for t in range(0, 3600, 5)) <= 100
    assert live(115) == 100 or live(125) == 100
    leaves = sorted(x["leave"] for x in s if x["leave"] is not None)
    per_min = [sum(1 for t in leaves if 60 * m <= t < 60 * m + 60) for m in range(3, 58)]
    assert 3 <= min(per_min) and max(per_min) <= 7
    assert {x["room"] for x in s} == {"A", "B"} and max(x["account"] for x in s) + 1 == wl.accounts_needed(100)


def test_a_session_keeps_its_seat_login_and_room():
    s = soak.build_sessions(3, 3600)
    seat_info = {}
    for x in s:
        assert seat_info.setdefault(x["seat"], (x["login"], x["room"], x["account"])) == (x["login"], x["room"], x["account"])
        assert x["login"] == x["seat"] // wl.CIRCUITS_PER_LOGIN


def test_join_budget_and_validation_catch_violations():
    assert soak.join_budget_ok([0, 0, 0, 0, 1.05, 2.1])
    assert not soak.join_budget_ok([0, 0, 0, 0, 0, 0])
    s = soak.build_sessions(3, 600)
    bad = [dict(x) for x in s] + [dict(s[0], j=999, offset=300.0, leave=None)]            # a second session on seat 0
    assert any("seat 0 holds two" in p for p in soak.validate_sessions(bad))
    crowded = [dict(x, offset=0.0) for x in s[:20]]
    assert any("per-address" in p for p in soak.validate_sessions(crowded))
    nine = [{"j": i, "seat": i, "login": 0, "account": 0, "room": "A", "offset": 10.0 * i, "leave": None} for i in range(9)]
    assert any("login 0 holds 9" in p for p in soak.validate_sessions(nine))
    assert any("under 20 s" in p for p in soak.validate_sessions([dict(s[0], leave=s[0]["offset"] + 5)]))
    assert any("duplicate" in p for p in soak.validate_sessions([s[0], dict(s[1], j=s[0]["j"])]))


def test_no_churn_gives_just_the_seats():
    s = soak.build_sessions(3, 3600, churn_per_min=0)
    assert len(s) == 100 and all(x["leave"] is None for x in s)
    assert soak.build_sessions(3, 3600, seats=8, churn_per_min=5)[0]["login"] == 0


def test_constants_follow_the_lead_spec():
    assert soak.WARMUP_S + soak.MEASURED_S == 3600 and soak.MEASURED_S == 50 * 60 and soak.WALL_CAP_S == 75 * 60
    assert soak.SAMPLE_S == 30 and soak.IDLE_S == 120 and soak.SEATS == 100 and soak.CHURN_PER_MIN == 5


# ---- the generator's finish document

def gen_rows(n=100, **over):
    rows = [{"j": j, "room": "A" if j < n // 2 else "B", "connect_s": 1.0, "packets": 4000, "invalid": 0, "left": j % 4 == 0,
             "error": None, "alive": {"204": 30}} for j in range(n)]
    for r in rows:
        r.update(over)
    return rows


def test_generator_summary_passes_a_healthy_run():
    summary, checks = soak.generator_summary(gen_rows())
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert summary["sessions"] == 100 and summary["left"] == 25 and summary["alive"] == {"204": 3000}
    assert {c.name for c in checks} == {"generator-sessions", "generator-leases", "heard:room-A", "heard:room-B"}


def test_generator_summary_catches_failures_refusals_and_silent_rooms():
    rows = gen_rows()
    for r in rows[:5]:
        r["error"] = "offer HTTP 429 slow down"
    _s, checks = soak.generator_summary(rows)
    assert [c.name for c in checks if not c.ok] == ["generator-sessions"]
    rows = gen_rows()
    for r in rows[:10]:
        r["alive"] = {"204": 3, "403": 20}
    assert [c.name for c in soak.generator_summary(rows)[1] if not c.ok] == ["generator-leases"]
    rows = gen_rows()
    for r in rows[50:]:
        r["packets"] = 3
    assert [c.name for c in soak.generator_summary(rows)[1] if not c.ok] == ["heard:room-B"]
    assert not soak.generator_summary([])[1][0].ok
