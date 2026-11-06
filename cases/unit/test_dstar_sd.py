# SPDX-License-Identifier: MIT
"""dvtest/dstar_sd.py: the D-STAR slow data generator and parser, spec-derived (docs/dstar-slowdata-cases.md).

The two oracle files (vectors/dstar_sd/corpus.json) are read here too: they were written by the
decoders of MMDVMHost and DStarGateway, so a test that reproduces them needs no Docker."""
import json
import os

import pytest

from dvtest import dstar_sd as sd
from dvtest import oracle_dstar_sd as orc
from dvtest import slowdata
from dvtest.proto import dextra

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
VEC = os.path.join(HERE, "..", "..", "vectors", "dstar_sd")

TEXT = "N0CALL: CQ DE N0CALL"


def _vec(name):
    with open(os.path.join(VEC, name)) as fh:
        return json.load(fh)


def _frames(superframes):
    return sd.render(sd.superframes_to_halves(superframes))


# ------------------------------------------------------------------ scrambler and checksums

def test_scrambler_is_derived_from_the_polynomial_not_typed_in():
    # [STD6] Ap1.1: S(x) = x^7 + x^4 + 1, all ones, LSB first
    assert sd.SCRAMBLER.hex() == "704f934064746d302b"
    assert sd.SCRAMBLE3 == bytes.fromhex("704f93") == dextra.SCRAMBLE
    # a maximal length sequence of a 7 bit register repeats after 127 bits
    bits = "".join("%d" % ((b >> k) & 1) for b in sd.lfsr_bytes(40) for k in range(8))
    assert bits[:127] == bits[127:254]
    assert bits[:63] != bits[63:126]


def test_filler_scrambles_to_the_known_wire_bytes():
    assert sd.xor3(b"\x66\x66\x66") == dextra.NULL_SLOW == bytes.fromhex("1629f5")
    assert sd.xor3(sd.xor3(b"abc")) == b"abc"


def test_crc16_check_values():
    assert sd.crc16_x25(b"123456789") == 0x906E             # CRC-16/X-25 catalogue value
    assert sd.crc16_x25(b"ABCDEF") == 0x6F5E                # the DStarGateway unit test value ("$$CRC6F5E,ABCDEF")
    radio = dextra.radio_header(my="N0CALL")
    assert sd.crc16_x25(radio[:39]).to_bytes(2, "little") == radio[39:] == dextra.header_crc(radio[:39])


def test_nmea_checksum_known_sentence():
    # the textbook example sentence of NMEA 0183 descriptions
    assert sd.nmea_checksum("GPGLL,4916.45,N,12311.12,W,225444,A") == 0x31
    assert sd.nmea_sentence("GPGLL,4916.45,N,12311.12,W,225444,A", crlf=False) == "$GPGLL,4916.45,N,12311.12,W,225444,A*31"


def test_dprs_line_shape_and_crc_scope():
    line = sd.dprs_line("N0CALL>API282,DSTAR*:/211234h0000.00N/00000.00E-027/000/x")
    assert line.startswith(b"$$CRC") and line[9:10] == b"," and line.endswith(b"\r")
    assert int(line[5:9], 16) == sd.crc16_x25(line[10:])             # the oracle run: the CR is covered
    other = sd.dprs_line("N0CALL>API282,DSTAR*:/211234h0000.00N/00000.00E-027/000/x", crc_covers_cr=False)
    assert other[5:9] != line[5:9]


# ------------------------------------------------------------------ block builders

def test_text_blocks():
    blocks = sd.text_blocks(TEXT)
    assert [b[0] for b in blocks] == [0x40, 0x41, 0x42, 0x43]
    assert b"".join(b[1:] for b in blocks) == TEXT.encode()
    assert b"".join(b[1:] for b in sd.text_blocks("hi")) == b"hi" + b" " * 18
    assert sd.text_blocks(b"\xc1" * 20)[0] == bytes([0x40]) + b"\xc1" * 5


