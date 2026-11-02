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


FLAT_BASE = dict({fam: 0.0 for fam in soak.FLAT_FAMILIES}, dv_edge_queue_drops_total=12.0)
REASONS_BASE = {"link_timeout": 30.0, "no_session": 10.0, "rx_ignored": 5.0, "x_stale": 2.0}
WINDOW_S = 3000.0


def counter_rows(growth=None, flat_growth=None, absent=(), flat_start=None, reasons=None, idle_reasons=None, window_s=WINDOW_S):
    """Counter snapshots: `growth` adds to the error-like families (recorded only) from the end of the measured part
    on, `flat_growth` {family: delta} to the flat families at the same points, `absent` names flat families that are not
    on /metrics, `flat_start` {family: value} replaces the base values (all five snapshots), `reasons` {reason: delta}
    grows the dv_edge_drops_total reasons over the measured part (and stays), `idle_reasons` {reason: delta} grows them
    in the idle window only, `window_s` is the length of the measured part. The snapshots carry the time (idle0,
    warmup_end 600 s, measured_end after `window_s`, idle_start 30 s later, idle_end 120 s after that)."""
    base = {"dv_hub_dropped_total": 5.0, "dv_panics_total": 0.0, "dv_edge_refused_total": 2.0}
    flat0 = {fam: None if fam in absent else v for fam, v in dict(FLAT_BASE, **(flat_start or {})).items()}
    times = {"idle0": T0 - 100, "warmup_end": T0 + 600, "measured_end": T0 + 600 + window_s, "idle_start": T0 + 630 + window_s,
             "idle_end": T0 + 750 + window_s}
    rows = []
    for label in ("idle0", "warmup_end", "measured_end", "idle_start", "idle_end"):
        counters, flat, rs = dict(base), dict(flat0), dict(REASONS_BASE)
        if label in ("measured_end", "idle_start", "idle_end"):
            for fam, v in (growth or {}).items():
                counters[fam] += v
            for fam, v in (flat_growth or {}).items():
                flat[fam] = (flat[fam] or 0.0) + v
            for reason, v in (reasons or {}).items():
                rs[reason] = rs.get(reason, 0.0) + v
        if label == "idle_end":
            for reason, v in (idle_reasons or {}).items():
                rs[reason] = rs.get(reason, 0.0) + v
        series = {fam: {"reason=x": v} for fam, v in flat.items() if v is not None}
        rows.append(soak.counters_row(times[label], label, counters, flat=flat, series=series, reasons=rs))
    return rows


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
    rows = [r for r in series("dv-svc-recorder") if r["phase"] != "warmup" and r["phase"] != "idle0"]
    assert "no-restart:dv-svc-recorder" in failed(verdict(rows))


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


def test_an_expected_name_with_several_processes_is_present_through_its_ranked_rows():
    rows = series("dv-room.01") + series("dv-room.02")
    res = verdict(rows, expected=("dv-room",))
    assert res.ok, failed(res)
    assert set(res.children) == {"dv-room.01", "dv-room.02"}
    assert failed(verdict(series("dv-roomx"), expected=("dv-room",))) == ["present:dv-room"]


def test_an_allow_listed_child_that_does_not_run_is_ignored():
    rows = series() + [soak.absent_row("dv-svc-recorder", T0 + 30 * i, "measured", 0) for i in range(5)]
    assert verdict(rows).ok


def test_several_children_are_judged_one_by_one():
    rows = series("dv-edge-xlx") + series("dv-svc-web", rss=lambda t: 20000 + max(0, t - 600) * 400 / 60.0)
    res = verdict(rows)
    assert failed(res) == ["rss:dv-svc-web"] and set(res.children) == {"dv-edge-xlx", "dv-svc-web"}


# ---- counters, events, idle

