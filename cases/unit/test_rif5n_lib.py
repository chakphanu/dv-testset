# SPDX-License-Identifier: MIT
"""dvtest.rif5n (DVT_RIF5N, the image wip-f1f3777, docs/rif5-cases.md "The image wip-f1f3777"): the judgements over synthetic host logs, the parsers of
what a room client hears, the RIF5k / RIF5l checks and the D-STAR rider comparison. Offline. The synthetic logs are built with the encoders of
dvtest.mmdvm_rf_nxdn and dvtest.mmdvm_rf_p25 (written for the RX direction of a real MMDVMHost); what DV-Router writes on TX is the same air frame
(inbound RDCH LICH, the row's RAN, one VCALL, one TX_REL; one HDU, LDU1 / LDU2, one TDU), so a judge that accepts the builder output and names each
deviation is a model of the document and of those two builders only; the rig run is the check against DV-Router."""
import math

import pytest

from dvtest import dstar_sd, echo
from dvtest import mmdvm_modem as mm
from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_rf_nxdn as nxrf
from dvtest import mmdvm_rf_p25 as p25rf
from dvtest import mmdvm_serial as ms
from dvtest import rif5, rif5n
from dvtest.proto import nxdn, p25

pytestmark = pytest.mark.unit

SRC_N, DST_N, RAN = 1002, 2005, 5
SRC_P, DST_P, NAC = 1023008, 2025, 677


def host(ftype, payload, t):
    return ms.HostFrame(t, t, ftype, bytes(payload))


def words(kind, n, seed="u"):
    frames = mm.default_frames(kind, n, seed)
    return rf.canonical_frames("nxdn", frames) if kind == "nxdn" else frames


def nxdn_log(sent, src=SRC_N, dst=DST_N, ran=RAN, mode=ms.MODE_NXDN, headers=1, trailers=1, extra=()):
    air = nxrf.build_keyup(src, dst, list(sent) + list(extra), ran=ran, header_frames=headers, trailer_frames=trailers)
    log = [host(ms.SET_MODE, bytes((mode,)), 0.0)] if mode is not None else []
    return log + [host(t, p, 0.1 + 0.08 * i) for i, (t, p, _d) in enumerate(air)]


def p25_log(sent, src=SRC_P, dst=DST_P, nac=NAC, with_hdu=True, mode=ms.MODE_P25, extra=()):
    air = p25rf.build_keyup(src, dst, list(sent) + list(extra), nac=nac, with_hdu=with_hdu)
    log = [host(ms.SET_MODE, bytes((mode,)), 0.0)] if mode is not None else []
    return log + [host(t, b"\x00" + p[1:], 0.1 + 0.18 * i) for i, (t, p, _d) in enumerate(air)]       # the host writes flag 00, the modem flag 01


# ------------------------------------------------------------------ NXDN, room to modem

def test_the_nxdn_judge_accepts_a_clean_over_of_every_size_and_counts_its_frames():
    for n in (24, 148, 22):                                        # whole frames, a long over, a partial last frame (two words of padding)
        sent = words("nxdn", n)
        v = rif5n.judge_tx_nxdn(nxdn_log(sent), sent, SRC_N, DST_N, RAN)
        assert not v.problems, (n, v.problems)
        assert v.facts["headers"] == 1 and v.facts["trailers"] == 1 and v.facts["voice_frames"] == math.ceil(n / 4)
        assert v.facts["voice_sent"] == n and v.facts["extra_words"] == (-n) % 4


