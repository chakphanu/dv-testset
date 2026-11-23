# SPDX-License-Identifier: MIT
"""RFPOS1 step B (DV-Router 47dac7b4, image dv-router:wip-c3b24d2): the node link edge states frame positions.

What the product change says (docs/design/link.md "Appendix A", media-format.md "Unit granularity", floor-control.md RTS flags): a link
`Units` record (kind 20) holds exactly ONE 20 ms unit and its `sequence` is the sender's 20 ms position of that unit (fabric media_seq times the
stream's bundle width, plus the unit index in its bundle). The receiving link edge states RTS_FRAME_POSITIONS for a non-D-STAR source
(ambe2-dmr, codec2-3200, imbe, pcm-s16le-8k), so the room's transcoder keeps the position of a unit across a loss: a unit lost on the link is a
GAP at its own position in the transcoded output, not a shift of every later unit. An ambe-dstar link stream is positioned by profile and does
not change.

This module is pure (stdlib + dvtest.proto.dextra), except `LinkRelay`, a small UDP forwarder. Black-box means: the tester sits ON the link as an
opaque relay between node 1 (the dialling node, `[[edges.link.uplink]]`) and node 2 (the accepting node, `[[edges.link.account]]`). The datagrams
are authenticated (HMAC tag) but NOT encrypted, and the tester never holds the key: it reads the cleartext header (type, session id, counter,
stream id, profile, sequence) and forwards every datagram unchanged, except the one it is told to drop. Wire layout (link.md "Transport"):

    type u8 | session_id u32 | counter u64 | body | tag[16]          authenticated records, kind >= 16
    kind 16 RoomMap       count u8, pairs[count] (local u8, remote u8)
    kind 17 RoomMapAck    count u8, results[count] u8   (0 listen_only, 1 talk, 2 not_permitted, 3 not_served)
    kind 18 Demand        pair u8, profiles u16
    kind 19 StreamStart   pair u8, stream_id u32, origin u64, hop u8, profile u16, carried u16, callsign[10], radio_id u32, raw_len u8, raw
                          (GUARDID2: bit 0 of `carried` is the source-id flag, dvtest/guardid2.py)
    kind 20 Units         stream_id u32, profile u16, sequence u64, payload_len u16, payload
    kind 21 StreamEnd     stream_id u32, reason u16
    kind 22 Refused       stream_id u32, reason u8   (busy 1, listen_only 2, loop 3, hop_limit 4, not_mapped 5, overload 6)

Cases: cases/test_rfpos1_link.py; unit tests: cases/unit/test_rfpos1_lib.py; document: docs/rfpos1-cases.md.
"""
import os
import select
import socket
import struct
import threading
import time

from dvtest.proto import dextra

SWITCH = "DVT_RFPOS1B_LINK"
LINK_PORT = 31801                  # the tester's side of the link: node 1 dials this, the relay forwards to node 2's server port
SERVER_PORT = 31800                # edges.link.port default of node 2
ROOM = "G"                         # the room both nodes map (an own room: no other case talks in it)
DSTAR_PERIOD = 21                  # D-STAR counter modulus (the DSVT sequence byte, 5 bits)

HEAD = 13                          # type 1 + session id 4 + counter 8
TAG = 16
UNIT_BYTES = {1: 9, 2: 9, 3: 8, 4: 11, 5: 320}
PROFILE_NAME = {1: "ambe-dstar", 2: "ambe2-dmr", 3: "codec2-3200", 4: "imbe", 5: "pcm-s16le-8k"}
ACK_STATUS = {0: "listen_only", 1: "talk", 2: "not_permitted", 3: "not_served"}
KIND_NAME = {1: "hello", 2: "challenge", 3: "auth", 4: "welcome", 5: "reject", 16: "roommap", 17: "roommapack", 18: "demand",
             19: "streamstart", 20: "units", 21: "streamend", 22: "refused", 23: "keepalive", 24: "bye"}


