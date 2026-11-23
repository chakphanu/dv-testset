# SPDX-License-Identifier: MIT
"""Protocol kits of the XSTALE1 cases (cases/test_xstale1.py, docs/xstale1-cases.md); not a test module.

A kit makes endpoints of ONE edge: the talker A and the listener B are two clients of the same server port of the system under test (the USRP dynamic hub,
the NXDN and P25 server ports of rig/config/dv-router/soak-edges.toml), so the room's copy of A's over is rendered at the very edge A talks into.

  Endpoint.link()      True when the edge answered (USRP REG:OK, the NXDN / P25 poll echo)
  Endpoint.burst(n, seed, end)
                       one paced burst of n native units (20 ms each; NXDN 4 per 80 ms packet), blocking; end=False sends no unkey / terminator / trailer.
                       Returns the units sent (a multiple of the kit's `multiple`)
  Endpoint.heard()     voice units received so far (USRP frames, P25 IMBE records, NXDN frames), terminators and polls not counted
  Endpoint.pump(s)     drains the socket (and keeps the link alive: P25 polls)
  Endpoint.close()     unlink and close
"""
import os
import time

from crossmode_fake import UsrpPeer, usrp_hub
from dvtest import clients, xstale1
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import nxdn, p25, usrp

USRP_CALLSIGNS = ("N0TEST", "N0CALL")
NXDN_IDS = (1001, 1002)                         # N0CALL, N0TEST in rig/config/nxdn.csv (as the soak lanes)
USRP_MODULE = "A"
TG = 31672                                      # tg_modules / server_tg of rig/config/dv-router/soak-edges.toml


class Endpoint:
    def link(self):
        raise NotImplementedError

    def burst(self, units, seed, end=True):
        raise NotImplementedError

    def heard(self):
        raise NotImplementedError

    def pump(self, seconds):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class UsrpEndpoint(Endpoint):
    def __init__(self, hub, index, module):
        self.peer = UsrpPeer(hub, callsign=USRP_CALLSIGNS[index], module=module)

    def link(self):
        if not self.peer.link(timeout=8.0):
            return False
        self.peer.pump(0.2)
        self.peer.inbox.clear()
        return True

    def burst(self, units, seed, end=True):
        frames = signals.frames(signals.tone(700.0, units * 20, amp=8000))[:units]
        start = time.monotonic()
        for i, frame in enumerate(frames):
            pace(start, i)
            self.peer.send(usrp.voice, frame)
        if end:
            self.peer.send(usrp.unkey)
        return len(frames)

    def heard(self):
        self.peer.pump(0.02)
        return len(self.peer.voice())

    def pump(self, seconds):
        self.peer.pump(seconds)

    def close(self):
        try:
            self.peer.unlink()
        except OSError:
            pass
        self.peer.close()


class P25Endpoint(Endpoint):
    def __init__(self, ip, index, tg):
        self.ip = ip
        self.c = p25.P25Client("0.0.0.0", callsign=clients.CALLSIGNS[index], src_id=clients.DMR_IDS[index], tg=tg)

    def link(self):
        return self.c.link(self.ip)

    def burst(self, units, seed, end=True):
        frames = signals.prng_frames("imbe", units, seed="xstale1/%s" % seed)
        records = p25.call_records(frames, self.c.src_id, self.c.tg, end=end)
        self.c.send(records, p25.GAP)
        return units

    def heard(self):
        self.c.pump(0.02)
        return sum(1 for r in self.c.records() if not r["terminator"])

    def pump(self, seconds):
        self.c.pump(seconds)

    def close(self):
        try:
            if self.c.dest:
                self.c.unlink()
        finally:
            self.c.close()


