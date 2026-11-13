# SPDX-License-Identifier: MIT
"""Dire Wolf as the independent oracle of the DIGI1 cases (docs/digi1-cases.md, "The oracle").

The oracle is a REAL Dire Wolf 1.8.1 (image dvt-direwolf:local) with `MYCALL N0TEST-10`, `DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$` and `DEDUPE 30`
(User Guide 9.6.6, the fill-in rule), on the modem loop of docs/direwolf-cases.md: node A sends the vectors into its KISS port, node B
(the digipeater) demodulates them and repeats what its rules allow, node A demodulates the repeat and hands it to its KISS client. The
job `digi` of rig/direwolf-entry.py runs that in one throw-away container (no network, no rig); this module builds the job, assigns every
repeat to the step it follows, compares it with the model of dvtest.digi1 and reads and writes the vectors file
`vectors/direwolf/digi_oracle.json`.

  python3 -m dvtest.oracle_direwolf digi            (docker; about 5 minutes; writes the vectors file)

The rig cases (cases/test_digi1.py) compare the edge's bytes with these vectors when the file exists. The file is NOT in the repository
until the job has run (docs/digi1-cases.md, GAP 1). Pure parts, unit-tested in cases/unit/test_digi1_oracle.py.
"""
import json
import os

from dvtest import ax25, digi1

SCHEMA = "dvt-digi-oracle-1"
PATH = os.path.join("vectors", "direwolf", "digi_oracle.json")
MYCALL = digi1.OURS
EXTRA = ("DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$", "DEDUPE 30")
GAP_S = 6.0                     # between two vectors: air time of the frame, the repeat, and the deafness of a half duplex node (docs/direwolf-cases.md)
TIMELINE_NAME = "T"
JOB_TIMEOUT_S = 900


def vector_suffix(vid):
    """A per-vector information suffix so that no two vectors share a duplicate key."""
    return (" o%s" % vid).encode("ascii")


def plan(gap_s=GAP_S, timeline_s=digi1.TIMELINE_S):
    """The steps of the job: [{"name", "at", "vector" (Golden or None), "frame" (bytes), "kind"}]. First every path vector (G1..G13, G2b is
    skipped: our station call is not N0TEST), then the extra vectors, then the key vectors a gap apart, then one frame sent at each time of the
    duplicate timeline (the frame of the first step is the reference of the later ones)."""
    steps = []
    at = 2.0
    for g in list(digi1.GOLDENS) + list(digi1.extra_vectors()):
        steps.append({"name": g.id, "at": at, "vector": g, "frame": digi1.golden_frame(g, vector_suffix(g.id)), "kind": "path"})
        at += gap_s
    for kid, _what, frame, _expect in digi1.key_vectors():
        steps.append({"name": kid, "at": at, "vector": None, "frame": frame, "kind": "key"})
        at += gap_s
    frame = ax25.build_ui("APDVT1", "N0TEST-9", ["WIDE1-1"], b">timeline", cr_src=False)
    base = at + gap_s
    for i, t in enumerate(timeline_s):
        steps.append({"name": "%s%d" % (TIMELINE_NAME, i), "at": base + t, "vector": None, "frame": frame, "kind": "timeline"})
    return steps


def job(steps=None):
    """The JSON job for rig/direwolf-entry.py mode digi."""
    steps = plan() if steps is None else steps
    last = max(s["at"] for s in steps)
    return {"mode": "digi", "mycall": MYCALL, "extra": list(EXTRA), "settle_s": 12.0,
            "steps": [{"name": s["name"], "at": s["at"], "frame_hex": s["frame"].hex()} for s in steps], "duration_s": last + 12.0}


def assign(sent, heard):
    """{step name: [frame hex, ...]} - a repeat belongs to the last step sent at or before it (the next step is at least one gap later)."""
    ordered = sorted(sent, key=lambda s: s["t"])
    out = {s["name"]: [] for s in ordered}
    for h in sorted(heard, key=lambda x: x["t"]):
        owner = None
        for s in ordered:
            if s["t"] <= h["t"]:
                owner = s
        if owner is not None:
            out[owner["name"]].append(h["frame_hex"])
    return out


