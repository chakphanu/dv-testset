# SPDX-License-Identifier: MIT
"""Helpers of the WebRTC browser cases (cases/test_web_webrtc.py, cases/test_web_recorder.py): a driver
for the Listen / Talk panel of the dashboard, the protocol side (a USRP dynamic client that talks a
tone into a room or listens to it), and readers of what the router publishes. Not a test module.

The page side is dvtest.browser.RTC_PROBE_JS (installed by BrowserPage.install_rtc_probe): it wraps
RTCPeerConnection, records state changes with performance.now() timestamps, taps the received audio and
reads getStats. The selectors are in cases/web_pages.py.
"""
import struct
import threading
import time

from crossmode_fake import UsrpPeer, usrp_hub
from dvtest import browser as brlib
from dvtest.audio import signals
from dvtest.proto import usrp

import web_pages as pages

TONE_HZ = 700.0
TONE_TOL_HZ = 20.0
SILENCE_RMS = 0.004             # float samples (-1..1); the 700 Hz tone of the tests is about 0.17 rms


# ----------------------------------------------------------------- protocol side

def usrp_client(target, callsign, module):
    """A linked USRP dynamic client (REG, SET_INFO, module by DTMF) or a skip."""
    import pytest
    hub, why = usrp_hub(target)
    if hub is None:
        pytest.skip(why)
    peer = UsrpPeer(hub, callsign=callsign, module=module)
    if not peer.link(timeout=8.0):
        peer.close()
        pytest.skip("USRP client %s gets no REG:OK from %s:%d: the dynamic USRP hub is off (run with DVT_USRP_DYNAMIC=1, "
                    "docs/rig.md)" % (callsign, hub[0], hub[1]))
    peer.pump(0.2)
    peer.inbox.clear()
    return peer


def tone_frames(seconds, freq=TONE_HZ, amp=8000):
    return signals.frames(signals.tone(freq, int(seconds * 1000), amp=amp))


class Talk(threading.Thread):
    """peer.talk(frames) on a thread (the page is polled meanwhile). `started` is set when the first
    frame is about to go out; `t0` is its monotonic time."""

    def __init__(self, peer, frames):
        super().__init__(daemon=True)
        self.peer, self.frames = peer, frames
        self.started = threading.Event()
        self.t0 = None
        self.error = None

    def run(self):
        try:
            self.t0 = time.monotonic()
            self.started.set()
            self.peer.talk(self.frames, tail=0.3)
        except Exception as exc:                 # noqa: BLE001 - reported by the case
            self.error = exc


def voice_frames(peer):
    """[(monotonic time, 320 byte PCM)] of the voice packets the client received."""
    out = []
    for t, d in peer.inbox:
        if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0 and usrp.parse(d).keyup == 1:
            out.append((t, d[usrp.HEADER_SIZE:]))
    return out


def pcm_floats(frames):
    """Float samples (-1..1) of S16LE PCM frames."""
    data = b"".join(frames)
    n = len(data) // 2
    return [v / 32768.0 for v in struct.unpack("<%dh" % n, data[:2 * n])]


def set_info_callsigns(peer):
    """Callsigns the client was told in SET_INFO TLVs (who is talking, as the hub says)."""
    out = []
    for _t, d in peer.inbox:
        payload = usrp.text_payload(d) if len(d) >= usrp.HEADER_SIZE and usrp.type_le(d) == usrp.TYPE_TEXT else None
        if payload is None and len(d) >= usrp.HEADER_SIZE and usrp.type_le(d) == usrp.TYPE_TLV:
            payload = d[usrp.HEADER_SIZE:]
        info = usrp.parse_set_info(payload) if payload else None
        if info:
            out.append(info["callsign"])
    return out


def gaps_ms(times):
    """Gaps between consecutive arrival times in ms."""
    return [round((b - a) * 1000.0, 1) for a, b in zip(times, times[1:])]


