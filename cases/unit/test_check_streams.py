# SPDX-License-Identifier: MIT
"""dvtest.check on synthetic DExtra, DMR, M17 and USRP streams built with
the dvtest.proto encoders: clean streams give no finding, each injected
fault its code."""
from dvtest.proto import dextra, dmr, m17, usrp
from test_check_common import Wire, codes, frames_of

R, T = "192.0.2.11", "192.0.2.30"      # reflector, tester
FRAME = bytes(range(9))


# DExtra

def dextra_stream(w, sid=0x1234, count=45, drop=(), swap=None, last=True, extra=()):
    port = 40001
    packets = [dextra.header(sid, "N0TEST", "B")] * 3
    for seq in range(count):
        slow = dextra.SYNC_DATA if seq % 21 == 0 else dextra.SLOW_DATA
        packets.append(dextra.voice(sid, seq, FRAME, False, slow))
    if last:
        packets.append(dextra.voice(sid, count, FRAME, True))
    packets += list(extra)
    order = [i for i in range(len(packets)) if i - 3 not in drop]
    if swap is not None:
        i = order.index(swap + 3)
        order[i], order[i + 1] = order[i + 1], order[i]
    for i in order:
        w.send(R, dextra.PORT, T, port, packets[i], 0.020)


def dextra_wire(**kw):
    w = Wire()
    w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"))
    w.send(R, dextra.PORT, T, 40001, b"N0TEST  BBACK\x00", 0.01)
    dextra_stream(w, **kw)
    w.send(T, 40001, R, dextra.PORT, dextra.keepalive_packet("N0TEST"), 6.0)
    return w


def test_dextra_clean(tmp_path):
    result = dextra_wire().check(tmp_path, "pcap", ">")
    assert codes(result) == []
    s = result["protocols"]["dextra"]["streams"][0]
    assert (s["frames"], s["loss"], s["end"], s["notes"]["header_repeats"]) == (46, 0, "terminated", 2)


def test_dextra_faults(tmp_path):
    result = dextra_wire(drop=(10, 11)).check(tmp_path)
    assert codes(result, "error") == ["dextra.seq_loss"] and frames_of(result, "dextra.seq_loss") == [16]
    assert codes(dextra_wire(swap=10).check(tmp_path), "error") == ["dextra.seq_reorder"]
    assert codes(dextra_wire(last=False).check(tmp_path)) == ["dextra.no_last"]
    after = dextra.voice(0x1234, 46, FRAME, False)
    assert codes(dextra_wire(extra=[after]).check(tmp_path)) == ["dextra.after_last"]
    short = dextra.voice(0x1234, 5, FRAME, False)[:-1]
    result = dextra_wire(extra=[short]).check(tmp_path)
    assert codes(result) == ["dextra.unparseable"] and frames_of(result, "dextra.unparseable") == [52]


# DMR (MMDVM / Homebrew DMRD)

def dmr_stream(w, sid=0x01020304, bursts=12, drop=(), order_fault=None, terminator=True, size=55):
    port, src, dst = 40002, dmr.DMR_ID, dmr.module_tg("B")
    sync = dmr.data_sync_payload()
    packets = [dmr.packet(0, src, dst, src, 0xA1, sid, sync)]
    for n in range(bursts):
        vseq = n % 6 if n != order_fault else (n + 1) % 6
        bits = 0x80 | (0x10 if vseq == 0 else 0) | vseq
        packets.append(dmr.packet(n + 1, src, dst, src, bits, sid, dmr.voice_payload(FRAME, FRAME, FRAME)))
    if terminator:
        packets.append(dmr.packet(bursts + 1, src, dst, src, 0xA2, sid, sync))
    for i, p in enumerate(packets):
        if i - 1 not in drop:
            w.send(R, dmr.PORT, T, port, p[:size] if i == 3 else p, 0.060)


def dmr_wire(**kw):
    w = Wire()
    ident = dmr.DMR_ID.to_bytes(4, "big")
    w.send(T, 40002, R, dmr.PORT, b"RPTL" + ident)
    w.send(R, dmr.PORT, T, 40002, b"RPTACK" + b"\x01\x02\x03\x04", 0.001)
    w.send(T, 40002, R, dmr.PORT, b"RPTK" + ident + b"\x00" * 32, 0.01)
    w.send(R, dmr.PORT, T, 40002, b"RPTACK", 0.001)
    dmr_stream(w, **kw)
    w.send(T, 40002, R, dmr.PORT, b"RPTPING" + ident, 6.0)
    return w


