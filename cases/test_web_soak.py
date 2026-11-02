# SPDX-License-Identifier: MIT
"""Web unit: SOAK1, a 60 minute soak of DV-Router with WebRTC listeners and protocol clients at once
(docs/web-soak-cases.md). Needs `scripts/run.sh --profile web,webload` (optionally interlink; NXDN and P25 come from soak-edges.toml), native
launch, DVT_USRP_DYNAMIC=1, and DVT_SOAK=1 (the case skips without it: it takes about 70 minutes).

Mix (DV-Router spec): 100 receive-only WebRTC listeners from the stage 5 generator with slow churn (about 5 leave
and rejoin a minute, inside the join budget of 1 a second per address, 8 circuits per login and the per-login
renewal bucket), plus protocol clients on one module each (DExtra talker with DPlus and DCS listeners, MMDVM DMR,
YSF, M17, USRP into rooms A and B which the WebRTC listeners hear, NXDN and P25 when the rig serves them, and the
XLX interlink peer as one more listener when profile interlink is up) with one over of about 10 s per module
every minute and every fifth minute all at once. ACL on (the launcher default). 10 minutes warmup, 50 minutes
measured, then all traffic stops, 170 s settle (every link of a departed client times out) and the SUT idles for 2 minutes.

Every 30 s the case reads RSS, threads, open fds and the start time of EVERY child of the launcher through the
read-only `proc-stat` control action and keeps the raw samples in out/<run>/web/soak-samples.jsonl. The verdict is
dvtest.soak.evaluate (restarts, RSS slope, threads and fds as the median of the last 10 minutes against the median of
the first 10 minutes after warmup, idle fds, error counters, the idle public event stream, the idle gauge; smaps_rollup
values are recorded when the proc-stat reply carries them); `python3 -m dvtest.soak out/<run>` judges a finished run again; the table per child is printed and written to out/<run>/web/soak.json. The run ends by itself
after 75 minutes at the latest (hard wall clock cap) and can be stopped with `touch out/<run>/web/soak.stop`;
the teardown is the same in both cases, and in a failure.

Environment (all optional): DVT_SOAK_WARMUP_S (600), DVT_SOAK_MEASURE_S (3000; DVT_SOAK_MEASURED_S is the same),
DVT_SOAK_SAMPLE_S (30), DVT_SOAK_SHORT=1 (the validation run: 180 s warmup + 240 s measured, a sample every 15 s, the
same mix and criteria, the RSS growth bound scaled to the shorter part: dvtest.soak.timing), DVT_SOAK_SEED (1),
DVT_SOAK_MODULES ("dstar=D,dmr=E,ysf=C,m17=J,usrp-a=A,usrp-b=B,nxdn=F,p25=G"). Shorter times are for validation only
and say so; the documented run uses the defaults. NXDN and P25 need rig/config/dv-router/soak-edges.toml in
DVT_SUT_CONFIG_EXTRA (docs/web-soak-cases.md).
"""
import json
import os
import time
from pathlib import Path

import pytest

import soak_support as ss
from dvtest import k22, soak, web
from dvtest import webload as wl
from test_web_load import load_accounts, load_gen, load_ready, loadavg  # noqa: F401  (fixtures of the stage 5 file)

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.slow]

SEATS = soak.SEATS
WORKERS = 2
ROOMS_HEARD_MIN_PACKETS = 50
IDLE0_TICKS = 3                     # samples of the quiet SUT before any traffic, IDLE0_GAP_S apart
IDLE0_GAP_S = 10.0
SETTLE_S = 170.0                    # after the last client left, before the idle window: longest link timeout (imrs 150 s) + sweep tick; soak.LINK_TIMEOUTS_S
GAUGE_BUDGET_S = 150.0              # for the listener gauge to reach zero after the generator finished
SAMPLE_PERIOD_S = None              # None: dvtest.soak.timing(env) (the offline rehearsal shortens it)


@pytest.fixture(scope="module")
def soak_on():
    if os.environ.get("DVT_SOAK") != "1":
        pytest.skip("SOAK1 takes about 70 minutes: run with DVT_SOAK=1 (docs/web-soak-cases.md)")
    return True


def out_dir():
    path = Path(os.environ.get("DVT_OUT", "out")) / "web"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Report:
    """soak.json: rewritten at every milestone, so an aborted run leaves its state behind."""

    def __init__(self, path):
        self.path = path
        self.data = {"schema": 1, "case": "SOAK1", "state": "starting"}

    def update(self, **kw):
        self.data.update(kw)
        try:
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
        except OSError as exc:
            web.finding("could not write %s: %s" % (self.path, exc))


