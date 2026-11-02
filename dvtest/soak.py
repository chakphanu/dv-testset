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
     least-squares slope x the measured span (not on the last sample)
  3  threads and fds at the end of the measured part <= max(end of warmup + 10 percent, +2 threads, +4 fds) over
     the end-of-warmup value (the floor applies next to the 10 percent rule: 10 percent of 8 threads is 0.8); after
     all traffic stopped the fds are within +5 of the idle value taken before any traffic (rulings of DV-Router,
     2026-11-02)
  4  the counters of FLAT_FAMILIES are FLAT over the measured part (value at the end of warmup == value at the
     end, absolute zero growth); a family that is not on /metrics is recorded as absent, a family that grows and
     that DV-Router ruled on goes into FLAT_OVERRIDES (starts empty) and is recorded with its growth; every
     other error-like family is recorded only
  5  the public event stream is quiet while idle (dvtest.k22.idle_stream_verdict, judged by the case)
"""
import json
import random
import re
from dataclasses import dataclass, field

from . import webload as wl

WARMUP_S = 600.0
MEASURED_S = 3000.0
SAMPLE_S = 30.0
IDLE_S = 120.0
WALL_CAP_S = 75 * 60.0
SEATS = 100                     # receive-only WebRTC listeners held at any time
CHURN_PER_MIN = 5
OVER_S = 10.0

RSS_FLOOR_KIB = 5 * 1024
RSS_FRACTION = 0.10
THREADS_FRACTION = 0.10
THREADS_FLOOR = 2
FDS_FRACTION = 0.10
FDS_FLOOR = 4
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
FLAT_FAMILIES = (
    "dv_edge_webrtc_panics_total", "dv_edge_drops_total", "dv_edge_queue_drops_total", "dv_edge_webrtc_audio_dropped_total",
    "dv_room_ingress_rejected_total", "dv_room_shell_errors_total", "dv_status_telemetry_rejected_total",
    "dv_status_input_dropped_total", "dv_fabric_put_dropped_total", "dv_recorder_dropped_total", "dv_codec_dropped_total",
    "dv_edge_tx_refused_total",
)
# Per-family overrides, {family: reason}: a family that grows with the traffic mix is NOT asserted but recorded with
# its name and growth, until DV-Router rules on it. Starts empty on purpose.
FLAT_OVERRIDES = {}


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
    failure of the RSS check in a short run is a lead, not a verdict. ValueError for a bad number or fewer than 6
    samples in the measured part (a slope needs MIN_SAMPLES)."""
    short = (env.get("DVT_SOAK_SHORT") or "").strip() == "1"
    warmup = _env_float(env, "DVT_SOAK_WARMUP_S")
    measured = _env_float(env, "DVT_SOAK_MEASURE_S", "DVT_SOAK_MEASURED_S")
    sample = _env_float(env, "DVT_SOAK_SAMPLE_S")
    warmup = warmup if warmup is not None else (SHORT_WARMUP_S if short else WARMUP_S)
    measured = measured if measured is not None else (SHORT_MEASURED_S if short else MEASURED_S)
    sample = sample if sample is not None else (SHORT_SAMPLE_S if short else SAMPLE_S)
    if sample < 0.05 or measured < (MIN_SAMPLES + 1) * sample:
        raise ValueError("measured %g s with a sample every %g s gives fewer than %d samples" % (measured, sample, MIN_SAMPLES + 1))
    smoke = (warmup, measured) != (WARMUP_S, MEASURED_S)
    return {"warmup_s": warmup, "measured_s": measured, "sample_s": sample, "short": short, "smoke": smoke,
            "rss_scale": min(1.0, measured / MEASURED_S)}


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
    """The raw row of a dvtest.webload.ProcSample; `label` replaces the comm as the child's name (dv-room.01 ...)."""
    return {"kind": "proc", "t": round(sample.t, 3), "seq": seq, "phase": phase, "name": label or sample.comm, "pid": sample.pid,
            "start": sample.starttime, "rss_kib": sample.vmrss_kib, "hwm_kib": sample.vmhwm_kib, "threads": sample.threads,
            "fds": sample.fds, "ticks": sample.ticks}


