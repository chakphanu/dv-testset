# SPDX-License-Identifier: MIT
"""Web unit, stage 3b: the recorder (K14b) of DV-Router seen through its web routes and its page. Needs
`scripts/run.sh --profile web,browser` with `rig/config/dv-router/recorder.toml` in DVT_SUT_CONFIG_EXTRA
(run.sh then makes the host directory out/<run>/recorder and the tester gets DVT_RECORDER=on); every case
skips with the reason otherwise. docs/web-browser-cases.md, section "Stage 3b".

Seeding: real overs, sent by USRP dynamic clients (PCM, profile pcm-s16le-8k: no vocoder needed) into two
rooms. Overs of AMBE and other vocoder profiles need the decoder worker of K14c (fake AMBEd, later): not here.

  a  test_recording_notice_is_published           public state info.recording_enabled / recording_keep_days, the dashboard callout
  b  test_list_visibility                         anonymous 401; user and operator see the same rows; the 11 fields; filters; paging
  c  test_detail_and_unknown_id                   GET /recordings/{id}; absent id 404
  d  test_audio_is_a_wav_with_the_tone            200, headers, 44 byte WAV header, the tone of the over, HEAD
  e  test_audio_range_requests                    206 / Content-Range / Content-Length, middle, end, suffix, 416, If-Range, HEAD
  f  test_audio_guards                            Sec-Fetch-Site, Origin, anonymous, another User-Agent
  g  test_audio_rate_limit                        20 WAV requests per 10 s per login (record)
  h  test_recordings_page_*                       the page lists the rows, plays one on each engine, audit row; anonymous text
  i  test_browser_talk_is_recorded_as_webrtc      a Hold Talk over of the browser: one row, protocol webrtc, reason terminator

Which over is which (DV-Router docs/design/recorder.md "PCM talk metadata", DOC6): the rows with protocol `usrp`
and reason `terminator` are the USRP overs this file seeds (OVERS, rooms S and T); a direct browser talk through
the WebRTC edge is recorded as protocol `webrtc`, and `terminator` is the explicit end of either (a PTT release
too). Both use profile pcm-s16le-8k.
"""
import json
import os
import re
import struct
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

import web_pages as pages  # noqa: E402
from dvtest import browser as brlib  # noqa: E402
from dvtest import web  # noqa: E402
from webrtc_support import (  # noqa: E402
    MIC_UNAVAILABLE, Rtc, Talk, on_air_or_end, pcm_floats, press, release, tone_frames, usrp_client, wait_js, wait_talk_text)

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc]

ROOM_A, ROOM_B = "S", "T"
ROOM_WEB = "U"                  # the browser talk of test_browser_talk_is_recorded_as_webrtc
WEB_TALKER = "N0BRU"            # br_user, the may_tx account of rtc_tx_page
FIELDS = {"id", "room", "start_ms", "end_ms", "duration_ms", "decoded_bytes", "callsign", "protocol", "profile", "reason", "lost_units"}
SAME = {"Sec-Fetch-Site": "same-origin", "Accept": "*/*"}
AUD = json.dumps(pages.RECORDING_AUDIO_CSS)       # the shared <audio> of the recordings page, as a JS string literal
AUDIO = "/api/v1/user/recordings/%s/audio.wav"
OVERS = [  # (room, callsign, seconds, tone Hz)
    (ROOM_A, "N0TEST", 2.0, 700.0),
    (ROOM_A, "N0USR", 1.0, 1000.0),
    (ROOM_B, "N0TEST", 0.8, 700.0),
]


@pytest.fixture(scope="session")
def recorder_on():
    if os.environ.get("DVT_RECORDER") != "on":
        pytest.skip("no recorder: add rig/config/dv-router/recorder.toml to DVT_SUT_CONFIG_EXTRA (scripts/run.sh then sets "
                    "DVT_RECORDER=on for the tester)")


@pytest.fixture(scope="session")
def rec_user(web_user, recorder_on):
    """The user whose API session the tester uses for the recordings routes (the browser has its own)."""
    return web_user("N0BRX", may_tx=False)


def day_window():
    """[from, to) in ms around now: the whole UTC day."""
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(start.timestamp() * 1000) + 86400000


_LAST_BROWSE = [0.0]


