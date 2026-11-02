# SPDX-License-Identifier: MIT
"""Web unit: the RTC circuit identity and the guest lease of DV-Router K22 / K22b (docs/web-k22-cases.md). Needs
`scripts/run.sh --profile web,browser` with `rig/config/dv-router/guest.toml` ([web] guest_listen = true) in
DVT_SUT_CONFIG_EXTRA and DVT_USRP_DYNAMIC=1; no NAT. One engine (Playwright Chromium): the circuit, lease and edge
rules do not depend on the engine; the engine matrix is the job of the NAT and WebRTC files.

DV-Router documents read: docs/design/web.md ("WebRTC edge" signalling, lease table, guest renewal, revocation),
docs/api/openapi-web.yaml (`/rtc/close`, `/rtc/alive`). The browser is the only client that can make a peer the
edge calls connected (DTLS), so every circuit here is made by the real page; the lease calls are made from inside
the same browser context (same IP, same User-Agent, same cookie jar), which is what the ownership rules key on.

  a  test_revoke_removes_exactly_that_peer        close of one circuit while another peer listens: only that peer ends
  b  test_repeated_revoke_is_a_no_op              the same close again: success or the 403 of a removed record, the
                                                  other peer is untouched
  c  test_guest_lease_without_a_peer              guest: renewed while the peer is up; after the peer is gone 204
                                                  without renewal, then 401 at expiry, then 403 once cleaned up
  c2 test_silently_closed_peer_leaves_the_edge    EDG1 (K1 fixed, DV-Router wip dbedd14): a peer closed with no revoke leaves the
                                                  edge (gauge, guest lease) inside the documented bound; needs an image with EDG1
  c3 test_closing_only_the_ctl_channel_removes_the_peer
                                                  EDG1: closing just the ctl DataChannel removes the WHOLE peer; others keep hearing
  d  test_foreign_and_malformed_circuits          other guest browser / login / guest-vs-login: 403; bad id: 400
  e  test_guest_b_churn_does_not_stop_guest_a     K22b: peers coming and going do not stop guest A's renewal
  f  test_status_stream_is_quiet_while_idle       no per-5 s event on the public stream with a WebRTC edge up (slow)
  g  test_edge_kill_stops_guest_renewal           SIGKILL of dv-edge-webrtc under a guest (no Disconnect): no renewal
                                                  past the freshness bound; the old guest never renews on the new edge
  h  test_status_hub_outage_ends_guests           dv-svc-status killed again and again for 40 s: guests end
  i  test_offer_right_after_a_hub_restart         an offer made right after the hub restarted

Skipped, DV-Router decided (docs/web-k22-cases.md): the new web service with an old hub in one image; several WebRTC
edges (a node has exactly one [edges.webrtc]), so the rtc_id filter is tested with one edge and several peers.
Differences from the documents are recorded with web.finding(), never encoded.
"""
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

from browser_fixtures import BrowserPage  # noqa: E402
from dvtest import k22, metrics, web  # noqa: E402
from webrtc_support import Rtc, Talk, tone_frames, usrp_client  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc]

ENGINE = "chromium"
ROOM = "H"
PROTO_CALL = "N0TEST"
# EDG1 bounds (web.md ICE1a and EDG1; K22 notes): the edge removes a peer at once on DTLS close_notify / SCTP loss / a closed ctl
# channel, at the latest 15 s after the last STUN request (its own timeout) or on ICE Disconnected; the gauge is published every
# ~10 s and the hub caches it (K22 saw up to 35 s), so a removal is SEEN on dv_edge_webrtc_listeners within 15 + 10 + hub caching;
# the case allows 40 s. A guest's lease then ends like in test_guest_lease_without_a_peer (stale proof 15 s + lease 30 s + 10 s
# margin), counted from the 15 s STUN timeout at the latest.
EDG1_GAUGE_S = 40.0
EDG1_STUN_S = 15.0
OTHER_UA = "dvt-k22-other-browser/1.0"
LEASE_S = 30.0              # web.md: authenticated circuits renew to 30 s; the guest lease after a renewal is recorded
INITIAL_LEASE_S = 15.0
EDGE_UP_BUDGET_S = 90.0
IDLE_S = int(os.environ.get("DVT_K22_IDLE_S", "600"))
LISTENERS = "dv_edge_webrtc_listeners"
_SEEN = set()


def _error_code(resp):
    """The short error code or name of an error answer body (nothing else of the body), or None."""
    try:
        body = resp.json()
    except Exception:                                       # noqa: BLE001
        return None
    if not isinstance(body, dict):
        return None
    inner = body.get("error") if isinstance(body.get("error"), dict) else body
    for key in ("code", "error", "kind"):
        val = inner.get(key)
        if isinstance(val, str) and len(val) <= 40:
            return val
    return None


def note(key, value):
    """A measurement: appended to $DVT_OUT/web/k22-results.jsonl and shown once as a FINDING line."""
    path = Path(os.environ.get("DVT_OUT", "out")) / "web" / "k22-results.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "value": value}, sort_keys=True) + "\n")
    except OSError:
        pass
    if key not in _SEEN:
        _SEEN.add(key)
        web.finding("K22: %s: %s" % (key, value if isinstance(value, str) else json.dumps(value, sort_keys=True)))


# ---------------------------------------------------------------------------------------- page helpers

