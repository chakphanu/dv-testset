# SPDX-License-Identifier: MIT
"""DPlus (REF, UDP 20001): connect/disconnect, login and reply,
keepalive, and DSVT streams wrapped in a 2-byte length/flag prefix
(header 58 B, voice 29 B, last 32 B) checked with the DExtra stream rules
of dvtest.check.dextra. xlxd repeats the header every 21 frames."""
from ..proto import dplus as P
from . import dextra

NAME = "dplus"
CONTROL = {P.CONNECT: "link", P.DISCONNECT: "unlink", P.KEEPALIVE: "keepalive",
           P.LOGIN_OK: "login_ok", P.LOGIN_BUSY: "login_busy"}


def kind_of(p):
    """(kind, DSVT bytes) as dvtest.check.dextra.kind_of, for DPlus packets."""
    if p in CONTROL:
        return CONTROL[p], None
    if len(p) == 28 and p[:4] == b"\x1c\xc0\x04\x00":
        return "login", None
    if len(p) == P.HEADER_SIZE and p[:2] == b"\x3a\x80" and p[2:6] == b"DSVT" and p[6] == 0x10:
        return "header", p[2:]
    if len(p) == P.VOICE_SIZE and p[:2] == b"\x1d\x80" and p[2:6] == b"DSVT" and p[6] == 0x20:
        return "voice", p[2:]
    if len(p) == P.LAST_SIZE and p[:2] == b"\x20\x80" and p[2:6] == b"DSVT" and p[6] == 0x20 and p[16] & 0x40:
        return "voice", p[2:]
    return None, None


def Checker(report, m17=None):
    return dextra.Checker(report, m17, name=NAME, port=P.PORT, kinds=kind_of)