def test_flat_families_are_the_eleven_strict_ones_and_the_drops_family_is_judged_by_reason():
    assert soak.FLAT_FAMILIES == (
        "dv_edge_webrtc_panics_total", "dv_edge_queue_drops_total", "dv_edge_webrtc_audio_dropped_total",
        "dv_room_ingress_rejected_total", "dv_room_shell_errors_total", "dv_status_telemetry_rejected_total",
        "dv_status_input_dropped_total", "dv_fabric_put_dropped_total", "dv_recorder_dropped_total", "dv_codec_dropped_total",
        "dv_edge_tx_refused_total")
    assert soak.FLAT_OVERRIDES == {}                                         # starts empty, DV-Router rules on growth
    assert soak.DROPS_FAMILY == "dv_edge_drops_total" and soak.DROPS_FAMILY not in soak.FLAT_FAMILIES
    assert soak.DROPS_FAMILY in soak.SNAPSHOT_FAMILIES and len(soak.SNAPSHOT_FAMILIES) == 12
    assert soak.DROPS_LEGIT == ("link_timeout", "no_session", "rx_ignored", "x_stale") and soak.DROPS_RATE_FLAG_PER_S == 1.0
    assert set(soak.LAZY_FAMILIES) == {"dv_edge_tx_refused_total", "dv_recorder_dropped_total", "dv_room_shell_errors_total"}
    assert set(soak.LAZY_FAMILIES) <= set(soak.FLAT_FAMILIES)


def test_flat_counters_pass_and_error_like_growth_of_other_families_is_only_recorded():
    res = verdict(series(), extra=gauge_rows() + counter_rows(growth={"dv_hub_dropped_total": 400.0, "dv_panics_total": 3.0},
                                                              absent=tuple(soak.LAZY_FAMILIES)))
    assert res.ok, failed(res)
    assert res.counters["growing"] == ["dv_hub_dropped_total", "dv_panics_total"]
    flat = res.counters["flat"]
    assert flat["dv_edge_queue_drops_total"] == {"status": "flat", "start": 12.0, "end": 12.0, "delta": 0.0}
    assert flat["dv_edge_tx_refused_total"] == {"status": "absent", "start": 0.0, "end": 0.0, "delta": 0.0, "lazy": True}   # missing = 0
    assert "dv_edge_drops_total" not in flat and "dv_edge_queue_drops_total" in soak.counters_table(res) and "absent" in soak.counters_table(res)
    assert "dv_edge_drops_total reason" in soak.counters_table(res)


@pytest.mark.parametrize("fam,start", [("dv_edge_queue_drops_total", 12.0), ("dv_edge_webrtc_audio_dropped_total", 0.0),
                                       ("dv_edge_webrtc_panics_total", 0.0)])
def test_a_flat_family_that_grows_fails_naming_family_and_delta(fam, start):
    res = verdict(series(), extra=gauge_rows() + counter_rows(flat_growth={fam: 3.0}))
    assert failed(res) == ["counters-flat", "flat:" + fam]
    detail = next(c.detail for c in res.checks if c.name == "flat:" + fam)
    assert fam in detail and "grew by 3" in detail and "reason=x" in detail
    assert res.counters["flat"][fam]["status"] == "grew" and res.counters["flat"][fam]["delta"] == 3.0


def test_growth_only_in_the_idle_window_is_not_a_flat_failure():
    rows = counter_rows()
    rows[4]["flat"]["dv_edge_queue_drops_total"] += 5                          # idle_end only
    assert verdict(series(), extra=gauge_rows() + rows).ok


def test_a_family_that_shows_up_after_warmup_counts_from_zero_and_an_absent_one_is_not_asserted():
    rows = counter_rows(absent=("dv_room_shell_errors_total",))
    for r in rows[2:]:
        r["flat"]["dv_room_shell_errors_total"] = 2.0                          # "nonzero values only": first seen at the end
    res = verdict(series(), extra=gauge_rows() + rows)
    assert "flat:dv_room_shell_errors_total" in failed(res)
    assert res.counters["flat"]["dv_room_shell_errors_total"]["delta"] == 2.0
    res = verdict(series(), extra=gauge_rows() + counter_rows(absent=tuple(soak.FLAT_FAMILIES)))
    assert res.ok and {v["status"] for v in res.counters["flat"].values()} == {"absent"}


