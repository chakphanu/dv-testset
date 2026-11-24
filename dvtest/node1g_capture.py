# SPDX-License-Identifier: MIT
"""NODE1g, the capture side (docs/node1g-cases.md): what the link datagrams of a run say about an over cut by a blackout and about reordering.

The cases write one JSON line per observation window to out/<run>/node1g/windows.jsonl (epoch seconds of the tester's host clock, which is the clock of
dumpcap too):

  {"case": "m1", "kind": "fold", "dir": "h2c", "t_start": ..., "t_cut": ..., "t_end": ..., "fold": {"central": {"overs.C.end_missing": 1.0}, ...}}
  {"case": "a",  "kind": "delay", "t_start": ..., "t_stop": ...}

and `scripts/run.sh --capture` with DVT_NODE1G=on keeps two sniffers: capture/xlxd-sut.pcapng (the central node: it RECEIVES the datagrams sent to its link
port 31800) and capture/dvr-peer.pcapng (the hotspot: it RECEIVES what the central node sends from port 31800). The link records are authenticated
but not encrypted: the cleartext header (kind, session, counter, stream id, unit sequence) is enough (dvtest.rfpos1.parse_record).

  python3 -m dvtest.node1g_capture out/<run>

prints, for every fold window, the streams of the window with the End and the units as the RECEIVING node saw them, the fold the rule says and the fold the hub
counted, and for every delay window the reordering the receiving node saw. The rule (dv-router, 2026-11-24): an over folds `complete` only when its End reached
the listener (outside the blackout) AND no unit before it was lost; in every other case `end_missing`. Exit 0 when every fold window agrees, 1 when one does not
(a defect: the run id, the stream id and the numbers are printed), 2 on a usage error. Stdlib only.
"""
import calendar
import json
import os
import re
import sys

from dvtest import pcap, rfpos1

LINK_PORT = 31800
CENTRAL_PCAP, HOTSPOT_PCAP = "xlxd-sut.pcapng", "dvr-peer.pcapng"
# the node that RECEIVES for each direction of the case (h2c: hotspot to central), and its capture
RECEIVER = {"h2c": "central", "c2h": "hotspot"}
CAPTURE = {"central": CENTRAL_PCAP, "hotspot": HOTSPOT_PCAP}
ROOM = {"central": "C", "hotspot": "B"}
RESEQ_MS = 40.0           # LINKSEQ1: a unit reordered within about 40 ms is resequenced and played, so it is not counted late


def is_rx(node, d):
    """True for a datagram the capture of `node` saw ARRIVE (central: to its link port; hotspot: from the central node's link port)."""
    return d.dport == LINK_PORT if node == "central" else d.sport == LINK_PORT


def read(path, node):
    """[(ts, record)] of the link records the node received, in capture order; records the reader does not know are left out."""
    out = []
    for d in pcap.udp_datagrams(path):
        if LINK_PORT not in (d.sport, d.dport) or not is_rx(node, d) or d.ts is None:
            continue
        rec = rfpos1.parse_record(d.payload)
        if rec is not None and rec["kind"] >= 16:
            out.append((d.ts, rec))
    return out


# ---------------------------------------------------------------- streams

def streams(records, t_lo, t_hi):
    """The link streams whose StreamStart arrived in [t_lo, t_hi]: [{stream_id, t_start, units, seq_min, seq_max, holes, t_last, end_ts, end_reason}].
    `holes` counts the unit positions between seq_min and seq_max that never arrived (a unit has one 20 ms position)."""
    found = {}
    for ts, rec in records:
        if rec["kind"] == 19 and t_lo <= ts <= t_hi and rec["stream_id"] not in found:
            found[rec["stream_id"]] = {"stream_id": rec["stream_id"], "t_start": ts, "seqs": set(), "t_last": None, "end_ts": None, "end_reason": None}
    closed = set()
    for ts, rec in records:
        sid = rec.get("stream_id")
        s = found.get(sid)
        if s is None or ts < s["t_start"] or sid in closed:
            continue
        if rec["kind"] == 19 and ts > s["t_start"] + 0.5:       # stream ids restart with a new link session: the same id again is another stream
            closed.add(sid)
            continue
        if rec["kind"] == 20:
            s["seqs"].add(rec["sequence"])
            s["t_last"] = ts if s["t_last"] is None else max(s["t_last"], ts)
        elif rec["kind"] == 21 and s["end_ts"] is None:
            s["end_ts"], s["end_reason"] = ts, rec["reason"]
    out = []
    for s in sorted(found.values(), key=lambda s: s["t_start"]):
        seqs = s.pop("seqs")
        s["units"] = len(seqs)
        s["seq_min"], s["seq_max"] = (min(seqs), max(seqs)) if seqs else (None, None)
        s["holes"] = (s["seq_max"] - s["seq_min"] + 1 - len(seqs)) if seqs else 0
        out.append(s)
    return out


