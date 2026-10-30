# SPDX-License-Identifier: MIT
"""M17 in xlxd reflector-client mode (UDP 17000): CONN/ACKN/PING/... and
54 B voice packets (frame number, EOT bit, LICH constant per stream)."""
from ..proto import m17 as M
from .common import ERROR, WARN, Live, Stream, finish_stream, flow_of, seq_finding, touches, voice_frames, Departures

NAME = "m17"
# control packets: tag -> allowed sizes
CONTROL = {b"CONN": (11,), b"LSTN": (11,), b"ACKN": (4,), b"NACK": (4,), b"PING": (10,),
           b"PONG": (10,), b"DISC": (4, 10)}


class Checker:
    def __init__(self, report):
        self.report = report
        self.streams = Live(self._ended)
        self.linked = set()
        self.left = Departures(report, NAME, 0.040, "after_disc")
        self.starts = []          # ts of fn 0 voice packets sent to the reflector

    def claims(self, d):
        return M.PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self.streams.expire(d.ts)
        tag = p[:4]
        if tag == b"M17 ":
            kind = "voice"
            size_ok = len(p) == M.PACKET_SIZE
        elif tag in CONTROL:
            kind = tag.decode().lower()
            size_ok = len(p) in CONTROL[tag]
        else:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B packet with no known M17 tag" % len(p))
            return
        r.count(NAME, d, kind)
        if not size_ok:
            r.error(NAME, d, "bad_length", "%s of %d B" % (kind.upper(), len(p)))
            return
        peer = (d.src, d.sport) if d.dport == M.PORT else (d.dst, d.dport)
        if kind in ("conn", "lstn"):
            self.linked.add(peer)
            self.left.back(peer)
        elif kind == "disc" and d.dport == M.PORT:
            # the listener left: its live streams end here (no silence timeout)
            for key in [k for k in self.streams.live if touches(k[0], peer)]:
                self.streams.end(key, "peer disconnected")
            self.left.leave(peer, d)
        if kind != "voice":
            return
        if d.dport != M.PORT and self.left.sent(peer, d, "EOT frame" if p[34] & 0x80 else "voice"):
            return
        if peer not in self.linked:
            self.linked.add(peer)
            r.warn(NAME, d, "unlinked_peer", "voice on a flow that sent no CONN in this capture")
        stream_id = int.from_bytes(p[4:6], "big")
        value = int.from_bytes(p[34:36], "big")
        fn, eot = value & 0x7FFF, bool(value & 0x8000)
        if fn == 0 and d.dport == M.PORT and d.ts is not None:
            self.starts.append(d.ts)
        key = (flow_of(d), stream_id)
        st = self.streams.live.get(key)
        if st is None:
            old = self.streams.after_end(key)
            if old is not None:
                r.error(NAME, d, "after_eot", "frame %d of stream %04x after its EOT (frame %d)" % (
                    fn, stream_id, old.last_frame))
                return
            st = Stream(NAME, key[0], "stream %04x" % stream_id, 0x8000, 0.040, d)
            st.lich = p[6:34]
            self.streams.live[key] = st
            if fn != 0:
                st.notes["late_entry_fn"] = fn
        elif p[6:34] != st.lich:
            r.error(NAME, d, "lich_changed", "LICH of stream %04x differs from frame %d" % (
                stream_id, st.first_frame))
        seq_finding(r, NAME, d, st, st.add(d, fn), "fn")
        voice_frames(st, [p[36:44], p[44:52]])
        if eot:
            self.streams.terminate(key, d)

    def _ended(self, st):
        if st.end == "peer disconnected":
            self.report.add(NAME, st.flow, st.last_frame, WARN, "no_eot",
                            "%s cut by the peer DISC (%d packets)" % (st.name, st.frames))
        elif st.end != "terminated":
            sev = ERROR if st.end == "timeout" else WARN
            self.report.add(NAME, st.flow, st.last_frame, sev, "no_eot",
                            "%s ended by %s without an EOT frame (%d packets)" % (st.name, st.end, st.frames))
        finish_stream(self.report, st)

    def finish(self, end_ts):
        self.streams.finish()