def test_squelch_block_is_the_published_example():
    # [SLOW]: "c2 19 19 / 66 66 66" for code 19 (the hex of the decimal value as shown to the user)
    assert sd.squelch_block(19) == sd.squelch_block("19") == bytes.fromhex("c21919666666")
    assert sd.squelch_block(7)[1:3] == b"\x07\x07"
    with pytest.raises(ValueError):
        sd.squelch_block("1a")


def test_header_blocks_cover_41_bytes_in_9_blocks():
    radio = dextra.radio_header(my="N0CALL", rpt1="N0CALL B", rpt2="XLX999 B")
    blocks = sd.header_blocks(radio)
    assert [b[0] for b in blocks] == [0x55] * 8 + [0x51]
    assert b"".join(b[1:6] for b in blocks[:8]) + blocks[8][1:2] == radio
    assert blocks[8][2:] == b"\x66" * 4
    with pytest.raises(ValueError):
        sd.header_blocks(radio[:40])


def test_simple_data_blocks_length_nibble_and_filler():
    blocks = sd.simple_data_blocks(b"$GPGGA,1*5C\r\n")
    assert [b[0] for b in blocks] == [0x35, 0x35, 0x33]
    assert blocks[2] == bytes([0x33]) + b"C\r\n" + b"\x66\x66"
    assert sd.simple_data_blocks(b"12345")[0][0] == 0x35 and len(sd.simple_data_blocks(b"12345")) == 1
    assert sd.simple_data_blocks(b"") == []
    one = sd.simple_data_blocks(b"\n")[0]
    assert one == bytes([0x31, 0x0A]) + b"\x66" * 4


def test_simple_data_forbidden_characters():
    for bad in (0x00, 0x11, 0x13, 0x76, 0x84, 0xE7, 0xFE):                # [STD6] 6.2
        with pytest.raises(ValueError):
            sd.simple_data_blocks(bytes([0x41, bad]))
    assert sd.simple_data_blocks(bytes([0x41, 0x84]), check=False)


def test_fast_data_mini_headers():
    assert sd.fast_data_mini_header(1) == 0x81 and sd.fast_data_mini_header(15) == 0x8F
    assert sd.fast_data_mini_header(16) == 0x90 and sd.fast_data_mini_header(28) == 0x9C
    for bad in (0, 29):
        with pytest.raises(ValueError):
            sd.fast_data_mini_header(bad)


# ------------------------------------------------------------------ the published block layouts

def test_published_structure_is_reproduced():
    pub = _vec("published_structure.json")["examples"]
    expect = {
        "header": sd.header_blocks(dextra.radio_header(my="N0CALL")),
        "gga": sd.simple_data_blocks(orc.GGA.encode()),
        "rmc": sd.simple_data_blocks(orc.RMC.encode()),
        "free": sd.simple_data_blocks(orc.FREEFORM),
        "dprs": sd.simple_data_blocks(sd.dprs_line(orc.DPRS_PAYLOAD)),
        "text": sd.text_blocks(TEXT),
        "squelch": [sd.squelch_block(19)],
    }
    for name, blocks in expect.items():
        assert ["%02x" % b[0] for b in blocks] == pub[name]["blocks"], name
    for name in ("gga", "rmc", "free", "dprs"):
        total = sum(b[0] & 0x0F for b in expect[name])
        assert total == pub[name]["payload_bytes"], name
    assert expect["squelch"][0].hex() == pub["squelch"]["full_hex"][0]


# ------------------------------------------------------------------ layout engine

def test_render_sync_scrambling_and_superframe_length():
    frames = sd.render(sd.halves(sd.text_blocks(TEXT)))
    assert len(frames) == 21 and [s for s, _ in frames] == list(range(21))
    assert frames[0] == (0, sd.SYNC)
    plain = [sd.xor3(w) for _, w in frames[1:9]]
    assert b"".join(plain)[0] == 0x40
    assert all(sd.xor3(w) == b"\x66\x66\x66" for _, w in frames[9:])
    longer = sd.render(sd.halves(sd.simple_data_blocks(b"x" * 120)))
    assert len(longer) == 63 and longer[21][1] == sd.SYNC and longer[42][1] == sd.SYNC


