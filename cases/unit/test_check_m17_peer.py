# SPDX-License-Identifier: MIT
"""dvtest.check.m17 on reflector peer traffic (stage 2): the tester plays
reflector peers at <net>.30:17011 / :17012 next to the system under test.
Peer datagrams follow the reflector to reflector rules, the same sizes from
a client stay errors, marked malformed traffic is not an error."""
import pytest

from dvtest import check
from dvtest.check import m17 as chk
from dvtest.proto import m17
from test_check_common import Wire, codes, frames_of

SUT, TESTER = "172.30.0.11", "172.30.0.30"
DVT, DVU = 17011, 17012
CLIENT = 40003
PEER_ENV = ("DVT_M17_PEER_DVT_HOST", "DVT_M17_PEER_DVU_HOST", "DVT_M17_PEER_DVT_PORT", "DVT_M17_PEER_DVU_PORT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in PEER_ENV:
        monkeypatch.delenv(name, raising=False)


def handshake(w, port=DVT, modules="M", name="M17-DVT"):
    """The SUT's CONN, the peer's ACKN and its own CONN (symmetric link)."""
    w.send(SUT, m17.PORT, TESTER, port, m17.peer_conn_packet("M17-SUT", "MN"), 0.01)
    w.send(TESTER, port, SUT, m17.PORT, m17.peer_ackn_packet(name, modules), 0.01)
    w.send(TESTER, port, SUT, m17.PORT, m17.peer_conn_packet(name, modules), 0.01)
    w.send(SUT, m17.PORT, TESTER, port, m17.peer_ackn_packet("M17-SUT", "MN"), 0.01)


def stream(w, src, sport, dst, dport, sid, trailer=b"M", n=6, eot=True, crc=True):
    """n stream packets 40 ms apart, trailer appended (None: bare 54 B)."""
    for fn in range(n):
        last = eot and fn == n - 1
        body = m17.stream_packet(sid, fn, bytes([fn + 1]) * 16, src="N0TEST", last=last, crc=crc)
        w.send(src, sport, dst, dport, body + (trailer if trailer is not None else b""), 0.040)


def m17p(body_size):
    """An M17P of body_size B (38..859) with valid CRCs."""
    return m17.m17p_packet(0x05, b"x" * (body_size - 37), src="N0TEST")


def run(w, tmp_path, windows=()):
    """check_file on <tmp>/xlxd-sut.pcapng with the rig addresses."""
    rig = check.rig_config("172.30.0.0/24")
    return check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"), rig, windows=windows)


def good_peer_wire():
    w = Wire()
    handshake(w)
    handshake(w, DVU, "MN", "M17-DVU")
    w.send(SUT, m17.PORT, TESTER, DVT, m17.peer_ping_packet("M17-SUT"), 1.0)
    w.send(TESTER, DVT, SUT, m17.PORT, m17.peer_pong_packet("M17-DVT"), 0.01)
    w.send(TESTER, DVT, SUT, m17.PORT, m17.peer_ping_packet("M17-DVT"), 0.1)
    w.send(SUT, m17.PORT, TESTER, DVT, m17.peer_pong_packet("M17-SUT"), 0.01)
    stream(w, TESTER, DVT, SUT, m17.PORT, 0x1001)                  # peer -> sut, 55 B
    stream(w, SUT, m17.PORT, TESTER, DVU, 0x1002, b"N")            # sut -> peer, 55 B
    for size in (38, 300, 859):
        w.send(TESTER, DVU, SUT, m17.PORT, m17p(size) + b"N", 0.5)     # 39 .. 860 B in
        w.send(SUT, m17.PORT, TESTER, DVT, m17p(size) + b"M", 0.5)     # and out
    w.send(SUT, m17.PORT, TESTER, DVT, b"NACK", 0.5)                      # NACK 4 B
    w.send(TESTER, DVU, SUT, m17.PORT, m17.peer_disc_packet("M17-DVU"), 0.5)
    w.send(SUT, m17.PORT, TESTER, DVU, b"DISC", 0.01)
    return w


def test_peer_endpoints_from_settings(monkeypatch):
    hosts = {"tester": TESTER}
    assert chk.peer_endpoints(hosts) == {(TESTER, DVT), (TESTER, DVU), ("172.30.0.32", DVU)}
    assert chk.peer_endpoints(None) == set()
    monkeypatch.setenv("DVT_M17_PEER_DVU_HOST", "172.30.0.99")
    monkeypatch.setenv("DVT_M17_PEER_DVT_PORT", "17111")
    assert (TESTER, 17111) in chk.peer_endpoints(hosts) and ("172.30.0.99", DVU) in chk.peer_endpoints(hosts)
    assert (TESTER, DVT) not in chk.peer_endpoints(hosts)


def test_peer_datagrams_of_each_kind_pass(tmp_path):
    result = run(good_peer_wire(), tmp_path)
    assert codes(result) == []
    kinds = result["protocols"]["m17"]["kinds"]
    assert kinds["peer_voice"] == 12 and kinds["peer_packet"] == 6 and kinds["peer_conn"] == 4
    assert kinds["peer_nack"] == 1 and kinds["peer_ping"] == 2 and kinds["peer_pong"] == 2
    assert kinds["peer_disc"] == 1 and kinds["peer_disc_ack"] == 1 and "voice" not in kinds
    assert [s["frames"] for s in result["protocols"]["m17"]["streams"]] == [6, 6]


def test_peer_on_the_second_tester_address_is_known(tmp_path, monkeypatch):
    # mrefd run: M17-DVU at <net>.32 (not a rig host, still no unknown_peer)
    w = Wire()
    w.send(SUT, m17.PORT, "172.30.0.32", DVU, m17.peer_conn_packet("M17-SUT", "MN"))
    w.send("172.30.0.32", DVU, SUT, m17.PORT, m17.peer_ackn_packet("M17-DVU", "MN"), 0.01)
    stream(w, "172.30.0.32", DVU, SUT, m17.PORT, 0x1003, b"N")
    assert codes(run(w, tmp_path)) == []


def test_same_sizes_from_a_client_still_fail(tmp_path):
    w = Wire()
    w.send(TESTER, CLIENT, SUT, m17.PORT, m17.conn_packet("N0TEST", "M"))
    w.send(SUT, m17.PORT, TESTER, CLIENT, b"ACKN", 0.01)
    stream(w, TESTER, CLIENT, SUT, m17.PORT, 0x2001, b"M", n=2, eot=False)     # 55 B stream
    w.send(TESTER, CLIENT, SUT, m17.PORT, m17p(859) + b"M", 0.1)               # 860 B M17P
    w.send(SUT, m17.PORT, TESTER, CLIENT, m17p(859) + b"M", 0.1)               # also towards a client
    result = run(w, tmp_path)
    assert frames_of(result, "m17.bad_length") == [3, 4]
    assert frames_of(result, "m17.m17p_length") == [5, 6]
    assert codes(result, "error") == ["m17.bad_length", "m17.m17p_length"]


def test_client_limits_unchanged_without_settings(tmp_path):
    # no rig, no settings, no 37 B handshake: nothing marks a peer
    w = Wire()
    w.send(TESTER, DVT, SUT, m17.PORT, m17.stream_packet(1, 0, bytes(16)) + b"M")
    w.send(TESTER, DVT, SUT, m17.PORT, m17p(859) + b"M", 0.1)
    result = w.check(tmp_path)
    assert codes(result, "error") == ["m17.bad_length", "m17.m17p_length"]


def test_peer_learned_from_a_37_byte_handshake(tmp_path):
    w = Wire()
    port = 50123                                  # unknown endpoint: told by its 37 B CONN / ACKN
    handshake(w, port)
    stream(w, TESTER, port, SUT, m17.PORT, 0x3001)
    w.send(TESTER, port, SUT, m17.PORT, m17p(300) + b"M", 0.5)
    w.send(SUT, m17.PORT, TESTER, port, m17p(859) + b"M", 0.5)
    result = w.check(tmp_path)
    assert codes(result, "error") == []


@pytest.mark.parametrize("fault, code, frame_of", [
    ("lower_trailer", "m17.peer_trailer", 6),         # 'm'
    ("nul_trailer", "m17.peer_trailer", 6),
    ("m17p_nul_trailer", "m17.peer_trailer", 6),
    ("bare_to_peer", "m17.peer_trailer", 6),
    ("long_stream", "m17.bad_length", 6),             # 56 B
    ("m17p_short", "m17.m17p_length", 6),             # 38 B: no trailer room
    ("m17p_long", "m17.m17p_length", 6),              # 861 B
    ("ackn4", "m17.bad_length", 6),
    ("lstn", "m17.bad_length", 6),
    ("list_lower", "m17.peer_layout", 6),
    ("list_after_nul", "m17.peer_layout", 6),
    ("designation", "m17.peer_layout", 6),
    ("m17p_type", "m17.m17p_type", 6),
])
def test_peer_faults_are_errors(tmp_path, fault, code, frame_of):
    w = Wire()
    handshake(w)                                         # frames 1..4
    w.send(TESTER, DVT, SUT, m17.PORT, m17.peer_ping_packet("M17-DVT"), 0.1)    # frame 5
    bad = {
        "lower_trailer": (TESTER, DVT, SUT, m17.PORT, m17.stream_packet(7, 0, bytes(16)) + b"m"),
        "nul_trailer": (TESTER, DVT, SUT, m17.PORT, m17.stream_packet(7, 0, bytes(16)) + b"\0"),
        "m17p_nul_trailer": (TESTER, DVT, SUT, m17.PORT, m17p(300) + b"\0"),
        "bare_to_peer": (SUT, m17.PORT, TESTER, DVT, m17.stream_packet(7, 0, bytes(16))),
        "long_stream": (TESTER, DVT, SUT, m17.PORT, m17.stream_packet(7, 0, bytes(16)) + b"MM"),
        "m17p_short": (TESTER, DVT, SUT, m17.PORT, m17p(38)),
        "m17p_long": (TESTER, DVT, SUT, m17.PORT, m17p(859) + b"MM"),
        "ackn4": (TESTER, DVT, SUT, m17.PORT, b"ACKN"),
        "lstn": (TESTER, DVT, SUT, m17.PORT, m17.lstn_packet("N0TEST", "M")),
        "list_lower": (TESTER, DVT, SUT, m17.PORT, b"ACKN" + m17.encode_callsign("M17-DVT") + b"Mm".ljust(27, b"\0")),
        "list_after_nul": (TESTER, DVT, SUT, m17.PORT, b"CONN" + m17.encode_callsign("M17-DVT")
                           + b"M\0N".ljust(27, b"\0")),
        "designation": (TESTER, DVT, SUT, m17.PORT, b"CONN" + b"\xff" * 6 + b"M".ljust(27, b"\0")),
        "m17p_type": (TESTER, DVT, SUT, m17.PORT, m17.m17p_packet(0x05, b"hi", type_=0xF001) + b"M"),
    }[fault]
    w.send(*bad, 0.1)                                    # frame 6
    result = run(w, tmp_path)
    assert frames_of(result, code) == [frame_of]
    assert code in codes(result, "error")


def test_peer_crc_and_bare_stream_from_a_peer_are_warnings(tmp_path):
    w = Wire()
    handshake(w)
    stream(w, TESTER, DVT, SUT, m17.PORT, 0x4001, None, n=3)             # bare 54 B from the peer
    stream(w, TESTER, DVT, SUT, m17.PORT, 0x4002, b"M", n=3, crc=False)
    result = run(w, tmp_path)
    assert codes(result, "error") == []
    assert {"m17.peer_trailer", "m17.peer_crc"} <= set(codes(result, "warn"))


def test_marked_malformed_peer_traffic_is_not_an_error(tmp_path):
    w = Wire()
    handshake(w)
    t0 = w.t
    w.send(TESTER, DVT, SUT, m17.PORT, m17.stream_packet(7, 0, bytes(16)) + b"m", 0.1)
    w.send(TESTER, DVT, SUT, m17.PORT, b"ACKN", 0.1)
    w.send(TESTER, DVT, SUT, m17.PORT, m17p(859) + b"MM", 0.1)                       # 861 B
    w.send(TESTER, CLIENT, SUT, m17.PORT, m17p(859) + b"M", 0.1)                     # 860 B client packet
    w.send(TESTER, CLIENT, SUT, m17.PORT, m17.m17p_packet(0x05, b"hi", type_=0xF005), 0.1)   # stream bit
    # the sut relays the odd packet unchanged to a client and to a peer
    w.send(SUT, m17.PORT, TESTER, CLIENT, m17.m17p_packet(0x05, b"hi", type_=0xF005), 0.1)
    w.send(SUT, m17.PORT, TESTER, 41000, m17p(859) + b"M", 0.1)
    # a stream that stops without EOT (timeout) from the tester
    stream(w, TESTER, 41001, SUT, m17.PORT, 0x5001, None, n=4, eot=False)
    w.wait(7.0)
    w.send(TESTER, 41001, SUT, m17.PORT, m17.pong_packet("N0TEST"), 0.1)
    window = [(TESTER, t0, w.t, "mark m17-test")]
    plain = run(w, tmp_path)
    assert {"m17.peer_trailer", "m17.bad_length", "m17.m17p_length", "m17.m17p_type", "m17.no_eot"} \
        <= set(codes(plain, "error"))
    marked = run(w, tmp_path, window)
    assert codes(marked, "error") == []
    notes = [f["message"] for f in marked["findings"] if f["code"] == "m17.m17p_type"]
    assert any("forwarded during mark m17-test" in m for m in notes)
    assert any("[during mark m17-test]" in f["message"] for f in marked["findings"] if f["code"] == "m17.no_eot")


def test_sut_sending_a_bad_trailer_stays_an_error_in_a_window(tmp_path):
    w = Wire()
    handshake(w)
    t0 = w.t
    w.send(SUT, m17.PORT, TESTER, DVT, m17.stream_packet(7, 0, bytes(16)) + b"m", 0.1)
    result = run(w, tmp_path, [(TESTER, t0, w.t + 1, "mark m17-test")])
    assert codes(result, "error") == ["m17.peer_trailer"]