def test_an_override_records_the_growth_without_failing():
    res = verdict(series(), extra=gauge_rows() + counter_rows(flat_growth={"dv_edge_queue_drops_total": 7.0}),
                  params={"flat_overrides": {"dv_edge_queue_drops_total": "grows with the mix"}})
    assert res.ok, failed(res)
    info = res.counters["flat"]["dv_edge_queue_drops_total"]
    assert info["status"] == "override" and info["delta"] == 7.0 and info["reason"] == "grows with the mix"


def test_a_vanished_flat_family_is_recorded_only_and_missing_snapshots_fail():
    rows = counter_rows()
    rows[2]["flat"]["dv_edge_queue_drops_total"] = None
    res = verdict(series(), extra=gauge_rows() + rows)
    assert res.ok and res.counters["flat"]["dv_edge_queue_drops_total"]["status"] == "vanished"
    assert failed(verdict(series(), extra=gauge_rows())) == ["counters-flat"]


@pytest.mark.parametrize("fam", sorted(soak.LAZY_FAMILIES))
def test_a_lazy_family_may_be_missing_and_fails_only_when_it_appears_or_grows(fam):
    # missing everywhere = 0 = flat; never "must be present"
    res = verdict(series(), extra=gauge_rows() + counter_rows(absent=(fam,)))
    assert res.ok, failed(res)
    assert res.counters["flat"][fam]["status"] == "absent" and res.counters["flat"][fam]["delta"] == 0.0
    # it appears over the measured part (created at its first increment): growth from 0
    rows = counter_rows(absent=(fam,))
    for r in rows[2:]:
        r["flat"][fam] = 1.0
    res = verdict(series(), extra=gauge_rows() + rows)
    assert failed(res) == ["counters-flat", "flat:" + fam]
    assert res.counters["flat"][fam]["status"] == "grew" and res.counters["flat"][fam]["start"] == 0.0 and res.counters["flat"][fam]["delta"] == 1.0
    # it appears with 0 only: still flat
    rows = counter_rows(absent=(fam,))
    for r in rows[2:]:
        r["flat"][fam] = 0.0
    assert verdict(series(), extra=gauge_rows() + rows).ok
    # present with a value at the end of warmup and growing: fails as any strict family
    res = verdict(series(), extra=gauge_rows() + counter_rows(flat_start={fam: 3.0}, flat_growth={fam: 2.0}))
    assert "flat:" + fam in failed(res)


def test_the_strict_queue_drops_stay_flat_and_a_missing_series_is_zero():
    for fam in ("dv_edge_queue_drops_total", "dv_edge_webrtc_audio_dropped_total"):
        assert fam in soak.FLAT_FAMILIES
        assert verdict(series(), extra=gauge_rows() + counter_rows(absent=(fam,))).ok
        res = verdict(series(), extra=gauge_rows() + counter_rows(flat_start={fam: 0.0}, flat_growth={fam: 1.0}))
        assert failed(res) == ["counters-flat", "flat:" + fam]
        rows = counter_rows(absent=(fam,))
        for r in rows[2:]:
            r["flat"][fam] = 4.0                                              # appears in the window
        assert "flat:" + fam in failed(verdict(series(), extra=gauge_rows() + rows))


# ---- dv_edge_drops_total: by reason, not flat

def test_growth_of_the_legit_drop_reasons_passes_and_is_recorded_with_its_rate():
    res = verdict(series(), extra=gauge_rows() + counter_rows(reasons={"link_timeout": 60.0, "no_session": 30.0, "x_stale": 3.0}))
    assert res.ok, failed(res)
    d = res.counters["drops"]
    assert d["reasons"]["link_timeout"]["delta"] == 60.0 and d["reasons"]["link_timeout"]["rate_per_s"] == pytest.approx(0.02)
    assert d["reasons"]["rx_ignored"]["delta"] == 0.0 and d["legit_total"] == 93.0
    assert d["legit_rate_per_s"] == pytest.approx(93.0 / 3000.0) and d["window_s"] == 3000.0 and not d.get("flagged")
    assert res.warnings == [] and "dv_edge_drops_total" not in res.counters["growing"]
    assert {c.name for c in res.checks} >= {"drops-reasons", "drops-idle"}


