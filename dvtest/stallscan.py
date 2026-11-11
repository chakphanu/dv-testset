# SPDX-License-Identifier: MIT
"""WEBSTALL1 evidence: what the host and the audit log say about a web request that took longer than 1 s.

dv-svc-web has one tokio worker and a login writes its audit record with a synchronous fsync on it (dv-router analysis 252): a host I/O
stall holds every request. For each slow request of a load run this module adds

  * the I/O pressure of the host in that window (/proc/pressure/io `some` and `full` stall time, microseconds, from a 1 Hz sampler), and
  * the timestamp of the nearest login in the SUT audit log (sut-state/web/audit.jsonl, `ts_ms`) and how far it is from the request.

The load case calls `annotate` at the end of a run (the audit file is read when it is there); after a run

  python3 -m dvtest.stallscan out/<run>

prints the same table from web/load.json and sut-state/web/audit.jsonl. Stdlib only.
"""
import argparse
import json
import os
import sys

PSI_PATH = "/proc/pressure/io"
AUDIT_REL = os.path.join("sut-state", "web", "audit.jsonl")
LOAD_REL = os.path.join("web", "load.json")


def parse_psi(text):
    """{"some_us", "full_us", "some_avg10", "full_avg10"} of a /proc/pressure/io text; None when it has no `some` line."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        kv = dict(p.partition("=")[::2] for p in parts[1:])
        try:
            out[parts[0] + "_us"] = int(kv["total"])
            out[parts[0] + "_avg10"] = float(kv.get("avg10", 0.0))
        except (KeyError, ValueError):
            continue
    return out if "some_us" in out else None


def read_psi(path=PSI_PATH):
    """parse_psi of the file, None when the kernel has no PSI or the file cannot be read."""
    try:
        with open(path, encoding="ascii") as fh:
            return parse_psi(fh.read())
    except OSError:
        return None


def psi_delta(rows, start, end):
    """Stall time added between two wall-clock instants: (some_us, full_us) from rows [epoch, some_us, full_us] (cumulative totals), taken from the
    last row at or before `start` to the first row at or after `end`; None when fewer than two rows bracket the window."""
    before = [r for r in rows if r[0] <= start]
    after = [r for r in rows if r[0] >= end]
    if not before or not after:
        return None
    a, b = before[-1], after[0]
    return (b[1] - a[1], b[2] - a[2])


def load_audit(path):
    """The records of an audit.jsonl (a bad line is skipped); [] when the file is absent."""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def nearest_login(audit, epoch_s, action="login"):
    """The audit record of `action` closest in time to `epoch_s` as {"ts", "delta_s" (record minus request, negative = before), "result"}; None
    without one."""
    best = None
    for row in audit:
        ts = row.get("ts_ms")
        if row.get("action") != action or not isinstance(ts, (int, float)):
            continue
        delta = ts / 1000.0 - epoch_s
        if best is None or abs(delta) < abs(best["delta_s"]):
            best = {"ts": round(ts / 1000.0, 3), "delta_s": round(delta, 3), "result": row.get("result")}
    return best


def annotate(items, psi_rows, audit):
    """Add io_some_ms, io_full_ms (stall time of the host in [start, start + seconds]) and nearest_login to each slow item (dicts with start and
    seconds); returns the items."""
    for item in items:
        start, seconds = item.get("start"), item.get("seconds")
        if start is None or seconds is None:
            continue
        delta = psi_delta(psi_rows, start, start + seconds)
        item["io_some_ms"] = None if delta is None else round(delta[0] / 1000.0, 1)
        item["io_full_ms"] = None if delta is None else round(delta[1] / 1000.0, 1)
        item["nearest_login"] = nearest_login(audit, start)
    return items


def audit_candidates(out_dir=None):
    """Where the web audit log may be read from a case (the run's sut-state directory)."""
    out_dir = out_dir or os.environ.get("DVT_OUT")
    return [os.path.join(out_dir, AUDIT_REL)] if out_dir else []


def describe(item):
    """One line for a slow item that has been annotated."""
    near = item.get("nearest_login")
    login = "no login in the audit log" if not near else "nearest login %+.3f s (%s)" % (near["delta_s"], near.get("result"))
    io = "io pressure not sampled" if item.get("io_some_ms") is None else "host io stall some %s ms / full %s ms" % (item["io_some_ms"], item["io_full_ms"])
    return "%s %s %.2f s from %.3f: %s; %s; dv-svc-web cpu %s %%" % (
        item.get("method"), item.get("path"), item.get("seconds") or 0.0, item.get("start") or 0.0, login, io, item.get("web_cpu_pct_during"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="WEBSTALL1: slow web requests of a load run with the host I/O pressure and the nearest audit login")
    ap.add_argument("out", help="out/<run>")
    args = ap.parse_args(argv)
    try:
        with open(os.path.join(args.out, LOAD_REL), encoding="utf-8") as fh:
            cases = json.load(fh).get("cases", {})
    except (OSError, ValueError) as exc:
        print("stallscan: cannot read %s: %s" % (os.path.join(args.out, LOAD_REL), exc), file=sys.stderr)
        return 2
    audit = load_audit(os.path.join(args.out, AUDIT_REL))
    shown = 0
    for name, case in sorted(cases.items()):
        slow = (case.get("slow_requests") or {}).get("items") or []
        rows = (case.get("io_pressure") or {}).get("series") or []
        if not slow:
            continue
        print("%s: %d slow requests (%s audit records)" % (name, (case.get("slow_requests") or {}).get("count", len(slow)), len(audit)))
        for item in annotate(slow, rows, audit):
            print("  " + describe(item))
            shown += 1
    if not shown:
        print("stallscan: no slow request in %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
