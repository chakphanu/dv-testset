# SPDX-License-Identifier: MIT
"""DV-Router DP7 position cases, SECOND SET (image with MAP1, ETAG1, W8A1, XCOBS1, LSTOP1, DPRS1; tag not known when this was
written). The first set is cases/test_dp15_positions.py (public rows, drop switches, ambiguity, object kill); this file covers
what its "Not covered" list named, except the browser DOM (cases/test_dp7_positions_page.py) and the public ETag
(cases/test_web_etag_diag.py). Case table, run commands, UNVERIFIED shapes: docs/dp7-cases.md ("DP7 second set").

  DP7-e  IS-to-RF courtesy posit (data.md 9.3 / 8.3 "IS to RF", config.md `courtesy_posit`, worker core_rf_tx.rs):
         a message from an APRS-IS station to an RF-heard station is gated to RF (third-party frame at the fake KISS TNC); with
         courtesy_posit = true the NEXT position of that station on APRS-IS is transmitted once as a courtesy position;
         never without a gated message (no credit), never twice for one message (one-use credit), never when
         [data] positions = false, the base callsign is in position_optout or its account has hide_position, never when the
         path has courtesy_posit = false, and nothing at all when RF transmit is off
  DP7-f  hide_position for RF: an RF-heard position and object of a callsign whose ACCOUNT has hide_position is dropped
         (room admission, counters + absence) and a hidden callsign's RF transit to APRS-IS never reaches the private aprsc;
         after the flag is cleared the RF position is public again
  DP7-g  SSE replay after a hide: a hidden station's position is absent from a NEW public SSE connection that resumes
         (last_event_id) from before it, and from the public state; the control station's is replayed; after the flag is
         cleared the retained row is back (design-implied: web filters retained rows with its CURRENT hidden set)
  DP7-h  APRS items (`)NAME!pos`) over M17 packet mode and RF: public like objects, kill is coordinate-free
  DP7-i  retention: the hub keeps a position 30 minutes (a constant, NOT configurable: config.md has no key); the case is
         opt-in (DVT_DP7_RETENTION_LONG=1, about 35 minutes), otherwise skipped with that reason

Run shapes (docs/dp7-cases.md): the courtesy cases need one of
  tx on + path courtesy_posit true   (dp7-courtesy-tx.toml, dp7-courtesy-filter.toml, dp7-courtesy-path.toml)
  tx on + path courtesy_posit false  (... dp7-courtesy-path-off.toml INSTEAD of dp7-courtesy-path.toml)
  tx off, receive-only port          (dp7-rf.toml + dp7-courtesy-filter.toml)
and position_optout from dp7-position-optout.toml; everything with the profile web,aprs. The cases read the RENDERED config
and skip with the reason in the shapes that do not fit. Observations: stdout and out/<run>/dp7-positions2.txt ("RECORD",
"FINDING").

NOT RUN ON A RIG yet. Every shape that could not be verified from the sources is listed in docs/dp7-cases.md.
"""
import contextlib
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprsis_client as ai, kiss_tnc, web  # noqa: E402

import data_support as ds  # noqa: E402
import position2_support as p2  # noqa: E402
import txguard_support  # noqa: E402
import position_support as ps  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
# fixtures of the first set, reused by name: the hub metrics, the web side and the private aprsc (its `shape` argument is
# the fixture of THIS module, which has the fields it needs)
from test_dp15_positions import aprsc, hub, web_ctx  # noqa: E402,F401

pytestmark = pytest.mark.rig

MODULE = p1.MODULE
LOG = "dp7-positions2.txt"
SETTING = "require_ident=%s" % os.environ.get("DVT_REQUIRE_IDENT", "true")
CONNECT_S = p1.CONNECT_S
TX_WAIT_S = 25.0                    # a transmit passes the PTT snapshot, the scheduler and the min gap (cases/test_dp15_rf.py)
QUIET_S = 14.0                      # how long "nothing is transmitted" is watched
HEARD_SETTLE_S = 2.0                # the worker has the RF station in its heard table
PUSH_S = 3.0                        # a hide_position change reaches the worker (web/position-policy-updates)
TOCALL = "APDVT1"                   # destination of the tester's own APRS-IS packets
RX_STATION = "N0PRX-2"              # the RF-heard addressee of the gated message
GATEWAY = "N0TEST-1"                # [edges.ax25] callsign: the transmitting station
CORR_OK = "N0PSD"                   # correspondent of the positive case
CORR_NOCREDIT = "N0PSN"             # sends a position without a gated message before
CORR_OFF = "N0PSE"                  # path with courtesy_posit = false
CORR_TXOFF = "N0PSF"                # RF transmit off
CORR_POSOFF = "N0PSB"               # [data] positions = false
CORR_HIDE = "N0PSH"                 # account with hide_position
HIDE_RF_CALL = "N0PFH"              # account of the RF hide case
HIDE_RF_CONTROL = "N0PFC"           # control of the RF transit
SSE_MARK, SSE_CONTROL, SSE_HIDE = "N0PGM", "N0PGK", "N0PGH"
ITEM_CALL = {"m17": "N0PIM", "rf": "N0PIR"}
ITEM_NAME = {"m17": "DVT7ITM", "rf": "DVT7ITR"}
RETENTION_CALL = "N0PRT"


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


# ---------------------------------------------------------------- the run shape (rendered config)

