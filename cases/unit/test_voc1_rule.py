# SPDX-License-Identifier: MIT
"""dvtest/tools/voc1_rule.py: the VOC1 decision rule on synthetic result files (pass and every failure mode)."""
import copy
import json

import pytest

from dvtest.tools import voc1_rule as v

pytestmark = pytest.mark.unit

SPEECH = ["%s@%d" % (c, lvl) for c in ("thai_fixed", "porjai02", "porjai03") for lvl in (-26, -20, -14)]
# production: mean speech delta per pair (dB) and tone rms
PROD_SPEECH = {"m17>dmr": -1.9, "m17>dstar": -1.7, "m17>ysf": -4.8, "dmr>dstar": -0.7, "dmr>m17": 1.5, "dmr>ysf": -3.4,
               "dstar>dmr": -0.9, "dstar>m17": -0.2, "dstar>ysf": -4.1}
PROD_TONE_RMS = {"m17>dmr": 438, "m17>dstar": 985, "m17>ysf": 73, "dmr>dstar": 5865, "dmr>m17": 5678, "dmr>ysf": 5145,
                 "dstar>dmr": 5584, "dstar>m17": 3962, "dstar>ysf": 4740}


def row(tag, pair, signal, delta, rms, sent=300, got=301, ok=True):
    return {"tag": tag, "pair": pair, "signal": signal, "in_active_dbfs": -20.0, "sent": sent, "got": got, "drops": max(0, sent - got),
            "rms": rms, "out_active_dbfs": round(-20.0 + delta, 1), "delta_db": round(delta, 1), "ok": ok, "dominant": None,
            "share": None, "env_corr": 0.98}


def make_rows(tag, speech=None, tone=None):
    speech, tone = speech or PROD_SPEECH, tone or PROD_TONE_RMS
    rows = {}
    for pair in v.PAIRS:
        rows[(pair, "tone")] = row(tag, pair, "tone", -10.0, tone[pair], sent=150, got=151)
        for s in SPEECH:
            rows[(pair, s)] = row(tag, pair, s, speech[pair], 1500)
    return rows


def prod():
    return make_rows("64s-pcm-823ce72a")


def cand(speech=None, tone=None):
    """A candidate that fixes the Codec2 tone and moves nothing else."""
    t = dict(PROD_TONE_RMS, **{"m17>dmr": 1250, "m17>dstar": 1240})
    t.update(tone or {})
    return make_rows("64s-pcm-49f237f0", speech, t)


def load(streams_ok=16, cpu=5.0, tag="x"):
    streams = {chr(65 + i): {"pair": "m17>dmr", "sent": 300, "got": 302} for i in range(16)}
    for i in range(16 - streams_ok):
        streams[chr(65 + i)] = {"pair": "m17>dmr", "error": "boom"}
    summary = {"tag": tag, "streams_ok": streams_ok, "streams": 16, "frames_got": 4800, "frames_sent": 4800, "lost": 0, "window_s": 22.0,
               "cpu_samples": 10, "cpu_mean": cpu, "cpu_max": cpu + 1, "mem_max_mib": 5.0}
    return summary


def status(result, n):
    return next(r for r in result["rules"] if r["n"] == n)["status"]


def problems(result, n):
    return next(r for r in result["rules"] if r["n"] == n)["problems"]


def test_candidate_that_only_fixes_the_codec2_tone_ships():
    r = v.decide(cand(), prod(), load(), load(cpu=4.6))
    assert [x["status"] for x in r["rules"]] == [v.PASS] * 4
    assert r["ship"] and r["verdict"].startswith("SHIP")


def test_same_image_as_both_sides_fails_only_the_tone_rule():
    r = v.decide(prod(), prod(), load(), load())
    assert status(r, 1) == status(r, 2) == status(r, 4) == v.PASS
    assert status(r, 3) == v.FAIL and "does not rise" in problems(r, 3)[0]
    assert not r["ship"]


def test_rule1_lost_frames_and_content_failure():
    c = cand()
    c[("dmr>m17", "porjai02@-20")]["got"] = 299
    c[("dstar>dmr", "tone")]["ok"] = False
    r = v.decide(c, prod(), load(), load())
    assert status(r, 1) == v.FAIL
    assert any("299 of 300" in p for p in problems(r, 1)) and any("content check failed" in p for p in problems(r, 1))
    assert not r["ship"]


def test_rule1_a_missing_pair_fails():
    c = {k: x for k, x in cand().items() if k[0] != "dmr>ysf"}
    r = v.decide(c, prod(), load(), load())
    assert status(r, 1) == v.FAIL and any("dmr>ysf: no rows" in p for p in problems(r, 1))


