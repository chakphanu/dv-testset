# SPDX-License-Identifier: MIT
"""Helpers shared by the P25 case files (test_p25.py, test_edge_relink.py).
Moved out of test_p25.py unchanged."""
from dvtest import clients
from dvtest.proto import ambed, p25


def imbe_served(addr, port):
    """(True, how) when the transcoder at addr serves IMBE: CAPS mask bit 4,
    or for a transcoder without CAPS a legacy open 4 -> (1, 2, 3)."""
    client = ambed.AmbedClient(addr, port)
    try:
        caps = ambed.parse_caps(client.rpc(ambed.CAPS_REQUEST) or b"")
        if caps is not None:
            mask = "CAPS mask 0x%02x" % caps.mask
            return bool(caps.mask & (1 << ambed.IMBE)), mask
        reply = client.rpc(ambed.open_legacy("N0TEST", ambed.IMBE, ambed.AMBEPLUS, ambed.AMBE2PLUS,
                                             ambed.CODEC2_3200)) or b""
        std = ambed.parse_std(reply)
        if std is not None:
            client.close_stream(std.stream_id)
            return True, "no CAPS, legacy open 4->(1,2,3) accepted"
        return False, "no CAPS, legacy open 4->(1,2,3) refused: %r" % reply
    finally:
        client.close()


def encode(rig, codec, pcm):
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [codec], pcm, port=rig.ambed_port,
                           transaction=0x50320000 + codec)[codec]


def p25_client(tg, index):
    return p25.P25Client("0.0.0.0", callsign=clients.CALLSIGNS[index], src_id=clients.DMR_IDS[index], tg=tg)
