# SPDX-License-Identifier: MIT
"""Round trips for the DPlus (REF) codec (dvtest/proto/dplus.py) and the
client's link handshake against a loopback fake reflector."""
import socket
import threading

from dvtest.proto import dextra, dplus


def test_control_packets():
    assert dplus.CONNECT == b"\x05\x00\x18\x00\x01"
    assert dplus.DISCONNECT == b"\x05\x00\x18\x00\x00"
    assert dplus.KEEPALIVE == b"\x03\x60\x00"
    login = dplus.login_packet("N0TEST")
    assert len(login) == 28
    assert login == b"\x1c\xc0\x04\x00N0TEST" + bytes(10) + b"DV019999"
    assert dplus.login_packet(dextra._cs8("N0TEST")) == login
    assert dplus.LOGIN_OK == b"\x08\xc0\x04\x00OKRW" and len(dplus.LOGIN_BUSY) == 8


def test_header_and_voice():
    head = dplus.header(0xBEEF, "N0TEST", "D")
    assert len(head) == dplus.HEADER_SIZE and head[:2] == b"\x3a\x80"
    assert head[2:] == dextra.header(0xBEEF, "N0TEST", "D")
    assert dplus.parse_header(head) == {"stream_id": 0xBEEF, "rpt2": b"XLX999 D",
                                        "rpt1": b"N0TEST D", "ur": b"CQCQCQ  ",
                                        "my": b"N0TEST  "}
    frame = bytes(range(9, 18))
    for seq in (0, 20, 21, 44):
        data = dplus.voice(0xBEEF, seq, frame)
        assert len(data) == dplus.VOICE_SIZE and data[:2] == b"\x1d\x80"
        assert dplus.parse_voice(data) == (0xBEEF, seq % 21, False, frame)
    last = dplus.last_voice(0xBEEF, 60)
    assert len(last) == dplus.LAST_SIZE and last[:2] == b"\x20\x80"
    assert last[8] == 0x81 and last[16] == (60 % 21) | 0x40
    assert last[17:] == dplus.END_PATTERN
    assert dplus.parse_voice(last) == (0xBEEF, 60 % 21, True, dplus.END_PATTERN[:9])
    assert dplus.parse_voice(head) is None and dplus.parse_header(last) is None
    # a DExtra packet is not a DPlus packet
    assert dplus.parse_voice(dextra.voice(1, 0, frame, False)) is None


class FakeRef:
    """Answers connect with its echo, login with OKRW and records the
    rest, like xlxd's DPlus side minus the ACK delay."""

    def __init__(self, reply=dplus.LOGIN_OK):
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
            if data in (dplus.CONNECT, dplus.DISCONNECT):
                self.sock.sendto(data, addr)
            elif len(data) == 28:
                self.sock.sendto(self.reply, addr)
                self.sock.sendto(dplus.KEEPALIVE, addr)

    def close(self):
        self.stop = True
        self.thread.join()
        self.sock.close()


def test_client_link_join_talk_unlink():
    ref = FakeRef()
    client = dplus.DplusClient("127.0.0.1", callsign="N0TEST", module="D")
    try:
        assert client.link("127.0.0.1", timeout=1.0, port=ref.port)
        # keepalive answered, join = header + last frame on module D
        client.client.pump(0.2)
        assert ref.got[0] == dplus.CONNECT and len(ref.got[1]) == 28
        assert dplus.KEEPALIVE in ref.got
        heads = [dplus.parse_header(p) for p in ref.got if dplus.parse_header(p)]
        assert heads and heads[0]["rpt2"] == b"XLX999 D"
        lasts = [dplus.parse_voice(p) for p in ref.got if len(p) == dplus.LAST_SIZE]
        assert lasts and lasts[0][0] == heads[0]["stream_id"]
        ref.got.clear()
        frames = [bytes([i]) * 9 for i in range(25)]
        client.talk(frames, 0x1234, settle=0.05, tail=0.2)
        voices = [dplus.parse_voice(p) for p in ref.got if dplus.parse_voice(p)]
        assert [v[3] for v in voices if not v[2]] == frames
        assert [v[1] for v in voices] == [i % 21 for i in range(26)] and voices[-1][2]
        client.unlink()
        assert ref.got[-1] == dplus.DISCONNECT
    finally:
        client.close()
        ref.close()


def test_client_login_busy():
    ref = FakeRef(reply=dplus.LOGIN_BUSY)
    client = dplus.DplusClient("127.0.0.1")
    try:
        assert not client.link("127.0.0.1", timeout=0.5, port=ref.port)
    finally:
        client.close()
        ref.close()
