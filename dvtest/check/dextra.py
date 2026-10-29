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
    """(kind, DSVT bytes) of a DExtra packet; the DSVT bytes are p for a
    header or voice packet, None otherwise; (None, None) when it is no
    DExtra packet. Other D-STAR family checkers pass their own kind_of
    that maps their header/voice packets onto this DSVT layout."""
    if len(p) == D.HEADER_SIZE and p.startswith(b"DSVT") and p[4] == 0x10:
        return "header", p
    if len(p) == D.VOICE_SIZE and p.startswith(b"DSVT") and p[4] == 0x20:
        return "voice", p
    if len(p) == 9 and p[8] == 0:
        return "keepalive", None
    if len(p) == 11 and p[10] == 0:
        return ("unlink" if p[9:10] == b" " else "link"), None
    if len(p) == 14 and p[10:14] in (b"ACK\x00", b"NAK\x00"):
        return p[10:13].decode().lower(), None
    return None, None


class Checker:
    def __init__(self, report, m17=None, name=NAME, port=D.PORT, kinds=kind_of, implicit_header=False):
        """m17: the dvtest.check.m17 Checker of the same capture (its
        fn 0 times mark M17-sourced streams). name/port/kinds let the XLX,
        DPlus and DCS checkers reuse the DSVT stream rules; implicit_header:
        every voice packet carries the header (DCS), none is sent alone."""
        self.report = report
        self.m17 = m17
        self.name, self.port, self.kind_of = name, port, kinds
        self.implicit_header = implicit_header
        self.streams = Live(self._ended)
        self.linked = set()       # client (ip, port) that sent a link

    def claims(self, d):
        return self.port in (d.sport, d.dport)

    def feed(self, d):
        r = self.report
        self.streams.expire(d.ts)
        kind, p = self.kind_of(d.payload)
        r.count(self.name, d, kind or "unparseable")
        if kind is None:
            r.error(self.name, d, "unparseable", "%d B datagram is no link/keepalive/header/voice"
                    % len(d.payload))
            return
        client = (d.src, d.sport) if d.dport == self.port else (d.dst, d.dport)
        if kind == "link":
            self.linked.add(client)
        if kind not in ("header", "voice"):
            return
        if client not in self.linked:
            self.linked.add(client)
            r.warn(self.name, d, "unlinked_peer", "DSVT on a flow that sent no link in this capture")
        stream_id = int.from_bytes(p[12:14], "little")
        key = (flow_of(d), stream_id)
        st = self.streams.live.get(key)
        if kind == "header":
            if st is None:
                st = self._new(key, d, stream_id, True)
                st.header = p[15:]
            elif st.header is None:
                r.warn(self.name, d, "late_header", "header after voice frames of stream %04x" % stream_id)
                st.header = p[15:]
            elif st.header != p[15:]:
                r.error(self.name, d, "header_changed", "stream %04x header differs from frame %d" % (
                    stream_id, st.first_frame))
            else:
                st.notes["header_repeats"] += 1
            return
        seq, last = p[14] & 0x1F, bool(p[14] & 0x40)
        if st is None:
            old = self.streams.after_end(key)
            if old is not None:
                r.error(self.name, d, "after_last", "voice for stream %04x after its last frame (frame %d)" % (
                    stream_id, old.last_frame))
                return
            st = self._new(key, d, stream_id, self.implicit_header)
            if not self.implicit_header:
                r.warn(self.name, d, "no_header", "voice for stream %04x without a header" % stream_id)
        if p[14] & 0x80 or seq > 20:
            r.error(self.name, d, "bad_counter", "frame counter byte %02x" % p[14])
            return
        if st.frames == 0 and st.expect == 0 and seq == 2 and self._from_m17(st.t0):
            st.expect = None
            st.loss = 2
            r.warn(self.name, d, "seq_loss", "sequence jump, 2 frame(s) missing in %s"
                   " (upstream: M17 frame 0 dropped)" % st.name)
        seq_finding(r, self.name, d, st, st.add(d, seq))
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
        st = Stream(self.name, key[0], "stream %04x" % stream_id, 21, 0.020, d, 0 if header else None)
        st.header = None
        self.streams.live[key] = st
        return st

    def _ended(self, st):
        r = self.report
        if st.end != "terminated" and st.frames == 0:
            r.add(self.name, st.flow, st.last_frame, WARN, "header_only",
                  "%s: header(s) only, no voice and no last frame (ended by %s)" % (st.name, st.end))
        elif st.end != "terminated":
            sev = ERROR if st.end == "timeout" else WARN
            r.add(self.name, st.flow, st.last_frame, sev, "no_last",
                  "%s ended by %s without a last frame (%d frames)" % (st.name, st.end, st.frames))
        for note, text in (("sync_missing", "frame 0 without the sync pattern 55 2d 16"),
                           ("sync_misplaced", "sync pattern outside frame 0")):
            if st.notes[note]:
                if not st.notes["voice_nonzero"]:
                    text += " (all AMBE frames zero: transcoder bypass, upstream)"
                r.add(self.name, st.flow, st.first_frame, WARN, note, "%s: %d x %s" % (st.name, st.notes[note], text))
        finish_stream(r, st)

    def finish(self, end_ts):
        self.streams.finish()
