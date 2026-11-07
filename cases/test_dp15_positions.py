# SPDX-License-Identifier: MIT
"""DV-Router position cases after DP7 (image dv-router:wip-ac750c7, commit ef4ecba): every admitted position and object is
PUBLIC at the precision the station sent; PRIVATE means DROPPED before publication ([data] positions = false,
[data] position_optout, an account's hide_position, matched by base callsign on every source). Specification:
DV-Router docs/design/data.md sections 5, 13.2, 13.4 and 9.5, web/README.md "Position map and position privacy",
docs/api/openapi-web.yaml (Position, PositionBody); case table, run commands, UNVERIFIED shapes: docs/dp7-cases.md.

  DP7-a  anonymous visibility: a position of a D-STAR D-PRS beacon, an M17 packet APRS position, a YSF GPS fix and a
         DMRG is in the ANONYMOUS public state (the data of the public #/positions page), on the public SSE data topic
         and in the operator feed; the public row has only the allow-listed keys; never in the user message history
  DP7-b  drop switches (three run shapes, the cases skip with the reason in the others):
         positions=false  nothing from any source is admitted     position_optout  one base callsign (SSID variants)
         is dropped for D-STAR, M17, YSF, DMR, objects, RF-heard APRS, RF transit and the APRS-IS uplink while another
         callsign passes     hide_position  the same drop for an account hidden through the operator API
         asserted by dv_pkt_items_total{result=optout} + dv_room_data_total{result=optout} AND by absence in the
         public state, the public SSE and the operator feed (and at the private aprsc for transit / uplink)
  DP7-c  ambiguity: an APRS position with 1..4 blanked minute digits sent in M17 packet mode and over RF (fake KISS
         TNC), and a D-PRS beacon with ambiguity 2 (also uplinked to the private aprsc with the digits blanked again), is
         carried with that level and printed at the SENT precision (the page's rule, compared with the generator's exact
         value); GPS sources (YSF GPS, DMRG, a full precision D-PRS beacon) never claim ambiguity
  DP7-d  object kill: an APRS object killed with '_' gives a coordinate-free public SSE event (name and state 0 only),
         and the object leaves the public listing

Run shapes and commands: docs/dp7-cases.md. Observations go to stdout and out/<run>/dp15-positions.txt ("RECORD" = what
was seen, "FINDING" = differs from the documented behaviour). Controller limits per base callsign (position and object
share 1 per 30 s) are respected by data_support.PACER; every case uses its OWN base callsigns (module constants below),
dropped items spend no credit (step 5 of the admission comes before the rate step).

NOT RUN ON A RIG yet (written against the sources and the design only). Shapes that could not be verified are listed in
docs/dp7-cases.md ("written against the described shape, UNVERIFIED"): the operator feed body field `ambiguity`, the
ident `stream` of a D-STAR D-PRS beacon for the uplink path, the SSE kill event row, the filtering of retained public rows by a
hide, the timing of the policy push.
"""
import contextlib
import os
import sys
import time
import tomllib
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprs_passcode, aprsis_client as ai, clients, dstar_sd, kiss_tnc, metrics, web  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import dmr, m17, ysf, ysf_gps  # noqa: E402

import data_support as ds  # noqa: E402
import position_support as ps  # noqa: E402
from data_support import (  # noqa: E402
    PACER, SseWatch, ambe, build_over, dstar_listener, hub_session, m17_client, pump_for, send_over, write_note)

pytestmark = pytest.mark.rig

MODULE = "B"
LOG = "dp15-positions.txt"
SETTING = "require_ident=%s" % os.environ.get("DVT_REQUIRE_IDENT", "true")
CONNECT_S = 90.0
COUNTER_WAIT_S = 30.0               # the controller and the worker publish their counters every 10 s
ABSENT_S = 6.0                      # how long "nothing appears" is watched after the counters moved
UPLINK_WAIT_S = 45.0                # a room position to the private aprsc, and an RF transit packet
UPLINK_ABSENT_S = 15.0
OPT_CALL = "N0OPT"                  # rig/config/dv-router/dp7-position-optout.toml (DMR: N0DMP)
OPT_DMR = "N0DMP"
HIDE_CALL = "N0HIDE"                # the account of the hide_position case
RF_TOCALL = "APDVT1"

# one base callsign per case and source: the controller admits 1 position per 30 s per base callsign
A_CALL = {"dstar": "N0PAD", "m17": "N0PAM", "ysf": "N0PAY", "dmr": "STR041"}
CONTROL_CALL = {"dstar": "N0PCD", "m17": "N0PCM", "ysf": "N0PCY", "dmr": "STR042", "m17-object": "N0PCO", "rf": "N0PCR", "rf-object": "N0PCB"}
OFF_CALL = {"dstar": "N0POD", "m17": "N0POM", "ysf": "N0POY", "dmr": "STR043", "m17-object": "N0POO", "rf": "N0POR", "rf-object": "N0POB"}
AMB_M17_CALL = {0: "N0PM0", 1: "N0PM1", 2: "N0PM2", 3: "N0PM3", 4: "N0PM4"}
AMB_RF_CALL = {1: "N0PR1", 2: "N0PR2", 3: "N0PR3", 4: "N0PR4"}
AMB_DPRS_CALL = "N0PDA"
UPLINK_CONTROL_CALL = "N0PUC"
TRANSIT_CONTROL_CALL = "N0PTC"
OBJECT_REPORTER = "N0PDO"
OBJECT_NAME = "DVT7OBJ"
SOURCES = ["dstar", "m17", "ysf", "dmr", "m17-object", "rf", "rf-object"]
OBJECT_SOURCES = ("m17-object", "rf-object")
RF_SOURCES = ("rf", "rf-object")
HIDE_SOURCES = ["dstar", "m17", "ysf", "m17-object"]


# ---------------------------------------------------------------- notes, fixtures

