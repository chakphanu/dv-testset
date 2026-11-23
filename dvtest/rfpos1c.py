# SPDX-License-Identifier: MIT
"""RFPOS1 step C (DV-Router fb3167bb, "AllStar streams state frame positions through the transcoder"; docs/rfpos1c-cases.md): the AllStar edge
(dv-edge-allstar, IAX2) rechunks timestamped IAX2 audio into one 320 B PCM unit per 20 ms and states RTS_FRAME_POSITIONS, so the room's transcoded
output keeps the position of every unit, gaps included: one IAX2 voice frame lost on the way is ONE missing position at the listeners (D-STAR: a
skipped counter; DMR: the lost frame inside its burst, the burst still carries the other two; USRP PCM: a skipped position), not a compaction
(the old output was one frame short and every later frame shifted by one). The first received packet of an over is position 0.

How the case loses exactly one frame: the tester is the IAX2 caller (dvtest.proto.iax2.Client, account "acct" of the edge, ulaw) and does not send
the frame (Client.voice(drop=True)): the call clock still advances by 20 ms, so the timestamps in the edge's capture have a hole of one position. The
tester therefore knows which index it dropped, and the edge's own tap capture proves that the hole arrived (`voice_stamps`, `holes`).

How audio identity is matched frame for frame: every IAX2 frame is 160 bytes of random mu-law (a distinct, seeded frame per position). The edge
decodes G.711 to 320 B of PCM, so the PCM at a USRP listener is `ulaw_to_pcm(frame)` byte for byte, and the fake transcoder (profile asl-edge forces
DVT_TRANSCODER_FAKE=1) turns that PCM into the D-STAR / DMR frame `ambed_ext.expected_output(PCM, codec, pcm)` (out[i] = in[i mod 320] ^ K ^ i, i.e. a
function of the first nine PCM bytes: distinct for distinct frames). No marker is needed: each received frame is looked up in the expected list and
the judge compares the position it arrived at with the index it came from.

Pure and stdlib only (cases/unit/test_rfpos1c_lib.py); the sockets and the rig are in cases/test_rfpos1c.py.
"""
import os
import random
import struct

from dvtest import asl_lab, crossmode, medload1, rfpos1, rfpos1b
from dvtest.proto import usrp

ENV = "DVT_RFPOS1C"
ROOM = "L"                          # room / module of the AllStar edge in the asl-lab (rig/config/dv-router/allstar-lab.toml)
ROOM_LABEL = "l"                    # the room label in the hub counters
USRP_PEER_PORT = 32004              # the tester's side of the static USRP row of module L (rig/config/dv-router/rfpos1c-usrp.toml)
USRP_SUT_PORT = 34004
COUNT = 90                          # 20 ms frames of an over: 30 DMR bursts, 1.8 s (above the 50 frame short stream guard)
DROP = 40                           # the lost position: burst 13, unit 1 (the middle frame of a DMR burst)
FULL_AT = DROP                      # the position that is a full voice frame in the full-frame cases
PCM = 5                             # AMBEd codec number of the PCM profile
INGRESS_PROC = "allstar"            # the proc label of the AllStar edge in the hub counters (a guess: the rx series of every proc is recorded too)
EGRESS = {"dstar": "dextra", "dmr": "dmrhb", "usrp": "usrp"}     # the proc label of the edge that serves each listener
LOW16 = 1 << 16


