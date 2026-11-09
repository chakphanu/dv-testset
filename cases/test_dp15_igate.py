# SPDX-License-Identifier: MIT
"""DV-Router RF-to-APRS-IS igate cases of data.md 15.5 (DP16, data.md 8.9 and 9.3): the tester plays the KISS TNC rf-a
(dvtest/kiss_tnc.py, the AX.25 edge connects to it) and reads the PRIVATE aprsc (profile aprs) as an APRS-IS client. A frame heard
on rf-a travels `edge:ax25/rf-a -> edge:aprsis` over the transit path `dp15-igate` (rig/config/dv-router/dp15-rf-igate.toml).
Specification and case table: docs/dp15-cases.md "RF to APRS-IS igate (group 8)".

  DP15-8w  an RF-heard positionless weather frame (`_`) arrives at the aprsc exactly once with `,qAR,<igate call>` (`qAO` in the
           receive-only shape), the path as heard, the information field byte-identical
  DP15-8t  the same for a telemetry frame (`T#...`)
  DP15-8q  an RF-heard general query `?APRS?` is never gated
  DP15-8n  a frame whose path contains NOGATE is never gated
  DP15-8r  a frame whose path contains RFONLY is never gated

The igate call is the `callsign` of [edges.ax25] (N0TEST-1 in dp15-rf.toml), NOT the APRS-IS login (N0TEST-5): the worker appends
`rf_callsign` (config_client.rs, igate.rs). aprsc keeps a q construct that a client sends when the packet is not originated by the
client and the port is not `clientonly` (parse_qc.c); the case still asserts only that the q construct STARTS with qAR and the igate
call and records everything the server put after it.

Negative cases: each sends the frame that must not be gated, then a positive control (a weather frame of another source) and watches
the aprsc for `max(ABSENT_MIN_S, ABSENT_FACTOR x the control's latency, ...)` after the control arrived, so the window is measured on
the gate of THIS run and the control proves the path was alive (a case without a control would pass on a broken rig). aprsc itself
drops a packet with NOGATE or RFONLY in its path (incoming.c, INERR_NOGATE), so for 8n and 8r the absence at the aprsc is not evidence
about DV-Router; the evidence is the path counter `dv_pkt_path_total{route=<dp15-igate>,result=no_gate}` (filtered for the query),
which is asserted when the family has a series for the route.

Positive cases and "exactly once": aprsc drops an identical packet (source, destination, information) within 30 s, so a second send
by DV-Router would not be visible at the aprsc; the counter `dv_pkt_path_total{result=forwarded}` +1 per frame (asserted) and the
TCP stream in capture/xlxd-sut.pcapng cover it.

NOT RUN ON A RIG yet (written against the sources: DV-Router-wip 84cfcb14 and the aprsc source of .cache/aprsc). Observations go to
stdout and out/<run>/dp15-igate.txt ("RECORD", "FINDING"); latencies and windows also to out/<run>/dp15-igate.json.
"""
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import kiss_tnc, metrics  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

import data_support as ds  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_rf as rfm  # noqa: E402
# fixtures of the two earlier modules, reused by name: the fake TNCs rf-a / rf-b (their `hub` argument is the fixture of THIS module)
# and the private aprsc with its `shape` argument
from test_dp15_positions import aprsc, shape  # noqa: E402,F401
from test_dp15_rf import rf  # noqa: E402,F401

pytestmark = pytest.mark.rig

