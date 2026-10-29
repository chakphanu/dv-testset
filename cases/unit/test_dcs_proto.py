# SPDX-License-Identifier: MIT
"""Round trips for the DCS codec (dvtest/proto/dcs.py) and the client's
link and keepalive against a loopback fake reflector."""
import socket
import threading

from dvtest.proto import dcs, dextra


def test_link_unlink_keepalive():
    link = dcs.link_packet("N0TEST", "D")
    assert len(link) == dcs.LINK_SIZE
    assert link[:11] == b"N0TEST DDD\x00" and link[11:].rstrip(b"\x00") == dcs.TEXT
    assert dcs.link_packet("N0TEST", "D", own_module="B")[:10] == b"N0TEST BBD"
    assert dcs.unlink_packet("N0TEST", "B") == b"N0TEST BB \x00"
    assert dcs.keepalive_packet("N0TEST", "B", "XLX999", "D") == b"N0TEST B\x00XLX999 D"
    assert dcs.parse_ack(b"N0TEST BBDACK\x00") == "ACK"
    assert dcs.parse_ack(b"N0TEST B  NAK\x00") == "NAK"
    assert dcs.parse_ack(b"N0TEST BBDACK") is None
    alive = b"DCS999 D N0TEST BB\x0a\x00\x20\x20"
    assert dcs.is_reflector_keepalive(alive)
    assert not dcs.is_reflector_keepalive(b"DCS999  ")


def test_voice_round_trip():
    frame = bytes(range(20, 29))
    head = dextra.parse_header(dextra.header(0x4321, "N0TEST", "D"))
    for seq, last in ((0, False), (21, False), (60, True)):
        data = dcs.voice(0x4321, seq, frame, last, 0x10203 + seq, "N0TEST", "D")
        assert len(data) == dcs.PACKET_SIZE and data[:4] == b"0001"
        assert data[61] == 0x01 and data[62:] == bytes(38)
        info = dcs.parse(data)
        assert info == {"rpt2": head["rpt2"], "rpt1": head["rpt1"], "ur": head["ur"],
                        "my": head["my"], "stream_id": 0x4321, "seq": seq % 21, "last": last,
                        "frame": frame, "slow": dextra.SLOW_DATA, "counter": 0x10203 + seq}
    assert dcs.parse(data[:99]) is None and dcs.parse(b"0002" + data[4:]) is None


class FakeDcs:
    """ACKs a link, sends one per-client keepalive, records the rest."""

    def __init__(self, reply=b"ACK"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.reply = reply
        self.got = []
        self.stop = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            self.got.append(data)
            if len(data) == dcs.LINK_SIZE:
                self.sock.sendto(data[:7] + b" " + data[8:10] + self.reply + b"\x00", addr)
                self.sock.sendto(b"DCS999 " + data[9:10] + b" " + data[:7] + data[7:8] * 2
                                 + b"\x0a\x00\x20\x20", addr)

    def close(self):
        self.stop = True
        self.thread.join()
        self.sock.close()


def test_client_link_talk_unlink():
    ref = FakeDcs()
    client = dcs.DcsClient("127.0.0.1", callsign="N0TEST", module="D")
    try:
        assert client.link("127.0.0.1", timeout=1.0, port=ref.port)
        client.client.pump(0.2)
        assert dcs.keepalive_packet("N0TEST", "D", "XLX999", "D") in ref.got
        ref.got.clear()
        frames = [bytes([i]) * 9 for i in range(30)]
        client.talk(frames, 0x55AA, settle=0.05, tail=0.2)
        packets = [dcs.parse(p) for p in ref.got if dcs.parse(p)]
        assert [p["frame"] for p in packets if not p["last"]] == frames
        assert [p["counter"] for p in packets] == list(range(31)) and packets[-1]["last"]
        assert {p["stream_id"] for p in packets} == {0x55AA}
        client.unlink()
        assert ref.got[-1] == dcs.unlink_packet("N0TEST", "D")
    finally:
        client.close()
        ref.close()


def test_client_link_nak():
    ref = FakeDcs(reply=b"NAK")
    client = dcs.DcsClient("127.0.0.1")
    try:
        assert not client.link("127.0.0.1", timeout=0.5, port=ref.port)
    finally:
        client.close()
        ref.close()
