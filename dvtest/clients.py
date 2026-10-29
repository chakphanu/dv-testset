# SPDX-License-Identifier: MIT
"""One way to make, link and drive the digital clients by kind name, for
cases that loop over protocols. KINDS (dstar, dmr, m17) is the matrix set;
EXTRA_KINDS (ysf, dplus, dcs) are made and driven the same way but kept out
of the matrix (ysf is not byte exact through xlxd, see wire_frames)."""
import random

from .proto import dcs, dextra, dmr, dplus, m17, ysf

KINDS = ("dstar", "dmr", "m17")
EXTRA_KINDS = ("ysf", "dplus", "dcs")
# AMBEd codec of each kind's frames
CODEC = {"dstar": 1, "dmr": 2, "m17": 3, "ysf": 2, "dplus": 1, "dcs": 1}
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
    if kind == "ysf":
        return ysf.YsfClient("0.0.0.0", callsign=CALLSIGNS[index], module=module)
    if kind == "dplus":
        return dplus.DplusClient("0.0.0.0", callsign=CALLSIGNS[index], module=module)
    if kind == "dcs":
        return dcs.DcsClient("0.0.0.0", callsign=CALLSIGNS[index], module=module)
    raise ValueError(kind)


def fresh_stream_id(kind):
    """Random non-zero stream id: xlxd drops a header whose id equals the
    module's last outbound id, so every talk gets a new one."""
    bits = 32 if kind == "dmr" else 16
    return _rng.randrange(1, 1 << bits)


def talk(client, kind, frames, reflector="XLX999", tail=2.0):
    sid = fresh_stream_id(kind)
    if kind in ("dstar", "dplus", "dcs"):
        client.talk(frames, sid, reflector=reflector, tail=tail)
    elif kind == "ysf":
        client.talk(frames, sid, tail=tail)        # xlxd assigns YSF stream ids
    elif kind == "dmr":
        client.talk(frames, stream_id=sid, tail=tail)
    else:
        client.talk(frames, sid, tail=tail)
    return sid


def wire_frames(kind, frames, m17_fn0=False):
    """Frames a same-kind listener receives through xlxd for a talk of
    frames: M17 packet 0 opens the stream and is not forwarded (xlxd; a
    reflector that relays it, m17_fn0=True, delivers it too), the EOT
    packet repeats the last pair; DMR pads to whole bursts of three. YSF is
    not byte exact (xlxd steps the b2 gain bits) and has no entry here."""
    if kind == "ysf":
        raise ValueError("ysf frames are not byte exact through xlxd")
    if kind == "m17":
        out = []
        for packet in m17.voice_packets(frames, 1, CALLSIGNS[0])[0 if m17_fn0 else 1:]:
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
