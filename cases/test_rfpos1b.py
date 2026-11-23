# SPDX-License-Identifier: MIT
"""RFPOS1 step B (DV-Router 47dac7b4, in 5333e4fc, image dv-router:wip-c3b24d2): the shared edge core no longer lets an end-hint unit widen the egress width,
which had moved the last frame of a YSF end bundle and made a false end-of-over gap on every egress edge. docs/rfpos1b-cases.md; the arithmetic and the verdict are
dvtest/rfpos1b.py. Switch DVT_RFPOS1B=on asserts; unset, the same over is played and the counters are only RECORDED (a FINDING line per difference).

One clean YSF over (65 voice frames = 13 packets, header and terminator) of a talker on module Y, heard by a YSF listener on the same module (the same-mode egress),
plus, per case, one cross-mode listener (the cross-mode egress):

  RFPOS1B-ysf    YSF -> YSF only
  RFPOS1B-dmr    YSF -> YSF and DMR (the edge dmrhb)
  RFPOS1B-dstar  YSF -> YSF and D-STAR (the edge dextra; D-STAR link streams are unchanged by the fix)

Asserted under DVT_RFPOS1B=on: the YSF listener heard the whole over (header first, terminator last with the end bit) and, for a cross case, the cross listener heard it;
the hub counters over the over: room closed one over, YSF ingress gap 0, room gap 0 and lost 0, the over not lossy or end_missing, on every egress edge
dv_edge_over_units_total{dir=tx,result=lost} 0 and no lossy or end_missing egress over (a false end-of-over gap on the egress edge was `lost`), a fold at the cross edge.

The YSF proto cases (cases/proto/test_ysf.py) run in the same stage and need nothing more. Needs DVT_RIG_CONTROL=1 (set by scripts/run.sh), DVT_SUT_PROFILE=dvrouter
DVT_SUT_LAUNCH=native and an image with MEDLOAD1 (the per-over series; without them the cases skip with the reason). NOT RUN on a rig: written against the sources of
47dac7b4.
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import clients, medload1, metrics, rfpos1b  # noqa: E402

import data_support as ds  # noqa: E402
import test_short_stream_guard as sg  # noqa: E402
import test_ysf as ty  # noqa: E402

pytestmark = [pytest.mark.rig]

LOG = "rfpos1b.txt"
MODULE = ty.MOD
CROSS = ("ysf", "dmr", "dstar")                 # the cases; "ysf" has no cross-mode listener


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


@pytest.fixture(scope="module")
def hub(rig_control):
    """A reader of the hub samples; skips when the image has no MEDLOAD1 series (the per-over counters this case reads)."""
    def read():
        return metrics.parse(rig_control.metrics("xlxd-sut"))

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if medload1.families_present(read()):
            return read
        time.sleep(5.0)
    pytest.skip("no dv_edge_over_units_total / dv_room_over_units_total in the hub metrics after 60 s: the image has no MEDLOAD1 (wip-f1f3777 or later is needed)")


def listener_of(kind, ip):
    """A linked listener of `kind` on module Y (dmr or dstar)."""
    if kind == "dmr":
        c = clients.make("dmr", MODULE, 0)
        assert c.link(ip), "%s: DMR link failed" % ip
        return c
    return sg.dstar(ip, "N0RFPB", MODULE)


def close_listener(kind, c):
    if kind == "dmr":
        clients.close(c)
    else:
        c.client.close()


@pytest.mark.parametrize("cross", CROSS)
def test_rfpos1b_ysf_over_ends_without_gap_or_lost(cross, rig, target, hub):
    case = "RFPOS1B-" + cross
    strict = rfpos1b.on()
    ip = rig.xlxd_sut
    procs = [rfpos1b.EGRESS_PROC["ysf"]] + ([rfpos1b.EGRESS_PROC[cross]] if cross != "ysf" else [])
    missing, seen = rfpos1b.procs_missing(hub(), procs)
    if missing:
        pytest.fail("%s: no dv_edge_overs_total{proc=%s} on the hub metrics (procs with the family: %s): the image has no egress over accounting, or the edge is not running" % (
            case, ", ".join(missing), seen))

    def read():
        return rfpos1b.reading(hub(), procs)

    talker, listener = ty.new(ip), ty.new(ip)
    extra = None
    try:
        ty.ready(ip, talker, listener)
        if cross != "ysf":
            extra = listener_of(cross, ip)
            time.sleep(ty.CACHE)
            ty.clear(listener)
            extra.client.drain()
            extra.client.inbox.clear()
        before = rfpos1b.stable_baseline(read)
        sent = ty.frames(seed="rfpos1b-" + cross)
        ty.send(talker, ty.Y.stream_packets(sent, talker.callsign, talker.dgid), listener, *([extra] if extra is not None else []), wait=2.0)
        checks = ty.egress_checks(ty.ysfd(listener))
        cross_heard = len(extra.client.inbox) if extra is not None else None
    finally:
        if extra is not None:
            close_listener(cross, extra)
        ty.done(talker, listener)
    note(target, case, "RECORD", "YSF listener heard %d packets (header first %s, terminator last %s, end bit only last %s)%s" % (
        checks["packets"], checks["header_first"], checks["terminator_last"], checks["eot_only_last"],
        "; %s listener heard %d datagrams" % (cross, cross_heard) if extra is not None else ""))
    assert checks["packets"], "%s: the YSF listener heard nothing: the over did not reach the egress edge (a harness slip, not a result)" % case
    if extra is not None:
        assert cross_heard, "%s: the %s listener heard nothing: the cross egress was not exercised (a harness slip, not a result)" % (case, cross)
    fold = tuple(p for p in procs if p != rfpos1b.EGRESS_PROC["ysf"])      # the cross edge always folds; a YSF listener may be served by the fast path
    try:
        after = rfpos1b.wait_settled(read, before, fold_procs=fold)
    except TimeoutError as exc:
        pytest.fail("%s: %s (series of the edges: %s)" % (case, exc, "; ".join(
            moved_lines(before, read())) or "none moved"))
    change = rfpos1b.delta(after, before)
    verdict = rfpos1b.judge(change, fold_procs=fold)
    note(target, case, "RECORD", "delta of the counters: %s" % verdict.facts)
    for line in verdict.findings:
        note(target, case, "FINDING", line)
    if strict:
        assert checks["header_first"] and checks["terminator_last"] and checks["eot_only_last"], "%s: the YSF egress structure is wrong: %s" % (case, checks)
        assert not verdict.problems, "%s: %s" % (case, verdict.problems)
    else:
        for line in verdict.problems:
            note(target, case, "FINDING", "not asserted (DVT_RFPOS1B is off): %s" % line)


def moved_lines(before, after):
    """What moved between two Readings, for the failure of a close that never came."""
    out = []
    for (proc, a), (_, b) in zip(before.egress, after.egress):
        if a != b:
            out.append("%s %s -> %s" % (proc, dict(a._asdict()), dict(b._asdict())))
    if before.room != after.room:
        out.append("room %s -> %s" % (dict(before.room._asdict()), dict(after.room._asdict())))
    return out
