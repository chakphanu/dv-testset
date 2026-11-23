# SPDX-License-Identifier: MIT
"""XSTALE1c (docs/xstale1c-cases.md): dvtest/xstale1c.py (switch, no_radio_id series, verdicts) and the D-STAR talker / leave and rejoin of the endpoints."""
import json
import socket
from types import SimpleNamespace

import xstale1_support as xs
import xstale1c_support as xc
from dvtest import metrics, xstale1 as x, xstale1c as c
from dvtest.proto import dextra


def zero(**kw):
    base = dict.fromkeys(x.Counts._fields, 0.0)
    base.update(kw)
    return x.Counts(**base)


def test_switch():
    assert c.on({"DVT_XSTALE1C": "on"}) and c.on({"DVT_XSTALE1C": "1"}) and not c.on({}) and not c.on({"DVT_XSTALE1C": "off"}) and not c.on({"DVT_XSTALE1": "on"})


def test_no_radio_id_series_per_proc():
    samples = metrics.parse('dv_edge_drops_total{proc="p25",reason="no_radio_id"} 2\n'
                            'dv_edge_drops_total{proc="p25",reason="x_stale"} 9\n'
                            'dv_edge_drops_total{proc="dmrhb",reason="no_radio_id"} 1\n'
                            'dv_edge_overs_total{proc="p25",dir="tx",result="complete"} 4\n')
    got = c.no_radio_id(samples)
    assert got == {"p25": 2.0, "dmrhb": 1.0} and c.total(got) == 3 and c.no_radio_id([]) == {}
    assert c.moved_procs(got, {"p25": 1.0, "dmrhb": 1.0}) == {"p25": 1.0} and c.moved_procs(got, got) == {}


def test_judge_leave():
    assert c.judge_leave(zero(complete=1)) == []
    assert any("lost" in p for p in c.judge_leave(zero(complete=1, lost=350, lossy=0)))                        # the behaviour before the fix: sent 150 lost 350
    assert any("lossy" in p for p in c.judge_leave(zero(lossy=1, lost=350)))
    assert any("joined" in p for p in c.judge_leave(zero(joined=1)))
    assert any("folded 0 overs" in p for p in c.judge_leave(zero()))
    assert any("folded 2 overs" in p for p in c.judge_leave(zero(complete=2)))


def test_judge_leave_allow_end_missing_only_when_asked():
    assert c.judge_leave(zero(end_missing=1), allow=("end_missing",)) == []
    assert any("end_missing" in p for p in c.judge_leave(zero(end_missing=1)))
    assert any("lost" in p for p in c.judge_leave(zero(end_missing=1, lost=350), allow=("end_missing",)))


def test_judge_rejoin():
    ok = zero(joined=1)
    assert c.judge_rejoin(ok, 190, 200, 396, 9) == []
    assert any("did not fold as joined" in p for p in c.judge_rejoin(zero(complete=1), 190, 200, 396, 9))
    assert any("joined 2" in p for p in c.judge_rejoin(zero(joined=2), 190, 200, 396, 9))
    assert any("heard 100" in p for p in c.judge_rejoin(ok, 100, 200, 396, 9))
    assert any("lost" in p for p in c.judge_rejoin(zero(joined=1, lost=150), 190, 200, 396, 9))             # a double count of the first interval
    assert any("late" in p for p in c.judge_rejoin(zero(joined=1, late=1), 190, 200, 396, 9))


def test_judge_refusal():
    one = {"p25": 1.0}
    assert c.judge_refusal(zero(), one, 0) == []
    assert any("exactly one" in p for p in c.judge_refusal(zero(), {}, 0))
    assert any("exactly one" in p for p in c.judge_refusal(zero(), {"p25": 1.0, "dmrhb": 1.0}, 0))
    assert any("dropped rose by 200" in p for p in c.judge_refusal(zero(dropped=200), one, 0))
    assert any("opened no egress" in p for p in c.judge_refusal(zero(complete=1), one, 0))
    assert any("heard 3" in p for p in c.judge_refusal(zero(), one, 3))


