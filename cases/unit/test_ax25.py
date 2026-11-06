# SPDX-License-Identifier: MIT
"""dvtest.ax25: AX.25 UI frames, TNC2 text, FCS, bit stuffing and KISS framing,
and the committed Dire Wolf vectors (vectors/direwolf/*.json) as the expected values.
Offline; no container is run here."""
import json
import os

import pytest

from dvtest import ax25

VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "direwolf")


def load(name):
    path = os.path.join(VEC, name)
    if not os.path.exists(path):
        pytest.skip("%s not generated" % name)
    with open(path, encoding="ascii") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ pure parts

def test_fcs_check_value():
    assert ax25.fcs(b"123456789") == 0x906E        # CRC-16/X-25 catalogue check value
    assert ax25.fcs(b"") == 0x0000


def test_with_fcs_is_low_byte_first_and_self_checking():
    framed = ax25.with_fcs(b"\x01\x02\x03")
    assert framed[:3] == b"\x01\x02\x03"
    assert ax25.fcs(framed) == 0x0F47             # 0xF0B8 (the X-25 residue) after the final xor


def test_address_bytes():
    assert ax25.encode_address("N0CALL", 7, cr=True, last=False).hex() == "9c6086829898ee"
    assert ax25.encode_address("A", 0, cr=False, last=True).hex() == "82404040404061"
    d = ax25.decode_address(bytes.fromhex("9c6086829898ee"))
    assert (d["call"], d["ssid"], d["bit7"], d["last"], d["text"]) == ("N0CALL", 7, True, False, "N0CALL-7")


@pytest.mark.parametrize("bad", ["", "TOOLONG1", "n0call", "N0 CALL", "N-0CALL"])
def test_address_rejects(bad):
    with pytest.raises(ValueError):
        ax25.encode_address(bad)


def test_ssid_range():
    with pytest.raises(ValueError):
        ax25.encode_address("N0CALL", 16)
    with pytest.raises(ValueError):
        ax25.split_call("N0CALL-16")


def test_tnc2_round_trip_and_h_bits():
    text = "N0CALL-15>N0TEST-1,N0DIG-2*,WIDE2-1:hello"
    frame = ax25.from_tnc2(text)
    assert ax25.to_tnc2(frame) == text
    f = ax25.parse_frame(frame)
    assert [d["bit7"] for d in f["digis"]] == [True, False]
    assert [d["last"] for d in f["digis"]] == [False, True]
    assert (f["control"], f["pid"], f["info"]) == (0x03, 0xF0, b"hello")


def test_star_on_later_digi_marks_earlier_ones_too():
    f = ax25.parse_frame(ax25.from_tnc2("N0CALL>APRS,D1,D2,D3*,D4:x"))
    assert [d["bit7"] for d in f["digis"]] == [True, True, True, False]
    assert ax25.to_tnc2(ax25.from_tnc2("N0CALL>APRS,D1,D2,D3*,D4:x")) == "N0CALL>APRS,D1,D2,D3*,D4:x"


def test_info_escapes():
    assert ax25.info_from_text("a<0xc0>b<0xDB>") == b"a\xc0b\xdb"
    assert ax25.info_to_text(b"a\xc0b\x0d") == "a<0xc0>b<0x0d>"


def test_nine_digis_and_bad_text_refused():
    with pytest.raises(ValueError):
        ax25.from_tnc2("A>B,D1,D2,D3,D4,D5,D6,D7,D8,D9:x")
    for bad in ("no colon", "NOGT:x", "n0call>aprs:x", "A>:x"):
        with pytest.raises(ValueError):
            ax25.from_tnc2(bad)


def test_parse_frame_errors():
    with pytest.raises(ValueError):
        ax25.parse_frame(b"\x00" * 10)
    frame = ax25.from_tnc2("A>B:x")
    with pytest.raises(ValueError):
        ax25.parse_frame(frame[:14])         # no control / pid


def test_stuffing_counts():
    assert ax25.stuffed_zeros(b"\xff") == 1          # 11111111 -> one zero after the first five
    assert ax25.stuffed_zeros(b"\xff\xff") == 3      # 16 ones: after 5, 10, 15
    assert ax25.stuffed_zeros(b"\x00") == 0
    assert ax25.stuffed_zeros(b"\x1f\x80") == 1      # 11111 then 0000 0001: run of five ones ends the first byte


def test_air_bits_arithmetic():
    frame = ax25.from_tnc2("A>B:x")                   # 17 bytes
    body = ax25.with_fcs(frame)
    assert ax25.air_bits(frame) == 8 + 8 * len(body) + ax25.stuffed_zeros(body) + 8
    assert ax25.air_bits(frame, preamble_flags=32, postamble_flags=2) == ax25.air_bits(frame) + 8 * 34
    assert ax25.air_ms(1200, 1200) == 1000


