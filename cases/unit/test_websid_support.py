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
    assert ws.cap_refusal([200] * 8 + [429], 8) == []
    assert ws.cap_refusal([200] * 7 + [503, 503], 8) != [] and ws.cap_refusal([200] * 9, 8) != []
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
    for name in ("JS_SSE_START", "JS_SSE_STATE", "JS_WAV_START", "JS_WAV_STATE", "JS_WAV_RELEASE", "JS_STEP_UP", "JS_SESSION", "JS_OFFER_ONLY",
                 "JS_ALIVE_START", "JS_ALIVE_SET_CSRF", "JS_ALIVE_STATE", "JS_ICE", "JS_GET_STATUS"):
        text = getattr(ws, name)
        assert text.count("{") == text.count("}") and text.count("(") == text.count(")"), name
    assert "'bye'" in ws.JS_SSE_START and "hello" in ws.JS_SSE_START
    assert "ctl.rate" in ws.JS_WAV_START and "w.ctl.rate = 0" in ws.JS_WAV_RELEASE
    assert "/api/v1/auth/session/step-up" in ws.JS_STEP_UP and "X-DV-CSRF" in ws.JS_STEP_UP
    assert "X-DV-RTC-Session" in ws.JS_ICE and "/api/v1/rtc/offer" in ws.JS_OFFER_ONLY
    assert ws.HEARTBEAT_S == 15.0 and ws.RTC_SWEEP_S == 5.0