def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics with the data worker on (skips with the reason otherwise)."""
    yield from hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    """The web side: the operator session, a reader (a user: the message history must never carry a position), a factory
    of anonymous clients."""
    return types.SimpleNamespace(op=web_operator, reader=web_user("N0DPR", may_tx=False).client, anon=web_client, user=web_user)


@pytest.fixture(scope="module")
def ledger():
    """What the DP7-a cases saw per source (state row, operator row): read by the DP7-c case of the GPS sources."""
    return {}


def read_config(target):
    """The RENDERED dv-router.toml of this run as a dict ({} when it is not readable)."""
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def shape_of(doc):
    """The run shape the cases need, from a rendered config dict: positions switch, opt-out base callsigns, the RF port rf-a (room
    bound), the transit path to APRS-IS, the room-to-APRS-IS uplink path and the APRS-IS edge."""
    data = doc.get("data") or {}
    edges = doc.get("edges") or {}
    ax = edges.get("ax25") if isinstance(edges.get("ax25"), dict) else {}
    port = next((p for p in (ax.get("port") or []) if p.get("name") == "rf-a" and p.get("kind") == "kiss-tcp"), None)
    rf = bool(ax.get("enable")) and port is not None and port.get("room") == MODULE and not ax.get("tx_enable")
    paths = list(data.get("path") or [])
    return types.SimpleNamespace(
        known=bool(doc), positions=data.get("positions", True) is not False,
        optout={ps.base_call(c) for c in (data.get("position_optout") or [])},
        rf=rf, rf_port=int(port["port"]) if rf and port.get("port") else None,
        aprsis=bool((edges.get("aprsis") or {}).get("enable")),
        igate=any(p.get("from") == "edge:ax25/rf-a" and p.get("to") == "edge:aprsis" and "transit" in (p.get("types") or []) for p in paths),
        uplink=any(p.get("from") == "room:%s" % MODULE and p.get("to") == "edge:aprsis" and "position" in (p.get("types") or []) for p in paths))


@pytest.fixture(scope="module")
def shape(target):
    return shape_of(read_config(target))


@pytest.fixture(scope="module")
def rf(target, shape, hub):
    """The fake KISS TNC rf-a (the AX.25 edge connects to it as a kiss-tcp client). Skips without dp7-rf.toml in the run."""
    if not shape.rf:
        pytest.skip("the rendered config has no receive-only AX.25 edge with the room-bound port rf-a: add rig/config/dv-router/dp7-rf.toml "
                    "(instead of dp15-rf.toml) to DVT_SUT_CONFIG_EXTRA")
    tnc = kiss_tnc.FakeKissTnc("0.0.0.0", shape.rf_port, name="rf-a")
    tnc.start()
    try:
        if not tnc.wait_connected(CONNECT_S):
            pytest.fail("the AX.25 edge did not connect to the fake TNC rf-a (port %s) in %.0f s" % (shape.rf_port, CONNECT_S))
        note(target, "rf", "RECORD", "AX.25 edge connected to the fake TNC rf-a (port %s, room %s)" % (shape.rf_port, MODULE))
        yield tnc
    finally:
        tnc.stop()


@pytest.fixture(scope="module")
def aprsc(shape, hub, target):
    """The private aprsc (profile aprs) with the DV-Router edge logged in and verified; skips with the reason otherwise."""
    if os.environ.get("DVT_APRS") != "on":
        pytest.skip("the private aprsc is not part of this rig: run scripts/run.sh --profile web,aprs (DVT_APRS is not 'on')")
    host = os.environ.get("DVT_APRSC", "")
    if not host:
        pytest.skip("DVT_APRSC (the aprsc address) is not set")
    if not shape.aprsis:
        pytest.skip("the rendered config has no enabled [edges.aprsis]: add rig/config/dv-router/dp15-aprsis.toml")
    import socket
    end = time.monotonic() + 30.0
    while True:
        try:
            socket.create_connection((host, ai.PORT), timeout=3.0).close()
            break
        except OSError as exc:
            if time.monotonic() >= end:
                pytest.skip("the aprsc service at %s:%d does not answer (%s)" % (host, ai.PORT, exc))
            time.sleep(1.0)
    end = time.monotonic() + 90.0
    while "dv_edge_aprsis_verified" in metrics.families(hub.text()) and hub.total("dv_edge_aprsis_verified") < 1:
        if time.monotonic() >= end:
            pytest.fail("the DV-Router APRS-IS edge is not logged in and verified on the private aprsc after 90 s (dv_edge_aprsis_verified = 0)")
        time.sleep(2.0)
    return host


@contextlib.contextmanager
def aprsc_login(host):
    """The tester's own verified login on the private aprsc; the filter lets it read every N0.. / STR.. source."""
    with ai.AprsisClient(host, login=aprs_passcode.TESTER_LOGIN, filter="p/N0/STR") as client:
        resp = client.connect()
        assert resp["verified"], resp
        yield client


# ---------------------------------------------------------------- senders: one position (or object) per call

def pace_before(call, spend, kinds=("position",)):
    if spend:
        for kind in kinds:
            PACER.wait(call, kind)


def pace_after(call, spend, kinds=("position",)):
    if spend:
        for kind in kinds:
            PACER.offered(call, kind)


def send_dstar(ctx, call, lat, lon, level=0, comment="dp7", spend=True, **_ignored):
    """A D-STAR talker (MY = the base of `call`, linked as a DExtra client of module B) sends an over with the TX message and
    a D-PRS GPS-A line `CALL>API510,DSTAR*:!<position>` (the line's SRC is `call`, an SSID allowed). Source 1."""
    base = ps.base_call(call)
    pace_before(base, spend, ("status", "position"))
    payload = "%s>API510,DSTAR*:%s" % (call, ps.position_info(lat, lon, level, "/>", comment))
    fields = dstar_sd.text_gps_fields("DVT DP7 %04d" % (int(time.time()) % 10000), payload, superframes=3)
    _sid, packets = build_over(base, fields, ambe(len(fields), "dp7/" + call))
    with dstar_listener(ctx.rig.xlxd_sut, module=MODULE, callsign=base) as talker:
        send_over(talker, packets)
        pace_after(base, spend, ("status", "position"))
        pump_for(2.0, talker)


def send_m17_info(ctx, call, info, spend=True):
    """An M17 packet-mode APRS payload (specifier 0x02): the TNC2 text `CALL>APDVT1:<info>` from a client linked as the base."""
    base = ps.base_call(call)
    pace_before(base, spend)
    packet = m17.m17p_packet(0x02, ps.tnc2(call, info).encode("ascii"), src=call)
    with m17_client(ctx.rig.xlxd_sut, callsign=base, module=MODULE) as sender:
        sender.client.send(sender.dest, packet)
        pace_after(base, spend)
        pump_for(1.0, sender)


