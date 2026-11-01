# SPDX-License-Identifier: MIT
"""Helpers of the NAT cases (cases/test_web_nat.py, profile nat): the NAT rig client from the environment, a
Listen panel driver that also records the ICE and offer traffic, and the page-clock measurements (answer to
connected, first audio, arrival timing, loss of the connection). Not a test module.
"""
import json
import os
import time

import pytest

from dvtest import browser as brlib
from dvtest import nat
from webrtc_support import Rtc


NAT_TYPES = ("full_cone", "port_restricted", "symmetric")

# adopted pass criteria (DV-Router docs/project/testing.md "Browser/NAT rig criteria")
CONNECT_BUDGET_S = 10.0
JITTER_P99_MS = 20.0
JITTER_MAX_MS = 60.0

LOST_TEXT_PARTS = ("connect again", "connection lost", "closed")


def addr(text):
    host, _, port = text.rpartition(":")
    return host, int(port)


def rig_client():
    """The NatClient of the run, or a skip with the reason."""
    if os.environ.get("DVT_NAT") != "on":
        pytest.skip("nat profile not up: run scripts/run.sh --profile web,browser,nat (the tester gets DVT_NAT=on)")
    try:
        refl = os.environ["DVT_NAT_REFLECTORS"].split(",")
        client = nat.NatClient(addr(os.environ["DVT_NAT_CTL"]), addr(os.environ["DVT_NAT_PROBE"]), refl[0], refl[1])
    except (KeyError, ValueError, IndexError):
        pytest.skip("DVT_NAT=on but DVT_NAT_CTL / DVT_NAT_PROBE / DVT_NAT_REFLECTORS are not set")
    deadline = time.monotonic() + 90
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            st = client.status()
            if st.get("ok"):
                return client
        except (OSError, nat.NatError, ValueError) as exc:
            last = str(exc)
        time.sleep(1.0)
    pytest.skip("the NAT router does not answer on %s within 90 s (%s)" % (os.environ["DVT_NAT_CTL"], last))


def turn_on():
    return os.environ.get("DVT_TURN") == "on"