POST_JS = """async ({path, body, csrf}) => {
  const headers = {'content-type': 'application/json', 'x-dvt-k22': '1'};
  if (csrf) {
    const s = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});
    if (s.status === 200) headers['X-DV-CSRF'] = (await s.json()).csrf;
  }
  const r = await fetch(path, {method: 'POST', credentials: 'same-origin', headers, body: JSON.stringify(body)});
  return r.status;
}"""

POLL_JS = """({id, every, csrf}) => {
  window.__k22 = [];
  window.__k22stop = false;
  let token = null;
  const tick = async () => {
    if (window.__k22stop) return;
    try {
      const headers = {'content-type': 'application/json', 'x-dvt-k22': '1'};
      if (csrf) {
        if (!token) {
          const s = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});
          if (s.status === 200) token = (await s.json()).csrf;
        }
        if (token) headers['X-DV-CSRF'] = token;
      }
      const r = await fetch('/api/v1/rtc/alive', {method: 'POST', credentials: 'same-origin', headers,
                                                    body: JSON.stringify({sess_id: id})});
      window.__k22.push([Date.now() / 1000, r.status]);
    } catch (e) { window.__k22.push([Date.now() / 1000, null]); }
    setTimeout(tick, every);
  };
  tick();
  return true;
}"""


class K22Page:
    """One browser context (a guest, or a login) with the Listen panel driver, plus the lease calls made from
    inside the context."""

    def __init__(self, engine_browsers, web_base_url, request, name, user=None, user_agent=None):
        opts = {"user_agent": user_agent} if user_agent else None
        self.bp = BrowserPage(ENGINE, engine_browsers(ENGINE), web_base_url,
                              "%s-%s" % (request.node.name.replace("[", "-").rstrip("]"), name), context_options=opts)
        self.bp.install_rtc_probe()
        self.user = user
        self.name = name
        if user is not None:
            status = self.bp.api_login(user.callsign, user.password)
            if status != 200:
                self.bp.finish(True)
                pytest.fail("browser API login of %s: HTTP %s" % (user.callsign, status))
        self.rtc = Rtc(self.bp)
        self.page = self.bp.page
        self.polling = False

    # -- the Listen panel
    def open(self):
        self.rtc.open()

    def connect(self, room=ROOM, tries=4):
        """Listen / Connect; a guest refused by the quota of a circuit that is still being cleaned up waits and tries
        again (the page offers its Connect button again after a failure)."""
        last = None
        for attempt in range(tries):
            try:
                return self.rtc.connect(room, timeout_s=25.0)
            except AssertionError as exc:
                last = exc
                time.sleep(6.0)
        raise last

    @property
    def sess(self):
        return self.rtc.offers[-1] if self.rtc.offers else ""

    def peer_state(self):
        return (self.rtc.state() or {}).get("connection")

    def ctl_close(self):
        """Close ONLY the control DataChannel of the page's peer (the peer connection, ICE and DTLS stay up in the browser)."""
        return self.page.evaluate("""() => {
            const all = (window.__dvtTap && window.__dvtTap.channels) || [];
            const ctl = all.filter((c) => c.label === 'ctl');
            const ch = ctl[ctl.length - 1] || all[all.length - 1];
            if (ch) ch.close();
            return ch ? ch.label : null;
        }""")

    def pc_close(self):
        """Close the page's own RTCPeerConnection from the outside (a browser that goes away without telling the
        server: the page is not told, DTLS says goodbye if the engine sends it)."""
        return self.page.evaluate("() => { const p = window.__dvtTap.last(); if (p) p.close(); return !!p; }")

    def abort_close(self):
        """Make every POST /api/v1/rtc/close of the page fail in the network layer: the page's own revocation (its
        Disconnect, a failure, the page hide) must not decide the lease cases. The calls of this module carry the header
        x-dvt-k22 and go through."""
        def handler(route):
            if route.request.headers.get("x-dvt-k22"):
                route.continue_()
            else:
                route.abort()
        self.bp.context.route("**/api/v1/rtc/close", handler)

    def allow_close(self):
        try:
            self.bp.context.unroute("**/api/v1/rtc/close")
        except Exception:                                   # noqa: BLE001
            pass

    # -- lease calls from inside the context
    def post(self, path, sess_id):
        return self.page.evaluate(POST_JS, {"path": path, "body": {"sess_id": sess_id}, "csrf": self.user is not None})

    def alive(self, sess_id):
        return self.post("/api/v1/rtc/alive", sess_id)

    def close(self, sess_id):
        return self.post("/api/v1/rtc/close", sess_id)

    def start_poll(self, sess_id, every_ms=1000):
        self.page.evaluate(POLL_JS, {"id": sess_id, "every": every_ms, "csrf": self.user is not None})
        self.polling = True

    def series(self):
        return [(t, s) for t, s in (self.page.evaluate("() => window.__k22 ? window.__k22.slice() : []") or [])]

    def stop_poll(self):
        if self.polling:
            try:
                self.page.evaluate("() => { window.__k22stop = true; }")
            except Exception:                               # noqa: BLE001
                pass
            self.polling = False

    def end(self, request):
        """Leave nothing behind: stop polling, let the page's revocations through, close the circuits."""
        self.stop_poll()
        self.allow_close()
        for sid in set(self.rtc.offers):
            try:
                self.close(sid)
            except Exception:                               # noqa: BLE001
                pass
        rep = getattr(request.node, "rep_call", None)
        self.bp.finish(bool(rep is not None and rep.failed))


