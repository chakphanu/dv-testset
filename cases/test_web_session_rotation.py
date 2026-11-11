# SPDX-License-Identifier: MIT
"""Web unit, WEBSID1: open streams, RTC leases and WAV transfers follow the login across a step-up and end with it, in real browsers
(Playwright Chromium, Firefox and Playwright WebKit). WRITTEN OFFLINE for the image after wip-7bd0082 (DV-Router merge a1ff890c); NOT RUN.
WAV bodies are read by the tester over a raw TLS socket (websid_support.RawWav; a browser fetch cannot hold a body back, run 20261110T220224Z).
Needs `scripts/run.sh --profile web,browser` with rig/config/dv-router/recorder.toml and a USRP dynamic hub (DVT_USRP_DYNAMIC=1); docs/websid1-cases.md
has the run command, the runtime and the GAPs. Source: DV-Router web.md "Built (WEBSID1)" and review-248 "dv-testset browser case to request".

  1  test_step_up_keeps_sse_circuit_and_wav[engine]       an operator login with an open operator EventSource, a Listen circuit and a slow WAV
                                                          download; a step-up in a second tab rotates cookie and CSRF token, the old cookie gets
                                                          401, no SSE bye, the circuit stays connected, the WAV arrives whole, and the NEW cookie
                                                          renews and closes the original circuit
  2  test_step_up_does_not_multiply_the_caps[engine]      eight circuits and two held WAV bodies: after a step-up a ninth circuit and a third
                                                          body are still refused, and the TURN issue budget (one per minute, burst two) does not
                                                          refill (skipped part without TURN: --profile nat with turn.toml)
  3  test_session_end_ends_the_handles[engine-cause]      logout, own-session revoke, role change and account disable end SSE (within one
                                                          heartbeat), the RTC circuit (within the 5 s sweep) and the WAV transfer (cut)
  4  test_denials_coalesce_across_a_step_up               a repeated reload denial across a step-up: one audit record per 10 s, the next carries
                                                          `suppressed` (admin service, the SECRELOAD2 route)
GAPs: idle and absolute expiry (hard-coded: users 12 h / 7 d, operators 30 min / 12 h, crates/svc/web/src/auth/mod.rs:72-77), TOTP enable / replace /
disable variants, the 100 ms WAV bound (the browser sees the end only after its own buffers drain: the case shows the body is CUT). Skips on an image
without WEBSID1 (probe: an operator stream that dies at a step-up); DVT_WEBSID1=on turns that into a failure.
FINDING WEBSID1b (dv-router, a product defect fixed in unit WEBSID1b): the CSRF token rotates with a step-up but reaches a page only in JSON replies, so the
OTHER tab's next write is 403 `csrf` and its RTC keepalive treats that as lost ownership and drops the circuit ("Connection ownership expired"), although
the server lease is alive. Case 1 therefore records a lost circuit as a FINDING (web.finding) and keeps asserting the rest; DVT_WEBSID1B=on makes the circuit
staying up past two keepalive periods an assertion. A step-up in the SAME tab is the positive control (test_step_up_in_the_same_tab_keeps_the_circuit).
"""
import os
import threading
import time
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

import web_pages as pages  # noqa: E402
import websid_support as ws  # noqa: E402
from browser_fixtures import BrowserPage, _failed, _operator_account  # noqa: E402
from dvtest import secret_reload as sr  # noqa: E402
from dvtest import web  # noqa: E402
from webrtc_support import Rtc, Talk, tone_frames, usrp_client  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc]

ROOM = "V"                      # the Listen room and the room of the long over (S, T, U are the recorder cases, H..Z the WebRTC cases)
OVER_S = float(os.environ.get("DVT_WEBSID_OVER_S") or 150.0)       # a long over: the WAV body (16 KB/s) must not fit the buffers on the way
OVER_CALL = "N0SIDV"
AUDIO = "/api/v1/user/recordings/%s/audio.wav"
OBSERVE_S = 20.0                # longer than one SSE heartbeat (15 s) and the 15 s circuit lease
SLOW_S = 45.0                   # the slow download takes about this long
CAUSES = ["logout", "revoke", "role", "disable"]


def strict():
    return os.environ.get("DVT_WEBSID1", "").lower() in ("on", "1", "true", "yes")


def strict_1b():
    """DVT_WEBSID1B=on: the image has the WEBSID1b fix (the rotated CSRF token reaches every tab), so a circuit that does not survive a step-up in
    ANOTHER tab fails the case instead of being a FINDING."""
    return os.environ.get("DVT_WEBSID1B", "").lower() in ("on", "1", "true", "yes")