def turn_secret():
    """The per-run shared secret (out/<run>/web/turn.secret, mode 0600). Never printed or logged."""
    path = os.environ.get("DVT_TURN_SECRET_FILE", "")
    try:
        with open(path, "r", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError as exc:
        pytest.skip("TURN is on but the secret file %s cannot be read: %s" % (path, exc.strerror))


def turn_ttl():
    return int(os.environ.get("DVT_TURN_TTL", "120"))


class IcePacer:
    """The web service issues one TURN credential per minute per login with a burst of two (web.md). The
    cases share logins across test functions; this sleeps until a login has the tokens the next case needs."""

    def __init__(self):
        self.tokens, self.stamp = {}, {}

    def wait(self, key, need=1):
        now = time.monotonic()
        have = min(2.0, self.tokens.get(key, 2.0) + (now - self.stamp.get(key, now)) / 60.0)
        if have < need:
            time.sleep((need - have) * 60.0 + 1.0)
            have = float(need)
        self.tokens[key], self.stamp[key] = have, time.monotonic()

    def used(self, key, n=1):
        now = time.monotonic()
        have = min(2.0, self.tokens.get(key, 2.0) + (now - self.stamp.get(key, now)) / 60.0)
        self.tokens[key], self.stamp[key] = max(0.0, have - n), now


class NatRtc(Rtc):
    """The Listen panel driver plus the traffic the NAT cases judge: every /rtc/ice answer (status and body,
    held in the tester's memory only), every /rtc/offer request (and whether its SDP had candidates)."""

    def __init__(self, bp):
        super().__init__(bp)
        self.ice = []             # {"status", "body"} per GET /api/v1/rtc/ice
        self.offer_reqs = []      # {"candidates": n} per POST /api/v1/rtc/offer
        bp.page.on("response", self._ice)

    def _ice(self, resp):
        if resp.url.split("?")[0].endswith("/api/v1/rtc/ice") and resp.request.method == "GET":
            body = None
            if resp.status == 200:
                try:
                    body = resp.json()
                except Exception:                           # noqa: BLE001
                    body = None
            self.ice.append({"status": resp.status, "body": body, "at": time.time()})
        elif resp.url.split("?")[0].endswith("/api/v1/rtc/offer") and resp.request.method == "POST":
            try:
                data = resp.request.post_data or ""
            except Exception:                               # noqa: BLE001
                data = ""
            self.offer_reqs.append({"candidates": data.count("a=candidate"), "bytes": len(data)})

    def ice_statuses(self):
        return [i["status"] for i in self.ice]

    def entries(self):
        """The ice_servers entries of every 200 answer."""
        out = []
        for i in self.ice:
            if i["status"] == 200 and isinstance(i["body"], dict):
                out.extend(i["body"].get("ice_servers") or [])
        return out

    def pair(self):
        return brlib.selected_pair(self.ev("window.__dvtTap.stats()"))

    def conn_events(self, since_ms=0.0):
        """[(t, state)] of the connectionstatechange events of the page since a page-clock time."""
        return [(e["t"], e["state"]) for e in self.events() if e.get("what") == "connectionstatechange" and e["t"] >= since_ms]

    def state_events(self, since_ms=0.0):
        """[(t, 'conn:<state>' or 'ice:<state>')] of the connection and ICE state changes since a page-clock time. The
        page closes its peer at the first ICE 'disconnected' (web.md), which fires no connectionstatechange, so the ICE
        events are the ones that time a loss."""
        out = []
        for e in self.events():
            if e["t"] >= since_ms and e.get("what") in ("connectionstatechange", "iceconnectionstatechange"):
                out.append((e["t"], ("conn:" if e["what"] == "connectionstatechange" else "ice:") + e["state"]))
        return out

    def event_time(self, what, last=True, **match):
        ev = [e["t"] for e in self.events() if e.get("what") == what and all(e.get(k) == v for k, v in match.items())]
        if not ev:
            return None
        return ev[-1] if last else ev[0]

    def clock(self):
        return self.ev("performance.now()")

    def start_arrivals(self, ms):
        """Start the getStats sampler in the page without waiting for it (collect_arrivals gets the result)."""
        self.ev("(ms) => { window.__dvtArr = window.__dvtTap.arrivals(ms); return true; }", ms)

    def collect_arrivals(self):
        return self.ev("() => window.__dvtArr")

    def lost_text(self):
        """The page gave up the connection: the Listen card's data-state is 'failed' (WEB2: a failed peer reports
        failed) or the status line has a loss text ('Connection lost. Connect again manually.', ...)."""
        if self.panel_state() == "failed":
            return True
        text = self.status().lower()
        return any(p in text for p in LOST_TEXT_PARTS) and not text.startswith(("listening", "connecting"))

    def secrets_in_page(self, needles):
        """Which of `needles` (strings) appear in the page text, its HTML, web storage, the cookie jar, the console
        or the page errors. Returns the indexes of the needles found (never the needles)."""
        hay = []
        hay.append(self.page.content())
        hay.append(self.ev("document.body ? document.body.innerText : ''"))
        hay.append(json.dumps(self.ev("""() => [Object.entries(localStorage), Object.entries(sessionStorage), document.cookie]""")))
        hay.append(json.dumps([c["value"] for c in self.bp.context.cookies()]))
        hay.append(json.dumps(self.bp.log.console))
        hay.append(json.dumps(self.bp.log.errors))
        text = "\n".join(hay)
        return [i for i, n in enumerate(needles) if n and n in text]


def window_summary(events, t0):
    """Connection-state events at or after the page-clock time t0 as [(ms after t0, state)]."""
    return [(round(t - t0), st) for t, st in events if t >= t0 - 1]