def send_m17(ctx, call, lat, lon, level=0, comment="dp7", spend=True, **_ignored):
    send_m17_info(ctx, call, ps.position_info(lat, lon, level, "/>", comment), spend)


def send_m17_object(ctx, call, lat, lon, level=0, comment="dp7", spend=True, name=OBJECT_NAME, live=True, **_ignored):
    send_m17_info(ctx, call, ps.object_info(name, lat, lon, live, level, "/>", comment), spend)


def send_ysf(ctx, call, lat, lon, spend=True, **_ignored):
    """A YSF client of module B sends an over of 16 packets whose FN 6/7 frames carry the GPS record twice. Source 3."""
    base = ps.base_call(call)
    pace_before(base, spend)
    client = ysf.YsfClient("0.0.0.0", callsign=base, module=MODULE)
    try:
        assert client.link(ctx.rig.xlxd_sut), "YSF link to %s failed" % ctx.rig.xlxd_sut
        packets = ysf_gps.gps_stream_packets([ysf.SILENCE] * 80, base, lat, lon, ysf_gps.RADIO_FT1D, dgid=client.dgid)
        client.keepalive()
        client.client.send(client.dest, packets[0])
        client.client.pump(0.3)
        client.client.paced(client.dest, packets[1:], ysf.PACKET_GAP)
        pace_after(base, spend)
        client.client.pump(1.0)
    finally:
        client.close()


def send_dmr(ctx, call, lat, lon, spend=True, **_ignored):
    """A DMR client with the id the rig's registry maps to `call` sends a voice stream and one DMRG inside it. Source 4."""
    rid = ps.dmr_id_for(call)
    assert rid is not None, "no DMR id for %s in rig/config/dmrid.dat" % call
    base = ps.base_call(call)
    pace_before(base, spend)
    client = dmr.DmrClient("0.0.0.0", dmrid=rid, module=MODULE)
    try:
        assert client.link(ctx.rig.xlxd_sut), "DMR link to %s failed" % ctx.rig.xlxd_sut
        frames = signals.prng_frames(2, 30, seed="dp7/" + base)
        dmr.talk_with_side_packets(client, frames, {3: [dmr.dmrg_packet(rid, lat, lon)]}, stream_id=clients.fresh_stream_id("dmr"))
        pace_after(base, spend)
    finally:
        client.close()


def send_rf_info(ctx, call, info, spend=True):
    """The fake TNC rf-a hears a UI frame `CALL>APDVT1,WIDE1-1:<info>`. Identical frames are deduplicated by the worker: every
    call of a case sends a different information field."""
    base = ps.base_call(call)
    pace_before(base, spend)
    assert ctx.rf is not None and ctx.rf.inject(kiss_tnc.ui_frame(call, RF_TOCALL, info, digis=("WIDE1-1",))), \
        "the fake TNC rf-a has no connection to inject into"
    pace_after(base, spend)


def send_rf(ctx, call, lat, lon, level=0, comment="dp7", spend=True, **_ignored):
    send_rf_info(ctx, call, ps.position_info(lat, lon, level, "/>", comment), spend)


def send_rf_object(ctx, call, lat, lon, level=0, comment="dp7", spend=True, name=OBJECT_NAME, live=True, **_ignored):
    send_rf_info(ctx, call, ps.object_info(name, lat, lon, live, level, "/>", comment), spend)


SENDERS = {"dstar": send_dstar, "m17": send_m17, "ysf": send_ysf, "dmr": send_dmr, "m17-object": send_m17_object,
           "rf": send_rf, "rf-object": send_rf_object}
KIND = {s: ("object" if s in OBJECT_SOURCES else "position") for s in SOURCES}


def make_ctx(request, rig, source):
    """The context of a sender: the rig and, for the RF sources only, the fake TNC (the `rf` fixture skips the case with the reason
    when the run has no dp7-rf.toml)."""
    return types.SimpleNamespace(rig=rig, rf=request.getfixturevalue("rf") if source in RF_SOURCES else None)


# ---------------------------------------------------------------- watching the surfaces

@contextlib.contextmanager
def feeds(web_ctx):
    """The public SSE (anonymous) open and the operator feed baseline, read before the send."""
    baseline = ps.operator_baseline(web_ctx.op)
    with SseWatch(web_ctx.anon(), ps.PUBLIC_EVENTS, seconds=240.0) as pub:
        assert pub.ready(), "the public SSE gave no hello: %s" % pub.error
        yield types.SimpleNamespace(pub=pub, baseline=baseline, t0=time.monotonic())


def spatial_pred(call, item_type="position", name=None, state=None):
    def pred(row):
        body = row.get("body") if isinstance(row.get("body"), dict) else {}
        return (ps.is_spatial(row) and row.get("item_type") == item_type and ps.same_base(row.get("callsign", ""), call)
                and (name is None or str(body.get("name", "")).strip() == name)
                and (state is None or body.get("state") == state))
    return pred


def collect(web_ctx, fd, call, item_type="position", name=None, timeout=ps.PUBLIC_WAIT_S):
    """Waits for a position / object of the base callsign on the three surfaces; each member is None when it never showed:
    sse (the public data event), state (the anonymous public state row), op (the operator feed row)."""
    pred = spatial_pred(call, item_type, name, state=1 if item_type == "object" else None)
    hit = fd.pub.wait_data(pred, timeout, since=fd.t0)
    state, _rows = ps.wait_public_row(web_ctx.anon(), call, timeout, room=MODULE, item_type=item_type, name=name,
                                      pred=lambda r: pred(r))
    ops, op_rows = ps.wait_operator_rows(web_ctx.op, fd.baseline, pred, timeout=ps.OPERATOR_WAIT_S)
    return types.SimpleNamespace(sse=None if hit is None else hit[1], sse_at=None if hit is None else hit[0], state=state,
                                 op=ops[0] if ops else None, op_rows=op_rows)


def assert_visible(got, label):
    assert got.sse is not None, "%s: no position / object event of the base callsign on the ANONYMOUS public SSE data topic" % label
    assert got.state is not None, "%s: not in the anonymous public state (rooms[].positions, the data of #/positions)" % label
    assert got.op is not None, "%s: not in the operator feed" % label
    ps.check_public_row(got.sse)
    ps.check_public_row(got.state)


