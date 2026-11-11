# SPDX-License-Identifier: MIT
"""cases/websid_support.py: the verdicts of the WEBSID1 browser cases (no browser)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import websid_support as ws  # noqa: E402

pytestmark = pytest.mark.unit


def sse(**kw):
    base = {"opened": 1, "errors": 0, "names": {"hello": 1}, "bye": None, "t_bye": None, "t_error": None, "ready": 1}
    base.update(kw)
    return base


def test_sse_alive_problems():
    assert ws.sse_alive_problems(sse()) == []
    for change, needle in [(dict(opened=0), "never opened"), (dict(ready=2), "readyState"), (dict(errors=2), "error event"),
                           (dict(names={"hello": 1, "bye": 1}, bye='{"reason":"session ended"}'), "bye")]:
        problems = ws.sse_alive_problems(sse(**change))
        assert any(needle in p for p in problems), (change, problems)
    assert ws.sse_alive_problems(None) == ["the page has no SSE state"]


def test_sse_ended_and_end_time():
    assert not ws.sse_ended(sse()) and ws.sse_end_ms(sse()) is None
    assert ws.sse_ended(sse(names={"bye": 1}, t_bye=1500)) and ws.sse_end_ms(sse(names={"bye": 1}, t_bye=1500, t_error=1800)) == 1500
    closed = sse(ready=2, t_error=2000)
    assert ws.sse_ended(closed) and ws.sse_end_ms(closed) == 2000
    assert ws.sse_end_ms(sse(t_error=2000)) is None                      # an error while still OPEN (reconnecting) is not the end
    assert not ws.sse_ended(None) and ws.sse_end_ms(None) is None


def wav(**kw):
    base = {"status": 200, "length": 1000, "got": 1000, "done": True, "err": None}
    base.update(kw)
    return base


def test_wav_progress_problems_and_cut():
    assert ws.wav_progress_problems(wav(), True) == []
    assert any("truncated" in p for p in ws.wav_progress_problems(wav(got=900), True))
    assert any("not finished" in p for p in ws.wav_progress_problems(wav(done=False, got=10), True))
    assert any("read error" in p for p in ws.wav_progress_problems(wav(err="TypeError: network error"), True))
    assert ws.wav_progress_problems(wav(done=False, got=10), False) == []         # a running body is fine when completion is not required
    assert ws.wav_progress_problems(wav(status=429), False) == ["status 429, want 200"]
    assert ws.wav_progress_problems(None, True) == ["the page has no WAV state"]
    assert ws.wav_cut(wav(got=900)) and ws.wav_cut(wav(err="x", got=10, done=True)) and not ws.wav_cut(wav())
    assert not ws.wav_cut(wav(done=False, got=10)) and not ws.wav_cut(wav(status=503)) and not ws.wav_cut(None)
    assert ws.wav_fits_buffers(wav()) and not ws.wav_fits_buffers(wav(got=5)) and not ws.wav_fits_buffers(wav(err="x"))


def test_within_and_rotation_and_caps():
    assert ws.within(1100, 1000, 0.2) and not ws.within(1300, 1000, 0.2) and not ws.within(None, 1000, 5) and not ws.within(1000, None, 5)
    assert ws.rotation_problems("a", "b", "c", "d") == []
    assert ws.rotation_problems("a", "a", "c", "d") == ["the session cookie did not rotate"]
    assert ws.rotation_problems("a", "b", "c", "c") == ["the CSRF token did not rotate"]
    assert len(ws.rotation_problems(None, "b", "", "d")) == 2
    assert ws.cap_refusal([200] * 8 + [503], 8) == []                          # the ninth circuit is 503 (dv-router, a1ff890c)
    assert ws.cap_refusal([200] * 8 + [429], 8) != [] and ws.cap_refusal([200] * 7 + [503, 503], 8) != [] and ws.cap_refusal([200] * 9, 8) != []
    assert (ws.CIRCUIT_CAP, ws.CIRCUIT_REFUSED, ws.WAV_BODY_CAP, ws.WAV_REFUSED) == (8, 503, 2, 429)
    assert ws.circuit_problems({"connection": "connected"}, "connected") == []
    assert len(ws.circuit_problems({"connection": "failed"}, "failed")) == 2 and ws.circuit_problems(None, "connected")


def test_coalescing_problems():
    recs = [{"result": "denied"}, {"result": "denied", "suppressed": 5}]
    assert ws.coalescing_problems(recs[:1], 0, 1, False) == []
    assert ws.coalescing_problems(recs, 0, 2, True) == []
    assert any("want 1" in p for p in ws.coalescing_problems(recs, 0, 1, False))            # a second record inside the window
    assert any("suppressed" in p for p in ws.coalescing_problems([{"result": "denied"}, {"result": "denied"}], 0, 2, True))
    assert ws.coalescing_problems(recs, 1, 1, True) == []                                   # the first record belongs to the baseline
    assert any("suppressed" in p for p in ws.coalescing_problems([], 0, 0, True))


def test_javascript_snippets_are_self_consistent():
    for name in ("JS_SSE_START", "JS_SSE_STATE", "JS_STEP_UP", "JS_SESSION", "JS_OFFER_ONLY",
                 "JS_ALIVE_START", "JS_ALIVE_SET_CSRF", "JS_ALIVE_STATE", "JS_ICE", "JS_GET_STATUS", "JS_RTC_POST", "JS_REVOKE_OWN"):
        text = getattr(ws, name)
        assert text.count("{") == text.count("}") and text.count("(") == text.count(")"), name
    assert "'bye'" in ws.JS_SSE_START and "hello" in ws.JS_SSE_START
    assert "/api/v1/auth/session/step-up" in ws.JS_STEP_UP and "X-DV-CSRF" in ws.JS_STEP_UP
    assert "X-DV-RTC-Session" in ws.JS_ICE and "/api/v1/rtc/offer" in ws.JS_OFFER_ONLY
    assert ws.HEARTBEAT_S == 15.0 and ws.RTC_SWEEP_S == 5.0


def test_refusal_shapes():
    assert ws.refusal_shape_problems("x", {"status": 503, "code": "unavailable", "retry_after": "5"}, 503, "unavailable", 5) == []
    assert ws.refusal_shape_problems("x", {"status": 503}, 503, "unavailable", 5) == []                      # a missing code / header is not judged
    assert len(ws.refusal_shape_problems("x", {"status": 429, "code": "rate_limited", "retry_after": "1"}, 503, "unavailable", 5)) == 3
    assert ws.refusal_shape_problems("x", {"status": 429, "code": "rate_limited", "retry_after": "1"}, 429, "rate_limited", 1) == []
    assert any("Retry-After" in p for p in ws.refusal_shape_problems("x", {"status": 429, "retry_after": "9"}, 429, None, 1))


def test_revoke_and_post_snippets_use_the_documented_shapes():
    assert "list.items" in ws.JS_REVOKE_OWN and "x.current" in ws.JS_REVOKE_OWN and "/api/v1/user/me/sessions/" in ws.JS_REVOKE_OWN
    assert "body.ok === true" in ws.JS_REVOKE_OWN and "retry-after" in ws.JS_OFFER_ONLY


# ---------------------------------------------------------------- RawWav against a local server (plain TCP)

import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


class Server:
    """One-shot HTTP server on loopback: serves `respond(conn, request_bytes)` to the first connection."""

    def __init__(self, respond):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.request = b""
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self.run, args=(respond,), daemon=True)
        self.thread.start()

    def run(self, respond):
        conn, _ = self.sock.accept()
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            self.request = data
            respond(conn)
        except OSError:
            pass
        finally:
            self.finished.set()
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.sock.close()


def ok_body(size, cut_at=None):
    def respond(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % size)
        data = b"x" * (size if cut_at is None else cut_at)
        conn.sendall(data)
    return respond


def reader(server, **kw):
    r = ws.RawWav("127.0.0.1", server.port, "/api/v1/user/recordings/ab/audio.wav", "dv_web=cookie-value", "TestAgent/1.0", **kw)
    r.start()
    return r


def wait_done(r, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not r.snapshot()["done"]:
        time.sleep(0.02)
    return r.snapshot()


def test_rawwav_sends_the_cookie_and_the_agent_and_reads_the_whole_body():
    server = Server(ok_body(50000))
    r = reader(server)
    got = wait_done(r)
    assert got["status"] == 200 and got["length"] == 50000 and got["got"] == 50000 and got["err"] is None
    assert ws.wav_progress_problems(got, True) == [] and not ws.wav_cut(got)
    assert b"Cookie: dv_web=cookie-value" in server.request and b"User-Agent: TestAgent/1.0" in server.request
    assert b"Sec-Fetch-Site: same-origin" in server.request and server.request.startswith(b"GET /api/v1/user/recordings/ab/audio.wav HTTP/1.1")
    server.close()


def test_rawwav_shows_a_body_cut_short():
    server = Server(ok_body(50000, cut_at=12000))
    got = wait_done(reader(server))
    assert got["got"] == 12000 and got["done"] and ws.wav_cut(got) and not ws.wav_fits_buffers(got)
    assert any("truncated" in p for p in ws.wav_progress_problems(got, True))
    server.close()


def test_rawwav_reads_a_refusal_with_its_code_and_retry_after():
    body = json.dumps({"code": "rate_limited", "detail": "recording rate limit"}).encode()

    def respond(conn):
        conn.sendall(b"HTTP/1.1 429 Too Many Requests\r\nContent-Type: application/problem+json\r\nRetry-After: 1\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n%s" % (len(body), body))
    server = Server(respond)
    r = reader(server)
    first = r.wait_headers(5.0)
    got = wait_done(r)
    assert first["status"] == 429 and got["code"] == "rate_limited" and got["retry_after"] == "1"
    assert ws.refusal_shape_problems("the third WAV body", got, ws.WAV_REFUSED, "rate_limited", 1) == []
    server.close()


def test_rawwav_hold_keeps_the_server_busy_until_release():
    size = 32 * 1024 * 1024
    server = Server(ok_body(size))
    r = reader(server, hold=True)
    first = r.wait_headers(5.0)
    assert first["status"] == 200 and first["hold"] is True
    time.sleep(0.6)
    assert not server.finished.is_set(), "a 32 MiB body fit the socket buffers: the held reader does not hold the server back"
    assert r.snapshot()["got"] < 100000                                  # nothing but the first bytes behind the headers
    r.release()
    got = wait_done(r, 20.0)
    assert got["got"] == size and server.finished.wait(5.0)
    server.close()


def test_rawwav_rate_throttles_the_reader():
    server = Server(ok_body(200000))
    r = reader(server, rate=400000.0)                                    # 200 KB at 400 KB/s: about 0.5 s
    r.wait_headers(5.0)
    time.sleep(0.15)
    assert r.snapshot()["got"] < 150000 and not r.snapshot()["done"]
    got = wait_done(r)
    assert got["got"] == 200000
    server.close()


def test_rawwav_reports_a_refused_connection_as_an_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    r = ws.RawWav("127.0.0.1", port, "/x", "", "A", timeout=2.0)
    r.start()
    got = wait_done(r, 5.0)
    assert got["err"] and got["done"] and got["status"] is None and ws.wav_cut(got) is False
    assert r.wait_headers(1.0) is not None


def test_parse_head_and_problem_code():
    status, headers = ws.parse_head(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Type: application/problem+json")
    assert status == 503 and headers["retry-after"] == "5"
    assert ws.problem_code_of(b'{"code": "unavailable"}') == "unavailable" and ws.problem_code_of(b"not json") is None and ws.problem_code_of(b"[]") is None


def test_probe_verdict_tells_the_causes_apart():
    # run 20261111T040647Z: the stream lived exactly 40.007 s = the probe's own read timeout, no bye
    assert ws.probe_verdict(True, False, True, 40.007, 40.0)[0] == "client_timeout"
    assert ws.probe_verdict(True, True, True, 27.0, 150.0)[0] == "bye"
    assert ws.probe_verdict(True, True, False, None, 150.0)[0] == "bye"                    # a bye wins whatever the thread state
    assert ws.probe_verdict(True, False, True, 31.2, 150.0)[0] == "dropped"
    assert ws.probe_verdict(True, False, True, None, 150.0)[0] == "dropped"
    assert ws.probe_verdict(True, False, False, None, 150.0)[0] == "follows"
    assert ws.probe_verdict(False, False, True, 3.0, 150.0)[0] == "no_hello"


def test_probe_decision():
    assert ws.probe_decision(["follows"]) == (True, "an attempt kept the stream open across the step-up")
    assert ws.probe_decision(["client_timeout", "follows"])[0] is True
    assert ws.probe_decision(["bye"])[0] is False and ws.probe_decision(["follows", "bye"])[0] is True
    assert ws.probe_decision(["dropped"])[0] is None and ws.probe_decision(["dropped", "dropped"])[0] is False
    assert ws.probe_decision(["dropped", "client_timeout", "dropped"])[0] is None                  # not two in a row
    assert ws.probe_decision(["client_timeout", "no_hello", "client_timeout"])[0] is None
    assert "inconclusive after 3" in ws.probe_decision(["a", "b", "c"])[1] and ws.probe_decision([])[0] is None


def test_rawwav_asks_the_proxy_for_small_buffers_unless_told_not_to():
    server = Server(ok_body(1000))
    r = reader(server)
    wait_done(r)
    assert b"X-Dvt-Buf: %d\r\n" % ws.PROXY_BUF in server.request
    server.close()
    server = Server(ok_body(1000))
    r = reader(server, proxy_buf=0)
    wait_done(r)
    assert b"X-Dvt-Buf" not in server.request
    server.close()