def strict_on(environ=None):
    """DVT_RFPOS1B_LINK=on: the image has RFPOS1 step B and the rig runs the link pair (rfpos1-link-a.toml / -b.toml, run.sh --profile twonode)."""
    return (environ if environ is not None else os.environ).get(SWITCH, "").lower() in ("on", "1", "true", "yes")


# ---------------------------------------------------------------- the link wire (cleartext header only)

def parse_record(data):
    """A dict for one link datagram, or None when it is not a record this module reads.

    Handshake records (kind 1..5) give {"kind"} only. An authenticated record gives kind, session, counter and, for the kinds the cases
    read, their fields: units {stream_id, profile, sequence, payload_len, payload}, streamstart {pair, stream_id, profile, carried},
    roommap {pairs}, roommapack {results}, demand {pair, profiles}, streamend {stream_id, reason}, refused {stream_id, reason}; a streamstart
    with a whole fixed part also gives callsign (text), radio_id, raw_len and size (the datagram length). A kind-20 record whose declared payload
    length is not what the datagram holds gives None (the cases never act on a malformed record)."""
    if not data:
        return None
    kind = data[0]
    if 1 <= kind <= 5:
        return {"kind": kind}
    if kind < 16 or kind > 24 or len(data) < HEAD + TAG:
        return None
    session, counter = struct.unpack_from("<IQ", data, 1)
    body = data[HEAD:len(data) - TAG]
    rec = {"kind": kind, "session": session, "counter": counter}
    try:
        if kind == 20:
            stream_id, profile, sequence, plen = struct.unpack_from("<IHQH", body, 0)
            payload = body[16:]
            if plen != len(payload):
                return None
            rec.update(stream_id=stream_id, profile=profile, sequence=sequence, payload_len=plen, payload=bytes(payload))
        elif kind == 19:
            pair, stream_id, _origin, _hop, profile, carried = struct.unpack_from("<BIQBHH", body, 0)
            rec.update(pair=pair, stream_id=stream_id, profile=profile, carried=carried)
            if len(body) >= 33:                 # GUARDID2: callsign, radio id and the raw header length (the whole datagram size is `size`)
                callsign = bytes(body[18:28])
                radio_id, raw_len = struct.unpack_from("<IB", body, 28)
                rec.update(callsign=callsign.rstrip(b" \0").decode("ascii", "replace"), radio_id=radio_id, raw_len=raw_len, size=len(data))
        elif kind == 16:
            count = body[0]
            rec["pairs"] = [(body[1 + 2 * i], body[2 + 2 * i]) for i in range(count)]
        elif kind == 17:
            count = body[0]
            rec["results"] = list(body[1:1 + count])
        elif kind == 18:
            pair, profiles = struct.unpack_from("<BH", body, 0)
            rec.update(pair=pair, profiles=profiles)
        elif kind == 21:
            stream_id, reason = struct.unpack_from("<IH", body, 0)
            rec.update(stream_id=stream_id, reason=reason)
        elif kind == 22:
            stream_id, reason = struct.unpack_from("<IB", body, 0)
            rec.update(stream_id=stream_id, reason=reason)
    except (struct.error, IndexError):
        return None
    return rec


def build_units(session, counter, stream_id, profile, sequence, payload, tag=bytes(TAG)):
    """A kind-20 datagram with a caller-given tag (unit tests and the relay self-check; a real tag needs the key)."""
    body = struct.pack("<IHQH", stream_id, profile, sequence, len(payload)) + payload
    return bytes([20]) + struct.pack("<IQ", session, counter) + body + tag


def build_record(kind, session, counter, body, tag=bytes(TAG)):
    return bytes([kind]) + struct.pack("<IQ", session, counter) + body + tag


def pair_index(pairs, room=ROOM):
    """Index of the RoomMap pair whose local room is `room`: the pair bytes are ASCII letters, or letter indexes 0..25."""
    for index, (local, _remote) in enumerate(pairs):
        if local in (ord(room), ord(room) - ord("A")):
            return index
    return None


