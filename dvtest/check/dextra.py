# SPDX-License-Identifier: MIT
"""D-STAR over DExtra (UDP 30001): link/keepalive packets and DSVT
streams (header, voice frames 0..20, last frame with bit 0x40)."""
from ..proto import dextra as D
from .common import ERROR, WARN, Live, Stream, finish_stream, flow_of, seq_finding

NAME = "dextra"
# xlxd (cm17protocol.cpp) accepts M17 voice only for frame number > 0, so a
# D-STAR stream relayed from M17 starts at frame 2; the M17 fn 0 packet
# reaches the reflector this close before the D-STAR header
M17_LEAD = 1.0


def kind_of(p):
    """Packet kind name, or None when it is no DExtra packet."""
    if len(p) == D.HEADER_SIZE and p.startswith(b"DSVT") and p[4] == 0x10:
        return "header"
    if len(p) == D.VOICE_SIZE and p.startswith(b"DSVT") and p[4] == 0x20:
        return "voice"
    if len(p) == 9 and p[8] == 0:
        return "keepalive"
    if len(p) == 11 and p[10] == 0:
        return "unlink" if p[9:10] == b" " else "link"
    if len(p) == 14 and p[10:14] in (b"ACK\x00", b"NAK\x00"):
        return p[10:13].decode().lower()
    return None


class Checker:
    def __init__(self, report, m17=None):
        """m17: the dvtest.check.m17 Checker of the same capture (its
        fn 0 times mark M17-sourced streams)."""
        self.report = report
        self.m17 = m17
        self.streams = Live(self._ended)
        self.linked = set()       # client (ip, port) that sent a link

    def claims(self, d):
        return D.PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self.streams.expire(d.ts)
        kind = kind_of(p)
        r.count(NAME, d, kind or "unparseable")
        if kind is None:
            what = "DSVT" if p.startswith(b"DSVT") else "packet"
            r.error(NAME, d, "unparseable", "%d B %s is no link/keepalive/header/voice" % (len(p), what))
            return
        client = (d.src, d.sport) if d.dport == D.PORT else (d.dst, d.dport)
        if kind == "link":
            self.linked.add(client)
        if kind not in ("header", "voice"):
            return
        if client not in self.linked:
            self.linked.add(client)
            r.warn(NAME, d, "unlinked_peer", "DSVT on a flow that sent no link in this capture")
        stream_id = int.from_bytes(p[12:14], "little")
        key = (flow_of(d), stream_id)
        st = self.streams.live.get(key)
        if kind == "header":
            if st is None:
                st = self._new(key, d, stream_id, True)
                st.header = p[15:]
            elif st.header is None:
                r.warn(NAME, d, "late_header", "header after voice frames of stream %04x" % stream_id)
                st.header = p[15:]
            elif st.header != p[15:]:
                r.error(NAME, d, "header_changed", "stream %04x header differs from frame %d" % (
                    stream_id, st.first_frame))
            else:
                st.notes["header_repeats"] += 1
            return
        seq, last = p[14] & 0x1F, bool(p[14] & 0x40)
        if st is None:
            old = self.streams.after_end(key)
            if old is not None:
                r.error(NAME, d, "after_last", "voice for stream %04x after its last frame (frame %d)" % (
                    stream_id, old.last_frame))
                return
            st = self._new(key, d, stream_id, False)
            r.warn(NAME, d, "no_header", "voice for stream %04x without a header" % stream_id)
        if p[14] & 0x80 or seq > 20:
            r.error(NAME, d, "bad_counter", "frame counter byte %02x" % p[14])
            return
        if st.frames == 0 and st.expect == 0 and seq == 2 and self._from_m17(st.t0):
            st.expect = None
            st.loss = 2
            r.warn(NAME, d, "seq_loss", "sequence jump, 2 frame(s) missing in %s"
                   " (upstream: M17 frame 0 dropped)" % st.name)
        seq_finding(r, NAME, d, st, st.add(d, seq))
        if not last and any(p[15:24]):
            st.notes["voice_nonzero"] += 1
        sync = p[24:27] == D.SYNC_DATA
        if seq == 0 and not sync and not last:
            st.notes["sync_missing"] += 1
        elif seq != 0 and sync:
            st.notes["sync_misplaced"] += 1
        if last:
            self.streams.terminate(key, d)

    def _from_m17(self, header_ts):
        return self.m17 is not None and header_ts is not None and any(
            0 <= header_ts - ts <= M17_LEAD for ts in self.m17.starts)

    def _new(self, key, d, stream_id, header):
        st = Stream(NAME, key[0], "stream %04x" % stream_id, 21, 0.020, d, 0 if header else None)
        st.header = None
        self.streams.live[key] = st
        return st

    def _ended(self, st):
        r = self.report
        if st.end != "terminated" and st.frames == 0:
            r.add(NAME, st.flow, st.last_frame, WARN, "header_only",
                  "%s: header(s) only, no voice and no last frame (ended by %s)" % (st.name, st.end))
        elif st.end != "terminated":
            sev = ERROR if st.end == "timeout" else WARN
            r.add(NAME, st.flow, st.last_frame, sev, "no_last",
                  "%s ended by %s without a last frame (%d frames)" % (st.name, st.end, st.frames))
        for note, text in (("sync_missing", "frame 0 without the sync pattern 55 2d 16"),
                           ("sync_misplaced", "sync pattern outside frame 0")):
            if st.notes[note]:
                if not st.notes["voice_nonzero"]:
                    text += " (all AMBE frames zero: transcoder bypass, upstream)"
                r.add(NAME, st.flow, st.first_frame, WARN, note, "%s: %d x %s" % (st.name, st.notes[note], text))
        finish_stream(r, st)

    def finish(self, end_ts):
        self.streams.finish()