def test_the_nxdn_judge_names_each_deviation():
    sent = words("nxdn", 24)
    ok = nxdn_log(sent)

    def problems(log, **kw):
        args = dict(src=SRC_N, dst=DST_N, ran=RAN)
        args.update(kw)
        return " | ".join(rif5n.judge_tx_nxdn(log, sent, args["src"], args["dst"], args["ran"]).problems)

    assert "RAN" in problems(ok, ran=6)                                                   # the SACCH carries the row's RAN
    assert "dst" in problems(ok, dst=2006) and "src" in problems(ok, src=1001)
    assert "SET_MODE" in problems(nxdn_log(sent, mode=None))
    assert "SET_MODE" in problems(nxdn_log(sent, mode=ms.MODE_DMR))
    assert "headers" in problems(nxdn_log(sent, headers=2)) or "[header x 2" in problems(nxdn_log(sent, headers=2))
    assert "trailer" in problems(ok[:-1])                                                 # no TX_REL
    assert "after the 24 sent" in problems(nxdn_log(sent, extra=[bytes(7)] * 4))          # an extra silence frame (a full last frame adds none)
    changed = list(sent)
    changed[9] = bytes(b ^ 0x10 for b in changed[9])
    assert "word 9" in " | ".join(rif5n.judge_tx_nxdn(nxdn_log(changed), sent, SRC_N, DST_N, RAN).problems)
    stray = ok + [host(ms.YSF_DATA, bytes(121), 9.0)]
    assert "frames" in problems(stray) and "ysf" in problems(stray).lower() or "YSF_DATA" in problems(stray)
    assert "native size 49" in problems(ok + [host(ms.NXDN_DATA, bytes(50), 9.0)])
    # a dropped word is a problem in a short over and a FINDING in a long (lossy) one
    dropped = list(sent[:5]) + list(sent[6:]) + [bytes(7)]
    v = rif5n.judge_tx_nxdn(nxdn_log(dropped), sent, SRC_N, DST_N, RAN)
    assert any("not written" in p or "differs" in p for p in v.problems)
    v = rif5n.judge_tx_nxdn(nxdn_log(dropped), sent, SRC_N, DST_N, RAN, lossy=True)
    assert not [p for p in v.problems if "not written" in p] and v.findings


def test_the_nxdn_judge_reads_the_lich_and_the_sacch_structure():
    sent = words("nxdn", 24)
    log = nxdn_log(sent)
    # the voice frames of the builder carry the SACCH structure 3, 2, 1, 0 like the document: no finding
    assert not rif5n.judge_tx_nxdn(log, sent, SRC_N, DST_N, RAN).findings
    # an outbound LICH on the header is refused: the document says all TX uses the inbound RDCH LICH
    info = nxrf.decode_frame(nxrf.from_payload(log[1].payload))
    assert info["lich"]["outbound"] is False and info["lich"]["rfct"] == nxdn.RFCT_RDCH
    frame = nxrf.assemble_frame(nxdn.lich(nxdn.RFCT_RDCH, nxdn.FCT_SACCH_NS, nxdn.OPTION_STEAL_FACCH, outbound=True), nxdn.sacch(0, nxdn.SACCH_IDLE, RAN),
                                [nxdn.facch1(nxdn.VCALL, SRC_N, DST_N)] * 2)
    bad = [log[0], host(ms.NXDN_DATA, nxrf.to_payload(frame), 0.1)] + log[2:]
    assert "LICH" in " | ".join(rif5n.judge_tx_nxdn(bad, sent, SRC_N, DST_N, RAN).problems)


# ------------------------------------------------------------------ P25, room to modem

def test_the_p25_judge_accepts_a_clean_over_and_reads_the_header_fields():
    for n in (36, 144, 30):                                       # whole LDUs, a long over, a partial last LDU (six words of padding)
        sent = words("p25", n)
        v = rif5n.judge_tx_p25(p25_log(sent), sent, SRC_P, DST_P, NAC)
        assert not v.problems, (n, v.problems)
        assert (v.facts["hdus"], v.facts["tdus"], v.facts["ldus"]) == (1, 1, math.ceil(n / 9))
        assert v.facts["hdu"]["tgid"] == DST_P and v.facts["hdu"]["algid"] == 0x80 and v.facts["extra_words"] == (-n) % 9


