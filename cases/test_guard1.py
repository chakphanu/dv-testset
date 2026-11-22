# SPDX-License-Identifier: MIT
"""GUARDSYNC1 / GUARDMERGE1 (DV-Router c1b239d1 and da900207, in bc9af2b1, image dv-router:wip-f1f3777): a strike-3 block is a node-wide penalty and survives
the restart of a room controller. Document: floor-control.md "Guard penalties" (cross-room state), fabric.md "link state"; docs/guard1-cases.md. Pure judge:
dvtest/guard1.py. Switch DVT_GUARD1=on; without it every case skips with the reason. NOT RUN on a rig: written against the sources of bc9af2b1.

A callsign earns strike 3 in room U (three rounds of three short D-STAR overs, the B18 ladder of cases/test_short_stream_guard.py: blocks of 3 s, 10 s, then 300 s). The record
`guard/<callsign>` is put to every controller and edge. The room T is then SIGKILLed (dv-room by module letter): the launcher respawns it with a NEW room_epoch and an EMPTY
guard cache. Probes of the blocked callsign linked to T (header + 5 frames, a short stream never starts: a refused probe opens none) must not be heard from 31 s (+ slack)
after T carried a control callsign's over again:

  GUARD1-a  after   the strike-3 block is earned BEFORE the kill (the record is held by the other controllers; T asks them at startup: GUARDSYNC1)
  GUARD1-b  during  T is killed again and again for 45 s WHILE the ladder is climbed in U (T misses every put; the strikes 1 to 3 only reach it by the snapshot after its
                    last restart: GUARDMERGE1 / GUARDSYNC1, the record must come as the highest strike with its 300 s block, never as nothing)

A probe heard before the deadline is a FINDING (the window the bound allows), after it a failure. The control callsign (not blocked) is heard on T once the room is back: a
room that never comes back is a failure of its own. UNVERIFIED shapes (recorded when they break): the same DExtra callsign linked to two modules at once; the block of U
spanning module T (a precondition, checked before the kill in GUARD1-a).
"""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import flk15, guard1  # noqa: E402

import data_support as ds  # noqa: E402
import test_short_stream_guard as sg  # noqa: E402

pytestmark = [pytest.mark.rig]

