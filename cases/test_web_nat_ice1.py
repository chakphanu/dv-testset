# SPDX-License-Identifier: MIT
"""Web unit, stage 4b: ICE1, the client address changes during a Listen session. Needs
`scripts/run.sh --profile web,browser,nat` (docs/rig.md "NAT profile", docs/web-nat-cases.md "ICE1 address change"), the dynamic
USRP hub (DVT_USRP_DYNAMIC=1) and, as in the NAT row of the SKILL table, `turn.toml` (TURN on is the documented run; the case does
not need it and also runs without).

DV-Router ICE1 (image wip-97c669d, e439f62; ICE1b not landed): a signed-in browser is Listening on a module, then the NAT router
starts translating the browser to a different outside address (a new source address and port as the SUT sees them). The page
performs an ICE restart on the SAME circuit:

  1  the circuit the page holds is unchanged: no new POST /rtc/offer session, no new RTCPeerConnection, no new login, no new
     circuit in the operator's client list
  2  voice frames reach the listener again, within RESUME_BUDGET_S of the change (Chromium 8 s, Firefox and Playwright WebKit
     12 s until ICE1b lands; dvtest/ice1.py), measured on the inbound packet counter of the page while a protocol talker keeps
     the module on air before, during and after the change
  3  the old candidate pair is gone on the SUT side. What can be seen from outside: the NAT router's conntrack shows the SUT only
     the new address after the change (and only the old one before), and the router counts every datagram the SUT's media port
     still sends to the OLD address once the audio is back (it must be none). The operator client list is read and recorded; its
     field names are UNVERIFIED (docs/web-nat-cases.md), so it is not asserted
  4  the page shows no error banner and ends in the connected state

The address change is the router control word `readdress` (dvtest/nat.py): only UDP moves to the other outside address, TCP keeps
its address (the web service ties a circuit to the client address of the HTTP requests, so moving TCP too would test that rule
instead of the ICE restart). Whatever restores the original mapping runs in a try/finally of the case, in the fixture of the case
and in a finalizer of the module and the session.

Signed in as the browser user (br_user). Three NAT types x three engines. The address-restricted cone NAT is not built in the
router (docs/web-nat-cases.md "Gaps"), so it is not run. Differences from the spec are recorded with web.finding().

  a  test_ice1_readdress_moves_the_mapping_and_back   no browser: the NAT classification before, moved and back (the type stays,
                                                      the address changes)
  b  test_ice1_address_change_restarts_ice_on_the_same_circuit   the case above
"""
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

import web_pages as pages  # noqa: E402
from browser_fixtures import _rtc_end, _rtc_page  # noqa: E402
from dvtest import browser as brlib  # noqa: E402
from dvtest import ice1, metrics, nat, web  # noqa: E402
from nat_support import NAT_TYPES, IcePacer, NatRtc, rig_client, turn_on  # noqa: E402
from webrtc_support import Talk, tone_frames, usrp_client  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc, pytest.mark.nat]

ROOM = "I"
PROTO_CALL = "N0TEST"
EXPECTED_CLASS = {"full_cone": (nat.EI, nat.EI), "port_restricted": (nat.EI, nat.APD), "symmetric": (nat.APD, nat.APD)}

OVER_S = 55.0               # the one over of the talker: connect + 4 s steady + up to 20 s of observation + 4 s hold, with room
STEADY_S = 4.0              # audio before the change
FIRST_AUDIO_S = 8.0         # the audio must be flowing this soon after the talker keyed
OBSERVE_EXTRA_S = 8.0       # how long after the budget the case still looks for the audio (a late resume is recorded, then fails)
SEAMLESS_AFTER_S = 2.0      # a counter that never stalled counts as "never interrupted" only after this long
STRAY_START_S = 1.0         # the old address is checked from this long after the audio is back ...
STRAY_WINDOW_S = 3.0        # ... for this long
FIRSTS_MARGIN_S = 1.0       # the router's sniffer horizon (nat.Firsts.HORIZON_S, 20 s) plus this after the change, then `firsts` is read
SAMPLE_EVERY_S = 0.1
UI_EVERY_S = 0.5
AUDIO_PACKETS_PER_S = 50

_SEEN = set()
_PACER = IcePacer()


