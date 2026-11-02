# SPDX-License-Identifier: MIT
"""dvtest/tools/webload_gen.py: the parts that need neither aiortc nor a network (the listener's RTP reader and
the procfs readers)."""
import json
import struct

import pytest

from dvtest import webload as wl
from dvtest.tools import webload_gen as gen

pytestmark = pytest.mark.unit


def rtp(payload, seq=1, pt=0, ts=160, ssrc=0x11223344, cc=0, ext=None, pad=0, version=2):
    b0 = (version << 6) | (0x20 if pad else 0) | (0x10 if ext is not None else 0) | cc
    head = struct.pack(">BBHII", b0, pt, seq, ts, ssrc) + b"\0\0\0\0" * cc
    if ext is not None:
        head += struct.pack(">HH", 0xBEDE, len(ext) // 4) + ext
    tail = bytes(pad - 1) + bytes([pad]) if pad else b""
    return head + payload + tail


def listener():
    return gen.Listener({"j": 3, "room": "A", "login": 0}, wl.Plan(listeners=1))


def test_listener_reads_marker_packets():
    lst = listener()
    for i in (7, 8, 9):
        lst.on_rtp(1000 * i, rtp(wl.expected_pcmu(i), seq=i))
    assert list(lst.idx) == [7, 8, 9] and list(lst.t) == [7000, 8000, 9000] and list(lst.rtp_seq) == [7, 8, 9]
    assert lst.invalid == 0 and lst.packets == 3 and lst.tail_ok and lst.first_ns == 7000


def test_listener_reads_through_csrc_extension_and_padding():
    lst = listener()
    lst.on_rtp(1, rtp(wl.expected_pcmu(42), cc=2, ext=bytes(range(1, 9)), pad=4))
    assert list(lst.idx) == [42] and lst.invalid == 0


@pytest.mark.parametrize("pkt", [
    b"\x80\x00\x00",                                          # shorter than a header
    rtp(wl.expected_pcmu(1), version=1),                      # not RTP version 2
    rtp(wl.expected_pcmu(1), pt=8),                           # PCMA, not PCMU
    rtp(wl.expected_pcmu(1)[:-1]),                            # 159 bytes
    rtp(bytes(161)),                                          # 161 bytes
    rtp(bytes([127]) + wl.expected_pcmu(1)[1:]),              # a digit out of range
    rtp(wl.expected_pcmu(1), ext=b"\0" * 8)[:16],             # extension cut short
])
def test_listener_counts_invalid(pkt):
    lst = listener()
    lst.on_rtp(1, pkt)
    assert lst.invalid == 1 and len(lst.idx) == 0


def test_listener_flags_a_different_tail_but_keeps_the_marker():
    lst = listener()
    lst.on_rtp(1, rtp(wl.expected_pcmu(5)[:4] + b"\x55" * 156))
    assert list(lst.idx) == [5] and lst.tail_ok is False


def test_listener_is_bounded():
    lst = listener()
    for i in range(gen.MAX_PACKETS + 50):
        lst.on_rtp(i, rtp(wl.expected_pcmu(i % 3000)))
    assert len(lst.t) == gen.MAX_PACKETS and lst.packets == gen.MAX_PACKETS + 50


def test_proc_readers():
    ticks, rss = gen.read_proc_ticks()
    assert ticks is not None and ticks >= 0 and rss > 0
    assert gen.read_proc_ticks(99999999) == (None, None)
    cg = gen.read_cgroup()
    assert isinstance(cg, dict)


def test_server_refuses_a_second_run_and_too_few_accounts(monkeypatch):
    server = gen.Server(set())
    with pytest.raises(ValueError):
        server.start({"listeners": 200, "accounts": [["N0CALL", "x"]], "base_url": "https://x", "ca": "/x"})
    with pytest.raises(ValueError):
        server.start({"listeners": 201, "accounts": [["N0CALL", "x"]] * 9, "base_url": "https://x", "ca": "/x"})
    with pytest.raises(RuntimeError):
        server.finish()


def test_relay_sdp_keeps_only_the_sha256_fingerprint():
    fp = ":".join(["AB"] * 32)
    sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=fingerprint:sha-256 %s\r\na=fingerprint:sha-384 %s\r\na=fingerprint:sha-512 CD\r\na=setup:actpass\r\n" % (fp, fp)
    out = gen.relay_sdp(sdp)
    assert out.count("a=fingerprint:") == 1 and "a=fingerprint:sha-256 " + fp in out
    assert out.startswith("v=0\r\n") and out.endswith("a=setup:actpass\r\n") and "\n\n" not in out


# ---- soak mode (dvtest/soak.py sessions: join, leave, no arrival log)

def test_soak_listener_counts_packets_and_keeps_no_log():
    lst = gen.Listener({"j": 1, "room": "A", "login": 0}, wl.Plan(listeners=1), counts_only=True)
    for i in range(500):
        lst.on_rtp(i, rtp(bytes(160), seq=i))
    lst.on_rtp(1, rtp(bytes(160), pt=8))
    assert lst.packets == 501 and lst.invalid == 1 and len(lst.t) == 0 and len(lst.idx) == 0


class FakePopen:
    made = []

    def __init__(self, argv, stdin=None, stdout=None, text=None, bufsize=None):
        import io
        self.argv, self.stdin, self.stdout, self.written = argv, io.StringIO(), io.StringIO(""), None
        self.pid = 4000 + len(FakePopen.made)
        FakePopen.made.append(self)

    def poll(self):
        return 0


def test_soak_run_hands_whole_logins_to_workers_and_validates_the_sessions(monkeypatch):
    from dvtest import soak
    FakePopen.made = []
    monkeypatch.setattr(gen.subprocess, "Popen", FakePopen)
    sessions = soak.build_sessions(1, 900)
    accounts = [["N0LD%02d" % i, "x"] for i in range(5)]
    req = {"listeners": 100, "workers": 2, "accounts": accounts, "base_url": "https://x", "ca": "/x", "soak": {"sessions": sessions}}
    server = gen.Server(set())
    answer = server.start(req)
    assert answer["workers"] == 2 and len(FakePopen.made) == 2
    cfgs = []
    for proc in FakePopen.made:
        cfgs.append(json.loads(proc.stdin.getvalue().splitlines()[0]))
    assert all(c["soak"] is True for c in cfgs)
    assert sorted(s["j"] for c in cfgs for s in c["slots"]) == [s["j"] for s in sessions]
    assert {s["login"] % 2 for s in cfgs[0]["slots"]} == {0} and {s["login"] % 2 for s in cfgs[1]["slots"]} == {1}
    assert any(s["leave"] is not None for c in cfgs for s in c["slots"])
    # a second run while one is going is refused, and so are sessions that break the budgets
    with pytest.raises(RuntimeError):
        server.start(req)
    crowded = [dict(s, offset=0.0) for s in sessions[:30]]
    with pytest.raises(ValueError):
        gen.Server(set()).start(dict(req, soak={"sessions": crowded}))


def test_soak_job_status_counts_live_sessions():
    job = gen.Job({"listeners": 100})
    job.connected.update({1: 1.0, 2: 1.2, 3: 0.9})
    gen.Server.reader(job, 0, type("P", (), {"stdout": iter(['{"ev":"left","j":2}\n', "junk\n", '{"ev":"failed","j":4,"why":"x"}\n']),
                                             "poll": lambda self: 0})())
    st = job.status()
    assert st["connected"] == 3 and st["left"] == 1 and st["live"] == 2 and st["failed"] == 1


def test_soak_worker_result_is_compact():
    cfg = {"plan": {"listeners": 100}, "slots": [], "accounts": [], "start_mono": 0.0, "soak": True, "threads": 1}
    worker = gen.Worker(cfg)
    for j in (2, 1):
        lst = gen.Listener({"j": j, "seat": j, "room": "B", "login": 0}, worker.plan, counts_only=True)
        lst.packets, lst.left = 10 * j, j == 1
        worker.listeners.append(lst)
    res = worker.result()
    assert [r["j"] for r in res["rows"]] == [1, 2] and res["rows"][0]["left"] is True and res["rows"][1]["packets"] == 20
    assert set(res["rows"][0]) == {"j", "seat", "room", "login", "connect_s", "packets", "invalid", "left", "error", "alive"}
    assert res["loop_lag"]["samples"] == 0


def test_soak_release_revokes_closes_and_forgets_the_peer():
    import asyncio
    cfg = {"plan": {"listeners": 100}, "slots": [], "accounts": [], "start_mono": 0.0, "soak": True, "threads": 1}
    worker = gen.Worker(cfg)
    calls, events = [], []

    class Client:
        def request(self, method, path, json_body=None, pace=True):
            calls.append((method, path, json_body))
            return type("R", (), {"status": 204})()

    class Pc:
        closed = False

        async def close(self):
            self.closed = True

    pc = Pc()
    worker.pcs.append(pc)
    lst = gen.Listener({"j": 5, "seat": 5, "room": "A", "login": 0}, worker.plan, counts_only=True)
    lst.sess_id = "ab" * 8
    worker.emit = lambda **ev: events.append(ev)
    asyncio.run(worker.release(lst, Client(), pc))
    assert calls == [("POST", gen.CLOSE_PATH, {"sess_id": "ab" * 8})] and pc.closed and worker.pcs == []
    assert lst.left and lst.client is None and events == [{"ev": "left", "j": 5}]
