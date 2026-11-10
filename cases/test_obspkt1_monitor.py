# SPDX-License-Identifier: MIT
"""DV-Router OBSPKT1 rig cases: the admin packet monitor (`GET /api/v1/admin/packets`, `GET /api/v1/admin/packets/stream`; data.md 13.5, admin.md "Packet
monitor", openapi-admin.yaml PktMonRow; merge b9a6839e, image after wip-8891cc7). The packets come from the paths the DP15 / DP11e cases already use: the fake
modems rf-a / rf-b (dvtest/kiss_tnc.py), the private aprsc, the transit path to APRS-IS, a web room text to RF and a local KISS client. Case table, run
command, runtime, GAPs and the shapes that are not confirmed: docs/obspkt1-cases.md; helpers: dvtest/obspkt1.py.

  OBSPKT1-a  access: viewer and operator get 403 forbidden on both routes; admin with a step-up window gets the snapshot (documented shape, no-store,
             filters) and the SSE hello with the producers; bad parameters are 400
  OBSPKT1-b  the rows for injected packets, with the gate decision: RF rx and tx, APRS-IS rx and tx, the KISS client; worker rows carry the line, edge
             rows the header and outcomes only, grouped by ref
  OBSPKT1-c  privacy: a hidden station's position is blank in every row; Mic-E destinations show "(mic-e)"; edge rows of worker-decided packets have no
             line; a station hidden LATER loses its position from the rows still retained
  OBSPKT1-e  no packet body anywhere else: container logs, public state, other admin routes, telemetry labels
  OBSPKT1-f  admin without a fresh step-up window gets 403 step_up_required on both routes (waits until the login's window has closed)
  OBSPKT1-d  (LAST) a process restart clears the rings: kill dv-data-worker (worker rows go, a new boot, a `gap` on the stream), restart dv-edge-ax25 (edge rows go)

Shape: dp11e-kiss.toml + dp11e-kiss-igate.toml + dp15-aprsis.toml + dp15-rf-out.toml + obspkt1-hidden.toml, profile web,aprs. Skips with a reason on an image
without the packets routes (404), without the shape, or without a prerequisite. NOT RUN ON A RIG. Observations: stdout and out/<run>/obspkt1.txt.
"""
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprs_passcode, dp11e, kiss_app, kiss_tnc, obspkt1 as ob, txguard, web  # noqa: E402

import data_support as ds  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
# the private aprsc with its `shape`, and the fake modems rf-a / rf-b (their `hub` argument is the fixture of THIS module), reused by name
from test_dp15_positions import aprsc, shape  # noqa: E402,F401
from test_dp15_rf import rf  # noqa: E402,F401

pytestmark = pytest.mark.rig

LOG = "obspkt1.txt"
SETTING = "obspkt1"
APPEAR_S = 75.0                       # a row reaches the admin service through a 1 Hz poll; the bound is generous
POLICY_PUSH_S = 5.0
PACKETS = "/packets"
STREAM = "/packets/stream"


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    return types.SimpleNamespace(op=web_operator, anon=web_client, user=web_user, poster=web_user("N0OBP", may_tx=True))


def snapshot(admin, **query):
    """GET /api/v1/admin/packets with the query as given; (Response, body or None)."""
    qs = "&".join("%s=%s" % (k, v) for k, v in query.items())
    resp = admin.get(admin.path(PACKETS) + ("?" + qs if qs else ""), expect=None)
    return resp, (resp.json() if resp.status == 200 else None)


def code_of(resp):
    try:
        return (resp.json() or {}).get("code")
    except Exception:                                               # noqa: BLE001  a non-JSON refusal has no code
        return None


