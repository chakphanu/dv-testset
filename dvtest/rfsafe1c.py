# SPDX-License-Identifier: MIT
"""What a DV-Router image with RFSAFE1c lets a black-box case assert (DV-Router commit 63fecd9f, docs radio.md 10.4, status.md, data.md 10.x, fabric.md "Events").

Switch: DVT_RFSAFE1C=on asserts the checks below (strict); off (the default) skips them with a reason. Four statements from the DV-Router team:

(1) a fence that BEGINS mid-run on AX.25 (the observer's session cut, a foreign holder injected) shows in the status view within 2 s of the cause, and
    TX is refused from the first moment regardless of the view. Pure helpers: `view_latency` / `judge_latency`. The causes cannot be produced from the black-box
    side with the control channel of scripts/run.sh (no action reaches the fabric session or a `rf/<physical>/ptt` token), so the case is a documented GAP
    (docs/rfsafe1c-cases.md); the helpers are ready for the day a primitive exists.
(2) no `session` / `update` hint item appears in the admin event history, the admin or public change feed: the `update` event is a view-only invalidation of the
    edge -> hub path (data.md 10.x, status.md); the repaired snapshot's `snapshot` item is allowed. Helpers: `event_hints`, `frame_hints`.
(3) the SUT log line is `RF TX fenced on <physical>: probe failed part=<own_token|link_check|holder_watch|holder_query|task>; recovery is automatic`, once per
    failure run, naming a configured physical only and no holder, key or backend error text (radio.md 10.4). Helpers: `parse_probe_failed`, `judge_log`; the host
    scan is `python3 -m dvtest.rfsafe1c out/<run>` because the SUT logs are collected after pytest ends (dvtest/logscan.py).
(4) the hub's procs[] row of a token-bearing process (edge, codec-worker, data-worker, controller) has `since_ms` equal to the launcher child's `since_ms` once its
    `pid` equals the launcher child's `pid`. After a restart the new token can appear before the launcher's state put lands, so for up to the 10 s launcher query the
    row can show the new `up` with the PREVIOUS child's `pid` and `since_ms`: the stale row is recorded, and the comparison waits for the pid match. Helpers:
    `origin_pairs`, `classify`, `wait_origin`. Both lists come from ONE admin status reply (`GET /api/v1/admin/status`: `procs` from the hub, `launcher.children`).

Pure and stdlib only; the clock and sleep are injectable (cases/unit/test_rfsafe1c.py).
"""
import json
import os
import re
import sys
import time
from collections import namedtuple

VIEW_LATENCY_MAX_S = 2.0            # (1) a mid-run fence is in the view within 2 s of the cause
CATCHUP_MAX_S = 15.0                # (4) the hub row catches up to the launcher child: the 10 s launcher query plus slack
CATCHUP_POLL_S = 0.5
CONFIRM_POLLS = 2                   # (4) a since_ms difference at a matching pid must be seen on this many consecutive reads
PROBE_PARTS = ("own_token", "link_check", "holder_watch", "holder_query", "task")
TOKEN_KINDS = ("edge", "codec-worker", "data-worker", "controller")
HINT_TYPE = "session"
HINT_EV = "update"
LOG_TAIL = "; recovery is automatic"


def strict_on(environ=None):
    """DVT_RFSAFE1C=on: the image has RFSAFE1c, so the statements above are asserted."""
    return (environ if environ is not None else os.environ).get("DVT_RFSAFE1C", "").lower() in ("on", "1", "true", "yes")


# ================================================================ (4) procs[] row against the launcher child

def child_proc(name):
    """(kind, proc) of a launcher child key, as dv-svc-status maps it (crates/svc/status/src/procs.rs child_proc): `dv-edge-ax25` -> (edge, ax25), `dv-room[AB]` ->
    (controller, AB), `dv-codec-ambed[x]` -> (codec-worker, x), `dv-data-worker[x]` -> (data-worker, x), `dv-svc-web` -> (web, dv-svc-web), else (launcher, name)."""
    def bracket(prefix):
        if name.startswith(prefix + "[") and name.endswith("]"):
            return name[len(prefix) + 1:-1]
        return None
    if name.startswith("dv-edge-"):
        return "edge", name[len("dv-edge-"):]
    for prefix, kind in (("dv-room", "controller"), ("dv-codec-ambed", "codec-worker"), ("dv-data-worker", "data-worker")):
        inner = bracket(prefix)
        if inner is not None:
            return kind, inner
    if name.startswith("dv-svc-"):
        return name[len("dv-svc-"):], name
    return "launcher", name