# ------------------------------------------------------- what the router publishes

def find_values(obj, wanted, path=""):
    """Paths in a JSON value whose string value (case-insensitive, prefix of a '/suffix' allowed) equals wanted."""
    out = []
    wanted = wanted.upper()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += find_values(v, wanted, "%s.%s" % (path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_values(v, wanted, "%s[%d]" % (path, i))
    elif isinstance(obj, str) and obj.upper().split("/")[0].strip() == wanted:
        out.append(path)
    return out


def public_state(client):
    return client.get("/api/v1/public/state", expect=200).json()


def watch_public_state(client, callsign, seconds, interval=0.5):
    """Poll the public state for `seconds`; returns (paths, room_snippet) of the first state that names
    `callsign` (paths of the string values, the JSON of the object holding the first path), or ([], None)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        st = public_state(client)
        paths = find_values(st, callsign)
        if paths:
            return paths, st
        time.sleep(interval)
    return [], None


# ----------------------------------------------------------------- the page side

class Rtc:
    """The Listen / Talk panel of one BrowserPage (probe installed, dashboard workaround applied)."""

    def __init__(self, bp):
        self.bp = bp
        self.page = bp.page
        self.offers = []          # sess_id of every /rtc/offer answer (the page keeps it in memory only)
        self.sent = []            # (method, path) of every request the page issued (a keepalive fetch shows no response event)
        self.page.on("response", self._offer)
        self.page.on("request", lambda r: self.sent.append((r.method, r.url.split("?")[0].replace(bp.base, ""))))

    def _offer(self, resp):
        if resp.url.endswith("/api/v1/rtc/offer") and resp.request.method == "POST" and resp.status == 200:
            try:
                self.offers.append(resp.json().get("sess_id", ""))
            except Exception:                    # noqa: BLE001
                pass

    # -- navigation
    def open(self, timeout_ms=30000):
        self.bp.goto("/#/")
        pages.listen_panel(self.page).wait_for(state="visible", timeout=timeout_ms)

    def ev(self, js, arg=None):
        return self.page.evaluate(js, arg) if arg is not None else self.page.evaluate(js)

    def room_choice(self, room):
        pages.listen_room_select(self.page).select_option(room)

    def status(self):
        return pages.listen_status(self.page).inner_text().strip()

    def talk_status(self):
        return pages.talk_status(self.page).inner_text().strip()

    def state(self):
        return self.ev("window.__dvtTap ? window.__dvtTap.state() : null")

    def events(self):
        return self.ev("window.__dvtTap ? window.__dvtTap.events : []")

    def wait_state(self, want="connected", timeout_s=20.0):
        """Poll the connection state of the page's peer; returns seconds waited or raises AssertionError with
        the status line of the panel when the page gave up (an error text) or time ran out."""
        t0 = time.monotonic()
        last = None
        while time.monotonic() - t0 < timeout_s:
            last = self.state()
            if last and last.get("connection") == want:
                return time.monotonic() - t0
            text = self.status()
            if text not in (pages.LISTEN_IDLE_TEXT,) and not text.startswith(("Connecting", "Listening", "Switching")):
                raise AssertionError("the panel shows %r while waiting for %s (peer %s)" % (text, want, last))
            time.sleep(0.1)
        raise AssertionError("peer not %s after %.0f s (last %s, panel %r)" % (want, timeout_s, last, self.status()))

    def connect(self, room, timeout_s=20.0):
        """Choose `room`, click Listen / Connect, wait for 'connected' and for the status line to say
        'Listening on module <room>'. Returns {'click_to_connected_ms': page-side performance.now()
        difference between the click and the connected state, 'wall_to_listening_ms': ...}."""
        self.room_choice(room)
        wall = time.monotonic()
        pages.listen_button(self.page).click()
        self.wait_state("connected", timeout_s)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.status() == "Listening on module %s" % room:
                break
            time.sleep(0.1)
        wall_ms = (time.monotonic() - wall) * 1000.0
        return {"click_to_connected_ms": self.connect_time_ms(), "wall_to_listening_ms": round(wall_ms), "status": self.status()}

    def connect_time_ms(self):
        """Page-side ms from the last click event to the last 'connected' state change."""
        ev = self.events()
        connected = [e for e in ev if e.get("what") == "connectionstatechange" and e.get("state") == "connected"]
        if not connected:
            return None
        end = connected[-1]["t"]
        clicks = [e for e in ev if e.get("what") == "click" and e["t"] <= end]
        return round(end - clicks[-1]["t"]) if clicks else None

    def disconnect(self):
        pages.listen_button(self.page).click()

    # -- numbers
    def stats(self):
        return brlib.summarize_stats(self.ev("window.__dvtTap.stats()"))

    def inbound(self):
        """The inbound-rtp snapshot as inbound_verdict wants it, plus the raw entry."""
        s = self.stats()
        e = s["inbound"]
        if not e:
            return None
        return {"t": e.get("timestamp", time.time() * 1000.0), "packetsReceived": e.get("packetsReceived", 0),
                "bytesReceived": e.get("bytesReceived", 0), "packetsLost": e.get("packetsLost", 0),
                "jitter": e.get("jitter"), "codec": s["codec_in"], "raw": e}

    def tap_start(self):
        return self.ev("window.__dvtTap.start()")

    def analyse(self, seconds=1.0, lo=200.0, hi=3600.0, step=10.0):
        return self.ev("([s, a, b, c]) => window.__dvtTap.analyse(s, a, b, c)", [seconds, lo, hi, step])

    def sdp(self):
        return self.ev("window.__dvtTap.sdp()")

    def receivers(self):
        return self.ev("window.__dvtTap.receivers()")

    def alive_posts(self):
        """Statuses of the POST /api/v1/rtc/alive answers the page has seen."""
        return [r["status"] for r in self.bp.log.requests if r["path"] == "/api/v1/rtc/alive" and r["method"] == "POST"]

    def sent_posts(self, suffix):
        """How many POST /api/v1/rtc/<suffix> requests the page has sent."""
        return sum(1 for m, p in self.sent if m == "POST" and p == "/api/v1/rtc/" + suffix)

    def posts(self, suffix):
        return [r["status"] for r in self.bp.log.requests if r["path"] == "/api/v1/rtc/" + suffix and r["method"] == "POST"]

    def audio_element(self):
        return self.ev("""() => { const a = document.querySelector('audio.listen-audio');
            return a ? {paused: a.paused, muted: a.muted, ready: a.readyState, hasStream: !!a.srcObject,
                        error: a.error ? a.error.code : null} : null; }""")


def pump_while(peer, seconds, poll=None, poll_every=0.5):
    """Read the client's socket continuously for `seconds` (the kernel buffer holds only about 160 USRP
    packets, 3 s of voice), calling poll() every `poll_every` s; returns the list of poll() results."""
    end = time.monotonic() + seconds
    nxt = time.monotonic()
    out = []
    while time.monotonic() < end:
        peer.pump(0.05)
        if poll is not None and time.monotonic() >= nxt:
            out.append(poll())
            nxt = time.monotonic() + poll_every
    return out


def wait_js(page, expression, timeout_s=20.0, every=0.2):
    """Poll `expression` (a JavaScript function, e.g. "() => document.title") with page.evaluate until it is
    truthy; returns its value or raises AssertionError. Playwright's wait_for_function evaluates a string with
    eval(), which the page's CSP (default-src 'self') forbids."""
    end = time.monotonic() + timeout_s
    value = None
    while time.monotonic() < end:
        value = page.evaluate(expression)
        if value:
            return value
        time.sleep(every)
    raise AssertionError("not true after %.0f s: %s (last %r)" % (timeout_s, expression[:100], value))
