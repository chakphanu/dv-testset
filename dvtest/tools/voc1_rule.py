# SPDX-License-Identifier: MIT
"""VOC1 decision rule: may a vocoder image (the candidate) replace the production image on the same SUT image?

Input: the rows of cases/test_transcoder_levels.py (out/<run>/transcoder-levels.jsonl, one run per transcoder image tag, same SUT image)
of the candidate and of the production tag, and optionally the 16-stream load files of both runs. Output: a table of the four rules with
PASS / FAIL (or NO DATA) per rule and an overall verdict. The rule text is the decision of the DV-Router lead (VOC1, 2026-11-07):

  1. Delivery: frames received >= sent on every pair and content check ok on every row (tone and speech), candidate run.
  2. Speech: for each of the 9 pairs the mean active-level delta to the source over the 9 speech signals is within 0.5 dB of the
     production tag, or the pair moved TOWARD the source (|mean delta| decreases); no pair gets farther by more than 0.5 dB.
  3. Tone (Codec2 sources only): Codec2 -> DMR and Codec2 -> D-STAR 700 Hz rms (amp 8000) rises above production and reaches at least
     900 rms (target about 1260 = Codec2's own level); every pair whose source is not Codec2 stays identical (0.0 dB, tone and
     speech rows) and the 150/150 tone-frame delivery holds.
  4. Load: the 16-stream case completes 16/16 and the transcoder CPU (mean of the docker stats window) is within 2x of production.

  python3 -m dvtest.tools.voc1_rule --candidate CAND.jsonl --production PROD.jsonl \\
      [--candidate-load CAND-load.json --candidate-cpu CAND-cpu.txt --production-load PROD-load.json --production-cpu PROD-cpu.txt] [--json]

Exit status: 0 = ship the candidate (all four rules PASS), 1 = do not ship (a rule failed or has no data), 2 = unusable input.
Rule 4 has no data without the four load files; the verdict is then "do not ship" (the decision needs all four rules)."""
import argparse
import json
import sys

from dvtest import crossaudio

SOURCE_OF_CODEC2 = "m17"
PAIRS = ("m17>dmr", "m17>dstar", "m17>ysf", "dmr>dstar", "dmr>m17", "dmr>ysf", "dstar>dmr", "dstar>m17", "dstar>ysf")
SPEECH_SIGNALS = 9
SPEECH_WINDOW_DB = 0.5          # rule 2: unchanged within this many dB
SPEECH_FARTHER_DB = 0.5         # rule 2: no pair may get farther than this
TONE_PAIRS = ("m17>dmr", "m17>dstar")
TONE_MIN_RMS = 900              # rule 3: accept (within 3 dB of the target)
TONE_TARGET_RMS = 1260          # rule 3: Codec2's own level (LEVELS.md:42 of the vocoder delivery)
TONE_FRAMES = 150               # rule 3: tone frames sent per over
IDENTICAL_DB = 0.05             # rule 3: rows are rounded to 0.1 dB, so this means equal
LOAD_STREAMS = 16
CPU_FACTOR = 2.0

PASS, FAIL, NODATA = "PASS", "FAIL", "NO DATA"


class InputError(Exception):
    pass


def short(tag):
    """The image tag without repository and prefix (the same as dvtest.tools.levels_table)."""
    return tag.split(":")[-1].replace("64s-pcm-", "")


def read_rows(paths, what):
    """The rows of one side, the last row wins per (pair, signal); one tag only."""
    rows = {}
    tags = set()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    tags.add(short(row["tag"]))
                    rows[(row["pair"], row["signal"])] = row
        except (OSError, ValueError, KeyError) as exc:
            raise InputError("%s: cannot read %s: %r" % (what, path, exc))
    if not rows:
        raise InputError("%s: no rows" % what)
    if len(tags) != 1:
        raise InputError("%s: rows of %d tags (%s), one tag per side" % (what, len(tags), ", ".join(sorted(tags))))
    return rows, tags.pop()


def by_pair(rows):
    """{pair: {"tone": row or None, "speech": {signal: row}}}"""
    out = {}
    for (pair, signal), row in rows.items():
        slot = out.setdefault(pair, {"tone": None, "speech": {}})
        if signal == "tone":
            slot["tone"] = row
        else:
            slot["speech"][signal] = row
    return out


def mean(values):
    return sum(values) / len(values) if values else 0.0


