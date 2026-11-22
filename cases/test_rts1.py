# SPDX-License-Identifier: MIT
"""RTS1 (DV-Router b64733cd, in bc9af2b1, image dv-router:wip-f1f3777): an RTS in the winner gap gets DENY with reason 12 (no_winner), counted, never silently dropped;
an RTS waiting at a link down is counted `link_down`. Document: floor-control.md "DENY reasons" (12 no current winner) and "RTS", status.md (dv_room_rts_total,
dv_edge_drops_total{reason=deny_no_winner}); docs/rts1-cases.md. Pure helpers: dvtest/rts1.py. Switch DVT_RTS1=on. NOT RUN on a rig: written against the sources of bc9af2b1.

RTS and CTS are fabric-internal (a query on key `rts` between the edge and the room): no legacy client protocol carries them, so the "old client must read DENY 12 as busy" statement is
about an edge BUILT BEFORE RTS1 (it maps the unknown reason to Busy and counts deny_busy) and cannot be mixed with this image in one node: a GAP. The winner gap itself (a connected,
non-fenced controller that is not the highest live token, a few ms wide) cannot be provoked from outside the launcher either: a GAP. What a black box CAN assert:

  RTS1-a  the image has RTS1 (HELP of dv_room_rts_total reads "RTS processing, by result"); the five fixed results are present at zero for every room; the sparse ones obey
          conservation (no_winner is part of deny); the edge reason series deny_no_winner is recorded (a FINDING when absent)
  RTS1-b  one normal over: grant rises, no_winner, link_down and deny_no_winner do not move (no RTS of a healthy room falls into the gap)
  RTS1-c  the room is SIGKILLed while overs are offered (six seconds of kills): afterwards a fresh over is relayed in full, the room's no_winner never exceeds its deny, and the edge
          reasons that moved (deny_no_winner, deny_controller_absent, deny_cts_timeout) are recorded: an over offered to a missing controller is dropped with a counted reason
"""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import flk15, metrics, rts1  # noqa: E402

import data_support as ds  # noqa: E402
import test_short_stream_guard as sg  # noqa: E402

pytestmark = [pytest.mark.rig]