def test_a_drop_reason_outside_the_legit_set_that_appears_or_grows_fails():
    res = verdict(series(), extra=gauge_rows() + counter_rows(reasons={"hold_drop": 1.0}))              # appears: growth from 0
    assert failed(res) == ["drops-reasons"]
    assert "hold_drop" in next(c.detail for c in res.checks if c.name == "drops-reasons")
    assert res.counters["drops"]["outside"] == {"hold_drop": 1.0}
    base = counter_rows()
    for r in base:
        r["reasons"]["queue_full"] = 4.0                                      # present from the start, then grows
    for r in base[2:]:
        r["reasons"]["queue_full"] += 2.0
    assert failed(verdict(series(), extra=gauge_rows() + base)) == ["drops-reasons"]
    quiet = counter_rows()
    for r in quiet:
        r["reasons"]["queue_full"] = 4.0                                      # present but flat: fine
    assert verdict(series(), extra=gauge_rows() + quiet).ok


def test_the_legit_drop_rate_above_one_per_second_is_flagged_not_failed():
    res = verdict(series(), extra=gauge_rows() + counter_rows(reasons={"link_timeout": 2400.0, "no_session": 900.0}))      # 3300 in 3000 s
    assert res.ok, failed(res)
    assert res.counters["drops"]["flagged"] and res.counters["drops"]["legit_rate_per_s"] == pytest.approx(1.1)
    assert len(res.warnings) == 1 and "FLAG dv_edge_drops_total" in res.warnings[0] and "link_timeout" in res.warnings[0]
    assert "FLAGGED" in soak.counters_table(res)
    at = verdict(series(), extra=gauge_rows() + counter_rows(reasons={"link_timeout": 3000.0}))                              # exactly 1/s
    assert at.warnings == []
    short = verdict(series(), extra=gauge_rows() + counter_rows(reasons={"link_timeout": 300.0}, window_s=240.0))             # 1.25/s in 240 s
    assert short.ok and short.warnings and short.counters["drops"]["window_s"] == 240.0


def test_any_drop_growth_in_the_idle_window_fails():
    res = verdict(series(), extra=gauge_rows() + counter_rows(idle_reasons={"link_timeout": 1.0}))
    assert failed(res) == ["drops-idle"] and "link_timeout" in next(c.detail for c in res.checks if c.name == "drops-idle")
    assert failed(verdict(series(), extra=gauge_rows() + counter_rows(idle_reasons={"brand_new": 1.0}))) == ["drops-idle"]
    # growth over the measured part only (frozen in the idle window) is fine
    assert verdict(series(), extra=gauge_rows() + counter_rows(reasons={"link_timeout": 50.0})).ok


def test_drops_without_per_reason_snapshots_fail_and_no_counter_rows_fail_once():
    rows = counter_rows()
    for r in rows:
        del r["reasons"]
    assert failed(verdict(series(), extra=gauge_rows() + rows)) == ["drops-idle", "drops-reasons"]
    assert failed(verdict(series(), extra=gauge_rows())) == ["counters-flat"]


def test_drop_reasons_and_gauge_values_of_a_scrape():
    text = ('dv_edge_drops_total{proc="a",reason="no_session"} 3\ndv_edge_drops_total{proc="b",reason="no_session"} 4\n'
            'dv_edge_drops_total{proc="b",reason="x_stale"} 1\ndv_edge_drops_total{proc="b"} 2\n'
            'dv_status_clients{protocol="p",kind="k"} 3\ndv_status_clients{protocol="q",kind="k"} 4\ndv_up 1\ndv_x_total 9\n')
    samples = metrics.parse(text)
    assert soak.drop_reasons(samples) == {"-": 2.0, "no_session": 7.0, "x_stale": 1.0}
    assert soak.gauge_values(samples) == {"dv_status_clients": {"n": 2, "sum": 7.0}, "dv_up": {"n": 1, "sum": 1.0}}


