# SPDX-License-Identifier: MIT
"""The Dire Wolf oracle of DIGI1 (dvtest/digi1_oracle.py, the job `digi` of rig/direwolf-entry.py, `python3 -m dvtest.oracle_direwolf digi`): the plan, the job, the
assignment of repeats to steps, the comparison with the model, the vectors file. No Dire Wolf, no docker."""
import importlib.util
import json
from pathlib import Path

import pytest

from dvtest import ax25, digi1, digi1_oracle

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("direwolf_entry", ROOT / "rig" / "direwolf-entry.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def fake_result(steps, answer):
    """A job result: every step sent GAP_S apart, `answer(step)` gives the repeats Dire Wolf returned (frame bytes list)."""
    sent = [{"name": s["name"], "t": s["at"] + 0.01, "frame_hex": s["frame"].hex()} for s in steps]
    heard, b_heard = [], []
    for s in steps:
        b_heard.append({"t": s["at"] + 0.5, "frame_hex": s["frame"].hex()})
        for frame in answer(s):
            heard.append({"t": s["at"] + 2.0, "frame_hex": frame.hex()})
    return {"sent": sent, "heard": heard, "b_heard": b_heard}


def model_answer(step):
    g = step["vector"]
    if g is not None:
        return [digi1.golden_expect(g, step["frame"])] if g.result == "repeat" else []
    return []


def test_plan_has_every_vector_the_key_vectors_and_the_timeline_with_distinct_keys():
    steps = digi1_oracle.plan()
    names = [s["name"] for s in steps]
    assert names[:13] == ["G%d" % n for n in range(1, 14)] and "X11" in names and names[-5:] == ["T0", "T1", "T2", "T3", "T4"]
    assert len(names) == len(set(names)) == 13 + 11 + 8 + 5
    path_keys = [digi1.dedupe_key(s["frame"]) for s in steps if s["kind"] == "path"]
    assert len(path_keys) == len(set(path_keys))                  # the suffix keeps G1 and G2 apart, or the second would be a duplicate
    ats = [s["at"] for s in steps]
    assert ats == sorted(ats) and all(b - a >= 6.0 - 1e-9 for a, b in zip(ats, ats[1:]) if not names[ats.index(b)].startswith("T"))
    timeline = [s for s in steps if s["kind"] == "timeline"]
    assert [round(s["at"] - timeline[0]["at"], 3) for s in timeline] == list(digi1.TIMELINE_S)
    assert len({s["frame"] for s in timeline}) == 1


def test_job_is_the_json_the_container_reads():
    steps = digi1_oracle.plan()
    job = digi1_oracle.job(steps)
    json.dumps(job)
    assert job["mode"] == "digi" and job["mycall"] == "N0TEST-10" and job["extra"] == ["DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$", "DEDUPE 30"]
    assert len(job["steps"]) == len(steps) and job["steps"][0]["frame_hex"] == steps[0]["frame"].hex() and job["duration_s"] > 250


def test_the_container_config_carries_the_digipeater_rules_only_on_the_oracle_node():
    plain = entry.loop_conf("N0TEST-8", 8001, 7355, "/tmp/x")
    oracle = entry.loop_conf("N0TEST-10", 8002, 7356, "/tmp/y", extra=digi1_oracle.EXTRA)
    assert "DIGIPEAT" not in plain and "DEDUPE" not in plain          # the SUT's TNCs have no digipeater
    assert "MYCALL N0TEST-10" in oracle and "DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$" in oracle.splitlines() and "DEDUPE 30" in oracle.splitlines()
    assert entry.loop_conf("N0TEST-8", 8001, 7355, "/tmp/x", kisscopy=True).splitlines()[-1] == "KISSCOPY"
    assert "digi" in entry.MODES


def test_kiss_stream_decodes_across_chunks_and_escapes():
    k = entry.KissStream()
    out = []
    for chunk in (b"\xc0\x00ab", b"c\xc0\xc0\x00\xdb", b"\xdcz\xc0\xc0\x01xx\xc0"):
        out += k.feed(chunk)
    assert out == [b"abc", b"\xc0z"]                                   # the command 1 frame is not data


def test_assign_gives_a_repeat_to_the_step_it_follows():
    sent = [{"name": "A", "t": 2.0}, {"name": "B", "t": 8.0}]
    heard = [{"t": 3.5, "frame_hex": "aa"}, {"t": 9.0, "frame_hex": "bb"}, {"t": 9.5, "frame_hex": "cc"}, {"t": 1.0, "frame_hex": "dd"}]
    assert digi1_oracle.assign(sent, heard) == {"A": ["aa"], "B": ["bb", "cc"]}


def build_doc(answer):
    steps = digi1_oracle.plan()
    return steps, digi1_oracle.file_doc(fake_result(steps, answer), steps, "Dire Wolf Release 1.8.1")


def test_an_oracle_that_agrees_with_the_model_has_no_problems_and_notes_the_deviations():
    steps, doc = build_doc(model_answer)
    by = {r["case"]: r for r in doc["records"]}
    for s in steps:
        g = s["vector"]
        if g is None:
            continue
        problems, notes = digi1_oracle.oracle_problems(g, by[g.id])
        assert problems == [], g.id
        assert bool(notes) == g.dw.startswith("differs"), g.id          # a documented deviation is a note, even when Dire Wolf happens to agree
    assert digi1_oracle.compare_dw(digi1.GOLDENS[0], by["G1"])[0] == "equal"
    assert digi1_oracle.compare_dw(digi1.GOLDENS[3], by["G4"]) == ("equal", "silent, like the model")


def test_an_oracle_that_repeats_what_the_model_refuses_is_a_deviation_only_where_documented():
    def eager(step):
        g = step["vector"]
        if g is None:
            return []
        frame = step["frame"]
        idx = ax25.first_unused_hop(frame)
        return [ax25.replace_hop(frame, idx, digi1.OURS)] if idx is not None else []

    steps, doc = build_doc(eager)
    by = {r["case"]: r for r in doc["records"]}
    verdicts = {}
    for s in steps:
        g = s["vector"]
        if g is not None:
            verdicts[g.id] = digi1_oracle.oracle_problems(g, by[g.id])
    assert verdicts["G10"][0] == [] and verdicts["G11"][0] == [] and verdicts["G9"][0] == []     # documented: Dire Wolf repeats, DV-Router refuses
    assert verdicts["G4"][0] and "oracle disagrees" in verdicts["G4"][0][0]                     # WIDE2-1 first: Dire Wolf must stay silent
    assert verdicts["G6"][0]                                                                     # own path: silent in both


def test_a_frame_the_digipeater_never_heard_proves_nothing():
    steps = digi1_oracle.plan()
    result = fake_result(steps, lambda s: [])
    result["b_heard"] = []
    doc = digi1_oracle.file_doc(result, steps)
    rec = {r["case"]: r for r in doc["records"]}["G1"]
    assert digi1_oracle.compare_dw(digi1.GOLDENS[0], rec)[0] == "unusable"
    problems, notes = digi1_oracle.oracle_problems(digi1.GOLDENS[0], rec)
    assert problems == [] and "proves nothing" in notes[0]


def test_masked_command_bits_and_extra_frames_are_told_apart():
    g = digi1.GOLDENS[0]
    steps = digi1_oracle.plan()
    step = next(s for s in steps if s["name"] == "G1")
    want = digi1.golden_expect(g, step["frame"])
    flipped = bytearray(want)
    flipped[13] ^= 0x80                                                 # the source command/response bit
    for answer, verdict in (([bytes(flipped)], "equal_masked"), ([want, want], "differs"), ([want], "equal")):
        doc = digi1_oracle.file_doc(fake_result([step], lambda s, a=answer: a), [step])
        assert digi1_oracle.compare_dw(g, doc["records"][0])[0] == verdict


def test_timeline_outcomes_and_the_vectors_file_round_trip(tmp_path):
    steps = digi1_oracle.plan()
    frame = next(s["frame"] for s in steps if s["kind"] == "timeline")

    def answer(step):
        if step["kind"] == "timeline" and step["name"] in ("T0", "T3"):
            return [ax25.replace_hop(frame, 0, digi1.OURS)]
        return model_answer(step)

    _steps, doc = build_doc(answer)
    path = tmp_path / "v" / "digi_oracle.json"
    digi1_oracle.write(doc, str(path))
    assert json.loads(path.read_text())["schema"] == digi1_oracle.SCHEMA and path.read_text().isascii()
    loaded = digi1_oracle.load(str(path))
    assert digi1_oracle.timeline_outcomes(loaded) == ["repeat", "silent", "silent", "repeat", "silent"]
    assert digi1_oracle.timeline_outcomes({}) == [] and digi1_oracle.load(str(tmp_path / "nothing.json")) == {}
    (tmp_path / "other.json").write_text(json.dumps({"schema": "x", "records": []}))
    assert digi1_oracle.load(str(tmp_path / "other.json")) == {}


def test_the_committed_vectors_file_when_present_agrees_with_the_model():
    loaded = digi1_oracle.load(root=str(ROOT))
    if not loaded:
        pytest.skip("vectors/direwolf/digi_oracle.json is not committed yet (docs/digi1-cases.md GAP 1)")
    for g in list(digi1.GOLDENS) + list(digi1.extra_vectors()):
        problems, _notes = digi1_oracle.oracle_problems(g, loaded[g.id])
        assert problems == [], problems
    assert digi1_oracle.timeline_outcomes(loaded) == ["repeat", "silent", "silent", "repeat", "silent"]
