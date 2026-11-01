# SPDX-License-Identifier: MIT
"""Web unit, stage 3b: WebRTC Listen and Talk of DV-Router's web UI in real browsers (Playwright
Chromium, Firefox and Playwright WebKit, which is the engine of Safari and not Safari). Needs
`scripts/run.sh --profile web,browser` (docs/rig.md "Browser profile", docs/web-browser-cases.md, section
"Stage 3b"). The protocol side is a USRP dynamic client of the rig (PCM straight into the room, no
transcoder). Differences from the documents are recorded with web.finding(), not asserted.

Calibrated against dv-router:wip-13e7e70: the dashboard renders as served (DASH1), the selectors are the WEB2
hooks of cases/web_pages.py (listen-room, listen-toggle, talk-ptt; the Listen card's data-state), and TALK1 keeps
a held over alive with 'renewed' acks of the lease. The Listen panel is on the dashboard route `#/`; the page
chooses the module, connects with "Listen / Connect" (offer through the proxy, ICE-lite edge at <net>.11:50000,
media to the browser container), and sends /rtc/alive every 10 s.

  a  test_listen_is_not_offered_to_anonymous      guest_listen is off: no Listen panel for a signed-out visitor
  b  test_listen_receives_the_tone                connect, silence, a 700 Hz USRP talker: getStats growth, codec, 20 ms,
                                                  Goertzel in the page, end of the over, Disconnect closes the circuit
  c  test_listen_lease_stays_alive_past_30_s      /rtc/alive every 10 s, still receiving after 35 s (slow)
  d  test_talk_reaches_a_protocol_listener        6 s hold: on air throughout (TALK1), the USRP listener gets it, release ends it (slow)
  d2 test_talk_hold_10_s_stays_on_air_until_release  10 s hold: on air until release, 'renewed' acks counted and spaced (slow)
  d3 test_talk_release_inside_the_watchdog_window  2 s hold: "Ended: Button released." (slow)
  e  test_talk_denied_without_may_tx              no Talk controls; a raw ptt frame on the control channel is recorded
  f  test_talk_floor_busy_when_the_room_is_taken  a protocol talker holds the floor, Hold Talk is denied (slow)
  g  test_talk_second_talker_is_denied            the browser holds the floor, a second protocol talker is not heard (slow)
  h  test_reconnect_after_disconnect_and_reload   manual reconnect, reload, record what the page does (record only)
  i  test_reconnect_after_the_lease_expires       stop /rtc/alive, record page and server (slow, record only)
  j  test_listen_gauges_in_the_hub_metrics        dv_edge_webrtc_listeners / _room_listeners{room}: 1 while listening, 0 after Disconnect
"""
import json
import os
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

import web_pages as pages  # noqa: E402
from dvtest import browser as brlib  # noqa: E402
from dvtest import metrics, web  # noqa: E402
from webrtc_support import (  # noqa: E402
    MIC_UNAVAILABLE, SILENCE_RMS, TONE_HZ, TONE_TOL_HZ, Rtc, Talk, find_values, gaps_ms, on_air_or_end, pcm_floats, press,
    public_state, pump_while, release, set_info_callsigns, tone_frames, usrp_client, voice_frames, wait_talk_text)

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc]

ROOM_LISTEN, ROOM_LEASE, ROOM_TALK, ROOM_RX, ROOM_FLOOR, ROOM_BUSY, ROOM_RECONNECT, ROOM_EXPIRE = "HJKLQWRX"
ROOM_HOLD10, ROOM_SHORT, ROOM_GAUGE = "YZM"       # recorder cases use S, T, U
PROTO_CALL = "N0TEST"           # the protocol talker / listener
PROTO_CALL2 = "N0USR"           # the second protocol client
SECOND_HZ = 1700.0              # its tone: not a harmonic of Firefox's 1000 Hz beep or of the 500 Hz of WebKit's mock device
_SEEN = set()


def note(engine, key, value):
    """A measurement or observation of one engine: appended to $DVT_OUT/web/webrtc-notes.jsonl and shown as a
    FINDING line (web-findings.txt) once."""
    row = {"engine": brlib.label(engine), "key": key, "value": value}
    path = Path(os.environ.get("DVT_OUT", "out")) / "web" / "webrtc-notes.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass
    k = (engine, key)
    if k not in _SEEN:
        _SEEN.add(k)
        web.finding("%s: %s: %s" % (brlib.label(engine), key, value if isinstance(value, str) else json.dumps(value, sort_keys=True)))