def shape2_of(doc):
    """What the cases need from the RENDERED dv-router.toml (a dict, {} when unreadable): positions switch, opt-out base
    callsigns, the receive port rf-a (room bound) and whether it transmits, the IS-to-RF path and its courtesy flag, the RF-to-IS
    transit path, the APRS-IS edge and its extra filter."""
    data = doc.get("data") or {}
    edges = doc.get("edges") or {}
    ax = edges.get("ax25") if isinstance(edges.get("ax25"), dict) else {}
    port = next((p for p in (ax.get("port") or []) if p.get("name") == "rf-a" and p.get("kind") == "kiss-tcp"), None)
    rf = bool(ax.get("enable")) and port is not None and port.get("room") == MODULE and bool(port.get("port"))
    paths = list(data.get("path") or [])
    to_rf = next((p for p in paths if p.get("from") == "edge:aprsis" and str(p.get("to", "")).startswith("edge:ax25")
                  and p.get("enable", True) is not False), None)
    aprsis = edges.get("aprsis") if isinstance(edges.get("aprsis"), dict) else {}
    return types.SimpleNamespace(
        known=bool(doc), positions=data.get("positions", True) is not False,
        optout={ps.base_call(c) for c in (data.get("position_optout") or [])},
        rf=rf, rf_port=int(port["port"]) if rf else None,
        tx_on=rf and bool(ax.get("tx_enable")) and bool(port.get("tx")),
        to_rf=to_rf is not None, courtesy=bool(to_rf and to_rf.get("courtesy_posit")),
        aprsis=bool(aprsis.get("enable")), filter_extra=str(aprsis.get("filter_extra", "")),
        igate=any(p.get("from") == "edge:ax25/rf-a" and p.get("to") == "edge:aprsis" and "transit" in (p.get("types") or []) for p in paths))


@pytest.fixture(scope="module")
def shape(target):
    return shape2_of(p1.read_config(target))


@pytest.fixture(scope="module")
def tnc(request, target, shape, hub):
    """The fake KISS TNC rf-a (the AX.25 edge connects to it as a kiss-tcp client); skips without a room-bound port rf-a."""
    if not shape.rf:
        pytest.skip("the rendered config has no AX.25 edge with the room-bound kiss-tcp port rf-a: add rig/config/dv-router/"
                    "dp7-courtesy-tx.toml (transmit) or dp7-rf.toml (receive only)")
    fake = kiss_tnc.FakeKissTnc("0.0.0.0", shape.rf_port, name="rf-a")
    fake.start()
    try:
        if not fake.wait_connected(CONNECT_S):
            pytest.fail("the AX.25 edge did not connect to the fake TNC rf-a (port %s) in %.0f s" % (shape.rf_port, CONNECT_S))
        note(target, "rf", "RECORD", "AX.25 edge connected to rf-a; tx_on %s, IS-to-RF path %s (courtesy_posit %s), positions %s, optout %s, "
             "aprsis filter_extra %r" % (shape.tx_on, shape.to_rf, shape.courtesy, shape.positions, sorted(shape.optout), shape.filter_extra))
        fake.tx_guard = txguard_support.make_guard(request, lambda case, tag, text: note(target, case, tag, text))
        yield fake
    finally:
        fake.stop()


# ---------------------------------------------------------------- RF side: hearing and transmitted frames

def rf_hear(tnc, station=RX_STATION):
    """The fake TNC hears `station` (a status beacon: identical frames are deduplicated for 30 s, so the text is unique)."""
    info = ">dp7 heard %04d" % (int(time.time() * 10) % 10000)
    assert tnc.inject(kiss_tnc.ui_frame(station, TOCALL, info, digis=("WIDE1-1",))), "the fake TNC rf-a has no connection to inject into"
    time.sleep(HEARD_SETTLE_S)


def tx_frames(tnc, since):
    """[(arrival time, parsed frame, third-party dict)] of the data frames the edge transmitted since `since` whose information
    field is a third-party packet; the rest (beacons, acks) is ignored."""
    out = []
    for at, parsed, _text in tnc.decoded(since):
        third = p2.third_party(parsed["info"])
        if third is not None:
            out.append((at, parsed, third))
    return out


def tx_from(tnc, since, call, positions=None):
    """The transmitted third-party frames of the base callsign `call`; `positions` True keeps positions, False keeps the rest."""
    hits = [x for x in tx_frames(tnc, since) if ps.same_base(x[2]["inner_src"], call)]
    if positions is not None:
        hits = [x for x in hits if p2.is_position_info(x[2]["inner_info"]) == positions]
    return hits


def wait_tx(tnc, since, call, positions, timeout=TX_WAIT_S):
    tnc.wait(lambda t: bool(tx_from(t, since, call, positions)), timeout)
    return tx_from(tnc, since, call, positions)


# ---------------------------------------------------------------- APRS-IS side: one correspondent per login

@contextlib.contextmanager
def correspondent(host, call):
    """A verified login on the private aprsc under the correspondent's own callsign (the packets' source equals the login, as
    in cases/test_dp15_aprsis.py: UNVERIFIED that the server would pass another source)."""
    with ai.AprsisClient(host, login=call, filter="") as client:
        resp = client.connect()
        assert resp["verified"], resp
        yield client


def send_is_message(client, call, text, msgid="1"):
    line = ai.message_line(call, RX_STATION, text, msgid, dest=TOCALL)
    client.send_line(line)
    return line


