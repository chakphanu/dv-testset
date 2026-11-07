# SPDX-License-Identifier: MIT
"""Post-run check of the ASL1h log lines of the AllStar edge (one INFO line per call setup, hangup and client registration, no
secrets; DV-Router deployment.md has the grep commands). run.sh writes out/<run>/logs/xlxd-sut.log only at the end of a run, so
the cases cannot read it; run this afterwards:

  python3 -m dvtest.tools.asl_reg_log out/<run>/logs/xlxd-sut.log [--expect-registrations N]

It counts the lines per class, fails (exit 1) when a fixture secret or a typed wrong secret appears in any line of the log, and
with --expect-registrations N when fewer than N `allstar_client_registration` lines are there. The exact line layout is DV-Router's
(not asserted here): a line is recognised by its class name.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dvtest import asl_register

CLASSES = ("allstar_client_registration", "allstar_call event=setup", "allstar_call event=hangup")   # as written by wip-c165c93
FORBIDDEN = ("test-secret-not-real", "wrong-secret-not-real")     # the lab fixture secret and the wrong proof of cases R9 / R10


def scan(lines, forbidden=FORBIDDEN):
    """{"counts": {class: n}, "leaks": [(line number, which secret)]} for the lines of a log."""
    counts = {name: 0 for name in CLASSES}
    leaks = []
    for n, line in enumerate(lines, 1):
        for name in CLASSES:
            if name in line:
                counts[name] += 1
        for secret in forbidden:
            if secret in line:
                leaks.append((n, secret))
    return {"counts": counts, "leaks": leaks}


# ---------------------------------------------------------------- registration failure lines (ASL1i / ASL1j, wip-1590f26)
# `level=warn event=allstar_registration_failed node=2993 result=http http_status=500 error="..." next_retry_delay_ms=120000`, once per
# failed attempt that the limiter admits (first failure, then one per node and result class every 300 s: design doc lines 726-737).
# The tester cannot read the SUT log (run.sh writes logs/xlxd-sut.log after pytest ends), so the case checks of these lines run
# afterwards through `--register DIR` (below) or, for a finished run, through DVT_ASL_SUT_LOG (case R13 of cases/test_asl_edge.py).

FAILED_RE = re.compile(r'event=allstar_registration_failed\s+node=(\d+)\s+result=(\w+)\s+http_status=(\S+)\s+'
                       r'error="([^"]*)"\s+next_retry_delay_ms=(\d+)')
STAMP_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z")
REG_LINE_MARKS = ("allstar_registration", "allstar_register", "registrar")        # the registration logging
FORBIDDEN_TOKENS = ("test-secret-not-real", "wrong-secret-not-real", "passwd", "password", ".pw", "asl-secrets", "://")
# the lab's own start-up warning (insecure = true); it names no secret and, on wip-1590f26, no URL either: whitelisted by its text
ALLOWED_WARNING = re.compile(r"WARNING: AllStar registrar insecure = true: LAB CLEARTEXT HTTP sends portal credentials without encryption")


def stamp(line):
    """Epoch seconds of the first ISO timestamp (docker --timestamps, UTC 'Z') in a line, None without one."""
    m = STAMP_RE.search(line)
    if not m:
        return None
    whole = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return whole + (float("0." + m.group(2)) if m.group(2) else 0.0)


def parse_failures(lines):
    """The allstar_registration_failed lines as dicts: line, t (epoch or None), node, result, http_status (int or None), error,
    delay_s (next_retry_delay_ms / 1000)."""
    out = []
    for n, line in enumerate(lines, 1):
        m = FAILED_RE.search(line)
        if m:
            out.append({"line": n, "t": stamp(line), "node": m.group(1), "result": m.group(2),
                        "http_status": None if m.group(3) == "-" else int(m.group(3)), "error": m.group(4),
                        "delay_s": int(m.group(5)) / 1000.0})
    return out


def leak_problems(lines, forbidden=FORBIDDEN_TOKENS):
    """Problems (strings) for the registration logging lines (any line that mentions allstar_registration, allstar_register or
    registrar): none may contain a password (fixture value, 'passwd', 'password'), a password file name or directory, or a URL ('://').
    Only the lab's own insecure = true start-up warning is exempt, matched by its exact text."""
    problems = []
    for n, line in enumerate(lines, 1):
        if not any(mark in line for mark in REG_LINE_MARKS) or ALLOWED_WARNING.search(line):
            continue
        low = line.lower()
        for token in forbidden:
            if token.lower() in low:
                problems.append("line %d of the registration logging contains %r" % (n, "<secret>" if "secret" in token else token))
    return problems


