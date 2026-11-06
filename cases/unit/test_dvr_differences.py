# SPDX-License-Identifier: MIT
"""The documented differences between Dire Wolf and DV-Router (docs/direwolf-cases.md, "Documented
differences"), as far as they can be checked offline: the DV-Router lead's statements are
restated as a model in dvtest.ax25 (nothing here runs DV-Router) and compared with the committed
Dire Wolf vectors. A difference is a documented fact, not a failure."""
import json
import os

import pytest

from dvtest import ax25

VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "direwolf")


def _load(name):
    with open(os.path.join(VEC, name), encoding="ascii") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ frame window

def test_frame_windows():
    ui = ax25.from_tnc2("N0CALL-9>APZDVR:x")
    assert len(ui) == 17 and ax25.dvrouter_accepts_frame(ui)
    assert ax25.dvrouter_accepts_frame(ui[:16]) is True                          # 16 bytes: two addresses, control, PID
    assert ax25.dvrouter_accepts_frame(ui[:15]) is False                         # 15 bytes: no PID
    assert ax25.dw_accepts_len(15) and not ax25.dw_accepts_len(14)
    edge = ax25.from_tnc2("N0CALL-9>APZDVR:" + "x" * (1100 - 16))
    assert len(edge) == 1100 and ax25.dvrouter_accepts_frame(edge)
    over = ax25.from_tnc2("N0CALL-9>APZDVR:" + "x" * (1101 - 16))
    assert len(over) == 1101 and not ax25.dvrouter_accepts_frame(over) and ax25.dw_accepts_len(1101)
    assert ax25.dw_accepts_len(2123) and not ax25.dw_accepts_len(2124)


def test_only_ui_frames_with_pid_f0():
    base = ax25.from_tnc2("N0CALL-9>APZDVR:hello")
    assert ax25.dvrouter_accepts_frame(base)
    for control, pid in ((0x13, 0xF0), (0x03, 0xCF), (0x01, 0xF0), (0x00, 0x00)):
        f = bytearray(base)
        f[14], f[15] = control, pid
        assert not ax25.dvrouter_accepts_frame(bytes(f)), (control, pid)
    with_digis = ax25.from_tnc2("N0CALL-9>APZDVR,WIDE1-1,WIDE2-1:hello")
    assert ax25.dvrouter_accepts_frame(with_digis)


# ------------------------------------------------------------------ command / response bits

def test_cr_bits_dire_wolf_versus_dv_router():
    frames = [bytes.fromhex(r["dw_ax25_hex"]) for r in _load("rx_frames.json")["records"]
              if r["dw_generated"] and not r["case"].startswith("neg-")]
    assert len(frames) == 17
    for dw in frames:
        assert dw[6] & 0x80 and dw[13] & 0x80                       # Dire Wolf: C/R = 1 on both
    text = "N0CALL-9>APZDVR:x"
    dvr = ax25.from_tnc2(text, cr_dest=True, cr_src=False)
    assert dvr[6] == 0xE0                                            # APZDVR-0 -> 0xE0
    assert dvr[13] == 0x73                                           # SSID 9, reserved 0x60, last 0x01, C/R 0
    mid = ax25.from_tnc2("N0CALL-9>APZDVR,WIDE1-1:x", cr_src=False)
    assert mid[13] == 0x72                                           # not the last address: 0x72
    dw_like = ax25.from_tnc2(text)
    assert dw_like != dvr and dw_like[13] == 0xF3
    assert ax25.mask_cr_bits(dw_like) == ax25.mask_cr_bits(dvr)
    for dw in frames:
        # re-encoding the same text with DV-Router's bits differs from Dire Wolf's bytes and
        # is equal once bit 7 of both SSID bytes is masked
        parsed = ax25.parse_frame(dw)
        as_dvr = bytearray(dw)
        as_dvr[6] |= 0x80
        as_dvr[13] &= 0x7F
        assert ax25.mask_cr_bits(bytes(as_dvr)) == ax25.mask_cr_bits(dw)
        assert bytes(as_dvr) != dw and parsed["src"]["bit7"]


