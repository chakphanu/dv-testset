# SPDX-License-Identifier: MIT
"""dvtest.mmdvm_fw: the firmware-like TX model of the RIF5 fake (DVT_RIF5FW, design-276 1.2 and 1.9) in virtual time. Offline."""
import pytest

from dvtest import mmdvm_fw as fw
from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_serial as ms

pytestmark = pytest.mark.unit

HEADER = rf.dstar_header("N0TEST", rpt1="N0CALL G", rpt2="N0CALL B", ur="CQCQCQ", flags=(0, 0, 0))
DATA = bytes(12)


def model(profile="mmdvm", delay=100):
    log = []
    m = fw.FwModel(profile, delay, lambda t, event, **detail: log.append((round(t, 4), event, detail)))
    return m, log


def names(log, *which):
    return [e for e in log if e[1] in which]


def test_idle_space_and_airtime_numbers_of_the_two_boards():
    m, _ = model("mmdvm")
    assert [m.space(k, 0.0) for k in ("dstar", "dmr1", "dmr2", "ysf")] == [28, 10, 10, 33]            # design-276 1.2
    h, _ = model("hs")
    assert [h.space(k, 0.0) for k in ("dstar", "dmr1", "dmr2", "ysf")] == [76, 25, 25, 12]
    assert fw.airtime_s(ms.DSTAR_HEADER) == pytest.approx(0.1417) and fw.airtime_s(ms.DSTAR_EOT) == 0.030
    assert (fw.stored_bytes(ms.DSTAR_HEADER), fw.stored_bytes(ms.DSTAR_DATA), fw.stored_bytes(ms.DSTAR_EOT)) == (42, 13, 1)
    assert (fw.stored_bytes(ms.DMR_DATA2), fw.stored_bytes(ms.YSF_DATA)) == (33, 120)


def test_preamble_follows_the_board_and_tx_delay_and_is_capped_at_one_second():
    for delay, want in ((0, 0.5), (100, 0.6), (500, 1.0)):
        m, _ = model("mmdvm", delay)
        assert m.preamble_s("dstar", ms.DSTAR_HEADER) == pytest.approx(want)
        assert m.preamble_s("dmr2", ms.DMR_DATA2) == pytest.approx(want) and m.preamble_s("ysf", ms.YSF_DATA) == pytest.approx(want)
    hs, _ = model("hs", 100)
    assert hs.preamble_s("dstar", ms.DSTAR_HEADER) == pytest.approx(0.35)                         # 250 ms + tx delay
    assert fw.FwModel("mmdvm", 900).delay_ms() == 500                                              # the SET_CONFIG range is 0-500 ms
    assert model()[0].preamble_s("dstar", ms.DSTAR_DATA) == 0.0                                    # D-STAR data: none


def test_the_fifo_is_not_consumed_during_the_preamble_and_a_frame_leaves_when_its_playout_starts():
    m, log = model("mmdvm", 100)
    assert m.write(ms.DSTAR_HEADER, HEADER, 10.0)
    for i in range(3):
        assert m.write(ms.DSTAR_DATA, DATA, 10.0 + 0.01 * i)
    assert m.fifo_bytes()["dstar"] == 42 + 3 * 13 and m.space("dstar", 10.3) == (370 - 81) // 13       # preamble 0.6 s: nothing played yet
    assert m.tx_on(10.3) and names(log, "preamble_start")[0][2]["duration_ms"] == 600.0
    assert m.space("dstar", 10.59) == (370 - 81) // 13
    # 10.6: the preamble ends, the header leaves the FIFO and airs 141.7 ms; one data frame leaves per 20 ms after it
    assert m.space("dstar", 10.61) == (370 - 39) // 13 and m.fifo_bytes()["dstar"] == 39
    assert m.space("dstar", 10.6 + 0.1417 + 0.001) == (370 - 26) // 13                             # first data frame popped
    assert m.space("dstar", 10.6 + 0.1417 + 0.041) == 28                                          # all three popped
    pops = [e for e in names(log, "pop")]
    assert [round(e[0], 3) for e in pops] == [10.6, 10.742, 10.762, 10.782]


def test_the_firmware_nak_five_when_a_frame_does_not_fit():
    m, log = model("mmdvm", 100)
    assert m.write(ms.DSTAR_HEADER, HEADER, 0.0)
    accepted = sum(m.write(ms.DSTAR_DATA, DATA, 0.001 * i) for i in range(40))
    assert accepted == (370 - 42) // 13 == 25 and m.counts["nak"] == 15                            # the 26th frame no longer fits
    assert names(log, "nak")[0][2]["reason"] == ms.NAK_FULL
    assert m.space("dstar", 0.1) == (370 - 42 - 25 * 13) // 13 == 0
    y, _ = model("mmdvm")
    assert y.space("ysf", 0) == 33 and sum(y.write(ms.YSF_DATA, bytes(121), 0.0) for _ in range(40)) == 33


def test_txflag_follows_preamble_and_playout_and_drops_when_the_fifo_is_dry_without_an_underrun_after_an_eot():
    m, log = model("mmdvm", 0)
    m.write(ms.DSTAR_HEADER, HEADER, 0.0)
    m.write(ms.DSTAR_DATA, DATA, 0.0)
    m.write(ms.DSTAR_EOT, b"", 0.0)
    assert m.tx_on(0.2) and m.tx_on(0.5 + 0.1417 + 0.02 + 0.029) and not m.tx_on(0.5 + 0.1417 + 0.02 + 0.031)
    assert names(log, "tx_off")[0][2]["end_frame"] is True
    # a new over later: the header is a new over, not an underrun
    m.write(ms.DSTAR_HEADER, HEADER, 0.9)
    assert m.counts["underrun"] == 0