def note(text):
    print("[RECORD] WEBSID1: %s" % text)


def inconclusive(message):
    """A case that could not exercise what it checks: a failure with DVT_WEBSID1=on, a skip otherwise (never a pass)."""
    if strict():
        pytest.fail("inconclusive (DVT_WEBSID1=on): %s" % message)
    pytest.skip(message)


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def sid_recorder():
    if os.environ.get("DVT_RECORDER") != "on":
        pytest.skip("no recorder: add rig/config/dv-router/recorder.toml to DVT_SUT_CONFIG_EXTRA (DVT_RECORDER=on in the tester)")


@pytest.fixture(scope="session")
def sid_operator(web_operator, web_base_url, web_ca):
    """The operator whose logins the cases use (TOTP enrolled, no live session). A session of its own per case: at most three per account."""
    return _operator_account(web_operator, web_base_url, web_ca, "N0BSD")


@pytest.fixture(scope="session")
def sid_listing_user(web_user):
    return web_user("N0BSL", may_tx=False)


@pytest.fixture(scope="session")
def sid_wav(sid_recorder, target, sid_listing_user):
    """A long over into ROOM from a USRP dynamic client, then the id and size of its recording: (id, content length)."""
    peer = usrp_client(target, OVER_CALL, ROOM)
    try:
        talk = Talk(peer, tone_frames(OVER_S, freq=700.0))
        talk.start()
        talk.join(timeout=OVER_S + 20)
        assert talk.error is None, talk.error
    finally:
        peer.close()
    end = time.monotonic() + 60.0
    client = sid_listing_user.client
    while time.monotonic() < end:
        time.sleep(1.0)
        resp = client.get("/api/v1/user/recordings?limit=100", expect=200)
        rows = [r for r in resp.json().get("items", []) if r.get("room") == ROOM and (r.get("callsign") or "").upper().startswith(OVER_CALL)]
        if rows:
            row = max(rows, key=lambda r: r.get("start_ms", 0))
            return row["id"], int(row.get("decoded_bytes", 0)) + 44
    pytest.fail("the recorder listed no recording of %s in room %s 60 s after the over" % (OVER_CALL, ROOM))


PROBE_ATTEMPTS = 3
PROBE_CLIENT_TIMEOUT_S = 150.0          # far above hello wait + a TOTP step wait + the window


def probe_once(sid_operator, web_base_url, web_ca):
    """One attempt: a fresh operator login of the HTTP client, a stream, a step-up, 18 s of watching. Returns (kind, text) of ws.probe_verdict, or
    ("step_up_failed", status) when the step-up itself did not answer 200. Both TOTP codes are taken BEFORE the stream opens, so no wait for the next
    30 s step can eat the window."""
    client = web.WebClient(web_base_url, web_ca)
    client.authenticator = sid_operator.client.authenticator
    client.hide(sid_operator.password)
    client.login(sid_operator.callsign, sid_operator.password, client.authenticator.next_code())
    code = client.authenticator.next_code()
    client.hide(code)
    frames, stop = [], threading.Event()
    marks = {"open": None, "end": None}

    def run():
        marks["open"] = time.monotonic()
        try:
            client.sse("/api/v1/operator/stream", max_events=100000, timeout=PROBE_CLIENT_TIMEOUT_S, on_frame=frames.append, stop=stop)
        except web.WebError:
            pass
        marks["end"] = time.monotonic()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        end = time.monotonic() + 10.0
        while time.monotonic() < end and not any(f.get("event") == "hello" for f in frames):
            time.sleep(0.2)
        stepped = client.post("/api/v1/auth/session/step-up", {"code": code}, expect=None)
        if stepped.status != 200:
            return "step_up_failed", "HTTP %d" % stepped.status
        time.sleep(18.0)
        ended = not thread.is_alive()
        ran = None if marks["end"] is None or marks["open"] is None else marks["end"] - marks["open"]
        return ws.probe_verdict(any(f.get("event") == "hello" for f in frames), any(f.get("event") == "bye" for f in frames), ended, ran,
                                PROBE_CLIENT_TIMEOUT_S)
    finally:
        stop.set()
        thread.join(5)
        try:
            client.session()
            client.logout()
        except web.WebError:
            pass


