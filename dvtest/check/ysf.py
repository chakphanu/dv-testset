# SPDX-License-Identifier: MIT
"""YSF (UDP 42000): YSFP poll -> ack and YSFS -> 42 B status pairing,
YSFU, and 155 B YSFD streams: sync, FICH CRC, V/D mode 2 header ->
voice (FN cycling 0..FT, one packet per 100 ms) -> terminator with the
EOT bit only on the terminator. WiresX (data FR) packets are counted."""
from ..proto import ysf as Y
from .common import ERROR, REPLY_TIMEOUT, WARN, Live, Stream, dmr_form, finish_stream, flow_of, seq_finding, voice_frames

NAME = "ysf"


class Checker:
    def __init__(self, report):
        self.report = report
        self.streams = Live(self._ended)
        self.pending = []         # [(d, kind)] polls / status requests waiting for the reply

    def claims(self, d):
        return Y.PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self.streams.expire(d.ts)
        self._expire(d.ts)
        to_reflector = d.dport == Y.PORT
        if len(p) == Y.POLL_SIZE and p[:4] in (b"YSFP", b"YSFU"):
            if not to_reflector:
                r.count(NAME, d, "poll_ack")
                self._answer(d, "poll", Y.is_poll_ack(p), "poll ack")
            else:
                kind = Y.parse_poll(p)["kind"]
                r.count(NAME, d, kind)
                if kind == "poll":
                    self.pending.append((d, "poll"))
            return
        if p == Y.status_request() and to_reflector:
            r.count(NAME, d, "status")
            self.pending.append((d, "status"))
            return
        if Y.parse_status(p) is not None and not to_reflector:
            r.count(NAME, d, "status_reply")
            self._answer(d, "status", True, "status reply")
            return
        info = Y.parse(p)
        if info is None:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B datagram is no YSFP/YSFU/YSFS/YSFD packet" % len(p))
            return
        kind = info["kind"] or "fich_bad"
        r.count(NAME, d, kind)
        if not info["sync_ok"]:
            r.error(NAME, d, "bad_sync", "YSFD frame without the sync D4 71 C9 63 4D")
        if info["fich"] is None:
            r.error(NAME, d, "fich_crc", "FICH does not decode (CRC wrong after FEC)")
            return
        if kind == "data":
            return
        if kind == "other":
            r.warn(NAME, d, "other_mode", "FICH FI %d DT %d is no V/D mode 2 traffic (not relayed by xlxd)" % (
                info["fich"]["fi"], info["fich"]["dt"]))
            return
        if info["eot"] != (kind == "terminator"):
            r.error(NAME, d, "eot_bit", "%s with the EOT bit %s" % (kind, "set" if info["eot"] else "clear"))
        key = (flow_of(d), info["src"])
        st = self.streams.live.get(key)
        if kind == "header":
            if st is None:
                st = self._new(key, d, info, True)
            else:
                st.notes["header_repeats"] += 1
            return
        if st is None:
            if self.streams.after_end(key) is not None:
                r.error(NAME, d, "after_terminator", "%s from %s after its terminator" % (kind, info["src"]))
                return
            st = self._new(key, d, info, False)
            if kind == "voice":
                r.warn(NAME, d, "no_header", "voice from %s without a header" % info["src"])
        if kind == "terminator":
            self.streams.terminate(key, d)
            return
        fich = info["fich"]
        if st.modulo != fich["ft"] + 1:
            if st.ext is not None:
                r.error(NAME, d, "ft_changed", "frame total FT changed to %d inside the stream" % fich["ft"])
            st.modulo = fich["ft"] + 1
        if fich["fn"] > fich["ft"]:
            r.error(NAME, d, "bad_fn", "FN %d over FT %d" % (fich["fn"], fich["ft"]))
            return
        seq_finding(r, NAME, d, st, st.add(d, fich["fn"]), "fn")
        voice_frames(st, info["frames"])
        dmr_form(st, info["frames"])

    def _new(self, key, d, info, header):
        st = Stream(NAME, key[0], "stream from %s" % info["src"], 7, Y.PACKET_GAP, d, 0 if header else None)
        self.streams.live[key] = st
        return st

    def _answer(self, d, kind, ok, what):
        for index, (req, want) in enumerate(self.pending):
            if want == kind and (req.src, req.sport) == (d.dst, d.dport):
                del self.pending[index]
                if not ok:
                    self.report.error(NAME, d, "bad_ack", "%s is not %r" % (what, Y.ACK))
                return
        self.report.warn(NAME, d, "unsolicited", "%s with no pending request" % what)

    def _expire(self, now):
        if now is None:
            return
        while self.pending and now - self.pending[0][0].ts > REPLY_TIMEOUT:
            req, kind = self.pending.pop(0)
            self.report.error(NAME, req, "no_reply", "%s not answered within %.1f s" % (kind, REPLY_TIMEOUT))

    def _ended(self, st):
        if st.end != "terminated":
            sev = ERROR if st.end == "timeout" else WARN
            self.report.add(NAME, st.flow, st.last_frame, sev, "no_terminator",
                            "%s ended by %s without a terminator (%d voice packets)" % (st.name, st.end, st.frames))
        finish_stream(self.report, st)

    def finish(self, end_ts):
        self._expire(end_ts)
        self.streams.finish()
