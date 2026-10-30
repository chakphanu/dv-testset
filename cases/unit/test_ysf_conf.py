# SPDX-License-Identifier: MIT
"""Round trips of the YSF conformance builders (dvtest/proto/ysf.py)."""
import pytest

from dvtest.proto import ysf as Y

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("dt", [Y.DT_VD_MODE1, Y.DT_DATA_FR_MODE, Y.DT_VD_MODE2, Y.DT_VOICE_FR_MODE])
def test_dt_stream_packets(dt):
    packets = Y.dt_stream_packets("N0CALL", dt, 4, sq=Y.module_dgid("Y"))
    assert [len(p) for p in packets] == [Y.PACKET_SIZE] * 6
    infos = [Y.parse(p) for p in packets]
    assert [i["fich"]["fi"] for i in infos] == [Y.FI_HEADER] + [Y.FI_COMMUNICATIONS] * 4 + [Y.FI_TERMINATOR]
    assert {i["fich"]["dt"] for i in infos} == {dt}
    assert {i["fich"]["sq"] for i in infos} == {Y.module_dgid("Y")}
    assert [i["counter"] for i in infos] == list(range(6))
    assert [i["eot"] for i in infos] == [False] * 5 + [True]
    assert all(i["sync_ok"] for i in infos)


def test_raw_frame_payload_kept():
    payload = bytes(range(90))
    body = Y.raw_frame(Y.FI_COMMUNICATIONS, Y.DT_VOICE_FR_MODE, payload, fn=3, ft=6)
    assert len(body) == Y.FRAME_SIZE and body[30:] == payload
    assert Y.fich_decode(body[5:30])["fn"] == 3
    with pytest.raises(ValueError):
        Y.raw_frame(Y.FI_HEADER, Y.DT_VD_MODE2, bytes(89))


def test_with_status_and_corrupt_fich():
    data = Y.dt_stream_packets("N0CALL", Y.DT_VD_MODE2, 1)[1]
    assert Y.parse(Y.with_status(data, counter=127))["counter"] == 127
    flipped = Y.parse(Y.with_status(data, eot=True))
    assert flipped["eot"] and flipped["counter"] == 1
    assert Y.parse(Y.corrupt_fich(data))["fich"] is None


def test_options_and_info_packets():
    yo = Y.options_packet("N0CALL", "20;21;")
    assert len(yo) == Y.OPTIONS_SIZE and yo[:4] == b"YSFO" and yo[14:20] == b"20;21;"
    yi = Y.info_packet("N0CALL")
    assert len(yi) == Y.INFO_SIZE and yi[:4] == b"YSFI" and yi[4:14] == b"N0CALL    "


def test_status_layout():
    reply = b"YSFS" + b"01234" + b"XLX999".ljust(16) + b"XLX reflector".ljust(14) + b"007"
    assert Y.status_layout(reply) == {"size": 42, "id_digits": True, "count_digits": True}
    assert Y.parse_status(reply)["count"] == 7


def test_wiresx_conn_round_trip():
    packets = Y.wiresx_packets("N0CALL", Y.wiresx_conn(2))
    cmd, body = Y.parse_wiresx_command(Y.parse(packets[2])["data2"])
    assert cmd == Y.WIRESX_CONN_REQ[:3] and body[7:10] == b"002"


def test_wiresx_reply_kind():
    reply = Y.WIRESX_REPLIES["dx"] + b"N0CALL".ljust(16)
    body = Y.raw_frame(Y.FI_COMMUNICATIONS, Y.DT_DATA_FR_MODE,
                       Y.fr_payload(reply[:20].ljust(20, b" "), bytes(20)), fn=1, ft=1)
    info = Y.parse(Y.packet("XLX999", "XLX999", Y.ALL, 1, body))
    assert info["kind"] == "data"
    assert Y.wiresx_reply_kind([info]) == "dx"
    assert Y.wiresx_reply_kind([]) is None