def test_render_skips_the_sync_frame():
    halves = [bytes([i, i, i]) for i in range(1, 25)]                 # 24 data fields: more than one superframe
    frames = sd.render(halves)
    plain = [sd.xor3(w) for s, w in frames if s != 0]
    assert plain[:20] == halves[:20] and plain[20:24] == halves[20:24]
    assert frames[21] == (0, sd.SYNC)


def test_render_even_start_makes_a_block_straddle_the_sync():
    blocks = [bytes([0x40 + i]) + bytes([i]) * 5 for i in range(1, 12)]
    frames = sd.render(sd.halves(blocks), start=2)
    plain = [sd.xor3(w) for s, w in frames if s != 0]
    assert plain[0] == b"\x66\x66\x66"                               # frame 1 is before the start
    # the receiver pairs frame 1+2, 3+4, ...: its first block is (filler, first half of block 1)
    assert plain[0] + plain[1] != blocks[0]


def test_render_is_inverse_of_decoder_pairing():
    frames = sd.render(sd.halves(sd.text_blocks(TEXT)))
    dec = sd.Decoder()
    assert [e["text"] for e in dec.feed_all(frames)] == [TEXT]
    assert len(dec.blocks) == 10


def test_place_spills_into_the_next_superframe():
    blocks = [bytes([0x40 + i]) * 6 for i in range(4)]
    sfs = sd.place(blocks, first_slot=8)
    assert len(sfs) == 2 and sfs[0][8:] == blocks[:2] and sfs[1][:2] == blocks[2:]
    assert sfs[0][:8] == [None] * 8 and sfs[1][2:] == [None] * 8
    assert len(sd.place(blocks, 0)) == 1 and len(sd.place(blocks, 0, superframes=3)) == 3
    with pytest.raises(ValueError):
        sd.place(blocks, 10)


def test_superframes_to_halves_checks_the_width():
    with pytest.raises(ValueError):
        sd.superframes_to_halves([[None] * 9])
    assert len(sd.superframes_to_halves([[None] * 10])) == 20


def test_interleave_patterns_of_the_g4klx_tables():
    tb = sd.text_blocks(TEXT)
    gps = [bytes([0x35]) + bytes([0x61 + i]) * 5 for i in range(14)]
    sfs = sd.interleave(sd.LAYOUTS["text-gps"], {"T": ("cycle", tb), "G": ("once", gps)})
    assert len(sfs) == 3                                              # 6 G slots per superframe: 14 blocks = 3 superframes
    first = sfs[0]
    assert [b[0] for b in first[0::2][:4]] == [0x40, 0x41, 0x42, 0x43]  # T at slots 0,2,4,6
    assert first[1] == gps[0] and first[3] == gps[1] and first[7] == gps[3] and first[8] == gps[4] and first[9] == gps[5]
    assert sfs[2][-1] is None and sfs[2][0] == tb[0]
    hdr = sd.header_blocks(dextra.radio_header())
    sq = sd.squelch_block(19)
    sfs = sd.interleave(sd.LAYOUTS["header"], {"H": ("cycle", hdr)}, superframes=2)
    assert sfs[0] == hdr + [None] and sfs[1] == hdr + [None]
    sfs = sd.interleave("CHHHHHHHHH", {"C": ("cycle", [sq]), "H": ("cycle", hdr)}, superframes=2)
    assert sfs[0][0] == sq and sfs[0][1:] == hdr and sfs[1][0] == sq and sfs[1][1:] == hdr
    with pytest.raises(ValueError):
        sd.interleave("TT", {})


