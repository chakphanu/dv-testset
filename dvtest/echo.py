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
  header_record(packet)      RPT1 / RPT2 / MY / UR of a DSVT header, as text
  integrity(...)             the D-STAR echo return judge (E9): lost frames, arrival gaps,
                             seq bytes, sync pattern, end bit, header
  summarize(label, results)  the numbers of the E9 summary for a list of integrity() results
  integrity_failures(...)    the assertion messages of E9 for the summaries of all streams
"""
import re
import tomllib
from collections import namedtuple

from . import clients, metrics, sut_config
from .audio import signals
from .proto import dextra, dmr, m17, ysf

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
SEQ_PERIOD = 21                 # D-STAR frame counter: 0..20, frame 0 of a superframe carries the sync pattern
END_BIT = 0x40                  # on the seq byte of the last voice packet
SEQ_BAD_KEEP = 5                # how many (index, got, want) rows integrity() keeps
SYNC_FIELD = dextra.SYNC_DATA   # slow data 55 2d 16 on frame 0 of each superframe


def dstar_streams(items, gap=0.4):
    """Streams among `items` = [(t, datagram)]: datagrams that are not a DSVT header or voice packet
    (keepalives, link replies) are left out; a new stream starts when more than `gap` seconds passed
    since the previous header or voice packet (the echo starts about 1 s after the over it repeats).
    Returns [{"start": t, "end": t, "sids": [stream ids], "header": 56 B packet or None,
    "voice": [(t, 27 B packet)]}] in time order."""
    out, last = [], None
    for t, data in sorted(((t, bytes(d)) for t, d in items), key=lambda i: i[0]):
        head = dextra.parse_header(data)
        voice = None if head else dextra.parse_voice(data)
        if not head and not voice:
            continue
        if last is None or t - last > gap:
            out.append({"start": t, "end": t, "sids": [], "header": None, "voice": []})
        cur = out[-1]
        last = cur["end"] = t
        sid = head["stream_id"] if head else voice[0]
        if sid not in cur["sids"]:
            cur["sids"].append(sid)
        if head:
            cur["header"] = cur["header"] or data
        else:
            cur["voice"].append((t, data))
    return out


def header_record(packet):
    """{"rpt1", "rpt2", "my", "ur"} of a 56 B DSVT header as stripped text (ASCII, replacement for
    other bytes), None for anything else."""
    head = dextra.parse_header(bytes(packet)) if packet else None
    if head is None:
        return None
    return {k: head[k].decode("ascii", "replace").strip() for k in ("rpt1", "rpt2", "my", "ur")}


def integrity(sent_frames, voice_packets, arrivals=None, header=None, max_gap_ms=GAP_MAX_MS):
    """Judge one D-STAR stream (a forward copy or an echo return) against the frames that were sent.
    Pure: no I/O, no clock.

    sent_frames   the 9 byte AMBE frames of the over, in order (the sender's end packet is not one)
    voice_packets the 27 byte DSVT voice packets of the stream in arrival order, the last one included
    arrivals      the arrival time (seconds, monotonic) of each packet; None skips the gap checks
    header        the 56 byte DSVT header of the stream, or None

    Voice packet layout (27 bytes): "DSVT" (0-3), 0x20 (4), 00 00 00 (5-7), 0x20 (8), 00 01 02 (9-11),
    stream id (12-13, little endian), seq byte (14), AMBE (15-23), slow data (24-26). The seq byte is the
    frame's position in the stream (0-based) modulo 21, and the last packet of the stream has bit 0x40
    set on top of it. The slow data of a frame at position mod 21 == 0 is the sync pattern 55 2d 16 and
    no other frame carries it. Position is counted in the stream as heard, so a lost frame shows as a
    gap and as a wrong seq from there on, as it would to a receiver.

    Returns a dict:
      frames_sent, frames_heard (voice packets but the last, which is the end of the stream),
      lost (sent frames whose payload was not heard), lost_indexes (first few), extra (frames heard that
      were not sent), reordered (heard frames earlier in the sent order than the one before),
      max_gap_ms, gaps_over_25ms ([(position, gap_ms)] over max_gap_ms),
      seq_checked, seq_bad_count, seq_ok_share, seq_bad ([(position, got, want)], first few),
      last_frame_has_end_bit, sync_frames (positions of packets with 55 2d 16), sync_missing (positions
      mod 21 == 0 without it), sync_extra (other positions with it), header (header_record or None),
      packets (voice packets in the stream)."""
    sent = [bytes(f) for f in sent_frames]
    packets = [bytes(p) for p in voice_packets]
    parsed = [dextra.parse_voice_raw(p) for p in packets]
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
    gaps = []
    max_gap = 0.0
    if arrivals is not None:
        times = list(arrivals)
        for pos in range(1, min(len(times), len(packets))):
            gap = (times[pos] - times[pos - 1]) * 1000.0
            max_gap = max(max_gap, gap)
            if gap > max_gap_ms:
                gaps.append((pos, round(gap, 1)))

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
    return {
        "frames_sent": len(sent), "frames_heard": len(heard), "lost": len(lost_idx),
        "lost_indexes": lost_idx[:SEQ_BAD_KEEP * 4], "extra": extra, "reordered": reordered,
        "max_gap_ms": round(max_gap, 1), "gaps_over_25ms": gaps,
        "seq_checked": checked, "seq_bad_count": seq_bad_count,
        "seq_ok_share": round((checked - seq_bad_count) / checked, 4) if checked else 0.0,
        "seq_bad": seq_bad,
        "last_frame_has_end_bit": bool(parsed and parsed[-1] and parsed[-1]["last"]),
        "sync_frames": sync_frames, "sync_missing": sync_missing, "sync_extra": sync_extra,
        "header": header_record(header), "packets": len(packets)}


def summarize(label, results):
    """The numbers of one stream kind (forward / echo_listener / echo_sender) over the runs:
    `results` is the list of integrity() dicts, one per run (an empty stream is integrity() of no
    packets). share_of_frames_with_wrong_seq is over every packet judged."""
    checked = sum(r["seq_checked"] for r in results)
    return {
        "stream": label, "runs": len(results),
        "echoes_with_lost_frame": sum(1 for r in results if r["lost"]),
        "lost_frames_total": sum(r["lost"] for r in results),
        "extra_frames_total": sum(r["extra"] for r in results),
        "share_of_frames_with_wrong_seq": round(sum(r["seq_bad_count"] for r in results) / checked, 4) if checked else None,
        "max_gap_ms": max([r["max_gap_ms"] for r in results] or [0.0]),
        "gaps_over_25ms": sum(len(r["gaps_over_25ms"]) for r in results),
        "sync_errors": sum(len(r["sync_missing"]) + len(r["sync_extra"]) for r in results),
        "missing_end_bit": sum(1 for r in results if not r["last_frame_has_end_bit"]),
        "empty_streams": sum(1 for r in results if not r["packets"])}


def integrity_failures(summaries):
    """Assertion messages of E9 for {"forward": summary, "echo_listener": ..., "echo_sender": ...}
    (summarize() dicts). The forward copy is the control: a failure there is a different defect and
    is worded so. Empty list when everything is clean."""
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
        if s["sync_errors"]:
            bad.append("%d frame(s) with a wrong sync pattern" % s["sync_errors"])
        if s["missing_end_bit"]:
            bad.append("%d stream(s) whose last packet has no end bit" % s["missing_end_bit"])
        if bad:
            out.append("%s: %s" % (names[key], "; ".join(bad)))
    return out
