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
from dvtest.proto import dmr, nxdn, p25, usrp, ysf

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
    """{position: matched index or None} of the DMR frames in a flat list (the position is the index; a burst the edge did not send shifts everything
    after it, so a case uses dmr_burst_slots instead)."""
    return _slots(list(enumerate(frames)), want)


def dmr_bursts(datagrams):
    """({burst index: [3 frames]}, [DMRD seq of each voice burst in arrival order]) of a DMR listener's datagrams. The burst index is the position of a burst
    in the over from its voice sequence letter (flags byte, low nibble: A..F = 0..5): a letter that does not rise starts the next superframe, so a burst
    the edge did not send is a hole in the indexes (and in the letters), not a shift of the later bursts. Placing by arrival order would call that a
    shift: the wip-e9fcc38 run 20261123T153031Z was judged that way first and misread a position kept at burst granularity as a compaction."""
    out, seqs = {}, []
    superframe, prev = 0, None
    for data in datagrams:
        if not dmr.is_voice(data):
            continue
        letter = data[15] & 0x0F
        if prev is not None and letter <= prev:
            superframe += 1
        prev = letter
        out[superframe * 6 + letter] = dmr.extract(data[20:53])
        seqs.append(data[4])
    return out, seqs


def dmr_burst_slots(bursts, want):
    """{position: matched index or None} with position = burst index * 3 + frame in the burst (a burst not sent leaves its three positions absent)."""
    return _slots([(b * 3 + i, f) for b, trio in sorted(bursts.items()) for i, f in enumerate(trio)], want)


def seq_gaps(seqs):
    """The DMRD sequence bytes that skip a number (mod 256): [(after, next)]. Empty for a contiguous run."""
    return [(a, b) for a, b in zip(seqs, seqs[1:]) if (b - a) & 0xFF != 1]


def judge_native(name, slots, count=COUNT, dropped=None, per=3):
    """A listener whose native frame packs `per` 20 ms units (DMR burst 3, YSF frame 5, NXDN frame 4, P25 LDU 9): positions = native frame * per + unit,
    judged like judge_slots(filled=True) (the lost unit's slot exists and carries a fill that is not its own frame, every other slot holds its own frame)
    with one named partial result: when the output lacks exactly the `per` positions of the lost unit's native frame and every other position holds its
    own frame, the edge dropped the WHOLE native frame (the survivors of it are not carried) but kept the later ones at their positions. dv-router's
    expectation (BUNDLEFILL1): the native frame is still sent at its own position and time, the missing unit filled."""
    if dropped is not None and slots:
        first = dropped - dropped % per
        native = list(range(first, first + per))
        absent = [p for p in range(count) if p not in slots]
        rest = [p for p in range(count) if p in slots and slots[p] != p]
        if absent == native and not rest:
            others = [p for p in native if p != dropped]
            return ["%s: the whole native frame of the lost unit (positions %s) was not sent: units %s are not carried; every other native frame holds its own "
                    "units (positions kept at native frame granularity, not compacted)" % (name, native, others)]
        if absent == [dropped] and not rest and per > 1:
            return ["%s: the lost unit's slot (position %d) was not sent at all (no fill, the unit is simply absent); every other position holds its own unit" % (
                name, dropped)]
    return judge_slots(name, slots, count, dropped, filled=True)


def judge_dmr(slots, count=COUNT, dropped=None):
    """The DMR listener (burst of 3 units); see judge_native."""
    return judge_native("DMR", slots, count, dropped, 3)


# ---------------------------------------------------------------- the bundled egresses (BUNDLEFILL1: DMR 3, YSF 5, NXDN 4, P25 LDU 9 units per native frame)