def rule1(cand):
    """Delivery and content on every row of the candidate."""
    bad, n = [], 0
    for (pair, signal), r in sorted(cand.items()):
        n += 1
        if r["got"] < r["sent"]:
            bad.append("%s %s: %d of %d frames" % (pair, signal, r["got"], r["sent"]))
        elif not r.get("ok"):
            bad.append("%s %s: content check failed" % (pair, signal))
    missing = [p for p in PAIRS if not any(k[0] == p for k in cand)]
    for p in missing:
        bad.append("%s: no rows" % p)
    return (FAIL if bad else PASS), "%d rows, %d problems" % (n, len(bad)), bad


def speech_means(pairs, who):
    """{pair: mean speech delta dB}; every pair needs the nine speech signals."""
    out, bad = {}, []
    for p in PAIRS:
        sp = pairs.get(p, {"speech": {}})["speech"]
        if len(sp) != SPEECH_SIGNALS:
            bad.append("%s %s: %d speech signals, expected %d" % (who, p, len(sp), SPEECH_SIGNALS))
            continue
        out[p] = mean([r["delta_db"] for r in sp.values()])
    return out, bad


def rule2(cand_pairs, prod_pairs):
    cm, bad = speech_means(cand_pairs, "candidate")
    pm, bad2 = speech_means(prod_pairs, "production")
    bad += bad2
    detail = []
    for p in PAIRS:
        if p not in cm or p not in pm:
            continue
        c, q = cm[p], pm[p]
        moved = c - q
        farther = abs(c) - abs(q)
        if abs(moved) <= SPEECH_WINDOW_DB:
            state, ok = "same", farther <= SPEECH_FARTHER_DB
        elif abs(c) < abs(q):
            state, ok = "toward", True
        else:
            state, ok = "AWAY", False
        if not ok:
            bad.append("%s: speech mean delta %+.1f dB vs production %+.1f dB (%s, change %+.1f dB)" % (p, c, q, state, moved))
        detail.append({"pair": p, "candidate": round(c, 2), "production": round(q, 2), "change": round(moved, 2), "state": state, "ok": ok})
    return (FAIL if bad else PASS), "%d pairs, %d problems" % (len(detail), len(bad)), bad, detail


def rule3(cand_pairs, prod_pairs):
    bad, detail = [], []
    for p in TONE_PAIRS:
        c = cand_pairs.get(p, {}).get("tone")
        q = prod_pairs.get(p, {}).get("tone")
        if c is None or q is None:
            bad.append("%s: no tone row" % p)
            continue
        ok = c["rms"] >= TONE_MIN_RMS and c["rms"] > q["rms"]
        detail.append({"pair": p, "candidate_rms": c["rms"], "production_rms": q["rms"], "ok": ok})
        if c["rms"] <= q["rms"]:
            bad.append("%s: tone rms %d does not rise above production %d" % (p, c["rms"], q["rms"]))
        elif c["rms"] < TONE_MIN_RMS:
            bad.append("%s: tone rms %d rises but stays below %d (target about %d)" % (p, c["rms"], TONE_MIN_RMS, TONE_TARGET_RMS))
    for p in PAIRS:
        if p.split(">")[0] == SOURCE_OF_CODEC2:
            continue
        cp, qp = cand_pairs.get(p), prod_pairs.get(p)
        if cp is None or qp is None:
            bad.append("%s: no rows" % p)
            continue
        rows = [("tone", cp["tone"], qp["tone"])] + [(s, cp["speech"].get(s), qp["speech"].get(s)) for s in sorted(qp["speech"])]
        for name, c, q in rows:
            if c is None or q is None:
                bad.append("%s %s: row missing on one side" % (p, name))
            elif abs(c["delta_db"] - q["delta_db"]) > IDENTICAL_DB:
                bad.append("%s %s: delta %+.1f dB vs production %+.1f dB (non-Codec2 source must stay identical)" % (p, name, c["delta_db"], q["delta_db"]))
    for p, slot in sorted(cand_pairs.items()):
        t = slot["tone"]
        if t is not None and (t["sent"] != TONE_FRAMES or t["got"] < TONE_FRAMES):
            bad.append("%s tone: %d/%d frames, expected %d/%d" % (p, t["got"], t["sent"], TONE_FRAMES, TONE_FRAMES))
    return (FAIL if bad else PASS), "%d problems" % len(bad), bad, detail


