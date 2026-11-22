# SPDX-License-Identifier: MIT
"""BUD1 / BUD1b checker: no legitimate body may be refused by a byte budget (DV-Router a15b08fa and 364a5760, merged in bc9af2b1; docs design/status.md, fabric.md "bodies";
K6b `crates/router/tests/real_children/body_budgets.rs`; image dv-router:wip-f1f3777).

A refusal is counted in dv_fabric_body_rejected_total{reason} of the refusing process (reasons query_reply_oversized, query_request_oversized, subscription_oversized in every process; admin also
hub_reply_oversize and admin_reply_oversize, web hub_reply_oversize and web_reply_oversize), and in the hub's own dv_status_body_rejected_total; the related drops are
dv_edge_drops_total{reason="body_oversized"}, dv_codec_body_dropped_total and dv_pkt_body_dropped_total{reason="oversized"}. In a suite of legitimate traffic every one of them must stay 0 in every
process: K6b asserts it for the exact set of processes of its smoke (launcher, a controller, a usrp edge, web, admin, recorder, basemap, the data worker) and fails on a process that published
without the complete family. The hub re-exports the newest record of each process with a `proc` label about every 10 s; a counter restarts at 0 with the process, so a refusal in a process that was
restarted later is only seen by a sample taken in between.

How it is read (docs/bud1-cases.md):

  * with DVT_BUD1=on a pytest session hook (dvtest/bud1_plugin.py, wired in cases/conftest.py) samples the hub metrics through the control channel every DVT_BUD1_EVERY_S seconds (default 60) into
    out/<run>/bud1/samples.jsonl (the non-zero refusal counters and the process set of each read) and, at the end of the session, after DVT_BUD1_SETTLE_S seconds (default 12: one telemetry
    period), writes the full final snapshot out/<run>/bud1/final-<service>.prom;
  * this module judges a run directory (or many, one per stage of a chain):

        python3 -m dvtest.bud1 out/<run> [out/<run2> ...]          exit 0 clean, 1 a refusal or an incomplete family, 2 no snapshot (was DVT_BUD1=on?)
        python3 -m dvtest.bud1 --lenient out/<run>                 an image without the family (before BUD1) is a note, not a failure

Pure and stdlib only (the clock is injectable where it matters; cases/unit/test_bud1_lib.py).
"""
import json
import os
import sys
from collections import namedtuple

from dvtest import metrics

SUBDIR = "bud1"
SAMPLES = "samples.jsonl"
FINAL_PREFIX, FINAL_SUFFIX = "final-", ".prom"
FAMILY = "dv_fabric_body_rejected_total"
ADAPTER_REASONS = ("query_reply_oversized", "query_request_oversized", "subscription_oversized")
STATUS_FAMILY = "dv_status_body_rejected_total"
# (family, label subset): every sample of these must be 0 in a suite of legitimate traffic
WATCHED = (
    (FAMILY, {}),
    (STATUS_FAMILY, {}),
    ("dv_edge_drops_total", {"reason": "body_oversized"}),
    ("dv_codec_body_dropped_total", {}),
    ("dv_pkt_body_dropped_total", {"reason": "oversized"}),
)
EVERY_S = 60.0
SETTLE_S = 12.0

Refusal = namedtuple("Refusal", "name labels value")


def strict_on(environ=None):
    """DVT_BUD1=on: the image has BUD1 / BUD1b (the in-run cases assert; the hook records)."""
    return (environ if environ is not None else os.environ).get("DVT_BUD1", "").lower() in ("on", "1", "true", "yes")


def _matches(sample, name, subset):
    return sample.name == name and all(sample.labels.get(k) == v for k, v in subset.items())


def refusals(samples):
    """Every sample of a WATCHED family that is not 0 (NaN counts as a problem), as Refusal tuples."""
    out = []
    for s in samples:
        for name, subset in WATCHED:
            if _matches(s, name, subset) and not (s.value == 0):
                out.append(Refusal(s.name, dict(s.labels), s.value))
    return out


def procs_of(samples):
    """The set of `proc` labels the hub exports (every family)."""
    return {s.labels["proc"] for s in samples if "proc" in s.labels}


def family_gaps(samples):
    """{proc: [missing adapter reasons]} for the processes the hub exports: K6b fails a process that published without the complete family. A proc with no sample of the family at
    all is listed with all three reasons."""
    have = {}
    for s in samples:
        if s.name == FAMILY and "proc" in s.labels:
            have.setdefault(s.labels["proc"], set()).add(s.labels.get("reason"))
    gaps = {}
    for proc in sorted(procs_of(samples)):
        missing = [r for r in ADAPTER_REASONS if r not in have.get(proc, set())]
        if missing:
            gaps[proc] = missing
    return gaps