@pytest.fixture(scope="module")
def ctx(request, target, hub, admin_session):
    """The run shape, the probe of the packets routes and the live pieces; skips with the reason otherwise."""
    doc = p1.read_config(target)
    shp = dp11e.kiss_shape(doc)
    if shp is None or "apps" not in shp.listeners:
        pytest.skip("shape K of DP11e is needed (listener apps): rig/config/dv-router/dp11e-kiss.toml, dp11e-kiss-igate.toml, dp15-aprsis.toml, dp15-rf-out.toml, "
                    "obspkt1-hidden.toml (docs/obspkt1-cases.md)")
    data = doc.get("data") or {}
    if ob.HIDDEN_BASE not in (data.get("position_optout") or []):
        pytest.skip("position_optout does not hide %s: add rig/config/dv-router/obspkt1-hidden.toml" % ob.HIDDEN_BASE)
    if not any(p.get("to") == "edge:aprsis" and "transit" in (p.get("types") or []) for p in data.get("path") or []):
        pytest.skip("no transit path to APRS-IS: add rig/config/dv-router/dp11e-kiss-igate.toml")
    admin = admin_session("admin")
    admin.step_up()
    resp, body = snapshot(admin)
    if resp.status == 404:
        pytest.skip("the image has no packet monitor: GET /api/v1/admin/packets answered 404")
    assert resp.status == 200, "GET /api/v1/admin/packets with a step-up window answered HTTP %d: %s" % (resp.status, resp.text()[:200])
    user, password = admin_session.credentials("admin")
    stale = admin.clone()                                           # a second login: its step-up window is the login's, and closes after 5 minutes
    stale.authenticator = admin.authenticator
    stale.hide(password)
    stale.login(user, password, admin.authenticator.next_code())
    stale.session()
    note(target, "ctx", "RECORD", "packets routes present; snapshot keys %s; %d rows; producers %s" % (sorted(body), len(body.get("rows", [])),
                                                                                                   [p.get("id") for p in body.get("producers", [])]))
    return types.SimpleNamespace(shape=shp, admin=admin, stale=stale, stale_login_at=time.monotonic(), admin_session=admin_session,
                                 tnc=request.getfixturevalue("rf"), host=request.getfixturevalue("aprsc"), target=target)


# ================================================================ the traffic of the module

def wait_rows(ctx, pred, timeout=APPEAR_S, **query):
    """The snapshot body once `pred(rows)` holds (polled every 3 s), else the last one after `timeout` s."""
    end, body = time.monotonic() + timeout, None
    while True:
        resp, body = snapshot(ctx.admin, limit=1000, **query)
        assert resp.status == 200, "snapshot answered HTTP %d" % resp.status
        if pred(body["rows"]) or time.monotonic() >= end:
            return body
        time.sleep(3.0)


@pytest.fixture(scope="module")
def traffic(ctx, hub, web_ctx, target):
    """Inject the module's packets once and return the snapshot in which they all appear."""
    tnc = ctx.tnc
    m = {k: ob.new_marker(k) for k in ("S", "N", "H", "V", "M", "W", "A", "I", "R", "C", "P")}
    tnc.tx_guard("OBSPKT1")
    sent = {}
    sent["status"] = ob.rf_frame(ob.SRC_STATUS, ">" + m["S"])
    sent["nogate"] = ob.rf_frame(ob.SRC_NOGATE, ">" + m["N"], digis=("WIDE1-1*", "NOGATE"))
    sent["query"] = ob.rf_frame(ob.SRC_QUERY, "?APRS?")
    sent["hidden_pos"] = ob.rf_frame(ob.SRC_HIDDEN_POS, ob.position_info(ob.POS_HIDDEN, m["H"]))
    sent["visible_pos"] = ob.rf_frame(ob.SRC_VISIBLE_POS, ob.position_info(ob.POS_VISIBLE, m["V"]))
    sent["account_pos"] = ob.rf_frame(ob.SRC_ACCOUNT, ob.position_info(ob.POS_ACCOUNT, m["A"]))
    sent["hidden_mice"], mice_dest_hidden = ob.mice_frame(ob.SRC_HIDDEN_MICE, 37.62, -122.4, m["M"])
    sent["visible_mice"], mice_dest_visible = ob.mice_frame(ob.SRC_VISIBLE_MICE, 41.2, -87.6, m["W"])
    web_ctx.user(ob.ACCOUNT, may_tx=False)
    t0 = time.monotonic()
    for frame in sent.values():
        assert tnc.a.inject(frame), "the fake modem rf-a has no connection to inject into"
        time.sleep(0.3)
    # APRS-IS: a message to the room name arrives at the edge through the private aprsc
    with p1.aprsc_login(ctx.host) as tester:
        tester.send_message(aprs_passcode.TESTER_LOGIN, "XLX999B", "OBS %s" % m["I"], msgid="81")
        time.sleep(2.0)
    # a web room text leaves over RF (rf-out)
    resp = web.post_room_text(web_ctx.poster.client, "B", "OBS %s" % m["R"], expect=None)
    assert resp.status == 202, "the room text POST answered HTTP %d" % resp.status
    # a local KISS client transmits a message and a position
    client = kiss_app.AppKissClient(ctx.shape.listeners["apps"].host, ctx.shape.listeners["apps"].port, name="obspkt1")
    assert client.connect(), "cannot connect to listener apps: %s" % client.error
    try:
        client.send_frame(dp11e.good_frame(kiss_tnc.aprs_message_info("N0OBC", m["C"]).decode("latin-1")))
        time.sleep(1.0)
        client.send_frame(dp11e.good_frame(ob.position_info(ob.POS_CLIENT, m["P"]), src=dp11e.SRC))
        time.sleep(1.0)
    finally:
        client.close()
    want = (m["S"], m["V"], m["I"], m["R"], m["C"])
    body = wait_rows(ctx, lambda rows: all(ob.mentions(rows, x) for x in want))
    note(target, "traffic", "RECORD", "markers %s; %d rows after %.0f s; missing: %s" % (
        sorted(m), len(body["rows"]), time.monotonic() - t0, [k for k in want if not ob.mentions(body["rows"], k)]))
    return types.SimpleNamespace(markers=m, body=body, rows=body["rows"], frames=sent, mice_dest_hidden=mice_dest_hidden,
                                 mice_dest_visible=mice_dest_visible, t0=t0)