LOG = "dp15-igate.txt"
SUMMARY = "dp15-igate.json"
SETTING = "tx=%s" % os.environ.get("DVT_DP15_TX_ENABLE", "false")
TOCALL = "APDVT1"                   # destination of the injected frames
DIGIS = ("WIDE1-1*",)               # a digipeater that repeated the frame: the star must survive to the line
GATE_WAIT_S = 45.0                  # a positive frame reaches the aprsc (the edge reconnect and the worker pipeline are fast; the bound is generous)
ONCE_S = 8.0                        # how long a second copy of a positive frame is watched for after the first
ABSENT_MIN_S = 10.0                 # the shortest window in which a frame that must not be gated is watched
ABSENT_FACTOR = 4.0                 # window = factor x the slowest positive latency seen in the run
ABSENT_MAX_S = 60.0
COUNTER_QUIET_S = 12.0              # the worker publishes its counters every 10 s: unchanged for this long = settled
COUNTER_LIMIT_S = 90.0
COUNTER_WAIT_S = 30.0               # a counter the worker already bumped shows up within one publish (10 s) plus the control read
ROUTE_NAME = "dp15-igate"

# one source per frame (the receive dedupe key is source + destination + information for 30 s)
SRC_WEATHER, SRC_TELEMETRY = "N0IGW-3", "N0IGT-3"
SRC_QUERY, SRC_NOGATE, SRC_RFONLY = "N0IGQ-3", "N0IGN-3", "N0IGR-3"
SRC_CONTROL = {"query": "N0IGC-1", "nogate": "N0IGC-2", "rfonly": "N0IGC-3"}


# ---------------------------------------------------------------- pure helpers (unit tests: cases/unit/test_dp15_igate_helpers.py)

def route_label(name):
    """DV-Router's metric label of a path: `p` plus the 16 lower case hex digits of the FNV-1a 64 bit hash of the name
    (config.md `[[data.path]]` `name`, router/types.rs metric_route)."""
    value = 0xCBF29CE484222325
    for byte in name.encode("utf-8"):
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return "p%016x" % value


def igate_shape(doc):
    """What the case needs from a rendered config dict ({} when unreadable): the transit path from edge:ax25[/rf-a] to
    edge:aprsis (its name), the station callsign of [edges.ax25] (the igate call), whether rf-a has effective TX and so which q
    construct is expected (qAR with TX, qAO without; data.md 9.3 rule 5). None when the run lacks the edge, the port, the APRS-IS
    edge or the path."""
    edges = doc.get("edges") or {}
    ax = edges.get("ax25") if isinstance(edges.get("ax25"), dict) else {}
    if ax.get("enable") is not True or not (edges.get("aprsis") or {}).get("enable"):
        return None
    port = next((p for p in (ax.get("port") or []) if p.get("name") == "rf-a"), None)
    paths = [p for p in ((doc.get("data") or {}).get("path") or [])
             if p.get("from") in ("edge:ax25", "edge:ax25/rf-a") and p.get("to") == "edge:aprsis"
             and "transit" in (p.get("types") or []) and p.get("enable", True) is not False]
    if port is None or not paths or not ax.get("callsign"):
        return None
    tx_on = bool(ax.get("tx_enable")) and bool(port.get("tx"))
    return types.SimpleNamespace(path=paths[0]["name"], gateway=ax["callsign"], tx_on=tx_on, q="qAR" if tx_on else "qAO",
                                 per_min=int(paths[0].get("per_min", 20)))


def absent_window(latencies):
    """The seconds a frame that must not be gated is watched after the control arrived: ABSENT_FACTOR times the slowest
    positive latency of the run, at least ABSENT_MIN_S and at most ABSENT_MAX_S."""
    slowest = max(latencies, default=0.0)
    return min(ABSENT_MAX_S, max(ABSENT_MIN_S, ABSENT_FACTOR * slowest))


def check_gated_line(parsed, src, info, digis, q, gateway):
    """The problems of one line read at the aprsc ([] when it is the gated frame): source, destination, path as heard before
    the q construct, exactly one q construct that starts with `q` + the igate call, and the information field byte for byte."""
    problems = []
    if parsed["src"] != src:
        problems.append("source %r, expected %r" % (parsed["src"], src))
    if parsed["dest"] != TOCALL:
        problems.append("destination %r, expected %r" % (parsed["dest"], TOCALL))
    if parsed["before_q"] != list(digis):
        problems.append("path before the q construct %r, heard %r" % (parsed["before_q"], list(digis)))
    q_items = [item for item in parsed["path"] if len(item) == 3 and item.startswith("q") and item[1:].isalpha()]
    if len(q_items) != 1:
        problems.append("%d q constructs in the path %r" % (len(q_items), parsed["path"]))
    if parsed["q"][:2] != [q, gateway]:
        problems.append("q construct %r, expected it to start with [%r, %r]" % (parsed["q"], q, gateway))
    if parsed["info"] != info:
        problems.append("information field %r differs from the heard %r" % (parsed["info"], info))
    return problems