def test_hear_before():
    assert c.hear_before(150, 90) and not c.hear_before(150, 89)


def test_dstar_talker_skips_counters_and_keeps_the_slots(monkeypatch):
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(1.0)
    t = xc.DstarTalker("127.0.0.1", "N0CALL", "G")
    t.c.dest = ("127.0.0.1", srv.getsockname()[1])
    try:
        sent, left_out = t.over(30, "unit", skip=(5, 7, 99), end=True)
        got = []
        while True:
            try:
                got.append(srv.recvfrom(2048)[0])
            except socket.timeout:
                break
    finally:
        srv.close()
        t.close()
    assert (sent, left_out) == (28, 2)
    assert dextra.parse_header(got[0]) is not None
    voice = [dextra.parse_voice(d) for d in got[1:]]
    assert [v[1] for v in voice[:-1]] == [i % 21 for i in range(30) if i not in (5, 7)]
    assert voice[-1][2] and voice[-1][1] == 30 % 21
    assert len({v[0] for v in voice} | {dextra.parse_header(got[0])["stream_id"]}) == 1


def test_write_json_collects_one_dict_per_case(tmp_path):
    target = SimpleNamespace(out=str(tmp_path))
    xc.write_json(target, "XS1c-a[p25]", {"lost": 0})
    xc.write_json(target, "XS1c-d[p25]", {"rise": {"p25": 1.0}})
    xc.write_json(target, "XS1c-a[p25]", {"lost": 3})
    assert json.loads((tmp_path / "xstale1c.json").read_text()) == {"XS1c-a[p25]": {"lost": 3}, "XS1c-d[p25]": {"rise": {"p25": 1.0}}}


# ---- leave / rejoin of the endpoints against a loopback sink: an unlink goes out, no poll follows it, the rejoin polls again and restarts the count

def test_p25_endpoint_leave_stops_polls_and_rejoin_restarts_heard():
    import time
    from dvtest.proto import p25
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.settimeout(0.05)
    ep = xs.P25Endpoint("127.0.0.1", 1, 31672)
    ep.c.client.inbox.append(p25.poll_packet(ep.c.callsign))                      # the echo of the server
    ep.c.dest = sink.getsockname()
    try:
        ep.c.client.inbox.extend(p25.call_records(__import__("dvtest.audio.signals", fromlist=["x"]).prng_frames("imbe", 9, seed="u"), 1, 31672, end=True))
        ep.c.last_poll = time.monotonic()
        assert ep.heard() == 9
        ep.leave()
        assert ep.c.dest is None
        ep.c.last_poll = 0.0
        ep.pump(0.1)                                                              # POLL_PERIOD has passed: a client with a dest would poll now
        got = []
        while True:
            try:
                got.append(sink.recvfrom(2048)[0])
            except socket.timeout:
                break
        assert [p25.parse_poll(d)[0] for d in got] == ["unlink"]
        assert ep.rejoin() is True and ep.heard() == 0
        time.sleep(0.05)
        assert [p25.parse_poll(sink.recvfrom(2048)[0])[0]] == ["poll"]
    finally:
        ep.c.dest = None
        ep.close()
        sink.close()


def test_nxdn_endpoint_leave_and_rejoin():
    from dvtest.proto import nxdn
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.settimeout(0.5)
    ep = xs.NxdnEndpoint("127.0.0.1", 1, 31672)
    ep.c.client.inbox.append(nxdn.poll_packet(ep.c.callsign, 31672))
    ep.c.dest = sink.getsockname()
    try:
        ep.leave()
        assert ep.c.dest is None and nxdn.parse_poll(sink.recvfrom(2048)[0])["kind"] == "unlink"
        assert ep.rejoin() is True and nxdn.parse_poll(sink.recvfrom(2048)[0])["kind"] == "poll"
    finally:
        ep.c.dest = None
        ep.close()
        sink.close()
