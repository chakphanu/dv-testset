# SPDX-License-Identifier: MIT
"""dvtest/ice1.py: the pure helpers of the ICE1 case (address change, ICE restart on the same circuit)."""
import pytest

from dvtest import ice1

pytestmark = pytest.mark.unit


def series(rate_before, stall, rate_after, t_change=10.0, step=0.1, total=6.0, start=100):
    """[(t, packets)]: `rate_before` packets/s until t_change, nothing for `stall` s, then `rate_after` packets/s."""
    out, n, t = [], start, t_change - 2.0
    while t < t_change + total:
        out.append((round(t, 3), int(n)))
        rate = rate_before if t < t_change else (0.0 if t < t_change + stall else rate_after)
        n += rate * step
        t += step
    return out


def test_budgets_are_named_and_per_engine():
    assert ice1.resume_budget_s("chromium") == 8.0
    assert ice1.resume_budget_s("firefox") == 8.0 and ice1.resume_budget_s("webkit") == 12.0
    assert ice1.resume_budget_s("other") == ice1.RESUME_BUDGET_DEFAULT_S == 12.0


def test_resume_after_a_stall():
    a = ice1.resume_analysis(series(50, 2.0, 50), 10.0)
    assert a["stalled"] and a["resumed"] and not a["inconclusive"]
    assert 2.0 <= a["resume_s"] <= 2.5 and 2.0 <= a["gap_s"] <= 2.6
    assert 0.0 <= a["stall_start_s"] <= 0.2


def test_the_audio_that_never_stopped_is_a_resume_at_zero():
    a = ice1.resume_analysis(series(50, 0.0, 50), 10.0)
    assert a == {"stalled": False, "resumed": True, "resume_s": 0.0, "gap_s": 0.0, "stall_start_s": None, "inconclusive": False,
                 "samples": a["samples"]}


def test_no_resume_inside_the_samples():
    a = ice1.resume_analysis(series(50, 99.0, 50), 10.0)
    assert a["stalled"] and not a["resumed"] and a["resume_s"] is None and a["gap_s"] is None


def test_a_few_packets_in_flight_do_not_end_the_stall():
    s = series(50, 3.0, 50)
    # five packets that were already in the browser right after the change
    s = [(t, n + (5 if 10.0 < t < 11.0 else 0)) for t, n in s]
    s = [(t, n + (5 if t >= 11.0 else 0)) for t, n in s]
    a = ice1.resume_analysis(s, 10.0)
    assert a["resumed"] and a["resume_s"] >= 3.0                    # below resume_packets of growth is not "back"


def test_too_short_a_window_is_inconclusive_and_empty_samples_too():
    assert ice1.resume_analysis([(9.9, 100), (10.1, 105)], 10.0)["inconclusive"]
    assert ice1.resume_analysis([], 10.0)["inconclusive"]


def test_the_stall_must_last_stall_min_s():
    s = [(9.9, 100), (10.0, 105), (10.1, 105), (10.2, 110), (10.3, 115), (10.4, 120), (10.5, 125), (10.6, 130), (10.7, 135)]
    a = ice1.resume_analysis(s, 10.0)
    assert not a["stalled"] and a["resumed"] and a["resume_s"] == 0.0     # 0.1 s without a packet is no stall


def test_inbound_total_adds_the_growth_of_each_entry():
    c = ice1.InboundTotal()
    assert c.update("a", 10) == 10
    assert c.update("a", 25) == 25
    assert c.update("b", 5) == 30                                    # a new entry from zero
    assert c.update("b", 3) == 33                                    # the counter was reset
    assert c.update("b", 4) == 34
    assert c.update("a", None) == 34


SDP_A = "v=0\r\na=ice-ufrag:AbCd\r\na=ice-pwd:secretsecretsecret\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"
SDP_B = "v=0\r\na=ice-ufrag:WxYz\r\na=ice-pwd:othersecretothersec\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"


