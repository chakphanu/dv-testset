# SPDX-License-Identifier: MIT
"""Pure parts of the short WebKit talk repro (cases/test_web_talk_cycles.py): the plan of the cycles, the judgement of one
cycle from the page's status timeline, counter deltas from the hub metrics samples and the per-cycle table. No browser,
no network: unit tested in cases/unit/test_talk_cycles.py."""

ON_AIR = "On air"
EARLY_MARGIN_S = 0.3        # a status that is not "On air" this close to the planned end is the release, not an early end
PAIR_EVERY = 10             # the ICE pairs of every 10th cycle are kept even when it passed


def plan(cycles=30, holds=(2.0, 3.0)):
    """The planned hold (s) of each cycle: the holds alternate."""
    return [holds[i % len(holds)] for i in range(cycles)]


def early_end(timeline, planned_s, margin_s=EARLY_MARGIN_S):
    """(seconds after On air, status) of the first status that is not "On air" before planned_s - margin_s, else None.
    `timeline` is [(seconds after On air, status text)] polled while the button is held."""
    for t, status in timeline:
        if status != ON_AIR and t < planned_s - margin_s:
            return (t, status)
    return None


def keep_pairs(index, failed):
    """Whether the cycle's getStats pairs go into the table (compact table: failed cycles and every PAIR_EVERY-th)."""
    return bool(failed) or index % PAIR_EVERY == 0


def counter_total(samples, family, **labels):
    """Sum of the samples ({'name', 'labels', 'value'} as the trace hook stores them) of `family` matching `labels`."""
    return sum(s["value"] for s in samples or [] if s["name"] == family
               and all(s["labels"].get(k) == v for k, v in labels.items()))


def unrouted_delta(before, after):
    """{reason: increase} of dv_edge_webrtc_unrouted_total over a cycle plus {'audio_dropped': increase}; absent family = 0."""
    reasons = {s["labels"].get("reason", "?") for s in (before or []) + (after or []) if s["name"].endswith("unrouted_total")}
    out = {r: counter_total(after, "dv_edge_webrtc_unrouted_total", reason=r) - counter_total(before, "dv_edge_webrtc_unrouted_total", reason=r)
           for r in sorted(reasons)}
    out["audio_dropped"] = (counter_total(after, "dv_edge_webrtc_audio_dropped_total")
                            - counter_total(before, "dv_edge_webrtc_audio_dropped_total"))
    return out


def ptt_frames(digest, since_ms, until_ms):
    """The ptt frames of a ctl digest ([{'t_ms', 'op', 'state', 'code'}], times relative to some zero) between two times,
    as compact tuples (t_ms, 'state/code')."""
    return [(round(m["t_ms"]), "%s/%s" % (m.get("state"), m.get("code"))) for m in digest
            if m.get("op") == "ptt" and since_ms <= m["t_ms"] <= until_ms]


def summarize(rows):
    """{'cycles', 'early', 'failed': [indexes], 'no_on_air': [indexes]} of the table rows."""
    failed = [r["index"] for r in rows if r.get("early") or r.get("no_on_air")]
    return {"cycles": len(rows), "early": sum(1 for r in rows if r.get("early")),
            "no_on_air": [r["index"] for r in rows if r.get("no_on_air")], "failed": failed}
