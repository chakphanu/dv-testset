# SPDX-License-Identifier: MIT
"""Helpers of the WEBSID1 browser cases (cases/test_web_session_rotation.py, docs/websid1-cases.md).

DV-Router WEBSID1 (merge a1ff890c, dv-svc-web): a step-up, TOTP enrolment, replacement or disable ROTATES the cookie and the CSRF token of
the same login session; the old cookie answers 401 at once; open operator SSE streams, RTC / talk leases and WAV transfers follow the stable
server-side SessionKey instead of the cookie and survive; caps count across rotations; logout, revoke, expiry, role change and disable end
the handles (SSE within one heartbeat, RTC within the 5 s sweep, a WAV body within 100 ms). Pure logic (verdicts on the page states) and the
JavaScript the cases run in the page; stdlib only. Source: DV-Router web.md "Built (WEBSID1)", review-248.
"""
import socket
import ssl
import threading
import time

# refusal shapes pinned by dv-router from the code at a1ff890c: the ninth circuit of a login is 503 `unavailable` ("WebRTC capacity reached",
# Retry-After 5; the cap of 8 counts across rotations); the third concurrent WAV body of a (role, session) is 429 `rate_limited` ("recording rate
# limit", Retry-After 1, at once, no queue). Other limits on the same paths: 20 audio requests per 10 s, global browse 2 / play 4 slots with a 5 s wait.
CIRCUIT_CAP = 8
CIRCUIT_REFUSED = 503
WAV_BODY_CAP = 2
WAV_REFUSED = 429
HEARTBEAT_S = 15.0              # the default SSE heartbeat of the web service (hub sse.rs): the session is checked at each one
RTC_SWEEP_S = 5.0               # the RTC owner sweep tick (rtc/shell.rs)
WAV_CHECK_S = 0.1               # recordings: the owner is checked every 100 ms (plus the time to see the connection end)
WAV_RESIDUAL_BYTES = 49 * 1024  # "under 49 KiB buffered" after the end (review-248, 3)
SSE_EVENT_NAMES = ("hello", "bye", "session", "room", "status", "data", "config", "ids", "lag")

# ---------------------------------------------------------------- the WAV reader (a raw socket, not the browser)
#
# First rig run (wip-a31709a, out/20261110T220224Z): a browser fetch reader cannot hold a WAV body back. The 2.4 MB body (a 150 s over) arrived in
# about 130 ms at the proxy whatever the page did, because the browser's network stack and the autotuned TCP buffers swallow it (Chromium handed the page
# 2 MiB in ONE chunk at 3 s); the server-side transfer was over, its slot free, so a "third concurrent body" was accepted (429 only when the requests
# overlapped within that 130 ms). The bodies are therefore read by the tester over a TLS socket with a tiny receive buffer and its own throttle / hold,
# with the cookie and User-Agent of the browser login (the session is bound to the UA); nothing is buffered on the way beyond a few hundred KB.


