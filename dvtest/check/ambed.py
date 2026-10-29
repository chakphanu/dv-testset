# SPDX-License-Identifier: MIT
"""AMBEd transcoder protocol: control pairing on port 10100 and data
streams on the ports announced by STD."""
from ..proto import ambed as A
from .common import ERROR, REPLY_TIMEOUT, WARN, Stream, finish_stream, seq_finding

NAME = "ambed"
# OPEN callsign of a test stream that breaks the protocol on purpose (data
# after CS, idle until the transcoder times it out, data from another
# port): findings on its frames are warnings tagged as intentional
FAULT_CALLSIGN = b"N0FAULT "


def _open_problem(op):
    """Why an OPEN can only be answered BUSY, or None when it is valid."""
    outs = op.outs
    if op.codec_in not in A.CODECS:
        return "input codec %d unknown" % op.codec_in
    if not outs:
        return "no output codec"
    if A.NONE in outs:
        return "output list has a gap"
    if len(set(outs)) != len(outs):
        return "output codec repeated"
    if any(c not in A.CODECS for c in outs):
        return "output codec unknown"
    if op.codec_in in outs:
        return "output equals input"
    return None


def _caps_problem(op, caps):
    """Why the server's own CAPS rule this OPEN out, or None."""
    if caps is None:
        return None
    if len(op.outs) > caps.max_outputs:
        return "%d outputs over the advertised %d" % (len(op.outs), caps.max_outputs)
    missing = [c for c in (op.codec_in,) + op.outs if c in A.CODECS and not caps.mask & (1 << c)]
    if missing:
        return "codec %s not in the CAPS mask %08x" % (missing, caps.mask)
    return None