@pytest.fixture(scope="session")
def websid1(sid_operator, web_base_url, web_ca):
    """Whether the image has WEBSID1, probed: an operator SSE stream opened with the HTTP client must still be open 18 s after a step-up (an image
    before WEBSID1 ends it with a `bye`, the stream followed the cookie). Up to three attempts; a stream that ended by itself (a dropped connection, the
    probe's own timeout, no hello) is told apart from a server `bye` and retried. Skips with the reason; DVT_WEBSID1=on makes it a failure."""
    kinds, texts = [], []
    for _ in range(PROBE_ATTEMPTS):
        kind, text = probe_once(sid_operator, web_base_url, web_ca)
        kinds.append(kind)
        texts.append(text)
        print("[RECORD] WEBSID1 probe attempt %d: %s: %s" % (len(kinds), kind, text))
        if kind == "step_up_failed":
            pytest.fail("the step-up of the probe answered %s" % text)
        follows, reason = ws.probe_decision(kinds)
        if follows is not None:
            break
    if follows:
        return True
    if follows is False:
        message = "%s (attempts: %s)" % (reason, "; ".join("%s: %s" % kt for kt in zip(kinds, texts)))
        if strict():
            pytest.fail("DVT_WEBSID1=on but " + message)
        pytest.skip("image without WEBSID1: " + message)
    message = "the WEBSID1 probe was inconclusive: %s (%s)" % (reason, "; ".join(texts))
    if strict():
        pytest.fail(message)
    pytest.skip(message)


# ---------------------------------------------------------------- the page of one login

class Login:
    """One browser login: the page, its Rtc panel, the cookie / CSRF facts and the handles started in it."""

    def __init__(self, engine, engine_browsers, web_base_url, account, name, ca=None):
        ctx = {"permissions": ["microphone"]} if engine in ("chromium", "webkit") else None
        self.bp = BrowserPage(engine, engine_browsers(engine), web_base_url, name, context_options=ctx)
        self.engine, self.account, self.base, self.ca = engine, account, web_base_url, ca
        self.wavs = {}
        self.wav_log = []                                    # (name, t_ms) of every audio request of this login, for the 10 s rate window
        self.generation = 0                                  # the cookie generation: 0 before any step-up, +1 after each
        self.pending_code = None
        self.bp.install_rtc_probe()
        code = account.client.authenticator.next_code()
        account.client.hide(code)
        status = self.bp.api_login(account.callsign, account.password, code)
        if status != 200:
            self.bp.finish(True)
            pytest.fail("browser API login of %s: HTTP %s" % (account.callsign, status))
        self.rtc = None

    @property
    def page(self):
        return self.bp.page

    def cookie(self):
        c = self.bp.session_cookie()
        return c["value"] if c else None

    def csrf(self):
        return self.page.evaluate(ws.JS_SESSION)["csrf"]

    def listen(self):
        self.rtc = Rtc(self.bp)
        self.rtc.open()
        self.rtc.connect(ROOM)
        return self.rtc

    def sse_start(self):
        self.page.evaluate(ws.JS_SSE_START)
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            state = self.page.evaluate(ws.JS_SSE_STATE)
            if state and state.get("opened"):
                return state
            time.sleep(0.2)
        raise AssertionError("the operator EventSource did not open in 10 s")

    def sse(self):
        return self.page.evaluate(ws.JS_SSE_STATE)

    def user_agent(self):
        return self.page.evaluate("() => navigator.userAgent")

    def wav_start(self, name, wav, rate=0.0, hold=False):
        """Start a WAV download read by the TESTER over a raw TLS socket with a tiny receive buffer (a browser reader cannot hold a body back, see
        websid_support.RawWav), with this login's cookie and User-Agent. Returns the snapshot once the response headers are in."""
        rec_id, _length = wav
        cookie = self.bp.session_cookie()
        assert cookie, "the login has no session cookie to give the WAV reader"
        parts = urlsplit(self.base)
        reader = ws.RawWav(parts.hostname, parts.port or 443, AUDIO % rec_id, "%s=%s" % (cookie["name"], cookie["value"]), self.user_agent(),
                           ca=self.ca, rate=rate, hold=hold, meta={"cookie": "gen%d" % self.generation, "name": name})
        self.wavs[name] = reader
        self.wav_log.append((name, time.time() * 1000.0))
        reader.start()
        got = reader.wait_headers(15.0)
        assert got is not None, "the WAV request %s got no response headers in 15 s" % name
        if got["status"] != 200:
            got = reader.wait_done(5.0)                      # a refusal: its body (the problem code) is read after the headers
        return got

    def wav(self, name):
        return self.wavs[name].snapshot()

    def wav_report(self, engine, names):
        """RECORD lines for the audio requests `names` (what was sent: URL tail, method, Range, cookie generation; what came back)."""
        for name in names:
            if name in self.wavs:
                note("%s: %s" % (engine, ws.describe_request(name, self.wav(name), self.wav_log)))

    def wav_release(self, name):
        self.wavs[name].release()

    def second_tab(self):
        tab = self.bp.context.new_page()
        tab.goto(self.bp.url("/shared/img/favicon.svg"))
        return tab

    def prepare_step_up(self):
        """Take the TOTP code of the next step-up now. next_code() sleeps up to a whole TOTP step when the counter is two steps ahead (the login and
        the enrolment each used one): run 20261111T054920Z waited 23 s there while two WAV bodies were being held, long enough for the buffers on
        the way to take them and free their server-side slots. A code taken early stays valid for the window (+-1 step); step_up() uses it."""
        code = self.account.client.authenticator.next_code()
        self.account.client.hide(code)
        self.pending_code = code

    def step_up(self):
        """A step-up from a second tab of the same context (same cookie jar). Returns (status, csrf before)."""
        tab = self.second_tab()
        try:
            code = self.pending_code
            self.pending_code = None
            if code is None:
                code = self.account.client.authenticator.next_code()
                self.account.client.hide(code)
            got = tab.evaluate(ws.JS_STEP_UP, {"code": code})
        finally:
            tab.close()
        if got["status"] == 200:
            self.generation += 1
        return got["status"], got["csrf_before"]

    def finish(self, request):
        for reader in self.wavs.values():
            reader.release()
        try:
            self.bp.api_logout()
        except Exception:                                   # noqa: BLE001 - the session may be gone already
            pass
        self.bp.finish(_failed(request))