def send_is_position(client, call, comment):
    info = ps.position_info(ps.LAT, ps.LON, 0, "/>", comment)
    client.send_line("%s>%s,TCPIP*:%s" % (call, TOCALL, info))
    return info


def nonce():
    return "%04d" % (int(time.time() * 10) % 10000)


def settle_counters(hub, target, case, before):
    """RECORD the worker's IS-to-RF counters (published every 10 s): result of dv_pkt_rf_downlink_total and the reasons of
    dv_pkt_rf_drop_total. Names from core_rf_tx.rs, UNVERIFIED on a running hub."""
    time.sleep(11.0)
    after = {r: hub.total("dv_pkt_rf_drop_total", reason=r) for r in ("courtesy_unavailable", "courtesy_full", "query_rate")}
    results = {r: hub.total("dv_pkt_rf_downlink_total", result=r) for r in ("not_heard", "rate", "dedupe")}
    note(target, case, "RECORD", "dv_pkt_rf_drop_total %s (before %s); dv_pkt_rf_downlink_total %s" % (after, before, results))
    return after


def drop_baseline(hub):
    return {r: hub.total("dv_pkt_rf_drop_total", reason=r) for r in ("courtesy_unavailable", "courtesy_full", "query_rate")}


def check_message_frame(frame, call, text):
    """The gated message as transmitted: GATEWAY>APZDVR[,path]:}CALL>APDVT1,TCPIP,GATEWAY*::N0PRX-2  :text{1 (data.md 9.3 item 2)."""
    at, parsed, third = frame
    assert parsed["src"]["text"] == GATEWAY and parsed["dest"]["text"] == "APZDVR", \
        "outer header %s>%s, want %s>APZDVR" % (parsed["src"]["text"], parsed["dest"]["text"], GATEWAY)
    assert third["inner_src"] == call, third
    assert third["inner_path"][:2] == ["TCPIP", GATEWAY + "*"], "inner path %s, want TCPIP,%s*" % (third["inner_path"], GATEWAY)
    msg = ai.parse_message(third["inner_info"])
    assert msg is not None and msg[0] == RX_STATION and msg[1] == text, "inner information field %r" % third["inner_info"]


def check_courtesy_frame(frame, call, info):
    at, parsed, third = frame
    assert parsed["src"]["text"] == GATEWAY and parsed["dest"]["text"] == "APZDVR", (parsed["src"]["text"], parsed["dest"]["text"])
    assert third["inner_src"] == call and third["inner_path"][:2] == ["TCPIP", GATEWAY + "*"], third
    assert third["inner_info"] == info, "courtesy position %r, want the position as the station sent it %r" % (third["inner_info"], info)


def need_courtesy_shape(shape, positive=True, tnc=None, case="DP7-e"):
    """Skip unless the courtesy shape is on; then wait for the startup / recovery TX guard of the edge (RFSAFE1, radio.md 10.4) to
    end before the case provokes its first transmission. Call the guard again after any reload or edge restart."""
    if not shape.aprsis:
        pytest.skip("no enabled [edges.aprsis]: add rig/config/dv-router/dp15-aprsis.toml")
    if not shape.tx_on or not shape.to_rf:
        pytest.skip("this run has no IS-to-RF path with RF transmit: dp7-courtesy-tx.toml + dp7-courtesy-path.toml (tx_enable true, port tx true)")
    if "N0PS" not in shape.filter_extra:
        pytest.skip("the APRS-IS filter_extra %r does not pass the correspondents (p/N0PS): add dp7-courtesy-filter.toml after dp15-aprsis.toml" % shape.filter_extra)
    if positive and not shape.courtesy:
        pytest.skip("the IS-to-RF path has courtesy_posit = false: this case needs dp7-courtesy-path.toml")
    guard = getattr(tnc, "tx_guard", None)
    if guard is not None:
        guard(case)


# ================================================================ DP7-e: the courtesy posit

