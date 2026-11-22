# SPDX-License-Identifier: MIT
"""dvtest/stun_pair.py and dvtest/edg4.py. Pure, no rig."""
import pytest

from dvtest import edg4, metrics, stun_pair as sp

pytestmark = pytest.mark.unit

PWD = "abcdefghijklmnopqrstuvwxyz012345"


def test_request_is_well_formed_and_signed():
    tid = bytes(range(12))
    msg = sp.request(tid, "edge:cli", PWD)
    parsed = sp.parse(msg)
    assert parsed is not None and parsed.type == sp.BINDING_REQUEST and parsed.tid == tid
    assert parsed.attrs[sp.USERNAME] == b"edge:cli"
    assert sp.USE_CANDIDATE not in parsed.attrs and sp.ICE_CONTROLLING in parsed.attrs
    assert sp.integrity_ok(msg, PWD) and sp.fingerprint_ok(msg)
    assert not sp.integrity_ok(msg, PWD + "x")
    assert len(msg) % 4 == 0


def test_use_candidate_and_priority_attributes():
    msg = sp.request(sp.new_tid(), "a:b", PWD, priority=7, use_candidate=True, controlling=False)
    parsed = sp.parse(msg)
    assert sp.USE_CANDIDATE in parsed.attrs and sp.ICE_CONTROLLING not in parsed.attrs
    assert int.from_bytes(parsed.attrs[sp.PRIORITY], "big") == 7
    assert sp.integrity_ok(msg, PWD) and sp.fingerprint_ok(msg)


def test_a_flipped_bit_breaks_the_checks():
    msg = bytearray(sp.request(sp.new_tid(), "edge:cli", PWD))
    msg[25] ^= 0x01
    assert not sp.integrity_ok(bytes(msg), PWD) or not sp.fingerprint_ok(bytes(msg))


def test_success_detection():
    tid = sp.new_tid()
    resp = sp.success_response(tid, PWD)
    assert sp.is_success(resp, tid) and not sp.is_success(resp, sp.new_tid())
    assert sp.integrity_ok(resp, PWD) and sp.fingerprint_ok(resp)
    assert not sp.is_success(sp.request(tid, "a:b", PWD), tid)
    assert sp.parse(b"\x80" * 40) is None and sp.parse(b"") is None


def test_tid_length_is_checked():
    with pytest.raises(ValueError):
        sp.request(b"short", "a:b", PWD)


def test_datagram_shapes():
    alert = sp.dtls_fatal_alert()
    assert len(alert) == 15 and alert[0] == 21 and alert[1:3] == b"\xfe\xfd" and alert[-2:] == bytes([2, 40])
    junk = sp.junk_rtp()
    assert junk[0] == 0x80 and sp.parse(junk) is None


SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=ice-ufrag:EdGe\r\na=ice-pwd:" + PWD + "\r\na=candidate:1 1 udp 2130706431 172.30.0.11 50000 typ host\r\na=candidate:2 1 udp 1 172.30.0.12 50001 typ host\r\n"


def test_sdp_facts():
    assert edg4.ice_credentials(SDP) == ("EdGe", PWD)
    assert edg4.ice_credentials("") == (None, None)
    assert edg4.host_candidates(SDP) == [("172.30.0.11", 50000), ("172.30.0.12", 50001)]
    assert edg4.stun_username("EdGe", "cli") == "EdGe:cli"


def sample(name, value, **labels):
    return metrics.Sample(name, labels, value)


def test_counters_and_moved():
    samples = [sample(edg4.REFUSED, 2, reason="unknown_label", proc="webrtc"), sample(edg4.REFUSED, 1, reason="channel_limit"), sample(edg4.CLOSED, 1, reason="unknown_label"),
               sample(edg4.LIMIT_CLOSES, 1), sample(edg4.EXPIRED, 3), sample(edg4.UNROUTED, 4, reason="wrong_peer"), sample(edg4.LISTENERS, 2)]
    c = edg4.counters(samples)
    assert c.refused["unknown_label"] == 2 and c.closed["channel_limit"] == 0 and c.limit_closes == 1 and c.expired == 3 and c.unrouted["wrong_peer"] == 4 and c.listeners == 2
    zero = edg4.counters([])
    change = edg4.Counters(*[edg4.diff_map(a, b) if isinstance(a, dict) else a - b for a, b in zip(c, zero)])
    assert edg4.moved(change)["limit_closes"] == 1 and edg4.moved(change)["refused"] == {"unknown_label": 2, "channel_limit": 1}
    assert edg4.families_present(samples) and not edg4.families_present([])


def test_expiry_judge():
    assert edg4.judge_expiry(0, 1, 50.0) == []
    assert edg4.judge_expiry(0, 0, 50.0)                 # never counted
    assert edg4.judge_expiry(0, 2, 50.0)                 # counted twice
    assert edg4.judge_expiry(0, 1, 10.0)                 # early
    assert edg4.judge_expiry(0, 0, 36.0) == []           # inside the publish margin: not judged
    assert edg4.expiry_window(100.0, 131.0) == (31.0, True)
    assert edg4.strict_on({"DVT_EDG4": "on"}) and not edg4.strict_on({})
