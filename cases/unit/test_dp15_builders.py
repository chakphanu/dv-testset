# SPDX-License-Identifier: MIT
"""Offline tests of the DP15 byte builders: YSF GPS record (dvtest/proto/ysf_gps.py), DMRG / DMRA (dvtest/proto/dmr.py)
and legacy M17 META text (dvtest/proto/m17.py). The expected bytes are worked out by hand from the layouts named in
the module docstrings (DV-Router parsers crates/lib/pkt/src/{ysf,dmr,m17_meta}.rs, YSFClients GPS.cpp, MMDVMHost
DMRNetwork.cpp / DMRSlot.cpp / DMRTA.cpp, M17 spec Text Data); the builders are also compared with the round trip
of an independent decoder written from the same layout."""
import pytest

from dvtest.proto import dmr, m17, ysf, ysf_gps

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ YSF GPS

def test_ysf_record_hand_vector():
    # 10 deg 30.00 N, 20 deg 15.00 E, FT-1D. Digits: lat 1,0 / 3,0 / 0,0; lon degree 20 is in the 0x30 range with code
    # 0x26 + (20 - 10) = 0x30, minutes 15 -> 0x26 + 5 = 0x2B, hundredths 0 -> 0x1C. Sum of bytes 0..17 = 0x2C8 -> 0xC8.
    want = bytes.fromhex("02226200 24 51 50 53 50 30 30 30 2b 1c 000000 03 c8 00".replace(" ", ""))
    assert ysf_gps.gps_record(10.5, 20.25) == want


def test_ysf_record_second_hand_vector():
    # 30 N 120 E: longitude 120 is in the 0x50 range (110..179) with code 0x26 + 10 = 0x30; minutes 0 -> 0x58
    want = bytes.fromhex("0222620024 5350505050 3030581c 00000003 1400".replace(" ", ""))
    assert ysf_gps.gps_record(30, 120) == want


@pytest.mark.parametrize("lat,lon", [(10.5, 20.25), (-33.85, 151.2), (51.5, -0.12), (0.01, 5.5), (-0.5, -105.75),
                                     (89.99, 179.99), (45.0, 100.0), (45.0, 109.99), (7.25, 9.99)])
def test_ysf_record_round_trip(lat, lon):
    got = ysf_gps.decode_record(ysf_gps.gps_record(lat, lon, ysf_gps.RADIO_FT3D))
    assert got is not None
    assert abs(got[0] - lat) < 0.0001 and abs(got[1] - lon) < 0.0001 and got[2] == "FT-3D"


def test_ysf_record_long_magic_and_checks():
    rec = ysf_gps.gps_record(10.5, 20.25, magic=ysf_gps.LONG_GPS)
    assert rec[1:3] == b"\x47\x64" and ysf_gps.decode_record(rec) is not None
    bad = bytearray(rec)
    bad[18] ^= 1
    assert ysf_gps.decode_record(bytes(bad)) is None             # sum byte
    bad = bytearray(rec)
    bad[1] = 0
    assert ysf_gps.decode_record(bytes(bad)) is None             # magic


def test_ysf_record_range():
    with pytest.raises(ValueError):
        ysf_gps.gps_record(95, 0)
    with pytest.raises(ValueError):
        ysf_gps.gps_record(10, 180)                              # the record holds longitude degrees up to 179


def test_ysf_gps_stream_dch_survives_the_wire_format():
    frames = [ysf.SILENCE] * 40
    packets = ysf_gps.gps_stream_packets(frames, "N0TEST", 10.5, 20.25)
    parsed = [ysf.parse(p) for p in packets]
    assert parsed[0]["kind"] == "header" and parsed[-1]["kind"] == "terminator"
    voice = [p for p in parsed if p["kind"] == "voice"]
    assert [p["fich"]["fn"] for p in voice] == list(range(8)) and {p["fich"]["ft"] for p in voice} == {7}
    assert all(p["fich"]["fi"] == ysf.FI_COMMUNICATIONS and p["fich"]["dt"] == ysf.DT_VD_MODE2 for p in voice)
    record = voice[6]["dch"] + voice[7]["dch"]
    assert record == ysf_gps.gps_record(10.5, 20.25)
    assert all(p["src"] == "N0TEST" and p["gateway"] == "N0TEST" for p in parsed)


def test_ysf_gps_stream_needs_fn_6_and_7():
    with pytest.raises(ValueError):
        ysf_gps.gps_stream_packets([ysf.SILENCE] * 30, "N0TEST", 10.5, 20.25)


# ------------------------------------------------------------------ DMRG

def test_dmrg_hand_vectors():
    # lon 90 = 2^23 steps of 360/2^25, lat 45 = 2^22 steps of 180/2^24; error estimate 0
    assert dmr.dmrg_packet(1, 45, 90, error=0) == bytes.fromhex("444d5247 000001 00 800000 400000".replace(" ", ""))
    # negatives are two's complement: lon -90 = 2^25 - 2^23 (bit 24 set -> byte 0 bit 0), lat -45 = 2^24 - 2^22
    assert dmr.dmrg_packet(1, -45, -90, error=0) == bytes.fromhex("444d5247 000001 01 800000 c00000".replace(" ", ""))