def ack_status(results, pairs, room=ROOM):
    """(status number, name) the RoomMapAck gave for `room`, or (None, reason)."""
    index = pair_index(pairs, room)
    if index is None:
        return None, "room %s is not in the RoomMap %s" % (room, pairs)
    if index >= len(results):
        return None, "the RoomMapAck has %d results for pair %d" % (len(results), index)
    return results[index], ACK_STATUS.get(results[index], "status %d" % results[index])


# ---------------------------------------------------------------- the relay

class LinkRelay:
    """A UDP forwarder between the dialling node and the accepting node of one link, with an armed one-shot drop.

    `listen_port` is where node 1's uplink dials (the tester's address), `upstream` the (ip, port) of node 2's link server. The first datagram
    on the listen socket names node 1's address (replies go there). Every datagram is forwarded unchanged and in order, except the first
    client-to-server kind-20 record whose (profile, sequence) matches the armed drop: that one is logged with dropped=True and not forwarded
    (the receiver sees a counter gap, as for real loss). `events` is the log: dicts of direction ("c2s" / "s2c"), the parsed record fields,
    `t` (monotonic) and `dropped`."""

    def __init__(self, listen_port=LINK_PORT, upstream=("127.0.0.1", SERVER_PORT), bind_ip="0.0.0.0"):
        self.upstream = upstream
        self.client = None
        self.events = []
        self.forwarded = 0
        self.dropped = 0
        self._armed = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._down = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # node 1 side
        self._down.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._down.bind((bind_ip, listen_port))
        self._up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)         # node 2 side: ONE stable source address for the server
        self._up.bind((bind_ip, 0))
        self._thread = threading.Thread(target=self._run, name="rfpos1-link-relay", daemon=True)

    @property
    def port(self):
        return self._down.getsockname()[1]

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        for sock in (self._down, self._up):
            sock.close()

    def arm(self, profile, sequence):
        """Drop the next client-to-server Units record of `profile` with `sequence`, once."""
        with self._lock:
            self._armed = (profile, sequence)

    def disarm(self):
        with self._lock:
            self._armed = None

    def mark(self):
        """A position in `events`, for records_since()."""
        with self._lock:
            return len(self.events)

    def snapshot(self, since=0):
        with self._lock:
            return list(self.events[since:])

    def _take_drop(self, rec):
        with self._lock:
            if self._armed and rec and rec["kind"] == 20 and (rec["profile"], rec["sequence"]) == self._armed:
                self._armed = None
                return True
        return False

    def _log(self, direction, rec, dropped):
        entry = {"dir": direction, "t": time.monotonic(), "dropped": dropped}
        entry.update(rec or {"kind": None})
        entry.pop("payload", None)
        with self._lock:
            self.events.append(entry)

    def _run(self):
        socks = [self._down, self._up]
        while not self._stop.is_set():
            ready, _, _ = select.select(socks, [], [], 0.05)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    continue
                if sock is self._down:
                    self.client = addr
                    rec = parse_record(data)
                    if self._take_drop(rec):
                        self.dropped += 1
                        self._log("c2s", rec, True)
                        continue
                    self._log("c2s", rec, False)
                    self._up.sendto(data, self.upstream)
                    self.forwarded += 1
                else:
                    if addr != self.upstream or self.client is None:
                        continue
                    self._log("s2c", parse_record(data), False)
                    self._down.sendto(data, self.client)
                    self.forwarded += 1


# ---------------------------------------------------------------- what the relay saw

def kinds_seen(events):
    """{kind name: count} of the logged records (dropped ones included)."""
    out = {}
    for ev in events:
        name = KIND_NAME.get(ev.get("kind"), "other")
        out[name] = out.get(name, 0) + 1
    return out


