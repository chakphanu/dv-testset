# SPDX-License-Identifier: MIT
"""ASLDROP1 (DV-Router image wip-42d5473): the AllStar edge counts a REFUSED voice packet once, in dv_edge_over_units_total{dir=rx,result=late}. A packet is
refused when it is before the over's first timestamp, behind playout (more than 60 ms late) or a repeat of a pending position; nothing else moves for it.
Documents: docs/asldrop1-cases.md; judges in dvtest/asldrop1.py; switch DVT_ASLDROP1=on (every case skips without it); profile asl-edge, `-m asl_lab`
(scripts/run.sh adds rfpos1c-usrp.toml under the switch). The rig, the listeners (D-STAR, DMR, USRP in room L), the IAX2 caller and the hub counters are
those of cases/test_rfpos1c.py (imported, not copied).

The tester is the IAX2 caller of the edge's account "acct" and plays an over of 90 distinct mu-law frames at 20 ms (dvtest.rfpos1c.make_frames):

  ASLDROP1-a  frame 40 is sent 110 ms late (after frame 45, before 46): more than 60 ms behind playout. Asserted: not heard by the D-STAR and USRP listeners (every
              other frame once, in order); rx late +1, rx gap +1, room gap +1, over lossy +1 (late counts a packet that arrived and was not used, gap the missing
              position: they do not double count); lost +0, dropped +0, egress tx lost and dropped +0
  ASLDROP1-b  control: frame 40 is sent 30 ms late (after 41, before 42), inside the window: heard at its own position (D-STAR 0..89, USRP all in order), rx late +0,
              nothing moves (gap, lost, dropped, lossy all +0)
  ASLDROP1-c  a mini frame whose timestamp is 20 ms BEFORE the over's first one is sent right after frame 2: rx late +1, gap 0, room late +1, over lossy +1 (an over
              with a late unit folds lossy, as for a refused backward or duplicate D-STAR datagram); all 90 frames heard, no extra frame

The harness checks that the late frame really went out late: the edge's tap capture holds the call's voice frames in the order the tester sent them
(dvtest.asldrop1.order_problems). The shape of the output around the refused frame (a hole at its position, or the later frames closed up) is RECORDED, not
judged: the contract does not name it. Any difference from the contract is a failed assert whose text is also written as a FINDING line to out/<run>/asldrop1.txt;
the numbers per case go to out/<run>/asldrop1.json.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import asl_lab, asldrop1, medload1, metrics, rfpos1b, rfpos1c, xstale1  # noqa: E402
from dvtest.control import RigControl  # noqa: E402

import test_rfpos1c as r1c  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.asl_lab]

LOG = "asldrop1.txt"
JSON = "asldrop1.json"
COUNT = asldrop1.COUNT
RESULTS = {}


def note(lab, case, tag, text):
    line = "[%s] %s: %s" % (tag, case, text)
    print(line, flush=True)
    try:
        with open(Path(lab.out) / LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def save(lab, case, data):
    RESULTS[case] = data
    try:
        (Path(lab.out) / JSON).write_text(json.dumps(RESULTS, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


@pytest.fixture(scope="module")
def lab():
    if not asldrop1.on():
        pytest.skip("DVT_ASLDROP1=on is not set (the image has ASLDROP1, dv-router:wip-42d5473 or later)")
    if os.environ.get("DVT_SUT_PROFILE") != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_TRANSCODER_FAKE") != "1":
        pytest.skip("the D-STAR frames of a position are predictable only with the fake transcoder (--profile asl-edge forces DVT_TRANSCODER_FAKE=1)")
    lab = asl_lab.from_env()
    if lab is None or lab.hosts.get("edge") is None:
        pytest.skip("no AllStar edge: run scripts/run.sh --profile asl-edge (the tester gets DVT_ASL_EDGE=on)")
    lab.wait_ready()
    return lab


@pytest.fixture(scope="module")
def hub(lab):
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1 is set by scripts/run.sh)")
    control = RigControl(Path(lab.out) / "control")

    def read():
        return metrics.parse(control.metrics("xlxd-sut"))

    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if medload1.families_present(read()):
            return read
        time.sleep(5.0)
    pytest.skip("no dv_edge_over_units_total / dv_room_over_units_total in the hub metrics after 90 s: the image has no MEDLOAD1")


@pytest.fixture(scope="module")
def frames():
    ulaw, pcm = rfpos1c.make_frames(COUNT)
    want = rfpos1c.expected(pcm)
    assert all(rfpos1c.distinct(w) for w in want.values())
    marker_ulaw, _marker_pcm = rfpos1c.make_frames(1, seed="asldrop1-before-first")
    return ulaw, want, marker_ulaw[0]


def talk(caller, ulaw, listeners, holds=None, early=None):
    """Key, send COUNT frames at 20 ms. holds {index: ms}: the frame keeps its slot on the call clock but goes out `ms` later (the tester's own pacing:
    `due + ms`). early (after_index, marker frame): right after that frame a mini stamped 20 ms before the over's FIRST timestamp is sent. Unkey, collect."""
    holds = holds or {}
    caller.pump(0.5)
    caller.key()
    t_end = time.monotonic() + r1c.LEAD_S
    while time.monotonic() < t_end:
        caller.pump(0.005)
        listeners.drain()
    pending = []                                # (due time, timestamp, frame)

    def service(until, flush=False):
        """Pump and drain until `until`, sending every held frame whose time has come; flush: also wait for the held frames still pending."""
        while True:
            now = time.monotonic()
            for item in [p for p in pending if p[0] <= now]:
                pending.remove(item)
                caller.voice_at(item[1], item[2])
            if now >= until and not (flush and pending):
                return
            caller.pump(0.001)
            listeners.drain()

    start = time.monotonic()
    first = None
    for i in range(COUNT):
        due = start + i * 0.02
        service(due)
        caller.voice(ulaw[i], drop=i in holds)
        if first is None:
            first = caller.voice_log[0][0]
        if i in holds:
            pending.append((due + holds[i] / 1000.0, caller.voice_log[-1][0], ulaw[i]))
        if early is not None and i == early[0]:
            caller.voice_at(first - 20, early[1], kind="early")
    service(start + COUNT * 0.02 + 0.1, flush=True)
    caller.pump(0.1)
    caller.unkey()
    t_end = time.monotonic() + r1c.TAIL_S
    while time.monotonic() < t_end:
        caller.pump(0.005)
        listeners.drain()


