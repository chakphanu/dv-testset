# SPDX-License-Identifier: MIT
"""cases/prod_cross_support.py: the pure helpers of the production cross-mode level check (cases/test_prod_cross.py). Offline; the
plan test proves that no socket is opened and no name is resolved. Hosts and callsigns here are placeholders (N0CALL, HOST)."""
import json
import math
import socket
import struct

import pytest

import prod_cross_support as ps
from dvtest import crossaudio, echo
from dvtest.audio import signals
from dvtest.proto import dextra, m17

pytestmark = pytest.mark.unit

GOOD = {"DVT_PROD_CROSS": "1", "DVT_PROD_HOST": "HOST", "DVT_PROD_CALL_DSTAR": "N0CALL T", "DVT_PROD_CALL_M17": "N0CALL"}


# ---------------------------------------------------------------- configuration and the hard rails

def test_nothing_is_built_in_and_every_variable_is_required():
    with pytest.raises(ps.ConfigMissing):
        ps.config_from_env({})
    with pytest.raises(ps.ConfigMissing):
        ps.config_from_env({"DVT_PROD_CROSS": "0", **{k: v for k, v in GOOD.items() if k != "DVT_PROD_CROSS"}})
    for name in ("DVT_PROD_HOST", "DVT_PROD_CALL_DSTAR", "DVT_PROD_CALL_M17"):
        env = dict(GOOD)
        del env[name]
        with pytest.raises(ps.ConfigMissing) as exc:
            ps.config_from_env(env)
        assert name in str(exc.value)


def test_good_configuration():
    cfg = ps.config_from_env(GOOD)
    assert (cfg.host, cfg.call_dstar, cfg.call_m17, cfg.module, cfg.dry) == ("HOST", "N0CALL T", "N0CALL", "E", False)
    assert cfg.ref is None and cfg.tag == "unknown"
    cfg = ps.config_from_env(dict(GOOD, DVT_PROD_CROSS_DRY="1", DVT_PROD_CROSS_REF="x.jsonl",
                                  DVT_PROD_CROSS_TAG="vocoder-ambed:64s-pcm-688d9bf7-unity", DVT_PROD_MODULE="e"))
    assert cfg.dry and cfg.ref == "x.jsonl" and cfg.tag == "688d9bf7-unity" and cfg.module == "E"


@pytest.mark.parametrize("module", ["A", "H", "X", "", "EE", "E1"])
def test_a_module_but_e_is_refused(module):
    with pytest.raises(ps.ConfigRefused):
        ps.config_from_env(dict(GOOD, DVT_PROD_MODULE=module))
    with pytest.raises(ps.ConfigRefused):
        ps.require_module(module)


def test_the_module_is_a_constant_and_the_ports_are_the_production_ones():
    assert ps.MODULE == "E"
    assert ps.PORTS == {"dstar": 30001, "m17": 17000}


@pytest.mark.parametrize("env", [{"DVT_PROD_CALL_DSTAR": "N0CALL"}, {"DVT_PROD_CALL_DSTAR": "N0CALL  T"}, {"DVT_PROD_CALL_DSTAR": "N0CA!L T"},
                                 {"DVT_PROD_CALL_M17": "N0CALL-TOOLONG"}, {"DVT_PROD_CALL_M17": "N0CA!L"}, {"DVT_PROD_HOST": "a b"}])
def test_bad_values_are_refused_not_truncated(env):
    with pytest.raises(ps.ConfigRefused):
        ps.config_from_env(dict(GOOD, **env))


def test_limits_of_the_run():
    assert ps.OVER_S <= ps.MAX_OVER_S <= 3.2
    assert ps.MIN_GAP_S >= 6.0
    assert ps.WATCH_S <= ps.MIN_GAP_S and ps.GLOBAL_TIMEOUT_S > 0
    assert ps.check_over_frames(160) == 160
    with pytest.raises(ps.ConfigRefused):
        ps.check_over_frames(161)


def test_gap_wait():
    assert ps.gap_wait(None, 100.0) == 0.0
    assert ps.gap_wait(100.0, 102.0) == pytest.approx(4.0)
    assert ps.gap_wait(100.0, 106.0) == 0.0
    assert ps.gap_wait(100.0, 200.0) == 0.0


# ---------------------------------------------------------------- source signals