def note(engine, nat_type, key, value):
    """A measurement of one engine behind one NAT type: appended to $DVT_OUT/web/ice1-results.jsonl and shown once as a FINDING
    line (web-findings.txt). Addresses are rig addresses and stay in out/."""
    row = {"engine": brlib.label(engine) if engine else None, "nat": nat_type, "key": key, "value": value}
    path = Path(os.environ.get("DVT_OUT", "out")) / "web" / "ice1-results.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass
    k = (engine, nat_type, key)
    if k not in _SEEN:
        _SEEN.add(k)
        web.finding("ICE1 %s / %s: %s: %s" % (brlib.label(engine) if engine else "-", nat_type, key,
                                              value if isinstance(value, str) else json.dumps(value, sort_keys=True)))


# ---------------------------------------------------------------------------------------- fixtures

def restore(client):
    """Whatever is left of a move goes back: readdress off, then reset (port restricted, nothing blocked). Returns the problems
    (empty = the router is back at its default and not moved)."""
    problems = []
    for step in (lambda: client.readdress(False), lambda: client.ctl("reset")):
        try:
            step()
        except Exception as exc:                        # noqa: BLE001 - every step is tried
            problems.append("%s: %s" % (type(exc).__name__, str(exc)[:100]))
    try:
        st = client.status()
        if st.get("readdressed") or st.get("mode") != "port_restricted":
            problems.append("the router is still moved or not at its default: mode %s readdressed %s" % (st.get("mode"), st.get("readdressed")))
    except Exception as exc:                            # noqa: BLE001
        problems.append("status: %s: %s" % (type(exc).__name__, str(exc)[:100]))
    return problems


@pytest.fixture(scope="session")
def ice1_guard():
    """Session finalizer: if anything left the router moved, it is put back when the session ends."""
    holder = {"client": None}
    yield holder
    if holder["client"] is not None:
        restore(holder["client"])


@pytest.fixture(scope="module")
def ice1_rig(br_user, ice1_guard):
    """The NAT router client. br_user is made first (its registration needs the operator session, which does not last for
    an hour); module-scope finalizer: restore."""
    client = rig_client()
    ice1_guard["client"] = client
    client.ctl("reset")
    yield client
    problems = restore(client)
    assert not problems, problems


@pytest.fixture(params=NAT_TYPES)
def nat_type(request):
    return request.param


@pytest.fixture
def ice1_ctl(ice1_rig, nat_type):
    """The router in the NAT type of the case, not moved; restored afterwards (checked)."""
    ice1_rig.ctl("reset")
    ice1_rig.mode(nat_type)
    yield ice1_rig
    problems = restore(ice1_rig)
    assert not problems, problems


@pytest.fixture
def ice1_page(ice1_ctl, engine, engine_browsers, web_base_url, request, br_user):
    """The page of one (engine, NAT type), signed in as the browser user (br_user); logged out afterwards."""
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, br_user, talk=False)
    bp.nat_key = br_user.callsign
    yield bp
    _rtc_end(bp, request)


# ---------------------------------------------------------------------------------------- a

def test_ice1_readdress_moves_the_mapping_and_back(nat_type, ice1_ctl):
    """The rig part, no browser: `readdress` changes the address the reflectors see the inside socket at, the NAT type stays what
    it was (mapping and filtering), and `readdress off` brings the original address back."""
    want_mapping, want_filtering = EXPECTED_CLASS[nat_type]
    first = ice1_ctl.classify()
    ip_before = first["mapped"]["a1"][0]
    moved = None
    try:
        ans = ice1_ctl.readdress(True)
        moved = ice1_ctl.classify()
        ip_moved = {k: v[0] for k, v in moved["mapped"].items()}
        note(None, nat_type, "readdress: reflectors saw", {"before": first["mapped"], "moved": moved["mapped"], "answer": ans})
    finally:
        ice1_ctl.readdress(False)
    back = ice1_ctl.classify()
    note(None, nat_type, "readdress: back", back["mapped"])

    assert ans["from"] == ip_before and ans["to"] != ip_before, (ans, first["mapped"])
    assert set(ip_moved.values()) == {ans["to"]}, "the reflectors did not all see the new address: %s" % moved["mapped"]
    for name, res in (("before", first), ("moved", moved), ("back", back)):
        got = res["classification"]
        assert (got["mapping"], got["filtering"], got["type"]) == (want_mapping, want_filtering, nat_type), (name, res)
    assert {v[0] for v in back["mapped"].values()} == {ip_before}, "the original address did not come back: %s" % back["mapped"]
    assert ice1_ctl.status()["readdressed"] is False
    # the packet sniffer behind the `firsts` evidence of the ICE1 case needs CAP_NET_RAW on nat-router (rig/compose.yml)
    assert ans.get("sniffer") == "ok", "the router's packet sniffer did not start: %s (CAP_NET_RAW on nat-router?)" % ans.get("sniffer")
    snap = ice1_ctl.firsts()
    assert snap and snap["error"] is None and snap["old"] == ip_before and snap["new"] == ans["to"], snap