def test_interleave_header_cycle_carries_on_over_superframes():
    hdr = sd.header_blocks(dextra.radio_header())
    sfs = sd.interleave("AHHHHHHHHH"[:10], {"H": ("cycle", hdr), "A": ("once", [sd.block(0x35, b"abcde")])}, superframes=2)
    assert sfs[0][1:] == hdr[:9] and sfs[1][0] is None and sfs[1][1:] == hdr[:9]
    sfs = sd.interleave("HHHHHHHHFF", {"H": ("cycle", hdr)}, superframes=2)
    assert sfs[1][0] == hdr[8] and sfs[1][1] == hdr[0]                # position persists: block 9, then 1


def test_dvrouter_layout_equals_the_existing_encoder():
    tb = sd.text_blocks(TEXT)
    frames = _frames([tb + tb + [None, None]])
    assert [w for _, w in frames] == slowdata.encode_text_over(TEXT)
    assert [w for _, w in frames][:9] == [sd.SYNC] + dextra.slow_text(TEXT)[:8]


# ------------------------------------------------------------------ decoders

def test_decoder_text_header_squelch_and_fast_events():
    radio = dextra.radio_header(my="N0CALL", rpt1="N0CALL B", rpt2="XLX999 B")
    frames = _frames([[sd.squelch_block(19)] + sd.header_blocks(radio)])
    events = sd.Decoder().feed_all(frames)
    assert [e["kind"] for e in events] == ["squelch", "header"]
    assert events[0]["code"] == "19" and events[1]["bytes"] == radio and events[1]["crc_ok"]
    bad = bytearray(radio)
    bad[5] ^= 1
    ev = sd.Decoder().feed_all(_frames([sd.header_blocks(bytes(bad)) + [None]]))
    assert ev[0]["kind"] == "header" and not ev[0]["crc_ok"]
    mism = sd.Decoder().feed_all(_frames([[sd.block(0xC2, b"\x19\x18")] + [None] * 9]))
    assert mism[0]["code"] is None
    fast = sd.Decoder().feed_all(_frames([[sd.block(0x9C, b"x"), sd.block(0x85, b"x"), sd.block(0x9D, b"x"), sd.block(0x80, b"x")] + [None] * 6]))
    assert [e["length"] for e in fast] == [28, 5, None, None]


def test_decoder_dprs_and_nmea():
    line = sd.dprs_line(orc.DPRS_PAYLOAD)
    frames = sd.render(sd.halves(sd.simple_data_blocks(line) + sd.simple_data_blocks(orc.GGA.encode())))
    events = sd.Decoder().feed_all(frames)
    assert [e["kind"] for e in events] == ["dprs", "nmea"]
    assert events[0]["crc_ok"] and events[0]["line"] == line[10:] and events[0]["crc"] == line[5:9].decode()
    assert events[1]["checksum_ok"] and events[1]["sentence"] == orc.GGA.strip()
    # a changed character: the sentence is still reported, flagged
    broken = bytearray(line)
    broken[20] ^= 1
    ev = sd.Decoder().feed_all(sd.render(sd.halves(sd.simple_data_blocks(bytes(broken)))))
    assert ev[0]["kind"] == "dprs" and not ev[0]["crc_ok"]


def test_decoder_limits_of_the_dvrouter_buffers():
    long300 = orc._dprs_of_length(300)
    long301 = orc._dprs_of_length(301)
    frames = lambda raw: sd.render(sd.halves(sd.simple_data_blocks(raw)))
    assert [e["kind"] for e in sd.Decoder().feed_all(frames(long300))] == ["dprs"]
    assert sd.Decoder().feed_all(frames(long301)) == []
    assert [e["kind"] for e in sd.Decoder(max_dprs=301).feed_all(frames(long301))] == ["dprs"]
    n82 = orc._nmea_of_length(82).encode()
    n83 = orc._nmea_of_length(83).encode()
    assert [e["kind"] for e in sd.Decoder().feed_all(frames(n82))] == ["nmea"]
    assert sd.Decoder().feed_all(frames(n83)) == []


