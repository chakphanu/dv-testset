# SPDX-License-Identifier: MIT
"""Pure helpers of the built-in echo cases (cases/test_echo.py, docs/echo-cases.md):
no sockets, no clock, nothing that needs the rig.

DV-Router's echo service (dv-svc-echo) records a local over on a configured
module and replays it delay_ms after the end. The cases watch it from outside
only: UDP clients on the module, the hub metrics through the control channel and
the rendered dv-router.toml of the run.

  echo_settings(text)        the effective [echo] table of a rendered TOML
  edit_echo(text, ...)       the same text with modules / max_seconds / delay_ms
                             changed (one line each, comments kept)
  echo_overs(metrics, room, result)
                             dv_echo_overs_total of a room and result (any case of
                             the room label: the design writes it in lower case)
  events(kind, packets, times)
                             what a client heard, decoded per protocol
  split_overs(events, gap)   the overs inside one stream of events
  expected_frames(kind, frames)
                             the voice frames a same-kind listener may hear for a
                             talk of `frames` (pads and EOT repeats of the egress)
  compare_payload(sent, got) byte comparison of the sent frames with a replay
  dstar_streams(items, gap)  the DSVT streams (header + voice packets) among timed datagrams
  header_record(packet)      flags / RPT1 / RPT2 / UR / MY / suffix of a D-STAR header, as text
  header_failures(...)       the echo header rule of E9 (talker fields as sent, RPT2 as the live copy)
  voice_view / header_view   the fields of a voice / header packet of any E9 kind
  integrity(...)             the echo return judge (E9) for the kinds dextra, dplus, dcs, g3, xlx:
                             lost frames, arrival gaps, seq bytes, sync pattern, end bit, the
                             protocol's own counter (DCS frame counter, XLX DMR packet id)
  aux_vs_forward(...)        the echo's own counter sequence against the forward copy's
  summarize(label, results)  the numbers of the E9 summary for a list of integrity() results
  integrity_failures(...)    the assertion messages of E9 for the summaries of all streams
  xlx_peer_rows / xlx_mode   which XLX peer row of the rendered config the E9 xlx kind can use
"""
import re
import tomllib
from collections import namedtuple

from . import clients, metrics, sut_config
from .audio import signals
from .proto import dcs, dextra, dmr, dplus, m17, xlx, ysf

DEFAULT_MAX_SECONDS = 30
DEFAULT_DELAY_MS = 1000
MIN_FRAMES = 50                 # a shorter over is never replayed (the floor guard threshold)
FRAME_S = 0.020                 # one voice frame
CODEC_NAME = {"dstar": "dstar", "dmr": "dmr", "m17": "m17", "ysf": "dmr"}   # frame width of each kind
KEEP = object()                 # "leave this key as it is" in edit_echo


# ---------------------------------------------------------------- the [echo] table of a rendered TOML

