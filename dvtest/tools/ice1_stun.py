# SPDX-License-Identifier: MIT
"""ICE1 NAT run report: per cell, the selected local candidate type and the STUN PRIORITY of the nominating check and of the
consent checks before and after the browser's address change, and whether the edge logged a rebinding.

    python3 -m dvtest.tools.ice1_stun out/<run>

Reads out/<run>/web/ice1-results.jsonl (the cases/test_web_nat_ice1.py evidence), out/<run>/capture/xlxd-sut.pcapng (needs a
run with --capture) and out/<run>/logs/xlxd-sut.log. Prints a table and writes out/<run>/web/ice1-stun.json.

Why: DV-Router's consent rule (b) (path_selection.rs) switches the media destination only when a non-nominating check from
the new mapping carries the SAME PRIORITY as the check that nominated the pair. Firefox sends the prflx priority (type
preference 110) on the nominating check and the srflx priority (100) on later consent checks when its selected local
candidate is srflx, so the rule never fires in that case (ICE1e). The pair type depends on a gathering race, so each cell
needs its own evidence.

How a cell is placed in the capture (nothing is hardcoded): the results file records the NAT address before and after
("address change": from, to) and the port the SUT saw before and after ("router view"). STUN requests from the old
(ip, port) give the session (its USERNAME), the nominating check (USE-CANDIDATE) and the consent checks before; requests
of the same USERNAME from the new (ip, port) are the checks after. The instant of the change is the "t0_utc" the case records in
the "address change" row (the router's clock); runs made before that was recorded fall back to the first request from the new
mapping minus the recorded "first_stun_new_s" (the router sniffer's offset). It places the SUT log lines. The case's before/after
read of the edge counters (rebinding switches by reason: nomination, consent, liveness; drops; unrouted) is copied into the JSON.
Stdlib only.
"""

import argparse
import datetime
import json
import os
import re
import struct
import sys
from collections import Counter

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dvtest import ice1, pcap  # noqa: E402

COOKIE = 0x2112A442
ATTR_USERNAME, ATTR_PRIORITY, ATTR_USE_CANDIDATE = 0x0006, 0x0024, 0x0025
TYPE_PREF = {126: "host", 110: "prflx", 100: "srflx"}   # RFC 8445 recommended values; others (TURN relay checks use 5) print as tp<N>
DEFAULT_WINDOW_S = 20.0
LOG_SLACK_S = 0.5
T0_TOLERANCE_S = 0.25


def stun_message(payload):
    """A Binding request or response in a UDP payload -> {"kind": "request"|"response"|"error", "txid": bytes12, "priority":
    int|None, "use_candidate": bool, "username": str|None}, or None for anything else (other STUN methods, media, garbage)."""
    if len(payload) < 20 or payload[0] & 0xC0 or struct.unpack(">I", payload[4:8])[0] != COOKIE:
        return None
    mtype, length = struct.unpack(">HH", payload[0:4])
    klass = ((mtype >> 4) & 1) | ((mtype >> 7) & 2)
    method = (mtype & 0x000F) | ((mtype >> 1) & 0x0070) | ((mtype >> 2) & 0x0F80)
    if method != 1 or klass not in (0, 2, 3):
        return None
    out = {"kind": {0: "request", 2: "response", 3: "error"}[klass], "txid": bytes(payload[8:20]), "priority": None,
           "use_candidate": False, "username": None}
    end, pos = min(len(payload), 20 + length), 20
    while pos + 4 <= end:
        atype, alen = struct.unpack(">HH", payload[pos:pos + 4])
        value = payload[pos + 4:pos + 4 + alen]
        if atype == ATTR_PRIORITY and alen == 4:
            out["priority"] = struct.unpack(">I", value)[0]
        elif atype == ATTR_USE_CANDIDATE:
            out["use_candidate"] = True
        elif atype == ATTR_USERNAME:
            out["username"] = value.decode("utf-8", "replace")
        pos += 4 + ((alen + 3) & ~3)
    return out


def type_of(priority):
    """'prflx' etc. from the type preference (top byte) of an ICE priority; None for no priority."""
    if priority is None:
        return None
    return TYPE_PREF.get(priority >> 24, "tp%d" % (priority >> 24))


def label(priority):
    return "-" if priority is None else "%d(%s)" % (priority, type_of(priority))


# ------------------------------------------------------------------------- the results file

