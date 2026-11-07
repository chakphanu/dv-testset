# SPDX-License-Identifier: MIT
"""DV-Router data-channel cases of data.md 15.5 for stream metadata (DP15 groups 1, 2, 4 and the wire half of
7): what a talker's own slow data, META, DMRG / DMRA and GPS become on the web, and how an SMS or a web text
reaches the listeners of a module with a D-STAR QSO running. Specification: docs/dp15-plan.md; case table,
wording, bounds, status and run command: docs/dp15-cases.md.

  DP15-1a  D-STAR TX message -> the talker's `status` on the web within the ceiling
  DP15-1b  D-PRS GPS-A beacon -> one `position` (operator feed); a line with a foreign source is rejected (spoof)
  DP15-1c  a text with one block missing never gives a status or a partial one
  DP15-1d  D3: the native D-STAR to D-STAR slow data is byte identical (worker alive; killed: needs the kill action)
  DP15-2a  M17 SMS during a D-STAR QSO: M17 clients at once, D-STAR clients after a pause of 2 s
  DP15-2b  the same with a web sender (the render path) and the M17 voice bytes against a control run
  DP15-2c  META text of an M17 talker -> D-STAR text blocks in the cross-mode egress; a D-STAR over is untouched
  DP15-2d  M17 packet mode APRS payload (specifier 0x02) -> a position
  DP15-4a  YSF GPS fix -> one position       DP15-4b  DMRG -> one position (and `unbound` outside a stream)
  DP15-4c  DMRA -> a status, never the displayed callsign
  DP15-7a  the synthetic text over as an XLX peer receives it (wire shape and timing)

Only DV-Router answers these (DVT_SUT_PROFILE=dvrouter, DVT_SUT_LAUNCH=native) with the data worker on and the
web profile (scripts/run.sh --profile web): DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml:
rig/config/dv-router/dp15-paths.toml, 7a also rig/config/dv-router/xlx-peer-tester.toml. Every case skips with the
reason when its prerequisite is missing. Observations go to stdout and out/<run>/dp15-streams.txt ("RECORD" =
what was seen, "FINDING" = differs from the documented behaviour). Bounds: cases/data_support.py (the answer of
the DV-Router lead). The controller limits per base callsign (status 1 per 10 s, position 1 per 30 s) are
respected by data_support.PACER, so the cases wait for each other instead of rate limiting each other.

NOT RUN ON A RIG yet (written against the sources only).
"""
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import clients, control, dstar_sd, slowdata, web  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import dextra, dmr, m17, xlx, ysf, ysf_gps  # noqa: E402

import data_support as ds  # noqa: E402
from data_support import (  # noqa: E402
    AT_ONCE_S, IDLE_GAP_S, PACER, STATUS_CEILING_S, SseWatch, ambe, build_over, dstar_listener, hub_session,
    m17_client, m17_voice_payloads, m17p_packets, over_by_my, plain_fields, pump_for, send_over, send_over_thread, sms_packet,
    sms_text, text_headers, texts_of, write_note)

pytestmark = pytest.mark.rig

MODULE = "B"
XLX_ROOM = "D"                  # a module of xlx-peer-tester.toml (XLX996 serves D and E)
LOG = "dp15-streams.txt"
SETTING = "require_ident=%s" % os.environ.get("DVT_REQUIRE_IDENT", "true")
TEXT_OVER_LATE_S = IDLE_GAP_S + 0.6 + 1.5      # voice end + idle gap + slot (600 ms) + pipeline (lead: no number)
OPERATOR_WAIT_S = 15.0
POSITION_E7 = 200               # tolerance of a coordinate, 2e-5 degrees (the DMRG step is 1.1e-5)
LAT, LON = 10.5, 20.25          # 10 deg 30.00 N, 20 deg 15.00 E (every position of the cases)
LAT_E7, LON_E7 = 105000000, 202500000
TALKER_CALL, OTHER_CALL = "N0TEST", "N0CALL"     # D-STAR / M17 / YSF callsigns; DMR ids 1023008 / 1023007 map to them
DMR_ID = {"N0CALL": 1023007, "N0TEST": 1023008}
LATE_SLOP_S = 0.05


# ---------------------------------------------------------------- notes, fixtures

