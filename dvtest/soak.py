# SPDX-License-Identifier: MIT
"""Pure parts of the web soak case (cases/test_web_soak.py, docs/web-soak-cases.md); no network, no clock.

  samples      one JSON object per line (soak-samples.jsonl), written by the case as it samples: kind "proc" (one
               child of the launcher: pid, start time, RSS, threads, fds), "metrics" (error-like counters of the
               hub summed over their labels), "gauge" (listeners at the edge), "mark" (phase changes). parse_jsonl
               reads them back; a damaged line is counted, never fatal.
  schedule     build_overs / build_sessions: the traffic mix and the listener churn, deterministic from a seed
  judgement    evaluate(): the pass criteria (restarts, RSS slope, threads and fds, idle fds, error counters, the
               public event stream, the idle gauge), table(): the per child report

Lead defaults of the criteria (DV-Router, 2026-11-02), all constants below:
  1  no child restarts: one (pid, start time) per child over the whole run, and no child vanishes or appears late
  2  RSS growth over the measured part <= max(5 MiB, 10 percent of the RSS at the end of warmup), judged on the
     least-squares slope x the measured window (not on the last sample); NOT a verdict in a short run (a lead,
     params["rss_judged"] False). Next to the RSS the case records the gauge-style series of dv-svc-status and
     dv-router (ATTR_INCLUDE) and the number of series of the scrape, every tick, for attribution (recorded,
     never asserted: attribution())
  3  threads and fds are NOT judged by the last sample (the tokio blocking pool spawns threads lazily and keeps one
     about 10 s): per child the median over the LAST 10 minutes of the measured part against the median over the
     FIRST 10 minutes after warmup (both windows scaled with a shorter measured part: 10/50 of it), FAIL only when
     the later median exceeds the earlier by more than max(10 percent of the earlier, 3 threads) resp. max(10 percent,
     4 fds). A single last sample above the old limit (end of warmup + max(10 percent, +2 threads / +4 fds)) is only
     FLAGGED (a warning). After all traffic stopped the fds are within +5 of the idle value taken before any traffic
     (rulings of DV-Router, 2026-11-02)
  4  the counters of FLAT_FAMILIES are FLAT over the measured part (value at the end of warmup == value at the
     end, absolute zero growth); a series that is not on /metrics counts as 0 (LAZY_FAMILIES are created at their
     first increment: they must not appear or grow, and are never required to be present); a family that grows and
     that DV-Router ruled on goes into FLAT_OVERRIDES (starts empty) and is recorded with its growth; every
     other error-like family is recorded only. dv_edge_drops_total is NOT flat (it counts legitimate events of
     the lane mix): judge_drops(): idle window no reason grows; measured window only a reason outside
     DROPS_LEGIT fails, the legit reasons are recorded with their rate and flagged above 1 per second
  5  the public event stream is quiet while idle (dvtest.k22.idle_stream_verdict, judged by the case)
"""
import json
import random
import re
from dataclasses import dataclass, field

from . import webload as wl

WARMUP_S = 600.0
MEASURED_S = 3000.0
GA_MEASURED_S = 2400.0          # the GA profile (DVT_SOAK_GA=1): 10 min warmup + 40 min measured, limits scaled by 0.8
SAMPLE_S = 30.0
IDLE_S = 120.0
# Settle between the last client leaving and the start of the idle window: the longest link timeout of the SUT plus
# one sweep tick (DV-Router ruling 2026-11-02: imrs link timeout 150 s + sweep tick). The idle window must show no
# growth of any dv_edge_drops_total reason, so every link of a departed client must have aged out before it starts.
LINK_TIMEOUTS_S = {"imrs": 150, "mmdvm": 100, "dextra": 60, "dcs": 60, "ysf": 60, "xlx": 30, "dplus": 30, "m17": 30, "nxdn": 30,
                   "p25": 30}                   # usrp (dynamic) never expires
SWEEP_TICK_S = 20
SETTLE_MIN_S = max(LINK_TIMEOUTS_S.values()) + SWEEP_TICK_S        # 170 s; cases/test_web_soak.py SETTLE_S must be >= this
WALL_CAP_S = 75 * 60.0
SEATS = 100                     # receive-only WebRTC listeners held at any time
CHURN_PER_MIN = 5
OVER_S = 10.0

RSS_FLOOR_KIB = 5 * 1024
RSS_FRACTION = 0.10
THREADS_FRACTION = 0.10
THREADS_FLOOR = 3               # median rule: later median may exceed the earlier by max(10 percent, 3 threads)
FDS_FRACTION = 0.10
FDS_FLOOR = 4                   # ... and by max(10 percent, 4 fds)
MEDIAN_WINDOW_S = 600.0         # first / last 10 minutes of the real measured part (scaled for a shorter one, see timing())
MEDIAN_MIN_SAMPLES = 3          # a window with fewer samples takes the first / last this many samples instead
FLAG_THREADS_FLOOR = 2          # the OLD end-sample limit (end of warmup + max(10 percent, 2 / 4)): now only a flag
FLAG_FDS_FLOOR = 4
IDLE_FDS_SLACK = 5
MIN_SAMPLES = 5                 # measured samples a child needs for a slope

PHASES = ("idle0", "warmup", "measured", "stopping", "idle")
# Short mode (DVT_SOAK_SHORT=1, a validation of the lanes and the sampling, not the criterion): same mix and the
# same criteria, 180 s warmup + 240 s measured, a sample every 15 s.
SHORT_WARMUP_S = 180.0
SHORT_MEASURED_S = 240.0
SHORT_SAMPLE_S = 15.0

# Error-like families are RECORDED (value summed over the labels, deltas over the phases) when their name looks
# like an error; none of them is asserted unless it is in FLAT_FAMILIES.
RECORD_PATTERN = re.compile(r"drop|refus|reject|denied|panic|error|overflow|overrun|lost|fail|timeout|discard", re.I)

# Counters asserted FLAT over the measured part (DV-Router ruling 2026-11-02, docs/design/status.md "Metrics"):
# the value at the end of warmup must equal the value at the end, absolute zero growth.
# A series that is not on /metrics counts as 0 (rulings of DV-Router 2026-11-02).
FLAT_FAMILIES = (
    "dv_edge_webrtc_panics_total", "dv_edge_queue_drops_total", "dv_edge_webrtc_audio_dropped_total",
    "dv_room_ingress_rejected_total", "dv_room_shell_errors_total", "dv_status_telemetry_rejected_total",
    "dv_status_input_dropped_total", "dv_fabric_put_dropped_total", "dv_recorder_dropped_total", "dv_codec_dropped_total",
    "dv_edge_tx_refused_total",
)
# Created at their first increment: absent is 0, never "must be present"; the case only checks that they do not
# appear or grow over the measured part (appearing is growth from 0).
LAZY_FAMILIES = ("dv_edge_tx_refused_total", "dv_recorder_dropped_total", "dv_room_shell_errors_total")
# dv_edge_drops_total counts legitimate events of the lane mix (links time out, packets for unknown sessions): judged
# by reason (judge_drops), not flat. Idle window: no reason grows. Measured window: a reason outside DROPS_LEGIT
# fails; the legit ones are recorded with their rate and flagged (a warning) above DROPS_RATE_FLAG_PER_S in total.
DROPS_FAMILY = "dv_edge_drops_total"
DROPS_LEGIT = ("link_timeout", "no_session", "rx_ignored", "x_stale", "id_held")   # id_held: datagram held while a radio-id lookup is pending (DV-Router ruling)
DROPS_RATE_FLAG_PER_S = 1.0
SNAPSHOT_FAMILIES = FLAT_FAMILIES + (DROPS_FAMILY,)               # what the counter snapshots keep
# Per-family overrides, {family: reason}: a family that grows with the traffic mix is NOT asserted but recorded with
# its name and growth, until DV-Router rules on it. Starts empty on purpose.
FLAT_OVERRIDES = {}

