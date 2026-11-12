# SPDX-License-Identifier: MIT
"""dvtest.rif5: the judgements of the RIF5 cases over host frame logs, the port row of a rendered config, the public state rows and the
metrics deltas, plus the rig snippet rig/config/dv-router/rif5-mmdvm-udp.toml. Offline; the logs are built here from the RF encoders of
dvtest.mmdvm_rf (docs/rif5-cases.md)."""
import tomllib
from pathlib import Path

import pytest

from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_serial as ms
from dvtest import crossaudio, rif5, sut_config
from dvtest.audio import signals
from dvtest.proto import dmr as dmrp
from dvtest.proto import nxdn
from dvtest.proto import ysf as ysfp

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "rif5-mmdvm-udp.toml"
TEMPLATE = '[node]\nbind_ip = "<OWN_IP>"\n'


def rendered():
    return tomllib.loads(sut_config.render(TEMPLATE, "172.30.0", extras=[SNIPPET.read_text()]))


def port():
    return rif5.port_from_config(rendered())


def hf(t, ftype, payload=b""):
    return ms.HostFrame(t, t, ftype, bytes(payload))


# ------------------------------------------------------------------ the snippet and the port row

def test_snippet_renders_and_the_port_row_reads():
    doc = rendered()
    radio = doc["edges"]["radio"]
    assert radio["enable"] is True and radio["tx_enable"] is True and radio["callsign"] == "N0CALL"
    assert radio["tx_ack"] == "I am the control operator of N0CALL and accept responsibility for its transmissions"
    p = port()
    assert p.name == rif5.PORT_NAME and p.peer == ("172.30.0.30", rif5.MODEM_PORT) and p.bind == ("172.30.0.11", rif5.EDGE_PORT)
    assert p.modes == {"dstar": "P", "dmr": "Q", "ysf": "R"} and p.modes == {m: rif5.ROOMS[m] for m in p.modes}
    assert p.callsign == rif5.STATION and p.tx is True and p.physical and p.rx_freq == 434_000_000 == p.tx_freq
    assert (p.rf_hang, p.net_hang) == (1, 1) and p.id_mode == "none" and p.rpt_module == "B" and p.color_code == 1
    assert p.dmr_tg == 4017          # the talk group of room Q (DMR clients of the rig: module_tg("Q"))
    row = radio["port"][0]
    # the document's own validation: exactly one of device or udp, udp needs udp_bind, no module key, a tx row has a physical and a band in 70cm
    assert row["kind"] == "mmdvm-serial" and "device" not in row and "module" not in row and row["band"] == "70cm"
    assert 420_000_000 <= p.rx_freq <= 450_000_000
    # `rules = "other"` needs the explicit id.mode, id.interval_secs and timers.tot_secs of a transmitting row (config.md)
    assert radio["rules"] == "other" and row["id"]["interval_secs"] >= 1 and row["timers"]["tot_secs"] >= 1
    assert len(set(p.modes.values())) == 3                                      # modes must serve different modules
    assert all(m in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for m in p.modes.values())


def test_port_from_config_edge_cases():
    assert rif5.port_from_config({}) is None
    assert rif5.port_from_config({"edges": {"radio": {"enable": False}}}) is None
    doc = rendered()
    assert rif5.port_from_config(doc, "nope") is None
    del doc["edges"]["radio"]["port"][0]["udp"]
    assert rif5.port_from_config(doc) is None


def test_expected_setup_follows_the_row():
    p = port()
    freq, cfg = rif5.expected_setup(p)
    assert freq == ms.expected_set_freq(434_000_000, 434_000_000, 100)
    assert cfg[3] == 0x80 and cfg[4] == 7 and cfg[6] == 10 and cfg[29] == 1


# ------------------------------------------------------------------ the open dialogue

def open_log(t0=0.0, tries=1, spacing=1.8, freq=None, config=None, status=True):
    p = port()
    want_freq, want_cfg = rif5.expected_setup(p)
    frames = [hf(t0 + i * spacing, ms.GET_VERSION) for i in range(tries)]
    t = t0 + (tries - 1) * spacing + 0.01
    frames.append(hf(t, ms.SET_FREQ, (freq or want_freq)[3:]))
    cfg = bytearray(config or build_config(want_cfg))
    frames.append(hf(t + 0.01, ms.SET_CONFIG, bytes(cfg[3:])))
    if status:
        for i in range(10):
            frames.append(hf(t + 0.02 + 0.25 * i, ms.GET_STATUS))
    return frames


def build_config(want):
    frame = bytearray(40)
    frame[0], frame[1], frame[2] = 0xE0, 40, 2
    for at, value in want.items():
        frame[at] = value
    for at in (15, 16, 18, 19):
        frame[at] = 128
    return bytes(frame)


