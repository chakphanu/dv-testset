# SPDX-License-Identifier: MIT
"""RFPOS1 step C (DV-Router fb3167bb, "AllStar streams state frame positions through the transcoder"): one IAX2 voice frame lost between an AllStar
node and the edge is ONE missing position at the room's listeners, at its own position: nothing is closed up, nothing shifts, and the rest of the over
is not counted as lost. Documents: allstar.md "Runtime and media", media-format.md "Transcoded profiles", floor-control.md RTS flags;
docs/rfpos1c-cases.md. Pure helpers and judges: dvtest/rfpos1c.py. Switch: DVT_RFPOS1C=on (every case skips without it). Profile: asl-edge
(scripts/run.sh --profile asl-edge adds rfpos1c-usrp.toml under the switch).

The talker is the tester itself, an IAX2 client of the edge's account "acct" (ulaw, room L, the way test_asl_edge.py T1b does): it sends an over of
90 distinct random mu-law frames at 20 ms and leaves out ONE of them (dvtest.proto.iax2.Client.voice(drop=True): the call clock advances, nothing is
sent, so the timestamps arriving at the edge have a hole of exactly one position; the edge's own tap capture shows it). Three listeners sit in the
same room L: a D-STAR client (DExtra) and a DMR client, both transcoded (the fake transcoder, so every frame of a position is predictable), and a USRP
peer (static row, PCM, no codec: the control that already kept positions).

  RFPOS1C-a  no loss: every position at every listener holds its own frame; no gap, no lost, no lossy over anywhere (control)
  RFPOS1C-b  one MINI frame lost mid-over (position 40, the middle frame of DMR burst 13): D-STAR misses exactly position 40 (a skipped counter), DMR has
             its burst 13 with frames 39 and 41 intact and the fill at 40, USRP keeps the hole in time; every other position holds its own frame. Counters
             (the same before and after step C): ingress gap +1, room gap +1, lost +0, one lossy over; egress `tx lost` +0 (since MEDLOAD1b an egress counts
             only loss on its own leg, an upstream gap is not tx loss; a rise is a FINDING). The output before step C was one frame short, no gap and
             every later frame shifted (the compaction)
  RFPOS1C-c  the same with one FULL voice frame lost (position 40 is a full frame of the call, as Asterisk sends them now and then)
  RFPOS1C-d  control for c: the same over with a full frame at position 40, nothing lost

Judged apart, so a failure names its owner: "harness" (the hole did not arrive at the edge as planned, a listener heard nothing, an egress edge never
folded the over), "usrp" (the control listener), "dstar", "dmr", "counters". Numbers go to stdout and out/<run>/rfpos1c.txt (RECORD / FINDING) and, per
case, out/<run>/rfpos1c.json.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import asl_lab, clients, medload1, metrics, rfpos1b, rfpos1c, xstale1  # noqa: E402
from dvtest.control import RigControl  # noqa: E402
from dvtest.proto import dmr, iax2  # noqa: E402

import data_support as ds  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.asl_lab]

LOG = "rfpos1c.txt"
JSON = "rfpos1c.json"
COUNT, DROP = rfpos1c.COUNT, rfpos1c.DROP
SETUP_S = 1.5               # after the listeners linked: the room's client lists refresh
LEAD_S = 0.3                # after RADIO_KEY, before the first voice frame
TAIL_S = 4.0                # after the unkey: the egress clocks drain and the listeners collect
GAP_S = 4.0                 # between two overs
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
    if not rfpos1c.on():
        pytest.skip("DVT_RFPOS1C=on is not set (the image has RFPOS1 step C, DV-Router fb3167bb: dv-router:wip-3f142cd does not; set it to run the cases against any image)")
    if os.environ.get("DVT_SUT_PROFILE") != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_TRANSCODER_FAKE") != "1":
        pytest.skip("the D-STAR and DMR frames of a position are predictable only with the fake transcoder (--profile asl-edge forces DVT_TRANSCODER_FAKE=1)")
    lab = asl_lab.from_env()
    if lab is None or lab.hosts.get("edge") is None:
        pytest.skip("no AllStar edge: run scripts/run.sh --profile asl-edge (the tester gets DVT_ASL_EDGE=on)")
    lab.wait_ready()
    return lab


@pytest.fixture(scope="module")
def hub(lab):
    """A reader of the hub samples (the control channel of run.sh); skips when the image has no per-over series."""
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
    assert all(rfpos1c.distinct(w) for w in want.values()), "the frames of two positions are equal: a position cannot be identified by its content"
    return ulaw, want


def iax_caller(lab):
    edge = lab.node("edge")
    c = iax2.Client(edge.ip, asl_lab.ACCT_SECRET, username="acct", port=lab.port, codec=iax2.ULAW)
    c.call(edge.node, caller="7999", name="N0TEST", timeout=15, empty_token=True, wait_newkey=False)
    return c


class Listeners:
    """The three listeners of room L: D-STAR (DExtra), DMR and a USRP peer socket."""

    def __init__(self, ip):
        self.ip = ip
        self.dstar = self.dmr = self.usrp = None
        self.usrp_in = []

    def __enter__(self):
        try:
            self.dstar_cm = ds.dstar_listener(self.ip, module=rfpos1c.ROOM, callsign="N0CALL")
            self.dstar = self.dstar_cm.__enter__()
            self.dmr = clients.make("dmr", rfpos1c.ROOM, 0)
            assert self.dmr.link(self.ip), "DMR link to %s failed" % self.ip
            self.usrp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.usrp.bind(("0.0.0.0", rfpos1c.USRP_PEER_PORT))
            self.usrp.setblocking(False)
            time.sleep(SETUP_S)
            self.dstar.client.drain()
            self.dstar.client.inbox.clear()
            self.dmr.client.drain()
            self.dmr.client.inbox.clear()
            self.drain()
            self.usrp_in.clear()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self.usrp is not None:
            self.usrp.close()
        if self.dmr is not None:
            clients.close(self.dmr)
        if self.dstar is not None:
            self.dstar_cm.__exit__(None, None, None)
        return False

    def drain(self):
        self.dstar.client.drain()
        self.dmr.client.drain()
        try:
            while True:
                self.usrp_in.append(self.usrp.recv(2048))
        except BlockingIOError:
            pass


def talk(lab, caller, ulaw, listeners, drops, fulls):
    """Key, send COUNT frames at 20 ms (frame i dropped when i in drops, a full frame when i in fulls), unkey, collect for TAIL_S."""
    caller.pump(0.5)
    caller.key()
    t_end = time.monotonic() + LEAD_S
    while time.monotonic() < t_end:
        caller.pump(0.005)
        listeners.drain()
    start = time.monotonic()
    for i in range(COUNT):
        due = start + i * 0.02
        while time.monotonic() < due:
            caller.pump(0.001)
            listeners.drain()
        caller.voice(ulaw[i], drop=i in drops, full=i in fulls)
    caller.pump(0.1)
    caller.unkey()
    t_end = time.monotonic() + TAIL_S
    while time.monotonic() < t_end:
        caller.pump(0.005)
        listeners.drain()


def run_over(lab, hub, frames, case, drop=None, full=None):
    """One over with the three listeners; returns everything the judges need."""
    ulaw, want = frames
    ip = lab.node("edge").ip
    procs = list(rfpos1c.EGRESS.values())

    def read():
        return rfpos1b.reading(hub(), procs, room=rfpos1c.ROOM_LABEL, ingress=rfpos1c.INGRESS_PROC)

    before = rfpos1b.stable_baseline(read)
    rx_before = rfpos1c.rx_gaps(hub())
    drops, fulls = ({drop} if drop is not None else set()), ({full} if full is not None else set())
    with Listeners(ip) as listeners:
        with lab.scenario("rfpos1c-" + case, capture=["edge"]) as sc:
            caller = iax_caller(lab)
            call = caller.local
            try:
                talk(lab, caller, ulaw, listeners, drops, fulls)
            finally:
                caller.close()
            sc.note("dropped", sorted(drops))
        listeners.drain()
        dstar_rows, dstar_repeats = rfpos1c.dstar_rows(listeners.dstar.client.inbox)
        dmr_frames = []
        for data in listeners.dmr.client.inbox:
            if dmr.is_voice(data):
                dmr_frames.extend(dmr.extract(data[20:53]))
        usrp_rows = rfpos1c.usrp_voice(listeners.usrp_in)
        usrp_other = len(listeners.usrp_in) - len(usrp_rows)
    fold = [p for p in procs]
    try:
        after = rfpos1b.wait_settled(read, before, fold_procs=fold)
    except TimeoutError as exc:
        note(lab, case, "RECORD", "counters did not settle: %s" % exc)
        after = read()
    samples = hub()
    stamps = rfpos1c.edge_voice_stamps(sc.dir / "edge.pcap", ip, lab.port, call)
    usrp_times = rfpos1c.usrp_times(sc.dir / "edge.pcap", ip)
    time.sleep(GAP_S)
    return {"dstar_rows": dstar_rows, "dstar_repeats": dstar_repeats, "dmr_frames": dmr_frames, "usrp_rows": usrp_rows, "usrp_other": usrp_other,
            "usrp_times": usrp_times, "stamps": stamps, "change": rfpos1b.delta(after, before), "procs_seen": xstale1.procs_seen(samples),
            "rx_gap": {k: v - rx_before.get(k, 0) for k, v in rfpos1c.rx_gaps(samples).items()}, "voice_log": list(caller.voice_log)}


def judge(lab, case, frames, out, dropped, full=None):
    """All verdicts of one over: {owner: [sentences]}; the notes carry the numbers."""
    _ulaw, want = frames
    dropped_count = 0 if dropped is None else 1
    problems = {"harness": [], "usrp": [], "dstar": [], "dmr": [], "counters": []}
    holes, off = rfpos1c.holes(out["stamps"])
    want_holes = [] if dropped is None else [dropped]
    if holes != want_holes or off:
        problems["harness"].append("the edge's capture shows timestamp holes %s (off the 20 ms grid: %s), planned %s" % (holes, off[:3], want_holes))
    if len(out["stamps"]) != COUNT - dropped_count:
        problems["harness"].append("the capture holds %d voice frames of the call, planned %d" % (len(out["stamps"]), COUNT - dropped_count))
    sent_drops = [t for t, kind in out["voice_log"] if kind == "drop"]
    if len(sent_drops) != dropped_count or len(out["voice_log"]) != COUNT:
        problems["harness"].append("the tester logged %d frames of which %d dropped, planned %d of which %d" % (len(out["voice_log"]), len(sent_drops), COUNT, dropped_count))
    d_slots, _ = rfpos1c.dstar_slots(out["dstar_rows"], want["dstar"])
    m_slots, _ = rfpos1c.dmr_slots(out["dmr_frames"], want["dmr"])
    u_matched = rfpos1c.usrp_matched(out["usrp_rows"], want["usrp"])
    for name, slots in (("D-STAR", d_slots), ("DMR", m_slots), ("USRP", u_matched)):
        if not slots:
            problems["harness"].append("the %s listener heard nothing" % name)
    if out["dstar_repeats"]:
        problems["harness"].append("repeated D-STAR positions: %d" % out["dstar_repeats"])
    usrp_problems, usrp_facts = rfpos1c.judge_usrp(u_matched, out["usrp_times"], COUNT, dropped)
    problems["harness"] += [p for p in usrp_problems if p.startswith("harness:")]
    problems["usrp"] = [p for p in usrp_problems if not p.startswith("harness:")]
    problems["dstar"] = rfpos1c.judge_slots("D-STAR", d_slots, COUNT, dropped)
    problems["dmr"] = rfpos1c.judge_slots("DMR", m_slots, COUNT, dropped, filled=True)
    procs = [p for p in rfpos1c.EGRESS.values()]
    c_problems, c_findings, facts = rfpos1c.judge_counters(out["change"], dropped_count, procs)
    problems["counters"] = c_problems
    absent = {n: [p for p in range(COUNT) if p not in s] for n, s in (("dstar", d_slots), ("dmr", m_slots))}
    absent["usrp"] = [i for i in range(COUNT) if i not in set(u_matched)]
    note(lab, case, "RECORD", "dropped index %s (full frame at %s); the edge's capture: %d voice frames, timestamp holes %s, off grid %d" % (
        dropped, full, len(out["stamps"]), holes, len(off)))
    note(lab, case, "RECORD", "D-STAR: %d frames, missing positions %s, %s" % (len(d_slots), absent["dstar"][:8], rfpos1c.runs(d_slots)))
    note(lab, case, "RECORD", "DMR: %d frames, slots missing %s, %s" % (len(m_slots), absent["dmr"][:8], rfpos1c.runs(m_slots)))
    note(lab, case, "RECORD", "USRP: %d keyed packets (+%d other), frames not heard %s, arrival order %s; seq %s..%s (the edge's running counter); %s" % (
        len(out["usrp_rows"]), out["usrp_other"], absent["usrp"][:8], rfpos1c.runs(dict(enumerate(u_matched))),
        out["usrp_rows"][0][0] if out["usrp_rows"] else None, out["usrp_rows"][-1][0] if out["usrp_rows"] else None, usrp_facts))
    note(lab, case, "RECORD", "counter delta: %s; ingress gap by proc %s; procs with over series %s" % (facts, out["rx_gap"], out["procs_seen"]))
    for line in c_findings:
        note(lab, case, "FINDING", line)
    for owner, lines in problems.items():
        for line in lines:
            note(lab, case, "FINDING", "%s: %s" % (owner, line))
    save(lab, case, {
        "dropped_index": dropped, "full_frame_index": full, "count": COUNT,
        "capture_voice_frames": len(out["stamps"]), "capture_timestamp_holes": holes,
        "dstar": {"frames": len(d_slots), "missing_positions": absent["dstar"], "runs": rfpos1c.runs(d_slots)},
        "dmr": {"frames": len(m_slots), "missing_slots": absent["dmr"], "runs": rfpos1c.runs(m_slots), "fill_at_dropped": (
            None if dropped is None or dropped not in m_slots else m_slots[dropped])},
        "usrp": {"packets": len(out["usrp_rows"]), "frames_not_heard": absent["usrp"], "arrival_order": rfpos1c.runs(dict(enumerate(u_matched))), **usrp_facts,
                 "seq_first": out["usrp_rows"][0][0] if out["usrp_rows"] else None, "seq_last": out["usrp_rows"][-1][0] if out["usrp_rows"] else None},
        "counters": facts, "ingress_gap_by_proc": out["rx_gap"], "problems": problems})
    return problems


def check(problems):
    flat = ["%s: %s" % (owner, line) for owner, lines in problems.items() for line in lines]
    assert not flat, "; ".join(flat)


def test_rfpos1c_a_no_loss_keeps_every_position(lab, hub, frames):
    """Control: an over without loss. Every position holds its own frame at the D-STAR, DMR and USRP listeners; nothing is gap, lost or lossy."""
    out = run_over(lab, hub, frames, "a")
    check(judge(lab, "a", frames, out, None))


def test_rfpos1c_b_one_lost_mini_frame_is_one_missing_position(lab, hub, frames):
    """One IAX2 mini frame (position 40) never reaches the edge: the listeners miss exactly that position, every other frame stays at its own position,
    the room counts one gap and one lossy over. Before step C: no gap, one frame short, every later frame shifted, lost 0."""
    out = run_over(lab, hub, frames, "b", drop=DROP)
    check(judge(lab, "b", frames, out, DROP))


def test_rfpos1c_c_one_lost_full_frame_is_one_missing_position(lab, hub, frames):
    """The same with a full voice frame lost (position 40 is a full frame of the call)."""
    out = run_over(lab, hub, frames, "c", drop=DROP, full=rfpos1c.FULL_AT)
    check(judge(lab, "c", frames, out, DROP, full=rfpos1c.FULL_AT))


def test_rfpos1c_d_control_full_frame_mid_over_without_loss(lab, hub, frames):
    """Control for c: position 40 is a full voice frame and arrives; nothing is lost."""
    out = run_over(lab, hub, frames, "d", drop=None, full=rfpos1c.FULL_AT)
    check(judge(lab, "d", frames, out, None, full=rfpos1c.FULL_AT))
