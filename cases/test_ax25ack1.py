# SPDX-License-Identifier: MIT
"""AX25ACK1 and TXOFF2 (DV-Router 345f1ebe, in bc9af2b1, image dv-router:wip-f1f3777): the AX.25 edge transmits only with the exact control-operator sentence in
`[edges.ax25] tx_ack` (absent: dormant with a warning, wrong: an error); the transmit gates are one predicate; a per-port and a master Off through
`POST /api/v1/admin/radio/tx` work, and an Off on an INVALID file rebases on the accepted running source. Document: docs/design/config.md ("tx_ack"), radio.md 10.1,
openapi-admin; docs/ax25ack1-cases.md. Pure helpers: dvtest/ax25ack1.py. Switch DVT_AX25ACK1=on. NOT RUN on a rig: written against the sources of bc9af2b1.

Shape: the DP15 RF shape with TX on (DVT_DP15_TX_ENABLE=true, dp15-rf.toml + dp15-rf-out.toml, fake KISS TNCs, web profile, DVT_SUT_CONFIG_RW=1; the renderer adds the
tx_ack line under the same switch).

  AX25ACK1-a   the rendered config carries the exact sentence; the status row says acknowledged; one room text is transmitted at rf-a
  AX25ACK1-b   an empty tx_ack (config change) is dormant: the change loads with the RX only warning, the edge restarts, the port label is off, nothing is transmitted;
               the exact sentence put back transmits again
  AX25ACK1-c   a wrong sentence is refused (the launcher's diagnostic), the file etag and the edge stay
  TXOFF2-a     a per-port Off (operator, no step-up) restarts the edge, the port reads off, nothing is transmitted; On without the sentence is refused, On with it transmits
  TXOFF2-b     the master Off (no port) does the same for every port
  TXOFF2-c     an Off on an INVALID file rebases: 200, rebased true, history_seq; the replaced file is in history (needs the rw config file)
"""
import os
import sys
import time
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import ax25ack1, txguard  # noqa: E402

import data_support as ds  # noqa: E402
import dp18_support as dc  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_rf as tr  # noqa: E402

from test_dp15_rf import hub, rf, web_ctx  # noqa: E402,F401

pytestmark = [pytest.mark.rig, pytest.mark.web]

LOG = "ax25ack1.txt"
PORT = "rf-a"
SILENCE_S = 20.0
RESTART_WAIT_S = 60.0


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target):
    if not ax25ack1.strict_on():
        pytest.skip("DVT_AX25ACK1=on is not set (the image under test has AX25ACK1 + TXOFF2, DV-Router 345f1ebe: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native with --profile web")


@pytest.fixture(scope="module")
def ctx(target, hub, rf, web_ctx, admin_session):
    """The RF TX shape and the admin sessions; skips with the reason otherwise."""
    if not (rf.tx_on and rf.has_out_path):
        pytest.skip("RF transmit is off in this run: DVT_DP15_TX_ENABLE=true with dp15-rf.toml and dp15-rf-out.toml (docs/ax25ack1-cases.md)")
    doc = p1.read_config(target)
    operator, admin = admin_session("operator"), admin_session("admin")
    return type("Ctx", (), dict(doc=doc, rf=rf, web=web_ctx, operator=operator, admin=admin, hub=hub, idx=dc.port_index(doc, PORT),
                                call=str(((doc.get("edges") or {}).get("ax25") or {}).get("callsign", ""))))()


def status_of(client):
    resp = client.get(client.path("/status"), expect=None)
    return resp.json() if resp.status == 200 else {}


def child(ctx):
    return dc.child_state(status_of(ctx.operator), txguard.AX25_CHILD)


def config_state(ctx):
    resp = ctx.admin.get(ctx.admin.path("/config"), expect=None)
    assert resp.status == 200, "GET config answered HTTP %d" % resp.status
    meta = (resp.json() or {}).get("meta") or {}
    return dc.etag_of(resp), meta.get("writable", True)


