# SPDX-License-Identifier: MIT
"""XSTALE1 (docs/xstale1-cases.md): dvtest/xstale1.py (counts, settle waits, verdicts) and the protocol kits of cases/xstale1_support.py against loopback sockets."""
import socket
import time
from types import SimpleNamespace

import pytest

import xstale1_support as xs
from dvtest import metrics, xstale1 as x
from dvtest.proto import nxdn, p25, usrp


def text(proc="p25", x_stale=0, complete=0, lossy=0, end_missing=0, joined=0, late=0, dropped=0, lost=0, extra=""):
    lines = ['dv_edge_drops_total{proc="%s",reason="x_stale"} %g' % (proc, x_stale),
             'dv_edge_drops_total{proc="%s",reason="no_session"} 99' % proc]
    for result, value in (("complete", complete), ("lossy", lossy), ("end_missing", end_missing), ("joined", joined)):
        lines.append('dv_edge_overs_total{proc="%s",dir="tx",result="%s"} %g' % (proc, result, value))
    for result, value in (("late", late), ("dropped", dropped), ("lost", lost)):
        lines.append('dv_edge_over_units_total{proc="%s",dir="tx",result="%s"} %g' % (proc, result, value))
    return "\n".join(lines) + "\n" + extra


def counts(**kw):
    return x.counts(metrics.parse(text(**kw)), kw.get("proc", "p25"))


def zero(**kw):
    base = dict.fromkeys(x.Counts._fields, 0.0)
    base.update(kw)
    return x.Counts(**base)


def test_switch():
    assert x.on({"DVT_XSTALE1": "on"}) and x.on({"DVT_XSTALE1": "1"}) and not x.on({}) and not x.on({"DVT_XSTALE1": "off"})


def test_counts_read_one_proc_and_the_tx_direction_only():
    other = ('dv_edge_drops_total{proc="usrp",reason="x_stale"} 7\n'
             'dv_edge_overs_total{proc="usrp",dir="tx",result="joined"} 5\n'
             'dv_edge_overs_total{proc="p25",dir="rx",result="complete"} 40\n'
             'dv_edge_over_units_total{proc="p25",dir="rx",result="late"} 3\n')
    c = x.counts(metrics.parse(text(x_stale=4, complete=2, joined=1, late=1, lost=2, extra=other)), "p25")
    assert c == x.Counts(x_stale=4, complete=2, lossy=0, end_missing=0, joined=1, late=1, dropped=0, lost=2)
    assert x.counts(metrics.parse(text(extra=other)), "usrp").joined == 5 and x.counts([], "p25") == zero()
    assert x.procs_seen(metrics.parse(text(extra=other))) == ["p25", "usrp"]


def test_delta_and_folds():
    d = x.delta(counts(complete=3, joined=2, x_stale=5), counts(complete=1, x_stale=1))
    assert d.complete == 2 and d.joined == 2 and d.x_stale == 4 and x.folds(d) == 4


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_wait_settled_reads_once_more_after_the_quiet_period():
    clk, reads = Clock(), []

    def read():
        reads.append(clk.t)
        return zero(complete=1) if clk.t >= 10 else zero()

    got = x.wait_settled(read, zero(), clock=clk.now, sleep=clk.sleep)
    assert x.folds(got) == 1 and reads[-1] - reads[-2] == x.QUIET_S


def test_wait_settled_wants_two_folds_and_times_out():
    clk = Clock()
    with pytest.raises(TimeoutError, match="by 2"):
        x.wait_settled(lambda: zero(complete=1), zero(), want_folds=2, budget_s=20.0, clock=clk.now, sleep=clk.sleep)


def test_stable_baseline_waits_for_a_quiet_period():
    clk, seq = Clock(), iter([zero(joined=1), zero(joined=2), zero(joined=3), zero(joined=3)])
    got = x.stable_baseline(lambda: next(seq), clock=clk.now, sleep=clk.sleep)
    assert got.joined == 3 and clk.t == 3 * x.QUIET_S
    clk = Clock()
    n = [0]

    def moving():
        n[0] += 1
        return zero(joined=n[0])
    assert x.stable_baseline(moving, budget_s=30.0, clock=clk.now, sleep=clk.sleep).joined == n[0]         # never quiet: the last read, no exception


def test_units_and_rest_arithmetic():
    assert x.units_for(8.0, 9) == 396 and x.units_for(8.0, 12) == 396 and x.units_for(4.0, 12) == 192 and x.units_for(8.0) == 400
    assert x.expected_rest(8.0, 3.3) == 235 and x.expected_rest(8.0, 9.0) == 0
    assert x.heard_window(235, 396, 9) == (188, 405)
    assert x.SILENCE_S > x.SILENCE_LIMIT_S + 0.5 and x.BURST_S > x.SILENCE_LIMIT_S * 2


def test_judge_b():
    assert x.judge_b(zero(complete=1), 0) == []
    assert any("no egress over" in p for p in x.judge_b(zero(), 0))
    bad = x.judge_b(zero(complete=1, x_stale=380, end_missing=1, late=2), 0)
    assert len(bad) == 3 and any("x_stale" in p for p in bad) and any("end_missing" in p for p in bad)
    assert x.judge_b(zero(complete=1, x_stale=2), 0) == [] and x.judge_b(zero(complete=1, x_stale=3), 0)           # at most 2
    assert any("joined" in p for p in x.judge_b(zero(joined=1), 0))
    assert any("heard 5 units of its own" in p for p in x.judge_b(zero(complete=1), 5))


