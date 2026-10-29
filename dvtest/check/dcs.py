# SPDX-License-Identifier: MIT
"""DCS (UDP 30051): link 519 B, ACK/NAK 14 B, unlink 11 B, keepalives
(client 9/15/17/19/22 B, reflector 8/22 B) and 100 B voice packets that
each carry the D-STAR header, checked with the DExtra stream rules of
dvtest.check.dextra (seq mod 21 at [45], 0x40 on the last packet)."""
from ..proto import dcs as C
from . import dextra

NAME = "dcs"
KEEPALIVE_SIZES = (8, 9, 15, 17, 19, 22)
DSVT_VOICE_TAG = b"DSVT\x20\x00\x00\x00\x20\x00\x01\x02"


def kind_of(p):
    """(kind, DSVT bytes) as dvtest.check.dextra.kind_of; a DCS voice
    packet maps onto a 27 B DExtra voice packet."""
    if len(p) == C.PACKET_SIZE and p.startswith(b"0001"):
        return "voice", DSVT_VOICE_TAG + p[43:58]
    if len(p) == C.LINK_SIZE:
        return "link", None
    if C.parse_ack(p) is not None:
        return C.parse_ack(p).lower(), None
    if len(p) == 11 and p[9:11] == b" \x00":
        return "unlink", None
    if len(p) in KEEPALIVE_SIZES:
        return "keepalive", None
    return None, None


def Checker(report, m17=None):
    return dextra.Checker(report, m17, name=NAME, port=C.PORT, kinds=kind_of, implicit_header=True)