def gauge_now(ctl):
    from dvtest import metrics
    return round(metrics.total(metrics.parse(ctl.metrics("xlxd-sut")), ss.LISTENERS))


def wait_gauge_zero(ctl, budget_s, stop):
    end = time.monotonic() + budget_s
    seen = []
    while time.monotonic() < end and not stop():
        seen.append(gauge_now(ctl))
        if seen[-1] == 0:
            return True, seen[-6:]
        time.sleep(5.0)
    return False, seen[-6:]


def idle_events(client, seconds):
    """Frames of GET /api/v1/public/events for `seconds`, resumed every 30 s with the last event id (as the K22
    idle case does); the verdict of dvtest.k22.idle_stream_verdict and the event names seen."""
    frames_at, last_id = [], None
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        left = seconds - (time.monotonic() - t0)
        path = "/api/v1/public/events" + ("?last_event_id=%s" % last_id if last_id else "")
        start = time.monotonic() - t0
        _s, _c, frames = client.sse(path, max_events=100000, timeout=min(30.0, left))
        for fr in frames:
            frames_at.append((start, fr))
            if fr.get("id"):
                last_id = fr["id"]
    times = k22.event_times(frames_at)
    verdict = k22.idle_stream_verdict(times, seconds)
    verdict["names"] = sorted({fr.get("event") for _t, fr in frames_at if fr.get("event") not in (None, "hello")})
    verdict["events"] = len(times)
    return verdict


def probe_lanes(lanes, report, method="over"):
    """One short over per lane before the clock starts: a required lane that does not work fails the case at once
    (an hour must not be spent on a broken mix); NXDN and P25 are dropped with the reason. `method` "cycle" probes with a
    login without voice (the RSS isolation mode D)."""
    dropped = {}
    for name, lane in list(lanes.items()):
        res = getattr(lane, method)(2.0, "probe")
        if res["ok"] or (name.startswith("usrp") and res["sent"]):
            continue
        if name in ss.REQUIRED_LANES:
            pytest.fail("lane %s does not work before the soak starts: %s (heard %s)" % (name, res["error"], res["heard"]))
        hint = " (is rig/config/dv-router/soak-edges.toml in DVT_SUT_CONFIG_EXTRA?)" if name in ("nxdn", "p25") else ""
        dropped[name] = "probe over failed: %s (heard %s)%s" % (res["error"], res["heard"], hint)
        del lanes[name]
    report.update(lanes_dropped=dropped)
    return dropped