BUNDLE_ENV = "DVT_BUNDLEFILL1"
# per listener kind: units per native frame, the proc label of its egress edge in the hub counters, the port of the SUT server, the time step of one position of the
# counter (ms): a YSF frame 100, an NXDN frame 80, a P25 record 20 (one unit; its LDU of 9 is the native frame)
BUNDLED = {"ysf": {"name": "YSF", "per": 5, "proc": "ysf", "port": 42000, "native_ms": 100},
           "p25": {"name": "P25", "per": 9, "proc": "p25", "port": 41000, "native_ms": 20},
           "nxdn": {"name": "NXDN", "per": 4, "proc": "nxdn", "port": 41400, "native_ms": 80}}
DMR_NATIVE_MS = 60
P25_TG = 31672                      # the talkgroup of the P25 and NXDN rows of bundlefill1-edges.toml (module L), as soak-edges.toml's
TIMING_SLACK_MS = 40.0              # the arrival time of a native frame may differ this much from the control's deviation (or from its own position for DMR)
YSF_FN_MODULUS = 7
P25_RECORDS = 18                    # LDU1 0x62..0x6A then LDU2 0x6B..0x73


def bundlefill_on(environ=None):
    """DVT_BUNDLEFILL1=on: the image has BUNDLEFILL1 and the rig serves the P25, YSF and NXDN listeners of module L (bundlefill1-edges.toml); the cases then
    run those listeners and assert them. Unset, they do not run at all."""
    return str((environ if environ is not None else os.environ).get(BUNDLE_ENV, "")).strip().lower() in ("on", "1", "true", "yes")


def cyclic_positions(values, modulus, descending=False):
    """Position of each native frame from a counter that cycles modulo `modulus` (YSF FN 0..6 ascending, P25 record type index 0..17 ascending, NXDN SACCH
    structure 3..0 descending): a value that does not advance starts the next cycle, so a native frame the edge did not send is a hole in the positions,
    not a shift of the later ones. Relative to the first (position 0)."""
    out, cycle, prev = [], 0, None
    for v in values:
        w = (modulus - 1 - v) if descending else v
        if prev is not None and w <= prev:
            cycle += 1
        prev = w
        out.append(cycle * modulus + w)
    base = out[0] if out else 0
    return [x - base for x in out]


def native_rows(kind, datagrams):
    """[(counter value, [unit frames], datagram)] of the voice datagrams of `kind` ("ysf", "p25", "nxdn"), in arrival order."""
    rows = []
    for data in datagrams:
        data = bytes(data)
        if kind == "ysf":
            info = ysf.parse(data)
            if info is not None and info["kind"] == "voice":
                rows.append((info["fich"]["fn"], [bytes(f) for f in info["frames"]], data))
        elif kind == "p25":
            info = p25.parse(data)
            if info is not None and not info["terminator"] and "imbe" in info:
                rows.append((info["type"] - 0x62, [bytes(info["imbe"])], data))
        elif kind == "nxdn":
            info = nxdn.parse(data)
            if info is not None and info["kind"] == "voice":
                rows.append((nxdn.parse_sacch(info["payload"][1:5])["structure"], [bytes(f) for f in nxdn.voice_frames(info["payload"])], data))
        else:
            raise ValueError(kind)
    return rows


def native_units(kind, datagrams):
    """({unit position: frame bytes}, [native frame position of each voice datagram]) of a listener of `kind`. A P25 record is one unit; P25 positions come
    from the record type (0x62..0x73) cycle, YSF from FN modulo 7, NXDN from the SACCH structure (3, 2, 1, 0)."""
    rows = native_rows(kind, datagrams)
    modulus = {"ysf": YSF_FN_MODULUS, "p25": P25_RECORDS, "nxdn": 4}[kind]
    positions = cyclic_positions([r[0] for r in rows], modulus, descending=(kind == "nxdn"))
    units = {}
    for pos, (_v, frames, _d) in zip(positions, rows):
        for i, frame in enumerate(frames):
            units[pos if kind == "p25" else pos * len(frames) + i] = frame
    return units, positions


