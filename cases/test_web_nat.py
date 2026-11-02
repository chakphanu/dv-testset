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
  d  test_blackhole_resume_and_failure  ICE1a: 2, 3, 5 s UDP blackholes resume without a new offer; from 'disconnected'
                                        the page says "Connection interrupted. Waiting to resume." (12, 16, 20 s) and waits
                                        12 s; 12 and 16 s are recorded (resumed, or lost: from which side), 20 s must end
                                        with "Connection lost. Connect again manually."; a manual connect = one new offer (slow)
  d2 test_talk_is_released_at_disconnected_and_needs_a_fresh_press  Talk held during a blackhole: released at 'disconnected',
                                        a held button does not resume after recovery, a fresh press goes on air (slow)
  e  test_turn_credential_expiry        relay session behind the symmetric NAT longer than turn_ttl (slow)
  f  test_ice_404_goes_direct           no TURN configured: GET /rtc/ice answers 404 and the page goes direct
  g  test_ice_429_goes_direct           third connect within a minute: 429 from the issuer, the page goes direct

Calibrated against dv-router:wip-13e7e70 and rerun on wip-3b0724a (ICE1a, K22, K22b): the dashboard renders as served (DASH1, no workaround), the Listen card
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
from dvtest import nat_plan  # noqa: E402
from dvtest import k22, nat, turn, web  # noqa: E402
from nat_support import (  # noqa: E402
    CONNECT_BUDGET_S, JITTER_MAX_MS, JITTER_P99_MS, NAT_TYPES, IcePacer, NatRtc, rig_client, turn_on, turn_secret, turn_ttl)
from webrtc_support import (  # noqa: E402
    MIC_UNAVAILABLE, TONE_HZ, TONE_TOL_HZ, Talk, on_air_or_end, press, pump_while, release, tone_frames, usrp_client, voice_frames,
    wait_talk_text)

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc, pytest.mark.nat]

ROOM_DIRECT, ROOM_TURN, ROOM_HOLE, ROOM_EXPIRE, ROOM_FALLBACK = "HJKLQ"
ROOM_TALK = "W"
PROTO_CALL = "N0TEST"
EXPECTED_CLASS = {"full_cone": (nat.EI, nat.EI), "port_restricted": (nat.EI, nat.APD), "symmetric": (nat.APD, nat.APD)}
BLACKHOLE_SHORT = (2, 3, 5)          # must resume without a new offer
BLACKHOLE_GRACE = (12,)              # the page's 12 s grace (ICE1a): recorded, the interrupted text is asserted
BLACKHOLE_EDGE_ZONE = (16,)          # between the edge's 15 s STUN timeout and the end of the page's grace: recorded
BLACKHOLE_LONG = (20,)               # clearly longer than the grace: must end with "Connection lost. Connect again manually."
# DVT_NAT_SHORT=1: the short profile for a final-tag check, at most 20 minutes with TURN on (docs/web-nat-cases.md "Short
# profile"): the same cases, fewer combinations (SHORT_PLAN below), the mapping-expiry observation 120 s unless
# DVT_NAT_EXPIRY_S says otherwise. Unset: the whole matrix, about an hour.
SHORT = os.environ.get("DVT_NAT_SHORT") == "1"
EXPIRY_OBSERVE_S = int(os.environ.get("DVT_NAT_EXPIRY_S") or ("120" if SHORT else "660"))
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

def blackhole_breaks(engine, nat_type):
    return nat_plan.breaks(engine, nat_type, BLACKHOLE_SHORT + BLACKHOLE_GRACE + BLACKHOLE_EDGE_ZONE + BLACKHOLE_LONG, SHORT)


def short_wanted(test, engine, nat_type):
    return nat_plan.wanted(test, engine, nat_type, SHORT)


@pytest.fixture(autouse=True)
def nat_short_profile(request):
    """DVT_NAT_SHORT=1: skip the combinations the short profile leaves out before any browser or account is touched."""
    if not SHORT:
        return
    params = request.node.callspec.params if hasattr(request.node, "callspec") else {}
    engine, nat_type = params.get("engine"), params.get("nat_type")
    if not short_wanted(request.node.originalname, engine, nat_type):
        pytest.skip("DVT_NAT_SHORT=1: not part of the short profile")


