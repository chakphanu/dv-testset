# SPDX-License-Identifier: MIT
"""P25 network records to/from a P25Reflector (UDP 41000): poll/echo
pairing, unlink, and calls of LDU1 0x62..0x6A / LDU2 0x6B..0x73 records
(one per 20 ms, sizes from dvtest.proto.p25) closed by a 17 B terminator."""
from ..proto import p25 as P
from .common import ERROR, REPLY_TIMEOUT, WARN, Live, Stream, client_of, finish_stream, flow_of, seq_finding

NAME = "p25"
SUPERFRAME = 2 * P.FRAMES_PER_LDU


class Checker:
    def __init__(self, report):
        self.report = report
        self.calls = Live(self._ended)
        self.polls = []           # [(d, payload)] polls waiting for the echo
        self.unlinked = {}        # client (ip, port) -> frame of its unlink

    def claims(self, d):
        return P.PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self.calls.expire(d.ts)
        self._expire(d.ts)
        to_reflector = d.dport == P.PORT
        client = (d.src, d.sport) if to_reflector else (d.dst, d.dport)
        poll = P.parse_poll(p)
        if poll is not None:
            kind = poll[0] if to_reflector else poll[0] + "_echo"
            r.count(NAME, d, kind)
            if kind == "poll":
                self.polls.append((d, p))
                self.unlinked.pop(client, None)
            elif kind == "unlink":
                self.unlinked[client] = d.frame
                for key in [k for k in self.calls.live if client_of(k, P.PORT) == client]:
                    self.calls.end(key, "unlink")
            elif kind == "poll_echo":
                for index, (req, data) in enumerate(self.polls):
                    if data == p and (req.src, req.sport) == (d.dst, d.dport):
                        del self.polls[index]
                        break
                else:
                    r.error(NAME, d, "unsolicited_echo", "poll echo with no pending poll")
            else:
                r.error(NAME, d, "unlink_from_reflector", "unlink sent by the reflector")
            return
        rec = P.parse(p)
        if rec is None:
            r.count(NAME, d, "unparseable")
            shape = P.layout(p[0]) if p else None
            if shape is not None or (p and p[0] == P.TERMINATOR):
                want = shape[0] if shape else P.TERMINATOR_SIZE
                r.error(NAME, d, "bad_length", "record 0x%02x of %d B, want %d" % (p[0], len(p), want))
            else:
                r.error(NAME, d, "unparseable", "%d B datagram is no poll/unlink/record" % len(p))
            return
        r.count(NAME, d, "terminator" if rec["terminator"] else "ldu%d" % rec["ldu"])
        if client in self.unlinked:
            r.error(NAME, d, "after_unlink", "record after the unlink at frame %d" % self.unlinked[client])
            return
        key = flow_of(d)
        st = self.calls.live.get(key)
        if rec["terminator"]:
            if st is None:
                r.warn(NAME, d, "stray_terminator", "terminator without a call"
                       " (upstream: xlxd relays an empty stream, e.g. a DMR join, as a lone terminator)")
            else:
                if (st.ext + 1) % P.FRAMES_PER_LDU:
                    r.warn(NAME, d, "terminator_in_ldu", "terminator after record 0x%02x, inside an LDU"
                           " (upstream: xlxd ends a call without LDU padding)" % P.VOICE_TYPES[st.ext % SUPERFRAME])
                self.calls.terminate(key, d)
            return
        index = P.VOICE_TYPES.index(rec["type"])
        if st is None:
            st = Stream(NAME, key, "call from frame %d" % d.frame, SUPERFRAME, 0.020, d)
            st.fields = {}
            self.calls.live[key] = st
            if index != 0:
                r.warn(NAME, d, "late_entry", "call opens with record 0x%02x, not 0x62" % rec["type"])
        seq_finding(r, NAME, d, st, st.add(d, index), "record")
        for field in ("src", "dst", "lcf", "algid"):
            if field in rec:
                if st.fields.setdefault(field, rec[field]) != rec[field]:
                    r.error(NAME, d, "field_changed", "%s changed inside the call (%s -> %s)" % (
                        field, st.fields[field], rec[field]))
                    st.fields[field] = rec[field]

    def _expire(self, now):
        while self.polls and now is not None and now - self.polls[0][0].ts > REPLY_TIMEOUT:
            req, _data = self.polls.pop(0)
            self.report.error(NAME, req, "no_echo", "poll not echoed within %.1f s" % REPLY_TIMEOUT)

    def _ended(self, st):
        if st.end != "terminated":
            sev = ERROR if st.end in ("timeout", "unlink") else WARN
            self.report.add(NAME, st.flow, st.last_frame, sev, "no_terminator",
                            "%s ended by %s without a terminator (%d records)" % (st.name, st.end, st.frames))
        finish_stream(self.report, st)

    def finish(self, end_ts):
        self._expire(end_ts)
        self.calls.finish()