# ---------------------------------------------------------------------------------------- b

def pace_ice(bp, need):
    if turn_on():
        _PACER.wait(bp.nat_key, need)


def used_ice(bp, rtc):
    n = sum(1 for i in rtc.ice if i["status"] in (200, 429))
    if turn_on() and n:
        _PACER.used(bp.nat_key, n)


def sample(rtc, total):
    """The running total of the packets the page received (the inbound-rtp counter of the probe)."""
    inb = rtc.inbound()
    return total.update(inb["raw"].get("id") if inb else None, inb["packetsReceived"] if inb else 0)


def page_view(rtc):
    return {"state": rtc.state(), "status": rtc.status(), "panel": rtc.panel_state(), "sdp": rtc.sdp(),
            "pcs": rtc.ev("window.__dvtTap.pcs.length"), "pair": rtc.pair(),
            "candidates": ice1.local_candidates(rtc.ev("window.__dvtTap.stats()"))}


def cookie_value(bp):
    c = bp.session_cookie()
    return c["value"] if c else None                    # compared, never printed


QUIET_PATHS = ("/api/v1/rtc/alive", "/api/v1/public/events", "/api/v1/operator/stream")


def requests_since(rtc, start):
    """{'METHOD path': n} of the requests the page sent since index `start` of its request audit (liveness posts and the event
    stream left out)."""
    out = {}
    for method, path in rtc.sent[start:]:
        if path in QUIET_PATHS:
            continue
        out["%s %s" % (method, path)] = out.get("%s %s" % (method, path), 0) + 1
    return out


def events_since(rtc, t0):
    """The page-clock digest of the ICE related events since t0: [(ms, what, state or type)]."""
    keep = ("connectionstatechange", "iceconnectionstatechange", "signalingstatechange", "icegatheringstatechange",
            "setRemoteDescription", "setConfiguration", "new", "channel-close", "channel-open")
    return [(round(e["t"] - t0), e["what"], e.get("state") or e.get("type") or e.get("label") or "")
            for e in rtc.events() if e["t"] >= t0 and e.get("what") in keep][:24]


def operator_rows(operator):
    """The WebRTC rows of the operator's client list, or {'error': ...}. Field names are UNVERIFIED."""
    if operator is None:
        return {"error": "no operator session"}
    try:
        items = operator.get("/api/v1/operator/clients?protocol=webrtc&limit=50", expect=200).json().get("items") or []
        return {"rows": ice1.webrtc_rows(items)}
    except Exception as exc:                            # noqa: BLE001 - recorded, not asserted
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:80])}


def operator_digest(snapshot, circuit, callsign):
    """What of the list is recorded: row count, the matched row's field names and its address-like values (rig addresses)."""
    if "error" in snapshot:
        return snapshot
    row, how = ice1.match_row(snapshot["rows"], circuit, callsign)
    return {"rows": len(snapshot["rows"]), "matched_by": how, "keys": sorted(row) if row else None,
            "address_like": ice1.address_like(row) if row else None}


def hub_families(rig_control):
    """Names (and label keys, never values) of the hub metric families that name webrtc or sessions: what could carry a
    per-circuit remote address. UNVERIFIED which one does."""
    try:
        samples = metrics.parse(rig_control.metrics("xlxd-sut"))
    except Exception as exc:                            # noqa: BLE001
        return {"error": type(exc).__name__}
    out = {}
    for s in samples:
        if "webrtc" in s.name or s.name == "dv_edge_sessions":
            out.setdefault(s.name, set()).update(s.labels)
    return {k: sorted(v) for k, v in sorted(out.items())}