@pytest.fixture(scope="session")
def nat_users(web_operator, web_user):
    """Every listener account of this module, made at the start: the operator session that registers them does not
    last for the hour this module can take. Skipped (nothing made) when the NAT rig is not up."""
    if os.environ.get("DVT_NAT") != "on":
        pytest.skip("nat profile not up")
    for i in range(1, len(brlib.ENGINES) * len(NAT_TYPES) + 1):
        if SHORT and i not in _short_user_indexes():
            continue
        web_user("N0NA%d" % i, may_tx=False)
    for i in range(1, len(brlib.ENGINES) + 1):
        web_user("N0NB%d" % i, may_tx=False)
    for i in range(1, len(brlib.ENGINES) + 1):
        if SHORT:
            break                                       # the Talk case is not part of the short profile
        web_user("N0NT%d" % i, may_tx=True)


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


def _short_user_indexes():
    """Accounts N0NA<i> the short profile uses (each registration costs pacer time at the start of the run)."""
    return {user_index(e, t) for e in brlib.ENGINES for t in NAT_TYPES
            if any(short_wanted(name, e, t) for name in ("test_turn_relay", "test_blackhole_resume_and_failure",
                                                         "test_turn_credential_expiry"))}


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

SETTLE_S = 10.0          # how long after the end of a blackhole the page must stay connected before it counts as resumed
                         # (Firefox reports the broken path only after the blackhole is over)


def window(rtc, nat_ctl, seconds):
    """One UDP blackhole of `seconds` during a listen session. ICE1a: the page shows "Connection interrupted. Waiting to
    resume." at the first 'disconnected' and gives up 12 s later ("Connection lost. Connect again manually.") unless
    connectivity returns. Polled every 0.25 s from the start of the break until the page has been connected and
    "Listening" for SETTLE_S after the break (resumed), or it gave up (`lost`, `lost_wall_s` = seconds from the start of
    the blackhole to the poll that saw the loss). Returns what the page did, with `interrupted` (the interrupted status
    text or data-state 'disconnected' was seen), `interrupted_first_s`, `lost_during_break` and the page-clock state
    changes relative to the start."""
    offers = len(rtc.offers)
    t0 = rtc.clock()
    nat_ctl.ctl("blackhole", state="for", seconds=seconds)
    wall = time.monotonic()
    lost, lost_wall = False, None
    interrupted_first = None
    stable_since = None
    texts = []
    while True:
        elapsed = time.monotonic() - wall
        if elapsed > seconds + SETTLE_S + k22.GRACE_S + 6.0:
            break
        time.sleep(0.25)
        elapsed = time.monotonic() - wall
        status = rtc.status()
        st = rtc.state() or {}
        if not texts or texts[-1] != status:
            texts.append(status)
        if rtc.lost_text() or st.get("connection") in ("failed", "closed"):
            lost, lost_wall = True, elapsed
            break
        if status == pages.LISTEN_INTERRUPTED_TEXT or rtc.panel_state() == "disconnected":
            interrupted_first = elapsed if interrupted_first is None else interrupted_first
            stable_since = None
        elif elapsed >= seconds and st.get("connection") == "connected" and status.startswith("Listening"):
            stable_since = stable_since if stable_since is not None else time.monotonic()
            if time.monotonic() - stable_since >= SETTLE_S:
                break
        else:
            stable_since = None
    events = [(round(t - t0), st) for t, st in rtc.state_events(t0)]
    first = {}
    for ms, st in events:
        first.setdefault(st, ms)
    w = {"seconds": seconds, "lost": lost, "lost_wall_s": round(lost_wall, 2) if lost_wall else None, "first_state_ms": first,
         "events": events, "status": rtc.status(), "statuses": texts[:8], "new_offers": len(rtc.offers) - offers,
         "interrupted": interrupted_first is not None or "ice:disconnected" in first or "conn:disconnected" in first,
         "interrupted_text_seen": pages.LISTEN_INTERRUPTED_TEXT in texts,
         "interrupted_first_s": round(interrupted_first, 2) if interrupted_first is not None else None,
         "lost_during_break": bool(lost and lost_wall < seconds),
         "router": nat_ctl.status().get("last_blackhole"), "peer": (rtc.state() or {}).get("connection")}
    if lost:
        w["judge"] = k22.judge_loss(first, w["lost_wall_s"], seconds)
        remaining = seconds - (time.monotonic() - wall)
        if remaining > 0:
            time.sleep(remaining + 0.5)                 # let the router end the break before the manual connect
    return w


