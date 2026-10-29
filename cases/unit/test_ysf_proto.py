# SPDX-License-Identifier: MIT
"""Round trips for the YSF reflector protocol as xlxd speaks it
(dvtest/proto/ysf.py): FICH, V/D mode 2 VCH / DCH, header CSD, packets,
WiresX disconnect and a loopback client."""
import random
import threading

from dvtest.net import UdpClient
from dvtest.proto import nxdn, ysf


def random_frame(rng):
    """Random 9-byte DMR-form AMBE+2 frame (a valid codeword)."""
    return nxdn.to_dmr(nxdn.join(rng.getrandbits(12), rng.getrandbits(12), rng.getrandbits(25)))


def flip(data, pos):
    out = bytearray(data)
    out[pos >> 3] ^= 0x80 >> (pos & 7)
    return bytes(out)


def random_fields(rng):
    return {"fi": rng.randrange(4), "cs": rng.randrange(4), "cm": rng.randrange(4),
            "bn": rng.randrange(4), "bt": rng.randrange(4), "fn": rng.randrange(8),
            "ft": rng.randrange(8), "dev": rng.random() < 0.5, "mr": rng.randrange(8),
            "voip": rng.random() < 0.5, "dt": rng.randrange(4), "sql": rng.random() < 0.5,
            "sq": rng.randrange(128)}


def test_poll_ack_unlink_status():
    poll = ysf.poll_packet("n0call")
    assert poll == b"YSFPN0CALL    " and len(poll) == ysf.POLL_SIZE
    assert ysf.parse_poll(poll) == {"kind": "poll", "callsign": "N0CALL"}
    assert ysf.unlink_packet("N0CALL") == b"YSFUN0CALL    "
    assert ysf.parse_poll(ysf.unlink_packet("N0CALL"))["kind"] == "unlink"
    assert ysf.is_poll_ack(b"YSFPREFLECTOR ") and not ysf.is_poll_ack(poll)
    assert ysf.parse_poll(poll[:13]) is None
    assert ysf.status_request() == b"YSFS"
    reply = b"YSFS" + b"01234" + b"XLX999".ljust(16) + b"XLX reflector " + b"007"
    assert ysf.parse_status(reply) == {"hash": "01234", "name": "XLX999",
                                       "description": "XLX reflector", "count": 7}
    assert ysf.parse_status(reply[:-1]) is None


def test_whitening_crc_and_golay():
    assert len(ysf.WHITENING) == 20 and ysf.WHITENING[:4] == bytes.fromhex("93d75121")
    assert ysf.crc16(b"123456789") == 0xCE3C          # CRC-16/GSM check value
    rng = random.Random(2)
    for _ in range(300):
        data = rng.getrandbits(12)
        word = nxdn.golay24(data)
        errors = 0
        for pos in rng.sample(range(1, 24), rng.randrange(4)):
            errors |= 1 << pos
        assert ysf.golay24_decode(word ^ errors) == data
        assert ysf.golay24_decode(word ^ 1) == data   # parity bit ignored


def test_fich_round_trip():
    rng = random.Random(3)
    cases = [random_fields(rng) for _ in range(300)]
    cases += [dict(random_fields(rng), fi=fi, dt=dt, sq=sq)
              for fi in range(4) for dt in range(4) for sq in (0, 10, 35, 127)]
    for fields in cases:
        coded = ysf.fich_encode(**fields)
        assert len(coded) == ysf.FICH_SIZE
        assert ysf.fich_decode(coded) == fields


def test_fich_bit_errors():
    rng = random.Random(4)
    for _ in range(4):
        fields = random_fields(rng)
        coded = ysf.fich_encode(**fields)
        for pos in range(8 * ysf.FICH_SIZE):           # every single flip is corrected
            assert ysf.fich_decode(flip(coded, pos)) == fields
        two = flip(flip(coded, rng.randrange(200)), rng.randrange(200))
        assert ysf.fich_decode(two) in (fields, None)
    junk = [bytes(rng.getrandbits(8) for _ in range(25)) for _ in range(40)]
    assert all(ysf.fich_decode(data) is None for data in junk)
    assert ysf.fich_decode(bytes(24)) is None