# ================================================================ OBSPKT1-a: access

def test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(ctx):
    """admin.md: the snapshot and the stream are admin-only sensitive reads; viewer and operator receive 403 `forbidden`."""
    for role in ("viewer", "operator"):
        admin = ctx.admin_session(role)
        if getattr(admin, "authenticator", None):
            admin.step_up()                                         # a window must not be what refuses them
        resp, _ = snapshot(admin)
        assert resp.status == 403 and code_of(resp) == "forbidden", "%s GET packets: HTTP %d code %r" % (role, resp.status, code_of(resp))
        with pytest.raises(web.WebError) as err:
            admin.sse(admin.path(STREAM), max_events=1, timeout=8.0)
        assert err.value.status == 403, "%s GET packets/stream: HTTP %s" % (role, err.value.status)


def test_obspkt1_a_admin_with_a_step_up_gets_the_documented_snapshot_and_stream(ctx, target):
    """openapi-admin.yaml PacketSnapshot / PktMonRow: rows (newest first, at most the limit), ports {id, port, first_seq, last_seq}, producers {id, boot};
    `Cache-Control: no-store`; filters port, dir, limit (clamped 1-1000); 400 for a bad parameter. The stream starts with `retry: 3000` and
    `event: hello` {producers:[{id,boot}]}. The shapes were read from the documents (UNVERIFIED): a deviation fails with what was seen."""
    admin = ctx.admin
    admin.step_up()
    resp, body = snapshot(admin, limit=1000)
    assert resp.status == 200
    assert "no-store" in (resp.headers.get("Cache-Control") or resp.headers.get("cache-control") or ""), "Cache-Control: %r" % resp.headers
    problems = ob.snapshot_problems(body)
    assert not problems, "; ".join(problems[:6])
    assert ob.newest_first(body["rows"]), "rows are not newest first"
    one_resp, one = snapshot(admin, limit=1)
    assert one_resp.status == 200 and len(one["rows"]) <= 1
    big_resp, big = snapshot(admin, limit=5000)
    assert big_resp.status in (200, 400), "limit=5000 answered HTTP %d" % big_resp.status
    if big_resp.status == 200:
        assert len(big["rows"]) <= ob.MAX_ROWS
    for query in ({"dir": "sideways"}, {"port": "x" * 200}):
        bad, _ = snapshot(admin, **query)
        assert bad.status == 400, "%s answered HTTP %d, expected 400" % (query, bad.status)
    rx_resp, rx = snapshot(admin, dir="rx")
    assert rx_resp.status == 200 and all(r["dir"] == "rx" for r in rx["rows"])
    port_resp, only = snapshot(admin, port="rf-a")
    assert port_resp.status == 200 and all(r["port"] == "rf-a" for r in only["rows"])
    status, ctype, frames = admin.sse(admin.path(STREAM), max_events=1, timeout=15.0)
    assert status == 200 and "text/event-stream" in ctype, "stream: HTTP %s %s" % (status, ctype)
    assert frames and frames[0].get("event") == "hello", "first stream frame: %s" % frames[:1]
    hello = ob.parse_stream(frames).hello
    assert hello and all(isinstance(p, dict) and p.get("id") and ob._boot_ok(p.get("boot")) for p in hello), "hello producers: %s" % hello
    note(target, "OBSPKT1-a", "RECORD", "snapshot keys %s, ports %s, hello producers %s, retry %s" % (
        sorted(body), body["ports"][:4], [p["id"] for p in hello], frames[0].get("retry")))