def browse_pace():
    """The recordings browse routes allow 2 requests/s (burst 10) per login (web.md): keep 0.55 s apart."""
    wait = _LAST_BROWSE[0] + 0.55 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _LAST_BROWSE[0] = time.monotonic()


def listing(client, **query):
    parts = ["%s=%s" % (k, v) for k, v in query.items()]
    browse_pace()
    return client.get("/api/v1/user/recordings" + ("?" + "&".join(parts) if parts else ""))


def all_rows(client, **query):
    rows, cursor = [], None
    for _ in range(30):
        q = dict(query)
        q.setdefault("limit", 100)
        if cursor:
            q["cursor"] = cursor
        resp = listing(client, **q)
        assert resp.status == 200, (resp.status, resp.text()[:200])
        body = resp.json()
        rows += body["items"]
        cursor = body.get("next")
        if not cursor:
            return rows
    raise AssertionError("more than 30 pages")


@pytest.fixture(scope="session")
def seeded(recorder_on, target, rec_user):
    """Send the OVERS as real overs; wait until the recorder lists them. Returns {(room, callsign): row}."""
    got = {}
    for room, call, seconds, hz in OVERS:
        peer = usrp_client(target, call, room)
        try:
            talk = Talk(peer, tone_frames(seconds, freq=hz))
            talk.start()
            talk.join(timeout=seconds + 10)
            assert talk.error is None, talk.error
        finally:
            peer.close()
        time.sleep(1.2)
    lo, hi = day_window()
    deadline = time.monotonic() + 45.0
    rows = []
    while time.monotonic() < deadline:
        rows = all_rows(rec_user.client, date_from_ms=lo, date_to_ms=hi)
        mine = [r for r in rows if r["room"] in (ROOM_A, ROOM_B)]
        if len(mine) >= len(OVERS):
            break
        time.sleep(1.0)
    mine = [r for r in rows if r["room"] in (ROOM_A, ROOM_B)]
    if len(mine) < len(OVERS):
        web.finding("recorder: %d overs sent into rooms %s/%s, %d rows listed after 45 s" % (len(OVERS), ROOM_A, ROOM_B, len(mine)))
        pytest.fail("the recorder listed %d of %d overs after 45 s (rows: %s)" % (len(mine), len(OVERS), [(r["room"], r["callsign"]) for r in rows]))
    for r in mine:
        got.setdefault((r["room"], r["callsign"]), r)
    return got


def row_for(seeded, room, call):
    for (rm, cs), row in seeded.items():
        if rm == room and (cs or "").split("/")[0].upper() == call:
            return row
    raise AssertionError("no row for %s %s in %s" % (room, call, sorted(seeded)))


def audio(client, rec_id, rng=None, method="GET", extra=None, pace=True):
    """One audio request, paced under the 20 WAV requests per 10 s of the service."""
    headers = dict(SAME)
    if rng:
        headers["Range"] = rng
    headers.update(extra or {})
    if pace:
        time.sleep(0.6)
    return client.request(method, AUDIO % rec_id, headers=headers)


# ------------------------------------------------------------------------------------ a

def test_recording_notice_is_published(recorder_on, web_client):
    info = web_client().get("/api/v1/public/state", expect=200).json().get("info") or {}
    assert info.get("recording_enabled") is True, info
    assert info.get("recording_keep_days") == 7, info


# ------------------------------------------------------------------------------------ b

