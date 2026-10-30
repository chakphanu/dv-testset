# SPDX-License-Identifier: MIT
"""DMRplus dongle builders and parsers (dvtest/proto/dmrplus.py) used by
cases/proto/test_dmrplus.py."""
import socket
import threading

import pytest

from dvtest.proto import dmr, dmrplus

FRAMES = [bytes([i]) * 9 for i in range(1, 37)]


def test_connect_and_disconnect_layout():
    pkt = dmrplus.connect_packet(1023007, 4008)
    assert len(pkt) == 31 and pkt[:8] == b"1023007\x00" and pkt[8:12] == b"4008" and not any(pkt[12:])
    off = dmrplus.disconnect_packet(1023007, "H")
    assert len(off) == 32 and off[:8] == b"1023007\x00" and off[11] == ord("7")
    assert dmrplus.reflector_of("A") == 4001 and dmrplus.reflector_of("Z") == 4026
    assert dmrplus.ACK == b"ACK OK\n\x00" and dmrplus.NAK == b"NAK OK\n\x00"


def test_packet_round_trip_and_layouts():
    burst = bytes(range(33))
    pkt = dmrplus.packet(dmrplus.VOICE, 7, burst, 1023007, 4008, 0x99)
    assert len(pkt) == 72 and pkt[16:18] == b"\x22\x22" and pkt[20:22] == b"\x11\x11" and pkt[62] == 1
    assert pkt[58] == 0 and pkt[59] == 32                       # pad first, byte 32 at 59
    fields = dmrplus.parse(pkt)
    assert fields["burst"] == burst and fields["seq"] == 7 and fields["type"] == dmrplus.VOICE
    assert (fields["src"], fields["dst"], fields["slot_type"]) == (1023007, 4008, 0x99)
    assert dmrplus.burst_id(fields["slot_type"]) == 2
    short = dmrplus.packet(dmrplus.VOICE, 7, burst, 1, 2, 0x77, layout="pair33")
    assert short[58] == 32 and dmrplus.parse(short)["burst33"] == burst
    assert dmrplus.parse(short)["burst"] != burst
    assert dmrplus.parse(pkt[:71]) is None
    assert dmrplus.swap16(b"\x01\x02\x03\x04") == b"\x02\x01\x04\x03"
    with pytest.raises(ValueError):
        dmrplus.packet(1, 0, bytes(32), 1, 2, 0x77)


def test_stream_shape_sync_and_embedded_lc():
    pkts = [dmrplus.parse(p) for p in dmrplus.stream(FRAMES, 1023007, 4008, seq0=4)]
    assert [p["type"] for p in pkts] == [2] + [1] * 12 + [3]
    assert [p["seq"] for p in pkts] == list(range(4, 18))
    voice = [p for p in pkts if p["type"] == 1]
    assert [dmrplus.burst_id(p["slot_type"]) for p in voice] == [0, 1, 2, 3, 4, 5] * 2
    assert dmrplus.centre(voice[0]["burst"]) == dmrplus.BS_VOICE
    sf = dmr.superframe_lc([p["burst"] for p in voice[1:5]])
    assert sf["checksum_ok"] and sf["fec_ok"] and sf["lc"]["dst"] == 4008 and sf["lc"]["src"] == 1023007
    assert all(e["valid"] and e["cc"] == 1 for e in sf["emb"])
    assert dmrplus.frames_of(pkts) == FRAMES
    assert dmrplus.centre(pkts[0]["burst"]) == dmrplus.BS_DATA


def _bptc_place(info12, cc=1, data_type=1, sync=dmrplus.BS_DATA):
    """Test-side inverse of bptc196_info / slot_type: info bits into their
    matrix positions, interleaved, with the slot type and sync around
    (parity left zero: the decoder does not check it)."""
    info = [(int.from_bytes(info12, "big") >> (95 - i)) & 1 for i in range(96)]
    deint = [0] * 196
    positions = list(range(4, 12)) + [p for s in range(16, 136, 15) for p in range(s, s + 11)]
    for pos, bit in zip(positions, info):
        deint[pos] = bit
    raw = [0] * 196
    for i in range(196):
        raw[(i * 181) % 196] = deint[i]
    slot = [(((cc << 4) | data_type) >> (7 - i)) & 1 for i in range(8)] + [0] * 12
    sync_bits = [(int.from_bytes(sync, "big") >> (47 - i)) & 1 for i in range(48)]
    bits = raw[:98] + slot[:10] + sync_bits + slot[10:] + raw[98:]
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value.to_bytes(33, "big")


def test_lc_header_extraction():
    lc = dmr.full_lc_group(1023007, 9)
    burst = _bptc_place(lc + b"\xaa\xbb\xcc")
    got = dmrplus.lc_header(burst)
    assert got["sync"] == dmrplus.BS_DATA and got["cc"] == 1 and got["data_type"] == 1
    assert got["lc"]["flco"] == 0 and got["lc"]["dst"] == 9 and got["lc"]["src"] == 1023007
    assert got["info"][9:] == b"\xaa\xbb\xcc"


def test_client_link_ack_and_keepalive():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    seen = []

    def serve():
        for _ in range(2):
            try:
                data, addr = server.recvfrom(128)
            except socket.timeout:
                return
            seen.append(data)
            server.sendto(dmrplus.ACK, addr)

    thread = threading.Thread(target=serve)
    thread.start()
    client = dmrplus.DmrPlusClient("127.0.0.1", 1023008, "H")
    try:
        assert client.link("127.0.0.1", port=server.getsockname()[1]) == [dmrplus.ACK]
        client.pump(1.1)
        thread.join()
        assert seen[0] == dmrplus.connect_packet(1023008, 4008) and len(seen) >= 2
    finally:
        client.close()
        server.close()