def rms_db(x):
    import math
    return round(20 * math.log10(x), 1) if x > 0 else None


def sdp_facts(sdp):
    text = (sdp or {}).get("remote") or ""
    return {"pcmu": bool(re.search(r"a=rtpmap:0 PCMU/8000", text)), "ptime20": "a=ptime:20" in text,
            "ice_lite": "a=ice-lite" in text, "setup_passive": "a=setup:passive" in text,
            "candidates": re.findall(r"a=candidate:\S+ \d+ udp \d+ (\S+) (\d+) typ (\w+)", text),
            "sctp": "m=application" in text}


def listening(rtc, room):
    t = rtc.connect(room)
    assert t["status"] == "Listening on module %s" % room, t
    return t


def audio_checks(engine, rtc, label):
    """Start the tap; returns the tap start dict (rate, state) or a dict with 'error'."""
    started = rtc.tap_start()
    note(engine, "%s: audio tap" % label, started)
    return started


# ------------------------------------------------------------------------------------ a

def test_listen_is_not_offered_to_anonymous(engine, dashboard_page):
    """guest_listen is off in the rig (default): a signed-out visitor sees the dashboard and no Listen panel;
    the offer route answers 401 (stage 2 covers the API)."""
    bp = dashboard_page
    bp.goto("/#/")
    pages.room_cards(bp.page).first.wait_for(state="visible", timeout=30000)
    bp.page.wait_for_timeout(1500)
    assert pages.listen_panel(bp.page).count() == 0
    assert bp.seen("/api/v1/rtc/") == [] or all(r["method"] != "POST" for r in bp.log.requests if "/rtc/" in r["path"])


# ------------------------------------------------------------------------------------ b

def test_listen_receives_the_tone(engine, rtc_rx_page, target):
    bp = rtc_rx_page
    rtc = Rtc(bp)
    rtc.open()
    # a user without may_tx listens and is not offered Talk
    assert pages.talk_button(bp.page).count() == 0
    t = listening(rtc, ROOM_LISTEN)
    note(engine, "connection time", {"click_to_connected_ms": t["click_to_connected_ms"],
                                     "click_to_listening_text_ms": t["wall_to_listening_ms"]})
    assert t["click_to_connected_ms"] is not None and t["click_to_connected_ms"] < 15000

    facts = sdp_facts(rtc.sdp())
    note(engine, "answer sdp", facts)
    assert facts["pcmu"], "the answer does not offer PCMU/8000"
    ice_servers = [r for r in bp.log.requests if r["path"] == "/api/v1/rtc/ice"]
    note(engine, "GET /rtc/ice", [r["status"] for r in ice_servers])
    assert len(ice_servers) == 1, "the page fetches /rtc/ice once per connect (web.md)"

    tap = audio_checks(engine, rtc, "listen")
    can_tap = bool(tap) and "error" not in tap
    time.sleep(1.5)
    silence = rtc.analyse(1.0) if can_tap else None
    inb0 = rtc.inbound()
    if silence is not None:
        note(engine, "silence before the over (rms)", silence["rms"])
        assert silence["rms"] < SILENCE_RMS, "sound with nobody talking: %s" % silence

    talker = usrp_client(target, PROTO_CALL, ROOM_LISTEN)
    try:
        talk = Talk(talker, tone_frames(6.0))
        talk.start()
        assert talk.started.wait(5)
        time.sleep(1.5)
        s1 = rtc.inbound()
        heard = None
        if can_tap:
            # best of three 1 s windows: Playwright WebKit's Web Audio tap sometimes drops buffers (a window
            # with holes has no clean peak); the claim is that the tone arrives, so one clean window is enough
            tries = []
            for _ in range(3):
                time.sleep(0.8)
                tries.append(rtc.analyse(1.0))
            heard = max(tries, key=lambda h: h["fraction"])
            if len([t for t in tries if abs(t["peak_hz"] - TONE_HZ) > TONE_TOL_HZ]) and engine != "chromium":
                note(engine, "audio tap windows", [(t["peak_hz"], round(t["fraction"], 2), round(t["rms"], 3)) for t in tries])
        s2 = rtc.inbound()
        talk.join(timeout=10)
        assert talk.error is None, talk.error
        growth, problems = brlib.inbound_verdict(s1, s2)
        note(engine, "inbound-rtp while talking", {"growth": growth, "codec": s2["codec"], "jitter_s": s2["raw"].get("jitter"),
                                                   "lost_total": s2["packetsLost"]})
        assert not problems, problems
        assert growth["bytes_per_packet"] == 160, "PCMU 20 ms is 160 bytes: %s" % growth
        assert "PCMU" in (s2["codec"] or "").upper(), s2["codec"]
        assert (s2["raw"].get("jitter") or 0) < 0.06, "jitter %s s" % s2["raw"].get("jitter")
        if inb0:
            assert s1["packetsReceived"] - inb0["packetsReceived"] >= 0

        if heard is not None:
            note(engine, "tone in the page", {k: heard[k] for k in ("peak_hz", "peak_amp", "fraction", "rms", "rate", "n")})
            if heard["rms"] == 0:
                note(engine, "audio tap", "silent while packets arrive: the engine does not feed the received stream "
                                          "to Web Audio here (headless); frequency not judged")
                if engine == "chromium":
                    pytest.fail("Playwright Chromium: Web Audio tap silent while RTP arrives")
            else:
                assert abs(heard["peak_hz"] - TONE_HZ) <= TONE_TOL_HZ, heard
                assert heard["fraction"] > 0.5, heard
        else:
            note(engine, "audio tap", "not available: %s" % tap)

        # the over ended: no packets of speech any more and the audio is silent again
        time.sleep(1.5)
        e1 = rtc.inbound()
        time.sleep(1.0)
        e2 = rtc.inbound()
        note(engine, "packets after the over", e2["packetsReceived"] - e1["packetsReceived"])
        if can_tap:
            after = rtc.analyse(0.5)
            note(engine, "silence after the over (rms)", after["rms"])
            assert after["rms"] < SILENCE_RMS, after
    finally:
        talker.close()

    # Disconnect closes the circuit
    circuit = rtc.offers[-1]
    rtc.disconnect()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not rtc.sent_posts("close"):
        time.sleep(0.1)
    assert rtc.sent_posts("close") == 1, rtc.sent
    assert rtc.wait_idle(), "after Disconnect: data-state %r, status %r" % (rtc.panel_state(), rtc.status())
    assert rtc.panel_state() == "closed", "a local disconnect reports data-state closed (web/README.md): %r" % rtc.panel_state()
    assert set(rtc.alive_posts()) <= {200, 204}
    # the close is a keepalive fetch (no response event in the page log): the server side shows it
    time.sleep(0.5)
    gone = post_rtc(rtc, "alive", circuit)
    note(engine, "alive on the circuit after Disconnect", gone)
    assert gone in (401, 403), "the circuit still lives after Disconnect: /rtc/alive -> %s" % gone
    assert (rtc.audio_element() or {}).get("hasStream") is False


