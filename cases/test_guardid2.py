# SPDX-License-Identifier: MIT
"""GUARDID2 (DV-Router 9a8d1cb4 in image dv-router:wip-3f142cd): the guard source identity crosses the router link.

Document: floor-control.md "Guard penalties" (the key of an RTS with RTS_SOURCE_ID is the decimal source id, so two unmapped sources behind one
placeholder callsign never strike each other), link.md "Appendix A" (bit 0 of the StreamStart `carried` u16 is the `source_id` flag, no bytes
added; the receiving link edge states RTS_SOURCE_ID), media-format.md. Pure helpers: dvtest/guardid2.py (and the relay of dvtest/rfpos1.py).
Switch: DVT_GUARDID2=on. Document of the cases: docs/guardid2-cases.md.

The pair: node 1 = the SUT (XLX999), node 2 = dvr-peer (XLX998), profile twonode, linked by a NODE LINK for room G (rfpos1-link-a / -b.toml; the
uplink of node 1 dials the tester, where this file runs the opaque relay of dvtest.rfpos1.LinkRelay that logs the cleartext record headers) and
both serve P25 (guardid2-p25.toml: UDP 41000, TG 31672 -> room G, no_radio_id fallback). Talkers and listeners are ordinary P25 clients, so no
transcoder is involved: node 2's listener hears node 1's IMBE units unchanged and each over is told apart by its own frames.

One scenario per case (the same ladder, run for a struck talker S and an other talker O in room G):
  1. S sends a long over (54 frames; relayed, heard on both nodes)
  2. S sends three short overs (9 frames, 0.5 s apart): the B18 ladder earns strike 1, a 3 s TX block on S's guard key
  3. S sends a probe over inside the block: refused at node 1 (counted deny_guard_block), nothing of it crosses the link
  4. O sends a long over inside the same block (about 1 s after the strike)
  GUARDID2-a  S, O unmapped (source ids in no directory, shared callsign P31672): every StreamStart of both on the link has the flag, the shown
              callsign P31672 and the talker's own radio id, no bytes added; O is granted on node 1 AND node 2 (heard whole by listeners on both
              nodes; node 2 has no guard denial, no Refused record on the link); S's probe is not heard anywhere
  GUARDID2-b  S = N0CALL, O = N0TEST (mapped): every StreamStart has the flag clear and the callsign; the strike blocks S's probe and not the
              other callsign: the same outcome as before the change

Counters are the hubs' (control channel `metrics xlxd-sut` / `metrics dvr-peer`, refreshed about every 10 s: read until three reads agree), the
deltas go to out/<run>/guardid2/<case>.json with the link records, what each listener heard and the verdict. Every case skips with the reason
when its prerequisite is missing; observations go to stdout and out/<run>/guardid2.txt ("RECORD" = what was seen, "FINDING" = differs from the
documented behaviour). Not tested (floor-control.md: one shared guard identity by design): radio FM ports, USRP rows, AllStar links.
"""
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import guardid2 as g2, rfpos1  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import p25  # noqa: E402

import data_support as ds  # noqa: E402

pytestmark = pytest.mark.rig

LOG = "guardid2.txt"
LINK_WAIT_S = 150.0                 # node 1 dials the relay and the link comes up
READY_WAIT_S = 90.0                 # the P25 server ports of both nodes answer a poll
LONG = 54                           # frames of a long over (6 whole LDUs, 1.1 s; above the 50-frame short stream guard)
SHORT = 9                           # frames of a short over (one LDU, far below 50)
SHORT_GAP_S = 0.5                   # close to next open, well inside the 2 s of the guard
STEP_GAP_S = 0.3                    # between the steps that follow the strike: the 3 s block of strike 1 is still in force
TAIL_S = 3.0                        # after the last over: the records cross the link and the listeners drain
SETTLE_PERIOD_S = 6.0               # the hub refreshes its records about every 10 s
SETTLE_BUDGET_S = 120.0
SETTLE_AGREE = 3                    # reads that must agree
FLOOR_FIRST_S = 12.0                # wait before the reads that follow traffic: one refresh period plus slack

NODES = (("node1", "xlxd-sut"), ("node2", "dvr-peer"))
SEEN = {"flagged": [], "plain": []}  # StreamStart records of case a / case b, for the size comparison


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting="twonode,link,p25", log=LOG)


# ---------------------------------------------------------------- the rig