def end_state(stream, t_cut, t_end):
    """Where the End of a stream arrived relative to the blackout [t_cut, t_end]: none, before, inside, after."""
    ts = stream["end_ts"]
    if ts is None:
        return "none"
    return "before" if ts < t_cut else "inside" if ts <= t_end else "after"


def expected_fold(stream, t_cut, t_end):
    """The fold the rule gives (see the module doc): complete when the End arrived outside the blackout and no unit position inside the stream is
    missing; end_missing otherwise."""
    return "complete" if end_state(stream, t_cut, t_end) in ("before", "after") and stream["holes"] == 0 else "end_missing"


def observed_fold(deltas, room):
    """The result a hub delta of one node counted for `room`: the single key overs.<room>.<result> that moved, or the list when it is not exactly one."""
    keys = {k.split(".", 2)[2]: v for k, v in deltas.items() if k.startswith("overs.%s." % room) and v}
    if len(keys) == 1 and sum(keys.values()) == 1:
        return next(iter(keys))
    return keys


# ---------------------------------------------------------------- reordering

def reorder(records, t_lo, t_hi):
    """What the receiving node saw of reordering inside [t_lo, t_hi]: datagrams whose session counter is below the highest one already seen (by how
    many positions, and how long after the datagram that overtook them), and Units whose stream position is below the highest of their stream (the
    frames of the over that arrived late: how many, by how many 20 ms positions at most).
    {datagrams, reordered, max_counter_back, max_late_ms, mean_late_ms, units, units_reordered, max_seq_back}."""
    top, top_ts, session = -1, None, None
    out = {"datagrams": 0, "reordered": 0, "max_counter_back": 0, "max_late_ms": 0.0, "units": 0, "units_reordered": 0, "max_seq_back": 0}
    lates, seq_top, unit_arrivals, unit_late, unit_back = [], {}, {}, [], []
    for ts, rec in records:
        if not t_lo <= ts <= t_hi:
            continue
        out["datagrams"] += 1
        c = rec["counter"]
        if rec["session"] != session:                   # a new session counts from its own start
            session, top, top_ts = rec["session"], -1, None
        if c > top:
            top, top_ts = c, ts
        else:
            out["reordered"] += 1
            out["max_counter_back"] = max(out["max_counter_back"], top - c)
            late = (ts - top_ts) * 1000.0
            lates.append(late)
            out["max_late_ms"] = max(out["max_late_ms"], late)
        if rec["kind"] == 20:
            out["units"] += 1
            sid, seq = rec["stream_id"], rec["sequence"]
            if seq > seq_top.get(sid, -1):
                seq_top[sid] = seq
            else:
                out["units_reordered"] += 1
                out["max_seq_back"] = max(out["max_seq_back"], seq_top[sid] - seq)
                # how late: after the FIRST unit of the stream that came with a higher position (the one that overtook it)
                over = [t for t, q in unit_arrivals.get(sid, ()) if q > seq]
                unit_late.append((ts - min(over)) * 1000.0)
                unit_back.append(seq_top[sid] - seq)
            unit_arrivals.setdefault(sid, []).append((ts, seq))
    out["mean_late_ms"] = sum(lates) / len(lates) if lates else 0.0
    out["unit_late_ms"] = list(unit_late)
    out["unit_back"] = list(unit_back)
    out["units_over_40ms"] = sum(1 for x in unit_late if x > RESEQ_MS)
    return out


# ---------------------------------------------------------------- the refusal log (LINKOBS1)

LOG_NODE = {"xlxd-sut.log": "central", "dvr-peer.log": "hotspot"}
REFUSED = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)\.(\d+)Z dv-edge-link: handshake refused side=(\S+) reason=(\S+) suppressed=(\d+)\s*$")
RESTART = re.compile(r"dv-edge-link: (?:stopping|started \(pid)")
LOG_WINDOW_S = 30.0       # LINKOBS1: at most one log line per Reject reason per half in 30 seconds; suppressed ones are summarized in the next line


def refusal_log(lines, node):
    """The refusal diagnostics of a collected log: ([(epoch s, side, reason, suppressed)], problems). Problems (sentences): a line whose `side=` is not the
    node's half, two lines of one reason closer than LOG_WINDOW_S (less 1 s of timestamp noise) without a restart of the edge between them, and a line that
    carries more than `side`, `reason` and a count (no account name, address or key material is allowed in it: the pattern is fixed, so an extra word
    makes the line no match and it is reported)."""
    out, problems, last = [], [], {}
    for line in lines:
        if RESTART.search(line):
            last.clear()                                   # a new process starts its own 30 s window
            continue
        if "handshake refused" not in line:
            continue
        m = REFUSED.search(line)
        if m is None:
            problems.append("a refusal line of an unexpected shape: %s" % line.strip()[-120:])
            continue
        y, mo, d, h, mi, s, frac, side, reason, suppressed = m.groups()
        ts = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s))) + float("0." + frac)
        if side != node:
            problems.append("the %s log has a refusal line of side=%s" % (node, side))
        key = (side, reason)
        if key in last and ts - last[key] < LOG_WINDOW_S - 1.0:
            problems.append("two %s lines of reason=%s %.1f s apart (at most one per %.0f s)" % (side, reason, ts - last[key], LOG_WINDOW_S))
        last[key] = ts
        out.append((ts, side, reason, int(suppressed)))
    return out, problems