def test_rule2_within_half_a_dB_passes_and_over_half_a_dB_away_fails():
    near = dict(PROD_SPEECH, **{"dmr>ysf": -3.4 - 0.4})        # 0.4 dB farther: still the same
    assert status(v.decide(cand(speech=near), prod(), load(), load()), 2) == v.PASS
    far = dict(PROD_SPEECH, **{"dmr>ysf": -3.4 - 0.7})         # 0.7 dB farther: moved away
    r = v.decide(cand(speech=far), prod(), load(), load())
    assert status(r, 2) == v.FAIL and "dmr>ysf" in problems(r, 2)[0]


def test_rule2_moving_toward_the_source_passes():
    toward = dict(PROD_SPEECH, **{"m17>ysf": -2.0, "dmr>m17": 0.6})   # |delta| decreases by 2.8 and 0.9
    r = v.decide(cand(speech=toward), prod(), load(), load())
    assert status(r, 2) == v.PASS
    states = {d["pair"]: d["state"] for d in r["speech"]}
    assert states["m17>ysf"] == states["dmr>m17"] == "toward" and states["dmr>dstar"] == "same"


def test_rule2_the_gain_tag_numbers_fail():
    gain = {"m17>dmr": -5.4, "m17>dstar": 3.3, "m17>ysf": -8.2, "dmr>dstar": 5.9, "dmr>m17": 5.5, "dmr>ysf": -3.4,
            "dstar>dmr": -10.3, "dstar>m17": -9.4, "dstar>ysf": -13.5}
    r = v.decide(cand(speech=gain), prod(), load(), load())
    assert status(r, 2) == v.FAIL
    away = [d["pair"] for d in r["speech"] if d["state"] == "AWAY"]
    assert len(away) == 8 and "dmr>ysf" not in away          # 8 of 9 pairs are farther, DMR -> YSF is unchanged


def test_rule2_needs_nine_speech_signals_per_pair():
    c = cand()
    del c[("m17>dmr", SPEECH[0])]
    r = v.decide(c, prod(), load(), load())
    assert status(r, 2) == v.FAIL and "8 speech signals" in problems(r, 2)[0]


def test_rule3_tone_must_reach_900_and_rise():
    r = v.decide(cand(tone={"m17>dmr": 800}), prod(), load(), load())
    assert status(r, 3) == v.FAIL and "stays below 900" in problems(r, 3)[0]
    r = v.decide(cand(tone={"m17>dstar": 985}), prod(), load(), load())             # production value: does not rise
    assert status(r, 3) == v.FAIL and "does not rise" in problems(r, 3)[0]
    r = v.decide(cand(tone={"m17>dmr": 900}), prod(), load(), load())               # exactly the accepted floor
    assert status(r, 3) == v.PASS


def test_rule3_a_changed_ambe_pair_fails_even_by_a_tenth_of_a_dB():
    c = cand()
    c[("dmr>dstar", "tone")]["delta_db"] = -9.9              # production -10.0
    r = v.decide(c, prod(), load(), load())
    assert status(r, 3) == v.FAIL and "dmr>dstar tone" in problems(r, 3)[0]
    c = cand()
    c[("dstar>m17", "porjai03@-14")]["delta_db"] = -0.5       # an AMBE -> Codec2 pair, speech row
    r = v.decide(c, prod(), load(), load())
    assert status(r, 3) == v.FAIL and "dstar>m17 porjai03@-14" in problems(r, 3)[0]
    # the same change on a Codec2-source pair is not a rule 3 problem (rule 2 judges it)
    c = cand()
    c[("m17>ysf", "porjai03@-14")]["delta_db"] = -0.5
    assert status(v.decide(c, prod(), load(), load()), 3) == v.PASS


def test_rule3_tone_frame_delivery_150_of_150():
    c = cand()
    c[("dstar>ysf", "tone")]["got"] = 149
    r = v.decide(c, prod(), load(), load())
    assert status(r, 3) == v.FAIL and any("149/150" in p for p in problems(r, 3))
    c = cand()
    c[("dstar>ysf", "tone")]["sent"] = 120
    assert status(v.decide(c, prod(), load(), load()), 3) == v.FAIL


def test_rule4_load_streams_and_cpu():
    assert status(v.decide(cand(), prod(), load(15), load()), 4) == v.FAIL
    assert status(v.decide(cand(), prod(), load(cpu=10.0), load(cpu=5.0)), 4) == v.PASS      # exactly 2x
    r = v.decide(cand(), prod(), load(cpu=10.1), load(cpu=5.0))
    assert status(r, 4) == v.FAIL and "2x" in problems(r, 4)[0]
    empty = dict(load(), cpu_samples=0)
    assert status(v.decide(cand(), prod(), empty, load()), 4) == v.FAIL


def test_rule4_without_load_files_is_no_data_and_does_not_ship():
    r = v.decide(cand(), prod())
    assert status(r, 4) == v.NODATA and not r["ship"] and "rule 4 NO DATA" in r["verdict"]


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in rows.values()), encoding="utf-8")


