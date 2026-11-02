# SPDX-License-Identifier: MIT
"""Pure parts of the RSS isolation experiment (cases/test_web_rss_isolation.py, docs/web-isolation-cases.md).

DV-Router asked which activity drives the linear RSS growth of dv-router (the launcher) and dv-svc-status seen in SOAK1.
The experiment runs ONE activity per rig run (6 modes, A to F) with the SOAK1 machinery, judges nothing except that the
activity ran, and writes DATA: per child Rss / Anon / Private_Dirty / Pss_Anon (smaps_rollup) and VmRSS first / last /
least-squares slope per minute over the measured part, plus the total series count of the hub scrape.

  MODES, resolve()      the mode table: A idle, B webrtc-churn, C voice, D proto-churn, E webrtc-voice, F voice-x3 (C with 3x the overs)
  timing()              5 + 10 minutes (DVT_ISOLATION_SHORT=1: 2 + 3), sample period
  build_plan()          the activity of a mode as a schedule (listener sessions, overs, login cycles), from a seed
  build_overs_scaled()  the overs of mode F: soak.build_overs with `factor` overs per lane and minute
  summarize()           per child statistics from the raw sample rows (soak-samples format), the two focus children
  activity_checks()     "the activity ran": the only judgement
  assemble()            the isolation-<mode>.json document
  events_table()        hub events and streams per mode (dvtest/events.py): events/min, estimate, streams/min, KiB per stream
  main()                python3 -m dvtest.isolation out/<run>...  the tables across the available isolation-*.json files
"""
import json
import random
from dataclasses import dataclass
from pathlib import Path

from . import soak

FOCUS = ("dv-router", "dv-svc-status")                 # the two children DV-Router asked about
SMAPS = soak.SMAPS_SERIES                               # rss, anon, private_dirty, pss_anon
TABLE_SERIES = ("rss", "anon", "private_dirty")         # the summary table columns
WARMUP_S = 300.0
MEASURED_S = 600.0
SAMPLE_S = 30.0
SHORT_WARMUP_S = 120.0
SHORT_MEASURED_S = 180.0
SHORT_SAMPLE_S = 15.0
WALL_MARGIN_S = 600.0                                   # hard cap = warmup + measured + this
SEATS = soak.SEATS                                      # 100 stable WebRTC listeners
CHURN_PER_MIN = soak.CHURN_PER_MIN                      # about 5 joins / leaves a minute
CYCLE_PERIOD_S = (4.0, 8.0)                             # login / logout every few seconds per lane (D)
CYCLE_HOLD_S = (1.0, 2.5)                               # a login lives this long before the logout
VOICE_LANES_E = ("usrp-a", "usrp-b")                    # E: one protocol source whose room the listeners hear
LANE_ROOM = {"usrp-a": "A", "usrp-b": "B"}
DEFAULT_E_LANE = "usrp-a"
LISTENER_FRACTION = 0.9                                 # activity: at least this share of the listeners connected
OVER_GAP_S = 15.0                                       # F: least seconds between the starts of two overs of one lane
VOICE_FACTOR_F = 3                                      # F: three overs per lane and minute instead of one (C)
OVER_FRACTION = 0.5                                     # activity: at least this share of planned overs / cycles ran and worked


# ------------------------------------------------------------------ the mode table

@dataclass(frozen=True)
class Mode:
    letter: str
    name: str
    listeners: int          # stable WebRTC listeners held by the generator
    churn_per_min: int      # listener joins / leaves a minute
    voice: str              # "none", "all" (the 6 required lanes + NXDN / P25 when they work) or "one"
    proto_churn: bool       # protocol clients log in and out every few seconds
    text: str
    voice_factor: int = 1   # overs per lane and minute relative to SOAK1 (one); F has 3

    @property
    def needs_generator(self):
        return self.listeners > 0

    @property
    def needs_lanes(self):
        return self.voice != "none" or self.proto_churn


MODES = {m.letter: m for m in (
    Mode("A", "idle", 0, 0, "none", False, "all children up, no clients at all"),
    Mode("B", "webrtc-churn", SEATS, CHURN_PER_MIN, "none", False, "100 stable WebRTC listeners + about 5 joins/leaves a minute, no voice"),
    Mode("C", "voice", 0, 0, "all", False, "the protocol lanes with overs as in SOAK1 (about 10 s per module per minute), no WebRTC listeners"),
    Mode("D", "proto-churn", 0, 0, "none", True, "protocol clients log in / out every few seconds on all lanes, no voice"),
    Mode("E", "webrtc-voice", SEATS, 0, "one", False, "100 stable WebRTC listeners, no churn, voice in one room (one protocol source)"),
    Mode("F", "voice-x3", 0, 0, "all", False, "the lane mix of C with 3x as many overs: three per lane and minute (one every 20 s slot), no WebRTC listeners",
         voice_factor=VOICE_FACTOR_F),
)}
ALIASES = {m.name: m.letter for m in MODES.values()}


