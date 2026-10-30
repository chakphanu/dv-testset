# SPDX-License-Identifier: MIT
"""MMDVM Homebrew control packet builders and parsers, DMR embedded LC
(dvtest/proto/dmr.py)."""
import hashlib

import pytest

from dvtest.audio import signals
from dvtest.proto import dmr

ID = 1023007
ID_BYTES = ID.to_bytes(4, "big")


def test_login_packets():
    assert dmr.rptl(ID) == b"RPTL" + ID_BYTES
    assert dmr.rptk(ID) == b"RPTK" + ID_BYTES + bytes(32)
    digest = dmr.auth_hash(b"\x01\x02\x03\x04", "passw0rd")
    assert digest == hashlib.sha256(b"\x01\x02\x03\x04passw0rd").digest()
    assert len(dmr.rptk(ID, digest)) == 40
    with pytest.raises(ValueError):
        dmr.rptk(ID, bytes(31))


def test_rptc_layout():
    data = dmr.rptc(ID, callsign="N0CALL", rx_freq=438800000, tx_freq=431200000, power=5, colour=1,
                    lat=1.5, lon=-2.25, height=12, location="Nowhere", slots="2",
                    url="http://example.org/", software="dvtest", package="dvtest")
    assert len(data) == dmr.CONFIG_SIZE == 8 + sum(w for _, w in dmr.RPTC_FIELDS)
    fields, offset = {}, 8
    for name, width in dmr.RPTC_FIELDS:
        fields[name] = data[offset:offset + width].decode()
        offset += width
    assert data[:8] == b"RPTC" + ID_BYTES
    assert fields["callsign"] == "N0CALL  " and fields["rx_freq"] == "438800000"
    assert fields["tx_freq"] == "431200000" and fields["power"] == "05" and fields["colour"] == "01"
    assert fields["lat"] == "1.500000" and fields["lon"] == "-2.250000" and fields["height"] == "012"
    assert fields["location"].rstrip() == "Nowhere" and fields["slots"] == "2"
    assert fields["url"].rstrip() == "http://example.org/" and fields["package"].rstrip() == "dvtest"
    assert len(dmr.rptc(ID, description="x" * 50)) == dmr.CONFIG_SIZE


def test_options_ping_close():
    assert dmr.rpto(ID, "TS2=4001;") == b"RPTO" + ID_BYTES + b"TS2=4001;"
    assert dmr.rptping(ID) == b"RPTPING" + ID_BYTES and len(dmr.rptping(ID)) == 11
    assert dmr.rptcl(ID) == b"RPTCL" + ID_BYTES
    assert dmr.rptcl(ID, 13) == b"RPTCL" + ID_BYTES + bytes(4)
    with pytest.raises(ValueError):
        dmr.rptcl(ID, 10)


def test_parse_control():
    assert dmr.parse_control(b"RPTACK\x01\x02\x03\x04") == {"tag": "RPTACK", "tail": b"\x01\x02\x03\x04"}
    assert dmr.parse_control(b"MSTPONG" + ID_BYTES) == {"tag": "MSTPONG", "tail": ID_BYTES}
    assert dmr.parse_control(b"MSTNAK") == {"tag": "MSTNAK", "tail": b""}
    assert dmr.parse_control(b"MSTCL" + ID_BYTES)["tag"] == "MSTCL"
    assert dmr.parse_control(b"DMRD" + bytes(51)) is None


def test_bitfield_round_trip():
    assert dmr.bitfield(2, False, dmr.DATA, dmr.HEADER) == 0xA1
    assert dmr.bitfield(2, False, dmr.DATA, dmr.TERMINATOR) == 0xA2
    assert dmr.bitfield(2, False, dmr.VOICE_SYNC, 0) == 0x90
    assert dmr.bitfield(1, True, dmr.VOICE, 5) == 0x45
    for value in range(256):
        f = dmr.parse_bitfield(value)
        assert dmr.bitfield(f["slot"], f["private"], f["frame_type"], f["vseq"]) == value


def test_sync_payloads():
    default = dmr.data_sync_payload()
    assert default == dmr.data_sync_payload(dmr.BS_DATA)
    assert default[13] == 0x0D and default[14:19] == bytes(dmr.DATA_SYNC) and default[19] == 0xD0
    assert dmr.data_sync_payload(dmr.MS_DATA)[13:20] == bytes.fromhex("0D5D7F77FD7570")
    assert dmr.data_sync_payload(dmr.BS_VOICE)[13:20] == bytes.fromhex("0755FD7DF75F70")


def test_gateway_link_burst():
    burst = dmr.gateway_link(ID, 4001, 1023008, 0x01020304)
    assert len(burst) == 5 and all(len(p) == dmr.PACKET_SIZE for p in burst)
    fields = [dmr.parse(p) for p in burst]
    assert [f["seq"] for f in fields] == [0, 1, 2, 3, 4]
    assert [f["bitfield"] for f in fields] == [0xE1] * 3 + [0xE2] * 2
    assert {(f["src"], f["dst"], f["repeater"], f["stream"]) for f in fields} == {(ID, 4001, 1023008, 0x01020304)}
    assert all(f["payload"] == dmr.data_sync_payload() for f in fields)
    assert [dmr.parse(p)["bitfield"] for p in dmr.gateway_link(ID, 4000, ID, 1, 1, 1)] == [0xE1, 0xE2]


# ---------------------------------------------------------------- embedded LC (ETSI TS 102 361-1)

def _poly_mod(value, poly):
    """value mod poly over GF(2)."""
    while value.bit_length() >= poly.bit_length():
        value ^= poly << (value.bit_length() - poly.bit_length())
    return value