def test_dmrg_error_estimate_and_id():
    pkt = dmr.dmrg_packet(1023007, 10.5, 20.25)
    assert len(pkt) == dmr.DMRG_SIZE and pkt[4:7] == (1023007).to_bytes(3, "big")
    assert (pkt[7] >> 1) & 7 == dmr.POSITION_UNKNOWN_ERROR           # 7 = not known, not "no fix"
    rid, lat, lon, err = dmr.parse_dmrg(pkt)
    assert rid == 1023007 and err == 7 and abs(lat - 10.5) < 1e-4 and abs(lon - 20.25) < 1e-4


@pytest.mark.parametrize("lat,lon", [(0.001, 0.001), (-0.001, -0.001), (89.9, 179.9), (-89.9, -179.9), (51.5, -0.12)])
def test_dmrg_round_trip(lat, lon):
    _rid, got_lat, got_lon, _err = dmr.parse_dmrg(dmr.dmrg_packet(7, lat, lon))
    assert abs(got_lat - lat) < 1e-5 and abs(got_lon - lon) < 2e-5


# ------------------------------------------------------------------ DMRA

def test_dmra_hand_vectors():
    # 7 bit: header 00 00110 (6 characters), then the 7 bit codes of N0CALL from bit 0 of byte 0 on
    assert [p.hex() for p in dmr.dmra_packets(1, "N0CALL")] == ["444d5241000001000d398438332600"]
    # ISO 8 bit: header 01 00110 0 = 0x4C, then the characters
    assert [p.hex() for p in dmr.dmra_packets(1, "N0CALL", dmr.ALIAS_ISO8)] == ["444d52410000010 04c4e3043414c4c".replace(" ", "")]
    # UTF-16 low byte: header 11 00110 0 = 0xCC, then (00, char) pairs over two blocks
    assert [p.hex() for p in dmr.dmra_packets(1, "N0CALL", dmr.ALIAS_UTF16)] == [
        "444d5241000001 00 cc004e00300043".replace(" ", ""), "444d5241000001 01 0041004c004c00".replace(" ", "")]


@pytest.mark.parametrize("fmt", [dmr.ALIAS_7BIT, dmr.ALIAS_ISO8, dmr.ALIAS_UTF8, dmr.ALIAS_UTF16])
@pytest.mark.parametrize("text", ["A", "N0TEST ALIAS", "DVT ALIAS 012"])
def test_dmra_round_trip(fmt, text):
    packets = dmr.dmra_packets(1023008, text, fmt)
    assert all(len(p) == dmr.DMRA_SIZE and p[4:7] == (1023008).to_bytes(3, "big") for p in packets)
    assert [p[7] for p in packets] == list(range(len(packets)))
    assert dmr.parse_dmra(packets) == text


def test_dmra_incomplete_is_none():
    packets = dmr.dmra_packets(1, "N0TEST ALIAS 0123456", dmr.ALIAS_ISO8)
    assert len(packets) >= 3 and dmr.parse_dmra(packets[:-1]) is None


def test_dmra_limits():
    with pytest.raises(ValueError):
        dmr.dmra_packets(1, "X" * 32)
    with pytest.raises(ValueError):
        dmr.dmra_packets(1, "X" * 28, dmr.ALIAS_ISO8)


# ------------------------------------------------------------------ M17 META text

def test_meta_text_control_bytes():
    assert [m[0] for m in m17.meta_text_blocks("x" * 52)] == [0xF1, 0xF2, 0xF4, 0xF8]
    assert [m[0] for m in m17.meta_text_blocks("x" * 26)] == [0x31, 0x32]
    assert [m[0] for m in m17.meta_text_blocks("x" * 27)] == [0x71, 0x72, 0x74]
    assert [m[0] for m in m17.meta_text_blocks("hi")] == [0x11]
    assert m17.meta_text_blocks("hi")[0] == b"\x11hi" + b" " * 11


def test_meta_text_round_trip_and_order():
    text = "Legacy META block assembly, four blocks complete."
    metas = m17.meta_text_blocks(text)
    assert all(len(m) == 14 for m in metas)
    assert m17.parse_meta_text(metas).rstrip() == text
    assert m17.parse_meta_text(metas[::-1]).rstrip() == text
    assert m17.parse_meta_text(metas[:3]) is None


def test_meta_text_limit():
    with pytest.raises(ValueError):
        m17.meta_text_blocks("x" * 53)


def test_meta_text_stream_shape():
    metas = m17.meta_text_blocks("x" * 26)
    payloads = [bytes(16)] * 60
    packets = m17.meta_text_stream(0x1234, payloads, "N0TEST", metas, per_block=28)
    parsed = [m17.parse_stream(p) for p in packets]
    assert all(p["crc_ok"] and p["type"] == 0x0005 for p in parsed)
    assert [p["fn"] for p in parsed] == list(range(60)) and parsed[-1]["last"] and not parsed[-2]["last"]
    assert {p["meta"] for p in parsed[:28]} == {metas[0]} and {p["meta"] for p in parsed[28:]} == {metas[1]}
