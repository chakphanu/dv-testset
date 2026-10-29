# SPDX-License-Identifier: MIT
"""DMR over MMDVM/Homebrew (UDP 62030): login/keepalive packets and DMRD
streams (voice LC header, bursts A..F with sync on A, terminator)."""
from ..proto import dmr as M
from .common import ERROR, WARN, Live, Stream, finish_stream, flow_of, seq_finding, voice_frames

NAME = "dmr"
# control packets: tag -> allowed sizes (None = any size)
CONTROL = ((b"RPTPING", (11,)), (b"RPTACK", (6, 10)), (b"RPTCL", (9, 13)), (b"RPTL", (8,)),
           (b"RPTK", (40,)), (b"RPTC", (302,)), (b"RPTO", None), (b"RPTSBKN", (11,)),
           (b"MSTPONG", (11,)), (b"MSTNAK", (6, 10)), (b"MSTCL", (5, 9)), (b"DMRA", None))
DMRD_SIZES = (53, M.PACKET_SIZE)
HEADER, TERMINATOR = 1, 2
# xlxd only honours the 13 B RPTCL (IsValidDisconnectPacket); the 9 B
# "RPTCL" + id form is ignored by the reflector
RPTCL_SIZE = 13
# xlxd refreshes its client cache every 1.0 s (RefreshClientCache), so
# DMRD to a removed client inside that window is a known upstream effect
# (a warning; dvtest.check.differential() raises it on sut-only captures)
CACHE_WINDOW = 1.0


def kind_of(p):
    """(kind, size ok) or (None, False)."""
    if p.startswith(b"DMRD"):
        return "dmrd", len(p) in DMRD_SIZES
    for tag, sizes in CONTROL:
        if p.startswith(tag):
            return tag.decode().lower(), sizes is None or len(p) in sizes
    return None, False