@pytest.fixture
def make_login(engine, engine_browsers, web_base_url, web_ca, request, websid1):
    made = []

    def make(account, name=None):
        login = Login(engine, engine_browsers, web_base_url, account, name or request.node.name.replace("[", "-").rstrip("]"), ca=web_ca)
        made.append(login)
        return login

    yield make
    for login in made:
        login.finish(request)


def card_text(rtc):
    try:
        return rtc.status()
    except Exception:                                               # noqa: BLE001 - the card is gone with the page state
        return "(no card)"


def rate_for(length, seconds=SLOW_S):
    """Bytes per second that make a body of `length` bytes take about `seconds`."""
    return max(1.0, length / seconds)


# ---------------------------------------------------------------- 1: a step-up keeps the handles

def test_step_up_keeps_sse_circuit_and_wav(engine, make_login, sid_operator, sid_wav, web_base_url):
    login = make_login(sid_operator)
    rtc = login.listen()
    login.sse_start()
    started = login.wav_start("slow", sid_wav, rate=rate_for(sid_wav[1]))
    assert started["status"] == 200, "the WAV download answered %s" % started["status"]
    time.sleep(3.0)
    before_got = login.wav("slow")["got"]
    assert before_got < sid_wav[1] * 0.5, "the slow download is already %d of %d bytes: it must still be running at the step-up" % (before_got, sid_wav[1])
    old_state = login.bp.context.storage_state()
    old_cookie, old_csrf = login.cookie(), login.csrf()

    status, _csrf = login.step_up()
    assert status == 200, "the step-up in the second tab answered %s" % status
    new_cookie, new_csrf = login.cookie(), login.csrf()
    problems = ws.rotation_problems(old_cookie, new_cookie, old_csrf, new_csrf)
    assert not problems, "rotation: %s" % "; ".join(problems)

    # the old cookie is refused at once (a second context with the same engine, the same User-Agent, the old cookie)
    other = BrowserPage(engine, login.bp.context.browser, web_base_url, "old-cookie", storage_state=old_state)
    try:
        other.page.goto(other.url("/shared/img/favicon.svg"))
        assert other.page.evaluate(ws.JS_GET_STATUS, "/api/v1/user/me") == 401, "the old session cookie is still accepted after the step-up"
    finally:
        other.finish(False)

    # the new cookie renews the ORIGINAL circuit (a second tab with the rotated cookie and the current CSRF token), at once
    sess_id = rtc.offers[-1] if rtc.offers else None
    assert sess_id, "the page never saw its /rtc/offer answer"
    tab = login.second_tab()
    try:
        renewed = tab.evaluate(ws.JS_RTC_POST, {"path": "/api/v1/rtc/alive", "id": sess_id})
    finally:
        tab.close()
    assert renewed == 204, "POST /rtc/alive with the rotated cookie on the original circuit answered %s, want 204" % renewed

    # the handles live on across a heartbeat and two keepalive periods (the circuit: see FINDING WEBSID1b)
    observe = OBSERVE_S + (12.0 if strict_1b() else 0.0)
    end = time.monotonic() + observe
    circuit_lost_at = None
    while time.monotonic() < end:
        sse = login.sse()
        problems = ws.sse_alive_problems(sse)
        assert not problems, "SSE after the step-up: %s (state %s)" % ("; ".join(problems), sse)
        if circuit_lost_at is None and ws.circuit_problems(rtc.state(), rtc.panel_state()):
            circuit_lost_at = time.monotonic() - (end - observe)
        time.sleep(1.0)
    posts = [r["status"] for r in login.bp.log.requests if r["path"].endswith("/rtc/alive")]
    note("%s: /rtc/alive statuses of the page: %s" % (engine, sorted(set(posts))))
    if circuit_lost_at is not None:
        msg = ("WEBSID1b: the Listen circuit of tab 1 was lost %.0f s after a step-up in tab 2 (page /rtc/alive statuses %s, card %r): the rotated CSRF token "
               "does not reach the other tab, its keepalive gets 403 csrf and drops the circuit although the server lease is alive"
               % (circuit_lost_at, sorted(set(posts)), card_text(rtc)))
        if strict_1b():
            pytest.fail(msg)
        web.finding(msg)
        note(msg)

    # the WAV body arrives whole
    end = time.monotonic() + SLOW_S + 30.0
    while time.monotonic() < end and not login.wav("slow")["done"]:
        time.sleep(1.0)
    final = login.wav("slow")
    problems = ws.wav_progress_problems(final, expect_complete=True)
    assert not problems, "the slow WAV download across the step-up: %s (state %s)" % ("; ".join(problems), final)

    # the new cookie closes the original circuit (from a tab that has the current CSRF token); a lease that expired because the page dropped it
    # (WEBSID1b) is recorded
    tab = login.second_tab()
    try:
        still = tab.evaluate(ws.JS_RTC_POST, {"path": "/api/v1/rtc/alive", "id": sess_id})
        closed = tab.evaluate(ws.JS_RTC_POST, {"path": "/api/v1/rtc/close", "id": sess_id}) if still == 204 else None
    finally:
        tab.close()
    note("%s: alive %s, close %s with the rotated cookie" % (engine, still, closed))
    if still == 204:
        assert closed == 204, "POST /rtc/close with the rotated cookie answered %s, want 204" % closed
    else:
        assert circuit_lost_at is not None, "the circuit was up in the page but POST /rtc/alive answered %s" % still