def dmr_native_positions(datagrams):
    """The burst index of each DMR voice datagram (what dmr_bursts uses), in arrival order."""
    out, superframe, prev = [], 0, None
    for data in datagrams:
        if not dmr.is_voice(data):
            continue
        letter = data[15] & 0x0F
        if prev is not None and letter <= prev:
            superframe += 1
        prev = letter
        out.append(superframe * 6 + letter)
    return out


def reference(units, count=COUNT):
    """The control's frame of each position as a list, or None when a position is missing or two positions hold the same frame (then a loss case can
    only judge the structure). The loss cases of the YSF, P25 and NXDN listeners compare with it: the same seeded PCM goes through the same codec
    path, so the frame the control received at position p is what p must hold in a loss case (no assumption on gain bits or codec details)."""
    if any(p not in units for p in range(count)):
        return None
    frames = [units[p] for p in range(count)]
    return frames if len(set(frames)) == count else None


def native_slots(units, ref):
    """{position: matched reference index or None} of a listener's units against the control's reference list."""
    return _slots(sorted(units.items()), ref)[0]


def native_times(path, edge_ip, port, kind):
    """Send times (s) of the voice datagrams of `kind` ("dmr" too) the edge sent to local port `port` of the tester in its tap capture, in order."""
    from dvtest import pcap
    out = []
    for dg in pcap.udp_datagrams(path):
        if dg.src != edge_ip or dg.dport != port:
            continue
        if kind == "dmr":
            if not dmr.is_voice(dg.payload):
                continue
        elif not native_rows(kind, [dg.payload]):
            continue
        out.append(dg.ts)
    return out


def timing_dev_ms(times, positions, native_ms):
    """Largest |(t_i - t_0) - (pos_i - pos_0) * native_ms| in ms over the native frames sent (None for fewer than two or when the counts disagree): how
    far a native frame is from its own position in time. A frame sent at its position and time keeps this near 0 whatever the hole before it."""
    if len(times) != len(positions) or len(times) < 2:
        return None
    return round(max(abs((t - times[0]) * 1000.0 - (p - positions[0]) * native_ms) for t, p in zip(times, positions)), 1)


# ---------------------------------------------------------------- lateness against the control (dv-router's contract for the bundled egresses)
#
# The AllStar edge holds the unit after a lost one until its 60 ms late window gives up on the lost unit, so the native frame that carries the fill cannot leave
# earlier than 60 ms + one unit (20 ms) after its position; the frames after it recover. Per bundled protocol (DMR burst, YSF frame, NXDN frame, P25 LDU), each
# measured as the send time of the native frame (a P25 LDU: its latest record) relative to the first datagram of the over, minus the same time in the control:
#   (1) the native frame that carries the fill is at most FILL_MAX_MS over the control
#   (2) the next native frame is back within RECOVER_MAX_MS of the control
#   (3) every later native frame up to the end of the over is within RECOVER_MAX_MS of the control
FILL_MAX_MS = 80.0                  # 60 ms late window + one unit of 20 ms
RECOVER_MAX_MS = 20.0
DMR_SPORT = 62030                   # the DMR edge's source port in the tap capture (the server port of the other three is BUNDLED[kind]["port"])
P25_LDU = 9                         # records of a P25 LDU: the native frame of the P25 listener, whose positions are single records


def offsets(times, positions):
    """{position: ms after the first datagram of the over} of the datagrams of one listener (the first datagram of a position wins). Empty when the counts disagree."""
    if len(times) != len(positions) or not times:
        return {}
    out = {}
    for t, p in zip(times, positions):
        out.setdefault(p, (t - times[0]) * 1000.0)
    return out