def write_load(tmp_path, name, tag, ok=16, cpu=5.0, start=1000.0, end=1022.0):
    streams = {chr(65 + i): ({"pair": "m17>dmr", "sent": 300, "got": 302} if i < ok else {"pair": "m17>dmr", "error": "x"}) for i in range(16)}
    (tmp_path / (name + ".json")).write_text(json.dumps({"tag": tag, "start": start, "end": end, "streams": streams}) + "\n", encoding="utf-8")
    lines = ["%.1f %.1f%% 5.0MiB / 1GiB" % (start + 2 * i, cpu) for i in range(10)] + ["%.1f 99.0%% 5.0MiB / 1GiB" % (end + 50)]
    (tmp_path / (name + ".txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(tmp_path / (name + ".json")), str(tmp_path / (name + ".txt"))


def test_cli_end_to_end_with_files(tmp_path, capsys):
    write_jsonl(tmp_path / "c.jsonl", cand())
    write_jsonl(tmp_path / "p.jsonl", prod())
    cl, cc = write_load(tmp_path, "cl", "49f237f0", cpu=6.0)
    pl, pc = write_load(tmp_path, "pl", "823ce72a", cpu=5.0)
    code = v.main(["--candidate", str(tmp_path / "c.jsonl"), "--production", str(tmp_path / "p.jsonl"),
                   "--candidate-load", cl, "--candidate-cpu", cc, "--production-load", pl, "--production-cpu", pc])
    out = capsys.readouterr().out
    assert code == 0
    assert "candidate 49f237f0 against production 823ce72a" in out and "Verdict: SHIP" in out
    assert out.count("| PASS |") >= 4


def test_cli_failing_candidate_exits_1_and_json(tmp_path, capsys):
    write_jsonl(tmp_path / "c.jsonl", prod())
    write_jsonl(tmp_path / "p.jsonl", prod())
    code = v.main(["--candidate", str(tmp_path / "c.jsonl"), "--production", str(tmp_path / "p.jsonl"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["ship"] is False and data["candidate_tag"] == "823ce72a"


def test_cli_input_errors_exit_2(tmp_path, capsys):
    write_jsonl(tmp_path / "p.jsonl", prod())
    assert v.main(["--candidate", str(tmp_path / "missing.jsonl"), "--production", str(tmp_path / "p.jsonl")]) == 2
    mixed = copy.deepcopy(cand())
    mixed[("m17>dmr", "tone")]["tag"] = "64s-pcm-other"
    write_jsonl(tmp_path / "m.jsonl", mixed)
    assert v.main(["--candidate", str(tmp_path / "m.jsonl"), "--production", str(tmp_path / "p.jsonl")]) == 2
    assert "one tag per side" in capsys.readouterr().err
    write_jsonl(tmp_path / "c.jsonl", cand())
    assert v.main(["--candidate", str(tmp_path / "c.jsonl"), "--production", str(tmp_path / "p.jsonl"), "--candidate-load", "x.json"]) == 2


def test_the_rows_of_the_recorded_runs_have_the_keys_the_rule_reads():
    from dvtest import crossaudio
    r = crossaudio.record_row("img:64s-pcm-823ce72a", "m17>dmr", "tone", -15.3, 150, 153,
                              {"rms": 438.2, "rms_dbfs": -37.0, "active_dbfs": -37.4}, {"ok": True, "dominant": 700, "share": 0.9, "env_corr": None})
    for key in ("tag", "pair", "signal", "sent", "got", "rms", "delta_db", "ok"):
        assert key in r
    assert v.short(r["tag"]) == "823ce72a"


def test_identical_ok_passes_a_carrier_that_changes_nothing():
    """The unity tag: no tone fix, every row identical to production. Rule 3 in the tone-fix form fails it, the carrier form passes."""
    same = make_rows("64s-pcm-688d9bf7-unity")
    assert status(v.decide(same, prod(), load(), load()), 3) == v.FAIL
    r = v.decide(same, prod(), load(), load(), identical_ok=True)
    assert status(r, 3) == v.PASS and r["ship"] is True
    assert "carrier image" in r["verdict"]


def test_identical_ok_fails_when_a_row_moves_even_toward_the_source():
    moved = dict(PROD_SPEECH, **{"dmr>dstar": -0.1})            # 0.6 dB closer to the source: fine for a tone-fix, not for a carrier
    r = v.decide(make_rows("64s-pcm-x", speech=moved), prod(), load(), load(), identical_ok=True)
    assert status(r, 3) == v.FAIL
    r = v.decide(cand(), prod(), load(), load(), identical_ok=True)       # the tone fix itself is not a carrier
    assert status(r, 3) == v.FAIL


def test_identical_ok_on_the_command_line(tmp_path, capsys):
    write_jsonl(tmp_path / "c.jsonl", make_rows("64s-pcm-688d9bf7-unity"))
    write_jsonl(tmp_path / "p.jsonl", prod())
    assert v.main(["--candidate", str(tmp_path / "c.jsonl"), "--production", str(tmp_path / "p.jsonl"), "--identical-ok", "--json"]) == 1   # rule 4 has no data
    data = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in data["rules"]][2] == v.PASS