def need_writable(ctx):
    etag, writable = config_state(ctx)
    if not writable:
        pytest.skip("the configuration file is not writable: run with DVT_SUT_CONFIG_RW=1")
    return etag


def wait_restart(ctx, before, case, target):
    end = time.monotonic() + RESTART_WAIT_S
    after = child(ctx)
    while not dc.restarted(before, after) and time.monotonic() < end:
        time.sleep(1.0)
        after = child(ctx)
    assert dc.restarted(before, after), "%s: the AX.25 edge was not restarted (%s -> %s)" % (case, before, after)
    result = txguard.wait_unfenced(txguard.ax25_reader(ctx.operator, PORT), kind="ax25")
    note(target, case, "RECORD", txguard.describe(result, "ax25", PORT))
    return after


def room_text(ctx, tag):
    """POST a nonce text into module B; (body, t0, status)."""
    tr.wait_tx_spacing(ctx.rf)
    body = ds.nonce_body(tag)
    t0, _t1, status = tr.post(ctx.web.posters[0], body)
    return body, t0, status


def transmits(ctx, tag, wait_s=tr.TX_WAIT_S):
    """True when one room text appears as a data frame at rf-a within wait_s."""
    _body, t0, status = room_text(ctx, tag)
    assert status == 202, "POST answered %s" % status
    return bool(ctx.rf.a.wait_data(1, timeout=wait_s, since=t0))


def silent(ctx, tag, case, target):
    """No data frame at either TNC for SILENCE_S after one accepted room text."""
    _body, t0, status = room_text(ctx, tag)
    assert status == 202, "%s: POST answered %s" % (case, status)
    time.sleep(SILENCE_S)
    frames = ctx.rf.a.data_frames(t0) + ctx.rf.b.data_frames(t0)
    note(target, case, "RECORD", "room text accepted (POST %s); frames at the TNCs in %.0f s: %d" % (status, SILENCE_S, len(frames)))
    assert frames == [], "%s: RF data frames while TX is off: %s" % (case, [f[2] for f in frames])


def post_tx(client, body, step_up=False):
    if step_up:
        client.step_up()
    return client.post(client.path("/radio/tx"), body, expect=None)


# ---------------------------------------------------------------- AX25ACK1

def test_ax25ack1_a_the_exact_sentence_transmits(rig, target, ctx):
    case = "AX25ACK1-a"
    problems = ax25ack1.config_ack_problems(ctx.doc)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
    status = status_of(ctx.operator)
    info = ax25ack1.ax25_info(status)
    note(target, case, "RECORD", "status ax25 %s; port %s tx label %s" % (info, PORT, ax25ack1.port_tx(status, PORT)))
    assert info is not None and info.get("acknowledged") is True and info.get("tx_enable") is True, "%s: the AX.25 row says %s" % (case, info)
    assert ax25ack1.port_tx(status, PORT) == "on", "%s: the port label is %r" % (case, ax25ack1.port_tx(status, PORT))
    tr.need_tx(ctx.rf, "ack1-a")
    assert transmits(ctx, "ACKA"), "%s: with the exact tx_ack no room text was transmitted at %s within %.0f s" % (case, PORT, tr.TX_WAIT_S)


