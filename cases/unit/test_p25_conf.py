# SPDX-License-Identifier: MIT
"""Builders and parsers the P25 conformance suite (cases/proto/
test_p25_network.py) relies on: call shapes, control fields, summaries."""
import socket

import pytest

from dvtest.proto import p25

IMBE = [bytes([i]) * 11 for i in range(1, 41)]


def test_record_control_fields_round_trip():
    mi = bytes(range(1, 10))
    fields = {}
    for index in range(18):
        rec = p25.parse(p25.record(p25.record_type(index), IMBE[index], src=1023007, dst=31672,
                                   lcf=p25.LCF_PROTECTED, mfid=0x90, algid=0x81, kid=0x1234, mi=mi))
        fields[rec["type"]] = rec
    assert fields[0x64]["lcf"] == 0x80 and fields[0x64]["mfid"] == 0x90 and fields[0x64]["svc"] == 0
    assert fields[0x65]["dst"] == 31672 and fields[0x66]["src"] == 1023007
    assert b"".join(fields[t]["mi"] for t in p25.MI_TYPES) == mi
    assert fields[0x70]["algid"] == 0x81 and fields[0x70]["kid"] == 0x1234
    assert all(fields[t]["parity"] == bytes(3) for t in p25.PARITY_TYPES)
    assert all(fields[t]["lsd"] == bytes(2) for t in p25.LSD_TYPES)
    clear = p25.parse(p25.record(0x70, IMBE[0]))
    assert clear["algid"] == p25.ALGID_CLEAR and clear["kid"] == 0


def test_call_records_shapes():
    full = p25.call_records(IMBE[:20], 1023007, 31672)
    assert len(full) == 28 and full[-1] == p25.terminator()            # padded to 27 + 0x80
    bare = p25.call_records(IMBE[:20], 1023007, 31672, padded=False, end=False)
    assert len(bare) == 20 and p25.parse(bare[-1])["type"] == 0x63            # wraps after 0x73
    late = p25.call_records(IMBE[:9], 1023007, 31672, start=2, padded=False)
    assert [p25.parse(r)["type"] for r in late[:2]] == [0x64, 0x65]
    assert p25.lifecycle([p25.parse(r)["type"] for r in late])[0].startswith("call opens with 0x64")
    assert p25.voice_records(IMBE[:9], 1, 2) == p25.call_records(IMBE[:9], 1, 2)


def test_resized_and_call_fields():
    rec = p25.record(0x63, IMBE[0])
    assert len(p25.resized(rec, 1)) == 15 and len(p25.resized(rec, -1)) == 13
    assert p25.parse(p25.resized(rec, 1)) is None and p25.parse(p25.resized(rec, -1)) is None
    parsed = [p25.parse(r) for r in p25.call_records(IMBE[:18], 1023008, 31672)]
    summary = p25.call_fields(parsed)
    assert summary["dst"] == {31672} and summary["src"] == {1023008}
    assert summary["lcf"] == {0} and summary["mfid"] == {0} and summary["algid"] == {0x80}
    assert summary["kid"] == {0} and summary["mi"] == {bytes(3)}
    assert sorted(summary["parity"]) == list(p25.PARITY_TYPES)


def test_client_send_paces_and_drains():
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.settimeout(1.0)
    client = p25.P25Client("127.0.0.1")
    try:
        client.dest = ("127.0.0.1", sink.getsockname()[1])
        client.send(p25.call_records(IMBE[:9], 1, 2), 0.002)
        got = [sink.recvfrom(64)[0] for _ in range(10)]
        assert got == p25.call_records(IMBE[:9], 1, 2)
    finally:
        client.close()
        sink.close()


@pytest.mark.parametrize("rtype", [0x62, 0x6A, 0x73])
def test_record_rejects_bad_imbe(rtype):
    with pytest.raises(ValueError):
        p25.record(rtype, bytes(10))