def records(result, steps):
    """The vectors file records of a job result: one per step with the repeats Dire Wolf gave, and the verdict against the model.
    `b_received` says whether the digipeater node heard the step's frame at all (a frame B never heard proves nothing about its rules)."""
    heard = assign(result.get("sent") or [], result.get("heard") or [])
    b_got = {h["frame_hex"] for h in result.get("b_heard") or []}
    sent_at = {s["name"]: s["t"] for s in result.get("sent") or []}
    out = []
    for s in steps:
        rec = {"case": s["name"], "kind": s["kind"], "at": sent_at.get(s["name"]), "sent_hex": s["frame"].hex(),
               "path": ax25.path_text(s["frame"]), "dw_heard_hex": heard.get(s["name"], []), "b_received": s["frame"].hex() in b_got}
        g = s["vector"]
        if g is not None:
            rec["model_result"] = g.result
            rec["dw_expected"] = g.dw
        out.append(rec)
    return out


def file_doc(result, steps, version=""):
    return {"schema": SCHEMA, "tool": version, "image": "dvt-direwolf:local", "mycall": MYCALL, "config": list(EXTRA),
            "records": records(result, steps)}


def write(doc, path=PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="ascii") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True, ensure_ascii=True)
        fh.write("\n")


def load(path=PATH, root=None):
    """{case: record} of the vectors file, or {} when it does not exist or has another schema."""
    full = os.path.join(root, path) if root else path
    try:
        with open(full, encoding="ascii") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if doc.get("schema") != SCHEMA:
        return {}
    return {r["case"]: r for r in doc.get("records") or []}


def dw_outcome(rec, sent):
    """("silent" | "repeat" | "other", frame or None) of one record: silent = Dire Wolf gave nothing back, repeat = one frame, other =
    several. `sent` is the frame bytes that were injected."""
    got = [bytes.fromhex(h) for h in rec.get("dw_heard_hex") or []]
    if not got:
        return "silent", None
    if len(got) == 1:
        return "repeat", got[0]
    return "other", got[0]


def compare_dw(g, rec):
    """Dire Wolf against the model for one path vector: ("equal" | "equal_masked" | "differs" | "unusable", text). `equal_masked` means the
    frames differ only in the command/response bits of destination and source (bit 7 of the two SSID bytes). A record whose digipeater node
    never received the frame is unusable (the oracle heard nothing, so its silence says nothing)."""
    if not rec.get("b_received"):
        return "unusable", "the Dire Wolf digipeater never received the frame (b_received false): its silence proves nothing"
    sent = bytes.fromhex(rec["sent_hex"])
    kind, frame = dw_outcome(rec, sent)
    want = digi1.golden_expect(g, sent)
    if kind == "other":
        return "differs", "Dire Wolf gave %d frames" % len(rec["dw_heard_hex"])
    if want is None:
        return ("equal", "silent, like the model") if kind == "silent" else ("differs", "Dire Wolf repeated (%s), the model does not" % ax25.path_text(frame))
    if kind == "silent":
        return "differs", "Dire Wolf is silent, the model repeats (%s)" % ax25.path_text(want)
    if frame == want:
        return "equal", "the same bytes"
    if ax25.mask_cr_bits(frame) == ax25.mask_cr_bits(want):
        return "equal_masked", "the same bytes except the command/response bits"
    return "differs", "Dire Wolf %s (%s), the model %s (%s)" % (frame.hex(), ax25.path_text(frame), want.hex(), ax25.path_text(want))


def oracle_problems(g, rec):
    """([problems], [notes]) for one path vector: a vector with dw == "equal" must be equal (or equal_masked: recorded as a note) in Dire
    Wolf; a documented deviation (dw "differs:...") is recorded as a note, never a problem. An unusable record is a note."""
    verdict, text = compare_dw(g, rec)
    if verdict == "unusable":
        return [], ["%s: %s" % (g.id, text)]
    if g.dw.startswith("differs"):
        return [], ["%s: documented deviation (%s): Dire Wolf %s - %s" % (g.id, g.dw.split(":", 1)[1], verdict, text)]
    if verdict == "differs":
        return ["%s: the oracle disagrees with the model: %s" % (g.id, text)], []
    if verdict == "equal_masked":
        return [], ["%s: %s" % (g.id, text)]
    return [], []


def timeline_outcomes(records_by_case, times=digi1.TIMELINE_S):
    """["repeat" | "silent" | ...] for the timeline steps T0.. in the vectors, [] when absent."""
    out = []
    for i in range(len(times)):
        rec = records_by_case.get("%s%d" % (TIMELINE_NAME, i))
        if rec is None:
            return []
        out.append(dw_outcome(rec, b"")[0])
    return out