Pair = namedtuple("Pair", "kind proc child pid since_ms restarts row")
# child: the launcher child key; pid / since_ms / restarts: the launcher child's; row: the procs[] row of (kind, proc) or None


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def origin_pairs(status_body, only=None):
    """[Pair] for every token-bearing launcher child of an admin status body (`launcher.children[]` and `procs[]`). A room-set child `dv-room[AB]` gives one pair per
    room letter (the same launcher start for every row). `only`: a list of launcher child keys to keep (None: all)."""
    body = status_body if isinstance(status_body, dict) else {}
    launcher = body.get("launcher") if isinstance(body.get("launcher"), dict) else {}
    rows = {}
    for row in body.get("procs") or []:
        if isinstance(row, dict) and isinstance(row.get("kind"), str) and isinstance(row.get("proc"), str):
            rows[(row["kind"], row["proc"])] = row
    out = []
    for child in launcher.get("children") or []:
        if not isinstance(child, dict) or not isinstance(child.get("name"), str):
            continue
        if only is not None and child["name"] not in only:
            continue
        kind, proc = child_proc(child["name"])
        if kind not in TOKEN_KINDS:
            continue
        procs = list(proc) if kind == "controller" and len(proc) > 1 else [proc]
        for p in procs:
            out.append(Pair(kind, p, child["name"], _int(child.get("pid")), child.get("since_ms"), child.get("restarts"), rows.get((kind, p))))
    return out


def classify(pair):
    """(state, detail) of one pair:
      no_pid     the launcher child has no pid now (backoff, stopped): nothing to compare
      no_row     the hub has no procs[] row yet (stale)
      stale      the row's pid is not the child's (the previous child, or none yet): tolerated until the pid matches
      match      pid equal and since_ms equal (both present, or both absent because the launcher start is unknown)
      mismatch   pid equal and since_ms different: the statement (4) is violated (confirmed by the caller over CONFIRM_POLLS reads)."""
    if pair.pid is None:
        return "no_pid", "launcher child %s has no pid (state/backoff)" % pair.child
    if pair.row is None:
        return "no_row", "no procs[] row for %s/%s" % (pair.kind, pair.proc)
    row_pid = _int(pair.row.get("pid"))
    if row_pid != pair.pid:
        return "stale", "row pid %r (up %r, since_ms %r), launcher child pid %r (since_ms %r)" % (row_pid, pair.row.get("up"), pair.row.get("since_ms"), pair.pid, pair.since_ms)
    child_since = _int(pair.since_ms)
    child_since = child_since if child_since and child_since > 0 else None            # a zero launcher start is "unknown" and omitted from the row
    row_since = pair.row.get("since_ms")
    if row_since == child_since:
        return "match", "pid %r since_ms %r restarts row %r child %r" % (pair.pid, row_since, pair.row.get("restarts"), pair.restarts)
    return "mismatch", "pid %r equal but row since_ms %r, launcher child since_ms %r" % (pair.pid, row_since, pair.since_ms)


OriginResult = namedtuple("OriginResult", "problems per_key stale polls waited_s")
# per_key: {(kind, proc): (state, detail)} the last classification (state: match | mismatch | stale | no_row | no_pid | old_child)
# stale: [(t, (kind, proc), detail, previous)] every stale or missing row met on the way (recorded, never a failure by itself); previous: the row still showed
#        the pid in `previous` (the child before the restart)