def leave(rtc):
    """Best effort, before the router is put back: close the Listen session so that the SUT does not see a second address
    change of a page that is about to end. Only a button that says Disconnect is clicked (an idle one would connect)."""
    try:
        if pages.listen_button(rtc.page).inner_text().strip().startswith("Disconnect"):
            rtc.disconnect()
            rtc.wait_idle(5.0)
    except Exception:                                   # noqa: BLE001
        pass


def judge_final(bp, rtc, view):
    """(4) no error banner, connected. Returns (problems, recorded) - the alert selector of the card is UNVERIFIED, so its
    text only counts when it reads like an error."""
    bad = []
    if view["panel"] != "connected":
        bad.append("the Listen card ends in data-state %r, not connected" % view["panel"])
    if (view["state"] or {}).get("connection") != "connected":
        bad.append("the peer ends in %s, not connected" % (view["state"],))
    if view["status"] != "%s %s" % (pages.LISTEN_LISTENING_PREFIX, ROOM):
        bad.append("the status line ends as %r" % view["status"])
    words = [w for w in pages.LISTEN_ERROR_WORDS if w in view["status"].lower()]
    if words:
        bad.append("the status line reads like an error (%s): %r" % (words, view["status"]))
    n_dash = pages.dashboard_alerts(bp.page).count()
    if n_dash:
        bad.append("the dashboard shows %d error banner(s)" % n_dash)
    texts = [t.strip() for t in pages.listen_alerts(rtc.page).all_inner_texts()]
    bad_texts = [t for t in texts if any(w in t.lower() for w in pages.LISTEN_ERROR_WORDS)]
    if bad_texts:
        bad.append("the Listen card shows an alert that reads like an error: %s" % [t[:80] for t in bad_texts])
    if bp.log.errors:
        bad.append("page errors: %s" % [e[:120] for e in bp.log.errors[:3]])
    return bad, {"dashboard_alerts": n_dash, "listen_alerts": texts[:3]}