@pytest.mark.slow
def test_blackhole_resume_and_failure(engine, nat_type, nat_page, nat_ctl, target):
    """A UDP blackhole during a listen session (ICE1a, web.md "Listen"): 2, 3 and 5 s must resume without a new offer;
    from the first ICE / peer 'disconnected' the page shows "Connection interrupted. Waiting to resume." (asserted for
    the 12 s and longer breaks) and waits 12 s; a break clearly longer than that (20 s) must end with "Connection lost.
    Connect again manually."; the 12 s and 16 s breaks are recorded (resumed, or lost: from which side: the page's grace
    expiry or the engine's ICE failure after the edge removed the peer, which the edge does 15 s after the last STUN
    request). After every loss the page sends no offer by itself and a manual connect sends exactly one new offer with
    a new circuit and audio flows."""
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


def _manual_reconnect(engine, nat_type, bp, rtc, target, peer, seconds):
    """After the page gave up: no offer by itself, the documented text, a Listen / Connect button, then one manual
    connect = exactly one new offer with a new circuit, and audio. Returns the violations."""
    bad = []
    offers_before = len(rtc.offers)
    time.sleep(4.0)
    text = rtc.status()
    note(engine, nat_type, "page text after the %s s loss" % seconds, text)
    if text != pages.LISTEN_LOST_TEXT:
        web.finding("%s / %s: after the loss the page says %r, web.md documents %r"
                    % (brlib.label(engine), nat_type, text, pages.LISTEN_LOST_TEXT))
    assert len(rtc.offers) == offers_before, "the page sent a new offer by itself after the loss"
    assert pages.listen_button(bp.page).inner_text().strip().startswith("Listen"), "no Listen / Connect button after the loss"
    pace_ice(bp, 1)
    rtc.connect(ROOM_HOLE, timeout_s=25.0)
    used_ice(bp, rtc)
    if len(rtc.offers) != offers_before + 1:
        bad.append("a manual reconnect must send exactly one new offer (%d sent)" % (len(rtc.offers) - offers_before))
    if len(set(rtc.offers)) != len(rtc.offers):
        bad.append("the new offer got the circuit of an old one")
    ok, got = short_over_heard(rtc, target, ROOM_HOLE, peer=peer)
    if not ok:
        bad.append("no audio after the manual reconnect (%d packets)" % got)
    return bad


def _blackhole_body(engine, nat_type, bp, rtc, nat_ctl, target, peer):
    ok, got = short_over_heard(rtc, target, ROOM_HOLE, peer=peer)
    assert ok, "no audio before the first blackhole (%d packets)" % got

    short_bad, other_bad, results = [], [], []
    for seconds in blackhole_breaks(engine, nat_type):
        w = window(rtc, nat_ctl, seconds)
        results.append(w)
        if not w["lost"]:
            heard, packets = short_over_heard(rtc, target, ROOM_HOLE, peer=peer)
            w["audio_after"], w["audio_packets"], w["over_counters"] = heard, packets, rtc.last_over
        note(engine, nat_type, "blackhole %d s" % seconds, {k: w[k] for k in w if k != "events"} | {"events": w["events"][:8]})
        bad = short_bad if seconds <= max(BLACKHOLE_SHORT) else other_bad
        if seconds <= max(BLACKHOLE_SHORT):
            if w["lost"]:
                bad.append("a %d s UDP blackhole ended the session (it must resume without a new offer): %s" % (seconds, w["events"]))
            elif w["new_offers"] or not w["audio_after"]:
                bad.append("%d s blackhole: new offers %d, audio after %s" % (seconds, w["new_offers"], w.get("audio_packets")))
        else:
            if seconds >= min(BLACKHOLE_GRACE):
                if not w["interrupted"]:
                    bad.append("%d s blackhole: the page never showed %r (no ICE / peer 'disconnected' seen)"
                               % (seconds, pages.LISTEN_INTERRUPTED_TEXT))
                elif not w["interrupted_text_seen"]:
                    bad.append("%d s blackhole: 'disconnected' fired (%s) but the status line never read %r (statuses %s)"
                               % (seconds, w["first_state_ms"], pages.LISTEN_INTERRUPTED_TEXT, w["statuses"]))
            if seconds >= min(BLACKHOLE_LONG) and not w["lost"]:
                bad.append("a %d s blackhole did not end the session (the 12 s grace should have expired)" % seconds)
            if not w["lost"] and (w["new_offers"] or not w.get("audio_after")):
                bad.append("%d s blackhole resumed with new offers %d / audio after %s" % (seconds, w["new_offers"], w.get("audio_packets")))
            if w["lost"]:
                note(engine, nat_type, "blackhole %d s: how it ended" % seconds, {
                    "judge": w["judge"], "lost_wall_s": w["lost_wall_s"], "status": w["status"],
                    "first_ice_disconnected_ms": w["first_state_ms"].get("ice:disconnected"),
                    "first_ice_failed_ms": w["first_state_ms"].get("ice:failed")})
        if w["lost"]:
            fixes = _manual_reconnect(engine, nat_type, bp, rtc, target, peer, seconds)
            (short_bad if seconds <= max(BLACKHOLE_SHORT) else other_bad).extend(fixes)
    nat_ctl.ctl("blackhole", state="off")
    note(engine, nat_type, "blackhole summary", [
        {"s": w["seconds"], "outcome": "lost" if w["lost"] else "resumed", "interrupted": w["interrupted"],
         "lost_during_break": w["lost_during_break"], "side": (w.get("judge") or {}).get("side"),
         "lost_wall_s": w["lost_wall_s"], "new_offers": w["new_offers"]} for w in results])
    assert not short_bad and not other_bad, short_bad + other_bad