def test_step_up_in_the_same_tab_keeps_the_circuit(engine, make_login, sid_operator):
    """The positive control of WEBSID1b: a step-up in the tab that owns the circuit updates its CSRF token, so the keepalive goes on."""
    login = make_login(sid_operator)
    rtc = login.listen()
    code = login.account.client.authenticator.next_code()
    login.account.client.hide(code)
    got = login.page.evaluate(ws.JS_STEP_UP, {"code": code})
    assert got["status"] == 200, "the step-up in the same tab answered %s" % got["status"]
    end = time.monotonic() + 25.0
    while time.monotonic() < end:
        bad = ws.circuit_problems(rtc.state(), rtc.panel_state())
        assert not bad, "the Listen circuit after a step-up in its own tab: %s" % "; ".join(bad)
        time.sleep(1.0)


# ---------------------------------------------------------------- 2: caps across a rotation

def test_step_up_does_not_multiply_the_caps(engine, make_login, sid_operator, sid_wav):
    login = make_login(sid_operator)
    login.bp.goto("/shared/img/favicon.svg")
    csrf = login.csrf()
    ids, statuses = [], []
    for _ in range(9):
        got = login.page.evaluate(ws.JS_OFFER_ONLY, {"room": ROOM, "csrf": csrf})
        statuses.append(got["status"])
        if got["status"] != 200:
            shape = ws.refusal_shape_problems("the ninth circuit", got, ws.CIRCUIT_REFUSED, "unavailable", 5)
            assert not shape, "; ".join(shape)
        if got["status"] == 200 and got["sess_id"]:
            ids.append(got["sess_id"])
        time.sleep(0.7)                                            # the join buckets
    problems = ws.cap_refusal(statuses, ws.CIRCUIT_CAP)
    assert not problems, "circuits of one login before the step-up: %s (statuses %s)" % ("; ".join(problems), statuses)
    login.page.evaluate(ws.JS_ALIVE_START, {"ids": ids, "csrf": csrf})
    ice_before = ice_probe(login, ids[0])
    login.prepare_step_up()                      # the wait for the next TOTP step happens HERE, with nothing held (see prepare_step_up)
    a = login.wav_start("one", sid_wav, hold=True)
    b = login.wav_start("two", sid_wav, hold=True)
    c = login.wav_start("three", sid_wav, hold=True)
    assert a["status"] == 200 and b["status"] == 200, "the first two WAV bodies answered %s / %s" % (a["status"], b["status"])
    shape = ws.refusal_shape_problems("the third WAV body", c, ws.WAV_REFUSED, "rate_limited", 1)
    assert not shape, "before the step-up: %s" % "; ".join(shape)
    note("%s: circuits %s; WAV statuses %s / %s / %s" % (engine, statuses, a["status"], b["status"], c["status"]))

    # The precondition of the verdict: the two bodies hold their slots RIGHT BEFORE the step-up (a probe, not an assumption). A server-side slot is
    # held only until the transfer task has queued its last chunk, so a body the buffers on the way swallowed would free it with no rotation involved.
    pre = login.wav_start("pre", sid_wav, hold=True)
    t_pre = time.monotonic()
    login.wav_report(engine, ("one", "two", "three", "pre"))
    if pre["status"] != ws.WAV_REFUSED:
        inconclusive("%s: a third WAV body answered %s just before the step-up: the two held bodies (%s / %s of %s bytes read) no longer hold their "
                     "slots (taken by the buffers on the way), so the step-up cannot be judged; the web-proxy X-Dvt-Buf hook did not limit them" % (
                         engine, pre["status"], login.wav("one")["got"], login.wav("two")["got"], sid_wav[1]))
    status, _ = login.step_up()
    assert status == 200, "the step-up answered %s" % status
    login.page.evaluate(ws.JS_ALIVE_SET_CSRF, login.csrf())
    csrf = login.csrf()
    ninth = login.page.evaluate(ws.JS_OFFER_ONLY, {"room": ROOM, "csrf": csrf})
    shape = ws.refusal_shape_problems("a ninth circuit after the step-up", ninth, ws.CIRCUIT_REFUSED, "unavailable", 5)
    assert not shape, "the cap counted per cookie again? %s" % "; ".join(shape)
    d = login.wav_start("four", sid_wav, hold=True)
    gap = time.monotonic() - t_pre
    login.wav_report(engine, ("four",))
    over = [n for n in ("one", "two") if ws.wav_finished(login.wav(n), sid_wav[1])]
    if d["status"] != ws.WAV_REFUSED and over:
        inconclusive("%s: bodies finished, cap not exercised: %s no longer held a slot at the probe after the step-up (%s)" % (
            engine, " and ".join(over), {n: (login.wav(n)["got"], login.wav(n)["done"], login.wav(n)["err"]) for n in over}))
    shape = ws.refusal_shape_problems("a third WAV body after the step-up", d, ws.WAV_REFUSED, "rate_limited", 1)
    assert not shape, "%s (a third body was refused %.1f s earlier with the same two held and not read: one %s / two %s of %s bytes read; the key of the cap is (role, session), it must not change at the step-up)" % (
        "; ".join(shape), gap, login.wav("one")["got"], login.wav("two")["got"], sid_wav[1])
    assert ws.wav_progress_problems(login.wav("one"), expect_complete=False) == [], "the held body one died at the step-up"
    note("%s: after the step-up a ninth circuit is %s and a third body %s (%.1f s after the last refusal before it)" % (engine, ninth["status"], d["status"], gap))
    if ice_before is not None:
        again = ice_probe(login, ids[0])
        assert again != 200, "the TURN issue budget refilled at the step-up (GET /rtc/ice answered 200 again; before: %s)" % (ice_before,)
        note("%s: TURN issue budget before / after the step-up: %s / %s" % (engine, ice_before, again))


