# SPDX-License-Identifier: MIT
"""Round trips for the NXDNReflector protocol and the 49-bit <-> DMR
AMBE+2 frame conversion (dvtest/proto/nxdn.py)."""
import random

from dvtest.net import UdpClient
from dvtest.proto import nxdn


def random_frame(rng):
    return nxdn.join(rng.getrandbits(12), rng.getrandbits(12), rng.getrandbits(25))


def test_poll_and_unlink():
    poll = nxdn.poll_packet("N0CALL", 31672)
    assert poll == b"NXDNPN0CALL    \x7b\xb8" and len(poll) == nxdn.POLL_SIZE
    assert nxdn.parse_poll(poll) == {"kind": "poll", "callsign": "N0CALL", "tg": 31672}
    unlink = nxdn.unlink_packet("n0test", 1)
    assert unlink == b"NXDNUN0TEST    \x00\x01"
    assert nxdn.parse_poll(unlink)["kind"] == "unlink"
    assert nxdn.parse_poll(poll[:16]) is None and nxdn.parse(poll) is None


def test_golay_and_prng():
    # generator polynomial and its extended form (weight 8)
    assert nxdn.golay23(1) == 0xC75 and nxdn.golay24(1) == 0x18EB
    rng = random.Random(1)
    for _ in range(200):
        a, b = rng.getrandbits(12), rng.getrandbits(12)
        if a != b:
            # minimum distance 8 of the extended code
            assert bin(nxdn.golay24(a) ^ nxdn.golay24(b)).count("1") >= 8
    # x0 = 0: the LCG walk from 0 gives this mask
    assert nxdn.prng_mask(0) == 0x216623
    assert all(nxdn.prng_mask(a) >> 23 == 0 for a in range(0, 4096, 97))


def test_dmr_interleave_covers_72_bits():
    assert sorted(nxdn.DMR_A + nxdn.DMR_B + nxdn.DMR_C) == list(range(72))
    assert nxdn.DMR_A[:3] == [0, 4, 8] and nxdn.DMR_B[0] == 25 and nxdn.DMR_C[-1] == 71


def test_frame_conversion_round_trip():
    rng = random.Random(2)
    for _ in range(300):
        frame = random_frame(rng)
        assert frame[6] & 0x7F == 0
        dmr9 = nxdn.to_dmr(frame)
        assert len(dmr9) == 9 and nxdn.dmr_valid(dmr9)
        assert nxdn.from_dmr(dmr9) == frame
        assert nxdn.split(frame) == nxdn.split(nxdn.join(*nxdn.split(frame)))
    # a flipped parity bit is not a codeword, and from_dmr does not correct
    broken = bytearray(nxdn.to_dmr(random_frame(rng)))
    broken[0] ^= 0x40                                     # bit 1: a parity
    assert not nxdn.dmr_valid(bytes(broken))


def test_gain_bits():
    frame = nxdn.join(0x00F, 0, 1 << 12)                  # b2 = 0b11111
    assert nxdn.gain_index(frame) == 31
    assert nxdn.without_gain(frame) == bytes(7)
    assert nxdn.gain_index(nxdn.join(0x008, 0, 0)) == 16


def test_lich_and_crc():
    assert nxdn.lich(nxdn.RFCT_RDCH, nxdn.FCT_SACCH_NS, nxdn.OPTION_STEAL_FACCH) == 0x81
    assert nxdn.lich(nxdn.RFCT_RDCH, nxdn.FCT_SACCH_SS, nxdn.OPTION_STEAL_NONE) == 0xAC
    assert nxdn.parse_lich(0xAC) == {"rfct": 2, "fct": 2, "option": 3, "outbound": False, "parity_ok": True}
    assert not nxdn.parse_lich(0xAD)["parity_ok"]
    assert nxdn.sacch(0) == bytes([0, 0, 0, 0x2B])
    assert nxdn.parse_sacch(nxdn.sacch(2, 0x2ABCD, ran=5)) == {
        "structure": 2, "ran": 5, "data": 0x2ABCD, "crc_ok": True}