def run_change(engine, nat_type, bp, rtc, ctl, target, operator, circuit, callsign):
    """The measured part: audio, change, observation. Returns the measurements; the restore is the caller's finally."""
    m = {"samples": [], "ui": []}
    total = ice1.InboundTotal()
    talker = usrp_client(target, PROTO_CALL, ROOM)
    talk = Talk(talker, tone_frames(OVER_S))
    try:
        talk.start()
        assert talk.started.wait(5), "the talker did not start"
        t_key = time.monotonic()
        base = sample(rtc, total)
        while sample(rtc, total) - base < AUDIO_PACKETS_PER_S:
            assert time.monotonic() - t_key < FIRST_AUDIO_S, "no audio at the listener %.0f s after the talker keyed (%d packets)" % (
                FIRST_AUDIO_S, total.total - base)
            time.sleep(SAMPLE_EVERY_S)
        end = time.monotonic() + STEADY_S
        while time.monotonic() < end:
            m["samples"].append((time.monotonic(), sample(rtc, total)))
            time.sleep(SAMPLE_EVERY_S)

        # ---- the state before
        m["flows_before"] = ctl.media_mappings()
        seen_before = sorted({r["seen_ip"] for r in m["flows_before"]})
        assert len(seen_before) == 1, "the router must hold the browser's UDP flow to the SUT at one address before the change: %s" % m["flows_before"]
        m["view_before"] = page_view(rtc)
        m["offers_before"] = list(rtc.offers)
        m["offer_posts_before"] = (rtc.sent_posts("offer"), len(bp.log.paths("/api/v1/rtc/offer", "POST")))
        m["req_start"] = len(rtc.sent)
        m["cookie_before"] = cookie_value(bp)
        m["operator_before"] = operator_rows(operator)
        m["router_before"] = ctl.status()
        t_page0 = rtc.clock()

        # ---- the change
        t_change = time.monotonic()
        ans = ctl.readdress(True)
        m["t_return_s"] = round(time.monotonic() - t_change, 3)
        m["from_to"] = (ans["from"], ans["to"])
        assert ans["from"] == seen_before[0], "the router moves the browser away from %s but the SUT saw it at %s" % (ans["from"], seen_before[0])

        # ---- the observation
        budget = ice1.resume_budget_s(engine)
        deadline = t_change + budget + OBSERVE_EXTRA_S
        last_ui, texts = 0.0, []
        resume_abs = c1 = t_c1 = c2 = t_c2 = None
        lost = None
        while True:
            now = time.monotonic()
            m["samples"].append((now, sample(rtc, total)))
            if now - last_ui >= UI_EVERY_S:
                last_ui = now
                status, panel, st = rtc.status(), rtc.panel_state(), rtc.state() or {}
                if not texts or texts[-1][1:] != (status, panel):
                    texts.append((round(now - t_change, 2), status, panel))
                if rtc.lost_text() or st.get("connection") in ("failed", "closed"):
                    lost = {"after_s": round(now - t_change, 2), "status": status, "panel": panel, "peer": st.get("connection")}
                    break
            an = ice1.resume_analysis(m["samples"], t_change)
            if resume_abs is None and an["resumed"] and (an["stalled"] or now - t_change >= SEAMLESS_AFTER_S):
                resume_abs = t_change + (an["resume_s"] or 0.0)
            if resume_abs is not None and c1 is None and now >= resume_abs + STRAY_START_S:
                c1, t_c1 = ctl.status()["dropped"]["unmapped_media"], now
            if c1 is not None and now >= t_c1 + STRAY_WINDOW_S:
                c2, t_c2 = ctl.status()["dropped"]["unmapped_media"], now
                m["late_growth"] = total.total - next((n for t, n in reversed(m["samples"]) if t <= now - STRAY_WINDOW_S), total.total)
                break
            if resume_abs is None and now > deadline:
                break
            time.sleep(SAMPLE_EVERY_S)
        m["still_talking"] = talk.is_alive()
        m["analysis"], m["lost"], m["texts"] = ice1.resume_analysis(m["samples"], t_change), lost, texts
        m["unmapped"] = (c1, c2, round(t_c2 - t_c1, 2) if t_c1 and t_c2 else None)

        # ---- the router's sniffer evidence (ICE1c): the stale sends are counted per second for the whole horizon, so wait for it
        # (the talker is still on air: OVER_S is longer than the longest case plus this)
        wait = t_change + nat.Firsts.HORIZON_S + FIRSTS_MARGIN_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            m["firsts"] = ctl.firsts()
        except Exception as exc:                        # noqa: BLE001 - evidence, not a verdict
            m["firsts"] = None
            m["firsts_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:100])

        # ---- the state after
        m["flows_after"] = ctl.media_mappings()
        m["view_after"] = page_view(rtc)
        m["offers_after"] = list(rtc.offers)
        m["offer_posts_after"] = (rtc.sent_posts("offer"), len(bp.log.paths("/api/v1/rtc/offer", "POST")))
        m["requests"] = requests_since(rtc, m["req_start"])
        m["cookie_after"] = cookie_value(bp)
        m["operator_after"] = operator_rows(operator)
        m["events"] = events_since(rtc, t_page0)
        m["router_after"] = ctl.status()
        return m
    finally:
        talker.close()
        talk.join(timeout=5)


@pytest.mark.slow
def test_ice1_address_change_restarts_ice_on_the_same_circuit(engine, nat_type, ice1_page, ice1_ctl, target, web_operator, br_user,
                                                              request):
    """ICE1 (module docstring): the router moves the browser's UDP to the other outside address while a talker keeps the module
    on air; the page must restart ICE on the same circuit, the audio must come back within the budget of its engine, the SUT must
    stop using the old address, and the page must end connected with no error banner."""
    bp = ice1_page
    rtc = NatRtc(bp)
    rtc.open()
    pace_ice(bp, 2)                                     # the restart may fetch a credential again (TURN on): one login, two per minute
    t = rtc.connect(ROOM, timeout_s=25.0)
    note(engine, nat_type, "first connect, click to connected (ms)", t["click_to_connected_ms"])
    assert len(rtc.offers) == 1, "the first connect sent %d offers" % len(rtc.offers)
    circuit = rtc.offers[0]
    try:
        rig_control = request.getfixturevalue("rig_control")
    except pytest.skip.Exception:
        rig_control = None
    if rig_control is not None and ("hub", "families") not in _SEEN:
        note(None, None, "hub metric families that name webrtc or sessions (names and label keys)", hub_families(rig_control))
        _SEEN.add(("hub", "families"))
    m = None
    try:
        m = run_change(engine, nat_type, bp, rtc, ice1_ctl, target, web_operator, circuit, br_user.callsign)
    finally:
        leave(rtc)
        try:
            ice1_ctl.readdress(False)                   # the original mapping of the NAT type, whatever happened above
        finally:
            used_ice(bp, rtc)
    st = ice1_ctl.status()
    bad = []
    if st.get("readdressed") or st.get("mode") != nat_type:
        bad.append("the router was not restored: mode %s readdressed %s" % (st.get("mode"), st.get("readdressed")))
    old_ip, new_ip = m["from_to"]
    an, budget = m["analysis"], ice1.resume_budget_s(engine)

    # ---- what was seen, recorded first
    note(engine, nat_type, "address change", {"from": old_ip, "to": new_ip, "router_answered_after_s": m["t_return_s"], "tcp": "unchanged"})
    # router-side evidence for DV-Router (ICE1c): when the browser's first datagrams reach the SUT from the new mapping and when the edge
    # follows; seconds after the router began the change; None = not seen in 20 s. Recorded, never asserted.
    ev = ice1.firsts_evidence(m["firsts"])
    for key in ("first_stun_new_s", "first_udp_new_s", "first_sut_to_new_s", "stale_to_old_per_s"):
        note(engine, nat_type, key, ev[key])
    note(engine, nat_type, "firsts: detail (kinds of the firsts, STUN response / RTP to the new mapping, stale totals)", ev)
    if ev["error"] or m.get("firsts_error"):
        web.finding("ICE1 %s / %s: no first-datagram evidence from the router: %s" % (
            brlib.label(engine), nat_type, ev["error"] or m["firsts_error"]))
    note(engine, nat_type, "audio after the change", {"analysis": an, "budget_s": budget, "late_growth_3s": m.get("late_growth"),
                                                      "talker_still_on_air": m["still_talking"]})
    note(engine, nat_type, "page status and Listen card states (s after the change)", m["texts"][:8])
    note(engine, nat_type, "ICE / signalling events of the page (ms after the change)", m["events"])
    note(engine, nat_type, "requests the page sent after the change", m["requests"])
    note(engine, nat_type, "offer posts (sent, answered) before / after", [m["offer_posts_before"], m["offer_posts_after"]])
    note(engine, nat_type, "router view: the SUT sees the browser at (before / after)", [m["flows_before"], m["flows_after"]])
    note(engine, nat_type, "datagrams from the SUT's media port to the old address (at settled start, at end, window s)", list(m["unmapped"]))
    note(engine, nat_type, "router: unmapped media in total, last move", (m["router_after"].get("dropped") or {}).get("unmapped_media"))
    note(engine, nat_type, "browser's local candidates (before / after)", [m["view_before"]["candidates"], m["view_after"]["candidates"]])
    note(engine, nat_type, "selected pair (before / after)", [m["view_before"]["pair"], m["view_after"]["pair"]])
    note(engine, nat_type, "GET /rtc/ice statuses of the session", rtc.ice_statuses())
    for label, key in (("before", "operator_before"), ("after", "operator_after")):
        note(engine, nat_type, "operator client list %s (field names UNVERIFIED)" % label, operator_digest(m[key], circuit, br_user.callsign))

    # ---- (1) the same circuit
    if m["lost"]:
        bad.append("the page gave up the connection %s s after the change: %s" % (m["lost"]["after_s"], m["lost"]))
    if set(m["offers_after"]) != {circuit}:
        bad.append("a new circuit: the page now holds %d session(s) from /rtc/offer (one before)" % len(set(m["offers_after"])))
    if m["view_after"]["pcs"] != m["view_before"]["pcs"]:
        bad.append("a new RTCPeerConnection: %s before, %s after" % (m["view_before"]["pcs"], m["view_after"]["pcs"]))
    if m["offer_posts_after"] != m["offer_posts_before"]:
        # the spec says no new POST /rtc/offer SESSION: a re-offer that the edge answers with the same circuit is not one
        web.finding("ICE1 %s / %s: POST /rtc/offer was sent again (sent, answered: %s -> %s); the circuit is %s" % (
            brlib.label(engine), nat_type, m["offer_posts_before"], m["offer_posts_after"],
            "the same" if set(m["offers_after"]) == {circuit} else "NOT the same"))
    if m["cookie_after"] != m["cookie_before"]:
        bad.append("a new HTTP session: the session cookie changed")
    if any(k.startswith("POST /api/v1/auth/session") for k in m["requests"]):
        bad.append("a new login: %s" % {k: v for k, v in m["requests"].items() if "auth/session" in k})
    if "rows" in m["operator_before"] and "rows" in m["operator_after"]:
        fresh = ice1.new_circuits(m["operator_before"]["rows"], m["operator_after"]["rows"])
        if fresh:
            bad.append("the operator client list shows %d new WebRTC circuit(s) after the change" % len(fresh))
    else:
        web.finding("ICE1 %s / %s: UNVERIFIED: the operator client list could not be compared (%s / %s)" % (
            brlib.label(engine), nat_type, m["operator_before"].get("error"), m["operator_after"].get("error")))
    restart = ice1.restart_seen(m["view_before"]["sdp"], m["view_after"]["sdp"])
    note(engine, nat_type, "ICE restart seen in the SDP (new ice-ufrag)", restart)
    if not restart["any"]:
        bad.append("no ICE restart: the ice-ufrag of the local and the remote description are the same after the change")

    # ---- (2) the audio
    if not an["resumed"]:
        bad.append("the audio did not come back: stalled %.1f s after the change, still silent after %.0f s (budget %.0f s)" % (
            an["stall_start_s"] or 0.0, budget + OBSERVE_EXTRA_S, budget))
    elif an["resume_s"] > budget:
        bad.append("the audio came back %.1f s after the change, budget %.0f s (%s)" % (an["resume_s"], budget, brlib.label(engine)))
    if not m["still_talking"]:
        bad.append("the talker's over ended before the check was done: rig, not the product (OVER_S %.0f s)" % OVER_S)
    elif an["resumed"] and (m.get("late_growth") or 0) < AUDIO_PACKETS_PER_S * STRAY_WINDOW_S * 0.7:
        bad.append("the audio did not stay: %s packets in the last %.0f s (about %d expected)" % (
            m.get("late_growth"), STRAY_WINDOW_S, AUDIO_PACKETS_PER_S * STRAY_WINDOW_S))

    # ---- (3) the old pair is gone on the SUT side
    bad += ice1.mapping_problems(m["flows_before"], m["flows_after"], old_ip, new_ip)
    c1, c2, window = m["unmapped"]
    if c1 is None or c2 is None:
        bad.append("the old address could not be checked: the audio never came back, so there is no settled window")
    else:
        bad += ice1.stray_problems(c1, c2, window)
    before_row = operator_digest(m["operator_before"], circuit, br_user.callsign)
    after_row = operator_digest(m["operator_after"], circuit, br_user.callsign)
    addr_after = (after_row.get("address_like") or {}) if isinstance(after_row, dict) else {}
    if not addr_after:
        web.finding("ICE1 %s / %s: UNVERIFIED: the operator client list row of the circuit has no address-like field (keys %s); the "
                    "SUT-side remote address of the circuit is seen only through the router (conntrack, unmapped-media counter)" % (
                        brlib.label(engine), nat_type, after_row.get("keys")))
    elif any(old_ip in v for v in addr_after.values()) and not any(new_ip in v for v in addr_after.values()):
        web.finding("ICE1 %s / %s: the operator client list still shows the OLD address %s for the circuit after the restart (%s); the field "
                    "may be the HTTP client address, which this case does not move (UNVERIFIED)" % (brlib.label(engine), nat_type, old_ip, sorted(addr_after)))
    elif any(new_ip in v for v in addr_after.values()):
        note(engine, nat_type, "operator client list shows the new address", {"before": before_row.get("address_like"), "after": addr_after})

    # ---- (4) the page
    final_bad, recorded = judge_final(bp, rtc, m["view_after"])
    note(engine, nat_type, "banners (dashboard alerts, Listen card alerts: selector UNVERIFIED)", recorded)
    bad += final_bad

    assert not bad, bad