def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics with the data worker on (skips with the reason otherwise)."""
    yield from hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    """The web side: the operator session, a reader (user, may not transmit), a poster (user, may transmit), and a
    factory of anonymous clients."""
    return types.SimpleNamespace(op=web_operator, reader=web_user("N0DPR", may_tx=False).client,
                                 poster=web_user("N0DPT", may_tx=True), anon=web_client)


# ---------------------------------------------------------------- helpers

def sutip(rig):
    return rig.xlxd_sut


def dl(rig, callsign):
    """A DExtra client of that callsign linked to module B (a context manager, empty inbox)."""
    return dstar_listener(sutip(rig), module=MODULE, callsign=callsign)


def ds_display(text):
    return slowdata.display_text(text)


def near(value, want, tol=POSITION_E7):
    return abs(value - want) <= tol


def position_rows(rows, callsign=None):
    return [r for r in rows if r.get("item_type") == "position" and (callsign is None or r.get("callsign", "").split("-")[0] == callsign)]


def status_rows(rows, callsign=None):
    return [r for r in rows if r.get("item_type") == "status" and (callsign is None or r.get("callsign", "").split("-")[0] == callsign)]


def operator_baseline(op):
    """The id of the newest event of the admin feed (0 when there is none), to read only what follows."""
    _items, last = web.operator_events(op, limit=1)
    return int(last or 0)


def wait_operator_rows(op, baseline, pred, timeout=OPERATOR_WAIT_S, poll=1.0, enough=1):
    """(matching rows, all rows after the baseline) of the operator feed once `enough` rows match pred or after timeout."""
    end = time.monotonic() + timeout
    seen = {}
    while True:
        items, _last = web.operator_events(op, after=baseline)
        for item in items:
            seen[item.get("id")] = item
        hits = [r for r in seen.values() if pred(r)]
        if len(hits) >= enough or time.monotonic() >= end:
            return hits, list(seen.values())
        time.sleep(poll)


def check_position(row, source, symbol=None, comment=None, lat_e7=LAT_E7, lon_e7=LON_E7, tol=POSITION_E7):
    body = row["body"]
    assert isinstance(body, dict), "position body is %r" % (body,)
    assert near(body["lat_e7"], lat_e7, tol) and near(body["lon_e7"], lon_e7, tol), \
        "position %s,%s e7, want about %s,%s" % (body["lat_e7"], body["lon_e7"], lat_e7, lon_e7)
    assert body["source"] == source, "position source %r, want %r" % (body["source"], source)
    if symbol is not None:
        assert body["symbol"] == symbol, "symbol %r, want %r" % (body["symbol"], symbol)
    if comment is not None:
        assert body["comment"] == comment, "comment %r, want %r" % (body["comment"], comment)


def record_anonymous_view(target, case, web_ctx, watch, since, rows_user):
    """RECORD (never assert) what the public surfaces and the user history show of positions (owner: APRS data is
    public; the rule is being revised in DP7)."""
    on_sse = [o for _t, o in watch.data_events(since) if o.get("item_type") == "position"]
    try:
        state = web_ctx.anon().get("/api/v1/public/state", pace=True).text()
    except web.WebError as exc:
        state = "(unreadable: %s)" % exc
    in_state = "lat_e7" in state
    in_history = position_rows(rows_user)
    note(target, case, "RECORD", "anonymous public SSE position events %d; public/state mentions lat_e7: %s; user message "
         "history position rows %d (documented: none, web.md 1178)" % (len(on_sse), in_state, len(in_history)))
    if in_history:
        note(target, case, "FINDING", "GET /api/v1/user/rooms/%s/messages carried %d position rows (web.md: never)" % (MODULE, len(in_history)))


def web_post(poster, room, text, pace=True):
    """POST a room text as the poster; (t before, t after, status). pace=False skips the wait for the account bucket
    (1 per 5 s, burst 2: the second of two back-to-back posts)."""
    if pace:
        PACER.wait(poster.callsign, "text")
    t0 = time.monotonic()
    resp = web.post_room_text(poster.client, room, text, expect=None)
    return t0, time.monotonic(), resp.status


# ================================================================ 1a: TX message -> status

def test_dp15_1a_tx_message_becomes_the_status(rig, target, hub, web_ctx):
    """data.md 15.5 (D-STAR): "a radio TX message appears as the talker's status on the web within one superframe plus
    pipeline". A D-STAR talker sends 3 superframes whose slow data carries the 20 character TX message; the public
    SSE gets ONE `status` data event of the talker with the text, no earlier than the end of the first superframe
    and within STATUS_CEILING_S of it; the room history shows the row; the status counter moves."""
    case = "1a"
    text = "DVT TXMSG %04d" % (int(time.time()) % 10000)
    PACER.wait(TALKER_CALL, "status")
    before = hub.settle()
    fields = dstar_sd.tx_message_fields(text, superframes=3)
    sid, packets = build_over(TALKER_CALL, fields, ambe(len(fields), case))
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, dl(rig, TALKER_CALL) as talker:
        assert sse.ready(), "the public SSE gave no hello: %s" % sse.error
        t_header, stamps = send_over(talker, packets)
        t_sf_end = stamps[20]                                    # the 21st voice frame: the first superframe is complete
        hit = sse.wait_data(lambda o: o.get("item_type") == "status" and o.get("callsign", "").startswith(TALKER_CALL),
                            timeout=STATUS_CEILING_S + 5.0, since=t_header)
        pump_for(3.0, talker)                                    # more status events would show up now
        events = [o for _t, o in sse.data_events(t_header) if o.get("item_type") == "status"
                  and o.get("callsign", "").startswith(TALKER_CALL)]
    assert hit is not None, "no status event for %s in %.0f s after the TX message over" % (TALKER_CALL, STATUS_CEILING_S + 5)
    at, obj = hit
    latency = at - t_sf_end
    note(target, case, "RECORD", "status %r arrived %.3f s after the end of the first superframe (%.3f s after the header); "
         "row %s" % (obj.get("body"), latency, at - t_header, {k: obj.get(k) for k in ("callsign", "protocol", "room", "item_type")}))
    if latency < -LATE_SLOP_S:
        note(target, case, "FINDING", "the status arrived %.3f s BEFORE the first superframe was complete" % -latency)
    assert obj.get("body", "").strip() == text, "status body %r, want %r" % (obj.get("body"), text)
    assert obj.get("room") == MODULE, obj
    assert latency <= STATUS_CEILING_S, "status %.3f s after the first superframe, ceiling %.1f s" % (latency, STATUS_CEILING_S)
    assert len(events) == 1, "%d status events for one over (identical bodies are suppressed): %s" % (len(events), events)
    rows = status_rows(web.room_messages(web_ctx.reader, MODULE), TALKER_CALL)
    assert any(r["body"].strip() == text for r in rows), "the room history has no status row %r: %s" % (text, rows[:3])
    after, seen = hub.wait_for(before, lambda now, was: now["pkt.status.accepted"] > was["pkt.status.accepted"])
    change = ds.delta(after, before)
    note(target, case, "RECORD", "counter deltas %s" % ds.moved(change))
    assert seen, "dv_pkt_items_total{kind=status,result=accepted} did not move in %.0f s" % ds.POLL_S
    assert change["room.accepted"] >= 1, ds.moved(change)


# ================================================================ 1b: D-PRS -> position

def test_dp15_1b_dprs_beacon_is_a_position(rig, target, hub, web_ctx):
    """data.md 15.5: "a D-PRS GPS-A beacon appears as the talker's position (users only)". The over carries the TX
    message and a D-PRS line `$$CRC...,N0TEST>API510,DSTAR*:!1030.00N/02015.00E>test` in the free slots. Asserted:
    ONE position row of the talker in the operator feed (source 1, 10.5 N 20.25 E, symbol /> ) within the ceiling of
    the superframe that completes the line. Recorded only: what anonymous clients and the user history show
    (the lead: positions are on the admin/operator scope today, the public rule is being revised in DP7). Then
    a line whose source is another station is rejected as `spoof`: no position, the reject counter moves."""
    case = "1b"
    PACER.wait(TALKER_CALL, "position")
    PACER.wait(TALKER_CALL, "status")
    text = "DVT DPRS %04d" % (int(time.time()) % 10000)
    payload = dstar_sd.aprs_position_payload(TALKER_CALL, LAT, LON, comment="test")
    fields = dstar_sd.text_gps_fields(text, payload, superframes=3)
    blocks = len(dstar_sd.simple_data_blocks(dstar_sd.dprs_line(payload)))
    complete_sf = (blocks - 1) // 6                                  # six free slots per superframe in the text-gps layout
    sid, packets = build_over(TALKER_CALL, fields, ambe(len(fields), case))
    baseline = operator_baseline(web_ctx.op)
    with SseWatch(web_ctx.op, "/api/v1/operator/stream") as op_stream, \
            SseWatch(web_ctx.anon(), "/api/v1/public/events") as anon_sse, \
            dl(rig, TALKER_CALL) as talker:
        assert op_stream.ready() and anon_sse.ready(), "SSE streams gave no hello: %s %s" % (op_stream.error, anon_sse.error)
        t_header, stamps = send_over(talker, packets)
        t_line_done = stamps[min(complete_sf * 21 + 20, len(stamps) - 2)]
        hit = op_stream.wait_data(lambda o: o.get("item_type") == "position" and o.get("callsign", "").startswith(TALKER_CALL),
                                  timeout=STATUS_CEILING_S + 8.0, since=t_header)
        pump_for(2.0, talker)
    hits, rows = wait_operator_rows(web_ctx.op, baseline, lambda r: r.get("item_type") == "position"
                                    and r.get("callsign", "").startswith(TALKER_CALL))
    note(target, case, "RECORD", "operator rows after the over %d (position %d); position row keys %s" % (
        len(rows), len(position_rows(rows)), sorted(hits[0]) if hits else None))
    assert hit is not None and hits, "no position of %s in the operator feed (stream %s, history %d rows)" % (
        TALKER_CALL, hit is not None, len(rows))
    latency = hit[0] - t_line_done
    note(target, case, "RECORD", "position arrived %.3f s after the superframe that completes the line (index %d, %d blocks)" % (
        latency, complete_sf, blocks))
    check_position(hits[0], source=1, symbol="/>")
    assert hits[0].get("room") == MODULE, hits[0]
    assert len(position_rows(rows, TALKER_CALL)) == 1, "%d position rows for one over" % len(position_rows(rows, TALKER_CALL))
    assert latency <= STATUS_CEILING_S, "position %.3f s after the line was complete, ceiling %.1f s" % (latency, STATUS_CEILING_S)
    record_anonymous_view(target, case, web_ctx, anon_sse, t_header, web.room_messages(web_ctx.reader, MODULE))
    # -- spoof: the line's source is not the talker
    PACER.wait(TALKER_CALL, "status")
    hub.settle()                                                  # a quiet baseline for the reject counter
    spoofed = dstar_sd.aprs_position_payload("N0XYZ", LAT, LON, comment="spoof")
    fields = dstar_sd.text_gps_fields("DVT SPOOF %04d" % (int(time.time()) % 10000), spoofed, superframes=3)
    sid, packets = build_over(TALKER_CALL, fields, ambe(len(fields), case + "s"))
    baseline = operator_baseline(web_ctx.op)
    rejects = hub.total("dv_pkt_reject_total", reason="spoof")
    with dl(rig, TALKER_CALL) as talker:
        send_over(talker, packets)
        pump_for(2.0, talker)
    _hits, rows = wait_operator_rows(web_ctx.op, baseline, lambda r: False, timeout=8.0)
    assert not position_rows(rows), "a line with a foreign source became a position: %s" % position_rows(rows)
    value, ok = hub.wait_total("dv_pkt_reject_total", rejects + 1, reason="spoof")
    note(target, case, "RECORD", "spoof: rejects %s -> %s" % (rejects, value))
    assert ok, "dv_pkt_reject_total{reason=spoof} did not move (%s -> %s)" % (rejects, value)


# ================================================================ 1c: a missing block

def test_dp15_1c_a_missing_text_block_gives_no_status(rig, target, hub, web_ctx):
    """data.md 15.5: "loss of a required text block loses the text and never produces a partial status". Every
    superframe of the over lacks block 2 (the filler takes its slot): no status event, no row, no accepted status
    counter. A control over with all four blocks right after proves the path was open."""
    case = "1c"
    text = "DVT LOSS %04d" % (int(time.time()) % 10000)
    before = hub.settle()
    fields = dstar_sd.tx_message_fields(text, superframes=3, drop=(2,))
    sid, packets = build_over(OTHER_CALL, fields, ambe(len(fields), case))
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, dl(rig, OTHER_CALL) as talker:
        assert sse.ready()
        t_header, _stamps = send_over(talker, packets)
        pump_for(STATUS_CEILING_S + 3.0, talker)
        got = [o for _t, o in sse.data_events(t_header) if o.get("item_type") == "status" and o.get("callsign", "").startswith(OTHER_CALL)]
    assert not got, "a status appeared for a text with a missing block: %s" % got
    rows = status_rows(web.room_messages(web_ctx.reader, MODULE), OTHER_CALL)
    assert not any(text[:8] in r["body"] for r in rows), "the history has a (partial) status: %s" % rows
    after = hub.snap()
    change = ds.delta(after, before)
    note(target, case, "RECORD", "counter deltas %s" % ds.moved(change))
    assert change["pkt.status.accepted"] == 0 and change["room.accepted"] == 0, ds.moved(change)
    # control: the same text complete
    PACER.wait(OTHER_CALL, "status")
    fields = dstar_sd.tx_message_fields(text, superframes=3)
    sid, packets = build_over(OTHER_CALL, fields, ambe(len(fields), case + "c"))
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, dl(rig, OTHER_CALL) as talker:
        assert sse.ready()
        t_header, _stamps = send_over(talker, packets)
        hit = sse.wait_data(lambda o: o.get("item_type") == "status" and o.get("callsign", "").startswith(OTHER_CALL),
                            timeout=STATUS_CEILING_S + 5.0, since=t_header)
    assert hit is not None and hit[1].get("body", "").strip() == text, "the control over gave no status: %s" % (hit,)


# ================================================================ 1d: D3, native slow data untouched

@pytest.mark.parametrize("worker", ["alive", "killed"])
def test_dp15_1d_native_dstar_slow_data_is_byte_identical(rig, target, hub, rig_control, worker):
    """data.md 15.5 (D3): "the native D-STAR to D-STAR slow data is byte-identical with and without the worker". A
    talker sends an over with the TX message and a D-PRS line; a listener on the same module gets every AMBE frame
    and every 3 byte slow data field unchanged (the data channel only looks). `killed`: the data worker is killed
    just before the over; needs the control channel to allow `kill-proc xlxd-sut dv-data-worker KILL` (unit U1, not
    in the allow-list yet: skips with that reason)."""
    case = "1d[%s]" % worker
    if worker == "killed" and ("xlxd-sut", "dv-data-worker") not in control.KILL_PROC_ALLOWED:
        pytest.skip("blocked on U1: ('xlxd-sut', 'dv-data-worker') is not in dvtest.control.KILL_PROC_ALLOWED")
    PACER.wait(TALKER_CALL, "status")
    PACER.wait(TALKER_CALL, "position")
    payload = dstar_sd.aprs_position_payload(TALKER_CALL, LAT, LON, comment="test")
    fields = dstar_sd.text_gps_fields("DVT D3 %04d" % (int(time.time()) % 10000), payload, superframes=3)
    frames = ambe(len(fields), case)
    sid, packets = build_over(TALKER_CALL, fields, frames)
    with dl(rig, TALKER_CALL) as talker, dl(rig, OTHER_CALL) as listener:
        if worker == "killed":
            killed = rig_control.kill_proc("xlxd-sut", "dv-data-worker", "KILL")
            note(target, case, "RECORD", "killed the data worker: %s" % (killed,))
            time.sleep(0.2)
        send_over(talker, packets)
        pump_for(1.5, talker, listener)
        overs = slowdata.overs(listener.client.inbox)
    got = over_by_my(overs, TALKER_CALL)
    assert got is not None, "the listener saw no over of %s: %s" % (TALKER_CALL, [ds.describe(o) for o in overs])
    sent = [(dextra.parse_voice_raw(p)["ambe"], dextra.parse_voice_raw(p)["slow"]) for p in packets[1:-1]]
    received = [(v["ambe"], v["slow"]) for v in got["voice"]]
    note(target, case, "RECORD", "sent %d frames, listener got %d" % (len(sent), len(received)))
    assert [a for a, _s in received] == [a for a, _s in sent], "AMBE frames differ"
    different = [i for i, (r, s) in enumerate(zip(received, sent)) if r[1] != s[1]]
    assert len(received) == len(sent) and not different, "slow data differs at frames %s" % different[:6]
    if worker == "killed":
        time.sleep(8.0)                                        # the launcher restarts the worker (0.5 s doubling to 5 s)


# ================================================================ 2: M17 SMS and web text during a D-STAR QSO

def qso_scenario(rig, target, case, send_sms, voice_frames=252, seed="qso", wait_text=True):
    """One module B with a D-STAR talker T (N0TEST), a D-STAR listener D, an M17 sender S (N0CALL) and an M17
    listener L (N0TEST). T keys a long over; 1.5 s after the header `send_sms(S, tl)` submits a text. Returns a
    namespace: tl (Timeline), t_header, t_last (T's last frame), t_sms (when the text was submitted), D, L and
    S inbox copies, the sent and received AMBE frames of T."""
    tl = ds.Timeline()
    frames = ambe(voice_frames, seed)
    fields = plain_fields(voice_frames)
    sid, packets = build_over(TALKER_CALL, fields, frames)
    out = []
    with dl(rig, TALKER_CALL) as talker, \
            dl(rig, OTHER_CALL) as dlist, \
            m17_client(sutip(rig), callsign=OTHER_CALL, module=MODULE) as sender, \
            m17_client(sutip(rig), callsign=TALKER_CALL, module=MODULE) as mlist:
        thread = send_over_thread(talker, packets, out)
        time.sleep(0.3 + 1.5)                                   # the header settle plus 1.5 s of voice
        t_sms = send_sms(sender, tl)
        while thread.is_alive():
            pump_for(0.05, dlist, mlist, sender)
        t_header, stamps = out[0]
        t_last = stamps[-1]
        tl.add("t_last", at=t_last)
        # the text over starts at least idle_gap after the voice end: wait for it
        end = time.monotonic() + (TEXT_OVER_LATE_S + 6.0 if wait_text else 0.0)
        while time.monotonic() < end and not text_headers(dlist):
            pump_for(0.05, dlist, mlist, sender)
        pump_for(1.5, dlist, mlist, sender)                    # the over itself (440 ms) and stragglers
        result = types.SimpleNamespace(
            tl=tl, t_header=t_header, t_last=t_last, t_sms=t_sms, frames=frames, d_overs=slowdata.overs(dlist.client.inbox),
            d_text_headers=text_headers(dlist), l_m17p=m17p_packets(mlist), l_voice=m17_voice_payloads(mlist),
            d_inbox=list(dlist.client.inbox))
    return result


def check_qso(target, case, got, body, expect_text_prefix, ident):
    """Assertions common to 2a and 2b (the D-STAR side): D hears T unchanged, no text header during the QSO, a text
    over on D after the pause."""
    t = got.d_text_headers
    during = [at for at in t if at < got.t_last]
    assert not during, "a text over header reached the D-STAR listener while T was on air (%d)" % len(during)
    over = over_by_my(got.d_overs, TALKER_CALL)
    assert over is not None, "D-STAR listener saw no over of T: %s" % [ds.describe(o) for o in got.d_overs]
    voice = [v["ambe"] for v in over["voice"]]
    assert voice == got.frames[:len(voice)] and len(voice) >= len(got.frames) - 2, \
        "T's voice at D differs: %d frames of %d" % (len(voice), len(got.frames))
    assert t, "no text over reached the D-STAR listener within %.1f s of the voice end" % (TEXT_OVER_LATE_S + 6.0)
    first = min(t)
    gap = first - got.t_last
    note(target, case, "RECORD", "D-STAR text over header %.3f s after T's last frame (idle gap %.1f s, late bound %.1f s)" % (
        gap, IDLE_GAP_S, TEXT_OVER_LATE_S))
    assert gap >= IDLE_GAP_S - LATE_SLOP_S, "the text over started %.3f s after the voice end, before the %.1f s pause" % (gap, IDLE_GAP_S)
    assert gap <= TEXT_OVER_LATE_S, "the text over started %.3f s after the voice end, bound %.1f s" % (gap, TEXT_OVER_LATE_S)
    texts = texts_of(got.d_overs)
    assert any(x["text"].rstrip().endswith(body) and x["text"].startswith(expect_text_prefix) for _s, x in texts), \
        "no text over %r...%r on D: %s" % (expect_text_prefix, body, [x["text"] for _s, x in texts])


def test_dp15_2a_m17_sms_during_a_dstar_qso(rig, target, hub):
    """data.md 15.5 (M17): "an SMS during a D-STAR QSO reaches M17 clients at once and D-STAR clients only in a pause of
    at least 2 s". The M17 sender is a direct client (SRC = link callsign, ident 1): the M17 listener gets the packet
    through the native relay (byte for byte; the data channel renders nothing for an M17 origin), within AT_ONCE_S
    and during T's over. The D-STAR listener gets the text over `N0CALL: <body>` only IDLE_GAP_S after T's last frame
    (and within TEXT_OVER_LATE_S), and T's voice at D is unchanged."""
    case = "2a"
    body = ds.nonce_body("DVT")
    PACER.wait(OTHER_CALL, "text")
    packet = sms_packet(body, OTHER_CALL)
    box = {}

    def send_sms(sender, tl):
        sender.client.send(sender.dest, packet)
        box["t"] = time.monotonic()
        tl.add("sms_sent", at=box["t"])
        return box["t"]

    got = qso_scenario(rig, target, case, send_sms, seed=case)
    match = [(at, p) for at, p in got.l_m17p if sms_text(p) == body]
    assert match, "the M17 listener got no SMS %r: %s" % (body, [(round(at - got.t_sms, 3), sms_text(p)) for at, p in got.l_m17p])
    at, parsed = match[0]
    latency = at - got.t_sms
    note(target, case, "RECORD", "M17 listener got the SMS %.3f s after it was sent, %.3f s before T's last frame; src %s" % (
        latency, got.t_last - at, parsed["src"]))
    assert latency <= AT_ONCE_S, "SMS reached the M17 listener after %.3f s, bound %.1f s" % (latency, AT_ONCE_S)
    assert at < got.t_last, "the SMS reached the M17 listener only after T's over"
    assert parsed["payload"] == packet[34:-2] and parsed["payload_crc_ok"], "the relayed packet differs from the one sent"
    check_qso(target, case, got, body, OTHER_CALL + ": ", ident=1)


def test_dp15_2b_web_text_during_a_dstar_qso(rig, target, hub, web_ctx):
    """data.md 15.5 (M17), the render path: the same with a WEB sender, so the M17 packet is rendered by the data
    channel: the M17 listener gets an `M17P` SMS within AT_ONCE_S of the POST (202) during T's over; D-STAR gets the
    text over (suffix `WEB `) only after the pause; T's voice is unchanged. The M17 voice bytes the listener
    receives are compared with a control run without the message (asserted with DVT_TRANSCODER_FAKE=1, whose
    transform is deterministic; recorded otherwise). Also recorded: whether room C received the forwarded copy
    (rig/config/dv-router/dp15-paths.toml)."""
    case = "2b"
    # control run first: the same over, nobody sends
    control_run = qso_scenario(rig, target, case + "c", lambda sender, tl: time.monotonic(), seed=case, wait_text=False)
    time.sleep(3.0)
    body = ds.nonce_body("WEB")
    box = {}

    def send_post(sender, tl):
        t0, t1, status = web_post(web_ctx.poster, MODULE, body)
        box.update(t0=t0, t1=t1, status=status)
        tl.add("post_sent", at=t0)
        return t0

    got = qso_scenario(rig, target, case, send_post, seed=case)
    assert box["status"] == 202, "POST answered %s" % box["status"]
    match = [(at, p) for at, p in got.l_m17p if body in (sms_text(p) or "")]
    assert match, "the M17 listener got no SMS with %r: %s" % (body, [sms_text(p) for _at, p in got.l_m17p])
    at, parsed = match[0]
    latency = at - box["t0"]
    note(target, case, "RECORD", "POST took %.3f s; M17 listener got `%s` (src %s) %.3f s after the POST started, %.3f s before "
         "T's last frame" % (box["t1"] - box["t0"], sms_text(parsed), parsed["src"], latency, got.t_last - at))
    assert latency <= AT_ONCE_S, "web text reached the M17 listener after %.3f s, bound %.1f s" % (latency, AT_ONCE_S)
    assert at < got.t_last, "the text reached the M17 listener only after T's over"
    check_qso(target, case, got, body, web_ctx.poster.callsign + ": ", ident=3)
    over = over_by_my(got.d_overs, "XLX999")
    if over is not None:
        note(target, case, "RECORD", "text over header %s" % ds.describe(over))
    same = control_run.l_voice == got.l_voice
    note(target, case, "RECORD", "M17 voice payloads: control %d, with message %d, identical %s" % (
        len(control_run.l_voice), len(got.l_voice), same))
    if os.environ.get("DVT_TRANSCODER_FAKE") == "1":
        assert same, "the M17 voice bytes changed while the web text was sent (fake transcoder: deterministic)"
    elif not same:
        note(target, case, "FINDING", "M17 voice differs from the control run (real transcoder: not asserted; run with DVT_TRANSCODER_FAKE=1)")
    try:
        copy = [r for r in web.room_messages(web_ctx.reader, "C") if body in r.get("body", "")]
        note(target, case, "RECORD", "room C holds %d forwarded copies of the web text (path dp15-b-to-c)" % len(copy))
    except web.WebError as exc:
        note(target, case, "RECORD", "room C history unreadable: %s" % exc)


def test_dp15_2c_m17_meta_text_rides_into_the_dstar_egress(rig, target, hub, web_ctx):
    """data.md 15.5 (M17): "META text of an M17 talker appears as D-STAR text blocks in the cross-mode egress of that
    over and the D-STAR-sourced over is untouched". An M17 talker (N0CALL) sends a stream whose LSF META carries a
    legacy two block text (TYPE 0x0005, control bytes 0x31 / 0x32, each block held 1.12 s: the edge re-publishes the
    LSF on a META change at most once a second). Asserted on a D-STAR listener of the module: a text (the first 20
    characters) decoded from the egress slow data, its blocks 0x40..0x43 at frames 1, 5, 9, 13 of a superframe; the
    status row of the talker exists. Then a D-STAR over (N0TEST) with the TX message and a D-PRS line: a second
    listener receives every AMBE frame and slow data field unchanged. Needs a transcoder for the M17 voice (the
    fake or the real one)."""
    case = "2c"
    text = "DVT META TEXT 2 BLOCKS"
    assert len(text.encode()) <= 26
    want = ds_display(text)
    metas = m17.meta_text_blocks(text)
    PACER.wait(OTHER_CALL, "status")
    hub.settle()
    renders = hub.total("dv_edge_render_total", kind="dstar-text20", result="framed")
    sid = clients.fresh_stream_id("m17")
    payloads = [bytes(16)] * 125                                  # 5 s of packets at 40 ms
    packets = m17.meta_text_stream(sid, payloads, OTHER_CALL, metas, per_block=28)
    with dl(rig, TALKER_CALL) as dlist, m17_client(sutip(rig), callsign=OTHER_CALL, module=MODULE) as talker:
        baseline = operator_baseline(web_ctx.op)
        talker.client.send(talker.dest, packets[0])
        talker.client.pump(0.3)
        talker.client.paced(talker.dest, packets[1:], 0.040, dlist.client)
        pump_for(2.0, dlist)
        overs = slowdata.overs(dlist.client.inbox)
    over = [o for o in overs if o["voice"]]
    assert over, "the D-STAR listener got no cross-mode voice of the M17 talker (transcoder up?)"
    stream = max(over, key=lambda o: len(o["voice"]))
    fields = [(v["counter"] & 0x1F, v["slow"]) for v in stream["voice"]]
    events = [e for e in dstar_sd.DvrUnitDecoder().feed_frames(fields) if e["kind"] == "text"]
    note(target, case, "RECORD", "egress: %d voice frames, %d text events %s" % (len(fields), len(events), [e["text"] for e in events][:3]))
    assert events, "no text decoded from the D-STAR egress slow data of the M17 stream"
    assert all(e["text"].rstrip() == want.rstrip() for e in events), "egress text %r, want %r" % ([e["text"] for e in events], want)
    # the block positions in a superframe that carries the text: frames 1, 5, 9, 13 start blocks 0x40..0x43
    positions = None
    for start in range(0, len(fields) - 20):
        if fields[start][0] == 0:
            sf = {seq: slow for seq, slow in fields[start:start + 21]}
            heads = [dstar_sd.xor3(sf[f])[0] if f in sf else None for f in (1, 5, 9, 13)]
            if heads == [0x40, 0x41, 0x42, 0x43]:
                positions = heads
                break
    assert positions is not None, "no superframe of the egress has the text blocks at frames 1, 5, 9, 13"
    hits, _rows = wait_operator_rows(web_ctx.op, baseline, lambda r: r.get("item_type") == "status"
                                     and r.get("callsign", "").startswith(OTHER_CALL))
    assert hits and hits[0]["body"].strip().startswith(text[:20]), "no status row of the M17 talker with the META text: %s" % hits
    value, ok = hub.wait_total("dv_edge_render_total", renders + 1, kind="dstar-text20", result="framed")
    note(target, case, "RECORD", "dv_edge_render_total{kind=dstar-text20,result=framed} %s -> %s" % (renders, value))
    assert ok, "dstar-text20 was not rendered (framed) for the M17 stream"
    # -- control: a D-STAR sourced over keeps its own slow data
    PACER.wait(TALKER_CALL, "status")
    PACER.wait(TALKER_CALL, "position")
    payload = dstar_sd.aprs_position_payload(TALKER_CALL, LAT, LON, comment="test")
    fields = dstar_sd.text_gps_fields("DVT NATIVE %04d" % (int(time.time()) % 10000), payload, superframes=3)
    frames = ambe(len(fields), case)
    sid, packets = build_over(TALKER_CALL, fields, frames)
    with dl(rig, TALKER_CALL) as talker2, dl(rig, OTHER_CALL) as listener2:
        send_over(talker2, packets)
        pump_for(1.5, talker2, listener2)
        overs2 = slowdata.overs(listener2.client.inbox)
    got = over_by_my(overs2, TALKER_CALL)
    assert got is not None, "no D-STAR over at the second listener"
    sent = [(dextra.parse_voice_raw(p)["ambe"], dextra.parse_voice_raw(p)["slow"]) for p in packets[1:-1]]
    received = [(v["ambe"], v["slow"]) for v in got["voice"]]
    assert received == sent, "the D-STAR sourced over was changed (%d of %d frames equal)" % (
        sum(1 for a, b in zip(received, sent) if a == b), len(sent))


def test_dp15_2d_m17_packet_aprs_is_a_position(rig, target, hub, web_ctx):
    """data.md 15.5 (M17): "an APRS payload in M17 packet mode (specifier 0x02) produces a position". A direct M17
    client (link N0TEST, SRC N0TEST-7) sends an M17P packet with the TNC2 text `N0TEST-7>APDVT1:!1030.00N/02015.00E>test`
    (the APRS source equals the SRC, so the identity stays): one position row (source 5) in the operator feed."""
    case = "2d"
    PACER.wait(TALKER_CALL, "position")
    baseline = operator_baseline(web_ctx.op)
    info = "N0TEST-7>APDVT1:!1030.00N/02015.00E>test".encode("ascii")
    packet = m17.m17p_packet(0x02, info, src="N0TEST-7")
    with m17_client(sutip(rig), callsign=TALKER_CALL, module=MODULE) as sender:
        sender.client.send(sender.dest, packet)
        pump_for(1.0, sender)
    hits, rows = wait_operator_rows(web_ctx.op, baseline, lambda r: r.get("item_type") == "position"
                                    and r.get("callsign", "").startswith(TALKER_CALL))
    note(target, case, "RECORD", "operator rows %d, position rows %s" % (len(rows), [(r.get("callsign"), r.get("protocol")) for r in position_rows(rows)]))
    assert hits, "no position in the operator feed for the M17 APRS packet"
    check_position(hits[0], source=5)
    assert len(position_rows(rows)) == 1, "%d position rows for one packet" % len(position_rows(rows))


# ================================================================ 4a: YSF GPS

def test_dp15_4a_ysf_gps_fix_is_one_position(rig, target, hub, web_ctx):
    """data.md 15.5 (YSF, DMR): "a GPS fix in a YSF over ... gives one position". A YSF client (N0CALL) sends an over
    of 16 packets whose FN 6/7 frames carry the GPS record (short magic 22 62, FT-1D, 10.5 N 20.25 E) twice (FN
    cycles 0..7 twice): ONE position row (source 3, symbol `/[`, comment FT-1D)."""
    case = "4a"
    PACER.wait(OTHER_CALL, "position")
    baseline = operator_baseline(web_ctx.op)
    client = ysf.YsfClient("0.0.0.0", callsign=OTHER_CALL, module=MODULE)
    try:
        assert client.link(sutip(rig)), "YSF link to %s failed" % sutip(rig)
        packets = ysf_gps.gps_stream_packets([ysf.SILENCE] * 80, OTHER_CALL, LAT, LON, ysf_gps.RADIO_FT1D, dgid=client.dgid)
        client.keepalive()
        client.client.send(client.dest, packets[0])
        client.client.pump(0.3)
        client.client.paced(client.dest, packets[1:], ysf.PACKET_GAP)
        client.client.pump(1.0)
    finally:
        client.close()
    hits, rows = wait_operator_rows(web_ctx.op, baseline, lambda r: r.get("item_type") == "position"
                                    and r.get("callsign", "").startswith(OTHER_CALL))
    note(target, case, "RECORD", "operator rows %d, position rows %s" % (len(rows), [(r.get("callsign"), r.get("protocol")) for r in position_rows(rows)]))
    assert hits, "no position in the operator feed for the YSF GPS over"
    check_position(hits[0], source=3, symbol="/[", comment="FT-1D")
    assert len(position_rows(rows, OTHER_CALL)) == 1, "%d position rows for one over" % len(position_rows(rows, OTHER_CALL))


# ================================================================ 4b, 4c: DMR

def dmr_link(ip, callsign):
    client = dmr.DmrClient("0.0.0.0", dmrid=DMR_ID[callsign], module=MODULE)
    assert client.link(ip), "DMR link to %s failed" % ip
    return client


def test_dp15_4b_dmrg_is_one_position(rig, target, hub, web_ctx):
    """data.md 15.5 (YSF, DMR): "a DMRG in a DMR over gives one position". DMR id 1023008 (N0TEST in the rig's dmrid.dat)
    sends a voice stream and, inside it, one DMRG datagram (10.5 N 20.25 E, error estimate 7 = unknown accuracy):
    ONE position row (source 4, symbol `/-`, callsign from the registry). A DMRG sent when no stream is open
    is rejected `unbound` (reject counter, no position)."""
    case = "4b"
    PACER.wait(TALKER_CALL, "position")
    hub.settle()
    baseline = operator_baseline(web_ctx.op)
    rid = DMR_ID[TALKER_CALL]
    client = dmr_link(sutip(rig), TALKER_CALL)
    try:
        frames = signals.prng_frames(2, 30, seed="dp15/4b")
        dmr.talk_with_side_packets(client, frames, {3: [dmr.dmrg_packet(rid, LAT, LON)]}, stream_id=clients.fresh_stream_id("dmr"))
        hits, rows = wait_operator_rows(web_ctx.op, baseline, lambda r: r.get("item_type") == "position"
                                        and r.get("callsign", "").startswith(TALKER_CALL))
        note(target, case, "RECORD", "operator rows %d, position rows %s" % (len(rows), [(r.get("callsign"), r.get("protocol")) for r in position_rows(rows)]))
        assert hits, "no position in the operator feed for the DMRG"
        check_position(hits[0], source=4, symbol="/-")
        assert len(position_rows(rows, TALKER_CALL)) == 1, "%d position rows for one DMRG" % len(position_rows(rows, TALKER_CALL))
        # negative twin: no stream is open now
        unbound = hub.total("dv_pkt_reject_total", reason="unbound")
        baseline = operator_baseline(web_ctx.op)
        client.client.send(client.dest, dmr.dmrg_packet(rid, LAT, LON))
        client.client.pump(0.5)
        value, ok = hub.wait_total("dv_pkt_reject_total", unbound + 1, reason="unbound")
        _hits, rows = wait_operator_rows(web_ctx.op, baseline, lambda r: False, timeout=4.0)
        note(target, case, "RECORD", "DMRG outside a stream: unbound %s -> %s, new position rows %d" % (unbound, value, len(position_rows(rows))))
        assert not position_rows(rows), "a DMRG outside a stream became a position"
        assert ok, "dv_pkt_reject_total{reason=unbound} did not move (%s -> %s)" % (unbound, value)
    finally:
        client.close()


class OnairProbe(threading.Thread):
    """Reads /api/v1/public/state every 0.4 s and keeps the on-air callsigns of one room."""

    def __init__(self, client, room):
        super().__init__(daemon=True)
        self.client, self.room, self.seen, self.stop = client, room, [], threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                state = self.client.get("/api/v1/public/state").json()
                for row in state.get("rooms", []):
                    if row.get("room") == self.room and row.get("onair"):
                        self.seen.append((time.monotonic(), row["onair"].get("callsign"), row["onair"].get("callsign_base")))
            except Exception:  # noqa: BLE001 - a missed sample is fine
                pass
            self.stop.wait(0.4)


def heard_calls(anon, room):
    data = anon.get("/api/v1/public/lastheard?limit=50").json()
    items = data if isinstance(data, list) else data.get("items", [])
    return [(r.get("callsign"), r.get("callsign_full")) for r in items if r.get("room") == room]


@pytest.mark.parametrize("alias", ["N0TEST ALIAS", "N0CALL"], ids=["alias", "foreign-callsign"])
def test_dp15_4c_dmra_is_a_status_and_never_the_callsign(rig, target, hub, web_ctx, alias):
    """data.md 15.5 (YSF, DMR): "a DMRA gives a status and never changes the displayed callsign". DMR id 1023008 sends a
    stream and, inside it, the DMRA blocks of a 7 bit talker alias. A `status` row of N0TEST with the alias text
    appears (public SSE, history); the on-air callsign of the room (public/state, sampled every 0.4 s) and the
    last-heard entry stay the registry callsign N0TEST. The alias `N0CALL` (another station's callsign) must not
    change them either."""
    case = "4c[%s]" % alias
    PACER.wait(TALKER_CALL, "status")
    rid = DMR_ID[TALKER_CALL]
    client = dmr_link(sutip(rig), TALKER_CALL)
    probe = OnairProbe(web_ctx.anon(), MODULE)
    try:
        with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse:
            assert sse.ready()
            probe.start()
            frames = signals.prng_frames(2, 120, seed="dp15/4c")
            side = {3: dmr.dmra_packets(rid, alias)}
            t0 = time.monotonic()
            dmr.talk_with_side_packets(client, frames, side, stream_id=clients.fresh_stream_id("dmr"))
            hit = sse.wait_data(lambda o: o.get("item_type") == "status" and o.get("callsign", "").startswith(TALKER_CALL),
                                timeout=6.0, since=t0)
            probe.stop.set()
            heard = heard_calls(web_ctx.anon(), MODULE)
    finally:
        probe.stop.set()
        client.close()
    calls = sorted({c for _t, c, _b in probe.seen if c})
    note(target, case, "RECORD", "on-air callsigns seen %s (%d samples); last heard %s; status event %s" % (
        calls, len(probe.seen), heard[:3], None if hit is None else hit[1].get("body")))
    assert hit is not None, "no status event of %s for the DMRA alias" % TALKER_CALL
    assert hit[1].get("body", "").strip() == alias, "status body %r, want the alias %r" % (hit[1].get("body"), alias)
    assert probe.seen, "never saw an on-air entry for room %s in public/state" % MODULE
    assert set(calls) == {TALKER_CALL}, "the displayed on-air callsign was %s, want only %s" % (calls, TALKER_CALL)
    assert heard and all(TALKER_CALL in (c or "") for c, _f in heard[:1]), "last heard shows %s" % heard[:3]
    assert not any("ALIAS" in (c or "") for c, _f in heard)
    if alias == "N0CALL":
        assert not any((c or "") == "N0CALL" for c in calls), "the alias changed the displayed callsign"


# ================================================================ 7a: the text over as an XLX peer receives it

def test_dp15_7a_text_over_on_the_xlx_peer_wire(rig, target, hub, web_ctx):
    """data.md 15.5 (XLX peer), wire half: the tester plays the peer XLX996 (xlx-peer-tester.toml, modules DE) and links
    to the SUT. A web text to module D reaches it as one stream: header MY `XLX999`, suffix `WEB `, 22 voice
    frames (21 with null AMBE and the text over slow data, the last frame) about 440 ms from header to last frame,
    sync at frame 0, text `N0DPT: <body>`. A second text submitted right after waits at least 1 s after the end of
    the first over (data.md 7.5, V3). The remote floor, recording and cross-mode effects need a second DV-Router
    node (unit U8) and are not covered. Not covered either (needs a live config edit): xlx_text_overs = false."""
    case = "7a"
    peer = xlx.XlxPeer("XLX996", "DE")
    try:
        if peer.link(sutip(rig)) is None:
            pytest.skip("no full XLX link to the SUT as XLX996: add rig/config/dv-router/xlx-peer-tester.toml to DVT_SUT_CONFIG_EXTRA")
        time.sleep(2.0)
        poster = web_ctx.poster
        t_start = time.monotonic()
        body1, body2 = ds.nonce_body("XA"), ds.nonce_body("XB")
        _t0, _t1, status1 = web_post(poster, XLX_ROOM, body1)
        _t0, _t1, status2 = web_post(poster, XLX_ROOM, body2, pace=False)         # the account bucket is 1 per 5 s, burst 2
        assert (status1, status2) == (202, 202), "POST answers %s %s" % (status1, status2)
        end = time.monotonic() + 3 * IDLE_GAP_S + 10.0
        streams = {}
        while time.monotonic() < end:
            streams = peer.streams(sutip(rig), since=t_start)
            done = [s for s in streams.values() if s["frames"] and s["frames"][-1]["last"]]
            if len(done) >= 2:
                break
            time.sleep(0.1)
        arrivals = {}
        for at, data in peer.packets(sutip(rig), t_start, pred=lambda d: d.startswith(b"DSVT")):
            parsed = xlx.parse_voice(data) or xlx.parse_header(data)
            if parsed:
                arrivals.setdefault(parsed["stream_id"], []).append((at, data))
    finally:
        peer.close()
    done = [(sid, s) for sid, s in streams.items() if s["frames"] and s["frames"][-1]["last"]]
    note(target, case, "RECORD", "streams from the SUT %d, complete %d" % (len(streams), len(done)))
    assert len(done) == 2, "want 2 complete streams (one per web text), got %d of %d" % (len(done), len(streams))
    spans = []
    for sid, s in sorted(done, key=lambda item: arrivals[item[0]][0][0]):
        header = s["header"]
        assert bytes(header["my"]).rstrip() == b"XLX999", header
        assert len(s["frames"]) == slowdata.OVER_FRAMES, "%d frames, want %d" % (len(s["frames"]), slowdata.OVER_FRAMES)
        assert all(f["ambe"] == dextra.NULL_AMBE for f in s["frames"][:21]), "voice frames are not null AMBE"
        text = slowdata.decode_text_over([f["slow"] for f in s["frames"][:21]])
        assert text is not None and text["repeat_ok"] and text["filler_ok"], "no text over in the slow data: %s" % text
        assert text["text"].startswith(poster.callsign + ": "), text
        first, last = arrivals[sid][0][0], arrivals[sid][-1][0]
        spans.append((first, last, text["text"].rstrip()))
        note(target, case, "RECORD", "stream %04x: %s, header to last frame %.3f s, suffix %r" % (sid, text["text"].rstrip(), last - first, header.get("suffix")))
        assert 0.3 <= last - first <= 0.9, "header to last frame took %.3f s, want about 0.44 s" % (last - first)
    assert {t for _a, _b, t in spans} == {"%s: %s" % (poster.callsign, body1), "%s: %s" % (poster.callsign, body2)}, spans
    gap = spans[1][0] - spans[0][1]
    note(target, case, "RECORD", "second over started %.3f s after the first ended" % gap)
    assert gap >= 1.0 - 0.15, "second text over started %.3f s after the first ended, want >= 1 s" % gap
