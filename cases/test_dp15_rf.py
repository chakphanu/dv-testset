# SPDX-License-Identifier: MIT
"""DV-Router data-channel cases of data.md 15.5 for RF through the AX.25 edge, tier A (DP15 group 6): the tester plays
the KISS TNCs (dvtest/kiss_tnc.py) the edge connects to as a kiss-tcp CLIENT, so the cases need no Dire Wolf and no
audio. Specification: docs/dp15-plan.md section 3.6; case table, bounds, status and the run commands (two runs: RF
transmit off and on): docs/dp15-cases.md. Tier B (a real Dire Wolf loop) is cases/test_dp15_rf_dw.py.

  DP15-6a  a message to the room name on RF reaches the room (D-STAR text over, M17 SMS, web); NO RF ack appears: the
           room name XLX999B has 7 characters and cannot be an AX.25 source (data.md 9.4)
  DP15-6b  RF transmit off: with tx_enable false (run DVT_DP15_TX_ENABLE=false, receive only) room text produces no frame
           at the TNC while the room still gets it; with tx_enable true the receive-only port rf-b never gets a frame
  DP15-6c  with tx_enable, the port tx switch, an explicit budget and the path rf-out, room text goes out as ONE group
           message `:XLX999B  :<text>` without id from N0TEST-1 to APZDVR (decoded UI frame)
  DP15-6d  never above the duty cycle: several texts in a row, the TNC sees at most tx_per_min + tx_burst frames in a
           rolling minute, the gap of the budget, and an airtime (DV-Router's upper bound) within the configured budget
  DP15-6e  a frame heard on two ports is one item (`heard_ports` and `duplicates` counted); a different message on the
           second port is a second item
  DP15-6g  right after a start the port with effective TX is a fence startup_guard with tx_guard_ms (AX.25) or tx_state fenced with a
           shrinking guard_ms (radio ports); read inside the first 13 s; a late read re-arms the fence by restarting the AX.25 child through the admin API and reads it again (RFSAFE1, RIF4b)
  DP15-6s  the five KISS parameter frames are written after every connect (also after a reconnect)

Run shapes (docs/dp15-cases.md): the snippet rig/config/dv-router/dp15-rf.toml with DVT_DP15_TX_ENABLE=false (6a, 6b off,
6e, 6s) and with DVT_DP15_TX_ENABLE=true plus dp15-rf-out.toml (6a, 6b port, 6c, 6d, 6e, 6s). The cases read the RENDERED
config of the run (out/<run>/xlxd-config/xlxd-sut/dv-router.toml) to learn which shape they run in and skip with the
reason in the other. Every case skips when its prerequisite is missing. Observations go to stdout and
out/<run>/dp15-rf.txt ("RECORD", "FINDING").

NOT RUN ON A RIG yet (written against the sources only). Assumptions about the edge that could not be verified are
listed in docs/dp15-cases.md.
"""
import os
import sys
import time
import tomllib
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import airtime, ax25, kiss_tnc, metrics, slowdata, txguard, web  # noqa: E402

import data_support as ds  # noqa: E402
import txguard_support  # noqa: E402
from data_support import (  # noqa: E402
    IDLE_GAP_S, PACER, ROOM, SseWatch, dstar_listener, first_header_times, hub_session, m17_client, m17p_packets, pump_for,
    sms_text, texts_of, write_note)

pytestmark = pytest.mark.rig

MODULE = ROOM
ROOM_NAME = "XLX999B"
LOG = "dp15-rf.txt"
SETTING = "require_ident=%s tx=%s" % (os.environ.get("DVT_REQUIRE_IDENT", "true"), os.environ.get("DVT_DP15_TX_ENABLE", "false"))
RF_SRC = "N0TEST-7"                 # the station heard on RF (an APRS source of N0CALL / NOCALL / SERVER would be rejected)
RF_TOCALL = "APDVT1"
STATION = "N0TEST-1"                # [edges.ax25] callsign of the snippet
EDGE_TOCALL = "APZDVR"
CONNECT_S = 90.0                    # the edge starts with the node; its reconnect backoff is 1..5 s
RF_AT_ONCE_S = 1.0                  # RF text to the M17 listener: pipeline edge -> worker -> controller -> worker -> edge (lead: 0.5 s guess, 1 s safer)
RF_TEXT_OVER_S = IDLE_GAP_S + 0.6 + 1.5      # an idle room: the slot is granted at once; the same allowance as DP15-2
SILENCE_S = 12.0                    # how long "nothing arrives at the TNC" is watched
TX_WAIT_S = 25.0                    # a transmit passes the PTT snapshot, the scheduler and the min gap
TX_SPACING_S = 31.0                 # tx_per_min 2, burst 1: the next frame needs 30 s after the last
TEXT_LIMIT = 67                     # data.md 10.5 / render.rs: messages are at most 67 characters
DRAIN_S = 8.0


