# SPDX-License-Identifier: MIT
"""dvtest.check.iax2 on a synthetic call built with dvtest.proto.iax2."""
import struct

from dvtest.proto import iax2 as I
from test_check_common import Wire, codes, frames_of

CALLER, NODE = ("192.0.2.30", 40000), ("192.0.2.40", 4569)
PCM = b"\x00\x01" * 160


class Side:
    def __init__(self, w, me, peer, callno):
        self.w, self.me, self.peer, self.callno = w, me, peer, callno
        self.remote = 0
        self.oseq = self.iseq = 0
        self.t0 = w.t

    def stamp(self):
        return int((self.w.t - self.t0) * 1000)

    def full(self, kind, sub, payload=b"", gap=0.001, ts=None, oseq=None):
        ts = self.stamp() if ts is None else ts
        seq = self.oseq if oseq is None else oseq
        self.w.send(*self.me, *self.peer, I.full_frame(self.callno, self.remote, ts, seq, self.iseq,
                                                       kind, sub, payload), gap)
        if not (kind == I.IAX and sub == I.ACK):
            self.oseq = (seq + 1) & 255
        return ts

    def ack(self, ts, gap=0.001):
        self.full(I.IAX, I.ACK, ts=ts, gap=gap)

    def mini(self, ts, payload=PCM, gap=0.020):
        self.w.send(*self.me, *self.peer, struct.pack("!HH", self.callno, ts & 0xFFFF) + payload, gap)


def exchange(a, b, kind, sub, payload=b"", ack=True):
    """a sends a reliable full frame, b receives it (and ACKs it)."""
    ts = a.full(kind, sub, payload)
    b.iseq = (b.iseq + 1) & 255
    if ack:
        b.ack(ts)
    return ts


def call(fault=None):
    w = Wire()
    c = Side(w, CALLER, NODE, 1234)
    n = Side(w, NODE, CALLER, 777)
    n.remote = 1234
    fmt = struct.pack("!I", I.SLIN)
    exchange(c, n, I.IAX, I.NEW, I.information(1, b"1999") + I.information(9, fmt) + I.information(8, fmt),
             ack=False)
    c.remote = 777
    exchange(n, c, I.IAX, I.AUTHREQ, I.information(14, b"\x00\x02") + I.information(15, b"123456789"))
    exchange(c, n, I.IAX, I.AUTHREP, I.information(16, b"0" * 32))
    if fault == "early_voice":
        c.mini(100)
    exchange(n, c, I.IAX, I.ACCEPT, I.information(9, fmt))
    exchange(n, c, I.CONTROL, 4)
    if fault == "oseq_gap":
        n.oseq += 2
    ts = exchange(n, c, I.VOICE, I.SLIN, PCM)
    for k in range(1, 30):
        if fault == "mini_gap" and k == 10:
            continue
        payload = PCM[:-2] if fault == "mini_size" and k == 5 else PCM
        n.mini(ts + 20 * k, payload)
    exchange(c, n, I.IAX, I.HANGUP, I.information(22, b"done"), ack=fault != "no_ack")
    if fault == "no_ack":
        w.send(*CALLER, *NODE, I.full_frame(9, 0, 0, 0, 0, I.IAX, 30), 6.0)    # POKE, no call
    return w


def test_clean_call(tmp_path):
    result = call().check(tmp_path)
    assert codes(result) == []
    p = result["protocols"]["iax2"]
    assert p["calls"][0]["state"] == "hangup" and p["calls"][0]["format"] == I.SLIN
    assert p["streams"][0]["frames"] == 30 and p["streams"][0]["loss"] == 0


def test_faults(tmp_path):
    assert codes(call("no_ack").check(tmp_path)) == ["iax2.no_ack"]
    result = call("oseq_gap").check(tmp_path)
    assert codes(result) == ["iax2.oseq_gap"]
    assert codes(call("mini_gap").check(tmp_path)) == ["iax2.voice_ts_loss"]
    result = call("mini_size").check(tmp_path)
    assert codes(result) == ["iax2.voice_size"] and frames_of(result, "iax2.voice_size") == [16]
    assert "iax2.voice_before_accept" in codes(call("early_voice").check(tmp_path))