def test_known_answer_all_zero():
    """All-zero inputs. The expected bytes were produced by this module and
    cross-checked once against xlxd's CYSFFICH::encode (field bytes 00 00
    00 00) and CYsfUtils::EncodeVD2Vch (the 9-byte DMR form of the
    all-zero 49-bit frame) compiled in a scratch harness outside the repo."""
    fich = ysf.fich_encode(0, cs=0, cm=0, mr=0, dt=0)
    assert fich.hex() == "00000045aa000003e6a9000002e6a8000003f6a90000013eab"
    vch = ysf.vch_encode(nxdn.to_dmr(bytes(7)))
    assert vch.hex() == "f31937da8c4c3bb96b7f550c63"


def test_vch_round_trip():
    rng = random.Random(5)
    for _ in range(500):
        frame9 = random_frame(rng)
        vch = ysf.vch_encode(frame9)
        assert len(vch) == 13 and ysf.vch_decode(vch) == frame9
    assert ysf.vch_decode(ysf.vch_encode(ysf.SILENCE)) == ysf.SILENCE
    # a frame that is not a DMR codeword loses its FEC, keeps its 49 bits
    raw = bytes(range(1, 10))
    assert nxdn.from_dmr(ysf.vch_decode(ysf.vch_encode(raw))) == nxdn.from_dmr(raw)


def test_dch_and_csd_round_trip():
    rng = random.Random(6)
    for _ in range(30):
        text = bytes(rng.getrandbits(8) for _ in range(10))
        coded = ysf.dch_encode(text)
        assert len(coded) == 25 and ysf.dch_decode(coded) == text
        assert ysf.dch_decode(flip(coded, rng.randrange(200))) == text
        data = bytes(rng.getrandbits(8) for _ in range(20))
        coded = ysf.csd_encode(data)
        assert len(coded) == 45 and ysf.csd_decode(coded) == data
        assert ysf.csd_decode(flip(coded, rng.randrange(360))) == data
    assert ysf.dch_decode(bytes(rng.getrandbits(8) for _ in range(25))) is None
    assert ysf.csd_decode(bytes(rng.getrandbits(8) for _ in range(45))) is None
    csd1, csd2 = ysf.csd("N0CALL", downlink="N0GW")
    assert csd1 == b"**********N0CALL    " and csd2 == b"N0GW" + b" " * 16
    payload = ysf.fr_payload(csd1, csd2)
    assert ysf.fr_fields(payload) == (csd1, csd2)
    frames = [random_frame(rng) for _ in range(5)]
    payload = ysf.vd2_payload(frames, b"N0CALL    ")
    assert len(payload) == ysf.PAYLOAD_SIZE
    assert ysf.vd2_frames(payload) == frames and ysf.vd2_dch(payload) == b"N0CALL    "


def test_packets_header_voice_terminator():
    rng = random.Random(7)
    frames = [random_frame(rng) for _ in range(12)]
    packets = ysf.stream_packets(frames, "N0CALL", dgid=ysf.module_dgid("C"))
    assert all(len(p) == ysf.PACKET_SIZE for p in packets)
    infos = [ysf.parse(p) for p in packets]
    assert [i["kind"] for i in infos] == ["header", "voice", "voice", "voice", "terminator"]
    assert [i["counter"] for i in infos] == [0, 1, 2, 3, 4]
    assert [i["eot"] for i in infos] == [False] * 4 + [True]
    assert all(i["sync_ok"] and i["fich"]["dt"] == ysf.DT_VD_MODE2 for i in infos)
    assert all(i["fich"]["sq"] == 12 and ysf.dgid_module(i["fich"]["sq"]) == "C" for i in infos)
    assert [i["fich"]["fn"] for i in infos] == [0, 0, 1, 2, 0]
    head = infos[0]
    assert (head["gateway"], head["src"], head["dst"]) == ("N0CALL", "N0CALL", "ALL")
    assert head["csd1"] == b"**********N0CALL    " and head["csd2"][:10] == b"N0CALL    "
    assert infos[-1]["csd1"] == head["csd1"]
    assert [i["dch"] for i in infos[1:4]] == [b"**********", b"N0CALL    ", b"N0CALL    "]
    got = [f for i in infos if i["kind"] == "voice" for f in i["frames"]]
    assert got == ysf.wire_frames(frames) == frames + [ysf.SILENCE] * 3
    assert packets[0][34] == 0 and packets[-1][34] == (4 << 1) | 1
    assert ysf.is_voice(packets[1]) and not ysf.is_voice(packets[0])
    assert ysf.parse(packets[1][:-1]) is None
    broken = packets[1][:40] + bytes(25) + packets[1][65:]
    assert ysf.parse(broken)["kind"] is None and ysf.parse(broken)["fich"] is None
    custom = ysf.packet("GW", "SRC", "DST", 5, ysf.voice_frame(3, frames[:5], b"0123456789"))
    info = ysf.parse(custom)
    assert (info["gateway"], info["src"], info["dst"], info["counter"]) == ("GW", "SRC", "DST", 5)
    assert info["frames"] == frames[:5] and info["dch"] == b"0123456789"
    assert ysf.module_dgid("A") == 10 and ysf.module_dgid("z") == 35
    assert ysf.dgid_module(9) is None and ysf.dgid_module(36) is None


