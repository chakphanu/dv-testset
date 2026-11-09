# SPDX-License-Identifier: MIT
"""dvtest.mmdvm_modem and dvtest.mmdvm_rf: the virtual MMDVM modem, offline.

The constants and offsets below are the format of the pinned MMDVMHost
(g4klx/MMDVMHost deb5efe6112863854f7fe7d5f94792d532feea35); every comment names
the file and line it was read from. The "host" in these tests is a few lines of
Python that write what CModem writes (frame start, length, type) and read the way
CModem::getResponse reads; no upstream code is used or copied.
"""
import os
import socket
import time

import pytest

from dvtest import mmdvm_modem as mm
from dvtest import mmdvm_rf as rf
from dvtest.audio import signals
from dvtest.proto import dmr as dmrp

pytestmark = pytest.mark.unit


class Host:
    """The host side of a UDP modem link: a bound socket, frames out, frames in."""

    def __init__(self, modem_addr):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2.0)
        self.modem = modem_addr
        self.parser = mm.FrameParser()
        self.inbox = []

    def write(self, ftype, payload=b""):
        self.sock.sendto(mm.frame(ftype, payload), self.modem)

    def read(self, want=None, timeout=2.0):
        """Next frame (of type `want`); other frames are kept in self.inbox."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for i, (t, p) in enumerate(self.inbox):
                if want is None or t == want:
                    del self.inbox[i]
                    return t, p
            self.sock.settimeout(max(0.05, end - time.monotonic()))
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                break
            assert addr == self.modem, "a reply must come from the address the host was told (UDPController.cpp:55-60)"
            self.inbox.extend(self.parser.feed(data))
        raise AssertionError("no frame %r from the modem" % want)

    def drain(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.sock.settimeout(0.05)
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            self.inbox.extend(self.parser.feed(data))

    def set_config_v2(self, modes=0x1F, colour_code=1, duplex=False):
        """40 bytes like CModem::setConfig2 (Modem.cpp:2313-2420): [3] flags (0x80 = simplex),
        [4] modes, [5] pocsag, [6] tx delay / 10, [7] mode, [29] colour code."""
        body = bytearray(37)
        body[0] = 0 if duplex else 0x80
        body[1] = modes
        body[3] = 10
        body[26] = colour_code
        self.write(mm.SET_CONFIG, bytes(body))


@pytest.fixture
def link():
    transport = mm.UdpTransport(("127.0.0.1", 0))
    modem = mm.VirtualModem(transport).start()
    host = Host(transport.sock.getsockname())
    yield modem, host
    modem.stop()
    host.sock.close()


# ------------------------------------------------------------------ framing

def test_frame_layout_and_parser():
    assert mm.FRAME_START == 0xE0                                   # Modem.cpp:46
    assert mm.frame(mm.GET_VERSION) == bytes((0xE0, 3, 0x00))       # readVersion Modem.cpp:2036-2040
    assert mm.frame(mm.SET_MODE, b"\x02") == bytes((0xE0, 4, 0x03, 2))   # setMode Modem.cpp:2683-2694
    assert (mm.GET_VERSION, mm.GET_STATUS, mm.SET_CONFIG, mm.SET_MODE, mm.SET_FREQ) == (0, 1, 2, 3, 4)   # Modem.cpp:48-52
    assert (mm.ACK, mm.NAK, mm.QSO_INFO) == (0x70, 0x7F, 0x91)      # Modem.cpp:101-105
    parser = mm.FrameParser()
    blob = mm.frame(0x11, b"\x01\x02") + b"\x00\x00" + mm.frame(0x13) + mm.frame(0x01)[:2]
    assert parser.feed(blob) == [(0x11, b"\x01\x02"), (0x13, b"")]   # garbage skipped, partial frame kept
    assert parser.feed(mm.frame(0x01)[2:]) == [(0x01, b"")]
    with pytest.raises(ValueError):
        mm.frame(0x20, bytes(253))                                   # one length byte only


def test_type_bytes_of_the_modes():
    # D-STAR 0x10-0x13, DMR slot 1/2 0x18-0x1B, YSF 0x20, P25 0x30/0x31, NXDN 0x40 (Modem.cpp:56-93)
    assert (rf.DSTAR_HEADER, rf.DSTAR_DATA, rf.DSTAR_EOT) == (0x10, 0x11, 0x13)
    assert (rf.DMR_DATA1, rf.DMR_DATA2, rf.YSF_DATA) == (0x18, 0x1A, 0x20)
    assert mm.MODE_OF_TYPE[0x30] == mm.MODE_OF_TYPE[0x31] == "p25"
    assert mm.MODE_OF_TYPE[0x40] == "nxdn"
    assert (mm.MODE_IDLE, mm.MODE_DSTAR, mm.MODE_DMR, mm.MODE_YSF, mm.MODE_P25, mm.MODE_NXDN) == (0, 1, 2, 3, 4, 5)  # Defines.h:31-36


# ------------------------------------------------------------------ handshake

def test_version_reply_is_protocol_2_and_names_dvtest(link):
    modem, host = link
    host.write(mm.GET_VERSION)
    ftype, payload = host.read(mm.GET_VERSION)
    # buffer[3] = protocol version, [4] caps1, [5] caps2, [6] CPU type, [7..22] UDID, [23..] text (Modem.cpp:2058-2070)
    buf = bytes((0xE0, 3 + len(payload), ftype)) + payload
    assert buf[3] == 2
    assert buf[4] == mm.CAP1_ALL == 0x1F and buf[5] == 0
    assert buf[6] in (0, 1, 2) and len(buf[7:23]) == 16
    assert b"dvtest" in buf[23:]
    assert modem.counts["version"] == 1


def test_status_reply_offsets_and_buffer_space(link):
    modem, host = link
    host.write(mm.GET_STATUS)
    ftype, payload = host.read(mm.GET_STATUS)
    assert ftype == mm.GET_STATUS and len(payload) == 12
    # version 2 status: [0] mode, [1] flags, [3] D-STAR, [4] DMR 1, [5] DMR 2, [6] YSF, [7] P25, [8] NXDN space (Modem.cpp:880-920)
    assert payload[0] == mm.MODE_IDLE and payload[1] == 0
    assert payload[3] >= 5                                           # the host writes a D-STAR header only with space > 4 (Modem.cpp:1055)
    assert min(payload[4], payload[5], payload[6], payload[7], payload[8]) > 1   # every other mode needs space > 1
    assert modem.counts["status"] == 1


def test_config_freq_mode_are_acked(link):
    modem, host = link
    host.write(mm.GET_VERSION)
    host.read(mm.GET_VERSION)
    host.write(mm.SET_FREQ, bytes(14))
    assert host.read(mm.ACK) == (mm.ACK, bytes((mm.SET_FREQ,)))
    host.set_config_v2(modes=0x1B, colour_code=3)
    assert host.read(mm.ACK) == (mm.ACK, bytes((mm.SET_CONFIG,)))
    assert modem.wait_configured(2)
    assert modem.config["modes"] == ["dstar", "dmr", "p25", "nxdn"]
    assert modem.config["colour_code"] == 3 and modem.config["duplex"] is False
    host.write(mm.SET_MODE, bytes((mm.MODE_DMR,)))
    assert host.read(mm.ACK) == (mm.ACK, bytes((mm.SET_MODE,)))
    host.write(mm.GET_STATUS)
    assert host.read(mm.GET_STATUS)[1][0] == mm.MODE_DMR


def test_unknown_command_is_nakked(link):
    modem, host = link
    host.write(0x55, b"\x01")
    assert host.read(mm.NAK) == (mm.NAK, bytes((0x55, mm.NAK_UNKNOWN_COMMAND)))   # NAK = command, reason (Modem.cpp:1000)


def test_pty_transport_handshake():
    try:
        transport = mm.PtyTransport()
    except OSError as exc:
        pytest.skip("no pseudo-terminal: %s" % exc)
    modem = mm.VirtualModem(transport).start()
    try:
        fd = os.open(transport.path, os.O_RDWR | os.O_NOCTTY)
        os.write(fd, mm.frame(mm.GET_VERSION))
        parser = mm.FrameParser()
        got = []
        end = time.monotonic() + 3
        while not got and time.monotonic() < end:
            os.set_blocking(fd, False)
            try:
                got = parser.feed(os.read(fd, 256))
            except BlockingIOError:
                time.sleep(0.02)
        os.close(fd)
        assert got and got[0][0] == mm.GET_VERSION and got[0][1][0] == 2
    finally:
        modem.stop()


# ------------------------------------------------------------------ buffer space

def test_space_follows_the_fill_of_the_buffer():
    modem = mm.VirtualModem(mm.UdpTransport(("127.0.0.1", 0)))
    try:
        t0 = 100.0
        assert modem.space("dstar", t0) == mm.CAPACITY["dstar"]
        modem._enqueue(rf.DSTAR_HEADER, t0)          # a header takes 80 ms of air = 4 frames
        modem._enqueue(rf.DSTAR_DATA, t0)
        assert modem.space("dstar", t0) == mm.CAPACITY["dstar"] - 5
        assert modem.space("dstar", t0 + 1.0) == mm.CAPACITY["dstar"]   # played out
        for _ in range(4):
            modem._enqueue(rf.DMR_DATA2, t0)
        assert modem.space("dmr2", t0) == mm.CAPACITY["dmr2"] - 4 and modem.space("dmr1", t0) == mm.CAPACITY["dmr1"]
        flags = modem.status_payload(t0)[1]
        assert flags & mm.FLAG_TX
        assert not modem.status_payload(t0 + 5)[1] & mm.FLAG_TX
    finally:
        modem.stop()


# ------------------------------------------------------------------ RF builders

def test_dstar_header_and_superframe():
    frames = signals.prng_frames("dstar", 50, seed="t1")
    air = rf.dstar_keyup(frames, my="N0CALL")
    assert [t for t, _, _ in air].count(rf.DSTAR_HEADER) == 1 and air[-1][0] == rf.DSTAR_EOT
    header = rf.parse_dstar_header(air[0][1])
    assert len(air[0][1]) == rf.DSTAR_HEADER_LEN == 41                # DStarDefines.h:23
    assert header["crc_ok"] and header["my"] == "N0CALL  " and header["rpt1"] == "N0CALL B" and header["rpt2"] == "N0CALL G"
    assert header["ur"] == "CQCQCQ  " and header["flags"][0] & 0x40   # repeater flag, DStarDefines.h:57 / DStarHeader.cpp:50
    voice = [p for t, p, _ in air if t == rf.DSTAR_DATA]
    assert len(voice) == 50 and all(len(p) == 12 for p in voice)       # DStarDefines.h:24
    assert [p[9:] for p in voice[::21]] == [rf.DSTAR_SYNC] * 3          # frame 0, 21, 42 carry the sync (DStarControl.cpp:376)
    assert all(p[9:] == rf.DSTAR_NULL_SLOW for i, p in enumerate(voice) if i % 21)
    assert [p[:9] for p in voice] == rf.canonical_frames("dstar", frames)


def test_dstar_ambe_is_a_fixed_point_and_keeps_data_bits():
    frames = signals.prng_frames("dstar", 30, seed="t2")
    for f in frames:
        c = rf.dstar_ambe(f)
        assert rf.dstar_ambe(c) == c
    # a word that is already valid is unchanged: the AMBE null word of DStarDefines.h:36 (descrambled form)
    assert rf.dstar_ambe(rf.DSTAR_NULL_AMBE) == rf.DSTAR_NULL_AMBE


def test_dmr_lc_burst_roundtrip_and_rs_parity():
    lc = dmrp.full_lc(dmrp.FLCO_GROUP, 1023007, 6)
    for dt in (1, 2):
        burst = rf.data_burst(lc, dt, colour_code=1)
        assert len(burst) == 33                                         # DMRDefines.h:22
        info = rf.parse_data_burst(burst)
        assert (info["cc"], info["dt"], info["slot_ok"], info["rs_ok"]) == (1, dt, True, True)
        assert dmrp.parse_full_lc(info["lc"]) == {"pf": 0, "flco": 0, "fid": 0, "options": 0, "dst": 6, "src": 1023007}
        assert rf.classify_burst(burst) == "data"
    # RS (12,9): generator x^3 + 14 x^2 + 56 x + 64 (RS129.cpp:28), parity of an all-zero LC is zero
    assert rf.rs129_parity(bytes(9)) == bytes(3)
    # the masks of the two types differ (DMRDefines.h:70-71)
    assert rf.data_burst(lc, 1) != rf.data_burst(lc, 2)
    # BPTC: every information bit comes back, parity bits satisfy the row checks
    info12 = bytes(range(1, 13))
    assert rf.bptc196_decode(rf.bptc196_encode(info12)) == info12


def test_dmr_keyup_sequence_markers_and_voice():
    frames = signals.prng_frames("dmr", 40, seed="t3")
    air = rf.dmr_keyup(frames, 1023007, 6, slot=2)
    assert {t for t, _, _ in air} == {rf.DMR_DATA2} and all(d == 60.0 for _, _, d in air)
    firsts = [p[0] for _, p, _ in air]
    # header 0x40|1, then A (0x20), B..F 1..5, A ..., terminator 0x40|2 (DMRSlot.cpp:256, 644-690; Modem.cpp:785)
    assert firsts[0] == 0x41 and firsts[-1] == 0x42
    assert firsts[1:-1] == ([0x20, 1, 2, 3, 4, 5] * 3)[:14]            # 40 frames -> 14 bursts
    got = rf.decode_dmr([(t, p) for t, p, _ in air])[2]
    assert [h["src"] for h in got["headers"]] == [1023007] and got["headers"][0]["dst"] == 6 and got["headers"][0]["rs_ok"]
    assert len(got["terminators"]) == 1 and got["voice_bursts"] == 14
    assert got["voice"] == rf.padded_frames("dmr", frames) and len(got["voice"]) == 42
    # slot 1 uses 0x18
    assert {t for t, _, _ in rf.dmr_keyup(frames[:3], 1, 2, slot=1)} == {rf.DMR_DATA1}


def test_dmr_embedded_lc_is_in_bursts_b_to_e():
    frames = signals.prng_frames("dmr", 18, seed="t4")
    air = rf.dmr_keyup(frames, 1023007, 6)
    bursts = [p[1:] for _, p, _ in air[1:-1]]
    sf = dmrp.superframe_lc(bursts[1:5])
    assert sf["fec_ok"] and sf["checksum_ok"] and sf["lc"]["src"] == 1023007 and sf["lc"]["dst"] == 6


def test_ysf_keyup_header_voice_terminator():
    frames = signals.prng_frames("dmr", 20, seed="t5")
    air = rf.ysf_keyup(frames, "N0CALL", "ALL", dgid=12)
    assert len(air) == 2 + 4 and {t for t, _, _ in air} == {rf.YSF_DATA}
    assert all(len(p) == 1 + 120 and p[0] == 0 for _, p, _ in air)       # YSFControl.cpp:150-165: tag, 0x00, frame
    got = rf.decode_ysf([(t, p) for t, p, _ in air])
    assert got["headers"] == [{"dest": "ALL       ", "src": "N0CALL    ", "dgid": 12}]
    assert len(got["terminators"]) == 1 and got["voice"] == rf.padded_frames("ysf", frames)
    assert [f["fn"] for f in got["fich"][1:-1]] == [0, 1, 2, 3]


def test_canonical_and_padded_frames_per_mode():
    assert len(rf.padded_frames("dmr", [bytes(9)] * 4)) == 6
    assert len(rf.padded_frames("ysf", [bytes(9)] * 7)) == 10
    assert len(rf.padded_frames("dstar", [bytes(9)] * 7)) == 7
    assert rf.FRAME_WIDTH == {"dstar": 9, "dmr": 9, "ysf": 9, "p25": 11, "nxdn": 7}
    f = mm.default_frames("p25", 3)
    assert [len(x) for x in f] == [11, 11, 11] and f == mm.default_frames("p25", 3)


def test_dstar_dtmf_like_words_are_avoided():
    """DStarGateway blanks a word that looks like a DTMF key to the null AMBE word (first run of MMDVM1 dstar: voice word 82 of 150,
    seed "mmdvm", arrived as 9e8d3288261a3f61e8). default_frames replaces such words and leaves every other one as it was."""
    seen_bad = 0
    for seed in ("mmdvm", "down", "a", "b", "c", "d"):
        raw = signals.prng_bytes("%s/dstar" % seed, 9 * 400)
        raw = [raw[i:i + 9] for i in range(0, len(raw), 9)]
        frames = mm.default_frames("dstar", 400, seed)
        assert len(frames) == 400 and frames == mm.default_frames("dstar", 400, seed)
        for old, new in zip(raw, frames):
            if rf.dstar_dtmf_like(rf.dstar_ambe(old)):
                seen_bad += 1
                assert new != old and len(new) == 9
            else:
                assert new == old
            assert not rf.dstar_dtmf_like(rf.dstar_ambe(new))
    assert seen_bad >= 1                       # the seeds above do contain such words (about 1 in 256)
    first = mm.default_frames("dstar", 150, "mmdvm")
    assert not any(rf.dstar_dtmf_like(w) for w in rf.canonical_frames("dstar", first))
    # the word of the first run: valid form dae8bcabee7f58741d is the key "2" by the masks of DStarBase/DTMF.h
    assert rf.dstar_dtmf_like(bytes.fromhex("dae8bcabee7f58741d"))
    assert rf.dstar_ambe(bytes.fromhex("8aada8abaf7b593409")) == bytes.fromhex("dae8bcabee7f58741d")
    assert first[82] != bytes.fromhex("8aada8abaf7b593409")
    assert not rf.dstar_dtmf_like(rf.DSTAR_NULL_AMBE) and not rf.dstar_dtmf_like(bytes(9)) and not rf.dstar_dtmf_like(b"short")


# ------------------------------------------------------------------ key-up over the wire and recording

@pytest.mark.parametrize("mode,kw", [
    ("dstar", {"my": "N0CALL"}),
    ("dmr", {"src": 1023007, "dst": 6}),
    ("ysf", {"src": "N0CALL"}),
    ("p25", {"src": 1023007, "dst": 31672}),
    ("nxdn", {"src": 1001, "dst": 31672}),
])
def test_keyup_reaches_the_host_in_real_time_and_decodes(link, mode, kw):
    modem, host = link
    host.write(mm.GET_VERSION)       # the host announces itself: the modem learns the peer address
    host.read(mm.GET_VERSION)
    ku = modem.keyup(mode, seconds=0.4, **kw)
    assert ku.wait(15)
    host.drain(0.3)
    frames = [(t, p) for t, p in host.inbox]
    assert len(frames) == len(ku.frames)
    assert [(t, p) for t, p, _ in ku.frames] == frames
    assert ku.voice_count == 20 and len(ku.voice) >= 20
    air_s = sum(d for _, _, d in ku.frames[:-1]) / 1000.0
    assert ku.duration_s >= air_s * 0.9                              # played in real time, not at once
    assert [t for t, _ in ku.sent] == sorted(t for t, _ in ku.sent)


@pytest.mark.parametrize("mode,kw", [
    ("dstar", {"my": "N0CALL"}),
    ("dmr", {"src": 1023007, "dst": 6}),
    ("ysf", {"src": "N0CALL"}),
    ("p25", {"src": 1023007, "dst": 31672}),
    ("nxdn", {"src": 1001, "dst": 31672}),
])
def test_recording_per_mode_and_decode_of_what_the_host_writes(link, mode, kw):
    """The host writes the same shapes down that a radio sends up; the recording keeps
    them with timestamps per mode and the decoder returns identity and voice."""
    modem, host = link
    air, voice, _ = modem.build_keyup(mode, seconds=0.4, **kw)
    t0 = time.monotonic()
    for ftype, payload, _ in air:
        host.write(ftype, payload)
    host.write(mm.GET_STATUS)
    host.read(mm.GET_STATUS)
    rows = modem.wait_frames(mode, len(air) if mode != "dmr" else len(air), timeout=3, since=t0)
    assert len(rows) == len(air)
    assert all(r.mode == mode and r.t > 1e9 and r.mono >= t0 for r in rows)
    assert [r.mono for r in rows] == sorted(r.mono for r in rows)
    down = modem.downlink(mode, since=t0)
    if mode == "dstar":
        assert down["headers"][0]["my"].strip() == "N0CALL" and down["headers"][0]["crc_ok"] and down["eot"] == 1
        assert down["voice"] == voice
    elif mode == "dmr":
        d = down[2]
        assert d["headers"][0]["src"] == 1023007 and d["headers"][0]["dst"] == 6 and d["terminators"] and d["voice"] == voice
    elif mode == "ysf":
        assert down["headers"][0]["src"].strip() == "N0CALL" and down["terminators"] and down["voice"] == voice
    elif mode == "p25":
        assert (down["src"], down["dst"], down["group"], down["terminated"]) == (1023007, 31672, True, True)
        assert down["voice"] == voice
    else:
        assert (down["src"], down["dst"], down["group"], down["terminated"]) == (1001, 31672, True, True)
        assert down["voice"] == voice
    modem.clear()
    assert modem.recording() == []


def test_a_status_poll_in_the_middle_of_a_keyup_is_answered(link):
    modem, host = link
    host.write(mm.GET_VERSION)
    host.read(mm.GET_VERSION)
    ku = modem.keyup("dstar", seconds=0.5, my="N0CALL")
    time.sleep(0.15)
    host.write(mm.GET_STATUS)
    ftype, payload = host.read(mm.GET_STATUS)
    assert ftype == mm.GET_STATUS and payload[1] & mm.FLAG_CD         # carrier detect while the radio transmits
    ku.wait(5)
    host.write(mm.GET_STATUS)
    host.drain(0.2)
    assert not [p for t, p in host.inbox if t == mm.GET_STATUS][-1][1] & mm.FLAG_CD


def test_keyup_of_an_unknown_mode_says_why():
    modem = mm.VirtualModem(mm.UdpTransport(("127.0.0.1", 0)))
    try:
        with pytest.raises(ValueError, match="no M17"):
            modem.build_keyup("m17", seconds=1)
    finally:
        modem.stop()