def test_mask_leaves_the_digipeater_has_been_repeated_bit():
    f = ax25.from_tnc2("N0CALL-9>APZDVR,WIDE1-1*,WIDE2-1:x")
    m = ax25.mask_cr_bits(f)
    assert m[20] & 0x80                                              # digipeater 1 keeps its H bit
    assert m[6] & 0x80 == 0 and m[13] & 0x80 == 0
    with pytest.raises(ValueError):
        ax25.mask_cr_bits(b"x" * 13)


def test_ssid_masks_to_four_bits_and_reserved_bits_are_set():
    for ssid in range(16):
        b = ax25.encode_address("N0CALL", ssid, cr=False, last=True)
        assert b[6] & 0x60 == 0x60 and (b[6] >> 1) & 0x0F == ssid


# ------------------------------------------------------------------ start-up parameters

def test_startup_parameter_stream():
    want = bytes.fromhex("c0011ec0" "c0023fc0" "c0030ac0" "c00403c0" "c00500c0")
    assert ax25.dvrouter_startup_params(0) == want
    assert ax25.dvrouter_startup_params(1)[:4] == bytes.fromhex("c0111ec0")          # per channel: port nibble
    # no SetHardware (6), no Return (0xFF) in it
    frames = ax25.kiss_decode(want)
    assert [f[1] for f in frames] == [1, 2, 3, 4, 5]
    assert all(f[0] == 0 for f in frames)


def test_dire_wolf_logs_the_startup_parameters_it_is_sent():
    recs = {r["case"]: r for r in _load("kiss_tx.json")["records"]}
    sent = {ax25.kiss_decode(bytes.fromhex(r["sent_hex"]))[0][1]: r for n, r in recs.items() if n.startswith("param-") and "port1" not in n and "sethardware" not in n}
    # TXDELAY 0x1E (30), P 0x3F (63), SlotTime 0x0A (10): the same values Dire Wolf logged for these cases
    assert any("TXDELAY = 30" in m for m in recs["param-txdelay"]["dw_messages"])
    assert any("= 63" in m for m in recs["param-p"]["dw_messages"])
    assert any("= 10" in m for m in recs["param-slottime"]["dw_messages"])
    assert any("FullDuplex = 0" in m for m in recs["param-fullduplex-0"]["dw_messages"])
    assert sorted(sent) == [1, 2, 3, 4, 5]
    # none of the parameter frames is copied to the other clients
    for n in ("param-txdelay", "param-p", "param-slottime", "param-txtail", "param-fullduplex-0", "param-fullduplex-1"):
        assert recs[n]["dw_copy_decode"] == [], n
    assert recs["param-txtail"]["sent_hex"] == "c00405c0"               # the vector uses 5; DV-Router sends 3


# ------------------------------------------------------------------ KISS decoder model versus Dire Wolf