def ice_probe(login, sess_id):
    """Issue TURN credentials until the budget (one per minute, burst two per login) refuses; the refusing status, or None without TURN (404)."""
    status = None
    for _ in range(4):
        status = login.page.evaluate(ws.JS_ICE, sess_id)
        if status == 404:
            return None
        if status != 200:
            return status
    pytest.fail("GET /rtc/ice issued four credentials in a row (budget: one per minute, burst two)")


# ---------------------------------------------------------------- 3: every end ends the handles

def throwaway(engine, cause, web_operator, web_base_url, web_ca):
    """A fresh operator that the case may demote or disable: one per (engine, cause), a role change or a disable cannot be undone for the next run."""
    call = "N0BR%s%s" % (engine[0].upper(), {"role": "R", "disable": "X"}[cause])
    return _operator_account(web_operator, web_base_url, web_ca, call)


def circuit_gone(rtc):
    """The page's peer is no longer connected or the Listen card left `connected`; a page that no longer shows the card (the SPA went to its login
    page after a 401) counts as gone."""
    try:
        state = rtc.state() or {}
        return state.get("connection") not in ("connected", None) or rtc.panel_state() != "connected"
    except Exception:                                               # noqa: BLE001 - the card or the probe is gone with the page state
        return True


def trigger(cause, login, web_operator):
    """Make the end happen; returns the browser-clock time (ms) of the trigger."""
    page = login.page
    if cause == "logout":
        tab = login.second_tab()
        try:
            t = tab.evaluate("async () => { const s = await (await fetch('/api/v1/auth/session', {credentials: 'same-origin'})).json(); "
                             "const t = Date.now(); await fetch('/api/v1/auth/session', {method: 'DELETE', credentials: 'same-origin', "
                             "headers: {'content-type': 'application/json', 'X-DV-CSRF': s.csrf}, body: '{}'}); return t; }")
        finally:
            tab.close()
        return t
    if cause == "revoke":
        tab = login.second_tab()
        try:
            got = tab.evaluate(ws.JS_REVOKE_OWN)
        finally:
            tab.close()
        assert got["found"], "GET /user/me/sessions lists no current login (total %s)" % got.get("total")
        assert got["status"] == 200 and got["ok"], "DELETE /user/me/sessions/{id} answered %s, want 200 {ok:true}" % got["status"]
        return got["t"]
    t = page.evaluate("() => Date.now()")
    body = {"role": "user"} if cause == "role" else {"status": "disabled"}
    resp = web_operator.request("PATCH", "/api/v1/operator/accounts/%s" % login.account.callsign, json_body=body, expect=None)
    assert resp.status == 200, "PATCH account %s %s: HTTP %d %s" % (login.account.callsign, body, resp.status, web_operator.scrub(resp.text())[:120])
    return t


