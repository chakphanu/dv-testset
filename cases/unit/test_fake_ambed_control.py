# SPDX-License-Identifier: MIT
"""JSON control socket and command line entry point of the fake AMBEd."""
import json
import os
import pathlib
import select
import signal
import socket
import subprocess
import sys
import time

import pytest

from dvtest import fake_ambed
from dvtest.fake_ambed import FakeAmbedServer
from dvtest.proto import ambed_ext as ext

HOST = "127.0.0.1"


class Ctl:
    def __init__(self, server):
        self.dest = (HOST, server.control_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, 0))
        self.sock.settimeout(2.0)

    def raw(self, data):
        self.sock.sendto(data, self.dest)
        return json.loads(self.sock.recvfrom(65535)[0])

    def op(self, **req):
        return self.raw(json.dumps(req).encode())

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    srv = FakeAmbedServer(HOST, port=0, control_port=0).start()
    yield srv
    srv.stop()


@pytest.fixture
def ctl(server):
    c = Ctl(server)
    yield c
    c.close()


def worker_sock(server):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((HOST, 0))
    s.settimeout(1.0)
    return s


def test_set_changes_modes_and_returns_them(server, ctl):
    reply = ctl.op(op="set", open="busy", data="delay", data_delay_ms=40, max_sessions=3)
    assert reply["ok"] is True
    assert reply["modes"]["open"] == "busy" and reply["modes"]["data_delay_ms"] == 40
    assert server.modes["max_sessions"] == 3
    # unspecified keys stay
    assert ctl.op(op="set", open="std")["modes"]["data"] == "delay"
    # shorthand with a number
    assert ctl.op(op="set", open="late:300")["modes"]["open_delay_ms"] == 300


def test_set_errors_are_objects_and_apply_nothing(server, ctl):
    for req in ({"op": "set", "open": "nope"}, {"op": "set", "bogus": 1},
                {"op": "set", "open": "busy", "max_sessions": "x"}):
        reply = ctl.op(**req)
        assert reply["ok"] is False and reply["error"]
    assert server.modes["open"] == "std"


def test_unknown_op_and_bad_requests(ctl):
    assert ctl.op(op="frobnicate") == {"ok": False, "error": "unknown op: frobnicate"}
    assert ctl.raw(b"not json")["ok"] is False
    assert ctl.raw(b"[1, 2]")["ok"] is False
    assert ctl.raw(b'{"no_op": 1}')["ok"] is False
    assert ctl.raw(b'{"op": 7}')["ok"] is False
    assert ctl.raw(b"\xff\xfe")["ok"] is False
    assert ctl.op(op="stats")["ok"] is True          # still serving


def test_stats_counters_and_streams(server, ctl):
    s = worker_sock(server)
    try:
        s.sendto(b"", (HOST, server.port))                  # junk: counted, no reply
        s.sendto(ext.ping("A"), (HOST, server.port))
        s.recvfrom(100)
        s.sendto(ext.open_request("N0TEST", 1, [2, 3], 77), (HOST, server.port))
        sid, port, *_ = ext.parse_std(s.recvfrom(100)[0])
        stats = ctl.op(op="stats")
        assert stats["ok"] and stats["streams_live"] == 1
        assert stats["streams"][0]["id"] == sid and stats["streams"][0]["port"] == port
        assert stats["streams"][0]["outs"] == [2, 3] and stats["streams"][0]["transaction"] == 77
        assert stats["counters"]["rx_ping"] == 1 and stats["counters"]["tx_std"] == 1
        assert stats["counters"]["rx_unknown"] == 1          # the empty datagram
        assert stats["caps"] == {"version": 1, "max_outputs": 4, "mask": 0x3E}
        assert stats["modes"]["open"] == "std"
    finally:
        s.close()