def test_the_p25_judge_names_each_deviation():
    sent = words("p25", 36)
    ok = p25_log(sent)

    def problems(log, **kw):
        args = dict(src=SRC_P, dst=DST_P, nac=NAC)
        args.update(kw)
        return " | ".join(rif5n.judge_tx_p25(log, sent, args["src"], args["dst"], args["nac"]).problems)

    assert "NAC" in problems(ok, nac=0x293) and "dst" in problems(ok, dst=1) and "src" in problems(ok, src=1)
    assert "SET_MODE" in problems(p25_log(sent, mode=None))
    assert "HDUs" in problems(p25_log(sent, with_hdu=False))
    assert "LDUs written" in problems(p25_log(sent, extra=[p25rf.NULL_IMBE] * 18))         # an extra full silence LDU pair
    assert "NAC" in problems(p25_log(sent, nac=0x111))
    assert "TDUs" in problems(ok + [ok[-1]]) and "TDUs" in problems(ok[:-1])
    changed = list(sent)
    changed[20] = bytes(b ^ 0x01 for b in changed[20])
    assert "word 20" in " | ".join(rif5n.judge_tx_p25(p25_log(changed), sent, SRC_P, DST_P, NAC).problems)
    assert "native sizes" in problems(ok + [host(ms.P25_LDU, bytes(100), 9.0)])
    assert "frames" in problems(ok + [host(ms.DSTAR_EOT, b"", 9.0)])
    # the flag byte of the host is 00; the modem's 01 on a host frame is refused
    flagged = [f if f.ftype == ms.SET_MODE else f._replace(payload=b"\x01" + f.payload[1:]) for f in ok]
    assert "flag byte" in problems(flagged)


def test_the_p25_hdu_fields_are_read_back_from_the_air_frame():
    hdu = b"\x00" + p25rf.build_hdu(NAC, DST_P, mfid=0, algid=0x80, key_id=0, mi=bytes(9))
    assert rif5n.p25_hdu_fields(hdu) == {"mi": bytes(9), "mfid": 0, "algid": 0x80, "kid": 0, "tgid": DST_P}
    enc = b"\x00" + p25rf.build_hdu(NAC, DST_P, mfid=0x90, algid=0x81, key_id=0x1234, mi=bytes(range(9)))
    assert rif5n.p25_hdu_fields(enc) == {"mi": bytes(range(9)), "mfid": 0x90, "algid": 0x81, "kid": 0x1234, "tgid": DST_P}


# ------------------------------------------------------------------ what a room client hears

def test_heard_nxdn_collects_header_voice_and_trailer():
    sent = words("nxdn", 24)
    packets = nxdn.stream_packets(sent, 1001, 31680)
    heard = rif5n.heard_nxdn([(1.0 + 0.08 * i, p) for i, p in enumerate(packets)])
    assert heard["headers"] == [(1001, 31680)] and heard["trailers"] == 1 and heard["voice"] == sent and heard["groups"] == {True}
    assert heard["other"] == 0 and heard["packets"] == len(packets) and heard["t_last"] > heard["t_first"]
    assert rif5n.heard_nxdn([(0.0, b"NXDNP" + bytes(12)), (0.1, b"junk")])["packets"] == 0


def test_heard_p25_collects_the_words_the_identity_and_checks_the_lifecycle():
    sent = words("p25", 36)
    records = p25.voice_records(sent, 1023007, 31681)
    heard = rif5n.heard_p25([(2.0 + 0.02 * i, r) for i, r in enumerate(records)])
    assert heard["voice"] == sent and heard["srcs"] == {1023007} and heard["dst"] == 31681 and heard["terminators"] == 1
    assert heard["lifecycle"] == [] and heard["algid"] == {0x80}
    cut = rif5n.heard_p25([(0.0, r) for r in records[:-1]])                                   # no terminator
    assert cut["lifecycle"] and cut["terminators"] == 0
    short = rif5n.heard_p25([(0.0, r) for r in p25.call_records(sent[:10], 1, 2, padded=False)])
    assert any("inside an LDU" in p for p in short["lifecycle"])


