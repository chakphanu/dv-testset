# SPDX-License-Identifier: MIT
"""Byte-exact expectations for cross-mode traffic through a system under test
whose transcoder is the fake extended AMBEd (dvtest/fake_ambed.py).

Pure functions, no network. They turn what a rig client sends into the codec
units the system under test ingests, apply the fake transform per listener
codec and fold the result into what a listener of a given protocol receives.
Every rule below is taken from DV-Router docs/design/media-format.md
("Unit granularity", "End unit", "Cross-mode egress", "Expected counts", "USRP")
and transcoding.md ("Controller transcoding"), as of XC1 (DV-Router 2c937c3):
a transcode session opens on the first voice unit, a stream without voice
(header only, or header and end unit) opens none, and a USRP listener gets
exactly the voice PCM units, then one unkey (no synthetic PCM end unit).

Kinds: dstar (DExtra), dplus, dcs (codec 1), dmr (codec 2), m17 (codec 3),
usrp (codec 5, PCM). The end unit is the unit the source edge marks with the
end hint: D-STAR last frame AMBE, DMR terminator (9 zero bytes), the second
half of the M17 EOT packet; USRP has none (unkey).
"""
from collections import namedtuple

from dvtest.proto import ambed_ext as ext

CODEC = {"dstar": 1, "dplus": 1, "dcs": 1, "dmr": 2, "m17": 3, "p25": 4, "usrp": 5}
DSTAR_KINDS = ("dstar", "dplus", "dcs")

# Codec2 silence of an M17 EOT packet (media-format.md, M17 egress)
M17_SILENCE = bytes.fromhex("c0006a439ce42108")
# D-STAR cross-mode slow data: sync at superframe position 0, filler elsewhere
# AMBE bytes of the DPlus client's 32 B last frame (dvtest.proto.dplus.END_PATTERN[:9])
DPLUS_END_AMBE = bytes.fromhex("55c87a555555555555")
DSTAR_SYNC = bytes.fromhex("552d16")
DSTAR_FILLER = bytes.fromhex("1629f5")

Source = namedtuple("Source", "voice end")
# frames: the units a listener receives, the whole list (count and order) is
# documented; note: the rule that shaped the tail
Expect = namedtuple("Expect", "frames note")


def codec_of(kind):
    return CODEC[kind]


def source_units(kind, frames, m17_pairs_only=False):
    """What the source edge ingests when a rig client of `kind` talks `frames`
    with the repository's client talk(): Source(voice units, end unit or None).

    dstar family: the frames, then the last frame whose AMBE is 9 zero bytes
    (the clients' last frame). dmr: the frames padded with zero units to whole
    bursts of three, end unit 9 zero bytes (terminator). m17: packet 0 repeats
    the first frame and is not ingested; the pairs follow (an odd last frame is
    repeated), the EOT packet repeats the last pair: its first unit is normal
    voice, its second the end unit. usrp: the PCM frames, no end unit.
    """
    frames = [bytes(f) for f in frames]
    if kind == "dplus":
        # the 32 B DPlus last frame carries the fixed JARL end pattern; its nine
        # AMBE bytes still belong to the received last frame (media-format.md,
        # Unit extensions)
        return Source(frames, DPLUS_END_AMBE)
    if kind in DSTAR_KINDS:
        return Source(frames, bytes(ext.WIDTH[1]))
    if kind == "dmr":
        pad = -len(frames) % 3
        return Source(frames + [bytes(9)] * pad, bytes(9))
    if kind == "m17":
        voice = []
        last = b""
        for index in range(0, len(frames), 2):
            pair = frames[index:index + 2]
            last = pair[0] + (pair[1] if len(pair) > 1 else pair[0])
            voice.extend([last[:8], last[8:]])
        if not frames:
            return Source([], bytes(8))
        voice.append(last[:8])
        return Source(voice, last[8:])
    if kind == "usrp":
        return Source(frames, None)
    raise ValueError("no source rule for kind %r" % kind)


def transform_units(src, dst, units):
    """The fake's output for each unit, one hop."""
    return [ext.expected_output(src, dst, u) for u in units]


def _fold_dmr(units):
    """DMR egress: whole bursts of three; a partial last burst is padded with
    raw 9-byte zero frames (not transcoded); the end unit is not rendered."""
    return list(units) + [bytes(9)] * (-len(units) % 3)


