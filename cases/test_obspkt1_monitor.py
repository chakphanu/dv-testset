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
  OBSPKT1-d  (LAST) a process restart clears the rings: kill dv-data-worker (same id, a new boot, worker rows go, `gap` {id}), restart dv-edge-ax25 (the edge id
             CHANGES with the process: a `gap` for the old id, the new id appears in rows without a gap and was not in `hello`)

Shape: dp11e-kiss.toml + dp11e-kiss-igate.toml + dp15-aprsis.toml + dp15-rf-out.toml + obspkt1-hidden.toml, profile web,aprs. Skips with a reason on an image
without the packets routes (404), without the shape, or without a prerequisite. NOT RUN ON A RIG. Observations: stdout and out/<run>/obspkt1.txt.
"""
import json
import os
import sys
import threading
import time
import types
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprs_passcode, dp11e, kiss_app, kiss_tnc, logscan, obspkt1 as ob, txguard, web  # noqa: E402

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


def refused_rows(rows):
    """The edge tx rows on rf-a of a client frame the gate refused for its source: stage client, result src (data.md 13.5; kiss_server.rs monitor_refused)."""
    return [r for r in ob.edge_rows(rows) if r.get("port") == "rf-a" and r.get("dir") == "tx" and ob.has_outcome(r, "client", "src")]


def snapshot(admin, **query):
    """GET /api/v1/admin/packets with the query as given; (Response, body or None)."""
    qs = "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe="")) for k, v in query.items())       # a space or a slash in a value is percent-encoded
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
    m = {k: ob.new_marker(k) for k in ("S", "N", "H", "V", "M", "W", "A", "I", "R", "C", "P", "X")}
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
    # a KISS client is attached while the RF frames are heard, so that the rx rows get the delivery outcome (stage client, result sent)
    client = kiss_app.AppKissClient(ctx.shape.listeners["apps"].host, ctx.shape.listeners["apps"].port, name="obspkt1")
    assert client.connect(), "cannot connect to listener apps: %s" % client.error
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
    # the local KISS client transmits a message, a position and a frame the gate refuses (a source that is not in allow_src)
    try:
        client.send_frame(dp11e.good_frame(kiss_tnc.aprs_message_info("N0OBC", m["C"]).decode("latin-1")))
        time.sleep(1.0)
        client.send_frame(dp11e.good_frame(ob.position_info(ob.POS_CLIENT, m["P"]), src=dp11e.SRC))
        time.sleep(1.0)
        client.send_frame(dp11e.good_frame(kiss_tnc.aprs_message_info("N0OBC", m["X"]).decode("latin-1"), src="N0TEST-9"))
        time.sleep(1.0)
    finally:
        client.close()
    # the refused frame (X) leaves a BODY-FREE row (stage client, result src; the gate refuses before the line is built): its marker is in no row
    want = (m["S"], m["V"], m["I"], m["R"], m["C"])
    body = wait_rows(ctx, lambda rows: all(ob.mentions(rows, x) for x in want) and refused_rows(rows))
    note(target, "traffic", "RECORD", "markers %s; %d rows after %.0f s; missing: %s; refused client rows: %d" % (
        sorted(m), len(body["rows"]), time.monotonic() - t0, [k for k in want if not ob.mentions(body["rows"], k)], len(refused_rows(body["rows"]))))
    return types.SimpleNamespace(markers=m, body=body, rows=body["rows"], frames=sent, mice_dest_hidden=mice_dest_hidden,
                                 mice_dest_visible=mice_dest_visible, t0=t0)


# ================================================================ OBSPKT1-a: access

def test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(ctx):
    """admin.md: the snapshot and the stream are admin-only sensitive reads; viewer and operator receive 403 `forbidden`; a request with no session is
    401 `unauthorized` (problem+json, confirmed by DV-Router)."""
    anonymous = ctx.admin.clone()
    resp, _ = snapshot(anonymous)
    assert resp.status == 401 and code_of(resp) == "unauthorized", "no session, GET packets: HTTP %d code %r" % (resp.status, code_of(resp))
    with pytest.raises(web.WebError) as err401:
        anonymous.sse(anonymous.path(STREAM), max_events=1, timeout=8.0)
    assert err401.value.status == 401, "no session, GET packets/stream: HTTP %s" % err401.value.status
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
    assert one_resp.status == 200 and len(one["rows"]) == min(1, len(body["rows"]))
    zero_resp, zero = snapshot(admin, limit=0)                         # 0 becomes 1
    assert zero_resp.status == 200 and len(zero["rows"]) <= 1
    big_resp, big = snapshot(admin, limit=5000)                        # clamped to 1000
    assert big_resp.status == 200 and len(big["rows"]) <= ob.MAX_ROWS, "limit=5000: HTTP %d" % big_resp.status
    default_resp, default = snapshot(admin)                            # the default limit is 100
    assert default_resp.status == 200 and len(default["rows"]) <= 100
    for query in ({"dir": "sideways"}, {"port": "x" * 65}, {"port": "a/b"}, {"port": "a b"}, {"limit": "abc"}, {"limit": "-1"}):
        bad, _ = snapshot(admin, **query)
        assert bad.status == 400 and code_of(bad) == "bad_request", "%s answered HTTP %d code %r, expected 400 bad_request" % (query, bad.status, code_of(bad))
    for query in ({"dir": "both"}, {"dir": "tx"}, {"dir": ""}, {"port": "x" * 64}, {"port": "is"}):
        good, _ = snapshot(admin, **query)
        assert good.status == 200, "%s answered HTTP %d, expected 200" % (query, good.status)
    rx_resp, rx = snapshot(admin, dir="rx")
    assert rx_resp.status == 200 and all(r["dir"] == "rx" for r in rx["rows"])
    port_resp, only = snapshot(admin, port="rf-a")
    assert port_resp.status == 200 and all(r["port"] == "rf-a" for r in only["rows"])
    status, ctype, frames = admin.sse(admin.path(STREAM), max_events=3, timeout=15.0, until="hello")
    assert status == 200 and "text/event-stream" in ctype, "stream: HTTP %s %s" % (status, ctype)
    retry, frames = ob.split_retry(frames)                               # `retry: 3000` may arrive as a bare block before the first event
    assert retry == "3000", "the stream's retry is %r, expected 3000" % (retry,)
    assert frames and frames[0].get("event") == "hello", "first stream event: %s" % frames[:1]
    hello = ob.parse_stream(frames).hello
    assert hello and all(isinstance(p, dict) and p.get("id") and ob._boot_ok(p.get("boot")) for p in hello), "hello producers: %s" % hello
    note(target, "OBSPKT1-a", "RECORD", "snapshot keys %s, ports %s, hello producers %s, retry %s" % (
        sorted(body), body["ports"][:4], [p["id"] for p in hello], retry))


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
        assert result in ob.ROUTE_RESULTS and all(res in ob.ROUTE_RESULTS for stage, res in ob.outcomes(row) if stage == "route"), (
            "%s: a route result is outside the dv_pkt_path_total set %s: %s" % (src, ob.ROUTE_RESULTS, ob.outcomes(row)))
        edge = [r for r in ob.rows_from(ob.edge_rows(rows), src, port="rf-a", dir="rx")]
        assert edge, "no edge rx row for %s on rf-a" % src
        assert all("line" not in r for r in edge), "an edge rx row carries a line: %s" % [r for r in edge if "line" in r][:1]
        assert all(text not in (r.get("header") or "") for r in edge if text != "?APRS?"), "an edge header carries the information field"
        assert any(r.get("ref") is not None and r.get("ref") == row.get("ref") for r in edge), "no edge row shares ref %s with the worker row: %s" % (
            row.get("ref"), [r.get("ref") for r in edge])
        assert any(r.get("header") == ob.line_header(row["line"]) for r in edge), "no edge header equals the worker line header %r: %s" % (
            ob.line_header(row["line"]), [r.get("header") for r in edge])
    # the delivery to a KISS client is a LATER, body-free row (dir rx, same port, `of` = the seq of the edge rx row, the same `ref`, stage client result
    # sent; kiss_server.rs output_sent_observed), not an outcome of the rx row itself
    status_rx = ob.rows_from(ob.edge_rows(rows), ob.SRC_STATUS, port="rf-a", dir="rx")
    delivered = [w for r in status_rx for w in ob.late_rows(rows, r) if ob.has_outcome(w, "client", "sent")]
    assert delivered, "no later row with a client / sent outcome for the edge rx row of the status packet while a KISS client was attached: rx rows %s; late rows %s" % (
        [(r.get("seq"), r.get("ref"), ob.outcomes(r)) for r in status_rx], [(w.get("of"), ob.outcomes(w)) for r in status_rx for w in ob.late_rows(rows, r)])
    assert all(w.get("port") == "rf-a" and w.get("dir") == "rx" and "line" not in w and "header" not in w for w in delivered), "a delivery row: %s" % delivered[:1]
    # APRS-IS tx comes in two rows (confirmed): the start row (dir tx, port is, ref, header, stage render result received) and a later row with `of` =
    # the start row's seq and the same ref that carries stage write with a result of the fixed set (framed for a gated packet on a verified login)
    starts = [r for r in ob.rows_from(ob.edge_rows(rows), ob.SRC_STATUS, dir="tx") if r.get("port") == "is" and ob.has_outcome(r, "render", "received")]
    assert starts, "no APRS-IS edge start row (render / received) for the gated frame of %s; tx rows on is: %s" % (
        ob.SRC_STATUS, [(r.get("header"), ob.outcomes(r)) for r in ob.edge_rows(rows) if r["port"] == "is" and r["dir"] == "tx"][:4])
    start = starts[0]
    assert start.get("ref") is not None and start.get("header") and "line" not in start, "the APRS-IS start row: %s" % start
    assert traffic.markers["S"] not in json.dumps(start), "the APRS-IS edge tx row carries the information field"
    writes = ob.late_rows(rows, start)
    assert writes, "no later row with of=%s and ref=%s carries the write outcome; rows of that producer: %s" % (
        start.get("seq"), start.get("ref"), [(r["seq"], r.get("of"), ob.outcomes(r)) for r in rows if r.get("id") == start.get("id")][:6])
    results = [res for w in writes for stage, res in ob.outcomes(w) if stage == "write"]
    assert results, "the late row has no write outcome: %s" % [ob.outcomes(w) for w in writes]
    assert all(res in ob.WRITE_RESULTS for res in results), "write result %s is outside %s" % (results, ob.WRITE_RESULTS)
    assert all("line" not in w for w in writes), "a late write row carries the information field"
    if "framed" not in results:
        note(target, "OBSPKT1-b", "FINDING", "the gated packet on a verified login has write result %s, DV-Router expected 'framed' (read from constants, not a run)" % results)
    else:
        note(target, "OBSPKT1-b", "RECORD", "APRS-IS tx: start row seq %s ref %s, write result %s" % (start.get("seq"), start.get("ref"), results))


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
    # the edge tx start row has the header and the ref and no outcome; txcore / sched / modem outcomes come as LATER body-free rows (`of` = its seq, the
    # same `ref`; tx.rs finish_monitor, shell.rs release_monitor)
    late_tx = [w for r in tx_edge for w in ob.late_rows(rows, r)]
    assert any(ob.has_outcome(w, stage) for w in late_tx for stage in ("txcore", "sched")), "no txcore / sched outcome in a later row: start rows %s, late rows %s" % (
        [(r.get("seq"), r.get("ref"), ob.outcomes(r)) for r in tx_edge], [(w.get("of"), ob.outcomes(w)) for w in late_tx])
    assert all("line" not in w and "header" not in w for w in late_tx), "a late tx row carries a line or header: %s" % late_tx[:1]
    assert any(r.get("ref") == tx_worker[0].get("ref") for r in tx_edge), "the edge tx row does not share the ref %s of the worker row" % tx_worker[0].get("ref")
    note(target, "OBSPKT1-b", "RECORD", "rf tx worker outcomes %s; edge start rows %s; late outcomes %s" % (
        ob.outcomes(tx_worker[0]), [ob.outcomes(r) for r in tx_edge][:3], [ob.outcomes(w) for w in late_tx][:4]))


def test_obspkt1_b_a_local_kiss_client_row_keeps_its_line_but_not_a_position(ctx, traffic, target):
    """data.md 13.5: a local KISS client frame never passes the worker; its AX.25 edge transmit row (dir tx, NO row-level stage, no `ref`) keeps the
    line, except that a spatial information field is blanked after the header and marked `redacted`. The outcome is stage `client` result `queued`; a frame
    the gate refuses carries the refusal as the result (here `src`)."""
    rows = [r for r in ob.edge_rows(traffic.rows) if r["port"] == "rf-a" and r["dir"] == "tx"]
    message = [r for r in rows if traffic.markers["C"] in (r.get("line") or "")]
    assert message, "no client tx row with the message body; client rows: %s" % [r.get("line") or r.get("header") for r in rows][:4]
    assert ob.has_outcome(message[0], "client", "queued"), "client row outcomes: %s" % ob.outcomes(message[0])
    assert "ref" not in message[0] and "stage" not in message[0], "a KISS client tx row has no ref and no row-level stage: %s" % sorted(message[0])
    position = [r for r in rows if r.get("redacted") and r.get("line", "").startswith(dp11e.SRC + ">")]
    assert position, "no redacted client position row; rows: %s" % [(r.get("line"), r.get("redacted")) for r in rows][:4]
    assert ob.info_of(position[0]) == "" and ob.POS_CLIENT[0] not in json.dumps(traffic.rows), "the client position leaked: %s" % position[0]
    assert position[0]["redacted"] == ob.REDACTED
    # a frame the gate refuses never gets a line: kiss_server.rs monitor_refused makes a body-free tx row (port, dir tx, one client outcome, the refusal
    # as the result); the line is built only after the admission checks (kiss_server_gate.rs)
    refused = refused_rows(traffic.rows)
    assert refused, "no client tx row with the refusal `src` (source N0TEST-9 is not in allow_src); client rows: %s" % [
        (r.get("port"), r.get("line") or r.get("header"), ob.outcomes(r)) for r in rows][:6]
    assert all("line" not in r and "header" not in r and "ref" not in r for r in refused), "a refusal row carries a line, header or ref: %s" % refused[:1]
    assert not ob.mentions(traffic.rows, traffic.markers["X"]), "the payload of the refused frame appears in a row"
    assert ob.has_outcome(refused[0], "client", "src") and "src" in ob.CLIENT_REFUSALS, "refused client row outcomes: %s" % ob.outcomes(refused[0])
    assert not ob.has_outcome(refused[0], "client", "queued") and not ob.has_outcome(refused[0], "sched"), "a refused frame must not be queued or released: %s" % ob.outcomes(refused[0])


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
    assert visible and "redacted" not in visible[0], "the visible Mic-E worker row: %s" % visible[:1]
    assert ob.split_tnc2(visible[0]["line"])[1] == traffic.mice_dest_visible, (
        "a worker line shows (mic-e) only when redacted; the visible station's destination is %r, the row says %r" % (traffic.mice_dest_visible, visible[0]["line"]))
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
    texts["public state"] = web_ctx.anon().get("/api/v1/public/state", expect=200).text()
    for route in ("/status", "/clients", "/events", "/lastheard"):
        ctx.admin.step_up()
        resp = ctx.admin.get(ctx.admin.path(route), expect=None)
        texts["admin" + route] = resp.text() if resp.status == 200 else ""
    texts["hub metrics"] = hub.text()
    leaks = ob.find_leaks(texts, markers)
    note(target, "OBSPKT1-e", "RECORD", "scanned %s (%d bytes); leaks: %s" % (sorted(texts), sum(len(t) for t in texts.values()), leaks))
    assert not leaks, "a packet body appears outside the monitor: %s" % leaks
    # The SUT logs are collected on the host after pytest ends: here the needles are left for `python3 -m dvtest.logscan out/<run>`
    # (exit 0 = clean), or the logs are scanned at once when they are readable.
    state, found, looked = logscan.request(ctx.target.out, "OBSPKT1-e", markers)
    if state == "scanned":
        note(target, "OBSPKT1-e", "RECORD", "SUT log scanned here: %d line(s), hits %s" % (looked, {os.path.basename(k): v for k, v in found.items()}))
        assert not found, "a packet body appears in the SUT log: %s" % found
        return
    note(target, "OBSPKT1-e", "RECORD", "the SUT log is not readable in the case; needle file %s" % found)
    pytest.skip("the other texts are clean; the SUT log is collected after the run: run `%s` on the host (needles in %s)" % (logscan.command_for(ctx.target.out), found))


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
        self.times = []                                                 # [(monotonic, event, id or reason)] one entry per frame, for the timeline
        self.t0 = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def on_frame(self, frame):
        self.frames.append(frame)
        try:
            data = json.loads(frame.get("data") or "null")
        except ValueError:
            data = None
        tag = (data.get("id") or data.get("reason")) if isinstance(data, dict) else None
        self.times.append((time.monotonic() - self.t0, frame.get("event", "message"), tag))

    def timeline(self):
        """A compact text of the frames that are not row batches (hello, gap, bye, resync), with the seconds since the stream was opened, and the row
        batch counts per producer id."""
        rows = {}
        for _t, event, tag in self.times:
            if event == "rows":
                rows[tag] = rows.get(tag, 0) + 1
        other = ["%.0fs %s %s" % (t, event, tag) for t, event, tag in self.times if event != "rows"]
        return "%s; row batches %s" % (other, rows)

    def gap_seen(self, ids):
        return any(event == "gap" and tag in ids for _t, event, tag in self.times)

    def ended(self):
        return [(event, tag) for _t, event, tag in self.times if event in ("bye", "resync")]

    def run(self):
        try:
            self.admin.sse(self.admin.path(STREAM), max_events=100000, timeout=900.0, on_frame=self.on_frame, stop=self.stop)
        except Exception:                                               # noqa: BLE001  the stream ending is not a failure of the reader
            pass

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5.0)


def evicted_total(hub):
    """(dv_pkt_monitor_evicted_total, dv_edge_monitor_evicted_total) of the hub metrics, or None for a family that cannot be read."""
    out = []
    for family in ("dv_pkt_monitor_evicted_total", "dv_edge_monitor_evicted_total"):
        try:
            out.append(hub.total(family))
        except Exception:                                               # noqa: BLE001  the family may be absent; the record then says so
            out.append(None)
    return tuple(out)


def boots(body):
    return {p["id"]: tuple(p["boot"]) for p in body["producers"]}


def test_obspkt1_d_a_process_restart_clears_its_rings(ctx, traffic, rig_control, hub, target):
    """data.md 13.5: each producer keeps an always-on memory-only ring; a new process boot starts empty. Confirmed by DV-Router: a WORKER restart keeps the
    id and gives a new boot (stream: `gap` {id}, then rows with the new boot); an EDGE restart gives a new id (`<name>-<edge_inc>`): a `gap` for the OLD id,
    and the new id appears in rows without a gap (it was not in `hello`). The data worker is killed with the allowed kill-proc (the launcher respawns it);
    then the AX.25 child is restarted through the admin API (as DP15-6g). The rings live in the child processes, so no container restart is needed.
    On wip-16e8e6c the `gap` for the OLD AX.25 id is a recorded FINDING (OBSPKT1b: the admin poller treats a discovery error as an empty producer list,
    packets.rs:162, so the gaps come from re-bootstrapping and an id the hub still lists never gets one); every other assertion is hard. The case
    records what is needed to judge it: the hello list and the stream open time, the producer ids across the restart, the monitor eviction counters
    before and after, whether the rows after a gap replay old sequences, and a needle for an admin `query failed` log line."""
    case = "OBSPKT1-d"
    markers = [traffic.markers["S"], traffic.markers["C"], traffic.markers["I"]]
    resp, before = snapshot(ctx.admin, limit=1000)
    worker_ids = [p["id"] for p in before["producers"] if ob.producer_kind(p["id"]) == "worker"]
    edge_ids = [p["id"] for p in before["producers"] if ob.producer_kind(p["id"]) == "edge"]
    old_ax = [e for e in edge_ids if ob.edge_name(e) == "ax25"]
    assert worker_ids and old_ax, "producers seen: %s" % [p["id"] for p in before["producers"]]
    assert any(ob.mentions(ob.worker_rows(before["rows"]), m) for m in markers) and ob.edge_rows(before["rows"]), "nothing to clear"
    boot0 = boots(before)
    evicted0 = evicted_total(hub)
    producers_seen = [(0.0, sorted(p["id"] for p in before["producers"]))]

    def see(body, t_zero):
        ids = sorted(p["id"] for p in body["producers"])
        if ids != producers_seen[-1][1]:
            producers_seen.append((time.monotonic() - t_zero, ids))

    admin = ctx.admin_session("admin")                          # a second login for the restart call, made BEFORE the stream opens
    with Streamer(ctx.admin) as stream:
        time.sleep(5.0)
        t_stream = stream.t0
        rig_control.kill_proc("xlxd-sut", "dv-data-worker", "KILL")
        end, after = time.monotonic() + 90.0, None
        while time.monotonic() < end and after is None:
            time.sleep(3.0)
            ctx.admin.step_up()
            r, body = snapshot(ctx.admin, limit=1000)
            if r.status == 200:
                see(body, t_stream)
            if r.status == 200 and any(boots(body).get(w) not in (None, boot0[w]) for w in worker_ids):
                after = body
        assert after is not None, "the worker boot did not change within 90 s of the kill"
        new_boot = boots(after)
        note(target, case, "RECORD", "worker boots: %s -> %s" % ({k: v[:2] for k, v in boot0.items() if k in worker_ids}, {k: v[:2] for k, v in new_boot.items() if k in worker_ids}))
        assert all(w in new_boot for w in worker_ids), "the worker id changed with the restart (it must stay): %s -> %s" % (worker_ids, sorted(new_boot))
        for m in markers:
            assert not ob.mentions(ob.worker_rows(after["rows"]), m), "a worker row survived the worker restart: %s" % ob.mentions(ob.worker_rows(after["rows"]), m)[:1]
        assert ob.edge_rows(after["rows"]) and all(new_boot.get(e) == boot0[e] for e in edge_ids), "the edge rings or boots changed with the worker restart"
        admin.step_up()
        child = admin.post(admin.path("/children/%s/restart" % txguard.AX25_CHILD), {}, expect=None)
        assert child.status == 200, "restart of %s answered HTTP %d" % (txguard.AX25_CHILD, child.status)
        t_restart = time.monotonic()
        end, final, new_ax = time.monotonic() + 90.0, None, []
        while time.monotonic() < end and final is None:
            time.sleep(3.0)
            ctx.admin.step_up()
            r, body = snapshot(ctx.admin, limit=1000)
            ids = [p["id"] for p in body["producers"]] if r.status == 200 else []
            if r.status == 200:
                see(body, t_stream)
            new_ax = [i for i in ids if ob.edge_name(i) == "ax25" and i not in old_ax]
            if new_ax:
                final = body
        assert final is not None, "no new AX.25 producer id appeared within 90 s of the restart (the edge id changes with the process)"
        # The poller retires a producer only when the producer list (the hub process table, which lags about 10 s) stops naming it: the `gap` of the OLD
        # id comes then, so the stream is kept open until it arrives (or ends, or 40 s pass).
        end = time.monotonic() + 40.0
        while time.monotonic() < end and not stream.gap_seen(old_ax) and not stream.ended():
            ctx.admin.step_up()
            r, body = snapshot(ctx.admin, limit=1000)
            if r.status == 200:
                see(body, t_stream)
            time.sleep(1.0)
        note(target, case, "RECORD", "%.0f s after the restart call: old ids %s, new ids %s, gap for the old id seen: %s" % (
            time.monotonic() - t_restart, old_ax, new_ax, stream.gap_seen(old_ax)))
        assert not [r for r in final["rows"] if r.get("id") in old_ax], "rows of the old AX.25 id survived the restart"
        assert not any(ob.mentions([r for r in final["rows"] if ob.producer_kind(r.get("id")) == "edge"], m) for m in (traffic.markers["S"], traffic.markers["C"])), (
            "old AX.25 edge rows survived the restart")
        time.sleep(3.0)
    events = ob.parse_stream(stream.frames)
    note(target, case, "RECORD", "stream: hello %s, gaps %s, rows for %s, byes %s, resync %s; new AX.25 id %s" % (
        [p.get("id") for p in events.hello or []], events.gaps, sorted({r[0] for r in events.rows}), events.byes, events.resync, new_ax))
    note(target, case, "RECORD", "stream timeline: %s" % stream.timeline())
    hello_at = next((t for t, event, _tag in stream.times if event == "hello"), None)
    note(target, case, "RECORD", "hello at %s s after the stream was opened: %s; producer ids seen by the snapshots (seconds since the stream opened): %s" % (
        "%.1f" % hello_at if hello_at is not None else "?", [p.get("id") for p in events.hello or []], [("%.0f" % t, ids) for t, ids in producers_seen]))
    evicted1 = evicted_total(hub)
    note(target, case, "RECORD", "monitor eviction counters before %s, after %s" % (evicted0, evicted1))
    for pid in worker_ids:
        rep_ = ob.replay_after_gap(stream.frames, pid)
        note(target, case, "RECORD", "rows around the gap of %s: gap %s, before %s, after %s, replayed (boot, seq) pairs %d" % (
            pid, rep_["gap"], rep_["before"][-2:], rep_["after"][:3], len(rep_["replayed"])))
    out_dir = getattr(target, "out", None)
    if out_dir:
        state, found, looked = logscan.request(out_dir, "OBSPKT1-d-query-failed", ["query failed"])
        if state == "scanned":
            note(target, case, "RECORD", "admin log lines with `query failed`: %s" % {os.path.basename(k): v[:3] for k, v in found.items()})
        else:
            note(target, case, "RECORD", "admin log scan left for the host: `%s` (a FOUND for OBSPKT1-d-query-failed is the finding, not a failure)" % logscan.command_for(out_dir))
    assert any(w in events.gaps for w in worker_ids), "no `gap` {id} for the restarted worker: gaps %s" % events.gaps
    if any(e in events.gaps for e in old_ax):
        note(target, case, "RECORD", "the OLD AX.25 id %s got a gap, as data.md 13.5 and the DV-Router note describe" % old_ax)
    else:
        note(target, case, "FINDING", "OBSPKT1b: no `gap` for the OLD AX.25 id %s after its restart (gaps %s; stream ended with %s; the hub process table may still list "
              "the id, or the poller re-bootstrapped instead); timeline %s" % (old_ax, events.gaps, stream.ended(), stream.timeline()))
    if any(n in events.gaps for n in new_ax):
        note(target, case, "FINDING", "OBSPKT1b: a gap was reported for the NEW AX.25 id %s: gaps %s (the poller re-bootstraps every producer with a cursor when discovery fails)" % (
            new_ax, events.gaps))
    assert not any(n in [p.get("id") for p in events.hello or []] for n in new_ax), "the new AX.25 id was already in hello"
