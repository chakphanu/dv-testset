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


def test_dextra_unlink_mid_stream_ends_the_stream(tmp_path):
    """A listener that unlinks mid-stream ends its egress stream (a warning,
    even with synthesized_terminator declared); silence without an unlink
    is still an error."""
    for unlink, want_error in ((True, []), (False, ["dextra.no_last"])):
        w = Wire()
        w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"))
        w.send(R, dextra.PORT, T, 40001, dextra.header(0x4444, "N0TEST", "B"), 0.01)
        for seq in range(10):
            w.send(R, dextra.PORT, T, 40001, dextra.voice(0x4444, seq, FRAME, False,
                                                         dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA), 0.02)
        if unlink:
            w.send(T, 40001, R, dextra.PORT, dextra.unlink_packet("N0TEST", "B"), 0.02)
        w.send(T, 40002, R, dextra.PORT, dextra.keepalive_packet("N0OTHER"), 6.0)
        result = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"))
        check.apply_divergences([result], {"synthesized_terminator"}, {R})
        assert codes(result, "error") == want_error
        cut = [f for f in result["findings"] if f["code"] == "dextra.no_last"]
        assert len(cut) == 1 and ("peer unlink" in cut[0]["message"]) == unlink


def test_m17_disc_mid_stream_ends_the_stream(tmp_path):
    for disc, want_error in ((True, []), (False, ["m17.no_eot"])):
        w = Wire()
        w.send(T, 40003, R, m17.PORT, m17.conn_packet("N0TEST", "B"))
        w.send(R, m17.PORT, T, 40003, b"ACKN", 0.01)
        for fn in range(10):
            w.send(R, m17.PORT, T, 40003, m17.packet(0x5151, fn, "N0OTHER", FRAME[:8] * 2), 0.04)
        if disc:
            w.send(T, 40003, R, m17.PORT, m17.disc_packet("N0TEST"), 0.02)
        w.send(T, 40004, R, m17.PORT, m17.pong_packet("N0X"), 6.0)
        result = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"))
        check.apply_divergences([result], {"synthesized_terminator"}, {R})
        assert codes(result, "error") == want_error
        cut = [f for f in result["findings"] if f["code"] == "m17.no_eot"]
        assert len(cut) == 1 and ("peer DISC" in cut[0]["message"]) == disc


def test_dmr_tg4000_unlink_ends_egress(tmp_path):
    """A repeater's TG 4000 unlink ends its live egress stream (warning);
    DMRD to it afterwards: one burst within 60 ms in flight, else an error."""
    src, tg = dmr.DMR_ID, dmr.module_tg("B")
    for after, gap, want_error in ((0, 0.0, []), (1, 0.010, []), (2, 0.010, ["dmr.after_unlink"]),
                                   (1, 0.100, ["dmr.after_unlink"])):
        w = dmr_wire(bursts=6, terminator=False)
        w.rows.pop()
        w.t = w.rows[-1][0]
        w.send(T, 40002, R, dmr.PORT, dmr.packet(0, src, 4000, src, 0xA2, 0x33333333, dmr.data_sync_payload()), 0.005)
        for n in range(after):
            w.send(R, dmr.PORT, T, 40002, dmr.packet(7 + n, src, tg, src, 0x80 | (n + 1), 0x01020304,
                                                     dmr.voice_payload(FRAME, FRAME, FRAME)), gap if n == 0 else 0.06)
        w.wait(6.0)
        result = w.check(tmp_path)
        assert codes(result, "error") == want_error, (after, gap)
        cut = [f for f in result["findings"] if f["code"] == "dmr.no_terminator" and "peer unlink" in f["message"]]
        assert len(cut) == 1
        assert ("dmr.in_flight" in codes(result)) == (after >= 1 and gap <= 0.06)