def handshake_state(events):
    """(welcomed, (status, name) of the first RoomMapAck for ROOM or None, reason) from the relay's log. welcomed: a Welcome (kind 4) went
    node 2 -> node 1. The RoomMap is the c2s record of kind 16, the ack the s2c record of kind 17."""
    welcomed = any(ev["dir"] == "s2c" and ev.get("kind") == 4 for ev in events)
    roommap = next((ev for ev in events if ev["dir"] == "c2s" and ev.get("kind") == 16), None)
    ack = next((ev for ev in events if ev["dir"] == "s2c" and ev.get("kind") == 17), None)
    if not welcomed:
        return False, None, "no Welcome seen"
    if roommap is None or ack is None:
        return True, None, "no RoomMap / RoomMapAck seen yet"
    return True, ack_status(ack["results"], roommap["pairs"]), ""


def demand_for(events, profile, since=0):
    """True when node 2 has demanded `profile` (a Demand record s2c whose mask has the bit) after log position `since`."""
    return any(ev["dir"] == "s2c" and ev.get("kind") == 18 and ev["profiles"] & (1 << profile) for ev in events[since:])


def over_records(events, profile):
    """The client-to-server Units records of `profile` of the FIRST stream that started in `events`, in arrival order, dropped ones
    included: [{stream_id, sequence, payload_len, dropped}]. The first StreamStart of that profile (kind 19) names the stream; records of
    another stream id or profile are not part of the result. Empty when no such StreamStart was seen."""
    stream = None
    for ev in events:
        if ev["dir"] == "c2s" and ev.get("kind") == 19 and ev["profile"] == profile:
            stream = ev["stream_id"]
            break
    if stream is None:
        return []
    return [{"stream_id": ev["stream_id"], "sequence": ev["sequence"], "payload_len": ev["payload_len"], "dropped": ev["dropped"]}
            for ev in events if ev["dir"] == "c2s" and ev.get("kind") == 20 and ev["profile"] == profile and ev["stream_id"] == stream]


def carried_masks(events):
    """The carried-profile masks of the client-to-server StreamStart records (kind 19), in order. link.md "Demand and transcoding": the sender
    always sends the source profile PLUS every other profile of the receiver's demand set that ITS room publishes for the stream (the output of
    its own codec worker), announced in this mask; the receiver publishes those carried units directly and never transcodes them again."""
    return [ev["carried"] for ev in events if ev["dir"] == "c2s" and ev.get("kind") == 19]


def profile_counts(events):
    """{profile id: number of client-to-server Units records} of the logged events (dropped ones included)."""
    out = {}
    for ev in events:
        if ev["dir"] == "c2s" and ev.get("kind") == 20:
            out[ev["profile"]] = out.get(ev["profile"], 0) + 1
    return out


def carry_problems(events, listener_profile):
    """Harness precondition of the transcoding cases: node 1 must NOT carry the listener's profile. When it does (it has a codec worker and node 2's
    room demands the profile), node 2 publishes the carried units directly and its own transcoder never sees the over, so a unit lost on the
    source-profile record cannot show: the output is complete from node 1's copy. A list of sentences, empty when node 2 has to transcode."""
    out = []
    masks = carried_masks(events)
    if not masks:
        out.append("no StreamStart of the over seen on the link")
    for mask in masks:
        if mask & (1 << listener_profile):
            out.append("node 1 carried %s (mask 0x%04x) and sent %s Units records of it: node 2 does not transcode a carried profile, so the "
                       "loss of a source record cannot show (node 1 needs no codec worker for that profile)" % (
                           PROFILE_NAME.get(listener_profile, listener_profile), mask, profile_counts(events).get(listener_profile, 0)))
    return out


def wire_problems(records, profile, count, dropped=None):
    """What the link wire of one over must show (link.md "Units": one unit per record, `sequence` = the 20 ms position): every record holds
    exactly one unit of the profile, positions 0..count-1 each appear once (a retransmission does not exist), and exactly the armed record is
    marked dropped. A list of sentences, empty when it holds. Positions at or above `count` (an end unit) are allowed and ignored."""
    out = []
    if not records:
        return ["no Units record of profile %s (%d) seen on the link" % (PROFILE_NAME.get(profile, "?"), profile)]
    size = UNIT_BYTES[profile]
    wrong = sorted({r["payload_len"] for r in records if r["payload_len"] != size})
    if wrong:
        out.append("a Units record holds %s bytes, one %s unit is %d" % (wrong, PROFILE_NAME[profile], size))
    seen = [r["sequence"] for r in records if r["sequence"] < count]
    missing = [q for q in range(count) if q not in seen]
    repeated = sorted({q for q in seen if seen.count(q) > 1})
    if missing:
        out.append("link positions %s never left node 1 (of 0..%d)" % (missing[:8], count - 1))
    if repeated:
        out.append("link positions %s were sent more than once" % repeated[:8])
    marked = [r["sequence"] for r in records if r["dropped"]]
    want = [] if dropped is None else [dropped]
    if marked != want:
        out.append("the relay dropped link positions %s, wanted %s" % (marked, want))
    return out