def test_kiss_encode_decode():
    assert ax25.kiss_encode(0, 0, b"\xc0\xdb").hex() == "c000dbdcdbddc0"
    assert ax25.kiss_encode(1, 0, b"x").hex() == "c01078c0"
    assert ax25.kiss_encode(0, ax25.CMD_TXDELAY, b"\x1e").hex() == "c0011ec0"
    frames = ax25.kiss_decode(bytes.fromhex("c000dbdcdbddc0c0c01078c0"))
    assert [(f[0], f[1], f[2]) for f in frames] == [(0, 0, b"\xc0\xdb"), (1, 0, b"x")]
    with pytest.raises(ValueError):
        ax25.kiss_encode(16, 0)


def test_kiss_decoder_is_streaming_and_counts_bad_escapes():
    dec = ax25.KissDecoder()
    stream = bytes.fromhex("c000aa" "db" "dd" "bb" "c0")
    out = []
    for b in stream:
        out += dec.feed(bytes([b]))
    assert [f[2] for f in out] == [b"\xaa\xdb\xbb"]
    dec2 = ax25.KissDecoder()
    assert dec2.feed(bytes.fromhex("c000dbaac0"))[0][2] == b"\xaa"
    assert dec2.bad_escapes == 1
    assert ax25.KissDecoder().feed(b"AB").__len__() == 0     # noise before the first FEND stays pending


# ------------------------------------------------------------------ Dire Wolf vectors

def test_rx_vectors_agree_with_our_encoder():
    doc = load("rx_frames.json")
    ran = 0
    for rec in doc["records"]:
        if rec["case"].startswith("neg-") or not rec["dw_generated"]:
            continue
        ran += 1
        ours = ax25.from_tnc2(rec["tnc2"])
        assert ours.hex() == rec["dw_ax25_hex"], rec["case"]
        assert ax25.kiss_encode(0, 0, ours).hex() == rec["dw_kiss_hex"], rec["case"]
        assert rec["ax25_match"] and rec["kiss_match"] and rec["kiss_data_equals_ax25"]
    assert ran >= 15


def test_rx_vectors_stream_is_frames_back_to_back():
    # the TNC writes each frame as FEND type data FEND: neighbours meet as "C0 C0", nothing between
    doc = load("rx_frames.json")
    frames = [r["dw_kiss_hex"] for r in doc["records"] if r.get("dw_kiss_hex")]
    assert doc["kiss_stream_hex"] == "".join(frames)
    assert "c0c000" in doc["kiss_stream_hex"]
    assert [f[3] for f in ax25.kiss_decode(bytes.fromhex(doc["kiss_stream_hex"]))] == [0] * len(frames)


def test_rx_vectors_negative_cases_are_documented():
    doc = {r["case"]: r for r in load("rx_frames.json")["records"]}
    assert doc["neg-lowercase"]["dw_generated"] is False       # Dire Wolf refuses a lower case callsign
    nine = doc["neg-9-digis"]
    assert nine["dw_ax25_hex"] == ax25.from_tnc2("A>B,D1,D2,D3,D4,D5,D6,D7,D8:x").hex()   # the ninth is dropped


def test_rx_vectors_audio_length_matches_the_bit_model():
    # samples = quiet time (about 32 symbols + a random part, 36 samples each) + bits * 44100 / 1200
    doc = load("rx_frames.json")
    seen = 0
    for rec in doc["records"]:
        if "ours_air_bits" not in rec:
            continue
        seen += 1
        frame = bytes.fromhex(rec["dw_ax25_hex"])
        bits = ax25.air_bits(frame, preamble_flags=doc["pre_flags"], postamble_flags=doc["post_flags"])
        assert bits == rec["ours_air_bits"]
        quiet = rec["samples"] - bits * 44100 / 1200
        assert 32 * 36 <= quiet < 33 * 36 + 1, (rec["case"], quiet)
        assert rec["quiet_samples"] == pytest.approx(quiet)
    assert seen >= 15


def test_tx_vectors_dire_wolf_unwrap_equals_our_decoder():
    doc = load("kiss_tx.json")
    checked = 0
    for rec in doc["records"]:
        if rec.get("volatile"):
            continue
        dw = rec["dw_unwrapped_hex"]
        sent = bytes.fromhex(rec["sent_hex"])
        # only the clean cases: the data frames of the canonical shape
        if rec["case"] in ("data-ui", "data-escapes", "info-256", "info-1024", "info-2029"):
            frames = ax25.kiss_decode(sent)
            assert len(frames) == 1 and dw == [frames[0][2].hex()], rec["case"]
            assert rec["dw_copy_equals_sent"] is True
            checked += 1
    assert checked == 5