def echo_settings(text):
    """{"max_seconds": int, "delay_ms": int, "modules": {letter: bool}} of the [echo]
    table of a TOML text (defaults 30 and 1000 for an absent key), or None when
    there is no [echo] table. ValueError for a text that is not TOML."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("not TOML: %s" % exc) from exc
    table = data.get("echo")
    if not isinstance(table, dict):
        return None
    modules = table.get("modules") or {}
    return {"max_seconds": int(table.get("max_seconds", DEFAULT_MAX_SECONDS)),
            "delay_ms": int(table.get("delay_ms", DEFAULT_DELAY_MS)),
            "modules": {str(k): bool(v) for k, v in modules.items()}}


def modules_value(modules):
    """The TOML inline table for a {letter: bool} mapping: { E = true, M = false } ("{}" when empty)."""
    if not modules:
        return "{}"
    return "{ %s }" % ", ".join("%s = %s" % (k, "true" if v else "false") for k, v in modules.items())


def edit_echo(text, modules=KEEP, max_seconds=KEEP, delay_ms=KEEP):
    """`text` with the given keys of its [echo] table set (a key that is absent is added
    after the last value line of the table; modules is a {letter: bool} mapping, {} removes
    every row; None removes the key line itself). Everything else, comments included, is
    kept byte for byte. ValueError when there is no [echo] table, a value spans lines, or
    the result is not TOML."""
    parts = sut_config._split_tables(text)
    target = next((p for p in parts if p[0] == "echo"), None)
    if target is None:
        raise ValueError("no [echo] table")
    wanted = {}
    if modules is not KEEP:
        wanted["modules"] = None if modules is None else modules_value(modules)
    if max_seconds is not KEEP:
        wanted["max_seconds"] = None if max_seconds is None else str(int(max_seconds))
    if delay_ms is not KEEP:
        wanted["delay_ms"] = None if delay_ms is None else str(int(delay_ms))
    body, done = [], set()
    for line in target[3]:
        m = sut_config._KEY.match(line)
        key = m.group(1) if m else None
        if key in wanted:
            if sut_config._spans_lines(line):
                raise ValueError("the value of %s spans lines" % key)
            done.add(key)
            if wanted[key] is not None:
                body.append("%s = %s" % (key, wanted[key]))
            continue
        body.append(line)
    at = max([i for i, line in enumerate(body) if line.strip() and not line.lstrip().startswith("#")] or [-1]) + 1
    add = ["%s = %s" % (k, v) for k, v in wanted.items() if k not in done and v is not None]
    target[3] = body[:at] + add + body[at:]
    out = sut_config._join_tables(parts)
    try:
        tomllib.loads(out)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("the edited text is not TOML: %s" % exc) from exc
    return out


# ---------------------------------------------------------------- telemetry

def echo_overs(samples, room, result):
    """Sum of dv_echo_overs_total{room, result}; `room` is compared without regard to case
    (design: the label is the lower case module letter; the DV-Router brief wrote "E").
    `samples` is metrics text or a list from metrics.parse."""
    if isinstance(samples, str):
        samples = metrics.parse(samples)
    return sum(s.value for s in samples
               if s.name == "dv_echo_overs_total" and s.labels.get("result") == result
               and s.labels.get("room", "").lower() == room.lower() and s.value == s.value)


def room_label_cases(samples):
    """The set of ways the room label of dv_echo_overs_total is spelled: {"lower"}, {"upper"}, both or empty."""
    if isinstance(samples, str):
        samples = metrics.parse(samples)
    out = set()
    for s in samples:
        if s.name == "dv_echo_overs_total":
            room = s.labels.get("room", "")
            out.add("lower" if room == room.lower() else "upper")
    return out


# ---------------------------------------------------------------- what a client heard

# kind is "header" (a stream start), "voice", "end" (the stream's last packet) or "ctrl" (a DMR data
# packet: header or terminator, not told apart); ident is the talker as the packet names it
Ev = namedtuple("Ev", "t kind sid ident frames")
Over = namedtuple("Over", "start end voice_start voice_end frames idents sids n_voice header ended")


def _dstar_event(t, data):
    head = dextra.parse_header(data)
    if head:
        return Ev(t, "header", head["stream_id"], head["my"].decode("ascii", "replace").strip(), [])
    voice = dextra.parse_voice(data)
    if voice:
        sid, _seq, last, frame = voice
        return Ev(t, "end" if last else "voice", sid, None, [] if last else [frame])
    return None


def _dmr_event(t, data):
    info = dmr.parse(data)
    if info is None:
        return None
    if dmr.is_voice(data):
        return Ev(t, "voice", info["stream"], info["src"], dmr.extract(info["payload"]))
    return Ev(t, "ctrl", info["stream"], info["src"], [])


def _m17_event(t, data):
    info = m17.parse(data)
    if info is None:
        return None
    return Ev(t, "end" if info["eot"] else "voice", info["stream_id"], info["src"], list(info["frames"]))


def _ysf_event(t, data):
    info = ysf.parse(data)
    if info is None or info["kind"] not in ("header", "voice", "terminator"):
        return None
    kind = {"header": "header", "voice": "voice", "terminator": "end"}[info["kind"]]
    return Ev(t, kind, None, info["src"], list(info.get("frames") or []))


_DECODERS = {"dstar": _dstar_event, "dmr": _dmr_event, "m17": _m17_event, "ysf": _ysf_event}


def events(kind, packets, times):
    """Events of the stream packets among `packets` (keepalives and everything that is not
    a stream packet of the protocol are left out), `times` the arrival time of each."""
    decode = _DECODERS[kind]
    out = []
    for data, t in zip(packets, times):
        ev = decode(t, bytes(data))
        if ev is not None:
            out.append(ev)
    return out


def split_overs(evs, gap=0.4):
    """Overs of a time ordered event list: a new over starts when more than `gap` seconds
    passed since the previous event (the replay starts a second after the over it repeats;
    20 ms to 100 ms separate the packets of one stream)."""
    groups, cur = [], []
    for ev in evs:
        if cur and ev.t - cur[-1].t > gap:
            groups.append(cur)
            cur = []
        cur.append(ev)
    if cur:
        groups.append(cur)
    out = []
    for group in groups:
        voice = [e for e in group if e.frames]
        first_voice = next((i for i, e in enumerate(group) if e.frames), None)
        out.append(Over(
            start=group[0].t, end=group[-1].t,
            voice_start=voice[0].t if voice else None, voice_end=voice[-1].t if voice else None,
            frames=[f for e in group for f in e.frames],
            idents=sorted({e.ident for e in group if e.ident is not None}, key=str),
            sids=sorted({e.sid for e in group if e.sid is not None}),
            n_voice=sum(1 for e in group if e.frames),
            header=first_voice is not None and any(e.kind == "header" for e in group[:first_voice]),
            ended=group[-1].kind in ("end", "ctrl")))
    return out


def after(overs, t):
    """The overs that start after time t."""
    return [o for o in overs if o.start > t]


# ---------------------------------------------------------------- what to expect

def make_frames(kind, n, seed):
    """n deterministic frames a client of `kind` can send and a same-kind listener can hear back
    unchanged: PRNG frames of the kind's codec; for YSF they are passed through the VCH
    coder once (an arbitrary 9-byte frame is not a codeword and would not round trip)."""
    frames = signals.prng_frames(CODEC_NAME[kind], n, seed=seed)
    if kind == "ysf":
        frames = [ysf.vch_decode(ysf.vch_encode(f)) for f in frames]
    return frames


def expected_frames(kind, frames):
    """Voice frames of the talk of `frames` as a same-kind listener hears them: D-STAR as
    sent, DMR padded to whole bursts of three, YSF to whole packets of five, M17 with the
    EOT packet repeating the last pair (DV-Router relays packet 0 too). The replay is
    judged against this and, as a prefix, against `frames` themselves."""
    frames = list(frames)
    if kind == "ysf":
        return ysf.wire_frames(frames)
    return clients.wire_frames(kind, frames, m17_fn0=True)


Compare = namedtuple("Compare", "identical prefix_ok first_diff extra missing")


def compare_payload(sent, got):
    """Compare the frames sent with the frames heard. `identical`: the same list; `prefix_ok`:
    every sent frame is there, in order, byte for byte, at the start of what was heard;
    `first_diff`: index of the first frame that differs (None when prefix_ok); `extra`: frames
    heard after the sent ones (a list, empty when none); `missing`: sent frames not heard."""
    sent, got = list(sent), list(got)
    n = min(len(sent), len(got))
    diff = next((i for i in range(n) if sent[i] != got[i]), None)
    if diff is None and len(got) < len(sent):
        diff = len(got)
    prefix_ok = diff is None
    return Compare(identical=sent == got, prefix_ok=prefix_ok, first_diff=diff,
                   extra=got[len(sent):] if prefix_ok else [], missing=max(0, len(sent) - len(got)))


def duration(over):
    """Seconds from the first to the last voice packet of an over (None without voice)."""
    if over.voice_start is None:
        return None
    return over.voice_end - over.voice_start


def over_seconds(frames):
    """Length of an over of `frames` 20 ms frames, in seconds."""
    return frames * FRAME_S


def in_window(value, low, high):
    return value is not None and low <= value <= high


def retained_frames(max_seconds):
    """At most this many 20 ms frames of an over are retained (design: max_seconds * 50)."""
    return int(max_seconds) * 50


def reload_wait(poll_s=1.0, stable_polls=2, margin=1.0):
    """Seconds to wait after editing the rendered TOML before the change is certainly applied:
    the watcher polls every `poll_s` and applies a file that looked the same `stable_polls`
    polls in a row (docs/design/config.md, Reload), plus `margin`."""
    return poll_s * (stable_polls + 1) + margin


_LETTER = re.compile(r"^[A-Z]$")


def valid_module(letter):
    return bool(_LETTER.match(letter or ""))


# ---------------------------------------------------------------- D-STAR echo return integrity (E9)
GAP_MAX_MS = 25.0               # an arrival gap above this is a lost or late frame (a frame is 20 ms)
GAP_CATCHUP_MS = 35.0           # a gap of this or more is a tick that should have emitted two units and emitted one (about 40 ms)
SEQ_PERIOD = 21                 # D-STAR frame counter: 0..20, frame 0 of a superframe carries the sync pattern
END_BIT = 0x40                  # on the seq byte of the last voice packet
SEQ_BAD_KEEP = 5                # how many (index, got, want) rows integrity() keeps
SYNC_FIELD = dextra.SYNC_DATA   # slow data 55 2d 16 on frame 0 of each superframe
KINDS = ("dextra", "dplus", "dcs", "g3", "xlx")     # the client / peer kinds E9 judges
HEADER_KEYS = ("flags", "rpt1", "ur", "my", "suffix")   # the echo must return these as the talker sent them
DMR_IDS_PERIOD = 18             # XLX revision 2/3 voice frames: DMR packet id 0..5 x sub id 1..3 = 18 steps


def _le16(raw):
    return int.from_bytes(raw, "little")


def header_view(kind, data):
    """The header a packet of `kind` carries, as {"stream_id", "flags", "rpt2", "rpt1", "ur", "my", "suffix"}
    (raw bytes), or None. dextra / g3 / xlx: the 56 byte DSVT header; dplus: the 58 byte packet (3a 80 +
    DSVT header); dcs: the 100 byte voice packet (DCS has no header packet, every voice packet carries
    the header fields, so the first voice packet of a stream is its header)."""
    data = bytes(data) if data else b""
    if kind == "dcs":
        if len(data) < dcs.PACKET_SIZE or not data.startswith(b"0001"):
            return None
        raw = dcs.parse_raw(data)
        return {"stream_id": _le16(raw["sid"]), **{k: raw[k] for k in ("flags", "rpt2", "rpt1", "ur", "my", "suffix")}}
    if kind == "dplus":
        if len(data) != dplus.HEADER_SIZE or data[:2] != b"\x3a\x80":
            return None
        data = data[2:]
    full = dextra.parse_header_full(data)
    if full is None:
        return None
    return {k: full[k] for k in ("stream_id", "flags", "rpt2", "rpt1", "ur", "my", "suffix")}


def voice_view(kind, data):
    """The voice packet of `kind` as {"stream_id", "counter" (the raw seq byte, 0x40 = last), "last", "ambe"
    (9 bytes), "slow" (3 bytes)} plus the kind's own counter: dcs "frame_counter" (u24 at byte 58), xlx
    "pid" / "subid" (DMR packet id and sub id, bytes 27 and 28 of a revision 2 / 3 frame). None for
    anything that is not a voice packet of that kind (headers, keepalives, link packets).

    dextra / g3: 27 byte DSVT; dplus: 29 byte (1d 80 + DSVT) and the 32 byte last form (20 80 + DSVT ...);
    dcs: 100 byte packet starting 0001; xlx: 27 / 45 / 53 byte DSVT (revision 1 / 2 / 3)."""
    data = bytes(data) if data else b""
    if kind in ("dextra", "g3"):
        return dextra.parse_voice_raw(data)
    if kind == "dplus":
        if len(data) == dplus.VOICE_SIZE and data[:2] == b"\x1d\x80":
            return dextra.parse_voice_raw(data[2:])
        if len(data) == dplus.LAST_SIZE and data[:2] == b"\x20\x80" and data[2:6] == b"DSVT" and data[6] == 0x20:
            return {"stream_id": _le16(data[14:16]), "counter": data[16], "last": bool(data[16] & END_BIT),
                    "ambe": data[17:26], "slow": data[26:29]}
        return None
    if kind == "dcs":
        if len(data) < dcs.PACKET_SIZE or not data.startswith(b"0001"):
            return None
        raw = dcs.parse_raw(data)
        return {"stream_id": _le16(raw["sid"]), "counter": raw["seq_byte"], "last": raw["last"],
                "ambe": raw["frame"], "slow": raw["slow"], "frame_counter": raw["counter"]}
    if kind == "xlx":
        if len(data) not in xlx.REVISION_OF_SIZE or not data.startswith(b"DSVT") or data[4] != 0x20 or data[8] != 0x20:
            return None
        out = {"stream_id": _le16(data[12:14]), "counter": data[14], "last": bool(data[14] & END_BIT),
               "ambe": data[15:24], "slow": data[24:27]}
        if len(data) >= 45:
            out["pid"], out["subid"] = data[27], data[28]
        return out
    raise ValueError("unknown kind %r (known: %s)" % (kind, ", ".join(KINDS)))


def dstar_streams(items, gap=0.4, kind="dextra"):
    """Streams among `items` = [(t, datagram)] of a client / peer of `kind` (default dextra): datagrams that
    are not a header or voice packet of the kind (keepalives, link replies) are left out; a new stream
    starts when more than `gap` seconds passed since the previous header or voice packet (the echo starts
    about 1 s after the over it repeats).
    Returns [{"start": t, "end": t, "sids": [stream ids], "header": the header packet or None,
    "voice": [(t, voice packet)]}] in time order. For dcs there is no header packet: "header" is the
    first voice packet of the stream (it carries the header fields)."""
    out, last = [], None
    for t, data in sorted(((t, bytes(d)) for t, d in items), key=lambda i: i[0]):
        head = None if kind == "dcs" else header_view(kind, data)
        voice = None if head else voice_view(kind, data)
        if not head and not voice:
            continue
        if last is None or t - last > gap:
            out.append({"start": t, "end": t, "sids": [], "header": None, "voice": []})
        cur = out[-1]
        last = cur["end"] = t
        sid = (head or voice)["stream_id"]
        if sid not in cur["sids"]:
            cur["sids"].append(sid)
        if head:
            cur["header"] = cur["header"] or data
        else:
            cur["voice"].append((t, data))
            if kind == "dcs" and cur["header"] is None:
                cur["header"] = data
    return out


def header_record(packet):
    """{"flags" (hex), "rpt1", "rpt2", "my", "ur", "suffix"} of a D-STAR header as stripped text (ASCII,
    replacement for other bytes), None for anything else. `packet` is the 56 byte DSVT header (DExtra, G3,
    XLX), the 58 byte DPlus header or a 100 byte DCS voice packet (told apart by size)."""
    data = bytes(packet) if packet else b""
    if len(data) == dextra.HEADER_SIZE:
        view = header_view("dextra", data)
    elif len(data) == dplus.HEADER_SIZE:
        view = header_view("dplus", data)
    elif len(data) >= dcs.PACKET_SIZE:
        view = header_view("dcs", data)
    else:
        view = None
    if view is None:
        return None
    out = {k: view[k].decode("ascii", "replace").strip() for k in ("rpt1", "rpt2", "my", "ur", "suffix")}
    out["flags"] = bytes(view["flags"]).hex()
    return out


def header_failures(sent, forward_header, echo_header):
    """The echo header rule (DV-Router): the echo returns the talker's flags (3 bytes), RPT1, UR, MY and
    suffix exactly as sent, and its RPT2 follows the live rule, i.e. is the RPT2 the forward (live) copy of
    the same over carries (reflector name + room module, `XRF999 E`), so that ircDDBGateway style listeners
    accept it. All three arguments are header_record() dicts (None: not heard). Returns the messages, empty
    when the rule holds."""
    if echo_header is None:
        return ["no echo header"]
    out = []
    if sent is None:
        out.append("the header the sender sent is unknown")
    else:
        for key in HEADER_KEYS:
            if echo_header.get(key) != sent.get(key):
                out.append("echo %s is %r, the sender sent %r" % (key.upper(), echo_header.get(key), sent.get(key)))
    if forward_header is None:
        out.append("no forward header to compare RPT2 with")
    elif echo_header.get("rpt2") != forward_header.get("rpt2"):
        out.append("echo RPT2 is %r, the forward copy carries %r" % (echo_header.get("rpt2"), forward_header.get("rpt2")))
    return out


def _dmr_ids_want(pos, base_pos, base_k):
    k = (base_k + pos - base_pos) % DMR_IDS_PERIOD
    return (k // 3, k % 3 + 1)


def integrity(sent_frames, voice_packets, arrivals=None, header=None, max_gap_ms=GAP_MAX_MS, kind="dextra"):
    """Judge one stream (a forward copy or an echo return) of a client / peer of `kind` against the frames
    that were sent. Pure: no I/O, no clock.

    sent_frames   the 9 byte AMBE frames of the over, in order (the sender's end packet is not one)
    voice_packets the voice packets of the stream (voice_view layout of the kind) in arrival order, the last
                  one included
    arrivals      the arrival time (seconds, monotonic) of each packet; None skips the gap checks
    header        the header packet of the stream (header_record), or None
    kind          dextra (default), dplus, dcs, g3 or xlx

    What is judged, for every kind: the payload (each sent 9 byte frame is heard, byte for byte, none extra),
    the arrival gaps between consecutive packets (above 25 ms; and the subset of 35 ms or more, a tick that
    emitted one unit where it should have emitted two), the D-STAR seq byte, the sync pattern, the end bit.
    The 27 byte DSVT voice layout (DExtra, G3, XLX revision 1; DPlus and DCS wrap the same fields): "DSVT"
    (0-3), 0x20 (4), 00 00 00 (5-7), 0x20 (8), 00 01 02 (9-11), stream id (12-13, little endian), seq byte
    (14), AMBE (15-23), slow data (24-26). The seq byte is the frame's position in the stream (0-based)
    modulo 21, and the last packet of the stream has bit 0x40 set on top of it. The slow data of a frame at
    position mod 21 == 0 is the sync pattern 55 2d 16 and no other frame carries it. Position is counted in
    the stream as heard, so a lost frame shows as a gap and as a wrong seq from there on, as it would to a
    receiver. On the XLX interlink this seq byte is xlxd's "packet id".
    The kind's own counter (never on the last packet): dcs "frame_counter" (u24 at byte 58) must equal the
    position; xlx revision 2 / 3 frames carry a DMR packet id (0..5) and sub id (1..3) that must advance by
    one step per packet (id 0..5 x sub 1..3, wrapping) from the first packet's values; aux_vs_forward()
    compares the sequence with the forward copy's.

    Returns a dict:
      kind, frames_sent, frames_heard (voice packets but the last, which is the end of the stream),
      lost (sent frames whose payload was not heard), lost_indexes (first few), extra (frames heard that
      were not sent), reordered (heard frames earlier in the sent order than the one before),
      max_gap_ms, gaps_over_25ms ([(position, gap_ms)] over max_gap_ms), gaps_ge_35ms (the subset of
      GAP_CATCHUP_MS or more),
      seq_checked, seq_bad_count, seq_ok_share, seq_bad ([(position, got, want)], first few),
      last_frame_has_end_bit, sync_frames (positions of packets with 55 2d 16), sync_missing (positions
      mod 21 == 0 without it), sync_extra (other positions with it), header (header_record or None),
      packets (voice packets in the stream),
      aux_name (None, "frame_counter" or "dmr_packet_id"), aux_checked, aux_bad_count, aux_bad
      ([(position, got, want)], first few), aux_seq (the counter values by position, for aux_vs_forward)."""
    sent = [bytes(f) for f in sent_frames]
    packets = [bytes(p) for p in voice_packets]
    parsed = [voice_view(kind, p) for p in packets]
    body = [(pos, v) for pos, v in enumerate(parsed) if v and not v["last"]]
    heard = [v["ambe"] for _, v in body]

    # payload comparison: a sent frame is lost when no unused heard frame equals it
    pool = {}
    for i, frame in enumerate(heard):
        pool.setdefault(frame, []).append(i)
    lost_idx, matched = [], []
    for i, frame in enumerate(sent):
        slot = pool.get(frame)
        if slot:
            matched.append((slot.pop(0), i))
        else:
            lost_idx.append(i)
    used = {h for h, _ in matched}
    extra = len(heard) - len(used)
    order = [i for _, i in sorted(matched)]
    reordered = sum(1 for a, b in zip(order, order[1:]) if b < a)

    # arrival gaps between consecutive packets
    gaps, catchup = [], []
    max_gap = 0.0
    if arrivals is not None:
        times = list(arrivals)
        for pos in range(1, min(len(times), len(packets))):
            gap = (times[pos] - times[pos - 1]) * 1000.0
            max_gap = max(max_gap, gap)
            if gap > max_gap_ms:
                gaps.append((pos, round(gap, 1)))
            if gap >= GAP_CATCHUP_MS:
                catchup.append((pos, round(gap, 1)))

    # seq byte and sync pattern by position
    seq_bad, seq_bad_count, sync_frames, sync_missing, sync_extra = [], 0, [], [], []
    last_pos = len(parsed) - 1
    for pos, v in enumerate(parsed):
        if v is None:
            continue
        want = (pos % SEQ_PERIOD) | (END_BIT if pos == last_pos else 0)
        if v["counter"] != want:
            seq_bad_count += 1
            if len(seq_bad) < SEQ_BAD_KEEP:
                seq_bad.append((pos, v["counter"], want))
        has_sync = v["slow"] == SYNC_FIELD
        if has_sync:
            sync_frames.append(pos)
        if v["last"]:
            continue                    # the end packet carries the end pattern, not slow data of a frame
        if pos % SEQ_PERIOD == 0 and not has_sync:
            sync_missing.append(pos)
        elif pos % SEQ_PERIOD != 0 and has_sync:
            sync_extra.append(pos)
    checked = sum(1 for v in parsed if v)

    # the kind's own counter
    aux_name, aux_checked, aux_bad_count, aux_bad, aux_seq = None, 0, 0, [], []
    base = None
    for pos, v in body:
        if "frame_counter" in v:
            aux_name = "frame_counter"
            got, want = v["frame_counter"], pos
            aux_seq.append(got)
        elif "pid" in v:
            aux_name = "dmr_packet_id"
            got = (v["pid"], v["subid"])
            if base is None:
                valid = 0 <= v["pid"] <= 5 and 1 <= v["subid"] <= 3
                base = (pos, v["pid"] * 3 + v["subid"] - 1 if valid else pos % DMR_IDS_PERIOD)
            want = _dmr_ids_want(pos, *base)
            aux_seq.append(got)
        else:
            continue
        aux_checked += 1
        if got != want:
            aux_bad_count += 1
            if len(aux_bad) < SEQ_BAD_KEEP:
                aux_bad.append((pos, got, want))
    return {
        "kind": kind, "frames_sent": len(sent), "frames_heard": len(heard), "lost": len(lost_idx),
        "lost_indexes": lost_idx[:SEQ_BAD_KEEP * 4], "extra": extra, "reordered": reordered,
        "max_gap_ms": round(max_gap, 1), "gaps_over_25ms": gaps, "gaps_ge_35ms": catchup,
        "seq_checked": checked, "seq_bad_count": seq_bad_count,
        "seq_ok_share": round((checked - seq_bad_count) / checked, 4) if checked else 0.0,
        "seq_bad": seq_bad,
        "last_frame_has_end_bit": bool(parsed and parsed[-1] and parsed[-1]["last"]),
        "sync_frames": sync_frames, "sync_missing": sync_missing, "sync_extra": sync_extra,
        "header": header_record(header), "packets": len(packets),
        "aux_name": aux_name, "aux_checked": aux_checked, "aux_bad_count": aux_bad_count, "aux_bad": aux_bad,
        "aux_seq": aux_seq}


def aux_vs_forward(forward, echo_result):
    """The echo's own counter sequence (integrity() "aux_seq") against the forward copy's, position by position:
    the echo must follow the same rule as the live copy (XLX: the DMR packet id / sub id). None when either
    stream has no such counter, else {"compared": n, "differ": positions that differ (a length difference
    counts), "first": [(position, forward value, echo value)] first few}."""
    a, b = forward.get("aux_seq") or [], echo_result.get("aux_seq") or []
    if not a or not b:
        return None
    rows = [(i, a[i], b[i]) for i in range(min(len(a), len(b))) if a[i] != b[i]]
    return {"compared": min(len(a), len(b)), "differ": len(rows) + abs(len(a) - len(b)), "first": rows[:SEQ_BAD_KEEP]}


def summarize(label, results):
    """The numbers of one stream kind (forward / echo_listener / echo_sender) over the runs:
    `results` is the list of integrity() dicts, one per run (an empty stream is integrity() of no
    packets; a dict may carry "aux_vs_forward" from aux_vs_forward()). share_of_frames_with_wrong_seq
    is over every packet judged. max_gap_ms is the largest arrival gap of any run; gaps_over_25ms
    counts the gaps above 25 ms, gaps_ge_35ms the (separate, smaller) count of the gaps of 35 ms or more."""
    checked = sum(r["seq_checked"] for r in results)
    names = [r["aux_name"] for r in results if r.get("aux_name")]
    versus = [r["aux_vs_forward"] for r in results if r.get("aux_vs_forward")]
    return {
        "stream": label, "runs": len(results),
        "echoes_with_lost_frame": sum(1 for r in results if r["lost"]),
        "lost_frames_total": sum(r["lost"] for r in results),
        "extra_frames_total": sum(r["extra"] for r in results),
        "share_of_frames_with_wrong_seq": round(sum(r["seq_bad_count"] for r in results) / checked, 4) if checked else None,
        "max_gap_ms": max([r["max_gap_ms"] for r in results] or [0.0]),
        "gaps_over_25ms": sum(len(r["gaps_over_25ms"]) for r in results),
        "gaps_ge_35ms": sum(len(r.get("gaps_ge_35ms") or []) for r in results),
        "sync_errors": sum(len(r["sync_missing"]) + len(r["sync_extra"]) for r in results),
        "missing_end_bit": sum(1 for r in results if not r["last_frame_has_end_bit"]),
        "empty_streams": sum(1 for r in results if not r["packets"]),
        "aux_name": names[0] if names else None,
        "aux_errors": sum(r.get("aux_bad_count", 0) for r in results),
        "aux_vs_forward_errors": sum(v["differ"] for v in versus)}


def integrity_failures(summaries):
    """Assertion messages of E9 for {"forward": summary, "echo_listener": ..., "echo_sender": ...}
    (summarize() dicts; any subset). The forward copy is the control: a failure there is a different defect
    and is worded so. Empty list when everything is clean."""
    names = {"forward": "forward copy (control, a defect of the live relay)",
             "echo_listener": "echo return to the listener", "echo_sender": "echo return to the sender"}
    out = []
    for key in ("forward", "echo_listener", "echo_sender"):
        s = summaries.get(key)
        if s is None:
            continue
        bad = []
        if s["empty_streams"]:
            bad.append("%d run(s) with no stream at all" % s["empty_streams"])
        if s["lost_frames_total"]:
            bad.append("%d lost frame(s) in %d run(s)" % (s["lost_frames_total"], s["echoes_with_lost_frame"]))
        if s["extra_frames_total"]:
            bad.append("%d frame(s) that were not sent" % s["extra_frames_total"])
        if s["share_of_frames_with_wrong_seq"]:
            bad.append("%.1f%% of the frames with a wrong seq byte" % (100.0 * s["share_of_frames_with_wrong_seq"]))
        if s["gaps_over_25ms"]:
            bad.append("%d arrival gap(s) over %g ms (max %.1f ms)" % (s["gaps_over_25ms"], GAP_MAX_MS, s["max_gap_ms"]))
        if s.get("gaps_ge_35ms"):
            bad.append("%d gap(s) of %g ms or more (a dropped unit in a catch-up tick; max %.1f ms)" % (
                s["gaps_ge_35ms"], GAP_CATCHUP_MS, s["max_gap_ms"]))
        if s["sync_errors"]:
            bad.append("%d frame(s) with a wrong sync pattern" % s["sync_errors"])
        if s["missing_end_bit"]:
            bad.append("%d stream(s) whose last packet has no end bit" % s["missing_end_bit"])
        if s.get("aux_errors"):
            bad.append("%d packet(s) with a wrong %s" % (s["aux_errors"], (s.get("aux_name") or "counter").replace("_", " ")))
        if s.get("aux_vs_forward_errors"):
            bad.append("%d packet(s) whose %s differs from the forward copy's" % (
                s["aux_vs_forward_errors"], (s.get("aux_name") or "counter").replace("_", " ")))
        if bad:
            out.append("%s: %s" % (names[key], "; ".join(bad)))
    return out


def parse_kinds(text):
    """(kinds, unknown) of a comma list of E9 kinds (DVT_ECHO_INTEGRITY_KINDS): the known names in KINDS order,
    each once, and the names that are not kinds, in the order given. A blank text means every kind."""
    names = [n.strip().lower() for n in (text or "").split(",") if n.strip()]
    if not names:
        return list(KINDS), []
    return [k for k in KINDS if k in names], [n for n in dict.fromkeys(names) if n not in KINDS]


# ---------------------------------------------------------------- the XLX peer of the rendered config (E9, kind xlx)

TESTER_HOSTS = ("tester",)      # a peer row naming the tester by host name


def xlx_peer_rows(text):
    """[{"callsign", "host", "modules"}] of the [[edges.xlx.peer]] rows of a rendered TOML (empty when none;
    ValueError for a text that is not TOML)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("not TOML: %s" % exc) from exc
    rows = (((data.get("edges") or {}).get("xlx") or {}).get("peer")) or []
    return [{"callsign": str(r.get("callsign", "")), "host": str(r.get("host", "")), "modules": str(r.get("modules", ""))}
            for r in rows if isinstance(r, dict)]


def xlx_mode(rows, tester_ip, module):
    """How E9 can watch the XLX peer for `module`: ("tester", row) when a peer row that serves the module names the
    tester (its address or the host name `tester`): the tester plays that peer (XlxPeer) and judges the
    interlink wire; else ("reflector", row) for another row that serves it (the rig's xlxd-peer-sut): a
    D-STAR client on that peer reflector judges what the peer re-encodes; else (None, None)."""
    serving = [r for r in rows if module in r["modules"]]
    for row in serving:
        if row["host"] == tester_ip or row["host"] in TESTER_HOSTS:
            return "tester", row
    return ("reflector", serving[0]) if serving else (None, None)