def test_dp7_e_courtesy_posit_after_a_gated_message(rig, target, hub, shape, tnc, aprsc):
    """data.md 8.3 "IS to RF": positions are never gated to RF except ONE courtesy position of the station whose message was just
    gated to RF, when courtesy_posit = true. The fake TNC hears N0PRX-2 (so the message to it can be gated); N0PSD-1 (a verified
    APRS-IS login) sends a message to N0PRX-2: the TNC sees the third-party frame `N0TEST-1>APZDVR:}N0PSD-1>APDVT1,TCPIP,N0TEST-1*::
    N0PRX-2  :text{1`. Then N0PSD-1 sends a position on APRS-IS: the TNC sees ONE more third-party frame carrying the position
    exactly as sent. A SECOND position of the same station is not transmitted (the credit is one use; counter
    dv_pkt_rf_drop_total{reason=courtesy_unavailable} is recorded). Two frames are booked on the worker's flat IS-to-RF limit
    (6 per minute, 10 per 5 minutes)."""
    case = "DP7-e[courtesy]"
    need_courtesy_shape(shape, tnc=tnc, case=case)
    if not shape.positions or CORR_OK in shape.optout:
        pytest.skip("positions are off or %s is opted out in this run" % CORR_OK)
    call = CORR_OK + "-1"
    text = "dp7 courtesy " + nonce()
    drops = drop_baseline(hub)
    p2.IGATE.wait(2)
    rf_hear(tnc)
    with correspondent(aprsc, call) as client:
        t0 = time.monotonic()
        send_is_message(client, call, text)
        msg = wait_tx(tnc, t0, CORR_OK, positions=False)
        note(target, case, "RECORD", "gated message frame: %s" % ([m[2]["inner_line"] for m in msg],))
        assert msg, ("the message of %s to the RF-heard %s was not transmitted within %.0f s (is the APRS-IS filter passing it? "
                     "p/N0PS in filter_extra; path dp7-is-to-rf; the addressee heard on rf-a)" % (call, RX_STATION, TX_WAIT_S))
        check_message_frame(msg[0], call, text)
        t1 = time.monotonic()
        info = send_is_position(client, call, "cp " + nonce())
        posit = wait_tx(tnc, t1, CORR_OK, positions=True)
        note(target, case, "RECORD", "courtesy position frame: %s (%.1f s after the IS position)" % (
            [m[2]["inner_line"] for m in posit], (posit[0][0] - t1) if posit else -1))
        assert posit, "no courtesy position was transmitted within %.0f s of the position of %s on APRS-IS" % (TX_WAIT_S, call)
        check_courtesy_frame(posit[0], call, info)
        assert len(posit) == 1, "more than one courtesy position frame: %s" % [m[2]["inner_line"] for m in posit]
        t2 = time.monotonic()
        send_is_position(client, call, "again " + nonce())
        time.sleep(QUIET_S)
        extra = tx_from(tnc, t2, CORR_OK)
    settle_counters(hub, target, case, drops)
    assert not extra, "a second position of %s was transmitted although the credit is one use: %s" % (call, [m[2]["inner_line"] for m in extra])


def test_dp7_e_no_courtesy_without_a_gated_message(rig, target, hub, shape, tnc, aprsc):
    """No message was gated for N0PSN-1, so a position of it on APRS-IS earns nothing: the TNC sees no frame from it
    (core_rf_tx.rs: `courtesy()` finds no credit; counter courtesy_unavailable is recorded)."""
    case = "DP7-e[no credit]"
    need_courtesy_shape(shape, tnc=tnc, case=case)
    if not shape.positions:
        pytest.skip("positions are off in this run")
    call = CORR_NOCREDIT + "-1"
    drops = drop_baseline(hub)
    rf_hear(tnc)
    with correspondent(aprsc, call) as client:
        t0 = time.monotonic()
        send_is_position(client, call, "nocredit " + nonce())
        time.sleep(QUIET_S)
        got = tx_from(tnc, t0, CORR_NOCREDIT)
    settle_counters(hub, target, case, drops)
    assert not got, "a position without a gated message was transmitted on RF: %s" % [m[2]["inner_line"] for m in got]


def blocked_correspondent(shape):
    """(base callsign, why) of the correspondent whose position must NOT be transmitted in this run shape, or None."""
    if not shape.positions:
        return CORR_POSOFF, "[data] positions = false"
    if "N0OPT" in shape.optout:
        return "N0OPT", "position_optout"
    return None


def test_dp7_e_no_courtesy_for_optout_or_positions_off(rig, target, hub, shape, tnc, aprsc):
    """data.md 13.4: the worker applies [data] positions and position_optout to the IS-to-RF courtesy posit. The message of the
    blocked correspondent (N0OPT-1 with dp7-position-optout.toml, or N0PSB-1 with positions = false) IS gated and
    transmitted (the control: messages are not spatial), its position on APRS-IS is NOT transmitted."""
    case = "DP7-e[optout]"
    need_courtesy_shape(shape, tnc=tnc, case=case)
    blocked = blocked_correspondent(shape)
    if blocked is None:
        pytest.skip("neither positions = false nor position_optout (N0OPT) is configured in this run")
    base, why = blocked
    call = base + "-1"
    text = "dp7 blocked " + nonce()
    drops = drop_baseline(hub)
    p2.IGATE.wait(1)
    rf_hear(tnc)
    with correspondent(aprsc, call) as client:
        t0 = time.monotonic()
        send_is_message(client, call, text)
        msg = wait_tx(tnc, t0, base, positions=False)
        assert msg, "the control message of %s (%s) was not gated to RF within %.0f s: the absence check proves nothing" % (call, why, TX_WAIT_S)
        check_message_frame(msg[0], call, text)
        t1 = time.monotonic()
        send_is_position(client, call, "blocked " + nonce())
        time.sleep(QUIET_S)
        got = tx_from(tnc, t1, base, positions=True)
    settle_counters(hub, target, case, drops)
    assert not got, "a courtesy position was transmitted for %s although %s: %s" % (call, why, [m[2]["inner_line"] for m in got])


