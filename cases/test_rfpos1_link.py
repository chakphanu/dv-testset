# SPDX-License-Identifier: MIT
"""RFPOS1 step B (DV-Router 47dac7b4 in image dv-router:wip-c3b24d2, code 5333e4fc): the node link edge states frame positions, so a frame lost on a
link keeps its position through the room's transcoder. Document: link.md "Appendix A" (Units: one unit per record, `sequence` = the sender's 20 ms
position), media-format.md "Unit granularity", floor-control.md RTS flags (RTS_FRAME_POSITIONS: a non-D-STAR link stream); docs/rfpos1-cases.md.
Pure helpers: dvtest/rfpos1.py. Switch: DVT_RFPOS1B=on. NOT RUN on a rig: written against the sources of 47dac7b4.

The pair: node 1 = the SUT (XLX999), node 2 = dvr-peer (XLX998), profile twonode; with DVT_RFPOS1B=on scripts/run.sh also links them by a NODE LINK
(`[edges.link]`, rfpos1-link-a.toml / -b.toml, room G both sides; NOT the XLX interlink of DP15 7b-7d). Node 1's uplink dials the TESTER
(<tester>:31801), where this file runs an opaque UDP relay (dvtest.rfpos1.LinkRelay) to node 2's link server. The datagrams are authenticated but not
encrypted and the tester has no key: the relay reads the cleartext record header, forwards everything unchanged and drops exactly ONE kind-20
(Units) record when told to. That is the black-box way to lose one link frame; both talker and listener are ordinary protocol clients.

  RFPOS1B-a  no loss, DMR talker on node 1 (ambe2-dmr, 3 units per frame) -> D-STAR listener on node 2 (transcoded by node 2's room): the link
             carries one unit per Units record at sequence 0..N-1 (the 20 ms position), and the listener gets positions 0..N-1, each once (and, with the
             fake transcoder, every unit byte for byte)
  RFPOS1B-b  the same over with the Units record of position P (in the middle of a DMR burst) dropped on the link: the listener's output has exactly
             one gap, AT P (not at the end: the pre-RFPOS1 output was a contiguous run 0..N-2), every other unit at its own position; lost count 1
  RFPOS1B-c  a D-STAR talker on node 1 (ambe-dstar) -> D-STAR listener on node 2, one record dropped: the same gap shape at the record's position, no
             change from before RFPOS1 (an ambe-dstar link stream is positioned by profile)

Run: docs/rfpos1-cases.md. Every case skips with the reason when its prerequisite is missing; observations go to stdout and out/<run>/rfpos1.txt
("RECORD" = what was seen, "FINDING" = differs from the documented behaviour).
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import clients, crossmode, rfpos1  # noqa: E402
from dvtest.audio import signals  # noqa: E402

import data_support as ds  # noqa: E402

pytestmark = pytest.mark.rig

LOG = "rfpos1.txt"
ROOM = rfpos1.ROOM
COUNT = 90                          # 20 ms units per over: 30 DMR bursts, 1.8 s (above the 50-frame short stream guard)
DROP = 40                           # the dropped position: burst 13, unit 1 (a loss in the middle of a frame)
LINK_WAIT_S = 150.0                 # node 1 dials the relay and the link comes up (the reconnect delay of the client is not specified)
DEMAND_WAIT_S = 20.0                # node 2's Demand for the listener's profile reaches node 1
TAIL_S = 3.0                        # after the talk: the last records cross the link and the listener drains
GAP_S = 3.0                         # between two overs


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting="twonode,link", log=LOG)


@pytest.fixture(scope="module")
def link(rig, target):
    """The relay, with node 1's uplink through it and room G accepted for talk; skips with the reason otherwise."""
    if not rfpos1.strict_on():
        pytest.skip("DVT_RFPOS1B=on is not set (the image has RFPOS1 step B, DV-Router 47dac7b4: dv-router:wip-c3b24d2 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_TWONODE") != "on":
        pytest.skip("the second DV-Router node is not part of this rig: run scripts/run.sh --profile twonode with DVT_RFPOS1B=on")
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
        pytest.skip("the node link is not up after %.0f s (%s; records seen at the relay %s): is rfpos1-link-a.toml in the SUT's config "
                    "(scripts/run.sh --profile twonode with DVT_RFPOS1B=on) and rfpos1-link-b.toml in dvr-peer's?" % (LINK_WAIT_S, why, seen))
    status, name = ack
    if status != 1:
        relay.stop()
        pytest.skip("room %s is not accepted for talk on the link: RoomMapAck %s" % (ROOM, name))
    note(target, "link", "RECORD", "node 1 %s -> relay tester:%d -> node 2 %s:%d; room %s accepted (%s); records %s" % (
        rig.xlxd_sut, rfpos1.LINK_PORT, ip2, rfpos1.SERVER_PORT, ROOM, name, seen))
    try:
        yield types.SimpleNamespace(relay=relay, ip1=rig.xlxd_sut, ip2=ip2, out=Path(target.out), target=target)
    finally:
        note(target, "link", "RECORD", "relay at the end: forwarded %d, dropped %d, records %s" % (
            relay.forwarded, relay.dropped, rfpos1.kinds_seen(relay.snapshot())))
        relay.stop()


