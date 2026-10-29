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


def Checker(report, m17=None):
    return dextra.Checker(report, m17, name=NAME, port=PORT, kinds=kind_of)