@pytest.mark.parametrize("cause", CAUSES)
def test_session_end_ends_the_handles(engine, cause, make_login, sid_operator, sid_wav, web_operator, web_base_url, web_ca):
    account = sid_operator if cause in ("logout", "revoke") else throwaway(engine, cause, web_operator, web_base_url, web_ca)
    login = make_login(account)
    rtc = login.listen()
    login.sse_start()
    login.wav_start("held", sid_wav, hold=True)                    # a paused reader
    login.wav_start("slow", sid_wav, rate=rate_for(sid_wav[1]))
    time.sleep(2.0)
    assert ws.sse_alive_problems(login.sse()) == [], "the stream is not open before the end"
    t0 = trigger(cause, login, web_operator)

    # SSE: within one heartbeat
    end = time.monotonic() + ws.HEARTBEAT_S + 5.0
    sse = login.sse()
    while time.monotonic() < end and not ws.sse_ended(sse):
        time.sleep(0.25)
        sse = login.sse()
    t_sse = ws.sse_end_ms(sse)
    note("%s %s: SSE ended %s ms after the trigger (bye %s, readyState %s)" % (engine, cause, None if t_sse is None else t_sse - t0, "bye" in (sse.get("names") or {}), sse.get("ready")))
    assert ws.sse_ended(sse), "the operator stream was still open %.0f s after %s" % (ws.HEARTBEAT_S + 5.0, cause)
    assert ws.within(t_sse, t0, ws.HEARTBEAT_S + 2.0), "the stream ended %s ms after %s, want within one heartbeat (%.0f s)" % (
        None if t_sse is None else t_sse - t0, cause, ws.HEARTBEAT_S)

    # RTC: within the 5 s sweep (plus the time the page needs to see the peer go)
    end = time.monotonic() + ws.RTC_SWEEP_S + 5.0
    lost = False
    while time.monotonic() < end:
        if circuit_gone(rtc):
            lost = True
            break
        time.sleep(0.2)
    waited = ws.RTC_SWEEP_S + 5.0 - max(0.0, end - time.monotonic())
    note("%s %s: the Listen circuit went away after %.1f s" % (engine, cause, waited))
    assert lost, "the Listen circuit was still connected %.0f s after %s (sweep %.0f s)" % (ws.RTC_SWEEP_S + 5.0, cause, ws.RTC_SWEEP_S)

    # WAV: the bodies are CUT. The paused reader is released only now; what the server had already queued is below 49 KiB past the buffers,
    # so a body that arrives whole was fully buffered (the over is too short for this case) and is reported instead of passing.
    held_for = time.time() - login.wavs["held"].snapshot()["t_start"] / 1000.0
    login.wav_release("held")
    login.wav_release("slow")
    end = time.monotonic() + 20.0
    while time.monotonic() < end and not (login.wav("held")["done"] and login.wav("slow")["done"]):
        time.sleep(0.5)
    held, slow = login.wav("held"), login.wav("slow")
    note("%s %s: WAV held %s / slow %s" % (engine, cause, {k: held.get(k) for k in ("status", "got", "length", "done", "err")},
                                           {k: slow.get(k) for k in ("status", "got", "length", "done", "err")}))
    if ws.wav_fits_buffers(held):
        pytest.skip("the held WAV body (%s bytes) arrived whole after %s: it fit the buffers on the way, so a cut cannot be shown; raise "
                    "DVT_WEBSID_OVER_S (now %.0f s)" % (held.get("length"), cause, OVER_S))
    if held_for >= ws.WRITE_TIMEOUT_S - 1.0 and ws.wav_cut(held):
        inconclusive("%s %s: the held WAV body was paused for %.0f s, as long as the server write timeout (%.0f s): a cut cannot be told from a timeout" % (
            engine, cause, held_for, ws.WRITE_TIMEOUT_S))
    note("%s %s: the held body was paused %.1f s before its release" % (engine, cause, held_for))
    assert ws.wav_cut(held), "the held WAV body was not cut after %s: %s" % (cause, held)
    assert ws.wav_cut(slow), "the slow WAV body was not cut after %s: %s" % (cause, slow)