class Checker:
    def __init__(self, report):
        self.report = report
        self.streams = Live(self._ended)
        self.logged = set()       # repeater (ip, port) that sent RPTL
        self.closed = {}          # peer -> (frame, ts, tag) of MSTCL/RPTCL

    def claims(self, d):
        return M.PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self.streams.expire(d.ts)
        kind, size_ok = kind_of(p)
        if kind == "dmrd" and size_ok:
            bits = p[15]
            kind = "voice_sync" if bits & 0x30 == 0x10 else "voice" if bits & 0x30 == 0 else \
                "header" if bits & 0x3F == 0x20 | HEADER else "terminator" if bits & 0x3F == 0x20 | TERMINATOR else \
                "data" if bits & 0x30 == 0x20 else "bad_type"
        r.count(NAME, d, kind or "unparseable")
        if kind is None:
            r.error(NAME, d, "unparseable", "%d B packet with no known MMDVM tag" % len(p))
            return
        if not size_ok:
            r.error(NAME, d, "bad_length", "%s of %d B" % (kind.upper(), len(p)))
            return
        peer = (d.src, d.sport) if d.dport == M.PORT else (d.dst, d.dport)
        if kind == "rptl":
            self.logged.add(peer)
            self.closed.pop(peer, None)
        elif kind == "rptcl" and len(p) != RPTCL_SIZE:
            r.warn(NAME, d, "close_ignored", "%d B RPTCL, the reflector only honours the %d B form" % (
                len(p), RPTCL_SIZE))
        elif kind in ("mstcl", "rptcl"):
            self.closed[peer] = (d.frame, d.ts, kind.upper())
            cut = [k for k in self.streams.live if _of_peer(k[0], peer)]
            for key in cut:
                self.streams.end(key, "peer closed")
            if cut:     # precondition of after_close for the ref/sut comparison
                p_ = r.proto(NAME)
                p_["closes_mid_stream"] = p_.get("closes_mid_stream", 0) + 1
        if kind not in ("voice", "voice_sync", "header", "terminator", "data", "bad_type"):
            return
        if peer in self.closed:
            frame, ts, tag = self.closed[peer]
            text = "DMRD after the %s at frame %d closed this peer" % (tag, frame)
            if tag == "MSTCL" and d.ts is not None and ts is not None and d.ts - ts <= CACHE_WINDOW:
                r.warn(NAME, d, "after_close_cache", text + " (upstream: client cache not invalidated"
                        " on remove, within %.1f s)" % CACHE_WINDOW)
            else:
                r.error(NAME, d, "after_close", text)
            return
        if peer not in self.logged:
            self.logged.add(peer)
            r.warn(NAME, d, "unlinked_peer", "DMRD on a flow that sent no RPTL login in this capture")
        if kind == "bad_type":
            r.error(NAME, d, "bad_type", "DMRD frame type bits %02x" % (bits & 0x30))
            return
        if kind == "data":
            return
        stream_id = int.from_bytes(p[16:20], "little")
        key = (flow_of(d), stream_id)
        fields = (p[5:8], p[8:11], bits & 0xC0)
        st = self.streams.live.get(key)
        if st is None:
            if kind != "header" and self.streams.after_end(key) is not None:
                r.error(NAME, d, "after_terminator", "%s for stream %08x after its terminator" % (kind, stream_id))
                return
            st = Stream(NAME, key[0], "stream %08x" % stream_id, 256, 0.060, d)
            st.fields, st.vseq = fields, None
            self.streams.live[key] = st
            if kind != "header":
                r.warn(NAME, d, "no_header", "%s opens stream %08x without a voice LC header" % (kind, stream_id))
        elif st.fields != fields:
            r.error(NAME, d, "field_changed", "src/dst/slot changed inside stream %08x" % stream_id)
        result = st.add(d, p[4])
        seq_finding(r, NAME, d, st, result)
        if kind == "header":
            if st.frames > 1:
                st.notes["header_repeats"] += 1
            st.gap()
            return
        if kind == "terminator":
            self.streams.terminate(key, d)
            return
        st.notes["voice"] += 1
        voice_frames(st, M.extract(p[20:53]))
        vseq = bits & 0x0F
        if vseq > 5 or (kind == "voice_sync") != (vseq == 0):
            r.error(NAME, d, "burst_type", "burst %s marked %s" % (
                chr(65 + vseq) if vseq <= 5 else "?%d" % vseq, kind))
        if st.vseq is None:
            st.notes["first_burst_" + chr(65 + min(vseq, 6))] += 1
        elif result is None and vseq != (st.vseq[0] + st.ext - st.vseq[1]) % 6:
            r.error(NAME, d, "burst_order", "burst %s after burst %s in stream %08x" % (
                chr(65 + min(vseq, 6)), chr(65 + st.vseq[0]), stream_id))
        st.vseq = (min(vseq, 5), st.ext)

    def _ended(self, st):
        r = self.report
        if st.end == "peer closed":
            r.add(NAME, st.flow, st.last_frame, WARN, "no_terminator",
                  "%s cut by the peer close (%d packets)" % (st.name, st.frames))
        elif st.end != "terminated" and st.notes["voice"] == 0:
            r.add(NAME, st.flow, st.last_frame, WARN, "header_only",
                  "%s: header(s) only, no voice and no terminator (ended by %s)" % (st.name, st.end))
        elif st.end != "terminated":
            sev = ERROR if st.end == "timeout" else WARN
            r.add(NAME, st.flow, st.last_frame, sev, "no_terminator",
                  "%s ended by %s without a terminator (%d packets)" % (st.name, st.end, st.frames))
        first = [k for k in st.notes if k.startswith("first_burst_")]
        if first and first[0] != "first_burst_A":
            r.add(NAME, st.flow, st.first_frame, WARN, "first_burst",
                  "%s: first voice burst is %s, not A" % (st.name, first[0][-1]))
        finish_stream(r, st)

    def finish(self, end_ts):
        self.streams.finish()



def _of_peer(flow, peer):
    """flow (flow_of) runs between the reflector and this repeater."""
    end = "%s:%d" % peer
    return flow.startswith(end + ">") or flow.endswith(">" + end)
