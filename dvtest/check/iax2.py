# SPDX-License-Identifier: MIT
"""IAX2 (RFC 5456, UDP 4569): per call oseqno/iseqno continuity, implicit
acknowledgement through iseqno, timestamps, call lifecycle and voice
(full and mini frames) against the negotiated codec."""
import struct

from ..proto import iax2 as I
from .common import STREAM_TIMEOUT, WARN, Stream, finish_stream, flow_of, seq_finding

NAME = "iax2"
PORT = 4569
INVAL, LAGRQ, LAGRP, TXCNT, TXACC, POKE = 10, 11, 12, 23, 24, 30
# full frames that consume no oseqno and need no acknowledgement
NO_ACK = (I.ACK, INVAL, I.VNAK, TXCNT, TXACC)
# replies that echo the timestamp of the frame they answer
ECHO_TS = (I.ACK, I.PONG, LAGRP, I.VNAK, INVAL)
# bytes per 20 ms frame by IAX format bit
FRAME_BYTES = {I.SLIN: 320, 4: 160, 8: 160, 2: 33}
FRAME_NAMES = {1: "dtmf", 2: "voice", 3: "video", 4: "control", 5: "null", 6: "iax", 7: "text",
               8: "image", 9: "html", 10: "cng"}
IAX_NAMES = {1: "new", 2: "ping", 3: "pong", 4: "ack", 5: "hangup", 6: "reject", 7: "accept",
             8: "authreq", 9: "authrep", 10: "inval", 11: "lagrq", 12: "lagrp", 13: "regreq",
             14: "regauth", 15: "regack", 16: "regrej", 17: "regrel", 18: "vnak", 30: "poke",
             40: "calltoken"}


class Call:
    def __init__(self, d, caller):
        self.first = d
        self.caller = caller          # (ip, port, callno)
        self.state = "new"
        self.offered = set()
        self.format = None
        self.ended = None             # frame number of HANGUP/REJECT
        self.dirs = {}
        self.warned = set()

    def side(self, key):
        if key not in self.dirs:
            self.dirs[key] = {"next": 0, "sent": {}, "ack_upto": 0, "ts": None, "iseq": None,
                              "voice": None, "vts": None, "retransmits": 0}
        return self.dirs[key]


