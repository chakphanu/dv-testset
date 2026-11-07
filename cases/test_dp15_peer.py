# SPDX-License-Identifier: MIT
"""DV-Router data-channel cases of data.md 15.5 "XLX peer": the remote floor, recording and cross-mode effects of a
synthetic text over on the RECEIVING node. Two DV-Router nodes of the same image: the SUT (xlxd-sut, XLX999, .11) is the
sending node, the rig service dvr-peer (XLX998, .16, profile twonode) is a plain XLX interlink peer of it on modules DE.
Specification: docs/dp15-plan.md (unit U8); case table, wording, bounds, observation points and run command:
docs/dp15-cases.md "DP15-7b, 7c, 7d"; the setup: docs/rig.md "Two DV-Router nodes".

  DP15-7b  remote floor: while the text over of node 1 comes in, the module is busy on node 2: a local talker that keys
           inside the 440 ms is denied (silent drop, `dv_room_rts_total{result=deny}`), one that keys after the end gets
           the floor (grant), the listener hears only the text over from the first
  DP15-7c  recording: node 2 records the over (class 1 voice from the reflector callsign: `dv_recorder_records_total`
           and its archive directory), node 1 records nothing and takes no floor for it (local exclusion)
  DP15-7d  cross-mode: the DExtra (D-STAR) and M17 listeners of node 2 get the over (D-STAR header, text blocks, M17
           voice of the carried or transcoded silence), and node 2 derives no data item from it (the re-injection guard)

The trigger is a class 0 text: a direct M17 SMS from the tester to module D of node 1 (no web needed). Observation
points are the hubs of both nodes through the control channel (`metrics xlxd-sut`, `metrics dvr-peer`, refreshed every
10 s, so every counter check polls and settles), the protocol listeners on node 2 and the recorder archive directory
out/<run>/recorder-peer. The web service of node 2 is NOT part of the rig (the TLS proxy is bound to the SUT), so the
"shown as remote" state of the room and web listeners are not observed; see docs/dp15-cases.md.

Run: DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_CONFIG=<template>
DVT_TRANSCODER_FAKE=1 DVT_RIG_CONTROL=1 scripts/run.sh -m rig --capture --profile twonode -- cases/test_dp15_peer.py
Every case skips with the reason when its prerequisite is missing. Observations go to stdout and
out/<run>/dp15-peer.txt ("RECORD" = what was seen, "FINDING" = differs from the documented behaviour).

First rig run (20261107T111542Z): 7c and 7d passed; 7b failed on the node 1 assertion only, because node 1's delta covered the
whole case, including the relayed stream of the second talker (a harness error, not a product finding). 7b now reads node 1
before the second talker keys. NOT RERUN since that fix.
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import metrics, slowdata  # noqa: E402
from dvtest.proto import dextra, m17  # noqa: E402

import data_support as ds  # noqa: E402
from data_support import PACER, Hub, ambe, build_over, plain_fields, sms_packet, write_note  # noqa: E402

pytestmark = pytest.mark.rig

ROOM = "D"                      # a module of both peer rows (dp15-twonode-a.toml / -b.toml: DE)
ROOM_LABEL = ROOM.lower()       # the room label of the hub metrics
SENDER_CALL = "N0CALL"          # M17 sender on node 1 (a direct client: its SMS is a class 0 text)
TALKER_CALL = "N0TEST"          # D-STAR local talker on node 2
LISTENER_CALL = "N0CALL"        # D-STAR listener on node 2
M17_LISTENER_CALL = "N0TEST"    # M17 listener on node 2
REFLECTOR_1 = "XLX999"          # MY of the text over: the reflector callsign of node 1
LOG = "dp15-peer.txt"
SETTING = "twonode"
OVER_WAIT_S = 20.0              # the text over reaches node 2 within this of the SMS (slot, queue, 440 ms)
COUNTER_WAIT_S = 45.0           # a counter shows up within this (hub refresh 10 s, recorder store, controller clock)
QUIET_S = ds.QUIET_S            # a baseline is quiet when nothing changed for one refresh and a bit
LINK_WAIT_S = 60.0
TALKER_FRAMES = 12              # the local talker that keys inside the over: 240 ms, short on purpose (it is denied)
TALKER_FRAMES_LONG = 60         # the one that keys after the over: 1.2 s (> 50 frames: the short stream guard stays out)
M17_FRAMES_MIN, M17_FRAMES_MAX = 8, 14      # 440 ms of 40 ms M17 stream packets is 11 (+ the last one)
GRANT_SLACK_S = 0.5             # between the end of the over at the listener and the second talker


# ---------------------------------------------------------------- pure helpers (cases/unit/test_dp15_peer_config.py)

def counts(text, room=ROOM_LABEL):
    """The numbers one hub read gives (a dict of floats, plus 'has.recorder' / 'has.data' = 1.0 when the family is there).
    Controller counters are per room (label room, lower case); everything else is node wide."""
    samples = metrics.parse(text)
    families = metrics.families(text)
    out = {"has.recorder": 1.0 if "dv_recorder_records_total" in families else 0.0,
           "has.data": 1.0 if "dv_room_data_total" in families else 0.0}
    for result in ("grant", "deny", "busy_delayed", "duplicate"):
        out["rts." + result] = metrics.total(samples, "dv_room_rts_total", room=room, result=result)
    out["closed"] = metrics.total(samples, "dv_room_streams_closed_total", room=room)
    out["units.published"] = metrics.total(samples, "dv_room_units_total", room=room, kind="published")
    out["rec.records"] = metrics.total(samples, "dv_recorder_records_total")
    out["rec.bytes"] = metrics.total(samples, "dv_recorder_bytes_total")
    out["rec.dropped"] = metrics.total(samples, "dv_recorder_dropped_total")
    out["data.all"] = metrics.total(samples, "dv_room_data_total")
    out["items.all"] = metrics.total(samples, "dv_pkt_items_total")
    out["raw.sent"] = metrics.total(samples, "dv_edge_raw_total", result="sent")
    out["textover.sent"] = metrics.total(samples, "dv_edge_text_overs_total", result="sent")
    out["textover.all"] = metrics.total(samples, "dv_edge_text_overs_total")
    out["xlx.peers"] = metrics.total(samples, "dv_edge_sessions", kind="peer", proc="xlx")
    return out


def delta(after, before):
    """after - before for every numeric counter key (the 'has.*' flags and the gauge 'xlx.peers' are not counters)."""
    return {k: after[k] - before[k] for k in after if not k.startswith("has.") and k != "xlx.peers" and k in before}


def moved(delta_map):
    """The non-zero entries, for printing."""
    return {k: int(v) if float(v).is_integer() else v for k, v in sorted(delta_map.items()) if v}


def own_over_problems(d1, recorder=True):
    """What node 1 (the sending node) did for its OWN synthetic text over alone, measured before anything else keys: the
    design (data.md 7.6) is the local exclusion, so no floor, no closed stream, no published unit and no record. A list of
    sentences, empty when the exclusion holds. `recorder` is False when the hub has no recorder family."""
    out = []
    for key in ("rts.grant", "closed", "units.published"):
        if d1.get(key, 0):
            out.append("%s moved by %s" % (key, d1[key]))
    if recorder and (d1.get("rec.records", 0) or d1.get("rec.bytes", 0)):
        out.append("the recorder stored the own text over (records %s, bytes %s)" % (d1.get("rec.records", 0), d1.get("rec.bytes", 0)))
    return out


def relayed_stream_problems(d1, frames):
    """What node 1 shows for the stream of the SECOND talker (a local D-STAR talker on node 2, relayed over the XLX peer
    link to node 1 as ordinary voice from a peer), as the delta since the snapshot taken after the text over: exactly one
    grant, one closed stream, and about `frames` published units (header + voice frames; a frame or two of slack for the
    last frame and the stream guard). A list of sentences, empty when it matches."""
    out = []
    if d1.get("rts.grant", 0) != 1:
        out.append("grants %s, want 1" % d1.get("rts.grant", 0))
    if d1.get("closed", 0) != 1:
        out.append("closed streams %s, want 1" % d1.get("closed", 0))
    units = d1.get("units.published", 0)
    if not frames - 3 <= units <= frames + 2:
        out.append("published units %s, want about %s" % (units, frames))
    return out


def m17_streams(datagrams):
    """[{stream_id, src, dst, frames, last, first_fn}] of the M17 stream packets (54 B) among `datagrams`, in order of first
    appearance; 'frames' counts the packets of the stream, 'last' is True when one carried the end flag."""
    order, by_id = [], {}
    for data in datagrams:
        parsed = m17.parse_stream(data)
        if not parsed:
            continue
        sid = parsed["stream_id"]
        if sid not in by_id:
            by_id[sid] = {"stream_id": sid, "src": parsed["src"], "dst": parsed["dst"], "frames": 0, "last": False,
                          "first_fn": parsed["fn"]}
            order.append(sid)
        by_id[sid]["frames"] += 1
        by_id[sid]["last"] = by_id[sid]["last"] or parsed["last"]
    return [by_id[sid] for sid in order]


def header_text(header):
    """(my, ur, suffix, rpt1, rpt2) of a parsed DSVT header as str, for the notes."""
    def f(key):
        return bytes(header.get(key, b"")).decode("ascii", "replace")
    return f("my"), f("ur"), f("suffix"), f("rpt1"), f("rpt2")


def file_count(directory):
    """Number of regular files under `directory` (0 when it does not exist)."""
    root = Path(directory)
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0


def settle(read, quiet=QUIET_S, limit=90.0, poll=2.0, clock=time.monotonic, sleep=time.sleep):
    """read() after it did not change for `quiet` seconds (a refresh of an earlier case has landed)."""
    last, changed, end = read(), clock(), clock() + limit
    while clock() - changed < quiet and clock() < end:
        sleep(poll)
        now = read()
        if now != last:
            last, changed = now, clock()
    return last


def wait_for(read, pred, timeout=COUNTER_WAIT_S, poll=2.0, clock=time.monotonic, sleep=time.sleep):
    """(value, True) once pred(read()) holds, else (last value, False) after `timeout` seconds."""
    end = clock() + timeout
    while True:
        value = read()
        if pred(value):
            return value, True
        if clock() >= end:
            return value, False
        sleep(poll)


# ---------------------------------------------------------------- notes, fixtures

def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def nodes(rig, target, rig_control):
    """Both nodes: their addresses and hubs. Skips with the reason when the second node is not part of the rig."""
    if target.sut_profile != "dvrouter":
        pytest.skip("DV-Router only: DVT_SUT_PROFILE=dvrouter (profile %s)" % target.sut_profile)
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_TWONODE") != "on":
        pytest.skip("the second DV-Router node is not part of this rig: run scripts/run.sh --profile twonode (DVT_TWONODE is not 'on')")
    ip2 = os.environ.get("DVT_DVR_PEER", "")
    if not ip2:
        pytest.skip("DVT_DVR_PEER (the address of dvr-peer) is not set")
    hub1, hub2 = Hub(rig_control), Hub(rig_control, "dvr-peer")
    end = time.monotonic() + ds.FAMILY_WAIT_S
    while True:
        try:
            have = [metrics.families(h.text()) for h in (hub1, hub2)]
        except RuntimeError as exc:
            pytest.skip("the hub metrics are not reachable through the control channel (%s)" % exc)
        if all("dv_room_rts_total" in fams for fams in have) or time.monotonic() >= end:
            break
        time.sleep(3.0)
    if not all("dv_room_rts_total" in fams for fams in have):
        pytest.skip("no dv_room_rts_total family in the hub of node %s after %.0f s" % (
            " and ".join(str(i + 1) for i, fams in enumerate(have) if "dv_room_rts_total" not in fams), ds.FAMILY_WAIT_S))
    # the XLX link: one peer circuit on each edge. The gauge is read with the hub clock (10 s), so it is only a
    # precondition to wait for, not an assertion: the cases assert on the over itself
    link = False
    end = time.monotonic() + LINK_WAIT_S
    while time.monotonic() < end:
        link = all(counts(h.text())["xlx.peers"] >= 1 for h in (hub1, hub2))
        if link:
            break
        time.sleep(3.0)
    note(target, "nodes", "RECORD", "node 1 %s, node 2 %s, xlx peer circuit on both hubs: %s; recorder families: node 1 %s, node 2 %s" % (
        rig.xlxd_sut, ip2, link, bool(counts(hub1.text())["has.recorder"]), bool(counts(hub2.text())["has.recorder"])))
    return types.SimpleNamespace(ip1=rig.xlxd_sut, ip2=ip2, hub1=hub1, hub2=hub2, link=link, out=Path(target.out))


def read_counts(hub):
    return lambda: counts(hub.text())


def quiet_baselines(nodes):
    """Settled counts of both hubs (two reads, one per node, each quiet for QUIET_S)."""
    return settle(read_counts(nodes.hub1)), settle(read_counts(nodes.hub2))


def send_sms(nodes, body, sender):
    """Offer one M17 SMS to module D of node 1 from the linked M17 client `sender`; the time it was sent."""
    packet = sms_packet(body, SENDER_CALL)
    sender.client.send(sender.dest, packet)
    PACER.offered(SENDER_CALL, "text")
    return time.monotonic()


def wait_xlx_header(listener, seconds):
    """Arrival time of the first header with MY = the reflector callsign of node 1 in the listener's inbox, polling every
    2 ms (the local talker of 7b must key inside the 440 ms), else None."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        listener.client.drain()
        got = ds.first_header_times(listener, lambda h: bytes(h["my"]).rstrip() == REFLECTOR_1.encode())
        if got:
            return got[0][0]
        time.sleep(0.002)
    return None


