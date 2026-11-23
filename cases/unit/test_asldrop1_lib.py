# SPDX-License-Identifier: MIT
"""dvtest/asldrop1.py (ASLDROP1: a refused AllStar voice packet is counted once, as rx late) and Client.voice_at."""
import socket
import struct

import pytest

from dvtest import asldrop1, metrics, radioseq1, rfpos1b, rfpos1c, xstale1
from dvtest.proto.iax2 import Client, parse_full

pytestmark = pytest.mark.unit


def test_switch():
    assert asldrop1.on({"DVT_ASLDROP1": "on"}) and not asldrop1.on({}) and not asldrop1.on({"DVT_ASLDROP1": ""})


def test_voice_at_sends_a_mini_with_the_given_timestamp_and_leaves_the_clock():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(0.5)
        caller = Client("127.0.0.1", "test-only", port=server.getsockname()[1], codec=4)
        try:
            frames = [bytes([i]) * 160 for i in range(5)]
            caller.voice(frames[0])
            base = caller.voice_log[0][0]
            caller.voice(frames[1])
            caller.voice(frames[2], drop=True)                  # frame 2 keeps its slot (base + 40) and is held back
            caller.voice(frames[3])
            caller.voice_at(base + 40, frames[2])                # sent after its successor
            caller.voice_at(base - 20, frames[4], kind="early")  # before the first timestamp
            clock = caller.voice_timestamp
            caller.voice(frames[4])
            assert caller.voice_timestamp == clock + 20
            packets = []
            while True:
                try:
                    packets.append(server.recvfrom(512)[0])
                except socket.timeout:
                    break
            assert len(packets) == 6
            assert parse_full(packets[0]).timestamp == base
            order = [struct.unpack("!HH", p[:4])[1] for p in packets[1:]]
            assert order == [(base + 20) & 0xffff, (base + 60) & 0xffff, (base + 40) & 0xffff, (base - 20) & 0xffff, (base + 80) & 0xffff]
            assert packets[3][4:] == frames[2] and packets[4][4:] == frames[4]
            assert [k for _t, k in caller.voice_log] == ["full", "mini", "drop", "mini", "late", "early", "mini"]
            assert asldrop1.sent_order(caller.voice_log) == [base, base + 20, base + 60, base + 40, (base - 20) & 0xffffffff, base + 80]
        finally:
            caller.socket.close()


def test_order_problems():
    log = [(1000, "full"), (1020, "mini"), (1040, "drop"), (1060, "mini"), (1080, "mini"), (1040, "late")]
    assert asldrop1.order_problems([1000, 1020, 1060, 1080, 1040], log) == []
    assert "another order" in asldrop1.order_problems([1000, 1020, 1040, 1060, 1080], log)[0]       # not reordered on the way
    assert "holds 4 voice frames of the call, the tester sent 5" in asldrop1.order_problems([1000, 1020, 1060, 1080], log)[0]


def heard(count=90, skip=None, shift=False):
    slots = {}
    n = 0
    for i in range(count):
        if i == skip:
            if not shift:
                n += 1                                  # a hole at that position
            continue
        slots[n] = i
        n += 1
    return slots


def test_judge_heard_control_and_refused():
    assert asldrop1.judge_heard(heard(), list(range(90)), None) == []
    # a refused frame: not heard, the others once in order; the output may keep a hole at the position or close it up (recorded, not judged)
    others = [i for i in range(90) if i != 40]
    assert asldrop1.judge_heard(heard(skip=40), others, 40) == []
    assert asldrop1.judge_heard(heard(skip=40, shift=True), others, 40) == []
    # the late frame was heard anyway (D-STAR at a wrong position, USRP in order), or a frame is missing besides it
    assert any("was heard" in p for p in asldrop1.judge_heard(heard(), list(range(90)), 40))
    assert any("wanted every frame but 40" in p for p in asldrop1.judge_heard(heard(skip=40), others[:-1], 40))
    assert any("was heard" in p and p.startswith("USRP") for p in asldrop1.judge_heard(heard(skip=40), list(range(90)), 40))
    # control: the 30 ms late frame must sit at its own position, a gap is a problem
    assert asldrop1.judge_heard(heard(skip=40), others, None)
    assert asldrop1.judge_early(heard(), list(range(90))) == []


def reading(room, **egress):
    return rfpos1b.Reading(radioseq1.Counts(*room), tuple((proc, xstale1.Counts(*c)) for proc, c in egress.items()))