LOG = "rts1.txt"
MODULE = "Q"
ROOM = "q"
EDGE_REASONS = ("deny_no_winner", "deny_controller_absent", "deny_cts_timeout", "deny_busy")
SETTLE_S = 45.0                      # the hub refreshes the records about every 10 s: three periods
KILL_S = 6.0
UP_BUDGET_S = 90.0


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not rts1.strict_on():
        pytest.skip("DVT_RTS1=on is not set (the image under test has RTS1, DV-Router b64733cd: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


def read(rig_control):
    text = rig_control.metrics("xlxd-sut")
    return text, metrics.parse(text)


def edge_reasons(samples):
    return {r: metrics.total(samples, "dv_edge_drops_total", reason=r) for r in EDGE_REASONS}


def wait_for(rig_control, ready, budget_s=SETTLE_S):
    """Reads the hub every 5 s until ready(samples) or the budget; returns the last (text, samples)."""
    end, last = time.monotonic() + budget_s, None
    while True:
        last = read(rig_control)
        if ready(last[1]) or time.monotonic() >= end:
            return last
        time.sleep(5.0)


def test_rts1_a_the_image_and_the_series(rig, target, rig_control):
    case = "RTS1-a"
    text, samples = wait_for(rig_control, lambda s: any(x.name == "dv_room_rts_total" for x in s))
    marker = rts1.image_has_rts1(text)
    assert marker is not None, "%s: no dv_room_rts_total in the hub metrics after %.0f s (no controller published)" % (case, SETTLE_S)
    assert marker is True, "%s: HELP of dv_room_rts_total is %r, RTS1 has %r: the image does not have RTS1" % (case, rts1.help_text(text), rts1.NEW_HELP)
    rooms = sorted({s.labels.get("room") for s in samples if s.name == "dv_room_rts_total"})
    assert rooms, "%s: no room label on dv_room_rts_total" % case
    for room in rooms:
        results = rts1.room_results(samples, room)
        missing = rts1.fixed_series_missing(results)
        assert not missing, "%s: room %s lacks the fixed result series %s (status.md: present at zero from the first record)" % (case, room, missing)
        problems = rts1.conservation_problems(results)
        assert not problems, "%s: room %s: %s" % (case, room, "; ".join(problems))
    series = [s for s in samples if s.name == "dv_edge_drops_total" and s.labels.get("reason") == rts1.EDGE_REASON]
    if not series:
        note(target, case, "FINDING", "dv_edge_drops_total{reason=%s} is not exported by any edge before its first count (status.md says fixed labels; zero present is stated only for deny_clock_domain)" % rts1.EDGE_REASON)
    note(target, case, "RECORD", "rooms %s; sparse results seen: %s" % (rooms, sorted({s.labels.get("result") for s in samples if s.name == "dv_room_rts_total" and s.labels.get("result") in rts1.SPARSE_RESULTS})))


def test_rts1_b_a_normal_over_never_falls_into_the_gap(rig, target, rig_control):
    case = "RTS1-b"
    ip = rig.xlxd_sut
    talker, listener = sg.dstar(ip, "N0RTSA", MODULE), sg.dstar(ip, "N0RTSL", MODULE)
    _t, before = read(rig_control)
    base = rts1.room_results(before, ROOM)
    base_edge = edge_reasons(before)
    assert sg.talk_long(talker, listener), "%s: a long over on module %s was not heard in full" % (case, MODULE)
    _t, after = wait_for(rig_control, lambda s: rts1.room_results(s, ROOM).get("grant", 0.0) > base.get("grant", 0.0))
    now = rts1.room_results(after, ROOM)
    assert now.get("grant", 0.0) > base.get("grant", 0.0), "%s: dv_room_rts_total{room=%s,result=grant} did not rise after an over (%s -> %s)" % (case, ROOM, base, now)
    for key in rts1.SPARSE_RESULTS:
        assert now.get(key, 0.0) == base.get(key, 0.0), "%s: %s moved on a healthy room: %s -> %s" % (case, key, base.get(key, 0.0), now.get(key, 0.0))
    assert edge_reasons(after)["deny_no_winner"] == base_edge["deny_no_winner"], "%s: an edge counted deny_no_winner on a healthy room" % case
    assert not rts1.conservation_problems(now)


def test_rts1_c_a_room_restart_leaves_no_silent_loss(rig, target, rig_control):
    case = "RTS1-c"
    ip = rig.xlxd_sut
    talker, listener = sg.dstar(ip, "N0RTSB", MODULE), sg.dstar(ip, "N0RTSM", MODULE)
    assert sg.talk_long(talker, listener), "%s: no over is relayed before the kill" % case
    _t, before = read(rig_control)
    edge_before = edge_reasons(before)
    result = {}

    def loop():
        result["landed"], result["t0"], result["t1"] = flk15.kill_loop(lambda: rig_control.kill_proc("xlxd-sut", "dv-room", "KILL", rooms=MODULE), KILL_S, period_s=1.0)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    offered = 0
    while thread.is_alive():
        sg.probe(talker, listener)
        offered += 1
        time.sleep(0.5)
    thread.join(10.0)
    assert result.get("landed", 0) >= 1, "%s: no kill landed" % case
    t0 = time.monotonic()
    while not sg.probe(talker, listener):
        if time.monotonic() - t0 > UP_BUDGET_S:
            pytest.fail("%s: no over was relayed within %.0f s of the room restart" % (case, UP_BUDGET_S))
        time.sleep(1.0)
    assert sg.talk_long(talker, listener), "%s: a fresh over after the restart is not relayed in full" % case
    _t, after = wait_for(rig_control, lambda s: any(edge_reasons(s)[r] > edge_before[r] for r in EDGE_REASONS))
    edge_after = edge_reasons(after)
    moved = {r: edge_after[r] - edge_before[r] for r in EDGE_REASONS if edge_after[r] != edge_before[r]}
    now = rts1.room_results(after, ROOM)
    note(target, case, "RECORD", "%d kills, %d probes offered; edge reasons that moved %s; room %s counters %s" % (result.get("landed", 0), offered, moved, ROOM, now))
    assert not rts1.conservation_problems(now), "%s: %s" % (case, "; ".join(rts1.conservation_problems(now)))
    if now.get("link_down", 0.0):
        note(target, case, "FINDING", "link_down = %g after a process kill: the room's router link did not go down, the process did (status.md)" % now["link_down"])
    if "deny_busy" in moved:
        note(target, case, "FINDING", "an edge counted deny_busy during the restart window (%s): reason 12 read as busy is the PRE-RTS1 edge behaviour" % moved["deny_busy"])
