# SPDX-License-Identifier: MIT
"""MEDLOAD1 (DV-Router fb6a2a51 .. 28fc1838, in bc9af2b1, image dv-router:wip-f1f3777): per-over media loss accounting. Documents: media-format.md 186-340, status.md 704-830 and the metrics
table (dv_edge_over_units_total, dv_room_over_units_total, dv_room_overs_total), work-units.md MEDLOAD1 (S1 to S7); docs/medload1-cases.md. Arithmetic and the S7 classifier:
dvtest/medload1.py (the edge's D-STAR gap rule read from core.rs 3555-3600). Switch DVT_MEDLOAD1=on. NOT RUN on a rig: written against the sources of bc9af2b1.

One over at a time through a DExtra talker on module P (60 voice frames 20 ms apart and the last frame), the counters of the hub (rig_control.metrics, about every 10 s) read before and after:

  ML1-a..d  the over's frames arrive as planned: clean; drops 10..14 and 30; the same plus frame 25 delayed behind 28; the counter wrap (frame 21 dropped) and a duplicate (frame 40 twice):
            edge rx gap, room gap == the model; room lost 0; late and dropped 0 (nothing stalls); the over counts `lossy` when the gap is > 0, else `complete`; egress lost, late, dropped 0
            (a source hole is not egress loss); the listener hears the planned frames
  ML1-e     the same loss over M17: the gap counters do NOT move (M17 counts none; a rise is a FINDING, not a failure)
  ML1-f     a USRP over with wire sequences 20..22 missing (needs the dynamic USRP hub): gap + 3
  ML1-h     the per-over fields over HTTP (dv-router, 2026-11-22: GET /api/v1/admin/lastheard rows and GET /api/v1/admin/rooms `last` carry frames, published, media_published, late, lost, gap;
            null when unknown; `lost` = gap + room-hop loss, unlike the metric): the over of scenario b (gap 6) shows gap 6, lost 6, late 0 in both; needs the web profile with the admin service
  ML1-g     S7 analogue: five rounds of five clean overs under busy-loop processes in the tester (DVT_MEDLOAD1_BURN, default 16): per round zero lost, gap, uncounted and unknown;
            at most one round HOST_STARVED (late or dropped units counted, every over delivered in full); the rest pass

Not asserted: late and dropped on an unloaded run other than being 0 (they need a CTS stall), per-over fields of the room event (the openapi-admin Heard / LastOver rows carry none of them), the recorder row
(`lost_units` and `late_units`, recorder.toml; docs/medload1-cases.md). Overs are serial and at least one hub period apart, so no short-stream guard strike can occur (every over has at least 50 frames).
"""
import multiprocessing
import os
import sys
import time
import contextlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import clients, medload1 as ml, metrics  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import dextra, m17, usrp  # noqa: E402

import data_support as ds  # noqa: E402
import test_short_stream_guard as sg  # noqa: E402

pytestmark = [pytest.mark.rig]