def test_judge_c():
    ok = zero(joined=1)
    assert x.judge_c(ok, 235, 235, 396, 9) == []
    assert x.judge_c(ok, 188, 235, 396, 9) == []                                        # exactly 80 percent
    assert any("heard 187" in p for p in x.judge_c(ok, 187, 235, 396, 9))
    assert any("heard 0" in p for p in x.judge_c(zero(complete=1, end_missing=1, x_stale=300), 0, 235, 396, 9))        # the behaviour before XSTALE1
    assert any("more than" in p for p in x.judge_c(ok, 406, 235, 396, 9))
    assert any("did not fold as joined" in p for p in x.judge_c(zero(complete=1), 235, 235, 396, 9))
    assert any("lost" in p for p in x.judge_c(zero(joined=1, lost=3), 235, 235, 396, 9))
    assert any("no egress over" in p for p in x.judge_c(zero(), 235, 235, 396, 9))


def test_judge_d():
    two = zero(complete=2)
    assert x.judge_d(two, 190, 192, 192, 192, 4) == []
    assert x.judge_d(zero(complete=1, end_missing=1, lossy=1), 190, 192, 192, 192, 4) == []         # silence without End: documented, not asserted
    assert any("second burst" in p for p in x.judge_d(zero(complete=1), 190, 192, 0, 192, 4))
    assert any("first burst" in p for p in x.judge_d(two, 70, 192, 192, 192, 4))                    # the stream closed 1.6 s in
    assert any("new stream" in p for p in x.judge_d(zero(complete=1), 190, 192, 192, 192, 4))
    assert any("x_stale" in p for p in x.judge_d(zero(complete=2, x_stale=100), 190, 192, 192, 192, 4))
    assert any("late" in p for p in x.judge_d(zero(complete=2, late=1), 190, 192, 192, 192, 4))


# ---- the kits against loopback sockets: what a burst puts on the wire and what heard() counts

def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    return sock


def collect(sock, seconds):
    out, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            out.append(sock.recvfrom(2048)[0])
        except socket.timeout:
            pass
    return out


def forward(packets, ep_sock_addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for p in packets:
        s.sendto(p, ep_sock_addr)
    s.close()


def test_usrp_endpoint_burst_and_heard():
    sink = receiver()
    a, b = xs.UsrpEndpoint(sink.getsockname(), 0, "A"), xs.UsrpEndpoint(sink.getsockname(), 1, "A")
    try:
        assert a.burst(25, "t", True) == 25
        got = collect(sink, 0.2)
        assert len(got) == 26 and [usrp.parse(d).keyup for d in got[:25]] == [1] * 25 and usrp.parse(got[-1]).keyup == 0
        assert a.burst(10, "t", False) == 10 and len(collect(sink, 0.2)) == 10             # no unkey
        forward(got, b.peer.sock.getsockname())
        time.sleep(0.05)
        assert b.heard() == 25                                                            # the unkey is not voice
    finally:
        a.close()
        b.close()
        sink.close()


def test_p25_endpoint_burst_and_heard():
    sink = receiver()
    a, b = xs.P25Endpoint("127.0.0.1", 0, 31672), xs.P25Endpoint("127.0.0.1", 1, 31672)
    a.c.dest = b.c.dest = sink.getsockname()
    try:
        assert a.burst(18, "t", True) == 18
        got = collect(sink, 0.2)
        assert len(got) == 19 and p25.parse(got[-1])["terminator"] and not any(p25.parse(d)["terminator"] for d in got[:-1])
        assert a.burst(9, "t", False) == 9 and len(collect(sink, 0.2)) == 9                # no terminator
        forward(got, b.c.client.local)
        time.sleep(0.05)
        assert b.heard() == 18
    finally:
        a.c.dest = b.c.dest = None
        a.close()
        b.close()
        sink.close()


def test_nxdn_endpoint_burst_and_heard():
    sink = receiver()
    a, b = xs.NxdnEndpoint("127.0.0.1", 0, 31672), xs.NxdnEndpoint("127.0.0.1", 1, 31672)
    a.c.dest = b.c.dest = sink.getsockname()
    try:
        assert a.burst(24, "t", True) == 24
        got = [d for d in collect(sink, 0.3) if nxdn.parse(d)]
        kinds = [nxdn.parse(d)["kind"] for d in got]
        assert kinds == ["header"] + ["voice"] * 6 + ["trailer"]
        assert a.burst(12, "t", False) == 12
        assert [nxdn.parse(d)["kind"] for d in collect(sink, 0.3) if nxdn.parse(d)] == ["header"] + ["voice"] * 3        # no trailer
        forward(got, b.c.client.local)
        time.sleep(0.05)
        assert b.heard() == 24
    finally:
        a.c.dest = b.c.dest = None
        a.close()
        b.close()
        sink.close()


def test_kits_follow_the_target_and_the_environment():
    target = SimpleNamespace(xlxd_sut="192.0.2.1", sut_profile="dvrouter")
    table, missing = xs.kits(target, env={})
    assert "p25" in table and "nxdn" in table
    assert set(table) | set(missing) == {"usrp", "p25", "nxdn"}
    assert table["p25"].units(8.0) == 396 and table["nxdn"].units(8.0) == 396 and table["p25"].slack == 9 and table["nxdn"].slack == 4
    assert all(k.proc in x.EDGES and "soak-edges" in k.hint for n, k in table.items() if n != "usrp")