# ================================================================ OBSPKT1-b: the rows

def test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic, target):
    """data.md 13.5: the worker records parse, RF receive policy, route/gate; the AX.25 edge records modem/raw. A frame heard on rf-a that the transit path
    gates (`forwarded`), one with NOGATE (`no_gate`) and a general query (`filtered`) show the decision in their route outcome. The worker row has the
    TNC2 line; the edge row of the same ref has the header and outcomes and NO line."""
    rows = traffic.rows
    expected = [(ob.SRC_STATUS, "forwarded", traffic.markers["S"]), (ob.SRC_NOGATE, "no_gate", traffic.markers["N"]), (ob.SRC_QUERY, "filtered", "?APRS?")]
    for src, result, text in expected:
        worker = [r for r in ob.rows_from(ob.worker_rows(rows), src, port="rf-a", dir="rx") if text in (r.get("line") or "")]
        assert worker, "no worker rx row for %s on rf-a with %r; sources seen: %s" % (src, text, sorted({ob.row_src(r) or "?" for r in ob.worker_rows(rows)})[:12])
        row = worker[0]
        note(target, "OBSPKT1-b", "RECORD", "%s: line %r, outcomes %s, ref %s" % (src, row["line"], ob.outcomes(row), row.get("ref")))
        assert ob.has_outcome(row, "route", result), "%s: route outcome %s, expected result %r" % (src, ob.outcomes(row), result)
        edge = [r for r in ob.rows_from(ob.edge_rows(rows), src, port="rf-a", dir="rx")]
        assert edge, "no edge rx row for %s on rf-a" % src
        assert all("line" not in r for r in edge), "an edge rx row carries a line: %s" % [r for r in edge if "line" in r][:1]
        assert all(text not in (r.get("header") or "") for r in edge if text != "?APRS?"), "an edge header carries the information field"
        assert any(r.get("ref") is not None and r.get("ref") == row.get("ref") for r in edge), "no edge row shares ref %s with the worker row: %s" % (
            row.get("ref"), [r.get("ref") for r in edge])
    gated = [r for r in ob.rows_from(ob.edge_rows(rows), ob.SRC_STATUS, dir="tx") if r.get("port") == "is"]
    assert gated, "no APRS-IS edge tx row for the gated frame of %s (ports seen: %s)" % (ob.SRC_STATUS, sorted({r["port"] for r in rows}))
    assert all("line" not in r and traffic.markers["S"] not in json.dumps(r) for r in gated), "the APRS-IS edge tx row carries the information field"
    assert any(ob.has_outcome(r, "write") for r in gated), "no write outcome on the APRS-IS tx row: %s" % [ob.outcomes(r) for r in gated]