def _dw_taken(rec):
    """Frames Dire Wolf's KISS layer accepted as AX.25 (15..2123 bytes), from its own unwrapped dump."""
    return [bytes.fromhex(h) for h in rec["dw_unwrapped_hex"] if ax25.dw_accepts_len(len(h) // 2)]


def _model_taken(rec):
    m = ax25.DvrouterKiss()
    return [f for _port, f in m.feed(bytes.fromhex(rec["sent_hex"])) if ax25.dvrouter_accepts_frame(f)], m


# cases where the model of the lead's statements and Dire Wolf take different frames
DIFFERENT = {
    "frame-15-control-only": "15 bytes: Dire Wolf takes it, DV-Router needs 16 (a PID)",
    "info-2029": "a 2045 byte frame: Dire Wolf takes it, DV-Router's KISS limit is 1100",
    "truncated-then-frame": "after a closing FEND the next bytes: Dire Wolf calls them noise until another FEND, DV-Router starts a new frame (ANSWERED by the lead, kiss.rs)",
    "fesc-before-fend": "FESC directly before the FEND: both close the first frame; Dire Wolf then calls the next frame noise, DV-Router starts it",
    "data-port1": "port nibble 1: Dire Wolf's KISS layer takes the frame (and refuses to transmit it), DV-Router drops it silently (one configured channel)",
    "data-port2": "port nibble 2: as data-port1",
    "data-port15": "port nibble 15: as data-port1",
    "limit-1101-plain": "1101 data bytes: Dire Wolf takes it, DV-Router discards the frame (limit 1100 unescaped bytes)",
    "limit-1101-escapes": "1101 unescaped data bytes (20 escaped): Dire Wolf takes it, DV-Router discards it",
}


def test_model_and_dire_wolf_take_the_same_frames_except_the_documented_ones():
    recs = _load("kiss_tx.json")["records"]
    differ = {}
    for rec in recs:
        if rec.get("volatile"):
            continue
        mine, _m = _model_taken(rec)
        dw = _dw_taken(rec)
        if mine != dw:
            differ[rec["case"]] = (len(mine), len(dw))
    assert set(differ) == set(DIFFERENT), differ
    assert differ["frame-15-control-only"] == (0, 1)
    assert differ["info-2029"] == (0, 1)
    assert differ["truncated-then-frame"] == (2, 1)
    assert differ["fesc-before-fend"] == (2, 1)
    for name in ("data-port1", "data-port2", "data-port15", "limit-1101-plain", "limit-1101-escapes"):
        assert differ[name] == (0, 1), name


def test_model_counters_match_what_dire_wolf_logged():
    recs = {r["case"]: r for r in _load("kiss_tx.json")["records"]}
    _f, m = _model_taken(recs["bad-escape"])
    assert m.bad_escapes == 1 and any("Found 0x41 after FESC" in x for x in recs["bad-escape"]["dw_messages"])
    _f, m = _model_taken(recs["garbage-before"])
    assert m.dropped_noise == 2
    _f, m = _model_taken(recs["no-leading-fend"])
    assert m.dropped_noise == 39 and _f == []
    _f, m = _model_taken(recs["info-2029"])
    assert m.overlong == 1
    _f, m = _model_taken(recs["frame-empty"])
    assert _f == [] and m.ignored_commands == 0
    for name in ("param-txdelay", "param-p", "param-slottime", "param-txtail", "param-fullduplex-1", "param-sethardware", "cmd-return"):
        _f, m = _model_taken(recs[name])
        assert _f == [] and m.ignored_commands == 1, name
    for cmd in range(7, 15):
        _f, m = _model_taken(recs["cmd-unknown-%d" % cmd])
        assert _f == [] and m.ignored_commands == 1 and m.commands_7_to_14 == 1 and m.unspecified_commands == 1, cmd
        assert any("Invalid command %d" % cmd in x for x in recs["cmd-unknown-%d" % cmd]["dw_messages"]), cmd
        assert recs["cmd-unknown-%d" % cmd]["dw_copy_decode"] == []
    for name, port in (("data-port1", 1), ("data-port2", 2), ("data-port15", 15)):
        _f, m = _model_taken(recs[name])
        assert _f == [] and m.dropped_channel == 1 and m.ignored_commands == 0, name
        assert any("Invalid transmit channel %d" % port in x for x in recs[name]["dw_messages"]), name
    _f, m = _model_taken(recs["fesc-before-fend"])
    assert m.bad_escapes == 0 and len(_f) == 2           # FEND wins over a pending FESC, no bad escape counted


def test_model_streaming_and_limits():
    m = ax25.DvrouterKiss(max_data=10)
    assert m.feed(b"junk" + bytes([ax25.FEND, 0x00]) + b"abc") == []
    assert m.feed(bytes([ax25.FEND])) == [(0, b"abc")]
    assert m.dropped_noise == 4
    # over the limit: discarded up to the next FEND, then the stream goes on
    m = ax25.DvrouterKiss(max_data=10)
    out = m.feed(bytes([ax25.FEND, 0x00]) + b"x" * 11 + b"yy" + bytes([ax25.FEND, ax25.FEND, 0x00]) + b"ok" + bytes([ax25.FEND]))
    assert out == [(0, b"ok")] and m.overlong == 1
    # escapes
    m = ax25.DvrouterKiss()
    assert m.feed(bytes([ax25.FEND, 0x00, ax25.FESC, ax25.TFEND, ax25.FESC, ax25.TFESC, ax25.FESC, 0x41, 0x42, ax25.FEND])) == [(0, bytes([ax25.FEND, ax25.FESC, 0x42]))]
    assert m.bad_escapes == 1
    # the exact-limit frame is taken
    m = ax25.DvrouterKiss(max_data=10)
    assert m.feed(bytes([ax25.FEND, 0x00]) + b"x" * 10 + bytes([ax25.FEND])) == [(0, b"x" * 10)]


# ------------------------------------------------------------------ rules answered by the lead (kiss.rs, tcp.rs)

def _frame(total, specials=b""):
    n = total - 16
    return ax25.from_tnc2("N0CALL-7>APRS:" + ax25.info_to_text(specials + b"x" * (n - len(specials))))


def test_limit_counts_unescaped_bytes_after_the_type_byte():
    specials = bytes([ax25.FEND, ax25.FESC]) * 10
    for total, specials_, taken in ((1100, b"", True), (1101, b"", False), (1100, specials, True), (1101, specials, False)):
        frame = _frame(total, specials_)
        wire = ax25.kiss_encode(0, 0, frame)
        assert len(wire) == 1 + 1 + total + len(specials_) + 1
        m = ax25.DvrouterKiss()
        out = m.feed(wire)
        assert (out == [(0, frame)]) is taken, (total, len(specials_))
        assert m.overlong == (0 if taken else 1)
    # the model keeps listening after a discarded frame, and the FEND that reports it opens the next one
    m = ax25.DvrouterKiss()
    good = _frame(40)
    out = m.feed(ax25.kiss_encode(0, 0, _frame(1101)) + ax25.kiss_encode(0, 0, good)[1:])
    assert out == [(0, good)] and m.overlong == 1


def test_overlong_is_counted_at_the_closing_fend_only():
    m = ax25.DvrouterKiss()
    m.feed(bytes([ax25.FEND, 0x00]) + b"x" * 2000)
    assert m.overlong == 0                                   # no FEND yet: nothing reported
    m.feed(bytes([ax25.FEND]))
    assert m.overlong == 1


def test_every_fend_closes_and_opens():
    m = ax25.DvrouterKiss(channels=None)
    out = m.feed(bytes([ax25.FEND, 0x00]) + b"abc" + bytes([ax25.FEND, 0x10]) + b"def" + bytes([ax25.FEND]))
    assert out == [(0, b"abc"), (1, b"def")]
    m = ax25.DvrouterKiss(channels=None)
    assert m.feed(bytes([ax25.FEND, 0x00, ax25.FESC, ax25.FEND, 0x00]) + b"z" + bytes([ax25.FEND])) == [(0, b""), (0, b"z")]
    assert m.bad_escapes == 0


def test_command_nibbles_other_than_zero_are_ignored_and_other_ports_dropped():
    for cmd in range(1, 16):
        m = ax25.DvrouterKiss()
        assert m.feed(bytes([ax25.FEND, cmd, 0x01, ax25.FEND])) == []
        assert m.ignored_commands == 1 and m.commands_7_to_14 == (1 if 7 <= cmd <= 14 else 0)
    m = ax25.DvrouterKiss(channels=(0, 3))
    out = m.feed(bytes([ax25.FEND, 0x30, 0x41, ax25.FEND, 0x10, 0x42, ax25.FEND, 0x00, 0x43, ax25.FEND]))
    assert out == [(3, b"A"), (0, b"C")] and m.dropped_channel == 1
    # the whole byte 0xFF is port 15 command 15: ignored as a command, not as a channel
    m = ax25.DvrouterKiss()
    assert m.feed(bytes([ax25.FEND, 0xFF, ax25.FEND])) == [] and m.ignored_commands == 1 and m.dropped_channel == 0
