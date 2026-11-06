# SPDX-License-Identifier: MIT
"""dvtest/oracle_dsdcc.py: the RF modulator, driver text, summaries (no Docker) and the committed
vectors/dstar_sd/dsdcc.json (stage 2 of HWFREE1 item 4)."""
import json
import os
import struct

import pytest

from dvtest import dstar_sd as sd
from dvtest import oracle_dsdcc as dsd

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _vec(name):
    with open(os.path.join(ROOT, "vectors", "dstar_sd", name)) as fh:
        return json.load(fh)


def test_frame_bits_are_lsb_first_and_end_with_the_sync_pattern():
    bits = dsd.frame_bits(sd.SYNC)
    assert len(bits) == dsd.FRAME_SYMBOLS == 96
    # 0x55 0x2D 0x16 sent LSB first: 10101010 10110100 01101000
    assert "".join(map(str, bits[72:])) == "101010101011010001101000"
    assert dsd.frame_bits(b"\x01\x00\x00")[72:80] == [1, 0, 0, 0, 0, 0, 0, 0]


def test_modulate_is_deterministic_two_level_and_sized():
    frames = sd.render(sd.halves(sd.text_blocks("N0CALL: CQ DE N0CALL")))
    raw = dsd.modulate(frames)
    assert raw == dsd.modulate(frames)
    n = len(raw) // 2
    # lead-in + 21 frames + one more sync frame + tail, 10 samples per symbol
    assert n == (dsd.LEAD_IN_SYMBOLS + 22 * dsd.FRAME_SYMBOLS + dsd.TAIL_SYMBOLS) * dsd.SPS
    samples = struct.unpack("<%dh" % n, raw)
    assert max(samples) == dsd.AMPLITUDE and min(samples) == -dsd.AMPLITUDE
    # the middle of every symbol sits at full amplitude
    mid = [samples[(dsd.LEAD_IN_SYMBOLS + i) * dsd.SPS + 6] for i in range(8)]
    assert all(abs(v) == dsd.AMPLITUDE for v in mid)


def test_parse_states_and_frame_of():
    loc = "JJ00AA".encode().hex()
    text = "\n".join([
        "noise", "R state 4000 %s - 16" % (b" " * 20).hex(),
        "R state %d %s %s 48" % (4000 + 3 * 960, (b"N0" + b" " * 18).hex(), loc), "D end 99"])
    got = dsd.parse_states(text)
    assert got[0] == {"sample": 4000, "text": " " * 20, "locator": "", "header_flags": 16}
    assert got[1]["locator"] == "JJ00AA" and got[1]["header_flags"] == 48 and got[1]["text"].startswith("N0")
    st = dsd.states(got)
    assert [s["frame"] for s in st] == [0, 3]
    assert dsd.frame_of(dsd.LEAD_IN_SYMBOLS * dsd.SPS + 959) == 0 and dsd.frame_of(dsd.LEAD_IN_SYMBOLS * dsd.SPS + 960) == 1


def test_modulable_only_when_every_superframe_is_complete():
    frames = sd.render([], start=1, frames=42)
    assert dsd.modulable(frames)
    assert not dsd.modulable([f for i, f in enumerate(frames) if i != 4])
    assert not dsd.modulable([f for i, f in enumerate(frames) if i != 21])


def test_docker_argv_is_the_contained_form():
    argv = dsd.docker_argv([], env={})
    joined = " ".join(argv)
    for word in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--memory", "--pids-limit"):
        assert word in joined
    assert argv[-1] == dsd.IMAGE == "dvt-dsdcc:local"


def test_summary_of_a_recording_holds_no_content():
    parsed = [
        {"sample": 1, "text": " " * 20, "locator": "", "header_flags": 0},
        {"sample": 2, "text": "ABCDE" + " " * 15, "locator": "", "header_flags": 31},
        {"sample": 3, "text": "ABCDEFGHIJ" + " " * 10, "locator": "JN00AA", "header_flags": 63},
    ]
    summary = dsd.summarize_recording(parsed)
    flat = json.dumps(summary)
    assert "ABCDE" not in flat and "JN00AA" not in flat
    assert summary["header_complete_printable"] and summary["text_states"] == 2 and summary["text_longest_chars"] == 10
    assert summary["locator_valid_shape"] and not summary["header_garbled"]


def test_pins_come_from_env_example():
    pins = dsd.pins()
    assert set(pins) == {"DSDCC_REPO", "DSDCC_COMMIT", "DSDCC_SAMPLE_WITH_HEADER_SHA256", "DSDCC_SAMPLE_ON_THE_FLY_SHA256",
                         "DSDCC_BASE_IMAGE"}
    assert len(pins["DSDCC_COMMIT"]) == 40 and int(pins["DSDCC_COMMIT"], 16) >= 0
    for key in ("DSDCC_SAMPLE_WITH_HEADER_SHA256", "DSDCC_SAMPLE_ON_THE_FLY_SHA256"):
        assert len(pins[key]) == 64 and int(pins[key], 16) >= 0


# ------------------------------------------------------------------ the committed record

def _by_name():
    return {r["case"]: r for r in _vec("dsdcc.json")["cases"]}


def test_committed_record_covers_the_corpus_and_skips_only_cases_with_holes():
    corpus = _vec("corpus.json")["cases"]
    rows = _by_name()
    assert [c["case"] for c in corpus] == [r["case"] for r in _vec("dsdcc.json")["cases"]]
    for rec in corpus:
        frames = [(int(f.split()[0]), bytes.fromhex(f.split()[1])) for f in rec["frames"]]
        assert ("skipped" in rows[rec["case"]]) == (not dsd.modulable(frames)), rec["case"]
    assert _vec("dsdcc.json")["pins"] == dsd.pins()


def test_dsdcc_findings_the_doc_relies_on():
    rows = _by_name()
    full = "N0CALL: CQ DE N0CALL"
    # accumulates over a sync like DStarGateway: the split cases show the full text, MMDVMHost's reset would not
    for case in ("text-slot8-split", "text-slot9-split", "text-missing-block-late", "text-aligned", "text-slot6"):
        assert rows[case]["states"][-1]["text"] == full, case
    for case in ("text-slot8-split", "text-slot9-split", "text-missing-block-late"):
        assert rows[case]["lenient_texts_shown"] == [True] and rows[case]["expected_texts_shown"] == []
    # old and new blocks mix, same string as DStarGateway printed
    assert rows["text-stale-mix"]["final_text"] == "N0CALL: CQ DE NORLD." and rows["text-stale-mix"]["lenient_texts_shown"] == [True]
    # a byte counter, not frame pairs: the offset layout that straddles the sync decodes
    assert rows["text-straddle"]["final_text"] == full
    # bit 7 is not stripped: the byte becomes '.'
    assert rows["text-high-bit"]["final_text"] == "." * 20
    # a missing block leaves the gap
    assert rows["text-missing-block-never"]["final_text"] == "N0CALL: CQ     0CALL"
    # D-PRS is evaluated only when a text block starts a later superframe; headers only when every byte is printable
    assert [c for c, r in rows.items() if r.get("locator_valid_shape_shown")] == ["dprs-then-text"]
    assert [c for c, r in rows.items() if r.get("header_decoded")] == ["header-printable-bytes"]
    # NMEA, squelch, fast data and filler change nothing visible
    for case in ("nmea-gga", "nmea-rmc", "squelch-only", "fast-data-superframe", "filler-only"):
        assert len(rows[case]["states"]) == 1, case