def test_log_op_hex_since_limit_kinds(server, ctl):
    s = worker_sock(server)
    try:
        for _ in range(3):
            s.sendto(ext.ping("A"), (HOST, server.port))
            s.recvfrom(100)
        full = ctl.op(op="log")
        assert full["ok"] and full["more"] is False
        kinds = [(e["dir"], e["kind"]) for e in full["entries"] if e["kind"] != "unknown"]
        assert kinds == [("rx", "ping"), ("tx", "pong")] * 3
        assert full["entries"][0]["hex"] == ext.ping("A").hex()
        assert full["entries"][0]["peer"].startswith(HOST + ":")
        mid = full["entries"][3]["t"]
        later = ctl.op(op="log", since=mid)
        assert [e["seq"] for e in later["entries"]] == [e["seq"] for e in full["entries"] if e["t"] > mid]
        assert ctl.op(op="log", since_seq=full["entries"][-2]["seq"])["entries"][0]["kind"] == "pong"
        assert ctl.op(op="log", kinds=["pong"])["entries"][0]["kind"] == "pong"
        limited = ctl.op(op="log", limit=2)
        assert len(limited["entries"]) == 2 and limited["more"] is True
        assert limited["last_seq"] == limited["entries"][-1]["seq"]
        assert ctl.op(op="log", since="x")["ok"] is False
        assert ctl.op(op="log", kinds="ping")["ok"] is False
    finally:
        s.close()


def test_log_reply_stays_below_one_datagram(server, ctl):
    s = worker_sock(server)
    try:
        s.sendto(ext.open_request("N0TEST", 1, [2, 3, 4, 5], 1), (HOST, server.port))
        _sid, port, *_ = ext.parse_std(s.recvfrom(100)[0])
        d = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        d.bind((HOST, 0))
        d.settimeout(1.0)
        for pid in range(80):                       # 80 x 353 B replies: far over 64 KB of hex
            d.sendto(ext.data_request(1, pid, bytes(9)), (HOST, port))
            d.recvfrom(4096)
        d.close()
        reply = ctl.op(op="log", kinds=["data_reply"])
        assert reply["ok"] and reply["more"] is True and 0 < len(reply["entries"]) < 80
        rest = ctl.op(op="log", kinds=["data_reply"], since_seq=reply["last_seq"])
        assert rest["entries"][0]["seq"] > reply["last_seq"]
    finally:
        s.close()


def test_reset_op_clears_everything(server, ctl):
    ctl.op(op="set", open="busy", data="silent")
    s = worker_sock(server)
    try:
        s.sendto(ext.ping("A"), (HOST, server.port))
        s.recvfrom(100)
    finally:
        s.close()
    t_before = ctl.op(op="stats")["now"]
    reply = ctl.op(op="reset")
    assert reply["ok"] and reply["modes"]["open"] == "std" and reply["now"] >= t_before
    stats = ctl.op(op="stats")
    assert stats["counters"]["ctl_ops"] == 1 and stats["log_size"] == 0   # only this stats op
    assert stats["modes"] == fake_ambed.default_modes()


def test_end_to_end_mode_switch_per_case(server, ctl):
    s = worker_sock(server)
    try:
        ctl.op(op="set", open="busy")
        s.sendto(ext.open_request("N0TEST", 1, [2], 3), (HOST, server.port))
        assert s.recvfrom(100)[0] == ext.busy(3)
        ctl.op(op="reset")
        s.sendto(ext.open_request("N0TEST", 1, [2], 3), (HOST, server.port))
        assert ext.parse_std(s.recvfrom(100)[0])[4] == 3
    finally:
        s.close()


def test_control_port_off_and_same_ip():
    with FakeAmbedServer(HOST, port=0, control_port=None) as srv:
        assert srv.control_port is None
    with FakeAmbedServer(HOST, port=0, control_port=0) as srv:
        assert srv.control_port not in (None, srv.port)
        assert srv.csock.getsockname()[0] == HOST


# ---- command line ---------------------------------------------------------------

def _read_line(proc, timeout=5.0):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    assert ready, "no output within %.0f s" % timeout
    return proc.stdout.readline().strip()