class NxdnEndpoint(Endpoint):
    def __init__(self, ip, index, tg):
        self.ip = ip
        self.c = nxdn.NxdnClient("0.0.0.0", tg, nxdn_id=NXDN_IDS[index], callsign=clients.CALLSIGNS[index])

    def link(self):
        return self.c.link(self.ip)

    def burst(self, units, seed, end=True):
        frames = [nxdn.from_dmr(f) for f in signals.prng_frames("dmr", units, seed="xstale1/%s" % seed)]
        packets = nxdn.stream_packets(frames, self.c.id, self.c.tg, trailer=end)
        self.c.keepalive()
        self.c.client.paced(self.c.dest, packets, nxdn.PACKET_GAP)
        return units

    def heard(self):
        self.c.client.pump(0.02)
        return sum(len(nxdn.voice_frames(info["payload"])) for info in self.c.packets() if info["kind"] == "voice")

    def pump(self, seconds):
        self.c.client.pump(seconds)

    def close(self):
        try:
            if self.c.dest:
                self.c.unlink()
        finally:
            self.c.close()


class WitnessEndpoint(Endpoint):
    """A client of ANOTHER protocol (DVT_XSTALE1_WITNESS=dmr, dstar, ...) linked to the room of the edge under test for the whole case: a diagnosis, not a verdict. It
    tells whether the talker's over reached the room at all when the edge under test never folds it (a room with only clients of one edge may publish nothing back)."""

    def __init__(self, ip, kind, module):
        self.ip, self.kind = ip, kind
        self.c = clients.make(kind, module, 1)

    def link(self):
        if not self.c.link(self.ip):
            return False
        self.c.client.pump(0.3)
        self.c.client.inbox.clear()
        return True

    def burst(self, units, seed, end=True):
        raise NotImplementedError

    def heard(self):
        self.c.client.pump(0.02)
        return len(self.c.client.inbox)

    def pump(self, seconds):
        self.c.client.pump(seconds)

    def close(self):
        try:
            if self.c.dest:
                self.c.unlink()
        finally:
            self.c.close()


class Kit:
    """name: the id of the parametrization; proc: the metrics label of the edge; multiple: units of a burst are a multiple of it; slack: units a listener may
    hear beyond what was sent (the padding of a last P25 LDU of 9 records, a last NXDN packet of 4 frames)."""

    def __init__(self, name, multiple, slack, make, hint="", module="A"):
        self.name, self.proc, self.multiple, self.slack, self.make, self.hint, self.module = name, name, multiple, slack, make, hint, module

    def endpoint(self, index):
        return self.make(index)

    def witness(self, ip, kind):
        return WitnessEndpoint(ip, kind, self.module)

    def units(self, seconds):
        return xstale1.units_for(seconds, self.multiple)


def kits(target, env=None):
    """{name: Kit} of the three edges, or a skip reason string for the USRP hub: ({kits}, {name: reason it is not available})."""
    env = os.environ if env is None else env
    ip = target.xlxd_sut
    out, missing = {}, {}
    hub, why = usrp_hub(target)
    module = env.get("DVT_XSTALE1_USRP_MODULE", USRP_MODULE)
    if hub is None:
        missing["usrp"] = why
    else:
        out["usrp"] = Kit("usrp", 1, 0, lambda i: UsrpEndpoint(hub, i, module),
                          "run with DVT_USRP_DYNAMIC=1 (docs/rig.md): the dynamic USRP hub answers REG:OK", module)
    p25_tg = int(env.get("DVT_P25_TG") or TG)
    nxdn_tg = int(env.get("DVT_NXDN_TG") or TG)
    out["p25"] = Kit("p25", 9, p25.FRAMES_PER_LDU, lambda i: P25Endpoint(ip, i, p25_tg),
                     "put rig/config/dv-router/soak-edges.toml into DVT_SUT_CONFIG_EXTRA (the SUT's own P25 server port, TG %d)" % p25_tg,
                     env.get("DVT_P25_MODULE") or "G")
    out["nxdn"] = Kit("nxdn", 12, 4, lambda i: NxdnEndpoint(ip, i, nxdn_tg),
                      "put rig/config/dv-router/soak-edges.toml into DVT_SUT_CONFIG_EXTRA (the SUT's own NXDN server port, TG %d)" % nxdn_tg,
                      env.get("DVT_NXDN_MODULE") or "F")
    return out, missing
