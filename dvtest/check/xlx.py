# SPDX-License-Identifier: MIT
"""XLX interlink between reflectors (UDP 10002, xlxd cxlxprotocol.cpp):
keepalive 9 B, connect 'L' / ack 'A' 39 B, disconnect 'U' / nack 'N'
10 B, and DSVT streams (56 B header, voice 27 / 45 / 53 B for protocol
revision 1 / 2 / 3) with the DExtra stream rules of dvtest.check.dextra."""
from ..proto import dextra as D
from . import dextra

NAME = "xlx"
PORT = 10002
VOICE_SIZES = (D.VOICE_SIZE, 45, 53)
LINK = {b"L": "connect", b"A": "ack"}
UNLINK = {b"U": "disconnect", b"N": "nack"}


def kind_of(p):
    """(kind, DSVT bytes) as dvtest.check.dextra.kind_of, for XLX packets."""
    if len(p) == D.HEADER_SIZE and p.startswith(b"DSVT") and p[4] == 0x10:
        return "header", p
    if len(p) in VOICE_SIZES and p.startswith(b"DSVT") and p[4] == 0x20 and p[8] == 0x20:
        return "voice", p
    if len(p) == 9:
        return "keepalive", None
    if len(p) == 39 and p[:1] in LINK and p[38] == 0:
        return ("link" if p[:1] == b"L" else LINK[p[:1]]), None
    if len(p) == 10 and p[:1] in UNLINK and p[9] == 0:
        return UNLINK[p[:1]], None
    return None, None


# a stream a reflector received from an XLX peer must not go back out to an
# XLX peer while it is open (no transit); open = a packet within this window
TRANSIT_WINDOW = 2.0


class Checker(dextra.Checker):
    """DSVT stream rules plus the no-transit rule: a DSVT stream id that a
    reflector received on 10002 is not sent by it to an XLX peer within
    TRANSIT_WINDOW of the last received packet (xlx.transit, one finding
    per reflector, stream id and destination)."""

    def __init__(self, report, m17=None):
        super().__init__(report, m17, name=NAME, port=PORT, kinds=kind_of)
        self.received = {}        # (reflector ip, stream id) -> (ts, peer ip)
        self.reported = set()

    def feed(self, d):
        kind, p = kind_of(d.payload)
        if kind in ("header", "voice") and d.sport == PORT and d.dport == PORT and d.ts is not None:
            sid = int.from_bytes(p[12:14], "little")
            got = self.received.get((d.src, sid))
            if got is not None and d.ts - got[0] <= TRANSIT_WINDOW and (d.src, sid, d.dst) not in self.reported:
                self.reported.add((d.src, sid, d.dst))
                self.report.error(NAME, d, "transit", "stream %04x received from XLX peer %s is sent on to XLX peer"
                                  " %s (%.2f s after its last received packet)" % (sid, got[1], d.dst, d.ts - got[0]))
            self.received[(d.dst, sid)] = (d.ts, d.src)
        super().feed(d)
