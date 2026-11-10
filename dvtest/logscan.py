# SPDX-License-Identifier: MIT
"""Scan the SUT logs of a run for needles that must never be logged (a peer address, a packet body).

The SUT container logs are collected on the host after pytest ends, so a case cannot read them. A case calls
`request(out_dir, case, needles, ...)`: when the logs are already there it scans at once, otherwise it leaves a needle file
under <out>/logscan/ and the host runs, after the run:

  python3 -m dvtest.logscan out/<run>

Exit 0: every needle file scanned and clean; 1: a needle was found (file and line number go to stdout, the needle text too:
the needles are synthetic markers or a test address); 2: usage error, or a needle file whose logs are still missing.
Stdlib only.
"""
import argparse
import json
import os
import sys

SUBDIR = "logscan"
LOG_GLOB_DIR = "logs"
LOG_NAME_PART = "sut"


def log_files(out_dir):
    """The SUT log files of a run (out/<run>/logs/*sut*), sorted; [] when the directory is absent."""
    d = os.path.join(out_dir, LOG_GLOB_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, n) for n in os.listdir(d) if LOG_NAME_PART in n and os.path.isfile(os.path.join(d, n)))


def scan_text(text, needles, line_filter=None):
    """[(line number, needle)] for the lines of `text` that contain a needle. line_filter: lower-case substring a line must contain to be looked at."""
    hits = []
    for no, line in enumerate(text.splitlines(), 1):
        if line_filter and line_filter not in line.lower():
            continue
        for n in needles:
            if n and n in line:
                hits.append((no, n))
    return hits


def scan_files(paths, needles, line_filter=None):
    """{path: [(line number, needle)]} for the files with a hit; also returns the number of lines looked at as the second item."""
    found, looked = {}, 0
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        if line_filter:
            looked += sum(1 for l in text.splitlines() if line_filter in l.lower())
        else:
            looked += len(text.splitlines())
        hits = scan_text(text, needles, line_filter)
        if hits:
            found[p] = hits
    return found, looked


def request(out_dir, case, needles, line_filter=None):
    """Called from a case. Returns ("scanned", found, looked) when the logs exist, else ("pending", path of the needle file, 0).
    The needle file is written in both cases, so the host scan can be repeated."""
    needles = [n for n in needles if n]
    d = os.path.join(out_dir, SUBDIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % case)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"case": case, "needles": needles, "line_filter": line_filter}, fh, indent=1)
    files = log_files(out_dir)
    if not files:
        return "pending", path, 0
    found, looked = scan_files(files, needles, line_filter)
    return "scanned", found, looked


def command_for(out_dir):
    return "python3 -m dvtest.logscan %s" % out_dir


def run_dir(out_dir, out=None):
    """Scan every needle file of a run. Returns the exit code (0 clean, 1 hit, 2 error)."""
    out = out or sys.stdout
    d = os.path.join(out_dir, SUBDIR)
    if not os.path.isdir(d):
        print("logscan: no %s directory under %s (no case asked for a scan)" % (SUBDIR, out_dir), file=out)
        return 2
    files = log_files(out_dir)
    if not files:
        print("logscan: no SUT log under %s/%s" % (out_dir, LOG_GLOB_DIR), file=out)
        return 2
    rc = 0
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(d, name), encoding="utf-8") as fh:
            spec = json.load(fh)
        found, looked = scan_files(files, spec.get("needles", []), spec.get("line_filter"))
        print("logscan: %s: %d needle(s), %d line(s) in %d file(s): %s" % (
            spec.get("case", name), len(spec.get("needles", [])), looked, len(files), "FOUND" if found else "clean"), file=out)
        for p, hits in sorted(found.items()):
            for no, n in hits[:5]:
                print("logscan:   %s:%d contains %r" % (os.path.basename(p), no, n), file=out)
            rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.logscan")
    ap.add_argument("out_dir", help="results directory of one run, e.g. out/20261110T085447Z")
    args = ap.parse_args(argv)
    return run_dir(args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
