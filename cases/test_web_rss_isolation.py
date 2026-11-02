# SPDX-License-Identifier: MIT
"""Web unit: the RSS isolation experiment (docs/web-isolation-cases.md). DV-Router asked which activity drives the linear RSS
growth of dv-router (the launcher) and dv-svc-status seen in SOAK1, so this case runs ONE activity per rig run with the SOAK1
machinery (sampler, traffic lanes, the WebRTC listener generator with join/leave churn, proc-stat samples with the
smaps_rollup keys, the hub scrape) and writes DATA. It judges nothing except that the activity ran.

Skipped unless DVT_ISOLATION=<mode> is set (A to F, or idle, webrtc-churn, voice, proto-churn, webrtc-voice, voice-x3):

  A idle          all children up, no clients at all
  B webrtc-churn  100 stable WebRTC listeners + about 5 joins/leaves a minute, no voice
  C voice         the protocol lanes with overs as in SOAK1 (about 10 s per module per minute), no WebRTC listeners
  D proto-churn   protocol clients log in / out every few seconds (all lanes), no voice
  E webrtc-voice  100 stable WebRTC listeners, no churn, voice in one room (one protocol source: USRP into room A)
  F voice-x3      the lane mix of C with 3x as many overs: three per lane and minute (one per 20 s slot), no WebRTC listeners

5 minutes warmup + 10 minutes measured (DVT_ISOLATION_SHORT=1: 2 + 3 minutes, a smoke run). Needs `scripts/run.sh --profile
web,webload`, native launch and DVT_USRP_DYNAMIC=1 (as SOAK1), no --capture. Output: out/<run>/web/isolation-<letter>.json
(per child Rss / Anon / Private_Dirty / Pss_Anon and VmRSS first, last and slope per minute over the measured part, the focus
children dv-router and dv-svc-status, the series count of the scrape, the activity checks; `hub_events` / `events_note`: the events of the
public stream per minute from the start of warmup, the estimate events/min x 1.16 KiB next to the dv-svc-status slope; `streams`: overs
delivered, streams per minute and KiB/min per stream of dv-router, dv-svc-status and dv-room: dvtest/events.py), the raw samples
out/<run>/web/isolation-<letter>-samples.jsonl, and a stop file out/<run>/web/isolation.stop. `python3 -m dvtest.isolation
out/<run>...` prints one table across the runs.

Environment (all optional): DVT_ISOLATION_SHORT, DVT_ISOLATION_WARMUP_S (300), DVT_ISOLATION_MEASURE_S (600),
DVT_ISOLATION_SAMPLE_S (30), DVT_ISOLATION_SEED (DVT_SOAK_SEED, 1), DVT_ISOLATION_E_LANE (usrp-a), and the lane modules of
SOAK1 (DVT_SOAK_MODULES). The pure parts are dvtest/isolation.py.
"""
import os
import time

import pytest

import soak_support as ss
import test_web_soak as tws
from dvtest import isolation as iso
from dvtest import soak, web
from dvtest import webload as wl
from test_web_load import load_accounts, load_gen, load_ready, loadavg  # noqa: F401  (fixtures of the stage 5 file)

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.slow]

WORKERS = 2
ROOMS_HEARD_MIN_PACKETS = 50
SAMPLE_PERIOD_S = None              # None: iso.timing(env) (the offline rehearsal shortens it)


@pytest.fixture(scope="module")
def isolation_mode():
    raw = os.environ.get("DVT_ISOLATION", "").strip()
    if not raw:
        pytest.skip("RSS isolation takes 15 minutes a mode: run with DVT_ISOLATION=A..F (docs/web-isolation-cases.md)")
    try:
        return iso.resolve(raw)
    except ValueError as exc:
        pytest.fail(str(exc))


def make_events_client(base_url, ca_file):
    """The client of the event reader (no login: the public stream); a function of its own so that the rehearsal can replace it."""
    return web.WebClient(base_url, ca_file)


@pytest.fixture
def gen_access(request):
    """gen_access() -> (load generator, accounts function): fetched only by the modes that hold WebRTC listeners, so the
    idle, voice and proto-churn modes do not need the generator service."""
    def get():
        return request.getfixturevalue("load_gen"), request.getfixturevalue("load_accounts")
    return get


