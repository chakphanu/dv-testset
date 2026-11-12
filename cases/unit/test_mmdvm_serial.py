# SPDX-License-Identifier: MIT
"""dvtest.mmdvm_serial: the MMDVM envelope (both length forms), the two setup frames as DV-Router's document gives them, and the
fake modem for DV-Router's `mmdvm-serial` adapter, offline over loopback UDP (docs/rif5-cases.md).

The "edge" in these tests is a few lines of Python that write what the adapter writes (radio.md 7.1: GET_VERSION, SET_FREQ, SET_CONFIG,
GET_STATUS, SET_MODE, native frames) and read the way it reads; no DV-Router code is used. The two D-STAR literals are the hand-written
wire literals of DV-Router's own test (crates/router/tests/real_children/radio/mmdvm.rs RF_HEADER and TX_HEADER, at c017e491, checked there
against MMDVMHost DStarHeader.cpp:88-151), used here as an oracle for the encoders of dvtest.mmdvm_rf.
"""
import socket
import time

import pytest

from dvtest import mmdvm_modem as mm
from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_serial as ms

pytestmark = pytest.mark.unit

RF_HEADER = b"\xe0\x2c\x10\x40\x00\x00N0CALL GN0CALL BCQCQCQ  N0CALL      \x31\x43"
TX_HEADER = b"\xe0\x2c\x10\x00\x00\x00N0CALL BN0CALL GCQCQCQ  N0CALL      \x99\x86"


# ------------------------------------------------------------------ the envelope

def test_short_and_long_forms():
    assert ms.encode_frame(ms.GET_VERSION) == bytes((0xE0, 3, 0))
    assert ms.encode_frame(ms.SET_MODE, b"\x01") == bytes((0xE0, 4, 3, 1))
    assert ms.encode_frame(0x20, bytes(252)) == bytes((0xE0, 255, 0x20)) + bytes(252)          # the longest short frame
    long_ = ms.encode_frame(0x20, bytes(253))                                                  # radio.md 7.1: total 255 + L2
    assert long_[:4] == bytes((0xE0, 0, 2, 0x20)) and len(long_) == 257
    assert ms.encode_frame(0x20, bytes(506))[:4] == bytes((0xE0, 0, 255, 0x20)) and len(ms.encode_frame(0x20, bytes(506))) == 510
    with pytest.raises(ValueError):
        ms.encode_frame(0x20, bytes(507))
    # the short form agrees with the modem module the cases already use
    assert ms.encode_frame(0x11, b"\x01\x02") == mm.frame(0x11, b"\x01\x02")


def test_reader_both_forms_partial_reads_and_resync():
    blob = ms.encode_frame(0x10, bytes(41)) + b"\x00\x01" + ms.encode_frame(0x20, bytes(300)) + ms.encode_frame(ms.GET_STATUS)
    reader = ms.FrameReader()
    out = []
    for i in range(0, len(blob), 7):                                  # arbitrary partial reads
        out += reader.feed(blob[i:i + 7])
    assert [(f.ftype, len(f.payload)) for f in out] == [(0x10, 41), (0x20, 300), (1, 0)]
    assert out[1].raw == ms.encode_frame(0x20, bytes(300))
    assert reader.resyncs == 2                                        # the two stray bytes
    # invalid lengths 1 and 2 and a zero extension drop the E0 and rescan (radio.md 7.1)
    reader = ms.FrameReader()
    out = reader.feed(b"\xe0\x01" + b"\xe0\x02" + b"\xe0\x00\x00" + ms.encode_frame(ms.GET_STATUS))
    assert [(f.ftype, f.payload) for f in out] == [(1, b"")]
    assert reader.resyncs >= 3


