# SPDX-License-Identifier: MIT
"""DV-Router DP18 cases against the PRIVATE aprsc (profile aprs): the node beacon and the node objects the data worker originates to APRS-IS
(data.md 9.5 "Built (DP18)", config.md `[edges.aprsis.beacon]` / `[[edges.aprsis.objects]]`, review 232). The expected lines are built from
the RENDERED config with dvtest/proto/aprs.py (hand-written APRS101 shapes), never from DV-Router's encoder. Case table, run commands, limits:
docs/dp18-cases.md. Pure helpers and their tests: cases/dp18_support.py, cases/unit/test_dp18_support.py.

  DP18-a  the beacon and the object arrive at the aprsc as the gateway call's own reports: path before the server's q construct exactly
          `TCPIP*` (no client q construct), the server's `qAC`, destination `tocall`, the information field byte for byte; the first line
          comes one full interval after the node started
  DP18-b  the object comes 35 s after the beacon (the stagger: interval + 35 s per earlier configured report)
  DP18-c  the object's HMS timestamp equals the send time within 1 s
  DP18-n  dv_pkt_origin_total{result=generated} grows by one per report and kind; nothing is withheld, no_edge or no_rf_edge
  DP18-e  (run shape dp18-origin-withheld.toml) a position policy that withholds the gateway call: no line at the aprsc for the first
          interval plus the stagger, dv_pkt_origin_total{result=withheld} grows per kind, generated stays 0
  DP18-k  (LAST: it waits one more interval) after a dv-data-worker kill nothing arrives before one full interval, then the beacon does

Skips with the reason on an image without DP18 (the served configuration schema does not know `simulate`, `beacon` and `objects`: a
capability probe, not an image tag), without the private aprsc, or in the wrong run shape. NOT RUN ON A RIG (no image with DP18 yet).
Observations: stdout and out/<run>/dp18-origin.txt ("RECORD", "FINDING").
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprsis_client as ai, metrics, txguard  # noqa: E402

import data_support as ds  # noqa: E402
import dp18_support as dc  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
# the private aprsc (and the `shape` it reads) of the first DP7 set, reused by name; `hub` is the fixture of THIS module
from test_dp15_positions import aprsc, shape  # noqa: E402,F401

pytestmark = pytest.mark.rig

LOG = "dp18-origin.txt"
SETTING = "dp18"
LOGIN_SLACK_S = 120.0                 # how long after the last expected line a report is waited for
COUNTER_WAIT_S = 40.0
KILL_MARGIN_S = 15.0
RESTART_WAIT_S = 180.0                # the restarted worker's first beacon is awaited this long after the quiet window


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


def report_key(report):
    return "beacon" if report.kind == "beacon" else "object:" + report.name


def counts_now(hub):
    return dc.origin_counts(metrics.parse(hub.text()))


@pytest.fixture(scope="module")
def ctx(request, target, hub, admin_session):
    """The run shape, the DP18 capability probe and the private aprsc; skips with the reason otherwise."""
    shp = dc.origin_shape(p1.read_config(target))
    if shp is None:
        pytest.skip("the rendered config has no enabled [edges.aprsis] with a beacon or an object: add rig/config/dv-router/dp15-aprsis.toml "
                    "and dp18-origin.toml (docs/dp18-cases.md)")
    admin = admin_session("operator")
    schema = admin.get(admin.path("/schema"), expect=None)
    if schema.status != 200 or not dc.schema_has_dp18(schema.text()):
        pytest.skip("the image does not know DP12c / DP18: the configuration schema has no `simulate`, `beacon` and `objects` (HTTP %d)" % schema.status)
    host = request.getfixturevalue("aprsc")
    base = counts_now(hub)
    note(target, "origin", "RECORD", "shape: gateway %s, tocall %s, reports %s, first due %s s, position policy withholds the gateway call: %s; aprsc %s; "
         "dv_pkt_origin_total at the start: %s" % (shp.gateway, shp.tocall, [report_key(r) for r in shp.reports],
                                                  [r.first_due_s for r in shp.reports], shp.withheld, host, base))
    return types.SimpleNamespace(shape=shp, host=host, admin=admin, base=base, interval_s=min(r.interval_s for r in shp.reports))


@pytest.fixture(scope="module")
def session(ctx):
    """The tester's login on the aprsc, held for the whole module (the lines come minutes apart)."""
    login = p1.aprsc_login(ctx.host)
    tester = login.__enter__()
    try:
        yield tester
    finally:
        login.__exit__(None, None, None)


def lines_of(session, shp, since=0):
    """[(wall epoch, report key, parsed)] of the node's own lines at the aprsc after the mark `since`."""
    out = []
    for at, line in session.snapshot()[since:]:
        parsed = ai.parse_line(line)
        key = dc.classify(parsed, shp) if parsed else None
        if key:
            out.append((dc.wall_of(at), key, parsed, at))
    return out


