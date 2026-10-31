# SPDX-License-Identifier: MIT
"""dvtest.m17_relay: the byte-level comparisons of the stage 2 relay cases."""
import pytest

from dvtest import m17_relay as relay
from dvtest.proto import m17

pytestmark = pytest.mark.unit


def stream(sid=0x1234, n=4, dst=None, last=True):
    return [m17.stream_packet(sid, i, bytes([i]) * 16, src="N0CALL", dst=dst, last=last and i == n - 1)
            for i in range(n)]


def test_egress_exact_client_and_peer():
    sent = stream()
    assert relay.egress_problems(sent, sent) == []
    assert relay.egress_problems(sent, [m17.with_trailer(p, "M") for p in sent], "M") == []
    assert relay.egress_problems(sent, [m17.with_trailer(p, "M") for p in sent], "M", trailer=False) == []


def test_egress_detects_size_trailer_and_content():
    sent = stream()
    bare = relay.egress_problems(sent, sent, "M")
    assert len(bare) == 4 and "54 B, want 55 B" in bare[0]
    wrong = relay.egress_problems(sent, [m17.with_trailer(p, "N") for p in sent], "M")
    assert wrong[0] == "packet 0: trailer N, want M"
    zero = relay.egress_problems(sent, [m17.with_trailer(p, 0) for p in sent], "M")
    assert zero[0] == "packet 0: trailer 0x00, want M"
    assert relay.egress_problems(sent, [p + b"x" for p in sent])[0].startswith("packet 0: 55 B, want 54 B")
    got = list(sent)
    got[2] = got[2][:36] + b"\xee" + got[2][37:]
    assert relay.egress_problems(sent, got) == ["packet 2: differs in payload"]
    assert relay.egress_problems(sent, sent[:3]) == ["3 datagrams, want 4"]
    assert relay.egress_problems(sent, sent + sent[:1])[0] == "5 datagrams, want 4"


def test_egress_bad_crc_in_unchanged_body():
    bad = [m17.stream_packet(7, i, b"\0" * 16, crc=0xDEAD) for i in range(2)]
    assert relay.egress_problems(bad, bad) == ["packet 0: bad CRC", "packet 1: bad CRC"]


def test_with_dst_rewrites_and_recomputes_crc():
    packet = m17.stream_packet(9, 3, b"x" * 16, src="N0CALL", dst="M17-DVT M")
    out = relay.with_dst(packet, None)
    assert out[6:12] == m17.BROADCAST and relay.crc_ok(out) and not relay.crc_ok(packet[:6] + m17.BROADCAST + packet[12:])
    assert relay.diff_fields(packet, out) == ["dst", "crc"]
    assert relay.with_dst(out, None) == out


def test_group_by_stream_ignores_other_datagrams():
    a, b = stream(1, 2), stream(2, 2)
    mixed = [a[0], b[0], m17.peer_ping_packet("M17-DVT"), a[1] + b"M", b[1], m17.m17p_packet(5, b"hi\0")]
    groups = relay.group_by_stream(mixed)
    assert sorted(groups) == [1, 2] and len(groups[1]) == 2 and len(groups[2]) == 2


def test_fn_problems():
    sent = stream(n=5)
    assert relay.fn_problems(sent) == []
    assert relay.fn_problems(sent[:2] + sent[3:])[0] == "packet 2: FN 3 after 1"
    assert "last-frame bit" in relay.fn_problems(sent[:3])[0]
    assert relay.fn_problems([]) == []


def test_closing_packet_checks():
    sent = stream(n=3, last=False)
    closing = m17.stream_packet(0x1234, 3, b"\0" * 16, src="N0CALL", last=True)
    assert relay.closing_problems(sent[-1], closing) == []
    assert relay.closing_problems(sent[-1], m17.with_trailer(closing, "M"), "M") == []
    assert relay.closing_problems(sent[-1], m17.with_trailer(closing, "M")) == ["55 B, want 54 B"]
    wrong = m17.stream_packet(0x1234, 5, b"\0" * 16, last=True)
    assert relay.closing_problems(sent[-1], wrong)[0].startswith("FN 5 last=True, want 3")
    assert "stream id" in relay.closing_problems(sent[-1], m17.stream_packet(0x4321, 3, b"\0" * 16, last=True))[0]
    open_end = m17.stream_packet(0x1234, 3, b"\0" * 16, last=False)
    assert "last=False" in relay.closing_problems(sent[-1], open_end)[0]
    assert relay.closing_problems(sent[-1], m17.stream_packet(0x1234, 3, b"\0" * 16, last=True, crc=0))[-1] == "bad CRC"
    wrapped = m17.stream_packet(0x1234, 0x7FFF, b"\0" * 16)
    assert relay.closing_problems(wrapped, m17.stream_packet(0x1234, 0, b"\0" * 16, last=True)) == []


def test_m17p_with_low_byte():
    letter = relay.m17p_with_low_byte(True)
    other = relay.m17p_with_low_byte(False)
    exact = relay.m17p_with_low_byte(0x4D)
    assert 0x41 <= letter[-1] <= 0x5A and not 0x41 <= other[-1] <= 0x5A and exact[-1] == 0x4D
    for packet in (letter, other, exact):
        parsed = m17.parse_m17p(packet, trailer=False)
        assert parsed["payload_crc_ok"] and parsed["lsf_crc_ok"] and len(packet) >= 39
    assert relay.m17p_with_low_byte(False, protocol=5)[:4] == b"M17P"