def test_cli_logs_one_line_per_session_and_stops_on_sigterm():
    root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "dvtest.fake_ambed", "--bind", HOST, "--port", "0",
         "--control-port", "0", "--data-ports", "0", "--max-sessions", "1"],
        cwd=str(root), env=env, stdout=subprocess.PIPE, text=True)
    try:
        line = _read_line(proc)
        assert line.startswith("fake ambed listening on %s:" % HOST)
        port = int(line.split(":")[1].split()[0])
        control = int(line.split("control")[1])
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((HOST, 0))
        s.settimeout(2.0)
        s.sendto(ext.open_request("N0TEST", 1, [2, 3], 0xABCDEF01), (HOST, port))
        sid, dport, *_ = ext.parse_std(s.recvfrom(100)[0])
        assert _read_line(proc) == "session open id=%d port=%d in=1 outs=2,3 tx=abcdef01" % (sid, dport)
        s.sendto(ext.open_request("N0TEST", 1, [2], 5), (HOST, port))       # max sessions 1
        assert s.recvfrom(100)[0] == ext.busy(5)
        s.sendto(b'{"op":"stats"}', (HOST, control))
        assert json.loads(s.recvfrom(65535)[0])["streams_live"] == 1
        s.sendto(ext.cs(sid), (HOST, port))
        assert _read_line(proc) == "session close id=%d port=%d" % (sid, dport)
        s.close()
        proc.send_signal(signal.SIGTERM)
        assert _read_line(proc) == "fake ambed stopped"
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def test_cli_requires_bind(monkeypatch):
    monkeypatch.delenv("TRANSCODER_IP", raising=False)
    with pytest.raises(SystemExit) as exc:
        fake_ambed.main([])
    assert exc.value.code == 2


def test_scheduler_runs_in_due_order_and_stops():
    sched = fake_ambed._Scheduler()
    sched.start()
    seen = []
    sched.after(0.10, lambda: seen.append("b"))
    sched.after(0.02, lambda: seen.append("a"))
    time.sleep(0.3)
    sched.after(5, lambda: seen.append("never"))
    sched.stop()
    sched.join(timeout=2)
    assert seen == ["a", "b"] and not sched.is_alive()


def test_cli_data_port_options(monkeypatch):
    seen = {}

    class Stub:
        port = 1
        control_port = 2

        def __init__(self, *args, **kw):
            seen["args"], seen["kw"] = args, kw
            raise SystemExit(0)

    monkeypatch.setattr(fake_ambed, "FakeAmbedServer", Stub)
    with pytest.raises(SystemExit):
        fake_ambed.main(["--bind", HOST])
    assert seen["kw"]["data_ports"] == (10200, 10299) and seen["kw"]["lenient_open"] is False
    assert seen["args"][5] is None                      # no 10100 + id form by default
    with pytest.raises(SystemExit):
        fake_ambed.main(["--bind", HOST, "--data-ports", "41000-41009", "--lenient-open",
                         "--data-port-base", "10100"])
    assert seen["kw"]["data_ports"] == (41000, 41009) and seen["kw"]["lenient_open"] is True
    assert seen["args"][5] == 10100
    with pytest.raises(SystemExit):
        fake_ambed.main(["--bind", HOST, "--data-ports", "0"])
    assert seen["kw"]["data_ports"] is None


def test_cli_rejects_bad_data_ports():
    for bad in ("abc", "20-10", "0-5"):
        with pytest.raises(SystemExit) as exc:
            fake_ambed.main(["--bind", HOST, "--data-ports", bad])
        assert exc.value.code == 2


def test_json_set_lenient_open(server):
    c = Ctl(server)
    try:
        assert c.op(op="stats")["modes"]["lenient_open"] is False
        assert c.op(op="set", lenient_open=True)["ok"] is True
        assert c.op(op="set", lenient_open="yes")["ok"] is False
        assert c.op(op="reset")["modes"]["lenient_open"] is False
    finally:
        c.close()