def wait_origin(read, *, only=None, previous=None, clock=time.monotonic, sleep=time.sleep, poll_s=CATCHUP_POLL_S, max_wait_s=CATCHUP_MAX_S):
    """Poll `read()` (an admin status body, or None when it could not be read) until every pair is decided: match, a confirmed mismatch (the same pid and the
    same pair of since_ms values on CONFIRM_POLLS consecutive reads). A pair whose pid never matches within `max_wait_s` is a problem (the row did
    not catch up; the launcher query is every 10 s). A child with no pid for the whole wait is recorded, not a problem. `previous`: {(kind, proc): the pid of the child before a restart}: a stale row that still shows it is flagged, and a launcher child that still has it is not yet "the child after the restart" (state old_child, pending; a problem when it lasts the whole wait).
    No pair at all (the launcher lists no token-bearing child) is a problem for the caller to turn into a skip."""
    t0 = clock()
    decided = {}
    last = {}
    streak = {}
    stale = []
    seen_stale = set()
    polls = 0
    while True:
        body = read()
        polls += 1
        now = clock() - t0
        pairs = origin_pairs(body, only) if body is not None else []
        for pair in pairs:
            key = (pair.kind, pair.proc)
            if key in decided:
                continue
            state, detail = classify(pair)
            if previous and pair.pid is not None and pair.pid == previous.get(key):
                state, detail = "old_child", "the launcher child still has the pre-restart pid %r (%s)" % (pair.pid, detail)
            last[key] = (state, detail)
            if state in ("stale", "no_row", "no_pid", "old_child"):
                streak.pop(key, None)
                tag = (key, detail)
                if tag not in seen_stale:
                    seen_stale.add(tag)
                    prev_pid = (previous or {}).get(key)
                    stale.append((round(now, 2), key, detail, bool(prev_pid is not None and pair.row and _int(pair.row.get("pid")) == prev_pid)))
            elif state == "mismatch":
                sig = (pair.pid, pair.row.get("since_ms"), pair.since_ms)
                streak[key] = (sig, streak[key][1] + 1) if key in streak and streak[key][0] == sig else (sig, 1)
                if streak[key][1] >= CONFIRM_POLLS:
                    decided[key] = "mismatch"
            else:
                streak.pop(key, None)
                decided[key] = state
        if pairs and all((p.kind, p.proc) in decided for p in pairs):
            break
        if now >= max_wait_s:
            break
        sleep(poll_s)
    problems = []
    keys = sorted(last)
    if not keys:
        problems.append("the launcher lists no token-bearing child%s" % ("" if only is None else " among %s" % list(only)))
    for key in keys:
        if decided.get(key) == "mismatch" or (key not in decided and last[key][0] == "mismatch"):
            problems.append("%s/%s: %s" % (key[0], key[1], last[key][1]))
        elif key not in decided and last[key][0] == "no_pid":
            pass                                                    # a child without a pid for the whole wait (stopped): nothing to compare, recorded
        elif key not in decided:
            problems.append("%s/%s: the procs[] row did not reach the launcher child's pid within %.0f s (%s)" % (key[0], key[1], max_wait_s, last[key][1]))
    return OriginResult(problems, dict(last), stale, polls, round(clock() - t0, 2))


def describe_origin(result):
    """The text of the [RECORD] line for one wait."""
    states = {"%s/%s" % k: v[0] for k, v in sorted(result.per_key.items())}
    stale = ["t=%.1fs %s/%s%s: %s" % (t, k[0], k[1], " (previous child)" if prev else "", d) for t, k, d, prev in result.stale]
    return "procs[] against launcher.children: %s; %d read(s), %.1f s; stale rows met: %s" % (
        states, result.polls, result.waited_s, stale if stale else "none")


# ================================================================ (2) no session/update hint item in the feeds

Hints = namedtuple("Hints", "hints others seen evs")
# hints: items that are type session with ev update (a violation); others: ev update of another type (recorded); seen: items looked at; evs: {type: {ev: n}}


def _is_update(obj, frame_event=None):
    if not isinstance(obj, dict) or obj.get("ev") != HINT_EV:
        return None
    typ = obj.get("type") or frame_event
    return "hint" if typ in (None, HINT_TYPE) else "other"


def _tally(evs, obj, frame_event=None):
    typ = obj.get("type") or frame_event or "?"
    evs.setdefault(typ, {})
    ev = obj.get("ev") if isinstance(obj.get("ev"), str) else "?"
    evs[typ][ev] = evs[typ].get(ev, 0) + 1


def event_hints(items, after_id=0):
    """Judge the `items` of GET /api/v1/admin/events (newest first, each {id, type, ev, ts_ms, ...}); only ids above `after_id` count."""
    hints, others, evs, seen = [], [], {}, 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        if isinstance(ident, int) and not isinstance(ident, bool) and ident <= after_id:
            continue
        seen += 1
        _tally(evs, item)
        kind = _is_update(item)
        if kind == "hint":
            hints.append({k: item.get(k) for k in ("id", "type", "ev", "ts_ms")})
        elif kind == "other":
            others.append({k: item.get(k) for k in ("id", "type", "ev", "ts_ms")})
    return Hints(hints, others, seen, evs)