def test_dialogues_split_on_a_progressed_or_a_stale_get_version():
    log = open_log(0.0) + open_log(20.0) + [hf(40.0, ms.GET_VERSION), hf(41.8, ms.GET_VERSION), hf(43.6, ms.GET_VERSION), hf(60.0, ms.GET_VERSION)]
    ds = rif5.dialogues(log)
    assert [len(d.versions) for d in ds] == [1, 1, 3, 1]
    assert ds[0].freq is not None and ds[0].config is not None and len(ds[0].statuses) == 10
    assert ds[2].freq is None
    assert rif5.dialogues([hf(0, ms.GET_STATUS)]) == []


def test_judge_open_accepts_the_documented_dialogue_and_names_every_difference():
    p = port()
    assert rif5.judge_open(rif5.dialogues(open_log())[0], p).problems == []
    assert rif5.judge_open(rif5.dialogues(open_log(tries=3))[0], p).problems == []
    # a retry gap that is not 1.8 s, too many tries
    assert "1.8 s" in " ".join(rif5.judge_open(rif5.dialogues(open_log(tries=2, spacing=1.0))[0], p).problems)
    assert "GET_VERSION attempts" in " ".join(rif5.judge_open(rif5.dialogues(open_log(tries=7))[0], p).problems)
    # SET_FREQ with a wrong RF level
    bad = bytearray(ms.expected_set_freq(434_000_000, 434_000_000, 100))
    bad[11] = 7
    assert "SET_FREQ" in " ".join(rif5.judge_open(rif5.dialogues(open_log(freq=bytes(bad)))[0], p).problems)
    # SET_CONFIG with a wrong colour code and without the simplex flag
    _, want = rif5.expected_setup(p)
    cfg = bytearray(build_config(want))
    cfg[29], cfg[3] = 9, 0
    problems = " ".join(rif5.judge_open(rif5.dialogues(open_log(config=bytes(cfg)))[0], p).problems)
    assert "(29, 9, 1)" in problems and "simplex" in problems
    # a version 1 sized frame to a version 2 modem
    short = build_config(want)[:26]
    log = open_log(config=short)
    assert "40" in " ".join(rif5.judge_open(rif5.dialogues(log)[0], p).problems)
    # voice before the first status
    log = open_log()
    log.insert(3, hf(0.015, ms.DSTAR_HEADER, bytes(41)))
    assert "before the first GET_STATUS" in " ".join(rif5.judge_open(rif5.dialogues(log)[0], p).problems)
    # the order SET_CONFIG before SET_FREQ
    log = open_log()
    log[1], log[2] = log[2], log[1]
    assert "SET_FREQ, SET_CONFIG, GET_STATUS" in " ".join(rif5.judge_open(rif5.dialogues(log)[0], p).problems)


def test_unstated_level_bytes_are_a_finding_only_when_they_are_not_the_default_level():
    p = port()
    v = rif5.judge_open(rif5.dialogues(open_log())[0], p)
    assert v.problems == [] and v.findings == [] and v.facts["unstated_level_bytes"] == {15: 128, 16: 128, 18: 128, 19: 128}
    assert ms.pct(50) == 128
    _, want = rif5.expected_setup(p)
    cfg = bytearray(build_config(want))
    cfg[18] = 77
    v = rif5.judge_open(rif5.dialogues(open_log(config=bytes(cfg)))[0], p)
    assert v.problems == [] and "(18, 77)" in " ".join(v.findings)


def test_status_cadence():
    polls = [hf(0.25 * i, ms.GET_STATUS) for i in range(20)]
    v = rif5.status_cadence(polls)
    assert v.problems == [] and v.facts["median"] == 0.25 and v.facts["polls"] == 20
    slow = [hf(0.5 * i, ms.GET_STATUS) for i in range(20)]
    assert "median" in " ".join(rif5.status_cadence(slow).problems)
    gap = polls[:10] + [hf(0.25 * i + 1.0, ms.GET_STATUS) for i in range(10, 20)]
    assert "longest" in " ".join(rif5.status_cadence(gap).problems)
    assert "intervals" in " ".join(rif5.status_cadence(polls[:5]).problems)
    assert rif5.status_cadence(polls, since=2.0, until=3.0).problems          # a window with too few polls


# ------------------------------------------------------------------ the startup guard

def test_judge_guard():
    quiet = [hf(1.0, ms.GET_VERSION), hf(1.1, ms.SET_FREQ), hf(1.2, ms.SET_CONFIG)] + [hf(1.3 + 0.25 * i, ms.GET_STATUS) for i in range(40)]
    v = rif5.judge_guard(quiet, 1.0, 13.0)
    assert v.problems == [] and v.findings == [] and v.facts["frames"]["GET_STATUS"] == 40
    leak = quiet + [hf(5.0, ms.DSTAR_HEADER, bytes(41)), hf(5.1, ms.SEND_CWID, b"N0CALL"), hf(5.2, 0x1A, bytes(34)), hf(5.3, ms.YSF_DATA, bytes(121))]
    v = rif5.judge_guard(leak, 1.0, 13.0)
    assert len(v.problems) == 4 and "DSTAR_HEADER at +4.00 s" in v.problems[0]
    assert rif5.judge_guard(leak, 6.0, 13.0).problems == []                      # outside the window
    mode = quiet + [hf(6.0, ms.SET_MODE, b"\x01"), hf(6.1, 0x55)]
    v = rif5.judge_guard(mode, 1.0, 13.0)
    assert v.problems == [] and len(v.findings) == 2 and "SET_MODE" in v.findings[0]