def test_dp7_e_no_courtesy_for_a_hidden_account(rig, target, hub, web_ctx, shape, tnc, aprsc):
    """data.md 13.4 / 8.9: the account hidden set applies to the courtesy posit too. The account N0PSH (created through the
    operator) has hide_position: its message is gated (control), its position on APRS-IS is NOT transmitted. After the
    flag is cleared a new message and position of it are transmitted again (a flag change applies to new items)."""
    case = "DP7-e[hide_position]"
    need_courtesy_shape(shape, tnc=tnc, case=case)
    if not shape.positions or CORR_HIDE in shape.optout:
        pytest.skip("positions are off or %s is opted out in this run" % CORR_HIDE)
    web_ctx.user(CORR_HIDE, may_tx=False)
    call = CORR_HIDE + "-1"
    p2.IGATE.wait(3)
    try:
        p1.set_hide_position(web_ctx, CORR_HIDE, True)
        time.sleep(PUSH_S)
        rf_hear(tnc)
        with correspondent(aprsc, call) as client:
            t0 = time.monotonic()
            text = "dp7 hidden " + nonce()
            send_is_message(client, call, text)
            msg = wait_tx(tnc, t0, CORR_HIDE, positions=False)
            assert msg, "the control message of the hidden account %s was not gated to RF: the absence check proves nothing" % call
            t1 = time.monotonic()
            send_is_position(client, call, "hidden " + nonce())
            time.sleep(QUIET_S)
            got = tx_from(tnc, t1, CORR_HIDE, positions=True)
            assert not got, "a courtesy position of the hidden account %s was transmitted: %s" % (call, [m[2]["inner_line"] for m in got])
            p1.set_hide_position(web_ctx, CORR_HIDE, False)
            time.sleep(PUSH_S)
            rf_hear(tnc)
            t2 = time.monotonic()
            text2 = "dp7 shown " + nonce()
            send_is_message(client, call, text2, msgid="2")
            msg2 = wait_tx(tnc, t2, CORR_HIDE, positions=False)
            assert msg2, "after clearing the flag the message of %s was not gated to RF" % call
            t3 = time.monotonic()
            info = send_is_position(client, call, "shown " + nonce())
            posit = wait_tx(tnc, t3, CORR_HIDE, positions=True)
            note(target, case, "RECORD", "after clearing the flag the courtesy position was transmitted: %s" % bool(posit))
            assert posit, "after clearing hide_position the courtesy position of %s was still not transmitted" % call
            check_courtesy_frame(posit[0], call, info)
    finally:
        with contextlib.suppress(AssertionError, web.WebError):
            p1.set_hide_position(web_ctx, CORR_HIDE, False)


def test_dp7_e_no_courtesy_when_the_path_has_it_off(rig, target, hub, shape, tnc, aprsc):
    """config.md `courtesy_posit` (default false): with the path dp7-is-to-rf and courtesy_posit = false (dp7-courtesy-path-off.toml)
    the message is gated and transmitted, the correspondent's position is not."""
    case = "DP7-e[path off]"
    need_courtesy_shape(shape, positive=False, tnc=tnc, case=case)
    if shape.courtesy:
        pytest.skip("the IS-to-RF path has courtesy_posit = true in this run: this case needs dp7-courtesy-path-off.toml")
    if not shape.positions:
        pytest.skip("positions are off in this run")
    call = CORR_OFF + "-1"
    text = "dp7 pathoff " + nonce()
    p2.IGATE.wait(1)
    rf_hear(tnc)
    with correspondent(aprsc, call) as client:
        t0 = time.monotonic()
        send_is_message(client, call, text)
        msg = wait_tx(tnc, t0, CORR_OFF, positions=False)
        assert msg, "the control message was not gated to RF within %.0f s" % TX_WAIT_S
        check_message_frame(msg[0], call, text)
        t1 = time.monotonic()
        send_is_position(client, call, "off " + nonce())
        time.sleep(QUIET_S)
        got = tx_from(tnc, t1, CORR_OFF, positions=True)
    assert not got, "a courtesy position was transmitted although the path has courtesy_posit = false: %s" % [m[2]["inner_line"] for m in got]


def test_dp7_e_nothing_on_rf_when_transmit_is_off(rig, target, hub, shape, tnc, aprsc):
    """data.md 10.8 / config.md: with tx_enable false there is no RF output (and the launcher refuses any path to the port): a message and a
    position from APRS-IS, the same sequence as the positive case, produce NO data frame at the fake TNC. Receive-only shape
    (dp7-rf.toml + dp7-courtesy-filter.toml). The check is weak by construction (no IS-to-RF path can exist); it proves that
    nothing else transmits a gated packet."""
    case = "DP7-e[tx off]"
    if shape.tx_on:
        pytest.skip("RF transmit is on in this run: the receive-only shape is dp7-rf.toml (+ dp7-courtesy-filter.toml)")
    if not shape.aprsis:
        pytest.skip("no enabled [edges.aprsis]: add rig/config/dv-router/dp15-aprsis.toml")
    call = CORR_TXOFF + "-1"
    rf_hear(tnc)
    with correspondent(aprsc, call) as client:
        t0 = time.monotonic()
        send_is_message(client, call, "dp7 txoff " + nonce())
        time.sleep(8.0)
        send_is_position(client, call, "txoff " + nonce())
        time.sleep(QUIET_S)
        frames = tnc.data_frames(t0)
    note(target, case, "RECORD", "data frames at the TNC with transmit off: %d" % len(frames))
    assert not frames, "the edge transmitted %d frame(s) with tx_enable = false: %s" % (len(frames), [f[2][:40] for f in frames])


# ================================================================ DP7-f: hide_position for RF