def rule3_identical(cand_pairs, prod_pairs):
    """Rule 3 for a carrier image that must NOT change the levels (the unity tag): every row of every pair, the Codec2 tones
    included, equals production (delta within IDENTICAL_DB; tone rms within 1 percent), and the tone frames are all delivered."""
    bad, detail = [], []
    for p in PAIRS:
        cp, qp = cand_pairs.get(p), prod_pairs.get(p)
        if cp is None or qp is None:
            bad.append("%s: no rows" % p)
            continue
        rows = [("tone", cp["tone"], qp["tone"])] + [(sg, cp["speech"].get(sg), qp["speech"].get(sg)) for sg in sorted(qp["speech"])]
        for name, c, q in rows:
            if c is None or q is None:
                bad.append("%s %s: row missing on one side" % (p, name))
                continue
            if abs(c["delta_db"] - q["delta_db"]) > IDENTICAL_DB:
                bad.append("%s %s: delta %+.1f dB vs production %+.1f dB (a carrier must stay identical)" % (p, name, c["delta_db"], q["delta_db"]))
            if name == "tone":
                same = abs(c["rms"] - q["rms"]) <= 0.01 * max(q["rms"], 1)
                detail.append({"pair": p, "candidate_rms": c["rms"], "production_rms": q["rms"], "ok": same})
                if not same:
                    bad.append("%s tone: rms %d vs production %d (a carrier must stay identical)" % (p, c["rms"], q["rms"]))
                if c["sent"] != TONE_FRAMES or c["got"] < TONE_FRAMES:
                    bad.append("%s tone: %d/%d frames, expected %d/%d" % (p, c["got"], c["sent"], TONE_FRAMES, TONE_FRAMES))
    return (FAIL if bad else PASS), "%d problems" % len(bad), bad, detail


def read_load(load_path, cpu_path, what):
    """(streams ok, streams, cpu mean, cpu max) of a load run, or raises InputError."""
    try:
        with open(load_path, encoding="utf-8") as fh:
            lines = [line for line in fh.read().splitlines() if line.strip()]
        load = json.loads(lines[-1])
        with open(cpu_path, encoding="utf-8") as fh:
            summary = crossaudio.load_summary(load, fh.read().splitlines())
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise InputError("%s load: cannot read %s / %s: %r" % (what, load_path, cpu_path, exc))
    return summary


def rule4(cand_load, prod_load):
    """cand_load / prod_load: load_summary dicts or None."""
    if cand_load is None or prod_load is None:
        return NODATA, "needs the load and cpu files of both runs", []
    bad = []
    if cand_load["streams"] != LOAD_STREAMS or cand_load["streams_ok"] != LOAD_STREAMS:
        bad.append("candidate: %d of %d streams complete, expected %d/%d" % (cand_load["streams_ok"], cand_load["streams"], LOAD_STREAMS, LOAD_STREAMS))
    if cand_load["cpu_samples"] == 0 or prod_load["cpu_samples"] == 0:
        bad.append("no cpu samples in the load window (candidate %d, production %d)" % (cand_load["cpu_samples"], prod_load["cpu_samples"]))
    elif cand_load["cpu_mean"] > CPU_FACTOR * prod_load["cpu_mean"]:
        bad.append("cpu mean %.1f %% is more than %.0fx the production %.1f %%" % (cand_load["cpu_mean"], CPU_FACTOR, prod_load["cpu_mean"]))
    note = "%d/%d streams, cpu mean %.1f %% (max %.1f) vs production %.1f %% (max %.1f)" % (
        cand_load["streams_ok"], cand_load["streams"], cand_load["cpu_mean"], cand_load["cpu_max"], prod_load["cpu_mean"], prod_load["cpu_max"])
    return (FAIL if bad else PASS), note, bad


