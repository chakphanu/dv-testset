# SPDX-License-Identifier: MIT
"""Web unit, EDG3 / EDG4b / EDG4c (DV-Router 5abe7510, in bc9af2b1, image dv-router:wip-f1f3777): the WebRTC edge admits ONE ctl DataChannel per peer, expires validated sources after 30 s of
replay-protected silence, and pins the handshake to the selected pair. Documents: design/web.md 1147-1241 (EDG4, EDG4b, ICE1c), 1328-1333, 1367-1394 (EDG3), design/status.md 872-877 and 1027-1036;
docs/edg4-cases.md. Helpers: dvtest/edg4.py (counters, SDP facts, the expiry judge), dvtest/stun_pair.py (a signed STUN Binding client, so a tester socket can become a VALIDATED source of a
peer). Switch DVT_EDG4=on. NOT RUN on a rig: written against the sources of bc9af2b1. Needs `--profile web,browser` with a USRP dynamic hub (DVT_USRP_DYNAMIC=1, the Listen room of cases/test_web_k22.py).

  EDG3-a  a normal Listen session opens exactly one data channel (`ctl`), closes none, and moves no channel_refused / channel_closed / channel_limit_closes counter
  EDG3-b  a page opens one more channel (label `x`): that stream is reset (a channel-close event for `x`), the peer, `ctl` and the connection stay; refused{unknown_label} +1, closed{unknown_label} +1,
          limit_closes 0. Then a second refused open (`y`): the whole association closes (the connection leaves `connected` within 8 s, the gauge falls by one), limit_closes +1, refused +1 more
  EDG4b   a tester socket validated by a signed Binding check: refreshed every 5 s for 45 s it never expires (control); silent it expires once, 30 s after its last fresh check
          (+ the 10 s publication of the counter), the peer (browser, its own source fresh) lives on; after the expiry a non-STUN datagram from that socket is counted unrouted
  EDG4c   (hand-written offer, no browser) a signed USE-CANDIDATE check from socket A selects it, a plain signed check from socket B validates it, a plaintext fatal DTLS alert from B is dropped:
          the peer still answers A's next check, and unrouted{wrong_peer or no_source} rises; the handshake switch itself is a GAP (unit tests only)
"""
import socket
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import edg4, metrics, sdp_offer as so, stun_pair as sp  # noqa: E402

import data_support as ds  # noqa: E402
from test_web_k22 import gauge_below, k22_users, make_page, stable_listeners  # noqa: E402,F401

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc]