def resolve(text):
    """Mode of DVT_ISOLATION: the letter (A to F, any case) or the name (idle, webrtc-churn, voice, proto-churn,
    webrtc-voice, voice-x3). ValueError listing the valid values otherwise."""
    key = (text or "").strip()
    letter = key.upper() if key.upper() in MODES else ALIASES.get(key.lower())
    if letter is None:
        raise ValueError("DVT_ISOLATION=%r is not a mode; use one of %s" % (text, ", ".join("%s (%s)" % (m.letter, m.name) for m in MODES.values())))
    return MODES[letter]


def e_lane_of(env):
    lane = (env.get("DVT_ISOLATION_E_LANE") or DEFAULT_E_LANE).strip()
    if lane not in VOICE_LANES_E:
        raise ValueError("DVT_ISOLATION_E_LANE=%r must be one of %s (the source has to talk into a room the listeners hear)" % (lane, VOICE_LANES_E))
    return lane


def lanes_for(mode, available, e_lane=DEFAULT_E_LANE):
    """The lane names (of `available`, in order) that take part in the mode."""
    if mode.voice == "one":
        return [n for n in available if n == e_lane]
    if mode.voice == "all" or mode.proto_churn:
        return list(available)
    return []


# ------------------------------------------------------------------ timing

def _seconds(env, name):
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("%s=%r is not a number" % (name, raw)) from None
    if not value == value or value < 0:
        raise ValueError("%s=%r must be a number of seconds >= 0" % (name, raw))
    return value


def timing(env):
    """{"warmup_s", "measured_s", "sample_s", "short", "smoke"}: 300 + 600 s with a sample every 30 s; DVT_ISOLATION_SHORT=1 is
    the smoke run (120 + 180 s, a sample every 15 s). DVT_ISOLATION_WARMUP_S / _MEASURE_S / _SAMPLE_S override either; any other
    timing than the documented one is a smoke run. ValueError for a bad number or fewer than 6 samples in the measured part."""
    short = (env.get("DVT_ISOLATION_SHORT") or "").strip() == "1"
    warmup = _seconds(env, "DVT_ISOLATION_WARMUP_S")
    measured = _seconds(env, "DVT_ISOLATION_MEASURE_S")
    sample = _seconds(env, "DVT_ISOLATION_SAMPLE_S")
    warmup = warmup if warmup is not None else (SHORT_WARMUP_S if short else WARMUP_S)
    measured = measured if measured is not None else (SHORT_MEASURED_S if short else MEASURED_S)
    sample = sample if sample is not None else (SHORT_SAMPLE_S if short else SAMPLE_S)
    if sample < 0.05 or measured < (soak.MIN_SAMPLES + 1) * sample:
        raise ValueError("measured %g s with a sample every %g s gives fewer than %d samples" % (measured, sample, soak.MIN_SAMPLES + 1))
    return {"warmup_s": warmup, "measured_s": measured, "sample_s": sample, "short": short,
            "smoke": (warmup, measured) != (WARMUP_S, MEASURED_S)}


# ------------------------------------------------------------------ the activity plan

def build_cycles(seed, lanes, duration_s, period_s=CYCLE_PERIOD_S, hold_s=CYCLE_HOLD_S):
    """Login / logout cycles of the protocol clients (mode D): every lane gets its own period (4 to 8 s, +- 25 percent
    jitter) so that some lanes cycle faster than others; a cycle links the client, holds it `dur_s` seconds and unlinks.
    [{"t", "lane", "dur_s"}] sorted by t, t in seconds after the traffic start; the last cycle starts 10 s before the end."""
    rng = random.Random("isolation-cycles/%s" % seed)
    out = []
    for lane in lanes:
        period = rng.uniform(*period_s)
        t = rng.uniform(0.0, period)
        while t < duration_s - 10.0:
            out.append({"t": round(t, 2), "lane": lane, "dur_s": round(rng.uniform(*hold_s), 1)})
            t += period * rng.uniform(0.75, 1.25)
    out.sort(key=lambda c: (c["t"], c["lane"]))
    return out