def test_dextra_frame_in_flight_at_unlink(tmp_path):
    """One voice frame within 20 ms after the unlink is in flight (warning);
    two frames, or one frame later than 20 ms, are an error."""
    for after, gap, want_error in ((1, 0.007, []), (2, 0.007, ["dextra.after_unlink"]),
                                   (1, 0.030, ["dextra.after_unlink"])):
        w = Wire()
        w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"))
        w.send(R, dextra.PORT, T, 40001, dextra.header(0xadd8, "N0TEST", "B"), 0.01)
        for seq in range(10):
            w.send(R, dextra.PORT, T, 40001, dextra.voice(0xadd8, seq, FRAME, False,
                                                         dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA), 0.02)
        w.send(T, 40001, R, dextra.PORT, dextra.unlink_packet("N0TEST", "B"), 0.019)
        for n in range(after):
            w.send(R, dextra.PORT, T, 40001, dextra.voice(0xadd8, 10 + n, FRAME, False), gap if n == 0 else 0.02)
        w.send(T, 40002, R, dextra.PORT, dextra.keepalive_packet("N0OTHER"), 6.0)
        result = w.check(tmp_path)
        assert codes(result, "error") == want_error, (after, gap)
        assert "dextra.in_flight" in codes(result) or want_error


def test_m17_frame_after_disc(tmp_path):
    for gap, want_error in ((0.010, []), (0.100, ["m17.after_disc"])):
        w = Wire()
        w.send(T, 40003, R, m17.PORT, m17.conn_packet("N0TEST", "B"))
        for fn in range(10):
            w.send(R, m17.PORT, T, 40003, m17.packet(0x5252, fn, "N0OTHER", FRAME[:8] * 2), 0.04)
        w.send(T, 40003, R, m17.PORT, m17.disc_packet("N0TEST"), 0.03)
        w.send(R, m17.PORT, T, 40003, m17.packet(0x5252, 10, "N0OTHER", FRAME[:8] * 2), gap)
        w.send(T, 40004, R, m17.PORT, m17.pong_packet("N0X"), 6.0)
        assert codes(w.check(tmp_path), "error") == want_error, gap


def test_stream_end_in_flight_at_unlink(tmp_path):
    """A stream's end (DMR terminator, D-STAR last frame, M17 EOT) within one
    frame time after the listener left is in flight, like one voice packet;
    more packets, or a later one, stay an error."""
    src, tg = dmr.DMR_ID, dmr.module_tg("B")
    sync = dmr.data_sync_payload()
    for packets, gap, want_error in (([0xA2], 0.00004, []), ([0x81, 0xA2], 0.00004, ["dmr.after_unlink"]),
                                     ([0xA2], 0.080, ["dmr.after_unlink"])):
        w = dmr_wire(bursts=6, terminator=False)
        w.rows.pop()
        w.t = w.rows[-1][0]
        w.send(T, 40002, R, dmr.PORT, dmr.packet(0, src, 4000, src, 0xA2, 0x33333333, sync), 0.005)
        for n, bits in enumerate(packets):
            payload = sync if bits == 0xA2 else dmr.voice_payload(FRAME, FRAME, FRAME)
            w.send(R, dmr.PORT, T, 40002, dmr.packet(7 + n, src, tg, src, bits, 0x01020304, payload),
                   gap if n == 0 else 0.001)
        w.wait(6.0)
        result = w.check(tmp_path)
        assert codes(result, "error") == want_error, (packets, gap)
        if not want_error:
            assert [f for f in result["findings"] if f["code"] == "dmr.in_flight"][0]["message"].startswith(
                "terminator in flight")
    w = Wire()
    w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"))
    w.send(R, dextra.PORT, T, 40001, dextra.header(0x4545, "N0TEST", "B"), 0.01)
    for seq in range(5):
        w.send(R, dextra.PORT, T, 40001, dextra.voice(0x4545, seq, FRAME, False,
                                                     dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA), 0.02)
    w.send(T, 40001, R, dextra.PORT, dextra.unlink_packet("N0TEST", "B"), 0.019)
    w.send(R, dextra.PORT, T, 40001, dextra.voice(0x4545, 5, FRAME, True), 0.001)
    w.send(T, 40002, R, dextra.PORT, dextra.keepalive_packet("N0OTHER"), 6.0)
    result = w.check(tmp_path)
    assert codes(result, "error") == [] and "dextra.in_flight" in codes(result)
    w = Wire()
    w.send(T, 40003, R, m17.PORT, m17.conn_packet("N0TEST", "B"))
    for fn in range(5):
        w.send(R, m17.PORT, T, 40003, m17.packet(0x5353, fn, "N0OTHER", FRAME[:8] * 2), 0.04)
    w.send(T, 40003, R, m17.PORT, m17.disc_packet("N0TEST"), 0.03)
    w.send(R, m17.PORT, T, 40003, m17.packet(0x5353, 5, "N0OTHER", FRAME[:8] * 2, eot=True), 0.001)
    w.send(T, 40004, R, m17.PORT, m17.pong_packet("N0X"), 6.0)
    result = w.check(tmp_path)
    assert codes(result, "error") == []
    assert [f for f in result["findings"] if f["code"] == "m17.in_flight"][0]["message"].startswith("EOT frame")