def assert_absent(web_ctx, fd, calls, label, window=ABSENT_S, item_type=None, name=None):
    """After `window` s: no position / object of any base callsign in `calls` on the public SSE, in the anonymous public state or
    in the operator feed (the feeds are read from the baseline taken before the send)."""
    time.sleep(window)
    calls = list(calls)
    on_sse = [o for c in calls for o in ps.sse_rows(fd.pub, c, fd.t0, item_type, name)]
    on_state = [r for c in calls for r in ps.spatial_rows(ps.public_rows(web_ctx.anon(), MODULE), c, item_type, name)]
    _hits, rows = ps.wait_operator_rows(web_ctx.op, fd.baseline, lambda r: False, timeout=1.0)
    on_op = [r for c in calls for r in ps.spatial_rows(rows, c, item_type, name)]
    assert not on_sse, "%s: a dropped position / object reached the public SSE: %s" % (label, on_sse[:2])
    assert not on_state, "%s: a dropped position / object is in the anonymous public state: %s" % (label, on_state[:2])
    assert not on_op, "%s: a dropped position / object is in the operator feed: %s" % (label, on_op[:2])


def user_history_has_no_spatial(web_ctx):
    rows = web.room_messages(web_ctx.reader, MODULE)
    return not [r for r in rows if ps.is_spatial(r)]


class Drops:
    """The drop counters before a send and after: worker dv_pkt_items_total{kind,result=optout} and controller
    dv_room_data_total{result=optout} (the controller's denied/13)."""

    def __init__(self, hub, kind):
        self.hub, self.kind = hub, kind
        self.items = hub.total("dv_pkt_items_total", kind=kind, result="optout")
        self.room = hub.total("dv_room_data_total", result="optout")

    def wait(self, minimum=1, timeout=COUNTER_WAIT_S):
        """(items delta, room delta, reached): polls both counters (10 s clocks) until both rose by `minimum`."""
        end = time.monotonic() + timeout
        while True:
            items = self.hub.total("dv_pkt_items_total", kind=self.kind, result="optout") - self.items
            room = self.hub.total("dv_room_data_total", result="optout") - self.room
            if (items >= minimum and room >= minimum) or time.monotonic() >= end:
                return items, room, items >= minimum and room >= minimum
            time.sleep(2.0)


def assert_dropped_counters(target, case, drops, minimum=1):
    items, room, reached = drops.wait(minimum)
    note(target, case, "RECORD", "optout counters: dv_pkt_items_total{kind=%s} +%d, dv_room_data_total +%d" % (drops.kind, items, room))
    assert reached, ("the drop counters did not rise by %d in %.0f s: dv_pkt_items_total{kind=%s,result=optout} +%d, "
                     "dv_room_data_total{result=optout} +%d" % (minimum, COUNTER_WAIT_S, drops.kind, items, room))
    if items != minimum or room != minimum:
        note(target, case, "RECORD", "more than %d drops counted (a stream may offer one fix twice)" % minimum)


def dropped_send(ctx, target, web_ctx, hub, case, source, call, label, **kw):
    """One send that must be dropped: counters (optout) rise, nothing on any surface for the base callsign."""
    kind = KIND[source]
    drops = Drops(hub, kind)
    with feeds(web_ctx) as fd:
        SENDERS[source](ctx, call, kw.pop("lat", ps.LAT), kw.pop("lon", ps.LON), spend=False, **kw)
        assert_dropped_counters(target, case, drops)
        assert_absent(web_ctx, fd, [call], "%s %s" % (case, label), item_type=kind)


# ================================================================ DP7-a: anonymous visibility

A_EXPECT = {      # source -> (position source code, symbol, comment); None: not asserted
    "dstar": (1, "/>", None), "m17": (5, "/>", None), "ysf": (3, "/[", "FT-1D"), "dmr": (4, "/-", None)}


@pytest.mark.parametrize("source", ["dstar", "m17", "ysf", "dmr"])
def test_dp7_a_position_is_public(rig, target, hub, web_ctx, shape, ledger, source):
    """data.md 13.4 (DP7): "every admitted position and object is public at received precision in its room: RF-heard APRS,
    D-STAR D-PRS/GPS-A, DMR, YSF, NXDN and M17 reports". One fix per source (10.5 N 20.25 E) from its OWN base callsign
    (module constants A_CALL): the position is in the ANONYMOUS public state (rooms[].positions), on the anonymous public
    SSE data topic and in the operator feed, with the allow-listed keys only (openapi Position: no radio id, account, origin),
    the right source code, symbol and ambiguity 0 (a fix claims no ambiguity), and it is NEVER in the user message history."""
    case = "DP7-a[%s]" % source
    if not shape.positions:
        pytest.skip("this run has [data] positions = false (dp7-positions-off.toml): the visibility cases need a run without it")
    call = A_CALL[source]
    assert call not in shape.optout, "%s is in position_optout" % call
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    with feeds(web_ctx) as fd:
        SENDERS[source](ctx, call, ps.LAT, ps.LON)
        got = collect(web_ctx, fd, call)
        history_clean = user_history_has_no_spatial(web_ctx)
    code, symbol, comment = A_EXPECT[source]
    note(target, case, "RECORD", "public SSE %s (%.3f s after the send), public state %s, operator feed %s; protocol %r, ident %r, via %r; "
         "operator body keys %s; history without positions: %s" % (
             got.sse is not None, (got.sse_at or 0) - fd.t0, got.state is not None, got.op is not None,
             (got.state or {}).get("protocol"), (got.state or {}).get("ident"), (got.state or {}).get("via"),
             sorted((got.op or {}).get("body") or {}), history_clean))
    assert_visible(got, case)
    for surface, row in (("public SSE", got.sse), ("public state", got.state), ("operator feed", got.op)):
        ps.check_position(row, source=code, symbol=symbol, comment=comment,
                          ambiguity=0 if "ambiguity" in row["body"] or surface != "operator feed" else None)
    if "ambiguity" not in got.op["body"]:
        note(target, case, "FINDING", "the operator feed body has no `ambiguity` field (data.md 5 / 13.2 describe the 29 byte layout with it): %s" % sorted(got.op["body"]))
    assert history_clean, "GET /api/v1/user/rooms/%s/messages carried a position row (web.md / data.md 13.2: never)" % MODULE
    ledger[source] = types.SimpleNamespace(state=got.state, op=got.op, sse=got.sse)


# ================================================================ DP7-b: the drop switches