def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics with the data worker on (skips with the reason otherwise)."""
    yield from hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    """The web side: a reader and three posters (users that may transmit; the account bucket is 1 per 5 s, burst 2)."""
    return types.SimpleNamespace(op=web_operator, reader=web_user("N0DRR", may_tx=False).client, anon=web_client,
                                 posters=[web_user("N0DRA", may_tx=True), web_user("N0DRB", may_tx=True), web_user("N0DRC", may_tx=True)])


def read_rf_config(target):
    """The [edges.ax25] table and the [[data.path]] rows of the RENDERED dv-router.toml of this run, or None."""
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ax = (doc.get("edges") or {}).get("ax25")
    if not isinstance(ax, dict) or ax.get("enable") is not True or not ax.get("port"):
        return None
    return ax, list((doc.get("data") or {}).get("path") or [])


def port_row(ax, name):
    return next((p for p in ax["port"] if p.get("name") == name), None)


def dw_config(ax):
    """True when the edge's port rf-a points at the Dire Wolf TNC dw-a of the rig (rig/config/dv-router/dp15-rf-dw.toml, tier B:
    cases/test_dp15_rf_dw.py): the fake TNCs of this file would listen for an edge that never connects to them."""
    row = port_row(ax, "rf-a")
    return bool(row) and bool(os.environ.get("DVT_DW_A")) and row.get("host") == os.environ["DVT_DW_A"]


@pytest.fixture(scope="module")
def rf(request, target, hub):
    """Two fake KISS TNCs on the ports rf-a and rf-b of the rendered config, connected to by the AX.25 edge. Skips when the
    run has no AX.25 edge with those ports (add rig/config/dv-router/dp15-rf.toml); fails when the edge does not connect."""
    got = read_rf_config(target)
    if got is None:
        pytest.skip("the rendered config has no enabled [edges.ax25]: add rig/config/dv-router/dp15-rf.toml to DVT_SUT_CONFIG_EXTRA")
    ax, paths = got
    if dw_config(ax):
        pytest.skip("the rendered config points the AX.25 edge at the Dire Wolf TNCs (dp15-rf-dw.toml): tier B, cases/test_dp15_rf_dw.py")
    rows = {name: port_row(ax, name) for name in ("rf-a", "rf-b")}
    if not all(r and r.get("kind") == "kiss-tcp" and r.get("port") for r in rows.values()):
        pytest.skip("the [edges.ax25] config lacks the kiss-tcp ports rf-a and rf-b of dp15-rf.toml")
    out_paths = [p for p in paths if p.get("to") == "edge:ax25/rf-a"]
    tx_on = bool(ax.get("tx_enable")) and bool(rows["rf-a"].get("tx"))
    tncs = {name: kiss_tnc.FakeKissTnc("0.0.0.0", int(row["port"]), name=name) for name, row in rows.items()}
    started = []
    try:
        for tnc in tncs.values():
            tnc.start()
            started.append(tnc)
        for name, tnc in tncs.items():
            if not tnc.wait_connected(CONNECT_S):
                pytest.fail("the AX.25 edge did not connect to the fake TNC %s (port %s) in %.0f s" % (name, rows[name]["port"], CONNECT_S))
        ctx = types.SimpleNamespace(cfg=ax, paths=paths, rows=rows, a=tncs["rf-a"], b=tncs["rf-b"], tx_on=tx_on,
                                    has_out_path=bool(out_paths), tx_per_min=int(rows["rf-a"].get("tx_per_min", 2)),
                                    tx_burst=int(rows["rf-a"].get("tx_burst", 1)), budget=rows["rf-a"].get("tx_budget") or {},
                                    tx_guard=txguard_support.make_guard(request, lambda case, tag, text: note(target, case, tag, text)))
        note(target, "rf", "RECORD", "AX.25 edge connected to the fake TNCs; tx_enable %s, port rf-a tx %s, rf-out path %s, tx_per_min %s burst %s, tx_budget %s" % (
            ax.get("tx_enable"), rows["rf-a"].get("tx"), ctx.has_out_path, ctx.tx_per_min, ctx.tx_burst, ctx.budget))
        yield ctx
    finally:
        for tnc in started:
            tnc.stop()


# ---------------------------------------------------------------- helpers

def sutip(rig):
    return rig.xlxd_sut


def rf_frame(body, msg_id=None, digis=("WIDE1-1",), src=RF_SRC):
    """An AX.25 UI frame with an APRS message to the room name, as a station on RF sends it."""
    return kiss_tnc.ui_frame(src, RF_TOCALL, kiss_tnc.aprs_message_info(ROOM_NAME, body, msg_id), digis=digis)


def post(poster, text, pace=True):
    """POST a room text into module B as `poster`; (time before, time after, status)."""
    if pace:
        PACER.wait(poster.callsign, "text")
    t0 = time.monotonic()
    resp = web.post_room_text(poster.client, MODULE, text, expect=None)
    return t0, time.monotonic(), resp.status


def m17_times(client, body):
    return [at for at, p in m17p_packets(client) if body in (sms_text(p) or "")]


def dstar_times(listener, body):
    sids = {sid for sid, got in texts_of(slowdata.overs(listener.client.inbox)) if body in got["text"]}
    return [at for at, _h in first_header_times(listener, lambda h: h["stream_id"] in sids)]    # one per over (5 header repeats)


def sse_texts(watch, body, since=0.0):
    return [(at, o) for at, o in watch.data_events(since) if o.get("item_type") == "text" and body in (o.get("body") or "")]


def pump_until(seconds, listeners, until):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for item in listeners:
            item.client.drain()
        if until():
            return True
        time.sleep(0.02)
    return until()


def modem_total(hub, port, result):
    return hub.total("dv_edge_modem_total", port=port, result=result)


def last_data_frame(rf):
    """time.monotonic() of the newest data frame at either TNC, else 0."""
    times = [t for t, _ch, _f in rf.a.data_frames()] + [t for t, _ch, _f in rf.b.data_frames()]
    return max(times, default=0.0)


def wait_tx_spacing(rf):
    """Sleep until TX_SPACING_S passed since the last frame the TNCs saw (tx_per_min 2, burst 1)."""
    need = last_data_frame(rf) + TX_SPACING_S - time.monotonic()
    if need > 0:
        time.sleep(need)


def need_tx(rf, case=""):
    """Skip unless RF transmit is on; then wait for the startup / recovery TX guard of the edge (RFSAFE1, radio.md 10.4: 13 s after
    start, a reload and a recovery) to end, so the first expected transmission is not held back by it. Call it again after any
    reload or edge restart the case performs."""
    if not (rf.tx_on and rf.has_out_path):
        pytest.skip("RF transmit is off in this run: use DVT_DP15_TX_ENABLE=true with dp15-rf-out.toml (docs/dp15-cases.md)")
    guard = getattr(rf, "tx_guard", None)
    if guard is not None:
        guard("DP15-%s" % case if case else "DP15-6")


def need_rx_only(rf):
    if rf.tx_on or rf.has_out_path:
        pytest.skip("this case needs tx_enable false and no rf-out path: run with DVT_DP15_TX_ENABLE=false and dp15-rf.toml alone")


# ================================================================ 6g: the startup guard is reported as a fence

def read_rendered_doc(target):
    """The RENDERED dv-router.toml of this run as a dict ({} when unreadable)."""
    try:
        return tomllib.loads((Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def radio_tx_ports(doc):
    """Names of the [[edges.radio.port]] rows with tx = true when [edges.radio] is enabled, else []."""
    radio = (doc.get("edges") or {}).get("radio")
    if not isinstance(radio, dict) or radio.get("enable") is not True or not radio.get("tx_enable"):
        return []
    return [p["name"] for p in radio.get("port") or [] if p.get("tx") is True and p.get("name")]


def rearm_ax25(request, target, case, admin_session, port, timing=None):
    """Re-arm the fence of AX.25 port `port` by restarting the AX.25 child through the product's own audited admin API
    (POST /api/v1/admin/children/{name}/restart, admin role, step-up; admin.md about line 278), then read the fence by polling
    GET /api/v1/admin/clients every 200 ms: any non-null fence is the shape (the cause is recorded), tx_guard_ms must be present and
    decrease between two reads, and the case waits until the port is unfenced again (fails after 20 s) so that 6b/6c/6d do not start
    against a fenced edge. The child key is resolved at run time from GET /api/v1/admin/status launcher.children: the single name
    containing "ax25" (zero or several: skip with the names). What the fake TNC saw (reconnect, KISS rewrite) is recorded, not asserted."""
    reader_admin = admin_session("operator")
    status = reader_admin.get(reader_admin.path("/status"), expect=None)
    names, everything = txguard.ax25_child_names(status.json() if status.status == 200 else {})
    if len(names) != 1:
        pytest.skip("cannot re-arm the AX.25 fence: %d launcher children contain 'ax25' (%s); children: %s" % (
            len(names), names, everything))
    child = names[0]
    tnc = request.getfixturevalue("rf")                           # the fake TNCs must be listening before the edge restarts
    before = {"a_conn": tnc.a.connections, "b_conn": tnc.b.connections, "a_params": len(tnc.a.startup_params(tnc.a.connections))}
    note(target, case, "RECORD", "re-arm: restarting the launcher child %r (key resolved from /api/v1/admin/status); fake TNCs before: %s" % (child, before))
    admin = admin_session("admin")

    def restart():
        admin.step_up()
        resp = admin.post(admin.path("/children/%s/restart" % child), {}, expect=None)
        assert resp.status == 200, "POST children/%s/restart answered HTTP %d, want 200" % (child, resp.status)

    res = txguard.rearm_fence(txguard.ax25_reader(reader_admin, port), restart, **(timing or {}))
    tnc.a.wait(lambda t: t.connections > before["a_conn"] and len(t.startup_params(t.connections)) >= 5, 15.0)
    after = {"a_conn": tnc.a.connections, "b_conn": tnc.b.connections, "a_params": len(tnc.a.startup_params(tnc.a.connections))}
    note(target, case, "RECORD", "re-arm: fake TNCs after (not asserted): %s; the edge %s and %s the KISS parameters" % (
        after, "reconnected" if after["a_conn"] > before["a_conn"] else "did NOT reconnect (yet)",
        "wrote" if after["a_conn"] > before["a_conn"] and after["a_params"] >= 5 else "did not write"))
    after_status = reader_admin.get(reader_admin.path("/status"), expect=None)
    if after_status.status == 200:
        restarts = [c.get("restarts") for c in ((after_status.json().get("launcher") or {}).get("children") or []) if c.get("name") == child]
        note(target, case, "RECORD", "re-arm: child %r restarts counter after: %s" % (child, restarts))
    note(target, case, "FINDING" if res.outcome != "ok" else "RECORD",
         "re-arm of the fence on ax25 port %s: %s: %s; cause %r; row gone and back: %s; %d reads; keys met: %s" % (
             port, res.outcome, res.detail, res.cause, res.row_gone, res.polls, " | ".join(res.keys)))
    assert res.outcome == "ok", "re-arm of the fence on port %s: %s (%s)" % (port, res.outcome, res.detail)


def test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds(request, rig, target, admin_session, web_client):
    """RFSAFE1 + RIF4b (radio.md 10.4, status.md): right after a start, a reload or a recovery an AX.25 or radio port with effective TX
    is fenced for about 13 s. The FIRST test of this module, with no hub, TNC or web fixtures: the admin status read shows the node
    uptime, and the case reads the port inside the window. AX.25 port rf-a: GET /api/v1/admin/clients, kind modem, tx_fence ==
    "startup_guard" and tx_guard_ms present. Radio ports with tx (none in the rig snippets today): tx_state fenced, fence
    startup_guard, guard_ms present and smaller in a second read 1 s later. Lateness is not a failure of this first attempt. When it
    is too late for AX.25 port rf-a, the fence is RE-ARMED by restarting the AX.25 child through the admin API (rearm_ax25), so the
    case normally passes instead of skipping; the case then ends with the port unfenced. Needs the shape RF-ON (DVT_DP15_TX_ENABLE=true)
    for the AX.25 half."""
    case = "DP15-6g"
    doc = read_rendered_doc(target)
    got = read_rf_config(target)
    ax_port = None
    if got is not None:
        row = port_row(got[0], "rf-a")
        if row and bool(got[0].get("tx_enable")) and bool(row.get("tx")) and not dw_config(got[0]):
            ax_port = "rf-a"
    radio_ports = radio_tx_ports(doc)
    if ax_port is None and not radio_ports:
        pytest.skip("no AX.25 port rf-a with effective TX (DVT_DP15_TX_ENABLE=true) and no radio port with tx in the rendered config: no startup fence to read")
    admin = admin_session("operator")
    uptime = txguard.uptime_reader(admin)
    results = []
    if ax_port is not None:
        results.append(("ax25", ax_port, txguard.startup_fenced_shape(txguard.ax25_reader(admin, ax_port), uptime, kind="ax25")))
    for name in radio_ports:
        results.append(("radio", name, txguard.startup_fenced_shape(txguard.radio_reader(web_client, name), uptime, kind="radio")))
    for kind, name, res in results:
        tag = "FINDING" if res.outcome == "violation" else "RECORD"
        note(target, case, tag, "startup fence on %s port %s: %s: %s; keys met: %s" % (
            kind, name, res.outcome, res.detail, " | ".join(res.obs.keys) if res.obs else "none"))
    bad = [(k, n, r) for k, n, r in results if r.outcome == "violation"]
    assert not bad, "; ".join("%s port %s: %s" % (k, n, r.detail) for k, n, r in bad)
    ax = next((r for k, _n, r in results if k == "ax25"), None)
    if ax is not None and ax.outcome == "late":
        rearm_ax25(request, target, case, admin_session, ax_port)
        return
    if all(r.outcome == "late" for _k, _n, r in results):
        pytest.skip("too late to see the startup fence: " + "; ".join("%s port %s: %s" % (k, n, r.detail) for k, n, r in results))


# ================================================================ 6s: the KISS parameter frames

def test_dp15_6s_kiss_parameters_are_written_after_every_connect(rig, target, hub, rf):
    """data.md 10.4: "KISS parameters are written once after connect and again on reconnect". Each fake TNC saw TXDELAY, P, SlotTime,
    TXtail, FullDuplex in that order for channel 0 with the values of the port's `kiss` table (the defaults 300 ms / 63 /
    100 ms / 30 ms / half duplex = 30, 63, 10, 3, 0). Then the TNC drops the connection: the edge reconnects within its
    backoff (1..5 s in the snippet) and writes them again. No SetHardware or Return command is ever written."""
    case = "6s"
    for name, tnc in (("rf-a", rf.a), ("rf-b", rf.b)):
        params = tnc.wait_params(timeout=15.0, conn=1)
        note(target, case, "RECORD", "%s connection 1: %s" % (name, params))
        assert [(ch, cmd, val) for ch, cmd, val in params[:5]] == [(0, cmd, val) for cmd, val in ax25.DVR_PARAMS], \
            "%s start-up frames %s, want %s" % (name, params, ax25.DVR_PARAMS)
        assert tnc.other_frames() == [], "the edge wrote KISS commands other than the five parameters: %s" % tnc.other_frames()
    count = rf.a.connections
    rf.a.drop_connection()
    assert rf.a.wait(lambda t: t.connections > count and len(t.startup_params(t.connections)) >= 5, 30.0), \
        "the edge did not reconnect to rf-a and write its parameters within 30 s of the drop"
    again = rf.a.startup_params(rf.a.connections)
    assert [(ch, cmd, val) for ch, cmd, val in again[:5]] == [(0, cmd, val) for cmd, val in ax25.DVR_PARAMS], again
    note(target, case, "RECORD", "after the drop the edge reconnected as connection %d and wrote %d parameter frames" % (rf.a.connections, len(again)))
    assert rf.a.other_frames() == []


# ================================================================ 6a: a message to the room name on RF reaches the room

def test_dp15_6a_message_to_the_room_name_on_rf_reaches_the_room(rig, target, hub, rf, web_ctx):
    """data.md 15.5 (RF): "a message to the room name on RF reaches the room". The fake TNC rf-a hears a UI frame
    `N0TEST-7>APDVT1,WIDE1-1::XLX999B  :DVT nnnn{3`; the room gets ONE text item: a D-STAR text over on a DExtra listener,
    an M17 SMS on an M17 listener, a `text` row on the public SSE and in the room history. Counters: the port's `rx`, the
    accepted text item. NO ack frame appears at either TNC for 8 s: an RF ack must come from the addressee, which is 7
    characters and not an AX.25 address, and `alias_calls` is empty (data.md 9.4; answer 8 of the DV-Router lead)."""
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
        assert rf.a.inject(frame), "the fake TNC rf-a has no connection to inject into"
        pump_until(RF_TEXT_OVER_S + 6.0, [dlist, mlist], lambda: bool(m17_times(mlist, body)) and bool(dstar_times(dlist, body)))
        pump_for(DRAIN_S, dlist, mlist)
        m17 = m17_times(mlist, body)
        dstar = dstar_times(dlist, body)
        events = sse_texts(sse, body, t_inject)
        texts = [x["text"].rstrip() for _s, x in texts_of(slowdata.overs(dlist.client.inbox))]
    rows = [r for r in web.room_messages(web_ctx.reader, MODULE) if r.get("item_type") == "text" and body in (r.get("body") or "")]
    note(target, case, "RECORD", "M17 SMS %s s, D-STAR text over %s s, SSE text events %d, history rows %d after the frame; D-STAR texts %s; row %s" % (
        None if not m17 else "%.3f" % (m17[0] - t_inject), None if not dstar else "%.3f" % (dstar[0] - t_inject), len(events), len(rows), texts[:2],
        None if not rows else {k: rows[0].get(k) for k in ("callsign", "protocol", "room")}))
    assert len(m17) == 1, "%d M17 SMS packets with %r, want 1" % (len(m17), body)
    assert m17[0] - t_inject <= RF_AT_ONCE_S, "the RF message reached the M17 listener after %.3f s, bound %.1f s" % (m17[0] - t_inject, RF_AT_ONCE_S)
    assert len(dstar) == 1, "%d D-STAR text overs with %r, want 1: %s" % (len(dstar), body, texts)
    assert dstar[0] - t_inject <= RF_TEXT_OVER_S, "the D-STAR text over started %.3f s after the frame, bound %.1f s" % (dstar[0] - t_inject, RF_TEXT_OVER_S)
    assert len(events) == 1 and len(rows) == 1, "web: %d SSE events, %d history rows, want 1 each" % (len(events), len(rows))
    assert rows[0].get("room") == MODULE
    # no RF ack, on either port, whatever the transmit switch says
    assert rf.a.data_frames(t_inject) == [] and rf.b.data_frames(t_inject) == [], \
        "a frame was transmitted after a message with an id to a 7 character room name: %s" % [f[2] for f in rf.a.data_frames(t_inject) + rf.b.data_frames(t_inject)]
    after, seen = hub.wait_for(before, lambda now, was: now["pkt.text.accepted"] > was["pkt.text.accepted"])
    change = ds.delta(after, before)
    rx = modem_total(hub, "rf-a", "rx") - rx_before
    note(target, case, "RECORD", "counter deltas %s; dv_edge_modem_total{port=rf-a,result=rx} +%d" % (ds.moved(change), rx))
    assert seen, "dv_pkt_items_total{kind=text,result=accepted} did not move in %.0f s" % ds.POLL_S
    assert rx >= 1, "dv_edge_modem_total{port=rf-a,result=rx} did not move"


# ================================================================ 6b: RF transmit off

def test_dp15_6b_no_rf_transmit_while_tx_enable_is_false(rig, target, hub, rf, web_ctx):
    """data.md 15.5 (RF): "room text goes out as a group message only when tx_enable ... allow". Run shape: tx_enable false and
    no path to RF (DV-Router refuses `tx = true` and a path to RF without tx_enable, so this is the only way to run
    it). A web user posts a text to module B; the room still gets it (an M17 SMS and a D-STAR text over: RF is independent
    of the room), and for 12 s NOTHING arrives at either TNC except the KISS parameter frames written at connect;
    `dv_edge_modem_total{result=tx}` does not move."""
    case = "6b[tx_enable=false]"
    need_rx_only(rf)
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
    frames = rf.a.data_frames(t0) + rf.b.data_frames(t0)
    tx_after = modem_total(hub, "rf-a", "tx") + modem_total(hub, "rf-b", "tx")
    note(target, case, "RECORD", "POST %s; room got M17 %d, D-STAR %d; frames at the TNCs %d; other KISS commands %d; modem tx counter %s -> %s" % (
        status, len(m17), len(dstar), len(frames), len(rf.a.other_frames()) + len(rf.b.other_frames()), tx_before, tx_after))
    assert status == 202, "POST answered %s" % status
    assert m17 and dstar, "the room did not get the text (M17 %d, D-STAR %d): the silence at the TNC would prove nothing" % (len(m17), len(dstar))
    assert frames == [], "RF data frames with tx_enable false: %s" % [f[2] for f in frames]
    assert tx_after == tx_before, "dv_edge_modem_total{result=tx} moved: %s -> %s" % (tx_before, tx_after)


def test_dp15_6b_the_receive_only_port_never_transmits(rig, target, hub, rf, web_ctx):
    """The same on a port without `tx`: with tx_enable true and the path rf-out to rf-a, one room text is transmitted on rf-a
    and NEVER on rf-b (receive only, tx = false). Waits TX_SPACING_S after the last frame first (tx_per_min 2, burst 1)."""
    case = "6b[port rf-b rx only]"
    need_tx(rf, "6b")
    wait_tx_spacing(rf)
    body = ds.nonce_body("RXO")
    t0, _t1, status = post(web_ctx.posters[1], body)
    got = rf.a.wait_data(1, timeout=TX_WAIT_S, since=t0)
    time.sleep(SILENCE_S / 2)
    on_b = rf.b.data_frames(t0)
    on_a = rf.a.data_frames(t0)
    note(target, case, "RECORD", "POST %s; frames at rf-a %d, at rf-b %d" % (status, len(on_a), len(on_b)))
    assert status == 202
    assert got, "no frame at rf-a within %.0f s: the control of this case failed (see 6c)" % TX_WAIT_S
    assert on_b == [], "the receive-only port rf-b got a data frame: %s" % [f[2] for f in on_b]


# ================================================================ 6c: room text goes out as a group message

def test_dp15_6c_room_text_goes_out_as_one_group_message(rig, target, hub, rf, web_ctx):
    """data.md 15.5 (RF): "room text goes out as a group message only when `tx_enable` and the budget allow". With tx_enable, the
    port's tx switch, `tx_budget = {}` and the path rf-out, a web text of module B leaves as ONE KISS data frame at rf-a: a UI
    frame from N0TEST-1 (the station callsign) to APZDVR (tocall), no digipeaters (empty tx_path), information field
    `:XLX999B  :<text>` WITHOUT a message id (no ack, data.md 7.4). A second, 100 character text (30+ s later) is cut to at
    most 67 characters or refused: which one is recorded. Counters: the modem tx, and the scheduler's sent."""
    case = "6c"
    need_tx(rf, "6c")
    wait_tx_spacing(rf)
    body = ds.nonce_body("OUT")
    hub.settle()
    tx_before = modem_total(hub, "rf-a", "tx")
    sent_before = hub.total("dv_edge_rf_tx_total", result="sent")
    t0, _t1, status = post(web_ctx.posters[2], body)
    rf.a.wait_data(1, timeout=TX_WAIT_S, since=t0)
    time.sleep(DRAIN_S)
    frames = rf.a.data_frames(t0)
    assert status == 202, "POST answered %s" % status
    assert len(frames) == 1, "%d data frames at rf-a after one room text, want exactly 1 (within %.0f s)" % (len(frames), TX_WAIT_S + DRAIN_S)
    at, channel, raw = frames[0]
    msg = kiss_tnc.decode_message(raw)
    note(target, case, "RECORD", "frame %.3f s after the POST, %d bytes, channel %d: %s" % (at - t0, len(raw), channel, ax25.to_tnc2(raw)))
    assert msg is not None, "the transmitted frame is not an APRS message UI frame: %s" % raw.hex()
    assert channel == 0 and ax25.dvrouter_accepts_frame(raw)
    assert msg["src"] == STATION, "AX.25 source %r, want the station callsign %r" % (msg["src"], STATION)
    assert msg["dst"] == EDGE_TOCALL, "AX.25 destination %r, want tocall %r" % (msg["dst"], EDGE_TOCALL)
    assert msg["digis"] == [], "digipeaters %s, want none (tx_path is empty)" % msg["digis"]
    assert msg["addressee"] == ROOM_NAME, "message addressee %r, want %r" % (msg["addressee"], ROOM_NAME)
    assert msg["info"][:11] == b":XLX999B  :", msg["info"]
    assert msg["msg_id"] is None and b"{" not in msg["info"], "the group message carries an id: %r" % msg["info"]
    assert body in msg["text"], "text %r does not contain the posted %r" % (msg["text"], body)
    assert len(msg["text"]) <= TEXT_LIMIT
    assert rf.b.data_frames(t0) == [], "the receive-only port rf-b got a frame"
    tx_after, ok = hub.wait_total("dv_edge_modem_total", tx_before + 1, port="rf-a", result="tx")
    note(target, case, "RECORD", "dv_edge_modem_total{rf-a,tx} %s -> %s; dv_edge_rf_tx_total{sent} %s -> %s; other KISS commands %d" % (
        tx_before, tx_after, sent_before, hub.total("dv_edge_rf_tx_total", result="sent"), len(rf.a.other_frames())))
    assert ok, "dv_edge_modem_total{port=rf-a,result=tx} did not rise from %s" % tx_before
    assert rf.a.other_frames() == []
    # the 67 character bound: a 100 character text (record what happens)
    wait_tx_spacing(rf)
    long_text = ("LONG %s " % ds.nonce_body("L") + "x" * 100)[:100]
    t1, _t2, status_long = post(web_ctx.posters[0], long_text)
    long_frames = rf.a.wait_data(1, timeout=TX_WAIT_S, since=t1)
    if not long_frames:
        note(target, case, "RECORD", "a 100 character text: POST %s, NO frame within %.0f s (refused or dropped)" % (status_long, TX_WAIT_S))
    else:
        long_msg = kiss_tnc.decode_message(long_frames[0][2])
        note(target, case, "RECORD", "a 100 character text: POST %s, frame with %d text characters: %r" % (
            status_long, len(long_msg["text"]) if long_msg else -1, long_msg["text"] if long_msg else None))
        assert long_msg is not None and len(long_msg["text"]) <= TEXT_LIMIT, "the transmitted text has %d characters, bound %d" % (
            len(long_msg["text"]) if long_msg else -1, TEXT_LIMIT)