def test_ax25ack1_b_an_absent_sentence_is_dormant(rig, target, ctx):
    case = "AX25ACK1-b"
    etag = need_writable(ctx)
    sentence = ax25ack1.ack_sentence(ctx.call)
    before = child(ctx)
    ctx.admin.step_up()
    resp = ctx.admin.post(ctx.admin.path("/config/changes"), dc.replace_request("/edges/ax25/tx_ack", "", etag, "dvt AX25ACK1-b empty"), expect=None)
    note(target, case, "RECORD", "replace tx_ack with empty: HTTP %d; diagnostics %s" % (resp.status, ax25ack1.diagnostics(resp.json() if resp.status in (200, 422) else None)[:4]))
    assert resp.status == 200, "%s: emptying tx_ack answered HTTP %d: %s (an absent acknowledgement is dormant, not an error)" % (case, resp.status, resp.text()[:300])
    try:
        wait_restart(ctx, before, case, target)
        status = status_of(ctx.operator)
        info = ax25ack1.ax25_info(status)
        assert info is not None and info.get("acknowledged") is False, "%s: the AX.25 row says %s, want acknowledged false" % (case, info)
        assert ax25ack1.port_tx(status, PORT) == "off", "%s: the port label is %r, want off" % (case, ax25ack1.port_tx(status, PORT))
        silent(ctx, "ACKB", case, target)
    finally:
        etag2, _w = config_state(ctx)
        before2 = child(ctx)
        ctx.admin.step_up()
        back = ctx.admin.post(ctx.admin.path("/config/changes"), dc.replace_request("/edges/ax25/tx_ack", sentence, etag2, "dvt AX25ACK1-b restore"), expect=None)
        assert back.status == 200, "%s: restoring the sentence answered HTTP %d: %s" % (case, back.status, back.text()[:300])
        wait_restart(ctx, before2, case + " restore", target)
    assert transmits(ctx, "ACKB2"), "%s: after the sentence was put back no room text was transmitted" % case


def test_ax25ack1_c_a_wrong_sentence_is_refused(rig, target, ctx):
    case = "AX25ACK1-c"
    etag = need_writable(ctx)
    wrong = ax25ack1.ack_sentence("N0TEST-9" if ctx.call != "N0TEST-9" else "N0TEST-8")
    before = child(ctx)
    ctx.admin.step_up()
    resp = ctx.admin.post(ctx.admin.path("/config/changes"), dc.replace_request("/edges/ax25/tx_ack", wrong, etag, "dvt AX25ACK1-c wrong"), expect=None)
    time.sleep(3.0)
    etag_after, _w = config_state(ctx)
    after = child(ctx)
    note(target, case, "RECORD", "wrong sentence: HTTP %d, diagnostics %s; etag moved %s; edge restarted %s" % (resp.status, ax25ack1.diagnostics(resp.json() if resp.status in (409, 422) else None)[:3], etag != etag_after, dc.restarted(before, after)))
    assert resp.status >= 400, "%s: a wrong sentence was accepted (HTTP %d)" % (case, resp.status)
    assert ax25ack1.WRONG_ACK_ERROR in resp.text(), "%s: the refusal does not carry %r: %s" % (case, ax25ack1.WRONG_ACK_ERROR, resp.text()[:300])
    assert etag_after == etag, "%s: a refused change moved the etag" % case
    assert not dc.restarted(before, after), "%s: the AX.25 edge restarted after a refused change" % case


# ---------------------------------------------------------------- TXOFF2

def test_txoff2_a_a_port_off_and_on(rig, target, ctx):
    case = "TXOFF2-a"
    need_writable(ctx)
    assert ctx.idx is not None, "%s: no port %s in the rendered config" % (case, PORT)
    sentence = ax25ack1.ack_sentence(ctx.call)
    before = child(ctx)
    resp = post_tx(ctx.operator, ax25ack1.radio_tx_body(False, port=ctx.idx))
    note(target, case, "RECORD", "port %d Off (operator, no step-up): HTTP %d %s" % (ctx.idx, resp.status, resp.text()[:200]))
    assert resp.status == 200, "%s: Off answered HTTP %d: %s" % (case, resp.status, resp.text()[:300])
    wait_restart(ctx, before, case, target)
    assert ax25ack1.port_tx(status_of(ctx.operator), PORT) == "off", "%s: the port label is not off after Off" % case
    silent(ctx, "OFFA", case, target)
    refused = post_tx(ctx.admin, ax25ack1.radio_tx_body(True, port=ctx.idx), step_up=True)
    note(target, case, "RECORD", "On without the sentence: HTTP %d %s" % (refused.status, refused.text()[:200]))
    assert refused.status != 200, "%s: On without the control-operator sentence was accepted" % case
    before2 = child(ctx)
    on = post_tx(ctx.admin, ax25ack1.radio_tx_body(True, port=ctx.idx, ack=sentence), step_up=True)
    assert on.status == 200, "%s: On with the exact sentence answered HTTP %d: %s" % (case, on.status, on.text()[:300])
    wait_restart(ctx, before2, case + " on", target)
    assert transmits(ctx, "OFFA2"), "%s: after On with the sentence no room text was transmitted" % case