# ------------------------------------------------------------------ an over from the room

def timed(items, t0=10.0, gap=0.02):
    """[(ftype, payload)] -> HostFrames, one every gap seconds."""
    return [hf(t0 + gap * i, ftype, payload) for i, (ftype, payload) in enumerate(items)]


def dstar_tx(words, my="N0TEST", rpt2="N0CALL B", rpt1="N0CALL G", eot=1, mode=1, flags=(0, 0, 0)):
    items = [(ms.SET_MODE, bytes((mode,))), (ms.DSTAR_HEADER, rf.dstar_header(my, rpt1=rpt1, rpt2=rpt2, ur="CQCQCQ", flags=flags))]
    for i, w in enumerate(words):
        items.append((ms.DSTAR_DATA, w + (rf.DSTAR_SYNC if i % 21 == 0 else rf.DSTAR_NULL_SLOW)))
    items += [(ms.DSTAR_EOT, b"")] * eot
    return timed(items)


def words(mode, n, seed="rif5"):
    return rf.canonical_frames(mode, [signals.prng_bytes("%s/%s/%d" % (seed, mode, i), 9) for i in range(n)])


def test_judge_tx_dstar():
    w = words("dstar", 50)
    v = rif5.judge_tx_dstar(dstar_tx(w), w, "N0TEST")
    assert v.problems == [] and v.facts["voice_written"] == 50
    log = dstar_tx(w)
    # no EOT, two EOTs
    assert "EOT" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, eot=0), w, "N0TEST").problems)
    assert "EOT" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, eot=2), w, "N0TEST").problems)
    # RPT fields, MY, repeater flag, mode
    assert "rpt2" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, rpt2="N0CALL G"), w, "N0TEST").problems)
    assert "rpt1" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, rpt1="N0CALL B"), w, "N0TEST").problems)
    assert "MY" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, my="N0CALL"), w, "N0TEST").problems)
    assert "flags" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, flags=(0x40, 0, 0)), w, "N0TEST").problems)
    assert "SET_MODE" in " ".join(rif5.judge_tx_dstar(dstar_tx(w, mode=2), w, "N0TEST").problems)
    # a lost word, a changed word, a short run
    assert "word 3" in " ".join(rif5.judge_tx_dstar(dstar_tx(w[:3] + w[4:]), w, "N0TEST").problems)
    assert "30 voice words written, 50 sent" in " ".join(rif5.judge_tx_dstar(dstar_tx(w[:30]), w, "N0TEST").problems)
    # frame 0 of a superframe without the sync triple
    bad = [f if i != 2 else f._replace(payload=f.payload[:9] + b"\x01\x02\x03") for i, f in enumerate(log)]
    assert "sync triple" in " ".join(rif5.judge_tx_dstar(bad, w, "N0TEST").problems)
    # another mode's frame or a CW ID in the middle
    stray = log + [hf(11.5, ms.YSF_DATA, bytes(121)), hf(11.6, ms.SEND_CWID, b"N0CALL")]
    assert "one mode per over" in " ".join(rif5.judge_tx_dstar(stray, w, "N0TEST").problems)
    # longer than sent is a fact, not a problem
    assert rif5.judge_tx_dstar(dstar_tx(w + w[:2]), w, "N0TEST").facts["extra_words"] == 2


def dmr_tx(words_, src=1023008, dst=4017, cc=1, headers=8, terminators=3, mode=2, ctl=0):
    up = rf.dmr_keyup(words_, src, dst, colour_code=cc)
    head, voice, term = up[0], up[1:-1], up[-1]
    items = [(ms.SET_MODE, bytes((mode,)))]
    items += [(ms.DMR_DATA2, bytes((ctl,)) + head[1][1:])] * headers
    items += [(ms.DMR_DATA2, bytes((ctl,)) + v[1][1:]) for v in voice]
    items += [(ms.DMR_DATA2, bytes((ctl,)) + term[1][1:])] * terminators
    return timed(items, gap=0.06)