@pytest.fixture(scope="session")
def k22_env(web_base_url, web_client):
    """The guest cases need guest_listen = true (rig/config/dv-router/guest.toml)."""
    if os.environ.get("DVT_BROWSER") != "on":
        pytest.skip("browser profile not up (--profile web,browser)")
    info = web_client().get("/api/v1/public/state", expect=200).json()
    cfg = info.get("info") or {}
    if cfg.get("guest_listen") is not True:
        pytest.skip("[web] guest_listen is off: add rig/config/dv-router/guest.toml to DVT_SUT_CONFIG_EXTRA")
    return cfg


@pytest.fixture(scope="session")
def k22_users(k22_env, web_user):
    """Every account of this module at the start (the operator session that registers them does not last for the
    whole file)."""
    return {name: web_user(name, may_tx=False) for name in ("N0KA", "N0KB", "N0KC")}


@pytest.fixture
def make_page(engine_browsers, web_base_url, request, k22_users):
    made = []

    def factory(name, user=None, guest_ua=None):
        kp = K22Page(engine_browsers, web_base_url, request, name, user=k22_users[user] if user else None, user_agent=guest_ua)
        made.append(kp)
        kp.open()
        return kp

    yield factory
    for kp in reversed(made):
        try:
            kp.end(request)
        except Exception:                                   # noqa: BLE001
            pass


@pytest.fixture
def proto_peer(target, k22_env):
    peer = usrp_client(target, PROTO_CALL, ROOM)
    yield peer
    peer.close()


def received(rtc):
    inb = rtc.inbound()
    return inb["packetsReceived"] if inb else 0


def over_heard(rtc, peer, seconds=3.0):
    """Packets that reached the page during a short over of the protocol client (50 a second of audio)."""
    before = received(rtc)
    talk = Talk(peer, tone_frames(seconds))
    talk.start()
    talk.started.wait(5)
    talk.join(timeout=seconds + 8)
    time.sleep(0.6)
    return received(rtc) - before


def wait_until(pred, timeout_s, every=0.5):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(every)
    return None


def listeners(rig_control):
    return metrics.total(metrics.parse(rig_control.metrics("xlxd-sut")), LISTENERS)


def poll_until(kp, want, timeout_s, since=0.0):
    """Wait until the in-page poll has seen status `want` after epoch `since`; the epoch seconds, or None."""
    def seen():
        for t, s in kp.series():
            if t >= since and s == want:
                return t
        return None
    return wait_until(seen, timeout_s, every=1.0)


# ---------------------------------------------------------------------------------------- a, b

def test_revoke_removes_exactly_that_peer(make_page, proto_peer, rig_control):
    """K22 (web.md "Revocation", openapi `/rtc/close`): "Revocation drops exactly the edge peers mapped to the circuit RTC
    id". Two logged-in listeners; the circuit of A is closed through the API while the page of A is left alone (its own
    peer stays up in the browser): the browser of A must find its peer dropped by the edge, the peer of B must keep
    its state and its audio, and the listener gauge must fall by one."""
    a = make_page("a", user="N0KA")
    b = make_page("b", user="N0KB")
    a.connect()
    b.connect()
    a.abort_close()                                       # the page's own revocation after the edge drops it must not interfere
    assert a.sess and b.sess and a.sess != b.sess
    b.rtc.tap_start()
    assert over_heard(b.rtc, proto_peer) >= 100, "B hears nothing before the revoke"
    total_before = wait_until(lambda: (lambda n: n if n >= 2 else None)(listeners(rig_control)), 40.0, every=2.0)
    note("revoke: listeners gauge before", total_before)

    t0 = time.monotonic()
    status = a.close(a.sess)
    assert status == 204, "POST /rtc/close of an owned live circuit answered %s (documented 204)" % status
    gone = wait_until(lambda: a.peer_state() != "connected", 30.0, every=0.25)
    took = round(time.monotonic() - t0, 2) if gone else None
    note("revoke: A's browser leaves 'connected' after (s); the page itself closes at 'ownership expired' (alive 401), so "
         "the edge-side proof is the gauge below", {"seconds": took, "state": a.peer_state(), "listen": a.rtc.status()})
    assert gone, "30 s after the close of A's circuit the browser of A still has a connected peer"

    assert b.peer_state() == "connected", "closing A's circuit changed B's peer: %s" % b.peer_state()
    heard = over_heard(b.rtc, proto_peer)
    assert heard >= 100, "B stopped hearing after the revoke of A (%d packets)" % heard
    assert b.alive(b.sess) == 204, "B's own lease call after the revoke of A"
    if total_before is not None:
        after = wait_until(lambda: (lambda n: n if n <= total_before - 1 else None)(listeners(rig_control)), 40.0, every=2.0)
        note("revoke: listeners gauge after", after)
        assert after is not None, "the listener gauge did not fall from %s after the revoke of one of two" % total_before
        assert after >= 1, "the gauge fell to %s: B's peer was removed too" % after
    # the answer for A's circuit afterwards: revoked and retained is 401, cleaned up is 403
    later = a.alive(a.sess)
    note("revoke: alive on the revoked circuit", later)
    assert later in (401, 403), "alive on a closed circuit answered %s (documented 401 while retained, 403 once removed)" % later