class RawWav(threading.Thread):
    """GET a path on host:port over TLS (or plain when ca is None) with the given Cookie and User-Agent and read the body slowly. After the
    response headers: `hold` true reads nothing until release(); `rate` > 0 sleeps len/rate seconds after each read (bytes per second); release()
    drains the rest at full speed. snapshot() gives the dict the verdict functions read: status, length, got, done, err, code (a refusal's problem
    code), retry_after, t_start / t_end (time.time() ms)."""

    def __init__(self, host, port, path, cookie, user_agent, ca=None, rate=0.0, hold=False, rcvbuf=4096, timeout=30.0, extra_headers=None):
        super().__init__(daemon=True)
        self.host, self.port, self.path, self.cookie, self.ua = host, port, path, cookie, user_agent
        self.ca, self.rate, self.hold, self.rcvbuf, self.timeout = ca, float(rate), bool(hold), rcvbuf, timeout
        self.extra = dict(extra_headers or {})
        self.lock = threading.Lock()
        self.state = {"status": None, "length": None, "got": 0, "done": False, "err": None, "code": None, "retry_after": None,
                      "t_start": None, "t_end": None, "headers_at": None}
        self.headers_seen = threading.Event()

    def snapshot(self):
        with self.lock:
            return dict(self.state, hold=self.hold, rate=self.rate)

    def release(self):
        with self.lock:
            self.hold, self.rate = False, 0.0

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _request(self):
        lines = ["GET %s HTTP/1.1" % self.path, "Host: %s" % self.host, "User-Agent: %s" % self.ua, "Accept: */*",
                 "Sec-Fetch-Site: same-origin", "Connection: close"]
        if self.cookie:
            lines.append("Cookie: %s" % self.cookie)
        lines += ["%s: %s" % kv for kv in self.extra.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    def run(self):
        sock = None
        self._set(t_start=time.time() * 1000.0)
        try:
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)          # before connect: the window the server sees
            raw.settimeout(self.timeout)
            raw.connect((self.host, self.port))
            if self.ca is not None:
                ctx = ssl.create_default_context(cafile=self.ca)
                sock = ctx.wrap_socket(raw, server_hostname=self.host)
            else:
                sock = raw
            sock.sendall(self._request())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("closed before the response headers")
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            status, headers = parse_head(head)
            length = headers.get("content-length")
            self._set(status=status, length=int(length) if length is not None and length.isdigit() else None,
                      retry_after=headers.get("retry-after"), headers_at=time.time() * 1000.0)
            self.headers_seen.set()
            got = len(body)
            self._set(got=got)
            if status != 200:
                rest = body
                sock.settimeout(2.0)
                try:
                    while True:
                        more = sock.recv(4096)
                        if not more:
                            break
                        rest += more
                except (socket.timeout, OSError):
                    pass
                self._set(code=problem_code_of(rest), done=True, t_end=time.time() * 1000.0)
                return
            sock.settimeout(self.timeout)
            while True:
                while self.hold:
                    time.sleep(0.02)
                data = sock.recv(8192)
                if not data:
                    break
                got += len(data)
                self._set(got=got)
                if self.rate > 0:
                    time.sleep(len(data) / self.rate)
            self._set(done=True, t_end=time.time() * 1000.0)
        except (OSError, ConnectionError, ssl.SSLError) as exc:
            self._set(err="%s: %s" % (type(exc).__name__, str(exc)[:120]), done=True, t_end=time.time() * 1000.0)
            self.headers_seen.set()
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def wait_headers(self, timeout=15.0):
        """The snapshot once the response headers (or a failure) are in; None after `timeout`."""
        return self.snapshot() if self.headers_seen.wait(timeout) else None


