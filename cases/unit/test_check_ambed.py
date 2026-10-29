# SPDX-License-Identifier: MIT
"""dvtest.check.ambed on synthetic captures built with dvtest.proto.ambed."""
import pytest

from dvtest.proto import ambed as A
from test_check_common import Wire, codes, frames_of

X, T = "192.0.2.11", "192.0.2.21"     # reflector (client), transcoder (server)
PORT = 10101


def ctl(w, data, reply, gap=0.0):
    w.send(X, A.PORT, T, A.PORT, data, gap)
    if reply is not None:
        w.send(T, A.PORT, X, A.PORT, reply, 0.001)


def stream(w, frames=10, codec_in=A.PCM, outs=(A.AMBEPLUS, A.CODEC2_3200), pids=None, skip_reply=()):
    for i, pid in enumerate(pids if pids is not None else range(frames)):
        w.send(X, PORT, T, PORT, A.data_request(codec_in, pid, b"\x00" * A.WIDTH[codec_in]), 0.020)
        if i not in skip_reply:
            w.send(T, PORT, X, PORT, A.data_reply(pid, [(c, A.fake_payload(c)) for c in outs]), 0.001)


def session(w, outs=(A.AMBEPLUS, A.CODEC2_3200), tx=0x1000, **kw):
    ctl(w, A.ping("XLX999"), A.PONG)
    ctl(w, A.CAPS_REQUEST, A.caps_reply(4, A.MASK_ALL))
    ctl(w, A.open_ext("XLX999", A.PCM, outs, tx), A.std(1, PORT, A.PCM, outs[0], tx), 0.1)
    stream(w, outs=outs, **kw)
    ctl(w, A.close(1), None, 0.1)
    ctl(w, A.open_legacy("XLX999", A.AMBEPLUS, A.AMBE2PLUS), A.std(2, PORT + 1, A.AMBEPLUS, A.AMBE2PLUS), 0.1)
    ctl(w, A.open_ext("XLX999", A.PCM, (A.PCM,), tx + 1), A.busy(tx + 1), 0.1)
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)


@pytest.mark.parametrize("fmt,end", [("pcapng", "<"), ("pcapng", ">"), ("pcap", "<")])
def test_clean_session(tmp_path, fmt, end):
    w = Wire()
    session(w)
    result = w.check(tmp_path, fmt, end)
    assert codes(result) == ["ambed.open_invalid"]          # the request side of the BUSY case
    p = result["protocols"]["ambed"]
    assert p["kinds"]["data_request"] == p["kinds"]["data_reply"] == 10
    assert p["streams"][0]["frames"] == 10 and p["streams"][0]["loss"] == 0


def test_tx_mismatch(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 7), A.std(1, PORT, A.PCM, A.AMBEPLUS, 8))
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.tx_mismatch") == [2] and frames_of(result, "ambed.no_reply") == [1]


def test_missing_and_bad_replies(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS, A.CODEC2_3200), 1), A.std(1, PORT, A.PCM, A.AMBEPLUS, 1))
    stream(w, frames=6, skip_reply=(2,))
    w.send(T, PORT, X, PORT, A.data_reply(9, [(A.AMBEPLUS, A.fake_payload(A.AMBEPLUS))]), 0.001)
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.reply_missing") == [7]      # request pid 2
    assert codes(result, "error") == ["ambed.reply_bad", "ambed.reply_missing"]


def test_pid_dup_and_data_after_close(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 1), A.std(1, PORT, A.PCM, A.AMBEPLUS, 1))
    stream(w, outs=(A.AMBEPLUS,), pids=[0, 1, 1, 2])
    ctl(w, A.close(1), None)
    stream(w, outs=(A.AMBEPLUS,), pids=[3])
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)
    result = w.check(tmp_path)
    assert codes(result, "error") == ["ambed.data_after_close", "ambed.pid_dup"]


def test_retransmit_must_keep_the_stream(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 5), A.std(1, PORT, A.PCM, A.AMBEPLUS, 5))
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 5), A.std(2, PORT + 1, A.PCM, A.AMBEPLUS, 5))
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.retransmit_new_stream") == [4]


def test_invalid_open_accepted_and_unanswered_ping(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS, A.AMBEPLUS), 3), A.std(1, PORT, A.PCM, A.AMBEPLUS, 3))
    ctl(w, A.ping("XLX999"), None)
    ctl(w, A.CAPS_REQUEST + b"x", None, 2.0)
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.open_invalid_accepted") == [2]
    assert frames_of(result, "ambed.no_reply") == [3]
    assert frames_of(result, "ambed.unparseable") == [4]
