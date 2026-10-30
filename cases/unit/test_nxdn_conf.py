# SPDX-License-Identifier: MIT
"""Round trips for the NXDN conformance builders (dvtest/proto/nxdn.py):
NXDN TS 1-A test vectors, layer 3 fields, stolen halves, data frames,
stream options and the tester-as-reflector (FakeReflector)."""
import random

from dvtest.net import UdpClient
from dvtest.proto import nxdn


def frame(rng):
    return nxdn.join(rng.getrandbits(12), rng.getrandbits(12), rng.getrandbits(25))


def test_spec_vectors_round_trip():
    # TS 1-A 7.2.1/1 (tone) and 7.2.1/2 (silence): 49-bit and 72-bit forms
    for dmr9, bits49 in ((nxdn.TONE_DMR, "FEE2121212100"), (nxdn.SILENCE_DMR, "F801A99F8CE08")):
        assert nxdn.dmr_valid(dmr9)
        assert nxdn.from_dmr(dmr9).hex().upper().startswith(bits49)
        assert nxdn.to_dmr(nxdn.from_dmr(dmr9)) == dmr9


def test_layer3_fields():
    l3 = nxdn.layer3(nxdn.VCALL, 1001, 31672, call_type=nxdn.CALL_INDIVIDUAL, cipher=1, key=5)
    info = nxdn.parse_layer3(l3)
    assert info == {"msg": 1, "cc_option": 0, "call_type": 4, "src": 1001, "dst": 31672, "cipher": 1, "key": 5}
    # default unchanged: group call, no cipher
    assert nxdn.layer3(nxdn.VCALL, 1, 2)[2] == 0x20 and nxdn.layer3(nxdn.VCALL, 1, 2)[7] == 0
    # TX_REL carries no octet 7
    assert nxdn.layer3(nxdn.TX_REL, 1, 2, cipher=3)[7] == 0


def test_header_with_idle_sacch():
    head = nxdn.control_payload(nxdn.VCALL, 1001, 31672, nxdn.SACCH_IDLE)
    sacch = nxdn.parse_sacch(head[1:5])
    assert sacch["structure"] == 0 and sacch["crc_ok"] and sacch["data"] >> 12 == nxdn.IDLE >> 2
    assert (sacch["data"] >> 10) & 0x3F == nxdn.IDLE
    assert nxdn.flags_for(head) == nxdn.GROUP | nxdn.HEADER


def test_steal_payload():
    rng = random.Random(1)
    pair = [frame(rng), frame(rng)]
    for half, lich in ((1, 0xA4), (2, 0xA8)):
        payload = nxdn.steal_payload(pair, 1001, 31672, 0, half=half)
        assert payload[0] == lich and nxdn.parse_lich(lich)["parity_ok"]
        assert nxdn.flags_for(payload) == nxdn.GROUP
        frames = nxdn.voice_frames(payload)
        assert (frames[2:] if half == 1 else frames[:2]) == pair
        block = payload[5:17] if half == 1 else payload[19:31]
        assert nxdn.parse_facch1(block)["crc_ok"] and nxdn.parse_facch1(block)["msg"] == nxdn.VCALL


def test_data_payload_flags():
    payload = nxdn.data_payload(b"\x01\x02")
    assert payload[0] == 0x90 and nxdn.parse_lich(0x90)["parity_ok"]
    assert nxdn.flags_for(payload) == nxdn.GROUP | nxdn.DATA
    assert nxdn.parse(nxdn.packet(1, 2, payload))["kind"] == "data"


def test_stream_options():
    rng = random.Random(2)
    frames = [frame(rng) for _ in range(8)]
    kinds = [nxdn.parse(p)["kind"] for p in nxdn.stream_packets(frames, 1, 2, header=False)]
    assert kinds == ["voice", "voice", "trailer"]
    private = nxdn.stream_packets(frames, 1, 2, group=False)
    assert all(not nxdn.parse(p)["group"] for p in private)
    assert nxdn.parse_layer3(nxdn.parse(private[0])["payload"][5:15])["call_type"] == nxdn.CALL_INDIVIDUAL
    enc = nxdn.stream_packets(frames, 1, 2, cipher=1)
    assert nxdn.parse_layer3(nxdn.parse(enc[0])["payload"][5:15])["cipher"] == 1
    # the cipher also rides in the voice SACCH superframe (octet 7 in part 4/4)
    parts = [nxdn.parse_sacch(nxdn.parse(p)["payload"][1:5])["data"] for p in enc[1:3]]
    assert parts[0] == nxdn.vcall_sacch(1, 2, 0, cipher=1)


def test_set_gain_index():
    rng = random.Random(3)
    f = frame(rng)
    for b2 in (0, 7, 31):
        g = nxdn.set_gain_index(f, b2)
        assert nxdn.gain_index(g) == b2 and nxdn.without_gain(g) == nxdn.without_gain(f)


def test_fake_reflector_loopback():
    refl = nxdn.FakeReflector("127.0.0.1", 0, 31673)
    peer = UdpClient("127.0.0.1")
    try:
        dest = refl.client.local
        peer.send(dest, nxdn.poll_packet("XLX999", 31673))
        assert refl.wait_poll(1.0)
        peer.pump(0.1)
        assert nxdn.poll_packet("XLX999", 31673) in peer.inbox        # echoed
        peer.inbox.clear()
        peer.send(dest, nxdn.poll_packet("XLX999", 1))                 # wrong TG: no echo
        refl.pump(0.1)
        peer.pump(0.1)
        assert not peer.inbox and refl.polls[-1][2]["tg"] == 1
        refl.echo = False
        peer.send(dest, nxdn.poll_packet("XLX999", 31673))
        refl.pump(0.1)
        peer.pump(0.1)
        assert not peer.inbox
        peer.send(dest, nxdn.unlink_packet("XLX999", 31673))
        refl.pump(0.1)
        assert refl.polls[-1][2]["kind"] == "unlink"
        refl.send(nxdn.packet(1, 31673, nxdn.control_payload(nxdn.VCALL, 1, 31673)))
        peer.pump(0.1)
        assert nxdn.parse(peer.inbox[-1])["kind"] == "header"
    finally:
        refl.close()
        peer.close()
