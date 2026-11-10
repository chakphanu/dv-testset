# SPDX-License-Identifier: MIT
"""Helpers of the WEBSID1 browser cases (cases/test_web_session_rotation.py, docs/websid1-cases.md).

DV-Router WEBSID1 (merge a1ff890c, dv-svc-web): a step-up, TOTP enrolment, replacement or disable ROTATES the cookie and the CSRF token of
the same login session; the old cookie answers 401 at once; open operator SSE streams, RTC / talk leases and WAV transfers follow the stable
server-side SessionKey instead of the cookie and survive; caps count across rotations; logout, revoke, expiry, role change and disable end
the handles (SSE within one heartbeat, RTC within the 5 s sweep, a WAV body within 100 ms). Pure logic (verdicts on the page states) and the
JavaScript the cases run in the page; stdlib only. Source: DV-Router web.md "Built (WEBSID1)", review-248.
"""

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

# A WAV download read slowly: after each chunk the reader sleeps value.length / rate seconds (rate in bytes/s), so the body stays in
# progress for about length / rate seconds whatever the chunk sizes are. `hold` true reads nothing until JS_WAV_RELEASE (which also removes the throttle, to drain what is left).
JS_WAV_START = """async ({name, url, rate, hold}) => {
  window.__sidWav = window.__sidWav || {};
  const st = {name, status: null, length: null, got: 0, done: false, err: null, t_start: Date.now(), t_end: null, t_err: null, chunks: 0};
  const ctl = {hold: !!hold, rate};
  window.__sidWav[name] = {st, ctl};
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let response;
  try { response = await fetch(url, {credentials: 'same-origin', headers: {'Accept': '*/*'}}); }
  catch (e) { st.err = String(e); st.t_err = Date.now(); st.done = true; return Object.assign({}, st); }
  st.status = response.status;
  const length = response.headers.get('content-length');
  st.length = length === null ? null : Number(length);
  st.retry_after = response.headers.get('retry-after');
  if (response.status !== 200) {
    try { const body = await response.json(); st.code = body.code || null; } catch (e) { st.code = null; }
    st.done = true; st.t_end = Date.now(); return Object.assign({}, st);
  }
  const reader = response.body.getReader();
  (async () => {
    try {
      for (;;) {
        while (ctl.hold) await sleep(20);
        const {done, value} = await reader.read();
        if (done) { st.done = true; st.t_end = Date.now(); return; }
        st.got += value.length; st.chunks += 1;
        if (ctl.rate > 0) await sleep(value.length * 1000 / ctl.rate);
      }
    } catch (e) { st.err = String(e); st.t_err = Date.now(); st.done = true; }
  })();
  return Object.assign({}, st);
}"""

JS_WAV_STATE = """(name) => {
  const w = window.__sidWav && window.__sidWav[name];
  return w ? Object.assign({}, w.st, {hold: w.ctl.hold, now: Date.now()}) : null;
}"""

JS_WAV_RELEASE = """(name) => { const w = window.__sidWav && window.__sidWav[name]; if (w) { w.ctl.hold = false; w.ctl.rate = 0; } return !!w; }"""

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