def test_ice_ufrags_never_read_the_password():
    assert ice1.ice_ufrags(SDP_A) == ["AbCd"]
    assert ice1.ice_ufrags(SDP_A + SDP_A) == ["AbCd"]
    assert ice1.ice_ufrags(None) == [] and ice1.ice_ufrags("") == []
    assert "secret" not in repr(ice1.ice_ufrags(SDP_A))


def test_restart_seen_needs_a_new_ufrag_on_a_side():
    same = {"local": SDP_A, "remote": SDP_B}
    assert ice1.restart_seen(same, dict(same)) == {"local": False, "remote": False, "any": False}
    assert ice1.restart_seen(same, {"local": SDP_B, "remote": SDP_B}) == {"local": True, "remote": False, "any": True}
    assert ice1.restart_seen(same, {"local": SDP_A, "remote": SDP_A})["remote"] is True
    assert ice1.restart_seen(same, {"local": None, "remote": ""})["any"] is False
    assert ice1.restart_seen(None, same)["any"] is True


OLD, NEW = "172.30.0.92", "172.30.0.93"


def m(ip, port=41000):
    return {"browser_port": port, "seen_ip": ip, "seen_port": port}


def test_mapping_problems():
    assert ice1.mapping_problems([m(OLD)], [m(NEW, 41000)], OLD, NEW) == []
    assert ice1.mapping_problems([m(OLD), m(OLD, 41001)], [m(NEW)], OLD, NEW) == []
    assert "no UDP flow" in ice1.mapping_problems([], [m(NEW)], OLD, NEW)[0]
    assert "expected only 172.30.0.92" in ice1.mapping_problems([m(NEW)], [m(NEW)], OLD, NEW)[0]
    assert "no UDP flow" in ice1.mapping_problems([m(OLD)], [], OLD, NEW)[0]
    both = ice1.mapping_problems([m(OLD)], [m(OLD), m(NEW, 41002)], OLD, NEW)
    assert len(both) == 1 and "after the change" in both[0]
    assert len(ice1.mapping_problems(None, None, OLD, NEW)) == 2


def test_stray_problems():
    assert ice1.stray_problems(10, 10, 3.0) == []
    assert ice1.stray_problems(10, 12, 3.0, maximum=2) == []
    out = ice1.stray_problems(10, 160, 3.0)
    assert len(out) == 1 and "150 datagrams" in out[0] and "old candidate pair" in out[0]
    assert ice1.STRAY_OLD_PAIR_MAX == 0


def test_local_candidates():
    entries = [{"type": "local-candidate", "candidateType": "prflx", "address": "172.30.0.93", "port": 41000, "protocol": "udp"},
               {"type": "local-candidate", "candidateType": "host", "ip": "172.31.0.91", "port": 41000, "protocol": "udp"},
               {"type": "local-candidate", "candidateType": "srflx", "protocol": "udp"},
               {"type": "remote-candidate", "candidateType": "host", "address": "172.30.0.11", "port": 50000},
               {"type": "candidate-pair"}]
    got = ice1.local_candidates(entries)
    assert [c["type"] for c in got] == ["host", "prflx", "srflx"]
    assert got[1]["addr"] == "172.30.0.93:41000" and got[2]["addr"] == "?:?" and got[0]["addr"] == "172.31.0.91:41000"
    assert ice1.local_candidates(None) == []


ROWS = [{"protocol": "webrtc", "circuit": "c1", "callsign": "N0BRU", "since_ms": 5, "address": "172.30.0.92:41000", "edge": "webrtc"},
        {"protocol": "webrtc", "circuit": "c2", "callsign": "N0XYZ", "since_ms": 9, "remote_addr": "x"},
        {"protocol": "usrp", "circuit": "u1"}]