def frame_hints(frames):
    """Judge SSE frames ({event, id, data} dicts as dvtest.web WebClient.sse returns them, the data a JSON object): a frame named `update`, or whose data
    is a session event with ev update, is a hint. The hello, resync and heartbeat frames are not events and are skipped."""
    hints, others, evs, seen = [], [], {}, 0
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        name = frame.get("event")
        if name in ("hello", "resync", "bye", "hb", None):
            continue
        try:
            obj = json.loads(frame.get("data", ""))
        except ValueError:
            obj = None
        seen += 1
        if not isinstance(obj, dict):
            if name == HINT_EV:
                hints.append({"id": frame.get("id"), "type": name, "ev": None, "ts_ms": None})
            continue
        _tally(evs, obj, name)
        kind = _is_update(obj, name)
        if name == HINT_EV and kind is None:
            kind = "hint"
        if kind == "hint":
            hints.append({"id": frame.get("id"), "type": obj.get("type") or name, "ev": obj.get("ev"), "ts_ms": obj.get("ts_ms")})
        elif kind == "other":
            others.append({"id": frame.get("id"), "type": obj.get("type") or name, "ev": obj.get("ev"), "ts_ms": obj.get("ts_ms")})
    return Hints(hints, others, seen, evs)


# ================================================================ (3) the probe failed log line

PROBE_RE = re.compile(r"RF TX fenced on (?P<physical>\S+): probe failed part=(?P<part>[^;\s]*)(?P<tail>.*)$")
RECOVERED_RE = re.compile(r"RF TX observation on (?P<physical>\S+) recovered")
PROBE_NEEDLE = "probe failed"
LogLine = namedtuple("LogLine", "no physical part tail")
LogJudgement = namedtuple("LogJudgement", "problems findings lines")


def parse_probe_failed(text):
    """([LogLine], [unparsable (line number, text)]) for every line of `text` that contains "probe failed". A prefix before "RF TX fenced" (a timestamp, a process
    tag) is allowed; `tail` is what follows the part."""
    parsed, bad = [], []
    for no, line in enumerate(text.splitlines(), 1):
        if PROBE_NEEDLE not in line:
            continue
        m = PROBE_RE.search(line)
        if m is None:
            bad.append((no, line.strip()[:200]))
        else:
            parsed.append(LogLine(no, m.group("physical"), m.group("part"), m.group("tail").rstrip()))
    return parsed, bad


def judge_log(text, physicals=None):
    """Judge the probe failed lines of one SUT log. Problems: a line that does not have the shape, a part outside PROBE_PARTS, a physical that is not configured
    (`physicals`, None: not checked), any text after `; recovery is automatic` (no holder, key or backend text enters the line). A FINDING (never a failure): a second
    line for the same physical with no "RF TX observation on <physical> recovered" line between (the statement is one line per failure run; a successful probe
    resets a run before its guard ends, so two runs can look like one) and any probe failed line at all (a healthy run has none)."""
    parsed, bad = parse_probe_failed(text)
    problems, findings = [], []
    for no, line in bad:
        problems.append("line %d contains 'probe failed' but not the shape 'RF TX fenced on <physical>: probe failed part=<part>; recovery is automatic': %s" % (no, line))
    for item in parsed:
        if item.part not in PROBE_PARTS:
            problems.append("line %d: part=%s is outside the fixed set %s" % (item.no, item.part, list(PROBE_PARTS)))
        if physicals is not None and item.physical not in physicals:
            problems.append("line %d: physical %s is not a configured physical %s" % (item.no, item.physical, sorted(physicals)))
        if item.tail != LOG_TAIL:
            problems.append("line %d: the line continues with %r, expected exactly %r (no other text may enter it)" % (item.no, item.tail, LOG_TAIL))
    recovered = [(no, m.group("physical")) for no, line in enumerate(text.splitlines(), 1) for m in [RECOVERED_RE.search(line)] if m]
    last = {}
    for item in parsed:
        prev = last.get(item.physical)
        if prev is not None and not any(prev.no < no < item.no and phys == item.physical for no, phys in recovered):
            findings.append("lines %d and %d both say probe failed for %s with no recovery line between: one line per failure run is stated" % (
                prev.no, item.no, item.physical))
        last[item.physical] = item
    if parsed:
        findings.append("%d probe failed line(s) in the SUT log: %s" % (len(parsed), [(i.no, i.physical, i.part) for i in parsed[:6]]))
    return LogJudgement(problems, findings, parsed)