def load_cells(path):
    """{(engine, nat): {key: value}} of the browser cells of ice1-results.jsonl (a cell has an 'address change' row; the router
    self-test rows have engine null). The last row of a key wins."""
    cells = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("engine") is None:
                continue
            cells.setdefault((row["engine"], row.get("nat")), {})[row["key"]] = row.get("value")
    return {k: v for k, v in cells.items() if "address change" in v}


def endpoints(cell):
    """((old ip, old port), (new ip, new port), problems): the NAT address from 'address change', the ports from 'router view'
    (the first entry of the before and the after list). A missing piece gives None and a problem text."""
    change = cell.get("address change") or {}
    view = cell.get("router view: the SUT sees the browser at (before / after)") or []
    problems = []

    def port(side):
        try:
            return int(view[side][0]["seen_port"])
        except (IndexError, KeyError, TypeError, ValueError):
            problems.append("no router view port (%s)" % ("before", "after")[side])
            return None

    old_port, new_port = port(0), port(1)
    old_ip, new_ip = change.get("from"), change.get("to")
    if not old_ip or not new_ip:
        problems.append("no address change from/to")
    old = (old_ip, old_port) if old_ip and old_port else None
    new = (new_ip, new_port) if new_ip and new_port else None
    return old, new, problems


# ------------------------------------------------------------------------- the capture

def read_checks(pcap_path, wanted):
    """(requests, responses) of the STUN Binding traffic that involves an endpoint of `wanted` ((ip, port) set). A request:
    {ts, src, sport, dst, dport, txid, priority, use_candidate, username}, in capture order; responses: {(client ip, client
    port, txid): (ts, kind)} (the datagram goes to the client)."""
    requests, responses = [], {}
    for dg in pcap.udp_datagrams(pcap_path):
        payload = dg.payload
        if len(payload) < 20 or payload[0] & 0xC0 or payload[4:8] != b"\x21\x12\xa4\x42":
            continue
        if (dg.src, dg.sport) not in wanted and (dg.dst, dg.dport) not in wanted:
            continue
        msg = stun_message(payload)
        if msg is None or dg.ts is None:
            continue
        if msg["kind"] == "request":
            if (dg.src, dg.sport) in wanted:
                requests.append({"ts": dg.ts, "src": dg.src, "sport": dg.sport, "dst": dg.dst, "dport": dg.dport,
                                 "txid": msg["txid"], "priority": msg["priority"], "use_candidate": msg["use_candidate"],
                                 "username": msg["username"]})
        elif (dg.dst, dg.dport) in wanted:
            responses[(dg.dst, dg.dport, msg["txid"])] = (dg.ts, msg["kind"])
    return requests, responses


def analyse_checks(requests, responses, old, new, first_stun_new_s=None, t0=None):
    """The STUN evidence of one cell from the requests of read_checks. Returns a dict; 'problems' lists what could not be
    determined. Times are seconds relative to the instant of the change ('t0', epoch seconds): the recorded one when given (the
    case writes it from the router's clock), else the first request from the new mapping minus first_stun_new_s, else that first
    request itself. When both the recorded and the derived value exist they are compared (a gap over 0.25 s is a problem).

      priority_match         every consent check from the new mapping carries a PRIORITY of the NOMINATING check (the rule of
                             DV-Router before ICE1e); None without consent checks after or without a nominating check
      priority_in_seen_set   every such PRIORITY was already seen on an authenticated check from the old mapping (the bounded
                             set of ICE1e, wip-caebc57)"""
    out = {"session": None, "t0": None, "t0_source": None, "nominate_priorities": [], "consent_before": [], "consent_after": [],
           "after_requests": 0, "after_answered": 0, "after_use_candidate": 0, "first_after_s": None,
           "priority_match": None, "priority_in_seen_set": None, "t0_derived": None, "problems": []}
    from_old = [r for r in requests if (r["src"], r["sport"]) == old]
    if not from_old:
        out["problems"].append("no STUN request from the old mapping %s:%s" % old)
        return out
    user = Counter(r["username"] for r in from_old if r["username"]).most_common(1)
    session = user[0][0] if user else None
    out["session"] = session
    from_old = [r for r in from_old if r["username"] == session]
    after = [r for r in requests if (r["src"], r["sport"]) == new and r["username"] == session]
    nominations = [r for r in from_old if r["use_candidate"]]
    first_nom = nominations[0]["ts"] if nominations else None
    out["nominate_priorities"] = sorted({r["priority"] for r in nominations if r["priority"] is not None})
    if not nominations:
        out["problems"].append("no nominating (USE-CANDIDATE) request from the old mapping")
    before_checks = [r for r in from_old if not r["use_candidate"] and (first_nom is None or r["ts"] > first_nom)]
    out["consent_before"] = [r["priority"] for r in before_checks]
    if not after:
        out["problems"].append("no STUN request from the new mapping %s:%s" % new)
        return out
    after.sort(key=lambda r: r["ts"])
    if first_stun_new_s is not None:
        out["t0_derived"], derived_source = after[0]["ts"] - first_stun_new_s, "first request after - first_stun_new_s"
    else:
        out["t0_derived"], derived_source = after[0]["ts"], "first request after (results have no first_stun_new_s)"
    if t0 is not None:
        out["t0"], out["t0_source"] = t0, "recorded by the case (address change t0_utc)"
        if first_stun_new_s is not None and abs(t0 - out["t0_derived"]) > T0_TOLERANCE_S:
            out["problems"].append("recorded t0 differs from the derived one by %.3f s" % (t0 - out["t0_derived"]))
    else:
        out["t0"], out["t0_source"] = out["t0_derived"], derived_source
    out["after_requests"] = len(after)
    out["after_answered"] = sum(1 for r in after if responses.get((r["src"], r["sport"], r["txid"]), (0, ""))[1] == "response")
    out["after_use_candidate"] = sum(1 for r in after if r["use_candidate"])
    out["first_after_s"] = round(after[0]["ts"] - out["t0"], 3)
    out["consent_after"] = [r["priority"] for r in after if not r["use_candidate"]]
    if out["consent_after"] and out["nominate_priorities"]:
        out["priority_match"] = all(p in out["nominate_priorities"] for p in out["consent_after"])
    seen = {r["priority"] for r in from_old if r["priority"] is not None}
    if out["consent_after"] and seen:
        out["priority_in_seen_set"] = all(p in seen for p in out["consent_after"])
    return out