def test_txoff2_b_the_master_off_and_on(rig, target, ctx):
    case = "TXOFF2-b"
    need_writable(ctx)
    sentence = ax25ack1.ack_sentence(ctx.call)
    before = child(ctx)
    resp = post_tx(ctx.operator, ax25ack1.radio_tx_body(False))
    assert resp.status == 200, "%s: master Off answered HTTP %d: %s" % (case, resp.status, resp.text()[:300])
    wait_restart(ctx, before, case, target)
    info = ax25ack1.ax25_info(status_of(ctx.operator))
    note(target, case, "RECORD", "status ax25 after the master Off: %s" % info)
    assert info is not None and info.get("tx_enable") is False, "%s: the AX.25 row says %s, want tx_enable false" % (case, info)
    silent(ctx, "OFFM", case, target)
    before2 = child(ctx)
    on = post_tx(ctx.admin, ax25ack1.radio_tx_body(True, ack=sentence), step_up=True)
    assert on.status == 200, "%s: master On answered HTTP %d: %s" % (case, on.status, on.text()[:300])
    wait_restart(ctx, before2, case + " on", target)
    assert transmits(ctx, "OFFM2"), "%s: after the master On no room text was transmitted" % case


def test_txoff2_c_off_on_an_invalid_file_rebases(rig, target, ctx):
    case = "TXOFF2-c"
    need_writable(ctx)
    rw = Path(target.out) / "xlxd-config" / "xlxd-sut" / "rw" / "dv-router.toml"
    if not rw.is_file() or not os.access(rw, os.W_OK):
        pytest.skip("the rw config file %s is not writable by the tester (DVT_SUT_CONFIG_RW=1 puts it there)" % rw)
    sentence = ax25ack1.ack_sentence(ctx.call)
    original = rw.read_text(encoding="utf-8")
    rw.write_text(original + "\ndvt_not_a_key = 1\n", encoding="utf-8")
    before = child(ctx)
    try:
        resp = post_tx(ctx.operator, ax25ack1.radio_tx_body(False))
        body = resp.json() if resp.status == 200 else {}
        note(target, case, "RECORD", "master Off on an invalid file: HTTP %d, rebased %r, history_seq %r" % (resp.status, body.get("rebased"), body.get("history_seq")))
        problems = ax25ack1.rebase_problems(resp.status, body)
        assert not problems, "%s: %s: %s" % (case, "; ".join(problems), resp.text()[:300])
        now = rw.read_text(encoding="utf-8")
        assert "dvt_not_a_key" not in now, "%s: the invalid key is still in the file after the rebase" % case
        assert tomllib.loads(now)["edges"]["ax25"]["tx_enable"] is False, "%s: the rebased file has tx_enable true" % case
        history = ctx.admin.get(ctx.admin.path("/config/history/%d" % body["history_seq"]), expect=None)
        note(target, case, "RECORD", "history entry %s: HTTP %d" % (body["history_seq"], history.status))
        if history.status == 200:
            assert "dvt_not_a_key" in history.text(), "%s: the replaced (invalid) file is not in history entry %s" % (case, body["history_seq"])
        wait_restart(ctx, before, case, target)
        silent(ctx, "OFFI", case, target)
    finally:
        etag, _w = config_state(ctx)
        before2 = child(ctx)
        on = post_tx(ctx.admin, ax25ack1.radio_tx_body(True, ack=sentence), step_up=True)
        assert on.status == 200, "%s: restoring the master On answered HTTP %d: %s" % (case, on.status, on.text()[:300])
        wait_restart(ctx, before2, case + " on", target)