def test_wiresx_disconnect():
    command = ysf.wiresx_command(ysf.WIRESX_DISC_REQ)
    assert len(command) == 20 and command[:6] == b"\x00\x5d\x2a\x5f\x03\xe9"
    assert ysf.parse_wiresx_command(command) == (ysf.WIRESX_DISC_REQ, command[:5])
    assert ysf.parse_wiresx_command(command[:5] + b"\x00" + command[6:]) is None
    packets = ysf.wiresx_disc_packets("N0CALL")
    infos = [ysf.parse(p) for p in packets]
    assert all(i["kind"] == "data" and i["fich"]["dt"] == ysf.DT_DATA_FR_MODE for i in infos)
    assert [(i["fich"]["fi"], i["fich"]["fn"], i["fich"]["ft"]) for i in infos] == \
        [(0, 0, 1), (1, 0, 1), (1, 1, 1), (2, 1, 1)]
    assert infos[2]["gateway"] == "N0CALL" and infos[2]["data2"] == command
    assert [i["eot"] for i in infos] == [False, False, False, True]


def test_client_link_talk_collect_unlink():
    """Loopback: a fake reflector acks the poll; the talk is relayed back."""
    reflector = UdpClient("127.0.0.1")
    talker = ysf.YsfClient("127.0.0.1", "N0CALL", module="D")
    rng = random.Random(8)
    frames = [random_frame(rng) for _ in range(7)]
    try:
        reflector.responder = lambda data, _addr: ysf.ACK if data.startswith(b"YSFP") else None
        port = reflector.local[1]
        assert talker.dest is None
        pump = threading.Thread(target=reflector.pump, args=(1.0,))
        pump.start()
        assert talker.link("127.0.0.1", timeout=1.0, port=port, join_gap=0.05)
        pump.join()
        joined = [ysf.parse(d) for d in reflector.inbox if ysf.parse(d)]
        assert [i["kind"] for i in joined] == ["header", "terminator"]
        assert joined[0]["fich"]["sq"] == 13               # DG-ID of module D
        reflector.inbox.clear()
        talker.client.inbox.clear()
        talker.talk(frames, stream_id=0x1234, settle=0.05, tail=0.1, gap=0.001)
        reflector.pump(0.1)
        sent = [d for d in reflector.inbox if ysf.parse(d)]
        assert [ysf.parse(d)["kind"] for d in sent] == ["header", "voice", "voice", "terminator"]
        assert reflector.inbox[0] == ysf.poll_packet("N0CALL")
        for data in sent:                                  # relay back
            reflector.send(talker.client.local, data)
        assert talker.collect(settle=0.1) == frames + [ysf.SILENCE] * 3
        reflector.inbox.clear()
        talker.unlink(wait=0.05)
        reflector.pump(0.1)
        assert [ysf.parse(d)["kind"] for d in reflector.inbox[:4]] == ["data"] * 4
        assert reflector.inbox[4] == ysf.unlink_packet("N0CALL")
    finally:
        talker.close()
        reflector.close()