# ------------------------------------------------------------------------- the SUT log

LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z\s+webrtc: (rebinding|peer failed|peer removed)\b(.*)")


def _epoch(stamp, frac):
    base = datetime.datetime.fromisoformat(stamp).replace(tzinfo=datetime.timezone.utc).timestamp()
    return base + (float("0." + frac) if frac else 0.0)


def webrtc_log_events(path):
    """[(epoch s, 'rebinding'|'peer failed'|'peer removed', 'reason=...' text)] of the SUT log, in file order."""
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = LOG_LINE.search(line)
            if not m:
                continue
            reason = re.search(r"reason=(\w+)", m.group(4))
            events.append((_epoch(m.group(1), m.group(2)), m.group(3), reason.group(1) if reason else ""))
    return events


def events_in_window(events, t0, window_s):
    """The log events between t0 (less LOG_SLACK_S) and t0 + window_s, as (offset s, what, reason)."""
    return [(round(t - t0, 2), what, reason) for t, what, reason in events if t0 - LOG_SLACK_S <= t <= t0 + window_s]


# ------------------------------------------------------------------------- put together

def short_engine(name):
    return re.sub(r"^Playwright ", "", name or "").lower()


def cell_report(key, cell, requests, responses, log_events):
    engine, nat = key
    old, new, problems = endpoints(cell)
    pair = cell.get("selected pair (before / after)") or [{}, {}]
    local = [(p or {}).get("local") for p in pair] + [None, None]
    detail = cell.get("firsts: detail (kinds of the firsts, STUN response / RTP to the new mapping, stale totals)") or {}
    per_s = cell.get("stale_to_old_per_s") or []
    audio = (cell.get("audio after the change") or {}).get("analysis") or {}
    out = {"engine": engine, "nat": nat, "old": None if not old else "%s:%s" % old, "new": None if not new else "%s:%s" % new,
           "local_before": local[0], "local_after": local[1], "resumed": audio.get("resumed"),
           "resume_s": audio.get("resume_s"), "problems": list(problems)}
    if old and new:
        recorded = ice1.parse_iso_ms((cell.get("address change") or {}).get("t0_utc"))
        out.update(analyse_checks(requests, responses, old, new, detail.get("first_stun_new_s"), recorded))
        out["problems"] = problems + out["problems"]
    window = float(len(per_s)) if per_s else DEFAULT_WINDOW_S
    out["window_s"] = window
    out["edge_counters"] = cell.get(ice1.EDGE_KEY)       # the case's before / after read of the edge counters (ICE1e), None in older runs
    out["rebinding"] = None
    out["log"] = []
    if out.get("t0") is not None and log_events is not None:
        found = events_in_window(log_events, out["t0"], window)
        out["log"] = found
        rebinds = [e for e in found if e[1] == "rebinding"]
        out["rebinding"] = rebinds[0][2] if rebinds else "none"
        out["rebinding_at_s"] = rebinds[0][0] if rebinds else None
    # the ICE1e signature: the SUT answered consent checks from the new mapping, but their PRIORITY differs from the nominating
    # check's and no rebinding followed
    out["blocked_by_priority"] = bool(out.get("after_answered") and out.get("priority_match") is False and out["rebinding"] == "none")
    # the same with the bounded set of ICE1e (wip-caebc57): the consent PRIORITY was never seen on the selected source
    out["blocked_by_seen_set"] = bool(out.get("after_answered") and out.get("priority_in_seen_set") is False and out["rebinding"] == "none")
    return out