LOG = "edg4.txt"
ROOM = "H"
EDG1_GAUGE_S = 40.0


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not edg4.strict_on():
        pytest.skip("DVT_EDG4=on is not set (the image under test has EDG3 / EDG4b / EDG4c, DV-Router 5abe7510: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or __import__("os").environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


@pytest.fixture
def read(rig_control):
    def fn():
        samples = metrics.parse(rig_control.metrics("xlxd-sut"))
        assert edg4.families_present(samples), "no dv_edge_webrtc_channel_* / ice_source_expired series in the hub metrics: the image does not have EDG3 / EDG4b"
        return edg4.counters(samples)
    return fn


def settled(read, ready, budget_s=edg4.SETTLE_S, every_s=5.0):
    """Reads Counters every `every_s` until ready(counters) or the budget (the edge publishes every ~10 s, the hub caches); the last reading."""
    end = time.monotonic() + budget_s
    while True:
        got = read()
        if ready(got) or time.monotonic() >= end:
            return got
        time.sleep(every_s)


def delta(after, before):
    return edg4.Counters(*[edg4.diff_map(a, b) if isinstance(a, dict) else a - b for a, b in zip(after, before)])


def opens(kp, labels=None):
    return [e for e in kp.rtc.events() if e.get("what") == "channel-open" and (labels is None or e.get("label") in labels)]


def closes(kp, label=None):
    return [e for e in kp.rtc.events() if e.get("what") == "channel-close" and (label is None or e.get("label") == label)]


def open_channel(kp, label):
    return kp.page.evaluate("(l) => { const p = window.__dvtTap.last(); const c = p.createDataChannel(l); return c.label; }", label)


# ---------------------------------------------------------------- EDG3

def test_edg3_a_one_ctl_channel_and_no_refusal(make_page, read, target):
    case = "EDG3-a"
    before = read()
    kp = make_page("a", user="N0KA")
    kp.connect()
    time.sleep(4.0)
    labels = [e["label"] for e in opens(kp)]
    assert labels == ["ctl"], "%s: a normal Listen session opened data channels %s, want exactly one `ctl`" % (case, labels)
    assert not closes(kp), "%s: a channel closed in a normal session: %s" % (case, closes(kp))
    after = settled(read, lambda c: False, budget_s=15.0)
    moved = edg4.moved(delta(after, before))
    note(target, case, "RECORD", "counters that moved over the session: %s" % moved)
    assert after.limit_closes == before.limit_closes and after.refused == before.refused and after.closed == before.closed, \
        "%s: a well-behaved client moved the channel counters: %s" % (case, moved)


def test_edg3_b_refused_opens_reset_the_stream_then_close_the_peer(make_page, read, rig_control, target):
    case = "EDG3-b"
    kp = make_page("b", user="N0KB")
    kp.connect()
    time.sleep(3.0)
    level = stable_listeners(rig_control, at_least=1)
    assert level, "%s: the listeners gauge never settled" % case
    before = settled(read, lambda c: False, budget_s=12.0)
    assert open_channel(kp, "x") == "x"
    end = time.monotonic() + 8.0
    while time.monotonic() < end and not closes(kp, "x"):
        time.sleep(0.25)
    assert closes(kp, "x"), "%s: the refused channel `x` was never closed (events %s)" % (case, kp.rtc.events()[-6:])
    time.sleep(3.0)
    assert not closes(kp, "ctl"), "%s: the FIRST refusal closed `ctl` (it resets only the refused stream)" % case
    assert kp.peer_state() == "connected", "%s: the first refusal ended the connection (state %s)" % (case, kp.peer_state())
    mid = settled(read, lambda c: c.refused["unknown_label"] > before.refused["unknown_label"])
    ch1 = delta(mid, before)
    note(target, case, "RECORD", "after the first refused open: %s" % edg4.moved(ch1))
    assert ch1.refused["unknown_label"] == 1 and ch1.closed["unknown_label"] == 1 and ch1.limit_closes == 0, \
        "%s: first refusal counters: refused %s closed %s limit_closes %s (want unknown_label 1 / 1 / 0)" % (case, ch1.refused, ch1.closed, ch1.limit_closes)
    assert open_channel(kp, "y") == "y"
    end = time.monotonic() + 8.0
    while time.monotonic() < end and kp.peer_state() == "connected":
        time.sleep(0.25)
    state = kp.peer_state()
    note(target, case, "RECORD", "connection state 8 s after the second refused open: %s (the edge closes the association cleanly: no ICE consent wait)" % state)
    assert state != "connected", "%s: the second refusal did not end the peer connection" % case
    gone = gauge_below(rig_control, level, EDG1_GAUGE_S + 20.0)
    assert gone is not None and gone <= EDG1_GAUGE_S, "%s: the listeners gauge still counted the peer %s s after the second refusal" % (case, gone or ">%d" % (EDG1_GAUGE_S + 20))
    end_c = settled(read, lambda c: c.limit_closes > before.limit_closes)
    ch2 = delta(end_c, before)
    note(target, case, "RECORD", "after the second refused open: %s" % edg4.moved(ch2))
    assert ch2.limit_closes == 1, "%s: dv_edge_webrtc_channel_limit_closes_total rose by %g, want 1" % (case, ch2.limit_closes)
    assert ch2.refused["unknown_label"] == 2, "%s: refused{unknown_label} rose by %g, want 2 (two refused opens)" % (case, ch2.refused["unknown_label"])


# ---------------------------------------------------------------- EDG4b

class Source:
    """One tester UDP socket aimed at the edge: sends signed Binding checks and waits for the success."""

    def __init__(self, edge_addr, edge_ufrag, edge_pwd, client_ufrag):
        self.addr, self.user, self.pwd = edge_addr, edge_ufrag + ":" + client_ufrag, edge_pwd
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)

    def check(self, use_candidate=False, wait_s=2.5):
        """True when the edge answered the signed check with a Binding success (this source is validated)."""
        tid = sp.new_tid()
        self.sock.sendto(sp.request(tid, self.user, self.pwd, use_candidate=use_candidate), self.addr)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            try:
                data, _src = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            if sp.is_success(data, tid):
                return True
        return False

    def send(self, data):
        self.sock.sendto(data, self.addr)

    def close(self):
        self.sock.close()


def edge_facts(kp):
    sdp = kp.rtc.sdp() or {}
    ufrag, pwd = edg4.ice_credentials(sdp.get("remote"))
    client_ufrag, _ = edg4.ice_credentials(sdp.get("local"))
    cands = edg4.host_candidates(sdp.get("remote"))
    if not (ufrag and pwd and client_ufrag and cands):
        pytest.skip("the answer SDP has no ice credentials or no UDP host candidate (%s, %s, %s)" % (bool(ufrag and pwd), bool(client_ufrag), cands))
    return cands[0], ufrag, pwd, client_ufrag