def _fold_m17(units, end):
    """M17 egress: unit n pairs with n+1; the EOT packet carries [pending unit
    or Codec2 silence, end unit or Codec2 silence]."""
    units = list(units)
    pending = len(units) % 2
    frames = units[:len(units) - pending]
    frames.append(units[-1] if pending else M17_SILENCE)
    frames.append(end if end is not None else M17_SILENCE)
    return frames


def expected_listener(src_kind, dst_kind, frames, m17_fn0=False):
    """Expect for a listener of dst_kind when src_kind talks `frames`.

    Same protocol (same-protocol fast path): the frames the client library
    expects (dvtest.clients.wire_frames; m17_fn0 says whether packet 0 is
    relayed). Same codec, other protocol (D-STAR family): the frames unchanged.
    Different codec: the single-hop fake transform of every unit, the end unit
    included where the listener renders it.
    """
    src, dst = CODEC[src_kind], CODEC[dst_kind]
    if src_kind == dst_kind:
        from dvtest import clients
        out = clients.wire_frames(src_kind, [bytes(f) for f in frames], m17_fn0=m17_fn0)
        return Expect(out, "same-protocol path, no transform")
    unit = source_units(src_kind, frames)
    if src == dst:
        voice, end = list(unit.voice), unit.end
    else:
        voice = transform_units(src, dst, unit.voice)
        end = None if unit.end is None else ext.expected_output(src, dst, unit.end)
    if dst_kind in DSTAR_KINDS:
        # the last frame is not in the voice list the clients collect
        return Expect(voice, "end unit goes to the last frame")
    if dst_kind == "dmr":
        out = _fold_dmr(voice)
        return Expect(out, "end unit not rendered; padded to whole bursts")
    if dst_kind == "m17":
        out = _fold_m17(voice, end)
        return Expect(out, "EOT = [pending or silence, end unit or silence]")
    if dst_kind == "usrp":
        # media-format.md USRP (XC1): one keyed frame per voice PCM unit; the
        # end-hinted unit is never rendered and StreamEnd adds no PCM unit
        return Expect(voice, "voice PCM units only, then the unkey")
    raise ValueError("no listener rule for kind %r" % dst_kind)


def dstar_cross_slow(count):
    """Slow data of cross-mode D-STAR voice frames 0..count-1 when the talker
    sent no status / GPS: sync at position 0, filler elsewhere (position =
    media_seq mod 21)."""
    return [DSTAR_SYNC if i % 21 == 0 else DSTAR_FILLER for i in range(count)]


def dstar_cross_seq(count):
    """Sequence byte (without the 0x40 bit) of cross-mode D-STAR frames."""
    return [i % 21 for i in range(count)]


# ---- the transcoder link as the fake saw it ---------------------------------

Session = namedtuple("Session", "open std busy requests replies cs")


def _hex(entry):
    return bytes.fromhex(entry["hex"]) if "hex" in entry else bytes(entry["data"])


def sessions(entries):
    """Group fake log entries by stream. Returns (list of Session, strays):
    one Session per valid OPEN seen on the control port (parsed open tuple
    (callsign, codec_in, outs, transaction)), its STD (parsed or None), the
    BUSY transaction or None, the data requests (codec_in, pid, frame) and the
    replies (raw bytes) of its stream port, and the CS stream ids that named
    it. strays: entries that matched no session (invalid opens, drops)."""
    opens = []
    by_port = {}
    strays = []
    cs_ids = []
    for e in entries:
        kind, d = e["kind"], e["dir"]
        raw = _hex(e)
        if kind == "open" and d == "rx":
            if e.get("note") == "invalid":
                strays.append(e)
                continue
            parsed = ext.parse_open(raw)
            if any(o["open"][3] == parsed[3] and o["open"][1:3] == parsed[1:3] for o in opens):
                continue                                 # retransmission of the same key
            opens.append({"open": parsed, "std": None, "busy": None, "req": [], "rep": [], "cs": []})
        elif kind == "std" and d == "tx":
            std = ext.parse_std(raw)
            for o in reversed(opens):
                if o["open"][3] == std[4] and o["std"] is None:
                    o["std"] = std
                    by_port[std[1]] = o
                    break
        elif kind == "busy" and d == "tx":
            tx = ext.parse_busy(raw)
            for o in reversed(opens):
                if o["open"][3] == tx and o["busy"] is None:
                    o["busy"] = tx
                    break
        elif kind == "data_request" and d == "rx":
            if e.get("note") == "dropped" or e["port"] not in by_port:
                strays.append(e)
                continue
            by_port[e["port"]]["req"].append(ext.parse_data_request(raw))
        elif kind == "data_reply" and d == "tx":
            if e["port"] in by_port:
                by_port[e["port"]]["rep"].append(raw)
            else:
                strays.append(e)
        elif kind == "cs" and d == "rx":
            cs_ids.append(ext.parse_cs(raw))
    for o in opens:
        if o["std"]:
            o["cs"] = [i for i in cs_ids if i == o["std"][0]]
    return [Session(o["open"], o["std"], o["busy"], o["req"], o["rep"], o["cs"]) for o in opens], strays