def test_flat_snapshot_sums_series_over_procs_and_marks_absent_families():
    text = ('dv_edge_drops_total{proc="dv-edge-ysf",reason="no_session"} 3\ndv_edge_drops_total{proc="dv-edge-m17",reason="no_session"} 4\n'
            'dv_edge_drops_total{proc="dv-edge-m17",reason="hold_drop"} 1\ndv_edge_webrtc_panics_total{proc="dv-edge-webrtc"} 0\n')
    flat, ser = soak.flat_snapshot(metrics.parse(text))
    assert flat["dv_edge_drops_total"] == 8.0 and flat["dv_edge_webrtc_panics_total"] == 0.0 and flat["dv_recorder_dropped_total"] is None
    assert ser["dv_edge_drops_total"] == {"reason=hold_drop": 1.0, "reason=no_session": 7.0} and "dv_recorder_dropped_total" not in ser


# ---- thresholds of the ruling: max(10 percent, +2 threads, +4 fds) over the end-of-warmup value

@pytest.mark.parametrize("base,end,ok", [(8, 10, True), (8, 11, False), (3, 5, True), (3, 6, False), (100, 110, True), (100, 111, False),
                                         (30, 33, True), (30, 34, False)])
def test_thread_limit_is_the_larger_of_ten_percent_and_two(base, end, ok):
    res = verdict(series(threads=lambda t: base if t < 600 else end))
    assert ("threads:dv-edge-webrtc" not in failed(res)) is ok, res.children["dv-edge-webrtc"]["threads"]


@pytest.mark.parametrize("base,end,ok", [(20, 24, True), (20, 25, False), (0 + 3, 7, True), (3, 8, False), (100, 110, True), (100, 111, False),
                                         (50, 55, True), (50, 56, False)])
def test_fd_limit_is_the_larger_of_ten_percent_and_four(base, end, ok):
    res = verdict(series(fds=lambda t: base if t < 600 else end, idle_fds=base, idle0_fds=base))
    assert ("fds:dv-edge-webrtc" not in failed(res)) is ok, res.children["dv-edge-webrtc"]["fds"]


def test_idle_fds_stay_within_five_of_the_idle_value_before_the_traffic():
    res = verdict(series(idle0_fds=20, fds=lambda t: 30, idle_fds=26))
    assert failed(res) == ["fds-idle:dv-edge-webrtc"]
    assert "fds-idle:dv-edge-webrtc" not in failed(verdict(series(idle0_fds=20, fds=lambda t: 30, idle_fds=25)))


# ---- timing: the real run and the short mode

def test_timing_defaults_are_the_real_run():
    tm = soak.timing({})
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"]) == (600.0, 3000.0, 30.0)
    assert not tm["short"] and not tm["smoke"] and tm["rss_scale"] == 1.0
    assert soak.timing({"DVT_SOAK_WARMUP_S": "", "DVT_SOAK_MEASURE_S": "", "DVT_SOAK_SHORT": ""})["smoke"] is False      # empty = unset (compose)


def test_timing_short_mode_and_overrides():
    tm = soak.timing({"DVT_SOAK_SHORT": "1"})
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"]) == (180.0, 240.0, 15.0) and tm["short"] and tm["smoke"]
    assert tm["rss_scale"] == pytest.approx(240.0 / 3000.0)
    tm = soak.timing({"DVT_SOAK_SHORT": "1", "DVT_SOAK_WARMUP_S": "60", "DVT_SOAK_MEASURE_S": "300", "DVT_SOAK_SAMPLE_S": "10"})
    assert (tm["warmup_s"], tm["measured_s"], tm["sample_s"]) == (60.0, 300.0, 10.0)
    assert soak.timing({"DVT_SOAK_MEASURED_S": "900"})["measured_s"] == 900.0                      # the older spelling
    assert soak.timing({"DVT_SOAK_MEASURE_S": "900", "DVT_SOAK_MEASURED_S": "1200"})["measured_s"] == 900.0
    assert soak.timing({"DVT_SOAK_SHORT": "0"})["short"] is False


@pytest.mark.parametrize("env", [{"DVT_SOAK_WARMUP_S": "x"}, {"DVT_SOAK_MEASURE_S": "-5"}, {"DVT_SOAK_MEASURE_S": "50"},
                                 {"DVT_SOAK_SAMPLE_S": "0"}, {"DVT_SOAK_MEASURE_S": "nan"}])