def run_over(lab, hub, frames, case, holds=None, early=None):
    ulaw, want, marker = frames
    ip = lab.node("edge").ip
    procs = list(rfpos1c.EGRESS.values())

    def read():
        return rfpos1b.reading(hub(), procs, room=rfpos1c.ROOM_LABEL, ingress=rfpos1c.INGRESS_PROC)

    before = rfpos1b.stable_baseline(read)
    rx_before = asldrop1.rx_counts(hub())
    with r1c.Listeners(ip) as listeners:
        with lab.scenario("asldrop1-" + case, capture=["edge"]) as sc:
            caller = r1c.iax_caller(lab)
            call = caller.local
            try:
                talk(caller, ulaw, listeners, holds, (early, marker) if early is not None else None)
            finally:
                caller.close()
        listeners.drain()
        dstar_rows, dstar_repeats = rfpos1c.dstar_rows(listeners.dstar.client.inbox)
        dmr_bursts, _seqs = rfpos1c.dmr_bursts(listeners.dmr.client.inbox)
        usrp_rows = rfpos1c.usrp_voice(listeners.usrp_in)
    try:
        after = rfpos1b.wait_settled(read, before, fold_procs=list(procs))
    except TimeoutError as exc:
        note(lab, case, "RECORD", "counters did not settle: %s" % exc)
        after = read()
    samples = hub()
    stamps = rfpos1c.edge_voice_stamps(sc.dir / "edge.pcap", ip, lab.port, call)
    time.sleep(r1c.GAP_S)
    return {"dstar_rows": dstar_rows, "dstar_repeats": dstar_repeats, "dmr_bursts": dmr_bursts, "usrp_rows": usrp_rows, "stamps": stamps,
            "voice_log": list(caller.voice_log), "change": rfpos1b.delta(after, before),
            "rx": asldrop1.rx_delta(asldrop1.rx_counts(samples), rx_before), "procs_seen": xstale1.procs_seen(samples)}