def test_list_visibility(seeded, rec_user, web_operator, web_client):
    anon = web_client()
    assert listing(anon).status == 401
    lo, hi = day_window()
    user_rows = all_rows(rec_user.client, date_from_ms=lo, date_to_ms=hi)
    op_rows = all_rows(web_operator, date_from_ms=lo, date_to_ms=hi)
    assert [r["id"] for r in user_rows] == [r["id"] for r in op_rows], "an operator sees other rows than a user"
    mine = [r for r in user_rows if r["room"] in (ROOM_A, ROOM_B)]
    assert len(mine) >= len(OVERS)
    protocols = sorted({r["protocol"] for r in mine})
    reasons = sorted({r["reason"] for r in mine})
    web.finding("recorder: the seeded USRP overs (rooms %s/%s) are listed with protocol %s, end reason %s"
                % (ROOM_A, ROOM_B, protocols, reasons))
    # recorder.md "PCM talk metadata": a USRP-origin over records protocol usrp (PCM alone does not select it)
    assert protocols == ["usrp"], "USRP overs listed with protocol %s" % protocols
    for r in mine:
        assert set(r) == FIELDS, sorted(set(r) ^ FIELDS)
        assert re.fullmatch(r"[0-9a-f]{96}", r["id"])
        assert r["profile"] == "pcm-s16le-8k"
        assert r["duration_ms"] == r["decoded_bytes"] // 16 and r["decoded_bytes"] % 320 == 0
        assert r["end_ms"] >= r["start_ms"] and r["lost_units"] == 0
    # the three overs by room and callsign, durations of the right size (20 ms frames)
    for room, call, seconds, _hz in OVERS:
        row = row_for(seeded, room, call)
        assert abs(row["duration_ms"] - seconds * 1000) <= 200, (room, call, row["duration_ms"])
    # filters
    only_a = all_rows(rec_user.client, room=ROOM_A, date_from_ms=lo, date_to_ms=hi)
    assert only_a and {r["room"] for r in only_a} == {ROOM_A}
    by_call = all_rows(rec_user.client, callsign="n0usr", date_from_ms=lo, date_to_ms=hi)
    assert by_call and all((r["callsign"] or "").split("/")[0].upper() == "N0USR" for r in by_call)
    assert all_rows(rec_user.client, profile="ambe-dstar", date_from_ms=lo, date_to_ms=hi) == []
    assert all_rows(rec_user.client, date_from_ms=hi, date_to_ms=hi + 1000) == []
    # paging: one at a time reaches the same ids, oldest first
    paged, cursor = [], None
    for _ in range(40):
        q = {"limit": 1, "room": ROOM_A, "date_from_ms": lo, "date_to_ms": hi}
        if cursor:
            q["cursor"] = cursor
        body = listing(rec_user.client, **q).json()
        paged += [r["id"] for r in body["items"]]
        cursor = body["next"]
        if not cursor:
            break
    assert paged == [r["id"] for r in only_a]
    assert [r["start_ms"] for r in only_a] == sorted(r["start_ms"] for r in only_a)
    for bad in ("limit=0", "limit=101", "room=a", "cursor=zz", "date_from_ms=-1", "profile=nope"):
        st = listing(rec_user.client, **dict([bad.split("=")])).status
        assert st == 400, "%s -> %s" % (bad, st)


# ------------------------------------------------------------------------------------ c

def test_detail_and_unknown_id(seeded, rec_user):
    row = row_for(seeded, ROOM_A, "N0TEST")
    browse_pace()
    got = rec_user.client.get("/api/v1/user/recordings/%s" % row["id"], expect=200).json()
    assert got == row
    browse_pace()
    assert rec_user.client.get("/api/v1/user/recordings/%s" % ("0" * 96)).status == 404
    browse_pace()
    assert rec_user.client.get("/api/v1/user/recordings/not-an-id").status == 400


# ------------------------------------------------------------------------------------ d

def wav_info(data):
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE" and data[12:16] == b"fmt "
    fmt, ch, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", data[20:36])
    assert data[36:40] == b"data"
    size = struct.unpack("<I", data[40:44])[0]
    return {"fmt": fmt, "channels": ch, "rate": rate, "bits": bits, "data": size, "riff": struct.unpack("<I", data[4:8])[0]}


def test_audio_is_a_wav_with_the_tone(seeded, rec_user):
    for room, call, seconds, hz in OVERS:
        row = row_for(seeded, room, call)
        resp = audio(rec_user.client, row["id"])
        assert resp.status == 200, resp.status
        h = resp.headers
        assert h.get("content-type", "").startswith("audio/wav"), h.get("content-type")
        assert h.get("accept-ranges") == "bytes"
        assert "no-store" in h.get("cache-control", "")
        assert h.get("content-disposition", "").startswith("inline")
        assert h.get("cross-origin-resource-policy") == "same-origin"
        data = resp.body
        assert len(data) == 44 + row["decoded_bytes"] == int(h["content-length"])
        info = wav_info(data)
        assert info == {"fmt": 1, "channels": 1, "rate": 8000, "bits": 16, "data": row["decoded_bytes"], "riff": 36 + row["decoded_bytes"]}, info
        samples = pcm_floats([data[44:]])
        peak, _amp, frac, level = brlib.scan_peak(samples, 8000)
        assert abs(peak - hz) <= 20.0 and frac > 0.5 and level > 0.05, (room, call, peak, frac, level)
        head = audio(rec_user.client, row["id"], method="HEAD")
        assert head.status == 200 and int(head.headers["content-length"]) == len(data) and head.body == b""
    web.finding("recorder: audio headers %s" % sorted(k for k in resp.headers if k in ("etag", "last-modified", "vary", "content-length")))