def test_dp7_f_hide_position_drops_rf_heard_positions(request, rig, target, hub, web_ctx, shape, tnc):
    """data.md 13.4: an account's hide_position drops EVERY source "including RF-heard APRS" at the controller, and the worker applies
    the hidden set to RF transit (RF to APRS-IS). The account N0PFH: an RF-heard position (the fake TNC rf-a, room-bound port) is
    public (control); after hide_position the retained row leaves the anonymous state, a new RF position and an RF object of N0PFH are
    dropped (optout counters rise, nothing on the public SSE, the state or the operator feed); with the transit path the hidden
    callsign's frame never reaches the private aprsc while the control N0PFC's does; after clearing the flag an RF position
    is public again (comment `again`)."""
    case = "DP7-f[hide rf]"
    if not shape.positions:
        pytest.skip("positions are off in this run")
    web_ctx.user(HIDE_RF_CALL, may_tx=False)
    ctx = types.SimpleNamespace(rig=rig, rf=tnc)
    aprsc = None
    if shape.igate and shape.aprsis:
        try:
            aprsc = request.getfixturevalue("aprsc")
        except pytest.skip.Exception as exc:
            note(target, case, "RECORD", "the transit half is skipped: %s" % exc)
    transit = aprsc is not None
    try:
        with p1.feeds(web_ctx) as fd:
            p1.send_rf(ctx, HIDE_RF_CALL, ps.LAT, ps.LON, comment="before")
            got = p1.collect(web_ctx, fd, HIDE_RF_CALL)
        p1.assert_visible(got, case + " before the flag")
        p1.set_hide_position(web_ctx, HIDE_RF_CALL, True)
        time.sleep(PUSH_S)
        gone = ps.wait_gone(web_ctx.anon(), HIDE_RF_CALL, room=MODULE, item_type="position")
        note(target, case, "RECORD", "retained RF row left the anonymous public state after the flag: %s" % gone)
        assert gone, "the retained RF position of a hidden account is still in the anonymous public state"
        p1.dropped_send(ctx, target, web_ctx, hub, case + " position", "rf", HIDE_RF_CALL, "rf position")
        p1.dropped_send(ctx, target, web_ctx, hub, case + " object", "rf-object", HIDE_RF_CALL, "rf object", name="DVT7HRF")
        if transit:
            host = aprsc
            with p1.aprsc_login(host) as tester:
                mark = tester.mark()
                ctl = ps.position_info(ps.LAT, ps.LON, 0, "/>", "dp7 f transit %s" % nonce())
                p1.send_rf_info(ctx, HIDE_RF_CONTROL + "-7", ctl)
                hit = tester.wait_packet(lambda p: ps.same_base(p["src"], HIDE_RF_CONTROL) and ctl in p["info"], p1.UPLINK_WAIT_S, since=mark)
                assert hit is not None, "the control position of %s never reached the private aprsc through the transit path" % HIDE_RF_CONTROL
                mark2 = tester.mark()
                p1.send_rf_info(ctx, HIDE_RF_CALL + "-7", ps.position_info(ps.LAT, ps.LON, 0, "/>", "dp7 f hidden %s" % nonce()), spend=False)
                time.sleep(p1.UPLINK_ABSENT_S)
                lines = [p["raw"] for _t, p in p1.aprsc_lines(tester, mark2, HIDE_RF_CALL)]
            note(target, case, "RECORD", "lines of the hidden callsign at the aprsc: %s" % lines)
            assert not lines, "an RF transit packet of a hidden account reached the private aprsc: %s" % lines
        else:
            note(target, case, "RECORD", "no RF-to-APRS-IS transit path in this run: the transit half is skipped (dp7-rf-igate.toml)")
        p1.set_hide_position(web_ctx, HIDE_RF_CALL, False)
        time.sleep(PUSH_S)
        with p1.feeds(web_ctx) as fd:
            p1.send_rf(ctx, HIDE_RF_CALL, ps.LAT, ps.LON, comment="again")
            again = p1.collect(web_ctx, fd, HIDE_RF_CALL)
        p1.assert_visible(again, case + " after clearing the flag")
        assert again.sse["body"].get("comment") == "again", again.sse
    finally:
        with contextlib.suppress(AssertionError, web.WebError):
            p1.set_hide_position(web_ctx, HIDE_RF_CALL, False)


# ================================================================ DP7-g: SSE replay after a hide

def rows_of(watch, call):
    return [o for _t, o in watch.data_events() if ps.is_spatial(o) and ps.same_base(o.get("callsign", ""), call)]


def replay_watch(web_ctx, cursor, wait_s=6.0):
    """Opens a NEW public SSE connection resuming after `cursor`, reads for `wait_s`, returns (frames, event names)."""
    with p1.SseWatch(web_ctx.anon(), p2.replay_path(cursor), seconds=40.0) as watch:
        assert watch.ready(), "the resumed public SSE gave no frame: %s" % watch.error
        time.sleep(wait_s)
        frames = watch.snapshot()
        data = [o for _t, o in watch.data_events()]
    return frames, data