def path_counts(samples, label):
    """{result: total} of dv_pkt_path_total{route=label, dir=out} and whether the route has any series."""
    seen = [s for s in samples if s.name == "dv_pkt_path_total" and s.labels.get("route") == label]
    counts = {}
    for s in seen:
        if s.labels.get("dir", "out") == "out":
            counts[s.labels.get("result", "")] = counts.get(s.labels.get("result", ""), 0) + int(s.value)
    return counts, bool(seen)


# ---------------------------------------------------------------- fixtures

def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics with the data worker on (skips with the reason otherwise)."""
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def ledger():
    """What the cases measured: latencies of the positive frames and controls (seconds), the windows used, per frame."""
    return {"latency_s": {}, "window_s": {}, "counters": {}}


@pytest.fixture(scope="module")
def ctx(request, target, hub, ledger):
    """The run shape and the live pieces: skips with the reason when the rendered config lacks the edge, the APRS-IS edge or the
    transit path; then the fake TNCs (connected by the edge) and the private aprsc (the edge logged in and verified)."""
    shp = igate_shape(p1.read_config(target))
    if shp is None:
        pytest.skip("the rendered config lacks the AX.25 edge with port rf-a, the APRS-IS edge or a transit path edge:ax25/rf-a -> "
                    "edge:aprsis: add rig/config/dv-router/dp15-rf.toml, dp15-aprsis.toml and dp15-rf-igate.toml (docs/dp15-cases.md)")
    tnc = request.getfixturevalue("rf")
    host = request.getfixturevalue("aprsc")
    note(target, "igate", "RECORD", "shape: path %s, igate call %s, rf-a effective TX %s, expected q construct %s, aprsc %s" % (
        shp.path, shp.gateway, shp.tx_on, shp.q, host))
    yield types.SimpleNamespace(shape=shp, rf=tnc, host=host, label=route_label(shp.path))
    try:
        (Path(target.out) / SUMMARY).write_text(json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- the common steps

def read_counts(hub, ctx):
    samples = metrics.parse(hub.text())
    return path_counts(samples, ctx.label)


def settled_counts(hub, ctx):
    """The path counts once they did not change for COUNTER_QUIET_S (an earlier frame's publish has landed)."""
    counts, known = read_counts(hub, ctx)
    changed, end = time.monotonic(), time.monotonic() + COUNTER_LIMIT_S
    while time.monotonic() - changed < COUNTER_QUIET_S and time.monotonic() < end:
        time.sleep(2.0)
        current, known = read_counts(hub, ctx)
        if current != counts:
            counts, changed = current, time.monotonic()
    return counts, known


def counter_delta(hub, ctx, before, result, want, timeout=None):
    """(delta of dv_pkt_path_total{result} since `before`, route has a series) once the delta reached `want`, the route is
    still unknown after `timeout` seconds (default COUNTER_WAIT_S) or the time is up."""
    end = time.monotonic() + (COUNTER_WAIT_S if timeout is None else timeout)
    while True:
        counts, known = read_counts(hub, ctx)
        delta = counts.get(result, 0) - before.get(result, 0)
        if (known and delta >= want) or time.monotonic() >= end:
            return delta, known
        time.sleep(2.0)


def inject(ctx, src, info, digis):
    """The fake TNC rf-a hears `src>APDVT1[,digis]:info`; the monotonic time just before the write."""
    frame = kiss_tnc.ui_frame(src, TOCALL, info, digis=digis)
    t0 = time.monotonic()
    assert ctx.rf.a.inject(frame), "the fake TNC rf-a has no connection to inject into"
    return t0


def lines_from(tester, src, mark):
    return tester.matching(lambda p: p["src"] == src, mark)


def finish_counters(target, hub, ctx, case, before, expect, ledger):
    """Assert the path counters `expect` ({result: delta}) after the publish landed; a route without any series is a FINDING
    (the family is documented in data.md 13.1; its labels were read from the sources, not from a rig)."""
    for result, want in expect.items():
        _delta, known = counter_delta(hub, ctx, before, result, want)    # wait for the publish of the worker
        if not known:
            break                                                          # no series for the route: do not wait again
    time.sleep(COUNTER_QUIET_S)                         # a surplus count (a second send, a wrong verdict) lands in the next publish
    after, known = read_counts(hub, ctx)
    deltas = {r: after.get(r, 0) - before.get(r, 0) for r in set(after) | set(before) | set(expect)}
    ledger["counters"][case] = {r: d for r, d in deltas.items() if d}
    note(target, case, "RECORD", "dv_pkt_path_total{route=%s} deltas: %s" % (ctx.shape.path, {r: d for r, d in deltas.items() if d}))
    if not known:
        note(target, case, "FINDING", "dv_pkt_path_total has no series for the route %s (%s): the counter checks are skipped" % (
            ctx.shape.path, ctx.label))
        return
    for result, want in expect.items():
        assert deltas.get(result, 0) == want, "dv_pkt_path_total{route=%s,result=%s} moved by %d, expected %d (all deltas %s)" % (
            ctx.shape.path, result, deltas.get(result, 0), want, {r: d for r, d in deltas.items() if d})


def positive_case(target, hub, ctx, ledger, case, src, info):
    """Inject `info` from `src` and require ONE line at the aprsc that check_gated_line accepts; record the latency."""
    before, _known = settled_counts(hub, ctx)
    with p1.aprsc_login(ctx.host) as tester:
        mark = tester.mark()
        t0 = inject(ctx, src, info, DIGIS)
        hit = tester.wait_packet(lambda p: p["src"] == src, GATE_WAIT_S, since=mark)
        assert hit is not None, "%s: the frame from %s never reached the private aprsc in %.0f s (the transit path %s, or the aprsc login)" % (
            case, src, GATE_WAIT_S, ctx.shape.path)
        latency = hit[0] - t0
        time.sleep(ONCE_S)
        got = lines_from(tester, src, mark)
        raw = [parsed["raw"] for _at, parsed in got]
    ledger["latency_s"][case] = round(latency, 3)
    note(target, case, "RECORD", "latency heard on rf-a -> line at the aprsc: %.3f s; line: %s" % (latency, raw[0] if raw else None))
    assert len(got) == 1, "%s: %d lines from %s at the aprsc (the server's own 30 s duplicate window hides a second send): %s" % (
        case, len(got), src, raw)
    problems = check_gated_line(got[0][1], src, info, DIGIS, ctx.shape.q, ctx.shape.gateway)
    assert not problems, "%s: %s; line %s" % (case, "; ".join(problems), raw[0])
    if got[0][1]["q"][2:]:
        note(target, case, "RECORD", "after the igate call the server put: %s" % got[0][1]["q"][2:])
    finish_counters(target, hub, ctx, case, before, {"forwarded": 1}, ledger)


def negative_case(target, hub, ctx, ledger, case, control_key, bad_src, bad_info, bad_digis, result):
    """Inject the frame that must not be gated, then a weather control; the control must arrive, and the bad frame must not
    appear in absent_window(latencies) after that. `result` is the path counter that must count the refusal."""
    before, _known = settled_counts(hub, ctx)
    control_src = SRC_CONTROL[control_key]
    control_info = aprs.weather_info(11, 10, 1, 2, 90, 3, 4, 50, humidity=61, pressure=10100, software="d", unit="vtc")
    with p1.aprsc_login(ctx.host) as tester:
        mark = tester.mark()
        inject(ctx, bad_src, bad_info, bad_digis)
        t0 = inject(ctx, control_src, control_info, DIGIS)
        hit = tester.wait_packet(lambda p: p["src"] == control_src, GATE_WAIT_S, since=mark)
        assert hit is not None, "%s: the control frame from %s never reached the private aprsc in %.0f s: the check proves nothing" % (
            case, control_src, GATE_WAIT_S)
        control_latency = hit[0] - t0
        window = absent_window(list(ledger["latency_s"].values()) + [control_latency])
        time.sleep(window)
        bad = lines_from(tester, bad_src, mark)
        ctl = lines_from(tester, control_src, mark)
    ledger["latency_s"][case + " control"] = round(control_latency, 3)
    ledger["window_s"][case] = round(window, 1)
    note(target, case, "RECORD", "control latency %.3f s, watched %.1f s after it (positive latencies of the run: %s)" % (
        control_latency, window, dict(ledger["latency_s"])))
    assert not bad, "%s: a frame that must not be gated reached the private aprsc: %s" % (case, [p["raw"] for _at, p in bad])
    assert len(ctl) == 1, "%s: the control arrived %d times" % (case, len(ctl))
    finish_counters(target, hub, ctx, case, before, {"forwarded": 1, result: 1}, ledger)


# ================================================================ the cases

def test_dp15_8w_rf_weather_is_gated_with_qar(rig, target, hub, ctx, ledger):
    """data.md 9.3 rule 3 and 5: a positionless weather frame heard on RF goes up unchanged with `,qAR,<igate call>`."""
    info = aprs.weather_info(11, 10, 1, 0, 220, 4, 5, 77, rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900,
                             software="d", unit="vt1")
    positive_case(target, hub, ctx, ledger, "DP15-8w", SRC_WEATHER, info)


def test_dp15_8t_rf_telemetry_is_gated_with_qar(rig, target, hub, ctx, ledger):
    """data.md 9.3 rule 3 and 5: a `T#` telemetry frame heard on RF goes up unchanged with `,qAR,<igate call>`."""
    info = aprs.telemetry_info(7, (199, 0, 255, 73, 123), "01101001", comment="dvt")
    positive_case(target, hub, ctx, ledger, "DP15-8t", SRC_TELEMETRY, info)


def test_dp15_8q_rf_general_query_is_not_gated(rig, target, hub, ctx, ledger):
    """data.md 9.3 rule 2: a generic query (`?APRS?`) is never gated; dv_pkt_path_total counts it as filtered."""
    negative_case(target, hub, ctx, ledger, "DP15-8q", "query", SRC_QUERY, aprs.query_info("APRS"), ("WIDE1-1",), "filtered")


def test_dp15_8n_nogate_path_is_not_gated(rig, target, hub, ctx, ledger):
    """data.md 9.3 rule 2: NOGATE anywhere in the path blocks the gate (weather content, so only the path differs from 8w).
    aprsc drops such a packet itself, so the path counter result=no_gate is the DV-Router evidence."""
    info = aprs.weather_info(11, 10, 1, 1, 100, 1, 2, 60, humidity=70, pressure=10050, software="d", unit="vtn")
    negative_case(target, hub, ctx, ledger, "DP15-8n", "nogate", SRC_NOGATE, info, ("WIDE1-1*", "NOGATE"), "no_gate")


def test_dp15_8r_rfonly_path_is_not_gated(rig, target, hub, ctx, ledger):
    """data.md 9.3 rule 2: RFONLY anywhere in the path blocks the gate (as 8n)."""
    info = aprs.weather_info(11, 10, 1, 3, 110, 2, 3, 61, humidity=71, pressure=10060, software="d", unit="vtr")
    negative_case(target, hub, ctx, ledger, "DP15-8r", "rfonly", SRC_RFONLY, info, ("RFONLY",), "no_gate")