# RSS attribution (recorded every sample tick next to the RSS, never asserted): the gauge-style series of the hub
# /metrics of dv-svc-status (events ring, positions, text history, clients, rooms on air ...) and of dv-router (sessions,
# rooms, routes, launcher state). A series is kept when its name matches one of ATTR_INCLUDE and none of
# ATTR_EXCLUDE (counters and histogram parts are not size gauges). One constant, so that the names learned from a
# live /metrics text (the "gauges" snapshots of soak.json) are fixed in one place.
ATTR_INCLUDE = (
    r"^dv_status_", r"^dv_pkt_", r"^dv_launcher_", r"^dv_fabric_", r"^dv_router_", r"^dv_room_",
    r"^dv_edge_(sessions|webrtc_listeners|webrtc_room_listeners)$", r"^dv_idsync_entries$", r"^dv_codec_(active_sessions|queue_frames)$",
)
ATTR_EXCLUDE = (r"_total$", r"_bucket$", r"_sum$", r"_count$", r"_up$", r"timestamp")
ATTR_MAX_SERIES = 400               # series kept per scrape; the rest is counted (attr row "truncated")
ATTR_KEY_SERIES_TOTAL = "scrape:series_total"

# smaps_rollup of a process (KiB), recorded next to the RSS when the proc-stat reply carries it (scripts/lib/procstat.sh
# keys smaps_rss_kb, smaps_anon_kb, smaps_private_dirty_kb, smaps_pss_anon_kb): never asserted, absent is fine.
SMAPS_SERIES = ("rss", "anon", "private_dirty", "pss_anon")


# ------------------------------------------------------------------ timing (real run and short mode)

