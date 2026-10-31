# SPDX-License-Identifier: MIT
"""M17 in xlxd reflector-client mode (UDP 17000): CONN/ACKN/PING/... and
54 B voice packets (frame number, EOT bit, LICH constant per stream), plus
M17P packet-mode datagrams (size, LSF and payload CRC, packet-mode TYPE).

Reflector peers (stage 2): a datagram with a configured or learned reflector
peer (ip, port) at one end is a peer datagram and follows the reflector to
reflector rules: CONN / ACKN 37 B, a stream is the 54 B body plus ONE trailer
byte (upper-case module letter), M17P is the body (38..859 B) plus the
trailer byte. Client datagrams keep the client limits."""
import os

from ..proto import m17 as M
from .common import ERROR, WARN, Live, Stream, finish_stream, flow_of, seq_finding, touches, voice_frames, Departures

NAME = "m17"
# xlxd's late-entrant promotion can replay an ended stream from fn 0 (two
# packets with EOT, then fn 0.. in a ~1 ms burst) about 0.8 s after the EOT.
# Timing dependent, so it shows on ref or sut alone; a warning under the
# xlxd profile, an error under dvrouter (dvtest.check.apply_profile).
REPLAY_WINDOW = 2.0
REPLAY_TAG = "(late-entrant replay: xlxd, timing dependent)"
REPLAY_SEVERITY = "warn"
# M17P datagram: "M17P" + LSD + LSF CRC + protocol + data + CRC (m17.md)
M17P_SIZES = (38, 859)
DISC_ACK_SIZE = 4
# control packets: tag -> allowed sizes (DISC: 10 B request, 4 B acknowledgement)
CONTROL = {b"CONN": (11,), b"LSTN": (11,), b"ACKN": (4,), b"NACK": (4,), b"PING": (10,),
           b"PONG": (10,), b"DISC": (4, 10)}
# between reflectors: CONN / ACKN carry the module list (37 B), no LSTN
PEER_CONTROL = {b"CONN": (37,), b"ACKN": (37,), b"NACK": (4,), b"PING": (10,),
                b"PONG": (10,), b"DISC": (4, 10)}
PEER_PORTS = {"DVT": 17011, "DVU": 17012}
PEER_M17P_SIZES = (M17P_SIZES[0] + 1, M17P_SIZES[1] + 1)
DESIGNATION_LIMIT = 40 ** 9           # 6 B base-40 value of a 9 character designation


def peer_endpoints(hosts=None, env=None):
    """{(ip, port)} of the reflector peers the tester plays, from the same
    settings the rig and the cases use (DVT_M17_PEER_{DVT,DVU}_{PORT,HOST}, see
    dvtest.m17_peer.peer_env / peer_binds): M17-DVT at DVT_M17_PEER_DVT_HOST
    (default the tester address), M17-DVU at DVT_M17_PEER_DVU_HOST, else at
    the tester address or <net>.32 (the rig default under mrefd). hosts:
    rig_config()["hosts"]."""
    env = os.environ if env is None else env
    tester = (hosts or {}).get("tester")
    ports = {k: int(env.get("DVT_M17_PEER_%s_PORT" % k) or v) for k, v in PEER_PORTS.items()}
    dvt = [env.get("DVT_M17_PEER_DVT_HOST") or tester]
    dvu = [env.get("DVT_M17_PEER_DVU_HOST"), tester]
    if tester and tester.count(".") == 3:
        dvu.append(tester.rsplit(".", 1)[0] + ".32")
    return {(ip, ports["DVT"]) for ip in dvt if ip} | {(ip, ports["DVU"]) for ip in dvu if ip}


def peer_layout_problem(p):
    """What is wrong with the designation or module list of a 37 B reflector
    CONN / ACKN: (severity, text), or None."""
    value = int.from_bytes(p[4:10], "big")
    if value == 0 or value >= DESIGNATION_LIMIT:
        return ERROR, "designation %s is not base-40" % p[4:10].hex()
    raw = bytes(p[10:37])
    end = raw.find(b"\0")
    letters = raw if end < 0 else raw[:end]
    if end >= 0 and raw[end:].strip(b"\0"):
        return ERROR, "module list has bytes after its NUL padding"
    if any(not 0x41 <= c <= 0x5A for c in letters):
        return ERROR, "module list %r holds a byte outside A-Z" % bytes(letters)
    if not letters:
        return WARN, "empty module list"
    return None


