# SPDX-License-Identifier: MIT
"""dvtest.m17_packet: the byte-level expectations of the M17P (packet mode)
stage 2 cases."""
import pytest

from dvtest import m17_packet as mp
from dvtest.proto import m17

pytestmark = pytest.mark.unit

TOPO = mp.topology({"a": "M", "b": "M", "l": "M", "c": "N", "d": "N"}, {"dvt": "M", "dvu": "NO"})


def test_marker_is_seven_ascii_bytes_and_unique_per_index():
    nonce = mp.new_nonce()
    assert len(nonce) == 4
    assert mp.marker(nonce, 7) == (nonce + "007").encode()
    assert mp.marker(nonce, 1) != mp.marker(nonce, 2)


@pytest.mark.parametrize("size", [37, 38, 39, 300, 859, 860])
def test_sized_packet_has_exactly_that_size_and_valid_crcs(size):
    packet = mp.sized(size, meta=b"x" * 7)
    assert len(packet) == size
    assert packet[:4] == b"M17P"
    assert int.from_bytes(packet[32:34], "big") == m17.crc16(packet[4:32])
    assert int.from_bytes(packet[-2:], "big") == m17.crc16(packet[34:-2])
    assert packet[18:32] == b"x" * 7 + bytes(7)


def test_sized_rejects_a_size_without_specifier():
    with pytest.raises(ValueError):
        mp.sized(36)


def test_sized_is_deterministic_per_seed():
    assert mp.sized(100, seed="s") == mp.sized(100, seed="s")
    assert mp.sized(100, seed="s") != mp.sized(100, seed="t")


def test_with_dst_matches_the_builder_for_a_new_destination():
    sent = m17.m17p_packet(5, b"hi\0", dst="M17-DVT M")
    assert mp.with_dst(sent, None) == m17.m17p_packet(5, b"hi\0")
    assert mp.with_dst(sent, "@ALL") == m17.m17p_packet(5, b"hi\0", dst="ALL")
    assert mp.with_dst(sent, "N0TEST") == m17.m17p_packet(5, b"hi\0", dst="N0TEST")


def test_with_dst_recomputes_only_the_lsf_crc():
    # a wrong LSF CRC is repaired, a wrong payload CRC is left alone
    sent = m17.m17p_packet(5, b"hi\0", dst="M17-DVT M", crc=(0xDEAD, 0xBEEF))
    out = mp.with_dst(sent, None)
    assert out == m17.m17p_packet(5, b"hi\0", crc=(None, 0xBEEF))
    assert out[-2:] == b"\xbe\xef" and out[34:-2] == sent[34:-2]


def test_with_dst_keeps_everything_else():
    sent = m17.m17p_packet(0x05, b"x" * 50, dst="URF999  C", type_=0xF030, meta=b"meta", can=3)
    out = mp.with_dst(sent, None)
    assert out[:4] == sent[:4] and out[10:32] == sent[10:32] and out[34:] == sent[34:]
    assert out[4:10] == m17.BROADCAST and len(out) == len(sent)


def test_which_dst_names_the_candidate_or_differs():
    sent = m17.m17p_packet(5, b"hi\0", dst="M17-DVT M")
    cands = ("@ALL", "M17-DVT M")
    assert mp.which_dst(sent, mp.with_dst(sent, "@ALL"), cands) == "@ALL"
    assert mp.which_dst(sent, sent, cands) == "M17-DVT M"
    broken = bytearray(mp.with_dst(sent, "@ALL"))
    broken[32] ^= 0xFF                      # LSF CRC not recomputed
    assert mp.which_dst(sent, bytes(broken), cands) == "differs"


def test_expect_legs_client_origin_reaches_its_module_and_the_peer_with_trailer():
    body = m17.m17p_packet(5, b"hi\0")
    want = mp.expect_legs(TOPO, "a", body)
    assert want == {"a": [], "b": [body], "l": [body], "c": [], "d": [], "dvt": [body + b"M"], "dvu": []}


def test_expect_legs_other_module_goes_to_the_other_peer():
    body = m17.m17p_packet(5, b"hi\0")
    want = mp.expect_legs(TOPO, "d", body)
    assert want["c"] == [body] and want["dvu"] == [body + b"N"]
    assert want["a"] == want["b"] == want["l"] == want["dvt"] == want["d"] == []


def test_expect_legs_peer_origin_strips_the_trailer_and_is_one_hop():
    body = m17.m17p_packet(5, b"hi\0")
    want = mp.expect_legs(TOPO, "dvt", body, "M")
    assert want == {"a": [body], "b": [body], "l": [body], "c": [], "d": [], "dvt": [], "dvu": []}
    want = mp.expect_legs(TOPO, "dvu", body, "N")
    assert want["c"] == want["d"] == [body] and want["dvt"] == want["dvu"] == want["a"] == []


def test_expect_legs_a_peer_that_shares_the_module_too():
    topo = mp.topology({"a": "M", "b": "M"}, {"dvt": "M", "dvu": "MN"})
    body = m17.m17p_packet(5, b"hi\0")
    assert mp.expect_legs(topo, "a", body)["dvu"] == [body + b"M"]


def test_outcome_relayed_dropped_and_partial():
    body = m17.m17p_packet(5, b"hi\0", meta=b"abcd001")
    want = mp.expect_legs(TOPO, "a", body)
    got = {"b": [body], "l": [body], "dvt": [body + b"M"]}
    assert mp.outcome(got, want, b"abcd001") == "relayed"
    assert mp.outcome({}, want, b"abcd001") == "dropped"
    # other probes' datagrams are not looked at
    other = m17.m17p_packet(5, b"hi\0", meta=b"abcd002")
    assert mp.outcome(dict(got, c=[other]), want, b"abcd001") == "relayed"
    assert mp.outcome({"b": [body]}, want, b"abcd001").startswith("partial: l got [] B, want [40] B")


def test_outcome_flags_stray_duplicate_and_trailer_problems():
    body = m17.m17p_packet(5, b"hi\0", meta=b"abcd001")
    want = mp.expect_legs(TOPO, "a", body)
    good = {"b": [body], "l": [body], "dvt": [body + b"M"]}
    assert "c got [40] B, want [] B" in mp.outcome(dict(good, c=[body]), want, b"abcd001")
    assert "b got [40,40] B, want [40] B" in mp.outcome(dict(good, b=[body, body]), want, b"abcd001")
    assert "dvt got [40] B, want [41] B" in mp.outcome(dict(good, dvt=[body]), want, b"abcd001")


def test_outcome_reports_the_first_differing_byte_of_equal_sizes():
    body = m17.m17p_packet(5, b"hi\0", meta=b"abcd001")
    changed = bytearray(body)
    changed[32] ^= 1
    want = mp.expect_legs(TOPO, "a", body)
    got = {"b": [bytes(changed)], "l": [body], "dvt": [body + b"M"]}
    assert "b got [40] B, want [40] B (first difference at byte 32)" in mp.outcome(got, want, b"abcd001")


def test_outcome_restricted_to_client_or_peer_legs():
    body = m17.m17p_packet(5, b"hi\0", meta=b"abcd001")
    want = mp.expect_legs(TOPO, "a", body)
    only_clients = {"b": [body], "l": [body]}
    assert mp.outcome(only_clients, want, b"abcd001", legs=mp.CLIENT_LEGS) == "relayed"
    assert mp.outcome(only_clients, want, b"abcd001").startswith("partial")
    assert mp.outcome({"dvt": [body + b"M"]}, want, b"abcd001", legs=mp.PEER_LEGS) == "relayed"
