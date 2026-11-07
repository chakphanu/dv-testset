# SPDX-License-Identifier: MIT
"""dvtest/kiss_tnc.py: the fake KISS TNC of the DP15 RF cases, against a loopback client that behaves like the
DV-Router AX.25 edge (connects, writes the five start-up parameter frames, reads injected frames, writes a data
frame when it "transmits"). Offline: loopback sockets only."""
import socket
import time

import pytest

from dvtest import ax25, kiss_tnc

pytestmark = pytest.mark.unit


class EdgeClient:
    """What crates/edge/ax25/src/adapter/tcp.rs does on a kiss-tcp port: connect, write the parameters of channel
    0 (kiss_parameters), then read KISS frames."""

    def __init__(self, port, channel=0, params=True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(0.2)
        self.decoder = ax25.KissDecoder()
        self.frames = []
        if params:
            self.sock.sendall(ax25.dvrouter_startup_params(channel))

    def read(self, count=1, timeout=5.0):
        end = time.monotonic() + timeout
        while len(self.frames) < count and time.monotonic() < end:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.frames.extend(self.decoder.feed(chunk))
        return self.frames

    def transmit(self, frame, channel=0):
        self.sock.sendall(ax25.kiss_encode(channel, 0, frame))

    def close(self):
        self.sock.close()


FRAME = kiss_tnc.ui_frame("N0TEST-7", "APDVT1", kiss_tnc.aprs_message_info("XLX999B", "DVT 1234", "3"), digis=("WIDE1-1",))


def test_records_the_five_startup_frames_in_order():
    with kiss_tnc.FakeKissTnc() as tnc:
        assert not tnc.is_connected() and tnc.connections == 0
        edge = EdgeClient(tnc.port)
        try:
            assert tnc.wait_connected(5)
            params = tnc.wait_params(timeout=5)
        finally:
            edge.close()
    assert [(cmd, value) for _ch, cmd, value in params] == [(1, 0x1E), (2, 0x3F), (3, 0x0A), (4, 0x03), (5, 0x00)]
    assert tuple(cmd for _ch, cmd, _v in params) == kiss_tnc.PARAM_COMMANDS
    assert {ch for ch, _cmd, _v in params} == {0}
    assert tnc.data_frames() == [] and tnc.other_frames() == []


def test_a_reconnect_gives_a_second_list_of_parameters():
    with kiss_tnc.FakeKissTnc() as tnc:
        first = EdgeClient(tnc.port)
        assert len(tnc.wait_params(timeout=5)) == 5
        first.close()
        assert tnc.wait(lambda t: not t.is_connected(), 5)
        second = EdgeClient(tnc.port)
        try:
            assert tnc.wait(lambda t: t.connections == 2 and len(t.startup_params(2)) == 5, 5)
            assert tnc.startup_params(1) == tnc.startup_params(2) and len(tnc.startup_params(1)) == 5
            assert tnc.current_connection() == 2
        finally:
            second.close()


def test_inject_reaches_the_client_as_a_kiss_data_frame():
    with kiss_tnc.FakeKissTnc() as tnc:
        assert tnc.inject(FRAME) is False                      # nobody connected yet
        edge = EdgeClient(tnc.port)
        try:
            assert tnc.wait_connected(5)
            assert tnc.inject(FRAME) is True
            assert tnc.inject(FRAME, channel=1) is True
            got = edge.read(2)
        finally:
            edge.close()
    assert [(f[0], f[1], f[2]) for f in got] == [(0, 0, FRAME), (1, 0, FRAME)]
    parsed = ax25.parse_frame(got[0][2])
    assert parsed["src"]["text"] == "N0TEST-7" and parsed["info"] == b":XLX999B  :DVT 1234{3"


def test_frames_the_edge_transmits_are_recorded_and_decoded():
    t0 = time.monotonic()
    out = kiss_tnc.ui_frame("N0TEST-1", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", "hello room"))
    with kiss_tnc.FakeKissTnc() as tnc:
        edge = EdgeClient(tnc.port)
        try:
            tnc.wait_params(timeout=5)
            edge.transmit(out)
            edge.transmit(b"\xc0\xdb" * 5 + out[10:])          # a frame with bytes that need escaping (does not parse)
            frames = tnc.wait_data(2, timeout=5, since=t0)
        finally:
            edge.close()
        assert len(frames) == 2 and frames[0][2] == out
        decoded = tnc.decoded(t0)
    assert len(decoded) == 1                                    # the second one is not a UI frame and is skipped
    _at, parsed, tnc2 = decoded[0]
    assert tnc2 == "N0TEST-1>APZDVR::XLX999B  :hello room"
    msg = kiss_tnc.decode_message(out)
    assert msg["src"] == "N0TEST-1" and msg["dst"] == "APZDVR" and msg["digis"] == []
    assert msg["addressee"] == "XLX999B" and msg["text"] == "hello room" and msg["msg_id"] is None


def test_other_kiss_commands_are_reported():
    with kiss_tnc.FakeKissTnc() as tnc:
        edge = EdgeClient(tnc.port, params=False)
        try:
            edge.sock.sendall(ax25.kiss_encode(0, 6, b"\x01") + ax25.kiss_encode(15, 15))
            assert tnc.wait(lambda t: len(t.other_frames()) == 2, 5)
        finally:
            edge.close()
        assert [f.command for f in tnc.other_frames()] == [6, 15]
        assert tnc.startup_params() == []


def test_drop_connection_makes_the_client_see_eof_or_a_reset():
    with kiss_tnc.FakeKissTnc() as tnc:
        edge = EdgeClient(tnc.port)
        try:
            assert tnc.wait_connected(5)
            tnc.drop_connection()
            end = time.monotonic() + 5
            closed = False
            while time.monotonic() < end and not closed:
                try:
                    closed = edge.sock.recv(10) == b""
                except socket.timeout:
                    pass
                except ConnectionResetError:                   # unread bytes on our side turn the close into a reset
                    closed = True
            assert closed
            assert tnc.wait(lambda t: not t.is_connected(), 5)
            assert tnc.inject(FRAME) is False
        finally:
            edge.close()


def test_inject_raw_and_stop_is_clean():
    tnc = kiss_tnc.FakeKissTnc().start()
    edge = EdgeClient(tnc.port)
    assert tnc.wait_connected(5)
    assert tnc.inject_raw(ax25.kiss_encode(0, 0, FRAME)) is True
    assert edge.read(1)[0][2] == FRAME
    tnc.stop()
    edge.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", tnc.port), timeout=1).close()


def test_two_tncs_on_two_ports_are_independent():
    with kiss_tnc.FakeKissTnc(name="a") as a, kiss_tnc.FakeKissTnc(name="b") as b:
        assert a.port != b.port
        ea, eb = EdgeClient(a.port), EdgeClient(b.port)
        try:
            a.wait_params(timeout=5)
            b.wait_params(timeout=5)
            a.inject(FRAME)
            assert len(ea.read(1)) == 1 and eb.read(1, timeout=0.5) == []
        finally:
            ea.close()
            eb.close()


# ---- builders

def test_aprs_message_info_pads_the_addressee_and_adds_the_id():
    assert kiss_tnc.aprs_message_info("XLX999B", "hi", "12") == b":XLX999B  :hi{12"
    assert kiss_tnc.aprs_message_info("N0TEST-7", "x") == b":N0TEST-7 :x"
    assert kiss_tnc.aprs_message_info("ABCDEFGHI", "x") == b":ABCDEFGHI:x"
    for bad in ("", "ABCDEFGHIJ"):
        with pytest.raises(ValueError):
            kiss_tnc.aprs_message_info(bad, "x")
    with pytest.raises(ValueError):
        kiss_tnc.aprs_message_info("XLX999B", "x", "123456")


def test_ui_frame_roundtrips_through_decode_message():
    frame = kiss_tnc.ui_frame("N0TEST-7", "APDVT1", b":XLX999B  :DVT 1{4", digis=("WIDE1-1", "WIDE2-1"))
    assert ax25.dvrouter_accepts_frame(frame)
    msg = kiss_tnc.decode_message(frame)
    assert msg["src"] == "N0TEST-7" and msg["dst"] == "APDVT1" and msg["digis"] == ["WIDE1-1", "WIDE2-1"]
    assert msg["addressee"] == "XLX999B" and msg["text"] == "DVT 1" and msg["msg_id"] == "4"


def test_decode_message_refuses_other_frames():
    pos = kiss_tnc.ui_frame("N0TEST-7", "APDVT1", b"!1030.00N/02015.00E>test")
    assert kiss_tnc.decode_message(pos) is None
    assert kiss_tnc.decode_message(b"\x00" * 20) is None
    assert kiss_tnc.decode_message(b"short") is None
    assert kiss_tnc.decode_message(kiss_tnc.ui_frame("N0TEST-1", "APZDVR", b":XLX999B  ")) is None