def judge_node(node, request_times, failures, result, process_start=None, timeout=False, delay_fn=None):
    """Problems (strings) of one node's failure lines against its registrar-side request times (sorted epochs of the last edge
    process) and the rules of dvtest.asl_register.

    For every attempt k (1-based) that is followed by another request, the line is judged to exist exactly when the limiter admits
    it (completion time = request time + attempt duration; a boundary within 2 s is not judged); a line found for attempt k has the
    result class `result`, a delay inside rule (a) for failure number k (or equal to `delay_fn(k)` seconds when given: rate_limited
    and mismatch have exact delays) and the next request arrives delay +/- 1 s after the line's own time. Lines at or after the last request are not judged (the
    attempt may not have completed, and later cases of the same run log more). Attempts that are not followed by a request cannot
    be judged."""
    problems = []
    times = sorted(request_times)
    lines = [f for f in failures if f["node"] == str(node) and f["t"] is not None
             and (process_start is None or f["t"] >= process_start - 1.0) and times[0] - 0.5 <= f["t"] < times[-1]]
    by_attempt = {}
    for f in lines:
        k = max((i for i, t in enumerate(times) if t <= f["t"] + 0.0005), default=0)    # the attempt this line belongs to (0-based)
        by_attempt.setdefault(k, []).append(f)
    a_lo, a_hi = asl_register.attempt_bounds(timeout)
    # completion time of each attempt: from its line when it has one, else request time + a typical attempt duration
    completion = [times[i] + (by_attempt[i][0]["t"] - times[i] if i in by_attempt else (a_lo + a_hi) / 2) for i in range(len(times) - 1)]
    admit = asl_register.limiter_admits(completion)
    for k, want in enumerate(admit):
        got = by_attempt.get(k, [])
        if len(got) > 1:
            problems.append("node %s attempt %d: %d failure lines (one per attempt at most)" % (node, k + 1, len(got)))
        ambiguous = any(abs((completion[k] - completion[j]) - asl_register.LOG_LIMIT_S) < 2.0 for j in range(k))
        if want and not got and not ambiguous:
            problems.append("node %s attempt %d: no allstar_registration_failed line (the limiter admits it)" % (node, k + 1))
        if not want and got and not ambiguous:
            problems.append("node %s attempt %d: a failure line inside the 300 s limit window" % (node, k + 1))
    for k, got in sorted(by_attempt.items()):
        if k >= len(times) - 1:
            continue
        f = got[0]
        if f["result"] != result:
            problems.append("node %s attempt %d: result=%s, expected %s" % (node, k + 1, f["result"], result))
        if delay_fn is not None:
            want_s = delay_fn(k + 1)
            if abs(f["delay_s"] - want_s) > 0.001:
                problems.append("node %s attempt %d: next_retry_delay_ms=%d, expected %d" % (node, k + 1, round(f["delay_s"] * 1000), want_s * 1000))
        else:
            lo, hi = asl_register.failure_delay_range(k + 1)
            if not lo - 0.001 <= f["delay_s"] <= hi + 0.001:
                problems.append("node %s failure %d: next_retry_delay_ms=%d outside %.0f..%.0f s" % (node, k + 1, round(f["delay_s"] * 1000), lo, hi))
        measured = times[k + 1] - f["t"]
        if abs(measured - f["delay_s"]) > asl_register.LOG_TOLERANCE_S:
            problems.append("node %s attempt %d: next request %.1f s after the failure line, logged delay %.3f s (+/- 1 s)"
                            % (node, k + 1, measured, f["delay_s"]))
        duration = f["t"] - times[k]
        if not a_lo - 0.5 <= duration <= a_hi + 0.5:
            problems.append("node %s attempt %d: the failure line came %.1f s after the request (attempt %.1f..%.1f s)"
                            % (node, k + 1, duration, a_lo, a_hi))
        if f["http_status"] is not None and not 100 <= f["http_status"] <= 599:
            problems.append("node %s attempt %d: http_status=%s" % (node, k + 1, f["http_status"]))
    return problems


def judge_run(lines, rows_by_node, process_start=None, interval_s=120.0):
    """All log problems of one run: the leak scan over the registration logging and judge_node for every node that has a result class in
    asl_register.REG_RESULTS. `rows_by_node` maps a node (str) to its request times (epoch)."""
    problems = leak_problems(lines)
    failures = parse_failures(lines)
    for node, result in asl_register.REG_RESULTS.items():
        times = rows_by_node.get(node) or []
        if len(times) < 2:
            problems.append("node %s: fewer than two recorded requests, nothing to judge" % node)
            continue
        fixed = None
        if result == "mismatch":
            fixed = lambda n: asl_register.success_delay_s(interval_s)
        elif result == "rate_limited":
            after = asl_register.RETRY_AFTER_S if node == "2997" else None
            fixed = lambda n, after=after: asl_register.rate_limited_delay(n, interval_s, after)
        problems += judge_node(node, times, failures, result, process_start, timeout=(result == "timeout"), delay_fn=fixed)
    return problems


def load_register_dir(directory):
    """({node: [request epochs]}, process_start, interval_s) from out/<run>/asl-lab/register/ (requests.jsonl, schedule.json)."""
    base = Path(directory)
    sched = json.loads((base / "schedule.json").read_text())
    rows = [json.loads(line) for line in (base / "requests.jsonl").read_text().splitlines() if line.strip()]
    by_node = {}
    for r in rows:
        node = asl_register.row_node(r)
        if node is not None:
            by_node.setdefault(node, []).append(r["t"])
    return {n: sorted(v) for n, v in by_node.items()}, sched.get("process_start"), float(sched.get("interval_s") or 120.0)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.tools.asl_reg_log", description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    ap.add_argument("--expect-registrations", type=int, default=0)
    ap.add_argument("--register", metavar="DIR", help="out/<run>/asl-lab/register: also judge the registration failure lines "
                    "(asl_register retry rules, 300 s limiter, no secret/URL in the registration logging)")
    args = ap.parse_args(argv)
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    result = scan(text.splitlines())
    for name, n in result["counts"].items():
        print("%-30s %d" % (name, n))
    for n, secret in result["leaks"]:
        print("LEAK line %d contains a secret (%d chars)" % (n, len(secret)))
    bad = bool(result["leaks"]) or result["counts"]["allstar_client_registration"] < args.expect_registrations
    if args.register:
        by_node, start, interval = load_register_dir(args.register)
        problems = judge_run(text.splitlines(), by_node, start, interval)
        for p in problems:
            print("PROBLEM " + p)
        bad = bad or bool(problems)
    print("FAIL" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