# ------------------------------------------------------------------ RIF5k and RIF5l

def test_the_end_sequence_check_of_rif5k_on_the_existing_judges():
    # D-STAR: 22 words, 22 data frames (no end unit rendered as DATA); the older images wrote a 23rd
    dstar = rif5.Verdict([], [], {"voice_written": 22, "voice_sent": 22, "extra_words": 0})
    assert rif5n.end_sequence_problems("dstar", dstar, 22) == []
    assert rif5n.end_sequence_problems("dstar", rif5.Verdict([], [], {"voice_written": 23, "extra_words": 1}), 22)
    # YSF: 15 words = 3 full groups: header + 3 + terminator = 5 frames; the older images wrote 6 (an extra all-zero group)
    assert rif5n.end_sequence_problems("ysf", rif5.Verdict([], [], {"frames": 5, "extra_words": 0}), 15) == []
    problems = rif5n.end_sequence_problems("ysf", rif5.Verdict([], [], {"frames": 6, "extra_words": 5}), 15)
    assert len(problems) == 2
    assert rif5n.end_sequence_problems("ysf", rif5.Verdict([], [], {"frames": 2 + 4, "extra_words": 0}), 17) == []       # a partial group is completed, zero padded
    assert rif5n.end_sequence_problems("dmr", rif5.Verdict([], [], {"voice_bursts": 6, "extra_words": 0}), 18) == []
    assert rif5n.end_sequence_problems("dmr", rif5.Verdict([], [], {"voice_bursts": 7, "extra_words": 3}), 18)


def test_a_voiceless_join_must_leave_the_byte_link_silent():
    log = [host(ms.GET_STATUS, b"", 1.0), host(ms.SET_MODE, b"\x00", 1.5), host(ms.GET_STATUS, b"", 2.0)]
    assert not rif5n.judge_no_keying(log, 1.0, 3.0).problems                                  # polls and an idle SET_MODE are not keying
    keyed = log + [host(ms.SET_MODE, b"\x03", 1.2), host(ms.YSF_DATA, bytes(121), 1.4), host(ms.P25_LDU, bytes(19), 1.6), host(ms.SEND_CWID, b"N0CALL", 1.8)]
    problems = rif5n.judge_no_keying(keyed, 1.0, 3.0).problems
    assert len(problems) == 4 and any("SET_MODE 03" in p for p in problems) and any("YSF_DATA" in p for p in problems)
    assert not rif5n.judge_no_keying(keyed, 5.0, 6.0).problems                                # outside the window


# ------------------------------------------------------------------ RIF5h: the D-STAR rider

def dstar_over(wires, ambe):
    """Host frames of a D-STAR over: SET_MODE, the header, one data frame per (seq, wire), the EOT."""
    log = [host(ms.SET_MODE, b"\x01", 0.0),
           host(ms.DSTAR_HEADER, rf.dstar_header("N0TEST", rpt1="N0CALL G", rpt2="N0CALL B", ur="CQCQCQ", flags=(0, 0, 0)), 0.1)]
    log += [host(ms.DSTAR_DATA, ambe[i] + bytes(wire), 0.2 + 0.02 * i) for i, (_seq, wire) in enumerate(wires)]
    return log + [host(ms.DSTAR_EOT, b"", 0.2 + 0.02 * len(wires))]


def ride_pair():
    payload = dstar_sd.aprs_position_payload("N0TEST", 10.5, 20.25, "rif5n")
    ride = dstar_sd.text_gps_fields("N0TEST STATUS", payload, superframes=3)
    base = dstar_sd.render([], frames=len(ride))
    ambe = [rf.dstar_ambe(w) for w in echo.make_frames("dstar", len(ride), "rif5n-ride")]
    return dstar_over(base, ambe), dstar_over(ride, ambe), ambe