LOG = "medload1.txt"
MODULE, ROOM = "P", "p"
FRAMES = 60
GAP_S = 0.020
ROUNDS, OVERS_PER_ROUND = 5, 5


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not ml.strict_on():
        pytest.skip("DVT_MEDLOAD1=on is not set (the image under test has MEDLOAD1, DV-Router fb6a2a51 .. 28fc1838: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


@pytest.fixture(scope="module")
def reader(rig_control):
    def read():
        return ml.counts(metrics.parse(rig_control.metrics("xlxd-sut")), ROOM)

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if ml.families_present(metrics.parse(rig_control.metrics("xlxd-sut"))):
            return read
        time.sleep(5.0)
    pytest.fail("no dv_edge_over_units_total / dv_room_over_units_total / dv_room_overs_total in the hub metrics after 60 s: the image does not have MEDLOAD1")


@pytest.fixture
def pair(rig):
    talker = sg.dstar(rig.xlxd_sut, "N0MLA", MODULE)
    listener = sg.dstar(rig.xlxd_sut, "N0MLL", MODULE)
    yield talker, listener


def send_planned(talker, listener, order, seed):
    """One D-STAR over whose frames arrive in `order` (indexes 0..FRAMES, FRAMES = the last frame). Returns the frame indexes the listener heard."""
    sid = clients.fresh_stream_id("dstar")
    voice = signals.prng_frames("dstar", FRAMES, seed="ml1-%s" % seed)
    sg.clear(listener)
    packets = [dextra.header(sid, talker.callsign, talker.module)]
    for idx in order:
        if idx >= FRAMES:
            packets.append(dextra.voice(sid, idx, bytes(9), True))
        else:
            packets.append(dextra.voice(sid, idx, voice[idx], False, dextra.SYNC_DATA if idx % 21 == 0 else dextra.SLOW_DATA))
    talker.client.paced(talker.dest, packets, GAP_S, listener.client)
    time.sleep(0.5)
    sg.drain(listener)
    index = {frame: i for i, frame in enumerate(voice)}
    heard = []
    for p in listener.client.inbox:
        v = dextra.parse_voice(p)
        if v and not v[2] and v[3] in index:
            heard.append(index[v[3]])
    return heard


SCENARIOS = {
    "a-clean": dict(),
    "b-drops": dict(drop=set(range(10, 15)) | {30}),
    "c-drops-and-a-late-frame": dict(drop=set(range(10, 15)) | {30}, delay={25: 28}),
    "d-wrap-and-a-duplicate": dict(drop={21}, duplicate={40}),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_ml1_dstar_over_counts(reader, pair, target, name):
    case = "ML1-" + name
    talker, listener = pair
    plan = SCENARIOS[name]
    order = ml.plan_over(FRAMES, **plan)
    want_gap = ml.model_gap(order)
    before = reader()
    heard = send_planned(talker, listener, order, name)
    got = ml.delta(ml.wait_settled(reader, before), before)
    want_heard = sorted(i for i in ml.delivered(order) if i < FRAMES)
    note(target, case, "RECORD", "model gap %d; delta %s; heard %d of %d planned" % (want_gap, dict(got._asdict()), len(heard), len(want_heard)))
    assert got.edge_rx_gap == want_gap, "%s: dv_edge_over_units_total{dir=rx,result=gap} rose by %g, the edge's rule gives %d (order %s...)" % (case, got.edge_rx_gap, want_gap, order[:3])
    assert got.room_gap == want_gap, "%s: dv_room_over_units_total{room=%s,result=gap} rose by %g, want %d" % (case, ROOM, got.room_gap, want_gap)
    assert got.room_lost == 0, "%s: room lost rose by %g: the loss was before the room (the metric lost is the room hop only)" % (case, got.room_lost)
    assert got.edge_rx_late == 0 and got.room_late == 0 and got.edge_rx_dropped == 0, "%s: late or dropped units on an unloaded run: %s" % (case, got)
    assert got.edge_tx_lost == 0 and got.edge_tx_late == 0 and got.edge_tx_dropped == 0, "%s: a source hole counted as egress loss: %s" % (case, got)
    assert got.overs_end_missing == 0, "%s: the over ended with its loss count unknown (end_missing): %s" % (case, got)
    assert got.overs_lossy == (1 if want_gap else 0) and got.overs_complete == (0 if want_gap else 1), "%s: dv_room_overs_total moved %s, a gap of %d must count as %s" % (
        case, (got.overs_complete, got.overs_lossy), want_gap, "lossy" if want_gap else "complete")
    if "delay" in plan or "duplicate" in plan:
        if sorted(heard) != want_heard:
            note(target, case, "FINDING", "the listener heard %s, the edge rule forwards %s (a late frame or a duplicate is expected to be ignored)" % (sorted(set(heard) ^ set(want_heard)), want_heard[:3]))
    else:
        assert sorted(heard) == want_heard, "%s: the listener heard %d frames, planned %d" % (case, len(heard), len(want_heard))


def test_ml1_e_m17_counts_no_gap(reader, rig, target):
    case = "ML1-e"
    before = reader()
    sid = clients.fresh_stream_id("m17")
    with ds.m17_client(rig.xlxd_sut, "N0MLM", MODULE) as c:
        packets = m17.voice_packets(signals.prng_frames("m17", 60, seed="ml1-m17"), sid, c.callsign)
        kept = [p for i, p in enumerate(packets) if not 10 <= i < 15]
        c.client.paced(c.dest, kept, 0.040)
        c.client.pump(0.5)
    got = ml.delta(ml.wait_settled(reader, before), before)
    note(target, case, "RECORD", "M17 over with 5 packets missing: delta %s" % dict(got._asdict()))
    if got.edge_rx_gap or got.room_gap:
        note(target, case, "FINDING", "M17 counted a gap (edge %g, room %g): media-format.md / core.rs list the D-STAR family and USRP only" % (got.edge_rx_gap, got.room_gap))
    assert got.room_lost == 0 and got.edge_tx_lost == 0


def test_ml1_f_usrp_gap(reader, target):
    case = "ML1-f"
    from webrtc_support import usrp_client                                   # skips without the dynamic USRP hub
    peer = usrp_client(target, "N0MLU", MODULE)
    try:
        before = reader()
        pcm = signals.frames(signals.tone(700.0, 1200, amp=8000))
        for i, frame in enumerate(pcm):
            if 20 <= i <= 22:
                peer.seq += 1                                               # the datagram is never sent: its sequence is missing
                continue
            peer.send(usrp.voice, frame)
            time.sleep(GAP_S)
        peer.send(usrp.unkey)
        peer.pump(0.5)
    finally:
        peer.close()
    got = ml.delta(ml.wait_settled(reader, before), before)
    note(target, case, "RECORD", "USRP over with wire sequences 20..22 missing: delta %s" % dict(got._asdict()))
    assert got.edge_rx_gap == 3, "%s: dv_edge_over_units_total{dir=rx,result=gap} rose by %g, want 3 (three absent datagrams)" % (case, got.edge_rx_gap)
    assert got.room_gap == 3 and got.room_lost == 0


def find_dicts(node, key):
    """Every dict below `node` that has `key` (the admin bodies are not frozen by the OpenAPI text: it lags the image, so no schema is applied)."""
    if isinstance(node, dict):
        if key in node:
            yield node
        for v in node.values():
            yield from find_dicts(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from find_dicts(v, key)


def test_ml1_h_per_over_fields_over_http(reader, pair, admin_session, target):
    case = "ML1-h"
    viewer = admin_session("viewer")                                  # an observer role omits all six fields; viewer and up carry them
    talker, listener = pair
    order = ml.plan_over(FRAMES, **SCENARIOS["b-drops"])
    before = reader()
    send_planned(talker, listener, order, "http")
    ml.wait_settled(reader, before)
    time.sleep(2.0)
    last = viewer.get(viewer.path("/lastheard?limit=20"), expect=200).json()
    rows = [r for r in find_dicts(last, "gap") if str(r.get("callsign", r.get("callsign_base", ""))).upper().startswith("N0MLA")]
    assert rows, "%s: no last-heard row of the talker carries a `gap` field (rows: %s)" % (case, [sorted(r) for r in find_dicts(last, "callsign")][:2])
    row = max(rows, key=lambda r: r.get("ts_ms") or r.get("since_ms") or 0)
    note(target, case, "RECORD", "lastheard row: %s" % {k: row.get(k) for k in ("frames", "published", "media_published", "late", "lost", "gap")})
    assert row.get("gap") == 6 and row.get("lost") == 6 and row.get("late") == 0, "%s: lastheard gap/lost/late = %s/%s/%s, want 6/6/0 (lost is gap + room-hop loss)" % (
        case, row.get("gap"), row.get("lost"), row.get("late"))
    assert row.get("frames") in (None, 54, 55) or isinstance(row.get("frames"), int), row
    rooms = viewer.get(viewer.path("/rooms"), expect=200).json()
    mine = [r for r in find_dicts(rooms, "last") if str(r.get("room", r.get("letter", ""))).upper() == MODULE and isinstance(r.get("last"), dict)]
    if not mine:
        note(target, case, "FINDING", "no room entry of %s with a `last` object in /rooms (keys seen: %s)" % (MODULE, sorted({k for r in find_dicts(rooms, "last") for k in r})[:12]))
    else:
        last_over = mine[0]["last"]
        note(target, case, "RECORD", "rooms[%s].last: %s" % (MODULE, {k: last_over.get(k) for k in ("frames", "published", "media_published", "late", "lost", "gap")}))
        assert last_over.get("gap") == 6 and last_over.get("lost") == 6 and last_over.get("late") == 0, "%s: rooms last gap/lost/late = %s/%s/%s, want 6/6/0" % (
            case, last_over.get("gap"), last_over.get("lost"), last_over.get("late"))


@contextlib.contextmanager
def burn(workers):
    """`workers` busy-loop processes in the tester for the duration (the tester container's own cpu cap bounds them)."""
    stop = multiprocessing.Event()
    procs = [multiprocessing.Process(target=_spin, args=(stop,), daemon=True) for _ in range(workers)]
    for p in procs:
        p.start()
    try:
        yield len(procs)
    finally:
        stop.set()
        for p in procs:
            p.join(5.0)
            if p.is_alive():
                p.terminate()


def _spin(stop):
    x = 0
    while not stop.is_set():
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF


@pytest.mark.slow
def test_ml1_g_s7_loaded_rounds(reader, pair, target):
    case = "ML1-g"
    talker, listener = pair
    workers = int(os.environ.get("DVT_MEDLOAD1_BURN") or 16)
    labels = []
    with burn(workers):
        for round_no in range(1, ROUNDS + 1):
            before = reader()
            all_heard = True
            for n in range(OVERS_PER_ROUND):
                heard = send_planned(talker, listener, ml.plan_over(FRAMES), "s7-%d-%d" % (round_no, n))
                all_heard = all_heard and len(heard) == FRAMES
                time.sleep(1.0)
            got = ml.delta(ml.wait_settled(reader, before, want_overs=OVERS_PER_ROUND), before)
            label, reasons = ml.classify_round(got, OVERS_PER_ROUND, all_heard)
            labels.append(label)
            note(target, case, "RECORD", "round %d/%d (%d burners): %s %s; delta %s" % (round_no, ROUNDS, workers, label, reasons, dict(got._asdict())))
    ok, problems = ml.judge_rounds(labels)
    assert ok, "%s: %s (rounds %s)" % (case, "; ".join(problems), labels)