# ---------------------------------------------------------------- 4: denials coalesce across a step-up

DENIAL_WINDOW_S = 10.0


def preview_denials(admin, op_user):
    """The config.reload.preview records of `op_user` (denied) in the audit tail read by the admin, oldest first."""
    audit = admin.get(admin.path("/audit") + "?limit=500", expect=200).json()
    return [i for i in sr.audit_chronological(audit)
            if i.get("action") == "config.reload.preview" and i.get("user") == op_user and i.get("result") == "denied"]


def test_denials_coalesce_across_a_step_up(admin_session, websid1):
    operator, admin = admin_session("operator"), admin_session("admin")
    admin.step_up()
    op_user = operator.identity["user"]
    base = len(preview_denials(admin, op_user))
    code = operator.authenticator.next_code()                       # taken now: it stays valid for the 10 s window
    operator.hide(code)
    t0 = time.monotonic()
    for _ in range(3):
        assert operator.get(operator.path("/config/reload")).status == 403, "an operator must be refused the reload preview"
    status = operator.post(operator.path("/session/step-up"), {"code": code}, expect=None).status
    assert status == 200, "the operator step-up answered %s" % status
    operator.session()                                               # the rotated CSRF token
    for _ in range(3):
        assert operator.get(operator.path("/config/reload")).status == 403
    elapsed = time.monotonic() - t0
    during = preview_denials(admin, op_user)
    problems = ws.coalescing_problems(during, base, expected_new=1, require_suppressed=False)
    assert elapsed < DENIAL_WINDOW_S - 1.0, "the probes took %.1f s, longer than the %.0f s window: the case is not measuring coalescing" % (elapsed, DENIAL_WINDOW_S)
    assert not problems, "six denials around a step-up inside %.0f s: %s" % (DENIAL_WINDOW_S, "; ".join(problems))
    time.sleep(max(0.0, DENIAL_WINDOW_S + 1.0 - (time.monotonic() - t0)))
    assert operator.get(operator.path("/config/reload")).status == 403
    after = preview_denials(admin, op_user)
    problems = ws.coalescing_problems(after, base, expected_new=2, require_suppressed=True)
    assert not problems, "one more denial after the window: %s" % "; ".join(problems)
    note("denial records of %s: %d new, newest suppressed %s" % (op_user, len(after) - base, after[-1].get("suppressed")))