def test_sync_policy_decides_the_split_text():
    tb = sd.text_blocks(TEXT)
    frames = sd.render(sd.superframes_to_halves(sd.place(tb, 8)))
    acc = sd.Decoder("accumulate").feed_all(frames)
    rst = sd.Decoder("reset").feed_all(frames)
    assert [e["text"] for e in acc] == [TEXT] and rst == []
    with pytest.raises(ValueError):
        sd.Decoder("both")


def test_a_consumed_message_clears_the_slots_and_old_slots_mix():
    a, b = sd.text_blocks(TEXT), sd.text_blocks(orc.TEXT_B)
    frames = _frames([a + [None] * 6, b[:2] + [None] * 8, b[2:] + [None] * 8])
    assert [e["text"] for e in sd.Decoder("accumulate").feed_all(frames)] == [TEXT, orc.TEXT_B]
    mix = sd.Decoder("accumulate").feed_all(_frames([a[:3] + [None] * 7, b[3:] + [None] * 9]))
    assert [e["text"] for e in mix] == [TEXT[:15] + orc.TEXT_B[15:]]


def test_straddling_blocks_never_decode():
    frames = sd.render(sd.halves(sd.text_blocks(TEXT) * 3), start=2)
    assert sd.Decoder("accumulate").feed_all(frames) == []
    assert sd.Decoder("reset").feed_all(frames) == []


def test_text_bit_7_is_stripped():
    frames = _frames([sd.text_blocks(b"\xc1" * 20) + [None] * 6])
    assert sd.Decoder().feed_all(frames)[0]["text"] == "A" * 20


# ------------------------------------------------------------------ fast data

def test_fast_data_superframe_round_trip_and_structure():
    payload = bytes((7 * i + 1) & 0xFF for i in range(208))
    frames = sd.fast_data_superframe(payload)
    assert len(frames) == 21 and frames[0][2] == sd.SYNC
    assert sd.fast_data_payload(frames) == payload
    plain_data = [sd.xor3(d) for seq, _v, d in frames if seq != 0]
    assert plain_data[0][0] == 0x9C and plain_data[2][0] == 0x94 and plain_data[18][0] == 0x94
    assert [p[0] for p in plain_data[0::2]] == [0x9C] + [0x94] * 9
    assert all(sd.xor3(d)[0] == 0x20 for seq, _v, d in frames if seq in (2, 4, 20))      # the guard byte
    for seq, voice, _d in frames:
        unscrambled = bytes(a ^ b for a, b in zip(voice, sd.SCRAMBLER))
        assert unscrambled[4] == 0x02                                  # mitigation
    with pytest.raises(ValueError):
        sd.fast_data_superframe(payload[:-1])


def test_fast_data_payload_rejects_a_damaged_block():
    payload = bytes(range(208))
    frames = sd.fast_data_superframe(payload)
    bad = list(frames)
    seq, voice, data = bad[3]
    bad[3] = (seq, bytes([voice[0] ^ 1]) + voice[1:4] + bytes([voice[4] ^ 0x01]) + voice[5:], data)
    assert sd.fast_data_payload(bad) is None
    bad = list(frames)
    bad[1] = (1, bad[1][1], sd.xor3(bytes([0x94]) + sd.xor3(bad[1][2])[1:]))
    assert sd.fast_data_payload(bad) is None


def test_fast_data_pairs_look_like_blocks_to_a_slow_data_receiver():
    frames = sd.fast_data_superframe(bytes(208))
    dec = sd.Decoder()
    dec.feed_all([(seq, d) for seq, _v, d in frames])
    assert [t for _s, t in dec.blocks] == [0x9C] + [0x94] * 9


# ------------------------------------------------------------------ packet loss