def text_over_of(listener, body):
    """(over dict, decoded text dict) of the whole text over carrying `body` in the listener's inbox, else (None, None)."""
    for over in slowdata.overs(listener.client.inbox):
        got = slowdata.over_text(over)
        if got and got["text"].rstrip().endswith(body):
            return over, got
    return None, None


# ================================================================ 7b: the remote floor

def test_dp15_7b_remote_floor_of_the_text_over(nodes, target):
    """data.md 7.6 / 15.5 (XLX peer): the receiving node sees the synthetic text over as ordinary class 1 voice from the
    reflector callsign: it takes the floor of the module for about 440 ms. A local D-STAR talker on node 2 that keys right
    after the header of the over arrived is denied (busy: `dv_room_rts_total{result=deny}` or `busy_delayed`, the over is
    dropped silently), the listener of the module hears only the text over from XLX999 and not the talker. A talker that
    keys after the end of the over gets the floor (a grant) and the listener hears all of it. The grants of node 2's room
    D are exactly two (the text over and the second talker). Not observed: the room's on-air state as remote (needs the
    web or status feed of node 2)."""
    case = "7b"
    body = ds.nonce_body("RF")
    base1, base2 = quiet_baselines(nodes)                  # before the links: a DExtra client times out in about 30 s of silence
    PACER.wait(SENDER_CALL, "text")
    with ds.dstar_listener(nodes.ip2, module=ROOM, callsign=LISTENER_CALL) as listener, \
            ds.dstar_listener(nodes.ip2, module=ROOM, callsign=TALKER_CALL) as talker, \
            ds.m17_client(nodes.ip1, callsign=SENDER_CALL, module=ROOM) as sender:
        t_sms = send_sms(nodes, body, sender)
        t_hdr = wait_xlx_header(talker, OVER_WAIT_S)
        assert t_hdr is not None, "no header of %s reached the talker's link on node 2 within %.0f s of the SMS" % (REFLECTOR_1, OVER_WAIT_S)
        # key at once: header, then the 12 frames at 20 ms, no settle (the RTS must land inside the 440 ms)
        _sid1, packets1 = build_over(TALKER_CALL, plain_fields(TALKER_FRAMES), ambe(TALKER_FRAMES, "7b1"), module=ROOM)
        t_talk = time.monotonic()
        talker.client.send(talker.dest, packets1[0])
        talker.client.paced(talker.dest, packets1[1:], 0.020, listener.client)
        # the rest of the text over, until its last frame reached the listener
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            ds.pump_for(0.05, listener, talker)
            if text_over_of(listener, body)[0] is not None:
                break
        t_over_end = time.monotonic()
        overs_first = slowdata.overs(listener.client.inbox)
        # let the denied stream settle, then read node 1 BEFORE the second talker keys: its counters so far are those of the
        # text over alone (the design: no floor on the sending node). The links keep being pumped while the hub settles
        # (a DExtra client times out in about 30 s of silence)
        ds.pump_for(GRANT_SLACK_S + 2.0, listener, talker)
        mid1 = settle(read_counts(nodes.hub1), sleep=lambda seconds: ds.pump_for(seconds, listener, talker))
        sid2, packets2 = build_over(TALKER_CALL, plain_fields(TALKER_FRAMES_LONG), ambe(TALKER_FRAMES_LONG, "7b2"), module=ROOM)
        t_talk2 = time.monotonic()
        stamps2 = ds.send_over(talker, packets2)[1]
        ds.pump_for(1.5, listener, talker)
        overs_all = slowdata.overs(listener.client.inbox)
    _after, ok = wait_for(read_counts(nodes.hub2), lambda c: c["rts.grant"] - base2["rts.grant"] >= 2
                          and c["rts.deny"] + c["rts.busy_delayed"] - base2["rts.deny"] - base2["rts.busy_delayed"] >= 1)
    _after1, ok1 = wait_for(read_counts(nodes.hub1), lambda c: c["rts.grant"] - mid1["rts.grant"] >= 1)
    final1, final2 = settle(read_counts(nodes.hub1)), settle(read_counts(nodes.hub2))
    d1, d2 = delta(final1, base1), delta(final2, base2)
    d1_over, d1_second = delta(mid1, base1), delta(final1, mid1)
    note(target, case, "RECORD", "SMS sent; header of %s at the talker %.3f s later; talker keyed %.3f s after it; the over end at the "
         "listener %.3f s after the SMS; second talker %.3f s after the first key" % (
             REFLECTOR_1, t_hdr - t_sms, t_talk - t_hdr, t_over_end - t_sms, t_talk2 - t_talk))
    note(target, case, "RECORD", "node 2 hub delta %s; node 1 hub delta %s (the text over alone %s, then the relayed second talker %s)" % (
        moved(d2), moved(d1), moved(d1_over), moved(d1_second)))
    texts = ds.texts_of(overs_first)
    assert any(x["text"].rstrip().endswith(body) and x["text"].startswith(SENDER_CALL + ": ") for _s, x in texts), \
        "the listener on node 2 got no whole text over %r...%r: %s" % (SENDER_CALL + ": ", body, [x["text"] for _s, x in texts])
    heard_first = ds.over_by_my(overs_first, TALKER_CALL)
    assert heard_first is None, "the listener heard the talker that keyed inside the text over (%d frames): the floor was not held" % (
        heard_first["frames"])
    assert ok, "node 2 room %s counters after %.0f s: %s (want 2 grants and a deny/busy_delayed)" % (ROOM, COUNTER_WAIT_S, moved(d2))
    assert d2["rts.grant"] == 2, "grants of room %s on node 2: %s, want 2 (the text over and the second talker): %s" % (
        ROOM, d2["rts.grant"], moved(d2))
    assert d2["rts.deny"] + d2["rts.busy_delayed"] >= 1, "no deny / busy_delayed of room %s on node 2 for the talker inside the over: %s" % (
        ROOM, moved(d2))
    second = [ov for ov in overs_all if ov["stream_id"] == sid2]
    assert second, "the listener did not hear the talker that keyed after the text over (floor not released): %s" % [
        ds.describe(ov) for ov in overs_all]
    heard = second[0]["frames"]
    note(target, case, "RECORD", "the listener heard the second talker: %d of %d frames; the first talker's over was not heard" % (
        heard, len(packets2) - 1))
    assert heard >= TALKER_FRAMES_LONG - 3, "the second talker's over reached the listener with %d of %d frames" % (heard, TALKER_FRAMES_LONG + 1)
    assert stamps2, "the second talker sent nothing"
    # the sending node took no floor for its own text over (read before the second talker keyed) ...
    problems = own_over_problems(d1_over, recorder=bool(mid1["has.recorder"]))
    assert not problems, "node 1 room %s took part in its own text over (before the second talker keyed): %s; delta %s" % (
        ROOM, "; ".join(problems), moved(d1_over))
    # ... and then saw exactly the relayed stream of the second talker, as ordinary voice from the XLX peer
    assert ok1, "node 1 room %s counted no grant for the relayed stream of the second talker within %.0f s: %s" % (
        ROOM, COUNTER_WAIT_S, moved(d1_second))
    problems = relayed_stream_problems(d1_second, len(packets2))
    assert not problems, "node 1 room %s, relayed stream of the second talker: %s; delta %s" % (ROOM, "; ".join(problems), moved(d1_second))