def test_rss_isolation(isolation_mode, gen_access, load_ready, rig_control, target, web_base_url, web_ca):  # noqa: F811
    mode = isolation_mode
    env = os.environ
    tm = iso.timing(env)
    warmup_s, measured_s = tm["warmup_s"], tm["measured_s"]
    sample_s = SAMPLE_PERIOD_S or tm["sample_s"]
    run_s = warmup_s + measured_s
    seed = env.get("DVT_ISOLATION_SEED") or env.get("DVT_SOAK_SEED", "1")
    e_lane = iso.e_lane_of(env)
    out = tws.out_dir()
    name = "isolation-%s" % mode.letter
    report = tws.Report(out / (name + ".json"))
    run_id = os.path.basename(os.path.dirname(os.path.abspath(str(out))))            # out/<run>/web
    report.data = {"schema": 1, "case": "ISOLATION", "mode": mode.letter, "name": mode.name, "activity": mode.text, "state": "starting",
                   "run": run_id}
    stop_file = out / "isolation.stop"
    stop_file.unlink(missing_ok=True)
    (out / ".isolation-touch").write_text("")
    t_wall = time.monotonic()
    cap = t_wall + run_s + iso.WALL_MARGIN_S

    def should_stop():
        return stop_file.exists() or time.monotonic() > cap

    if tm["smoke"]:
        web.finding("ISOLATION %s: a shortened run (warmup %g s, measured %g s, a sample every %g s) is a smoke run, not the experiment"
                    % (mode.letter, warmup_s, measured_s, sample_s))
    names, unlisted = ss.discover(rig_control)
    if unlisted:
        web.finding("ISOLATION: children of the launcher that are not in the proc-stat allow-list (not sampled): %s" % unlisted)

    lanes, skipped, dropped, modules = {}, {}, {}, {}
    if mode.needs_lanes:
        modules = ss.parse_modules(env.get("DVT_SOAK_MODULES", ""), env.get("DVT_NXDN_MODULE"), env.get("DVT_P25_MODULE"))
        every, skipped = ss.make_lanes(target, env, modules)
        wanted = iso.lanes_for(mode, list(every), e_lane)
        lanes = {n: every[n] for n in wanted}
        need = [e_lane] if mode.voice == "one" else list(ss.REQUIRED_LANES)
        for lane in need:
            if lane not in lanes:
                pytest.fail("required lane %s is not available: %s" % (lane, skipped.get(lane, "?")))
        dropped = tws.probe_lanes(lanes, report, "cycle" if mode.proto_churn else "over")      # mode D: no voice, not even the probe
    plan = iso.build_plan(mode, seed, run_s, list(lanes), e_lane)
    problems = soak.validate_sessions(plan["sessions"]) if plan["sessions"] else []
    assert not problems, "the churn plan breaks the budgets: %s" % problems
    plan_sum = iso.summarize_plan(plan)
    report.update(children=names, children_unsampled=unlisted, seed=seed, timing=tm, plan=plan_sum, lanes_skipped=skipped,
                  lanes_dropped=dropped, sample_s=sample_s)

    sink = ss.SampleFile(str(out / (name + "-samples.jsonl")))
    sampler = ss.ChildSampler(rig_control, names, sink, loadgen=None, period_s=sample_s)
    traffic = None
    gen = None
    reader = ss.EventsReader(lambda: make_events_client(web_base_url, web_ca))          # the public event stream, counted from the start of warmup
    generator_done = False
    gen_rows = []
    state = "setup"
    try:
        # the activity
        sink.write(soak.mark_row(time.time(), "warmup", "mode %s %s" % (mode.letter, mode.name)))
        if mode.needs_generator:
            gen, accounts = gen_access()
            creds = accounts(wl.accounts_needed(mode.listeners))
            sampler.gen = gen
            gen.run({"listeners": mode.listeners, "workers": WORKERS, "accounts": creds, "base_url": web_base_url,
                     "ca": "/webout/" + os.path.basename(web_ca), "plan": {}, "soak": {"sessions": plan["sessions"]}})
        t0 = time.monotonic() + 2.0                                        # the generator starts its clock 2 s after /run
        t_epoch0 = time.time() + 2.0
        items, method = iso.schedule_of(plan)
        if items:
            traffic = ss.Traffic(lanes, items, t0, seed, method=method)
            traffic.start()
        sink.write(soak.mark_row(time.time(), "warmup", "activity start"))
        sampler.phase = "warmup"
        reader.start()
        sampler.start()
        state = "warmup"
        report.update(state="warmup", traffic_start_epoch=round(t_epoch0, 1), loadavg_start=loadavg())

        # warmup and measured; the sampler thread takes the samples
        for label, until in (("warmup_end", t0 + warmup_s), ("measured_end", t0 + run_s)):
            next_note = time.monotonic() + 300.0
            while time.monotonic() < until and not should_stop():
                time.sleep(1.0)
                if time.monotonic() >= next_note:
                    next_note += 300.0
                    if gen is not None:
                        st = gen.status()
                        if st.get("state") == "error":
                            pytest.fail("the load generator failed: %s" % st.get("error"))
                    report.update(state=state, elapsed_s=round(time.monotonic() - t_wall),
                                  traffic=traffic.summary() if traffic else {}, sampling_errors=len(sampler.errors))
            if should_stop():
                break
            sampler.counters(label)                                       # the snapshots also fix the phase boundaries (soak.assign_phases)
            if label == "warmup_end":
                sampler.phase = "measured"
                state = "measured"
        if should_stop():
            reason = "stop file" if stop_file.exists() else "wall clock cap of %d s" % (run_s + iso.WALL_MARGIN_S)
            sink.write(soak.mark_row(time.time(), sampler.phase, "stopped early: " + reason))
            report.update(state="stopped early", stopped=reason)
            pytest.fail("ISOLATION %s stopped early (%s); the activity is torn down, no data" % (mode.letter, reason))

        # stop sampling and the event reader at the end of the measured part, then end the activity
        reader.stop()
        state = "stopping"
        sampler.phase = "stopping"
        sampler.halt()
        report.update(state="stopping")
        overs_ended = traffic.finish(90.0) if traffic else True
        if gen is not None:
            gen_rows = gen.finish(timeout=180.0)["rows"]
            generator_done = True
        sink.write(soak.mark_row(time.time(), "stopping", "activity ended"))
        sink.close()

        # the data and the one judgement
        rows, bad = soak.parse_jsonl((out / (name + "-samples.jsonl")).read_text(encoding="utf-8"))
        gsum = soak.generator_summary(gen_rows, min_packets=ROOMS_HEARD_MIN_PACKETS)[0] if gen is not None else None
        tsum = traffic.summary() if traffic else {}
        summary = iso.summarize(rows)
        hub_events, streams = ss.record_events_and_streams(reader, sampler, traffic, modules, t_epoch0 + warmup_s, t_epoch0 + run_s,
                                                           summary["children"], method)
        checks = iso.activity_checks(mode, plan_sum, summary, tsum, gsum)
        checks.append(soak.Check("activity:sampling", bad == 0 and len(sampler.errors) <= 3,
                                 "%d damaged lines, %d sampling errors %s" % (bad, len(sampler.errors), sampler.errors[:2])))
        checks.append(soak.Check("activity:ended", overs_ended, "the overs / cycles in flight ended: %s" % overs_ended))
        extra = {"state": "done", "hub_events": hub_events, "events_note": hub_events.get("events_note"), "streams": streams, "traffic": tsum, "generator": gsum, "sampling_errors": sampler.errors[:10], "slow_ticks": sampler.slow_ticks,
                 "loadavg_end": loadavg(), "wall_s": round(time.monotonic() - t_wall), "children_unsampled": unlisted,
                 "lanes_skipped": skipped, "lanes_dropped": dropped, "seed": seed, "run": run_id}
        doc = iso.assemble(mode, tm, plan_sum, rows, checks, extra)
        report.data = {}
        report.update(**doc)
    except BaseException as exc:
        if not str(report.data.get("state", "")).startswith("stopped"):
            report.update(state="failed: %s: %s" % (type(exc).__name__, str(exc)[:200]), at=state, wall_s=round(time.monotonic() - t_wall))
        raise
    finally:
        # clean teardown in every case: stop sampling, traffic and listeners; leave nothing connected
        sampler.stop.set()
        reader.stop()
        if traffic is not None:
            traffic.finish(60.0)
        if gen is not None and not generator_done:
            try:
                gen.abort()
            except Exception as exc:                                    # noqa: BLE001 - the abort is best effort
                web.finding("ISOLATION: abort of the load generator: %s" % str(exc)[:120])
        try:
            sink.close()
        except Exception:                                               # noqa: BLE001
            pass

    print("\nISOLATION %s (%s): %s" % (mode.letter, mode.name, mode.text))
    print("\n".join(doc["children_table"]))
    print("series_total: %s" % doc["series_total"])
    print("hub events: %s" % doc["events_note"])
    print("streams: %s in the measured part, %s a minute" % (streams.get("streams_total"), streams.get("streams_per_min")))
    for c in checks:
        print("%-4s %-34s %s" % ("ok" if c.ok else "FAIL", c.name, c.detail))
    for note, why in {**skipped, **dropped}.items():
        web.finding("ISOLATION: lane %s is not in the activity: %s" % (note, why))
    failed = [c for c in checks if not c.ok]
    assert not failed, "ISOLATION %s: the activity did not run as planned: %s" % (mode.letter, "; ".join("%s (%s)" % (c.name, c.detail) for c in failed[:6]))
