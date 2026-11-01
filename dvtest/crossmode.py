# SPDX-License-Identifier: MIT
"""Byte-exact expectations for cross-mode traffic through a system under test
whose transcoder is the fake extended AMBEd (dvtest/fake_ambed.py).

Pure functions, no network. They turn what a rig client sends into the codec
units the system under test ingests, apply the fake transform per listener
codec and fold the result into what a listener of a given protocol receives.
Every rule below is taken from DV-Router docs/design/media-format.md
("Unit granularity", "End unit", "Cross-mode egress", "Expected counts") and
transcoding.md; where those documents are silent a function says so by
returning a prefix-only expectation (Expect.exact False) instead of guessing.

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
# frames: list of the units a listener receives; exact: the whole list is
# documented (count and order); False: only the first `prefix` frames are
Expect = namedtuple("Expect", "frames exact prefix note")


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
        return Expect(out, True, len(out), "same-protocol path, no transform")
    unit = source_units(src_kind, frames)
    if src == dst:
        voice, end = list(unit.voice), unit.end
    else:
        voice = transform_units(src, dst, unit.voice)
        end = None if unit.end is None else ext.expected_output(src, dst, unit.end)
    if dst_kind in DSTAR_KINDS:
        # the last frame is not in the voice list the clients collect
        return Expect(voice, True, len(voice), "end unit goes to the last frame")
    if dst_kind == "dmr":
        out = _fold_dmr(voice)
        return Expect(out, True, len(out), "end unit not rendered; padded to whole bursts")
    if dst_kind == "m17":
        out = _fold_m17(voice, end)
        return Expect(out, True, len(out), "EOT = [pending or silence, end unit or silence]")
    if dst_kind == "usrp":
        # media-format.md does not say whether a PCM end unit is rendered:
        # the voice units are documented, a following unit is recorded
        return Expect(voice, False, len(voice), "end unit rendering undocumented for USRP")
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