# ------------------------------------------------------------------------------------ c

@pytest.mark.slow
def test_listen_lease_stays_alive_past_30_s(engine, rtc_rx_page, target):
    bp = rtc_rx_page
    rtc = Rtc(bp)
    rtc.open()
    listening(rtc, ROOM_LEASE)
    t0 = time.monotonic()
    rtc.tap_start()
    seen = []
    while time.monotonic() - t0 < 36.0:
        time.sleep(4.0)
        st = rtc.state()
        seen.append((round(time.monotonic() - t0), st["connection"] if st else None, len(rtc.alive_posts())))
        assert st and st["connection"] == "connected", seen
        assert pages.listen_status(bp.page).inner_text().strip() == "Listening on module %s" % ROOM_LEASE, seen
    alive = rtc.alive_posts()
    note(engine, "alive posts in 36 s", {"statuses": alive, "timeline": seen})
    assert len(alive) >= 3 and set(alive) <= {200, 204}, alive
    # still receiving after the 15 s initial and the 30 s renewed lease
    talker = usrp_client(target, PROTO_CALL, ROOM_LEASE)
    try:
        s0 = rtc.inbound() or {"packetsReceived": 0}       # no inbound-rtp entry before the first packet
        talk = Talk(talker, tone_frames(2.5))
        talk.start()
        time.sleep(1.6)
        heard = rtc.analyse(0.5)
        talk.join(timeout=10)
        time.sleep(0.5)
        s1 = rtc.inbound()
    finally:
        talker.close()
    got = s1["packetsReceived"] - s0["packetsReceived"]
    note(engine, "packets for a 2.5 s over after 36 s of listening", got)
    assert got >= 100, "only %d packets of a 2.5 s over after the lease had been renewed" % got
    if heard["rms"] > 0:
        # This case is about the lease. The tone is judged in the dedicated listen case; Playwright WebKit
        # once read a peak at 3600 Hz here (1 run of 2, same stream, packets fine), so it is recorded
        # instead of asserted for that engine.
        if engine == "webkit":
            note(engine, "tone peak in the lease case (Hz, sent %d)" % TONE_HZ, heard["peak_hz"])
        else:
            assert abs(heard["peak_hz"] - TONE_HZ) <= TONE_TOL_HZ, heard


