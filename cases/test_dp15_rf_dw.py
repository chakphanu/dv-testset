# SPDX-License-Identifier: MIT
"""DV-Router data-channel cases of data.md 15.5 for RF through the AX.25 edge, tier B (DP15 group 6): the edge is the kiss-tcp
CLIENT of two REAL Dire Wolf TNCs (rig services dw-a .48 and dw-b .49, profile dw), which sit on a modem loop: what one
transmits the other demodulates (AFSK 1200 baud audio relayed over UDP, no hardware; docs/direwolf-cases.md "Modem loop").
Tier A (cases/test_dp15_rf.py) plays the TNCs with a fake and proves the logic; tier B adds the real KISS bytes of Dire Wolf
and a real modulator and demodulator in between. Case table, run commands (two runs: RF transmit off and on) and what is real:
docs/dp15-cases.md, "RF tier B".

  DP15-6l  the loop itself: a frame the tester sends into dw-b's KISS port comes out of dw-a's, and the other way round
           (a control, addressed to nobody: it must not reach the room)
  DP15-6a  a message to the room name on RF reaches the room: the tester sends the UI frame `N0TEST-7>APDVT1::XLX999B  :...`
           into dw-b's KISS port, dw-b transmits, dw-a demodulates and hands it to the edge (port rf-a); D-STAR text over, M17
           SMS, public SSE and history row; no RF ack at all (the room name has 7 characters)
  DP15-6b  RF transmit off (run DVT_DP15_TX_ENABLE=false, receive only): room text produces no frame at all while the room
           still gets it; seen at dw-b's KISS port, which hears everything dw-a transmits
  DP15-6c  with tx_enable, the port tx switch, an explicit budget and the path rf-out, room text goes out of dw-a as ONE group
           message `:XLX999B  :<text>` without id from N0TEST-1 to APZDVR, observed as the frame dw-b demodulates and hands to
           the tester; the receive-only port rf-b never transmits (nothing arrives at dw-a); the edge hears its own frame on rf-b
           and the room gets no second item (an own identity)

Not covered here (tier A only): 6d (the duty cycle is checked on frame times, the audio adds nothing), 6e (a frame heard on two
ports: no hardware-free way to give two Dire Wolf instances the same audio without a tee; the dedupe is DV-Router's), 6s (the
KISS parameter frames are what the fake TNC records; Dire Wolf logs them).

Run shapes (docs/dp15-cases.md): `scripts/run.sh --profile web,dw` with DVT_DP15_TX_ENABLE=false (6l, 6a, 6b) and with
DVT_DP15_TX_ENABLE=true plus dp15-rf-out.toml (6l, 6a, 6c). The rendered config of the run (out/<run>/xlxd-config/xlxd-sut/
dv-router.toml) says which shape it is; every case skips with the reason when its prerequisite is missing. Observations go to
stdout and out/<run>/dp15-rf-dw.txt ("RECORD", "FINDING").

A Dire Wolf is deaf while its own transmission would still be on the air (docs/direwolf-cases.md): the cases keep a pause of
more than the air time between a transmission of one TNC and a frame in the other direction.

NOT RUN ON A RIG yet (written against the sources and the unit tests of the loop). Assumptions are listed in docs/dp15-cases.md.
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import ax25, kiss_client, kiss_tnc, slowdata, web  # noqa: E402

import data_support as ds  # noqa: E402
import txguard_support  # noqa: E402
from data_support import PACER, SseWatch, dstar_listener, m17_client, pump_for, texts_of, write_note  # noqa: E402
from test_dp15_rf import (  # noqa: E402,F401  (hub and web_ctx are the module fixtures of tier A, reused as they are)
    DRAIN_S, EDGE_TOCALL, MODULE, ROOM_NAME, RF_AT_ONCE_S, RF_SRC, RF_TEXT_OVER_S, SILENCE_S, STATION, TEXT_LIMIT, TX_SPACING_S, TX_WAIT_S,
    dstar_times, hub, m17_times, modem_total, pump_until, read_rf_config, rf_frame, port_row, post, sse_texts, sutip, web_ctx)

pytestmark = pytest.mark.rig

LOG = "dp15-rf-dw.txt"
SETTING = "tier B, require_ident=%s tx=%s" % (os.environ.get("DVT_REQUIRE_IDENT", "true"), os.environ.get("DVT_DP15_TX_ENABLE", "false"))
KISS_PORT = 8001
CONNECT_S = 120.0                   # the containers start with the rig; the edge reconnects within 1..5 s
LOOP_WAIT_S = 20.0                  # a frame through the loop: CSMA wait, TXDELAY, the frame (0.7 to 4.5 s of audio), relay (1 s)
PROBE_EVERY_S = 12.0
PROBE_SRC = "N0TPRB"                # an own base callsign for the probes: the room text bucket is per base callsign
PROBE_TO = "N0PRB"                  # an addressee that is not the room: dropped by the worker, counted by the edge
PAUSE_S = 8.0                       # between a transmission of one TNC and a frame in the other direction (deafness, see above)


def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


def probe_frame(seq, src=PROBE_SRC):
    return kiss_tnc.ui_frame(src, "APDVT1", kiss_tnc.aprs_message_info(PROBE_TO, "probe %d" % seq, str(seq)), digis=())


@pytest.fixture(scope="module")
def dw(request, target, hub):
    """The tester as a KISS client of both Dire Wolf TNCs, after the AX.25 edge of the run is configured for them and the loop
    carries a probe frame to the edge. Skips when the profile or the snippet is missing; fails when a TNC does not answer."""
    if os.environ.get("DVT_DW") != "on":
        pytest.skip("the Dire Wolf TNCs are not part of this rig: run scripts/run.sh --profile web,dw (DVT_DW is not 'on')")
    host_a, host_b = os.environ.get("DVT_DW_A", ""), os.environ.get("DVT_DW_B", "")
    if not (host_a and host_b):
        pytest.skip("DVT_DW_A / DVT_DW_B (the TNC addresses) are not set")
    got = read_rf_config(target)
    if got is None:
        pytest.skip("the rendered config has no enabled [edges.ax25]: add rig/config/dv-router/dp15-rf-dw.toml to DVT_SUT_CONFIG_EXTRA")
    ax, paths = got
    rows = {name: port_row(ax, name) for name in ("rf-a", "rf-b")}
    want = {"rf-a": host_a, "rf-b": host_b}
    if not all(r and r.get("kind") == "kiss-tcp" and r.get("host") == want[n] and int(r.get("port", 0)) == KISS_PORT for n, r in rows.items()):
        pytest.skip("the [edges.ax25] config does not point rf-a / rf-b at the Dire Wolf TNCs %s / %s port %d (dp15-rf-dw.toml)" % (host_a, host_b, KISS_PORT))
    out_paths = [p for p in paths if p.get("to") == "edge:ax25/rf-a"]
    tx_on = bool(ax.get("tx_enable")) and bool(rows["rf-a"].get("tx"))
    a = kiss_client.KissClient(host_a, KISS_PORT, name="dw-a")
    b = kiss_client.KissClient(host_b, KISS_PORT, name="dw-b")
    try:
        for name, client in (("dw-a", a), ("dw-b", b)):
            if not client.connect(CONNECT_S):
                pytest.fail("the Dire Wolf TNC %s (%s:%d) does not accept a KISS TCP client in %.0f s" % (name, client.host, client.port, CONNECT_S))
        # the loop and the edge: probe frames until the edge counts a frame heard at rf-a (the edge may connect after the TNC is up)
        base = modem_total(hub, "rf-a", "rx")
        start = time.monotonic()
        seq = 0
        heard_at_a = 0
        edge_rx = False
        while time.monotonic() - start < CONNECT_S and not edge_rx:
            seq += 1
            t0 = time.monotonic()
            if not b.send(probe_frame(seq)):
                pytest.fail("the KISS connection to dw-b broke")
            heard_at_a += len(a.wait_frames(1, timeout=PROBE_EVERY_S, since=t0))
            time.sleep(3.0)                      # the hub counters refresh every 10 s or so
            edge_rx = modem_total(hub, "rf-a", "rx") > base
        ctx = types.SimpleNamespace(cfg=ax, paths=paths, rows=rows, a=a, b=b, tx_on=tx_on, has_out_path=bool(out_paths), probes=seq,
                                    probes_heard=heard_at_a, edge_connected=edge_rx,
                                    tx_per_min=int(rows["rf-a"].get("tx_per_min", 2)), tx_burst=int(rows["rf-a"].get("tx_burst", 1)),
                                    tx_guard=txguard_support.make_guard(request, lambda case, tag, text: note(target, case, tag, text)))
        note(target, "dw", "RECORD", "KISS clients connected to dw-a and dw-b; %d probe frames sent through dw-b, %d seen at dw-a, the edge counted a frame at rf-a: %s; "
             "tx_enable %s, port rf-a tx %s, rf-out path %s" % (seq, heard_at_a, edge_rx, ax.get("tx_enable"), rows["rf-a"].get("tx"), ctx.has_out_path))
        if heard_at_a == 0:
            pytest.fail("no probe frame came out of dw-a's KISS port in %.0f s: the modem loop is broken (docs/direwolf-cases.md)" % CONNECT_S)
        yield ctx
    finally:
        a.close()
        b.close()


def need_tx(dw, case="6"):
    """Skip unless RF transmit is on; then wait for the startup / recovery TX guard of the edge (RFSAFE1, radio.md 10.4) to end.
    Call it again after any reload or edge restart the case performs."""
    if not (dw.tx_on and dw.has_out_path):
        pytest.skip("RF transmit is off in this run: use DVT_DP15_TX_ENABLE=true with dp15-rf-out.toml (docs/dp15-cases.md)")
    guard = getattr(dw, "tx_guard", None)
    if guard is not None:
        guard("DP15-%s" % case)


def need_rx_only(dw):
    if dw.tx_on or dw.has_out_path:
        pytest.skip("this case needs tx_enable false and no rf-out path: run with DVT_DP15_TX_ENABLE=false and dp15-rf-dw.toml alone")


def last_air(dw):
    """time.monotonic() of the newest frame either TNC demodulated (a transmission of the other one), else 0."""
    return max([t for t, _c, _f in dw.a.frames()] + [t for t, _c, _f in dw.b.frames()], default=0.0)


def wait_tx_spacing(dw):
    """Sleep until TX_SPACING_S passed since the last frame on the air (tx_per_min 2, burst 1 of the edge; also more than the
    air time of a Dire Wolf, which is deaf while it transmits)."""
    need = last_air(dw) + TX_SPACING_S - time.monotonic()
    if need > 0:
        time.sleep(need)


# ================================================================ 6l: the loop itself

def test_dp15_6l_the_modem_loop_passes_a_frame_both_ways(rig, target, hub, dw):
    """The control of every other case: the tester sends a UI frame into dw-b's KISS port; dw-b modulates it, dw-a demodulates
    it and gives it to its KISS clients (the tester and the edge): the same bytes come out. Then the other way round, after a
    pause (a Dire Wolf does not hear while its own transmission would be on the air). The frames are addressed to nobody
    (N0PRB), so the room sees nothing. Recorded: both latencies."""
    case = "6l"
    f1, f2 = probe_frame(101), probe_frame(102, src="N0TPRC")
    assert ax25.dvrouter_accepts_frame(f1) and ax25.dvrouter_accepts_frame(f2)
    time.sleep(PAUSE_S)
    t0 = time.monotonic()
    assert dw.b.send(f1)
    got = dw.a.wait_frames(1, timeout=LOOP_WAIT_S, since=t0)
    t1 = time.monotonic()
    time.sleep(PAUSE_S)
    t2 = time.monotonic()
    assert dw.a.send(f2)
    back = dw.b.wait_frames(1, timeout=LOOP_WAIT_S, since=t2)
    note(target, case, "RECORD", "dw-b -> dw-a: %s in %s s; dw-a -> dw-b: %s in %s s" % (
        len(got), None if not got else "%.2f" % (got[0][0] - t0), len(back), None if not back else "%.2f" % (back[0][0] - t2)))
    assert [f for _t, _c, f in got] == [f1], "dw-a's KISS port gave %s after a frame was sent into dw-b" % [f.hex() for _t, _c, f in got]
    assert [f for _t, _c, f in back] == [f2], "dw-b's KISS port gave %s after a frame was sent into dw-a" % [f.hex() for _t, _c, f in back]
    assert got[0][1] == 0 and back[0][1] == 0, "KISS channel (port nibble) not 0"
    assert t1 - t0 < LOOP_WAIT_S


# ================================================================ 6a: a message to the room name on RF reaches the room

def test_dp15_6a_message_to_the_room_name_on_rf_reaches_the_room(rig, target, hub, dw, web_ctx):
    """data.md 15.5 (RF): "a message to the room name on RF reaches the room". The tester sends the UI frame
    `N0TEST-7>APDVT1,WIDE1-1::XLX999B  :DVT nnnn{3` into dw-b's KISS port; dw-b transmits it, dw-a demodulates it (the same
    bytes come out of dw-a's KISS port, which the tester also sees) and the edge delivers it: the room gets ONE text item (a
    D-STAR text over on a DExtra listener, an M17 SMS on an M17 listener, a `text` row on the public SSE and in the room
    history). The bounds are those of tier A, counted from the moment the frame comes out of dw-a (the modem loop has its own
    latency, recorded). Counters: the port's `rx`, the accepted text item. NO frame appears at either TNC afterwards: an
    RF ack would have to come from the addressee, which has 7 characters and is not an AX.25 address, and `alias_calls` is
    empty (data.md 9.4); anything dw-b hears is something dw-a transmitted."""
    case = "6a"
    body = ds.nonce_body("DVT")
    PACER.wait(RF_SRC, "text")
    before = hub.settle()
    rx_before = modem_total(hub, "rf-a", "rx")
    frame = rf_frame(body, msg_id="3")
    assert ax25.dvrouter_accepts_frame(frame)
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, \
            dstar_listener(sutip(rig), module=MODULE, callsign="N0CALL") as dlist, \
            m17_client(sutip(rig), callsign="N0TEST", module=MODULE) as mlist:
        assert sse.ready(), "the public SSE gave no hello: %s" % sse.error
        t_inject = time.monotonic()
        assert dw.b.send(frame), "the KISS connection to dw-b is gone"
        heard = dw.a.wait_frames(1, timeout=LOOP_WAIT_S, since=t_inject)
        assert heard, "the frame did not come out of dw-a's KISS port within %.0f s: the modem loop failed (see 6l)" % LOOP_WAIT_S
        t_heard = heard[0][0]
        pump_until(RF_TEXT_OVER_S + 6.0, [dlist, mlist], lambda: bool(m17_times(mlist, body)) and bool(dstar_times(dlist, body)))
        pump_for(DRAIN_S, dlist, mlist)
        m17 = m17_times(mlist, body)
        dstar = dstar_times(dlist, body)
        events = sse_texts(sse, body, t_inject)
        texts = [x["text"].rstrip() for _s, x in texts_of(slowdata.overs(dlist.client.inbox))]
    rows = [r for r in web.room_messages(web_ctx.reader, MODULE) if r.get("item_type") == "text" and body in (r.get("body") or "")]
    note(target, case, "RECORD", "modem loop %.3f s (dw-b KISS in to dw-a KISS out); then M17 SMS %s s, D-STAR text over %s s, SSE text events %d, history rows %d after the frame left dw-a; D-STAR texts %s; row %s" % (
        t_heard - t_inject, None if not m17 else "%.3f" % (m17[0] - t_heard), None if not dstar else "%.3f" % (dstar[0] - t_heard), len(events), len(rows), texts[:2],
        None if not rows else {k: rows[0].get(k) for k in ("callsign", "protocol", "room")}))
    assert [f for _t, _c, f in heard] == [frame], "dw-a handed out %s, not the frame sent into dw-b" % [f.hex() for _t, _c, f in heard]
    assert len(m17) == 1, "%d M17 SMS packets with %r, want 1" % (len(m17), body)
    assert m17[0] - t_heard <= RF_AT_ONCE_S, "the RF message reached the M17 listener %.3f s after dw-a handed it out, bound %.1f s" % (m17[0] - t_heard, RF_AT_ONCE_S)
    assert len(dstar) == 1, "%d D-STAR text overs with %r, want 1: %s" % (len(dstar), body, texts)
    assert dstar[0] - t_heard <= RF_TEXT_OVER_S, "the D-STAR text over started %.3f s after the frame, bound %.1f s" % (dstar[0] - t_heard, RF_TEXT_OVER_S)
    assert len(events) == 1 and len(rows) == 1, "web: %d SSE events, %d history rows, want 1 each" % (len(events), len(rows))
    assert rows[0].get("room") == MODULE
    # no RF ack, whatever the transmit switch says: it would leave dw-a and be demodulated by dw-b
    assert dw.b.frames(t_inject) == [], "dw-b demodulated a frame (something dw-a transmitted) after a message with an id to a 7 character room name: %s" % [
        ax25.to_tnc2(f) for _t, _c, f in dw.b.frames(t_inject)]
    assert len(dw.a.frames(t_inject)) == 1, "dw-a handed out %d frames, want only the injected one" % len(dw.a.frames(t_inject))
    after, seen = hub.wait_for(before, lambda now, was: now["pkt.text.accepted"] > was["pkt.text.accepted"])
    change = ds.delta(after, before)
    rx = modem_total(hub, "rf-a", "rx") - rx_before
    note(target, case, "RECORD", "counter deltas %s; dv_edge_modem_total{port=rf-a,result=rx} +%d" % (ds.moved(change), rx))
    assert seen, "dv_pkt_items_total{kind=text,result=accepted} did not move in %.0f s" % ds.POLL_S
    assert rx >= 1, "dv_edge_modem_total{port=rf-a,result=rx} did not move"


# ================================================================ 6b: RF transmit off

def test_dp15_6b_no_rf_transmit_while_tx_enable_is_false(rig, target, hub, dw, web_ctx):
    """data.md 15.5 (RF): "room text goes out as a group message only when tx_enable ... allow". Run shape: tx_enable false and
    no path to RF (DV-Router refuses `tx = true` and a path to RF without tx_enable). A web user posts a text to module B;
    the room still gets it (an M17 SMS and a D-STAR text over: RF is independent of the room), and for 12 s NOTHING comes out
    of dw-b's KISS port (it would hear what dw-a transmits) or of dw-a's (it would hear dw-b); `dv_edge_modem_total{result=tx}`
    does not move. A transmission by the edge would be a real modulated frame here, not only a KISS write."""
    case = "6b[tx_enable=false]"
    need_rx_only(dw)
    time.sleep(PAUSE_S)
    body = ds.nonce_body("OFF")
    hub.settle()
    tx_before = modem_total(hub, "rf-a", "tx") + modem_total(hub, "rf-b", "tx")
    poster = web_ctx.posters[0]
    with dstar_listener(sutip(rig), module=MODULE, callsign="N0CALL") as dlist, \
            m17_client(sutip(rig), callsign="N0TEST", module=MODULE) as mlist:
        t0, _t1, status = post(poster, body)
        pump_until(RF_TEXT_OVER_S + 6.0, [dlist, mlist], lambda: bool(m17_times(mlist, body)) and bool(dstar_times(dlist, body)))
        pump_for(SILENCE_S, dlist, mlist)
        m17, dstar = m17_times(mlist, body), dstar_times(dlist, body)
    on_air = dw.a.frames(t0) + dw.b.frames(t0)
    tx_after = modem_total(hub, "rf-a", "tx") + modem_total(hub, "rf-b", "tx")
    note(target, case, "RECORD", "POST %s; room got M17 %d, D-STAR %d; frames demodulated by the TNCs %d; modem tx counter %s -> %s" % (
        status, len(m17), len(dstar), len(on_air), tx_before, tx_after))
    assert status == 202, "POST answered %s" % status
    assert m17 and dstar, "the room did not get the text (M17 %d, D-STAR %d): the silence at the TNCs would prove nothing" % (len(m17), len(dstar))
    assert on_air == [], "RF frames on the air with tx_enable false: %s" % [ax25.to_tnc2(f) for _t, _c, f in on_air]
    assert tx_after == tx_before, "dv_edge_modem_total{result=tx} moved: %s -> %s" % (tx_before, tx_after)


# ================================================================ 6c: room text goes out as a group message

def test_dp15_6c_room_text_goes_out_as_one_group_message(rig, target, hub, dw, web_ctx):
    """data.md 15.5 (RF): "room text goes out as a group message only when `tx_enable` and the budget allow". With tx_enable, the
    port's tx switch, `tx_budget = {}` and the path rf-out, a web text of module B leaves dw-a as ONE modulated frame that
    dw-b demodulates and hands to the tester: a UI frame from N0TEST-1 (the station callsign) to APZDVR (tocall), no
    digipeaters (empty tx_path), information field `:XLX999B  :<text>` WITHOUT a message id (no ack, data.md 7.4), at most
    67 text characters. Nothing comes out of dw-a (the receive-only port rf-b, whose TNC would have to transmit, never
    does). The edge hears its own frame on rf-b: the room gets no second item for it (the station callsign is an own identity,
    data.md 9.1): one history row. Counters: the modem tx, and the scheduler's sent."""
    case = "6c"
    need_tx(dw, case)
    wait_tx_spacing(dw)
    body = ds.nonce_body("OUT")
    hub.settle()
    tx_before = modem_total(hub, "rf-a", "tx")
    rxb_before = modem_total(hub, "rf-b", "rx")
    sent_before = hub.total("dv_edge_rf_tx_total", result="sent")
    t0, _t1, status = post(web_ctx.posters[2], body)
    dw.b.wait_frames(1, timeout=TX_WAIT_S + LOOP_WAIT_S, since=t0)
    time.sleep(DRAIN_S)
    frames = dw.b.frames(t0)
    assert status == 202, "POST answered %s" % status
    assert len(frames) == 1, "%d frames came out of dw-b after one room text, want exactly 1 (within %.0f s)" % (len(frames), TX_WAIT_S + LOOP_WAIT_S + DRAIN_S)
    at, channel, raw = frames[0]
    msg = kiss_tnc.decode_message(raw)
    note(target, case, "RECORD", "frame %.3f s after the POST (scheduler, dw-a CSMA and TXDELAY, air time, demodulation), %d bytes, channel %d: %s" % (
        at - t0, len(raw), channel, ax25.to_tnc2(raw)))
    assert msg is not None, "the demodulated frame is not an APRS message UI frame: %s" % raw.hex()
    assert channel == 0 and ax25.dvrouter_accepts_frame(raw)
    assert msg["src"] == STATION, "AX.25 source %r, want the station callsign %r" % (msg["src"], STATION)
    assert msg["dst"] == EDGE_TOCALL, "AX.25 destination %r, want tocall %r" % (msg["dst"], EDGE_TOCALL)
    assert msg["digis"] == [], "digipeaters %s, want none (tx_path is empty)" % msg["digis"]
    assert msg["addressee"] == ROOM_NAME, "message addressee %r, want %r" % (msg["addressee"], ROOM_NAME)
    assert msg["info"][:11] == b":XLX999B  :", msg["info"]
    assert msg["msg_id"] is None and b"{" not in msg["info"], "the group message carries an id: %r" % msg["info"]
    assert body in msg["text"], "text %r does not contain the posted %r" % (msg["text"], body)
    assert len(msg["text"]) <= TEXT_LIMIT
    assert dw.a.frames(t0) == [], "dw-a handed out a frame (something dw-b transmitted: the receive-only port rf-b wrote data): %s" % [
        ax25.to_tnc2(f) for _t, _c, f in dw.a.frames(t0)]
    tx_after, ok = hub.wait_total("dv_edge_modem_total", tx_before + 1, port="rf-a", result="tx")
    rxb = modem_total(hub, "rf-b", "rx") - rxb_before
    rows = [r for r in web.room_messages(web_ctx.reader, MODULE) if r.get("item_type") == "text" and body in (r.get("body") or "")]
    note(target, case, "RECORD", "dv_edge_modem_total{rf-a,tx} %s -> %s; dv_edge_rf_tx_total{sent} %s -> %s; the edge heard its own frame on rf-b: rx +%d; history rows with the text %d %s" % (
        tx_before, tx_after, sent_before, hub.total("dv_edge_rf_tx_total", result="sent"), rxb, len(rows), [r.get("callsign") for r in rows]))
    assert ok, "dv_edge_modem_total{port=rf-a,result=tx} did not rise from %s" % tx_before
    assert len(rows) == 1, "%d room rows with the posted text (the web post and, if the edge took its own frame from rf-b, a copy): %s" % (
        len(rows), [(r.get("callsign"), r.get("protocol")) for r in rows])