def decide(cand_rows, prod_rows, cand_load=None, prod_load=None, identical_ok=False):
    """The four rules. Returns {"rules": [{"n", "name", "status", "note", "problems"}], "verdict", "ship", "speech", "tone"}."""
    cand_pairs, prod_pairs = by_pair(cand_rows), by_pair(prod_rows)
    s1, n1, p1 = rule1(cand_rows)
    s2, n2, p2, speech = rule2(cand_pairs, prod_pairs)
    s3, n3, p3, tone = (rule3_identical if identical_ok else rule3)(cand_pairs, prod_pairs)
    s4, n4, p4 = rule4(cand_load, prod_load)
    rules = [
        {"n": 1, "name": "delivery and content", "status": s1, "note": n1, "problems": p1},
        {"n": 2, "name": "speech levels", "status": s2, "note": n2, "problems": p2},
        {"n": 3, "name": ("every row identical to production (carrier image)" if identical_ok else "tone (Codec2 sources) and AMBE pairs unchanged"), "status": s3, "note": n3, "problems": p3},
        {"n": 4, "name": "load 16/16 and cpu within 2x", "status": s4, "note": n4, "problems": p4},
    ]
    ship = all(r["status"] == PASS for r in rules)
    if ship:
        verdict = "SHIP the candidate (all four rules pass%s)" % ("; carrier image, identical to production" if identical_ok else "")
    else:
        verdict = "DO NOT SHIP (%s)" % ", ".join("rule %d %s" % (r["n"], r["status"]) for r in rules if r["status"] != PASS)
    return {"rules": rules, "verdict": verdict, "ship": ship, "speech": speech, "tone": tone}


def format_result(result, cand_tag, prod_tag):
    lines = ["VOC1 rule: candidate %s against production %s" % (cand_tag, prod_tag), "",
             "| rule | check | result | detail |", "|---|---|---|---|"]
    for r in result["rules"]:
        lines.append("| %d | %s | %s | %s |" % (r["n"], r["name"], r["status"], r["note"]))
    lines += ["", "Speech, mean active-level delta to the source over the 9 speech signals (dB):", "",
              "| pair | production | candidate | change | state |", "|---|---|---|---|---|"]
    for d in result["speech"]:
        lines.append("| %s | %+.1f | %+.1f | %+.1f | %s |" % (d["pair"], d["production"], d["candidate"], d["change"], d["state"]))
    lines += ["", "Tone, Codec2 sources (700 Hz, amp 8000; accept >= %d rms, target about %d):" % (TONE_MIN_RMS, TONE_TARGET_RMS), "",
              "| pair | production rms | candidate rms | result |", "|---|---|---|---|"]
    for d in result["tone"]:
        lines.append("| %s | %d | %d | %s |" % (d["pair"], d["production_rms"], d["candidate_rms"], PASS if d["ok"] else FAIL))
    problems = [(r["n"], p) for r in result["rules"] for p in r["problems"]]
    if problems:
        lines += ["", "Problems:"] + ["- rule %d: %s" % (n, p) for n, p in problems]
    lines += ["", "Verdict: " + result["verdict"]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="VOC1 decision rule for a vocoder image (docs/transcoder-levels.md)")
    ap.add_argument("--candidate", nargs="+", required=True, metavar="JSONL", help="transcoder-levels.jsonl of the candidate run")
    ap.add_argument("--production", nargs="+", required=True, metavar="JSONL", help="transcoder-levels.jsonl of the production run")
    ap.add_argument("--candidate-load", metavar="JSON", help="transcoder-levels-load.json of the candidate run")
    ap.add_argument("--candidate-cpu", metavar="TXT", help="transcoder-cpu-sample.sh file of the candidate run")
    ap.add_argument("--production-load", metavar="JSON")
    ap.add_argument("--production-cpu", metavar="TXT")
    ap.add_argument("--identical-ok", action="store_true", help="carrier image (the unity tag): rule 3 passes when EVERY row, the Codec2 tones included, is identical to production, instead of requiring the tone fix")
    ap.add_argument("--json", action="store_true", help="print the result as JSON instead of the table")
    args = ap.parse_args(argv)
    try:
        cand, cand_tag = read_rows(args.candidate, "candidate")
        prod, prod_tag = read_rows(args.production, "production")
        loads = []
        for side, load, cpu in (("candidate", args.candidate_load, args.candidate_cpu), ("production", args.production_load, args.production_cpu)):
            if bool(load) != bool(cpu):
                raise InputError("%s: give both the load file and the cpu file or neither" % side)
            loads.append(read_load(load, cpu, side) if load else None)
    except InputError as exc:
        print("voc1_rule: %s" % exc, file=sys.stderr)
        return 2
    result = decide(cand, prod, loads[0], loads[1], identical_ok=args.identical_ok)
    result["candidate_tag"], result["production_tag"] = cand_tag, prod_tag
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=1))
    else:
        print(format_result(result, cand_tag, prod_tag))
    return 0 if result["ship"] else 1


if __name__ == "__main__":
    sys.exit(main())