def test_obspkt1_b_aprs_is_rx_and_rf_tx_rows(ctx, traffic, target):
    """APRS-IS rx: a message to the room name from the tester's login: the edge `is` row has the header only, the worker row has the line with the message
    body (directed message bodies are available in this privileged monitor). RF tx: a web room text leaves rf-a: the edge tx row has the header and the
    scheduler outcomes and no line; the worker row with the same ref has the line."""
    rows = traffic.rows
    rx_worker = [r for r in ob.worker_rows(rows) if r["port"] == "is" and r["dir"] == "rx" and traffic.markers["I"] in (r.get("line") or "")]
    assert rx_worker, "no worker rx row on port is with the message body"
    rx_edge = [r for r in ob.edge_rows(rows) if r["port"] == "is" and r["dir"] == "rx" and ob.row_src(r) == aprs_passcode.TESTER_LOGIN]
    assert rx_edge and all("line" not in r for r in rx_edge), "APRS-IS edge rx rows: %s" % rx_edge[:2]
    tx_worker = [r for r in ob.worker_rows(rows) if r["port"] == "rf-a" and r["dir"] == "tx" and traffic.markers["R"] in (r.get("line") or "")]
    assert tx_worker, "no worker tx row on rf-a with the room text; tx rows seen: %s" % [(r["port"], ob.row_src(r)) for r in rows if r["dir"] == "tx"][:8]
    tx_edge = [r for r in ob.edge_rows(rows) if r["port"] == "rf-a" and r["dir"] == "tx" and ob.row_src(r) == dp11e.STATION]
    assert tx_edge and all("line" not in r for r in tx_edge), "RF edge tx rows of the worker-rendered text: %s" % tx_edge[:2]
    assert any(ob.has_outcome(r, stage) for r in tx_edge for stage in ("txcore", "sched")), "no txcore / sched outcome: %s" % [ob.outcomes(r) for r in tx_edge]
    assert any(r.get("ref") == tx_worker[0].get("ref") for r in tx_edge), "the edge tx row does not share the ref %s of the worker row" % tx_worker[0].get("ref")
    note(target, "OBSPKT1-b", "RECORD", "rf tx worker outcomes %s; edge outcomes %s" % (ob.outcomes(tx_worker[0]), [ob.outcomes(r) for r in tx_edge][:3]))


def test_obspkt1_b_a_local_kiss_client_row_keeps_its_line_but_not_a_position(ctx, traffic, target):
    """data.md 13.5: a local KISS client frame never passes the worker; its AX.25 edge transmit row keeps the line, except that a spatial information field is
    blanked after the header and marked `redacted` (message and other nonspatial lines are shown in full)."""
    rows = [r for r in ob.edge_rows(traffic.rows) if r["port"] == "rf-a" and r["dir"] == "tx" and ob.row_src(r) == dp11e.SRC]
    message = [r for r in rows if traffic.markers["C"] in (r.get("line") or "")]
    assert message, "no client tx row with the message body; client rows: %s" % [r.get("line") or r.get("header") for r in rows][:4]
    assert ob.has_outcome(message[0], "client") or ob.has_outcome(message[0], "txcore"), "client row outcomes: %s" % ob.outcomes(message[0])
    position = [r for r in rows if r.get("redacted") and r.get("line", "").startswith(dp11e.SRC + ">")]
    assert position, "no redacted client position row; rows: %s" % [(r.get("line"), r.get("redacted")) for r in rows][:4]
    assert ob.info_of(position[0]) == "" and ob.POS_CLIENT[0] not in json.dumps(traffic.rows), "the client position leaked: %s" % position[0]
    assert position[0]["redacted"] == ob.REDACTED


# ================================================================ OBSPKT1-c: privacy

def test_obspkt1_c_a_hidden_stations_position_is_blank_in_every_row(ctx, traffic, target):
    """data.md 13.5: a hidden base source (position_optout N0HID) has its spatial information blanked after `SRC>DST,PATH:` and marked
    `redacted: "position_policy"`; admin privileges do not bypass it. Its coordinates and comment appear in no row of the snapshot; the visible control
    station shows its position in the worker row."""
    rows = traffic.rows
    hidden = ob.rows_from(ob.worker_rows(rows), ob.SRC_HIDDEN_POS, dir="rx")
    assert hidden, "no worker row for the hidden station"
    for r in hidden:
        assert ob.info_of(r) == "" and r.get("redacted") == ob.REDACTED, "hidden station row: %s" % r
    for needle in (ob.POS_HIDDEN[0], ob.POS_HIDDEN[1], traffic.markers["H"]):
        assert not ob.mentions(rows, needle), "%r of the hidden station appears in a row: %s" % (needle, ob.mentions(rows, needle)[:1])
    visible = ob.rows_from(ob.worker_rows(rows), ob.SRC_VISIBLE_POS, dir="rx")
    assert visible and ob.POS_VISIBLE[0] in visible[0]["line"] and "redacted" not in visible[0], "the visible control row: %s" % visible[:1]
    note(target, "OBSPKT1-c", "RECORD", "hidden row: %r; visible row: %r" % (hidden[0].get("line"), visible[0].get("line")))


