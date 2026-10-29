# SPDX-License-Identifier: MIT
"""dvtest.check on synthetic DExtra, DMR, M17 and USRP streams built with
the dvtest.proto encoders: clean streams give no finding, each injected
fault its code."""
from dvtest import check
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


def dmr_wire(close=None, close_gap=0.0, **kw):
    """close: (src, payload) of a close packet sent close_gap s before the stream."""
    w = Wire()
    ident = dmr.DMR_ID.to_bytes(4, "big")
    w.send(T, 40002, R, dmr.PORT, b"RPTL" + ident)
    w.send(R, dmr.PORT, T, 40002, b"RPTACK" + b"\x01\x02\x03\x04", 0.001)
    w.send(T, 40002, R, dmr.PORT, b"RPTK" + ident + b"\x00" * 32, 0.01)
    w.send(R, dmr.PORT, T, 40002, b"RPTACK", 0.001)
    if close is not None:
        src = (T, 40002, R, dmr.PORT) if close[0] == T else (R, dmr.PORT, T, 40002)
        w.send(*src, close[1], 0.01)
        w.wait(close_gap)
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


def test_dmr_close_forms(tmp_path):
    ident = dmr.DMR_ID.to_bytes(4, "big")
    # a 9 B RPTCL is ignored by xlxd: no close, later traffic is fine
    result = dmr_wire(close=(T, b"RPTCL" + ident)).check(tmp_path)
    assert codes(result) == ["dmr.close_ignored"] and frames_of(result, "dmr.close_ignored") == [5]
    # the 13 B form closes the peer
    result = dmr_wire(close=(T, b"RPTCL" + ident + b"\x00" * 4)).check(tmp_path)
    assert codes(result) == ["dmr.after_close"]
    # DMRD right after MSTCL: upstream client-cache window, a warning
    result = dmr_wire(close=(R, b"MSTCL"), close_gap=0.5, bursts=12).check(tmp_path)
    assert codes(result, "error") == ["dmr.after_close"]
    assert codes(result, "warn") == ["dmr.after_close_cache"]
    assert "upstream: client cache" in [f for f in result["findings"]
                                        if f["code"] == "dmr.after_close_cache"][0]["message"]


def mstcl_mid_stream(after=0):
    """A DMR stream to a peer, cut by MSTCL; `after` more DMRD follow it."""
    w = dmr_wire(bursts=6, terminator=False)
    w.rows.pop()                                        # drop the trailing RPTPING
    w.t = w.rows[-1][0]
    w.send(R, dmr.PORT, T, 40002, b"MSTCL", 0.01)
    src, dst = dmr.DMR_ID, dmr.module_tg("B")
    for n in range(after):
        w.send(R, dmr.PORT, T, 40002, dmr.packet(7 + n, src, dst, src, 0x80 | (n + 1), 0x01020304,
                                                 dmr.voice_payload(FRAME, FRAME, FRAME)), 0.060)
    w.wait(6.0)
    return w


def pair(tmp_path, ref_wire, sut_wire, name="xlxd"):
    ref = check.check_file(ref_wire.save(tmp_path / ("%s-ref.pcapng" % name)))
    sut = check.check_file(sut_wire.save(tmp_path / ("%s-sut.pcapng" % name)))
    check.differential([ref, sut])
    return ref, sut


def severities(result, code):
    return {f["severity"] for f in result["findings"] if f["code"] == code}


def test_differential_rule(tmp_path):
    # the same code on both sides: upstream behaviour, a warning on both
    ref, sut = pair(tmp_path, mstcl_mid_stream(3), mstcl_mid_stream(3))
    assert severities(sut, "dmr.after_close_cache") == {"warn"} and sut["errors"] == 0
    assert all("also on reference" in f["message"] for f in sut["findings"] if f["code"] == "dmr.after_close_cache")
    # sut only, and the reference had a stream cut by MSTCL too: regression
    ref, sut = pair(tmp_path, mstcl_mid_stream(0), mstcl_mid_stream(3))
    assert severities(sut, "dmr.after_close_cache") == {"error"} and sut["errors"] == 3
    # sut only, but the reference had no stream at its close: not comparable
    idle_close = dmr_wire(close=(R, b"MSTCL"))
    del idle_close.rows[5:]                              # login, then MSTCL with nothing flowing
    ref, sut = pair(tmp_path, idle_close, mstcl_mid_stream(3))
    assert severities(sut, "dmr.after_close_cache") == {"warn"}
    assert all("not comparable on reference: no stream at close" in f["message"]
               for f in sut["findings"] if f["code"] == "dmr.after_close_cache")
    # any other code: both sides -> warn, sut only -> error, reference only -> warn
    ref, sut = pair(tmp_path, dmr_wire(drop=(4,)), dmr_wire(drop=(4,)))
    assert severities(sut, "dmr.seq_loss") == severities(ref, "dmr.seq_loss") == {"warn"}
    ref, sut = pair(tmp_path, dmr_wire(), dmr_wire(drop=(4,)))
    assert severities(sut, "dmr.seq_loss") == {"error"}
    ref, sut = pair(tmp_path, dmr_wire(drop=(4,)), dmr_wire())
    assert severities(ref, "dmr.seq_loss") == {"warn"} and ref["errors"] == 0
    # a capture without a partner keeps its severities
    alone = check.check_file(dmr_wire(drop=(4,)).save(tmp_path / "xlxd-sut.pcapng"))
    check.differential([alone])
    assert severities(alone, "dmr.seq_loss") == {"error"}

