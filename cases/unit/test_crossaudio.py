# SPDX-License-Identifier: MIT
"""dvtest/crossaudio.py: the pure helpers of the real-transcoder cross-mode YSF case."""
import pytest

from dvtest import crossaudio, echo
from dvtest.proto import nxdn
from dvtest.audio import signals
from dvtest.proto import dextra

pytestmark = pytest.mark.unit


def test_expected_ysf_is_what_the_vch_coder_returns_and_is_stable():
    frames = signals.prng_frames("dmr", 6, seed="x")
    once = crossaudio.expected_ysf(frames)
    assert len(once) == 6 and once != frames
    assert crossaudio.expected_ysf(once) == once          # a second pass changes nothing


def test_heard_frames_picks_the_first_over_after_a_time():
    frames = signals.prng_frames("dstar", 5, seed="y")
    evs = []
    for base, sid in ((10.0, 1), (20.0, 2)):
        pk = [dextra.header(sid, "N0CALL", "H")] + [dextra.voice(sid, i, f, False) for i, f in enumerate(frames)]
        evs += echo.events("dstar", pk, [base + i * 0.02 for i in range(len(pk))])
    assert crossaudio.heard_frames(evs) == frames
    assert crossaudio.heard_frames(evs, 15.0) == frames
    assert crossaudio.heard_frames(evs, 25.0) == []


def test_tone_report_finds_a_tone_and_not_silence():
    tone = b"".join(signals.frames(signals.tone(700, 1000)))
    rep = crossaudio.tone_report(tone, 700)
    assert abs(rep["dominant"] - 700) <= 20 and rep["share"] > 0.8
    flat = b"".join(signals.frames(signals.silence(1000)))
    assert crossaudio.tone_report(flat, 700)["share"] == 0.0


def test_repetition():
    assert crossaudio.repetition([]) == (None, 0)
    assert crossaudio.repetition([b"a", b"b", b"a"]) == (b"a", 2)


# byte pairs recorded on dv-router:wip-c165c93 (out/20261104T051933Z, 20261104T052813Z, 20261104T053737Z)
ECHO_YSF = [("8e38664559c199e31f", "ce5a01431b895da393", 12, 15), ("4b45860b222b9af23c", "6f63a30902e3debeb4", 2, 5),
            ("b0ef64d2065cf7c780", "94cb63d022b4f38f0c", 2, 5), ("f20efac02aa36777ab", "924c9de62eeba33727", None, None),
            ("70084b810b8b1abdd7", "106c0ea3a52bd679df", None, None)]
LIVE_YSF = [("fcd98eea5eb962c404", "9eb9e8ce38f1a2c488", 14, 12), ("9eb9e8ce38f1a3c488", "9a9fccc81819a78804", 12, 10),
            ("53ea845598942d6e4d", "57cc8353bc7c6966c1", 28, 27), ("3d02234572389de7cb", "7f404507705419a347", 3, 1),
            ("dba0613598fe41cd83", "dba0603598fe41cd83", 31, 30)]


@pytest.mark.parametrize("sent,heard,b2_sent,b2_heard", ECHO_YSF)
def test_recorded_echo_pairs_follow_the_two_gain_steps(sent, heard, b2_sent, b2_heard):
    got = crossaudio.gain_check([bytes.fromhex(sent)], [bytes.fromhex(heard)], ("p10", "m04"))
    assert got == {"checked": 1, "bad": []}
    if b2_sent is not None:
        assert nxdn.gain_index(nxdn.from_dmr(bytes.fromhex(sent))) == b2_sent
        assert nxdn.gain_index(nxdn.from_dmr(bytes.fromhex(heard))) == b2_heard


@pytest.mark.parametrize("sent,heard,b2_sent,b2_heard", LIVE_YSF)
def test_recorded_live_pairs_follow_the_egress_step(sent, heard, b2_sent, b2_heard):
    got = crossaudio.gain_check([bytes.fromhex(sent)], [bytes.fromhex(heard)], ("m04",))
    assert got == {"checked": 1, "bad": []}
    assert nxdn.gain_index(nxdn.from_dmr(bytes.fromhex(sent))) == b2_sent
    assert nxdn.gain_index(nxdn.from_dmr(bytes.fromhex(heard))) == b2_heard


def test_gain_check_names_a_wrong_gain_and_a_changed_bit():
    sent = bytes.fromhex("8e38664559c199e31f")
    assert crossaudio.gain_check([sent], [sent], ("m04",))["bad"] == [(0, 12, 12, 10, True)]
    other = bytearray(bytes.fromhex("ce5a01431b895da393"))
    other[0] ^= 0x80
    bad = crossaudio.gain_check([sent], [bytes(other)], ("p10", "m04"))["bad"]
    assert len(bad) == 1 and bad[0][0] == 0


def test_maps_are_32_entries_and_the_composition_of_the_report():
    assert len(crossaudio.P10) == len(crossaudio.M04) == 32
    comp = [crossaudio.mapped_gain(b, ("p10", "m04")) for b in range(32)]
    assert comp[:20] == [b + 3 for b in range(20)] and comp[30] == 30 and comp[31] == 30