def on(environ=None):
    """DVT_RFPOS1C=on: the image has RFPOS1 step C (DV-Router fb3167bb or later) and the cases run and assert; unset, every case skips naming the switch. Run
    on an older image with the switch set, the loss cases FAIL and the notes are the "before" (a compacted output)."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


# ---------------------------------------------------------------- the frames of an over

def make_frames(count=COUNT, seed="rfpos1c"):
    """(ulaw frames, pcm frames) of `count` positions: distinct seeded noise at a moderate level, mu-law coded; pcm[i] is exactly what a G.711 decoder
    makes of ulaw[i] (320 B, S16LE)."""
    rng = random.Random(seed)
    ulaw, pcm = [], []
    for _ in range(count):
        raw = b"".join(struct.pack("<h", rng.randint(-6000, 6000)) for _ in range(160))
        coded = asl_lab.ulaw_bytes(raw)
        ulaw.append(coded)
        pcm.append(asl_lab.ulaw_to_pcm(coded))
    return ulaw, pcm


def expected(pcm_frames):
    """The expected frame of every position at a listener of each kind: {"usrp": pcm, "dstar": 9 B, "dmr": 9 B} (the fake transcoder's transform)."""
    return {"usrp": [bytes(f) for f in pcm_frames],
            "dstar": crossmode.transform_units(PCM, crossmode.CODEC["dstar"], pcm_frames),
            "dmr": crossmode.transform_units(PCM, crossmode.CODEC["dmr"], pcm_frames)}


def distinct(frames):
    """True when no two frames are equal (a position can be identified by its content)."""
    return len(set(bytes(f) for f in frames)) == len(frames)


# ---------------------------------------------------------------- what the edge received (its tap capture)

def voice_stamps(payloads, call):
    """Unwrapped 32-bit timestamps (ms) of the voice frames of source call number `call` in `payloads` (UDP payloads of the capture, tester to edge),
    in capture order: a full voice frame (kind 2, subclass 4 = ulaw, or 6 = slin) gives its timestamp; a mini frame (F bit 0, source call, low 16 bits)
    is expanded against the last timestamp (16 bit wrap handled). A retransmitted full frame is counted once."""
    out = []
    last = None
    for data in payloads:
        if len(data) < 4:
            continue
        word = struct.unpack("!H", data[:2])[0]
        if word & 0x8000:
            if (word & 0x7fff) != call or len(data) < 12 or data[10] != 2:
                continue
            stamp = struct.unpack("!I", data[4:8])[0]
            if stamp in out[-3:]:
                continue
        else:
            if word != call:
                continue
            low = struct.unpack("!H", data[2:4])[0]
            if last is None:
                continue
            stamp = (last & ~(LOW16 - 1)) | low
            if stamp < last - LOW16 // 2:
                stamp += LOW16
            elif stamp > last + LOW16 // 2:
                stamp -= LOW16
        out.append(stamp)
        last = stamp
    return out


def holes(stamps, period=20):
    """Positions (stamp - first stamp) / period that are missing between the first and the last stamp, and the stamps that are not a multiple of
    `period` from the first (a clock the receiver cannot place): (missing positions, off-grid stamps)."""
    if not stamps:
        return [], []
    first = min(stamps)
    have = {(s - first) // period for s in stamps if (s - first) % period == 0}
    off = [s for s in stamps if (s - first) % period]
    top = max(have)
    return [p for p in range(top + 1) if p not in have], off


def edge_voice_stamps(path, edge_ip, port, call):
    """voice_stamps of the datagrams to `edge_ip`:`port` in the pcap `path`."""
    from dvtest import pcap
    return voice_stamps([dg.payload for dg in pcap.udp_datagrams(path) if dg.dst == edge_ip and dg.dport == port], call)


# ---------------------------------------------------------------- the listeners

def usrp_voice(datagrams):
    """[(seq, pcm320)] of the keyed voice packets of a USRP stream, in arrival order."""
    out = []
    for data in datagrams:
        if not usrp.is_voice(data):
            continue
        frame = usrp.parse(data)
        if frame.keyup == 1:
            out.append((frame.seq, bytes(frame.payload)))
    return out


def usrp_matched(rows, want):
    """The expected index of each keyed USRP packet, in arrival order (None: a packet of no expected position). The USRP `seq` is the edge's running packet
    counter (it continues across overs and has no hole), so a USRP listener sees a position only by its content and by its time."""
    index = {bytes(f): i for i, f in enumerate(want)}
    return [index.get(bytes(pcm)) for _seq, pcm in rows]


def usrp_times(path, edge_ip, peer_port=USRP_PEER_PORT):
    """Send times (s, capture clock) of the keyed USRP voice packets the edge sent to the tester's peer port, in the edge's tap capture `path`."""
    from dvtest import pcap
    out = []
    for dg in pcap.udp_datagrams(path):
        if dg.src == edge_ip and dg.dport == peer_port and usrp.is_voice(dg.payload) and usrp.parse(dg.payload).keyup == 1:
            out.append(dg.ts)
    return out


HOLE_GAP_S = 0.035                  # a lost 20 ms position keeps (at least) 1.75 ticks between its neighbours; a closed-up output has 1 tick
CONTROL_GAP_S = 0.060               # no gap this long in an over without loss


def judge_usrp(matched, times, count=COUNT, dropped=None, tick=0.020):
    """The PCM control listener (a static USRP row of the room): every position other than `dropped` once, at its own content, in order (a shifted or
    compacted stream has other content at a position, which cannot happen here: the PCM units are the source units), and the lost position kept IN TIME (the edge's 20 ms spacing plus its 60 ms late window, about 80 ms, is expected; only >= 35 ms is asserted):
    the gap between the packets of the frames next to the lost one is at least HOLE_GAP_S (the compacted output has one tick), and with no loss no gap
    is as long as CONTROL_GAP_S. Returns (problems, facts)."""
    out = []
    want = [i for i in range(count) if i != dropped]
    if matched != want:
        got = {n: m for n, m in enumerate(matched)}
        out.append("USRP: the packets hold %d frames in arrival order, wanted %d (every position but %s once, in order): %s" % (
            len(matched), len(want), dropped, runs(got) or "nothing heard"))
    facts = {"packets": len(matched), "times": len(times)}
    if len(times) != len(matched):
        out.append("harness: the edge's capture holds %d keyed USRP packets, the listener got %d" % (len(times), len(matched)))
        return out, facts
    gaps = [b - a for a, b in zip(times, times[1:])]
    facts["max_gap_ms"] = round(max(gaps) * 1000, 1) if gaps else None
    facts["span_ms"] = round((times[-1] - times[0]) * 1000, 1) if times else None
    if dropped is None:
        if gaps and max(gaps) >= CONTROL_GAP_S:
            out.append("USRP: a gap of %.0f ms in an over without loss" % (max(gaps) * 1000))
    elif 0 < dropped and dropped - 1 in matched and matched.index(dropped - 1) + 1 < len(times):
        j = matched.index(dropped - 1)
        hole = times[j + 1] - times[j]
        facts["hole_gap_ms"] = round(hole * 1000, 1)
        if hole < HOLE_GAP_S:
            out.append("USRP: %.0f ms between the packets of frames %d and %d, the lost position is closed up (want at least %.0f ms)" % (
                hole * 1000, dropped - 1, matched[j + 1] if matched[j + 1] is not None else -1, HOLE_GAP_S * 1000))
    return out, facts


def dstar_slots(rows, want):
    """{position: matched index or None} of D-STAR frames [(position, frame9)] (rfpos1.dstar_voice)."""
    return _slots(rows, want)


def dmr_slots(frames, want):
    """{position: matched index or None} of the DMR frames in arrival order (a flat list: the position is the index, a lost frame is a filled slot)."""
    return _slots(list(enumerate(frames)), want)


def _slots(rows, want):
    index = {bytes(f): i for i, f in enumerate(want)}
    slots, repeats = {}, 0
    for pos, frame in rows:
        if pos in slots:
            repeats += 1
            continue
        slots[pos] = index.get(bytes(frame))
    return slots, repeats


def runs(slots):
    """The slots as a short text of runs: "0-39 hold 0-39, 40-88 hold 41-89" (position range, matched index range; "?" for a frame of no expected
    position), for the notes."""
    out = []
    start = None
    for pos in sorted(slots):
        idx = slots[pos]
        if start is not None and pos == prev_pos + 1 and ((idx is None and prev_idx is None) or (idx is not None and prev_idx is not None and idx == prev_idx + 1)):
            prev_pos, prev_idx = pos, idx
            continue
        if start is not None:
            out.append(_run(start, prev_pos, start_idx, prev_idx))
        start, start_idx, prev_pos, prev_idx = pos, idx, pos, idx
    if start is not None:
        out.append(_run(start, prev_pos, start_idx, prev_idx))
    return ", ".join(out)


def _run(a, b, i, j):
    span = "%d" % a if a == b else "%d-%d" % (a, b)
    held = "?" if i is None else ("%d" % i if i == j else "%d-%d" % (i, j))
    return "%s hold %s" % (span, held)


def judge_slots(name, slots, count=COUNT, dropped=None, filled=False):
    """What a listener must show for an over of `count` positions with `dropped` lost (None: no loss). `slots` {position: matched index}: every position
    other than `dropped` holds the frame of that index (a compacted or shifted stream does not), the dropped position is absent (`filled`=True: a DMR
    burst always has three frames, the slot exists and must NOT hold the lost frame), and the output reaches the last position. A list of sentences."""
    out = []
    if not slots:
        return ["%s: nothing heard" % name]
    absent = [p for p in range(count) if p not in slots]
    want_absent = [] if dropped is None or filled else [dropped]
    if absent != want_absent:
        out.append("%s: missing positions %s, wanted %s (lost count %d, wanted %d)" % (name, absent[:10], want_absent, len(absent), len(want_absent)))
    wrong = [p for p in range(count) if p in slots and ((p != dropped and slots[p] != p) or (p == dropped and slots[p] not in (None, p)))]
    if wrong:
        out.append("%s: %d positions do not hold their own frame, first position %d holds %s (the lost frame is closed up: later frames moved)" % (
            name, len(wrong), wrong[0], "frame %d" % slots[wrong[0]] if slots[wrong[0]] is not None else "a frame of no position"))
    if dropped is not None and slots.get(dropped) == dropped:
        out.append("%s: the lost frame %d was heard" % (name, dropped))
    top = max((p for p in slots if p < count), default=-1)
    if top < count - 1 and dropped != count - 1:
        out.append("%s: the output ends at position %d, short of the last (%d)" % (name, top, count - 1))
    return out


# ---------------------------------------------------------------- the hub counters

def egress_procs(seen):
    """(wanted procs present, procs missing) of EGRESS in the proc labels `seen`: dextra and dmrhb are required, usrp is judged only when its series exists."""
    have = [p for p in EGRESS.values() if p in seen]
    return have, [p for p in ("dextra", "dmrhb") if p not in seen]


def judge_counters(change, dropped_count, procs):
    """Judge a rfpos1b.Reading delta of one over with `dropped_count` frames lost on the IAX2 side (0: a control, 1: the loss case). Returns (problems,
    findings, facts).

    Room side (step C does not change it; the same on wip-3f142cd): with one lost frame the ingress edge counts gap +1, the room gap +1, lost +0 and the over
    `lossy` +1; with none all of them stay 0. Egress side (dv-router, since MEDLOAD1b an egress counts only loss on ITS OWN leg: a hole the room already
    published as an upstream gap is upstream loss): `tx lost` rises by 0 at every edge in `procs`, with or without loss. A rise is a FINDING, not asserted.
    Every egress edge folded the over (else a harness slip); late / dropped are findings."""
    problems, findings = [], []
    room = change.room
    facts = {"room": dict(room._asdict()), "egress": {proc: dict(c._asdict()) for proc, c in change.egress}}
    if medload1.overs_total(room) < 1:
        problems.append("harness: no over closed in room %s (dv_room_overs_total did not move)" % ROOM_LABEL)
    want = {"edge_gap": dropped_count, "room_gap": dropped_count, "room_lost": 0, "overs_lossy": 1 if dropped_count else 0}
    names = {"edge_gap": "dv_edge_over_units_total{dir=rx,proc=%s,result=gap}" % INGRESS_PROC, "room_gap": "dv_room_over_units_total{room=%s,result=gap}" % ROOM_LABEL,
             "room_lost": "dv_room_over_units_total{room=%s,result=lost}" % ROOM_LABEL, "overs_lossy": "dv_room_overs_total{room=%s,result=lossy}" % ROOM_LABEL}
    for field, value in want.items():
        got = getattr(room, field)
        if got != value:
            problems.append("%s rose by %g, wanted %d (%s)" % (names[field], got, value, "one frame lost on the IAX2 side" if dropped_count else "an over without loss"))
    for proc, c in change.egress:
        folds = c.complete + c.lossy + c.end_missing + c.joined
        if folds < 1:
            problems.append("harness: egress edge %s folded no over: its listener did not take the stream" % proc)
        if c.lost:
            findings.append("dv_edge_over_units_total{dir=tx,proc=%s,result=lost} rose by %g (dv-router: +0, the hole is upstream loss; recorded, not asserted)" % (proc, c.lost))
        if c.late or c.dropped:
            findings.append("egress edge %s counted late %g, dropped %g" % (proc, c.late, c.dropped))
    return problems, findings, facts


def rx_gaps(samples):
    """{proc: value} of dv_edge_over_units_total{dir=rx,result=gap} by edge process (the ingress edge's label is not in a document)."""
    out = {}
    for s in samples:
        if s.name == medload1.EDGE_UNITS and s.labels.get("dir") == "rx" and s.labels.get("result") == "gap":
            out[s.labels.get("proc", "-")] = out.get(s.labels.get("proc", "-"), 0) + s.value
    return out


def dstar_rows(datagrams):
    """[(position, frame9)] of a D-STAR listener's inbox (rfpos1.dstar_voice, counters unwrapped) and the repeat count."""
    return rfpos1.dstar_voice(list(datagrams))
