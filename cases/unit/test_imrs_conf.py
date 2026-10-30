# SPDX-License-Identifier: MIT
"""Round trips for the IMRS builders and parsers (dvtest/proto/imrs.py)."""
import socket
import time

from dvtest.audio import signals
from dvtest.proto import imrs, nxdn


def dmr_frames(n, seed="imrs-unit"):
    # valid DMR-form codewords: the VCH carries only the 49 data bits
    return [nxdn.to_dmr(nxdn.from_dmr(f)) for f in signals.prng_frames("dmr", n, seed=seed)]


def test_ping_and_info():
    assert len(imrs.PING) == imrs.PING_SIZE and imrs.parse(imrs.PING) == {"kind": "ping"}
    pkt = imrs.info("N0CALL", firmware=0x01020304, dgids=(2, 10, 35), radio_id=b"ABCDE")
    assert len(pkt) == imrs.INFO_SIZE
    got = imrs.parse(pkt)
    assert got["kind"] == "info" and got["callsign"] == "N0CALL" and got["dgids"] == [2, 10, 35]
    assert got["firmware"] == 0x01020304 and got["radio_id"] == b"ABCDE"
    assert imrs.parse_info(pkt[:59]) is None


def test_header_layout():
    pkt = imrs.header(0x1234, "N0CALL", "N0CALL B", imrs.module_dgid("B"))
    assert len(pkt) == imrs.HEADER_SIZE and pkt[0] == 0 and pkt[1] == imrs.HEADER_SIZE - 16
    assert pkt[2:7] == bytes((0, 0, 0, 0, 7)) and pkt[16:19] == b"I**"
    # FICH as xlxd sends a header on module B: 24 06 02 0B
    assert pkt[23:31] == b"2406020B"
    assert pkt[41:51] == b"N0CALL    " and pkt[61:69] == b"N0CALL B"
    got = imrs.parse(pkt)
    assert (got["kind"], got["sid"], got["fid"]) == ("header", 0x1234, 0)
    assert got["fich"]["fi"] == 0 and got["fich"]["dt"] == 2 and got["fich"]["sq"] == 11


def test_voice_round_trip():
    frames = dmr_frames(5)
    pkt = imrs.voice(0x0042, frames, 19, fid=3)
    assert len(pkt) == imrs.VOICE_SIZE and pkt[16:17] == b"2"
    assert pkt[23:31] == b"60060213"                     # FI 1, FN 0, DT 2, DG-ID 19
    got = imrs.parse(pkt)
    assert got["frames"] == frames and got["fid"] == 3 and got["time"] == 300
    assert got["fich"]["fi"] == 1 and got["fich"]["sq"] == 19


def test_terminator_and_stream():
    pkt = imrs.terminator(7, 11, 4)
    assert len(pkt) == imrs.TERMINATOR_SIZE and pkt[2:7] == bytes(5) and pkt[23:31] == b"A00E020B"
    frames = dmr_frames(12)
    packets = imrs.stream_packets(frames, 9, "N0CALL", "N0CALL B", 11)
    kinds = [imrs.parse(p)["kind"] for p in packets]
    assert kinds == ["header", "voice", "voice", "voice", "terminator"]
    got = [f for p in packets if imrs.parse(p)["kind"] == "voice" for f in imrs.parse(p)["frames"]]
    assert got[:12] == frames and got[12:] == [imrs.ysf.SILENCE] * 3
    assert imrs.parse(b"\x00" + bytes(90)) is None


def test_hub_loopback():
    hub = imrs.Hub("127.0.0.1", port=0)
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    try:
        port = hub.sock.getsockname()[1]
        mark = hub.mark()
        for data in imrs.stream_packets(dmr_frames(5), 1, "N0CALL", "N0CALL B", 11):
            peer.sendto(data, ("127.0.0.1", port))
        deadline = time.monotonic() + 1
        while len(hub.packets(mark, "127.0.0.1")) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [p["kind"] for p in hub.packets(mark, "127.0.0.1")] == ["header", "voice", "terminator"]
    finally:
        hub.close()
        peer.close()
