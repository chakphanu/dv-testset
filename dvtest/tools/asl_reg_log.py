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
import sys

CLASSES = ("allstar_client_registration", "allstar_call_setup", "allstar_call_hangup")
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.tools.asl_reg_log", description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    ap.add_argument("--expect-registrations", type=int, default=0)
    args = ap.parse_args(argv)
    with open(args.log, encoding="utf-8", errors="replace") as fh:
        result = scan(fh.read().splitlines())
    for name, n in result["counts"].items():
        print("%-30s %d" % (name, n))
    for n, secret in result["leaks"]:
        print("LEAK line %d contains a secret (%d chars)" % (n, len(secret)))
    bad = bool(result["leaks"]) or result["counts"]["allstar_client_registration"] < args.expect_registrations
    print("FAIL" if bad else "ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