class Checker:
    def __init__(self, report):
        self.report = report
        self.pending = {}     # (client, server) -> [(d, kind, (Open, problem, caps) | None)]
        self.caps = {}        # server ip -> Caps
        self.answers = {}     # (client, server, tx, in, outs) -> Std
        self.streams = {}     # (server ip, port) -> dict
        self.by_id = {}       # (client, server, stream id) -> (server ip, port)
        self.fault_frames = set()     # frames of FAULT_CALLSIGN streams

    def claims(self, d):
        return A.PORT in (d.sport, d.dport) or (d.src, d.sport) in self.streams \
            or (d.dst, d.dport) in self.streams

    def feed(self, d):
        self._expire(d.ts)
        if (d.src, d.sport) in self.streams:
            self._reply(d, self.streams[(d.src, d.sport)])
        elif (d.dst, d.dport) in self.streams:
            self._request(d, self.streams[(d.dst, d.dport)])
        else:
            self._control(d)

    # control port

    def _control(self, d):
        r, p = self.report, d.payload
        to_server = d.dport == A.PORT and (d.sport != A.PORT or not _is_reply(p))
        if to_server:
            key = ((d.src, d.sport), (d.dst, d.dport))
            if A.parse_ping(p) is not None:
                r.count(NAME, d, "ping")
                self.pending.setdefault(key, []).append((d, "ping", None))
            elif p == A.CAPS_REQUEST:
                r.count(NAME, d, "caps")
                self.pending.setdefault(key, []).append((d, "caps", None))
            elif A.parse_open(p) is not None:
                op = A.parse_open(p)
                r.count(NAME, d, "open_ext" if op.size == 24 else "open_legacy")
                problem = _open_problem(op)
                caps = None if problem else _caps_problem(op, self.caps.get(d.dst))
                if problem:
                    r.warn(NAME, d, "open_invalid", "OPEN %s: %s" % (_ops(op), problem))
                if caps:
                    r.warn(NAME, d, "open_outside_caps", "OPEN %s: %s" % (_ops(op), caps))
                if op.size == 24 and d.sport == A.PORT and A.PCM not in (op.codec_in,) + op.outs:
                    # a reflector (source port 10100): upstream xlxd opens
                    # non-PCM streams with the legacy form
                    r.warn(NAME, d, "ext_open_non_pcm", "extended OPEN %s without PCM from a reflector"
                           " (upstream xlxd uses the legacy OPEN)" % _ops(op))
                self.pending.setdefault(key, []).append((d, "open", (op, problem, caps)))
            elif len(p) == 9 and p.startswith(A.CLOSE_TAG):
                r.count(NAME, d, "close")
                self._close(d, A.parse_close(p))
            else:
                r.count(NAME, d, "unparseable")
                r.error(NAME, d, "unparseable", "%d B control request not PING/CAPS/OPEN/CS" % len(p))
            return
        key = ((d.dst, d.dport), (d.src, d.sport))
        queue = self.pending.get(key, [])
        if p == A.PONG:
            r.count(NAME, d, "pong")
            self._match(d, queue, lambda k, _x: k == "ping", "PONG")
        elif A.parse_caps(p) is not None:
            r.count(NAME, d, "caps_reply")
            self.caps[d.src] = A.parse_caps(p)
            self._match(d, queue, lambda k, _x: k == "caps", "CAPS reply")
        elif A.parse_std(p) is not None or A.is_busy(p):
            std = A.parse_std(p)
            r.count(NAME, d, ("std%d" if std else "busy%d") % len(p))
            tx = std.transaction if std else A.busy_transaction(p)
            ext = len(p) in (13, 18)
            hit = self._match(d, queue, lambda k, x: k == "open" and (
                not ext or (x[0].size == 24 and x[0].transaction == tx)), "STD" if std else "BUSY")
            if hit is not None:
                self._answer(d, hit, std, key)
        else:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B control reply not PONG/CAPS/STD/BUSY" % len(p))

    def _match(self, d, queue, want, what):
        for index, (req, kind, extra) in enumerate(queue):
            if want(kind, extra):
                del queue[index]
                return req, extra
        if what in ("STD", "BUSY") and len(d.payload) in (13, 18):
            self.report.error(NAME, d, "tx_mismatch", "%s %d B matches no pending extended OPEN"
                              " transaction" % (what, len(d.payload)))
        else:
            self.report.error(NAME, d, "unsolicited", "%s %d B with no pending request" % (what, len(d.payload)))
        return None

    def _answer(self, d, hit, std, key):
        r = self.report
        req, (op, problem, caps) = hit
        if op.size == 24 and len(d.payload) not in (13, 18):
            r.error(NAME, d, "reply_form", "extended OPEN (frame %d) answered with the %d B legacy form" % (
                req.frame, len(d.payload)))
        if std is None:
            return
        if problem:
            r.error(NAME, d, "open_invalid_accepted", "STD for OPEN frame %d (%s)" % (req.frame, problem))
        if caps:
            (r.error if op.size == 24 else r.warn)(NAME, d, "open_outside_caps_accepted",
                                                   "STD for OPEN frame %d (%s)" % (req.frame, caps))
        if std.codec_in != op.codec_in or (op.outs and std.codec_out != op.outs[0]):
            r.error(NAME, d, "std_codecs", "STD codecs %d->%d, OPEN frame %d asked %s" % (
                std.codec_in, std.codec_out, req.frame, _ops(op)))
        if op.size == 24:
            same = (key[0], key[1], op.transaction, op.codec_in, op.outs)
            old = self.answers.get(same)
            if old is not None and (old.stream_id, old.port) != (std.stream_id, std.port):
                r.error(NAME, d, "retransmit_new_stream", "retransmitted OPEN tx %08x got stream %d"
                        " port %d, first answer was stream %d port %d" % (
                            op.transaction, std.stream_id, std.port, old.stream_id, old.port))
            self.answers.setdefault(same, std)
        server = (d.src, std.port)
        old = self.streams.get(server)
        if old is not None and old["open"] and old["id"] == std.stream_id:
            return        # answer to a retransmit: same stream
        if old is not None:
            self._end_stream(old, "port reused" if old["open"] else "closed")
        name = "stream %d port %d %d->%s" % (std.stream_id, std.port, op.codec_in, list(op.outs))
        self.streams[server] = {
            "id": std.stream_id, "client": d.dst, "codec_in": op.codec_in, "outs": op.outs,
            "open": True, "closed_at": None, "pending": [], "stream": None, "name": name,
            "flow": "%s<>%s:%d" % (d.dst, d.src, std.port), "replies": 0,
            "fault": op.callsign == FAULT_CALLSIGN}
        if op.callsign == FAULT_CALLSIGN:
            self.fault_frames.update((req.frame, d.frame))
        self.by_id[(key[0][0], d.src, std.stream_id)] = server

    def _close(self, d, stream_id):
        server = self.by_id.get((d.src, d.dst, stream_id))
        st = self.streams.get(server) if server else None
        if st is None or st["id"] != stream_id:
            self.report.warn(NAME, d, "close_unknown", "CS for stream %d that this capture never saw opened" % stream_id)
            return
        if not st["open"]:
            self.report.warn(NAME, d, "close_twice", "CS for stream %d already closed" % stream_id)
            return
        st["open"] = False
        st["closed_at"] = d.ts
        if st["fault"]:
            self.fault_frames.add(d.frame)

    # data ports

    def _request(self, d, st):
        r = self.report
        req = A.parse_data_request(d.payload)
        r.count(NAME, d, "data_request")
        if st["fault"]:
            self.fault_frames.add(d.frame)
        if d.src != st["client"]:
            r.error(NAME, d, "unknown_peer", "data request on %s from %s, the stream was opened by %s" % (
                st["name"], d.src, st["client"]))
        if not st["open"] and st["closed_at"] is not None:
            r.error(NAME, d, "data_after_close", "data request on %s after its CS" % st["name"])
        if req is None or req[0] != st["codec_in"]:
            r.error(NAME, d, "request_bad", "%d B data request on %s (want codec %d, %d B)" % (
                len(d.payload), st["name"], st["codec_in"], 2 + A.WIDTH[st["codec_in"]]))
            return
        if st["stream"] is None:
            st["stream"] = Stream(NAME, st["flow"], st["name"], 256, 0.020, d)
            # xlxd feeds bursts: DMR 3 frames per 60 ms, NXDN 4 frames per 80 ms
            st["stream"].warn_ms = 80.0
        seq_finding(r, NAME, d, st["stream"], st["stream"].add(d, req[1]), "pid")
        st["pending"].append((d, req[1]))

    def _reply(self, d, st):
        r = self.report
        r.count(NAME, d, "data_reply")
        st["replies"] += 1
        if st["fault"]:
            self.fault_frames.add(d.frame)
        try:
            pid, _entries = A.parse_data_reply(d.payload, st["outs"])
        except ValueError as exc:
            r.error(NAME, d, "reply_bad", "%s: %s" % (st["name"], exc))
            return
        for index, (req, want) in enumerate(st["pending"]):
            if want == pid:
                for lost, lost_pid in st["pending"][:index]:
                    r.error(NAME, lost, "reply_missing", "%s: no reply to pid %d (a later pid %d was"
                            " answered at frame %d)" % (st["name"], lost_pid, pid, d.frame))
                del st["pending"][:index + 1]
                return
        r.error(NAME, d, "reply_unsolicited", "%s: reply pid %d matches no pending request" % (st["name"], pid))

    # timeouts and end

    def _expire(self, now):
        if now is None:
            return
        for key, queue in self.pending.items():
            while queue and now - queue[0][0].ts > REPLY_TIMEOUT:
                req, kind, extra = queue.pop(0)
                if kind == "caps":
                    self.report.warn(NAME, req, "caps_no_reply", "CAPS not answered (legacy transcoder?)")
                else:
                    self.report.error(NAME, req, "no_reply", "%s not answered within %.1f s" % (
                        kind.upper() if kind != "open" else "OPEN " + _ops(extra[0]), REPLY_TIMEOUT))
        for st in self.streams.values():
            while st["pending"] and now - st["pending"][0][0].ts > REPLY_TIMEOUT:
                req, pid = st["pending"].pop(0)
                self.report.error(NAME, req, "reply_missing", "%s: pid %d not answered within %.1f s" % (
                    st["name"], pid, REPLY_TIMEOUT))

    def _end_stream(self, st, why):
        if st["stream"] is not None and st["stream"].end is None:
            st["stream"].end = why
            st["stream"].notes["replies"] = st["replies"]
            finish_stream(self.report, st["stream"])

    def finish(self, end_ts):
        self._expire(end_ts)
        for st in self.streams.values():
            self._end_stream(st, "capture end (open)" if st["open"] else "closed")
        for index, f in enumerate(self.report.findings):
            if f.protocol == NAME and f.frame in self.fault_frames and f.severity == ERROR:
                self.report.findings[index] = f._replace(severity=WARN, message=f.message + (
                    " (intentional: fault-injection stream, OPEN callsign %s)" % FAULT_CALLSIGN.decode().strip()))


def _is_reply(p):
    return p == A.PONG or A.parse_caps(p) is not None or A.parse_std(p) is not None or A.is_busy(p)


def _ops(op):
    tx = "" if op.transaction is None else " tx %08x" % op.transaction
    return "%d->%s (%d B%s)" % (op.codec_in, list(op.outs), op.size, tx)