@pytest.fixture(scope="module")
def link(rig, target, rig_control):
    """The relay, with node 1's uplink through it and room G accepted for talk; skips with the reason otherwise."""
    if not g2.on():
        pytest.skip("DVT_GUARDID2=on is not set (the image has GUARDID2, DV-Router 9a8d1cb4: dv-router:wip-3f142cd or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_TWONODE") != "on":
        pytest.skip("the second DV-Router node is not part of this rig: run scripts/run.sh --profile twonode with DVT_GUARDID2=on")
    ip2 = os.environ.get("DVT_DVR_PEER", "")
    if not ip2:
        pytest.skip("DVT_DVR_PEER (the address of dvr-peer) is not set")
    relay = rfpos1.LinkRelay(listen_port=rfpos1.LINK_PORT, upstream=(ip2, rfpos1.SERVER_PORT)).start()
    end = time.monotonic() + LINK_WAIT_S
    state = (False, None, "")
    while time.monotonic() < end:
        state = rfpos1.handshake_state(relay.snapshot())
        if state[1] is not None:
            break
        time.sleep(1.0)
    welcomed, ack, why = state
    seen = rfpos1.kinds_seen(relay.snapshot())
    if ack is None:
        relay.stop()
        pytest.skip("the node link is not up after %.0f s (%s; records seen at the relay %s): are rfpos1-link-a.toml in the SUT's config and "
                    "rfpos1-link-b.toml in dvr-peer's (scripts/run.sh --profile twonode with DVT_GUARDID2=on)?" % (LINK_WAIT_S, why, seen))
    status, name = ack
    if status != 1:
        relay.stop()
        pytest.skip("room %s is not accepted for talk on the link: RoomMapAck %s" % (g2.ROOM, name))
    note(target, "link", "RECORD", "node 1 %s -> relay tester:%d -> node 2 %s:%d; room %s accepted (%s); records %s" % (
        rig.xlxd_sut, rfpos1.LINK_PORT, ip2, rfpos1.SERVER_PORT, g2.ROOM, name, seen))
    try:
        yield types.SimpleNamespace(relay=relay, ip1=rig.xlxd_sut, ip2=ip2, out=Path(target.out), target=target, control=rig_control)
    finally:
        note(target, "link", "RECORD", "relay at the end: forwarded %d, dropped %d, records %s" % (
            relay.forwarded, relay.dropped, rfpos1.kinds_seen(relay.snapshot())))
        relay.stop()


def client(callsign, src_id):
    return p25.P25Client("0.0.0.0", callsign=callsign, src_id=src_id, tg=g2.TG)


def link_to(c, ip, budget_s=READY_WAIT_S):
    """Poll the P25 server until it echoes (the edge may still be starting); True when linked."""
    end = time.monotonic() + budget_s
    while time.monotonic() < end:
        if c.link(ip, timeout=3.0):
            return True
    return False