# ------------------------------------------------------------------------------------ e

def test_audio_range_requests(seeded, rec_user):
    row = row_for(seeded, ROOM_A, "N0TEST")
    rid = row["id"]
    full = audio(rec_user.client, rid).body
    total = len(full)
    mid = total // 2
    cases = [   # (Range header, status, first, last)  last inclusive; None = no Content-Range check
        ("bytes=0-1", 206, 0, 1),
        ("bytes=0-", 206, 0, total - 1),
        ("bytes=0-43", 206, 0, 43),
        ("bytes=44-", 206, 44, total - 1),
        ("bytes=%d-%d" % (mid, mid + 639), 206, mid, mid + 639),
        ("bytes=%d-" % (total - 2), 206, total - 2, total - 1),
        ("bytes=-2", 206, total - 2, total - 1),
        ("bytes=-100000000", 206, 0, total - 1),
        ("bytes=%d-%d" % (total - 2, total + 9999), 206, total - 2, total - 1),
    ]
    results = {}
    for rng, status, a, b in cases:
        resp = audio(rec_user.client, rid, rng)
        results[rng] = resp.status
        assert resp.status == status, (rng, resp.status)
        assert resp.headers.get("content-range") == "bytes %d-%d/%d" % (a, b, total), (rng, resp.headers.get("content-range"))
        assert int(resp.headers["content-length"]) == b - a + 1
        assert resp.body == full[a:b + 1], rng
    for rng in ("bytes=%d-" % total, "bytes=%d-%d" % (total + 5, total + 9), "bytes=100-99", "bytes=-0"):
        resp = audio(rec_user.client, rid, rng)
        results[rng] = resp.status
        assert resp.status == 416, (rng, resp.status)
        assert resp.headers.get("content-range") == "bytes */%d" % total, (rng, resp.headers.get("content-range"))
        assert resp.body == b""
    # not satisfiable forms the docs treat as "ignore the header": whole body, 200
    for rng in ("bytes=0-1,44-45", "bytes=example", "items=0-1"):
        resp = audio(rec_user.client, rid, rng)
        results[rng] = resp.status
        if resp.status != 200 or resp.body != full:
            web.finding("recorder: Range %r answered %s (documented: 200 with the whole body)" % (rng, resp.status))
    # conditional requests: documented as ignored (no ETag, no Last-Modified, never 304)
    etag = audio(rec_user.client, rid).headers.get("etag")
    cond = audio(rec_user.client, rid, "bytes=0-1", extra={"If-Range": '"stale"'})
    results["If-Range stale + Range"] = cond.status
    nm = audio(rec_user.client, rid, extra={"If-None-Match": "*"})
    results["If-None-Match *"] = nm.status
    ims = audio(rec_user.client, rid, extra={"If-Modified-Since": "Sun, 06 Nov 2094 08:49:37 GMT"})
    results["If-Modified-Since future"] = ims.status
    head = audio(rec_user.client, rid, "bytes=0-1", method="HEAD")
    results["HEAD + Range bytes=0-1"] = head.status
    web.finding("recorder: ETag header %r; Range matrix %s; HEAD with Range gives %s with content-length %s"
                % (etag, json.dumps(results, sort_keys=True), head.status, head.headers.get("content-length")))
    assert cond.status == 206 and cond.body == full[:2], "If-Range is documented as ignored: a Range is still honoured"
    assert nm.status == 200 and ims.status == 200
    assert head.status == 200 and int(head.headers["content-length"]) == total


# ------------------------------------------------------------------------------------ f, g

