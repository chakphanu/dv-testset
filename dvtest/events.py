# SPDX-License-Identifier: MIT
"""Pure parts of the hub-events and streams record of SOAK1 and the RSS isolation experiment (docs/web-soak-cases.md,
docs/web-isolation-cases.md). No I/O, no clock; the reader thread that feeds them is cases/soak_support.EventsReader.

DV-Router hypothesis: the RSS growth of dv-svc-status (about 113 KiB/min in SOAK1) may be the fill of its events ring
(10,000 events of about 1.16 KiB each) that had not filled in 50 minutes, not a leak; dv-router and dv-room may grow
once per distinct stream (a new Zenoh liveliness token key per over). So both cases record, never assert:

  EventTally            the events of the public stream: arrival time stamps, names per kind, reconnect gaps
  bin_per_minute()      counts per minute from a start time
  hub_events_block()    events per minute, the estimate events/min x 1.16 KiB next to the measured slopes, the ring note
  streams_block()       overs delivered per lane / module / room, streams per minute, KiB/min per stream of the focus children
  events_metric_*()     any hub /metrics family with "event" in its name (none in the SOAK1 image of 2026-11-02)
  slopes_of()           (vmrss, rss, anon) slope per minute of a child record of soak.json or isolation-<mode>.json
"""
import re
import threading
from collections import deque

RING = 10000                    # events of the ring of dv-svc-status
EVENT_KIB = 1.16                # about 1.16 KiB per event (DV-Router)
MATCH_LOW, MATCH_HIGH = 0.7, 1.4  # ratio estimate / measured slope that counts as "matches"
SEEN_LIMIT = 20000              # event ids kept to drop a replay after a reconnect
ROOM_CHILD = "dv-room"
FOCUS_CHILDREN = ("dv-router", "dv-svc-status", ROOM_CHILD)


# ------------------------------------------------------------------ per minute