def summarize(samples):
    """The record of one hub read for samples.jsonl: time is added by the caller."""
    return {"rejected": [[r.name, r.labels, r.value] for r in refusals(samples)], "procs": sorted(procs_of(samples)),
            "family": any(s.name == FAMILY for s in samples)}


# ================================================================ judging a run directory

Verdict = namedtuple("Verdict", "path problems findings notes finals samples")


def judge_dir(path, lenient=False):
    """Judge out/<run>/bud1. Problems (exit 1): a non-zero refusal in any sample or in a final snapshot; a process of the final snapshot without the complete adapter family (K6b); no
    BUD1 family at all unless `lenient`. Missing snapshot files are the caller's exit 2 (verdict.finals == 0 and verdict.samples == 0)."""
    problems, findings, notes = [], [], []
    base = os.path.join(path, SUBDIR)
    n_samples, n_finals = 0, 0
    first_seen = {}
    try:
        with open(os.path.join(base, SAMPLES), encoding="utf-8") as fh:
            for no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    findings.append("%s line %d is not JSON" % (SAMPLES, no))
                    continue
                n_samples += 1
                for name, labels, value in rec.get("rejected", []):
                    key = (rec.get("service"), name, json.dumps(labels, sort_keys=True))
                    if key not in first_seen:
                        first_seen[key] = (rec.get("t"), value)
    except OSError:
        pass
    for (service, name, labels), (t, value) in sorted(first_seen.items(), key=lambda kv: str(kv[0])):
        problems.append("%s: %s%s = %s at t=%s: a body was refused by its byte budget in a suite of legitimate traffic" % (service, name, labels, value, t))
    finals = []
    if os.path.isdir(base):
        finals = sorted(n for n in os.listdir(base) if n.startswith(FINAL_PREFIX) and n.endswith(FINAL_SUFFIX))
    for name in finals:
        n_finals += 1
        service = name[len(FINAL_PREFIX):-len(FINAL_SUFFIX)]
        with open(os.path.join(base, name), encoding="utf-8", errors="replace") as fh:
            samples = metrics.parse(fh.read())
        bad = refusals(samples)
        for r in bad:
            key = (service, r.name, json.dumps(r.labels, sort_keys=True))
            if key not in first_seen:
                problems.append("%s: final snapshot has %s%s = %s" % (service, r.name, json.dumps(r.labels, sort_keys=True), r.value))
        if not any(s.name == FAMILY for s in samples):
            (notes if lenient else problems).append("%s: no %s in the final snapshot: the image has no BUD1" % (service, FAMILY))
            continue
        if not any(s.name == STATUS_FAMILY for s in samples):
            findings.append("%s: no %s series (the hub's own counter, K6b reads exactly one)" % (service, STATUS_FAMILY))
        gaps = family_gaps(samples)
        for proc, missing in gaps.items():
            problems.append("%s: process %s exports no complete %s (missing reasons %s): K6b fails a process that published without the family" % (service, proc, FAMILY, missing))
        notes.append("%s: %d processes export the family, %d refusal series non-zero" % (service, len(procs_of(samples)), len(bad)))
    return Verdict(path, problems, findings, notes, n_finals, n_samples)


def main(argv=None, out=None):
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    lenient = "--lenient" in args
    paths = [a for a in args if not a.startswith("--")]
    if not paths or "-h" in args or "--help" in args:
        print("usage: python3 -m dvtest.bud1 [--lenient] out/<run> [out/<run> ...]   (BUD1: no byte-budget refusal in any process; exit 1 on a refusal, 2 without a snapshot)", file=out)
        return 2
    worst = 0
    for path in paths:
        verdict = judge_dir(path, lenient=lenient)
        if verdict.finals == 0 and verdict.samples == 0:
            print("bud1: %s: no snapshot under %s/%s (run with DVT_BUD1=on and the control channel)" % (path, path, SUBDIR), file=out)
            worst = max(worst, 2)
            continue
        print("bud1: %s: %d sample(s), %d final snapshot(s)" % (path, verdict.samples, verdict.finals), file=out)
        for n in verdict.notes:
            print("bud1:   note %s" % n, file=out)
        for f in verdict.findings:
            print("bud1:   FINDING %s" % f, file=out)
        for p in verdict.problems:
            print("bud1:   FAIL %s" % p, file=out)
        print("bud1: %s: %s" % (path, "FAIL" if verdict.problems else "ok"), file=out)
        if verdict.problems:
            worst = max(worst, 1)
        elif verdict.finals == 0:
            print("bud1: %s: samples only, no final snapshot (the session did not finish)" % path, file=out)
            worst = max(worst, 2)
    return worst


if __name__ == "__main__":
    sys.exit(main())