def lateness(control, loss, kind, dropped=DROP, count=COUNT):
    """How late each native frame of a loss over is against the control's, as numbers (ms, None when not measurable): `fill_ms` the native frame that carries the
    lost unit, `next_ms` the next native frame, `after_max_ms` the worst of every later one up to the end of the over. `control` and `loss` are offsets() of the
    same listener. A P25 position is one record (one unit) and its native frame is the LDU of 9 records, valued by its latest record; for DMR, YSF and NXDN a
    position is the native frame itself (`per` units)."""
    if kind == "p25":
        units, frame_of = P25_LDU, lambda pos: pos // P25_LDU
    else:
        units = BUNDLED[kind]["per"] if kind in BUNDLED else 3
        frame_of = lambda pos: pos                                  # noqa: E731
    last = (count - 1) // units
    worst = {}
    for pos, t in loss.items():
        frame = frame_of(pos)
        if pos in control and frame <= last:
            worst[frame] = max(worst.get(frame, float("-inf")), t - control[pos])
    fill = dropped // units
    later = [v for frame, v in worst.items() if frame > fill + 1]

    def rounded(v):
        return None if v is None else round(v, 1)

    return {"fill_ms": rounded(worst.get(fill)), "next_ms": rounded(worst.get(fill + 1)), "after_max_ms": rounded(max(later)) if later else None,
            "native_frames": len(worst)}


def judge_lateness(name, numbers):
    """Judge lateness(): the fill frame at most FILL_MAX_MS over the control, the next frame and every later one within RECOVER_MAX_MS. More than that is a
    defect (a lasting drift is one even when the fill frame is on time). A list of sentences."""
    out = []
    if numbers["fill_ms"] is None:
        return ["%s: no timing of the native frame that carries the lost unit (not sent, or the capture and the listener disagree)" % name]
    if numbers["fill_ms"] > FILL_MAX_MS:
        out.append("%s: the native frame that carries the fill is %.1f ms over the control, more than %.0f ms (60 ms late window + one unit)" % (name, numbers["fill_ms"], FILL_MAX_MS))
    if numbers["next_ms"] is None:
        out.append("%s: no native frame after the one that carries the fill" % name)
    elif numbers["next_ms"] > RECOVER_MAX_MS:
        out.append("%s: the next native frame is %.1f ms over the control, more than %.0f ms: no recovery" % (name, numbers["next_ms"], RECOVER_MAX_MS))
    if numbers["after_max_ms"] is not None and numbers["after_max_ms"] > RECOVER_MAX_MS:
        out.append("%s: a later native frame is %.1f ms over the control (more than %.0f ms): a lasting drift" % (name, numbers["after_max_ms"], RECOVER_MAX_MS))
    return out


def capture_offsets(path, edge_ip, kind):
    """offsets() of the voice datagrams of `kind` ("dmr", "ysf", "p25", "nxdn") the edge sent in its tap capture `path` (the edge's source port names the protocol)."""
    from dvtest import pcap
    sport = DMR_SPORT if kind == "dmr" else BUNDLED[kind]["port"]
    datagrams = [(dg.ts, bytes(dg.payload)) for dg in pcap.udp_datagrams(path) if dg.src == edge_ip and dg.sport == sport]
    if kind == "dmr":
        voice = [(t, d) for t, d in datagrams if dmr.is_voice(d)]
        return offsets([t for t, _d in voice], dmr_native_positions([d for _t, d in voice]))
    voice = [(t, d) for t, d in datagrams if native_rows(kind, [d])]
    return offsets([t for t, _d in voice], native_units(kind, [d for _t, d in voice])[1])


def rejudge_run(run_dir, edge_ip="172.30.0.11", loss=("b", "c"), control="a", noise="d"):
    """Re-judge the timing of a run directory of cases/test_rfpos1c.py from its tap captures (asl-lab/rfpos1c-<case>/edge.pcap): per case in `loss` and per protocol
    the lateness() numbers against the control `control` with their verdict, and for the second control `noise` the same numbers (the measuring noise: it has no
    lost unit, the numbers are taken at the same positions). Returns {case: {kind: {numbers..., "problems": [...]}}}; a protocol with no voice in the
    control is left out."""
    from pathlib import Path
    root = Path(run_dir) / "asl-lab"
    kinds = ["dmr"] + list(BUNDLED)
    ctrl = {k: capture_offsets(root / ("rfpos1c-%s" % control) / "edge.pcap", edge_ip, k) for k in kinds}
    out = {}
    for case in tuple(loss) + (noise,):
        path = root / ("rfpos1c-%s" % case) / "edge.pcap"
        if not path.exists():
            continue
        out[case] = {}
        for kind in kinds:
            if not ctrl[kind]:
                continue
            numbers = lateness(ctrl[kind], capture_offsets(path, edge_ip, kind), kind)
            numbers["problems"] = [] if case == noise else judge_lateness(BUNDLED[kind]["name"] if kind in BUNDLED else "DMR", numbers)
            out[case][kind] = numbers
    return out