@pytest.mark.parametrize("source", SOURCES)
def test_dp7_b_positions_false_drops_everything(request, rig, target, hub, web_ctx, shape, source):
    """data.md 13.4 (DP7): "[data] positions = false drops all positions and objects". Run with rig/config/dv-router/
    dp7-positions-off.toml INSTEAD of dp7-position-optout.toml. Every source (D-STAR D-PRS, M17 packet position and object,
    YSF GPS, DMRG; RF-heard position and object when the run has dp7-rf.toml) sends one item: the controller answers denied/13
    (dv_room_data_total{result=optout} and dv_pkt_items_total{kind,result=optout} rise) and NOTHING appears on the public state, the
    public SSE or the operator feed. The D-STAR over also carries the TX message: its `status` IS admitted (the control that
    the data path is open)."""
    case = "DP7-b[positions=false][%s]" % source
    if shape.positions:
        pytest.skip("[data] positions is not false in this run: add rig/config/dv-router/dp7-positions-off.toml")
    ctx = make_ctx(request, rig, source)
    call = OFF_CALL[source]
    if source != "dstar":
        dropped_send(ctx, target, web_ctx, hub, case, source, call, source, **({"name": "DVT7OFF"} if KIND[source] == "object" else {}))
        return
    drops = Drops(hub, "position")
    with feeds(web_ctx) as fd:
        send_dstar(ctx, call, ps.LAT, ps.LON, spend=True)
        status = fd.pub.wait_data(lambda o: o.get("item_type") == "status" and ps.same_base(o.get("callsign", ""), call),
                                  ds.STATUS_CEILING_S + 8.0, since=fd.t0)
        assert_dropped_counters(target, case, drops)
        assert_absent(web_ctx, fd, [call], case, item_type="position")
    note(target, case, "RECORD", "D-STAR TX message status event: %s" % (None if status is None else status[1].get("body")))
    assert status is not None, "the D-STAR TX message gave no status although positions are off (the control failed)"


@pytest.mark.parametrize("source", SOURCES)
def test_dp7_b_position_optout(request, rig, target, hub, web_ctx, shape, source):
    """data.md 13.4 and config.md data.position_optout: the base callsign N0OPT (and N0DMP for DMR) is dropped on EVERY source,
    SSID variants included, while another callsign still passes. Per source: the control (its own callsign) is public on the
    state, the SSE and the operator feed; then N0OPT and N0OPT-9 (N0DMP for DMR; no SSID in YSF / DMR) are dropped:
    dv_pkt_items_total{kind,result=optout} and dv_room_data_total{result=optout} rise, and nothing of the opted-out base appears.
    Sources: D-STAR D-PRS, M17 packet position and object, YSF GPS, DMRG, RF-heard position and object (fake TNC)."""
    case = "DP7-b[optout][%s]" % source
    if not shape.positions:
        pytest.skip("this run has positions = false: the opt-out cases need dp7-position-optout.toml instead")
    if {OPT_CALL, OPT_DMR} - shape.optout:
        pytest.skip("position_optout of the rendered config is %s: add rig/config/dv-router/dp7-position-optout.toml" % sorted(shape.optout))
    ctx = make_ctx(request, rig, source)
    kind = KIND[source]
    name = "DVT7CTL" if kind == "object" else None
    with feeds(web_ctx) as fd:                                                   # the control passes
        SENDERS[source](ctx, CONTROL_CALL[source], ps.LAT, ps.LON, **({"name": name} if name else {}))
        got = collect(web_ctx, fd, CONTROL_CALL[source], item_type=kind, name=name)
    assert_visible(got, case + " control")
    blocked = OPT_DMR if source == "dmr" else OPT_CALL
    variants = [blocked] if source in ("ysf", "dmr") else [blocked, blocked + "-9"]
    for variant in variants:
        dropped_send(ctx, target, web_ctx, hub, "%s %s" % (case, variant), source, variant, variant,
                     **({"name": "DVT7OPT"} if kind == "object" else {}))


def set_hide_position(web_ctx, callsign, value):
    """PATCH /api/v1/operator/accounts/{callsign} {hide_position} (an operator session; the account's own logins are revoked by the
    update, which this case does not need), then reads the account back."""
    resp = web_ctx.op.request("PATCH", "/api/v1/operator/accounts/%s" % callsign, json_body={"hide_position": bool(value)}, expect=None)
    assert resp.status == 200, "PATCH account %s hide_position=%s: HTTP %d %s" % (callsign, value, resp.status, web_ctx.op.scrub(resp.text())[:120])
    account = web_ctx.op.get("/api/v1/operator/accounts/%s" % callsign, expect=200).json()
    assert account.get("hide_position") is bool(value), "the account reads hide_position=%r after the PATCH to %s" % (account.get("hide_position"), value)


def test_dp7_b_account_hide_position(rig, target, hub, web_ctx, shape):
    """data.md 13.2 / 13.4 and web/README.md: an account's hide_position (set by an operator through the account API) drops every
    position and object of the account's BASE callsign. Steps with the account N0HIDE (created through the operator): a position
    (M17 packet) is public; the operator sets hide_position; the retained row leaves the anonymous public state (the web process
    filters retained rows with its current hidden set); then D-STAR, M17, YSF and an M17 object of N0HIDE are dropped (the
    optout counters rise, nothing on the surfaces); the flag is cleared and a new position (marked by its comment) is public again
    (a flag change applies to new items)."""
    case = "DP7-b[hide_position]"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    web_ctx.user(HIDE_CALL, may_tx=False)
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    try:
        with feeds(web_ctx) as fd:
            send_m17(ctx, HIDE_CALL, ps.LAT, ps.LON, comment="before")
            got = collect(web_ctx, fd, HIDE_CALL)
        assert_visible(got, case + " before the flag")
        set_hide_position(web_ctx, HIDE_CALL, True)
        time.sleep(3.0)                                                          # the policy is pushed on web/position-policy-updates
        gone = ps.wait_gone(web_ctx.anon(), HIDE_CALL, room=MODULE, item_type="position")
        note(target, case, "RECORD", "retained row left the anonymous public state after the flag: %s" % gone)
        assert gone, "the retained position of a hidden account is still in the anonymous public state"
        for source in HIDE_SOURCES:
            dropped_send(ctx, target, web_ctx, hub, "%s[%s]" % (case, source), source, HIDE_CALL, source,
                         **({"name": "DVT7HID"} if KIND[source] == "object" else {}))
        set_hide_position(web_ctx, HIDE_CALL, False)
        time.sleep(3.0)
        with feeds(web_ctx) as fd:
            send_m17(ctx, HIDE_CALL, ps.LAT, ps.LON, comment="again")
            again = collect(web_ctx, fd, HIDE_CALL)
        note(target, case, "RECORD", "after clearing the flag a new position is public: sse %s state %s op %s" % (
            again.sse is not None, again.state is not None, again.op is not None))
        assert_visible(again, case + " after clearing the flag")
        assert again.sse["body"].get("comment") == "again", "the SSE event after clearing the flag is not the new position: %s" % again.sse
        fresh, _rows = ps.wait_public_row(web_ctx.anon(), HIDE_CALL, room=MODULE, item_type="position",
                                          pred=lambda r: r["body"].get("comment") == "again")
        assert fresh is not None, "the new position (comment `again`) never replaced the retained one in the anonymous public state"
    finally:
        try:
            set_hide_position(web_ctx, HIDE_CALL, False)
        except (AssertionError, web.WebError) as exc:
            note(target, case, "RECORD", "could not clear hide_position at the end: %s" % exc)