def test_repeated_revoke_is_a_no_op(make_page, proto_peer):
    """K22: "no matching peer is a successful no-op" at the edge; over HTTP a second close of the same circuit is 204
    while the record is still retained and the 403 of an unknown circuit once cleanup removed it. The other peer is
    untouched."""
    a = make_page("a", user="N0KA")
    b = make_page("b", user="N0KB")
    a.connect()
    b.connect()
    a.abort_close()
    b.rtc.tap_start()
    first = a.close(a.sess)
    second = a.close(a.sess)
    time.sleep(8.0)                                      # past a sweep (5 s) of the cleanup
    third = a.close(a.sess)
    note("repeated revoke: statuses of three closes", [first, second, third])
    assert first == 204
    assert second in (204, 403), "second close answered %s" % second
    assert third in (204, 403), "third close answered %s" % third
    assert b.peer_state() == "connected"
    assert over_heard(b.rtc, proto_peer) >= 100, "B stopped hearing after repeated revokes of A"
    assert b.alive(b.sess) == 204


# ---------------------------------------------------------------------------------------- c

def kick_newest_peer(operator):
    """Operator kick (POST /api/v1/operator/edges/webrtc/clients/{circuit}/kick, web.md: "Web circuits appear in the
    edge's sessions and events, so operator kick ... work as for any edge") of the newest WebRTC row of
    GET /api/v1/operator/clients. The edge removes the peer and reports its Disconnect; the web service's circuit
    record is not revoked by it. Returns the row (never an address)."""
    rows = operator.get("/api/v1/operator/clients?protocol=webrtc&limit=50", expect=200).json().get("items") or []
    rows = [r for r in rows if "webrtc" in str(r.get("protocol", "")) + str(r.get("edge", ""))]
    assert rows, "the operator client list shows no WebRTC row while a guest listens"
    row = max(rows, key=lambda r: r.get("since_ms", 0))
    operator.post("/api/v1/operator/edges/webrtc/clients/%s/kick" % row["circuit"], {}, expect=200)
    return {k: row.get(k) for k in ("protocol", "kind", "module", "circuit", "since_ms")} | {"rows": len(rows)}


def test_guest_lease_without_a_peer(make_page, web_operator):
    """K22: a guest lease renews only with a live mapped peer; "an owned unexpired guest record still returns 204 with
    its existing deadline, then receives 401 at normal lease expiry"; once cleanup removed it the id is 403. The peer is
    removed at the edge by the operator kick (a Disconnect for the peer, no revoke of the circuit record), and the
    page's own revocation is blocked, so only the lease rules decide."""
    g = make_page("guest")
    g.connect()
    g.abort_close()
    sid = g.sess
    g.start_poll(sid)
    time.sleep(INITIAL_LEASE_S + LEASE_S)                # longer than the initial 15 s lease and a 30 s lease
    healthy = g.series()
    bad = [(round(t - healthy[0][0], 1), s) for t, s in healthy if s != 204]
    assert healthy and not bad, "a guest with a live peer was refused or ended after %s s: %s" % (INITIAL_LEASE_S + LEASE_S, bad[:5])

    t_gone = time.time()
    row = kick_newest_peer(web_operator)
    note("guest lease: operator kick of the guest's peer", row)
    t401 = poll_until(g, 401, 120.0, since=t_gone)
    series = g.series()
    after = [(t, s) for t, s in series if t >= t_gone]
    summary = k22.alive_summary(after, t_gone)
    note("guest lease: answers after the peer went away", {"first": summary["first"], "runs": summary["runs"][:6],
                                                          "browser peer": g.peer_state()})
    assert t401 is not None, "no 401 within 120 s of the peer going away: the guest lease was renewed without a peer: %s" % summary["runs"][:4]
    ended_after = round(t401 - t_gone, 1)
    note("guest lease: 401 after the peer went away (s)", ended_after)
    assert ended_after <= k22.FRESH_S + LEASE_S + 10.0, \
        "401 came %.1f s after the peer went away (stale proof 15 s + lease 30 s + margin)" % ended_after
    if summary["first"].get(204) is None or summary["first"][204] > ended_after:
        web.finding("K22: guest lease: no 204 after the peer went away before the 401 (first 204 at %s s)" % summary["first"].get(204))
    t403 = poll_until(g, 403, 90.0, since=t401)
    series = [(t, s) for t, s in g.series() if t >= t_gone]
    summary = k22.alive_summary(series, t_gone)
    note("guest lease: 403 after cleanup (s)", round(t403 - t_gone, 1) if t403 else None)
    assert summary["monotonic"], "statuses went back: %s" % summary["runs"]
    assert t403 is not None, "the expired guest id never turned 403 (cleanup) within 90 s: %s" % summary["runs"][-3:]


def stable_listeners(rig_control, at_least=1, budget_s=70.0):
    """The gauge once two readings 6 s apart agree and are >= at_least (the edge publishes every ~10 s and the hub caches the
    snapshot, so a fresh connect shows up late); None when it never settled."""
    end = time.monotonic() + budget_s
    last = None
    while time.monotonic() < end:
        now = listeners(rig_control)
        if now >= at_least and now == last:
            return now
        last = now
        time.sleep(6.0)
    return None