def check_link(session, src_codec, units, outs):
    """Problems (list of strings) of one session against what the source
    edge must have sent: OPEN input and outputs (ascending, input excluded),
    exactly the given units as data requests in order with pid = index mod 256,
    no duplicates, and one reply per request holding the transform for each
    output in OPEN order. Empty list = byte exact."""
    problems = []
    _cs, codec_in, got_outs, _tx = session.open
    if codec_in != src_codec:
        problems.append("OPEN input codec %d, expected %d" % (codec_in, src_codec))
    if tuple(got_outs) != tuple(outs):
        problems.append("OPEN outputs %s, expected %s" % (list(got_outs), list(outs)))
    reqs = session.requests
    if len(reqs) != len(units):
        problems.append("%d data requests, expected %d" % (len(reqs), len(units)))
    for index, (req, unit) in enumerate(zip(reqs, units)):
        if req is None:
            problems.append("request %d malformed" % index)
            continue
        if req[0] != src_codec or req[1] != index % 256 or req[2] != bytes(unit):
            problems.append("request %d is codec %d pid %d %s, expected pid %d %s" % (
                index, req[0], req[1], req[2].hex(), index % 256, bytes(unit).hex()))
            break
    seen = [r[2] for r in reqs if r]
    if len(set(seen)) != len(seen) and len(set(map(bytes, units))) == len(units):
        problems.append("a unit was sent to the transcoder more than once")
    if len(session.replies) != len(reqs):
        problems.append("%d replies for %d requests" % (len(session.replies), len(reqs)))
    for index, (rep, req) in enumerate(zip(session.replies, reqs)):
        if req and rep != ext.expected_reply(src_codec, outs, req[1], req[2]):
            problems.append("reply %d is not the transform" % index)
            break
    return problems


# ---- USRP listener framing (media-format.md USRP, XC1) ---------------------------

USRP_HEADER = 32
USRP_VOICE = 352


def usrp_tail_problems(inbox):
    """Problems (list of strings) of the stream framing a USRP listener got:
    keyed 352 B voice frames, then exactly one 32 B unkey after the last one;
    no keyed frame after the unkey (no synthetic PCM end unit, no extra keyed
    frame). inbox: datagrams or (time, datagram) pairs; frames that are not
    USRP type-0 voice / unkey (REG answers, text) are ignored."""
    seq = []
    for item in inbox:
        data = item[1] if isinstance(item, tuple) else item
        if len(data) not in (USRP_HEADER, USRP_VOICE) or not data.startswith(b"USRP"):
            continue
        if int.from_bytes(data[20:24], "little") != 0:
            continue
        keyup = int.from_bytes(data[12:16], "big")
        if len(data) == USRP_VOICE and keyup == 1:
            seq.append("v")
        elif len(data) == USRP_HEADER and keyup == 0:
            seq.append("u")
    if "v" not in seq:
        return ["no keyed voice frame"]
    first = seq.index("v")
    tail = seq[first:]
    last_voice = len(tail) - 1 - tail[::-1].index("v")
    unkeys_inside = tail[:last_voice].count("u")
    unkeys_after = tail[last_voice + 1:].count("u")
    problems = []
    if unkeys_inside:
        problems.append("%d unkey(s) between voice frames" % unkeys_inside)
    if unkeys_after != 1:
        problems.append("%d unkeys after the last voice frame, expected 1" % unkeys_after)
    return problems