def aprsc_lines(client, mark, call):
    return client.matching(lambda p: ps.same_base(p["src"], call), mark)


def test_dp7_b_rf_transit(rig, target, hub, web_ctx, shape, rf, aprsc):
    """data.md 8.2, 9.3 and 13.4: the worker applies the spatial policy to whole-packet RF-to-APRS-IS TRANSIT too (the path
    dp7-igate of rig/config/dv-router/dp7-rf-igate.toml; no item, no controller). The fake TNC hears a position and an object of
    the control callsign N0PTC and, in the optout shape, of N0OPT-7: the control reaches the private aprsc (the tester sees a line
    from N0PTC), the opted-out one does not (UPLINK_ABSENT_S of watching, after the control proved the path). In the positions=false
    shape NOTHING is gated. The room side of the same frames is DP7-b above."""
    case = "DP7-b[rf transit]"
    if not shape.igate:
        pytest.skip("no transit path edge:ax25/rf-a -> edge:aprsis: add rig/config/dv-router/dp15-aprsis.toml and dp7-rf-igate.toml")
    optout_shape = shape.positions and OPT_CALL in shape.optout
    if shape.positions and not optout_shape:
        pytest.skip("neither positions=false nor position_optout is configured in this run")
    ctx = types.SimpleNamespace(rig=rig, rf=rf)
    with aprsc_login(aprsc) as tester:
        mark = tester.mark()
        info_a = ps.position_info(ps.LAT, ps.LON, 0, "/>", "dp7 transit %04d" % (int(time.time()) % 10000))
        info_o = ps.object_info("DVT7TRN", ps.LAT, ps.LON, True, 0, "/>", "dp7")
        if optout_shape:
            send_rf_info(ctx, TRANSIT_CONTROL_CALL + "-7", info_a)
            send_rf_info(ctx, TRANSIT_CONTROL_CALL + "-8", info_o, spend=False)         # an object of the same base: shares no frame
            hit = tester.wait_packet(lambda p: ps.same_base(p["src"], TRANSIT_CONTROL_CALL) and info_a in p["info"], UPLINK_WAIT_S, since=mark)
            note(target, case, "RECORD", "control position at the aprsc: %s" % (None if hit is None else hit[2]))
            assert hit is not None, "the control position of %s never reached the private aprsc through the transit path (the check proves nothing)" % TRANSIT_CONTROL_CALL
            obj_hit = tester.wait_packet(lambda p: ps.same_base(p["src"], TRANSIT_CONTROL_CALL) and info_o in p["info"], 10.0, since=mark)
            note(target, case, "RECORD", "control object at the aprsc: %s" % (None if obj_hit is None else obj_hit[2]))
            blocked = [(OPT_CALL + "-7", ps.position_info(ps.LAT, ps.LON, 0, "/>", "dp7 opt %04d" % (int(time.time()) % 10000))),
                       (OPT_CALL + "-9", ps.object_info("DVT7OPT", ps.LAT, ps.LON, True, 0, "/>", "dp7"))]
        else:
            blocked = [(TRANSIT_CONTROL_CALL + "-7", info_a), (TRANSIT_CONTROL_CALL + "-8", info_o)]
        mark2 = tester.mark()
        for call, info in blocked:
            send_rf_info(ctx, call, info, spend=False)
        time.sleep(UPLINK_ABSENT_S)
        lines = [p["raw"] for _t, p in tester.matching(lambda p: ps.same_base(p["src"], OPT_CALL if optout_shape else TRANSIT_CONTROL_CALL), mark2)]
    note(target, case, "RECORD", "lines of the blocked callsign at the aprsc: %s" % lines)
    assert not lines, "a transit packet of a dropped callsign reached the private aprsc: %s" % lines


def test_dp7_b_uplink_to_aprsis(rig, target, hub, web_ctx, shape, aprsc):
    """data.md 9.5 and 13.4: a position that was NOT admitted is never uplinked. A D-STAR D-PRS beacon of the control callsign N0PUC
    is admitted and uplinked (the tester on the private aprsc sees `N0PUC>APZDVR,TCPIP*,q..:` ...; the path dp7-aprsis-up needs
    `min_ident = stream`: UNVERIFIED that the rig's D-STAR talker reaches ident stream, a missing control is reported as such);
    then the beacon of the opted-out N0OPT is dropped at the controller and never uplinked (watched UPLINK_ABSENT_S after the
    control proved the path). positions=false shape: nothing is uplinked."""
    case = "DP7-b[uplink]"
    if not shape.uplink:
        pytest.skip("no path room:B -> edge:aprsis: add rig/config/dv-router/dp15-aprsis.toml and dp7-aprsis-up.toml")
    optout_shape = shape.positions and OPT_CALL in shape.optout
    if shape.positions and not optout_shape:
        pytest.skip("neither positions=false nor position_optout is configured in this run")
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    with aprsc_login(aprsc) as tester:
        mark = tester.mark()
        if optout_shape:
            send_dstar(ctx, UPLINK_CONTROL_CALL, ps.LAT, ps.LON, comment="dp7")
            hit = tester.wait_packet(lambda p: ps.same_base(p["src"], UPLINK_CONTROL_CALL), UPLINK_WAIT_S, since=mark)
            note(target, case, "RECORD", "control uplink line at the aprsc: %s" % (None if hit is None else hit[2]))
            assert hit is not None, ("the control beacon of %s was not uplinked within %.0f s (ident stream not reached, or the path "
                                     "dp7-aprsis-up is not served): the absence check proves nothing" % (UPLINK_CONTROL_CALL, UPLINK_WAIT_S))
        mark2 = tester.mark()
        blocked = OPT_CALL if optout_shape else UPLINK_CONTROL_CALL
        drops = Drops(hub, "position")
        send_dstar(ctx, blocked, ps.LAT, ps.LON, comment="dp7", spend=False)
        assert_dropped_counters(target, case, drops)
        time.sleep(UPLINK_ABSENT_S)
        lines = [p["raw"] for _t, p in aprsc_lines(tester, mark2, blocked)]
    assert not lines, "a dropped position was uplinked to the private aprsc: %s" % lines