def test_timing_refuses_nonsense(env):
    with pytest.raises(ValueError):
        soak.timing(env)


def test_the_rss_bound_scales_with_a_short_measured_part():
    def leak(t):                                                                     # 80 KiB per minute, 320 KiB in 240 s
        return 20000 + max(0, t - 180) * 80 / 60.0

    short = dict(warm=180, measured=240, step=15)
    assert verdict(series(rss=leak, **short)).ok                                    # unscaled: 5 MiB in 240 s, passes
    res = verdict(series(rss=leak, **short), params={"rss_scale": 240 / 3000.0})   # scaled: 410 KiB allowed, 320 KiB grown
    assert res.ok, failed(res)
    res = verdict(series(rss=lambda t: 20000 + max(0, t - 180) * 150 / 60.0, **short), params={"rss_scale": 240 / 3000.0})
    assert failed(res) == ["rss:dv-edge-webrtc"]                                    # 150 KiB a minute = 600 KiB > 410 KiB
    assert res.children["dv-edge-webrtc"]["rss_kib"]["limit_kib"] == pytest.approx(5120 * 0.08, abs=0.5)
    assert verdict(series(rss=lambda t: 20000 + max(0, t - 180) * 150 / 60.0, **short)).ok          # the real bound would not see it


def test_a_short_run_does_not_judge_the_rss_slope_but_records_it_as_a_lead():
    short = dict(warm=180, measured=240, step=15)
    leak = series(rss=lambda t: 20000 + max(0, t - 180) * 150 / 60.0, **short)                   # 150 KiB a minute
    params = {"rss_scale": 240 / 3000.0, "rss_judged": False, "measured_s": 240.0}
    res = verdict(leak, params=params)
    assert res.ok, failed(res)
    rss = res.children["dv-edge-webrtc"]["rss_kib"]
    assert rss["judged"] is False and rss["over_limit"] is True and rss["slope_kib_per_min"] == pytest.approx(150.0, rel=0.01)
    check = next(c for c in res.checks if c.name == "rss:dv-edge-webrtc")
    assert check.ok and "LEAD ONLY" in check.detail
    assert len(res.warnings) == 1 and "RSS lead dv-edge-webrtc" in res.warnings[0]
    # the real run (judged, the default) fails the same series at the same scale
    assert failed(verdict(leak, params={"rss_scale": 240 / 3000.0})) == ["rss:dv-edge-webrtc"]
    # a short run that stays inside the bound has no lead
    assert verdict(series(**short), params=params).warnings == []


def test_the_full_run_rss_rule_is_the_slope_over_the_whole_measured_window():
    # end-of-warmup RSS 20000 KiB: bound = max(5 MiB, 10 percent) = 5120 KiB over 3000 s = 102.4 KiB/min
    ok = verdict(series(rss=lambda t: 20000 + max(0, t - 600) * 100 / 60.0), params={"measured_s": 3000.0})
    assert ok.ok, failed(ok)
    bad = verdict(series(rss=lambda t: 20000 + max(0, t - 600) * 105 / 60.0), params={"measured_s": 3000.0})
    assert failed(bad) == ["rss:dv-edge-webrtc"]
    rec = bad.children["dv-edge-webrtc"]["rss_kib"]
    assert rec["window_s"] == 3000.0 and rec["growth_kib"] == pytest.approx(5250.0, abs=60.0) and rec["limit_kib"] == 5120.0 and rec["judged"]
    # 10 percent of a big process: 200000 KiB -> 20000 KiB in 3000 s = 400 KiB/min
    assert verdict(series(rss=lambda t: 200000 + max(0, t - 600) * 395 / 60.0), params={"measured_s": 3000.0}).ok


def attr_rows(minutes=6, step=30.0, grow=None, truncated=0):
    """attr rows: a flat series, a growing series, the number of series of the scrape; phases warmup (2 minutes), measured."""
    rows = []
    for i in range(int(minutes * 60 / step)):
        t = i * step
        phase = "warmup" if t < 120 else "measured"
        mins = max(0.0, t - 120) / 60.0
        rows.append(soak.attr_row(T0 + t, phase, {"dv_status_clients{kind=web,proc=status}": 7.0, "dv_status_events_len{proc=status}": 100.0 + (grow or 0.0) * mins},
                                  200 + int(mins) * 3, truncated))
    return rows


