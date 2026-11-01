# SPDX-License-Identifier: MIT
"""FakeAmbedServer against an in-test client that plays the worker.

Everything runs on 127.0.0.1 with ephemeral ports; no rig, no real transcoder.
"""
import socket
import time

import pytest

from dvtest.fake_ambed import FakeAmbedServer
from dvtest.proto import ambed_ext as ext

HOST = "127.0.0.1"
FRAME9 = bytes(range(0x30, 0x39))


class Worker:
    """The worker side: one control socket, data sockets on request."""

    def __init__(self, server):
        self.server = server
        self.dest = (HOST, server.port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, 0))
        self.tx = 0x7000
        self.data_socks = []

    def close(self):
        self.sock.close()
        for s in self.data_socks:
            s.close()

    def send(self, data):
        self.sock.sendto(data, self.dest)

    def recv(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            return self.sock.recvfrom(2048)[0]
        except socket.timeout:
            return None

    def rpc(self, data, timeout=1.0):
        self.send(data)
        return self.recv(timeout)

    def open(self, codec_in, outs, transaction=None, timeout=1.0):
        if transaction is None:
            self.tx += 1
            transaction = self.tx
        reply = self.rpc(ext.open_request("N0TEST", codec_in, outs, transaction), timeout)
        return transaction, reply

    def stream(self, codec_in, outs, transaction=None):
        """Open and return a Stream on a STD; fail the test otherwise."""
        transaction, reply = self.open(codec_in, outs, transaction)
        parsed = ext.parse_std(reply or b"")
        assert parsed is not None, "no STD: %r" % (reply,)
        sid, port, _cin, _first, tx = parsed
        assert tx == transaction
        return Stream(self, sid, port, codec_in, tuple(outs), transaction)


class Stream:
    def __init__(self, worker, sid, port, codec_in, outs, transaction):
        self.worker, self.sid, self.port = worker, sid, port
        self.codec_in, self.outs, self.transaction = codec_in, outs, transaction
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, 0))
        worker.data_socks.append(self.sock)
        self.dest = (HOST, port)

    def request(self, pid, frame=None):
        if frame is None:
            frame = bytes((pid + i) & 0xFF for i in range(ext.WIDTH[self.codec_in]))
        self.sock.sendto(ext.data_request(self.codec_in, pid, frame), self.dest)
        return frame

    def reply(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            return self.sock.recvfrom(4096)
        except socket.timeout:
            return None

    def close(self):
        self.worker.send(ext.cs(self.sid))


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def server():
    srv = FakeAmbedServer(HOST, port=0, control_port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def worker(server):
    w = Worker(server)
    yield w
    w.close()


# ---- PING, CAPS ----------------------------------------------------------------

def test_ping_gets_pong_and_other_lengths_are_ignored(worker, server):
    assert worker.rpc(ext.ping("XLX999")) == b"AMBEDPONG"
    assert worker.rpc(ext.ping("XLX999")) == b"AMBEDPONG"
    assert worker.rpc(ext.ping("XLX999") + b"x", 0.15) is None
    assert worker.rpc(b"AMBEDPING", 0.15) is None
    assert server.stats()["counters"]["rx_unknown"] == 2


def test_caps_reply_is_fixed_for_the_life_of_the_server(worker):
    first = worker.rpc(ext.caps_request())
    assert first == bytes.fromhex("414d424544434150530104000000" "3e")
    for _ in range(5):
        assert worker.rpc(ext.caps_request()) == first


def test_caps_request_must_be_exactly_nine_bytes(worker):
    assert worker.rpc(b"AMBEDCAPS\x00", 0.15) is None
    # the echo of the request is the request itself; a reply is no request
    assert worker.rpc(ext.caps_reply(4, 0x3E), 0.15) is None


def test_custom_caps_and_invalid_caps_refused():
    with FakeAmbedServer(HOST, port=0, control_port=None, max_outputs=2, caps_mask=0x0E) as srv:
        w = Worker(srv)
        try:
            assert ext.parse_caps_reply(w.rpc(ext.caps_request())) == (1, 2, 0x0E)
        finally:
            w.close()
    with pytest.raises(ValueError):
        FakeAmbedServer(HOST, port=0, control_port=None, max_outputs=5)
    with pytest.raises(ValueError):
        FakeAmbedServer(HOST, port=0, control_port=None, caps_mask=0x41)


# ---- OPEN, STD, data -----------------------------------------------------------

def test_open_gets_std_with_echoed_transaction(worker):
    tx, reply = worker.open(1, [2, 3], 0xA1B2C3D4)
    assert len(reply) == 18
    sid, port, cin, first, echoed = ext.parse_std(reply)
    assert (cin, first, echoed) == (1, 2, 0xA1B2C3D4) and tx == 0xA1B2C3D4
    assert sid != 0 and port != 0 and port != worker.server.port


def test_data_reply_is_the_transform_in_open_order(worker):
    st = worker.stream(1, [2, 3])
    frame = st.request(0x41, FRAME9)
    data, src = st.reply()
    assert src == (HOST, st.port)                     # same port as the request went to
    assert len(data) == 20
    assert data == ext.expected_reply(1, [2, 3], 0x41, frame)
    assert data[:2] == b"\x02\x41" and data[11] == 3


@pytest.mark.parametrize("src", range(1, 6))
def test_every_input_codec_with_all_other_outputs(worker, src):
    outs = [c for c in range(1, 6) if c != src]
    st = worker.stream(src, outs)
    frame = st.request(src, None)
    data, _ = st.reply()
    assert data == ext.expected_reply(src, outs, src, ext.data_request(src, 0, bytes(
        (src + i) & 0xFF for i in range(ext.WIDTH[src])))[2:])
    assert len(data) == ext.reply_size(outs)
    assert frame is not None


def test_four_outputs_353_bytes(worker):
    st = worker.stream(1, [2, 3, 4, 5])
    frame = st.request(9, FRAME9)
    data, _ = st.reply()
    assert len(data) == 353 and data == ext.expected_reply(1, [2, 3, 4, 5], 9, frame)


def test_burst_keeps_count_order_and_pid(worker):
    st = worker.stream(2, [1, 5])
    frames = {}
    for pid in range(60):
        frames[pid] = st.request(pid)
    pids = []
    for _ in range(60):
        data, _ = st.reply()
        pid, entries = ext.parse_data_reply(data, [1, 5])
        assert data == ext.expected_reply(2, [1, 5], pid, frames[pid])
        pids.append(pid)
    assert pids == list(range(60))
    assert st.reply(0.15) is None                                  # exactly one reply per request
    stats = worker.server.stats()
    assert stats["streams"][0]["requests"] == 60 and stats["streams"][0]["replies"] == 60


def test_pid_wraps_modulo_256_on_the_wire(worker):
    st = worker.stream(1, [2])
    st.request(0x1FF, FRAME9)
    data, _ = st.reply()
    assert data[:2] == b"\x02\xff"


def test_retransmitted_open_gets_identical_std_and_no_second_stream(worker, server):
    tx, first = worker.open(1, [2, 3], 0x55)
    _, second = worker.open(1, [2, 3], 0x55)
    assert first == second and ext.parse_std(first) is not None
    stats = server.stats()
    assert stats["streams_live"] == 1 and stats["counters"]["open_new"] == 1
    assert stats["counters"]["open_repeat"] == 1
    # the data socket still works after the repeat
    st = Stream(worker, *ext.parse_std(first)[:2], 1, (2, 3), tx)
    st.request(1, FRAME9)
    assert st.reply() is not None


def test_new_transaction_or_other_outputs_make_a_new_stream(worker, server):
    _, a = worker.open(1, [2], 1)
    _, b = worker.open(1, [2], 2)
    _, c = worker.open(1, [2, 3], 1)
    ids = {ext.parse_std(x)[0] for x in (a, b, c)}
    ports = {ext.parse_std(x)[1] for x in (a, b, c)}
    assert len(ids) == 3 and len(ports) == 3
    assert server.stats()["streams_live"] == 3


def test_cs_frees_the_stream_and_its_port(worker, server):
    st = worker.stream(1, [2])
    st.request(1, FRAME9)
    assert st.reply() is not None
    st.close()
    assert wait_for(lambda: server.stats()["streams_live"] == 0)
    time.sleep(0.2)                                           # data thread notices within 50 ms
    st.request(2, FRAME9)
    assert st.reply(0.2) is None
    assert server.stats()["counters"]["cs_known"] == 1
    # the same OPEN afterwards is a new stream (the key was freed with it)
    again = worker.open(1, [2], st.transaction)[1]
    assert ext.parse_std(again) is not None and server.stats()["streams_live"] == 1


def test_cs_for_unknown_stream_is_ignored_without_reply(worker, server):
    assert worker.rpc(ext.cs(4242), 0.15) is None
    assert server.stats()["counters"]["cs_unknown"] == 1
    assert worker.rpc(ext.ping("A")) == b"AMBEDPONG"


def test_data_drops(worker, server):
    st = worker.stream(1, [2])
    st.sock.sendto(ext.data_request(2, 1, bytes(9)), st.dest)          # not the stream's input
    assert st.reply(0.15) is None
    st.sock.sendto(b"\x01\x02" + bytes(8), st.dest)                    # short frame
    assert st.reply(0.15) is None
    st.sock.sendto(b"\x01\x02", st.dest)
    assert st.reply(0.15) is None
    assert server.stats()["counters"]["data_dropped"] == 3
    st.request(3, FRAME9)
    assert st.reply() is not None


def test_lenient_length_uses_the_received_length(worker, server):
    server.set_modes(len_check="lenient")
    st = worker.stream(1, [3])
    st.sock.sendto(b"\x01\x07\x10\x20\x30", st.dest)
    data, _ = st.reply()
    assert data == b"\x03\x07" + ext.transform(1, 3, b"\x10\x20\x30")


def _bad_open(worker, server, reason, request):
    """The OPEN gets no reply, is counted and logged as invalid with its reason."""
    before = server.stats()["counters"].get("open_invalid", 0)
    assert worker.rpc(request, 0.15) is None, reason
    stats = server.stats()
    assert stats["counters"]["open_invalid"] == before + 1
    assert stats["counters"]["open_invalid_" + reason] >= 1
    assert stats["streams_live"] == 0
    last = [e for e in server.log_entries(kinds=["open"])][-1]
    assert last["note"] == "invalid" and last["reason"] == reason


def test_invalid_open_gets_no_reply(worker, server):
    _bad_open(worker, server, "codec_range", ext.open_request("N0TEST", 9, [2], 1))
    _bad_open(worker, server, "no_outputs", ext.open_request("N0TEST", 1, [], 1))
    _bad_open(worker, server, "codec_range", ext.open_request("N0TEST", 1, [7], 1))
    _bad_open(worker, server, "codec_range", ext.open_request("N0TEST", 0, [2], 1))


def test_strict_open_rejections(worker, server):
    # outputs not ascending, or equal
    _bad_open(worker, server, "order", ext.open_request("N0TEST", 1, [3, 2], 1))
    _bad_open(worker, server, "order", ext.open_request("N0TEST", 1, [2, 2], 1))
    # a gap: slot 1 unused but slot 2 used; unused slots must follow slot 0 contiguously
    gap = ext.open_request("N0TEST", 1, [2, 0, 3], 1)
    _bad_open(worker, server, "gap", gap)
    # first slot empty with a later slot used is "no outputs" in slot 0
    _bad_open(worker, server, "no_outputs", ext.open_request("N0TEST", 1, [0, 2], 1))
    # the input codec among the outputs
    _bad_open(worker, server, "input_in_outputs", ext.open_request("N0TEST", 2, [1, 2], 1))
    # transaction 0
    _bad_open(worker, server, "transaction_zero", ext.open_request("N0TEST", 1, [2], 0))
    assert worker.open(1, [2], 5)[1] is not None            # a good one still works


def test_strict_open_checks_caps_limits():
    with FakeAmbedServer(HOST, port=0, control_port=None, max_outputs=2, caps_mask=0x0E) as srv:
        w = Worker(srv)
        try:
            # three outputs against max outputs 2
            _bad_open(w, srv, "too_many_outputs", ext.open_request("N0TEST", 1, [2, 3, 4], 1))
            # IMBE (4) and PCM (5) are outside the mask 0x0E
            _bad_open(w, srv, "not_in_mask", ext.open_request("N0TEST", 1, [2, 4], 2))
            _bad_open(w, srv, "not_in_mask", ext.open_request("N0TEST", 5, [2], 3))
            assert ext.parse_std(w.open(1, [2, 3], 4)[1]) is not None
        finally:
            w.close()


def test_lenient_open_accepts_what_a_worker_never_sends():
    with FakeAmbedServer(HOST, port=0, control_port=None, lenient_open=True) as srv:
        w = Worker(srv)
        try:
            for codec_in, outs, tx in ((2, [1, 2], 0), (1, [3, 2], 7), (1, [1], 8)):
                assert ext.parse_std(w.open(codec_in, outs, tx)[1]) is not None
            # the transform still needs sane ids and one output
            _bad_open_lenient = [w.rpc(ext.open_request("N0TEST", 9, [2], 1), 0.15),
                                 w.rpc(ext.open_request("N0TEST", 1, [], 1), 0.15)]
            assert _bad_open_lenient == [None, None]
            assert srv.stats()["counters"]["open_invalid_codec_range"] == 1
            assert srv.stats()["counters"]["open_invalid_no_outputs"] == 1
            srv.set_modes(lenient_open=False)
            assert w.rpc(ext.open_request("N0TEST", 1, [3, 2], 9), 0.15) is None
            srv.reset()
            assert srv.stats()["modes"]["lenient_open"] is True        # the constructor value
        finally:
            w.close()


def test_open_log_marks_a_repeated_transaction(worker, server):
    worker.open(1, [2], 0x41)                       # new
    worker.open(1, [2], 0x41)                       # retransmission: same key
    worker.open(1, [3], 0x41)                       # same transaction, other outputs
    worker.open(1, [2], 0x42)
    server.reset()
    worker.open(1, [2], 0x42)                       # reset forgets earlier transactions
    flags = [e["tx_repeat"] for e in server.log_entries(kinds=["open"])]
    assert flags == [False]
    worker.open(1, [2], 0x42)
    worker.open(1, [4], 0x43)
    entries = server.log_entries(kinds=["open"])
    assert [e["tx_repeat"] for e in entries] == [False, True, False]
    assert [e.get("note") for e in entries] == [None, "tx_repeat", None]


def test_default_data_ports_are_the_fixed_range():
    with FakeAmbedServer(HOST, port=0, control_port=None) as srv:
        assert srv.data_ports == (10200, 10299)


def test_data_port_range_lowest_free_first_and_full_range_is_busy():
    probe = []
    for _ in range(50):                    # three consecutive free ports
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((HOST, 0))
        low = s.getsockname()[1]
        s.close()
        trial = []
        try:
            for p in (low + 1, low + 2):
                t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                trial.append(t)
                t.bind((HOST, p))
            probe = [low]
            break
        except OSError:
            continue
        finally:
            for t in trial:
                t.close()
    assert probe, "no free port triple"
    low = probe[0]
    with FakeAmbedServer(HOST, port=0, control_port=None, data_ports=(low, low + 2)) as srv:
        w = Worker(srv)
        try:
            ports = [ext.parse_std(w.open(1, [2], 100 + i)[1])[1] for i in range(3)]
            assert ports == [low, low + 1, low + 2]
            tx, reply = w.open(1, [2], 200)
            assert ext.parse_busy(reply) == tx
            assert srv.stats()["counters"]["alloc_failed"] == 1
            sid = srv.stats()["streams"][1]["id"]           # frees the middle port
            w.send(ext.cs(sid))
            assert wait_for(lambda: srv.stats()["streams_live"] == 2)
            time.sleep(0.2)
            assert ext.parse_std(w.open(1, [2], 201)[1])[1] == low + 1
        finally:
            w.close()


def test_data_ports_must_be_a_valid_range():
    for bad in ((0, 5), (20, 10), (1, 70000)):
        with pytest.raises(ValueError):
            FakeAmbedServer(HOST, port=0, control_port=None, data_ports=bad)


def test_data_port_base():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind((HOST, 0))
    free = probe.getsockname()[1]
    probe.close()
    with FakeAmbedServer(HOST, port=0, control_port=None, data_port_base=free - 1) as srv:
        w = Worker(srv)
        try:
            sid, port, *_ = ext.parse_std(w.open(1, [2])[1])
            assert port == free - 1 + sid
        finally:
            w.close()


# ---- max sessions ---------------------------------------------------------------

def test_max_sessions_busy_then_free_again(worker, server):
    server.set_modes(max_sessions=2)
    s1 = worker.stream(1, [2])
    worker.stream(1, [2])
    tx, reply = worker.open(1, [2])
    assert reply == ext.busy(tx) and len(reply) == 13
    assert server.stats()["streams_live"] == 2
    # a retransmitted OPEN of a live stream is still answered with its STD
    _, again = worker.open(1, [2], s1.transaction)
    assert ext.parse_std(again)[0] == s1.sid
    s1.close()
    assert wait_for(lambda: server.stats()["streams_live"] == 1)
    assert ext.parse_std(worker.open(1, [2])[1]) is not None
    assert server.stats()["counters"]["busy_capacity"] == 1


def test_max_sessions_zero_is_always_busy(worker, server):
    server.set_modes(max_sessions=0)
    tx, reply = worker.open(1, [2])
    assert reply == ext.busy(tx)
    server.set_modes(max_sessions=None)
    assert ext.parse_std(worker.open(1, [2])[1]) is not None


# ---- open modes -----------------------------------------------------------------

def test_open_busy_is_final_and_allocates_nothing(worker, server):
    server.set_modes(open="busy")
    tx, reply = worker.open(1, [2], 0x7001)
    assert reply == bytes.fromhex("414d42454442555359" "00007001")
    assert worker.recv(0.15) is None
    assert server.stats()["streams_live"] == 0
    server.set_modes(open="std")
    assert ext.parse_std(worker.open(1, [2], 0x7002)[1]) is not None


def test_open_silent_answers_nothing_and_allocates_nothing(worker, server):
    server.set_modes(open="silent")
    assert worker.open(1, [2], timeout=0.2)[1] is None
    assert server.stats()["streams_live"] == 0
    assert worker.rpc(ext.ping("A")) == b"AMBEDPONG"      # the control port still lives


def test_open_late_sends_the_std_after_the_delay(worker, server):
    server.set_modes(open="late:300")
    t0 = time.monotonic()
    worker.send(ext.open_request("N0TEST", 1, [2], 9))
    assert worker.recv(0.15) is None
    reply = worker.recv(1.0)
    assert time.monotonic() - t0 >= 0.29
    assert ext.parse_std(reply)[4] == 9
    assert server.stats()["streams_live"] == 1


def test_open_std_twice_sends_two_identical_stds(worker, server):
    server.set_modes(open="std_twice")
    worker.send(ext.open_request("N0TEST", 1, [2], 5))
    a, b = worker.recv(), worker.recv()
    assert a == b and ext.parse_std(a) is not None
    assert worker.recv(0.15) is None
    assert server.stats()["streams_live"] == 1


def test_open_std_then_other_sends_a_second_std_of_another_stream(worker, server):
    server.set_modes(open="std_then_other")
    worker.send(ext.open_request("N0TEST", 1, [2], 5))
    a, b = ext.parse_std(worker.recv()), ext.parse_std(worker.recv())
    assert a[4] == b[4] == 5                      # same transaction
    assert a[0] != b[0]                           # another stream id
    assert server.stats()["streams_live"] == 1    # the other stream is not real
    assert worker.rpc(ext.cs(b[0]), 0.15) is None  # its CS is an unknown stream, ignored
    st = Stream(worker, a[0], a[1], 1, (2,), 5)
    st.request(1, FRAME9)
    assert st.reply() is not None


# ---- data modes -----------------------------------------------------------------

def test_data_silent_counts_requests_but_never_replies(worker, server):
    server.set_modes(data="silent")
    st = worker.stream(1, [2])
    st.request(1)
    assert st.reply(0.2) is None
    assert server.stats()["counters"]["data_silenced"] == 1
    server.set_modes(data="echo")
    st.request(2)
    assert st.reply() is not None


def test_data_wrong_source_comes_from_another_port_of_the_same_ip(worker, server):
    server.set_modes(data="wrong_source")
    st = worker.stream(1, [2, 3])
    frame = st.request(5, FRAME9)
    data, src = st.reply()
    assert src[0] == HOST and src[1] != st.port
    assert data == ext.expected_reply(1, [2, 3], 5, frame)   # content is right, only the source is wrong


def test_data_delay(worker, server):
    server.set_modes(data="delay:250")
    st = worker.stream(1, [2])
    t0 = time.monotonic()
    st.request(1)
    assert st.reply(0.1) is None
    assert st.reply(1.0) is not None
    assert time.monotonic() - t0 >= 0.24


def test_data_delay_keeps_order_of_equal_delays(worker, server):
    server.set_modes(data="delay:100")
    st = worker.stream(1, [2])
    for pid in range(10):
        st.request(pid)
    assert [st.reply()[0][1] for _ in range(10)] == list(range(10))


def test_data_wrong_codec(worker, server):
    server.set_modes(data="wrong_codec")
    st = worker.stream(1, [2, 3])
    st.request(4)
    data, _ = st.reply()
    assert data[0] != 2 and 1 <= data[0] <= 5 and len(data) == 20
    with pytest.raises(ValueError):
        ext.parse_data_reply(data, [2, 3])


def test_data_trailing_bytes(worker, server):
    server.set_modes(data="trailing_bytes:3")
    st = worker.stream(1, [2, 3])
    frame = st.request(4)
    data, _ = st.reply()
    assert len(data) == 23 and data[:20] == ext.expected_reply(1, [2, 3], 4, frame)
    with pytest.raises(ValueError):
        ext.parse_data_reply(data, [2, 3])


def test_data_truncated_after_complete_entry(worker, server):
    server.set_modes(data="truncated_after_complete_entry")
    st = worker.stream(1, [2, 3, 4])
    frame = st.request(4)
    data, _ = st.reply()
    full = ext.expected_reply(1, [2, 3, 4], 4, frame)
    assert data == full[:11]
    pid, entries = ext.parse_data_reply(data, [2, 3, 4], allow_cut=True)
    assert pid == 4 and len(entries) == 1
    server.set_modes(data="truncated_after_complete_entry:2")
    st.request(5)
    data, _ = st.reply()
    assert len(data) == ext.reply_size([2, 3])
    # one output: the only entry is complete, nothing to cut
    one = worker.stream(1, [5])
    one.request(6)
    data, _ = one.reply()
    assert len(data) == ext.reply_size([5])


# ---- caps modes ------------------------------------------------------------------

def test_caps_delay(worker, server):
    server.set_modes(caps="delay:300")
    t0 = time.monotonic()
    worker.send(ext.caps_request())
    assert worker.recv(0.15) is None
    assert ext.parse_caps_reply(worker.recv(1.0)) == (1, 4, 0x3E)
    assert time.monotonic() - t0 >= 0.29
    server.set_modes(caps="normal")
    assert worker.rpc(ext.caps_request()) is not None


def test_caps_drop_n_then_normal(worker, server):
    server.set_modes(caps="drop:2")
    assert worker.rpc(ext.caps_request(), 0.15) is None
    assert worker.rpc(ext.caps_request(), 0.15) is None
    assert worker.rpc(ext.caps_request()) is not None
    assert server.stats()["counters"]["caps_dropped"] == 2


def test_caps_drop_all_until_changed_back(worker, server):
    server.set_modes(caps="drop")
    for _ in range(4):
        assert worker.rpc(ext.caps_request(), 0.1) is None
    server.set_modes(caps="normal")
    assert worker.rpc(ext.caps_request()) is not None


def test_caps_changed_gives_another_valid_value(worker, server):
    first = ext.parse_caps_reply(worker.rpc(ext.caps_request()))
    server.set_modes(caps="changed")
    second = ext.parse_caps_reply(worker.rpc(ext.caps_request()))
    assert second != first and ext.caps_reply_valid(*second) and second[1] == 3
    assert ext.parse_caps_reply(worker.rpc(ext.caps_request())) == second


def test_pong_silent(worker, server):
    server.set_modes(pong="silent")
    assert worker.rpc(ext.ping("A"), 0.15) is None
    server.set_modes(pong="normal")
    assert worker.rpc(ext.ping("A")) == b"AMBEDPONG"


# ---- log, counters, reset --------------------------------------------------------

def test_log_records_every_datagram_in_order(worker, server):
    worker.rpc(ext.ping("A"))
    worker.rpc(ext.caps_request())
    st = worker.stream(1, [2])
    st.request(1, FRAME9)
    st.reply()
    st.close()
    assert wait_for(lambda: server.count_of("cs") == 1)
    log = server.log_entries()
    kinds = [(e["dir"], e["kind"]) for e in log]
    assert kinds == [("rx", "ping"), ("tx", "pong"), ("rx", "caps_request"), ("tx", "caps_reply"),
                     ("rx", "open"), ("tx", "std"), ("rx", "data_request"), ("tx", "data_reply"),
                     ("rx", "cs")]
    assert [e["seq"] for e in log] == list(range(1, 10))
    assert all(a["t"] <= b["t"] for a, b in zip(log, log[1:]))
    assert log[0]["data"] == ext.ping("A") and log[5]["data"] == ext.std(*ext.parse_std(log[5]["data"]))
    assert log[6]["port"] == st.port and log[0]["port"] == server.port
    # since filters
    cut = log[3]["t"]
    assert [e["seq"] for e in server.log_entries(since=cut)][0] == 5
    assert [e["seq"] for e in server.log_entries(since_seq=7)] == [8, 9]
    assert [e["kind"] for e in server.log_entries(kinds=["open", "std"])] == ["open", "std"]


def test_log_is_bounded():
    with FakeAmbedServer(HOST, port=0, control_port=None, max_log=5) as srv:
        w = Worker(srv)
        try:
            for _ in range(10):
                w.rpc(ext.ping("A"))
        finally:
            w.close()
        assert len(srv.log_entries()) == 5 and srv.stats()["counters"]["log_dropped"] > 0


def test_reset_restores_defaults_and_closes_streams(worker, server):
    server.set_modes(open="busy", data="silent", max_sessions=1, caps="drop")
    server.set_modes(open="std", max_sessions=None, caps="normal")
    st = worker.stream(1, [2])
    server.set_modes(data="delay:50", open="silent")
    server.reset()
    stats = server.stats()
    assert stats["streams_live"] == 0 and stats["counters"] == {} and stats["log_size"] == 0
    assert stats["modes"]["open"] == "std" and stats["modes"]["data"] == "echo"
    time.sleep(0.15)
    st.request(1)
    assert st.reply(0.15) is None                         # its data socket is closed
    assert worker.rpc(ext.ping("A")) == b"AMBEDPONG"


def test_set_modes_is_atomic_and_validates(server):
    before = dict(server.modes)
    for bad in ({"open": "nope"}, {"data": "echo", "bogus": 1}, {"data_delay_ms": -1},
                {"max_sessions": "3"}, {"open": "std:x"}, {"open": "busy:5"}, {"len_check": "x"},
                {"caps_drop_n": 0}):
        with pytest.raises(ValueError):
            server.set_modes(**bad)
        assert server.modes == before


def test_stop_releases_ports_and_threads():
    srv = FakeAmbedServer(HOST, port=0, control_port=0).start()
    w = Worker(srv)
    st = w.stream(1, [2])
    port, ctl = srv.port, srv.control_port
    w.close()
    srv.stop()
    for p in (port, ctl, st.port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((HOST, p))
        s.close()
    assert not [t for t in srv.threads if t.is_alive()]