def fake_on():
    return os.environ.get("DVT_TRANSCODER_FAKE") == "1"


def talk_over(link, src_kind, listener_profile, drop=None, case=""):
    """One over of COUNT units from a `src_kind` talker on node 1 to a D-STAR listener on node 2, with the record of position `drop` lost on the
    link. Returns (frames sent, [(position, frame9)] at the listener, repeats, the relay's events of this over)."""
    link.relay.disarm()
    frames = signals.prng_frames({"dmr": "dmr", "dstar": "dstar"}[src_kind], COUNT, seed="rfpos1b/%s" % src_kind)
    wire_profile = {"dmr": 2, "dstar": 1}[src_kind]
    before = link.relay.mark()
    with ds.dstar_listener(link.ip2, module=ROOM, callsign="N0CALL") as listener:
        # node 2's room demands the listener's codec and forwards it to node 1 (a Demand record) before the over starts
        end = time.monotonic() + DEMAND_WAIT_S
        while time.monotonic() < end and not rfpos1.demand_for(link.relay.snapshot(), listener_profile, since=before):
            listener.client.pump(0.5)
        if not rfpos1.demand_for(link.relay.snapshot(), listener_profile, since=before):
            note(link.target, case, "RECORD", "no Demand for profile %d seen within %.0f s of the listener's link (the over goes on)" % (
                listener_profile, DEMAND_WAIT_S))
        mark = link.relay.mark()
        if drop is not None:
            link.relay.arm(wire_profile, drop)
        talker = clients.make(src_kind, ROOM, 0)
        try:
            assert talker.link(link.ip1), "%s link to node 1 module %s failed" % (src_kind, ROOM)
            time.sleep(1.2)
            clients.talk(talker, src_kind, frames, tail=TAIL_S)
        finally:
            clients.close(talker)
        listener.client.pump(TAIL_S)
        got, repeats = rfpos1.dstar_voice(list(listener.client.inbox))
    events = link.relay.snapshot(mark)
    link.relay.disarm()
    time.sleep(GAP_S)
    return frames, got, repeats, events


def expected_units(src_kind, frames):
    """The frame each 20 ms position must hold at the listener, or None when it is not predictable (a real transcoder)."""
    if src_kind == "dstar":
        return [bytes(f) for f in frames]
    if not fake_on():
        return None
    voice = crossmode.source_units("dmr", frames).voice
    return crossmode.transform_units(crossmode.CODEC["dmr"], crossmode.CODEC["dstar"], voice)


def judge(link, case, src_kind, wire_profile, drop, result):
    frames, got, repeats, events = result
    wire = rfpos1.over_records(events, wire_profile)
    problems = [("link wire", p) for p in rfpos1.wire_problems(wire, wire_profile, COUNT, dropped=drop)]
    expected = expected_units(src_kind, frames)
    problems += [("listener", p) for p in rfpos1.gap_problems(got, COUNT, dropped=drop, expected=expected)]
    seqs = [r["sequence"] for r in wire]
    positions = sorted(p for p, _f in got)
    note(link.target, case, "RECORD", "link: %d Units records of profile %d (sequences %s..%s), dropped %s; listener: %d frames, positions %s..%s, "
         "missing %s, repeats %d, payload check %s" % (
             len(wire), wire_profile, min(seqs, default=None), max(seqs, default=None), [r["sequence"] for r in wire if r["dropped"]],
             len(got), min(positions, default=None), max(positions, default=None), [q for q in range(COUNT) if q not in set(positions)][:10],
             repeats, "byte for byte" if expected is not None else "not predictable (a real transcoder)"))
    for where, text in problems:
        note(link.target, case, "FINDING", "%s: %s" % (where, text))
    return problems


def test_rfpos1b_a_no_loss_dmr_to_dstar_keeps_every_position(link):
    """No loss: the link carries the DMR over as one ambe2-dmr unit per Units record at sequence 0..N-1, and node 2's D-STAR listener gets the
    positions 0..N-1 once each, with the exact unit of each position when the transcoder is the fake."""
    result = talk_over(link, "dmr", 1, drop=None, case="a")
    problems = judge(link, "a", "dmr", 2, None, result)
    assert not problems, "; ".join("%s: %s" % p for p in problems)


def test_rfpos1b_b_one_lost_link_frame_is_a_gap_at_its_own_position(link):
    """One Units record (position DROP) is lost on the link: node 2's transcoded D-STAR output has exactly that one position missing and every
    other position at its own place (RFPOS1 step B). The pre-RFPOS1 output renumbered the survivors by arrival (contiguous 0..N-2, the gap at the
    end), which the judge reports as `missing positions [N-1]`."""
    result = talk_over(link, "dmr", 1, drop=DROP, case="b")
    problems = judge(link, "b", "dmr", 2, DROP, result)
    assert not problems, "; ".join("%s: %s" % p for p in problems)


def test_rfpos1b_c_dstar_link_stream_keeps_its_gap(link):
    """An ambe-dstar link stream is unchanged by RFPOS1 step B: one lost record is a gap at its own position at node 2's D-STAR listener, the
    survivors carry their own frames."""
    result = talk_over(link, "dstar", 1, drop=DROP, case="c")
    problems = judge(link, "c", "dstar", 1, DROP, result)
    assert not problems, "; ".join("%s: %s" % p for p in problems)