# ================================================================ DP7-c: ambiguity

def ambiguity_case(target, web_ctx, case, send, call, level, source_code):
    """Sends the APRS position of ambiguity `level` and checks every surface: the level in the body (public SSE, public state,
    operator feed), the box centre, and the text of the page equal to the text derived from the generator's exact value."""
    lat_exact, lon_exact, amb = ps.exact_axes(ps.APRS_LAT, ps.APRS_LON, level)
    assert amb == level
    with feeds(web_ctx) as fd:
        send(call, ps.APRS_LAT, ps.APRS_LON, level)
        got = collect(web_ctx, fd, call)
    assert_visible(got, case)
    shown = {}
    for surface, row in (("public SSE", got.sse), ("public state", got.state), ("operator feed", got.op)):
        ps.check_position(row, source=source_code, lat_e7=ps.centre_e7(lat_exact, level), lon_e7=ps.centre_e7(lon_exact, level), tol=ps.CENTRE_E7)
        shown[surface] = ps.check_displayed(row, lat_exact, lon_exact, level)
    assert len(set(shown.values())) == 1, "the surfaces disagree on the stored position: %s" % shown
    op_body = got.op["body"]
    note(target, case, "RECORD", "level %d: public text %s, operator body ambiguity %r (keys %s), lat_e7 %d lon_e7 %d, expected text %s" % (
        level, shown["public state"], op_body.get("ambiguity"), sorted(op_body), got.state["body"]["lat_e7"], got.state["body"]["lon_e7"],
        (ps.expected_display(lat_exact, level), ps.expected_display(lon_exact, level))))
    assert op_body.get("ambiguity") == level, "the operator feed body carries ambiguity %r, want %d (UNVERIFIED field name; keys %s)" % (
        op_body.get("ambiguity"), level, sorted(op_body))
    return got


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_dp7_c_ambiguity_in_m17_packet_mode(rig, target, hub, web_ctx, shape, level):
    """data.md 5 and 13.4: an APRS position with `level` blanked minute digits in an M17 packet (source 5) is carried with
    ambiguity `level` and the BOX CENTRE as coordinates, and the page prints it at the sent precision (5 places for level 0,
    `4 - level` places, truncated toward zero, otherwise). The position is 4903.50N/07201.75W blanked 0..4 digits, the
    expected text is derived from dvtest.aprs_expect's exact value (cases/position_support.expected_display)."""
    case = "DP7-c[m17 level %d]" % level
    if not shape.positions:
        pytest.skip("this run has positions = false")
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    ambiguity_case(target, web_ctx, case, lambda call, lat, lon, lvl: send_m17(ctx, call, lat, lon, lvl, "amb%d" % lvl),
                   AMB_M17_CALL[level], level, 5)


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_dp7_c_ambiguity_over_rf(rig, target, hub, web_ctx, shape, rf, level):
    """The same for an APRS position heard by the fake KISS TNC on the room-bound port rf-a (the RF-heard path, source 5): the body
    carries `level`, the box centre and the page prints the sent precision."""
    case = "DP7-c[rf level %d]" % level
    if not shape.positions:
        pytest.skip("this run has positions = false")
    ctx = types.SimpleNamespace(rig=rig, rf=rf)
    ambiguity_case(target, web_ctx, case, lambda call, lat, lon, lvl: send_rf(ctx, call, lat, lon, lvl, "amb%d" % lvl),
                   AMB_RF_CALL[level], level, 5)


DPRS_AMBIGUITY_FINDING = (
    "FINDING DP7-FIND1 (first rig run wip-ac750c7: the same line shape passes as a full precision beacon and as an M17 packet-mode APRS "
    "position of ambiguity 2; the line is CRC valid): the D-PRS path does not accept an ambiguous position. "
    "crates/lib/pkt/src/dstar_dprs.rs:105-106 slices the 19 byte field and calls position.rs coordinate(), whose decimal() "
    "(position.rs:116-125) takes only digits, '.' and '-' so the blanked digits give Reject::BadPosition "
    "(dv_pkt_reject_total{kind=\"dstar-slowdata\",reason=\"bad_position\"} +1) while the TX text of the same over still becomes a status. "
    "data.md 5 (line 505) says the ambiguity byte is nonzero for source 1 D-PRS, data.md 696 says the info field goes through the APRS "
    "position parser (9.1, which honours ambiguity: aprs/position.rs:226-257), data.md 1647 and 2339 say the level is carried and re-applied; "
    "the DP5a 'Built scope' paragraph (data.md 709-711) says GPS-A decodes uncompressed '!'/'=' positions and does not mention ambiguity. Detail: "
)


def dprs_ambiguity_finding(message):
    """Only the 'no event at all' failure is the finding; a later failure (level, centre, text) stays as it is."""
    if "no position / object event" in message:
        return DPRS_AMBIGUITY_FINDING + message
    return message


