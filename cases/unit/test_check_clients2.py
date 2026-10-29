# SPDX-License-Identifier: MIT
"""dvtest.check.dplus / dcs / ysf on synthetic traffic built with the
dvtest.proto encoders: clean streams give no finding, faults their code."""
from dvtest.proto import dcs, dextra, dplus, ysf
from test_check_common import Wire, codes, frames_of

R, T = "192.0.2.11", "192.0.2.30"
FRAME = bytes(range(9))


def slow(seq):
    return dextra.SYNC_DATA if seq % 21 == 0 else dextra.SLOW_DATA


def dplus_wire(drop=(), last=True):
    w = Wire()
    w.send(T, 40010, R, dplus.PORT, dplus.CONNECT)
    w.send(R, dplus.PORT, T, 40010, dplus.CONNECT, 0.001)
    w.send(T, 40010, R, dplus.PORT, dplus.login_packet("N0TEST"), 0.01)
    w.send(R, dplus.PORT, T, 40010, dplus.LOGIN_OK, 0.5)
    w.send(R, dplus.PORT, T, 40010, dplus.header(0x5555, "N0TEST", "B"), 0.01)
    for seq in range(30):
        if seq == 21:
            w.send(R, dplus.PORT, T, 40010, dplus.header(0x5555, "N0TEST", "B"), 0.001)
        if seq not in drop:
            w.send(R, dplus.PORT, T, 40010, dplus.voice(0x5555, seq, FRAME, slow(seq)), 0.020)
    if last:
        w.send(R, dplus.PORT, T, 40010, dplus.last_voice(0x5555, 30), 0.020)
    w.send(R, dplus.PORT, T, 40010, dplus.KEEPALIVE, 6.0)
    return w


def dcs_wire(drop=(), last=True, extra=b""):
    w = Wire()
    w.send(T, 40011, R, dcs.PORT, dcs.link_packet("N0TEST", "B"))
    w.send(R, dcs.PORT, T, 40011, b"N0TEST  BBACK\x00", 0.5)
    for seq in range(30):
        if seq not in drop:
            w.send(R, dcs.PORT, T, 40011, dcs.voice(0x6666, seq, FRAME, False, seq, "N0TEST", "B",
                                                     slow=slow(seq)), 0.020)
    if last:
        w.send(R, dcs.PORT, T, 40011, dcs.voice(0x6666, 30, FRAME, True, 30, "N0TEST", "B"), 0.020)
    if extra:
        w.send(R, dcs.PORT, T, 40011, extra, 0.020)
    w.send(T, 40011, R, dcs.PORT, dcs.keepalive_packet("N0TEST", "B"), 6.0)
    return w


def ysf_wire(fault=None):
    w = Wire()
    w.send(T, 40012, R, ysf.PORT, ysf.poll_packet("N0TEST"))
    if fault != "no_ack":
        w.send(R, ysf.PORT, T, 40012, ysf.ACK, 0.001)
    packets = ysf.stream_packets([FRAME] * 50, "N0TEST")       # header, 10 voice, terminator
    if fault == "drop":
        del packets[4]
    elif fault == "no_terminator":
        packets.pop()
    elif fault == "sync":
        packets[3] = packets[3][:35] + bytes(5) + packets[3][40:]
    elif fault == "fich":
        packets[3] = packets[3][:40] + bytes(25) + packets[3][65:]
    elif fault == "eot":
        packets[3] = packets[3][:34] + bytes((packets[3][34] | 1,)) + packets[3][35:]
    for p in packets:
        w.send(R, ysf.PORT, T, 40012, p, 0.100)
    w.send(T, 40012, R, ysf.PORT, ysf.status_request(), 6.0)
    w.send(R, ysf.PORT, T, 40012, b"YSFS" + b"12345" + b"XLX999".ljust(16) + b"dvtest".ljust(14) + b"001", 0.001)
    return w


def test_dplus(tmp_path):
    result = dplus_wire().check(tmp_path)
    assert codes(result) == []
    s = result["protocols"]["dplus"]["streams"][0]
    assert (s["frames"], s["end"], s["notes"]["header_repeats"]) == (31, "terminated", 1)
    result = dplus_wire(drop=(12,)).check(tmp_path)
    assert codes(result) == ["dplus.seq_loss"] and frames_of(result, "dplus.seq_loss") == [18]
    assert codes(dplus_wire(last=False).check(tmp_path)) == ["dplus.no_last"]


def test_dcs(tmp_path):
    result = dcs_wire().check(tmp_path, "pcap")
    assert codes(result) == [] and result["protocols"]["dcs"]["streams"][0]["frames"] == 31
    assert codes(dcs_wire(drop=(5, 6)).check(tmp_path)) == ["dcs.seq_loss"]
    assert codes(dcs_wire(last=False).check(tmp_path)) == ["dcs.no_last"]
    assert codes(dcs_wire(extra=b"0001" + bytes(50)).check(tmp_path)) == ["dcs.unparseable"]


def test_ysf(tmp_path):
    result = ysf_wire().check(tmp_path)
    assert codes(result) == []
    assert result["protocols"]["ysf"]["streams"][0]["frames"] == 10
    for fault, code in (("drop", "ysf.fn_loss"), ("no_terminator", "ysf.no_terminator"),
                        ("sync", "ysf.bad_sync"), ("fich", "ysf.fich_crc"), ("eot", "ysf.eot_bit"),
                        ("no_ack", "ysf.no_reply")):
        assert code in codes(ysf_wire(fault).check(tmp_path), "error"), fault