def test_packet_loss_rewrite_table():
    # [STD6] 6.6, rows in the order of the table
    z = b"\x66\x66\x66"
    assert sd.packet_loss_rewrite(z) == (z, z)
    assert sd.packet_loss_rewrite(b"\x31\x41\x66", first_lost=True) == (b"\x31\x80\x66", z)
    assert sd.packet_loss_rewrite(b"\x31\x41\x66") == (b"\x31\x41\x66", z)
    assert sd.packet_loss_rewrite(b"\x32\x41\x42") == (b"\x32\x41\x42", z)
    assert sd.packet_loss_rewrite(b"\x33\x41\x42") == (b"\x33\x41\x42", b"\x80\x66\x66")
    assert sd.packet_loss_rewrite(b"\x34\x41\x42") == (b"\x34\x41\x42", b"\x80\x00\x66")
    assert sd.packet_loss_rewrite(b"\x35\x41\x42") == (b"\x35\x41\x42", b"\x80\x00\x00")
    assert sd.packet_loss_rewrite(b"\x40\x41\x42") is None
    assert sd.packet_loss_rewrite(b"\x31\x41\x66", last_lost=False) is None


# ------------------------------------------------------------------ carriers

def test_over_datagrams_round_trip_through_the_existing_over_parser():
    tb = sd.text_blocks(TEXT)
    frames = _frames([tb + tb + [None, None]])
    datagrams = sd.over_datagrams(0x1234, frames)
    assert len(datagrams) == 1 + 21 + 1
    assert datagrams[0].startswith(b"DSVT") and len(datagrams[0]) == 56
    assert all(len(d) == 27 for d in datagrams[1:])
    overs = slowdata.overs(datagrams)
    assert len(overs) == 1 and overs[0]["stream_id"] == 0x1234
    assert slowdata.over_text(overs[0]) == {"text": TEXT, "repeat_ok": True, "filler_ok": True}
    assert dextra.parse_voice_raw(datagrams[1])["ambe"] == sd.NULL_AMBE


# ------------------------------------------------------------------ the committed oracle record

def test_committed_corpus_is_current_and_reproduced_by_our_decoders():
    doc = _vec("corpus.json")
    assert doc["schema"] == orc.SCHEMA and doc["image"] == orc.IMAGE
    by_name = {c["case"]: c for c in doc["cases"]}
    cases = orc.corpus()
    assert [n for n, _, _ in cases] == [c["case"] for c in doc["cases"]]
    for name, note, frames in cases:
        rec = by_name[name]
        assert rec["note"] == note, name
        assert rec["frames"] == ["%d %s" % (seq, wire.hex()) for seq, wire in frames], name
        assert rec["mmdvm_match"] and rec["dgw_match"], name
        assert [tuple(e) for e in rec["mmdvm"]] == orc._norm(orc.ours_mmdvm_view(frames)), name
        assert [tuple(e) for e in rec["dgw"]] == orc._norm(orc.ours_dgw_view(frames)), name


def test_committed_corpus_keeps_the_findings_the_doc_relies_on():
    by_name = {c["case"]: c for c in _vec("corpus.json")["cases"]}

    def texts(case, which):
        return [e for e in by_name[case][which] if e[0] == "text"]

    for case in ("text-slot8-split", "text-slot9-split", "text-missing-block-late"):
        assert texts(case, "mmdvm") == [] and len(texts(case, "dgw")) == 1            # MMDVMHost resets, DStarGateway accumulates
    assert len(texts("text-two-messages-split", "mmdvm")) == 1 and len(texts("text-two-messages-split", "dgw")) == 2
    for case in ("text-aligned", "text-slot1", "text-slot3", "text-slot6"):
        assert texts(case, "mmdvm") == texts(case, "dgw") and len(texts(case, "mmdvm")) == 1
    assert by_name["dprs-crc-without-cr"]["dgw"] == [] and len(by_name["dprs-aligned"]["dgw"]) == 1
    assert by_name["text-straddle"]["mmdvm"] == [e for e in by_name["text-straddle"]["mmdvm"] if e[0] == "block"]
    kinds = {e[0] for r in by_name.values() for e in r["dgw"]}
    assert {"text", "gpsa"} <= kinds and {k for k in kinds if k.startswith("nmea:")} == {
        "nmea:GPGGA", "nmea:GPGLL", "nmea:GPVTG", "nmea:GPRMC", "nmea:GPGSA", "nmea:GPGSV"}
