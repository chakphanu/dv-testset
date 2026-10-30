# SPDX-License-Identifier: MIT
"""XLX interlink builders and the XlxPeer link logic (loopback)."""
import socket
import threading
import time

import pytest

from dvtest.proto import dextra, xlx


def test_link_packets_round_trip():
    for build, tag in ((xlx.connect, "L"), (xlx.ack, "A")):
        data = build("XLX996", "DE", (2, 6, 1))
        assert len(data) == 39 and data[38] == 0
        assert xlx.parse_link(data) == {"tag": tag, "callsign": "XLX996", "version": (2, 6, 1), "modules": "DE"}
    for build, tag in ((xlx.nack, "N"), (xlx.disconnect, "U")):
        data = build("XLX999")
        assert len(data) == 10
        assert xlx.parse_short(data) == {"tag": tag, "callsign": "XLX999"}
    assert xlx.parse_keepalive(xlx.keepalive("XLX999")) == "XLX999"
    assert xlx.parse_link(xlx.connect("XLX996", "A" * 26)) is not None
    with pytest.raises(ValueError):
        xlx.connect("XLX996", "A" * 27)


def test_parsers_reject_other_forms():
    assert xlx.parse_link(xlx.connect("XLX996", "DE")[:38]) is None
    assert xlx.parse_link(b"L" + bytes(37) + b"x") is None
    assert xlx.parse_short(xlx.nack("XLX999")[:9]) is None
    assert xlx.parse_keepalive(b"DSVT" + bytes(5)) is None
    assert xlx.parse_voice(bytes(27)) is None


@pytest.mark.parametrize("version,revision", [((1, 3, 9), 0), ((1, 4, 0), 1), ((2, 1, 9), 1),
                                              ((2, 2, 0), 2), ((2, 5, 3), 2), ((2, 6, 0), 3), ((3, 0, 0), 3)])
def test_revision_of(version, revision):
    assert xlx.revision_of(version) == revision


@pytest.mark.parametrize("revision", [1, 2, 3])
def test_voice_round_trip(revision):
    ambe, ambe2, c2 = bytes(range(9)), bytes(range(10, 19)), bytes(range(20, 28))
    data = xlx.voice(0x1234, 25, ambe, revision=revision, ambe2=ambe2, codec2=c2)
    assert len(data) == xlx.FRAME_SIZE[revision]
    v = xlx.parse_voice(data)
    assert (v["stream_id"], v["seq"], v["last"], v["ambe"], v["revision"]) == (0x1234, 4, False, ambe, revision)
    if revision >= 2:
        assert (v["dmr_pid"], v["dmr_subid"], v["ambe2"]) == (2, 2, ambe2)
    if revision == 3:
        assert v["codec2"] == c2
    if revision == 1:
        assert data == dextra.voice(0x1234, 25, ambe, False)
    last = xlx.parse_voice(xlx.voice(1, 3, xlx.END_AMBE, last=True, revision=revision))
    assert last["last"] and last["seq"] == 3


def test_stream_of_groups_by_sid():
    packets = [xlx.header(7, "N0CALL", "D")] * 2 + [xlx.voice(7, i, bytes(9)) for i in range(3)] + \
        [xlx.voice(8, 0, bytes(9), revision=1)]
    streams = xlx.stream_of(packets)
    assert streams[7]["headers"] == 2 and len(streams[7]["frames"]) == 3
    assert streams[8]["headers"] == 0 and streams[8]["frames"][0]["size"] == 27
    assert streams[7]["header"]["rpt2"] == b"XLX996 D"


def test_peer_links_and_keeps_alive():
    """A fake reflector on 127.0.0.2: acks L, sends its own L; the peer
    answers with A and then sends keepalives."""
    fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake.bind(("127.0.0.2", 0))
    fake.settimeout(0.2)
    port = fake.getsockname()[1]
    peer = xlx.XlxPeer(bind_ip="127.0.0.1", port=0, peer_port=port)
    seen = []
    try:
        result = {}

        def reflector():
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                try:
                    data, addr = fake.recvfrom(100)
                except socket.timeout:
                    continue
                seen.append(data)
                link = xlx.parse_link(data)
                if link and link["tag"] == "L":
                    fake.sendto(xlx.ack("XLX999", "D"), addr)
                    fake.sendto(xlx.connect("XLX999", "D"), addr)
        t = threading.Thread(target=reflector)
        t.start()
        result["ack"] = peer.link("127.0.0.2", seconds=2)
        time.sleep(2.5)
        peer.close()
        t.join()
    finally:
        peer.close()
        fake.close()
    assert result["ack"]["modules"] == "D"
    kinds = [xlx.parse_link(d)["tag"] if xlx.parse_link(d) else xlx.parse_keepalive(d) or xlx.parse_short(d)["tag"]
             for d in seen]
    assert kinds[:2] == ["L", "A"], kinds
    assert kinds.count("XLX996") >= 2, kinds
    assert kinds[-1] == "U", kinds