def test_judge_tx_dmr():
    w = words("dmr", 18)
    sent = rf.padded_frames("dmr", w)
    v = rif5.judge_tx_dmr(dmr_tx(w), sent, 1023008, 4017, 1)
    assert v.problems == [] and v.facts["headers"] == 8 and v.facts["terminators"] == 3 and v.facts["voice_bursts"] == 6
    assert "8 expected" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, headers=1), sent, 1023008, 4017, 1).problems)
    assert "3 expected" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, terminators=1), sent, 1023008, 4017, 1).problems)
    assert "src" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, src=1023007), sent, 1023008, 4017, 1).problems)
    assert "dst" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, dst=9), sent, 1023008, 4017, 1).problems)
    assert "colour code" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, cc=2), sent, 1023008, 4017, 1).problems)
    assert "control byte" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, ctl=0x41), sent, 1023008, 4017, 1).problems)
    assert "SET_MODE" in " ".join(rif5.judge_tx_dmr(dmr_tx(w, mode=1), sent, 1023008, 4017, 1).problems)
    broken = list(w)
    broken[7] = bytes(rf.dmr_ambe(signals.prng_bytes("other", 9)))
    assert "voice word" in " ".join(rif5.judge_tx_dmr(dmr_tx(broken), sent, 1023008, 4017, 1).problems)
    # a slot 1 or duplex control frame
    stray = dmr_tx(w) + [hf(12.0, 0x1D, b"\x01")]
    assert "one mode per over" in " ".join(rif5.judge_tx_dmr(stray, sent, 1023008, 4017, 1).problems)


def ysf_tx(words_, src="N0TEST", mode=3, dev=False):
    up = rf.ysf_keyup(words_, src=src)
    items = [(ms.SET_MODE, bytes((mode,)))] + [(ms.YSF_DATA, payload) for _t, payload, _d in up]
    return timed(items, gap=0.1)


def test_judge_tx_ysf():
    w = words("ysf", 15)
    sent = rf.padded_frames("ysf", w)
    v = rif5.judge_tx_ysf(ysf_tx(w), sent, "N0TEST")
    assert v.problems == [] and v.facts["headers"] == 1 and v.facts["terminators"] == 1 and v.facts["frames"] == 5
    assert "talker" in " ".join(rif5.judge_tx_ysf(ysf_tx(w, src="N0CALL"), sent, "N0TEST").problems)
    assert "Dev flag" in " ".join(rif5.judge_tx_ysf(ysf_tx(w), sent, "N0TEST", lo_dev=True).problems)
    assert "SET_MODE" in " ".join(rif5.judge_tx_ysf(ysf_tx(w, mode=2), sent, "N0TEST").problems)
    assert "voice word" in " ".join(rif5.judge_tx_ysf(ysf_tx(w), sent[::-1], "N0TEST").problems)
    log = ysf_tx(w)
    assert "header and" in " ".join(rif5.judge_tx_ysf(log[:-1], sent, "N0TEST").problems) or "terminator" in " ".join(rif5.judge_tx_ysf(log[:-1], sent, "N0TEST").problems)
    assert set(rif5.JUDGE_TX) == {"dstar", "dmr", "ysf"}


def dstar_dropped(words, dropped, mode_frames=()):
    """The frames of an over in which the host queue dropped the sent words at `dropped`: the slow data carries the sync at the POSITION of the word in
    the over (mode/dstar.rs tx_data), not at the index of the frame."""
    items = [(ms.SET_MODE, bytes((1,))), (ms.DSTAR_HEADER, rf.dstar_header("N0TEST", rpt1="N0CALL G", rpt2="N0CALL B", ur="CQCQCQ", flags=(0, 0, 0)))]
    for i, w in enumerate(words):
        if i not in dropped:
            items.append((ms.DSTAR_DATA, w + (rf.DSTAR_SYNC if i % 21 == 0 else rf.DSTAR_NULL_SLOW)))
    items.append((ms.DSTAR_EOT, b""))
    return timed(items)


def test_align_tells_a_dropped_word_from_a_changed_one():
    assert rif5.align([b"a", b"b", b"c", b"d"], [b"a", b"c", b"d"]) == [0, 2, 3]
    assert rif5.align([b"a", b"b", b"c"], [b"a", b"x", b"c"]) == [0, None, 2]
    assert rif5.align([b"a", b"b"], []) == []


def test_judge_tx_dstar_dropped_words_keep_the_sync_at_the_position_of_the_word():
    w = words("dstar", 50)
    log = dstar_dropped(w, {23, 24, 25})                  # the first rig run: 47 of 50 words written, the first gap at word 23
    strict = rif5.judge_tx_dstar(log, w, "N0TEST")
    assert strict.problems and "voice word 23 written to the modem differs" in strict.problems[0]
    assert not any("sync triple" in p for p in strict.problems)           # frame 42 of the stream is position 45: no sync belongs there
    assert strict.facts["voice_dropped"] == 3
    lossy = rif5.judge_tx_dstar(log, w, "N0TEST", lossy=True)
    assert lossy.problems == [] and len(lossy.findings) == 1 and lossy.facts["voice_written"] == 47
    # the sync at the position, not at the written index: sync missing on the frame that carries word 42
    bad = [f if not (f.ftype == ms.DSTAR_DATA and f.payload[:9] == w[42]) else f._replace(payload=f.payload[:9] + b"\x01\x02\x03") for f in log]
    problems = " ".join(rif5.judge_tx_dstar(bad, w, "N0TEST", lossy=True).problems)
    assert "position 42" in problems and "sync triple" in problems
    # a word dropped at the end is a gap of the tail
    short = rif5.judge_tx_dstar(dstar_dropped(w, set(range(40, 50))), w, "N0TEST", lossy=True)
    assert short.problems == [] and short.facts["voice_dropped"] == 10