def configured_physicals(doc):
    """The set of `physical` names of the AX.25 and radio ports of a rendered dv-router.toml (a dict), or None when the file has no port with one."""
    edges = (doc or {}).get("edges") or {}
    names = set()
    for edge in ("ax25", "radio"):
        section = edges.get(edge)
        if isinstance(section, dict):
            for port in section.get("port") or []:
                if isinstance(port, dict) and isinstance(port.get("physical"), str):
                    names.add(port["physical"])
    return names or None


# ================================================================ (1) the view shows a mid-run fence within 2 s

Visibility = namedtuple("Visibility", "state latency_s fence reads")
# state: fenced (a fenced read after the cause) | already_fenced (fenced at the first read, before the cause) | never (not fenced within max_wait_s) | scope


def view_latency(read, cause, *, clock=time.monotonic, sleep=time.sleep, poll_s=0.25, max_wait_s=10.0):
    """After the case produced a cause (`cause()` returns once it has taken effect and gives the clock value of the cause), poll `read()` (a txguard Observation)
    until the port is fenced; the latency is counted from the cause. The first read BEFORE the cause must be unfenced (state already_fenced otherwise)."""
    first = read()
    if first.scope:
        return Visibility("scope", None, None, 1)
    if first.found and first.fenced:
        return Visibility("already_fenced", None, first.fence, 1)
    t_cause = cause()
    reads = 1
    while True:
        obs = read()
        reads += 1
        now = clock()
        if obs.scope:
            return Visibility("scope", None, None, reads)
        if obs.found and obs.fenced:
            return Visibility("fenced", now - t_cause, obs.fence, reads)
        if now - t_cause >= max_wait_s:
            return Visibility("never", None, None, reads)
        sleep(poll_s)


def judge_latency(vis, *, max_s=VIEW_LATENCY_MAX_S):
    """Problems for a Visibility: not fenced at all, fenced before the case could have caused it, or later than `max_s` after the cause."""
    if vis.state == "scope":
        return ["the fence view carries no `modem` object (operator or admin needed)"]
    if vis.state == "already_fenced":
        return ["the port was already fenced (%s) before the cause: the precondition of the case is not met" % (vis.fence,)]
    if vis.state == "never":
        return ["the mid-run fence never showed in the status view"]
    if vis.latency_s > max_s:
        return ["the mid-run fence (%s) showed %.2f s after the cause, bound %.1f s" % (vis.fence, vis.latency_s, max_s)]
    return []


# ================================================================ host scan of the SUT log: python3 -m dvtest.rfsafe1c out/<run>

def scan_run(out_dir):
    """(problems, findings, files) for the SUT logs of a run directory, with the physicals of the rendered config when it is there."""
    from dvtest import logscan
    doc = {}
    path = os.path.join(out_dir, "xlxd-config", "xlxd-sut", "dv-router.toml")
    try:
        import tomllib
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, ValueError, ImportError):
        doc = {}
    physicals = configured_physicals(doc)
    problems, findings, files = [], [], logscan.log_files(out_dir)
    for log in files:
        with open(log, encoding="utf-8", errors="replace") as fh:
            judged = judge_log(fh.read(), physicals)
        name = os.path.basename(log)
        problems += ["%s: %s" % (name, p) for p in judged.problems]
        findings += ["%s: %s" % (name, f) for f in judged.findings]
    return problems, findings, files, physicals


def main(argv=None, out=None):
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("usage: python3 -m dvtest.rfsafe1c out/<run>   (RFSAFE1c: the probe failed log lines of the SUT logs; exit 1 on a line that breaks the shape)", file=out)
        return 2
    problems, findings, files, physicals = scan_run(args[0])
    if not files:
        print("rfsafe1c: no SUT log under %s/logs" % args[0], file=out)
        return 2
    print("rfsafe1c: %d SUT log file(s); configured physicals %s" % (len(files), sorted(physicals) if physicals else "unknown (rendered config not found)"), file=out)
    for f in findings:
        print("rfsafe1c: FINDING %s" % f, file=out)
    for p in problems:
        print("rfsafe1c: FAIL %s" % p, file=out)
    print("rfsafe1c: %s" % ("FAIL" if problems else "ok"), file=out)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