# ================================================================ 7c: recording

def test_dp15_7c_the_receiving_node_records_the_text_over(nodes, target):
    """data.md 7.6 (XLX peer): "can be recorded there ... the edge-local fabric and recorder exclusion applies only on the
    sending node". After one text over from node 1: node 2 (recorder on, archive out/<run>/recorder-peer) counts one more
    stored record (`dv_recorder_records_total` and `_bytes_total`; `_dropped_total` does not move) and its room D granted
    and closed one stream; node 1 (recorder on too) stores nothing, publishes no unit and takes no floor for its own text
    over (no grant, no closed stream). The archive directory of node 2 holds more files than before (recorded only)."""
    case = "7c"
    body = ds.nonce_body("RC")
    archive = nodes.out / "recorder-peer"
    base1, base2 = quiet_baselines(nodes)
    PACER.wait(SENDER_CALL, "text")
    with ds.dstar_listener(nodes.ip2, module=ROOM, callsign=LISTENER_CALL) as listener, \
            ds.m17_client(nodes.ip1, callsign=SENDER_CALL, module=ROOM) as sender:
        files_before = file_count(archive)
        t_sms = send_sms(nodes, body, sender)
        got = ds.wait_for_overs(listener, OVER_WAIT_S, want=ds.has_text_with(body))
        t_got = time.monotonic()
    texts = ds.texts_of(got)
    assert any(x["text"].rstrip().endswith(body) for _s, x in texts), "node 2 got no whole text over with %r: %s" % (
        body, [x["text"] for _s, x in texts])
    _after2, ok2 = wait_for(read_counts(nodes.hub2), lambda c: c["rec.records"] > base2["rec.records"])
    final1, final2 = settle(read_counts(nodes.hub1)), settle(read_counts(nodes.hub2))
    d1, d2 = delta(final1, base1), delta(final2, base2)
    files_after = file_count(archive)
    note(target, case, "RECORD", "over reached node 2 %.3f s after the SMS; node 2 hub delta %s; node 1 hub delta %s; archive files %d -> %d" % (
        t_got - t_sms, moved(d2), moved(d1), files_before, files_after))
    assert final2["has.recorder"] == 1.0, "node 2 hub has no dv_recorder_records_total: the recorder is not running (dp15-twonode-b.toml, out/<run>/recorder-peer)"
    assert ok2 and d2["rec.records"] >= 1, "node 2 stored no record of the text over within %.0f s: %s" % (COUNTER_WAIT_S, moved(d2))
    assert d2["rec.bytes"] > 0, "node 2 recorder counted a record without bytes: %s" % moved(d2)
    assert d2["rec.dropped"] == 0, "node 2 recorder dropped something while recording the text over: %s" % moved(d2)
    assert d2["rts.grant"] == 1 and d2["closed"] >= 1, "node 2 room %s: want one grant and one closed stream for the remote over: %s" % (ROOM, moved(d2))
    # the sending node: local exclusion (no ingress, no media, no recorder, no floor)
    assert d1["rts.grant"] == 0 and d1["closed"] == 0 and d1["units.published"] == 0, \
        "node 1 room %s took the floor or published units for its own synthetic text over: %s" % (ROOM, moved(d1))
    assert d1["textover.sent"] >= 1, "node 1 counted no text over sent (dv_edge_text_overs_total{result=sent}): %s" % moved(d1)
    if final1["has.recorder"] == 1.0:
        assert d1["rec.records"] == 0 and d1["rec.bytes"] == 0, "node 1 recorded its own synthetic text over: %s" % moved(d1)
    else:
        note(target, case, "RECORD", "node 1 has no recorder family: the sending side of the recorder is not asserted here")
    if files_after <= files_before:
        note(target, case, "RECORD", "the archive directory of node 2 holds no new file (%d -> %d): the store may be indexed elsewhere" % (files_before, files_after))