# ================================================================ 6d: never above the duty cycle

def budget_numbers(rf):
    """(duty fraction, window seconds, min gap seconds) of the rf-a budget: the explicit overrides, else the defaults of the
    port class afsk1200-shared (5 %, 600 s, 1000 ms; crates/lib/txsched/src/defaults.rs)."""
    b = rf.budget
    return (b.get("duty_pct", 5) / 100.0, float(b.get("window_secs", 600)), b.get("min_gap_ms", 1000) / 1000.0)


def sliding_max(times, width):
    """The most events inside any half-open window [t, t + width) that starts at an event."""
    times = sorted(times)
    best = 0
    for i, start in enumerate(times):
        best = max(best, sum(1 for t in times[i:] if t < start + width))
    return best


def test_dp15_6d_never_above_the_duty_cycle(rig, target, hub, rf, web_ctx):
    """data.md 15.5 (RF): "never above the duty cycle", `tx_per_min`. Six web texts from three accounts in about 8 s, then 140 s
    of watching rf-a (the scheduler sends the first at once and the rest paced, or drops them). Asserted: at most
    tx_per_min + tx_burst frames start in any rolling minute (the packet-count limit of the port), consecutive frames
    are at least the budget's min gap apart, and the sum of DV-Router's airtime upper bound (dvtest.airtime.afsk_airtime_ns,
    TXDELAY/TXtail included) of the frames in any budget window is within duty x window. Recorded: the frame times and the
    scheduler's drop counters. Only the class defaults are exposed by the snippet (`tx_budget = {}`); a low budget would
    also starve 6c, so no low budget run is written."""
    case = "6d"
    need_tx(rf, "6d")
    wait_tx_spacing(rf)
    duty, window, min_gap = budget_numbers(rf)
    hub.settle()
    drops_before = {k: hub.total("dv_edge_rf_tx_total", result=k) for k in ("sent", "delayed", "dropped_budget", "dropped_ttl", "dropped_queue", "dropped_quota", "dropped_exceeds_quota", "dropped_gap")}
    t0 = time.monotonic()
    statuses = []
    for i in range(6):
        poster = web_ctx.posters[i % 3]
        statuses.append(post(poster, ds.nonce_body("D%d" % i), pace=(i >= 3))[2])
        time.sleep(1.2)
    time.sleep(140.0 - 1.2 * 6)
    frames = rf.a.data_frames(t0)
    times = [t - t0 for t, _c, _f in frames]
    note(target, case, "RECORD", "POST statuses %s; %d frames at rf-a at +%s s; budget duty %.0f %% window %.0f s min gap %.1f s; port tx_per_min %d burst %d" % (
        statuses, len(frames), ["%.1f" % t for t in times], duty * 100, window, min_gap, rf.tx_per_min, rf.tx_burst))
    drops = {k: hub.total("dv_edge_rf_tx_total", result=k) - v for k, v in drops_before.items()}
    note(target, case, "RECORD", "dv_edge_rf_tx_total deltas (counters lag by up to 10 s) %s" % {k: v for k, v in drops.items() if v})
    # the room text bucket of the controller is 1 per 5 s with a burst of 4 (DV-Router answer 12): a 429 for the fifth post
    # 4.8 s into the burst is the product's own limit, not a defect; the first four must be accepted and nothing else may be refused
    assert all(s in (202, 429) for s in statuses) and statuses[:4] == [202] * 4, "POST statuses %s" % statuses
    assert frames, "no frame at rf-a at all: the control of this case failed (see 6c)"
    assert sliding_max(times, 60.0) <= rf.tx_per_min + rf.tx_burst, \
        "%d frames inside one minute, bound tx_per_min %d + burst %d" % (sliding_max(times, 60.0), rf.tx_per_min, rf.tx_burst)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(g >= min_gap - 0.1 for g in gaps), "frames %.2f s apart, the budget's min gap is %.2f s" % (min(gaps or [99]), min_gap)
    capacity_ns = duty * window * 1e9
    ns = [airtime.afsk_airtime_ns(len(f)) for _t, _c, f in frames]
    worst = max((sum(n for t, n in zip(times, ns) if start <= t < start + window) for start in times), default=0)
    note(target, case, "RECORD", "airtime upper bound of the frames in the worst window %.2f s of a capacity of %.2f s" % (worst / 1e9, capacity_ns / 1e9))
    assert worst <= capacity_ns, "airtime %.2f s inside one %.0f s window, capacity %.2f s" % (worst / 1e9, window, capacity_ns / 1e9)
    assert rf.b.data_frames(t0) == [], "the receive-only port rf-b got a frame"