def test_obspkt1_c_mic_e_destinations_show_the_placeholder(ctx, traffic, target):
    """data.md 13.5: a Mic-E destination encodes the latitude, so a redacted Mic-E line shows `(mic-e)` in place of the destination; edge rows ALWAYS show
    `(mic-e)` in the header of a Mic-E packet. The real destination strings appear in no row of the hidden station and in no edge row."""
    rows = traffic.rows
    hidden = ob.rows_from(ob.worker_rows(rows), ob.SRC_HIDDEN_MICE, dir="rx")
    assert hidden, "no worker row for the hidden Mic-E station"
    parsed = ob.split_tnc2(hidden[0]["line"])
    assert parsed and parsed[1] == ob.MIC_E and parsed[3] == "" and hidden[0].get("redacted") == ob.REDACTED, "hidden Mic-E worker row: %s" % hidden[0]
    assert not ob.mentions(rows_of_source(rows, ob.SRC_HIDDEN_MICE), traffic.mice_dest_hidden), "the Mic-E destination of the hidden station leaked"
    for src, dest in ((ob.SRC_HIDDEN_MICE, traffic.mice_dest_hidden), (ob.SRC_VISIBLE_MICE, traffic.mice_dest_visible)):
        edge = ob.rows_from(ob.edge_rows(rows), src, dir="rx")
        assert edge and all(ob.MIC_E in (r.get("header") or "") and dest not in (r.get("header") or "") for r in edge), (
            "edge rows of %s: %s" % (src, [r.get("header") for r in edge]))
    visible = ob.rows_from(ob.worker_rows(rows), ob.SRC_VISIBLE_MICE, dir="rx")
    note(target, "OBSPKT1-c", "RECORD", "visible Mic-E worker row: %s" % [(r.get("line"), r.get("redacted")) for r in visible])


def rows_of_source(rows, src):
    return [r for r in rows if ob.row_src(r) == src]


def test_obspkt1_c_edge_rows_of_worker_decided_packets_have_no_line(ctx, traffic):
    """data.md 13.5: edges do not own the hidden set and retain no information field of a packet the worker decides: every edge row without a `client`
    outcome has no `line`, only the optional `header`."""
    offenders = [r for r in ob.edge_rows(traffic.rows) if "line" in r and not ob.has_outcome(r, "client")]
    assert not offenders, "edge rows with a line: %s" % [(r["port"], r["dir"], r["line"][:40]) for r in offenders[:4]]
    assert any("header" in r for r in ob.edge_rows(traffic.rows)), "no edge row carries a header at all"


def test_obspkt1_c_a_station_hidden_later_loses_its_position_from_retained_rows(ctx, traffic, web_ctx, target):
    """data.md 13.5: retained worker rows, receive and transmit, are checked against the CURRENT spatial policy when queried, so a station whose account
    gets hide_position afterwards loses its position from every row still retained."""
    before = ob.rows_from(ob.worker_rows(traffic.rows), ob.SRC_ACCOUNT, dir="rx")
    assert before and ob.POS_ACCOUNT[0] in before[0]["line"], "the account's position was not visible before the hide: %s" % before[:1]
    p1.set_hide_position(web_ctx, ob.ACCOUNT, True)
    try:
        time.sleep(POLICY_PUSH_S)
        end, rows = time.monotonic() + 40.0, []
        while time.monotonic() < end:
            _resp, body = snapshot(ctx.admin, limit=1000)
            rows = ob.rows_from(ob.worker_rows(body["rows"]), ob.SRC_ACCOUNT, dir="rx")
            if rows and ob.info_of(rows[0]) == "":
                break
            time.sleep(3.0)
        note(target, "OBSPKT1-c", "RECORD", "after hide_position: %s" % [(r.get("line"), r.get("redacted")) for r in rows])
        assert rows and ob.info_of(rows[0]) == "" and rows[0].get("redacted") == ob.REDACTED, "the retained row still shows the position: %s" % rows[:1]
        assert not ob.mentions(body["rows"], ob.POS_ACCOUNT[0]) and not ob.mentions(body["rows"], ob.POS_ACCOUNT[1]), "the coordinates are still in a row"
    finally:
        p1.set_hide_position(web_ctx, ob.ACCOUNT, False)


