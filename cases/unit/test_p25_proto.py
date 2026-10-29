# SPDX-License-Identifier: MIT
"""P25 reflector-link records (dvtest/proto/p25.py) and P25Client against a
loopback reflector that echoes polls and relays records."""
import socket
import threading

import pytest

from dvtest.proto import p25


def test_poll_and_unlink():
    assert p25.poll_packet("N0TEST") == b"\xf0N0TEST    "
    assert p25.unlink_packet("n0call") == b"\xf1N0CALL    "
    assert p25.parse_poll(p25.poll_packet("N0TEST")) == ("poll", "N0TEST")
    assert p25.parse_poll(p25.unlink_packet("N0TEST")) == ("unlink", "N0TEST")
    assert p25.parse_poll(b"\xf0N0TEST") is None


def test_record_layouts():
    imbe = bytes(range(1, 12))
    sizes = {}
    for index in range(18):
        rtype = p25.record_type(index)
        data = p25.record(rtype, imbe, src=1023007, dst=10200)
        sizes[rtype] = len(data)
        fields = p25.parse(data)
        assert fields["imbe"] == imbe and fields["type"] == rtype
        assert fields["ldu"] == (1 if index < 9 else 2)
    assert [p25.record_type(i) for i in range(20)][:2] == [0x62, 0x63]
    assert p25.record_type(9) == 0x6B and p25.record_type(18) == 0x62
    assert sizes == {0x62: 22, 0x63: 14, 0x64: 17, 0x65: 17, 0x66: 17, 0x67: 17, 0x68: 17,
                     0x69: 17, 0x6A: 16, 0x6B: 22, 0x6C: 14, 0x6D: 17, 0x6E: 17, 0x6F: 17,
                     0x70: 17, 0x71: 17, 0x72: 17, 0x73: 16}
    # IMBE offsets: voice1 10, voice2 1, voice3..8 5, voice9 4
    assert p25.record(0x62, imbe)[10:21] == imbe
    assert p25.record(0x6C, imbe)[1:12] == imbe
    assert p25.record(0x6F, imbe)[5:16] == imbe
    assert p25.record(0x73, imbe)[4:15] == imbe
    assert p25.parse(p25.record(0x65, imbe, dst=10200))["dst"] == 10200
    assert p25.record(0x65, imbe, dst=10200)[1:4] == b"\x00\x27\xd8"
    assert p25.parse(p25.record(0x66, imbe, src=1023007))["src"] == 1023007
    assert p25.parse(p25.record(0x70, imbe))["algid"] == 0x80
    assert p25.parse(p25.record(0x64, imbe))["lcf"] == 0x00
    with pytest.raises(ValueError):
        p25.record(0x62, bytes(9))


def test_terminator_and_parse_rejects():
    term = p25.terminator()
    assert len(term) == 17 and term[0] == 0x80 and not any(term[1:])
    assert p25.parse(term) == {"type": 0x80, "terminator": True}
    assert p25.parse(p25.poll_packet("N0TEST")) is None
    assert p25.parse(p25.record(0x62, bytes(11))[:-1]) is None
    assert p25.parse(b"") is None
    assert not p25.is_voice(term) and p25.is_voice(p25.record(0x6A, bytes(11)))


def test_voice_records_pad_and_lifecycle():
    frames = [bytes([i]) * 11 for i in range(1, 21)]           # 20 -> 27 (3 LDUs)
    records = p25.voice_records(frames, 1023007, 10200)
    assert len(records) == 28 and records[-1] == p25.terminator()
    parsed = [p25.parse(r) for r in records]
    got = [r["imbe"] for r in parsed if not r["terminator"]]
    assert got[:20] == frames and got[20:] == [p25.SILENCE] * 7
    types = [r["type"] for r in parsed]
    assert p25.lifecycle(types) == []
    assert p25.lifecycle(types + types) == []
    assert p25.lifecycle(types[:-1]) == ["call not terminated"]
    assert p25.lifecycle(types[1:])[0].startswith("call opens with 0x63")
    assert p25.lifecycle(types[:5] + [0x80]) == ["terminator 5 inside an LDU"]
    assert p25.lifecycle(types[:5] + [0x80], whole_ldu=False) == []
    swapped = types[:3] + [types[4], types[3]] + types[5:]
    assert p25.lifecycle(swapped)[0] == "record 3 is 0x66, expected 0x65"


class LoopReflector:
    """Loopback stand-in for a P25Reflector: echo polls to the sender,
    forget a sender on unlink, relay everything else to the other
    linked senders."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.linked = set()
        self.log = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            self.log.append((addr, data))
            if data[:1] == b"\xf0":
                self.linked.add(addr)
                self.sock.sendto(data, addr)
            elif data[:1] == b"\xf1":
                self.linked.discard(addr)
            elif addr in self.linked:
                for other in list(self.linked):
                    if other != addr:
                        self.sock.sendto(data, other)

    def close(self):
        self.stop.set()
        self.thread.join()
        self.sock.close()


def test_client_link_talk_collect():
    ref = LoopReflector()
    talker = p25.P25Client("127.0.0.1", callsign="N0TEST", src_id=1023008, tg=10200)
    listener = p25.P25Client("127.0.0.1", callsign="N0CALL")
    try:
        assert listener.link("127.0.0.1", port=ref.port)
        assert talker.link("127.0.0.1", port=ref.port)
        listener.client.inbox.clear()
        talker.client.inbox.clear()
        frames = [bytes([i]) * 11 for i in range(1, 10)]
        talker.talk(frames, tail=0.3, gap=0.002)
        assert listener.collect(settle=0.3) == frames
        types = [r["type"] for r in listener.records()]
        assert types == list(p25.LDU1) + [0x80] and p25.lifecycle(types) == []
        src = [r for r in listener.records() if r.get("src")]
        assert src[0]["src"] == 1023008
        # the talker hears only poll echoes, never its own voice
        assert all(p25.parse_poll(d) for d in talker.client.inbox)
        talker.unlink()
        listener.client.pump(0.1)
        assert ("unlink", "N0TEST") in [p25.parse_poll(d) for _, d in ref.log]
    finally:
        talker.close()
        listener.close()
        ref.close()


def test_link_fails_without_echo():
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    client = p25.P25Client("127.0.0.1")
    try:
        assert not client.link("127.0.0.1", timeout=0.4, port=sink.getsockname()[1])
    finally:
        client.close()
        sink.close()