def test_audio_guards(seeded, rec_user, web_client):
    rid = row_for(seeded, ROOM_A, "N0TEST")["id"]
    anon = web_client()
    assert anon.request("GET", AUDIO % rid, headers=SAME).status == 401
    got = {}
    got["Sec-Fetch-Site none"] = audio(rec_user.client, rid, extra={"Sec-Fetch-Site": "none"}).status
    got["Sec-Fetch-Site cross-site"] = audio(rec_user.client, rid, extra={"Sec-Fetch-Site": "cross-site"}).status
    got["foreign Origin"] = audio(rec_user.client, rid, extra={"Origin": "https://evil.example"}).status
    got["AppleCoreMedia UA"] = audio(rec_user.client, rid, "bytes=0-1", extra={"User-Agent": "AppleCoreMedia/1.0.0 (Macintosh)"}).status
    got["other UA, audio"] = audio(rec_user.client, rid, extra={"User-Agent": "someone-else/1.0"}).status
    browse_pace()
    got["other UA, list"] = rec_user.client.request("GET", "/api/v1/user/recordings?limit=1", headers={"User-Agent": "someone-else/1.0"}).status
    got["same session afterwards"] = listing(rec_user.client, limit=1).status
    web.finding("recorder: audio guards %s" % json.dumps(got, sort_keys=True))
    assert got["Sec-Fetch-Site none"] == 403 and got["Sec-Fetch-Site cross-site"] == 403 and got["foreign Origin"] == 403
    assert got["same session afterwards"] == 200, "a different User-Agent ended the session"


@pytest.mark.slow
def test_audio_rate_limit(seeded, rec_user, web_operator):
    """20 WAV requests per 10 s per login (web.md): record where the 429 starts. A throwaway session of the
    same account, so the other cases keep their budget."""
    rid = row_for(seeded, ROOM_B, "N0TEST")["id"]
    time.sleep(10.5)
    session = web_operator.user_session(rec_user.callsign, rec_user.password)
    codes, retry = [], None
    t0 = time.monotonic()
    for _ in range(30):
        resp = audio(session, rid, "bytes=0-0", pace=False)
        codes.append(resp.status)
        if resp.status == 429 and retry is None:
            retry = resp.headers.get("retry-after")
    first = codes.index(429) + 1 if 429 in codes else None
    web.finding("recorder: audio rate limit: first 429 at request %s of 30 in %.1f s, Retry-After %s (documented 20 per 10 s)"
                % (first, time.monotonic() - t0, retry))
    session.logout()
    assert first is not None and 18 <= first <= 24, codes


# ------------------------------------------------------------------------------------ h page

def recordings_page(bp, path_query=""):
    bp.goto(pages.RECORDINGS + path_query)
    return bp.page


@pytest.fixture
def rec_page(user_page, recorder_on):
    """user_page (N0BRU, signed in); recordings.js as served (DASH1 removed the fragments, wip-13e7e70)."""
    return user_page


def test_recordings_page_anonymous_text(engine, browser_page, recorder_on):
    """The page as served renders signed out (stage 3b: it stayed "Loading recordings..." until DASH1) and says
    what to do: the 401 text of recordings.js errorText."""
    bp = browser_page
    bp.goto(pages.RECORDINGS)
    expect_text = "Sign in to browse recordings."
    wait_js(bp.page, "() => document.querySelector('main') && document.querySelector('main').innerText.includes(%s)"
            % json.dumps(expect_text), 15.0)
    body = bp.page.inner_text("main")
    note_text = body[:200].replace("\n", " | ")
    web.finding("%s: /recordings/ signed out shows %r" % (brlib.label(engine), note_text))
    assert pages.recording_rows(bp.page).count() == 0
    assert "Loading recordings..." not in body
    assert not [e for e in bp.log.errors if "createElement" in e or "InvalidCharacter" in e], bp.log.errors[:1]


def test_dashboard_shows_the_recording_notice(engine, user_page, recorder_on):
    bp = user_page
    bp.goto("/#/")
    notice = pages.recording_callout(bp.page).filter(has_text=pages.RECORDING_NOTICE)
    notice.first.wait_for(state="visible", timeout=30000)
    assert "Recordings are kept for 7 days" in notice.first.inner_text()
    link = pages.recordings_link(bp.page)
    assert link.count() == 1
    web.finding("%s: dashboard recordings link href %r" % (brlib.label(engine), link.first.get_attribute("href")))