class Checker:
    def __init__(self, report):
        self.report = report
        self.calls = {}              # (ip, port, callno) -> Call
        self.all_calls = []
        self.unattached = 0

    def claims(self, d):
        return PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self._expire(d.ts)
        if len(p) < 4:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B datagram" % len(p))
            return
        if p[0] & 0x80:
            self._full(d, p)
        elif p[:2] == b"\x00\x00":
            r.count(NAME, d, "meta")
            r.warn(NAME, d, "meta_frame", "meta/trunk frame (not checked)")
        else:
            self._mini(d, p)

    def _full(self, d, p):
        r = self.report
        if len(p) < 12:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B full frame" % len(p))
            return
        try:
            f = I.parse_full(p)
        except ValueError as exc:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", str(exc))
            return
        retransmit = bool(p[2] & 0x80)
        kind = FRAME_NAMES.get(f.kind, "type%d" % f.kind)
        if f.kind == I.IAX:
            kind = IAX_NAMES.get(f.subclass, "iax%d" % f.subclass)
        r.count(NAME, d, kind)
        me, peer = (d.src, d.sport, f.source), (d.dst, d.dport, f.destination)
        call = self.calls.get(me)
        if f.kind == I.IAX and f.subclass == I.NEW and f.destination == 0 and not retransmit:
            if call is not None and call.state != "ended":
                r.error(NAME, d, "new_in_call", "NEW on a call that is still up (frame %d)" % call.first.frame)
            call = Call(d, me)
            self.calls[me] = call
            self.all_calls.append(call)
            ies = _ies(r, d, f.payload)
            call.offered = {_int(ies.get(9))} | _bits(_int(ies.get(8)))
        elif call is None and f.destination and peer in self.calls:
            call = self.calls[peer]
            self.calls[me] = call
        if call is None:
            if f.kind == I.IAX and f.subclass in (POKE, I.PONG, INVAL, I.ACK, I.CALLTOKEN) or \
                    f.kind == I.IAX and 13 <= f.subclass <= 17:
                self.unattached += 1
                return
            r.error(NAME, d, "unknown_call", "%s frame for call %d that no NEW in this capture opened" % (
                kind, f.source))
            return
        mine = call.side(me)
        theirs = call.side(peer) if f.destination else {"next": 0, "sent": {}}
        sub = f.subclass if f.kind == I.IAX else None
        after = call.ended is not None and not (sub == I.ACK)
        if after:
            r.warn(NAME, d, "after_hangup", "%s after the call ended at frame %d" % (kind, call.ended))
        # implicit acknowledgement: iseqno is the next frame expected from the peer
        if sub != INVAL:
            if mine["iseq"] is not None and 0 < ((mine["iseq"] - f.in_sequence) & 255) < 128:
                r.warn(NAME, d, "iseq_backwards", "iseqno %d after %d" % (f.in_sequence, mine["iseq"]))
            elif 0 < ((f.in_sequence - theirs["next"]) & 255) < 128:
                r.error(NAME, d, "iseq_ahead", "iseqno %d acknowledges frames the peer never sent (next %d)" % (
                    f.in_sequence, theirs["next"]))
            else:
                mine["iseq"] = f.in_sequence
                for seq in [s for s in theirs["sent"] if 0 < ((f.in_sequence - s) & 255) < 128]:
                    del theirs["sent"][seq]
        if sub in NO_ACK:
            return
        if retransmit:
            mine["retransmits"] += 1
            if 0 < ((mine["next"] - f.out_sequence) & 255) < 128:
                return
            r.warn(NAME, d, "retransmit_unknown", "retransmit of oseqno %d that was never sent" % f.out_sequence)
        if f.out_sequence != mine["next"]:
            behind = 0 < ((mine["next"] - f.out_sequence) & 255) < 128
            r.error(NAME, d, "oseq_dup" if behind else "oseq_gap", "oseqno %d, expected %d (%s)" % (
                f.out_sequence, mine["next"], kind))
        mine["next"] = (f.out_sequence + 1) & 255
        if not after:
            mine["sent"][f.out_sequence] = d
        if sub not in ECHO_TS:
            if mine["ts"] is not None and f.timestamp < mine["ts"]:
                r.warn(NAME, d, "ts_backwards", "timestamp %d after %d (%s)" % (f.timestamp, mine["ts"], kind))
            mine["ts"] = max(f.timestamp, mine["ts"] or 0)
        if f.kind == I.IAX:
            self._command(d, f, call, kind)
        elif f.kind == I.VOICE:
            self._voice(d, call, mine, f.timestamp, f.payload, f.subclass, True)

    def _command(self, d, f, call, kind):
        r = self.report
        if f.subclass == I.ACCEPT:
            ies = _ies(r, d, f.payload)
            call.format = _int(ies.get(9))
            if call.state != "new":
                r.error(NAME, d, "accept_twice", "ACCEPT on a call in state %s" % call.state)
            call.state = "up"
            if call.format and call.offered - {None} and call.format not in call.offered:
                r.error(NAME, d, "accept_format", "ACCEPT format %d was not offered in NEW" % call.format)
        elif f.subclass in (I.HANGUP, I.REJECT):
            if call.ended is None:
                call.ended = d.frame
                call.end_kind = kind
                call.end_ts = d.ts
                if f.subclass == I.REJECT and call.state == "up":
                    r.error(NAME, d, "reject_after_accept", "REJECT on an accepted call")

    def _mini(self, d, p):
        r = self.report
        r.count(NAME, d, "mini")
        call = self.calls.get((d.src, d.sport, struct.unpack("!H", p[:2])[0]))
        if call is None:
            r.error(NAME, d, "unknown_call", "mini frame for call %d that no NEW in this capture opened" %
                    struct.unpack("!H", p[:2])[0])
            return
        mine = call.side((d.src, d.sport, struct.unpack("!H", p[:2])[0]))
        if mine["vts"] is None:
            if call.state != "up":
                r.error(NAME, d, "voice_before_accept", "mini frame in call state %s" % call.state)
            else:
                r.error(NAME, d, "mini_first", "mini frame before any full voice frame")
            return
        ts = (mine["vts"] & ~0xFFFF) | struct.unpack("!H", p[2:4])[0]
        if ts < mine["vts"] - 0x8000:
            ts += 0x10000
        self._voice(d, call, mine, ts, p[4:], call.format, False)

    def _voice(self, d, call, mine, ts, payload, codec, full):
        r = self.report
        if call.state != "up":
            r.error(NAME, d, "voice_before_accept", "voice in call state %s" % call.state)
        if call.format and codec != call.format:
            r.error(NAME, d, "codec_mismatch", "voice codec %d, ACCEPT chose %d" % (codec, call.format))
        size = FRAME_BYTES.get(codec)
        frames = 1
        if size is None:
            if codec not in call.warned:
                call.warned.add(codec)
                r.warn(NAME, d, "codec_unknown", "voice codec %s: payload size not checked" % codec)
        elif not payload or len(payload) % size:
            r.error(NAME, d, "voice_size", "%d B voice, codec %d needs a multiple of %d" % (len(payload), codec, size))
        else:
            frames = len(payload) // size
        st = mine["voice"]
        if full or st is None:
            if st is not None:
                st.end = "resync"
                finish_stream(r, st)
            st = mine["voice"] = Stream(NAME, flow_of(d), "voice call %d from frame %d" % (
                call.caller[2], d.frame), 1 << 32, 0.020, d)
            st.base = ts
        mine["vts"] = ts
        if (ts - st.base) % 20:
            st.notes["ts_not_20ms"] += 1
        seq_finding(r, NAME, d, st, st.add(d, (ts - st.base) // 20), "voice_ts")
        st.ext += frames - 1

    def _expire(self, now):
        if now is None:
            return
        for call in self.all_calls:
            for side in call.dirs.values():
                for seq, sent in list(side["sent"].items()):
                    if now - sent.ts > STREAM_TIMEOUT:
                        del side["sent"][seq]
                        self.report.error(NAME, sent, "no_ack", "oseqno %d never acknowledged" % seq)

    def finish(self, end_ts):
        self._expire(end_ts)
        r = self.report
        calls = []
        for call in self.all_calls:
            for side in call.dirs.values():
                if side["voice"] is not None:
                    side["voice"].end = "call end"
                    finish_stream(r, side["voice"])
            if call.ended is None:
                r.add(NAME, flow_of(call.first), call.first.frame, WARN, "call_open",
                      "call %d still up at capture end (no HANGUP/REJECT)" % call.caller[2])
            calls.append({"first_frame": call.first.frame, "caller": "%s:%d/%d" % call.caller,
                          "state": call.state if call.ended is None else call.end_kind,
                          "format": call.format,
                          "retransmits": sum(s["retransmits"] for s in call.dirs.values())})
        p = r.proto(NAME)
        p["calls"] = calls
        p["unattached"] = self.unattached


def _ies(report, d, payload):
    try:
        return I.parse_information(payload)
    except ValueError as exc:
        report.error(NAME, d, "bad_ie", str(exc))
        return {}


def _int(raw):
    if raw is None or len(raw) not in (1, 2, 4, 8):
        return None
    return int.from_bytes(raw, "big")


def _bits(mask):
    return {1 << n for n in range(64) if mask and mask & (1 << n)}
