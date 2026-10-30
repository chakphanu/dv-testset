# SPDX-License-Identifier: MIT
"""dvtest.check zero-data passthrough: an all-zero stream the system under
test sends is fill only when its source did not already send that codec
data all zero (no_zero_fill)."""
from dvtest import check
from dvtest.proto import dextra, dmr, nxdn
from test_check_common import Wire

X, PEER, T = "192.0.2.11", "192.0.2.12", "192.0.2.30"     # sut, XLX peer, DMR listener
ZERO9 = bytes(9)


def xlx_voice(sid, seq, dmr_slot):
    """45 B XLX revision 2 frame: DExtra voice + DMR id, subid, AMBE+2, sync."""
    return dextra.voice(sid, seq, bytes(range(9)), False) + bytes(2) + dmr_slot + bytes(7)


def dmr_egress(w, sid=0x0b0b, frame=ZERO9):
    src, tg = dmr.DMR_ID, dmr.module_tg("B")
    sync = dmr.data_sync_payload()
    w.send(X, dmr.PORT, T, 40002, dmr.packet(0, src, tg, 1, 0xA1, sid, sync), 0.001)
    for n in range(6):
        w.send(X, dmr.PORT, T, 40002, dmr.packet(n + 1, src, tg, 1, 0x80 | (0x10 if n == 0 else 0) | n, sid,
                                                 dmr.voice_payload(frame, frame, frame)), 0.06)
    w.send(X, dmr.PORT, T, 40002, dmr.packet(7, src, tg, 1, 0xA2, sid, sync), 0.06)


def xlx_relay(dmr_slot):
    w = Wire()
    w.send(T, 40002, X, dmr.PORT, b"RPTL" + dmr.DMR_ID.to_bytes(4, "big"))
    w.send(PEER, 10002, X, 10002, b"LXLX998  " + bytes(29) + b"\x00", 0.01)
    w.send(PEER, 10002, X, 10002, dextra.header(0x3e3e, "N0TEST", "B"), 0.01)
    for seq in range(18):
        w.send(PEER, 10002, X, 10002, xlx_voice(0x3e3e, seq, dmr_slot), 0.02)
        if seq == 2:
            dmr_egress(w)
    w.send(PEER, 10002, X, 10002, dextra.voice(0x3e3e, 18, bytes(9), True), 0.02)
    w.wait(6.0)
    w.send(T, 40002, X, dmr.PORT, b"RPTPING" + dmr.DMR_ID.to_bytes(4, "big"))
    return w


def zero_fill(tmp_path, wire, code):
    result = check.check_file(wire.save(tmp_path / "xlxd-sut.pcapng"))
    check.apply_divergences([result], {"no_zero_fill"}, {X})
    found = [f for f in result["findings"] if f["code"] == code]
    assert len(found) == 1, result["findings"]
    return found[0]


def test_zero_dmr_slot_relayed_from_xlx_peer_is_passthrough(tmp_path):
    f = zero_fill(tmp_path, xlx_relay(ZERO9), "dmr.zero_fill")
    assert f["severity"] == "warn" and "relayed from XLX peer 192.0.2.12" in f["message"]


def test_zero_dmr_from_a_non_zero_xlx_slot_is_fill(tmp_path):
    f = zero_fill(tmp_path, xlx_relay(bytes(range(1, 10))), "dmr.zero_fill")
    assert f["severity"] == "error" and "declared divergence no_zero_fill" in f["message"]


def nxdn_from_dmr_talker(frame):
    """A DMR talker into the sut, relayed as NXDN to the NXDN reflector."""
    w = Wire()
    src, tg = dmr.DMR_ID, dmr.module_tg("B")
    w.send(T, 40002, X, dmr.PORT, b"RPTL" + dmr.DMR_ID.to_bytes(4, "big"))
    w.send(T, 40002, X, dmr.PORT, dmr.packet(0, src, tg, src, 0xA1, 0x0c0c, dmr.data_sync_payload()), 0.01)
    w.send(X, 40100, "192.0.2.51", nxdn.PORT, nxdn.packet(1001, 31672, nxdn.control_payload(nxdn.VCALL, 1001, 31672)),
           0.01)
    for n in range(6):
        w.send(T, 40002, X, dmr.PORT, dmr.packet(n + 1, src, tg, src, 0x80 | (0x10 if n == 0 else 0) | n, 0x0c0c,
                                                 dmr.voice_payload(frame, frame, frame)), 0.06)
        if n % 2:
            w.send(X, 40100, "192.0.2.51", nxdn.PORT, nxdn.packet(1001, 31672, nxdn.voice_payload(
                [bytes(7)] * 4, 1001, 31672, n // 2)), 0.001)
    w.send(T, 40002, X, dmr.PORT, dmr.packet(7, src, tg, src, 0xA2, 0x0c0c, dmr.data_sync_payload()), 0.06)
    w.send(X, 40100, "192.0.2.51", nxdn.PORT, nxdn.packet(1001, 31672, nxdn.control_payload(nxdn.TX_REL, 1001, 31672)),
           0.001)
    w.wait(6.0)
    w.send(T, 40002, X, dmr.PORT, b"RPTPING" + dmr.DMR_ID.to_bytes(4, "big"))
    return w


def test_zero_bits_from_a_dmr_talker_are_passthrough(tmp_path):
    # to_dmr(zero bits) is not zero bytes, but carries zero AMBE+2 data bits
    f = zero_fill(tmp_path, nxdn_from_dmr_talker(nxdn.to_dmr(bytes(7))), "nxdn.zero_fill")
    assert f["severity"] == "warn" and "its dmr source 192.0.2.30 sent the ambe+2 bits data all zero" in f["message"]
    f = zero_fill(tmp_path, nxdn_from_dmr_talker(bytes(range(1, 10))), "nxdn.zero_fill")
    assert f["severity"] == "error"


def test_passthrough_carries_to_the_receiving_capture(tmp_path):
    """The sut's NXDN stream also appears in nxdn-sut.pcapng, where the DMR
    source is not visible: it takes the tag found in xlxd-sut.pcapng."""
    w = nxdn_from_dmr_talker(nxdn.to_dmr(bytes(7)))
    sut = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"))
    seen = Wire()
    seen.rows = [row for row in w.rows if b"NXDN" in row[1]]
    receiver = check.check_file(seen.save(tmp_path / "nxdn-sut.pcapng"))
    check.share_passthrough([sut, receiver])
    check.apply_divergences([sut, receiver], {"no_zero_fill"}, {X})
    for result in (sut, receiver):
        f = [f for f in result["findings"] if f["code"] == "nxdn.zero_fill"]
        assert len(f) == 1 and f[0]["severity"] == "warn" and "passthrough" in f[0]["message"]