def test_recordings_page_lists_and_plays(engine, rec_page, seeded, web_operator):
    bp = rec_page
    page = bp.page
    seen = []

    def on_resp(resp):
        if "/audio.wav" in resp.url:
            req = resp.request
            seen.append({"status": resp.status, "range": req.headers.get("range"), "content_range": resp.headers.get("content-range")})

    page.on("response", on_resp)
    recordings_page(bp)
    pages.recordings_heading(page).wait_for(state="visible", timeout=30000)
    pages.recording_notice(page).first.wait_for(state="visible", timeout=20000)
    assert pages.RECORDING_NOTICE in pages.recording_notice(page).first.inner_text()
    pages.recording_room_filter(page).select_option(ROOM_A)
    pages.recording_apply(page).click()
    wait_js(page, "() => document.querySelectorAll(%s).length >= 2" % json.dumps(pages.RECORDING_ROWS_CSS))
    api_rows = [r for r in seeded.values() if r["room"] == ROOM_A]
    rows = pages.recording_rows(page)
    assert rows.count() >= len(api_rows)
    # the row of the 2 s over by N0TEST: module, callsign, protocol, reason, lost units as the API says
    row = [r for r in seeded.values() if r["room"] == ROOM_A and (r["callsign"] or "").upper().startswith("N0TEST")][0]
    cell = pages.recording_row(page, row["id"])
    cell.wait_for(state="visible", timeout=10000)
    text = cell.inner_text()
    for want in (row["room"], row["callsign"], row["protocol"], row["reason"], str(row["lost_units"])):
        assert str(want) in text, (want, text)
    web.finding("%s: recordings page row text %r" % (brlib.label(engine), text.replace("\t", " | ")))
    # play: the shared audio element
    pages.recording_play_button(page, row["id"]).click()
    player = pages.recording_player(page)
    try:
        wait_js(page, "() => { const a = document.querySelector(%s); return a && a.currentTime > 0.3 && (!a.error || a.error.message.includes('MediaSink')); }" % AUD, 15.0)
    except AssertionError:
        diag = page.evaluate("""() => { const a = document.querySelector(%s);
            return a ? {t: a.currentTime, duration: a.duration, paused: a.paused, ready: a.readyState, network: a.networkState,
                        error: a.error ? [a.error.code, a.error.message] : null, src: a.currentSrc.slice(-40)} : null; }""" % AUD)
        alert = pages.recording_alert(page).all_inner_texts()
        web.finding("%s: playback did not start in 15 s: audio %s, page alert %s, audio requests %s, console %s"
                    % (brlib.label(engine), json.dumps(diag), alert, json.dumps(seen), [c["text"][:100] for c in bp.log.console[-4:]]))
        raise
    state = page.evaluate("""() => { const a = document.querySelector(%s);
        return {t: a.currentTime, duration: a.duration, paused: a.paused, ended: a.ended, ready: a.readyState,
                network: a.networkState, error: a.error ? a.error.code : null, src: a.currentSrc.replace(location.origin, '')}; }""" % AUD)
    status = pages.recording_player_status(page).inner_text()
    assert "Module %s" % ROOM_A in status and row["callsign"] in status, status
    sink = page.evaluate("""() => { const a = document.querySelector(%s);
        return a && a.error ? a.error.message : ''; }""" % AUD)
    if "MediaSink" in sink:
        # no sound card in the container: Firefox raises OnMediaSinkAudioError after its clock ran; the data itself loaded
        web.finding("%s: playback START works (currentTime advanced to %.2f of %.2f s, requests %s) but the engine reports %s: no audio "
                    "device in the browser container (media.cubeb.force_null_context did not help); end-of-clip and seek not judged"
                    % (brlib.label(engine), state["t"], state["duration"] or 0, json.dumps(seen), sink))
        assert abs(state["duration"] - row["duration_ms"] / 1000.0) < 0.3, state
        assert seen and all(x["status"] in (200, 206) for x in seen), seen
        return
    assert state["error"] is None
    # to the end of the 2 s clip
    wait_js(page, "() => { const a = document.querySelector(%s); return a.ended || a.currentTime > 1.8; }" % AUD)
    end = page.evaluate("""() => { const a = document.querySelector(%s);
        return {t: a.currentTime, duration: a.duration, ended: a.ended, error: a.error ? a.error.code : null}; }""" % AUD)
    web.finding("%s: playback of a 2 s recording: started %s, end %s, requests %s" % (brlib.label(engine), json.dumps(state), json.dumps(end), json.dumps(seen)))
    assert end["error"] is None and abs(end["duration"] - row["duration_ms"] / 1000.0) < 0.3, end
    assert pages.recording_alert(page).count() == 0
    # status 0 = a response Playwright could not read (Playwright WebKit shows one for the media request)
    assert seen and all(x["status"] in (0, 200, 206) for x in seen) and any(x["status"] in (200, 206) for x in seen), seen
    assert player.get_attribute("controlslist") == "nodownload"
    # seek into the middle: a range request, playback goes on
    page.evaluate("""() => { const a = document.querySelector(%s); a.currentTime = 0.5; a.play(); }""" % AUD)
    wait_js(page, "() => { const a = document.querySelector(%s); return !a.error && a.currentTime > 0.6; }" % AUD)
    web.finding("%s: after seeking to 0.5 s the page's audio requests are %s" % (brlib.label(engine), json.dumps(seen[-4:])))
    # the playback audit row (web.md: recording_play before the first byte)
    audit = web_operator.get("/api/v1/operator/audit?limit=200", expect=200).json()
    items = audit.get("items", audit) if isinstance(audit, dict) else audit
    plays = [i for i in items if "recording" in json.dumps(i).lower()]
    web.finding("%s: audit rows mentioning recording after playback: %d" % (brlib.label(engine), len(plays)))
    problems = bp.problems({("/api/v1/rtc/ice", 404)})
    web.finding("%s: recordings page problems: %s" % (brlib.label(engine), problems[:5] or "none"))