def test_dp7_c_dprs_ambiguity_and_uplink(rig, target, hub, web_ctx, shape, request):
    """data.md 5 ("nonzero only for sources 1 D-PRS and 5 APRS") and 9.5: a D-PRS GPS-A line whose position has two blanked
    minute digits is source 1 with ambiguity 2 and printed at that precision. When the run has the APRS-IS uplink
    (dp7-aprsis-up.toml, profile aprs) the uplinked line at the private aprsc blanks the same two digits in both coordinates
    again (`!` / `=` position `1030.  N/02015.  E`): DV-Router neither refines nor coarsens what the station sent. UNVERIFIED: the
    ident `stream` of a D-STAR talker for the uplink (a missing line is recorded, not asserted, when only the room side is wanted)."""
    case = "DP7-c[d-prs level 2]"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    call = AMB_DPRS_CALL
    if shape.uplink:
        host = request.getfixturevalue("aprsc")
        stack = aprsc_login(host)
    else:
        stack = contextlib.nullcontext(None)
    with stack as tester:
        mark = tester.mark() if tester is not None else 0
        try:
            ambiguity_case(target, web_ctx, case, lambda c, lat, lon, lvl: send_dstar(ctx, c, lat, lon, lvl, "amb2"), call, 2, 1)
        except AssertionError as exc:
            raise AssertionError(dprs_ambiguity_finding(str(exc))) from None
        if tester is not None:
            hit = tester.wait_packet(lambda p: ps.same_base(p["src"], call), UPLINK_WAIT_S, since=mark)
            note(target, case, "RECORD", "uplinked line at the aprsc: %s" % (None if hit is None else hit[2]))
            assert hit is not None, "the D-PRS position of %s was not uplinked within %.0f s (ident stream? path dp7-aprsis-up?)" % (call, UPLINK_WAIT_S)
            want = ps.plain_text(ps.APRS_LAT, ps.APRS_LON, 2, "/>")
            info = hit[1]["info"]
            assert info[0] in "!=" and info[1:1 + len(want)] == want, \
                "the uplinked report is %r, want the position %r with the two blanked digits re-applied" % (info[:1 + len(want)], want)


def test_dp7_c_gps_sources_never_claim_ambiguity(target, ledger):
    """data.md 5: "0 for every GPS fix". The rows the DP7-a cases saw for the YSF GPS fix, the DMRG and the full precision D-PRS
    beacon (and the M17 APRS position) have ambiguity 0 on the public state, the public SSE and the operator feed. (The D-PRS line
    CAN carry an ambiguity when the station blanks digits: DP7-c above; the NMEA GPS mode of a D-STAR radio is not written.)"""
    case = "DP7-c[gps ambiguity 0]"
    if not ledger:
        pytest.skip("the DP7-a cases did not run in this session (they fill the ledger)")
    for source in ("ysf", "dmr", "dstar", "m17"):
        if source not in ledger:
            continue
        seen = ledger[source]
        for surface, row in (("public state", seen.state), ("public SSE", seen.sse), ("operator feed", seen.op)):
            ambiguity = row["body"].get("ambiguity")
            note(target, case, "RECORD", "%s %s: ambiguity %r" % (source, surface, ambiguity))
            assert ambiguity == 0, "%s on the %s carries ambiguity %r although its position has every digit" % (source, surface, ambiguity)


# ================================================================ DP7-d: object kill

def keys_of(value, found=None):
    """Every key of a nested JSON value."""
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            keys_of(item, found)
    elif isinstance(value, list):
        for item in value:
            keys_of(item, found)
    return found


def test_dp7_d_object_kill_is_coordinate_free(rig, target, hub, web_ctx, shape):
    """data.md 9.1 / 13.2: "a killed object is passed with the flag so the map removes it"; "an object kill emits only its name and
    killed state, without coordinates". An M17 packet-mode APRS object `;DVT7OBJ  *HHMMSSh<position>` from N0PDO-7 is public (public
    SSE `object` event with name, state 1 and coordinates; the anonymous public state lists it); 34 s later (objects share the 1 per
    30 s bucket of the reporting base callsign) the kill `;DVT7OBJ  _HHMMSSh...` gives a public SSE `object` event whose body is
    {name, state 0} and which has NO coordinate key anywhere (lat_e7, lon_e7, lat, lon, ...), and the object leaves the anonymous
    public listing. The retention (30 minutes for a live object, data.md 13.2) is recorded, not waited for. UNVERIFIED shape: the
    kill event row, taken from openapi Position (item_type object, body {name, state 0})."""
    case = "DP7-d"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    call = OBJECT_REPORTER
    with feeds(web_ctx) as fd:
        send_m17_object(ctx, call + "-7", ps.LAT, ps.LON, name=OBJECT_NAME, live=True)
        live = collect(web_ctx, fd, call, item_type="object", name=OBJECT_NAME)
        assert_visible(live, case + " live object")
        ps.check_position(live.state, source=5, ambiguity=0)
        assert live.state["body"].get("state") == 1 and str(live.state["body"].get("name")).strip() == OBJECT_NAME, live.state
        note(target, case, "RECORD", "live object row: %s" % {k: live.state.get(k) for k in ("callsign", "protocol", "ident", "via")})
        t_kill = time.monotonic()
        send_m17_object(ctx, call + "-7", ps.LAT, ps.LON, name=OBJECT_NAME, live=False)
        hit = fd.pub.wait_data(spatial_pred(call, "object", OBJECT_NAME, state=0), ps.PUBLIC_WAIT_S, since=t_kill)
        gone = ps.wait_gone(web_ctx.anon(), call, room=MODULE, item_type="object", name=OBJECT_NAME)
        ops, _rows = ps.wait_operator_rows(web_ctx.op, fd.baseline, spatial_pred(call, "object", OBJECT_NAME, state=0), timeout=ps.OPERATOR_WAIT_S)
    assert hit is not None, "no public SSE kill event (object, state 0) for %s" % OBJECT_NAME
    event = hit[1]
    note(target, case, "RECORD", "kill event %s; keys at any depth %s; left the public listing: %s; operator feed kill row: %s; retention of a live "
         "object: 30 min (data.md 13.2), not waited for" % (event, sorted(keys_of(event)), gone, ops[0] if ops else None))
    ps.check_public_row(event)
    assert event["item_type"] == "object", event
    body = event["body"]
    assert str(body.get("name")).strip() == OBJECT_NAME and body.get("state") == 0, "kill body %r, want name %r and state 0" % (body, OBJECT_NAME)
    coordinate_keys = {k for k in keys_of(event) if k.lower().startswith(("lat", "lon"))}
    assert not coordinate_keys, "the kill event carries coordinate keys %s" % sorted(coordinate_keys)
    assert gone, "the killed object %s is still in the anonymous public listing" % OBJECT_NAME