def test_the_rider_is_decoded_from_the_slow_data_the_modem_received():
    base, ride, _ = ride_pair()
    assert rif5n.dprs_lines(base) == []
    lines = rif5n.dprs_lines(ride)
    assert len(lines) >= 1 and lines[0][0] is True
    source, dest, path, info = lines[0][1]
    assert (source, dest, path) == ("N0TEST", "API510", "DSTAR*") and info.startswith(b"!1030.00N/02015.00E")
    assert [e["text"] for e in rif5n.rider_events(ride) if e["kind"] == "text"][0].startswith("N0TEST STATUS")
    assert rif5n.parse_dprs(b"junk") is None and rif5n.parse_dprs(b"A>B:info\r") == ("A", "B", "", b"info")


def test_the_over_is_otherwise_identical_with_the_rider_and_the_comparison_names_each_difference():
    base, ride, ambe = ride_pair()
    v = rif5n.compare_overs(base, ride)
    assert not v.problems, v.problems
    assert v.facts["changed"] and all(i % 21 for i in v.facts["changed"]) and v.facts["frames"][0] == v.facts["frames"][1]
    # the negative control: the same two overs on a port without ride_along must not differ at all
    assert any("ride_along is off" in p for p in rif5n.compare_overs(base, ride, rider=False).problems)
    assert not rif5n.compare_overs(base, base, rider=False).problems
    assert any("nothing rode along" in p for p in rif5n.compare_overs(base, base).problems)
    # a changed voice word, a changed sync, a different length, a different EOT
    data = [i for i, f in enumerate(ride) if f.ftype == ms.DSTAR_DATA]
    word = list(ride)
    word[data[7]] = ride[data[7]]._replace(payload=bytes(b ^ 0xFF for b in ride[data[7]].payload[:9]) + ride[data[7]].payload[9:])
    assert any("voice word 7" in p for p in rif5n.compare_overs(base, word).problems)
    sync = list(ride)
    sync[data[21]] = ride[data[21]]._replace(payload=ride[data[21]].payload[:9] + b"\x00\x00\x00")
    assert any("sync" in p for p in rif5n.compare_overs(base, sync).problems)
    assert any("data frames" in p for p in rif5n.compare_overs(base, ride[:-3] + ride[-1:]).problems)
    assert any("EOT" in p for p in rif5n.compare_overs(base, ride[:-1]).problems)
    # a slot of the base that carried native slow data may not be overwritten: change a base field to non-filler, the ride still differs there
    native = list(base)
    at = next(i for i in changed_indices(base, ride))
    native[data[at]] = base[data[at]]._replace(payload=base[data[at]].payload[:9] + b"\x01\x02\x03")
    assert any("not the unused filler" in p for p in rif5n.compare_overs(native, ride).problems)
    del ambe


def changed_indices(base, ride):
    bd, rd = rif5n.dstar_data(base), rif5n.dstar_data(ride)
    return [i for i, (x, y) in enumerate(zip(bd, rd)) if x.payload != y.payload]


def test_the_rf_dprs_fields_make_a_radio_heard_over_the_decoder_reads_back():
    fields = rif5n.rf_dprs_fields("N0CALL")
    air = rf.dstar_keyup(echo.make_frames("dstar", 63, "rif5n-heard"), slow=lambda n: fields[n][1])
    frames = [host(t, p, i * 0.02) for i, (t, p, _d) in enumerate(air)]
    lines = rif5n.dprs_lines(frames)
    assert lines and lines[0][1][0] == "N0CALL" and lines[0][0] is True and b"1030.00N" in lines[0][1][3]


# ------------------------------------------------------------------ metrics