def dmr_dropped(words_, dropped_bursts):
    up = rf.dmr_keyup(words_, 1023008, 4017, colour_code=1)
    head, voice, term = up[0], up[1:-1], up[-1]
    items = [(ms.SET_MODE, bytes((2,)))] + [(ms.DMR_DATA2, bytes((0,)) + head[1][1:])] * 8
    items += [(ms.DMR_DATA2, bytes((0,)) + v[1][1:]) for i, v in enumerate(voice) if i not in dropped_bursts]
    items += [(ms.DMR_DATA2, bytes((0,)) + term[1][1:])] * 3
    return timed(items, gap=0.06)


def test_judge_tx_dmr_a_dropped_burst_does_not_renumber_the_letters():
    w = words("dmr", 60)                                   # 20 bursts, A at 0, 6, 12, 18
    sent = rf.padded_frames("dmr", w)
    log = dmr_dropped(w, {4})                              # burst E dropped: the written bursts keep the letters of the bursts they carry
    v = rif5.judge_tx_dmr(log, sent, 1023008, 4017, 1, lossy=True)
    assert v.problems == [] and v.facts["voice_dropped"] == 3 and len(v.findings) == 1
    strict = rif5.judge_tx_dmr(log, sent, 1023008, 4017, 1)
    assert strict.problems and "voice word 12" in " ".join(strict.problems)
    # burst 36 of the first rig run: the burst written at index 36 is the sent burst 37, a B, and carries no voice sync
    assert not any("voice sync" in p for p in v.problems)


def with_modes(log, *extra):
    """The frames of `log` after its SET_MODE, with extra (offset seconds from the first native frame, payload) SET_MODEs inserted."""
    first = next(f for f in log if f.ftype not in (ms.SET_MODE,))
    out = list(log)
    for dt, payload in extra:
        out.append(hf(first.mono + dt, ms.SET_MODE, payload))
    return sorted(out, key=lambda f: f.mono)


def test_judge_tx_a_leading_idle_is_the_hang_of_the_previous_over():
    w = words("ysf", 15)
    sent = rf.padded_frames("ysf", w)
    log = ysf_tx(w)
    # the first rig run, YSF after DMR: SET_MODE 00 first, then the claim
    lead = with_modes(log, (-0.05, b"\x00"))
    v = rif5.judge_tx_ysf([f for f in lead if not (f.ftype == ms.SET_MODE and f.payload == b"\x03")] + [hf(lead[0].mono + 0.1, ms.SET_MODE, b"\x03")], sent, "N0TEST")
    assert v.problems == [] and v.facts["leading_idle_s"]
    # an idle plus the claim, in order
    ok = rif5.judge_tx_ysf(sorted(log + [hf(log[0].mono - 0.3, ms.SET_MODE, b"\x00")], key=lambda f: f.mono), sent, "N0TEST")
    assert ok.problems == [] and ok.facts["leading_idle_s"][0] >= 0.3
    # another mode's claim before ours, no claim at all
    odd = rif5.judge_tx_ysf(sorted(log + [hf(log[0].mono - 0.3, ms.SET_MODE, b"\x02")], key=lambda f: f.mono), sent, "N0TEST")
    assert "before the claim" in " ".join(odd.problems)
    assert "no SET_MODE 03" in " ".join(rif5.judge_tx_ysf([f for f in log if f.ftype != ms.SET_MODE], sent, "N0TEST").problems)
    # an idle in the middle of the stream
    mid = rif5.judge_tx_ysf(sorted(log + [hf(log[3].mono + 0.01, ms.SET_MODE, b"\x00")], key=lambda f: f.mono), sent, "N0TEST")
    assert "never interrupts a stream" in " ".join(mid.problems)


def stepped(frames, steps=rif5.YSF_GAIN_STEPS):
    """The words as a YSF listener hears them: only b2 mapped by the named steps."""
    out = []
    for f in frames:
        bits = nxdn.from_dmr(f)
        out.append(nxdn.to_dmr(nxdn.set_gain_index(bits, crossaudio.mapped_gain(nxdn.gain_index(bits), steps))))
    return out


