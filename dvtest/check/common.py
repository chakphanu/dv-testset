# SPDX-License-Identifier: MIT
"""Finding model, per-protocol counters and stream statistics shared by the
protocol checkers."""
from collections import Counter, namedtuple

from ..proto.nxdn import from_dmr

Finding = namedtuple("Finding", "protocol flow frame severity code message")
ERROR, WARN = "error", "warn"
# a stream with no packet for this long has ended (terminator or not)
STREAM_TIMEOUT = 5.0
# a request with no reply for this long is unanswered
REPLY_TIMEOUT = 1.0
# sequence indices remembered for dup/reorder detection; longer gaps are
# reported at once
WINDOW = 256


def flow_of(d):
    return "%s:%d>%s:%d" % (d.src, d.sport, d.dst, d.dport)


def touches(flow, endpoint):
    """flow (flow_of) runs to or from endpoint (ip, port)."""
    end = "%s:%d" % endpoint
    return flow.startswith(end + ">") or flow.endswith(">" + end)


class Departures:
    """Clients that left (unlink, DISC): traffic sent to them afterwards.
    One stream packet (voice, or the stream's end: DMR terminator, D-STAR
    last frame, M17 EOT) within one frame time after the departure was in
    flight (warning in_flight); anything else is an error `code`, reported
    once per departure."""

    def __init__(self, report, protocol, window, code="after_unlink"):
        self.report, self.protocol, self.window, self.code = report, protocol, window, code
        self.left = {}            # client (ip, port) -> [ts, frame, in flight seen, reported]

    def leave(self, client, d):
        self.left[client] = [d.ts, d.frame, False, False]

    def back(self, client):
        self.left.pop(client, None)

    def sent(self, client, d, what=None):
        """True when d (to client) came after the client left: reported, and
        the caller must not count it in a stream. what names a stream packet
        that may be in flight ("voice", "terminator", ...), None otherwise."""
        e = self.left.get(client)
        if e is None:
            return False
        dt = None if d.ts is None or e[0] is None else d.ts - e[0]
        if what and not e[2] and dt is not None and 0 <= dt <= self.window:
            e[2] = True
            self.report.warn(self.protocol, d, "in_flight", "%s in flight at the unlink of frame %d"
                             " (%.1f ms after it)" % (what, e[1], dt * 1000.0))
        elif not e[3]:
            e[3] = True
            self.report.error(self.protocol, d, self.code, "traffic to the client %s after its unlink at"
                              " frame %d (later packets to it not reported again)" % (
                                  "%.1f ms" % (dt * 1000.0) if dt is not None else "", e[1]))
        self.report.proto(self.protocol)["after_unlink_packets"] = \
            self.report.proto(self.protocol).get("after_unlink_packets", 0) + 1
        return True


def client_of(flow, port):
    """(ip, port) of the side of a flow_of() name that is not on `port`."""
    a, b = flow.split(">")
    ip, p = a.rsplit(":", 1)
    if int(p) == port:
        ip, p = b.rsplit(":", 1)
    return ip, int(p)


class Report:
    """Findings and counters of one capture file."""

    def __init__(self, jitter_warn_ms=None):
        self.findings = []
        self.protocols = {}
        self.spans = []           # finished streams: protocol, flow, times, codec zero counts
        self.gap_start = {}       # frame where a sequence gap showed -> ts of the packet before it
        self.jitter_warn_ms = jitter_warn_ms

    def proto(self, name):
        if name not in self.protocols:
            self.protocols[name] = {"datagrams": 0, "flows": Counter(), "kinds": Counter(),
                                    "streams": []}
        return self.protocols[name]

    def count(self, name, d, kind):
        p = self.proto(name)
        p["datagrams"] += 1
        p["flows"][flow_of(d)] += 1
        p["kinds"][kind] += 1

    def add(self, protocol, flow, frame, severity, code, message):
        self.findings.append(Finding(protocol, flow, frame, severity, protocol + "." + code, message))

    def error(self, protocol, d, code, message):
        self.add(protocol, flow_of(d), d.frame, ERROR, code, message)

    def warn(self, protocol, d, code, message):
        self.add(protocol, flow_of(d), d.frame, WARN, code, message)

    def stream(self, protocol, stream):
        self.proto(protocol)["streams"].append(stream)


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