# ---------------------------------------------------------------- the D-STAR listener (the output of node 2)

def unwrap_counters(counters, period=DSTAR_PERIOD):
    """Positions of D-STAR sequence bytes in arrival order: the first counter is the first position; each later one steps forward by
    (counter - previous) modulo `period`, so a skipped counter is a skipped position. A step of 0 (the same counter again) is a repeat: it is
    returned as None at that index (a duplicate, never a position). Returns a list of positions or None, same length as `counters`."""
    out = []
    prev_pos = None
    for counter in counters:
        if prev_pos is None:
            prev_pos = counter
            out.append(counter)
            continue
        step = (counter - prev_pos) % period
        if step == 0:
            out.append(None)
            continue
        prev_pos += step
        out.append(prev_pos)
    return out


def dstar_voice(datagrams):
    """[(position, frame9)] of the D-STAR voice frames in `datagrams` (the last-frame packet excluded), for the FIRST stream id seen, in
    arrival order; repeated counters are dropped from the list and returned separately as the count `repeats`.
    Returns (list, repeats)."""
    rows = []
    stream = None
    for data in datagrams:
        parsed = dextra.parse_voice(data)
        if not parsed:
            continue
        sid, seq, last, frame = parsed
        if stream is None:
            stream = sid
        if sid != stream or last:
            continue
        rows.append((seq, frame))
    positions = unwrap_counters([seq for seq, _ in rows])
    out, repeats = [], 0
    for pos, (_seq, frame) in zip(positions, rows):
        if pos is None:
            repeats += 1
        else:
            out.append((pos, bytes(frame)))
    return out, repeats


def gap_problems(got, count, dropped=None, expected=None):
    """Judge the positions a D-STAR listener of node 2 got for an over of `count` 20 ms units (positions 0..count-1).

    With a loss at `dropped`: that one position is absent and every other position 0..count-1 is present once, at its own position (so the
    output does not end one frame early, the pre-RFPOS1 shape: a contiguous run 0..count-2). With `dropped=None` all positions are present.
    `expected` (a list of `count` frames, or None) is compared byte for byte at every present position: a shifted stream shows as a mismatch.
    A list of sentences, empty when it holds. Positions at or above `count` (the end unit) are ignored."""
    out = []
    by_pos = {}
    for pos, frame in got:
        if pos in by_pos:
            out.append("position %d arrived twice" % pos)
        by_pos[pos] = frame
    if not by_pos:
        return ["no D-STAR voice frame reached the listener of node 2"]
    missing = [q for q in range(count) if q not in by_pos]
    want_missing = [] if dropped is None else [dropped]
    if missing != want_missing:
        out.append("missing positions %s, wanted %s (lost count %d, wanted %d)" % (missing[:10], want_missing, len(missing), len(want_missing)))
    top = max(p for p in by_pos if p < count) if any(p < count for p in by_pos) else -1
    if top < count - 1 and dropped != count - 1:
        out.append("the output ends at position %d, before the last one (%d): a truncated stream" % (top, count - 1))
    if expected is not None:
        wrong = [q for q in range(count) if q in by_pos and by_pos[q] != expected[q]]
        if wrong:
            out.append("frames at positions %s differ from the expected unit of that position (a shifted or wrong unit), first %s" % (
                wrong[:8], wrong[0]))
    return out