# ================================================================ OBSPKT1-e: no body anywhere else

def test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, traffic, web_ctx, hub, target):
    """data.md 13.5: monitor rows never enter logs (including debug), disk or telemetry; the status hub and its feed carry none; the public state carries
    none. The unique payload markers of packets that never become room items (the RF status packets and the KISS client message) must appear in the
    monitor (the control) and in none of: the SUT logs of the run, the public state, the other admin routes (status, clients, events, lastheard), the hub
    metrics (labels and values)."""
    markers = [traffic.markers[k] for k in ("S", "N", "C")]
    for m in markers:
        assert ob.mentions(traffic.rows, m), "the control: %s is not in the monitor" % m
    texts = {}
    logs = Path(ctx.target.out) / "logs"
    for path in sorted(logs.glob("*")) if logs.is_dir() else []:
        if path.is_file() and "sut" in path.name:
            texts["log:" + path.name] = path.read_text(encoding="utf-8", errors="replace")
    texts["public state"] = web_ctx.anon.get("/api/v1/public/state", expect=200).text()
    for route in ("/status", "/clients", "/events", "/lastheard"):
        ctx.admin.step_up()
        resp = ctx.admin.get(ctx.admin.path(route), expect=None)
        texts["admin" + route] = resp.text() if resp.status == 200 else ""
    texts["hub metrics"] = hub.text()
    leaks = ob.find_leaks(texts, markers)
    note(target, "OBSPKT1-e", "RECORD", "scanned %s (%d bytes); leaks: %s" % (sorted(texts), sum(len(t) for t in texts.values()), leaks))
    assert not leaks, "a packet body appears outside the monitor: %s" % leaks
    if not any(k.startswith("log:") for k in texts):
        pytest.skip("no SUT log under %s/logs was scanned (the other texts are clean)" % ctx.target.out)


# ================================================================ OBSPKT1-f: no fresh step-up

def test_obspkt1_f_admin_without_a_fresh_step_up_gets_403_step_up_required(ctx, target):
    """admin.md: an admin without a fresh step-up window receives 403 `step_up_required` on the snapshot and on the stream. A login opens a window, so the
    case uses the second admin login made at the start of the module and waits (up to about 5 minutes) until that window has closed."""
    wait = ob.stale_wait_s(time.monotonic() - ctx.stale_login_at)
    note(target, "OBSPKT1-f", "RECORD", "waiting %.0f s for the step-up window of the second admin login to close" % wait)
    time.sleep(wait)
    resp, _ = snapshot(ctx.stale)
    assert resp.status == 403 and code_of(resp) == "step_up_required", "snapshot without a window: HTTP %d code %r" % (resp.status, code_of(resp))
    with pytest.raises(web.WebError) as err:
        ctx.stale.sse(ctx.stale.path(STREAM), max_events=1, timeout=8.0)
    assert err.value.status == 403, "stream without a window: HTTP %s" % err.value.status
    ctx.stale.step_up()                                                  # and a fresh window opens it again
    again, _ = snapshot(ctx.stale)
    assert again.status == 200, "after a step-up the snapshot answered HTTP %d" % again.status


# ================================================================ OBSPKT1-d: restart clears the rings

class Streamer:
    """The admin packet stream read in a thread; `frames` is what arrived."""

    def __init__(self, admin):
        self.admin, self.frames, self.stop = admin, [], threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            self.admin.sse(self.admin.path(STREAM), max_events=100000, timeout=900.0, on_frame=self.frames.append, stop=self.stop)
        except Exception:                                               # noqa: BLE001  the stream ending is not a failure of the reader
            pass

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5.0)