class Stream:
    """Sequence numbers modulo `modulo` of one stream: loss, dup and
    reorder by unwrapping to an absolute index; inter-arrival jitter is
    |arrival delta - index delta * nominal| per consecutive pair.

    add() returns None for an in-order packet, else (kind, detail) with
    kind "loss" (detail = frames missing), "dup" or "reorder"."""

    def __init__(self, protocol, flow, name, modulo, nominal, first, expect=None):
        self.protocol, self.flow, self.name = protocol, flow, name
        self.modulo, self.nominal = modulo, nominal
        self.first_frame, self.last_frame = first.frame, first.frame
        self.t0 = self.t1 = first.ts
        self.frames = self.loss = self.dup = self.reorder = 0
        self.ext = None          # absolute index of the newest packet
        self.seen = set()
        self.jitter = []
        self.prev = None         # (ts, ext) of the previous packet
        self.end = None          # "terminated", "timeout", "capture end", ...
        self.notes = Counter()
        self.codecs = {}         # codec family -> [voice packets, packets with non-zero data]
        self.expect = expect     # sequence number the first packet must carry
        self.missing = {}        # absolute index -> frame where its gap showed
        self.gaps = {}           # frame where a gap showed -> ts of the packet before the gap
        self.prefix = "seq"      # finding code prefix (seq_loss, pid_loss, ...)
        self.warn_ms = None      # jitter warning threshold, default one period

    def reset(self):
        """Forget the sequence history (the sender restarted)."""
        self.ext = None
        self.expect = None
        self.seen = set()
        self.prev = None
        self.missing = {}

    def gap(self):
        """No jitter sample between the last packet and the next one."""
        self.prev = None

    def add(self, d, seq):
        self.frames += 1
        self.last_frame = d.frame
        before = self.t1
        if d.ts is not None:
            self.t1 = d.ts
        if self.ext is None:
            if self.expect is None or self.expect == seq:
                self.ext = seq
                self.seen.add(seq)
                self.prev = (d.ts, seq)
                return None
            self.ext = self.expect - 1
        half = self.modulo // 2
        delta = (seq - self.ext) % self.modulo
        if delta > half:
            delta -= self.modulo
        ext = self.ext + delta
        result = None
        if ext in self.seen:
            self.dup += 1
            return ("dup", 0)
        self.seen.add(ext)
        if len(self.seen) > 2 * WINDOW:
            self.seen = {e for e in self.seen if e > ext - WINDOW}
        if ext > self.ext:
            if ext > self.ext + 1:
                self.loss += ext - self.ext - 1
                result = ("loss", ext - self.ext - 1)
                self.gaps[d.frame] = before
                if ext - self.ext - 1 <= WINDOW:
                    for gap in range(self.ext + 1, ext):
                        self.missing[gap] = d.frame
            self.ext = ext
        else:
            self.reorder += 1
            self.loss = max(0, self.loss - 1)
            self.missing.pop(ext, None)
            result = ("reorder", 0)
        ts0, e0 = self.prev or (None, None)
        if d.ts is not None and ts0 is not None and self.nominal and result is None:
            self.jitter.append(abs((d.ts - ts0) - (ext - e0) * self.nominal) * 1000.0)
        self.prev = (d.ts, ext)
        return result

    def summary(self):
        out = {"name": self.name, "flow": self.flow, "frames": self.frames,
               "first_frame": self.first_frame, "last_frame": self.last_frame,
               "loss": self.loss, "dup": self.dup, "reorder": self.reorder,
               "duration_s": None if self.t0 is None or self.t1 is None else round(self.t1 - self.t0, 3),
               "end": self.end}
        if self.jitter:
            out["jitter_ms"] = {"p50": round(percentile(self.jitter, 0.5), 2),
                                "p95": round(percentile(self.jitter, 0.95), 2),
                                "max": round(max(self.jitter), 2)}
        if self.notes:
            out["notes"] = dict(self.notes)
        return out


# a gap is a loss once the stream ran this many indices past it (a late
# packet inside the horizon is a reorder, not a loss)
LOSS_HORIZON = 8


def seq_finding(report, protocol, d, stream, result, code_prefix="seq"):
    """Report a Stream.add() result: dup and reorder at once, losses when
    they are older than LOSS_HORIZON."""
    stream.prefix = code_prefix
    if result is not None and result[0] == "loss" and result[1] > WINDOW:
        report.gap_start[d.frame] = stream.gaps.get(d.frame)
        report.error(protocol, d, code_prefix + "_loss", "sequence jump, %d frame(s) missing in %s" % (
            result[1], stream.name))
    elif result is not None and result[0] != "loss":
        text = "duplicate frame" if result[0] == "dup" else "frame out of order"
        report.error(protocol, d, "%s_%s" % (code_prefix, result[0]), "%s in %s" % (text, stream.name))
    flush_loss(report, stream, stream.ext - LOSS_HORIZON if stream.ext is not None else None)