# ================================================================ 7d: cross-mode effects

def test_dp15_7d_cross_mode_effects_on_the_receiving_node(nodes, target):
    """data.md 7.6 / 15.5 (XLX peer): the text over also reaches the other protocols of node 2. The D-STAR (DExtra)
    listener gets a whole text over: header MY `XLX999`, 21 frames of null AMBE with the text blocks `N0CALL: <body>`, the
    last frame (header UR / suffix / RPT are recorded; the design says UR `ALL`, suffix `M17 ` for an M17 origin). The M17
    listener gets one voice stream of about 440 ms (8 to 14 stream packets; carried or transcoded silence, SRC recorded).
    Node 2 derives no data item from it (the re-injection guard, data.md 686): `dv_room_data_total` and
    `dv_pkt_items_total` do not move, node 2's edges render no text over of their own. Not observed: DMR, web listeners."""
    case = "7d"
    body = ds.nonce_body("XM")
    _base1, base2 = quiet_baselines(nodes)
    PACER.wait(SENDER_CALL, "text")
    with ds.dstar_listener(nodes.ip2, module=ROOM, callsign=LISTENER_CALL) as dlist, \
            ds.m17_client(nodes.ip2, callsign=M17_LISTENER_CALL, module=ROOM) as mlist, \
            ds.m17_client(nodes.ip1, callsign=SENDER_CALL, module=ROOM) as sender:
        send_sms(nodes, body, sender)
        end = time.monotonic() + OVER_WAIT_S
        while time.monotonic() < end:
            ds.pump_for(0.05, dlist, mlist, sender)
            if text_over_of(dlist, body)[0] is not None:
                break
        ds.pump_for(1.5, dlist, mlist, sender)                      # the M17 side lags: stragglers and the end packet
        overs = slowdata.overs(dlist.client.inbox)
        streams = m17_streams(mlist.client.inbox)
    final2 = settle(read_counts(nodes.hub2))
    d2 = delta(final2, base2)
    over = ds.over_by_my(overs, REFLECTOR_1)
    note(target, case, "RECORD", "D-STAR listener overs %s; M17 listener streams %s; node 2 hub delta %s" % (
        [ds.describe(o) for o in overs], streams, moved(d2)))
    assert over is not None, "the D-STAR listener on node 2 saw no over with MY %s: %s" % (REFLECTOR_1, [ds.describe(o) for o in overs])
    text = slowdata.over_text(over)
    assert text is not None, "the over of %s is not a whole text over (frames %d)" % (REFLECTOR_1, over["frames"])
    assert text["text"].startswith(SENDER_CALL + ": ") and text["text"].rstrip().endswith(body), "text over reads %r" % text["text"]
    assert all(v["ambe"] == dextra.NULL_AMBE for v in over["voice"]), "the voice frames are not null AMBE on node 2's DExtra egress"
    my, ur, suffix, rpt1, rpt2 = header_text(over["header"])
    note(target, case, "RECORD", "D-STAR header on node 2: MY %r UR %r suffix %r RPT1 %r RPT2 %r" % (my, ur, suffix, rpt1, rpt2))
    if suffix != "M17 ":
        note(target, case, "FINDING", "text over header suffix on node 2 is %r, the design has `M17 ` for an M17 origin (data.md 7.6)" % suffix)
    assert streams, "the M17 listener on node 2 got no stream packet (a carried or transcoded stream of the text over)"
    stream = streams[0]
    note(target, case, "RECORD", "M17 stream on node 2: SRC %r DST %r, %d packets, last flag %s" % (
        stream["src"], stream["dst"], stream["frames"], stream["last"]))
    assert M17_FRAMES_MIN <= stream["frames"] <= M17_FRAMES_MAX, \
        "M17 stream of %d packets, want %d to %d (about 440 ms of 40 ms packets)" % (stream["frames"], M17_FRAMES_MIN, M17_FRAMES_MAX)
    if stream["src"].rstrip() != REFLECTOR_1:
        note(target, case, "FINDING", "the M17 stream names SRC %r, the over's talker is the reflector callsign %s" % (stream["src"], REFLECTOR_1))
    # the re-injection guard: nothing derived from the over on the receiving node
    if final2["has.data"] != 1.0:
        note(target, case, "RECORD", "node 2 hub has no dv_room_data_total family: the guard check is weak (items.all %s)" % d2.get("items.all"))
    assert d2["data.all"] == 0 and d2["items.all"] == 0, "node 2 derived a data item from the text over (re-injection guard): %s" % moved(d2)
    assert d2["textover.all"] == 0, "node 2 rendered a text over of its own: %s" % moved(d2)
    note(target, case, "RECORD", "node 2 raw units sent %s (informational: a stream of the reflector callsign / M17 suffix is not turned into a status)" % d2["raw.sent"])