def test_edg4b_a_silent_validated_source_expires_and_a_fresh_one_does_not(make_page, read, target):
    case = "EDG4b"
    kp = make_page("c", user="N0KC")
    kp.connect()
    time.sleep(3.0)
    edge_addr, ufrag, pwd, client_ufrag = edge_facts(kp)
    # control: validated, refreshed every 5 s for 45 s: it never expires
    a = Source(edge_addr, ufrag, pwd, client_ufrag)
    try:
        assert a.check(), "%s: the edge gave no Binding success to a signed check of the tester (validation of a second source failed)" % case
        base = settled(read, lambda c: False, budget_s=12.0)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 45.0:
            time.sleep(5.0)
            assert a.check(), "%s: a fresh signed check of a validated source got no success at %.0f s" % (case, time.monotonic() - t0)
        control = settled(read, lambda c: False, budget_s=12.0)
        assert control.expired == base.expired, "%s: dv_edge_webrtc_ice_source_expired_total rose by %g while the tester source (and the browser) stayed fresh" % (case, control.expired - base.expired)
    finally:
        a.close()
    # silent: validated once, then nothing
    b = Source(edge_addr, ufrag, pwd, client_ufrag)
    try:
        assert b.check(), "%s: the second tester source was not validated" % case
        t_last = time.monotonic()
        before = settled(read, lambda c: False, budget_s=12.0)
        b.send(sp.junk_rtp())                                             # inside the 30 s: recorded, not asserted (a validated source may send media)
        mid = settled(read, lambda c: False, budget_s=12.0)
        note(target, case, "RECORD", "junk datagram from the validated source inside the 30 s: unrouted moved %s" % {k: v - before.unrouted[k] for k, v in mid.unrouted.items()})
        got = mid
        while got.expired == before.expired and time.monotonic() - t_last < 80.0:
            time.sleep(5.0)
            got = read()
        silent_s = time.monotonic() - t_last
        problems = edg4.judge_expiry(before.expired, got.expired, silent_s if got.expired != before.expired else 80.0)
        note(target, case, "RECORD", "expired counter %g -> %g seen %.0f s after the last fresh check of the silent source" % (before.expired, got.expired, silent_s))
        assert got.expired > before.expired, "%s: the silent validated source never expired (%.0f s of silence)" % (case, silent_s)
        assert not problems, "%s: %s" % (case, "; ".join(problems))
        assert kp.peer_state() == "connected", "%s: the browser's peer did not survive the expiry of the tester's source (state %s)" % (case, kp.peer_state())
        un0 = got
        for _ in range(3):
            b.send(sp.junk_rtp())
            time.sleep(0.3)
        after = settled(read, lambda c: sum(c.unrouted.values()) > sum(un0.unrouted.values()), budget_s=30.0)
        note(target, case, "RECORD", "after the expiry, 3 junk datagrams: unrouted moved %s" % {k: v - un0.unrouted[k] for k, v in after.unrouted.items()})
        assert sum(after.unrouted.values()) > sum(un0.unrouted.values()), "%s: a non-STUN datagram from an expired source was not counted unrouted{no_source|wrong_peer}" % case
        assert b.check(), "%s: a fresh signed check from the expired source was not answered (it must validate again)" % case
    finally:
        b.close()


# ---------------------------------------------------------------- EDG4c

def test_edg4c_an_alternate_source_cannot_kill_a_handshake(web_user, k22_users, read, target):
    case = "EDG4c"
    user = k22_users["N0KA"]
    client = user.client
    offer = so.build_offer(["audio"], ufrag="dvtc")
    resp = client.request("POST", so.OFFER_PATH, json_body={"sdp": offer, "room": ROOM}, pace=False)
    assert resp.status == 200, "%s: the hand-written offer was refused: HTTP %d %s" % (case, resp.status, client.scrub(resp.text())[:120])
    answer = resp.json()
    ufrag, pwd = edg4.ice_credentials(answer.get("sdp"))
    cands = edg4.host_candidates(answer.get("sdp"))
    if not (ufrag and pwd and cands):
        pytest.skip("the answer has no ice credentials or no UDP host candidate")
    before = settled(read, lambda c: False, budget_s=1.0)
    a = Source(cands[0], ufrag, pwd, "dvtc")
    b = Source(cands[0], ufrag, pwd, "dvtc")
    try:
        assert a.check(use_candidate=True), "%s: the signed USE-CANDIDATE check of socket A got no success" % case
        assert b.check(), "%s: the plain signed check of socket B got no success (not validated)" % case
        b.send(sp.dtls_fatal_alert())
        time.sleep(1.5)
        alive = a.check()
        note(target, case, "RECORD", "after B's plaintext fatal DTLS alert, A's next signed check answered: %s" % alive)
        assert alive, "%s: the peer stopped answering the selected source after a fatal alert from an alternate validated source (the handshake pin must drop it)" % case
    finally:
        a.close()
        b.close()
        try:
            client.request("POST", so.CLOSE_PATH, json_body={"sess_id": answer.get("sess_id")}, pace=False)
        except Exception:                                                  # the lease would end it anyway
            pass
    after = settled(read, lambda c: sum(c.unrouted.values()) > sum(before.unrouted.values()), budget_s=30.0)
    moved = {k: v - before.unrouted[k] for k, v in after.unrouted.items()}
    note(target, case, "RECORD", "unrouted moved %s" % moved)
    assert sum(moved.values()) >= 1, "%s: the alert from the alternate source was not counted in dv_edge_webrtc_unrouted_total (status.md: wrong_peer)" % case
    if not moved.get("wrong_peer"):
        note(target, case, "FINDING", "unrouted moved as %s, not wrong_peer (status.md 1027-1036 names wrong_peer for alternate non-STUN input during the initial handshake)" % moved)
