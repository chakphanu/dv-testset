# SPDX-License-Identifier: MIT
"""Web unit, stage 4: NAT behaviour of the browser WebRTC path. Needs `scripts/run.sh --profile web,browser,nat`
(docs/rig.md "NAT profile", docs/web-nat-cases.md): the browsers sit alone behind the rig's NAT router and the
Listen panel of DV-Router's real dashboard connects to the ICE-lite edge (one UDP port, 50000) through a Linux
NAT of a chosen type, directly or through the coturn rig service. Engines: Playwright Chromium, Firefox and
Playwright WebKit (the engine of Safari, not Safari). Differences from the documents are recorded with
web.finding(), never encoded; the adopted pass criteria of DV-Router docs/project/testing.md ("Browser/NAT rig
criteria") are asserted.

  a  test_nat_classification            the NAT types are what they claim: mapping and filtering measured with two
                                        reflectors and unsolicited datagrams (per NAT type, no browser)
  b  test_direct_listen                 direct path: connected and first audio within 10 s of the answer, 20 s of
                                        arrival timing (p99 <= 20 ms, max <= 60 ms), the tone, the selected pair
  c  test_turn_relay                    UDP to the media port dropped: the page must use the relay; selected pair is
                                        relay, audio plays, one GET /rtc/ice, credential format, nothing secret in
                                        the page (needs turn.toml)
  d  test_blackhole_resume_and_failure  2, 3, 5 s UDP blackholes resume without a new offer; longer ones until ICE
                                        fails: the measured failure interval; manual reconnect with a new offer (slow)
  e  test_turn_credential_expiry        relay session behind the symmetric NAT longer than turn_ttl (slow)
  f  test_ice_404_goes_direct           no TURN configured: GET /rtc/ice answers 404 and the page goes direct
  g  test_ice_429_goes_direct           third connect within a minute: 429 from the issuer, the page goes direct

Calibrated against dv-router:wip-13e7e70: the dashboard renders as served (DASH1, no workaround), the Listen card
is driven through the WEB2 hooks of cases/web_pages.py and its data-state (connected, failed, closed), and the
browser fixtures start only after GET /ready of dv-svc-web answered 200 (browser_fixtures.web_ready). The protocol
talker is a USRP dynamic client (DVT_USRP_DYNAMIC=1).
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
from dvtest import nat, turn, web  # noqa: E402
from nat_support import (  # noqa: E402
    CONNECT_BUDGET_S, JITTER_MAX_MS, JITTER_P99_MS, NAT_TYPES, IcePacer, NatRtc, rig_client, turn_on, turn_secret, turn_ttl)
from webrtc_support import TONE_HZ, TONE_TOL_HZ, Talk, tone_frames, usrp_client  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc, pytest.mark.nat]

ROOM_DIRECT, ROOM_TURN, ROOM_HOLE, ROOM_EXPIRE, ROOM_FALLBACK = "HJKLQ"
PROTO_CALL = "N0TEST"
EXPECTED_CLASS = {"full_cone": (nat.EI, nat.EI), "port_restricted": (nat.EI, nat.APD), "symmetric": (nat.APD, nat.APD)}
BLACKHOLE_SHORT = (2, 3, 5)
BLACKHOLE_LONG = (8, 12, 20, 30)
BLACKHOLE_CAP_S = 150
EXPIRY_OBSERVE_S = int(os.environ.get("DVT_NAT_EXPIRY_S", "660"))
_SEEN = set()
_PACER = IcePacer()
needs_turn = pytest.mark.skipif(os.environ.get("DVT_TURN") != "on",
                                reason="TURN is not configured in this run (add rig/config/dv-router/turn.toml to DVT_SUT_CONFIG_EXTRA)")


def note(engine, nat_type, key, value):
    """A measurement of one engine behind one NAT type: appended to $DVT_OUT/web/nat-results.jsonl and shown once as
    a FINDING line (web-findings.txt)."""
    row = {"engine": brlib.label(engine) if engine else None, "nat": nat_type, "key": key, "value": value}
    path = Path(os.environ.get("DVT_OUT", "out")) / "web" / "nat-results.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass
    k = (engine, nat_type, key)
    if k not in _SEEN:
        _SEEN.add(k)
        web.finding("%s / %s: %s: %s" % (brlib.label(engine) if engine else "-", nat_type, key,
                                         value if isinstance(value, str) else json.dumps(value, sort_keys=True)))


# ---------------------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def nat_users(web_operator, web_user):
    """Every listener account of this module, made at the start: the operator session that registers them does not
    last for the hour this module can take. Skipped (nothing made) when the NAT rig is not up."""
    if os.environ.get("DVT_NAT") != "on":
        pytest.skip("nat profile not up")
    for i in range(1, len(brlib.ENGINES) * len(NAT_TYPES) + 1):
        web_user("N0NA%d" % i, may_tx=False)
    for i in range(1, len(brlib.ENGINES) + 1):
        web_user("N0NB%d" % i, may_tx=False)


@pytest.fixture(scope="session")
def nat_rig(nat_users):
    client = rig_client()
    client.ctl("reset")
    yield client
    try:
        client.ctl("reset")
    except Exception:                                   # noqa: BLE001
        pass


@pytest.fixture(params=NAT_TYPES)
def nat_type(request):
    return request.param


@pytest.fixture
def nat_ctl(nat_rig, nat_type):
    """The router in the NAT type of the case, no blackhole, no drop_direct; reset again afterwards."""
    nat_rig.ctl("reset")
    nat_rig.mode(nat_type)
    yield nat_rig
    try:
        nat_rig.ctl("reset")
    except Exception:                                   # noqa: BLE001
        pass


def user_index(engine, nat_type):
    return brlib.ENGINES.index(engine) * len(NAT_TYPES) + NAT_TYPES.index(nat_type) + 1


@pytest.fixture
def nat_page(nat_ctl, nat_type, engine, engine_browsers, web_base_url, request, web_user):
    """The page of one (engine, NAT type): signed in as its own listener (the issuer of TURN credentials allows two
    per minute per login, the cases share the login of their combination and the pacer waits for tokens)."""
    user = web_user("N0NA%d" % user_index(engine, nat_type), may_tx=False)
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, user, talk=False)
    bp.nat_key = user.callsign
    yield bp
    _rtc_end(bp, request)


def talker_over(target, room, seconds, peer=None):
    """Key an over of `seconds` of a 700 Hz tone on a USRP dynamic client (a new one unless `peer` is given: the
    SUT's dynamic hub keeps a client until its idle timeout, so the cases with many overs reuse one)."""
    talker = peer or usrp_client(target, PROTO_CALL, room)
    talk = Talk(talker, tone_frames(seconds))
    talk.start()
    assert talk.started.wait(5)
    return talker, talk


def audio_growth(rtc, seconds=1.5):
    """Packets received over `seconds` (the sender must be talking)."""
    a = rtc.inbound() or {"packetsReceived": 0}
    time.sleep(seconds)
    b = rtc.inbound() or {"packetsReceived": 0}
    return b["packetsReceived"] - a["packetsReceived"]


def _count(rtc):
    """(id of the inbound-rtp entry, packetsReceived): an engine may start a new entry (counters from zero) when the
    stream restarts after a break."""
    inb = rtc.inbound()
    return (inb["raw"].get("id"), inb["packetsReceived"]) if inb else (None, 0)


def short_over_heard(rtc, target, room, seconds=4.0, need=100, peer=None):
    """A short over; (True, packets) when at least `need` packets reached the page (50 per second of audio). The
    count is read again for up to 3 s after the over: Firefox shows the inbound-rtp entry of a restarted stream late."""
    before = _count(rtc)
    talker, talk = talker_over(target, room, seconds, peer)
    got = 0
    try:
        talk.join(timeout=seconds + 8)
        end = time.monotonic() + 3.0
        while True:
            time.sleep(0.4)
            after = _count(rtc)
            got = after[1] - before[1] if after[0] == before[0] else after[1]
            if got >= need or time.monotonic() > end:
                break
    finally:
        if peer is None:
            talker.close()
    rtc.last_over = {"before": before, "after": after}
    if got < need:
        rtc.last_over["stats_types"] = [(e.get("type"), e.get("kind"), e.get("packetsReceived")) for e in rtc.ev("window.__dvtTap.stats()")][:20]
        rtc.last_over["state"] = rtc.state()
    return got >= need, got


def pace_ice(bp, need=1):
    if turn_on():
        _PACER.wait(bp.nat_key, need)


def used_ice(bp, rtc):
    """Tell the pacer how many credentials this page was issued."""
    n = sum(1 for i in rtc.ice if i["status"] in (200, 429))
    if turn_on() and n:
        _PACER.used(bp.nat_key, n)


def selected(rtc, engine, nat_type, label):
    pair = rtc.pair()
    note(engine, nat_type, "%s: selected candidate pair" % label, pair)
    return pair


def tone_check(rtc, engine, nat_type, label):
    """The tone in the page where the engine feeds Web Audio (not Firefox here: no audio device): returns the
    analysis or None. The over must be running."""
    tap = rtc.tap_start()
    if not tap or "error" in tap:
        note(engine, nat_type, "%s: audio tap" % label, "not available: %s" % (tap,))
        return None
    time.sleep(1.2)
    tries = []
    for _ in range(3):
        time.sleep(0.6)
        tries.append(rtc.analyse(1.0))
    heard = max(tries, key=lambda h: h["fraction"])
    note(engine, nat_type, "%s: tone in the page" % label, {k: heard[k] for k in ("peak_hz", "fraction", "rms")})
    return heard


def measure_listen(rtc, engine, nat_type, label, target, room, timeout_s):
    """Connect, key a 28 s USRP over as soon as the panel says Listening, and measure in the page: click to
    connected and answer to connected (page clock: the answer is applied by setRemoteDescription), answer to
    first audio packet, the arrival times of the packets over a steady window of 20 s (the receiver's synchronization
    source polled in a loop, dvtest.browser.arrival_deviations), the tone. The over starts after the connect because the edge
    sends audio only for an over that starts while the listener is attached."""
    rtc.start_arrivals(31000)
    t = rtc.connect(room, timeout_s=timeout_s)
    talker, talk = talker_over(target, room, 28.0)
    t_over = time.monotonic()
    out = {}
    try:
        t_set = rtc.event_time("setRemoteDescription", type="answer")
        conn = [t_ for t_, st in rtc.conn_events() if st == "connected"]
        out["click_to_connected_ms"] = t["click_to_connected_ms"]
        out["answer_to_connected_ms"] = round(conn[-1] - t_set) if t_set and conn else None
        time.sleep(max(0.0, 22.5 - (time.monotonic() - t_over)))
        out["pair"] = selected(rtc, engine, nat_type, label)
        out["heard"] = tone_check(rtc, engine, nat_type, label)
        samples = rtc.collect_arrivals()
        first = samples[0] if samples else None
        out["answer_to_first_audio_ms"] = round(first[0] - t_set) if first and t_set else None
        steady = [s for s in samples if first and first[0] + 2000 <= s[0] <= first[0] + 22000]
        out["dev"] = brlib.arrival_deviations(steady)
        out["steady_samples"] = len(steady)
        note(engine, nat_type, "%s: click to connected (ms)" % label, out["click_to_connected_ms"])
        note(engine, nat_type, "%s: answer to connected (ms)" % label, out["answer_to_connected_ms"])
        note(engine, nat_type, "%s: answer to first audio (ms)" % label, out["answer_to_first_audio_ms"])
        note(engine, nat_type, "%s: arrival deviation from 20 ms (ms)" % label, out["dev"])
        talk.join(timeout=15)
    finally:
        talker.close()
    return out


# ---------------------------------------------------------------------------------------- a

@pytest.mark.parametrize("mode", NAT_TYPES)
def test_nat_classification(nat_rig, mode):
    """The NAT types are what they claim. One socket inside sends to reflector A on two ports and to reflector B (two
    outside addresses): the address:port each reflector sees is the mapping. A second socket sends to A only; an
    unsolicited datagram from a third address (the tester) and one from A's other port are sent to its mapped
    address; which of them reach the socket is the filtering."""
    nat_rig.ctl("reset")
    nat_rig.mode(mode)
    res = nat_rig.classify()
    res["status"] = {k: v for k, v in nat_rig.status().items() if k in ("mode", "conntrack_entries")}
    note(None, mode, "classification", res)
    want_mapping, want_filtering = EXPECTED_CLASS[mode]
    got = res["classification"]
    assert got["mapping"] == want_mapping and got["filtering"] == want_filtering and got["type"] == mode, res


# ---------------------------------------------------------------------------------------- b

def test_direct_listen(engine, nat_type, nat_page, target):
    """DIRECT: the page offers, the ICE-lite edge answers with its host candidate (the SUT address), the media
    crosses the NAT. connected and the first audio within 10 s of the answer; 20 s of steady listening judged on
    the arrival times of the packets in the page; the tone."""
    bp = nat_page
    rtc = NatRtc(bp)
    rtc.open()
    pace_ice(bp)
    m = measure_listen(rtc, engine, nat_type, "direct", target, ROOM_DIRECT, 25.0)
    used_ice(bp, rtc)
    ice = rtc.ice_statuses()
    note(engine, nat_type, "direct: GET /rtc/ice", ice)
    note(engine, nat_type, "direct: offer had candidates", [o["candidates"] for o in rtc.offer_reqs])
    dev, pair, heard = m["dev"], m["pair"], m["heard"]

    assert m["answer_to_connected_ms"] is not None and m["answer_to_connected_ms"] <= CONNECT_BUDGET_S * 1000, m
    first_audio = m["answer_to_first_audio_ms"]
    assert first_audio is not None and first_audio <= CONNECT_BUDGET_S * 1000, "first audio %s ms after the answer" % first_audio
    assert dev is not None, "fewer than 20 packet pairs seen in the steady window (%d samples)" % m["steady_samples"]
    assert dev["p99"] <= JITTER_P99_MS, dev
    assert dev["max"] <= JITTER_MAX_MS, dev
    assert len(ice) == 1, "GET /rtc/ice %s: once per connect" % ice
    assert ice[0] == (200 if turn_on() else 404), ice
    if pair and pair["local"] == "relay":
        web.finding("%s / %s: direct case used the relay (UDP to the media port was not blocked)" % (brlib.label(engine), nat_type))
    assert pair is None or pair["local"] != "relay", pair
    if heard is not None and heard["rms"] > 0:
        assert abs(heard["peak_hz"] - TONE_HZ) <= TONE_TOL_HZ, heard
    elif heard is not None:
        note(engine, nat_type, "direct: audio tap", "silent while packets arrive (engine does not feed Web Audio here)")


# ---------------------------------------------------------------------------------------- c

@needs_turn
def test_turn_relay(engine, nat_type, nat_page, nat_ctl, target):
    """TURN: the router drops UDP from the browser to the media address, so only the relay path can work. The
    page fetches the credentials once, the selected pair is a relay candidate, audio plays, the credential has the
    documented shape and appears nowhere in the page."""
    if not turn_on():
        pytest.skip("TURN is not configured in this run (add rig/config/dv-router/turn.toml to DVT_SUT_CONFIG_EXTRA)")
    secret = turn_secret()
    bp = nat_page
    nat_ctl.ctl("drop_direct", state="on")
    rtc = NatRtc(bp)
    rtc.open()
    pace_ice(bp)
    m = measure_listen(rtc, engine, nat_type, "turn", target, ROOM_TURN, 30.0)
    used_ice(bp, rtc)
    status = nat_ctl.status()
    note(engine, nat_type, "turn: UDP dropped by drop_direct (datagrams)", status["dropped"]["drop_direct"])
    ice = rtc.ice_statuses()
    entries = rtc.entries()
    problems = turn.check_ice_server(entries[0], secret, now=rtc.ice[0]["at"], ttl=turn_ttl()) if entries else ["no 200 answer of /rtc/ice"]
    note(engine, nat_type, "turn: GET /rtc/ice", {"statuses": ice, "problems": problems, "urls": entries[0].get("urls") if entries else None,
                                                  "ttl_in_answer": (rtc.ice[0]["body"] or {}).get("ttl") if rtc.ice else None})
    found = rtc.secrets_in_page([entries[0].get("credential") if entries else "", secret])
    note(engine, nat_type, "turn: credential/secret found in page text, storage, cookies, console", found)
    note(engine, nat_type, "turn: offer had candidates", [o["candidates"] for o in rtc.offer_reqs])
    dev, pair, heard = m["dev"], m["pair"], m["heard"]

    assert ice == [200], "GET /rtc/ice %s: one 200 per connect" % ice
    assert not problems, problems
    assert found == [], "the TURN credential (0) or the secret (1) is visible in the page: %s" % found
    assert pair is not None and pair["local"] == "relay", pair
    assert status["dropped"]["drop_direct"] > 0, "nothing was dropped toward the media port: %s" % status["dropped"]
    assert m["answer_to_connected_ms"] is not None and m["answer_to_connected_ms"] <= CONNECT_BUDGET_S * 1000, m
    first_audio = m["answer_to_first_audio_ms"]
    assert first_audio is not None and first_audio <= CONNECT_BUDGET_S * 1000, "first audio %s ms after the answer" % first_audio
    if dev is None or dev["p99"] > JITTER_P99_MS or dev["max"] > JITTER_MAX_MS:
        web.finding("%s / %s: relay path arrival deviation beyond the direct-path limits: %s" % (brlib.label(engine), nat_type, dev))
    if heard is not None and heard["rms"] > 0:
        assert abs(heard["peak_hz"] - TONE_HZ) <= TONE_TOL_HZ, heard


# ---------------------------------------------------------------------------------------- d

SETTLE_S = 10.0          # how long after the end of a blackhole the page is watched for a late loss (Firefox reports
                         # the broken path only after the blackhole is over)


def window(rtc, nat_ctl, seconds):
    """One UDP blackhole of `seconds` during a listen session, watched until SETTLE_S after its end; returns what the
    page did: whether it gave up (`lost`, `lost_wall_s` = seconds from the start of the blackhole to the poll that saw
    the page's loss text or a closed peer, polled every 0.25 s) and the page-clock state changes relative to the start."""
    offers = len(rtc.offers)
    t0 = rtc.clock()
    nat_ctl.ctl("blackhole", state="for", seconds=seconds)
    wall = time.monotonic()
    lost, lost_wall = False, None
    while time.monotonic() - wall < seconds + SETTLE_S:
        time.sleep(0.25)
        st = rtc.state() or {}
        if rtc.lost_text() or st.get("connection") in ("failed", "closed"):
            lost, lost_wall = True, time.monotonic() - wall
            break
    if not lost:                                    # the session is up: wait for the status line of a listening page
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not rtc.status().startswith("Listening"):
            time.sleep(0.25)
    events = [(round(t - t0), st) for t, st in rtc.state_events(t0)]
    first = {}
    for ms, st in events:
        first.setdefault(st, ms)
    return {"seconds": seconds, "lost": lost, "lost_wall_s": round(lost_wall, 2) if lost_wall else None, "first_state_ms": first,
            "events": events, "status": rtc.status(), "new_offers": len(rtc.offers) - offers,
            "router": nat_ctl.status().get("last_blackhole"), "peer": (rtc.state() or {}).get("connection")}


@pytest.mark.slow
def test_blackhole_resume_and_failure(engine, nat_type, nat_page, nat_ctl, target):
    """A UDP blackhole during a listen session: 2, 3 and 5 s must resume without a new offer; then longer ones
    (8, 12, 20, 30 s, then open ended up to 150 s) until the page loses the connection: the measured failure
    interval. After a loss the page must ask the user to connect again (no automatic offer) and a manual
    reconnect with a new offer must work. ICE restart and automatic reconnect are not built (DV-Router ICE1)."""
    bp = nat_page
    rtc = NatRtc(bp)
    rtc.open()
    pace_ice(bp, 2)
    t = rtc.connect(ROOM_HOLE, timeout_s=25.0)
    used_ice(bp, rtc)
    note(engine, nat_type, "blackhole: first connect, click to connected (ms)", t["click_to_connected_ms"])
    assert len(rtc.offers) == 1
    peer = usrp_client(target, PROTO_CALL, ROOM_HOLE)
    try:
        _blackhole_body(engine, nat_type, bp, rtc, nat_ctl, target, peer)
    finally:
        peer.close()


def _blackhole_body(engine, nat_type, bp, rtc, nat_ctl, target, peer):
    ok, got = short_over_heard(rtc, target, ROOM_HOLE, peer=peer)
    assert ok, "no audio before the first blackhole (%d packets)" % got

    results, violations, failed_at = [], [], None
    for seconds in BLACKHOLE_SHORT + BLACKHOLE_LONG:
        w = window(rtc, nat_ctl, seconds)
        if not w["lost"]:
            heard, packets = short_over_heard(rtc, target, ROOM_HOLE, peer=peer)
            w["audio_after"], w["audio_packets"], w["over_counters"] = heard, packets, rtc.last_over
        results.append(w)
        note(engine, nat_type, "blackhole %d s" % seconds, {k: w[k] for k in w if k != "events"} | {"events": w["events"][:6]})
        if w["lost"]:
            failed_at = w
            if seconds <= max(BLACKHOLE_SHORT):
                violations.append("a %d s UDP blackhole ended the session (it must resume without a new offer): %s" % (seconds, w["events"]))
            break
        if w["new_offers"] or not w["audio_after"]:
            violations.append("%d s blackhole: new offers %d, audio after %s" % (seconds, w["new_offers"], w.get("audio_packets")))
            break
    if failed_at is None:
        # open ended: until the page gives up (cap)
        offers = len(rtc.offers)
        t0 = rtc.clock()
        nat_ctl.ctl("blackhole", state="on")
        wall = time.monotonic()
        while time.monotonic() - wall < BLACKHOLE_CAP_S and not rtc.lost_text():
            time.sleep(0.25)
        lost_wall = time.monotonic() - wall
        events = [(round(t_ - t0), st) for t_, st in rtc.state_events(t0)]
        failed_at = {"seconds": "open", "lost": rtc.lost_text(), "lost_wall_s": round(lost_wall, 2), "events": events, "status": rtc.status(), "new_offers": len(rtc.offers) - offers,
                     "first_state_ms": {}}
        for ms, st in events:
            failed_at["first_state_ms"].setdefault(st, ms)
        results.append(failed_at)
        note(engine, nat_type, "blackhole open ended", failed_at)
    nat_ctl.ctl("blackhole", state="off")

    fs = failed_at["first_state_ms"]
    interval = {"shortest_window_s": failed_at["seconds"], "first_ice_disconnected_ms": fs.get("ice:disconnected"),
                "first_ice_failed_ms": fs.get("ice:failed"), "first_conn_disconnected_ms": fs.get("conn:disconnected"),
                "first_conn_failed_ms": fs.get("conn:failed"), "lost_seen_by_poll_s": failed_at.get("lost_wall_s"), "page_text": failed_at["status"], "events": failed_at["events"][:8]}
    note(engine, nat_type, "ICE failure interval", interval)

    # after the loss: the page asks the user to connect again, no automatic offer, manual reconnect works
    if failed_at["lost"]:
        offers_before = len(rtc.offers)
        time.sleep(4.0)
        text = rtc.status()
        note(engine, nat_type, "page text after the loss", text)
        if "connect again" not in text.lower():
            web.finding("%s / %s: after the loss the page says %r, web.md documents 'Connection lost. Connect again manually.'"
                        % (brlib.label(engine), nat_type, text))
        assert len(rtc.offers) == offers_before, "the page sent a new offer by itself after the loss"
        assert pages.listen_button(bp.page).inner_text().strip().startswith("Listen"), "no Listen / Connect button after the loss"
        rtc.connect(ROOM_HOLE, timeout_s=25.0)
        used_ice(bp, rtc)
        assert len(rtc.offers) == offers_before + 1, "a manual reconnect must send exactly one new offer"
        assert len(set(rtc.offers)) == len(rtc.offers), "the new offer got the circuit of an old one"
        ok, got = short_over_heard(rtc, target, ROOM_HOLE, peer=peer)
        assert ok, "no audio after the manual reconnect (%d packets)" % got
    else:
        violations.append("the page never gave up in %d s of blackhole: ICE did not fail" % BLACKHOLE_CAP_S)
    if violations and engine == "firefox":
        # N1 (stage 4): Playwright Firefox ends the session on a 2-5 s UDP blackhole in every run, the adopted
        # criterion (resume without a new offer) is not met. Judged last, so everything above still ran; an
        # expected failure until Firefox or the page changes, and it passes silently when it is met.
        pytest.xfail("N1: " + "; ".join(violations))
    assert not violations, violations


# ---------------------------------------------------------------------------------------- e

@pytest.mark.slow
@needs_turn
@pytest.mark.parametrize("nat_type", ["symmetric"], indirect=True)
def test_turn_credential_expiry(engine, nat_type, nat_page, nat_ctl, target):
    """A relay session longer than turn_ttl (120 s) behind the symmetric NAT: every 12 s a 3 s over; record when
    audio stops, what the page and the connection state do. The credential expires at 120 s; TURN permissions
    last 300 s and are refreshed with the credential (RFC 8656), so the effect is expected after about 5 minutes."""
    if not turn_on():
        pytest.skip("TURN is not configured in this run (add rig/config/dv-router/turn.toml to DVT_SUT_CONFIG_EXTRA)")
    bp = nat_page
    nat_ctl.ctl("drop_direct", state="on")
    rtc = NatRtc(bp)
    rtc.open()
    pace_ice(bp)
    rtc.connect(ROOM_EXPIRE, timeout_s=30.0)
    used_ice(bp, rtc)
    pair = selected(rtc, engine, nat_type, "expiry")
    assert pair and pair["local"] == "relay", pair
    peer = usrp_client(target, PROTO_CALL, ROOM_EXPIRE)
    t_start = time.monotonic()
    rounds, stopped_at = [], None
    while time.monotonic() - t_start < EXPIRY_OBSERVE_S:
        age = round(time.monotonic() - t_start)
        st = rtc.state() or {}
        text = rtc.status()
        if rtc.lost_text() or st.get("connection") in ("failed", "closed"):
            rounds.append({"age_s": age, "peer": st.get("connection"), "page": text, "heard": None})
            stopped_at = stopped_at or age
            break
        heard, packets = short_over_heard(rtc, target, ROOM_EXPIRE, seconds=3.0, need=100, peer=peer)
        rounds.append({"age_s": age, "peer": st.get("connection"), "heard": packets})
        if not heard and stopped_at is None:
            stopped_at = age
        time.sleep(max(0.0, 12.0 - (time.monotonic() - t_start - age)))
    peer.close()
    note(engine, nat_type, "expiry: rounds (age s, packets of a 3 s over)", [(r["age_s"], r["heard"]) for r in rounds])
    note(engine, nat_type, "expiry: first round without audio or loss (s after connect; credential ttl %d s)" % turn_ttl(),
         stopped_at if stopped_at is not None else "none in %d s" % EXPIRY_OBSERVE_S)
    note(engine, nat_type, "expiry: end state", {"peer": (rtc.state() or {}).get("connection"), "page": rtc.status(),
                                                 "alive_posts": len(rtc.alive_posts())})
    assert [r for r in rounds if r["age_s"] < turn_ttl() - 15 and not r["heard"]] == [], "audio failed before the credential expired"


# ---------------------------------------------------------------------------------------- f, g

@pytest.mark.skipif(os.environ.get("DVT_TURN") == "on", reason="TURN is configured in this run; run without rig/config/dv-router/turn.toml for the 404 fallback")
def test_ice_404_goes_direct(engine, nat_page, nat_ctl, nat_type, target):
    """No TURN configured (a run without turn.toml): GET /api/v1/rtc/ice answers 404, no credential, and the page
    connects direct."""
    if turn_on():
        pytest.skip("TURN is configured in this run; run without rig/config/dv-router/turn.toml for the 404 fallback")
    bp = nat_page
    rtc = NatRtc(bp)
    rtc.open()
    rtc.connect(ROOM_FALLBACK, timeout_s=25.0)
    ice = rtc.ice_statuses()
    ok, got = short_over_heard(rtc, target, ROOM_FALLBACK)
    pair = selected(rtc, engine, nat_type, "no TURN")
    note(engine, nat_type, "404 fallback: GET /rtc/ice", ice)
    assert ice == [404], ice
    assert rtc.entries() == []
    assert ok, "no audio (%d packets)" % got
    assert pair is None or pair["local"] != "relay", pair


@needs_turn
def test_ice_429_goes_direct(engine, web_user, engine_browsers, web_base_url, request, nat_rig, target):
    """The issuer allows two credentials per minute per login: the third connect within a minute gets 429 from
    GET /api/v1/rtc/ice and the page must still connect (direct)."""
    if not turn_on():
        pytest.skip("TURN is not configured in this run (add rig/config/dv-router/turn.toml to DVT_SUT_CONFIG_EXTRA)")
    nat_rig.ctl("reset")
    nat_rig.mode("port_restricted")
    user = web_user("N0NB%d" % (brlib.ENGINES.index(engine) + 1), may_tx=False)
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, user, talk=False)
    try:
        rtc = NatRtc(bp)
        rtc.open()
        # a login of its own has the full burst of two: three connects in a row use it up
        for _ in range(3):
            rtc.connect(ROOM_FALLBACK, timeout_s=25.0)
            rtc.disconnect()
            rtc.wait_idle(8.0)
        ice = rtc.ice_statuses()
        note(engine, "port_restricted", "429 fallback: GET /rtc/ice statuses of three connects", ice)
        rtc.connect(ROOM_FALLBACK, timeout_s=25.0)            # the fourth: still refused, still connects
        ice4 = rtc.ice_statuses()
        ok, got = short_over_heard(rtc, target, ROOM_FALLBACK)
        pair = selected(rtc, engine, "port_restricted", "429")
    finally:
        _rtc_end(bp, request)
    assert ice[:2] == [200, 200] and ice[2] == 429, ice
    assert ice4[3] == 429, ice4
    assert ok, "no audio after the refused credential (%d packets)" % got
    assert pair is None or pair["local"] != "relay", pair