def write_clip(path, seconds=6.0, amp=3000):
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * 8000)
    pcm = struct.pack("<%dh" % n, *[int(amp * math.sin(i * 0.05) * (1 if (i // 1600) % 2 == 0 else 0.1)) for i in range(n)])
    path.write_bytes(pcm)
    return pcm


def test_source_signals_prefers_the_thai_clip(tmp_path):
    write_clip(tmp_path / ".cache" / "speech-levels" / "thai_fixed.raw")
    write_clip(tmp_path / ".cache" / "speech-levels" / "8k" / "bdl" / "arctic_a0001.raw", amp=500)
    sigs = ps.source_signals(tmp_path)
    assert [n for n, _ in sigs] == ["tone", "thai_fixed@-20"]
    tone, speech = sigs[0][1], sigs[1][1]
    assert len(tone) == len(speech) == 150 * 320
    assert crossaudio.tone_report(tone, 700)["dominant"] == pytest.approx(700, abs=10)
    assert crossaudio.active_dbfs(tone) == pytest.approx(-15.3, abs=0.2)
    assert crossaudio.active_dbfs(speech) == pytest.approx(-20.0, abs=0.7)


def test_source_signals_falls_back_to_arctic_and_then_skips(tmp_path):
    with pytest.raises(ps.MissingClip):
        ps.source_signals(tmp_path)
    write_clip(tmp_path / ".cache" / "speech" / "8k" / "bdl" / "arctic_a0001.raw", seconds=2.0)
    names = [n for n, _ in ps.source_signals(tmp_path)]
    assert names == ["tone", "arctic_0001@-20"]


# ---------------------------------------------------------------- the plan opens nothing

def test_the_plan_opens_no_socket_and_resolves_no_name(tmp_path, monkeypatch):
    write_clip(tmp_path / ".cache" / "speech-levels" / "thai_fixed.raw")

    def boom(*a, **k):
        raise AssertionError("the dry run touched the network")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    cfg = ps.config_from_env(dict(GOOD, DVT_PROD_CROSS_DRY="1"))
    plan = ps.make_plan(cfg, ps.source_signals(tmp_path))
    text = ps.format_plan(plan)
    assert [s["pair"] for s in plan["steps"]] == ["m17>dstar", "m17>dstar", "dstar>m17", "dstar>m17"]
    assert [s["frames"] for s in plan["steps"]] == [150] * 4
    assert plan["links"]["dstar"]["port"] == 30001 and plan["links"]["m17"]["port"] == 17000
    assert plan["links"]["dstar"]["link_packet_hex"] == dextra.link_packet(b"N0CALL T", "E").hex()
    assert plan["links"]["m17"]["link_packet_hex"] == m17.conn_packet("N0CALL", "E").hex()
    assert "nothing is sent" in text and "module E" in text and "at least 6 s" in text


# ---------------------------------------------------------------- live copy and echo copy

def dstar_over(sid, t0, n=150, callsign="N0CALL"):
    """Packets and arrival times of one D-STAR stream (header, n voice, last)."""
    frames = signals.prng_frames("dstar", n, seed="s%d" % sid)
    pk = [dextra.header(sid, callsign, "E")] + [dextra.voice(sid, i, f, False) for i, f in enumerate(frames)] + [dextra.voice(sid, n, b"\x00" * 9, True)]
    return pk, [t0 + i * 0.02 for i in range(len(pk))], frames


def events_of(*overs):
    pk, tm = [], []
    for p, t, _f in overs:
        pk += p
        tm += t
    return echo.events("dstar", pk, tm)


def test_live_copy_is_scored_and_the_echo_copy_is_only_counted():
    live, rep = dstar_over(1, 100.3), dstar_over(2, 104.3)               # live from 100.3 to 103.3, echo 1.0 s after the end
    out = ps.live_and_echo(events_of(live, rep), t0=100.0, t_end=103.0)
    assert out["live"] and out["frames"] == live[2]
    assert out["echo_overs"] == 1 and out["echo_frames"] == 150
    assert out["live_start_s"] == pytest.approx(0.3, abs=0.01)


def test_echo_that_follows_the_live_copy_closer_than_the_gap_is_split_by_stream_id():
    live, rep = dstar_over(1, 100.3), dstar_over(2, 103.6)               # 0.28 s after the end of the live copy: one silence-split group
    out = ps.live_and_echo(events_of(live, rep), t0=100.0, t_end=103.0)
    assert out["frames"] == live[2] and out["echo_overs"] == 1 and out["echo_frames"] == 150


def test_two_echo_copies_are_counted_and_none_is_scored():
    out = ps.live_and_echo(events_of(dstar_over(1, 100.3), dstar_over(2, 104.3), dstar_over(3, 108.3)), t0=100.0, t_end=103.0)
    assert out["echo_overs"] == 2 and len(out["frames"]) == 150


def test_a_missing_live_copy_is_not_filled_by_the_echo():
    out = ps.live_and_echo(events_of(dstar_over(2, 104.3)), t0=100.0, t_end=103.0)
    assert not out["live"] and out["frames"] == [] and out["echo_overs"] == 1 and out["live_start_s"] is None


def test_traffic_before_the_over_is_ignored():
    out = ps.live_and_echo(events_of(dstar_over(9, 90.0), dstar_over(1, 100.3)), t0=100.0, t_end=103.0)
    assert out["live"] and out["echo_overs"] == 0 and len(out["frames"]) == 150


def test_nothing_heard():
    out = ps.live_and_echo([], t0=100.0, t_end=103.0)
    assert out == {"frames": [], "live": False, "echo_overs": 0, "echo_frames": 0, "live_start_s": None}


def test_m17_streams_split_the_same_way():
    frames = signals.prng_frames("m17", 150, seed="m")
    pk, tm = [], []
    for sid, t0 in ((1, 100.3), (2, 104.3)):
        pkts = m17.voice_packets(frames, sid, "N0CALL")
        pk += pkts
        tm += [t0 + i * 0.04 for i in range(len(pkts))]
    out = ps.live_and_echo(echo.events("m17", pk, tm), t0=100.0, t_end=103.0)
    assert out["live"] and len(out["frames"]) >= 150 and out["echo_overs"] == 1


# ---------------------------------------------------------------- rows

def tone_pcm(freq=700, amp=8000):
    return b"".join(signals.frames(signals.tone(freq, 3000, amp=amp)))


def test_measure_row_of_a_tone_and_of_nothing():
    src = tone_pcm()
    split = {"live": True, "echo_overs": 1, "echo_frames": 150}
    row = ps.measure_row("t", "m17>dstar", "tone", src, 150, 152, tone_pcm(amp=4000), split)
    assert row["pair"] == "m17>dstar" and row["sent"] == 150 and row["got"] == 152 and row["ok"] is True
    assert row["dominant"] == pytest.approx(700, abs=10) and row["delta_db"] == pytest.approx(-6.0, abs=0.2)
    assert row["echo_overs"] == 1 and row["echo_frames"] == 150 and row["live"] is True
    assert "host" not in json.dumps(row)
    empty = ps.measure_row("t", "m17>dstar", "tone", src, 150, 0, b"", {"live": False, "echo_overs": 0, "echo_frames": 0})
    assert empty["ok"] is False and empty["rms"] == 0 and empty["drops"] == 150


# ---------------------------------------------------------------- the comparison with the rig unity rows

def rig_row(pair="m17>dstar", signal="tone", delta=-15.2, dominant=700.0, env=None, sent=150, got=150, tag="688d9bf7-unity"):
    return {"tag": tag, "pair": pair, "signal": signal, "sent": sent, "got": got, "delta_db": delta, "dominant": dominant,
            "env_corr": env, "rms": 985, "out_active_dbfs": -30.4, "ok": True}


def prod_row(**kw):
    row = rig_row(**kw)
    row.update(echo_overs=1)
    return row


def status(row, ref):
    return ps.compare_row(row, ref)


def test_tolerances_are_dv_routers_numbers():
    assert (ps.TOL_SPEECH_DB, ps.TOL_TONE_DB, ps.TOL_TONE_HZ, ps.MIN_ENV_CORR, ps.MAX_LOSS_FRAMES) == (1.0, 1.0, 20.0, 0.90, 0)


def test_tone_level_boundary():
    ref = [rig_row()]
    assert status(prod_row(delta=-14.3), ref)["status"] == "PASS"          # +0.9 dB
    assert status(prod_row(delta=-14.2), ref)["status"] == "PASS"          # +1.0 dB: on the boundary
    res = status(prod_row(delta=-14.1), ref)                               # +1.1 dB
    assert res["status"] == "FAIL" and res["diff_db"] == pytest.approx(1.1)
    assert status(prod_row(delta=-16.4), ref)["status"] == "FAIL"          # -1.2 dB
    assert any("level" in line and "+1.1 dB" in line for line in ps.findings([res]))


def test_tone_frequency_boundary():
    ref = [rig_row(dominant=690.0)]
    assert status(prod_row(dominant=710.0), ref)["status"] == "PASS"
    assert status(prod_row(dominant=711.0), ref)["status"] == "FAIL"
    assert status(prod_row(dominant=None), ref)["status"] == "FAIL"
    assert status(prod_row(dominant=715.0), None)["status"] == "PASS"       # without a reference: 700 +- 20
    assert status(prod_row(dominant=721.0), None)["status"] == "FAIL"


def test_speech_level_and_envelope():
    ref = [rig_row(signal="thai_fixed@-20", delta=-1.7, dominant=None, env=0.99)]
    assert status(prod_row(signal="thai_fixed@-20", delta=-0.8, dominant=None, env=0.95), ref)["status"] == "PASS"
    assert status(prod_row(signal="thai_fixed@-20", delta=-0.6, dominant=None, env=0.95), ref)["status"] == "FAIL"      # +1.1 dB
    assert status(prod_row(signal="thai_fixed@-20", delta=-1.7, dominant=None, env=0.90), ref)["status"] == "PASS"
    res = status(prod_row(signal="thai_fixed@-20", delta=-1.7, dominant=None, env=0.89), ref)
    assert res["status"] == "FAIL" and any(c[0] == "envelope" and not c[1] for c in res["checks"])


def test_frame_loss_is_zero_tolerance_but_extra_frames_are_fine():
    ref = [rig_row()]
    assert status(prod_row(got=153), ref)["status"] == "PASS"
    assert status(prod_row(got=150), ref)["status"] == "PASS"
    assert status(prod_row(got=149), ref)["status"] == "FAIL"


def test_no_matching_reference_row_judges_only_the_absolute_checks():
    res = status(prod_row(delta=5.0), [rig_row(pair="dstar>m17")])
    assert res["status"] == "PASS" and res["diff_db"] is None and res["ref"] is None


def test_pick_ref_by_tag(tmp_path):
    rows = [rig_row(tag="823ce72a"), rig_row(tag="688d9bf7-unity", delta=-9.0)]
    assert [r["delta_db"] for r in ps.pick_ref(rows, "688d9bf7-unity")] == [-9.0]
    with pytest.raises(ps.ConfigError):
        ps.pick_ref(rows, "unknown")                                           # two tags, none named
    with pytest.raises(ps.ConfigError):
        ps.pick_ref(rows, "nope")
    assert len(ps.pick_ref(rows[:1], "unknown")) == 1                          # one tag in the file: that one


def test_report_and_cli(tmp_path, capsys):
    ref = tmp_path / "ref.jsonl"
    ref.write_text("\n".join(json.dumps(r) for r in [rig_row(tag="64s-pcm-688d9bf7-unity"), rig_row(pair="dstar>m17", delta=-3.0, dominant=690.0, tag="64s-pcm-688d9bf7-unity")]) + "\n")
    good = tmp_path / "prod-cross.jsonl"
    good.write_text("\n".join(json.dumps(r) for r in [prod_row(delta=-15.0), prod_row(pair="dstar>m17", delta=-3.2, dominant=690.0)]) + "\n")
    assert ps.main([str(good), "--ref", str(ref)]) == 0
    shown = capsys.readouterr().out
    assert "m17>dstar" in shown and "dstar>m17" in shown and shown.count("PASS") == 2 and "FINDING" not in shown
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(prod_row(delta=-12.0)) + "\n")
    assert ps.main([str(bad), "--ref", str(ref)]) == 1
    shown = capsys.readouterr().out
    assert "FAIL" in shown and "FINDING m17>dstar tone: level" in shown and "+3.2 dB" in shown
    assert ps.main([str(good)]) == 0
    assert "level not judged" in capsys.readouterr().out
    assert ps.main([str(tmp_path / "missing.jsonl")]) == 2
    assert ps.main([]) == 2


def test_rows_are_readable_by_the_levels_table(tmp_path):
    from dvtest.tools import levels_table
    src = tone_pcm()
    row = ps.measure_row("688d9bf7-unity", "m17>dstar", "tone", src, 150, 150, tone_pcm(amp=4000), {"live": True, "echo_overs": 0, "echo_frames": 0})
    path = tmp_path / "prod-cross.jsonl"
    path.write_text(json.dumps(row) + "\n")
    assert "m17>dstar" in crossaudio.format_table(levels_table.read([str(path)]))


def test_no_host_address_or_callsign_is_built_in():
    import re
    for name in ("cases/prod_cross_support.py", "cases/test_prod_cross.py"):
        text = (ps.ROOT / name).read_text(encoding="utf-8")
        found = [m for m in re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text) if m != "0.0.0.0"]     # 0.0.0.0 = the bind address
        assert not found, (name, found)
        assert all(ord(ch) < 128 for ch in text), name
    assert "DVT_PROD_HOST" in (ps.ROOT / "cases/prod_cross_support.py").read_text(encoding="utf-8")