class Checker:
    def __init__(self, report, peers=()):
        """peers: configured reflector peers, {(ip, port)} (peer_endpoints())."""
        self.report = report
        self.peers = set(peers)
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
        end = (d.src, d.sport) if d.dport == M.PORT else (d.dst, d.dport)
        if len(p) == M.PEER_CONN_SIZE and tag in (b"CONN", b"ACKN"):
            self.peers.add(end)      # a 37 B CONN / ACKN is never a client's: a learned peer
        if end in self.peers:
            self._peer(d, p, end)
            return
        if tag == b"M17P":
            self._packet_mode(d, p)
            return
        if tag == b"M17 ":
            kind = "voice"
            size_ok = len(p) == M.PACKET_SIZE
        elif tag in CONTROL:
            kind = tag.decode().lower()
            size_ok = len(p) in CONTROL[tag]
            if tag == b"DISC" and len(p) == DISC_ACK_SIZE:
                # the 4 B form only acknowledges a DISC (Part II, Disconnecting);
                # a client sending it does not leave (P-M17-17)
                kind = "disc_ack"
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
        self._voice(d, p, peer)

    def _voice(self, d, p, peer):
        """The stream checks on a 54 B body p (a client's packet, or a peer's
        without its trailer byte); peer: the far end's (ip, port)."""
        r = self.report
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
        replay = False
        if st is None:
            old = self.streams.after_end(key)
            if old is not None:
                since = None if d.ts is None or old.t1 is None else d.ts - old.t1
                replay = fn == 0 and since is not None and since <= REPLAY_WINDOW
                if not replay:
                    r.error(NAME, d, "after_eot", "frame %d of stream %04x after its EOT (frame %d)" % (
                        fn, stream_id, old.last_frame))
                    return
                if not old.notes["replay"]:
                    # one finding per replay: the replayed stream is tracked again
                    r.add(NAME, flow_of(d), d.frame, REPLAY_SEVERITY, "after_eot",
                          "stream %04x replayed from fn 0, %.2f s after its EOT (frame %d) %s" % (
                              stream_id, since, old.last_frame, REPLAY_TAG))
            st = Stream(NAME, key[0], "stream %04x" % stream_id, 0x8000, 0.040, d)
            st.lich = p[6:34]
            if replay:
                st.notes["replay"] += 1
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

    def _packet_mode(self, d, p, kind="packet"):
        """M17P (Part II, Packet Mode IP Packet): magic, LSD 28 B, LSF CRC,
        then protocol + user data and the payload CRC, 38..859 B in all.
        Size and the packet-mode TYPE are errors, the two CRCs warnings.
        kind "peer_packet": p is the body of a datagram between reflectors
        (its trailer byte already taken off and checked)."""
        r = self.report
        r.count(NAME, d, kind)
        if not M17P_SIZES[0] <= len(p) <= M17P_SIZES[1]:
            r.error(NAME, d, "m17p_length", "M17P of %d B, want %d..%d" % ((len(p),) + M17P_SIZES))
            return
        lsd, lsf_crc = p[4:32], int.from_bytes(p[32:34], "big")
        if M.crc16(lsd) != lsf_crc:
            # a warning: clients send wrong CRCs as a rule and a reflector
            # forwards M17P unchanged (P-M17-41), so egress carries them too
            r.warn(NAME, d, "m17p_lsf_crc", "M17P LSF CRC %04x, want %04x" % (lsf_crc, M.crc16(lsd)))
        if lsd[13] & 1:
            r.error(NAME, d, "m17p_type", "M17P TYPE %04x has the stream bit set" % int.from_bytes(lsd[12:14], "big"))
        body, crc = p[34:-2], int.from_bytes(p[-2:], "big")
        if M.crc16(body) != crc:
            r.warn(NAME, d, "m17p_crc", "M17P payload CRC %04x, want %04x" % (crc, M.crc16(body)))

    def _peer(self, d, p, peer):
        """A datagram to or from a reflector peer (Part II, reflector to
        reflector): sizes and layout per PEER_CONTROL, 55 B streams (body +
        module letter), M17P body + module letter. The body then gets the
        client checks. Direction matters only for a bare stream."""
        r = self.report
        to_peer = d.dport != M.PORT
        tag = p[:4]
        if tag == b"M17P":
            if not PEER_M17P_SIZES[0] <= len(p) <= PEER_M17P_SIZES[1]:
                r.count(NAME, d, "peer_packet")
                r.error(NAME, d, "m17p_length", "M17P of %d B between reflectors, want %d..%d" % (
                    (len(p),) + PEER_M17P_SIZES))
                return
            self._trailer(d, p[-1], to_peer)
            self._packet_mode(d, p[:-1], "peer_packet")
            return
        if tag == b"M17 ":
            r.count(NAME, d, "peer_voice")
            if len(p) not in (M.PACKET_SIZE, M.STREAM_TRAILER_SIZE):
                r.error(NAME, d, "bad_length", "VOICE of %d B between reflectors, want %d" % (
                    len(p), M.STREAM_TRAILER_SIZE))
                return
            if len(p) == M.PACKET_SIZE:
                # a stream from a one-module peer may omit the trailer (the
                # sut accepts it); a reflector never sends one without
                if to_peer:
                    r.error(NAME, d, "peer_trailer", "stream to a peer without the module trailer (54 B)")
                else:
                    r.warn(NAME, d, "peer_trailer", "bare 54 B stream from a peer (no module trailer)")
            else:
                self._trailer(d, p[-1], to_peer)
            if int.from_bytes(p[52:54], "big") != M.crc16(p[:52]):
                r.warn(NAME, d, "peer_crc", "stream CRC %04x, want %04x" % (
                    int.from_bytes(p[52:54], "big"), M.crc16(p[:52])))
            self._voice(d, p[:M.PACKET_SIZE], peer)
            return
        if tag in CONTROL and tag not in PEER_CONTROL:
            r.count(NAME, d, "peer_" + tag.decode().lower())
            r.error(NAME, d, "bad_length", "%s of %d B between reflectors: a client packet" % (tag.decode(), len(p)))
            return
        if tag not in PEER_CONTROL:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B packet with no known M17 tag" % len(p))
            return
        kind = "peer_" + tag.decode().lower()
        if tag == b"DISC" and len(p) == DISC_ACK_SIZE:
            kind = "peer_disc_ack"
        r.count(NAME, d, kind)
        if len(p) not in PEER_CONTROL[tag]:
            r.error(NAME, d, "bad_length", "%s of %d B between reflectors, want %s" % (
                tag.decode(), len(p), "/".join(str(n) for n in PEER_CONTROL[tag])))
            return
        if len(p) == M.PEER_CONN_SIZE:
            found = peer_layout_problem(p)
            if found:
                r.add(NAME, flow_of(d), d.frame, found[0], "peer_layout", "%s: %s" % (tag.decode(), found[1]))
                if found[0] == ERROR:
                    return
            self.linked.add(peer)
        elif tag == b"DISC" and len(p) != DISC_ACK_SIZE or tag == b"NACK":
            # the link is gone: its live streams end here (no silence timeout)
            self.linked.discard(peer)
            for key in [k for k in self.streams.live if touches(k[0], peer)]:
                self.streams.end(key, "peer disconnected")

    def _trailer(self, d, byte, to_peer):
        """The trailer byte of a datagram between reflectors: the upper-case
        module letter of the originating module."""
        if 0x41 <= byte <= 0x5A:
            return True
        self.report.error(NAME, d, "peer_trailer", "%s trailer byte 0x%02x is not a module letter A-Z" % (
            "sent" if to_peer else "received", byte))
        return False

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
