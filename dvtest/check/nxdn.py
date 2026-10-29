# SPDX-License-Identifier: MIT
"""NXDN to/from an NXDNReflector (UDP 41400): NXDNP poll/echo pairing,
NXDNU unlink, and NXDND streams (header, voice every 80 ms, trailer) with
LICH parity, SACCH CRC-6 and structure cycle 3,2,1,0, FACCH1 CRC-12 and
message type, dst == the reflector TG, and no relay back to the sender."""
from ..proto import nxdn as N
from .common import ERROR, REPLY_TIMEOUT, WARN, Live, Stream, client_of, finish_stream, flow_of, seq_finding

NAME = "nxdn"
CONTROL_LICH = (0x81, 0x83)          # FACCH1 in both blocks (header/trailer)


class Checker:
    def __init__(self, report):
        self.report = report
        self.streams = Live(self._ended)
        self.polls = []           # [(d, payload)] waiting for the echo
        self.tg = {}              # reflector ip -> TG learned from an echo
        self.unlinked = {}        # client (ip, port) -> frame of its NXDNU
        self.talking = {}         # (client ip, port) -> src id of its live stream

    def claims(self, d):
        return N.PORT in (d.sport, d.dport)

    def feed(self, d):
        r, p = self.report, d.payload
        self.streams.expire(d.ts)
        self._expire(d.ts)
        to_reflector = d.dport == N.PORT
        reflector = d.dst if to_reflector else d.src
        client = (d.src, d.sport) if to_reflector else (d.dst, d.dport)
        poll = N.parse_poll(p)
        if poll is not None:
            kind = poll["kind"] if to_reflector else poll["kind"] + "_echo"
            r.count(NAME, d, kind)
            if kind == "poll":
                self.polls.append((d, p))
                self.unlinked.pop(client, None)
            elif kind == "unlink":
                self.unlinked[client] = d.frame
            elif kind == "poll_echo":
                self.tg[reflector] = poll["tg"]
                for index, (req, data) in enumerate(self.polls):
                    if data == p and (req.src, req.sport) == client:
                        del self.polls[index]
                        break
                else:
                    r.error(NAME, d, "unsolicited_echo", "NXDNP echo with no pending identical poll")
            else:
                r.error(NAME, d, "unlink_from_reflector", "NXDNU sent by the reflector")
            return
        pkt = N.parse(p)
        if pkt is None:
            r.count(NAME, d, "unparseable")
            r.error(NAME, d, "unparseable", "%d B datagram is no 17 B NXDNP/NXDNU or 43 B NXDND" % len(p))
            return
        kind = pkt["kind"]
        r.count(NAME, d, kind)
        if client in self.unlinked:
            r.error(NAME, d, "after_unlink", "NXDND after the NXDNU at frame %d" % self.unlinked[client])
            return
        tg = self.tg.get(reflector)
        if not pkt["group"] or (tg is not None and pkt["dst"] != tg):
            r.error(NAME, d, "wrong_tg", "NXDND to %s %d, the reflector relays group calls to TG %s" % (
                "group" if pkt["group"] else "unit", pkt["dst"], tg))
        if not to_reflector and self.talking.get(client) == pkt["src"]:
            r.error(NAME, d, "relay_to_sender", "reflector relays src %d back to the client sending it" % pkt["src"])
            return
        if kind == "data":
            return
        self._payload(d, pkt)
        key = (flow_of(d), pkt["src"])
        st = self.streams.live.get(key)
        if kind == "header":
            if st is None:
                st = self._new(key, d, pkt, True)
            else:
                st.notes["header_repeats"] += 1
            return
        if st is None:
            if self.streams.after_end(key) is not None:
                r.error(NAME, d, "after_trailer", "%s for src %d after its trailer" % (kind, pkt["src"]))
                return
            st = self._new(key, d, pkt, False)
            if kind == "voice":
                r.warn(NAME, d, "no_header", "voice for src %d without a header" % pkt["src"])
        if kind == "trailer":
            self.streams.terminate(key, d)
            return
        structure = N.parse_sacch(pkt["payload"][1:5])["structure"]
        seq_finding(r, NAME, d, st, st.add(d, 3 - structure), "voice")

    def _new(self, key, d, pkt, header):
        st = Stream(NAME, key[0], "src %d to %d" % (pkt["src"], pkt["dst"]), 4, N.PACKET_GAP, d,
                    0 if header else None)
        self.streams.live[key] = st
        if d.dport == N.PORT:
            self.talking[(d.src, d.sport)] = pkt["src"]
        return st

    def _payload(self, d, pkt):
        """LICH, SACCH and FACCH1 checks of a header/trailer/voice payload."""
        r, payload, kind = self.report, pkt["payload"], pkt["kind"]
        if not N.parse_lich(payload[0])["parity_ok"]:
            r.error(NAME, d, "lich_parity", "LICH %02x parity bit wrong" % payload[0])
        if not N.parse_sacch(payload[1:5])["crc_ok"]:
            r.error(NAME, d, "sacch_crc", "SACCH CRC-6 wrong (%s)" % kind)
        if kind == "voice":
            if payload[0] in CONTROL_LICH:
                r.error(NAME, d, "flags_lich", "voice flags but LICH %02x says FACCH1" % payload[0])
            return
        if payload[0] not in CONTROL_LICH:
            r.error(NAME, d, "flags_lich", "%s flags but LICH %02x is no FACCH1 frame" % (kind, payload[0]))
            return
        want = N.VCALL if kind == "header" else N.TX_REL
        for block in (payload[5:17], payload[19:31]):
            f = N.parse_facch1(block)
            if not f["crc_ok"]:
                r.error(NAME, d, "facch_crc", "%s FACCH1 CRC-12 wrong" % kind)
            elif f["msg"] != want:
                r.error(NAME, d, "facch_type", "%s carries layer 3 type %02x, want %02x" % (kind, f["msg"], want))
            elif (f["src"], f["dst"]) != (pkt["src"], pkt["dst"]):
                r.error(NAME, d, "facch_ids", "FACCH1 %d->%d, packet %d->%d" % (
                    f["src"], f["dst"], pkt["src"], pkt["dst"]))

    def _expire(self, now):
        if now is None:
            return
        keep = []
        for req, data in self.polls:
            if now - req.ts <= REPLY_TIMEOUT:
                keep.append((req, data))
                continue
            tg, want = self.tg.get(req.dst), N.parse_poll(data)["tg"]
            if tg is None or tg == want:
                self.report.error(NAME, req, "no_echo", "NXDNP for TG %d not echoed within %.1f s" % (
                    want, REPLY_TIMEOUT))
            else:
                self.report.warn(NAME, req, "poll_wrong_tg", "NXDNP for TG %d, the reflector serves TG %d" % (
                    want, tg))
        self.polls = keep

    def _ended(self, st):
        client = client_of(st.flow, N.PORT)
        if self.talking.get(client) is not None and st.flow.endswith(":%d" % N.PORT):
            self.talking.pop(client, None)
        if st.end != "terminated":
            sev = ERROR if st.end == "timeout" else WARN
            self.report.add(NAME, st.flow, st.last_frame, sev, "no_trailer",
                            "%s ended by %s without a trailer (%d voice packets)" % (st.name, st.end, st.frames))
        finish_stream(self.report, st)

    def finish(self, end_ts):
        self._expire(end_ts)
        self.streams.finish()

