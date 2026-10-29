# SPDX-License-Identifier: MIT
"""USRP (chan_usrp): seq +1 per frame and direction, overs of 352 B keyed
voice frames each ended by a 32 B unkey."""
from ..proto import usrp as U
from .common import STREAM_TIMEOUT, Live, Stream, finish_stream, flow_of, flush_loss, seq_finding

NAME = "usrp"
TYPES = {U.TYPE_VOICE: "voice", U.TYPE_DTMF: "dtmf", U.TYPE_TEXT: "text", 3: "ping", 4: "tlv",
         5: "voice_adpcm", 6: "voice_ulaw"}


class Checker:
    def __init__(self, report, endpoints=()):
        """endpoints: [((ip, port), (ip, port))] configured USRP links;
        when given, their ports are USRP ports and other peers are flagged."""
        self.report = report
        self.endpoints = {frozenset(e) for e in endpoints}
        self.ports = {port for e in endpoints for _ip, port in e}
        self.seq = {}             # flow -> Stream over every frame
        self.overs = Live(self._ended)

    def claims(self, d):
        return d.payload[:4] == b"USRP" or d.sport in self.ports or d.dport in self.ports

    def feed(self, d):
        r, p = self.report, d.payload
        self.overs.expire(d.ts)
        f = U.parse(p)
        if f is None:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B on a USRP port without a USRP header" % len(p))
            return
        kind = TYPES.get(f.type, "type%d" % f.type)
        if f.type == U.TYPE_VOICE:
            kind = "keyed" if f.keyup else "unkey"
        r.count(NAME, d, kind)
        if self.endpoints and frozenset(((d.src, d.sport), (d.dst, d.dport))) not in self.endpoints:
            r.error(NAME, d, "unknown_peer", "USRP between endpoints that no configured link names")
        flow = flow_of(d)
        old = self.seq.get(flow)
        idle = old is not None and (old.unkeyed or (d.ts is not None and old.t1 is not None
                                                    and d.ts - old.t1 > STREAM_TIMEOUT))
        if f.seq == 0 and idle:
            old.notes["restarts"] += 1      # a new sender session starts at seq 0
            flush_loss(r, old)
            old.reset()
        if flow not in self.seq:
            self.seq[flow] = Stream(NAME, flow, "flow", 1 << 32, 0, d)
            self.seq[flow].unkeyed = False
        seq_finding(r, NAME, d, self.seq[flow], self.seq[flow].add(d, f.seq))
        # after an unkey (end of an over) a sender may restart at seq 0
        self.seq[flow].unkeyed = f.type == U.TYPE_VOICE and not f.keyup
        if f.type not in TYPES:
            r.warn(NAME, d, "unknown_type", "frame type %d" % f.type)
            return
        if f.type != U.TYPE_VOICE:
            return
        if f.keyup > 1:
            r.error(NAME, d, "bad_keyup", "keyup field %d" % f.keyup)
            return
        want = U.VOICE_SIZE if f.keyup else U.HEADER_SIZE
        if len(p) != want:
            r.error(NAME, d, "bad_length", "%s voice frame of %d B, want %d" % (kind, len(p), want))
            return
        over = self.overs.live.get(flow)
        if not f.keyup:
            if over is None:
                self.seq[flow].notes["idle_unkeys"] += 1
            else:
                over.add(d, f.seq)
                self.overs.terminate(flow, d)
            return
        if over is None:
            over = Stream(NAME, flow, "over from frame %d" % d.frame, 1 << 32, 0.020, d)
            self.overs.live[flow] = over
        over.add(d, f.seq)

    def _ended(self, over):
        over.missing.clear()        # losses are reported once, by the flow stream
        if over.end != "terminated":
            sev = "error" if over.end == "timeout" else "warn"
            self.report.add(NAME, over.flow, over.last_frame, sev, "missing_unkey",
                            "%s ended by %s without an unkey (%d frames)" % (over.name, over.end, over.frames))
        finish_stream(self.report, over)

    def finish(self, end_ts):
        self.overs.finish()
        for flow, st in self.seq.items():
            flush_loss(self.report, st)
            if st.notes:
                self.report.proto(NAME).setdefault("flow_notes", {})[flow] = dict(st.notes)


def endpoints_from(path, net):
    """USRP links from a rendered or template xlxd.usrp file (lines:
    module bind_ip bind_port peer_ip peer_port callsign; @NET@ = net)."""
    links = []
    with open(path) as fh:
        for line in fh:
            parts = line.split("#")[0].replace("@NET@", net).split()
            if len(parts) >= 5:
                links.append(((parts[1], int(parts[2])), (parts[3], int(parts[4]))))
    return links