# ------------------------------------------------------------------------------------ d..g talk

def mic_unavailable(engine, rtc, text):
    """The engine has no usable fake microphone: record and skip the rest of the case."""
    note(engine, "fake microphone", "not usable (talk status %r; panel %r)" % (text, rtc.status()))
    pytest.skip("%s: no fake microphone (talk status %r)" % (brlib.label(engine), text))


def hold_talk(engine, bp, target, web_client, room, hold_s, min_acks, label):
    """TALK1 (DV-Router web.md "PTT", wip-13e7e70): Listen on `room`, hold the Talk button for `hold_s` seconds
    after the page shows "On air", release. Asserted: the page stays "On air" for the whole hold (the edge
    acknowledges each accepted lease renewal with {"op":"ptt","state":"renewed","code":"ok"} and the page resets
    its 4 s watchdog on it), the release shows "Ended: Button released.", a USRP listener on the module hears
    >= 80 % of 50 frames/s of the time on air with no gap over the limit and nothing after the release, the
    renewal acks on the ctl DataChannel are exactly that frame, at most one per 500 ms (receipt intervals
    >= 450 ms) and at least `min_acks` of them. Returns the measurements."""
    rtc = Rtc(bp)
    rtc.open()
    assert pages.talk_button(bp.page).inner_text().strip() == "Hold Talk"
    assert rtc.talk_status() == pages.TALK_READY_TEXT
    assert pages.talk_button(bp.page).is_disabled()                      # not before Listen
    listener = usrp_client(target, PROTO_CALL, room)
    anon = web_client()
    try:
        listening(rtc, room)
        assert pages.talk_button(bp.page).is_enabled()
        rtc.tap_start()
        press(rtc)
        text, took = wait_talk_text(rtc, on_air_or_end, 30.0)
        if text.startswith(MIC_UNAVAILABLE):
            release(rtc)
            mic_unavailable(engine, rtc, text)
        note(engine, "%s: press to On air" % label, {"seconds": round(took, 2) if took else None, "status": text})
        assert text == pages.TALK_ON_AIR_TEXT, "talk status %r after pressing (panel %r)" % (text, rtc.status())
        t_air = time.monotonic()
        page_air = rtc.clock()
        seen_paths = []
        timeline = []

        def poll():
            timeline.append((round(time.monotonic() - t_air, 1), rtc.talk_status()))
            if not seen_paths:
                seen_paths.extend(find_values(public_state(anon), "N0BRU"))

        pump_while(listener, max(0.0, hold_s - (time.monotonic() - t_air)), poll, poll_every=0.5)
        left_on_air = next(((t, st) for t, st in timeline if st != pages.TALK_ON_AIR_TEXT), None)
        note(engine, "%s: status while the button is held" % label, {"timeline": timeline, "left On air": left_on_air})
        outbound = rtc.stats()["outbound"] or {}
        page_release = rtc.clock()
        t_release = release(rtc)
        on_air_s = t_release - t_air
        ended, took_end = wait_talk_text(rtc, lambda s: s != pages.TALK_ON_AIR_TEXT, 8.0)
        note(engine, "%s: after release" % label, {"status": ended, "seconds": round(took_end, 2) if took_end else None})
        pump_while(listener, 1.5)
        frames = voice_frames(listener)
        times = [t for t, _p in frames]
        samples = pcm_floats([p for _t, p in frames])
        peak, _amp, frac, level = brlib.scan_peak(samples, 8000) if samples else (0.0, 0.0, 0.0, 0.0)
        g = gaps_ms(times)
        note(engine, "%s: protocol listener heard" % label, {
            "frames": len(frames), "held_on_air_s": round(on_air_s, 1), "max_gap_ms": max(g) if g else None,
            "peak_hz": peak, "fraction": round(frac, 2), "rms": round(level, 3), "outbound_packets": outbound.get("packetsSent")})
        talkers = set_info_callsigns(listener)
        note(engine, "SET_INFO callsigns at the protocol listener", sorted(set(talkers)) or "none")
        # the renewal acknowledgements on the ctl channel, between On air and the release (+ 300 ms in flight)
        received = rtc.ctl_received()
        sent = rtc.ctl_sent()
        acks = brlib.renewal_verdict(received, since_ms=page_air, until_ms=page_release + 300.0)
        leases = [m for m in sent if m.get("label") == "ctl" and '"lease"' in (m.get("data") or "")
                  and page_air <= m["t"] <= page_release]
        note(engine, "%s: ctl renewal acks" % label, {k: acks[k] for k in ("acks", "min_interval_ms", "other_ptt", "odd_acks")}
             | {"leases_sent": len(leases), "intervals_ms": acks["intervals_ms"][:12]})

        # TALK1: a held button keeps the over until it is released
        assert left_on_air is None, "%s: the page left 'On air' after %.1f s of a %.0f s hold (status %r)" % (
            brlib.label(engine), left_on_air[0], hold_s, left_on_air[1])
        assert ended == pages.TALK_RELEASED_TEXT, "after the release the talk status is %r" % ended
        assert len(frames) >= 0.8 * 50 * on_air_s, "%d frames for %.1f s on air" % (len(frames), on_air_s)
        assert g, "fewer than two frames at the protocol listener"
        limit = 120.0 if engine != "webkit" else 250.0          # WebKit's mock device is bursty (gap recorded above)
        assert max(g) <= limit, "dropout: a gap of %.0f ms in the over (%s)" % (max(g), sorted(g)[-3:])
        # getUserMedia({audio: true}) leaves echo cancellation, noise suppression and AGC on: they treat a pure
        # tone as noise, so the tone fraction is judged loosely; the dominant frequency is what the file said
        if engine == "chromium":
            assert abs(peak - TONE_HZ) <= TONE_TOL_HZ and frac > 0.1, (peak, frac)
        else:
            # Firefox: its own fake source (a beep of about 1 kHz), WebKit: its mock device; recorded
            assert level > 0.01, "silence from the %s fake microphone" % brlib.label(engine)
            note(engine, "fake microphone tone", {"peak_hz": peak, "fraction": round(frac, 2)})
        # the over ended with the button: no voice in the last second
        assert time.monotonic() - times[-1] >= 0.9, "frames kept arriving after the release"
        # the acks: only observable through the page's own channel (the probe wraps createDataChannel)
        if not brlib.ctl_frames(received):
            web.finding("%s: %s: the probe saw no frame on the ctl DataChannel (not even the grant), so the "
                        "'renewed' acknowledgements of TALK1 are not observable in this engine" % (brlib.label(engine), label))
        else:
            assert not acks["problems"], acks["problems"]
            assert acks["acks"] >= min_acks, "%d 'renewed' acks during %.1f s on air (%d leases sent), expected at least %d" % (
                acks["acks"], on_air_s, len(leases), min_acks)
        # TX identity: the account callsign, wherever it can be seen
        if not seen_paths and "N0BRU" not in talkers:
            web.finding("%s: the callsign of the web talker (N0BRU) is neither in the public state during the over "
                        "nor in a SET_INFO to a USRP client" % brlib.label(engine))
        note(engine, "public state names the talker (paths)", seen_paths[:4] or "not seen")
        return {"on_air_s": on_air_s, "frames": len(frames), "acks": acks}
    finally:
        listener.close()


