# SPDX-License-Identifier: MIT
import socket
import time

import pytest

from dvtest.proto import ambed


def test_ping_pong_and_caps():
    assert ambed.ping("XLX999") == b"AMBEDPINGXLX999  "
    assert ambed.parse_ping(ambed.ping("XLX999")) == b"XLX999  "
    assert ambed.parse_ping(ambed.PONG) is None
    reply = ambed.caps_reply(4, 0x38)
    assert len(reply) == 15 and reply.hex().endswith("01040000" + "0038")
    assert ambed.parse_caps(reply) == ambed.Caps(1, 4, 0x38)
    assert ambed.parse_caps(ambed.CAPS_REQUEST) is None
    assert ambed.codecs_in(0x38) == [3, 4, 5] and ambed.mask_of([3, 4, 5]) == 0x38


@pytest.mark.parametrize("outs,size", [((2,), 17), ((2, 3), 18), ((2, 3, 4), 19)])
def test_legacy_open_round_trip(outs, size):
    data = ambed.open_legacy("XLX999", 1, *outs)
    assert len(data) == size
    parsed = ambed.parse_open(data)
    assert parsed == ambed.Open(b"XLX999  ", 1, outs, None, size)


def test_legacy_open_bounds():
    with pytest.raises(ValueError):
        ambed.open_legacy("XLX999", 1)
    with pytest.raises(ValueError):
        ambed.open_legacy("XLX999", 1, 2, 3, 4, 5)


def test_extended_open_round_trip():
    data = ambed.open_ext("N0TEST", 3, [2, 5], 0xA1B2C3D4)
    assert len(data) == 24
    assert data[15:20] == bytes([3, 2, 5, 0, 0]) and data[20:] == bytes.fromhex("a1b2c3d4")
    assert ambed.parse_open(data) == ambed.Open(b"N0TEST  ", 3, (2, 5), 0xA1B2C3D4, 24)
    with pytest.raises(ValueError):
        ambed.open_ext("N0TEST", 3, [1, 2, 4, 5, 1], 1)
    assert ambed.parse_open(data[:20]) is None


def test_std_busy_close():
    legacy = ambed.std(1, 10101, 1, 2)
    assert len(legacy) == 14 and legacy[8:12] == bytes.fromhex("01007527")
    assert ambed.parse_std(legacy) == ambed.Std(1, 10101, 1, 2, None)
    ext = ambed.std(2, 10102, 3, 5, 0x11223344)
    assert len(ext) == 18 and ambed.parse_std(ext) == ambed.Std(2, 10102, 3, 5, 0x11223344)
    assert ambed.parse_std(ext[:16]) is None
    assert ambed.busy() == b"AMBEDBUSY" and ambed.busy_transaction(ambed.busy()) is None
    assert len(ambed.busy(7)) == 13 and ambed.busy_transaction(ambed.busy(7)) == 7
    assert ambed.is_busy(ambed.busy(7)) and not ambed.is_busy(ext)
    assert ambed.close(513) == b"AMBEDCS\x01\x02" and ambed.parse_close(ambed.close(513)) == 513


def test_data_request_and_reply():
    req = ambed.data_request(ambed.AMBEPLUS, 300, bytes(range(9)))
    assert req == b"\x01\x2c" + bytes(range(9))
    assert ambed.parse_data_request(req) == (1, 0x2C, bytes(range(9)))
    assert ambed.parse_data_request(req[:-1]) is None
    with pytest.raises(ValueError):
        ambed.data_request(ambed.PCM, 0, bytes(10))
    outs = [ambed.IMBE, ambed.CODEC2_3200, ambed.PCM]
    entries = [(c, ambed.fake_payload(c)) for c in outs]
    reply = ambed.data_reply(7, entries)
    assert len(reply) == ambed.reply_size(outs) == 1 + 12 + 9 + 321
    assert reply[:2] == b"\x04\x07" and reply[13] == 3
    assert ambed.parse_data_reply(reply, outs) == (7, entries)
    with pytest.raises(ValueError):
        ambed.parse_data_reply(reply, [ambed.CODEC2_3200, ambed.IMBE, ambed.PCM])
    with pytest.raises(ValueError):
        ambed.parse_data_reply(reply[:-1], outs)


def _client():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    return s


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_fake_ambed_extended():
    port = _free_port()
    fake = ambed.FakeAmbed("127.0.0.1", port=port, max_outputs=2, mask=0x2E,
                           data_port_base=_free_port())
    sock = _client()
    try:
        dest = ("127.0.0.1", port)
        sock.sendto(ambed.ping("XLX999"), dest)
        assert sock.recv(64) == ambed.PONG and fake.connected.is_set()
        sock.sendto(ambed.CAPS_REQUEST, dest)
        assert ambed.parse_caps(sock.recv(64)) == ambed.Caps(1, 2, 0x2E)
        sock.sendto(ambed.open_ext("XLX999", 5, [1, 2], 99), dest)
        std = ambed.parse_std(sock.recv(64))
        assert (std.codec_in, std.codec_out, std.transaction) == (5, 1, 99)
        sock.sendto(ambed.data_request(5, 3, bytes(320)), ("127.0.0.1", std.port))
        pid, entries = ambed.parse_data_reply(sock.recv(4096), [1, 2])
        assert pid == 3 and entries == [(1, b"\x11" * 9), (2, b"\x12" * 9)]
        sock.sendto(ambed.close(std.stream_id), dest)
        time.sleep(0.2)
        assert fake.caps_requests() == 1 and fake.closes[0][1] == std.stream_id
        assert len(fake.data_in[std.port]) == 1
    finally:
        sock.close()
        fake.stop()


def test_fake_ambed_legacy_mode():
    port = _free_port()
    fake = ambed.FakeAmbed("127.0.0.1", port=port, mode="legacy", data_port_base=_free_port())
    sock = _client()
    sock.settimeout(0.4)
    try:
        dest = ("127.0.0.1", port)
        for request in (ambed.CAPS_REQUEST, ambed.open_ext("XLX999", 5, [1], 1)):
            sock.sendto(request, dest)
            with pytest.raises(socket.timeout):
                sock.recv(64)
        sock.sendto(ambed.open_legacy("XLX999", 1, 2, 3, 4), dest)
        std = ambed.parse_std(sock.recv(64))
        assert std is not None and std.transaction is None and std.codec_out == 2
        assert fake.caps_requests() == 1
    finally:
        sock.close()
        fake.stop()


def test_fake_ambed_open_delay():
    port = _free_port()
    fake = ambed.FakeAmbed("127.0.0.1", port=port, open_delay=0.3, data_port_base=_free_port())
    sock = _client()
    try:
        start = time.monotonic()
        sock.sendto(ambed.open_ext("XLX999", 3, [5], 5), ("127.0.0.1", port))
        assert ambed.parse_std(sock.recv(64)).transaction == 5
        assert time.monotonic() - start >= 0.25
    finally:
        sock.close()
        fake.stop()