# ---- timing of the transcoded path (transcoding.md Controller transcoding) -------

# Tolerances of the first-voice timing checks, seconds. The OPEN goes out in the
# controller turn of the first voice unit (no timer, no lookahead): it reaches the
# fake a few fabric hops after the tester sent that unit.
OPEN_BEFORE_VOICE_TOL = 0.010      # clock mapping error (half the control RTT) and jitter
OPEN_AFTER_VOICE_MAX = 0.150       # edge -> room -> worker -> fake, no added timer
OPEN_COST_TOL = 0.040              # the first output cannot precede the STD by more than this
SAME_DELAY_TOL = 0.080             # with vs without a leading header (M17 pairs, 20 ms pacing)

Timing = namedtuple("Timing", "gap open_after_voice output_after_voice")


def clock_offset(samples):
    """Offset of a remote monotonic clock against the local one from
    (local_before, remote_now, local_after) samples: (offset, half_rtt) of the
    sample with the shortest round trip; local = remote - offset."""
    if not samples:
        raise ValueError("no clock samples")
    before, remote, after = min(samples, key=lambda s: s[2] - s[0])
    return remote - (before + after) / 2.0, (after - before) / 2.0


def first_arrival(times, raw, match):
    """Receive time of the first datagram of raw for which match(datagram) is
    true, or None; times[i] is the receive time of raw[i] (same local clock as
    the send stamps). ValueError when the two lists are not aligned."""
    if len(times) != len(raw):
        raise ValueError("%d receive times for %d datagrams" % (len(times), len(raw)))
    for stamp, data in zip(times, raw):
        if match(data):
            return stamp
    return None


def header_cost_problems(with_header, without_header, open_s):
    """Problems (list of (id, documented, observed)) of two Timing records of
    the same voice stream, one with a voiceless header gap before the first
    voice unit, one with the voice right after the header, when the fake
    answers OPEN after open_s seconds. transcoding.md (XC1): the first voice
    unit emits CreateSession in the same controller turn, the OPEN wait counts
    from that unit, and the OPEN no longer overlaps the header-to-voice gap, so
    the transcoded path starts later than with opening at grant by
    min(OPEN time, gap): measured from the first voice unit, the delay is the
    same with or without the header gap and includes the OPEN time."""
    out = []
    for name, t in (("header-gap", with_header), ("no-gap", without_header)):
        if t.open_after_voice is None:
            out.append((name + "-open-seen", "one OPEN for the voiced stream", "no OPEN in the fake log"))
            continue
        if t.open_after_voice < -OPEN_BEFORE_VOICE_TOL:
            out.append((name + "-open-not-before-voice",
                        "the session opens on the first voice unit, not at the header",
                        "OPEN %.3f s before the first voice unit (gap %.3f s)" % (-t.open_after_voice, t.gap)))
        elif t.open_after_voice > OPEN_AFTER_VOICE_MAX:
            out.append((name + "-open-same-turn",
                        "CreateSession in the controller turn of the first voice unit (<= %.3f s)"
                        % OPEN_AFTER_VOICE_MAX,
                        "OPEN %.3f s after the first voice unit" % t.open_after_voice))
        if t.output_after_voice is None:
            out.append((name + "-output-seen", "transcoded output", "no transcoded output unit received"))
        elif t.output_after_voice < open_s - OPEN_COST_TOL:
            out.append((name + "-open-cost-not-hidden",
                        "the OPEN time (%.3f s) is not hidden before the first voice unit" % open_s,
                        "first output %.3f s after the first voice unit" % t.output_after_voice))
    if with_header.output_after_voice is not None and without_header.output_after_voice is not None:
        diff = with_header.output_after_voice - without_header.output_after_voice
        if abs(diff) > SAME_DELAY_TOL:
            out.append(("same-delay",
                        "the delay from the first voice unit does not depend on the header gap (+-%.3f s)"
                        % SAME_DELAY_TOL,
                        "%.3f s with a %.3f s header gap, %.3f s without" % (
                            with_header.output_after_voice, with_header.gap, without_header.output_after_voice)))
    return out