def bin_per_minute(times, t0, t1):
    """Counts per minute of the time stamps in [t0, t1] (bin i = [t0 + 60 i, t0 + 60 (i+1)), a partial last minute is its own bin, a
    stamp at t1 goes to the last bin). Stamps outside the interval are not counted."""
    span = max(0.0, t1 - t0)
    n = max(1, int(span // 60) + (1 if span % 60 else 0))
    counts = [0] * n
    for t in times:
        if t < t0 or t > t1:
            continue
        counts[min(int((t - t0) // 60), n - 1)] += 1
    return counts


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def gap_seconds(gaps, a, b):
    """Seconds of [a, b] covered by the gaps [{"from", "to"}] (gaps do not overlap each other: one is open at a time)."""
    return sum(overlap(g["from"], g["to"], a, b) for g in gaps)


def gap_per_minute(gaps, t0, t1):
    n = len(bin_per_minute([], t0, t1))
    return [round(gap_seconds(gaps, t0 + 60.0 * i, min(t1, t0 + 60.0 * (i + 1))), 1) for i in range(n)]


# ------------------------------------------------------------------ the tally of the event stream

class EventTally:
    """Events of the public stream with their arrival time (epoch seconds): the event name counts per kind, the first and last
    event, and the gaps (a reconnect after the stream broke; `from` the loss, `to` the next connection). `hello` frames and
    frames without an event name are not events; an id seen before (a replay after a resume) is counted once. Thread safe."""

    def __init__(self, t0, seen_limit=SEEN_LIMIT):
        self.t0 = t0
        self.events = []                      # (t, name)
        self.gaps = []                        # {"from", "to", "reason"}
        self.first = None
        self.last = None
        self.duplicates = 0
        self.hellos = 0
        self.rollovers = 0
        self.errors = []
        self._seen, self._order, self._limit = set(), deque(), seen_limit
        self._open = None
        self._lock = threading.Lock()

    def add(self, t, frame):
        """True when the frame was counted as an event."""
        name = frame.get("event")
        with self._lock:
            if name == "hello":
                self.hellos += 1
                return False
            if not name:
                return False
            fid = frame.get("id")
            if fid:
                if fid in self._seen:
                    self.duplicates += 1
                    return False
                self._seen.add(fid)
                self._order.append(fid)
                if len(self._order) > self._limit:
                    self._seen.discard(self._order.popleft())
            self.events.append((t, name))
            rec = {"t": round(t, 3), "event": name, "id": fid}
            if self.first is None:
                self.first = rec
            self.last = rec
            return True

    def lost(self, t, reason):
        """The stream broke at t (a gap opens; a second loss before the stream is back extends the open gap)."""
        with self._lock:
            if self._open is None:
                self._open = {"from": t, "to": None, "reason": str(reason)[:120]}
            if len(self.errors) < 20:
                self.errors.append(str(reason)[:120])

    def back(self, t):
        """The stream is connected again at t (closes the open gap, if any)."""
        with self._lock:
            if self._open is not None:
                self._open["to"] = max(t, self._open["from"])
                self.gaps.append(self._open)
                self._open = None

    def rolled(self):
        with self._lock:
            self.rollovers += 1

    def snapshot(self, t_end):
        """(events, gaps, first, last): a copy; a gap still open at t_end is closed at t_end."""
        with self._lock:
            gaps = [dict(g) for g in self.gaps]
            if self._open is not None:
                gaps.append(dict(self._open, to=max(t_end, self._open["from"])))
            return list(self.events), gaps, self.first, self.last

    def summary_inputs(self):
        with self._lock:
            return {"hellos": self.hellos, "duplicates": self.duplicates, "rollovers": self.rollovers, "errors": list(self.errors)}


# ------------------------------------------------------------------ slopes of a child record

def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def slopes_of(rec):
    """{"vmrss", "rss", "anon"} slope per minute (KiB) of a child record of soak.json (`rss_kib.slope_kib_per_min`, `smaps`) or of
    isolation-<mode>.json (`vmrss.slope_per_min`, `smaps`); None for what the record does not carry. `rss` is the smaps_rollup Rss,
    the VmRSS slope when the run has no smaps keys. {} fields are None for a missing record."""
    out = {"vmrss": None, "rss": None, "anon": None}
    if not rec:
        return out
    vm = _num((rec.get("vmrss") or {}).get("slope_per_min"))
    if vm is None:
        vm = _num((rec.get("rss_kib") or {}).get("slope_kib_per_min"))
    smaps = rec.get("smaps") or {}
    rss = _num((smaps.get("rss") or {}).get("slope_per_min"))
    out["vmrss"] = vm
    out["rss"] = rss if rss is not None else vm
    out["anon"] = _num((smaps.get("anon") or {}).get("slope_per_min"))
    return out


def child_slopes(children):
    """{focus child: slopes_of(record)} from the children of a result; dv-room is the SUM over its ranks (dv-room.01, ...),
    with the ranks under "ranks". A focus child that was not sampled maps to the all-None slopes."""
    out = {}
    for name in FOCUS_CHILDREN:
        if name == ROOM_CHILD:
            ranks = {k: slopes_of(v) for k, v in sorted((children or {}).items()) if k == ROOM_CHILD or k.startswith(ROOM_CHILD + ".")}
            total = {}
            for key in ("vmrss", "rss", "anon"):
                vals = [r[key] for r in ranks.values() if r[key] is not None]
                total[key] = round(sum(vals), 3) if vals else None
            total["ranks"] = {k: v["rss"] for k, v in ranks.items()}
            out[name] = total
        else:
            out[name] = slopes_of((children or {}).get(name))
    return out


# ------------------------------------------------------------------ events: the record, the estimate, the note

def _ratio(estimate, slope):
    if estimate is None or slope is None or slope <= 0:
        return None
    return round(estimate / slope, 3)


def hub_events_block(tally, t_end, win_from, win_to, slopes=None, ring=RING, kib=EVENT_KIB):
    """The `hub_events` record. `tally` an EventTally, `t_end` epoch seconds of the end of reading (the series covers
    tally.t0 .. t_end), [win_from, win_to] the measured part (epoch), `slopes` = child_slopes() (the dv-svc-status entry is used).

      events_total, by_kind          every event counted from the start of warmup to t_end
      events_per_min_series          [{"minute", "t", "events", "gap_s"}] per minute from the start of reading
      first_event, last_event        {"t", "event", "id"}
      measured                       events, observed_s (the window minus the gaps), mean_per_min, by_kind of the measured part
      events_kib_per_min             mean events/min x 1.16 KiB
      status_*_slope, ratio_*        the dv-svc-status Rss and Anon slopes and estimate / slope (1.0: the events alone explain the slope)
      cumulative_at_end              events counted up to the end of the measured part, ring_full (>= 10000) and the minutes to fill
      reconnects, gaps               breaks of the stream (a planned 60 s roll-over is not a break; `rollovers` counts them)
      events_note                    the sentences above in words
    Recorded only."""
    times_names, gaps, first, last = tally.snapshot(t_end)
    extra = tally.summary_inputs()
    times = [t for t, _n in times_names]
    kinds = {}
    for _t, n in times_names:
        kinds[n] = kinds.get(n, 0) + 1
    counts = bin_per_minute(times, tally.t0, t_end)
    gap_min = gap_per_minute(gaps, tally.t0, t_end)
    series = [{"minute": i, "t": round(tally.t0 + 60.0 * i, 1), "events": counts[i], "gap_s": gap_min[i]} for i in range(len(counts))]
    in_win = [(t, n) for t, n in times_names if win_from <= t < win_to]
    win_kinds = {}
    for _t, n in in_win:
        win_kinds[n] = win_kinds.get(n, 0) + 1
    observed = max(0.0, (win_to - win_from) - gap_seconds(gaps, win_from, win_to))
    mean = round(len(in_win) / observed * 60.0, 3) if observed > 0.0 else None
    estimate = round(mean * kib, 3) if mean is not None else None
    st = (slopes or {}).get("dv-svc-status") or {}
    cum = sum(1 for t in times if t < win_to)
    block = {
        "events_total": len(times), "by_kind": dict(sorted(kinds.items())), "events_per_min_series": series,
        "first_event": first, "last_event": last,
        "measured": {"events": len(in_win), "window_s": round(win_to - win_from, 1), "observed_s": round(observed, 1), "mean_per_min": mean,
                     "by_kind": dict(sorted(win_kinds.items()))},
        "kib_per_event": kib, "events_kib_per_min": estimate,
        "status_rss_slope_kib_per_min": st.get("rss"), "status_anon_slope_kib_per_min": st.get("anon"),
        "ratio_to_rss": _ratio(estimate, st.get("rss")), "ratio_to_anon": _ratio(estimate, st.get("anon")),
        "ring": ring, "cumulative_at_end": cum, "ring_full": cum >= ring,
        "minutes_to_fill": round((ring - cum) / mean, 1) if (mean and cum < ring) else None,
        "reconnects": len(gaps), "gaps": [{"from": round(g["from"], 1), "to": round(g["to"], 1), "reason": g["reason"]} for g in gaps],
        "gap_s_total": round(gap_seconds(gaps, tally.t0, t_end), 1),
        "rollovers": extra["rollovers"], "duplicates": extra["duplicates"], "hellos": extra["hellos"], "errors": extra["errors"][:5],
    }
    block["match"] = match_of(block)
    block["events_note"] = events_note(block)
    return block


def match_of(block):
    """"unknown" (no estimate or no slope), "matches" (estimate / Rss slope within 0.7 to 1.4), "part" (below) or "above"."""
    ratio = block.get("ratio_to_rss")
    if ratio is None:
        return "unknown"
    if ratio < MATCH_LOW:
        return "part"
    return "matches" if ratio <= MATCH_HIGH else "above"


def events_note(block):
    """A short sentence-like statement of the record (also written to soak.json / isolation-<mode>.json as `events_note`)."""
    m = block["measured"]
    if block["events_total"] == 0 and (block["errors"] or block["reconnects"]):
        return "no event was read from the public stream (%s): the estimate is not available" % (block["errors"][0] if block["errors"] else "stream broken")
    if m["mean_per_min"] is None:
        return "the measured part was observed for only %.0f s: no events per minute" % m["observed_s"]
    parts = ["%d events in the measured part, %.1f events/min, estimate %.1f KiB/min at %.2f KiB each" % (m["events"], m["mean_per_min"], block["events_kib_per_min"], block["kib_per_event"])]
    rss, anon = block["status_rss_slope_kib_per_min"], block["status_anon_slope_kib_per_min"]
    if block["ratio_to_rss"] is not None:
        parts.append("dv-svc-status Rss slope %.1f KiB/min (ratio %.2f%s)" % (rss, block["ratio_to_rss"],
                     ", Anon %.1f KiB/min ratio %.2f" % (anon, block["ratio_to_anon"]) if block["ratio_to_anon"] is not None else ""))
        parts.append({"matches": "the event rate accounts for the slope", "part": "the event rate accounts for only part of the slope",
                      "above": "the event rate would fill more than the slope shows"}[block["match"]])
    else:
        parts.append("no positive dv-svc-status slope to compare with")
    cum = block["cumulative_at_end"]
    if block["ring_full"]:
        parts.append("%d events counted by the end, the ring of %d would be FULL (a slope after that is not the ring filling)" % (cum, block["ring"]))
    else:
        fill = ", full in about %.0f more minutes at this rate" % block["minutes_to_fill"] if block["minutes_to_fill"] else ""
        parts.append("%d events counted by the end, the ring of %d would NOT be full%s" % (cum, block["ring"], fill))
    if block["reconnects"]:
        parts.append("%d reconnect gaps (%.0f s) in the stream" % (block["reconnects"], block["gap_s_total"]))
    return "; ".join(parts)


# ------------------------------------------------------------------ hub /metrics families with "event" in the name

_EVENT_NAME = re.compile(r"event", re.I)
_PARTS = re.compile(r"_(bucket|sum|count)$")


def events_metric_values(samples):
    """{family: sum over labels} of the plain samples of one scrape whose family name contains "event" (counters and gauges;
    histogram parts are skipped). {} when there is none."""
    out = {}
    for s in samples:
        if _EVENT_NAME.search(s.name) and not _PARTS.search(s.name) and s.value == s.value:
            out[s.name] = out.get(s.name, 0.0) + s.value
    return out


def events_metric_summary(rows, win_from, win_to):
    """{"names_seen", "series": {family: {"kind", "first", "last", "delta", "per_min"}}, "note"} of [(t, {family: value})]: the
    first and last reading inside the measured part (per_min only for counters, `_total`)."""
    names = sorted({n for _t, vals in rows for n in vals})
    if not names:
        return {"names_seen": [], "series": {}, "note": "the hub /metrics has no family with 'event' in its name"}
    series = {}
    for name in names:
        pts = sorted((t, vals[name]) for t, vals in rows if name in vals and win_from <= t <= win_to)
        if not pts:
            continue
        counter = name.endswith("_total")
        rec = {"kind": "counter" if counter else "gauge", "first": pts[0][1], "last": pts[-1][1], "delta": round(pts[-1][1] - pts[0][1], 3), "n": len(pts)}
        if counter and pts[-1][0] > pts[0][0]:
            rec["per_min"] = round((pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]) * 60.0, 3)
        series[name] = rec
    return {"names_seen": names, "series": series, "note": "families of the hub /metrics with 'event' in the name, summed over labels"}


# ------------------------------------------------------------------ distinct streams

def streams_block(results, lane_modules, win_from, win_to, slopes=None):
    """The `streams` record: the overs DELIVERED (result ok) per lane, per module and per room, from the per-over results of
    ss.Traffic (each has "lane", "ok", "t_start" epoch). `lane_modules` maps a lane to its module letter (a room is the module's
    room, so `per_room` repeats `per_module`: the DV-Router wording). streams_total is the count with t_start inside the measured part
    [win_from, win_to) and streams_per_min that divided by the length of the window; `run_total` is the whole run.
    `slope_per_stream`: for dv-router, dv-svc-status and dv-room (sum over ranks) the Rss and Anon slope per minute divided by
    streams per minute = KiB per stream (None without streams or slope). Recorded only."""
    delivered = [r for r in results if r.get("ok")]
    inside = [r for r in delivered if win_from <= r.get("t_start", 0.0) < win_to]
    per_lane, per_module = {}, {}
    for r in inside:
        lane = r["lane"]
        per_lane[lane] = per_lane.get(lane, 0) + 1
        mod = (lane_modules or {}).get(lane, lane)
        per_module[mod] = per_module.get(mod, 0) + 1
    minutes = (win_to - win_from) / 60.0
    per_min = round(len(inside) / minutes, 3) if minutes > 0 else None
    per_stream = {}
    for name, sl in (slopes or {}).items():
        rec = {"rss_kib_per_min": sl.get("rss"), "anon_kib_per_min": sl.get("anon")}
        for key in ("rss", "anon"):
            v = sl.get(key)
            rec["%s_kib_per_stream" % key] = round(v / per_min, 3) if (v is not None and per_min) else None
        per_stream[name] = rec
    return {"streams_total": len(inside), "streams_per_min": per_min, "run_total": len(delivered), "scheduled_total": len(results),
            "per_lane": dict(sorted(per_lane.items())), "per_module": dict(sorted(per_module.items())), "per_room": dict(sorted(per_module.items())),
            "window_s": round(win_to - win_from, 1), "slope_per_stream": per_stream}