def test_underrun_is_a_frame_of_the_same_over_after_the_fifo_ran_dry_and_d_star_data_has_no_new_preamble():
    m, log = model("mmdvm", 0)
    m.write(ms.DSTAR_HEADER, HEADER, 0.0)
    m.write(ms.DSTAR_DATA, DATA, 0.0)
    t_dry = 0.5 + 0.1417 + 0.020
    assert not m.tx_on(t_dry + 0.01)
    m.write(ms.DSTAR_DATA, DATA, t_dry + 0.3)                           # 300 ms later: the over went on, the modem had stopped
    assert m.counts["underrun"] == 1 and names(log, "underrun")[0][2]["dry_s"] == pytest.approx(0.3, abs=1e-3)
    assert m.tx_on(t_dry + 0.31) and not names(log, "preamble_start")[1:]                           # data after an underrun: keyed again, no preamble
    # DMR: the same gap keys again WITH a preamble
    d, dlog = model("mmdvm", 0)
    up = rf.dmr_keyup([bytes(9)] * 3, 1023008, 4017, colour_code=1)
    burst = bytes((0,)) + up[1][1][1:]                                                              # a voice burst
    d.write(ms.DMR_DATA2, burst, 0.0)
    assert d.tx_on(0.55) and not d.tx_on(0.5 + 0.061)
    d.write(ms.DMR_DATA2, burst, 0.9)
    assert d.counts["underrun"] == 1 and len(names(dlog, "preamble_start")) == 2
    # too late (more than the window) is a new over
    e, _ = model("mmdvm", 0)
    e.write(ms.DMR_DATA2, burst, 0.0)
    e.write(ms.DMR_DATA2, burst, 5.0)
    assert e.counts["underrun"] == 0


def test_end_frames_of_dmr_and_ysf_are_recognised():
    dmr = rf.dmr_keyup([bytes(9)] * 3, 1023008, 4017, colour_code=1)
    assert [fw.is_end_frame(ms.DMR_DATA2, bytes((0,)) + p[1:]) for _t, p, _d in dmr[1:-1]] == [False]
    assert fw.is_end_frame(ms.DMR_DATA2, bytes((0,)) + dmr[-1][1][1:]) is True                       # the terminator burst is a data burst
    ysf = rf.ysf_keyup([bytes(9)] * 5, src="N0TEST")
    assert [fw.is_end_frame(ms.YSF_DATA, p) for _t, p, _d in ysf] == [False, False, True]
    assert fw.is_end_frame(ms.DSTAR_EOT, b"") is True and fw.is_end_frame(ms.DSTAR_DATA, bytes(12)) is False
    assert fw.is_end_frame(ms.YSF_DATA, b"\x00" * 3) is False


def test_a_real_time_feed_after_the_preamble_never_runs_dry_and_the_host_cap_of_the_old_image_does():
    # a source at the air rate: header, then a data frame per 20 ms; the host writes what the FIFO takes at once (no cap)
    m, log = model("mmdvm", 100)
    m.write(ms.DSTAR_HEADER, HEADER, 0.0)
    t = 0.0
    for i in range(150):
        m.write(ms.DSTAR_DATA, DATA, t)
        t += 0.02
    m.write(ms.DSTAR_EOT, b"", t)
    m.advance(t + 5)
    assert m.counts["nak"] > 0                                                 # 0.6 s of preamble = 30 frames: more than 25 fit, the rest is NAK 5
    # the same feed paced like the host with the fixed image (the FIFO is refilled before it can empty): no NAK, no underrun
    p, plog = model("mmdvm", 100)
    p.write(ms.DSTAR_HEADER, HEADER, 0.0)
    t = 0.0
    for i in range(150):
        t = 0.6 + 0.02 * i                                                      # one frame per play-out from the end of the preamble
        p.write(ms.DSTAR_DATA, DATA, t)
    p.write(ms.DSTAR_EOT, b"", t + 0.02)
    p.advance(t + 5)
    assert p.counts["nak"] == 0 and p.counts["underrun"] == 0
    # a host that writes ten frames per 250 ms status (40 per second) against 50 per second starves the modem every cycle
    q, _ = model("mmdvm", 0)
    q.write(ms.DSTAR_HEADER, HEADER, 0.0)
    for cycle in range(8):
        for _ in range(10):
            q.write(ms.DSTAR_DATA, DATA, 0.5 + 0.25 * cycle)
    q.advance(10.0)
    assert q.counts["underrun"] >= 1


def test_a_mode_the_set_config_did_not_enable_reports_no_space():
    import socket
    from dvtest import mmdvm_serial
    transport = mmdvm_serial.PinnedUdp(("127.0.0.1", 0), ("127.0.0.1", 1))
    modem = fw.FwRif5Modem(transport)
    try:
        assert list(modem.status_payload(1.0)[3:7]) == [28, 10, 10, 33]
        modem.enabled_mask = 0b001                                          # D-STAR only
        assert list(modem.status_payload(1.0)[3:7]) == [28, 10, 0, 0]       # DMR slot 1 stays the fixed byte
    finally:
        transport.sock.close()