def parse_head(head):
    """(status, {lower-case header: value}) of a response head (bytes)."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return status, headers


def problem_code_of(body):
    """The `code` of a problem+json body (bytes), None when it has none."""
    import json
    try:
        value = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    return value.get("code") if isinstance(value, dict) else None


# ---------------------------------------------------------------- JavaScript run in the page

JS_SSE_START = """() => {
  const st = {opened: 0, errors: 0, names: {}, bye: null, t_bye: null, t_error: null, t_start: Date.now()};
  const es = new EventSource('/api/v1/operator/stream');
  es.onopen = () => { st.opened += 1; };
  es.onerror = () => { st.errors += 1; if (st.t_error === null) st.t_error = Date.now(); };
  for (const name of %s) {
    es.addEventListener(name, (e) => { st.names[name] = (st.names[name] || 0) + 1;
      if (name === 'bye') { st.bye = e.data; st.t_bye = Date.now(); } });
  }
  window.__sidSse = {st, es};
  return true;
}""" % list(SSE_EVENT_NAMES)

JS_SSE_STATE = """() => {
  const s = window.__sidSse;
  if (!s) return null;
  return Object.assign({}, s.st, {ready: s.es.readyState, now: Date.now()});
}"""

JS_STEP_UP = """async (body) => {
  const s = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});
  if (s.status !== 200) return {status: s.status, csrf_before: null};
  const csrf = (await s.json()).csrf;
  const r = await fetch('/api/v1/auth/session/step-up', {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json', 'X-DV-CSRF': csrf}, body: JSON.stringify(body)});
  return {status: r.status, csrf_before: csrf};
}"""

JS_SESSION = """async () => {
  const r = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return {status: r.status, csrf: body && body.csrf ? body.csrf : null};
}"""

JS_GET_STATUS = """async (path) => { const r = await fetch(path, {credentials: 'same-origin'}); return r.status; }"""

# an RTC circuit that is only OFFERED (a recvonly audio transceiver, non-trickle offer, the answer is not applied): the server holds a circuit
# record owned by the login, which is what the cap counts. The lease is kept alive by __sidAlive.
JS_OFFER_ONLY = """async ({room, csrf}) => {
  const pc = new RTCPeerConnection();
  pc.addTransceiver('audio', {direction: 'recvonly'});
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await new Promise((resolve) => { if (pc.iceGatheringState === 'complete') resolve(); else {
    pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') resolve(); });
    setTimeout(resolve, 4000); } });
  const r = await fetch('/api/v1/rtc/offer', {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json', 'X-DV-CSRF': csrf},
    body: JSON.stringify({sdp: pc.localDescription.sdp, room})});
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  (window.__sidPcs = window.__sidPcs || []).push(pc);
  return {status: r.status, sess_id: body && body.sess_id ? body.sess_id : null, code: body && body.code ? body.code : null,
          retry_after: r.headers.get('retry-after')};
}"""

JS_ALIVE_START = """({ids, csrf}) => {
  if (window.__sidAlive) clearInterval(window.__sidAlive.timer);
  const state = {ids, csrf, ok: 0, bad: [], last: null};
  const tick = async () => { for (const id of state.ids) {
    try { const r = await fetch('/api/v1/rtc/alive', {method: 'POST', credentials: 'same-origin',
      headers: {'content-type': 'application/json', 'X-DV-CSRF': state.csrf}, body: JSON.stringify({sess_id: id})});
      if (r.status === 204) state.ok += 1; else state.bad.push([id.slice(0, 8), r.status]); } catch (e) { state.bad.push([id.slice(0, 8), String(e)]); } } };
  state.timer = setInterval(tick, 4000);
  window.__sidAlive = state;
  return true;
}"""

JS_ALIVE_SET_CSRF = """(csrf) => { if (window.__sidAlive) window.__sidAlive.csrf = csrf; return !!window.__sidAlive; }"""
JS_ALIVE_STATE = """() => window.__sidAlive ? {ok: window.__sidAlive.ok, bad: window.__sidAlive.bad.slice(-6)} : null"""

# POST /rtc/alive or /rtc/close from a tab with the CURRENT csrf token (the stale one of another tab is the WEBSID1b defect)
JS_RTC_POST = """async ({path, id}) => {
  const s = await (await fetch('/api/v1/auth/session', {credentials: 'same-origin'})).json();
  const r = await fetch(path, {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json', 'X-DV-CSRF': s.csrf}, body: JSON.stringify({sess_id: id})});
  return r.status;
}"""

# revoke the current login by its public id (GET /user/me/sessions: {total, items:[{id, current, age_s, idle_s}]}, DELETE .../{id}: 200 {ok:true})
JS_REVOKE_OWN = """async () => {
  const s = await (await fetch('/api/v1/auth/session', {credentials: 'same-origin'})).json();
  const list = await (await fetch('/api/v1/user/me/sessions', {credentials: 'same-origin'})).json();
  const mine = (list.items || []).find((x) => x.current);
  if (!mine) return {t: Date.now(), status: null, found: false, total: list.total};
  const t = Date.now();
  const r = await fetch('/api/v1/user/me/sessions/' + mine.id, {method: 'DELETE', credentials: 'same-origin',
    headers: {'content-type': 'application/json', 'X-DV-CSRF': s.csrf}, body: '{}'});
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return {t, status: r.status, found: true, ok: body && body.ok === true};
}"""

JS_ICE = """async (id) => { const r = await fetch('/api/v1/rtc/ice', {credentials: 'same-origin', headers: {'X-DV-RTC-Session': id}}); return r.status; }"""

# ---------------------------------------------------------------- verdicts (pure)


def probe_verdict(got_hello, bye, ended, ran_s, client_timeout_s, margin_s=1.5):
    """The verdict of ONE probe attempt (an HTTP-client operator stream watched across a step-up): ("follows", ...) the stream was still open at the end of
    the window; ("bye", ...) the server said `session ended`; ("dropped", ...) the connection ended by itself without a bye before the client's own timeout;
    ("client_timeout", ...) the stream ended because the probe's CLIENT read timeout expired (run 20261111T040647Z: the step-up waited 27 s for a TOTP step,
    27 s + 18 s overran the 40 s timeout, and the proxy log shows the stream open for exactly 40007 ms) and ("no_hello", ...) the stream never opened. The last
    two say nothing about the image."""
    if not got_hello:
        return "no_hello", "the operator stream sent no hello"
    if bye:
        return "bye", "the server ended the stream with a `bye` (session ended)"
    if ended:
        if ran_s is not None and ran_s >= client_timeout_s - margin_s:
            return "client_timeout", "the probe's own %.0f s read timeout ended the stream after %.1f s" % (client_timeout_s, ran_s)
        return "dropped", "the connection ended after %s s without a `bye`" % ("?" if ran_s is None else "%.1f" % ran_s)
    return "follows", "the stream was still open"


def probe_decision(kinds):
    """(follows, reason) from the verdict kinds of the attempts, oldest first: any "follows" wins; a `bye` is final; a second "dropped" in a row is final;
    anything else is inconclusive (follows None)."""
    if "follows" in kinds:
        return True, "an attempt kept the stream open across the step-up"
    if "bye" in kinds:
        return False, "the server ended an operator stream with `bye` at a step-up"
    if len(kinds) >= 2 and kinds[-1] == "dropped" and kinds[-2] == "dropped":
        return False, "the operator stream was dropped without a `bye` in two attempts in a row"
    return None, "inconclusive after %d attempt(s): %s" % (len(kinds), ", ".join(kinds) or "none")


def sse_alive_problems(state):
    """What is wrong with a stream that must still be open after a step-up: it opened, its readyState is OPEN (1), no error event, no `bye`."""
    if not isinstance(state, dict):
        return ["the page has no SSE state"]
    out = []
    if not state.get("opened"):
        out.append("the stream never opened")
    if state.get("ready") != 1:
        out.append("readyState %r, want 1 (OPEN)" % (state.get("ready"),))
    if state.get("errors"):
        out.append("%d error event(s)" % state["errors"])
    if "bye" in (state.get("names") or {}):
        out.append("a `bye` event arrived: %s" % (state.get("bye"),))
    return out


def sse_ended(state):
    """True when the stream is over: a `bye` event, or readyState CLOSED (2), or an error event after which the reconnect was refused."""
    if not isinstance(state, dict):
        return False
    return "bye" in (state.get("names") or {}) or state.get("ready") == 2


def sse_end_ms(state):
    """The browser clock time (ms) the stream ended, from the `bye` event, else its first error; None while it is open."""
    if not isinstance(state, dict):
        return None
    return state.get("t_bye") or (state.get("t_error") if sse_ended(state) else None)


def wav_progress_problems(state, expect_complete):
    """What is wrong with a WAV download state. With expect_complete the body must have arrived whole: status 200, done, no error and got ==
    Content-Length (not truncated). Without it only a refusal-free start is checked (status 200)."""
    if not isinstance(state, dict):
        return ["the page has no WAV state"]
    out = []
    if state.get("status") != 200:
        out.append("status %r, want 200" % (state.get("status"),))
        return out
    if state.get("err"):
        out.append("read error: %s" % state["err"])
    if expect_complete:
        if not state.get("done"):
            out.append("the body is not finished")
        if isinstance(state.get("length"), int) and state.get("length") >= 0 and state.get("got") != state["length"]:
            out.append("received %s of %s bytes (truncated)" % (state.get("got"), state["length"]))
    return out


def wav_cut(state):
    """True when a started body was cut short: a read error, or done with fewer bytes than the Content-Length."""
    if not isinstance(state, dict) or state.get("status") != 200:
        return False
    if state.get("err"):
        return True
    length = state.get("length")
    return bool(state.get("done")) and isinstance(length, int) and length >= 0 and state.get("got", 0) < length


def wav_fits_buffers(state, buffered_limit=WAV_RESIDUAL_BYTES):
    """True when a body that was held (reader paused) then released arrives WHOLE although the session ended: it was fully buffered on the
    way, so the case cannot show a cut (the over is too short for the buffers); the case reports that instead of passing."""
    return isinstance(state, dict) and bool(state.get("done")) and not state.get("err") and state.get("got") == state.get("length")


def within(t_end_ms, t_event_ms, limit_s):
    """Whether something ended within limit_s seconds of the event (both browser-clock ms, the end after the event); False without an end."""
    if t_end_ms is None or t_event_ms is None:
        return False
    return (t_end_ms - t_event_ms) <= limit_s * 1000.0


def rotation_problems(old_cookie, new_cookie, old_csrf, new_csrf):
    """The cookie and the CSRF token must both change on a step-up."""
    out = []
    if not old_cookie or not new_cookie:
        out.append("no session cookie before or after")
    elif old_cookie == new_cookie:
        out.append("the session cookie did not rotate")
    if not old_csrf or not new_csrf:
        out.append("no CSRF token before or after")
    elif old_csrf == new_csrf:
        out.append("the CSRF token did not rotate")
    return out


def circuit_problems(peer_state, panel_state):
    """What is wrong with a Listen circuit that must still be connected: the page's own peer (probe) and the Listen card."""
    out = []
    if not isinstance(peer_state, dict) or peer_state.get("connection") != "connected":
        out.append("peer connection %r, want connected" % ((peer_state or {}).get("connection") if isinstance(peer_state, dict) else peer_state,))
    if panel_state != "connected":
        out.append("Listen card data-state %r, want connected" % (panel_state,))
    return out


def refusal_shape_problems(what, got, status, code=None, retry_after=None):
    """The pinned shape of a refusal: `got` is the dict the page returned (status, optional code, retry_after as the header string)."""
    out = []
    if got.get("status") != status:
        out.append("%s answered %s, want %s" % (what, got.get("status"), status))
    if code is not None and got.get("code") not in (None, code):
        out.append("%s code %r, want %r" % (what, got.get("code"), code))
    if retry_after is not None and got.get("retry_after") not in (None, str(retry_after)):
        out.append("%s Retry-After %r, want %s" % (what, got.get("retry_after"), retry_after))
    return out


def cap_refusal(statuses, cap):
    """(accepted count, refused list) of the offer statuses of one login: the first `cap` must be 200, every one after must not be."""
    ok = [s for s in statuses[:cap]]
    over = [s for s in statuses[cap:]]
    problems = []
    if any(s != 200 for s in ok):
        problems.append("the first %d offers were not all accepted: %s" % (cap, ok))
    if any(s != CIRCUIT_REFUSED for s in over):
        problems.append("an offer beyond the cap of %d was not refused with %d: %s" % (cap, CIRCUIT_REFUSED, over))
    return problems


def coalescing_problems(records, base_count, expected_new, require_suppressed):
    """Denial records of one session / action / route in an audit tail, oldest first (records: list of dicts with `result` and an optional
    `suppressed`): the number of NEW records since the count `base_count` must be `expected_new`; with require_suppressed the newest record carries
    `suppressed` >= 1 (the denials coalesced before it)."""
    out = []
    new = records[base_count:]
    if len(new) != expected_new:
        out.append("%d new denial record(s), want %d" % (len(new), expected_new))
    if require_suppressed:
        newest = new[-1] if new else None
        if newest is None or not isinstance(newest.get("suppressed"), int) or newest["suppressed"] < 1:
            out.append("the newest record carries no `suppressed` >= 1: %s" % ({k: v for k, v in (newest or {}).items() if k in ("result", "suppressed")},))
    return out