def test_soak_sixty_minutes(soak_on, load_gen, load_ready, rig_control, target, web_base_url, web_ca, web_client,  # noqa: F811
                            load_accounts):  # noqa: F811
    env = os.environ
    tm = soak.timing(env)
    warmup_s, measured_s, smoke = tm["warmup_s"], tm["measured_s"], tm["smoke"]
    sample_s = SAMPLE_PERIOD_S or tm["sample_s"]
    seed = env.get("DVT_SOAK_SEED", "1")
    out = out_dir()
    report = Report(out / "soak.json")
    stop_file = out / "soak.stop"
    stop_file.unlink(missing_ok=True)
    (out / ".soak-touch").write_text("")                                  # the disk takes a file before an hour starts
    t_wall = time.monotonic()
    cap = t_wall + soak.WALL_CAP_S

    def should_stop():
        return stop_file.exists() or time.monotonic() > cap

    if smoke:
        web.finding("SOAK1: a shortened run (warmup %d s, measured %d s, a sample every %g s, RSS bound x%.3f) is a validation, not the criterion"
                    % (warmup_s, measured_s, sample_s, tm["rss_scale"]))
    modules = ss.parse_modules(env.get("DVT_SOAK_MODULES", ""), env.get("DVT_NXDN_MODULE"), env.get("DVT_P25_MODULE"))
    names, unlisted = ss.discover(rig_control)
    report.update(children=names, children_unsampled=unlisted, seed=seed, warmup_s=warmup_s, measured_s=measured_s,
                  modules=modules, smoke=smoke, short=tm["short"], sample_s=sample_s, rss_scale=tm["rss_scale"],
                  wall_cap_s=soak.WALL_CAP_S, settle_s=SETTLE_S, median_window_s=tm["median_window_s"])
    if unlisted:
        web.finding("SOAK1: children of the launcher that are not in the proc-stat allow-list (not sampled): %s" % unlisted)
    lanes, skipped = ss.make_lanes(target, env, modules)
    report.update(lanes_skipped=skipped)
    for name in ss.REQUIRED_LANES:
        if name not in lanes:
            pytest.fail("required lane %s is not available: %s" % (name, skipped.get(name, "?")))
    dropped = probe_lanes(lanes, report)
    run_s = warmup_s + measured_s
    sessions = soak.build_sessions(seed, run_s, seats=SEATS)
    overs = soak.build_overs(seed, [n for n in modules if n in lanes], run_s)
    problems = soak.validate_sessions(sessions)
    assert not problems, "the churn plan breaks the budgets: %s" % problems
    report.update(plan=soak.summarize_plan(overs, sessions))

    sink = ss.SampleFile(str(out / "soak-samples.jsonl"))
    sampler = ss.ChildSampler(rig_control, names, sink, loadgen=load_gen, period_s=sample_s)
    traffic = None
    generator_done = False
    result = None
    extra = []
    gen_rows = []
    state = "setup"
    try:
        # idle0: the quiet SUT before any traffic (the fd baseline of the idle check)
        report.update(state="idle0")
        sampler.phase = "idle0"
        sink.write(soak.mark_row(time.time(), "idle0", "baseline"))
        for _ in range(IDLE0_TICKS):
            sampler.tick("idle0")
            time.sleep(IDLE0_GAP_S)
        sampler.counters("idle0")

        # traffic: generator (100 listeners with churn) and the protocol mix
        creds = load_accounts(wl.accounts_needed(SEATS))
        load_gen.run({"listeners": SEATS, "workers": WORKERS, "accounts": creds, "base_url": web_base_url,
                      "ca": "/webout/" + os.path.basename(web_ca), "plan": {}, "soak": {"sessions": sessions}})
        t0 = time.monotonic() + 2.0                                        # the generator starts its clock 2 s after /run
        t_traffic = time.time() + 2.0
        traffic = ss.Traffic(lanes, overs, t0, seed)
        traffic.start()
        sink.write(soak.mark_row(time.time(), "warmup", "traffic start"))
        sampler.phase = "warmup"
        sampler.start()
        state = "warmup"
        report.update(state="warmup", traffic_start_epoch=round(t_traffic, 1), loadavg_start=loadavg())

        # warmup and measured; the sampler thread takes the samples
        marks = [("warmup_end", t0 + warmup_s), ("measured_end", t0 + run_s)]
        for label, until in marks:
            next_note = time.monotonic() + 300.0
            while time.monotonic() < until and not should_stop():
                time.sleep(1.0)
                if time.monotonic() >= next_note:
                    next_note += 300.0
                    st = load_gen.status()
                    if st.get("state") == "error":
                        pytest.fail("the load generator failed: %s" % st.get("error"))
                    report.update(state=state, elapsed_s=round(time.monotonic() - t_wall), generator=st,
                                  traffic=traffic.summary(), sampling_errors=len(sampler.errors))
            if should_stop():
                break
            sampler.counters(label)
            if label == "warmup_end":
                sampler.phase = "measured"
                state = "measured"
        if should_stop():
            reason = "stop file" if stop_file.exists() else "wall clock cap of %d s" % soak.WALL_CAP_S
            sink.write(soak.mark_row(time.time(), sampler.phase, "stopped early: " + reason))
            report.update(state="stopped early", stopped=reason)
            pytest.fail("SOAK1 stopped early (%s); the traffic is torn down, no verdict" % reason)

        # stop everything: the protocol mix, then the listeners
        state = "stopping"
        sampler.phase = "stopping"
        report.update(state="stopping")
        overs_ended = traffic.finish(90.0)
        gen = load_gen.finish(timeout=180.0)
        generator_done = True
        gen_rows = gen["rows"]
        zero, gauge_seen = wait_gauge_zero(rig_control, GAUGE_BUDGET_S, should_stop)
        sink.write(soak.mark_row(time.time(), "stopping", "listeners zero: %s %s" % (zero, gauge_seen)))
        time.sleep(SETTLE_S)                                                # let rooms, leases and cleanups settle

        # idle: nothing connected, nothing talking
        state = "idle"
        sampler.phase = "idle"
        sampler.counters("idle_start")
        sink.write(soak.mark_row(time.time(), "idle", "start"))
        events = idle_events(web_client(), soak.IDLE_S)
        sampler.halt()
        sampler.tick("idle")                                                # the last sample, after the quiet minutes
        sampler.counters("idle_end")
        sink.write(soak.mark_row(time.time(), "idle", "end"))
        sink.close()

        # the verdict
        text = (out / "soak-samples.jsonl").read_text(encoding="utf-8")
        rows, bad = soak.parse_jsonl(text)
        # a short run is a validation: its RSS slopes are leads (warmup), not a verdict; the real run judges them
        result = soak.evaluate(rows, events=events, expected=names,
                               params={"rss_scale": tm["rss_scale"], "rss_judged": not tm["short"], "measured_s": measured_s,
                                       "median_window_s": tm["median_window_s"]})
        gsum, gchecks = soak.generator_summary(gen_rows, min_packets=ROOMS_HEARD_MIN_PACKETS)
        tsum = traffic.summary()
        # the lanes that stayed in the mix (the required ones, and NXDN / P25 when their probe over worked) must deliver
        judged = [n for n in list(ss.REQUIRED_LANES) + list(ss.OPTIONAL_LANES) if n in lanes and not n.startswith("usrp")]
        extra = list(gchecks) + ss.lane_checks({k: v for k, v in tsum.items() if k not in ("usrp-a", "usrp-b")}, required=judged)
        extra.append(soak.Check("sampling", bad == 0 and len(sampler.errors) <= 3,
                                "%d damaged lines, %d sampling errors %s" % (bad, len(sampler.errors), sampler.errors[:2])))
        extra.append(soak.Check("drained", zero and overs_ended, "listeners zero %s (%s), overs ended %s" % (zero, gauge_seen, overs_ended)))
        report.update(state="done", result=result.as_dict(), events=events, generator=gsum, traffic=tsum,
                      extra_checks=[{"name": c.name, "ok": c.ok, "detail": c.detail} for c in extra],
                      table=soak.table(result).splitlines(), counters_table=soak.counters_table(result).splitlines(),
                      attribution_table=soak.attribution_table(result).splitlines(), smaps_table=soak.smaps_table(result).splitlines(), warnings=result.warnings,
                      gauge_families={r["phase"]: r["gauges"] for r in rows if r.get("kind") == "metrics" and "gauges" in r},
                      metric_families=next((r["families"] for r in rows if r.get("kind") == "metrics" and "families" in r), []), samples=len(rows), sampling_errors=sampler.errors[:10],
                      slow_ticks=sampler.slow_ticks, loadavg_end=loadavg(), wall_s=round(time.monotonic() - t_wall))
    except BaseException as exc:
        if str(report.data.get("state", "")).startswith("stopped"):
            report.update(wall_s=round(time.monotonic() - t_wall))
        else:
            report.update(state="failed: %s: %s" % (type(exc).__name__, str(exc)[:200]), at=state, wall_s=round(time.monotonic() - t_wall))
        raise
    finally:
        # clean teardown in every case: stop sampling, traffic and listeners; leave nothing connected
        sampler.stop.set()
        if traffic is not None:
            traffic.finish(60.0)
        if not generator_done:
            try:
                load_gen.abort()
            except Exception as exc:                                    # noqa: BLE001 - the abort is best effort
                web.finding("SOAK1: abort of the load generator: %s" % str(exc)[:120])
        try:
            sink.close()
        except Exception:                                               # noqa: BLE001
            pass

    print("\n" + soak.table(result))
    print("\n" + soak.counters_table(result))
    print("\n" + soak.attribution_table(result))
    print("\n" + soak.smaps_table(result))
    for note in result.warnings:
        print("WARN %s" % note)
    for c in list(result.checks) + extra:
        print("%-4s %-34s %s" % ("ok" if c.ok else "FAIL", c.name, c.detail))
    for note in result.unmeasured:
        web.finding("SOAK1: not measured: %s" % note)
    for note in result.warnings:
        web.finding("SOAK1: %s" % note)
    for name, why in {**skipped, **dropped}.items():
        web.finding("SOAK1: lane %s is not in the mix: %s" % (name, why))
    failed = [c for c in list(result.checks) + extra if not c.ok]
    assert not failed, "SOAK1 failed: %s" % "; ".join("%s (%s)" % (c.name, c.detail) for c in failed[:6])