def test_dmr_stream_id_reuse_after_terminator(tmp_path):
    """A voice LC header that reuses a terminated stream id opens a new
    stream; voice after the terminator without one is a single finding."""
    src, tg, sid = dmr.DMR_ID, dmr.module_tg("B"), 0x4f87
    sync = dmr.data_sync_payload()

    def wire(with_header):
        w = dmr_wire(sid=sid, bursts=6)            # header, A..F, terminator
        w.rows.pop()
        w.t = w.rows[-1][0]
        if with_header:
            w.send(R, dmr.PORT, T, 40002, dmr.packet(0, src, tg, src, 0xA1, sid, sync), 0.5)
        for n in range(12):
            vseq = n % 6
            w.send(R, dmr.PORT, T, 40002, dmr.packet((1 if with_header else 8) + n, src, tg, src,
                                                     0x80 | (0x10 if vseq == 0 else 0) | vseq,
                                                     sid, dmr.voice_payload(FRAME, FRAME, FRAME)),
                   0.06 if n or with_header else 0.5)
        w.send(R, dmr.PORT, T, 40002, dmr.packet(13 if with_header else 20, src, tg, src, 0xA2, sid, sync), 0.06)
        w.send(T, 40002, R, dmr.PORT, b"RPTPING" + dmr.DMR_ID.to_bytes(4, "big"), 6.0)
        return w.check(tmp_path)
    reused = wire(True)
    assert codes(reused, "error") == [] and len(reused["protocols"]["dmr"]["streams"]) == 2
    continued = wire(False)
    assert codes(continued, "error") == ["dmr.after_terminator"]
    assert len([f for f in continued["findings"] if f["code"] == "dmr.after_terminator"]) == 1


def test_m17_packet_mode(tmp_path):
    """M17P is its own kind: size 38..859 and the packet-mode TYPE are
    errors, the LSF and payload CRCs warnings; a valid one gives no finding."""
    good = m17.m17p_packet(0x05, b"N0TEST hello", src="N0TEST")

    def check_one(packet):
        w = Wire()
        w.send(T, 40003, R, m17.PORT, m17.conn_packet("N0TEST", "B"))
        w.send(T, 40003, R, m17.PORT, packet, 0.01)
        w.send(R, m17.PORT, T, 40004, packet, 0.001)
        result = w.check(tmp_path)
        assert result["protocols"]["m17"]["kinds"]["packet"] == 2
        return codes(result, "error"), codes(result, "warn")
    assert check_one(good) == ([], [])
    bad_lsf = good[:20] + bytes([good[20] ^ 1]) + good[21:]
    assert check_one(bad_lsf) == ([], ["m17.m17p_lsf_crc"])
    assert check_one(good[:-1] + bytes([good[-1] ^ 1])) == ([], ["m17.m17p_crc"])
    stream_bit = bytearray(good)
    stream_bit[17] |= 1
    lsd = bytes(stream_bit[4:32])
    stream_bit[32:34] = m17.crc16(lsd).to_bytes(2, "big")
    assert check_one(bytes(stream_bit)) == (["m17.m17p_type"], [])
    assert check_one(good[:37]) == (["m17.m17p_length"], [])
    assert check_one(good + bytes(821)) == (["m17.m17p_length"], [])