LOG = "guard1.txt"
UP_BUDGET_S = 90.0                    # the respawned room carries an over again
ONE = dict(earn="U", observe="T", blocked="N0GDA", control="N0GDB", listen="N0GDL", listen_u="N0GDM")
TWO = dict(earn="S", observe="R", blocked="N0GDC", control="N0GDD", listen="N0GDN", listen_u="N0GDO")
KILL_DURING_S = 45.0


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not guard1.strict_on():
        pytest.skip("DVT_GUARD1=on is not set (the image under test has GUARDSYNC1 / GUARDMERGE1, DV-Router c1b239d1 + da900207: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


def earn_three_strikes(talker, listener):
    """The B18 ladder for one callsign in one room: three short overs, wait out the 3 s block, three more, wait out the 10 s block, three more (strike 3, 300 s)."""
    for block_s in guard1.BLOCK_S[:2]:
        sg.shorts(talker, listener)
        time.sleep(block_s + 1.5)
    sg.shorts(talker, listener)


def kill_room(rig_control, letter):
    return rig_control.kill_proc("xlxd-sut", "dv-room", "KILL", rooms=letter)


def observe(c, rig_control, target, case, links, window_s=guard1.APPLIED_BY_S + guard1.SLACK_S + 10.0):
    """After the kill / the kill loop: probe the blocked callsign (T) every 0.5 s while the control callsign is probed every probe round until it is heard once
    (t_up), then for `window_s` more seconds. Returns (probes, t_up); pytest.fail when the room never carries the control over."""
    probes, t_up, t0 = [], None, time.monotonic()
    while True:
        now = time.monotonic()
        if t_up is None and now - t0 > UP_BUDGET_S:
            pytest.fail("%s: the control callsign was not heard on module %s within %.0f s of the room restart" % (case, links["observe"], UP_BUDGET_S))
        if t_up is not None and now - t_up > window_s:
            return probes, t_up
        heard = sg.probe(c["blocked_t"], c["listen_t"])
        probes.append(guard1.Probe(time.monotonic(), heard))
        if t_up is None and sg.probe(c["control_t"], c["listen_t"]):
            t_up = time.monotonic()
        time.sleep(guard1.PROBE_PERIOD_S)


def links(ip, shape):
    """The clients of one scenario: the blocked callsign in the earn room U and in the observed room T, a control callsign and a listener in T, a listener in U."""
    return {
        "blocked_u": sg.dstar(ip, shape["blocked"], shape["earn"]),
        "listen_u": sg.dstar(ip, shape["listen_u"], shape["earn"]),
        "blocked_t": sg.dstar(ip, shape["blocked"], shape["observe"]),
        "control_t": sg.dstar(ip, shape["control"], shape["observe"]),
        "listen_t": sg.dstar(ip, shape["listen"], shape["observe"]),
        "observe": shape["observe"],
    }


def judge(target, case, probes, t_up):
    verdict = guard1.judge_probes(probes, t_up)
    note(target, case, "RECORD", guard1.describe(probes, t_up) + "; first refused %s s after the room returned" % verdict.first_refused)
    for p in verdict.early_leaks:
        note(target, case, "FINDING", "the blocked callsign was heard %.1f s after the room returned (inside the %.0f s bound): the record was not applied yet" % (p.t - t_up, guard1.APPLIED_BY_S))
    assert not verdict.problems, "%s: %s" % (case, "; ".join(verdict.problems))


def test_guard1_a_a_block_earned_before_survives_a_room_restart(rig, target, rig_control):
    case = "GUARD1-a"
    ip = rig.xlxd_sut
    c = links(ip, ONE)
    assert sg.probe(c["blocked_u"], c["listen_u"]), "%s: the callsign is not heard in U before any strike (the callsign or the module is in use)" % case
    assert sg.probe(c["blocked_t"], c["listen_t"]), "%s: the callsign is not heard in T before any strike" % case
    earn_three_strikes(c["blocked_u"], c["listen_u"])
    assert not sg.probe(c["blocked_u"], c["listen_u"]), "%s: strike 3 is not in force in the room that earned it (the ladder did not reach 300 s)" % case
    time.sleep(1.5)                                                       # the put reaches the other controllers
    if sg.probe(c["blocked_t"], c["listen_t"]):
        note(target, case, "FINDING", "PRODUCT FINDING per dv-router: the strike-3 block of module %s is not in force in module %s BEFORE any restart (floor-control.md: guard penalties are per callsign and node-wide)" % (ONE["earn"], ONE["observe"]))
        pytest.fail("%s: precondition: the block does not span modules before the kill" % case)
    killed = kill_room(rig_control, ONE["observe"])
    note(target, case, "RECORD", "dv-room %s killed (pids %s)" % (ONE["observe"], getattr(killed, "pids", "?")))
    probes, t_up = observe(c, rig_control, target, case, ONE)
    judge(target, case, probes, t_up)


def test_guard1_b_strikes_earned_while_a_room_is_down_arrive_by_the_snapshot(rig, target, rig_control):
    case = "GUARD1-b"
    ip = rig.xlxd_sut
    c = links(ip, TWO)
    assert sg.probe(c["blocked_u"], c["listen_u"]), "%s: the callsign is not heard in S before any strike" % case
    result = {}

    def loop():
        result["landed"], result["t0"], result["t1"] = flk15.kill_loop(lambda: kill_room(rig_control, TWO["observe"]), KILL_DURING_S, period_s=1.0)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    time.sleep(2.0)
    earn_three_strikes(c["blocked_u"], c["listen_u"])
    assert not sg.probe(c["blocked_u"], c["listen_u"]), "%s: strike 3 is not in force in the earning room" % case
    thread.join(KILL_DURING_S + 30.0)
    note(target, case, "RECORD", "room %s kills landed %s over %.0f s" % (TWO["observe"], result.get("landed"), result.get("t1", 0) - result.get("t0", 0)))
    assert result.get("landed", 0) >= 1, "%s: no kill of the room landed" % case
    probes, t_up = observe(c, rig_control, target, case, TWO)
    judge(target, case, probes, t_up)
