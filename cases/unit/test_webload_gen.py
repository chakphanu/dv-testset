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


def test_late_bins_marks_a_stall_seen_by_every_listener():
    import time
    import types
    plan = wl.Plan(listeners=2)
    first, end = plan.window
    lsts = []
    for j in range(2):
        lst = gen.Listener({"j": j, "room": "A", "login": 0}, plan)
        base = time.perf_counter_ns()
        t = base
        for idx in range(first, first + 20):
            # frame 10 arrives 12 ms late, frame 11 right behind it (deviations 12 ms and 12 ms)
            lst.t.append(t + (12_000_000 if idx == first + 10 else 0))
            lst.idx.append(idx)
            t += 20_000_000
        lsts.append(lst)
    fake = types.SimpleNamespace(listeners=lsts, plan=plan)
    bins = gen.Worker.late_bins(fake)
    assert sum(n for n, _dev in bins.values()) == 4          # two spacings per listener, two listeners
    assert all(dev >= 11.9 for _n, dev in bins.values())


# ---------------------------------------------------------------- offer retries and slow requests (WEBSTALL1)

def test_capacity_refusal_and_retry_delay():
    assert gen.is_capacity_refusal(503, '{"detail":"WebRTC capacity reached"}')
    assert not gen.is_capacity_refusal(503, '{"detail":"the edge does not answer"}')
    assert not gen.is_capacity_refusal(429, '{"detail":"WebRTC capacity reached"}')
    assert not gen.is_capacity_refusal(200, "")
    assert gen.retry_delay({"retry-after": "5"}) == 5.0
    assert gen.retry_delay({"retry-after": "0"}) == gen.RETRY_MIN_S and gen.retry_delay({}) == gen.RETRY_MIN_S
    assert gen.retry_delay({"retry-after": "soon"}) == gen.RETRY_MIN_S and gen.retry_delay(None) == gen.RETRY_MIN_S


NEW_BODY = json.dumps({"type": "https://example.invalid/problems/unavailable", "code": "unavailable", "detail": "WebRTC join window capacity reached"})
OLD_BODY = json.dumps({"code": "unavailable", "detail": "WebRTC capacity reached"})


def test_capacity_refusal_is_judged_by_status_code_and_retry_after_in_both_shapes():
    for body in (NEW_BODY, OLD_BODY):
        assert gen.is_capacity_refusal(503, body, {"retry-after": "5"})
    # the detail text does not decide: a renamed limit is still a capacity refusal, a capacity-looking text on another code is not
    assert gen.is_capacity_refusal(503, json.dumps({"code": "unavailable", "detail": "no room"}), {"retry-after": "5"})
    assert not gen.is_capacity_refusal(503, json.dumps({"code": "rate_limited", "detail": "WebRTC capacity reached"}), {"retry-after": "5"})
    assert not gen.is_capacity_refusal(503, NEW_BODY, {})                       # a 503 unavailable with no Retry-After is not a capacity refusal
    assert not gen.is_capacity_refusal(429, NEW_BODY, {"retry-after": "5"})


class Reply:
    def __init__(self, status, text="", headers=None):
        self.status, self._text, self.headers = status, text, headers or {}

    def text(self):
        return self._text


def offer_worker(monkeypatch, replies):
    import asyncio
    cfg = {"plan": {"listeners": 100}, "slots": [], "accounts": [], "start_mono": 0.0, "threads": 1}
    worker = gen.Worker(cfg)
    events, sleeps, sent = [], [], []
    worker.emit = lambda **ev: events.append(ev)

    async def no_pace(slot):
        pass

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    class Client:
        def request(self, method, path, json_body=None, pace=True):
            sent.append((method, path, pace))
            return replies.pop(0)

    worker.pace = no_pace
    monkeypatch.setattr(gen.asyncio, "sleep", fake_sleep)
    lst = gen.Listener({"j": 9, "room": "A", "login": 0}, worker.plan)

    def run():
        return asyncio.run(worker.offer({"room": "A", "login": 0}, lst, Client(), "v=0"))
    return run, lst, events, sleeps, sent


FULL = '{"detail":"WebRTC capacity reached"}'


def test_a_capacity_refusal_is_sent_again_after_the_retry_after(monkeypatch):
    run, lst, events, sleeps, sent = offer_worker(monkeypatch, [Reply(503, FULL, {"retry-after": "5"}), Reply(503, FULL, {}), Reply(200, "{}")])
    reply, _t = run()
    assert reply.status == 200 and lst.retries == 2 and len(sent) == 3 and all(not pace for _m, _p, pace in sent)
    assert sleeps == [5.0, gen.RETRY_MIN_S]
    assert [(e["ev"], e["n"]) for e in events] == [("retry", 1), ("retry", 2)]


def test_after_three_retries_the_refusal_is_final(monkeypatch):
    run, lst, events, sleeps, sent = offer_worker(monkeypatch, [Reply(503, FULL, {}) for _ in range(5)])
    reply, _t = run()
    assert reply.status == 503 and lst.retries == gen.OFFER_RETRIES == 3 and len(sent) == 4 and len(sleeps) == 3