def build_overs_scaled(seed, lanes, duration_s, factor=VOICE_FACTOR_F, storm_every=5):
    """soak.build_overs with `factor` overs per lane and minute (mode F, factor 3 = three times the over frequency of C): each minute is
    split into `factor` slots of 60 / factor seconds, and every slot holds one over of every lane of about 10 s (9 to 11 s) at the offset of
    SOAK1 scaled by 1 / factor (lanes spread over 45 / factor s of the slot, plus 0 to 2 s), so neighbouring lanes overlap as in C; every
    fifth minute all lanes start within a second of each other in EVERY slot. The overs of one lane are at least OVER_GAP_S (15 s) apart,
    longer than an over with its link and unlink (a start that would be closer, after a normal slot into a storm slot, is moved later),
    so a lane never overlaps itself. [{"t", "lane", "dur_s"}] sorted by t, t seconds after the traffic start. `factor` 1 is the
    shape of soak.build_overs (not the same numbers: another random stream)."""
    if not lanes:
        return []
    rng = random.Random("isolation-overs-x%d/%s" % (factor, seed))
    slot = 60.0 / factor
    out = []
    last = {}
    for minute in range(int(duration_s // 60)):
        storm = minute % storm_every == storm_every - 1
        for k in range(factor):
            for i, lane in enumerate(lanes):
                dur = round(rng.uniform(soak.OVER_S - 1.0, soak.OVER_S + 1.0), 1)
                off = (3.0 + rng.uniform(0.0, 1.0)) if storm else (i * (45.0 / factor) / len(lanes) + rng.uniform(0.0, 2.0))
                t = max(minute * 60.0 + k * slot + off, last.get(lane, -1e9) + OVER_GAP_S)
                last[lane] = t
                out.append({"t": round(t, 2), "lane": lane, "dur_s": dur})
    out.sort(key=lambda o: (o["t"], o["lane"]))
    return out


def build_plan(mode, seed, run_s, lane_names, e_lane=DEFAULT_E_LANE):
    """The schedule of a mode: {"sessions": WebRTC listener sessions (soak.build_sessions), "overs": voice overs
    (soak.build_overs), "cycles": login cycles (build_cycles), "lanes": the lane names used}. Nothing for the parts the
    mode does not have."""
    lanes = lanes_for(mode, lane_names, e_lane)
    sessions = soak.build_sessions(seed, run_s, seats=mode.listeners, churn_per_min=mode.churn_per_min) if mode.listeners else []
    if mode.voice == "none":
        overs = []
    elif mode.voice_factor > 1:
        overs = build_overs_scaled(seed, lanes, run_s, mode.voice_factor)
    else:
        overs = soak.build_overs(seed, lanes, run_s)
    cycles = build_cycles(seed, lanes, run_s) if mode.proto_churn else []
    return {"sessions": sessions, "overs": overs, "cycles": cycles, "lanes": lanes}


def schedule_of(plan):
    """(items, method): the Traffic schedule and the Lane method that runs an item ("over" or "cycle")."""
    if plan["cycles"]:
        return plan["cycles"], "cycle"
    return plan["overs"], "over"


def summarize_plan(plan):
    cycles = {}
    for c in plan["cycles"]:
        cycles[c["lane"]] = cycles.get(c["lane"], 0) + 1
    base = soak.summarize_plan(plan["overs"], plan["sessions"])
    base.update(cycles=sum(cycles.values()), cycles_per_lane=dict(sorted(cycles.items())), lanes=list(plan["lanes"]))
    return base


# ------------------------------------------------------------------ statistics from the raw sample rows

def series_stats(points):
    """{"n", "first", "last", "min", "max", "slope_per_min"} of [(t_seconds, value)] (least squares, per minute); {"n": 0} when empty."""
    pts = sorted(points)
    if not pts:
        return {"n": 0}
    xs, ys = [t for t, _ in pts], [v for _, v in pts]
    slope = soak.least_squares(xs, ys)[0] if len(pts) > 1 else 0.0
    return {"n": len(pts), "first": ys[0], "last": ys[-1], "min": min(ys), "max": max(ys), "slope_per_min": round(slope * 60.0, 3)}


def _measured_proc_rows(rows):
    by = {}
    for r in rows:
        if r.get("kind") == "proc" and r.get("name") and not r.get("absent") and r.get("phase") == "measured":
            by.setdefault(r["name"], []).append(r)
    for lst in by.values():
        lst.sort(key=lambda r: (r["t"], r.get("seq", 0)))
    return by


def child_record(rows):
    """Statistics of the measured proc rows of one child: VmRSS, threads, fds and the smaps_rollup series that the rows carry."""
    rec = {"n": len(rows), "vmrss": series_stats([(r["t"], r["rss_kib"]) for r in rows if isinstance(r.get("rss_kib"), (int, float))])}
    rec["threads"] = series_stats([(r["t"], r["threads"]) for r in rows if isinstance(r.get("threads"), (int, float))])
    rec["fds"] = series_stats([(r["t"], r["fds"]) for r in rows if isinstance(r.get("fds"), (int, float)) and r["fds"] >= 0])
    rec["smaps"] = soak.smaps_stats(rows)
    rec["restarts"] = len({(r.get("pid"), r.get("start")) for r in rows}) - 1
    return rec


def summarize(rows):
    """{"children": {name: child_record}, "focus": {dv-router, dv-svc-status: record or None}, "series_total": stats,
    "gauge": {"n", "min", "max"} of the listener gauge, "measured_s": span of the measured samples} from the parsed
    sample rows (any phase labels are first corrected by the counter snapshots, soak.assign_phases)."""
    rows = soak.assign_phases(rows)
    by = _measured_proc_rows(rows)
    children = {name: child_record(lst) for name, lst in sorted(by.items())}
    series = [(r["t"], r.get("series_total", 0)) for r in rows if r.get("kind") == "attr" and r.get("phase") == "measured"]
    gauge = [r["listeners"] for r in rows if r.get("kind") == "gauge" and r.get("phase") == "measured"]
    times = [r["t"] for lst in by.values() for r in lst]
    return {"children": children, "focus": {n: children.get(n) for n in FOCUS}, "series_total": series_stats(series),
            "gauge": {"n": len(gauge), "min": min(gauge) if gauge else None, "max": max(gauge) if gauge else None},
            "measured_span_s": round(max(times) - min(times), 1) if times else 0.0}


def _f(value, digits=1):
    return "-" if value is None else ("%.*f" % (digits, value))


def children_table(children):
    """Text lines: every child's RSS slope per minute (smaps Rss and VmRSS) next to Anon and Private_Dirty, KiB."""
    lines = ["%-18s %4s %9s %9s %10s %10s %10s %10s" % ("child", "n", "vmrss0", "vmrssN", "vmrss/min", "Rss/min", "Anon/min", "PrivDirty/min")]
    for name, c in sorted(children.items(), key=lambda kv: (-abs(kv[1]["vmrss"].get("slope_per_min", 0.0)), kv[0])):
        v, sm = c["vmrss"], c.get("smaps") or {}
        lines.append("%-18s %4d %9s %9s %10s %10s %10s %10s" % (
            name, c["n"], _f(v.get("first"), 0), _f(v.get("last"), 0), _f(v.get("slope_per_min")),
            _f((sm.get("rss") or {}).get("slope_per_min")), _f((sm.get("anon") or {}).get("slope_per_min")),
            _f((sm.get("private_dirty") or {}).get("slope_per_min"))))
    return lines


# ------------------------------------------------------------------ "the activity ran"

def _lane_ran(summary, planned, lane, what):
    info = (summary or {}).get(lane)
    if info is None:
        return soak.Check("activity:%s:%s" % (what, lane), False, "nothing ran")
    good = info["overs"] >= OVER_FRACTION * planned and info["ok"] >= OVER_FRACTION * info["overs"] and info["overs"] > 0
    return soak.Check("activity:%s:%s" % (what, lane), good, "%d of %d planned, %d worked, errors %s"
                      % (info["overs"], planned, info["ok"], info.get("errors") or "none"))


def activity_checks(mode, plan_sum, summary, traffic=None, generator=None):
    """The only judgement: [soak.Check] that the activity of the mode ran. `summary` = summarize(), `traffic` =
    ss.Traffic.summary(), `generator` = soak.generator_summary(rows)[0] (None when the mode has no listeners).
    Sampled: dv-router and dv-svc-status have enough measured samples. Listeners: the generator connected nearly all of
    them and the hub gauge reached nearly all. Churn: at least half of the planned leaves happened. Voice: the overs of every
    lane worked (the USRP lanes: sent) and in mode E the listeners of the talked room heard packets. Cycles: likewise.
    What the listeners heard in a mode without voice is only recorded (JSON), not judged."""
    checks = []
    got = {n: ((summary["focus"].get(n) or {}).get("n", 0)) for n in FOCUS}
    checks.append(soak.Check("activity:sampled", all(v >= soak.MIN_SAMPLES for v in got.values()),
                             "measured samples %s (need %d each)" % (got, soak.MIN_SAMPLES)))
    if mode.listeners:
        gen = generator or {}
        want = LISTENER_FRACTION * mode.listeners
        checks.append(soak.Check("activity:listeners-connected", gen.get("connected", 0) >= want and gen.get("failed", 0) <= max(2, 0.02 * gen.get("sessions", 0)),
                                 "%s connected, %s failed of %s sessions (seats %d)" % (gen.get("connected"), gen.get("failed"), gen.get("sessions"), mode.listeners)))
        peak = summary["gauge"].get("max")
        checks.append(soak.Check("activity:listeners-gauge", peak is not None and peak >= want,
                                 "hub listener gauge peak %s in the measured part (need %g)" % (peak, want)))
    if mode.churn_per_min:
        leaves = plan_sum.get("leaves", 0)
        left = (generator or {}).get("left", 0)
        checks.append(soak.Check("activity:churn", leaves > 0 and left >= OVER_FRACTION * leaves, "%d of %d planned leaves happened" % (left, leaves)))
    for lane in plan_sum.get("lanes", []):
        if mode.voice != "none":
            checks.append(_lane_ran(traffic, plan_sum["overs_per_lane"].get(lane, 0), lane, "voice"))
        if mode.proto_churn:
            checks.append(_lane_ran(traffic, plan_sum["cycles_per_lane"].get(lane, 0), lane, "cycles"))
    if mode.voice == "one" and mode.listeners:
        for lane in plan_sum.get("lanes", []):
            room = LANE_ROOM.get(lane)
            info = ((generator or {}).get("rooms") or {}).get(room) or {}
            checks.append(soak.Check("activity:room-heard:%s" % room, info.get("packets", 0) > 0,
                                     "room %s listeners received %s packets" % (room, info.get("packets", 0))))
    return checks


# ------------------------------------------------------------------ the document

def assemble(mode, tm, plan_sum, rows, checks, extra=None):
    """The isolation-<mode>.json document: mode, timing, the plan, the focus children, every child, the series count of the
    scrape, the listener gauge, the activity checks and `extra` (traffic and generator summaries, run notes)."""
    summary = summarize(rows)
    doc = {"schema": 1, "case": "ISOLATION", "mode": mode.letter, "name": mode.name, "activity": mode.text,
           "timing": dict(tm), "plan": plan_sum, "focus": summary["focus"], "children": summary["children"],
           "children_table": children_table(summary["children"]), "series_total": summary["series_total"],
           "gauge": summary["gauge"], "measured_span_s": summary["measured_span_s"], "samples": len(rows),
           "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks], "ok": all(c.ok for c in checks)}
    doc.update(extra or {})
    return doc


# ------------------------------------------------------------------ the offline summarizer

def find_documents(paths):
    """The isolation-*.json files under run directories (out/<run>, out/<run>/web) or named directly; sorted. Files that are
    not JSON objects of this case are skipped."""
    found = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            cands = sorted(p.glob("isolation-*.json")) + sorted((p / "web").glob("isolation-*.json"))
        else:
            cands = [p]
        for c in cands:
            if c not in found:
                found.append(c)
    docs = []
    for c in found:
        try:
            doc = json.loads(c.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("case") == "ISOLATION":
            doc["_path"] = str(c)
            docs.append(doc)
    return docs


def _run_of(doc):
    if doc.get("run"):
        return str(doc["run"])
    parts = Path(doc.get("_path", "")).parts
    return parts[-3] if len(parts) >= 3 and parts[-2] == "web" else (parts[-2] if len(parts) >= 2 else "?")


def _slope(rec, series):
    """Slope per minute of a smaps series of a focus record; for Rss the VmRSS slope when the run has no smaps."""
    if not rec:
        return None
    got = (rec.get("smaps") or {}).get(series)
    if got:
        return got.get("slope_per_min")
    if series == "rss":
        return (rec.get("vmrss") or {}).get("slope_per_min")
    return None


def table(docs):
    """One table over the documents: mode x (dv-router, dv-svc-status) x (Rss, Anon, Private_Dirty) slope in KiB per minute."""
    head1 = "%-5s %-13s %-17s %5s | %s | %s" % ("mode", "name", "run", "ok", "dv-router KiB/min".center(31), "dv-svc-status KiB/min".center(31))
    head2 = "%-5s %-13s %-17s %5s | %10s %10s %10s | %10s %10s %10s" % ("", "", "", "", "Rss", "Anon", "PrivDirty", "Rss", "Anon", "PrivDirty")
    lines = [head1, head2, "-" * len(head2)]
    for doc in sorted(docs, key=lambda d: (d.get("mode", "?"), _run_of(d))):
        cells = []
        for child in FOCUS:
            rec = (doc.get("focus") or {}).get(child)
            cells.extend(_f(_slope(rec, s)) for s in TABLE_SERIES)
        lines.append("%-5s %-13s %-17s %5s | %10s %10s %10s | %10s %10s %10s"
                     % ((doc.get("mode", "?"), doc.get("name", "?"), _run_of(doc)[:17], "yes" if doc.get("ok") else "NO") + tuple(cells)))
    lines.append("Rss = smaps_rollup Rss, VmRSS when the run has no smaps keys; '-' = not measured; 'ok' = the activity ran")
    return "\n".join(lines)


def _get(doc, *path):
    cur = doc
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def events_table(docs):
    """Hub events and streams per file (the `hub_events` and `streams` records of dvtest/events.py): events per minute over the measured
    part, the estimate events/min x 1.16 KiB, the dv-svc-status Rss slope and the ratio estimate / slope, streams per minute, and the
    Rss slope per stream (KiB/min divided by streams/min) of dv-router, dv-room (sum of the ranks) and dv-svc-status. '' when no file has
    the records (an older run)."""
    rows = [d for d in sorted(docs, key=lambda d: (d.get("mode", "?"), _run_of(d))) if d.get("hub_events") or d.get("streams")]
    if not rows:
        return ""
    head = "%-5s %-13s %-17s | %9s %9s %9s %6s | %9s | %10s %10s %10s" % (
        "mode", "name", "run", "events/min", "est KiB/m", "svc Rss/m", "ratio", "streams/m", "router/str", "room/str", "status/str")
    lines = [head, "-" * len(head)]
    for d in rows:
        per = ("dv-router", "dv-room", "dv-svc-status")
        cells = [_f(_get(d, "streams", "slope_per_stream", c, "rss_kib_per_stream"), 2) for c in per]
        lines.append("%-5s %-13s %-17s | %9s %9s %9s %6s | %9s | %10s %10s %10s" % (
            d.get("mode", "?"), d.get("name", "?"), _run_of(d)[:17], _f(_get(d, "hub_events", "measured", "mean_per_min")),
            _f(_get(d, "hub_events", "events_kib_per_min")), _f(_get(d, "hub_events", "status_rss_slope_kib_per_min")),
            _f(_get(d, "hub_events", "ratio_to_rss"), 2), _f(_get(d, "streams", "streams_per_min"), 2), *cells))
    lines.append("est = events/min x 1.16 KiB; ratio = est / dv-svc-status Rss slope (1.0: the event ring fill alone explains it); x/str = KiB per stream "
                 "(Rss slope / streams per minute); '-' = not measured")
    return "\n".join(lines)


def main(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.isolation", description="Print one table across the isolation-*.json files of "
                                 "RSS isolation runs: mode x (dv-router, dv-svc-status) x (Rss, Anon, Private_Dirty) slope in KiB/min, then the "
                                 "hub events and streams table.")
    ap.add_argument("paths", nargs="+", help="run directories (out/<utc>), their web directories or isolation-*.json files")
    ap.add_argument("--children", action="store_true", help="also print the table of every child per run")
    args = ap.parse_args(argv)
    docs = find_documents(args.paths)
    if not docs:
        print("no isolation-*.json found under: %s" % " ".join(args.paths), file=sys.stderr)
        return 1
    print(table(docs))
    extra = events_table(docs)
    if extra:
        print("\n" + extra)
    if args.children:
        for doc in sorted(docs, key=lambda d: (d.get("mode", "?"), _run_of(d))):
            print("\nmode %s %s, run %s, series_total slope/min %s" % (doc.get("mode"), doc.get("name"), _run_of(doc),
                                                                       (doc.get("series_total") or {}).get("slope_per_min")))
            print("\n".join(doc.get("children_table") or []))
            if doc.get("events_note"):
                print("events: %s" % doc["events_note"])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