@pytest.mark.slow
def test_talk_reaches_a_protocol_listener(engine, rtc_tx_page, target, web_client):
    """A 6 s hold (stage 3b: the page ended it by itself after about 4 s, fixed by TALK1): on air for the
    whole hold, the USRP listener hears it, release ends it."""
    hold_talk(engine, rtc_tx_page, target, web_client, ROOM_TALK, 6.0, min_acks=2, label="6 s hold")


@pytest.mark.slow
def test_talk_hold_10_s_stays_on_air_until_release(engine, rtc_tx_page, target, web_client):
    """TALK1: a 10 s hold, more than twice the page's 4 s watchdog: the 'renewed' acks of about one lease per
    second keep it alive (at least 4 acks asked for, about 9 expected), the router hears about 10 s of frames,
    the over ends on release with "Ended: Button released."."""
    hold_talk(engine, rtc_tx_page, target, web_client, ROOM_HOLD10, 10.0, min_acks=4, label="10 s hold")


@pytest.mark.slow
def test_talk_release_inside_the_watchdog_window(engine, rtc_tx_page, target, web_client):
    """A 2 s hold, released well inside the 4 s watchdog: "Ended: Button released." (finding 10 of stage 3b:
    not seen before because the watchdog always came first)."""
    hold_talk(engine, rtc_tx_page, target, web_client, ROOM_SHORT, 2.0, min_acks=0, label="2 s hold")