def judge_logs(run_dir, out=print):
    """Print the refusal log verdict of both collected logs; the number of problems."""
    run = os.path.basename(os.path.normpath(run_dir))
    bad = 0
    for name, node in LOG_NODE.items():
        path = os.path.join(run_dir, "logs", name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            found, problems = refusal_log(fh.read().splitlines(), node)
        if not found and not problems:
            continue
        bad += len(problems)
        out("%s %s log: %d refusal lines (reasons %s), %d refusals summarized as suppressed -> %s" % (
            run, node, len(found), sorted({r for _, _, r, _ in found}), sum(s for *_, s in found), "ok" if not problems else "DEFECT: " + "; ".join(problems)))
    return bad


# ---------------------------------------------------------------- the host step

def load_windows(run_dir):
    path = os.path.join(run_dir, "node1g", "windows.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def judge(run_dir, out=print):
    """Print the verdict of every window; the number of fold windows that disagree with the rule (0 = all agree)."""
    run = os.path.basename(os.path.normpath(run_dir))
    cache, bad = {}, 0

    def recs(node):
        if node not in cache:
            path = os.path.join(run_dir, "capture", CAPTURE[node])
            cache[node] = read(path, node) if os.path.isfile(path) else None
        return cache[node]

    for w in load_windows(run_dir):
        if w.get("kind") == "fold":
            node = RECEIVER[w["dir"]]
            records = recs(node)
            if records is None:
                out("%s %s: no capture of the %s node (run with --capture)" % (run, w["case"], node))
                continue
            found = streams(records, w["t_start"] - 1.0, w["t_start"] + 2.0)
            if len(found) != 1:
                out("%s %s: %d link streams started in the window (wanted 1): %s" % (run, w["case"], len(found), [s["stream_id"] for s in found]))
                bad += 1
                continue
            s = found[0]
            want, got = expected_fold(s, w["t_cut"], w["t_end"]), observed_fold(w["fold"].get(node, {}), ROOM[node])
            ok = got == want
            bad += 0 if ok else 1
            out("%s %s %s: stream %d, %d units (positions %s..%s, %d missing), last unit %.2f s after the cut, End %s%s; rule: %s, hub folded: %s -> %s" % (
                run, w["case"], w["dir"], s["stream_id"], s["units"], s["seq_min"], s["seq_max"], s["holes"],
                (s["t_last"] or w["t_cut"]) - w["t_cut"], end_state(s, w["t_cut"], w["t_end"]),
                "" if s["end_ts"] is None else " (reason %d, %.2f s after the cut)" % (s["end_reason"], s["end_ts"] - w["t_cut"]), want, got,
                "agree" if ok else "DEFECT: the over folded %s although %s" % (got, "its End and units did not all arrive" if want == "end_missing" else "it arrived whole")))
        elif w.get("kind") == "delay":
            for node in ("central", "hotspot"):
                records = recs(node)
                if records is None:
                    out("%s %s: no capture of the %s node (run with --capture)" % (run, w["case"], node))
                    continue
                r = reorder(records, w["t_start"], w["t_stop"])
                lates = w.get("late_units", {}).get(node)
                if w.get("linkseq1") and lates is not None:
                    allowed = r["units_over_40ms"]
                    seq_ok = lates <= allowed
                    bad += 0 if seq_ok else 1
                    out("%s %s %s LINKSEQ1: %d units reordered (lateness ms %s, positions back %s), %d later than %.0f ms; link-edge late units counted %g, at most %d expected -> %s" % (
                        run, w["case"], node, r["units_reordered"], [round(x) for x in r["unit_late_ms"]], r["unit_back"], allowed, RESEQ_MS, lates, allowed,
                        "ok" if seq_ok else "DEFECT: a unit reordered inside %.0f ms was counted late" % RESEQ_MS))
                out("%s %s %s receives: %d datagrams, %d reordered (back by up to %d, late by up to %.0f ms, mean %.0f ms); %d units, %d reordered (back by up to %d positions)" % (
                    run, w["case"], node, r["datagrams"], r["reordered"], r["max_counter_back"], r["max_late_ms"], r["mean_late_ms"], r["units"],
                    r["units_reordered"], r["max_seq_back"]))
    return bad + judge_logs(run_dir, out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not os.path.isdir(argv[0]):
        print("usage: python3 -m dvtest.node1g_capture out/<run>", file=sys.stderr)
        return 2
    return 1 if judge(argv[0]) else 0


if __name__ == "__main__":
    sys.exit(main())