def test_tx_vectors_parameter_frames_are_logged():
    doc = {r["case"]: r for r in load("kiss_tx.json")["records"]}
    assert any("TXDELAY = 30" in m for m in doc["param-txdelay"]["dw_messages"])
    assert any("Persistence = 63" in m for m in doc["param-p"]["dw_messages"])
    assert any("SlotTime = 10" in m for m in doc["param-slottime"]["dw_messages"])
    assert any("TXtail = 5" in m for m in doc["param-txtail"]["dw_messages"])
    assert any("FullDuplex = 1" in m for m in doc["param-fullduplex-1"]["dw_messages"])
    assert any("FullDuplex = 0" in m for m in doc["param-fullduplex-0"]["dw_messages"])
    assert any('hardware "TNC:"' in m for m in doc["param-sethardware"]["dw_messages"])
    assert any("chan 1" in m for m in doc["param-port1-txdelay"]["dw_messages"])


def test_tx_vectors_framing_edges_as_observed():
    doc = {r["case"]: r for r in load("kiss_tx.json")["records"]}
    assert doc["no-leading-fend"]["dw_unwrapped_hex"] == []           # nothing before the first FEND counts
    assert doc["garbage-before"]["dw_unwrapped_hex"] != []            # noise is dropped, the frame is taken
    assert len(doc["bad-escape-then-good"]["dw_unwrapped_hex"]) == 2  # the TNC recovers
    # frame length window of the TNC (its own message): 14 refused, 15 taken
    assert any("not in allowable range of 15 to 2123" in m for m in doc["frame-14-addresses-only"]["dw_messages"])
    assert not any("not in allowable range" in m for m in doc["frame-15-control-only"]["dw_messages"])


def test_kissutil_encoder_matches_ours():
    doc = load("kiss_tx.json")
    assert len(doc["kissutil_encoder"]) >= 4
    for rec in doc["kissutil_encoder"]:
        assert rec["kiss_match"] is True
        assert ax25.kiss_encode(0, 0, ax25.from_tnc2(rec["tnc2"])).hex() == rec["dw_kiss_hex"], rec["case"]


def test_tx_vectors_overlong_kiss_message_is_refused_and_not_stored():
    doc = {r["case"]: r for r in load("kiss_tx.json")["records"]}
    ok, over = doc["info-2029"], doc["info-2030"]
    assert len(bytes.fromhex(ok["sent_hex"])) == 2048 and ok["dw_copy_equals_sent"] is True
    assert len(bytes.fromhex(over["sent_hex"])) == 2049
    assert over["volatile"] is True and "dw_copy_hex" not in over        # stale memory: lengths only
    assert any("should end with FEND" in m for m in over["dw_messages"])


def test_tx_vectors_dvr_receive_rule_cases_as_observed():
    doc = {r["case"]: r for r in load("kiss_tx.json")["records"]}
    # port nibble other than 0: the TNC logs it as an invalid transmit channel but its KISS layer takes the frame
    for name, port in (("data-port1", 1), ("data-port2", 2), ("data-port15", 15)):
        rec = doc[name]
        frames = ax25.kiss_decode(bytes.fromhex(rec["sent_hex"]))
        assert frames[0][0] == port and frames[0][1] == 0
        assert any("Invalid transmit channel %d" % port in m for m in rec["dw_messages"]), name
        assert rec["dw_unwrapped_hex"] == [frames[0][2].hex()]
    # command nibbles 7 to 14: "KISS Invalid command N", no frame passed on
    for cmd in range(7, 15):
        rec = doc["cmd-unknown-%d" % cmd]
        assert ax25.kiss_decode(bytes.fromhex(rec["sent_hex"]))[0][1] == cmd
        assert any("Invalid command %d" % cmd in m for m in rec["dw_messages"]), cmd
        assert rec["dw_copy_decode"] == []
    # the 1100 / 1101 boundary: Dire Wolf's own limit is far above, it takes all four
    for name, total, wire_extra in (("limit-1100-plain", 1100, 0), ("limit-1101-plain", 1101, 0),
                                    ("limit-1100-escapes", 1100, 20), ("limit-1101-escapes", 1101, 20)):
        rec = doc[name]
        frames = ax25.kiss_decode(bytes.fromhex(rec["sent_hex"]))
        assert len(frames) == 1 and len(frames[0][2]) == total, name
        assert len(bytes.fromhex(rec["sent_hex"])) == total + 3 + wire_extra, name
        assert rec["dw_unwrapped_hex"] == [frames[0][2].hex()] and rec["dw_copy_equals_sent"] is True, name
    # FESC directly before the closing FEND: the frame is taken without the FESC, the next frame is noise
    rec = doc["fesc-before-fend"]
    assert len(rec["dw_unwrapped_hex"]) == 1 and len(rec["dw_unwrapped_hex"][0]) // 2 == 20