def absent_row(name, t, phase, seq):
    return {"kind": "proc", "t": round(t, 3), "seq": seq, "phase": phase, "name": name, "absent": True}


def counters_row(t, phase, counters, flat=None, series=None, families=None):
    """A counter snapshot: `counters` = the error-like families (summed over labels), `flat` = {family: value or None
    (absent)} of FLAT_FAMILIES, `series` = their series, `families` = every family name on /metrics (first snapshot)."""
    row = {"kind": "metrics", "t": round(t, 3), "phase": phase, "counters": dict(sorted(counters.items()))}
    if flat is not None:
        row["flat"] = dict(sorted(flat.items()))
    if series is not None:
        row["series"] = series
    if families is not None:
        row["families"] = sorted(families)
    return row


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
        if isinstance(row, dict) and row.get("kind") in ("proc", "metrics", "gauge", "mark") and isinstance(row.get("t"), (int, float)):
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


def flat_snapshot(samples, families=FLAT_FAMILIES):
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

    def add(self, name, ok, detail=""):
        self.checks.append(Check(name, bool(ok), detail))
        if not ok:
            self.ok = False

    def as_dict(self):
        return {"ok": self.ok, "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
                "children": self.children, "counters": self.counters, "unmeasured": self.unmeasured}


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
    growth = slope * span
    limit = max(p["rss_floor_kib"], p["rss_fraction"] * base_rss) * p["rss_scale"]
    rec["rss_kib"] = {"start": round(base_rss, 1), "end": median([r["rss_kib"] for r in meas[-3:]]), "max": max(r["rss_kib"] for r in meas),
                      "slope_kib_per_min": round(slope * 60.0, 3), "growth_kib": round(growth, 1), "limit_kib": round(limit, 1),
                      "span_s": round(span, 1), "hwm": max(r["hwm_kib"] for r in meas)}
    scaled = " (scaled x%.3f for a short measured part)" % p["rss_scale"] if p["rss_scale"] != 1.0 else ""
    checks.append(Check("rss:" + name, growth <= limit, "slope %.2f KiB/min x %.0f s = %.0f KiB, limit %.0f KiB%s (start %.0f KiB)"
                        % (slope * 60.0, span, growth, limit, scaled, base_rss)))

    base_thr = median([r["threads"] for r in warm[-3:]])
    end_thr = median([r["threads"] for r in meas[-5:]])
    lim_thr = _limit(base_thr, p["threads_fraction"], p["threads_floor"])
    rec["threads"] = {"start": base_thr, "end": end_thr, "max": max(r["threads"] for r in meas), "limit": round(lim_thr, 1)}
    checks.append(Check("threads:" + name, end_thr <= lim_thr, "end %.0f, start %.0f, limit %.1f" % (end_thr, base_thr, lim_thr)))

    fds_w = [r["fds"] for r in warm[-3:] if r.get("fds", -1) >= 0]
    fds_m = [r["fds"] for r in meas if r.get("fds", -1) >= 0]
    if not fds_w or not fds_m:
        rec["fds"] = {"available": False}
        rec["unmeasured"] = "fds unreadable (fds=-1)"
    else:
        base_fds = median(fds_w)
        end_fds = median(fds_m[-5:])
        lim_fds = _limit(base_fds, p["fds_fraction"], p["fds_floor"])
        rec["fds"] = {"available": True, "start": base_fds, "end": end_fds, "max": max(fds_m), "limit": round(lim_fds, 1)}
        checks.append(Check("fds:" + name, end_fds <= lim_fds, "end %.0f, start %.0f, limit %.1f" % (end_fds, base_fds, lim_fds)))
        base0 = [r["fds"] for r in idle0 if r.get("fds", -1) >= 0]
        last = [r["fds"] for r in idle if r.get("fds", -1) >= 0]
        if base0 and last:
            idle_limit = base0[-1] + p["idle_fds_slack"]
            rec["fds"].update(idle0=base0[-1], idle_end=last[-1], idle_limit=idle_limit)
            checks.append(Check("fds-idle:" + name, last[-1] <= idle_limit, "after the traffic %d, before it %d, limit %d"
                                % (last[-1], base0[-1], idle_limit)))
        else:
            rec.setdefault("unmeasured", "no idle fds sample (before: %d, after: %d)" % (len(base0), len(last)))
    return rec, checks