@pytest.mark.slow
def test_talk_denied_without_may_tx(engine, rtc_rx_page):
    """A user without may_tx: no Talk controls in the page (web.md); a raw 'ptt down' on the control channel
    (the page never sends it for this user) is answered and recorded, never granted."""
    bp = rtc_rx_page
    rtc = Rtc(bp)
    rtc.open()
    assert pages.talk_button(bp.page).count() == 0
    assert pages.talk_controls(bp.page).count() == 0
    listening(rtc, ROOM_RX)
    sent = rtc.ev("window.__dvtTap.sendControl(JSON.stringify({op: 'ptt', state: 'down', seq: 0, t_ms: 1}))")
    time.sleep(2.0)
    msgs = rtc.ev("window.__dvtTap.msgs")
    note(engine, "raw ptt down from a user without may_tx", {"sent": sent, "replies": [m["data"] for m in msgs]})
    assert sent
    for m in msgs:
        reply = json.loads(m["data"])
        assert reply.get("state") != "granted", reply
    out = rtc.stats()["outbound"]
    assert out is None or (out.get("packetsSent") or 0) == 0
    rtc.ev("window.__dvtTap.sendControl(JSON.stringify({op: 'ptt', state: 'up', seq: 1, t_ms: 2}))")


@pytest.mark.slow
def test_talk_floor_busy_when_the_room_is_taken(engine, rtc_tx_page, target):
    bp = rtc_tx_page
    rtc = Rtc(bp)
    rtc.open()
    holder = usrp_client(target, PROTO_CALL2, ROOM_FLOOR)
    try:
        listening(rtc, ROOM_FLOOR)
        talk = Talk(holder, tone_frames(24.0, freq=1000.0))
        talk.start()
        assert talk.started.wait(5)
        time.sleep(1.0)
        press(rtc)
        text, took = wait_talk_text(rtc, on_air_or_end, 30.0)
        if text.startswith(MIC_UNAVAILABLE):
            release(rtc)
            mic_unavailable(engine, rtc, text)
        note(engine, "Hold Talk while a protocol talker holds the floor", {"status": text, "seconds": round(took, 2) if took else None})
        release(rtc)
        assert text.startswith("Denied") or text.startswith("Ended"), "the floor was not refused: %r" % text
        assert text != "On air"
        if text not in ("Denied: The floor is busy.", "Denied: The floor is held."):
            web.finding("%s: floor taken by a protocol talker gives %r in the page; the documented codes are busy (The "
                        "floor is busy.) and held (The floor is held.)" % (brlib.label(engine), text))
        talk.join(timeout=30)
    finally:
        holder.close()


@pytest.mark.slow
def test_talk_second_talker_is_denied(engine, rtc_tx_page, target):
    """The browser holds the floor with 700 Hz; a protocol client that talks 1000 Hz on the same module is not
    heard by a third client (first arrival wins, floor-control.md)."""
    bp = rtc_tx_page
    rtc = Rtc(bp)
    rtc.open()
    listener = usrp_client(target, PROTO_CALL, ROOM_BUSY)
    second = usrp_client(target, PROTO_CALL2, ROOM_BUSY)
    try:
        listening(rtc, ROOM_BUSY)
        press(rtc)
        text, _took = wait_talk_text(rtc, on_air_or_end, 30.0)
        if text.startswith(MIC_UNAVAILABLE):
            release(rtc)
            mic_unavailable(engine, rtc, text)
        assert text == "On air", text
        listener.inbox.clear()
        time.sleep(0.4)
        talk = Talk(second, tone_frames(2.0, freq=SECOND_HZ))       # inside the page's 4 s of "On air"
        talk.start()
        pump_while(listener, 2.6)
        talk.join(timeout=5)
        during = [p for _t, p in voice_frames(listener)]
        # only the browser's tone: the second talker's tone must not be mixed in or take over
        samples = pcm_floats(during)
        second_amp = brlib.goertzel_amplitude(samples, 8000, SECOND_HZ) if samples else 0.0
        first_amp = brlib.goertzel_amplitude(samples, 8000, TONE_HZ) if samples else 0.0
        note(engine, "second talker during the browser's over",
             {"frames_at_listener": len(during), "amp_700hz_first": round(first_amp, 3), "amp_%dhz" % SECOND_HZ: round(second_amp, 3)})
        release(rtc)
        assert len(during) > 0
        near = max(brlib.goertzel_amplitude(samples, 8000, hz) for hz in (SECOND_HZ - 200.0, SECOND_HZ - 100.0, SECOND_HZ + 100.0, SECOND_HZ + 200.0)) if samples else 0.0
        assert second_amp < max(0.02, 3.0 * near), "the second talker was heard (%.3f at %d Hz, neighbours %.3f)" % (second_amp, SECOND_HZ, near)
    finally:
        listener.close()
        second.close()