def test_dextra_zero_frames_sync_note(tmp_path):
    w = Wire()
    w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"))
    w.send(R, dextra.PORT, T, 40001, dextra.header(0x2222, "N0TEST", "B"), 0.01)
    for seq in range(25):
        w.send(R, dextra.PORT, T, 40001, dextra.voice(0x2222, seq, bytes(9), False), 0.020)
    w.send(R, dextra.PORT, T, 40001, dextra.voice(0x2222, 25, bytes(9), True), 0.020)
    msg = [f for f in w.check(tmp_path)["findings"] if f["code"] == "dextra.sync_missing"][0]["message"]
    assert msg.endswith("(all AMBE frames zero: transcoder bypass, upstream)")


def m17_relay_wire(m17_first=True, start=2, drop=()):
    """M17 fn 0 into the reflector, then the D-STAR stream it relays."""
    w = Wire()
    w.send(T, 40003, R, m17.PORT, m17.conn_packet("N0TEST", "B"))
    w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"), 0.01)
    if m17_first:
        w.send(T, 40003, R, m17.PORT, m17.packet(0x4343, 0, "N0TEST", FRAME[:8] * 2), 0.01)
        w.send(T, 40003, R, m17.PORT, m17.packet(0x4343, 1, "N0TEST", FRAME[:8] * 2, eot=True), 0.04)
    w.send(R, dextra.PORT, T, 40001, dextra.header(0x3333, "N0TEST", "B"), 0.01)
    for seq in range(start, 30):
        if seq not in drop:
            slow = dextra.SYNC_DATA if seq % 21 == 0 else dextra.SLOW_DATA
            w.send(R, dextra.PORT, T, 40001, dextra.voice(0x3333, seq, FRAME, False, slow), 0.020)
    w.send(R, dextra.PORT, T, 40001, dextra.voice(0x3333, 30, FRAME, True), 0.020)
    return w


def test_dextra_m17_frame0_drop_is_upstream(tmp_path):
    result = m17_relay_wire().check(tmp_path)
    assert codes(result, "error") == [] and codes(result) == ["dextra.seq_loss"]
    assert result["findings"][0]["message"].endswith("(upstream: M17 frame 0 dropped)")
    assert result["protocols"]["dextra"]["streams"][0]["loss"] == 2
    # without an M17 source, or with any other gap, a seq_loss stays an error
    assert codes(m17_relay_wire(m17_first=False).check(tmp_path), "error") == ["dextra.seq_loss"]
    assert codes(m17_relay_wire(start=3).check(tmp_path), "error") == ["dextra.seq_loss"]
    result = m17_relay_wire(drop=(12,)).check(tmp_path)
    assert [f["severity"] for f in result["findings"] if f["code"] == "dextra.seq_loss"] == ["warn", "error"]


def test_usrp_restart_after_unkey(tmp_path):
    """A sender session may restart at seq 0 right after an unkey; without
    the unkey a repeat of seq 0 is still a duplicate."""
    pcm = b"\x01\x00" * 160
    for unkey, want in ((True, []), (False, ["usrp.seq_dup"])):
        w = Wire()
        for session in range(2):
            for seq in range(10):
                w.send(R, 34003, T, 32003, usrp.voice(seq, pcm), 0.020)
            if unkey or session:
                w.send(R, 34003, T, 32003, usrp.unkey(10), 0.020)
            w.wait(1.0)
        w.wait(6.0)
        assert codes(w.check(tmp_path), "error") == want


def test_xlx_no_transit(tmp_path):
    """A stream received from an XLX peer must not go back out to an XLX
    peer while it is open; another stream id, or the same id later, may."""
    X, P, Q = "192.0.2.11", "192.0.2.12", "192.0.2.14"

    def wire(back_to, sid=0x7777, gap=0.02):
        w = Wire()
        w.send(X, 10002, P, 10002, b"L" + b"XLX999  " + bytes(29) + b"\x00")
        w.send(P, 10002, X, 10002, b"L" + b"XLX998  " + bytes(29) + b"\x00", 0.01)
        w.send(P, 10002, X, 10002, dextra.header(0x7777, "N0TEST", "B"), 0.01)
        for seq in range(5):
            w.send(P, 10002, X, 10002, dextra.voice(0x7777, seq, FRAME, seq == 4,
                                                    dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA), 0.02)
        w.send(X, 10002, back_to, 10002, dextra.header(sid, "N0TEST", "B"), gap)
        w.send(X, 10002, back_to, 10002, dextra.voice(sid, 0, FRAME, True, dextra.SYNC_DATA), 0.02)
        return w
    for back_to, sid, gap, want in ((P, 0x7777, 0.02, ["xlx.transit"]), (Q, 0x7777, 0.02, ["xlx.transit"]),
                                    (P, 0x8888, 0.02, []), (P, 0x7777, 3.0, [])):
        result = wire(back_to, sid, gap).check(tmp_path)
        assert codes(result, "error") == want, (back_to, sid, gap)
        assert frames_of(result, "xlx.transit") == ([9] if want else [])