def test_dv_router_literals_parse_and_agree_with_the_rf_encoders():
    f = ms.FrameReader().feed(RF_HEADER)[0]
    assert f.ftype == ms.DSTAR_HEADER and len(f.payload) == 41
    info = rf.parse_dstar_header(f.payload)
    assert info["crc_ok"] and info["rpt1"] == "N0CALL B" and info["rpt2"] == "N0CALL G" and info["flags"] == (0x40, 0, 0)
    # an RF over of the modem (the repeater flag set, RPT1 = station + RF module, RPT2 = station + G): the same bytes
    assert ms.encode_frame(rf.DSTAR_HEADER, rf.dstar_header("N0CALL", rpt1="N0CALL B", rpt2="N0CALL G", ur="CQCQCQ")) == RF_HEADER
    # the transmit header the adapter writes: flags clear, RPT2 = station + module first, RPT1 = station + G
    tx = ms.FrameReader().feed(TX_HEADER)[0]
    info = rf.parse_dstar_header(tx.payload)
    assert info["crc_ok"] and info["flags"] == (0, 0, 0) and info["rpt2"] == "N0CALL B" and info["rpt1"] == "N0CALL G"
    assert ms.encode_frame(rf.DSTAR_HEADER, rf.dstar_header("N0CALL", rpt1="N0CALL G", rpt2="N0CALL B", ur="CQCQCQ", flags=(0, 0, 0))) == TX_HEADER


# ------------------------------------------------------------------ the setup frames

def test_set_freq_layout():
    frame = ms.expected_set_freq(434_000_000, 434_100_000, 100)
    assert frame[:4] == bytes((0xE0, 0x11, 0x04, 0x00)) and len(frame) == 17
    info = ms.parse_set_freq(frame[3:])
    assert info == {"zero": 0, "rx_hz": 434_000_000, "tx_hz": 434_100_000, "rf_level": 255, "pocsag_hz": 434_100_000}
    assert ms.parse_set_freq(b"short") is None
    assert ms.pct(50) == 128 and ms.pct(100) == 255 and ms.pct(0) == 0 and ms.dc(0) == 128 and ms.dc(-128) == 0


def build_v2(**kw):
    """A 40 byte v2 SET_CONFIG written from the document's offsets (the bytes the document gives no value for take the TX level)."""
    want = ms.expected_set_config_v2(**kw)
    frame = bytearray(40)
    frame[0], frame[1], frame[2] = 0xE0, 40, ms.SET_CONFIG
    for at, value in want.items():
        frame[at] = value
    base = ms.pct(kw.get("tx_level", 50))
    for at in (15, 16, 18, 19):
        frame[at] = base
    return bytes(frame)


def test_set_config_v2_offsets_and_comparison():
    frame = build_v2()
    assert frame[3] == 0x80 and frame[4] == 7 and frame[6] == 10 and frame[8] == frame[9] == 128 and frame[29] == 1 and frame[31] == 0x80
    assert ms.set_config_differences(frame, ms.expected_set_config_v2()) == []
    info = ms.parse_set_config_v2(frame[3:])
    assert info["simplex"] and info["modes"] == ["dstar", "dmr", "ysf"] and info["tx_delay_ms"] == 100 and info["color_code"] == 1
    # levels per mode, colour code, flags
    frame = build_v2(modes=("dstar",), tx_delay_ms=40, dmr_tx_level=10, ysf_tx_level=90, color_code=7, ysf_lo_dev=True, rx_dc=-5)
    info = ms.parse_set_config_v2(frame[3:])
    assert info["modes"] == ["dstar"] and info["dmr_level"] == 26 and info["ysf_level"] == 230 and info["color_code"] == 7
    assert frame[3] & 0x88 == 0x88 and info["rx_dc"] == 123
    # a wrong byte is reported with its offset
    broken = bytearray(frame)
    broken[29] = 3
    assert ms.set_config_differences(bytes(broken), ms.expected_set_config_v2(modes=("dstar",), tx_delay_ms=40, dmr_tx_level=10,
                                                                               ysf_tx_level=90, color_code=7, ysf_lo_dev=True, rx_dc=-5)) == [(29, 3, 7)]
    assert ms.parse_set_config_v2(b"\x80" * 22) is None            # the v1 length is not v2


# ------------------------------------------------------------------ the fake modem against a host that speaks the adapter's dialogue