def flush_loss(report, stream, below=None):
    """One seq_loss finding per gap (frame where it showed) for missing
    indices under `below` (all when None)."""
    gaps = {}
    for index, frame in stream.missing.items():
        gaps.setdefault(frame, []).append(index)
    for frame, indices in sorted(gaps.items()):
        if below is None or max(indices) < below:
            for index in indices:
                del stream.missing[index]
            report.gap_start[frame] = stream.gaps.get(frame)
            report.add(stream.protocol, stream.flow, frame, ERROR, stream.prefix + "_loss",
                       "sequence jump, %d frame(s) missing in %s" % (len(indices), stream.name))


# codec family each protocol's voice frames carry (an XLX frame carries
# several: D-STAR AMBE, DMR AMBE+2 and, from revision 3, Codec2). "ambe+2"
# is the 72-bit DMR form with FEC; "ambe+2 bits" the 49 data bits NXDN
# carries, which streams with the DMR form also record (dvtest.proto.nxdn
# from_dmr) so a zero NXDN frame can be traced to its DMR source.
FAMILY = {"dextra": "ambe", "dplus": "ambe", "dcs": "ambe", "xlx": "ambe", "dmr": "ambe+2",
          "ysf": "ambe+2", "nxdn": "ambe+2 bits", "m17": "codec2", "p25": "imbe"}


def dmr_form(stream, frames):
    """Count 72-bit DMR-form AMBE+2 frames as their 49 data bits too."""
    voice_frames(stream, [from_dmr(f) for f in frames], "ambe+2 bits")


def voice_frames(stream, frames, family=None):
    """Count one voice packet of codec frames of a codec family (default:
    the protocol's own, which also drives zero_fill)."""
    nonzero = any(any(f) for f in frames)
    counts = stream.codecs.setdefault(family or FAMILY.get(stream.protocol), [0, 0])
    counts[0] += 1
    counts[1] += nonzero
    if family is None:
        stream.notes["voice_packets"] += 1
        stream.notes["voice_nonzero"] += nonzero


def finish_stream(report, stream):
    """Record a stream summary, warn on jitter over the threshold and on a
    stream whose voice packets all carry all-zero codec frames."""
    flush_loss(report, stream)
    report.spans.append({"protocol": stream.protocol, "flow": stream.flow, "first_frame": stream.first_frame,
                         "t0": stream.t0, "t1": stream.t1, "codecs": dict(stream.codecs)})
    if stream.notes["voice_packets"] and not stream.notes["voice_nonzero"]:
        report.add(stream.protocol, stream.flow, stream.first_frame, WARN, "zero_fill",
                   "%s: all %d voice packets carry all-zero codec frames (upstream: zero fill"
                   " instead of transcoding)" % (stream.name, stream.notes["voice_packets"]))
    report.stream(stream.protocol, stream.summary())
    limit = report.jitter_warn_ms or stream.warn_ms
    if limit is None:
        limit = stream.nominal * 1000.0 if stream.nominal else None
    if limit and len(stream.jitter) >= 5:
        p95 = percentile(stream.jitter, 0.95)
        if p95 > limit:
            report.add(stream.protocol, stream.flow, stream.first_frame, WARN, "jitter",
                       "%s: jitter p95 %.1f ms over %.1f ms (max %.1f ms, %d frames)" % (
                           stream.name, p95, limit, max(stream.jitter), stream.frames))


class Live:
    """Streams in progress by key, plus recently terminated ones so late
    packets after a terminator can be told from a new stream."""

    def __init__(self, on_end):
        self.live = {}
        self.ended = {}      # key -> (stream, ts of the terminator)
        self.on_end = on_end

    def expire(self, now):
        if now is None:
            return
        for key, st in list(self.live.items()):
            if st.t1 is not None and now - st.t1 > STREAM_TIMEOUT:
                del self.live[key]
                st.end = "timeout"
                self.on_end(st)
        for key, (_st, ts) in list(self.ended.items()):
            if ts is not None and now - ts > STREAM_TIMEOUT:
                del self.ended[key]

    def terminate(self, key, d):
        st = self.live.pop(key)
        st.end = "terminated"
        self.ended[key] = (st, d.ts)
        self.on_end(st)

    def end(self, key, why):
        st = self.live.pop(key)
        st.end = why
        self.on_end(st)

    def after_end(self, key):
        """The stream this key ended within STREAM_TIMEOUT, else None."""
        got = self.ended.get(key)
        return got[0] if got else None

    def finish(self):
        for key, st in list(self.live.items()):
            del self.live[key]
            st.end = "capture end"
            self.on_end(st)