def test_metric_total_sums_a_family_and_says_none_without_one():
    text = ('dv_pkt_rf_drop_total{reason="rf_origin"} 3\ndv_pkt_rf_drop_total{reason="query_rate"} 2\n'
            'dv_edge_radio_over_total{port="p",dir="tx",result="no_voice"} 4\n')
    assert rif5n.metric_total(text, "dv_pkt_rf_drop_total", reason="rf_origin") == 3
    assert rif5n.metric_total(text, "dv_pkt_rf_drop_total") == 5
    assert rif5n.metric_total(text, "dv_pkt_missing_total") is None and rif5n.metric_total(None, "x") is None
    assert rif5n.NO_VOICE[0] == "dv_edge_radio_over_total" and rif5n.NO_VOICE[1] == {"dir": "tx", "result": "no_voice"}


def test_the_switches_and_the_port_row_of_the_five_mode_shape():
    assert rif5.n_on({"DVT_RIF5N": "on"}) and not rif5.n_on({}) and rif5.portctl1_on({"DVT_PORTCTL1": " ON "}) and not rif5.portctl1_on({"DVT_RIF5N": "on"})
    doc = {"edges": {"radio": {"enable": True, "callsign": "n0call", "tx_enable": True, "port": [{
        "name": "rif5-modem", "kind": "mmdvm-serial", "udp": "172.30.0.30:3340", "udp_bind": "172.30.0.11:3341", "rx_freq": 434000000, "tx_freq": 434000000,
        "modes": {"dstar": "P", "dmr": "Q", "ysf": "R", "nxdn": "S", "p25": "T"}, "nxdn": {"ran": 5, "tg": 2005}, "p25": {"nac": 677, "tg": 2025},
        "levels": {"p25_tx_level": 70}, "ride_along": True}]}}}
    port = rif5.port_from_config(doc)
    assert (port.nxdn_ran, port.nxdn_tg, port.p25_nac, port.p25_tg, port.ride_along) == (5, 2005, 677, 2025, True)
    freq, cfg = rif5.expected_setup(port)
    assert cfg[4] == 0x1F and cfg[15] == ms.pct(70) and cfg[16] == ms.pct(50)
    plain = rif5.port_from_config({"edges": {"radio": {"enable": True, "callsign": "N0CALL", "port": [{
        "name": "x", "kind": "mmdvm-serial", "udp": "127.0.0.1:5", "modes": {"dstar": "P"}}]}}})
    assert (plain.nxdn_ran, plain.nxdn_tg, plain.p25_nac, plain.p25_tg, plain.ride_along) == (1, 1, 659, 1, False)     # config.md defaults
    assert rif5.expected_setup(plain)[1][4] == 1 and 15 not in rif5.expected_setup(plain)[1]


def test_the_open_judge_takes_the_p25_and_nxdn_level_bytes_as_documented_when_the_modes_are_on():
    doc = {"edges": {"radio": {"enable": True, "callsign": "N0CALL", "port": [{
        "name": "x", "kind": "mmdvm-serial", "udp": "127.0.0.1:5", "rx_freq": 434000000, "tx_freq": 434000000, "modes": {"p25": "T", "nxdn": "S"}}]}}}
    port = rif5.port_from_config(doc)
    freq, want = rif5.expected_setup(port)
    frame = bytearray(ms.V2_FRAME_LEN)
    frame[:3] = bytes((0xE0, ms.V2_FRAME_LEN, ms.SET_CONFIG))
    for at, value in want.items():
        frame[at] = value
    frame[18] = frame[19] = ms.pct(50)
    payload = bytes(frame[3:])
    d = rif5.Dialogue(frames=[host(ms.GET_VERSION, b"", 0.0), host(ms.SET_FREQ, freq[3:], 0.1), host(ms.SET_CONFIG, payload, 0.2), host(ms.GET_STATUS, b"", 0.3)],
                      versions=[host(ms.GET_VERSION, b"", 0.0)], freq=host(ms.SET_FREQ, freq[3:], 0.1), config=host(ms.SET_CONFIG, payload, 0.2), statuses=[])
    v = rif5.judge_open(d, port)
    assert not v.problems, v.problems
    assert sorted(v.facts["unstated_level_bytes"]) == [18, 19]                                # 15 and 16 are named by the document now
    wrong = bytearray(frame)
    wrong[16] = 7                                                                              # NXDN level is not the row's
    d2 = d._replace(config=host(ms.SET_CONFIG, bytes(wrong[3:]), 0.2))
    assert any("(16, 7, 128)" in p for p in rif5.judge_open(d2, port).problems)