def idle(clients, seconds):
    """Drain every client for `seconds`, polling when due (a P25 link needs a poll at least every 30 s)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for c in clients:
            if c.dest and time.monotonic() - c.last_poll >= p25.POLL_PERIOD:
                c.keepalive()
            c.client.drain()
        time.sleep(0.02)


def over(talker, frames, everyone):
    """One over of `frames` (11-byte IMBE) from `talker`: whole LDUs and the terminator, 20 ms apart; the others are drained meanwhile."""
    records = p25.call_records(frames, talker.src_id, talker.tg)
    talker.send(records, p25.GAP, *[c for c in everyone if c is not talker])


def heard_frames(listener):
    return [r["imbe"] for r in listener.records() if not r["terminator"]]


def heard_sources(listener):
    return sorted({r["src"] for r in listener.records() if "src" in r})


def counters_of(control, service):
    return g2.counters(control.metrics(service))


def settled(control, service, first_wait_s=0.0):
    """The counters of `service` once SETTLE_AGREE reads in a row agree (the hub refreshes its records about every 10 s)."""
    if first_wait_s:
        time.sleep(first_wait_s)
    end = time.monotonic() + SETTLE_BUDGET_S
    reads = [counters_of(control, service)]
    while True:
        if len(reads) >= SETTLE_AGREE and all(r == reads[-1] for r in reads[-SETTLE_AGREE:]):
            return reads[-1]
        if time.monotonic() >= end:
            return reads[-1]
        time.sleep(SETTLE_PERIOD_S)
        reads.append(counters_of(control, service))


# ---------------------------------------------------------------- the ladder

def scenario(link, case, struck, other):
    """Run the ladder for the talkers `struck` and `other` (each (name, callsign, src_id, poll callsign)); returns everything the judge needs."""
    s_name, s_call, s_id, s_poll = struck
    o_name, o_call, o_id, o_poll = other
    out = link.out
    before = {n: settled(link.control, svc) for n, svc in NODES}
    ts, to = client(s_poll, s_id), client(o_poll, o_id)
    la, lb = client("N0LSTA", 1023991), client("N0LSTB", 1023992)
    everyone = (ts, to, la, lb)
    frames = {
        "base": signals.prng_frames("imbe", LONG, seed="guardid2/%s/base" % case),
        "short1": signals.prng_frames("imbe", SHORT, seed="guardid2/%s/short1" % case),
        "short2": signals.prng_frames("imbe", SHORT, seed="guardid2/%s/short2" % case),
        "short3": signals.prng_frames("imbe", SHORT, seed="guardid2/%s/short3" % case),
        "probe": signals.prng_frames("imbe", SHORT, seed="guardid2/%s/probe" % case),
        "other": signals.prng_frames("imbe", LONG, seed="guardid2/%s/other" % case),
    }
    try:
        for c, ip in ((ts, link.ip1), (to, link.ip1), (la, link.ip1), (lb, link.ip2)):
            assert link_to(c, ip), "P25 client %s: no poll echo from %s within %.0f s (is guardid2-p25.toml in both configs?)" % (c.callsign, ip, READY_WAIT_S)
        idle(everyone, 1.5)
        mark = link.relay.mark()
        t0 = time.monotonic()
        steps = {}

        def step(name, talker):
            steps[name] = round(time.monotonic() - t0, 2)
            over(talker, frames[name], everyone)

        step("base", ts)
        idle(everyone, 0.4)
        for name in ("short1", "short2", "short3"):
            step(name, ts)
            idle(everyone, SHORT_GAP_S if name != "short3" else STEP_GAP_S)
        step("probe", ts)
        idle(everyone, STEP_GAP_S)
        step("other", to)
        steps["end"] = round(time.monotonic() - t0, 2)
        idle(everyone, TAIL_S)
        heard = {}
        for who, listener in (("node1", la), ("node2", lb)):
            got = heard_frames(listener)
            heard[who] = {name: g2.heard(got, fr) for name, fr in frames.items()}
            heard[who]["frames_total"] = len(got)
            heard[who]["sources"] = heard_sources(listener)
    finally:
        for c in everyone:
            try:
                if c.dest:
                    c.unlink()
            finally:
                c.close()
    events = link.relay.snapshot(mark)
    after = {n: settled(link.control, svc, first_wait_s=FLOOR_FIRST_S) for n, svc in NODES}
    moved = {n: g2.delta(before[n], after[n]) for n, _ in NODES}
    return types.SimpleNamespace(case=case, frames=frames, steps=steps, heard=heard, events=events, before=before, after=after, moved=moved,
                                 struck=struck, other=other, out=out)


def judge(link, run, flag):
    """The problems (list of (where, sentence)) of one scenario run and the StreamStart records of the two talkers. `flag`: whether every
    StreamStart of both talkers must carry the source-id flag. The struck talker opens four link streams (the long over and the three short ones;
    its probe is refused on node 1), the other talker one."""
    s_name, s_call, s_id, _ = run.struck
    o_name, o_call, o_id, _ = run.other
    problems = []
    found_s = g2.starts(run.events, {s_id})
    found_o = g2.starts(run.events, {o_id})
    problems += [("link wire", p) for p in g2.wire_problems(found_s, s_id, s_call, flag, 4)]
    problems += [("link wire", p) for p in g2.wire_problems(found_o, o_id, o_call, flag, 1)]
    size = g2.same_size(found_s, found_o)
    if size:
        problems.append(("link wire", size))
    refused = g2.refused(run.events)
    if refused:
        problems.append(("node 2 refused", "Refused records on the link (node 2's room denied a relayed over): %s" % refused))
    # what the listeners heard: base and other whole, the probe nowhere
    for who in ("node1", "node2"):
        h = run.heard[who]
        problems += [(who + " listener", p) for p in g2.listener_problems(who, h["base"], LONG, True, "%s's first over (before the strike)" % s_name)]
        problems += [(who + " listener", p) for p in g2.listener_problems(who, h["probe"], SHORT, False, "%s's probe inside the block" % s_name)]
        problems += [(who + " listener", p) for p in g2.listener_problems(who, h["other"], LONG, True, "%s's over inside the block of %s" % (o_name, s_name))]
    # counters: node 1 saw the strike (denied the probe); node 2 granted every relayed over and denied none
    streams = len(found_s) + len(found_o)
    problems += [("node1 counters", p) for p in g2.counter_problems("node1", run.moved["node1"], grants_at_least=streams, denials_at_least=1)]
    problems += [("node2 counters", p) for p in g2.counter_problems("node2", run.moved["node2"], grants_at_least=streams, denials_exactly=0)]
    return problems, found_s, found_o


def report(link, run, problems, found_s, found_o):
    s_name, s_call, s_id, _ = run.struck
    o_name, o_call, o_id, _ = run.other
    case = run.case
    note(link.target, case, "RECORD", "link StreamStart: %s (id %d) %d streams, %s (id %d) %d streams; flags %s; callsigns %s; masks %s; sizes %s" % (
        s_name, s_id, len(found_s), o_name, o_id, len(found_o), sorted({r["flag"] for r in found_s + found_o}),
        sorted({r["callsign"] for r in found_s + found_o}), ["0x%04x" % r["carried"] for r in found_s + found_o],
        sorted({r["size"] for r in found_s + found_o})))
    note(link.target, case, "RECORD", "steps (s) %s; heard node1 %s; heard node2 %s" % (run.steps, run.heard["node1"], run.heard["node2"]))
    for node, _svc in NODES:
        note(link.target, case, "RECORD", "%s counters moved: %s" % (node, dict(sorted(run.moved[node].items()))))
    note(link.target, case, "RECORD", "relay events: records %s; Refused %s" % (rfpos1.kinds_seen(run.events), g2.refused(run.events)))
    for where, text in problems:
        note(link.target, case, "FINDING", "%s: %s" % (where, text))
    directory = link.out / "guardid2"
    directory.mkdir(parents=True, exist_ok=True)
    doc = {"case": case, "struck": {"name": s_name, "callsign": s_call, "radio_id": s_id}, "other": {"name": o_name, "callsign": o_call, "radio_id": o_id},
           "steps_s": run.steps, "heard": run.heard,
           "frames_sent": {k: len(v) for k, v in run.frames.items()},
           "starts": {"struck": found_s, "other": found_o}, "refused": g2.refused(run.events), "records": rfpos1.kinds_seen(run.events),
           "counters_before": run.before, "counters_after": run.after, "counters_moved": run.moved,
           "problems": ["%s: %s" % p for p in problems], "verdict": "pass" if not problems else "fail"}
    (directory / ("%s.json" % case)).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_guardid2_a_strike_on_one_unmapped_talker_leaves_the_other_granted_on_node_2(link):
    """Two unmapped P25 talkers X and Y (source ids in no directory) share the callsign P31672. X's strike (three short overs) blocks X on node 1
    (its probe is refused there and never crosses the link), and Y, which shares X's callsign, is granted inside the block on node 1 AND on node 2:
    the StreamStart records of both carry the source-id flag (bit 0 of `carried`), the callsign P31672 and the talker's own radio id, with no bytes
    added; node 2 counts no guard denial and sends no Refused."""
    struck = ("X", g2.SHARED, g2.UNMAPPED["X"], "N0UNKA")
    other = ("Y", g2.SHARED, g2.UNMAPPED["Y"], "N0UNKB")
    run = scenario(link, "a", struck, other)
    problems, found_s, found_o = judge(link, run, True)
    SEEN["flagged"] = found_s + found_o
    report(link, run, problems, found_s, found_o)
    assert not problems, "; ".join("%s: %s" % p for p in problems)


def test_guardid2_b_a_mapped_talker_behaves_as_before(link):
    """Control: two mapped talkers (N0CALL, N0TEST: source ids in the rig's directory) keep their callsign guard key: their StreamStart records
    have the flag clear and their own callsign, the strike of N0CALL blocks N0CALL's probe on node 1 (nothing of it crosses the link) and does not
    block N0TEST, which is granted on both nodes; node 2 counts no guard denial. The StreamStart size is the one of case a (no bytes added)."""
    struck = ("M", g2.MAPPED["M"][1], g2.MAPPED["M"][0], "N0CALL")
    other = ("M2", g2.MAPPED["M2"][1], g2.MAPPED["M2"][0], "N0TEST")
    run = scenario(link, "b", struck, other)
    problems, found_s, found_o = judge(link, run, False)
    SEEN["plain"] = found_s + found_o
    if SEEN["flagged"]:
        text = g2.same_size(SEEN["flagged"], SEEN["plain"])
        if text:
            problems.append(("link wire", "case a vs b: " + text))
    report(link, run, problems, found_s, found_o)
    assert not problems, "; ".join("%s: %s" % p for p in problems)