# ------------------------------------------------------------------------------------ h, i reconnect (record only)

def csrf_of(rtc):
    return rtc.ev("""async () => (await (await fetch('/api/v1/auth/session', {credentials: 'same-origin'})).json()).csrf""")


def post_rtc(rtc, name, sess_id):
    """POST /api/v1/rtc/<name> from the page with a circuit id; the HTTP status."""
    csrf = csrf_of(rtc)
    return rtc.ev("""async ([name, id, csrf]) => { const r = await fetch('/api/v1/rtc/' + name, {method: 'POST',
        credentials: 'same-origin', headers: {'content-type': 'application/json', 'X-DV-CSRF': csrf},
        body: JSON.stringify({sess_id: id})}); return r.status; }""", [name, sess_id, csrf])


def test_reconnect_after_disconnect_and_reload(engine, rtc_rx_page):
    """Record only. ICE restart and automatic reconnect are not built (ICE1): the page offers no reconnect
    of its own, a new connect is a new offer on a new circuit."""
    bp = rtc_rx_page
    rtc = Rtc(bp)
    rtc.open()
    first = listening(rtc, ROOM_RECONNECT)
    rtc.disconnect()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not rtc.sent_posts("close"):
        time.sleep(0.1)
    closed_status = post_rtc(rtc, "alive", rtc.offers[-1])
    note(engine, "alive on a closed circuit", closed_status)
    second = listening(rtc, ROOM_RECONNECT)
    note(engine, "second connect (new offer)", {"first_ms": first["click_to_connected_ms"], "second_ms": second["click_to_connected_ms"],
                                                "offers": len(rtc.offers), "distinct_circuits": len(set(rtc.offers))})
    assert len(set(rtc.offers)) == 2
    live_id = rtc.offers[-1]
    # reload while connected: pagehide revokes the circuit (keepalive close), the new page is idle
    bp.page.reload(wait_until="load")
    pages.listen_panel(bp.page).wait_for(state="visible", timeout=30000)
    text = rtc.status()
    time.sleep(1.5)
    after_reload = post_rtc(rtc, "alive", live_id)
    note(engine, "after a reload", {"panel": text, "alive on the old circuit": after_reload,
                                    "panel needs a click": text == pages.LISTEN_IDLE_TEXT})
    third = listening(rtc, ROOM_RECONNECT)
    note(engine, "connect after the reload", third["click_to_connected_ms"])
    if after_reload not in (401, 403):
        web.finding("%s: the circuit of a page that was reloaded still answers /rtc/alive with %s" % (brlib.label(engine), after_reload))
    rtc.disconnect()


@pytest.mark.slow
def test_reconnect_after_the_lease_expires(engine, rtc_rx_page, target):
    """Record only. Block /rtc/alive in the browser; the page's own deadline is 25 s after the last renewal
    (14 s before the first), the server lease 15 s then 30 s. No NET_ADMIN tricks: the media path is untouched."""
    bp = rtc_rx_page
    rtc = Rtc(bp)
    rtc.open()
    listening(rtc, ROOM_EXPIRE)
    rtc.tap_start()
    time.sleep(2.0)                          # first /rtc/alive done at connect
    circuit = rtc.offers[-1]
    bp.context.route("**/api/v1/rtc/alive", lambda route: route.abort())
    t0 = time.monotonic()
    panel = ""
    while time.monotonic() - t0 < 45.0:
        panel = rtc.status()
        if panel.startswith("Connection ownership expired") or panel == pages.LISTEN_IDLE_TEXT:
            break
        time.sleep(0.5)
    page_s = time.monotonic() - t0
    state = rtc.state()
    note(engine, "alive blocked: page", {"seconds_to_message": round(page_s, 1), "panel": panel, "peer": state})
    # the page revokes the circuit when it gives up
    time.sleep(2.0)
    note(engine, "alive blocked: close sent", rtc.sent_posts("close"))
    bp.context.unroute("**/api/v1/rtc/alive")
    # the server's lease: wait out the 30 s from the last good renewal, then ask the old circuit
    wait = max(0.0, 36.0 - (time.monotonic() - t0))
    time.sleep(wait)
    status = post_rtc(rtc, "alive", circuit)
    note(engine, "alive on the expired circuit", status)
    assert status in (401, 403, 404), status
    again = listening(rtc, ROOM_EXPIRE)
    note(engine, "connect after the lease expired", {"click_to_connected_ms": again["click_to_connected_ms"],
                                                     "needs": "a click on Listen / Connect (no automatic reconnect, ICE restart not built)"})
    rtc.disconnect()