def _talk_user(engine):
    return "N0NT%d" % (brlib.ENGINES.index(engine) + 1)


@pytest.fixture
def nat_talk_page(nat_ctl, engine, engine_browsers, web_base_url, request, web_user):
    """A page signed in as the may_tx account of the engine, with the microphone permission (Chromium, WebKit) or the
    fake-device prefs (Firefox)."""
    user = web_user(_talk_user(engine), may_tx=True)
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, user, talk=True)
    bp.nat_key = user.callsign
    yield bp
    _rtc_end(bp, request)


@pytest.mark.slow
@pytest.mark.parametrize("nat_type", ["port_restricted"], indirect=True)
def test_talk_is_released_at_disconnected_and_needs_a_fresh_press(engine, nat_type, nat_talk_page, nat_ctl, target):
    """ICE1a (web.md "Listen"): while the Talk button is held, a UDP blackhole makes the browser report 'disconnected';
    the page releases Talk at once (the status leaves "On air", the microphone is muted; in a blackhole the 4 s PTT
    watchdog has already ended the over at about 3.4 s, before any engine reports 'disconnected', so the timeline is
    recorded), keeps listening, and when
    connectivity returns inside the grace window the held button does NOT resume the transmission: the protocol listener
    hears nothing new while the button stays down, and a fresh press goes on air again. One NAT type (port restricted)
    per engine."""
    bp = nat_talk_page
    rtc = NatRtc(bp)
    rtc.open()
    assert pages.talk_button(bp.page).inner_text().strip() == "Hold Talk"
    pace_ice(bp, 1)
    rtc.connect(ROOM_TALK, timeout_s=25.0)
    used_ice(bp, rtc)
    listener = usrp_client(target, PROTO_CALL, ROOM_TALK)
    try:
        press(rtc)
        text, took = wait_talk_text(rtc, on_air_or_end, 30.0)
        if text.startswith(MIC_UNAVAILABLE):
            release(rtc)
            note(engine, nat_type, "talk during blackhole: fake microphone", "not usable (%r)" % text)
            pytest.skip("%s: no fake microphone (talk status %r)" % (brlib.label(engine), text))
        assert text == pages.TALK_ON_AIR_TEXT, "talk status %r after pressing (panel %r)" % (text, rtc.status())
        pump_while(listener, 1.5)
        before = len(voice_frames(listener))
        assert before > 0, "no voice frames at the protocol listener while on air"

        nat_ctl.ctl("blackhole", state="on")
        wall = time.monotonic()
        seen = None
        timeline = []                                   # (seconds, listen status, talk status) at every change
        while time.monotonic() - wall < 40.0:
            listener.pump(0.05)
            status, talk_now = rtc.status(), rtc.talk_status()
            if not timeline or timeline[-1][1:] != (status, talk_now):
                timeline.append((round(time.monotonic() - wall, 2), status, talk_now))
            if status == pages.LISTEN_INTERRUPTED_TEXT or rtc.panel_state() == "disconnected":
                seen = {"after_s": round(time.monotonic() - wall, 2), "listen": status, "talk": talk_now}
                break
            if rtc.lost_text():
                break
        if seen is None:
            nat_ctl.ctl("blackhole", state="off")
            note(engine, nat_type, "talk during blackhole: the page ended before 'disconnected'", timeline[:8])
            if "release timed out" in rtc.status().lower() or "release timed out" in rtc.talk_status().lower():
                # web.md (PTT): no renewal ack in 4 s ends the over, and no terminal reply to the release within 4 s closes
                # the peer; that acknowledgement deadline is suspended only from the browser's 'disconnected' on. An engine
                # that reports 'disconnected' later than about 8 s after the start of the break (Firefox: 4.6 to 10.9 s) is
                # ended by it first: the documented path, so the ICE1a release at 'disconnected' was not reached here.
                web.finding("%s / %s: with Talk held the page ended with %r before the engine reported 'disconnected' "
                            "(timeline %s): the 4 s release acknowledgement deadline of web.md ran out first"
                            % (brlib.label(engine), nat_type, rtc.status(), timeline[:6]))
                pytest.skip("%s: the engine reported 'disconnected' later than the 4 s PTT release deadline (documented 'Talk "
                            "release timed out'); the release-at-'disconnected' rule was not reached" % brlib.label(engine))
        assert seen is not None, "the page never showed 'disconnected' in 40 s of blackhole (status %r)" % rtc.status()
        seen["timeline"] = timeline[:8]
        note(engine, nat_type, "talk during blackhole: at the first 'disconnected'", seen)
        assert seen["talk"] != pages.TALK_ON_AIR_TEXT, "Talk was not released at 'disconnected': %r" % seen
        frames_at_release = len(voice_frames(listener))
        pump_while(listener, 2.0)                      # the button is still down, the path still broken
        nat_ctl.ctl("blackhole", state="off")
        t_back = time.monotonic()
        end = t_back + 30.0
        while time.monotonic() < end and not (rtc.status().startswith("Listening") and (rtc.state() or {}).get("connection") == "connected"):
            listener.pump(0.05)
            if rtc.lost_text():
                break
        resumed = rtc.status().startswith("Listening")
        note(engine, nat_type, "talk during blackhole: after the path returned", {
            "resumed": resumed, "seconds": round(time.monotonic() - t_back, 2), "listen": rtc.status(), "talk": rtc.talk_status()})
        assert resumed, "the session did not resume 2 s after 'disconnected' (status %r)" % rtc.status()
        assert rtc.talk_status() != pages.TALK_ON_AIR_TEXT, "the held button went back on air after recovery"
        frames_resumed = len(voice_frames(listener))
        pump_while(listener, 3.0)
        after_hold = len(voice_frames(listener))
        late = after_hold - frames_resumed
        talk_after = rtc.talk_status()
        note(engine, nat_type, "talk during blackhole: held button after recovery", {
            "voice_frames_during_interruption": frames_resumed - frames_at_release, "voice_frames_in_3s_after_recovery": late,
            "talk": talk_after})
        assert talk_after != pages.TALK_ON_AIR_TEXT, "the held button resumed the transmission (status %r)" % talk_after
        assert late <= 5, "%d voice frames reached the protocol listener in 3 s after recovery while the button was only held" % late
        release(rtc)
        time.sleep(0.5)
        # a fresh press goes on air again
        listener.inbox.clear()
        press(rtc)
        text2, took2 = wait_talk_text(rtc, on_air_or_end, 30.0)
        note(engine, nat_type, "talk during blackhole: fresh press", {"status": text2, "seconds": round(took2, 2) if took2 else None})
        assert text2 == pages.TALK_ON_AIR_TEXT, "a fresh press after recovery did not go on air: %r" % text2
        pump_while(listener, 2.0)
        fresh = len(voice_frames(listener))
        release(rtc)
        ended, _t = wait_talk_text(rtc, lambda s: s != pages.TALK_ON_AIR_TEXT, 8.0)
        note(engine, nat_type, "talk during blackhole: fresh press heard / after release", {"frames": fresh, "status": ended})
        assert fresh > 50, "a fresh press after recovery: %d voice frames in 2 s" % fresh
    finally:
        try:
            release(rtc)
        except Exception:                               # noqa: BLE001
            pass
        nat_ctl.ctl("blackhole", state="off")
        listener.close()


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
