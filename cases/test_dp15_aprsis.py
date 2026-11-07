# SPDX-License-Identifier: MIT
"""DV-Router APRS-IS cases of data.md 15.5 against a PRIVATE aprsc (DP15 group 5): a message to the room's APRS name
from a client logged in on the rig's own APRS-IS server reaches the room and is acked, and a repeat is acked again but
not delivered twice. Specification: docs/dp15-plan.md (section 3.5, 4.1); case table, run command and estimates:
docs/dp15-cases.md (section "DP15-5a / 5b").

  DP15-5a  a message `N0TEST-7>APDVT1,TCPIP*::XLX999B  :<text>{<id>` from a client of the private aprsc: D-STAR text over
           (header suffix APRS), M17 SMS, web `text` row, ONE ack line `XLX999B>APZDVR,TCPIP*,q..,..::N0TEST-7 :ack<id>`
           at the client; the line DV-Router sent carries NO q construct (the server adds exactly one)
  DP15-5b  the same message again 40 s later (after aprsc's own 30 s duplicate window, inside the controller's 60 s
           window): acked again (a second, byte for byte equal ack line), delivered once (no second text over, SMS
           or web row)

The server is a separate service of the rig (`--profile aprs`, compose service `aprsc` at <net>.47, port 14580, no uplink,
no internet); DV-Router logs in as N0TEST-5 (rig/config/dv-router/dp15-aprsis.toml) with the fixture passcode run.sh
writes, the tester logs in as N0TEST-7 with the passcode it computes (dvtest/aprs_passcode.py). Both skip with the
reason when the aprsc service is not up. Observations go to stdout and out/<run>/dp15-aprsis.txt ("RECORD" = what was
seen, "FINDING" = differs from the documented behaviour).

What cannot be observed, and how the cases cope: the line DV-Router WRITES is not visible anywhere (aprsc adds its q
construct on receipt and forwards the result). The forwarded ack at the tester shows the path as `<what DV-Router wrote>,
q..,<login>`; the cases assert that the part before the q construct is exactly `TCPIP*` and that there is exactly one q
construct (the server's), which holds only if DV-Router wrote none. The q construct itself is not asserted beyond its
shape (the DV-Router lead: the token is the server's choice, `qAS` or `qAO`; this aprsc 3.0.0 gives `qAS`).

NOT RUN ON A RIG yet (written against the sources; the aprsc side was smoke tested in a throw-away container).
"""
import os
import socket
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprs_passcode, aprsis_client as ai, metrics, slowdata, web  # noqa: E402

import data_support as ds  # noqa: E402
from data_support import (  # noqa: E402
    PACER, SseWatch, dstar_listener, hub_session, m17_client, m17p_packets, over_by_my, pump_for, sms_text, texts_of,
    write_note)

pytestmark = pytest.mark.rig

MODULE = "B"
ROOM_NAME = "XLX999" + MODULE          # the room's APRS name: reflector callsign + module letter
SENDER = aprs_passcode.TESTER_LOGIN    # login and message source of the tester (source == login: the server adds qAC)
EDGE_LOGIN = aprs_passcode.DVR_LOGIN   # login of the DV-Router edge (rig/config/dv-router/dp15-aprsis.toml)
SERVER_ID = "DVTEST"                   # ServerId of rig/config/aprsc/aprsc.conf
LOG = "dp15-aprsis.txt"
SETTING = "require_ident=%s" % os.environ.get("DVT_REQUIRE_IDENT", "true")
D_CALL, M_CALL = "N0CALL", "N0TEST"    # the DExtra listener and the M17 listener linked to module B

ACK_S = 3.0                            # data.md 15.5 plan: the ack arrives within 3 s of the message
EDGE_LOGIN_WAIT_S = 90.0               # the edge connects at start (a failed first connect waits 5 s, doubling)
DELIVER_WAIT_S = 20.0                  # D-STAR text over (after idle_gap_ms), SMS and web row show up within this
QUIET_AFTER_S = 6.0                    # nothing more may arrive this long after everything was seen
# 5b: the repeat must come after aprsc's own duplicate window (30 s, from the first ack and the first message) and
# before the controller's dedupe window (60 s from the first accepted message). 40 s leaves 10 s on both sides.
REPEAT_AFTER_S = 40.0
SERVER_DUPE_S, CONTROLLER_DUPE_S = 30.0, 60.0
MSGID_5A, MSGID_5B = "51", "52"