def main(argv=None):
    import json
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python3 -m dvtest.rfpos1c <out/run dir> [edge ip]  (re-judge the timing of cases b and c of a test_rfpos1c.py run from its tap captures)")
        return 2
    result = rejudge_run(argv[0], argv[1] if len(argv) > 1 else "172.30.0.11")
    print(json.dumps(result, indent=1, sort_keys=True))
    return 1 if any(n["problems"] for kinds in result.values() for n in kinds.values()) else 0


def judge_timing(name, dev_ms, control_ms=0.0, slack=TIMING_SLACK_MS):
    """Timing verdict: `dev_ms` (timing_dev_ms) must not exceed `control_ms` (the control's own deviation; 0 for DMR) by more than `slack`. A list."""
    if dev_ms is None:
        return ["%s: no timing (the capture and the listener disagree on the native frames sent)" % name]
    if dev_ms > control_ms + slack:
        return ["%s: a native frame arrives %.0f ms from its own position in time (control %.0f ms, slack %.0f ms)" % (name, dev_ms, control_ms, slack)]
    return []


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
    if wrong == [dropped] and slots[dropped] is not None:
        out.append("%s: the lost unit's slot (position %d) holds the frame of position %d, a copy of another unit and not a fill; every other position holds its own frame" % (
            name, dropped, slots[dropped]))
    elif wrong:
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


def judge_counters(change, dropped_count, procs, strict_lost=()):
    """Judge a rfpos1b.Reading delta of one over with `dropped_count` frames lost on the IAX2 side (0: a control, 1: the loss case). Returns (problems,
    findings, facts).

    Room side (step C does not change it; the same on wip-3f142cd): with one lost frame the ingress edge counts gap +1, the room gap +1, lost +0 and the over
    `lossy` +1; with none all of them stay 0. Egress side (dv-router, since MEDLOAD1b an egress counts only loss on ITS OWN leg: a hole the room already
    published as an upstream gap is upstream loss): `tx lost` rises by 0 at every edge in `procs`, with or without loss. A rise is a FINDING, not asserted.
    Every egress edge folded the over (else a harness slip); `dropped` rises by 0 everywhere (BUNDLEFILL1: a bundled egress fills a missing unit and sends
    the native frame; wip-e9fcc38's dmrhb dropped +2 for the two survivors of the burst); the procs in `strict_lost` (the bundled YSF / P25 / NXDN
    listeners of DVT_BUNDLEFILL1) also hold `tx lost` at 0 as a problem; `late` is a finding."""
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
        if c.lost and proc in strict_lost:
            problems.append("dv_edge_over_units_total{dir=tx,proc=%s,result=lost} rose by %g, wanted 0 (the hole is upstream loss)" % (proc, c.lost))
        elif c.lost:
            findings.append("dv_edge_over_units_total{dir=tx,proc=%s,result=lost} rose by %g (dv-router: +0, the hole is upstream loss; recorded, not asserted)" % (proc, c.lost))
        if c.dropped:
            problems.append("dv_edge_over_units_total{dir=tx,proc=%s,result=dropped} rose by %g, wanted 0 (a native frame with a missing unit is filled and sent, not dropped)" % (proc, c.dropped))
        if c.late:
            findings.append("egress edge %s counted late %g" % (proc, c.late))
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


if __name__ == "__main__":
    raise SystemExit(main())