def _env_float(env, *names):
    for name in names:
        raw = (env.get(name) or "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                raise ValueError("%s=%r is not a number" % (name, raw)) from None
            if not value == value or value < 0:
                raise ValueError("%s=%r must be a number of seconds >= 0" % (name, raw))
            return value
    return None


def timing(env):
    """The timing of a run from its environment: {"warmup_s", "measured_s", "sample_s", "short", "smoke", "rss_scale"}.
    Defaults: the real run, 600 s warmup + 3000 s measured, a sample every 30 s. DVT_SOAK_SHORT=1: the validation
    run, 180 s + 240 s, a sample every 15 s. DVT_SOAK_WARMUP_S, DVT_SOAK_MEASURE_S (also spelled DVT_SOAK_MEASURED_S)
    and DVT_SOAK_SAMPLE_S override either. `smoke` = not the real timing. `rss_scale` scales the RSS growth bound of
    a measured part shorter than the real one: the bound is a RATE (max(5 MiB, 10 percent) per 3000 s), so a measured
    part of 240 s gets 240/3000 of the bytes; it is as sharp as the real one in KiB per minute and noisier, so a
    failure of the RSS check in a short run is a lead, not a verdict. `median_window_s` = the first / last
    window of the thread and fd median rule: 600 s of the real 3000 s measured part, scaled by the same factor
    (240 s measured: 48 s). DVT_SOAK_GA=1 is the GA profile: 600 s + 2400 s, every limit scaled by 0.8 (RSS bound as a
    rate, median windows 480 s), judged like the real run (RSS slopes are a verdict; `ga` true, `smoke` false); an
    explicit DVT_SOAK_WARMUP_S / DVT_SOAK_MEASURE_S other than that makes it a validation again. ValueError for a bad number or fewer than 6 samples in the measured part (a slope needs
    MIN_SAMPLES)."""
    short = (env.get("DVT_SOAK_SHORT") or "").strip() == "1"
    ga = (env.get("DVT_SOAK_GA") or "").strip() == "1" and not short
    warmup = _env_float(env, "DVT_SOAK_WARMUP_S")
    measured = _env_float(env, "DVT_SOAK_MEASURE_S", "DVT_SOAK_MEASURED_S")
    sample = _env_float(env, "DVT_SOAK_SAMPLE_S")
    warmup = warmup if warmup is not None else (SHORT_WARMUP_S if short else WARMUP_S)
    measured = measured if measured is not None else (SHORT_MEASURED_S if short else GA_MEASURED_S if ga else MEASURED_S)
    sample = sample if sample is not None else (SHORT_SAMPLE_S if short else SAMPLE_S)
    if sample < 0.05 or measured < (MIN_SAMPLES + 1) * sample:
        raise ValueError("measured %g s with a sample every %g s gives fewer than %d samples" % (measured, sample, MIN_SAMPLES + 1))
    ga = ga and (warmup, measured) == (WARMUP_S, GA_MEASURED_S)          # an override of the timing makes it a validation again
    smoke = (warmup, measured) != (WARMUP_S, MEASURED_S) and not ga
    scale = min(1.0, measured / MEASURED_S)
    return {"warmup_s": warmup, "measured_s": measured, "sample_s": sample, "short": short, "smoke": smoke, "ga": ga,
            "rss_scale": scale, "median_window_s": MEDIAN_WINDOW_S * scale}


# ------------------------------------------------------------------ small maths

def median(values):
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    return float(vals[mid]) if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def least_squares(xs, ys):
    """(slope, intercept) of the least-squares line; (0.0, mean) for fewer than two distinct x."""
    n = len(xs)
    if n == 0 or n != len(ys):
        raise ValueError("least_squares needs equal, non-empty series")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if n < 2 or sxx == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


# ------------------------------------------------------------------ the sample file

def proc_row(sample, phase, seq, label=None):
    """The raw row of a dvtest.webload.ProcSample; `label` replaces the comm as the child's name (dv-room.01 ...). The
    smaps_rollup values (SMAPS_SERIES, KiB) are added as smaps_<name>_kib only when the proc-stat reply carried them."""
    row = {"kind": "proc", "t": round(sample.t, 3), "seq": seq, "phase": phase, "name": label or sample.comm, "pid": sample.pid,
           "start": sample.starttime, "rss_kib": sample.vmrss_kib, "hwm_kib": sample.vmhwm_kib, "threads": sample.threads,
           "fds": sample.fds, "ticks": sample.ticks}
    for short, value in sorted((getattr(sample, "smaps", None) or {}).items()):         # optional: only when the reply carried them
        row["smaps_%s_kib" % short] = value
    return row


def absent_row(name, t, phase, seq):
    return {"kind": "proc", "t": round(t, 3), "seq": seq, "phase": phase, "name": name, "absent": True}


def counters_row(t, phase, counters, flat=None, series=None, families=None, reasons=None, gauges=None):
    """A counter snapshot: `counters` = the error-like families (summed over labels), `flat` = {family: value or None
    (absent)} of SNAPSHOT_FAMILIES, `series` = their series, `families` = every family name on /metrics (first
    snapshot), `reasons` = {reason: value} of dv_edge_drops_total, `gauges` = {name: {"n", "sum"}} of the non-counter
    families (discovery of the attribution names)."""
    row = {"kind": "metrics", "t": round(t, 3), "phase": phase, "counters": dict(sorted(counters.items()))}
    if flat is not None:
        row["flat"] = dict(sorted(flat.items()))
    if series is not None:
        row["series"] = series
    if families is not None:
        row["families"] = sorted(families)
    if reasons is not None:
        row["reasons"] = dict(sorted(reasons.items()))
    if gauges is not None:
        row["gauges"] = dict(sorted(gauges.items()))
    return row


def attr_row(t, phase, values, series_total, truncated=0):
    """The attribution row of one tick: `values` = {series key: value} (attr_values), `series_total` = the number of
    series of the whole scrape."""
    return {"kind": "attr", "t": round(t, 3), "phase": phase, "series_total": int(series_total), "truncated": int(truncated),
            "values": dict(sorted(values.items()))}


def gauge_row(t, phase, listeners):
    return {"kind": "gauge", "t": round(t, 3), "phase": phase, "listeners": listeners}


def mark_row(t, phase, note):
    return {"kind": "mark", "t": round(t, 3), "phase": phase, "note": note}


def dump_line(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def parse_jsonl(text):
    """(rows, bad): the objects of a soak-samples.jsonl text and the number of lines that were not one."""
    rows, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict) and row.get("kind") in ("proc", "metrics", "gauge", "mark", "attr") and isinstance(row.get("t"), (int, float)):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def error_counters(samples):
    """{family: value summed over all labels} of the error-like families of dvtest.metrics samples."""
    out = {}
    for s in samples:
        if RECORD_PATTERN.search(s.name) and s.value == s.value and abs(s.value) != float("inf"):
            out[s.name] = out.get(s.name, 0.0) + s.value
    return out


def label_key(labels):
    return ",".join("%s=%s" % (k, labels[k]) for k in sorted(labels) if k != "proc") or "-"


def flat_snapshot(samples, families=SNAPSHOT_FAMILIES):
    """(flat, series) of dvtest.metrics samples for the flat families: flat = {family: value summed over all
    series, or None when the family is not on /metrics}, series = {family: {labels without proc: value summed over
    the procs}} (kept to name the series that grew). Any sample of the family, at any value, counts as present."""
    flat, series = {}, {}
    for fam in families:
        rows = [x for x in samples if x.name == fam and x.value == x.value]
        if not rows:
            flat[fam] = None
            continue
        flat[fam] = round(sum(x.value for x in rows), 6)
        per = {}
        for x in rows:
            per[label_key(x.labels)] = per.get(label_key(x.labels), 0.0) + x.value
        series[fam] = dict(sorted(per.items()))
    return flat, series


def drop_reasons(samples, family=DROPS_FAMILY):
    """{reason: value summed over all other labels and procs} of the drop counter; a series without a reason label
    counts under "-" (outside the legit set)."""
    out = {}
    for x in samples:
        if x.name == family and x.value == x.value and abs(x.value) != float("inf"):
            reason = x.labels.get("reason", "-")
            out[reason] = out.get(reason, 0.0) + x.value
    return {k: round(v, 6) for k, v in sorted(out.items())}


_COUNTER_PARTS = re.compile(r"_(total|bucket|sum|count)$")


def gauge_values(samples):
    """{name: {"n": series, "sum": value}} of the families that are not counters or histogram parts: what the case
    keeps at a few snapshots so that the names for ATTR_INCLUDE can be read from a live /metrics."""
    out = {}
    for x in samples:
        if _COUNTER_PARTS.search(x.name) or x.value != x.value or abs(x.value) == float("inf"):
            continue
        rec = out.setdefault(x.name, {"n": 0, "sum": 0.0})
        rec["n"] += 1
        rec["sum"] = round(rec["sum"] + x.value, 6)
    return out


def attr_wanted(name, include=None, exclude=None):
    include = ATTR_INCLUDE if include is None else include
    exclude = ATTR_EXCLUDE if exclude is None else exclude
    return any(re.search(p, name) for p in include) and not any(re.search(p, name) for p in exclude)


def series_key(sample):
    """name{label=value,...} with the labels sorted; the proc label stays (it names the process that reports)."""
    if not sample.labels:
        return sample.name
    return "%s{%s}" % (sample.name, ",".join("%s=%s" % (k, sample.labels[k]) for k in sorted(sample.labels)))


def attr_values(samples, include=None, exclude=None, limit=ATTR_MAX_SERIES):
    """(values, truncated): {series key: value (summed when the key repeats)} of the attributed series of dvtest.metrics
    samples, at most `limit` of them (the rest is counted in `truncated`)."""
    values, truncated = {}, 0
    for x in samples:
        if x.value != x.value or abs(x.value) == float("inf") or not attr_wanted(x.name, include, exclude):
            continue
        key = series_key(x)
        if key not in values and len(values) >= limit:
            truncated += 1
            continue
        values[key] = round(values.get(key, 0.0) + x.value, 6)
    return values, truncated


# ------------------------------------------------------------------ the traffic mix (deterministic from a seed)

def build_overs(seed, lanes, duration_s, storm_every=5):
    """One over of about 10 s per lane and minute. Lanes are spread over the first 45 s of the minute (so that
    neighbours overlap), and every `storm_every`-th minute they all start within a second of each other. Returns
    [{"t", "lane", "dur_s"}] sorted by t; t is seconds after the traffic start; every over ends inside its minute."""
    if not lanes:
        return []
    rng = random.Random("soak-overs/%s" % seed)
    out = []
    for minute in range(int(duration_s // 60)):
        storm = minute % storm_every == storm_every - 1
        for i, lane in enumerate(lanes):
            dur = round(rng.uniform(OVER_S - 1.0, OVER_S + 1.0), 1)
            off = (3.0 + rng.uniform(0.0, 1.0)) if storm else (i * 45.0 / len(lanes) + rng.uniform(0.0, 2.0))
            out.append({"t": round(minute * 60.0 + off, 2), "lane": lane, "dur_s": dur})
    out.sort(key=lambda o: (o["t"], o["lane"]))
    return out


def build_sessions(seed, duration_s, seats=SEATS, churn_per_min=CHURN_PER_MIN, gap_s=(3.0, 10.0)):
    """The listener sessions of the generator: `seats` listeners join at the paced rate of the join budget (the
    W8 offsets), then about `churn_per_min` leave and rejoin on the same seat (so the same login and room) after
    a short gap, the seats taken in a shuffled round. Session = {j, seat, login, account, room, offset, leave}
    (offset and leave in seconds after the start of the run; leave None = stays to the end). A seat holds one
    session at a time, a login owns 8 seats, so a login never holds more than 8 circuits."""
    rng = random.Random("soak-churn/%s" % seed)
    base = wl.slots(seats)
    offsets = wl.paced_offsets(seats, wl.RTC_IP_RATE, wl.RTC_IP_BURST)
    sessions, current = [], {}
    for slot, off in zip(base, offsets):
        row = {"j": len(sessions), "seat": slot.index, "login": slot.login, "account": slot.account, "room": slot.room,
               "offset": round(off, 2), "leave": None}
        sessions.append(row)
        current[slot.index] = row
    if churn_per_min <= 0:
        return sessions
    interval = 60.0 / churn_per_min
    order = list(range(seats))
    rng.shuffle(order)
    t = (offsets[-1] if offsets else 0.0) + 30.0
    k = 0
    while t < duration_s - 30.0:
        seat = order[k % len(order)]
        k += 1
        old = current[seat]
        old["leave"] = round(t, 2)
        row = dict(old, j=len(sessions), offset=round(t + rng.uniform(*gap_s), 2), leave=None)
        sessions.append(row)
        current[seat] = row
        t += interval + rng.uniform(-interval / 4.0, interval / 4.0)
    return sessions


def join_budget_ok(offsets, rate=wl.RTC_IP_RATE, burst=wl.RTC_IP_BURST, reserve=1):
    """True when joins at these (unsorted) offsets never wait for a token of the per-address bucket (offers 1/s,
    burst 5, one token kept back)."""
    now = [0.0]
    bucket = wl.Bucket(rate, burst, reserve=reserve, clock=lambda: now[0])
    for off in sorted(offsets):
        now[0] = off
        if bucket.take() > 0:
            return False
    return True


def validate_sessions(sessions, max_per_login=wl.CIRCUITS_PER_LOGIN):
    """[] when the sessions respect the budgets of the web service, else the list of problems: unique j, a seat
    holds one session at a time, at most `max_per_login` circuits per login at any instant, joins inside the
    per-address offer budget, every session lives at least 20 s."""
    problems = []
    if len({s["j"] for s in sessions}) != len(sessions):
        problems.append("duplicate session numbers")
    events = []
    for s in sessions:
        if s["leave"] is not None and s["leave"] < s["offset"] + 20.0:
            problems.append("session %d lives under 20 s" % s["j"])
        events.append((s["offset"], 1, s["login"], s["seat"]))
        if s["leave"] is not None:
            events.append((s["leave"], -1, s["login"], s["seat"]))
    held, seats = {}, {}
    for t, delta, login, seat in sorted(events, key=lambda e: (e[0], e[1])):          # a leave sorts before a join at the same time
        held[login] = held.get(login, 0) + delta
        seats[seat] = seats.get(seat, 0) + delta
        if held[login] > max_per_login:
            problems.append("login %d holds %d circuits at %.1f s" % (login, held[login], t))
        if seats[seat] > 1:
            problems.append("seat %d holds two sessions at %.1f s" % (seat, t))
    if not join_budget_ok([s["offset"] for s in sessions]):
        problems.append("joins exceed the per-address offer budget")
    return sorted(set(problems))[:10]


def summarize_plan(overs, sessions):
    per_lane = {}
    for o in overs:
        per_lane[o["lane"]] = per_lane.get(o["lane"], 0) + 1
    return {"overs": len(overs), "overs_per_lane": dict(sorted(per_lane.items())), "sessions": len(sessions),
            "leaves": sum(1 for s in sessions if s["leave"] is not None),
            "first_churn_s": min((s["leave"] for s in sessions if s["leave"] is not None), default=None)}


# ------------------------------------------------------------------ the judgement

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    ok: bool = True
    checks: list = field(default_factory=list)
    children: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)
    unmeasured: list = field(default_factory=list)
    warnings: list = field(default_factory=list)            # flagged, not failed
    attribution: dict = field(default_factory=dict)         # recorded series next to the RSS (attribution())

    def add(self, name, ok, detail=""):
        self.checks.append(Check(name, bool(ok), detail))
        if not ok:
            self.ok = False

    def as_dict(self):
        return {"ok": self.ok, "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
                "children": self.children, "counters": self.counters, "unmeasured": self.unmeasured, "warnings": self.warnings,
                "attribution": self.attribution}


def _proc_rows(rows):
    by = {}
    for r in rows:
        if r.get("kind") == "proc" and r.get("name"):
            by.setdefault(r["name"], []).append(r)
    for lst in by.values():
        lst.sort(key=lambda r: (r["t"], r.get("seq", 0)))
    return by


def _present(rows):
    return [r for r in rows if not r.get("absent")]


def _limit(base, fraction, floor_abs):
    return max(base * (1.0 + fraction), base + floor_abs)


def window_medians(rows, key, window_s, min_samples=MEDIAN_MIN_SAMPLES):
    """Medians of rows[key] over the first and the last `window_s` seconds of the rows (time order, each with "t"):
    {"early", "late", "n_early", "n_late"}. A window that holds fewer than `min_samples` rows takes the first / last
    `min_samples` rows instead (a short run with a sample every 15 to 19 s has 2 or 3 rows in 48 s)."""
    pts = [(r["t"], r[key]) for r in rows]
    t0, t1 = pts[0][0], pts[-1][0]
    early = [v for t, v in pts if t <= t0 + window_s]
    late = [v for t, v in pts if t >= t1 - window_s]
    if len(early) < min_samples:
        early = [v for _t, v in pts[:min_samples]]
    if len(late) < min_samples:
        late = [v for _t, v in pts[-min_samples:]]
    return {"early": median(early), "late": median(late), "n_early": len(early), "n_late": len(late)}


def smaps_stats(rows):
    """{series: {"n", "first", "last", "min", "max", "slope_per_min"}} (KiB) of the smaps_<series>_kib values of the
    measured rows of one child, only for the series the rows carry; {} when none do. Recorded, never asserted."""
    out = {}
    for short in SMAPS_SERIES:
        pts = [(r["t"], r["smaps_%s_kib" % short]) for r in rows if isinstance(r.get("smaps_%s_kib" % short), (int, float))]
        if not pts:
            continue
        xs, ys = [t for t, _ in pts], [v for _, v in pts]
        slope, _ = least_squares(xs, ys) if len(pts) > 1 else (0.0, ys[0])
        out[short] = {"n": len(pts), "first": ys[0], "last": ys[-1], "min": min(ys), "max": max(ys), "slope_per_min": round(slope * 60.0, 3)}
    return out


def judge_child(name, rows, params):
    """(record, checks) of one child; `rows` are all its proc rows in time order."""
    checks = []
    p = params
    live = _present(rows)
    rec = {"name": name}
    identities = {(r["pid"], r["start"]) for r in live}
    rec["pid"] = live[0]["pid"] if live else None
    rec["restarts"] = max(0, len(identities) - 1)
    first_seen = rows.index(live[0]) if live else None
    vanished = [r for r in rows[first_seen:] if r.get("absent")] if first_seen is not None else []
    late = bool(live) and live[0]["phase"] not in ("idle0", "warmup")
    rec["vanished_samples"] = len(vanished)
    ok_restart = len(identities) <= 1 and not vanished and not late
    why = []
    if len(identities) > 1:
        why.append("%d identities (pid, start): %s" % (len(identities), sorted(identities)[:4]))
    if vanished:
        why.append("absent in %d samples after it was seen" % len(vanished))
    if late:
        why.append("first seen in phase %s" % live[0]["phase"])
    checks.append(Check("no-restart:" + name, ok_restart, "; ".join(why) or "pid %s start %s" % (rec["pid"], live[0]["start"] if live else "?")))

    warm = [r for r in live if r["phase"] == "warmup"]
    meas = [r for r in live if r["phase"] == "measured"]
    idle0 = [r for r in live if r["phase"] == "idle0"]
    idle = [r for r in live if r["phase"] == "idle"]
    if not warm or len(meas) < p["min_samples"]:
        checks.append(Check("samples:" + name, False, "%d warmup and %d measured samples (need a warmup sample and %d measured)"
                            % (len(warm), len(meas), p["min_samples"])))
        rec["samples"] = len(meas)
        return rec, checks
    rec["samples"] = len(meas)
    base_rss = median([r["rss_kib"] for r in warm[-3:]])
    xs = [r["t"] for r in meas]
    slope, _ = least_squares(xs, [r["rss_kib"] for r in meas])
    span = max(xs) - min(xs)
    # the bound is on the line over the whole measured window (50 minutes in the real run), not on the span of the samples
    window = p["measured_s"] if p.get("measured_s") else span
    growth = slope * window
    limit = max(p["rss_floor_kib"], p["rss_fraction"] * base_rss) * p["rss_scale"]
    judged = bool(p.get("rss_judged", True))
    rec["rss_kib"] = {"start": round(base_rss, 1), "end": median([r["rss_kib"] for r in meas[-3:]]), "max": max(r["rss_kib"] for r in meas),
                      "slope_kib_per_min": round(slope * 60.0, 3), "growth_kib": round(growth, 1), "limit_kib": round(limit, 1),
                      "span_s": round(span, 1), "window_s": round(window, 1), "judged": judged, "over_limit": growth > limit,
                      "hwm": max(r["hwm_kib"] for r in meas)}
    scaled = " (scaled x%.3f for a short measured part)" % p["rss_scale"] if p["rss_scale"] != 1.0 else ""
    lead = "" if judged else "LEAD ONLY, not a verdict (short run, warmup): "
    checks.append(Check("rss:" + name, growth <= limit or not judged, "%sslope %.2f KiB/min x %.0f s = %.0f KiB, limit %.0f KiB%s (start %.0f KiB)"
                        % (lead, slope * 60.0, window, growth, limit, scaled, base_rss)))

    flags = []
    window_s = p.get("median_window_s") or MEDIAN_WINDOW_S * min(1.0, (p.get("measured_s") or span) / MEASURED_S)
    smaps = smaps_stats(meas)
    if smaps:
        rec["smaps"] = smaps

    # threads: median of the last window against the median of the first window of the measured part (not the last sample)
    thr = window_medians(meas, "threads", window_s)
    lim_thr_extra = max(p["threads_fraction"] * thr["early"], p["threads_floor"])
    lim_thr = thr["early"] + lim_thr_extra
    base_thr = median([r["threads"] for r in warm[-3:]])
    old_thr = _limit(base_thr, p["threads_fraction"], p["flag_threads_floor"])
    last_thr = meas[-1]["threads"]
    rec["threads"] = {"start": thr["early"], "end": thr["late"], "max": max(r["threads"] for r in meas), "limit": round(lim_thr, 1),
                      "window_s": round(window_s, 1), "n_early": thr["n_early"], "n_late": thr["n_late"], "warmup_end": base_thr,
                      "last": last_thr, "old_limit": round(old_thr, 1)}
    checks.append(Check("threads:" + name, thr["late"] <= lim_thr,
                        "median of the last %.0f s %.1f (n %d) vs the first %.0f s %.1f (n %d), limit %.1f; max %d, last %d"
                        % (window_s, thr["late"], thr["n_late"], window_s, thr["early"], thr["n_early"], lim_thr, rec["threads"]["max"], last_thr)))
    if last_thr > old_thr:
        flags.append("FLAG threads %s: the last sample is %d, above the old end-sample limit %.1f (end of warmup %.0f); median rule: %.1f vs %.1f, limit %.1f"
                     % (name, last_thr, old_thr, base_thr, thr["late"], thr["early"], lim_thr))

    fds_m = [r for r in meas if r.get("fds", -1) >= 0]
    fds_w = [r["fds"] for r in warm[-3:] if r.get("fds", -1) >= 0]
    if not fds_w or not fds_m:
        rec["fds"] = {"available": False}
        rec["unmeasured"] = "fds unreadable (fds=-1)"
    else:
        fd = window_medians(fds_m, "fds", window_s)
        lim_fds = fd["early"] + max(p["fds_fraction"] * fd["early"], p["fds_floor"])
        base_fds = median(fds_w)
        old_fds = _limit(base_fds, p["fds_fraction"], p["flag_fds_floor"])
        last_fds = fds_m[-1]["fds"]
        rec["fds"] = {"available": True, "start": fd["early"], "end": fd["late"], "max": max(r["fds"] for r in fds_m), "limit": round(lim_fds, 1),
                      "window_s": round(window_s, 1), "n_early": fd["n_early"], "n_late": fd["n_late"], "warmup_end": base_fds,
                      "last": last_fds, "old_limit": round(old_fds, 1)}
        checks.append(Check("fds:" + name, fd["late"] <= lim_fds,
                            "median of the last %.0f s %.1f (n %d) vs the first %.0f s %.1f (n %d), limit %.1f; max %d, last %d"
                            % (window_s, fd["late"], fd["n_late"], window_s, fd["early"], fd["n_early"], lim_fds, rec["fds"]["max"], last_fds)))
        if last_fds > old_fds:
            flags.append("FLAG fds %s: the last sample is %d, above the old end-sample limit %.1f (end of warmup %.0f); median rule: %.1f vs %.1f, limit %.1f"
                         % (name, last_fds, old_fds, base_fds, fd["late"], fd["early"], lim_fds))
        base0 = [r["fds"] for r in idle0 if r.get("fds", -1) >= 0]
        last = [r["fds"] for r in idle if r.get("fds", -1) >= 0]
        if base0 and last:
            idle_limit = base0[-1] + p["idle_fds_slack"]
            rec["fds"].update(idle0=base0[-1], idle_end=last[-1], idle_limit=idle_limit)
            checks.append(Check("fds-idle:" + name, last[-1] <= idle_limit, "after the traffic %d, before it %d, limit %d"
                                % (last[-1], base0[-1], idle_limit)))
        else:
            rec.setdefault("unmeasured", "no idle fds sample (before: %d, after: %d)" % (len(base0), len(last)))
    if flags:
        rec["flags"] = flags
    return rec, checks


def _grown_series(a, b):
    """The series of a family that grew between two snapshots: {labels: delta}."""
    a, b = a or {}, b or {}
    return {k: round(b.get(k, 0.0) - a.get(k, 0.0), 6) for k in sorted(set(a) | set(b)) if b.get(k, 0.0) - a.get(k, 0.0) > 0}


def judge_flat(snapshots, overrides=None, families=FLAT_FAMILIES):
    """(record, checks) of the flat families. `snapshots` = {label: counters row}. Per family: status "absent" (not on
    /metrics at either snapshot: it counts as 0, no assertion, never "must be present"), "flat", "grew" (a check FAILS, naming the family and the delta),
    "override" (grew, but in `overrides`: recorded only), "vanished" (on /metrics at the end of warmup, gone at the
    end: recorded, no assertion). A series that is missing counts as 0, so a family that first shows up after the end
    of warmup (LAZY_FAMILIES are created at their first increment) counts from zero: appearing is growth."""
    overrides = FLAT_OVERRIDES if overrides is None else overrides
    w, e = snapshots.get("warmup_end"), snapshots.get("measured_end")
    if not w or not e or "flat" not in w or "flat" not in e:
        return {}, [Check("counters-flat", False, "no counter snapshot with the flat families at the end of warmup and of the measured part")]
    record, checks = {}, []
    for fam in families:
        a, b = w["flat"].get(fam), e["flat"].get(fam)
        if a is None and b is None:
            record[fam] = {"status": "absent", "start": 0.0, "end": 0.0, "delta": 0.0}
            if fam in LAZY_FAMILIES:
                record[fam]["lazy"] = True
            continue
        if b is None:
            record[fam] = {"status": "vanished", "start": a}
            continue
        start = 0.0 if a is None else a
        delta = round(b - start, 6)
        info = {"status": "flat", "start": start, "end": b, "delta": delta}
        if fam in LAZY_FAMILIES:
            info["lazy"] = True
        if delta > 0:
            info["grown_series"] = _grown_series((w.get("series") or {}).get(fam), (e.get("series") or {}).get(fam))
            if fam in overrides:
                info.update(status="override", reason=str(overrides[fam]))
            else:
                info["status"] = "grew"
                checks.append(Check("flat:" + fam, False, "family %s grew by %g over the measured part (%g -> %g), series %s"
                                    % (fam, delta, start, b, info["grown_series"] or "?")))
        elif delta < 0:
            info["note"] = "went down (counter reset: a restart of its process?)"
        record[fam] = info

    def count(status):
        return sum(1 for v in record.values() if v["status"] == status)

    checks.append(Check("counters-flat", not any(v["status"] == "grew" for v in record.values()),
                        "%d flat, %d absent, %d grew, %d override, %d vanished (of %d families)"
                        % (count("flat"), count("absent"), count("grew"), count("override"), count("vanished"), len(families))))
    return record, checks


def _reason_deltas(a, b):
    """{reason: {"start", "end", "delta"}} between two {reason: value} dicts; a missing reason is 0."""
    a, b = a or {}, b or {}
    return {r: {"start": a.get(r, 0.0), "end": b.get(r, 0.0), "delta": round(b.get(r, 0.0) - a.get(r, 0.0), 6)} for r in sorted(set(a) | set(b))}


def judge_drops(snapshots, legit=DROPS_LEGIT, flag_rate=DROPS_RATE_FLAG_PER_S):
    """(record, checks, warnings) of dv_edge_drops_total, which counts legitimate events of the lane mix and is not
    flat. Per reason, a missing series is 0.
      drops-reasons  measured part (warmup_end -> measured_end): fails only when a reason OUTSIDE `legit` appears or
                     grows; the legit reasons are recorded with delta and rate per second
      drops-idle     idle window (idle_start -> idle_end): no reason of any kind grows
    A warning (a flag in the summary, not a failure) when the legit reasons together run above `flag_rate` per second
    over the measured part."""
    record = {"legit": list(legit), "flag_rate_per_s": flag_rate}
    checks, warnings = [], []
    w, e = snapshots.get("warmup_end"), snapshots.get("measured_end")
    if not w or not e or "reasons" not in w or "reasons" not in e:
        checks.append(Check("drops-reasons", False, "no per-reason snapshot of %s at the end of warmup and of the measured part" % DROPS_FAMILY))
    else:
        span = max(1.0, e["t"] - w["t"])
        per = _reason_deltas(w["reasons"], e["reasons"])
        for reason, info in per.items():
            info["legit"] = reason in legit
            info["rate_per_s"] = round(max(0.0, info["delta"]) / span, 4)
        outside = {r: i["delta"] for r, i in per.items() if not i["legit"] and i["delta"] > 0}
        total = sum(max(0.0, i["delta"]) for r, i in per.items() if i["legit"])
        rate = total / span
        record.update(reasons=per, window_s=round(span, 1), legit_total=total, legit_rate_per_s=round(rate, 4), outside=outside)
        legit_text = ", ".join("%s %g (%.3f/s)" % (r, i["delta"], i["rate_per_s"]) for r, i in per.items() if i["legit"] and i["delta"] > 0) or "none grew"
        checks.append(Check("drops-reasons", not outside,
                            ("reasons outside %s appeared or grew over the measured part: %s" % (list(legit), outside)) if outside
                            else "legit reasons over %.0f s: %s; total %.3f/s" % (span, legit_text, rate)))
        if rate > flag_rate:
            record["flagged"] = True
            warnings.append("FLAG %s: the legit reasons run at %.2f/s over the measured part (flag above %g/s): %s"
                            % (DROPS_FAMILY, rate, flag_rate, legit_text))
    i0, i1 = snapshots.get("idle_start"), snapshots.get("idle_end")
    if not i0 or not i1 or "reasons" not in i0 or "reasons" not in i1:
        checks.append(Check("drops-idle", False, "no per-reason snapshot of %s at the start and the end of the idle window" % DROPS_FAMILY))
    else:
        grown = {r: i["delta"] for r, i in _reason_deltas(i0["reasons"], i1["reasons"]).items() if i["delta"] > 0}
        record["idle"] = grown
        checks.append(Check("drops-idle", not grown, ("%s grew while idle: %s" % (DROPS_FAMILY, grown)) if grown
                            else "no reason of %s grew while idle" % DROPS_FAMILY))
    return record, checks, warnings


def judge_counters(snapshots, overrides=None):
    """(record, checks) of the counters. `snapshots` = {label: counters row} with the labels idle0, warmup_end,
    measured_end, idle_start, idle_end (missing ones are skipped). The flat families must not grow over the measured
    part (judge_flat); dv_edge_drops_total is judged by reason (judge_drops); every error-like family is recorded with
    its deltas over the measured part, the run and the idle window, and none of those is asserted. record["warnings"]
    holds the flags."""
    def delta(a, b):
        sa, sb = (snapshots.get(a) or {}).get("counters"), (snapshots.get(b) or {}).get("counters")
        if sa is None or sb is None:
            return None
        return {k: round(sb.get(k, 0.0) - sa.get(k, 0.0), 3) for k in sorted(set(sa) | set(sb))}
    record = {"measured": delta("warmup_end", "measured_end"), "run": delta("idle0", "idle_end"), "idle": delta("idle_start", "idle_end")}
    flat, checks = judge_flat(snapshots, overrides)
    record["flat"] = flat
    record["warnings"] = []
    if flat:                                                  # the snapshots exist
        drops, dchecks, warnings = judge_drops(snapshots)
        record["drops"] = drops
        record["warnings"] = warnings
        checks.extend(dchecks)
    record["growing"] = sorted(k for k, v in (record["measured"] or {}).items() if v > 0 and k not in SNAPSHOT_FAMILIES)
    return record, checks


def attribution(rows):
    """The recorded attribution series next to the RSS, from the "attr" rows of the sample file: {"series_total":
    stats of the number of series of the scrape, "series": {key: stats}, "raw": {key: [[seconds since the first row, value], ...]},
    "scrape": [[t, series_total], ...]}. stats = {n, first, last, min, max, slope_per_min} over the measured phase
    (least squares), recorded, never asserted. Keys are sorted by |slope| in `order`."""
    attr = sorted((r for r in rows if r.get("kind") == "attr"), key=lambda r: r["t"])
    if not attr:
        return {}
    t0 = attr[0]["t"]
    raw, scrape = {}, []
    for r in attr:
        scrape.append([round(r["t"] - t0, 1), r.get("series_total", 0)])
        for key, value in (r.get("values") or {}).items():
            raw.setdefault(key, []).append([round(r["t"] - t0, 1), value, r.get("phase")])

    def stats(points):
        meas = [(t, v) for t, v, ph in points if ph == "measured"]
        if not meas:
            return {"n": 0}
        xs, ys = [t for t, _ in meas], [v for _, v in meas]
        slope, _ = least_squares(xs, ys) if len(meas) > 1 else (0.0, ys[0])
        return {"n": len(meas), "first": ys[0], "last": ys[-1], "min": min(ys), "max": max(ys), "slope_per_min": round(slope * 60.0, 4)}

    scrape_pts = [(t, v, r.get("phase")) for (t, v), r in zip(scrape, attr)]
    series = {key: stats(points) for key, points in sorted(raw.items())}
    series_total = stats(scrape_pts)
    order = sorted(series, key=lambda k: (-abs(series[k].get("slope_per_min", 0.0)), k))
    return {"series_total": series_total, "series": series, "order": order, "scrape": scrape,
            "raw": {k: [[t, v] for t, v, _ph in pts] for k, pts in raw.items()},
            "truncated": max((r.get("truncated", 0) for r in attr), default=0)}


def generator_summary(rows, min_packets=50, failed_fraction=0.02):
    """(summary, checks) of the finish document rows of the load generator in soak mode: how many sessions
    connected, failed or left, the answers of the lease renewals, and whether the listeners of each room heard
    audio (the protocol talkers of the rooms are only seen through these listeners)."""
    total = len(rows)
    failed = [r for r in rows if r.get("error")]
    alive, rooms = {}, {}
    for r in rows:
        for status, n in (r.get("alive") or {}).items():
            alive[status] = alive.get(status, 0) + n
        room = rooms.setdefault(r.get("room"), {"sessions": 0, "heard": 0, "packets": 0})
        room["sessions"] += 1
        room["packets"] += r.get("packets", 0)
        room["heard"] += 1 if r.get("packets", 0) >= min_packets else 0
    summary = {"sessions": total, "connected": sum(1 for r in rows if r.get("connect_s") is not None), "failed": len(failed),
               "left": sum(1 for r in rows if r.get("left")), "alive": dict(sorted(alive.items())),
               "errors": sorted({str(r["error"])[:100] for r in failed})[:5], "rooms": rooms,
               "invalid_packets": sum(r.get("invalid", 0) for r in rows)}
    refused = {k: v for k, v in alive.items() if k not in ("204", "200")}
    checks = [
        Check("generator-sessions", total > 0 and len(failed) <= max(2, failed_fraction * total),
              "%d sessions, %d failed %s" % (total, len(failed), summary["errors"][:2])),
        Check("generator-leases", sum(refused.values()) <= max(2, failed_fraction * sum(alive.values())),
              "lease answers other than 204: %s of %s" % (refused or "none", sum(alive.values()))),
    ]
    for room, info in sorted(rooms.items(), key=lambda kv: str(kv[0])):
        checks.append(Check("heard:room-%s" % room, info["heard"] > 0, "%d of %d sessions heard >= %d packets (%d in all)"
                            % (info["heard"], info["sessions"], min_packets, info["packets"])))
    return summary, checks


def assign_phases(rows):
    """The rows with their phase corrected by the counter snapshots. A sampling tick takes ~19 s (23 helper containers) and
    carries the phase it started in, so a tick that began before the end of warmup or of the measured part can end after it
    (the listeners are gone by then: the edge RSS drops). A proc, gauge or attr row of phase warmup written after the
    warmup_end snapshot counts as measured; one of phase measured written after the measured_end snapshot counts as stopping."""
    snap = {r["phase"]: r["t"] for r in rows if r.get("kind") == "metrics" and r.get("phase") in ("warmup_end", "measured_end")}
    if not snap:
        return rows
    out = []
    for r in rows:
        if r.get("kind") in ("proc", "gauge", "attr"):
            if r.get("phase") == "measured" and "measured_end" in snap and r["t"] > snap["measured_end"]:
                r = dict(r, phase="stopping")
            elif r.get("phase") == "warmup" and "warmup_end" in snap and r["t"] > snap["warmup_end"]:
                r = dict(r, phase="measured")
        out.append(r)
    return out


def default_params():
    return {"min_samples": MIN_SAMPLES, "rss_floor_kib": RSS_FLOOR_KIB, "rss_fraction": RSS_FRACTION,
            "threads_fraction": THREADS_FRACTION, "threads_floor": THREADS_FLOOR, "fds_fraction": FDS_FRACTION,
            "fds_floor": FDS_FLOOR, "flag_threads_floor": FLAG_THREADS_FLOOR, "flag_fds_floor": FLAG_FDS_FLOOR,
            "median_window_s": None, "idle_fds_slack": IDLE_FDS_SLACK, "rss_scale": 1.0, "flat_overrides": None,
            "rss_judged": True, "measured_s": None}


def evaluate(rows, events=None, params=None, expected=()):
    """The verdict of a run from its raw rows (parse_jsonl). `events` = the dict of dvtest.k22.idle_stream_verdict
    for the idle sample (None: not sampled, a failed check). `expected` = child names that must be present (the
    children the case listed at the start). Returns a Result."""
    p = default_params()
    p.update(params or {})
    res = Result()
    rows = assign_phases(rows)
    by = _proc_rows(rows)
    for name in expected:
        # a name several processes carry (dv-room) is sampled as dv-room.01, dv-room.02, ...
        if not any(_present(rows_) for key, rows_ in by.items() if key == name or key.startswith(name + ".")):
            res.add("present:" + name, False, "never sampled although the container listed it")
    for name in sorted(by):
        if not _present(by[name]):
            continue                                    # allow-listed but not running in this rig (docs/web-soak-cases.md)
        rec, checks = judge_child(name, by[name], p)
        res.children[name] = rec
        res.checks.extend(checks)
        if rec.get("unmeasured"):
            res.unmeasured.append("%s: %s" % (name, rec["unmeasured"]))
    if not res.children:
        res.add("children", False, "no child process was sampled")
    snaps = {}
    for r in rows:
        if r.get("kind") == "metrics" and r.get("phase") in ("idle0", "warmup_end", "measured_end", "idle_start", "idle_end"):
            snaps[r["phase"]] = r
    rec, checks = judge_counters(snaps, p.get("flat_overrides"))
    res.counters = rec
    res.checks.extend(checks)
    res.warnings.extend(rec.get("warnings", []))
    for name, c in sorted(res.children.items()):
        r = c.get("rss_kib") or {}
        if r.get("over_limit") and not r.get("judged", True):
            res.warnings.append("RSS lead %s: slope %.1f KiB/min, growth %.0f KiB over the window, limit %.0f KiB (short run: not a verdict)"
                                % (name, r["slope_kib_per_min"], r["growth_kib"], r["limit_kib"]))
    for name, c in sorted(res.children.items()):
        res.warnings.extend(c.get("flags", []))
    res.attribution = attribution(rows)
    if events is None:
        res.add("events-quiet", False, "the public event stream was not sampled while idle")
    else:
        res.add("events-quiet", bool(events.get("ok")), "worst minute %s, %s events after the first minute"
                % (events.get("worst_minute"), events.get("after_warmup")))
    idle_gauge = [r["listeners"] for r in rows if r.get("kind") == "gauge" and r.get("phase") == "idle"]
    res.add("idle-listeners-zero", bool(idle_gauge) and max(idle_gauge) == 0,
            "listener gauge while idle: %s" % (sorted(set(idle_gauge)) if idle_gauge else "no reading"))
    for c in res.checks:
        if not c.ok:
            res.ok = False
    return res


def table(result):
    """Plain-text table, one row per child (what the case prints and the doc shows)."""
    head = "%-16s %9s %9s %11s %7s %7s %7s %7s %7s %7s" % ("child", "rss0 KiB", "rssN KiB", "slope/min", "thr0", "thrN", "fds0", "fdsN", "fdsIdle", "restart")
    lines = [head]
    for name, c in sorted(result.children.items()):
        rss, thr, fds = c.get("rss_kib") or {}, c.get("threads") or {}, c.get("fds") or {}
        lines.append("%-16s %9s %9s %11s %7s %7s %7s %7s %7s %7s" % (
            name, _f(rss.get("start")), _f(rss.get("end")), _f(rss.get("slope_kib_per_min")), _f(thr.get("start")), _f(thr.get("end")),
            _f(fds.get("start")), _f(fds.get("end")), _f(fds.get("idle_end")), c.get("restarts", "?")))
    return "\n".join(lines)


def counters_table(result):
    """Plain-text table of the flat families (what the case prints and the doc shows)."""
    lines = ["%-42s %-9s %12s %12s %9s" % ("flat family", "status", "warmup end", "end", "delta")]
    for fam, info in sorted((result.counters.get("flat") or {}).items()):
        lines.append("%-42s %-9s %12s %12s %9s" % (fam, info["status"], _f(info.get("start")), _f(info.get("end")), _f(info.get("delta"))))
    drops = result.counters.get("drops") or {}
    if drops.get("reasons") is not None:
        lines.append("")
        lines.append("%-42s %-9s %12s %12s %9s %9s" % (DROPS_FAMILY + " reason", "legit", "warmup end", "end", "delta", "per s"))
        for reason, info in drops["reasons"].items():
            if not info["start"] and not info["end"]:
                continue                                  # a reason that never counted (all of them are in soak.json)
            lines.append("%-42s %-9s %12s %12s %9s %9.3f" % (reason, "yes" if info["legit"] else "NO", _f(info["start"]), _f(info["end"]),
                                                              _f(info["delta"]), info["rate_per_s"]))
        lines.append("legit total %.3f/s over %.0f s (flag above %g/s)%s" % (drops["legit_rate_per_s"], drops["window_s"], drops["flag_rate_per_s"],
                                                                         ", FLAGGED" if drops.get("flagged") else ""))
    return "\n".join(lines)


def attribution_table(result, top=25):
    """Plain-text table of the attribution series (recorded, not asserted): the number of series of the scrape and,
    per series that moved, first and last value and the least-squares slope per minute over the measured part, the
    largest slopes first; the series that did not move are counted, not listed (all of them are in soak.json)."""
    att = result.attribution or {}
    if not att:
        return "attribution: no series recorded"
    lines = ["%-64s %12s %12s %12s" % ("attribution series (measured part)", "first", "last", "slope/min")]
    st = att["series_total"]
    lines.append("%-64s %12s %12s %12s" % (ATTR_KEY_SERIES_TOTAL, _f(st.get("first")), _f(st.get("last")), _f(st.get("slope_per_min"))))
    moved = [k for k in att["order"] if att["series"][k].get("n") and att["series"][k]["first"] != att["series"][k]["last"]]
    for k in moved[:top]:
        v = att["series"][k]
        lines.append("%-64s %12s %12s %12s" % (k[:64], _f(v["first"]), _f(v["last"]), _f(v["slope_per_min"])))
    lines.append("(%d series recorded, %d moved%s, %d unchanged; truncated %d)" % (len(att["series"]), len(moved),
                 ", top %d listed" % top if len(moved) > top else "", len(att["series"]) - len(moved), att.get("truncated", 0)))
    return "\n".join(lines)


def smaps_table(result):
    """Plain-text table of the smaps_rollup values (KiB) of the children whose proc-stat replies carried them: first, last
    and the least-squares slope per minute over the measured part (recorded, not asserted). One line when none did."""
    rows = [(name, c["smaps"]) for name, c in sorted(result.children.items()) if c.get("smaps")]
    if not rows:
        return "smaps_rollup: not in the proc-stat replies (scripts/lib/procstat.sh without smaps keys)"
    lines = ["%-16s %-14s %10s %10s %11s" % ("smaps (KiB)", "series", "first", "last", "slope/min")]
    for name, ser in rows:
        for short in SMAPS_SERIES:
            if short in ser:
                v = ser[short]
                lines.append("%-16s %-14s %10s %10s %11s" % (name, short, _f(v["first"]), _f(v["last"]), _f(v["slope_per_min"])))
    return "\n".join(lines)


def settle_observed(rows):
    """Seconds between the mark "listeners zero" (the last client is gone) and the mark that opens the idle window, from
    the mark rows of a sample file; None when either is missing. The case waits SETTLE_S in between (plus the counter
    snapshot), so this is what a finished run really had: it must be >= SETTLE_MIN_S."""
    zero = [r["t"] for r in rows if r.get("kind") == "mark" and str(r.get("note", "")).startswith("listeners zero")]
    start = [r["t"] for r in rows if r.get("kind") == "mark" and r.get("phase") == "idle" and r.get("note") == "start"]
    if not zero or not start or start[-1] < zero[-1]:
        return None
    return start[-1] - zero[-1]


def _f(value):
    if value is None:
        return "-"
    return ("%d" % value) if float(value).is_integer() else ("%.1f" % value)


# ------------------------------------------------------------------ offline re-judgement of a finished run

def find_samples(path):
    """The soak-samples.jsonl of a run: `path` may be the file, the web directory, or the run directory (out/<utc>)."""
    from pathlib import Path
    p = Path(path)
    for cand in (p, p / "soak-samples.jsonl", p / "web" / "soak-samples.jsonl"):
        if cand.is_file():
            return cand
    raise FileNotFoundError("no soak-samples.jsonl under %s" % path)


def params_of_run(saved, override=None):
    """Judgement params of a finished run from its soak.json (`saved`, may be {}): the measured part, the RSS scale and
    whether the RSS slope is a verdict (not in a short run), like the case passes them; `override` {measured_s,
    short} wins. A run without soak.json is judged as the real run."""
    override = override or {}
    measured = override.get("measured_s") or saved.get("measured_s") or MEASURED_S
    short = override["short"] if override.get("short") is not None else bool(saved.get("short"))
    scale = min(1.0, measured / MEASURED_S)
    return {"rss_scale": scale, "rss_judged": not short, "measured_s": measured, "median_window_s": MEDIAN_WINDOW_S * scale}


CHILD_CHECKS = ("no-restart", "rss", "threads", "fds", "fds-idle", "samples")


def rejudge(path, override=None, verbose=False):
    """(report text, ok) of a finished run re-evaluated with the current rules from its saved samples. The file's
    soak.json (next to the samples) supplies the timing, the children, the idle event verdict and the checks of the
    parts that are not rejudged (generator, traffic mix, sampling, drained), which are shown as saved."""
    from pathlib import Path
    samples = find_samples(path)
    saved = {}
    sj = samples.parent / "soak.json"
    if sj.is_file():
        try:
            saved = json.loads(sj.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
    rows, bad = parse_jsonl(samples.read_text(encoding="utf-8"))
    params = params_of_run(saved, override)
    res = evaluate(rows, events=saved.get("events"), params=params, expected=saved.get("children") or ())
    out = ["run: %s (%d rows, %d damaged lines); measured %g s, median window %g s, short %s"
           % (Path(samples).resolve().parent, len(rows), bad, params["measured_s"], params["median_window_s"], not params["rss_judged"]),
           "", table(res), "", counters_table(res), "", attribution_table(res), "", smaps_table(res), ""]
    for w in res.warnings:
        out.append("WARN %s" % w)
    quiet = 0
    for c in res.checks:
        if c.ok and not verbose and c.name.split(":")[0] in CHILD_CHECKS:
            quiet += 1                                          # 5 lines per child (26 rooms): only the failing ones are listed
            continue
        out.append("%-4s %-34s %s" % ("ok" if c.ok else "FAIL", c.name, c.detail))
    if quiet:
        out.append("ok   (%d passing per-child checks not listed: %s; --all lists them)" % (quiet, ", ".join(CHILD_CHECKS)))
    extra = saved.get("extra_checks") or []
    for c in extra:
        out.append("%-4s %-34s %s" % ("ok" if c.get("ok") else "FAIL", c.get("name"), "(as saved) " + str(c.get("detail"))))
    settle = settle_observed(rows)
    if settle is not None:
        out.append("")
        out.append("NOTE settle before the idle window in this run: %.0f s (rule: >= %d s)%s" % (
            settle, SETTLE_MIN_S, "" if settle >= SETTLE_MIN_S else "; drops-idle was measured on a window that started too early, "
                                                                    "so it cannot be re-judged offline"))
    old = {c["name"] for c in (saved.get("result") or {}).get("checks", []) if not c.get("ok")}
    new = {c.name for c in res.checks if not c.ok}
    if saved.get("result"):
        out.append("failed before: %s" % (sorted(old) or "none"))
        out.append("failed now:    %s" % (sorted(new) or "none"))
        out.append("changed:       fixed %s, new %s" % (sorted(old - new) or "none", sorted(new - old) or "none"))
    failed = sorted(new | {c.get("name") for c in extra if not c.get("ok")})
    out.append("")
    out.append("VERDICT: %s%s" % ("PASS" if not failed else "FAIL", "" if not failed else " (%s)" % ", ".join(failed)))
    return "\n".join(out), not failed


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.soak", description="Re-judge a finished SOAK1 run offline from out/<run>/web/soak-samples.jsonl "
                                 "(read only): prints the per child table, the counters, the checks and the verdict of the current rules.")
    ap.add_argument("path", help="run directory (out/<utc>), its web directory, or soak-samples.jsonl")
    ap.add_argument("--measured-s", type=float, help="length of the measured part when there is no soak.json")
    ap.add_argument("--all", action="store_true", help="list every passing per-child check too")
    ap.add_argument("--short", action="store_true", help="treat the run as a short run (RSS is a lead) when there is no soak.json")
    args = ap.parse_args(argv)
    text, ok = rejudge(args.path, {"measured_s": args.measured_s, "short": True if args.short else None}, verbose=args.all)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