def test_compare_ysf_words_accepts_the_documented_gain_steps_and_only_those_bits():
    sent = words("ysf", 30)
    heard = stepped(sent)
    assert heard != sent                                                   # the first rig run: word 0 differed
    v = rif5.compare_ysf_words(sent, heard)
    assert v.problems == [] and v.findings == [] and v.facts["voice_written"] == 30 and v.facts["extra_words"] == 0
    # the same words unchanged are not the documented map: a finding, not a problem
    same = rif5.compare_ysf_words(sent, sent)
    assert same.problems == [] and same.findings and "documented map" in same.findings[0]
    # another map (only the ingress step) is a finding with the histogram of the steps
    other = rif5.compare_ysf_words(sent, stepped(sent, ("p10",)))
    assert other.problems == [] and other.findings and other.facts["b2_steps"]
    # a bit other than b2 is a problem
    broken = list(heard)
    bits = bytearray(nxdn.from_dmr(broken[4]))
    bits[0] ^= 0x80
    broken[4] = nxdn.to_dmr(bytes(bits))
    bad = rif5.compare_ysf_words(sent, broken)
    assert bad.problems and "word 4" in bad.problems[0]
    # fewer words, more words
    assert "29 voice words, 30 sent" in " ".join(rif5.compare_ysf_words(sent, heard[:29]).problems)
    more = rif5.compare_ysf_words(sent, heard + stepped([bytes(ysfp.SILENCE)] * 5))
    assert more.problems == [] and more.facts["extra_words"] == 5


def test_judge_tx_ysf_takes_the_gain_steps_and_names_the_extra_end_frame():
    w = words("ysf", 15)
    sent = rf.padded_frames("ysf", w)
    heard = stepped(w) + stepped([bytes(ysfp.SILENCE)] * 5)              # 20 words written, as the first rig run: voice_written 20 of 15 sent
    v = rif5.judge_tx_ysf(ysf_tx(heard), sent, "N0TEST")
    assert v.problems == [] and v.findings == [] and v.facts["voice_written"] == 20 and v.facts["extra_words"] == 5


def test_timeline_lines_order_fields_and_bound(tmp_path):
    events = [("status", 100.00, 1, 1, 6, 10, 10, 8), ("frame", 100.01, ms.SET_MODE, 1, 1), ("frame", 100.02, ms.DSTAR_DATA, 12, 0x9e),
              ("status", 100.25, 1, 1, 10, 10, 10, 8)]
    lines = rif5.timeline_lines(events, 100.0, sent=(100.0, 100.04, 2), summary={"case": "RIF5-TX-dstar", "facts": {"voice_sent": 2}})
    kinds = [l["ev"] for l in lines]
    assert kinds[-1] == "summary" and kinds.count("sent") == 2 and kinds.count("status") == 2 and kinds.count("frame") == 2
    assert [l["t"] for l in lines[:-1]] == sorted(l["t"] for l in lines[:-1])
    by = {(l["ev"], l.get("type") or l.get("index")): l for l in lines}
    assert by[("frame", "SET_MODE")]["arg"] == "01" and by[("frame", "SET_MODE")]["len"] == 1
    assert by[("frame", "DSTAR_DATA")]["mode"] == "dstar" and "arg" not in by[("frame", "DSTAR_DATA")]
    assert by[("sent", 1)]["est"] is True and by[("sent", 1)]["t"] == 0.02
    status = [l for l in lines if l["ev"] == "status"][0]
    assert {k: status[k] for k in ("ev", "t", "mode", "flags", "space")} == {"ev": "status", "t": 0.0, "mode": 1, "flags": 1,
                                                                            "space": {"dstar": 6, "dmr1": 10, "dmr2": 10, "ysf": 8}}
    # the fields of DV-Router's proposed format (design-276 1.8) are added, ours stay
    assert status["dir"] == "m2h" and status["event"] == "status" and status["tx"] is True and status["space_triple"] == [6, 10, 8]
    assert by[("frame", "DSTAR_DATA")]["dir"] == "h2m" and by[("frame", "DSTAR_DATA")]["event"] == "write"
    # bounded: the first lines are kept, the cut is named, the summary stays last
    many = [("frame", 100.0 + i * 0.001, ms.DSTAR_DATA, 12, 0) for i in range(50)]
    cut = rif5.timeline_lines(many, 100.0, summary={"x": 1}, max_lines=10)
    assert len(cut) == 12 and cut[-2] == {"ev": "truncated", "lines": 40} and cut[-1] == {"x": 1, "ev": "summary"}


def test_write_timeline_writes_json_lines_under_the_run_dir(tmp_path):
    import json
    import sys
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "cases"))
    from rif5_support import write_timeline
    modem = SimpleNamespace(events_between=lambda since: [("frame", since + 0.5, ms.DSTAR_EOT, 0, None)])
    path = write_timeline(SimpleNamespace(out=str(tmp_path)), "RIF5-LONG-dstar", modem, 10.0, (10.0, 11.0, 2), {"case": "RIF5-LONG-dstar"})
    assert path == tmp_path / "rif5" / "timeline-LONG-dstar.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["ev"] for r in rows] == ["sent", "frame", "sent", "summary"] and rows[1]["type"] == "DSTAR_EOT"