def test_control_payloads():
    head = nxdn.control_payload(nxdn.VCALL, 1001, 31672)
    assert head.hex() == "810000002b01002003e97bb80000003250000001002003e97bb800000032500000"
    assert nxdn.flags_for(head) == nxdn.GROUP | nxdn.HEADER
    info = nxdn.parse_facch1(head[5:17])
    assert info == {"msg": nxdn.VCALL, "group": True, "src": 1001, "dst": 31672, "crc_ok": True}
    assert head[19:31] == head[5:17]
    tail = nxdn.control_payload(nxdn.TX_REL, 1001, 31672)
    assert nxdn.flags_for(tail) == nxdn.GROUP | nxdn.TRAILER
    assert nxdn.parse_facch1(tail[5:17])["msg"] == nxdn.TX_REL


def test_voice_payload_round_trip():
    rng = random.Random(3)
    frames = [random_frame(rng) for _ in range(4)]
    for index in range(5):
        payload = nxdn.voice_payload(frames, 1001, 31672, index)
        assert payload[0] == 0xAC and nxdn.flags_for(payload) == nxdn.GROUP
        assert nxdn.voice_frames(payload) == frames
        sacch = nxdn.parse_sacch(payload[1:5])
        assert sacch["structure"] == 3 - index % 4 and sacch["crc_ok"]
    # the four SACCH parts rebuild the VCALL message (first 9 bytes)
    parts = [nxdn.parse_sacch(nxdn.voice_payload(frames, 1001, 31672, i)[1:5])["data"] for i in range(4)]
    message = 0
    for part in parts:
        message = (message << 18) | part
    assert message.to_bytes(9, "big") == nxdn.layer3(nxdn.VCALL, 1001, 31672)[:9]


def test_packet_round_trip_and_stream():
    rng = random.Random(4)
    frames = [random_frame(rng) for _ in range(10)]
    packets = nxdn.stream_packets(frames, 1001, 31672)
    kinds = [nxdn.parse(p)["kind"] for p in packets]
    assert kinds == ["header", "voice", "voice", "voice", "trailer"]
    assert all(len(p) == nxdn.PACKET_SIZE for p in packets)
    first = nxdn.parse(packets[1])
    assert (first["src"], first["dst"], first["group"], first["flags"]) == (1001, 31672, True, 0x01)
    assert nxdn.parse(packets[0])["flags"] == 0x05 and nxdn.parse(packets[-1])["flags"] == 0x09
    got = []
    for data in packets:
        if nxdn.is_voice(data):
            got.extend(nxdn.voice_frames(nxdn.parse(data)["payload"]))
    assert got == nxdn.wire_frames(frames) and len(got) == 12
    assert nxdn.parse(packets[1][:42]) is None


def test_client_link_talk_collect():
    """Loopback: a fake reflector echoes the poll and relays NXDND."""
    reflector = UdpClient("127.0.0.1")
    talker = nxdn.NxdnClient("127.0.0.1", 31672)
    rng = random.Random(5)
    frames = [random_frame(rng) for _ in range(8)]
    try:
        poll = nxdn.poll_packet("N0CALL", 31672)

        def answer(data, _addr):
            return data if data == poll else None

        reflector.responder = answer
        port = reflector.local[1]
        talker.dest = ("127.0.0.1", port)
        talker.client.send(talker.dest, poll)
        reflector.pump(0.1)
        talker.client.pump(0.1)
        assert poll in talker.client.inbox
        talker.client.inbox.clear()
        talker.talk(frames, settle=0.05, tail=0.1, gap=0.001)
        reflector.pump(0.1)
        sent = [d for d in reflector.inbox if nxdn.parse(d)]
        assert [nxdn.parse(d)["kind"] for d in sent] == ["header", "voice", "voice", "trailer"]
        for data in sent:                               # relay back
            reflector.send(talker.client.local, data)
        assert talker.collect(settle=0.1) == frames
    finally:
        talker.close()
        reflector.close()