def test_dp7_g_sse_replay_and_state_after_a_hide(rig, target, hub, web_ctx, shape):
    """web/README.md: "Web filters state and streamed/replayed SSE with its in-memory hidden-base-callsign set"; data.md 13.4 / openapi
    Stream: "hidden accounts ... are omitted". Three M17 positions arrive on an open public SSE watch: a marker (N0PGM), a
    control (N0PGK) and the account N0PGH. The cursor is the SSE id of the marker's event. After hide_position of N0PGH: the
    anonymous state no longer lists N0PGH, a NEW connection resumed with last_event_id=<cursor> replays N0PGK but NOT N0PGH,
    and a plain new connection shows no N0PGH row either. After the flag is cleared the retained row is back in the state and
    in a new replay (design-implied: the filter is applied at read time with the CURRENT hidden set, the hub still holds the
    row; UNVERIFIED on a running image)."""
    case = "DP7-g[replay]"
    if not shape.positions:
        pytest.skip("positions are off in this run")
    web_ctx.user(SSE_HIDE, may_tx=False)
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    try:
        with p1.SseWatch(web_ctx.anon(), ps.PUBLIC_EVENTS, seconds=200.0) as live:
            assert live.ready(), "the public SSE gave no hello: %s" % live.error
            for call, comment in ((SSE_MARK, "mark"), (SSE_CONTROL, "control"), (SSE_HIDE, "hidden")):
                p1.send_m17(ctx, call, ps.LAT, ps.LON, comment=comment)
                hit = live.wait_data(p1.spatial_pred(call), ps.PUBLIC_WAIT_S)
                assert hit is not None, "the position of %s never reached the live public SSE" % call
            frames = live.snapshot()
        marker = next((f for _t, f in frames if f.get("event") == "data" and any(
            ps.same_base(o.get("callsign", ""), SSE_MARK) for o in [web.sse_data(f) or {}])), None)
        cursor = p2.cursor_of(marker)
        note(target, case, "RECORD", "SSE frame ids: %s; cursor %s; frame keys %s" % (
            [f.get("id") for _t, f in frames if f.get("event") == "data"][:6], cursor, sorted(marker or {})))
        assert cursor is not None, ("the data event frame of the marker has no well formed `id: <boot>-<ordinal>` (openapi Stream: optional id); "
                                    "frame %r" % (marker,))
        # without the flag the replay holds both stations (proves the replay works at all)
        _frames, before = replay_watch(web_ctx, cursor)
        have = {c for c in (SSE_CONTROL, SSE_HIDE) if any(ps.same_base(o.get("callsign", ""), c) for o in before if ps.is_spatial(o))}
        note(target, case, "RECORD", "replay before the hide holds %s; events %s" % (sorted(have), p2.event_names(_frames)))
        assert have == {SSE_CONTROL, SSE_HIDE}, ("the resumed stream (last_event_id=%s) did not replay the later positions before any hide: %s "
                                                  "(a `resync` frame instead?)" % (cursor, p2.event_names(_frames)))
        p1.set_hide_position(web_ctx, SSE_HIDE, True)
        time.sleep(PUSH_S)
        assert ps.wait_gone(web_ctx.anon(), SSE_HIDE, room=MODULE, item_type="position"), "the hidden station is still in the anonymous public state"
        state, _rows = ps.wait_public_row(web_ctx.anon(), SSE_CONTROL, room=MODULE)
        assert state is not None, "the control station left the public state with the hide of another account"
        frames2, after = replay_watch(web_ctx, cursor)
        seen_ctl = any(ps.same_base(o.get("callsign", ""), SSE_CONTROL) for o in after if ps.is_spatial(o))
        seen_hid = [o for o in after if ps.is_spatial(o) and ps.same_base(o.get("callsign", ""), SSE_HIDE)]
        note(target, case, "RECORD", "replay after the hide: control %s, hidden rows %d; events %s" % (seen_ctl, len(seen_hid), p2.event_names(frames2)))
        assert seen_ctl, "the replay after the hide lost the control station too (events %s)" % p2.event_names(frames2)
        assert not seen_hid, "a hidden station's position was REPLAYED on a new SSE connection: %s" % seen_hid[:1]
        with p1.SseWatch(web_ctx.anon(), ps.PUBLIC_EVENTS, seconds=30.0) as fresh:
            assert fresh.ready()
            time.sleep(6.0)
            plain = [o for _t, o in fresh.data_events() if ps.is_spatial(o) and ps.same_base(o.get("callsign", ""), SSE_HIDE)]
        assert not plain, "a plain new SSE connection carried the hidden station: %s" % plain[:1]
        p1.set_hide_position(web_ctx, SSE_HIDE, False)
        time.sleep(PUSH_S)
        back, _rows = ps.wait_public_row(web_ctx.anon(), SSE_HIDE, room=MODULE, item_type="position")
        note(target, case, "RECORD", "after clearing the flag the retained row is back in the public state: %s" % (back is not None))
        _frames3, restored = replay_watch(web_ctx, cursor)
        in_replay = any(ps.same_base(o.get("callsign", ""), SSE_HIDE) for o in restored if ps.is_spatial(o))
        note(target, case, "RECORD", "after clearing the flag a new replay holds the station again: %s" % in_replay)
        assert back is not None, ("after clearing hide_position the retained row is not back in the anonymous state (design-implied: the web process "
                                  "filters retained rows with its current hidden set; UNVERIFIED)")
        assert in_replay, "after clearing hide_position a new replay does not hold the station again (design-implied; UNVERIFIED)"
    finally:
        with contextlib.suppress(AssertionError, web.WebError):
            p1.set_hide_position(web_ctx, SSE_HIDE, False)


# ================================================================ DP7-h: items