# ---------------------------------------------------------------- notes, fixtures

def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics with the data worker on (skips with the reason otherwise)."""
    yield from hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    """The web side: a reader (user, may not transmit) for the room history and a factory of anonymous clients."""
    return types.SimpleNamespace(op=web_operator, reader=web_user("N0DPR", may_tx=False).client, anon=web_client)


@pytest.fixture(scope="module")
def aprsc():
    """The address of the private aprsc (profile aprs); skips with the reason when it is not up or not answering."""
    if os.environ.get("DVT_APRS") != "on":
        pytest.skip("the private aprsc is not part of this rig: run scripts/run.sh --profile web,aprs (DVT_APRS is not 'on')")
    host = os.environ.get("DVT_APRSC", "")
    if not host:
        pytest.skip("DVT_APRSC (the aprsc address) is not set")
    end = time.monotonic() + 30.0
    last = None
    while time.monotonic() < end:
        try:
            socket.create_connection((host, ai.PORT), timeout=3.0).close()
            return host
        except OSError as exc:
            last = exc
            time.sleep(1.0)
    pytest.skip("the aprsc service at %s:%d does not answer (%s)" % (host, ai.PORT, last))


# ---------------------------------------------------------------- helpers

def edge_logged_in(hub):
    """True / False from dv_edge_aprsis_verified (the edge's own view: logged in and the server said verified), None
    when the image has no such family."""
    text = hub.text()
    if "dv_edge_aprsis_verified" not in metrics.families(text):
        return None
    return metrics.total(metrics.parse(text), "dv_edge_aprsis_verified") >= 1


def wait_edge_login(hub, target, case):
    """Waits for the DV-Router edge to be logged in and verified on the private aprsc. Returns True, or None when the
    image has no metric (a fixed settle time is used then). Fails (pytest.fail) when the metric exists and stays 0."""
    end = time.monotonic() + EDGE_LOGIN_WAIT_S
    t0 = time.monotonic()
    while True:
        state = edge_logged_in(hub)
        if state:
            note(target, case, "RECORD", "dv_edge_aprsis_verified = 1 after %.1f s of waiting" % (time.monotonic() - t0))
            return True
        if state is None:
            note(target, case, "RECORD", "no dv_edge_aprsis_verified family in the hub metrics: waiting 10 s for the edge login instead")
            time.sleep(10.0)
            return None
        if time.monotonic() >= end:
            pytest.fail("the DV-Router APRS-IS edge is not logged in and verified on the private aprsc after %.0f s "
                        "(dv_edge_aprsis_verified = 0): check out/<run>/logs/aprsc.log and the passcode file of dp15-aprsis.toml"
                        % EDGE_LOGIN_WAIT_S)
        time.sleep(2.0)


def web_rows(reader, body):
    """The text rows of the room history of module B that carry `body`."""
    return [r for r in web.room_messages(reader, MODULE) if r.get("item_type") == "text" and body in str(r.get("body", ""))]


def sse_texts(watch, body, since=0.0):
    return [(at, o) for at, o in watch.data_events(since) if o.get("item_type") == "text" and body in str(o.get("body", ""))]


def dstar_text_overs(listener, body):
    """[(stream id, decoded text dict)] of the whole text overs on a D-STAR listener that end with `body`."""
    return [(sid, got) for sid, got in texts_of(slowdata.overs(listener.client.inbox)) if got["text"].rstrip().endswith(body)]


def m17_sms(client, body):
    """[(arrival time, parsed M17P)] of the SMS datagrams on a linked M17 client that carry `body`."""
    return [(at, p) for at, p in m17p_packets(client) if body in (sms_text(p) or "")]


def acks_for(client, msgid, since=0):
    """[(arrival time, parsed line)] of the ack lines for `msgid` that reached the tester's own login."""
    want = ai.message_info(SENDER, "ack" + msgid)
    return client.matching(lambda p: p["src"] == ROOM_NAME and p["info"] == want, since)


class Probe:
    """Everything the listeners saw, read at one moment."""

    def __init__(self, dlist, mlist, watch, reader, tester, body, msgid, mark):
        self.dlist, self.mlist, self.watch, self.reader, self.tester = dlist, mlist, watch, reader, tester
        self.body, self.msgid, self.mark = body, msgid, mark

    def pump(self, seconds):
        pump_for(seconds, self.dlist, self.mlist)

    def counts(self):
        return types.SimpleNamespace(
            text_overs=len(dstar_text_overs(self.dlist, self.body)), sms=len(m17_sms(self.mlist, self.body)),
            sse=len(sse_texts(self.watch, self.body)), acks=len(acks_for(self.tester, self.msgid, self.mark)))

    def pump_until(self, want, seconds):
        """Pump the listeners until counts() has at least the `want` counts (a dict field -> minimum) or `seconds` pass;
        the last counts."""
        end = time.monotonic() + seconds
        while True:
            self.pump(0.1)
            got = self.counts()
            if all(getattr(got, key) >= value for key, value in want.items()) or time.monotonic() >= end:
                return got


def check_ack_line(target, case, parsed, msgid):
    """The shape of the ack line the tester received: sent as the room name, tocall APZDVR, path TCPIP* then the
    server's q construct, addressed to the tester padded to 9 characters."""
    assert parsed["src"] == ROOM_NAME, "ack source %r, want the room name %r" % (parsed["src"], ROOM_NAME)
    assert parsed["dest"] == "APZDVR", "ack tocall %r, want APZDVR" % parsed["dest"]
    assert parsed["info"] == ai.message_info(SENDER, "ack" + msgid), "ack information field %r" % parsed["info"]
    assert parsed["before_q"] == ["TCPIP*"], \
        "path before the server's q construct is %r, want ['TCPIP*'] (DV-Router wrote a different path or a q construct)" % parsed["before_q"]
    qs = [item for item in parsed["path"] if ai.Q_CONSTRUCT.match(item)]
    assert len(qs) == 1, "%d q constructs in the forwarded ack %r, want exactly 1 (the server's; DV-Router must write none)" % (len(qs), parsed["raw"])
    assert parsed["q"] and parsed["q"][0] == qs[0] and len(parsed["q"]) == 2, "q construct is not `q..,<call>`: %r" % parsed["q"]
    note(target, case, "RECORD", "ack line at the tester: %s (q construct %s; before it: %s)" % (parsed["raw"], ",".join(parsed["q"]), ",".join(parsed["before_q"])))
    if parsed["q"][0] != "qAS":
        note(target, case, "RECORD", "q construct token %s differs from the qAS aprsc 3.0.0 gave in the smoke test (server's choice, not asserted)" % parsed["q"][0])


def check_delivered_once(out):
    """The room got the text exactly once on each path: one D-STAR text over, one M17 SMS, one web history row."""
    assert out.final.text_overs == 1, "%d D-STAR text overs with %r on the listener, want exactly 1" % (out.final.text_overs, out.body)
    assert out.final.sms == 1, "%d M17 SMS with %r on the M17 listener, want exactly 1" % (out.final.sms, out.body)
    assert len(out.rows) == 1, "%d web text rows with %r in the room history, want exactly 1: %s" % (len(out.rows), out.body, out.rows[:3])


def send_and_collect(rig, target, hub, web_ctx, aprsc_host, case, body, msgid, repeat=False):
    """Run one case: links the listeners and the tester client, sends the message (and, with `repeat`, the identical
    line REPEAT_AFTER_S later), collects. Returns a namespace of what happened."""
    PACER.wait(SENDER, "text")
    before = hub.settle()
    before_acks = {r: hub.total("dv_pkt_ack_total", result=r) for r in ("ack", "repeated", "rej", "dropped")}
    before_items = {r: hub.total("dv_pkt_items_total", kind="text", result=r) for r in ("accepted", "duplicate")}
    before_room_dup = hub.total("dv_room_data_total", result="duplicate")
    out = types.SimpleNamespace(case=case, body=body, msgid=msgid, before=before, before_acks=before_acks,
                                before_items=before_items, before_room_dup=before_room_dup)
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, \
            dstar_listener(rig.xlxd_sut, module=MODULE, callsign=D_CALL) as dlist, \
            m17_client(rig.xlxd_sut, callsign=M_CALL, module=MODULE) as mlist, \
            ai.AprsisClient(aprsc_host, login=SENDER) as tester:
        assert sse.ready(), "the public SSE gave no hello: %s" % sse.error
        logresp = tester.connect()
        assert logresp["verified"] and logresp["server"] == SERVER_ID, logresp
        note(target, case, "RECORD", "tester login %s: %s; banner %r" % (SENDER, logresp, tester.banner))
        mark = tester.mark()
        probe = Probe(dlist, mlist, sse, web_ctx.reader, tester, body, msgid, mark)
        out.probe = probe
        line = ai.message_line(SENDER, ROOM_NAME, body, msgid)
        assert not parse_q(line), "the line the tester writes carries a q construct: %s" % line
        t1 = tester.send_line(line)[0]
        PACER.offered(SENDER, "text")
        out.line, out.t1 = line, t1
        note(target, case, "RECORD", "sent: %s" % line)
        counts = probe.pump_until({"text_overs": 1, "sms": 1, "acks": 1}, DELIVER_WAIT_S)
        out.first = counts
        out.ack1 = acks_for(tester, msgid, mark)[:1]
        probe.pump(QUIET_AFTER_S)
        out.after_first = probe.counts()
        if repeat:
            # keep both listeners alive (they answer keepalives while pumped) until the repeat is due
            wait = out.t1 + REPEAT_AFTER_S - time.monotonic()
            note(target, case, "RECORD", "waiting %.1f s for the repeat (server window %.0f s, controller window %.0f s)" % (wait, SERVER_DUPE_S, CONTROLLER_DUPE_S))
            probe.pump(max(wait, 0.0))
            out.t2 = tester.send_line(line)[0]
            out.repeat_gap = out.t2 - out.t1
            note(target, case, "RECORD", "sent again %.1f s after the first: %s" % (out.repeat_gap, line))
            probe.pump_until({"acks": 2}, ACK_S + 2.0)
            probe.pump(DELIVER_WAIT_S / 2 + QUIET_AFTER_S)          # a second delivery would show up now
        out.final = probe.counts()
        out.acks = acks_for(tester, msgid, mark)
        out.all_packets = tester.snapshot()[mark:]
        out.dstar_overs = dstar_text_overs(dlist, body)
        out.dstar_headers = over_by_my(slowdata.overs(dlist.client.inbox), "XLX999")
        out.sms = m17_sms(mlist, body)
        out.sse_texts = sse_texts(sse, body)
        out.rows = web_rows(web_ctx.reader, body)
    return out


def parse_q(line):
    """The q constructs of a TNC2 line (empty for a line that has none)."""
    parsed = ai.parse_line(line)
    return [] if parsed is None else [i for i in parsed["path"] if ai.Q_CONSTRUCT.match(i)]


def counter_delta(hub, family, before, wait=ds.POLL_S, minimum=1, **labels):
    """(delta, reached) of one worker counter: the worker publishes every 10 s, so poll up to `wait` s for `minimum`."""
    value, ok = hub.wait_total(family, before + minimum, timeout=wait, **labels)
    return value - before, ok


def record_counters(hub, target, case, out):
    deltas = {}
    for result in ("ack", "repeated", "rej", "dropped"):
        deltas["ack." + result] = hub.total("dv_pkt_ack_total", result=result) - out.before_acks[result]
    for result in ("accepted", "duplicate"):
        deltas["text." + result] = hub.total("dv_pkt_items_total", kind="text", result=result) - out.before_items[result]
    deltas["room.duplicate"] = hub.total("dv_room_data_total", result="duplicate") - out.before_room_dup
    note(target, case, "RECORD", "counter deltas %s" % {k: int(v) for k, v in sorted(deltas.items()) if v})
    return deltas


# ================================================================ 5a: message to the room, acked once

def test_dp15_5a_aprsis_message_reaches_the_room_and_is_acked_once(aprsc, rig, target, hub, web_ctx):
    """data.md 15.5 (APRS-IS): "a message to XLX999B from a client on a private aprsc reaches the room (D-STAR text over,
    M17 SMS, web) and is acked once". The tester (login N0TEST-7, verified) sends `N0TEST-7>APDVT1,TCPIP*::XLX999B
    :<body>{51`. DV-Router (login N0TEST-5, filter g/XLX999B) receives it from the server and: a D-STAR listener on module
    B gets one text over (header suffix `APRS`, UR `CQCQCQ`, text `N0TEST...: <body>`), an M17 listener one SMS, the room
    history one `text` row; the tester gets exactly ONE ack line `XLX999B>APZDVR,TCPIP*,q..,N0TEST-5::N0TEST-7 :ack51`
    within ACK_S of the message, and the path before the server's q construct is exactly `TCPIP*` (DV-Router wrote no q
    construct). Counters: dv_pkt_ack_total{ack} +1, dv_pkt_items_total{kind=text,result=accepted} +1."""
    case = "5a"
    wait_edge_login(hub, target, case)
    body = ds.nonce_body("DVT")
    out = send_and_collect(rig, target, hub, web_ctx, aprsc, case, body, MSGID_5A)
    note(target, case, "RECORD", "within %.0f s: D-STAR text overs %d, M17 SMS %d, acks %d; at the end %s" % (
        DELIVER_WAIT_S, out.first.text_overs, out.first.sms, out.first.acks, vars(out.final)))
    # the ack: exactly one, shaped as documented, in time
    assert out.acks, "no ack line for id %s at the tester; lines received: %s" % (MSGID_5A, [p for _t, p in out.all_packets][:5])
    at, parsed = out.acks[0]
    latency = at - out.t1
    note(target, case, "RECORD", "ack %.3f s after the message (bound %.1f s)" % (latency, ACK_S))
    assert latency <= ACK_S, "the ack came %.3f s after the message, bound %.1f s" % (latency, ACK_S)
    check_ack_line(target, case, parsed, MSGID_5A)
    assert len(out.acks) == 1, "%d ack lines for one message, want exactly 1: %s" % (len(out.acks), [p["raw"] for _t, p in out.acks])
    # nothing else came back to the tester except server comments (the sender never gets its own packet back)
    others = [line for _t, line in out.all_packets if line not in [p["raw"] for _t, p in out.acks]]
    if others:
        note(target, case, "RECORD", "other packet lines at the tester: %s" % others[:3])
    # the room: once on each path
    check_delivered_once(out)
    rows = out.rows
    over_text = out.dstar_overs[0][1]["text"].rstrip()
    note(target, case, "RECORD", "D-STAR text over %r; header %s" % (over_text, ds.describe(out.dstar_headers) if out.dstar_headers else None))
    assert over_text.startswith(SENDER.split("-")[0]), "text over %r does not start with the sender %s" % (over_text, SENDER)
    assert out.dstar_headers is not None, "no text over header with MY XLX999"
    header = ds.describe(out.dstar_headers)
    assert header["suffix"].strip() == "APRS" and header["ur"].strip() == "CQCQCQ", \
        "text over header UR %r suffix %r, want CQCQCQ / APRS (data.md 7.6)" % (header["ur"], header["suffix"])
    sms = out.sms[0][1]
    note(target, case, "RECORD", "M17 SMS %r (src %s) %.3f s after the message; web row %s" % (
        sms_text(sms), sms.get("src"), out.sms[0][0] - out.t1, {k: rows[0].get(k) for k in ("callsign", "protocol", "room", "item_type")}))
    assert out.sms[0][0] - out.t1 <= DELIVER_WAIT_S, "the SMS took %.1f s" % (out.sms[0][0] - out.t1)
    if not out.sse_texts:
        note(target, case, "FINDING", "the public SSE carried no `text` event for the APRS-IS message (the history row is there)")
    else:
        note(target, case, "RECORD", "public SSE text event %.3f s after the message" % (out.sse_texts[0][0] - out.t1))
    # counters (the worker refreshes every 10 s)
    ack, seen_ack = counter_delta(hub, "dv_pkt_ack_total", out.before_acks["ack"], result="ack")
    accepted, seen_items = counter_delta(hub, "dv_pkt_items_total", out.before_items["accepted"], kind="text", result="accepted")
    deltas = record_counters(hub, target, case, out)
    assert seen_ack and ack == 1, "dv_pkt_ack_total{result=ack} moved by %s, want 1: %s" % (ack, deltas)
    assert seen_items and accepted == 1, "dv_pkt_items_total{kind=text,result=accepted} moved by %s, want 1: %s" % (accepted, deltas)


# ================================================================ 5b: a repeat is acked again, delivered once

def test_dp15_5b_a_repeated_aprsis_message_is_acked_again_and_delivered_once(aprsc, rig, target, hub, web_ctx):
    """data.md 15.5 (APRS-IS): "a repeated message is acked again and not delivered twice". The identical line (same source,
    addressee, text and id) is sent again REPEAT_AFTER_S (40 s) after the first: aprsc has forgotten it (its window is
    30 s, so the server does not swallow it) and the controller has not (60 s). Expected: TWO ack lines in all (the
    second is a byte for byte equal ack, it passes the server because the first left its window), ONE D-STAR text over,
    ONE M17 SMS, ONE web row; counters dv_pkt_ack_total{ack} +1 and {repeated} +1, dv_pkt_items_total{kind=text,
    result=accepted} +1. A repeat sooner than 30 s would be swallowed by the server (the DV-Router lead: the ack is
    identical, so the server drops it) and a repeat after 60 s would be delivered again: both are documented
    consequences, not defects, and are not tested here."""
    case = "5b"
    assert SERVER_DUPE_S < REPEAT_AFTER_S < CONTROLLER_DUPE_S - 5.0
    wait_edge_login(hub, target, case)
    body = ds.nonce_body("RPT")
    out = send_and_collect(rig, target, hub, web_ctx, aprsc, case, body, MSGID_5B, repeat=True)
    note(target, case, "RECORD", "first message: D-STAR text overs %d, M17 SMS %d, acks %d; at the end %s" % (
        out.after_first.text_overs, out.after_first.sms, out.after_first.acks, vars(out.final)))
    assert out.repeat_gap > SERVER_DUPE_S and out.repeat_gap < CONTROLLER_DUPE_S, "repeat after %.1f s" % out.repeat_gap
    # the first message was delivered and acked as in 5a
    assert out.after_first.acks == 1 and out.after_first.text_overs == 1 and out.after_first.sms == 1, \
        "the first message: %s, want one of each (see 5a)" % vars(out.after_first)
    # two acks in all, both of the documented shape, byte for byte equal, the second in time after the repeat
    assert len(out.acks) == 2, "%d ack lines after the repeat, want 2 (acked again): %s" % (len(out.acks), [p["raw"] for _t, p in out.acks])
    for _at, parsed in out.acks:
        check_ack_line(target, case, parsed, MSGID_5B)
    (at1, first), (at2, second) = out.acks
    assert first["raw"] == second["raw"], "the repeated ack differs from the first:\n  %s\n  %s" % (first["raw"], second["raw"])
    latency = at2 - out.t2
    note(target, case, "RECORD", "second ack %.3f s after the repeat (bound %.1f s), %.1f s after the first ack" % (latency, ACK_S, at2 - at1))
    assert at2 > out.t2, "the second ack came before the repeat was sent"
    assert latency <= ACK_S, "the repeated ack came %.3f s after the repeat, bound %.1f s" % (latency, ACK_S)
    assert at2 - at1 > SERVER_DUPE_S, "the two acks are %.1f s apart, inside the server window of %.0f s" % (at2 - at1, SERVER_DUPE_S)
    # delivered once
    assert out.final.text_overs == 1, "%d D-STAR text overs for a message sent twice, want 1" % out.final.text_overs
    assert out.final.sms == 1, "%d M17 SMS for a message sent twice, want 1" % out.final.sms
    assert len(out.rows) == 1, "%d web text rows for a message sent twice, want 1: %s" % (len(out.rows), out.rows[:3])
    assert len(out.sse_texts) <= 1, "%d public SSE text events for a message sent twice, want at most 1" % len(out.sse_texts)
    # counters: ack then repeated
    ack, seen_ack = counter_delta(hub, "dv_pkt_ack_total", out.before_acks["ack"], result="ack")
    repeated, seen_rep = counter_delta(hub, "dv_pkt_ack_total", out.before_acks["repeated"], result="repeated")
    accepted, seen_items = counter_delta(hub, "dv_pkt_items_total", out.before_items["accepted"], kind="text", result="accepted")
    deltas = record_counters(hub, target, case, out)
    assert seen_ack and ack == 1, "dv_pkt_ack_total{result=ack} moved by %s, want 1: %s" % (ack, deltas)
    assert seen_rep and repeated == 1, "dv_pkt_ack_total{result=repeated} moved by %s, want 1: %s" % (repeated, deltas)
    assert seen_items and accepted == 1, "dv_pkt_items_total{kind=text,result=accepted} moved by %s, want 1: %s" % (accepted, deltas)
    if deltas["text.duplicate"] != 1 and deltas["room.duplicate"] != 1:
        note(target, case, "FINDING", "neither dv_pkt_items_total{text,duplicate} nor dv_room_data_total{duplicate} moved by 1 (%s)" % deltas)