class Edge:
    """The adapter's side of a UDP byte port: a socket bound to the modem's peer address."""

    def __init__(self, modem_addr):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.addr = self.sock.getsockname()
        self.modem = modem_addr
        self.reader = ms.FrameReader()
        self.inbox = []

    def write(self, ftype, payload=b""):
        self.sock.sendto(ms.encode_frame(ftype, payload), self.modem)

    def read(self, want=None, timeout=1.5):
        end = time.monotonic() + timeout
        while True:
            for i, f in enumerate(self.inbox):
                if want is None or f.ftype == want:
                    return self.inbox.pop(i)
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                return None
            assert addr == self.modem
            self.inbox.extend(self.reader.feed(data))

    def open(self):
        self.write(ms.GET_VERSION)
        version = self.read(ms.GET_VERSION)
        self.sock.sendto(ms.expected_set_freq(434_000_000, 434_000_000), self.modem)
        assert self.read(ms.ACK).payload == bytes((ms.SET_FREQ,))
        self.sock.sendto(build_v2(), self.modem)
        assert self.read(ms.ACK).payload == bytes((ms.SET_CONFIG,))
        self.write(ms.GET_STATUS)
        return version, self.read(ms.GET_STATUS)


@pytest.fixture
def link():
    edge_holder = {}
    transport = ms.PinnedUdp(("127.0.0.1", 0), ("127.0.0.1", 1))        # the peer is replaced below, once the edge socket exists
    edge = Edge(transport.sock.getsockname())
    transport.peer = edge.addr
    modem = ms.Rif5Modem(transport).start()
    edge_holder["edge"] = edge
    yield modem, edge
    modem.stop()
    edge.sock.close()


def status_fields(payload):
    """The fields the adapter reads from a version 2 status payload (radio.md 7.1 and core status.rs offsets): mode 0, flags 1, spaces 3 / 5 / 6."""
    return {"mode": payload[0], "flags": payload[1], "dstar": payload[3], "dmr": payload[5], "ysf": payload[6]}


def test_open_dialogue_answers(link):
    modem, edge = link
    version, status = edge.open()
    assert version.payload[0] == 2 and version.payload[1] == 7 and len(version.payload) >= 20          # v2, D-STAR | DMR | YSF, >= 20 B
    assert version.payload[20:].decode() == "dvtest RIF5 modem 1.0 (no radio)"
    assert len(status.payload) == ms.STATUS_V2_MIN                                                       # a valid v2 status is 17 bytes
    s = status_fields(status.payload)
    assert s["mode"] == 0 and s["flags"] == 0 and s["dstar"] == mm.CAPACITY["dstar"] and s["dmr"] == mm.CAPACITY["dmr2"]
    assert modem.configured.is_set() and modem.counts["freq"] == 1 and modem.counts["config"] == 1
    # the log holds every frame of the dialogue, control frames included, in order
    assert [f.ftype for f in modem.host_frames()] == [ms.GET_VERSION, ms.SET_FREQ, ms.SET_CONFIG, ms.GET_STATUS]
    assert modem.host_frames()[1].payload == ms.expected_set_freq(434_000_000, 434_000_000)[3:]


def test_modes_buffers_and_naks(link):
    modem, edge = link
    edge.open()
    edge.write(ms.SET_MODE, b"\x01")
    assert edge.read(ms.ACK).payload == bytes((ms.SET_MODE,)) and modem.mode == ms.MODE_DSTAR
    edge.write(ms.SET_MODE, b"\x05")                                            # not a mode of this modem
    assert edge.read(ms.NAK).payload == bytes((ms.SET_MODE, ms.NAK_BAD_VALUE))
    edge.write(ms.DSTAR_HEADER, rf.dstar_header("N0TEST", rpt1="N0CALL G", rpt2="N0CALL B", flags=(0, 0, 0)))
    edge.write(ms.GET_STATUS)
    s = status_fields(edge.read(ms.GET_STATUS).payload)
    assert s["mode"] == 1 and s["flags"] & 1 and s["dstar"] <= mm.CAPACITY["dstar"] - 4                  # the header costs four, the TX flag is up
    edge.write(ms.DMR_DATA2, bytes(34))                                         # another mode while D-STAR holds the modem
    assert edge.read(ms.NAK).payload == bytes((ms.DMR_DATA2, ms.NAK_NOT_ACCEPTED))
    edge.write(ms.SEND_CWID, b"N0CALL")                                         # not from a DV mode
    assert edge.read(ms.NAK).payload == bytes((ms.SEND_CWID, ms.NAK_FULL))
    edge.write(0x55)
    assert edge.read(ms.NAK).payload == bytes((0x55, ms.NAK_UNKNOWN))
    assert [n[1:] for n in modem.naks] == [(ms.SET_MODE, 4), (ms.DMR_DATA2, 2), (ms.SEND_CWID, 5), (0x55, 1)]
    # the voice frames are in the per-mode recording the decoders read
    assert [t for t, _ in modem.pairs("dstar")] == [ms.DSTAR_HEADER]