# ------------------------------------------------------------------------------------ j gauges (WEB2)

LISTENERS = "dv_edge_webrtc_listeners"
ROOM_LISTENERS = "dv_edge_webrtc_room_listeners"
GAUGE_WAIT_S = 35.0       # the edge refreshes its snapshot every 1 s and publishes it every 10 s (web.md); the hub caches


def edge_gauges(rig_control, room):
    """(listeners, room listeners, families present) from the hub metrics of the SUT (dv-svc-status exports the edge's
    telemetry with a `proc` label; the room label is the module letter in lower case)."""
    text = rig_control.metrics("xlxd-sut")
    fam = metrics.families(text)
    samples = metrics.parse(text)
    room_sum = sum(s.value for s in samples if s.name == ROOM_LISTENERS and s.labels.get("room", "").lower() == room.lower())
    return metrics.total(samples, LISTENERS), room_sum, {LISTENERS, ROOM_LISTENERS} <= fam


def wait_gauges(rig_control, room, pred, timeout_s=GAUGE_WAIT_S):
    end = time.monotonic() + timeout_s
    seen = []
    while True:
        got = edge_gauges(rig_control, room)
        seen.append((round(got[0]), round(got[1])))
        if got[2] and pred(got[0], got[1]):
            return got, True, seen
        if time.monotonic() >= end:
            return got, False, seen
        time.sleep(2.0)


def test_listen_gauges_in_the_hub_metrics(engine, rtc_rx_page, target, rig_control):
    """WEB2: the edge gauges dv_edge_webrtc_listeners and dv_edge_webrtc_room_listeners{room} count an admitted
    browser listener: 1 on the module while the page listens, 0 after Disconnect (no other listener in the rig
    meanwhile: the cases run one at a time)."""
    if target.sut_profile != "dvrouter":
        pytest.skip("DV-Router only: DVT_SUT_PROFILE=dvrouter (profile %s)" % target.sut_profile)
    try:
        edge_gauges(rig_control, ROOM_GAUGE)
    except (RuntimeError, TimeoutError) as exc:
        pytest.skip("the hub metrics are not reachable through the control channel (%s)" % exc)
    bp = rtc_rx_page
    rtc = Rtc(bp)
    rtc.open()
    listening(rtc, ROOM_GAUGE)
    (total, room, present), ok, seen = wait_gauges(rig_control, ROOM_GAUGE, lambda t, r: r >= 1)
    note(engine, "edge gauges while listening on %s" % ROOM_GAUGE, {"listeners": total, "room": room, "samples": seen})
    if not present:
        pytest.skip("%s / %s not in the hub metrics after %.0f s of listening: not observable on this image"
                    % (LISTENERS, ROOM_LISTENERS, GAUGE_WAIT_S))
    assert ok and room == 1, "%s{room=%r} is %s while one page listens (samples %s)" % (ROOM_LISTENERS, ROOM_GAUGE.lower(), room, seen)
    assert total >= 1, "%s is %s while a page listens" % (LISTENERS, total)
    if total != 1:
        note(engine, "edge listeners gauge with one page listening", total)
    rtc.disconnect()
    assert rtc.wait_idle()
    (total, room, _p), ok, seen = wait_gauges(rig_control, ROOM_GAUGE, lambda t, r: r == 0)
    note(engine, "edge gauges after Disconnect", {"listeners": total, "room": room, "samples": seen})
    assert ok, "%s{room=%r} still %s %.0f s after Disconnect (samples %s)" % (ROOM_LISTENERS, ROOM_GAUGE.lower(), room, GAUGE_WAIT_S, seen)