def gauge_below(rig_control, level, timeout_s, every=2.0):
    """Seconds (from now) until the gauge reads below `level`, sampled every `every` s; None after timeout_s."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if listeners(rig_control) < level:
            return round(time.monotonic() - t0, 1)
        time.sleep(every)
    return None


@pytest.mark.slow
@pytest.mark.parametrize("who,after_s", [("user", 0), ("guest", 0), ("guest", 45)], ids=["user-at-once", "guest-at-once", "guest-after-45s"])
def test_silently_closed_peer_leaves_the_edge(make_page, rig_control, who, after_s):
    """EDG1 (DV-Router K1, fixed in wip dbedd14; needs an image with it, an older image fails this case): the edge acts on
    Event::Closed (DTLS close_notify, SCTP loss), so a browser that closes its RTCPeerConnection with no revoke (pc.close(),
    the page's own close blocked) loses its peer at the edge at once; at the latest the 15 s STUN silence timeout removes
    it. Asserted: the listener gauge (published every ~10 s, hub caching up to 35 s in K22, so allow EDG1_GAUGE_S = 40 s)
    drops below its value before the close, and for a guest that keeps calling /rtc/alive the lease stops renewing: a 401
    follows within the STUN timeout + stale proof 15 s + lease 30 s + 10 s margin. The measured seconds are recorded."""
    kp = make_page("peer", user="N0KA" if who == "user" else None)
    kp.connect()
    kp.abort_close()
    kp.start_poll(kp.sess)                              # keeps asking for its lease meanwhile (a logged-in circuit that stopped would be
    time.sleep(after_s)                                 # swept by its own 30 s lease, which would hide a peer the edge forgot to remove)
    before = stable_listeners(rig_control)
    assert before, "the listener gauge never showed the connected peer (>= 1 and steady) within 70 s"
    assert kp.pc_close()
    t_close = time.time()
    gone_s = gauge_below(rig_control, before, EDG1_GAUGE_S + 20.0)
    key = "silent close (%s, closed %d s after connecting)" % (who, after_s)
    note(key + ": seconds until the edge listeners gauge fell below %s (EDG1 bound %d s)" % (before, EDG1_GAUGE_S), gone_s)
    assert gone_s is not None and gone_s <= EDG1_GAUGE_S, \
        "the edge still counted the peer %s s after its browser closed the connection silently (gauge %s before): EDG1 says it " \
        "leaves at once (close_notify) or after 15 s without STUN, seen within %d s" % (gone_s or ">%d" % (EDG1_GAUGE_S + 20), before, EDG1_GAUGE_S)
    if kp.user is None:
        t401 = poll_until(kp, 401, 75.0, since=t_close)
        series = [(t, st) for t, st in kp.series() if t >= t_close]
        summary = k22.alive_summary(series, t_close)
        note(key + ": guest lease answers after pc.close()", {"first": summary["first"], "runs": summary["runs"][:6]})
        assert t401 is not None, "the guest lease was still renewed 75 s after its peer closed silently: %s" % summary["runs"][:4]
        ended = round(t401 - t_close, 1)
        note(key + ": 401 after pc.close() (s)", ended)
        bound = EDG1_STUN_S + k22.FRESH_S + LEASE_S + 10.0
        assert ended <= bound, "401 came %.1f s after the silent close (STUN timeout 15 + stale proof 15 + lease 30 + margin = %.0f s)" % (ended, bound)
        assert summary["monotonic"], "statuses went back: %s" % summary["runs"]


def test_closing_only_the_ctl_channel_removes_the_peer(make_page, proto_peer, rig_control):
    """EDG1 (DV-Router, wip dbedd14; needs an image with it): a closed ctl DataChannel goes through the edge's removal like a
    closed connection, so it removes the WHOLE peer; it used to keep listening. Two logged-in listeners A and B hear a protocol
    talker; only A's ctl channel is closed (page.evaluate on the channel the page opened; the peer connection stays up in the
    browser, the page's own revoke is blocked). Asserted: the gauge falls by one within EDG1_GAUGE_S = 40 s, A hears nothing from
    a later over while B hears it in full, and B's lease and peer are untouched."""
    a = make_page("a", user="N0KA")
    b = make_page("b", user="N0KB")
    a.connect()
    b.connect()
    a.abort_close()
    a.rtc.tap_start()
    b.rtc.tap_start()
    assert over_heard(a.rtc, proto_peer) >= 100, "A hears nothing before its ctl channel is closed"
    assert over_heard(b.rtc, proto_peer) >= 100, "B hears nothing before"
    before = stable_listeners(rig_control, at_least=2)
    assert before and before >= 2, "the gauge never showed both peers (>= 2, steady): %s" % before
    closed = a.ctl_close()
    assert closed, "the page of A has no data channel to close"
    note("ctl channel close: channel closed", closed)
    gone_s = gauge_below(rig_control, before, EDG1_GAUGE_S + 20.0)
    note("ctl channel close: seconds until the gauge fell below %s (EDG1 bound %d s)" % (before, EDG1_GAUGE_S), gone_s)
    assert gone_s is not None and gone_s <= EDG1_GAUGE_S, \
        "the edge still counted both peers %s s after only A's ctl channel was closed (EDG1: the whole peer goes)" % (gone_s or ">%d" % (EDG1_GAUGE_S + 20))
    assert listeners(rig_control) >= 1, "the gauge fell to 0: B's peer went too"
    time.sleep(2.0)
    a0, b0 = received(a.rtc), received(b.rtc)
    talk = Talk(proto_peer, tone_frames(3.0))
    talk.start()
    talk.started.wait(5)
    talk.join(timeout=11.0)
    time.sleep(0.6)
    heard_a, heard_b = received(a.rtc) - a0, received(b.rtc) - b0
    note("ctl channel close: packets heard in a 3 s over after the removal (A, B)", [heard_a, heard_b])
    assert heard_a < 10, "A still heard %d packets of a 3 s over after its peer should have been removed" % heard_a
    assert heard_b >= 100, "B heard only %d packets: closing A's ctl channel disturbed an ordinary listener" % heard_b
    assert b.peer_state() == "connected", "B's peer is %s" % b.peer_state()
    assert b.alive(b.sess) == 204, "B's own lease call"
    note("ctl channel close: A's browser peer state afterwards", a.peer_state())


# ---------------------------------------------------------------------------------------- d

def test_foreign_and_malformed_circuits(make_page, k22_users, web_base_url, web_ca):
    """Ownership: "Unknown or removed circuits and another login or different guest IP/User-Agent all receive 403
    forbidden"; invalid hexadecimal ids receive 400; a login cannot use a guest circuit and a guest cannot use a login's."""
    guest = make_page("guest")
    other = make_page("other-guest", guest_ua=OTHER_UA)
    user = make_page("user", user="N0KA")
    guest.connect()
    user.connect()
    sg, su = guest.sess, user.sess
    answers = {
        "other guest browser alive on guest circuit": other.alive(sg),
        "other guest browser close of guest circuit": other.close(sg),
        "login alive on guest circuit": user.alive(sg),
        "guest alive on login circuit": guest.alive(su),
        "guest close of login circuit": guest.close(su),
    }
    # N0KB's tester login of the fixture is the OLDEST session of the account by now: the earlier cases log N0KB in
    # three times through the browser and an account keeps at most 3 sessions (the oldest ends, its next request is
    # 401). So the foreign login is a FRESH one made right before the request (same client, same User-Agent).
    kb = k22_users["N0KB"]
    stale = kb.client.request("GET", "/api/v1/user/me", headers=None)
    fresh = web.WebClient(web_base_url, web_ca)
    fresh.hide(kb.password)
    fresh.login(kb.callsign, kb.password)
    me = fresh.request("GET", "/api/v1/user/me", headers=None)
    sessions = fresh.request("GET", "/api/v1/user/me/sessions", headers=None)
    other_login = fresh.request("POST", "/api/v1/rtc/alive", json_body={"sess_id": su}, headers=None)
    answers["other login (tester) alive on login circuit"] = other_login.status
    count = None
    if sessions.status == 200:
        body = sessions.json()
        items = body.get("sessions", body.get("items")) if isinstance(body, dict) else body
        count = len(items) if isinstance(items, list) else None
    note("foreign circuits: the other login", {"fixture_login_user_me": stale.status, "fresh_user_me": me.status,
                                               "account_sessions": count, "alive_status": other_login.status,
                                               "alive_code": _error_code(other_login)})
    answers["never-seen id"] = guest.alive("0" * 64)
    answers["bad id"] = guest.alive("zz")
    note("foreign circuits", answers)
    forbidden = [k for k, v in answers.items() if k not in ("bad id",) and v != 403]
    assert not forbidden, {k: answers[k] for k in forbidden}
    assert answers["bad id"] == 400
    # none of that hurt the owners
    assert guest.alive(sg) == 204 and user.alive(su) == 204
    assert guest.peer_state() == "connected" and user.peer_state() == "connected"


# ---------------------------------------------------------------------------------------- e

def test_guest_b_churn_does_not_stop_guest_a(make_page, proto_peer):
    """K22b: "The guest proof is cleared only by the Disconnect of its own peer", and the lease query carries the rtc_id of
    its own circuit. Guest A and a logged-in listener C stay connected while guest B connects and disconnects over and over
    (three peers at the edge, one coming and going); A polls its lease every second for longer than two lease lengths.
    A's answers must all be 204 (a lease that stopped renewing would turn 401), its peer must be connected and hear
    audio, so must C. The mechanism (the proof being cleared and restored by the next snapshot) is not visible from
    outside; the visible rule is that A's lease never lapses."""
    a = make_page("a-guest")
    b = make_page("b-guest", guest_ua=OTHER_UA)
    c = make_page("c-user", user="N0KC")
    a.connect()
    c.connect()
    a.abort_close()
    a.rtc.tap_start()
    c.rtc.tap_start()
    a.start_poll(a.sess)
    t0 = time.time()
    cycles = []
    while time.time() - t0 < 2 * LEASE_S + INITIAL_LEASE_S + 15.0:
        try:
            b.connect()
            time.sleep(1.5)
            b.rtc.disconnect()
            b.rtc.wait_idle(8.0)
            cycles.append("ok")
        except AssertionError as exc:
            cycles.append(str(exc)[:80])
    series = a.series()
    summary = k22.alive_summary(series, t0)
    note("churn: A's answers over %.0f s with %d B cycles" % (time.time() - t0, len(cycles)),
         {"runs": summary["runs"], "cycles ok": cycles.count("ok"), "other": sorted(set(cycles) - {"ok"})[:3]})
    assert summary["only"] == [204], "guest A's lease calls during B's churn: %s" % summary["runs"][:5]
    assert a.peer_state() == "connected" and c.peer_state() == "connected"
    assert over_heard(a.rtc, proto_peer) >= 100, "guest A stopped hearing"
    assert over_heard(c.rtc, proto_peer) >= 100, "listener C stopped hearing"
    assert cycles.count("ok") >= 6, "B's churn did not run: %s" % cycles[:6]


# ---------------------------------------------------------------------------------------- f

@pytest.mark.slow
def test_status_stream_is_quiet_while_idle(k22_env, web_client):
    """K22 (hub status refresh every 5 s): the public event stream must not gain an event per snapshot. With the WebRTC
    edge up and nobody listening, sample GET /api/v1/public/events for DVT_K22_IDLE_S seconds (default 600; one hour
    is the stated criterion, see docs/web-k22-cases.md), reconnecting with the last event id every 30 s; after the first
    minute no minute may hold more than 2 events (a per-5 s event is 12 a minute) and the event id may not run."""
    client = web_client()
    time.sleep(15.0)                                        # let the previous cases' circuits go
    frames_at, last_id, first_ids = [], None, []
    t0 = time.monotonic()
    while time.monotonic() - t0 < IDLE_S:
        left = IDLE_S - (time.monotonic() - t0)
        path = "/api/v1/public/events" + ("?last_event_id=%s" % last_id if last_id else "")
        start = time.monotonic() - t0
        _s, _c, frames = client.sse(path, max_events=100000, timeout=min(30.0, left))
        for fr in frames:
            frames_at.append((start, fr))
            if fr.get("id"):
                last_id = fr["id"]
                first_ids.append((start, fr["id"]))
    times = k22.event_times(frames_at)
    names = sorted({fr.get("event") for _t, fr in frames_at if fr.get("event") not in (None, "hello")})
    verdict = k22.idle_stream_verdict(times, IDLE_S)
    ids = [int(i.rsplit("-", 1)[1]) for _t, i in first_ids if i.rsplit("-", 1)[-1].isdigit()]
    note("idle stream: %d s sample" % IDLE_S, {"events": len(times), "names": names, "per_minute": verdict["per_minute"],
                                              "after_warmup": verdict["after_warmup"], "id_first": ids[0] if ids else None,
                                              "id_last": ids[-1] if ids else None})
    assert verdict["ok"], "the public stream is not quiet while idle: %s" % verdict
    if IDLE_S < 3600:
        web.finding("K22: the idle stream was sampled for %d s, not the one hour of the criterion (DVT_K22_IDLE_S=3600 repeats it)" % IDLE_S)


# ---------------------------------------------------------------------------------------- g, h, i

def wait_edge_back(make_page, name, budget_s=EDGE_UP_BUDGET_S):
    """One fresh guest (another User-Agent) offers again and again until a circuit connects (the edge or the hub is
    back); (page, seconds), or (None, seconds) when the budget is used up. One page only: each page holds an event
    stream and the service allows four per address."""
    t0 = time.monotonic()
    kp = make_page(name, guest_ua=OTHER_UA)
    while time.monotonic() - t0 < budget_s:
        try:
            kp.rtc.connect(ROOM, timeout_s=12.0)
            return kp, round(time.monotonic() - t0, 1)
        except AssertionError:
            time.sleep(2.0)
    return kp, round(time.monotonic() - t0, 1)


def test_edge_kill_stops_guest_renewal(make_page, rig_control):
    """K22 / K22b, the dropped-Disconnect approximation: SIGKILL of the dv-edge-webrtc child (never the launcher) under a
    guest that keeps calling /rtc/alive. The edge dies without a Disconnect; the stale proof is good for < 15 s. The guest
    must stop renewing inside that bound (so its lease, at most 30 s, ends: 401), never be renewed on the stale proof,
    and after the launcher restarted the edge the OLD guest must not renew against the new process (a fresh offer is
    needed)."""
    g = make_page("guest")
    g.connect()
    g.abort_close()
    sid = g.sess
    g.start_poll(sid)
    time.sleep(INITIAL_LEASE_S + 10.0)
    assert {s for _t, s in g.series()} == {204}, "the guest lease failed before the kill: %s" % k22.runs(g.series())[:3]

    killed = rig_control.kill_proc("xlxd-sut", "dv-edge-webrtc", "KILL")
    t_kill = killed.t_kill
    note("edge kill: pids", killed.pids)
    new, back = wait_edge_back(make_page, "fresh")
    if new is not None and new.peer_state() != "connected":
        new = None
    note("edge kill: a fresh guest offer connected after (s)", back if new is not None else None)
    t401 = poll_until(g, 401, 120.0, since=t_kill)
    series = [(t, s) for t, s in g.series() if t >= t_kill]
    summary = k22.alive_summary(series, t_kill)
    note("edge kill: answers of the old guest after the kill", {"first": summary["first"], "runs": summary["runs"][:6]})
    assert t401 is not None, "the old guest was still renewed 120 s after its edge was killed: %s" % summary["runs"][:4]
    ended = round(t401 - t_kill, 1)
    note("edge kill: old guest 401 after the kill (s)", ended)
    assert ended <= k22.FRESH_S + LEASE_S + 10.0, "401 only %.1f s after the kill (stale proof 15 s + lease 30 s)" % ended
    assert not k22.renewed_past(series, t_kill, k22.FRESH_S + LEASE_S, 10.0)
    if new is not None:
        in_time = back < ended
        note("edge kill: new edge before the old lease ended", in_time)
        new.start_poll(new.sess)
        time.sleep(INITIAL_LEASE_S + 8.0)
        assert {s for _t, s in new.series()} == {204}, "the fresh guest on the new edge: %s" % k22.runs(new.series())[:3]
    else:
        web.finding("K22: no fresh offer connected within %.0f s of the edge kill; the new edge was not seen" % EDGE_UP_BUDGET_S)
        pytest.fail("the launcher did not bring the WebRTC edge back within %.0f s" % EDGE_UP_BUDGET_S)


def kill_hub_for(rig_control, seconds):
    """SIGKILL the status hub again and again for `seconds` (the launcher respawns it within 0.5 to 5 s, so one kill is
    not an outage of 30 s): every respawn is killed before it can publish a snapshot. Returns (kills, t_start, t_end)."""
    kills, t_start = 0, time.time()
    while time.time() - t_start < seconds:
        try:
            rig_control.kill_proc("xlxd-sut", "dv-svc-status", "KILL")
            kills += 1
        except RuntimeError:
            pass                                            # error 3: not running right now (between respawns)
        time.sleep(0.3)
    return kills, t_start, time.time()


@pytest.mark.slow
def test_status_hub_outage_ends_guests(make_page, rig_control):
    """web.md: "a status hub outage longer than about 30 s lets all guest ownership leases expire; the sweep then revokes
    their peers". The hub is killed again and again for 40 s (a respawn is killed before it publishes a snapshot, so the
    guest proof goes stale and stays so). The guest must stop renewing (401 at lease expiry), its peer must be dropped by
    the sweep, and after the hub is back a fresh offer works and renews."""
    g = make_page("guest")
    g.connect()
    g.abort_close()
    g.start_poll(g.sess)
    time.sleep(INITIAL_LEASE_S + 10.0)
    assert {s for _t, s in g.series()} == {204}
    kills, t_start, t_end = kill_hub_for(rig_control, 40.0)
    note("hub outage: kills in 40 s", kills)
    assert kills >= 2, "the status hub was killed only %d times" % kills
    t401 = poll_until(g, 401, 90.0, since=t_start)
    series = [(t, s) for t, s in g.series() if t >= t_start]
    summary = k22.alive_summary(series, t_start)
    note("hub outage: old guest answers", {"first": summary["first"], "runs": summary["runs"][:6]})
    assert t401 is not None, "the guest was still renewed after 90 s of a dead status hub: %s" % summary["runs"][:4]
    ended = round(t401 - t_start, 1)
    note("hub outage: old guest 401 after the start of the outage (s)", ended)
    assert ended <= 40.0 + k22.FRESH_S + LEASE_S, "401 only %.1f s after the start of the hub outage" % ended
    dropped = wait_until(lambda: g.peer_state() != "connected", 60.0, every=1.0)
    note("hub outage: the guest's peer left 'connected' (s after the start)", round(time.time() - t_start, 1) if dropped else None)
    assert dropped, "the guest's peer was still connected 60 s after its lease ended"
    new, back = wait_edge_back(make_page, "after-hub")
    if new is not None and new.peer_state() != "connected":
        new = None
    note("hub outage: a fresh guest offer connected after the end of the outage (s)", back)
    assert new is not None, "no guest could connect within %.0f s after the hub outage" % EDGE_UP_BUDGET_S
    new.start_poll(new.sess)
    time.sleep(INITIAL_LEASE_S + 10.0)
    assert {s for _t, s in new.series()} == {204}, "a fresh guest after the hub outage: %s" % k22.runs(new.series())[:3]


def test_offer_right_after_a_hub_restart(make_page, rig_control):
    """web.md: "An offer made without a fresh mapping capability (for example during hub recovery) omits rtc_id. That
    guest cannot renew, even after the hub recovers, and its initial lease ends at 15 s." The hub is killed and a guest
    offers a little later (1, 2, 3.5, 5 and 7 s; the first attempt that makes a circuit is judged). Legal outcomes,
    recorded: every offer refused (503: the page says so), or a circuit without rtc_id whose lease ends at about 15 s, or
    the hub was back in time and the guest renews (the race was not hit). Only a lease that ends later than the initial
    15 s plus a sweep fails."""
    g = make_page("guest")
    g.abort_close()
    attempts = []
    made = None
    for delay in (1.0, 2.0, 3.5, 5.0, 7.0):
        t_kill = rig_control.kill_proc("xlxd-sut", "dv-svc-status", "KILL").t_kill
        time.sleep(delay)
        try:
            g.rtc.connect(ROOM, timeout_s=20.0)
            made = (delay, time.time(), t_kill)
            attempts.append({"delay_s": delay, "result": "circuit"})
            break
        except AssertionError as exc:
            attempts.append({"delay_s": delay, "result": "refused", "status": g.rtc.status(), "error": str(exc)[:60]})
            time.sleep(8.0)
    note("offer after hub restart: attempts", attempts)
    if made is None:
        pytest.skip("no circuit was made in five tries right after a hub kill: every offer was refused (the documented 503 "
                    "path; the window with an edge but without the mapping capability was not reached)")
    delay, t_offer, t_kill = made
    g.start_poll(g.sess)
    t401 = poll_until(g, 401, 60.0, since=t_offer)
    series = g.series()
    summary = k22.alive_summary(series, t_offer)
    note("offer after hub restart: answers", {"first": summary["first"], "runs": summary["runs"][:6], "offer_after_kill_s": round(t_offer - t_kill, 1)})
    if t401 is None:
        web.finding("K22: the guest that offered %.1f s after the hub kill was still renewed after 60 s: rtc_id was present "
                    "(the capability lookup had already recovered), the race of web.md was not hit" % (t_offer - t_kill))
        assert {s for _t, s in series} == {204}, k22.runs(series)[:4]
    else:
        ended = round(t401 - t_offer, 1)
        note("offer after hub restart: 401 after the offer (s)", ended)
        assert ended <= INITIAL_LEASE_S + 10.0, "the lease of a guest without rtc_id ended %.1f s after the offer (documented 15 s)" % ended