# ------------------------------------------------------------------ the config snippets

def rendered(name):
    import tomllib
    from pathlib import Path
    from dvtest import sut_config
    path = Path(__file__).resolve().parents[2] / "rig" / "config" / "dv-router" / name
    return tomllib.loads(sut_config.render("", "172.30.0", own=11, tester=30, extras=[path.read_text(encoding="utf-8")]))


def test_the_five_mode_snippets_render_one_port_with_the_documented_keys_and_differ_only_in_ride_along():
    plain, ride = rendered("rif5n-modes.toml"), rendered("rif5n-ride.toml")
    port = rif5.port_from_config(plain)
    assert sorted(port.modes) == sorted(("dstar", "dmr", "ysf", "nxdn", "p25")) and all(port.modes[m] == rif5.ROOMS[m] for m in port.modes)
    assert len(plain["edges"]["radio"]["port"]) == 1 and len(set(port.modes.values())) == 5          # one port, five different modules (config.md)
    assert (port.nxdn_ran, port.nxdn_tg, port.p25_nac, port.p25_tg) == (5, 2005, 677, 2025) and not port.ride_along
    assert port.peer == ("172.30.0.30", rif5.MODEM_PORT) and port.bind == ("172.30.0.11", rif5.EDGE_PORT)
    assert plain["edges"]["nxdn"] == {"enable": True, "tg_modules": "%d:%s" % (rif5n.NXDN_ROOM_TG, rif5.ROOMS["nxdn"])}
    assert plain["edges"]["p25"] == {"enable": True, "server_module": rif5.ROOMS["p25"], "server_tg": rif5n.P25_ROOM_TG}
    assert rif5.port_from_config(ride).ride_along is True
    ride["edges"]["radio"]["port"][0].pop("ride_along")
    assert ride == plain                                                                             # nothing else differs
    # the tx_ack of the exact sentence of config.md for this callsign (also in rif5-mmdvm-udp.toml)
    assert plain["edges"]["radio"]["tx_ack"] == "I am the control operator of N0CALL and accept responsibility for its transmissions"
    assert not rif5.port_from_config(rendered("rif5-mmdvm-udp.toml")).ride_along                      # the base snippet keeps it off


def test_the_base_snippet_is_unchanged_in_shape():
    port = rif5.port_from_config(rendered("rif5-mmdvm-udp.toml"))
    assert port.modes == {"dstar": "P", "dmr": "Q", "ysf": "R"} and port.nxdn_tg == 1 and not port.ride_along


def test_the_p25_rx_air_delivers_the_tdu_as_type_30_with_19_bytes():
    sent = words("p25", 18)
    air = p25rf.build_keyup(SRC_P, DST_P, sent, nac=NAC)
    assert [t for t, _p, _d in air] == [ms.P25_HEADER, ms.P25_LDU, ms.P25_LDU, ms.P25_LDU]           # the builder tags the TDU 31 (the host-to-modem tag)
    rx = rif5n.rx_air("p25", air)
    assert [(t, len(p)) for t, p, _d in rx] == [(0x30, 100), (0x31, 217), (0x31, 217), (0x30, 19)]   # radio.md 7.3: 30 takes 19/46/55/100, 31 takes 217
    assert rx[1:3] == air[1:3] and rx[0] == air[0]
    nxdn_air = nxrf.build_keyup(SRC_N, DST_N, words("nxdn", 8), ran=RAN)
    assert rif5n.rx_air("nxdn", nxdn_air) == nxdn_air