def test_switches_and_settings_of_the_firmware_like_fake_and_the_fix_image_cases():
    assert not rif5.fw_on({}) and not rif5.j_on({}) and rif5.fw_on({"DVT_RIF5FW": " ON "}) and rif5.j_on({"DVT_RIF5J": "on"})
    assert not rif5.fw_on({"DVT_RIF5FW": "1"}) and not rif5.j_on({"DVT_RIF5J": "off"})
    assert rif5.fw_settings({}) == ("mmdvm", None)
    assert rif5.fw_settings({"DVT_RIF5FW_PROFILE": "HS", "DVT_RIF5FW_TXDELAY_MS": "250"}) == ("hs", 250)
    assert rif5.fw_settings({"DVT_RIF5FW_TXDELAY_MS": "x"}) == ("mmdvm", None)


def test_status_cadence_keyed_wants_the_fast_poll():
    fast = [hf(10.0 + 0.05 * i, ms.GET_STATUS) for i in range(40)]
    v = rif5.status_cadence_keyed(fast, 10.0, 12.0)
    assert v.problems == [] and v.facts["median"] == pytest.approx(0.05, abs=0.001) and v.facts["polls"] == 40
    slow = [hf(10.0 + 0.25 * i, ms.GET_STATUS) for i in range(12)]
    assert "median GET_STATUS interval 0.250" in " ".join(rif5.status_cadence_keyed(slow, 10.0, 13.0).problems)
    gap = fast[:20] + fast[28:]                                                     # 0.45 s without a poll
    assert "longest" in " ".join(rif5.status_cadence_keyed(gap, 10.0, 12.0).problems)
    assert "only 3" in " ".join(rif5.status_cadence_keyed(fast[:4], 10.0, 12.0).problems)
    assert rif5.status_cadence_keyed(fast, 20.0, 21.0).problems                    # polls outside the window do not count


def test_has_series_counts_a_zero_and_needs_the_label():
    table = rif5.counters(METRICS)
    assert table is not None
    assert rif5.has_series(table, "dv_edge_radio_port_total", result="up")
    assert not rif5.has_series(table, "dv_edge_radio_modem_total", result="tx_underrun") and not rif5.has_series(None, "x")
    more = dict(table)
    more[("dv_edge_radio_modem_total", ("proc", "radio"), ("result", "tx_underrun"))] = 0.0
    assert rif5.has_series(more, "dv_edge_radio_modem_total", result="tx_underrun")


def test_observer_fields_are_the_nineteen_of_the_document():
    assert len(rif5.OBSERVER_FIELDS) == 19 and {"rx_freq_hz", "tx_freq_hz"} <= set(rif5.OBSERVER_FIELDS)
    assert rif5.EDGE_SILENCE_S < rif5.NATIVE_OVER_TIMEOUT_S


# ------------------------------------------------------------------ the modem goes silent

def silence_log(mute_at=100.0, backoff=2.0, versions=6, polls_after=7):
    """Polls every 250 ms up to the mute, `polls_after` more, then the reopen dialogue: backoff + 2 s settle after the last poll."""
    log = [hf(mute_at - 0.25 * (8 - i), ms.GET_STATUS) for i in range(8)]
    last = mute_at
    for i in range(polls_after):
        last = mute_at + 0.25 * (i + 1)
        log.append(hf(last - 0.02, ms.GET_STATUS))
    start = last + 0.25 + backoff + 2.0
    log += [hf(start + 1.8 * i, ms.GET_VERSION) for i in range(versions)]
    return log, mute_at


def test_judge_silence():
    log, t = silence_log()
    v = rif5.judge_silence(log, t)
    assert v.problems == [] and v.facts["version_tries"] == 6 and v.facts["polls_unanswered"] == 7
    assert 1.5 < v.facts["last_poll_after_last_answer_s"] < 2.1
    # a host that gives up after two missed polls, or never gives up
    early, t = silence_log(polls_after=2)
    assert rif5.judge_silence(early, t).problems
    late, t = silence_log(polls_after=14)
    assert "last poll came" in " ".join(rif5.judge_silence(late, t).problems)
    # a reopen sooner than the documented 2 s backoff + 2 s settle
    quick, t = silence_log(backoff=-1.0)
    assert "backoff" in " ".join(rif5.judge_silence(quick, t).problems)
    # five tries is a finding (the document: at most six)
    five, t = silence_log(versions=5)
    v = rif5.judge_silence(five, t, tries=True)
    assert v.problems == [] and "GET_VERSION" in " ".join(v.findings)
    assert rif5.judge_silence(five, t).findings == []          # a modem that answers the reopen: one try is normal
    assert "no GET_STATUS" in " ".join(rif5.judge_silence([], 5.0).problems)
    # reopen delays: the doubling
    log, t = silence_log(backoff=2.0)
    more, _ = silence_log(mute_at=140.0, backoff=4.0)
    delays = rif5.reopen_delays(log + more, 0.0)
    assert len(delays) >= 1 and all(isinstance(d[0], float) for d in delays)


