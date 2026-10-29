# SPDX-License-Identifier: MIT
"""Round trips for the DExtra, DMR, M17 and USRP codecs, plus UdpClient."""
import socket

from dvtest.net import UdpClient
from dvtest.proto import dextra, dmr, m17, usrp


def test_dextra_link_and_keepalive():
    assert dextra.link_packet("N0TEST", "B") == b"N0TEST  BB\x00"
    assert dextra.link_packet("N0TEST", "B", own_module="C") == b"N0TEST  CB\x00"
    assert dextra.unlink_packet("N0TEST", "B") == b"N0TEST  B \x00"
    assert dextra.keepalive_packet("N0TEST") == b"N0TEST  \x00"
    assert dextra.is_reflector_keepalive(b"XRF999 \x20\x00")
    assert not dextra.is_reflector_keepalive(b"N0TEST  \x00")


def test_dextra_header_and_voice():
    head = dextra.header(0x1234, "N0TEST", "B")
    assert len(head) == dextra.HEADER_SIZE
    fields = dextra.parse_header(head)
    assert fields == {"stream_id": 0x1234, "rpt2": b"XLX999 B", "rpt1": b"N0TEST B",
                      "ur": b"CQCQCQ  ", "my": b"N0TEST  "}
    frame = bytes(range(9))
    for seq, last in ((0, False), (22, False), (5, True)):
        data = dextra.voice(0x1234, seq, frame, last)
        assert len(data) == dextra.VOICE_SIZE
        assert dextra.parse_voice(data) == (0x1234, seq % 21, last, frame)
    assert dextra.parse_voice(head) is None and dextra.parse_header(data) is None


def test_dmr_payload_round_trip():
    frames = [bytes([i] * 9) for i in (0xA5, 0x5A, 0xC3)]
    payload = dmr.voice_payload(*frames)
    assert len(payload) == 33 and payload[14:19] == bytes(5)
    assert dmr.extract(payload) == frames
    sync = dmr.data_sync_payload()
    assert sync[13] == 0x0D and sync[14:19] == bytes(dmr.DATA_SYNC) and sync[19] == 0xD0


def test_dmr_packet_round_trip():
    data = dmr.packet(300, 1023007, 4002, 1023007, 0x81, 0x22222222, dmr.data_sync_payload())
    assert len(data) == dmr.PACKET_SIZE
    fields = dmr.parse(data)
    assert (fields["seq"], fields["src"], fields["dst"], fields["bitfield"], fields["stream"]) == \
        (300 & 0xFF, 1023007, 4002, 0x81, 0x22222222)
    assert dmr.module_tg("A") == 4001 and dmr.module_tg("Z") == 4026
    assert dmr.is_voice(data)
    assert not dmr.is_voice(dmr.packet(0, 1, 4001, 1, 0xA1, 1, dmr.data_sync_payload()))


def test_dmr_voice_packets():
    frames = [bytes([i] * 9) for i in range(7)]
    packets, next_seq = dmr.voice_packets(frames, 1023007, 4002, 7)
    assert len(packets) == 3 and next_seq == 4
    out = []
    for data in packets:
        out.extend(dmr.extract(dmr.parse(data)["payload"]))
    assert out[:7] == frames and out[7:] == [bytes(9)] * 2


def test_m17_callsign_and_packet():
    assert len(m17.encode_callsign("N0CALL")) == 6
    assert m17.decode_callsign(m17.encode_callsign("N0CALL B")) == "N0CALL B"
    data = m17.packet(0xBEEF, 3, "N0CALL", bytes(range(16)), eot=True)
    assert len(data) == m17.PACKET_SIZE
    fields = m17.parse(data)
    assert (fields["stream_id"], fields["seq"], fields["eot"], fields["src"]) == (0xBEEF, 3, True, "N0CALL")
    assert fields["frames"] == [bytes(range(8)), bytes(range(8, 16))]
    assert m17.conn_packet("N0CALL", "B")[-1:] == b"B" and len(m17.conn_packet("N0CALL", "B")) == 11


def test_m17_voice_packets():
    frames = [bytes([i] * 8) for i in range(5)]
    packets = m17.voice_packets(frames, 1, "N0CALL")
    assert len(packets) == 1 + 3 + 1
    parsed = [m17.parse(p) for p in packets]
    assert [p["seq"] for p in parsed] == [0, 1, 2, 3, 4] and parsed[-1]["eot"]
    assert parsed[3]["frames"] == [frames[4], frames[4]]


def test_usrp_round_trip():
    pcm = bytes(range(256)) + bytes(64)
    data = usrp.voice(7, pcm)
    assert len(data) == usrp.VOICE_SIZE and usrp.is_voice(data)
    f = usrp.parse(data)
    assert (f.seq, f.keyup, f.type, f.payload) == (7, 1, 0, pcm)
    off = usrp.unkey(8)
    assert len(off) == usrp.HEADER_SIZE and usrp.is_voice(off) and usrp.parse(off).keyup == 0
    tlv = usrp.frame(0, usrp.TYPE_TEXT, b"\x08\x00")
    assert usrp.parse(tlv).type == 2 and not usrp.is_voice(tlv)
    assert not usrp.is_voice(usrp.header(1, 1))          # keyed without PCM
    assert not usrp.is_voice(usrp.header(1, 0) + pcm)    # unkeyed with PCM


def test_udp_client_responder_and_pump():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.settimeout(1.0)
    client = UdpClient("127.0.0.1", responder=lambda data, _a: b"pong" if data == b"ping" else None)
    try:
        peer.sendto(b"ping", client.local)
        client.pump(0.1)
        assert client.inbox == [b"ping"] and peer.recv(16) == b"pong"
        stamps = client.paced(peer.getsockname(), [b"a", b"b", b"c"], 0.02)
        assert [peer.recv(16) for _ in range(3)] == [b"a", b"b", b"c"]
        assert stamps[2] - stamps[0] >= 0.035
    finally:
        client.close()
        peer.close()