@pytest.fixture(scope="module")
def first_lines(ctx, session, hub, target):
    """The first line of every configured report, waited for (positive shapes only): {key: (wall epoch, parsed, monotonic)}."""
    shp = ctx.shape
    if shp.withheld:
        pytest.skip("the position policy withholds the gateway call in this run (dp18-origin-withheld.toml): see DP18-e")
    want = [report_key(r) for r in shp.reports]
    limit = max(r.first_due_s for r in shp.reports) + LOGIN_SLACK_S + ctx.interval_s
    end = time.monotonic() + limit
    seen = {}
    while time.monotonic() < end and len(seen) < len(want):
        for wall, key, parsed, at in lines_of(session, shp):
            seen.setdefault(key, (wall, parsed, at))
        time.sleep(1.0)
    uptime = txguard.uptime_reader(ctx.admin)()
    note(target, "origin", "RECORD", "first lines at the aprsc: %s; node uptime now %s s" % (
        {k: v[1]["raw"] for k, v in seen.items()}, uptime))
    if len(seen) < len(want):
        pytest.fail("no line from the node for %s within %.0f s (dv_pkt_origin_total now: %s)" % (
            [k for k in want if k not in seen], limit, {k: v - ctx.base.get(k, 0) for k, v in counts_now(hub).items()}))
    seen["_uptime_now"] = uptime
    return seen


# ================================================================ the cases

def test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, first_lines, target):
    """data.md 9.5: each report is `<gateway>>tocall,TCPIP*:<info>` with no client q construct; the server adds `qAC` (the login sent
    its own packet). The information field is the hand-built APRS101 line from the configured position, symbol, comment, ambiguity."""
    shp = ctx.shape
    for report in shp.reports:
        wall, parsed, _at = first_lines[report_key(report)]
        problems = dc.check_origin_line(parsed, report, shp)
        note(target, "DP18-a", "RECORD", "%s: %s; q construct after the server: %s" % (report_key(report), parsed["raw"], parsed["q"]))
        assert not problems, "%s: %s; line %s" % (report_key(report), "; ".join(problems), parsed["raw"])
    uptime_now = first_lines["_uptime_now"]
    if uptime_now is not None:
        first = min(first_lines[report_key(r)][2] for r in shp.reports)
        arrival_uptime = uptime_now - (time.monotonic() - first)
        note(target, "DP18-a", "RECORD", "node uptime at the first line: %.1f s (interval %d s)" % (arrival_uptime, ctx.interval_s))
        assert arrival_uptime >= ctx.interval_s - 1.0, ("the first report came %.1f s after the node started, before one full interval (%d s)" % (
            arrival_uptime, ctx.interval_s))


def test_dp18_b_the_object_comes_35_s_after_the_beacon(ctx, first_lines, target):
    """data.md 9.5: the first attempt of the k-th configured report follows one interval plus 35 s per earlier report."""
    shp = ctx.shape
    if shp.beacon is None or not shp.objects:
        pytest.skip("this run has no beacon plus object pair")
    t0 = first_lines["beacon"][2]
    for report in shp.objects:
        delta = first_lines[report_key(report)][2] - t0
        want = dc.STAGGER_S * report.index
        note(target, "DP18-b", "RECORD", "%s arrived %.2f s after the beacon (expected %.0f s)" % (report_key(report), delta, want))
        lo, hi = want - 1.0, want + (dc.ARRIVAL_WINDOW_S[1] - dc.STAGGER_S)
        assert lo <= delta <= hi, "%s came %.2f s after the beacon, expected %.0f s (window %.0f..%.0f)" % (report_key(report), delta, want, lo, hi)


def test_dp18_c_the_object_timestamp_is_the_send_time(ctx, first_lines, target):
    """data.md 9.5: a live object carries a UTC HMS timestamp (`HHMMSSh`) of the moment it was generated; read at the aprsc it equals the
    wall clock within 1 s (whole seconds, plus a half second of transit allowance)."""
    from dvtest.proto import aprs
    shp = ctx.shape
    if not shp.objects:
        pytest.skip("this run has no object")
    for report in shp.objects:
        wall, parsed, _at = first_lines[report_key(report)]
        hms = aprs.parse_object(parsed["info"])["hms"]
        off = dc.hms_offset_s(hms, wall)
        note(target, "DP18-c", "RECORD", "%s: stamp %sh, read %.2f s after it" % (report_key(report), hms, off))
        assert dc.hms_ok(hms, wall), "%s: the stamp %sh is %.2f s from the time the line was read (limit %.1f s)" % (
            report_key(report), hms, off, dc.HMS_TOLERANCE_S)