# ------------------------------------------------------------------ status rows and metrics

STATE = {"procs": [{"name": "web"}, {"name": "dv-edge-radio", "radio": {"ports": [
    {"name": "other", "state": "up"}, {"name": "rif5-modem", "state": "down", "tx_state": "rx-only", "fence": None, "mode": None}]}}]}
ADMIN = {"radio_edges": [{"edge": "radio-1", "radio": {"ports": [{"name": "rif5-modem", "state": "down", "down_reason": "inactive", "errors": 1}]}}]}


def test_port_rows():
    assert rif5.public_port(STATE, "rif5-modem")["state"] == "down"
    assert rif5.public_port(STATE, "missing") is None and rif5.public_port({}, "x") is None and rif5.public_port(None, "x") is None
    assert rif5.admin_port(ADMIN, "rif5-modem")["down_reason"] == "inactive"
    assert rif5.admin_port({}, "rif5-modem") is None


METRICS = """# TYPE dv_edge_radio_port_total counter
dv_edge_radio_port_total{port="rif5-modem",result="up"} 2
dv_edge_radio_port_total{port="rif5-modem",result="down"} 1
dv_edge_radio_port_total{port="other",result="up"} 9
dv_edge_radio_modem_total{port="rif5-modem",result="inactive"} 1
dv_edge_radio_frames_total{port="rif5-modem",dir="tx",result="ok"} 40
dv_edge_radio_frames_total{port="rif5-modem",dir="rx",result="ok"} 10
dv_edge_radio_mode{port="rif5-modem"} 0
dv_launcher_child_up{child="x"} 1
"""


def test_counters_and_delta():
    a = rif5.counters(METRICS)
    b = rif5.counters(METRICS.replace('result="inactive"} 1', 'result="inactive"} 2').replace('dir="tx",result="ok"} 40', 'dir="tx",result="ok"} 55'))
    assert rif5.delta(a, b, "dv_edge_radio_modem_total", result="inactive") == 1
    assert rif5.delta(a, b, "dv_edge_radio_frames_total", dir="tx", result="ok") == 15
    assert rif5.delta(a, b, "dv_edge_radio_frames_total", dir="rx") == 0
    assert rif5.delta(a, b, "dv_edge_radio_port_total") == 0
    assert rif5.counters("") is None and rif5.counters("dv_launcher_child_up 1\n") is None and rif5.delta(None, b, "x") is None
    assert not any(k[0] == "dv_launcher_child_up" for k in a)


def test_gate_and_constants():
    assert rif5.gate_on({"DVT_RIF5": "on"}) and rif5.gate_on({"DVT_RIF5": " ON "}) and not rif5.gate_on({}) and not rif5.gate_on({"DVT_RIF5": "1"})
    assert rif5.RF_TYPES.isdisjoint(rif5.SETUP_TYPES) and ms.SET_MODE not in rif5.RF_TYPES
    assert dmrp.module_tg("Q") == 4017


def test_snippet_merges_into_a_template_that_has_the_radio_table():
    """A template with `[edges.radio] enable = "auto"` (off): the snippet's keys win key by key and the port row is added once."""
    template = TEMPLATE + '[edges.radio]\nenable = "auto"\n'
    doc = tomllib.loads(sut_config.render(template, "172.30.0", extras=[SNIPPET.read_text()]))
    assert doc["edges"]["radio"]["enable"] is True and len(doc["edges"]["radio"]["port"]) == 1
    assert rif5.port_from_config(doc).modes == {"dstar": "P", "dmr": "Q", "ysf": "R"}


def test_damaged_dmr_copy_fails_the_colour_code_check_and_nothing_else():
    good = rf.data_burst(dmrp.full_lc(dmrp.FLCO_GROUP, 1023007, 4017), rf.DT_VOICE_LC_HEADER, 1)
    bad = rif5.dmr_damaged_copy(1023007, 4017, 1)
    assert len(bad) == 34 and bad[0] == 0x41 and len(good) == 33
    info = rf.parse_data_burst(bad[1:])
    assert info["cc"] == 2 and info["dt"] == rf.DT_VOICE_LC_HEADER and info["slot_ok"] and info["rs_ok"]     # a valid burst of colour code 2
    assert rf.parse_data_burst(good)["cc"] == 1
    assert rif5.dmr_damaged_copy(1023007, 4017, 15)[1:] == rf.data_burst(dmrp.full_lc(dmrp.FLCO_GROUP, 1023007, 4017), rf.DT_VOICE_LC_HEADER, 0)
    assert rif5.NATIVE_OVER_TIMEOUT_S == 0.3
