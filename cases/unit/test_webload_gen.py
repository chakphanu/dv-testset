# SPDX-License-Identifier: MIT
"""dvtest/tools/webload_gen.py: the parts that need neither aiortc nor a network (the listener's RTP reader and
the procfs readers)."""
import struct

import pytest

from dvtest import webload as wl
from dvtest.tools import webload_gen as gen

pytestmark = pytest.mark.unit


def rtp(payload, seq=1, pt=0, ts=160, ssrc=0x11223344, cc=0, ext=None, pad=0, version=2):
    b0 = (version << 6) | (0x20 if pad else 0) | (0x10 if ext is not None else 0) | cc
    head = struct.pack(">BBHII", b0, pt, seq, ts, ssrc) + b"\0\0\0\0" * cc
    if ext is not None:
        head += struct.pack(">HH", 0xBEDE, len(ext) // 4) + ext
    tail = bytes(pad - 1) + bytes([pad]) if pad else b""
    return head + payload + tail


def listener():
    return gen.Listener({"j": 3, "room": "A", "login": 0}, wl.Plan(listeners=1))


def test_listener_reads_marker_packets():
    lst = listener()
    for i in (7, 8, 9):
        lst.on_rtp(1000 * i, rtp(wl.expected_pcmu(i), seq=i))
    assert list(lst.idx) == [7, 8, 9] and list(lst.t) == [7000, 8000, 9000] and list(lst.rtp_seq) == [7, 8, 9]
    assert lst.invalid == 0 and lst.packets == 3 and lst.tail_ok and lst.first_ns == 7000


def test_listener_reads_through_csrc_extension_and_padding():
    lst = listener()
    lst.on_rtp(1, rtp(wl.expected_pcmu(42), cc=2, ext=bytes(range(1, 9)), pad=4))
    assert list(lst.idx) == [42] and lst.invalid == 0


@pytest.mark.parametrize("pkt", [
    b"\x80\x00\x00",                                          # shorter than a header
    rtp(wl.expected_pcmu(1), version=1),                      # not RTP version 2
    rtp(wl.expected_pcmu(1), pt=8),                           # PCMA, not PCMU
    rtp(wl.expected_pcmu(1)[:-1]),                            # 159 bytes
    rtp(bytes(161)),                                          # 161 bytes
    rtp(bytes([127]) + wl.expected_pcmu(1)[1:]),              # a digit out of range
    rtp(wl.expected_pcmu(1), ext=b"\0" * 8)[:16],             # extension cut short
])
def test_listener_counts_invalid(pkt):
    lst = listener()
    lst.on_rtp(1, pkt)
    assert lst.invalid == 1 and len(lst.idx) == 0


def test_listener_flags_a_different_tail_but_keeps_the_marker():
    lst = listener()
    lst.on_rtp(1, rtp(wl.expected_pcmu(5)[:4] + b"\x55" * 156))
    assert list(lst.idx) == [5] and lst.tail_ok is False


def test_listener_is_bounded():
    lst = listener()
    for i in range(gen.MAX_PACKETS + 50):
        lst.on_rtp(i, rtp(wl.expected_pcmu(i % 3000)))
    assert len(lst.t) == gen.MAX_PACKETS and lst.packets == gen.MAX_PACKETS + 50


def test_proc_readers():
    ticks, rss = gen.read_proc_ticks()
    assert ticks is not None and ticks >= 0 and rss > 0
    assert gen.read_proc_ticks(99999999) == (None, None)
    cg = gen.read_cgroup()
    assert isinstance(cg, dict)


def test_server_refuses_a_second_run_and_too_few_accounts(monkeypatch):
    server = gen.Server(set())
    with pytest.raises(ValueError):
        server.start({"listeners": 200, "accounts": [["N0CALL", "x"]], "base_url": "https://x", "ca": "/x"})
    with pytest.raises(ValueError):
        server.start({"listeners": 201, "accounts": [["N0CALL", "x"]] * 9, "base_url": "https://x", "ca": "/x"})
    with pytest.raises(RuntimeError):
        server.finish()


def test_relay_sdp_keeps_only_the_sha256_fingerprint():
    fp = ":".join(["AB"] * 32)
    sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=fingerprint:sha-256 %s\r\na=fingerprint:sha-384 %s\r\na=fingerprint:sha-512 CD\r\na=setup:actpass\r\n" % (fp, fp)
    out = gen.relay_sdp(sdp)
    assert out.count("a=fingerprint:") == 1 and "a=fingerprint:sha-256 " + fp in out
    assert out.startswith("v=0\r\n") and out.endswith("a=setup:actpass\r\n") and "\n\n" not in out