@pytest.mark.parametrize("source", ["m17", "rf"])
def test_dp7_h_item_position_and_kill(request, rig, target, hub, web_ctx, shape, source):
    """data.md 8.3 (APRS objects/items), object.rs: an APRS ITEM `)NAME!<pos>` (3..9 character name, `!` live, `_` killed, no
    timestamp) is an `object` row like an object report. Sent as an M17 packet-mode APRS payload and as an RF frame from the fake
    TNC (room-bound port). The live item is on the anonymous state, the public SSE (item_type object, body.name, state 1, source
    5) and the operator feed; 34 s later (the shared 1 per 30 s bucket of the reporting callsign) the kill `)NAME_<pos>` gives a
    public SSE event whose body is {name, state 0} with no coordinate key at any depth, and the item leaves the public listing.
    The item/object distinction is not on the wire (the same row shape): UNVERIFIED that the hub treats the timestamp-free item
    like an object for the kill ordering (hub data.rs orders by acceptance time, so it should)."""
    case = "DP7-h[item %s]" % source
    if not shape.positions:
        pytest.skip("positions are off in this run")
    if source == "rf":
        tnc = request.getfixturevalue("tnc")
        ctx = types.SimpleNamespace(rig=rig, rf=tnc)
        send = lambda info: p1.send_rf_info(ctx, call + "-7", info)       # noqa: E731
    else:
        ctx = types.SimpleNamespace(rig=rig, rf=None)
        send = lambda info: p1.send_m17_info(ctx, call + "-7", info)       # noqa: E731
    call, name = ITEM_CALL[source], ITEM_NAME[source]
    if call in shape.optout:
        pytest.skip("%s is opted out" % call)
    plain = ps.plain_text(ps.LAT, ps.LON, 0, "/>")
    with p1.feeds(web_ctx) as fd:
        send(p2.item_info(name, plain, True, "dp7 item"))
        live = p1.collect(web_ctx, fd, call, item_type="object", name=name)
        p1.assert_visible(live, case + " live item")
        ps.check_position(live.state, source=5, ambiguity=0)
        assert live.state["body"].get("state") == 1 and str(live.state["body"].get("name")).strip() == name, live.state
        note(target, case, "RECORD", "live item row: %s; body keys %s" % ({k: live.state.get(k) for k in ("callsign", "protocol", "ident", "via")}, sorted(live.state["body"])))
        t_kill = time.monotonic()
        send(p2.item_info(name, plain, False))
        hit = fd.pub.wait_data(p1.spatial_pred(call, "object", name, state=0), ps.PUBLIC_WAIT_S, since=t_kill)
        gone = ps.wait_gone(web_ctx.anon(), call, room=MODULE, item_type="object", name=name)
        ops, _rows = ps.wait_operator_rows(web_ctx.op, fd.baseline, p1.spatial_pred(call, "object", name, state=0), timeout=ps.OPERATOR_WAIT_S)
    assert hit is not None, "no public SSE kill event (object, state 0) for the item %s" % name
    event = hit[1]
    note(target, case, "RECORD", "kill event %s; keys %s; left the public listing: %s; operator kill row: %s" % (
        event, sorted(p1.keys_of(event)), gone, ops[0] if ops else None))
    ps.check_public_row(event)
    body = event["body"]
    assert event["item_type"] == "object" and str(body.get("name")).strip() == name and body.get("state") == 0, event
    bad = {k for k in p1.keys_of(event) if k.lower().startswith(("lat", "lon"))}
    assert not bad, "the item kill event carries coordinate keys %s" % sorted(bad)
    assert gone, "the killed item %s is still in the anonymous public listing" % name


# ================================================================ DP7-i: retention

def test_dp7_i_position_retention_thirty_minutes(rig, target, hub, web_ctx, shape):
    """data.md 13.2: the hub keeps a position 30 minutes (crates/svc/status/src/hub/data.rs POSITION_AGE_MS, a CONSTANT: config.md has no
    key for it, so a short window cannot be configured; recorded only). Opt-in long case (DVT_DP7_RETENTION_LONG=1, about 35
    minutes, its own rig run): one M17 position of N0PRT is in the anonymous public state at 2 and at 25 minutes and gone by
    33 minutes (the page itself drops a row at 30 minutes by its own clock)."""
    case = "DP7-i[retention]"
    if os.environ.get("DVT_DP7_RETENTION_LONG") != "1":
        pytest.skip("the retention window is a constant of the hub (30 minutes, no configuration key): recorded only; set DVT_DP7_RETENTION_LONG=1 "
                    "for the 35 minute case")
    if not shape.positions:
        pytest.skip("positions are off in this run")
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    t0 = time.monotonic()
    p1.send_m17(ctx, RETENTION_CALL, ps.LAT, ps.LON, comment="retention")
    row, _rows = ps.wait_public_row(web_ctx.anon(), RETENTION_CALL, room=MODULE)
    assert row is not None, "the position never became public"
    for minutes in (2, 25):
        time.sleep(max(0.0, t0 + minutes * 60 - time.monotonic()))
        present, _rows = ps.wait_public_row(web_ctx.anon(), RETENTION_CALL, timeout=1.0, room=MODULE)
        note(target, case, "RECORD", "after %d minutes the position is in the public state: %s" % (minutes, present is not None))
        assert present is not None, "the position left the public state after %d minutes (retention is 30)" % minutes
    time.sleep(max(0.0, t0 + 33 * 60 - time.monotonic()))
    gone = ps.wait_gone(web_ctx.anon(), RETENTION_CALL, room=MODULE, item_type="position", timeout=60.0)
    note(target, case, "RECORD", "after 33 minutes the position is gone from the public state: %s" % gone)
    assert gone, "the position is still in the public state 33 minutes after it was admitted (retention 30 minutes)"