def test_a_frame_in_idle_selects_its_mode_and_a_disabled_mode_is_refused(link):
    modem, edge = link
    edge.open()
    edge.write(ms.YSF_DATA, bytes(121))
    edge.write(ms.GET_STATUS)
    assert status_fields(edge.read(ms.GET_STATUS).payload)["mode"] == ms.MODE_YSF
    edge.write(ms.SET_MODE, b"\x00")
    edge.read(ms.ACK)
    # a SET_CONFIG that enables D-STAR only: DMR is now refused (radio.md 7.1: a disabled mode is NAK 2)
    frame = bytearray(build_v2(modes=("dstar",)))
    edge.sock.sendto(bytes(frame), edge.modem)
    edge.read(ms.ACK)
    edge.write(ms.DMR_DATA2, bytes(34))
    assert edge.read(ms.NAK).payload == bytes((ms.DMR_DATA2, ms.NAK_NOT_ACCEPTED))


def test_cw_id_in_idle_keys_the_modem_without_an_answer(link):
    modem, edge = link
    edge.open()
    edge.write(ms.SEND_CWID, b"N0CALL")
    edge.write(ms.GET_STATUS)
    assert edge.read(ms.NAK, timeout=0.3) is None
    assert status_fields(edge.read(ms.GET_STATUS).payload)["flags"] & 1                                   # TX for the 89 bits of 50 ms
    assert ms.cw_bits("N0CALL") == 89 and ms.cw_bits(b"N0CALL") == 89


def test_silence_and_a_short_status(link):
    modem, edge = link
    edge.open()
    modem.status_mode = "short"
    edge.write(ms.GET_STATUS)
    assert len(edge.read(ms.GET_STATUS).payload) == 10                                                    # bytes flow, no valid v2 status
    modem.status_mode = "none"
    edge.write(ms.GET_STATUS)
    assert edge.read(ms.GET_STATUS, timeout=0.3) is None
    modem.status_mode = "valid"
    modem.mute = True
    edge.write(ms.GET_STATUS)
    edge.write(ms.GET_VERSION)
    assert edge.read(timeout=0.3) is None
    assert [f.ftype for f in modem.host_frames()][-2:] == [ms.GET_STATUS, ms.GET_VERSION]              # logged although unanswered
    modem.mute = False
    edge.write(ms.GET_STATUS)
    assert len(edge.read(ms.GET_STATUS).payload) == ms.STATUS_V2_MIN


def test_keyup_reaches_the_edge_as_framed_native_frames(link):
    modem, edge = link
    edge.open()
    ku = modem.keyup("dstar", seconds=0.4, my="N0CALL")
    assert modem.mode == ms.MODE_DSTAR                                                                    # the firmware selects the mode it decodes
    assert ku.wait(10) and ku.error is None
    frames = []
    while True:
        f = edge.read(timeout=0.3)
        if f is None:
            break
        frames.append(f)
    assert [f.ftype for f in frames][0] == ms.DSTAR_HEADER and frames[-1].ftype == ms.DSTAR_EOT
    assert sum(1 for f in frames if f.ftype == ms.DSTAR_DATA) == 20 and frames[-1].payload == b""
    assert frames[0].raw[:3] == bytes((0xE0, 44, 0x10)) and frames[1].raw[:3] == bytes((0xE0, 15, 0x11))
    # the carrier-detect flag is up while the over plays
    assert status_fields(modem.status_payload())["flags"] == 0                                           # ... and down after it


def test_foreign_datagrams_are_dropped_and_the_peer_is_not_learned():
    transport = ms.PinnedUdp(("127.0.0.1", 0), ("127.0.0.1", 9))
    modem = ms.Rif5Modem(transport).start()
    try:
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        other.bind(("127.0.0.1", 0))
        other.sendto(ms.encode_frame(ms.GET_VERSION), transport.sock.getsockname())
        deadline = time.monotonic() + 1.0
        while transport.foreign == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert transport.foreign == 1 and modem.host_frames() == [] and transport.peer == ("127.0.0.1", 9)
        other.close()
    finally:
        modem.stop()