def _grown_series(a, b):
    """The series of a family that grew between two snapshots: {labels: delta}."""
    a, b = a or {}, b or {}
    return {k: round(b.get(k, 0.0) - a.get(k, 0.0), 6) for k in sorted(set(a) | set(b)) if b.get(k, 0.0) - a.get(k, 0.0) > 0}


def judge_flat(snapshots, overrides=None, families=FLAT_FAMILIES):
    """(record, checks) of the flat families. `snapshots` = {label: counters row}. Per family: status "absent" (not on
    /metrics at either snapshot: no assertion), "flat", "grew" (a check FAILS, naming the family and the delta),
    "override" (grew, but in `overrides`: recorded only), "vanished" (on /metrics at the end of warmup, gone at the
    end: recorded, no assertion). A family that first shows up after the end of warmup counts from zero."""
    overrides = FLAT_OVERRIDES if overrides is None else overrides
    w, e = snapshots.get("warmup_end"), snapshots.get("measured_end")
    if not w or not e or "flat" not in w or "flat" not in e:
        return {}, [Check("counters-flat", False, "no counter snapshot with the flat families at the end of warmup and of the measured part")]
    record, checks = {}, []
    for fam in families:
        a, b = w["flat"].get(fam), e["flat"].get(fam)
        if a is None and b is None:
            record[fam] = {"status": "absent"}
            continue
        if b is None:
            record[fam] = {"status": "vanished", "start": a}
            continue
        start = 0.0 if a is None else a
        delta = round(b - start, 6)
        info = {"status": "flat", "start": start, "end": b, "delta": delta}
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


def judge_counters(snapshots, overrides=None):
    """(record, checks) of the counters. `snapshots` = {label: counters row} with the labels idle0, warmup_end,
    measured_end, idle_start, idle_end (missing ones are skipped). The flat families must not grow over the measured
    part (judge_flat); every error-like family is recorded with its deltas over the measured part, the run and the
    idle window, and none of those is asserted."""
    def delta(a, b):
        sa, sb = (snapshots.get(a) or {}).get("counters"), (snapshots.get(b) or {}).get("counters")
        if sa is None or sb is None:
            return None
        return {k: round(sb.get(k, 0.0) - sa.get(k, 0.0), 3) for k in sorted(set(sa) | set(sb))}
    record = {"measured": delta("warmup_end", "measured_end"), "run": delta("idle0", "idle_end"), "idle": delta("idle_start", "idle_end")}
    flat, checks = judge_flat(snapshots, overrides)
    record["flat"] = flat
    record["growing"] = sorted(k for k, v in (record["measured"] or {}).items() if v > 0 and k not in FLAT_FAMILIES)
    return record, checks


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


def default_params():
    return {"min_samples": MIN_SAMPLES, "rss_floor_kib": RSS_FLOOR_KIB, "rss_fraction": RSS_FRACTION,
            "threads_fraction": THREADS_FRACTION, "threads_floor": THREADS_FLOOR, "fds_fraction": FDS_FRACTION,
            "fds_floor": FDS_FLOOR, "idle_fds_slack": IDLE_FDS_SLACK, "rss_scale": 1.0, "flat_overrides": None}


def evaluate(rows, events=None, params=None, expected=()):
    """The verdict of a run from its raw rows (parse_jsonl). `events` = the dict of dvtest.k22.idle_stream_verdict
    for the idle sample (None: not sampled, a failed check). `expected` = child names that must be present (the
    children the case listed at the start). Returns a Result."""
    p = default_params()
    p.update(params or {})
    res = Result()
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
    return "\n".join(lines)


def _f(value):
    if value is None:
        return "-"
    return ("%d" % value) if float(value).is_integer() else ("%.1f" % value)
