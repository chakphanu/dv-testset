# SPDX-License-Identifier: MIT
"""Helpers of the XSTALE1c cases (cases/test_xstale1c.py, docs/xstale1c-cases.md); not a test module.

DstarTalker is a DExtra client that talks into the module of the P25 edge under test (a cross-protocol over: the room transcodes it to IMBE for the P25
listener) and can leave out single voice packets BEFORE the leave. A D-STAR source is placed by its own DSVT counter, so a counter the tester skips stays a
hole in the publication ordinals (a real loss at the listener's egress), which an IMBE or USRP talker cannot do from outside (their ordinals follow the
frames that arrived, the wire gaps are counted separately at the ingress).
"""
import json
import time
import zlib
from pathlib import Path

from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import dextra


class DstarTalker:
    """One DExtra client on module `module` of the system under test; `callsign` decides whether the radio id lookup of the egress edge finds an id."""

    def __init__(self, ip, callsign, module, reflector="XLX999"):
        self.ip, self.reflector = ip, reflector
        self.c = dextra.DstarClient("0.0.0.0", callsign=callsign, module=module)

    def link(self):
        return self.c.link(self.ip)

    def over(self, units, seed, skip=(), end=True):
        """Header, `units` voice packets at 20 ms (counters `index mod 21`, the packets of `skip` are not sent, their time slot stays) and the last packet.
        Returns (units sent, units left out)."""
        sid = 1 + zlib.crc32(seed.encode()) % 0xFFFE
        frames = signals.prng_frames("dstar", units, seed="xstale1c/%s" % seed)
        self.c.client.send(self.c.dest, self.c.header(sid, self.reflector))
        self.c.client.pump(0.3)
        start = time.monotonic()
        sent = 0
        for index, frame in enumerate(frames):
            pace(start, index)
            if index in skip:
                continue
            slow = dextra.SYNC_DATA if index % 21 == 0 else dextra.SLOW_DATA
            self.c.client.send(self.c.dest, dextra.voice(sid, index, frame, False, slow))
            self.c.client.drain()
            sent += 1
        if end:
            pace(start, units)
            self.c.client.send(self.c.dest, dextra.voice(sid, units, b"\x00" * 9, True))
        return sent, len([i for i in skip if i < units])

    def pump(self, seconds):
        self.c.client.pump(seconds)

    def close(self):
        try:
            if self.c.dest:
                self.c.unlink()
        finally:
            self.c.close()


def write_json(target, case, data, name="xstale1c.json"):
    """out/<run>/xstale1c.json holds one dict per case: read what the run has so far, set this case, write back."""
    path = Path(target.out) / name
    try:
        try:
            whole = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(whole, dict):
                whole = {}
        except (OSError, ValueError):
            whole = {}
        whole[case] = data
        path.write_text(json.dumps(whole, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
