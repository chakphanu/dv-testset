# SPDX-License-Identifier: MIT
"""dvtest.check.nxdn on synthetic NXDNReflector traffic built with
dvtest.proto.nxdn: a clean stream gives no finding, each fault its code."""
from dvtest.proto import nxdn
from test_check_common import Wire, codes, frames_of

X, REF = "192.0.2.11", "192.0.2.51"
CPORT, TG, SRC = 40005, 31672, 1001


def poll(w, tg=TG, echo=True, gap=0.0):
    w.send(X, CPORT, REF, nxdn.PORT, nxdn.poll_packet("XLX999", tg), gap)
    if echo:
        w.send(REF, nxdn.PORT, X, CPORT, nxdn.poll_packet("XLX999", tg), 0.001)


def flip(packet, index, mask=0x01):
    out = bytearray(packet)
    out[index] ^= mask
    return bytes(out)


def nxdn_wire(fault=None):
    w = Wire()
    poll(w)
    packets = nxdn.stream_packets([bytes([i]) * 7 for i in range(40)], SRC, TG)   # header, 10 voice, trailer
    if fault == "drop":
        del packets[4]
    elif fault == "lich":
        packets[3] = flip(packets[3], 10)
    elif fault == "sacch":
        packets[3] = flip(packets[3], 13)
    elif fault == "facch":
        packets[0] = flip(packets[0], 20)
    elif fault == "no_trailer":
        packets.pop()
    elif fault == "tg":
        packets[5] = nxdn.packet(SRC, TG + 1, packets[5][10:])
    if fault == "unlink":
        w.send(X, CPORT, REF, nxdn.PORT, nxdn.unlink_packet("XLX999", TG), 0.01)
    for p in packets:
        w.send(X, CPORT, REF, nxdn.PORT, p, 0.080)
        if fault == "relay_back" and p is packets[2]:
            w.send(REF, nxdn.PORT, X, CPORT, p, 0.001)
    poll(w, echo=fault != "no_echo", gap=6.0)
    if fault in ("no_echo", "wrong_tg_poll"):
        poll(w, tg=TG + 5 if fault == "wrong_tg_poll" else TG, echo=False, gap=0.5)
        poll(w, gap=2.0)
    return w


def test_clean_stream(tmp_path):
    result = nxdn_wire().check(tmp_path, "pcap", ">")
    assert codes(result) == []
    s = result["protocols"]["nxdn"]["streams"][0]
    assert (s["frames"], s["loss"], s["end"]) == (10, 0, "terminated")


def test_faults(tmp_path):
    result = nxdn_wire("drop").check(tmp_path)
    assert codes(result) == ["nxdn.voice_loss"] and frames_of(result, "nxdn.voice_loss") == [7]
    assert codes(nxdn_wire("lich").check(tmp_path)) == ["nxdn.lich_parity"]
    assert codes(nxdn_wire("sacch").check(tmp_path)) == ["nxdn.sacch_crc"]
    result = nxdn_wire("facch").check(tmp_path)
    assert codes(result) == ["nxdn.facch_crc"] and frames_of(result, "nxdn.facch_crc") == [3]
    assert codes(nxdn_wire("no_trailer").check(tmp_path)) == ["nxdn.no_trailer"]
    assert codes(nxdn_wire("tg").check(tmp_path)) == ["nxdn.wrong_tg"]
    assert codes(nxdn_wire("unlink").check(tmp_path)) == ["nxdn.after_unlink"]
    assert codes(nxdn_wire("relay_back").check(tmp_path)) == ["nxdn.relay_to_sender"]
    assert codes(nxdn_wire("no_echo").check(tmp_path)) == ["nxdn.no_echo"]
    assert codes(nxdn_wire("wrong_tg_poll").check(tmp_path)) == ["nxdn.poll_wrong_tg"]
