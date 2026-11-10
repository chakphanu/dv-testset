# SPDX-License-Identifier: MIT
"""dvtest/kiss_app.py: the KISS-over-TCP app client of the DP11e plan (docs/dp11e-plan.md), against a loopback server that behaves like a
KISS server: accepts, reads KISS frames, writes frames to the client, closes a refused client at once. Offline: loopback sockets only."""
import socket
import threading
import time

import pytest

from dvtest import ax25, kiss_app, kiss_tnc

pytestmark = pytest.mark.unit


class LoopServer:
    """Accept loop: `refuse` closes every connection at once; otherwise the bytes of each client are kept in `received` and `push`
    writes bytes to the newest client."""

    def __init__(self, refuse=False):
        self.refuse, self.received, self.conns = refuse, bytearray(), []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            if self.refuse:
                conn.close()
                continue
            conn.settimeout(0.1)
            self.conns.append(conn)
            threading.Thread(target=self.reader, args=(conn,), daemon=True).start()

    def reader(self, conn):
        while not self.stop.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.received += data

    def push(self, data):
        self.conns[-1].sendall(data)

    def close(self):
        self.stop.set()
        for c in self.conns:
            c.close()
        self.sock.close()


@pytest.fixture
def server():
    s = LoopServer()
    yield s
    s.close()


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


FRAME = kiss_tnc.ui_frame("N0TEST-7", "APDR16", "=1030.00N/02015.00E>dvt", digis=("WIDE1-1",))


def test_frames_flow_both_ways_with_the_channel_nibble(server):
    with kiss_app.AppKissClient("127.0.0.1", server.port) as client:
        assert client.send_frame(FRAME, channel=2)
        assert wait_for(lambda: len(server.received) > 0)
        assert ax25.kiss_decode(bytes(server.received)) == [(2, 0, FRAME, 0x20)]
        server.push(ax25.kiss_encode(5, 0, FRAME) + ax25.kiss_encode(0, 1, b"\x1e"))
        got = client.wait_frames(1, timeout=2.0)
        assert got and got[0][1] == 5 and got[0][2] == FRAME
        assert wait_for(lambda: client.other_commands())
        assert client.other_commands() == [(0, 1, b"\x1e")] and client.bytes_received > len(FRAME)


def test_commands_text_raw_and_split_writes(server):
    with kiss_app.AppKissClient("127.0.0.1", server.port) as client:
        assert client.send_command(1, b"\x1e") and client.send_command(15, b"", channel=15)
        assert client.send_text("ATE0\nKISS ON") and client.send_split(ax25.kiss_encode(0, 0, FRAME), size=5, gap=0.0)
        assert wait_for(lambda: bytes(server.received).endswith(b"\xc0"))
        data = bytes(server.received)
        assert data.startswith(b"\xc0\x01\x1e\xc0")                                         # FEND, TXDELAY command, value, FEND
        assert b"\xc0\xff\xc0" in data and b"ATE0\r\nKISS ON\r\n" in data and data.endswith(ax25.kiss_encode(0, 0, FRAME))
        with pytest.raises(ValueError):
            client.send_command(0)


def test_a_refused_client_sees_eof_with_zero_bytes():
    server = LoopServer(refuse=True)
    try:
        client = kiss_app.AppKissClient("127.0.0.1", server.port)
        assert client.connect()
        assert client.wait_closed(2.0) and client.closed_at is not None and client.bytes_received == 0 and not client.is_open
        assert client.frames() == []
        client.close()
    finally:
        server.close()


def test_connect_fails_cleanly_when_nothing_listens_and_source_ip_is_bound():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    client = kiss_app.AppKissClient("127.0.0.1", port, timeout=0.5)
    assert client.connect() is False and client.error and not client.is_open and client.send_frame(FRAME) is False
    srv = LoopServer()
    try:
        bound = kiss_app.AppKissClient("127.0.0.1", srv.port, source_ip="127.0.0.2")
        assert bound.connect()                                 # Linux routes all of 127/8 to lo
        assert wait_for(lambda: len(srv.conns) == 1)
        assert srv.conns[0].getpeername()[0] == "127.0.0.2"
        bound.close()
    finally:
        srv.close()


def test_pause_reading_holds_back_the_frames(server):
    with kiss_app.AppKissClient("127.0.0.1", server.port) as client:
        assert wait_for(lambda: len(server.conns) == 1)
        client.pause_reading()
        time.sleep(0.15)
        server.push(ax25.kiss_encode(0, 0, FRAME))
        time.sleep(0.3)
        assert client.frames() == [] and client.bytes_received == 0
        client.resume_reading()
        assert client.wait_frames(1, timeout=2.0)


def test_aprsdroid_frames_and_init_bytes():
    pos = "=1030.00N/02015.00E>dvt"
    frames = kiss_app.aprsdroid_frames("N0TEST-7", pos, ":N0TEST-1 :hello{1")
    assert len(frames) == 2
    assert ax25.to_tnc2(frames[0]) == "N0TEST-7>APDR16,WIDE1-1:" + pos
    assert ax25.to_tnc2(frames[1]) == "N0TEST-7>APDR16,WIDE1-1::N0TEST-1 :hello{1"
    assert len(kiss_app.aprsdroid_frames("N0TEST-7", pos)) == 1
    assert kiss_app.aprsdroid_init_bytes("") == b"" and kiss_app.aprsdroid_init_bytes("ATE0%0AKISS%20ON") == b"ATE0\r\nKISS ON\r\n"


def test_the_aprsdroid_reader_takes_channel_0_data_frames_only():
    reader = kiss_app.AprsdroidReader()
    assert reader.feed(ax25.kiss_encode(0, 0, FRAME)) == [FRAME]
    assert reader.feed(ax25.kiss_encode(1, 0, FRAME)) == [] and reader.dropped == 1            # channel 1: the type byte 0x10 corrupts the address
    assert reader.feed(ax25.kiss_encode(0, 0, FRAME)) == [FRAME]                                # the reader recovers at the next FEND
    # frames split over feeds, an escaped FEND/FESC inside the data, empty frames between
    framed = ax25.kiss_encode(0, 0, FRAME)
    assert reader.feed(framed[:7]) == [] and reader.feed(framed[7:] + b"\xc0\xc0") == [FRAME]
    odd = kiss_tnc.ui_frame("N0TEST-7", "APDR16", "!1030.00N/02015.00E>\xc0\xdb dvt".encode("latin-1"))
    assert reader.feed(ax25.kiss_encode(0, 0, odd)) == [odd]


def test_pinpoint_tx_uses_the_chosen_kiss_port():
    assert kiss_app.pinpoint_tx(FRAME) == ax25.kiss_encode(0, 0, FRAME)
    assert kiss_app.pinpoint_tx(FRAME, 3)[1] == 0x30
    with pytest.raises(ValueError):
        kiss_app.pinpoint_tx(FRAME, 16)