# ------------------------------------------------------------------------------------ i browser talk

WEB_HOLD_S = 2.5


@pytest.mark.slow
def test_browser_talk_is_recorded_as_webrtc(engine, rtc_tx_page, recorder_on, rec_user):
    """A Hold Talk over of the browser (N0BRU on module U, released by the button) is one recorder row with
    protocol `webrtc` and reason `terminator`, profile pcm-s16le-8k (recorder.md "PCM talk metadata": direct
    browser talk through the WebRTC edge records protocol webrtc; a PTT release is an explicit end). The
    callsign and the duration of the time on air are checked; lost units are recorded."""
    bp = rtc_tx_page
    rtc = Rtc(bp)
    rtc.open()
    rtc.connect(ROOM_WEB)
    t_press_ms = int(time.time() * 1000)
    press(rtc)
    text, _took = wait_talk_text(rtc, on_air_or_end, 30.0)
    if text.startswith(MIC_UNAVAILABLE):
        release(rtc)
        pytest.skip("%s: no fake microphone (talk status %r)" % (brlib.label(engine), text))
    assert text == pages.TALK_ON_AIR_TEXT, "talk status %r after pressing" % text
    t_air = time.monotonic()
    time.sleep(WEB_HOLD_S)
    on_air_s = release(rtc) - t_air
    ended, _took = wait_talk_text(rtc, lambda s: s != pages.TALK_ON_AIR_TEXT, 8.0)
    assert ended == pages.TALK_RELEASED_TEXT, ended
    lo, hi = day_window()
    deadline = time.monotonic() + 30.0
    mine = []
    while time.monotonic() < deadline and not mine:
        rows = all_rows(rec_user.client, room=ROOM_WEB, date_from_ms=lo, date_to_ms=hi)
        mine = [r for r in rows if (r["callsign"] or "").split("/")[0].upper() == WEB_TALKER and r["start_ms"] >= t_press_ms - 2000]
        if not mine:
            time.sleep(1.0)
    web.finding("%s: recorder row of a browser Hold Talk over (%.1f s on air): %s" % (
        brlib.label(engine), on_air_s, json.dumps([{k: r[k] for k in ("protocol", "reason", "profile", "duration_ms", "lost_units", "callsign")}
                                                   for r in mine])))
    assert len(mine) == 1, "browser over: %d rows for %s in room %s since the press" % (len(mine), WEB_TALKER, ROOM_WEB)
    row = mine[0]
    assert row["protocol"] == "webrtc", "a browser talk is recorded as protocol %r, recorder.md says webrtc" % row["protocol"]
    assert row["reason"] == "terminator", "a released browser over ends with reason %r, recorder.md says terminator" % row["reason"]
    assert row["profile"] == "pcm-s16le-8k", row["profile"]
    assert set(row) == FIELDS, sorted(set(row) ^ FIELDS)
    assert row["duration_ms"] == row["decoded_bytes"] // 16
    assert abs(row["duration_ms"] - on_air_s * 1000) <= 800, (row["duration_ms"], round(on_air_s, 2))