def test_attribution_records_first_last_and_slope_per_series_and_the_series_count():
    res = verdict(series(), extra=gauge_rows() + counter_rows() + attr_rows(grow=10.0))
    assert res.ok, failed(res)                                                 # recorded, never asserted
    att = res.attribution
    ev = att["series"]["dv_status_events_len{proc=status}"]
    assert ev["first"] == 100.0 and ev["last"] == pytest.approx(135.0) and ev["slope_per_min"] == pytest.approx(10.0, rel=0.02) and ev["n"] == 8
    assert att["series"]["dv_status_clients{kind=web,proc=status}"]["slope_per_min"] == 0.0
    assert att["series_total"]["first"] == 200 and att["series_total"]["last"] == 209 and att["series_total"]["slope_per_min"] > 2.0
    assert att["order"][0] == "dv_status_events_len{proc=status}"
    raw = att["raw"]["dv_status_events_len{proc=status}"]
    assert len(raw) == 12 and raw[0] == [0.0, 100.0] and att["scrape"][0] == [0.0, 200]           # the raw series stay in the report
    text = soak.attribution_table(res)
    assert "scrape:series_total" in text and "dv_status_events_len{proc=status}" in text and "dv_status_clients" not in text.split("\n", 3)[3]
    assert "1 unchanged" in text and res.as_dict()["attribution"]["raw"]


def test_attribution_without_rows_is_empty_and_attr_rows_survive_the_file():
    assert soak.attribution(series()) == {}
    assert "no series recorded" in soak.attribution_table(verdict(series()))
    rows, bad = soak.parse_jsonl("\n".join(soak.dump_line(r) for r in attr_rows(minutes=1)))
    assert bad == 0 and len(rows) == 2 and rows[0]["kind"] == "attr"


def test_attributed_series_are_selected_by_the_name_constants():
    text = ('dv_status_events_len{proc="status"} 5\ndv_status_inputs_total{kind="x"} 9\ndv_pkt_seen_entries{proc="data"} 4\n'
            'dv_status_process_up{kind="a"} 1\ndv_edge_webrtc_room_listeners{room="a"} 50\ndv_codec_dropped_total 1\ndv_web_sse_streams 3\n'
            'dv_launcher_children_unlabelled 0\n')
    values, truncated = soak.attr_values(metrics.parse(text))
    assert values == {"dv_status_events_len{proc=status}": 5.0, "dv_pkt_seen_entries{proc=data}": 4.0,
                      "dv_edge_webrtc_room_listeners{room=a}": 50.0, "dv_launcher_children_unlabelled": 0.0} and truncated == 0
    values, truncated = soak.attr_values(metrics.parse(text), limit=2)
    assert len(values) == 2 and truncated == 2
    assert soak.attr_wanted("dv_foo_bar", include=(r"^dv_foo_",)) and not soak.attr_wanted("dv_foo_total", include=(r"^dv_foo_",))


def test_a_tick_that_ends_after_the_measured_end_snapshot_is_not_judged_as_measured():
    rows = series()
    rows.append(proc("dv-edge-webrtc", T0 + 3600 + 9, "measured", rss=5000, threads=2, fds=3))     # started before the end, read after the listeners left
    res = verdict(rows)
    assert res.ok, failed(res)
    assert res.children["dv-edge-webrtc"]["rss_kib"]["slope_kib_per_min"] == 0.0
    fixed = soak.assign_phases(rows + counter_rows())
    assert [r["phase"] for r in fixed if r.get("kind") == "proc" and r["t"] == T0 + 3609] == ["stopping"]
    late_warm = series()
    late_warm.append(proc("dv-edge-webrtc", T0 + 610, "warmup"))                                     # read after the warmup_end snapshot
    assert [r["phase"] for r in soak.assign_phases(late_warm + counter_rows()) if r.get("kind") == "proc" and r["t"] == T0 + 610] == ["measured"]
    assert soak.assign_phases(late_warm) == late_warm                                                # no snapshots: unchanged


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
