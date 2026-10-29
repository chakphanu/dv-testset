# SPDX-License-Identifier: MIT
"""One way to make, link and drive the digital clients by kind name
("dstar", "dmr", "m17"), for cases that loop over protocols."""
import random

from .proto import dextra, dmr, m17

KINDS = ("dstar", "dmr", "m17")
# AMBEd codec of each kind's frames
CODEC = {"dstar": 1, "dmr": 2, "m17": 3}
# synthetic identities, present in rig/config/dmrid.dat
CALLSIGNS = ("N0CALL", "N0TEST")
DMR_IDS = (1023007, 1023008)

_rng = random.SystemRandom()


def make(kind, module, index=0):
    """Client number index (0 or 1) of a kind on a module."""
    if kind == "dstar":
        return dextra.DstarClient("0.0.0.0", callsign=CALLSIGNS[index], module=module)
    if kind == "dmr":
        return dmr.DmrClient("0.0.0.0", dmrid=DMR_IDS[index], module=module)
    if kind == "m17":
        return m17.M17Client("0.0.0.0", callsign=CALLSIGNS[index], module=module)
    raise ValueError(kind)


def fresh_stream_id(kind):
    """Random non-zero stream id: xlxd drops a header whose id equals the
    module's last outbound id, so every talk gets a new one."""
    bits = 32 if kind == "dmr" else 16
    return _rng.randrange(1, 1 << bits)


def talk(client, kind, frames, reflector="XLX999", tail=2.0):
    sid = fresh_stream_id(kind)
    if kind == "dstar":
        client.talk(frames, sid, reflector=reflector, tail=tail)
    elif kind == "dmr":
        client.talk(frames, stream_id=sid, tail=tail)
    else:
        client.talk(frames, sid, tail=tail)
    return sid


def wire_frames(kind, frames):
    """Frames a same-kind listener receives through xlxd for a talk of
    frames: M17 packet 0 opens the stream and is not forwarded, the EOT
    packet repeats the last pair; DMR pads to whole bursts of three."""
    if kind == "m17":
        out = []
        for packet in m17.voice_packets(frames, 1, CALLSIGNS[0])[1:]:
            out.extend(m17.parse(packet)["frames"])
        return out
    if kind == "dmr":
        return list(frames) + [bytes(9)] * (-len(frames) % 3)
    return list(frames)


def close(*clients):
    for client in clients:
        try:
            if client.dest:
                client.unlink()
        finally:
            client.close()