# ================================================================ 6e: a frame heard on two ports is one item

def test_dp15_6e_a_frame_heard_on_two_ports_is_one_item(rig, target, hub, rf, web_ctx):
    """data.md 15.5 (RF): "a frame heard on two ports is one item". The same source, destination and information field is heard
    on rf-a and, 0.4 s later and with another via list, on rf-b (the worker ignores via, data.md 8.4): ONE text over, ONE M17
    SMS, ONE web row; `dv_pkt_rf_heard_ports_total` and `dv_pkt_rf_duplicates_total` rise. Then, 31 s after the first, a
    DIFFERENT message heard on rf-b alone is a second item."""
    case = "6e"
    body1, body2 = ds.nonce_body("TWO"), ds.nonce_body("ONE")
    PACER.wait(RF_SRC, "text")
    before = hub.settle()
    names = ("dv_pkt_rf_heard_ports_total", "dv_pkt_rf_duplicates_total")
    families = metrics.families(hub.text())
    ports_before = {n: hub.total(n) for n in names}
    f1a = rf_frame(body1, msg_id="5", digis=("WIDE1-1",))
    f1b = rf_frame(body1, msg_id="5", digis=("WIDE1-1*", "WIDE2-1"))
    f2 = rf_frame(body2, msg_id="6", digis=())
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, \
            dstar_listener(sutip(rig), module=MODULE, callsign="N0CALL") as dlist, \
            m17_client(sutip(rig), callsign="N0TEST", module=MODULE) as mlist:
        assert sse.ready()
        t1 = time.monotonic()
        assert rf.a.inject(f1a), "no connection to inject into at rf-a"
        time.sleep(0.4)
        assert rf.b.inject(f1b), "no connection to inject into at rf-b"
        pump_until(RF_TEXT_OVER_S + 6.0, [dlist, mlist], lambda: bool(m17_times(mlist, body1)) and bool(dstar_times(dlist, body1)))
        pump_for(DRAIN_S, dlist, mlist)
        one = (m17_times(mlist, body1), dstar_times(dlist, body1), sse_texts(sse, body1, t1))
        wait = t1 + 31.0 - time.monotonic()
        pump_for(max(wait, 0.0), dlist, mlist)
        t2 = time.monotonic()
        PACER.wait(RF_SRC, "text")
        assert rf.b.inject(f2)
        pump_until(RF_TEXT_OVER_S + 6.0, [dlist, mlist], lambda: bool(m17_times(mlist, body2)) and bool(dstar_times(dlist, body2)))
        pump_for(2.0, dlist, mlist)
        two = (m17_times(mlist, body2), dstar_times(dlist, body2), sse_texts(sse, body2, t2))
    rows = web.room_messages(web_ctx.reader, MODULE)
    rows1 = [r for r in rows if body1 in (r.get("body") or "")]
    rows2 = [r for r in rows if body2 in (r.get("body") or "")]
    after, _seen = hub.wait_for(before, lambda now, was: now["pkt.text.accepted"] > was["pkt.text.accepted"])
    counters = {n: hub.wait_total(n, ports_before[n] + 1, timeout=ds.POLL_S)[0] - ports_before[n] for n in names if n in families}
    change = ds.delta(after, before)
    note(target, case, "RECORD", "heard on two ports: M17 %d, D-STAR %d, SSE %d, rows %d; second message on rf-b: M17 %d, D-STAR %d, SSE %d, rows %d; "
         "counter deltas %s; rf counters %s (families present: %s)" % (
             len(one[0]), len(one[1]), len(one[2]), len(rows1), len(two[0]), len(two[1]), len(two[2]), len(rows2), ds.moved(change), counters,
             [n for n in names if n in families]))
    assert len(one[0]) == 1 and len(one[1]) == 1 and len(one[2]) == 1 and len(rows1) == 1, \
        "a frame heard on two ports gave M17 %d, D-STAR %d, SSE %d, history %d (want 1 each)" % (len(one[0]), len(one[1]), len(one[2]), len(rows1))
    assert len(two[0]) == 1 and len(two[1]) == 1 and len(rows2) == 1, \
        "the different message on rf-b gave M17 %d, D-STAR %d, history %d (want 1 each)" % (len(two[0]), len(two[1]), len(rows2))
    for name in names:
        if name in families:
            assert counters[name] >= 1, "%s did not rise (%s)" % (name, counters[name])
        else:
            note(target, case, "RECORD", "no %s family in the hub metrics" % name)
    assert rf.a.data_frames(t1) == [] and rf.b.data_frames(t1) == [], "a frame was transmitted for RF-heard messages (no retransmission, no RF ack)"