def _even(value):
    return bin(value).count("1") % 2 == 0


def test_generator_tables_match_polynomials():
    # B.3.2: QR (17,9,5), g(x) = x8+x5+x4+x3+1, first two data bits deleted,
    # extended by an overall parity bit
    for data in range(128):
        word = dmr.qr16_7(data)
        assert _poly_mod(word >> 1, 0x139) == 0 and _even(word)
    # B.3.4: Hamming (15,11,3), g(x) = x4+x+1, extended by an overall parity bit
    for data in range(2048):
        word = dmr.hamming16_11(data)
        assert _poly_mod(word >> 1, 0x13) == 0 and _even(word)


def test_emb_fields():
    for cc in range(16):
        for pi in (0, 1):
            for lcss in range(4):
                assert dmr.parse_emb(dmr.emb(cc, pi, lcss)) == {"cc": cc, "pi": pi, "lcss": lcss, "valid": True}
    assert dmr.emb(0, 0, 0) == 0
    assert not dmr.parse_emb(dmr.emb(1, 0, 1) ^ 0x0001)["valid"]


def test_full_lc_and_checksum():
    lc = dmr.full_lc_group(ID, 4012)
    assert lc == bytes((0, 0, 0)) + (4012).to_bytes(3, "big") + ID.to_bytes(3, "big")
    assert dmr.parse_full_lc(lc) == {"pf": 0, "flco": 0, "fid": 0, "options": 0, "dst": 4012, "src": ID}
    assert dmr.parse_full_lc(dmr.full_lc(dmr.FLCO_PRIVATE, 1, 2))["flco"] == 3
    assert dmr.lc_checksum(bytes(range(1, 10))) == 45 % 31
    assert dmr.lc_checksum(b"\xff" * 9) == 2295 % 31


def test_embedded_lc_null_and_round_trip():
    # D.1: an all-zero message gives an all-zero transmit matrix
    assert dmr.encode_embedded_lc(bytes(9)) == [0, 0, 0, 0]
    for lc in (dmr.full_lc_group(ID, 4012), bytes(range(9)), b"\xff" * 9, dmr.full_lc(3, 0xFFFFFF, 1, 0x10, 0x20, 1)):
        fragments = dmr.encode_embedded_lc(lc)
        assert len(fragments) == 4 and all(0 <= f < 1 << 32 for f in fragments)
        assert dmr.decode_embedded_lc(fragments) == (lc, True, True)
    fragments = dmr.encode_embedded_lc(dmr.full_lc_group(ID, 4012))
    fragments[2] ^= 1 << 7
    assert dmr.decode_embedded_lc(fragments)[1] is False


def test_embedded_lc_interleave_order():
    # figure B.3: burst 1 starts LC(71) LC(60) LC(49) LC(39) LC(29) LC(19) LC(9) PC(15) LC(70)
    for bit in (71, 60, 49, 39, 29, 19, 9, 70):
        lc = (1 << bit).to_bytes(9, "big")
        fragments = dmr.encode_embedded_lc(lc)
        position = {71: 31, 60: 30, 49: 29, 39: 28, 29: 27, 19: 26, 9: 25, 70: 23}[bit]
        assert fragments[0] >> position & 1


def test_voice_packets_embedded_lc():
    frames = [bytes([i]) * 9 for i in range(54)]
    plain, _ = dmr.voice_packets(frames, ID, 4012, 0x01020304)
    packets, seq = dmr.voice_packets(frames, ID, 4012, 0x01020304, embedded_lc=True, colour_code=1)
    assert seq == 19 and len(packets) == 18
    for p, q in zip(plain, packets):
        # same header bytes and AMBE frames; only the burst centre differs
        assert p[:20] == q[:20] and dmr.extract(p[20:53]) == dmr.extract(q[20:53])
    for start in (0, 6, 12):
        sf = [dmr.parse(p)["payload"] for p in packets[start:start + 6]]
        assert sf[0] == dmr.parse(plain[start])["payload"]             # burst A unchanged
        got = dmr.superframe_lc(sf[1:5])
        assert [e["lcss"] for e in got["emb"]] == [1, 3, 3, 2]
        assert all(e["valid"] and e["cc"] == 1 and e["pi"] == 0 for e in got["emb"])
        assert got["fec_ok"] and got["checksum_ok"]
        assert (got["lc"]["src"], got["lc"]["dst"], got["lc"]["flco"]) == (ID, 4012, dmr.FLCO_GROUP)
        assert dmr.embedded(sf[5]) == (dmr.emb(1, 0, 0), 0)            # burst F: null, LCSS 0
        assert dmr.parse_emb(dmr.embedded(sf[5])[0])["valid"]
    other = dmr.voice_packets(frames[:18], ID, 4012, 1, embedded_lc=dmr.full_lc(3, ID, 99), colour_code=7)[0]
    got = dmr.superframe_lc([dmr.parse(p)["payload"] for p in other[1:5]])
    assert got["lc"]["flco"] == 3 and got["lc"]["dst"] == 99 and got["emb"][0]["cc"] == 7


def test_voice_packets_default_unchanged():
    # sha256 of voice_packets output pinned before embedded LC existed
    digest = hashlib.sha256()
    for n in (1, 2, 3, 36, 40, 54, 60, 120):
        packets, seq = dmr.voice_packets(signals.prng_frames("dmr", n, seed="pin%d" % n), ID, 4012, 0x22222222)
        digest.update(b"".join(packets) + bytes([seq]))
    assert digest.hexdigest() == "dc75804d0fb5bd24baf2a6f69bc4f8eb953d493d9c041577e1315bb73c19be87"