def build(run):
    """The report dict of an out/<run> directory."""
    results = os.path.join(run, "web", "ice1-results.jsonl")
    capture = os.path.join(run, "capture", "xlxd-sut.pcapng")
    log_path = os.path.join(run, "logs", "xlxd-sut.log")
    for path in (results, capture):
        if not os.path.exists(path):
            raise FileNotFoundError(path + (" (run with --capture)" if path == capture else ""))
    cells = load_cells(results)
    wanted = set()
    for cell in cells.values():
        old, new, _ = endpoints(cell)
        wanted.update(x for x in (old, new) if x)
    requests, responses = read_checks(capture, wanted)
    log_events = webrtc_log_events(log_path) if os.path.exists(log_path) else None
    reports = [cell_report(key, cell, requests, responses, log_events) for key, cell in sorted(cells.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))]
    return {"run": os.path.basename(os.path.normpath(run)), "capture": os.path.relpath(capture, run),
            "log": os.path.relpath(log_path, run) if log_events is not None else None, "cells": reports}


def _prios(values, limit=2):
    if not values:
        return "-"
    counts = Counter(values)
    shown = ["%s" % label(p) + ("x%d" % n if n > 1 else "") for p, n in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]
    return ",".join(shown) + ("+" if len(counts) > limit else "")


def table(report):
    rows = [("engine", "nat", "local b/a", "nominate", "consent before", "consent after", "after n/ans/uc", "match", "in set", "rebinding", "resumed")]
    for c in report["cells"]:
        match = {None: "-", True: "yes", False: "NO"}[c.get("priority_match")]
        rebinding = c["rebinding"] if c.get("rebinding") is None else (
            "none" if c["rebinding"] == "none" else "%s@%.2fs" % (c["rebinding"], c["rebinding_at_s"]))
        resumed = {None: "-", True: "yes", False: "NO"}[c["resumed"]]
        rows.append((short_engine(c["engine"]), str(c["nat"]), "%s/%s" % (c["local_before"], c["local_after"]),
                     _prios(c.get("nominate_priorities")), _prios(c.get("consent_before")), _prios(c.get("consent_after")),
                     "%d/%d/%d" % (c.get("after_requests", 0), c.get("after_answered", 0), c.get("after_use_candidate", 0)), match,
                     {None: "-", True: "yes", False: "NO"}[c.get("priority_in_seen_set")], "-" if rebinding is None else rebinding, resumed))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    notes = ["%s/%s: %s" % (short_engine(c["engine"]), c["nat"], p) for c in report["cells"] for p in c["problems"]]
    notes += ["%s/%s: consent checks answered but their PRIORITY differs from the nominating check and no rebinding followed "
              "(rule (b) blocked)" % (short_engine(c["engine"]), c["nat"]) for c in report["cells"] if c.get("blocked_by_priority")]
    notes += ["%s/%s: consent checks answered, their PRIORITY was never seen on the selected source and no rebinding followed "
              "(ICE1e bounded set)" % (short_engine(c["engine"]), c["nat"]) for c in report["cells"] if c.get("blocked_by_seen_set")]
    return "\n".join(lines + ([""] + notes if notes else []))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", help="out/<run> directory of an ICE1 NAT run made with --capture")
    args = ap.parse_args(argv)
    try:
        report = build(args.run)
    except FileNotFoundError as exc:
        print("ice1_stun: missing input: %s" % exc, file=sys.stderr)
        return 2
    target = os.path.join(args.run, "web", "ice1-stun.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True, default=str)
        fh.write("\n")
    print("run %s (capture %s, log %s)" % (report["run"], report["capture"], report["log"]))
    print(table(report))
    print("written %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