def judge(lab, case, frames, out, late, absent, gap=0, lossy=0, room_late=None):
    """Verdicts of one over: {owner: [sentences]}. late: refused packets expected (rx late); gap: missing positions expected (rx and room gap); lossy: the over folded lossy;
    room_late: the room's late count when asserted; absent: the position whose frame must not be heard (None: all heard)."""
    _ulaw, want, _marker = frames
    problems = {"harness": [], "listeners": [], "counters": []}
    problems["harness"] += asldrop1.order_problems(out["stamps"], out["voice_log"])
    d_slots, _ = rfpos1c.dstar_slots(out["dstar_rows"], want["dstar"])
    u_matched = rfpos1c.usrp_matched(out["usrp_rows"], want["usrp"])
    m_slots, _ = rfpos1c.dmr_burst_slots(out["dmr_bursts"], want["dmr"])
    if not d_slots or not u_matched:
        problems["harness"].append("a listener heard nothing (D-STAR %d frames, USRP %d packets)" % (len(d_slots), len(u_matched)))
    if out["dstar_repeats"]:
        problems["harness"].append("repeated D-STAR positions: %d" % out["dstar_repeats"])
    problems["listeners"] = asldrop1.judge_heard(d_slots, u_matched, absent)
    c_problems, c_findings, facts = asldrop1.judge_counters(out["change"], out["rx"], late, gap, lossy, room_late)
    problems["counters"] = c_problems
    missing = [p for p in range(COUNT) if p not in d_slots]
    order = asldrop1.order_problems(out["stamps"], out["voice_log"])
    note(lab, case, "RECORD", "the tester sent %s; the edge's capture holds %d voice frames, in the order sent: %s" % (
        [(k, sum(1 for _s, kind in out["voice_log"] if kind == k)) for k in ("full", "mini", "drop", "late", "early")], len(out["stamps"]),
        "yes" if not order else "NO (%s)" % order[0]))
    note(lab, case, "RECORD", "D-STAR: %d frames, missing positions %s, %s" % (len(d_slots), missing[:8], rfpos1c.runs(d_slots)))
    note(lab, case, "RECORD", "USRP: %d packets, arrival order %s" % (len(u_matched), rfpos1c.runs(dict(enumerate(u_matched)))))
    note(lab, case, "RECORD", "DMR (recorded): %d frames in %d bursts, %s" % (len(m_slots), len(out["dmr_bursts"]), rfpos1c.runs(m_slots)))
    note(lab, case, "RECORD", "counter delta: %s; procs with over series %s" % (facts, out["procs_seen"]))
    for line in c_findings:
        note(lab, case, "FINDING", line)
    for owner, lines in problems.items():
        for line in lines:
            note(lab, case, "FINDING", "%s: %s" % (owner, line))
    save(lab, case, {"expected_late": late, "absent_position": absent, "sent": [list(x) for x in out["voice_log"] if x[1] != "mini"],
                     "capture_voice_frames": len(out["stamps"]), "dstar": {"frames": len(d_slots), "missing_positions": missing, "runs": rfpos1c.runs(d_slots)},
                     "usrp": {"packets": len(u_matched), "arrival_order": rfpos1c.runs(dict(enumerate(u_matched)))},
                     "dmr": {"frames": len(m_slots), "runs": rfpos1c.runs(m_slots)}, "counters": facts, "rx_delta": out["rx"], "problems": problems})
    return problems


def check(problems):
    flat = ["%s: %s" % (owner, line) for owner, lines in problems.items() for line in lines]
    assert not flat, "; ".join(flat)


def test_asldrop1_a_frame_110_ms_late_is_refused_once_as_late(lab, hub, frames):
    """Frame 40 goes out 110 ms late (after frame 45): refused, not heard; rx late +1, rx gap +1, room gap +1, over lossy +1 (late counts a packet that arrived and was
    not used, gap the missing position: no double count); lost and dropped +0 everywhere."""
    out = run_over(lab, hub, frames, "a", holds={asldrop1.HELD: asldrop1.LATE_MS})
    check(judge(lab, "a", frames, out, late=1, absent=asldrop1.HELD, gap=1, lossy=1))


def test_asldrop1_b_frame_30_ms_late_is_heard_at_its_position(lab, hub, frames):
    """Control: frame 40 goes out 30 ms late, inside the window: heard at its own position, rx late +0, nothing moves."""
    out = run_over(lab, hub, frames, "b", holds={asldrop1.HELD: asldrop1.INSIDE_MS})
    check(judge(lab, "b", frames, out, late=0, absent=None))


def test_asldrop1_c_a_mini_before_the_first_timestamp_is_refused_once_as_late(lab, hub, frames):
    """A mini stamped 20 ms before the over's first timestamp is sent after frame 2: rx late +1, gap 0, room late +1, over lossy +1 (an over with a late unit folds
    lossy), all 90 frames heard and nothing extra."""
    out = run_over(lab, hub, frames, "c", early=asldrop1.BEFORE_AFTER)
    check(judge(lab, "c", frames, out, late=1, absent=None, gap=0, lossy=1, room_late=1))