@pytest.mark.parametrize("reply", [Reply(429, FULL), Reply(503, '{"detail":"the edge does not answer"}'), Reply(401, "")])
def test_other_refusals_are_not_retried(monkeypatch, reply):
    run, lst, events, sleeps, sent = offer_worker(monkeypatch, [reply])
    got, _t = run()
    assert got is reply and lst.retries == 0 and len(sent) == 1 and sleeps == [] and events == []


def test_the_job_counts_retries_exhausted_offers_and_slow_requests():
    import io
    job = gen.Job({"listeners": 200})
    lines = [{"ev": "retry", "j": 4, "n": 1}, {"ev": "retry", "j": 4, "n": 2}, {"ev": "retry", "j": 7, "n": 1}, {"ev": "exhausted", "j": 7, "tries": 3},
             {"ev": "slow", "m": "POST", "p": "/api/v1/rtc/offer", "d": 5.57, "t": 1000.5}, {"ev": "failed", "j": 7, "why": "offer HTTP 503"}]

    class Proc:
        stdout = io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n")

        def poll(self):
            return 0

    gen.Server.reader(job, 0, Proc())
    st = job.status()
    assert st["retried"] == 2 and st["retries"] == 3 and st["exhausted"] == 1 and st["failed"] == 1
    assert st["slow_count"] == 1 and st["slow"] == [{"m": "POST", "p": "/api/v1/rtc/offer", "d": 5.57, "t": 1000.5}]


def test_the_job_keeps_at_most_slow_keep_slow_requests_but_counts_all():
    import io
    job = gen.Job({"listeners": 1})
    n = gen.SLOW_KEEP + 30
    text = "".join(json.dumps({"ev": "slow", "m": "GET", "p": "/x", "d": 1.5, "t": float(i)}) + "\n" for i in range(n))

    class Proc:
        stdout = io.StringIO(text)

        def poll(self):
            return 0

    gen.Server.reader(job, 0, Proc())
    st = job.status()
    assert st["slow_count"] == n and len(st["slow"]) == gen.SLOW_KEEP


def test_the_timed_client_reports_only_requests_slower_than_the_limit(monkeypatch):
    import time as _time
    from dvtest import web
    seen = []

    def fake_request(self, method, path, json_body=None, headers=None, expect=None, pace=True, raw_body=None):
        assert pace is False                                 # the client side pacing is waited for before the clock starts
        _time.sleep(0.12 if path.startswith("/slow") else 0.0)
        return "reply"

    monkeypatch.setattr(web.WebClient, "request", fake_request)
    monkeypatch.setattr(web.Pacer, "wait", lambda self, kind: None, raising=False)
    monkeypatch.setattr(gen, "SLOW_S", 0.05)
    client = gen.timed_client_class()("https://dvt-web", None)
    client.slow_sink = lambda *a: seen.append(a)
    assert client.request("GET", "/fast") == "reply" and seen == []
    assert client.request("POST", "/slow/offer?x=1", json_body={}) == "reply"
    assert len(seen) == 1 and seen[0][0] == "POST" and seen[0][1] == "/slow/offer" and seen[0][2] >= 0.1


def test_a_new_shape_refusal_is_retried_and_its_detail_is_kept(monkeypatch):
    run, lst, events, sleeps, sent = offer_worker(monkeypatch, [Reply(503, NEW_BODY, {"retry-after": "5"}), Reply(200, "{}")])
    reply, _t = run()
    assert reply.status == 200 and lst.retries == 1 and sleeps == [5.0]
    assert events == [{"ev": "retry", "j": 9, "n": 1, "wait": 5.0, "detail": "WebRTC join window capacity reached"}]


def test_a_503_unavailable_without_retry_after_is_not_retried(monkeypatch):
    run, lst, events, sleeps, sent = offer_worker(monkeypatch, [Reply(503, NEW_BODY, {})])
    got, _t = run()
    assert got.status == 503 and lst.retries == 0 and sleeps == [] and events == []


def test_the_job_counts_the_details_of_the_retried_refusals():
    import io
    job = gen.Job({"listeners": 4})
    lines = [{"ev": "retry", "j": 1, "n": 1, "detail": "WebRTC join window capacity reached"},
             {"ev": "retry", "j": 2, "n": 1, "detail": "WebRTC join window capacity reached"},
             {"ev": "retry", "j": 2, "n": 2, "detail": "WebRTC peer slot capacity reached"}, {"ev": "retry", "j": 3, "n": 1}]

    class Proc:
        stdout = io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n")

        def poll(self):
            return 0

    gen.Server.reader(job, 0, Proc())
    st = job.status()
    assert st["refused_details"] == {"WebRTC join window capacity reached": 2, "WebRTC peer slot capacity reached": 1}
    assert st["retries"] == 4 and st["retried"] == 3


def test_plan_offsets_paced_or_all_at_once():
    assert gen.plan_offsets(4, burst=True) == [0.0, 0.0, 0.0, 0.0]
    paced = gen.plan_offsets(8)
    assert paced[:4] == [0.0] * 4 and paced[4] > 0 and paced == sorted(paced)
    assert gen.plan_offsets(0, burst=True) == []