def test_dmr_clean(tmp_path):
    result = dmr_wire().check(tmp_path)
    assert codes(result) == []
    s = result["protocols"]["dmr"]["streams"][0]
    assert (s["frames"], s["end"]) == (14, "terminated")


def test_dmr_faults(tmp_path):
    assert codes(dmr_wire(drop=(4,)).check(tmp_path), "error") == ["dmr.seq_loss"]
    assert codes(dmr_wire(order_fault=4).check(tmp_path)) == ["dmr.burst_order"]
    assert codes(dmr_wire(terminator=False).check(tmp_path)) == ["dmr.no_terminator"]
    result = dmr_wire(size=54).check(tmp_path)
    assert codes(result, "error") == ["dmr.bad_length", "dmr.seq_loss"]
    assert frames_of(result, "dmr.bad_length") == [8]


# M17

def m17_wire(fault=None):
    w = Wire()
    port = 40003
    w.send(T, port, R, m17.PORT, m17.conn_packet("N0TEST", "B"))
    w.send(R, m17.PORT, T, port, b"ACKN", 0.01)
    packets = m17.voice_packets([bytes([i]) * 8 for i in range(40)], 0x4242, "N0TEST")
    if fault == "drop":
        del packets[5]
    elif fault == "no_eot":
        packets.pop()
    elif fault == "lich":
        packets[7] = m17.packet(0x4242, 7, "N0CALL", bytes(16))
    elif fault == "after_eot":
        packets.append(m17.packet(0x4242, 30, "N0TEST", bytes(16)))
    for p in packets:
        w.send(R, m17.PORT, T, port, p, 0.040)
    w.send(T, port, R, m17.PORT, m17.pong_packet("N0TEST"), 6.0)
    return w


def test_m17_clean_and_faults(tmp_path):
    result = m17_wire().check(tmp_path, "pcap")
    assert codes(result) == [] and result["protocols"]["m17"]["streams"][0]["frames"] == 22
    assert codes(m17_wire("drop").check(tmp_path), "error") == ["m17.fn_loss"]
    assert codes(m17_wire("no_eot").check(tmp_path)) == ["m17.no_eot"]
    assert codes(m17_wire("lich").check(tmp_path)) == ["m17.lich_changed"]
    assert codes(m17_wire("after_eot").check(tmp_path)) == ["m17.after_eot"]


# USRP

def usrp_wire(fault=None):
    w = Wire()
    seq = 0
    pcm = b"\x01\x00" * 160
    for over in range(2):
        for n in range(20):
            if fault == "jump" and over == 1 and n == 5:
                seq += 3
            frame = usrp.voice(seq, pcm)
            if fault == "short" and over == 0 and n == 3:
                frame = usrp.header(seq, 1)
            w.send(R, 34003, T, 32003, frame, 0.020)
            if fault == "dup" and over == 0 and n == 7:
                w.send(R, 34003, T, 32003, frame, 0.001)
            seq += 1
        if not (fault == "no_unkey" and over == 0):
            w.send(R, 34003, T, 32003, usrp.unkey(seq), 0.020)
            seq += 1
        w.wait(6.0)
    return w


def test_usrp_clean_and_faults(tmp_path):
    result = usrp_wire().check(tmp_path)
    assert codes(result) == []
    assert [s["frames"] for s in result["protocols"]["usrp"]["streams"]] == [21, 21]
    assert codes(usrp_wire("jump").check(tmp_path)) == ["usrp.seq_loss"]
    assert codes(usrp_wire("dup").check(tmp_path)) == ["usrp.seq_dup"]
    assert codes(usrp_wire("no_unkey").check(tmp_path)) == ["usrp.missing_unkey"]
    result = usrp_wire("short").check(tmp_path)
    assert codes(result) == ["usrp.bad_length"] and frames_of(result, "usrp.bad_length") == [4]