def boots(body):
    return {p["id"]: tuple(p["boot"]) for p in body["producers"]}


def test_obspkt1_d_a_process_restart_clears_its_rings(ctx, traffic, rig_control, target):
    """data.md 13.5: each producer keeps an always-on memory-only ring; a new process boot starts empty. The data worker is killed with the allowed kill-proc
    (the launcher respawns it): its rows are gone, its boot is new and the stream says `gap`; the edge rows stay. Then the AX.25 child is restarted through
    the admin API (as DP15-6g): its rows are gone and its boot changed. (The rings live in the child processes, so a container restart is not needed.)"""
    case = "OBSPKT1-d"
    markers = [traffic.markers["S"], traffic.markers["C"], traffic.markers["I"]]
    resp, before = snapshot(ctx.admin, limit=1000)
    worker_ids = [p["id"] for p in before["producers"] if ob.producer_kind(p["id"]) == "worker"]
    edge_ids = [p["id"] for p in before["producers"] if ob.producer_kind(p["id"]) == "edge"]
    assert worker_ids and edge_ids, "producers seen: %s" % [p["id"] for p in before["producers"]]
    assert any(ob.mentions(ob.worker_rows(before["rows"]), m) for m in markers) and ob.edge_rows(before["rows"]), "nothing to clear"
    boot0 = boots(before)
    with Streamer(ctx.admin) as stream:
        time.sleep(5.0)
        rig_control.kill_proc("xlxd-sut", "dv-data-worker", "KILL")
        end, after = time.monotonic() + 90.0, before
        while time.monotonic() < end:
            time.sleep(3.0)
            ctx.admin.step_up()
            r, body = snapshot(ctx.admin, limit=1000)
            if r.status == 200 and any(boots(body).get(w) not in (None, boot0[w]) for w in worker_ids):
                after = body
                break
        assert after is not before, "the worker boot did not change within 90 s of the kill"
        new_boot = boots(after)
        note(target, case, "RECORD", "boots: %s -> %s" % ({k: v[:2] for k, v in boot0.items()}, {k: v[:2] for k, v in new_boot.items()}))
        for m in markers[:1] + markers[2:]:
            assert not ob.mentions(ob.worker_rows(after["rows"]), m), "a worker row survived the worker restart: %s" % ob.mentions(ob.worker_rows(after["rows"]), m)[:1]
        assert ob.edge_rows(after["rows"]), "the edge rows disappeared with the worker restart"
        assert any(boot0[e] == new_boot.get(e) for e in edge_ids), "the edge boot changed with the worker restart"
        admin = ctx.admin_session("admin")
        admin.step_up()
        child = admin.post(admin.path("/children/%s/restart" % txguard.AX25_CHILD), {}, expect=None)
        assert child.status == 200, "restart of %s answered HTTP %d" % (txguard.AX25_CHILD, child.status)
        end, final = time.monotonic() + 90.0, after
        while time.monotonic() < end:
            time.sleep(3.0)
            ctx.admin.step_up()
            r, body = snapshot(ctx.admin, limit=1000)
            if r.status == 200 and any(boots(body).get(e) not in (None, boot0[e]) for e in edge_ids):
                final = body
                break
        assert final is not after, "the AX.25 edge boot did not change within 90 s of the restart"
        ax = [e for e in edge_ids if e.startswith("ax25") and boots(final).get(e) not in (None, boot0[e])]
        assert ax, "the AX.25 producer %s kept its boot" % [e for e in edge_ids if e.startswith("ax25")]
        assert not [r for r in final["rows"] if r.get("id") in ax and traffic.markers["S"] in json.dumps(r)], "old AX.25 edge rows survived the restart"
        time.sleep(3.0)
    events = ob.parse_stream(stream.frames)
    note(target, case, "RECORD", "stream: hello %s, gaps %s, rows %d, byes %s, resync %s" % (
        [p.get("id") for p in events.hello or []], events.gaps, len(events.rows), events.byes, events.resync))
    assert events.gaps, "the stream reported no `gap` for a producer that restarted"