def test_webrtc_rows_and_address_like():
    rows = ice1.webrtc_rows(ROWS + [None, "x"])
    assert [r["circuit"] for r in rows] == ["c1", "c2"]
    assert ice1.address_like(rows[0]) == {"address": "172.30.0.92:41000"}
    assert ice1.address_like(rows[1]) == {"remote_addr": "x"}
    assert ice1.address_like({"description": "x", "ipv": "y", "ok": True, "ip": True, "peer_port": 7}) == {"peer_port": "7"}
    assert ice1.address_like(None) == {}


def test_match_row_prefers_the_circuit_then_the_callsign_then_the_newest():
    rows = ice1.webrtc_rows(ROWS)
    assert ice1.match_row(rows, "c2", "N0BRU") == (rows[1], "circuit")
    assert ice1.match_row(rows, "nope", "n0bru") == (rows[0], "callsign")
    assert ice1.match_row(rows, "nope", "N0NONE") == (rows[1], "newest")
    assert ice1.match_row([], "c1", "N0BRU") == (None, None)
    two = [dict(rows[0]), dict(rows[0], circuit="c3")]
    assert ice1.match_row(two, "nope", "N0BRU")[1] == "newest"


def test_new_circuits():
    before, after = ice1.webrtc_rows(ROWS[:1]), ice1.webrtc_rows(ROWS)
    assert ice1.new_circuits(before, after) == ["c2"]
    assert ice1.new_circuits(after, before) == []
    assert ice1.new_circuits([{"x": 1}], [{"x": 2}]) == []


def side(first_s=None, first_kind=None, by_kind=None, per_s=None, n=0, last_s=None, kinds=None):
    return {"n": n, "first_s": first_s, "first_kind": first_kind, "last_s": last_s, "kinds": kinds or {},
            "first_by_kind": by_kind or {}, "per_s": per_s or [0] * 20, "after_horizon": 0}


def test_firsts_evidence_reads_the_four_figures_dv_router_asked_for():
    stale = [50, 50, 12, 0, 0] + [0] * 15
    snap = {"t0": 1.0, "old": "o", "new": "n", "horizon_s": 20, "error": None, "sides": {
        "from_new": side(0.31, "stun-binding-request", {"stun-binding-request": 0.31, "rtp": 5.0}, n=9, kinds={"stun-binding-request": 3}),
        "to_new": side(13.9, "stun-binding-response", {"stun-binding-response": 13.9, "rtp": 14.0}, n=500),
        "to_old": side(0.0, "rtp", {"rtp": 0.0}, per_s=stale, n=112, last_s=2.4),
        "from_old": side()}}
    ev = ice1.firsts_evidence(snap)
    assert ev["first_udp_new_s"] == 0.31 and ev["first_stun_new_s"] == 0.31 and ev["first_sut_to_new_s"] == 13.9
    assert ev["first_udp_new_kind"] == "stun-binding-request" and ev["first_sut_to_new_kind"] == "stun-binding-response"
    assert ev["first_sut_stun_response_new_s"] == 13.9 and ev["first_sut_rtp_new_s"] == 14.0
    assert ev["stale_to_old_per_s"] == stale and ev["stale_to_old_total"] == 112 and ev["stale_to_old_last_s"] == 2.4
    assert ev["browser_from_old_total"] == 0 and ev["error"] is None


def test_firsts_evidence_with_nothing_seen_or_no_sniffer():
    empty = {"error": None, "sides": {k: side() for k in ("from_new", "to_new", "to_old", "from_old")}}
    ev = ice1.firsts_evidence(empty)
    assert ev["first_udp_new_s"] is None and ev["first_stun_new_s"] is None and ev["first_sut_to_new_s"] is None
    assert ev["stale_to_old_per_s"] == [0] * 20
    broken = ice1.firsts_evidence({"error": "PermissionError: no", "sides": empty["sides"]})
    assert broken["error"] == "PermissionError: no"
    none = ice1.firsts_evidence(None)
    assert none["error"] == "no snapshot" and none["stale_to_old_per_s"] is None
    assert ice1.firsts_evidence({"error": "x"})["first_udp_new_s"] is None            # a snapshot without sides