def test_levels_active_and_scaling():
    tone = b"".join(signals.frames(signals.tone(700, 1000, amp=8000)))
    silence = b"".join(signals.frames(signals.silence(500)))
    assert crossaudio.active_dbfs(tone) == pytest.approx(20 * __import__("math").log10(8000 / 2 ** 0.5 / 32768), abs=0.1)
    mixed = silence + tone + silence
    assert crossaudio.active_dbfs(mixed) == pytest.approx(crossaudio.active_dbfs(tone), abs=0.1)      # pauses do not count
    assert crossaudio.level_summary(mixed)["rms_dbfs"] < crossaudio.active_dbfs(mixed) - 2
    assert crossaudio.active_dbfs(silence) == -120.0
    for target in (-26, -20, -14):
        assert crossaudio.active_dbfs(crossaudio.scale_active(mixed, target)) == pytest.approx(target, abs=0.2)
    loud = crossaudio.scale_active(tone, -1.0)
    assert max(abs(v) for v in __import__("struct").unpack("<%dh" % (len(loud) // 2), loud)) <= 32768


def test_record_row_and_table():
    out = {"rms": 441.2, "rms_dbfs": -37.5, "active_dbfs": -37.0}
    rows = [crossaudio.record_row(tag, "m17>dmr", "tone", -15.0, 150, 150 if tag == "a" else 140, out,
                                  {"ok": True, "dominant": 700, "share": 0.9, "env_corr": None}) for tag in ("a", "b")]
    assert rows[0]["delta_db"] == -22.0 and rows[0]["drops"] == 0 and rows[1]["drops"] == 10 and rows[0]["rms"] == 441
    table = crossaudio.format_table(rows)
    lines = table.split("\n")
    assert len(lines) == 3 and lines[0].startswith("| pair | signal |") and "a: rms" in lines[0] and "b: rms" in lines[0]
    assert "441 / -37.0 (-22.0) / 150/150 / ok 700 Hz" in lines[2] and "140/150" in lines[2]
    assert crossaudio.format_table(rows[:1] + [dict(rows[1], pair="x")], ["a", "b"]).count("\n") == 3


def test_levels_table_tool_reads_jsonl_files(tmp_path, capsys):
    import json

    from dvtest.tools import levels_table
    out = {"rms": 100.0, "rms_dbfs": -50.0, "active_dbfs": -45.0}
    for i, tag in enumerate(("x", "y")):
        row = crossaudio.record_row(tag, "dmr>dstar", "tone", -15.0, 150, 150, out, {"ok": True, "dominant": 688, "share": 0.9, "env_corr": None})
        (tmp_path / ("f%d.jsonl" % i)).write_text(json.dumps(row) + "\n\n")
    levels_table.main([str(tmp_path / "f0.jsonl"), str(tmp_path / "f1.jsonl")])
    text = capsys.readouterr().out
    assert "x: rms" in text and "y: rms" in text and text.count("dmr>dstar") == 1 and "688 Hz" in text


def test_summarize_per_pair():
    out = lambda a: {"rms": 1000.0, "rms_dbfs": a, "active_dbfs": a}  # noqa: E731
    cont = {"ok": True, "dominant": None, "share": None, "env_corr": 0.99}
    rows = [crossaudio.record_row("t", "m17>dmr", "tone", -15.0, 150, 150, out(-37.0), cont)]
    rows += [crossaudio.record_row("t", "m17>dmr", "c@%d" % lvl, lvl, 300, 290, out(lvl - d), dict(cont, ok=d < 5)) for lvl, d in ((-26, 1.0), (-20, 2.0), (-14, 6.0))]
    text = crossaudio.summarize(rows)
    line = text.split("\n")[2]
    assert line.startswith("| m17>dmr |") and "-3.0 (-6.0..-1.0)" in line and "-22.0, 1000" in line and line.endswith("/ 30 / 1 |")


def test_cpu_window_and_load_summary():
    lines = ["100.0 1.00% 1.5MiB / 512MiB", "102.0 5.50% 2.5MiB / 512MiB", "104.0 7.50% 900KiB / 512MiB", "106.0 90.00% 1.0MiB / 512MiB", "bad", ""]
    n, mean, peak, mem = crossaudio.cpu_window(lines, 101.0, 105.0)
    assert n == 2 and mean == 6.5 and peak == 7.5 and mem == 2.5
    load = {"tag": "t", "start": 101.0, "end": 105.0,
            "streams": {"A": {"sent": 100, "got": 103}, "B": {"sent": 100, "got": 0}, "C": {"pair": "x", "error": "boom"}}}
    got = crossaudio.load_summary(load, lines)
    assert got["streams_ok"] == 1 and got["streams"] == 3 and got["lost"] == 100 and got["frames_got"] == 103 and got["cpu_max"] == 7.5