def test_rx_counts_and_delta():
    samples = metrics.parse('dv_edge_over_units_total{dir="rx",proc="allstar",result="late"} 3\n'
                            'dv_edge_over_units_total{dir="rx",proc="allstar",result="gap"} 1\n'
                            'dv_edge_over_units_total{dir="rx",proc="ysf",result="late"} 9\n'
                            'dv_edge_over_units_total{dir="tx",proc="allstar",result="late"} 7\n')
    now = asldrop1.rx_counts(samples)
    assert now == {"gap": 1.0, "late": 3.0, "dropped": 0.0, "lost": 0.0}
    assert asldrop1.rx_delta(now, {"gap": 1.0, "late": 2.0, "dropped": 0.0, "lost": 0.0})["late"] == 1.0


def test_judge_counters():
    # radioseq1.Counts: edge_gap edge_late edge_dropped room_gap room_late room_lost overs_complete overs_lossy overs_end_missing egress_lost egress_late egress_dropped
    egress = dict(dextra=(0, 1, 0, 0, 0, 0, 0, 0), dmrhb=(0, 1, 0, 0, 0, 0, 0, 0), usrp=(0, 1, 0, 0, 0, 0, 0, 0))
    quiet = {"gap": 0.0, "late": 0.0, "dropped": 0.0, "lost": 0.0}
    # a: refused as more than 60 ms late: rx late +1, rx gap +1, room gap +1, over lossy +1 (no double count of one packet as two losses)
    a_rx = dict(quiet, late=1.0, gap=1.0)
    a = reading((1, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0, 0), **egress)
    assert asldrop1.judge_counters(a, a_rx, 1, gap=1, lossy=1)[0] == []
    # c: stamped before the first timestamp: rx late +1, gap 0, room late +1, over lossy +1
    c_rx = dict(quiet, late=1.0)
    c = reading((0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0), **egress)
    assert asldrop1.judge_counters(c, c_rx, 1, gap=0, lossy=1, room_late=1)[0] == []
    # b: nothing moves, an over folded
    b = reading((0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0), **egress)
    assert asldrop1.judge_counters(b, quiet, 0)[0] == []
    # the old contract line (a without the gap) now fails, and so does c with a gap or without the lossy over
    assert any("result=gap} rose by 0, wanted 1" in p for p in asldrop1.judge_counters(reading((0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0), **egress), dict(quiet, late=1.0), 1, gap=1, lossy=1)[0])
    assert any("result=gap} rose by 1, wanted 0" in p for p in asldrop1.judge_counters(a, a_rx, 1, gap=0, lossy=1, room_late=1)[0])
    assert any("result=lossy} rose by 0, wanted 1" in p for p in asldrop1.judge_counters(reading((0, 1, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0), **egress), c_rx, 1, gap=0, lossy=1, room_late=1)[0])
    assert any("room=l,result=late} rose by 0, wanted 1" in p for p in asldrop1.judge_counters(reading((0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0), **egress), c_rx, 1, gap=0, lossy=1, room_late=1)[0])
    # without room_late the room's late count is recorded, not judged
    problems, findings, _ = asldrop1.judge_counters(a, a_rx, 1, gap=1, lossy=1)
    assert problems == [] and any("room=l,result=late} rose by 1" in f for f in findings)
    # the contract's counter did not move / moved twice
    assert any("result=late} rose by 0, wanted 1" in p for p in asldrop1.judge_counters(a, quiet, 1, gap=1, lossy=1)[0])
    assert any("result=late} rose by 1, wanted 0" in p for p in asldrop1.judge_counters(b, dict(quiet, late=1.0), 0)[0])
    # rx lost and dropped, the room's lost and the egress tx lost and dropped never move
    moved = dict(a_rx, lost=2.0, dropped=3.0)
    problems = asldrop1.judge_counters(a, moved, 1, gap=1, lossy=1)[0]
    assert len(problems) == 2 and any("result=lost} rose by 2" in p for p in problems) and any("result=dropped} rose by 3" in p for p in problems)
    room = reading((1, 1, 0, 1, 1, 2, 0, 1, 0, 0, 0, 0), dextra=(0, 1, 0, 0, 0, 0, 0, 4), usrp=(0, 1, 0, 0, 0, 0, 3, 0))
    problems = asldrop1.judge_counters(room, a_rx, 1, gap=1, lossy=1)[0]
    assert sorted(p.split(" rose")[0].split("{")[1] for p in problems) == ["dir=tx,proc=dextra,result=lost}", "dir=tx,proc=usrp,result=dropped}", "room=l,result=lost}"]
    # no over closed, an egress edge that folded nothing: harness slips
    idle = reading((0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0), dextra=(0, 0, 0, 0, 0, 0, 0, 0))
    problems = asldrop1.judge_counters(idle, quiet, 0)[0]
    assert sum(p.startswith("harness:") for p in problems) == 2
