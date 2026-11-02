# SPDX-License-Identifier: MIT
"""Pure helpers of the K22 / ICE1a cases (cases/test_web_k22.py, the blackhole case of cases/test_web_nat.py):
judging how a Listen session was lost, summarising a series of POST /api/v1/rtc/alive answers, and counting the
events of a long idle sample of the public stream. No I/O; unit-tested in cases/unit/test_k22.py."""

# DV-Router web.md: the page waits 12 s from the first 'disconnected' of the peer or ICE; the edge drops an
# established ICE-lite peer 15 s after the last STUN Binding request; a guest proof is fresh for < 15 s.
GRACE_S = 12.0
EDGE_STUN_TIMEOUT_S = 15.0
FRESH_S = 15.0
SWEEP_S = 5.0


def judge_loss(first_state_ms, lost_wall_s, break_s, grace_s=GRACE_S, tol_s=1.5):
    """How a session that the page gave up ended. `first_state_ms` maps 'ice:<state>' / 'conn:<state>' to the
    page-clock ms (from the start of the break) of the first such event; `lost_wall_s` is the seconds from the
    start of the break to the poll that saw the loss text (None when not lost); `break_s` the length of the
    break. Returns {"lost", "during_break", "side", "gap_s"}.

    side:
      "engine_failed"      the engine itself reported failed (ICE or peer) before the page's grace could expire: its
                           consent checks got no answer. The edge removes an established peer 15 s after the last
                           STUN request, which lands at the same time, so from the page the two cannot be told apart
      "page_grace_expiry"  the loss came grace_s (+- tol_s) after the first 'disconnected' with no failed event
      "unknown"            anything else (no disconnected event, or another gap)
    gap_s is loss time minus first disconnected time (None when either is unknown)."""
    if lost_wall_s is None:
        return {"lost": False, "during_break": False, "side": None, "gap_s": None}
    dis = [v for k, v in first_state_ms.items() if k in ("ice:disconnected", "conn:disconnected") and v is not None]
    failed = [k for k in first_state_ms if k in ("ice:failed", "conn:failed", "conn:closed", "ice:closed")]
    gap = None
    if dis:
        gap = round(lost_wall_s - min(dis) / 1000.0, 2)
    if failed:
        side = "engine_failed"
    elif gap is not None and abs(gap - grace_s) <= tol_s:
        side = "page_grace_expiry"
    else:
        side = "unknown"
    return {"lost": True, "during_break": lost_wall_s < break_s, "side": side, "gap_s": gap}


def runs(series):
    """Compress [(t, status)] into [{"status", "first", "last", "n"}] runs of equal consecutive statuses."""
    out = []
    for t, status in series:
        if out and out[-1]["status"] == status:
            out[-1]["last"] = t
            out[-1]["n"] += 1
        else:
            out.append({"status": status, "first": t, "last": t, "n": 1})
    return out


def alive_summary(series, t_ref):
    """Summary of the answers of POST /rtc/alive: `series` [(epoch seconds, HTTP status or None)], `t_ref` the
    epoch seconds the intervals are counted from. Returns the runs (times relative to t_ref, 1 decimal), the
    first time of each status and whether the order is the documented one for a lease that ends (204s, then
    401 while the record is retained, then 403 once cleanup removed it; nothing goes back)."""
    rel = [(round(t - t_ref, 1), s) for t, s in series]
    firsts = {}
    for t, s in rel:
        firsts.setdefault(s, t)
    order = [r["status"] for r in runs(rel)]
    rank = {204: 0, 401: 1, 403: 2}
    ranked = [rank[s] for s in order if s in rank]
    monotonic = all(a <= b for a, b in zip(ranked, ranked[1:]))
    return {"runs": runs(rel), "first": firsts, "monotonic": monotonic, "n": len(rel),
            "only": sorted({s for _t, s in rel if s is not None}, key=str)}


def renewed_past(series, t_gone, lease_s, margin_s):
    """True when an answer 204 is still seen later than lease_s + margin_s after t_gone: the lease of a circuit
    whose proof stopped at t_gone cannot outlive that."""
    return any(s == 204 and t - t_gone > lease_s + margin_s for t, s in series)


def event_times(frames_at, names=None):
    """Arrival times of the frames (each (t, frame dict)) that carry an event name (not `hello`); `names`
    restricts them further."""
    out = []
    for t, frame in frames_at:
        name = frame.get("event")
        if not name or name == "hello":
            continue
        if names is None or name in names:
            out.append(t)
    return out


def per_minute(times, total_s):
    """Counts per whole minute of the sample (a partial last minute is counted as its own bucket)."""
    n = int(total_s // 60) + (1 if total_s % 60 else 0)
    counts = [0] * max(n, 1)
    for t in times:
        i = min(int(t // 60), len(counts) - 1)
        if t >= 0:
            counts[i] += 1
    return counts


def idle_stream_verdict(times, total_s, warmup_s=60.0, max_per_min=2):
    """Judge the event times (seconds since the start of an idle sample) against 'no per-5 s status event': after
    `warmup_s` no whole minute may hold more than `max_per_min` events (a per-5 s event would be 12 a minute).
    Returns {"ok", "per_minute", "after_warmup", "worst_minute"}."""
    counts = per_minute(times, total_s)
    first_full = int(-(-warmup_s // 60))
    judged = counts[first_full:]
    worst = max(judged) if judged else 0
    return {"ok": worst <= max_per_min, "per_minute": counts, "after_warmup": sum(1 for t in times if t >= warmup_s),
            "worst_minute": worst}