def test_dp18_n_origin_counters_count_generated_reports(ctx, first_lines, hub, target):
    """data.md 13.1: dv_pkt_origin_total{kind,result=generated} counts a due and encoded report; nothing is withheld, no_edge or no_rf_edge."""
    shp = ctx.shape
    want = {"beacon": 1 if shp.beacon else 0, "object": len(shp.objects)}
    end = time.monotonic() + COUNTER_WAIT_S
    while True:
        now = counts_now(hub)
        got = {k: now.get((k, "generated"), 0) - ctx.base.get((k, "generated"), 0) for k in want}
        if all(got[k] >= want[k] for k in want) or time.monotonic() >= end:
            break
        time.sleep(2.0)
    note(target, "DP18-n", "RECORD", "dv_pkt_origin_total deltas since the start: %s" % ({k: v - ctx.base.get(k, 0) for k, v in now.items()},))
    for kind in want:
        assert got[kind] >= want[kind], "dv_pkt_origin_total{kind=%s,result=generated} grew by %d, expected at least %d" % (kind, got[kind], want[kind])
    bad = {k: v - ctx.base.get(k, 0) for k, v in now.items() if k[1] in ("withheld", "no_edge", "no_rf_edge") and v - ctx.base.get(k, 0)}
    assert not bad, "reports were not generated for a reason: %s" % bad


def test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, session, hub, target):
    """data.md 9.5: the position policy (positions off, base-callsign opt-out, hidden account) applied to the gateway call withholds every
    report: no line at the aprsc, counted in dv_pkt_origin_total{result=withheld}, and the report waits for its next interval."""
    shp = ctx.shape
    if not shp.withheld:
        pytest.skip("this run does not withhold the gateway call: add dp18-origin-withheld.toml (position_optout) to the shape of DP18-a..n")
    mark = session.mark()
    window = max(r.first_due_s for r in shp.reports) + 60.0
    note(target, "DP18-e", "RECORD", "watching the aprsc for %.0f s (the last report is due at %.0f s)" % (window, max(r.first_due_s for r in shp.reports)))
    time.sleep(window)
    got = lines_of(session, shp, mark)
    assert not got, "the node sent a line although the position policy withholds %s: %s" % (shp.gateway, [p["raw"] for _w, _k, p, _a in got])
    want = {"beacon": 1 if shp.beacon else 0, "object": len(shp.objects)}
    end = time.monotonic() + COUNTER_WAIT_S
    while True:
        now = counts_now(hub)
        delta = {k: now.get((k, "withheld"), 0) - ctx.base.get((k, "withheld"), 0) for k in want}
        if all(delta[k] >= want[k] for k in want) or time.monotonic() >= end:
            break
        time.sleep(2.0)
    note(target, "DP18-e", "RECORD", "dv_pkt_origin_total deltas: %s" % ({k: v - ctx.base.get(k, 0) for k, v in now.items()},))
    for kind in want:
        assert delta[kind] >= want[kind], "dv_pkt_origin_total{kind=%s,result=withheld} grew by %d, expected at least %d" % (kind, delta[kind], want[kind])
    generated = {k: v - ctx.base.get(k, 0) for k, v in now.items() if k[1] == "generated" and v - ctx.base.get(k, 0)}
    assert not generated, "reports were generated although the gateway call is withheld: %s" % generated


def test_dp18_k_nothing_arrives_before_one_full_interval_after_a_worker_kill(ctx, first_lines, session, rig_control, target):
    """data.md 9.5: due reports are not replayed and the restarted data worker waits a full interval (floor safe). The first data worker
    (the only originator) is killed with the allowed kill-proc (the launcher respawns it); for one interval minus a margin nothing from
    the gateway call arrives, although the unrestarted schedule would send the beacon within one interval of its previous line. Then the
    beacon of the NEW worker is awaited. Run last: it takes about one more interval."""
    shp = ctx.shape
    interval = min(r.interval_s for r in shp.reports)
    mark = session.mark()
    result = rig_control.kill_proc("xlxd-sut", "dv-data-worker", "KILL")
    t_kill = result.t_kill_monotonic
    quiet = dc.quiet_window_s(interval, KILL_MARGIN_S)
    note(target, "DP18-k", "RECORD", "dv-data-worker killed (pids %s); watching %.0f s for silence, then up to %.0f s for the first report of the new worker" % (
        result.pids, quiet, RESTART_WAIT_S))
    time.sleep(max(0.0, t_kill + quiet - time.monotonic()))
    early = lines_of(session, shp, mark)
    early = [e for e in early if e[3] < t_kill + quiet]
    assert not early, "the node sent %d line(s) within %.0f s of the worker kill: %s" % (len(early), quiet, [p["raw"] for _w, _k, p, _a in early])
    end = time.monotonic() + (interval - quiet) + RESTART_WAIT_S
    first = None
    while time.monotonic() < end and first is None:
        got = lines_of(session, shp, mark)
        first = got[0] if got else None
        time.sleep(1.0)
    assert first is not None, "the restarted worker sent no report within %.0f s of the kill" % (interval + RESTART_WAIT_S)
    note(target, "DP18-k", "RECORD", "first report of the restarted worker: %s, %.1f s after the kill (interval %d s)" % (
        first[1], first[3] - t_kill, interval))
    assert first[3] - t_kill >= interval - KILL_MARGIN_S, "the first report came %.1f s after the kill, before one full interval" % (first[3] - t_kill)
