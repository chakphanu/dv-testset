# SPDX-License-Identifier: MIT
"""dvtest.airtime: LoRa time on air, two independent code paths, and the committed
anchor table vectors/airtime/anchors.json. Offline."""
import json
import os
from fractions import Fraction

import pytest

from dvtest import airtime as a

VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "airtime", "anchors.json")


def test_symbol_time_and_ldro_rule():
    assert a.symbol_time(7, 125000) == Fraction(1024, 1000000)
    assert a.low_data_rate(11, 125000) == 1            # 16.384 ms > 16 ms
    assert a.low_data_rate(10, 125000) == 0            # 8.192 ms


def test_ldro_follows_symbol_time_not_sf():
    assert a.low_data_rate(12, 125000) == 1
    assert a.low_data_rate(12, 250000) == 1
    assert a.low_data_rate(12, 500000) == 0            # 8.192 ms
    assert a.low_data_rate(11, 250000) == 0            # 8.192 ms


@pytest.mark.parametrize("pl,sf,expect_ms", [
    (10, 7, "41.216"), (10, 11, "577.536"), (64, 12, "2793.472"), (68, 12, "2957.312"),
    (80, 12, "3284.992"), (20, 7, "56.576"), (1, 7, "25.856"),
])
def test_anchor_values(pl, sf, expect_ms):
    assert a.ms(a.time_on_air(pl, sf, 125000)) == expect_ms


def test_published_numbers_round_to_the_computed_ones():
    for row in a.anchor_rows():
        if row["published"] is None:
            continue
        pub = Fraction(row["published"])
        got = Fraction(row["computed_ms"])
        assert abs(pub - got) <= Fraction(5, 100), row["id"]      # published to 0.1 or 0.01 ms


def test_symbol_counts_hand_checked():
    # PL 10, SF7, CR 4/5, CRC on, explicit header: ceil((80 - 28 + 28 + 16) / 28) = 4 blocks * 5 + 8 = 28
    assert a.payload_symbols(10, 7) == 28
    # same with implicit header: ceil((80 - 28 + 28 + 16 - 20) / 28) = 3 -> 23
    assert a.payload_symbols(10, 7, implicit_header=True) == 23
    # no CRC: ceil((80 - 28 + 28) / 28) = 3 -> 23
    assert a.payload_symbols(10, 7, crc=False) == 23
    # low data rate: SF 11 and DE 1: denominator 4 * 9 = 36: ceil(80 - 44 + 28 + 16 = 80 / 36) = 3 -> 23
    assert a.payload_symbols(10, 11, de=1) == 23


def test_cr_and_payload_monotonic():
    base = a.time_on_air(40, 9, 125000)
    assert a.time_on_air(41, 9, 125000) >= base
    assert a.time_on_air(40, 9, 125000, cr=4) > base
    assert a.time_on_air(40, 10, 125000) > base
    assert a.time_on_air(40, 9, 250000) < base
    assert a.time_on_air(40, 9, 125000, preamble=12) > base


def test_us_style_matches_exact_for_standard_bandwidths():
    for sf in range(7, 13):
        for bw in (125000, 250000, 500000):
            for pl in (1, 10, 51, 62, 120, 255):
                exact = a.time_on_air(pl, sf, bw)
                us = a.time_on_air_us_style(pl, sf, bw)
                assert Fraction(us, 1000000) == exact, (sf, bw, pl)


def test_us_style_truncates_for_odd_bandwidth():
    exact = a.time_on_air(20, 7, 62500 + 1)
    us = a.time_on_air_us_style(20, 7, 62500 + 1)
    assert abs(float(exact) - us / 1e6) < 1e-3


def test_input_checks():
    with pytest.raises(ValueError):
        a.payload_symbols(10, 5)
    with pytest.raises(ValueError):
        a.payload_symbols(10, 7, cr=5)
    with pytest.raises(ValueError):
        a.payload_symbols(10, 2, de=1)


def test_committed_table_is_the_regenerated_table():
    if not os.path.exists(VEC):
        pytest.skip("anchors.json not generated")
    with open(VEC, encoding="ascii") as fh:
        doc = json.load(fh)
    assert doc["rows"] == json.loads(json.dumps(a.anchor_rows()))
    kinds = {r["kind"] for r in doc["rows"]}
    assert kinds == {"external", "project", "derived"}
    ext = [r for r in doc["rows"] if r["kind"] == "external"]
    assert len(ext) == 4
    for r in doc["rows"]:
        assert r["expected_ms"] == r["computed_ms"]
        assert Fraction(r["us_style_us"], 1000) == Fraction(r["computed_ms"])


def test_lora_aprs_rows_use_prefix_plus_line_length():
    rows = {r["id"]: r for r in a.anchor_rows()}
    for i, line in enumerate(a.LORA_APRS_LINES, start=1):
        assert rows["aprs-%d-sf12" % i]["payload_bytes"] == 3 + len(line)
    assert rows["aprs-3-sf12"]["computed_ms"] == "2793.472"


# ------------------------------------------------------------------ AFSK upper bound (DV-Router)

import random                                            # noqa: E402

from dvtest import ax25                                  # noqa: E402

RX_VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "direwolf", "rx_frames.json")


def _rx_frames():
    if not os.path.exists(RX_VEC):
        pytest.skip("rx_frames.json not generated")
    with open(RX_VEC, encoding="ascii") as fh:
        recs = json.load(fh)["records"]
    frames = [bytes.fromhex(r["dw_ax25_hex"]) for r in recs if r["dw_generated"] and not r["case"].startswith("neg-")]
    assert len(frames) == 17
    return frames


def test_upper_bound_formula_small_values():
    # 47-byte frame: (47 + 4) * 8 * 6 / 5 = 489.6 bits; at 1200 bps 408 ms; + 300 + 30
    assert a.afsk_upper_bound_bits(47) == Fraction(2448, 5)
    assert a.afsk_upper_bound_ms(47) == 408 + 300 + 30
    assert a.afsk_upper_bound_ms(47, txdelay_ms=0, txtail_ms=0) == 408
    # the ceil: 17-byte frame, 201.6 bits = 168 ms exactly
    assert a.afsk_upper_bound_ms(17, txdelay_ms=0, txtail_ms=0) == 168
    # a coarser granule rounds up
    assert a.afsk_upper_bound_ms(47, txdelay_ms=0, txtail_ms=0, granule_ms=100) == 500
    with pytest.raises(ValueError):
        a.afsk_upper_bound_ms(10, bps=0)
    with pytest.raises(ValueError):
        a.afsk_upper_bound_bits(-1)


def test_upper_bound_is_above_exact_bits_for_all_17_frames():
    for frame in _rx_frames():
        exact_bits = ax25.air_bits(frame)
        upper_bits = a.afsk_upper_bound_bits(len(frame))
        assert exact_bits <= upper_bits, len(frame)
        # ... and within the 6/5 allowance: the stuffed zeros are at most one per five bits
        assert ax25.stuffed_zeros(ax25.with_fcs(frame)) <= Fraction(8 * (len(frame) + 2), 5)
        assert upper_bits == Fraction((len(frame) + 4) * 8 * 6, 5)
        # in time, with no TXDELAY / TXtail, in the unit of DV-Router (1 ns): the bound is the exact
        # bit time of (len + 4) * 8 * 6 / 5 bits rounded UP to a whole nanosecond, nothing more
        exact_ms = a.afsk_exact_frame_ms(frame)
        upper_ns = a.afsk_airtime_ns(len(frame), 1200, 0, 0)
        assert exact_ms * 1_000_000 <= upper_ns
        want = Fraction((len(frame) + 4) * 8 * 6 * 10 ** 9, 5 * 1200)
        assert want <= upper_ns < want + 1
        # and TXDELAY + TXtail only add
        assert a.afsk_airtime_ns(len(frame)) == upper_ns + a.DVR_TXDELAY_NS + a.DVR_TXTAIL_NS


def test_upper_bound_holds_for_worst_case_and_random_frames():
    rng = random.Random(20261107)
    frames = [ax25.from_tnc2("N0CALL>APRS:" + "<0xff>" * n) for n in (0, 1, 10, 100, 500)]
    frames += [ax25.from_tnc2("N0CALL>APRS:") + bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
               for _ in range(300)]
    for frame in frames:
        assert ax25.air_bits(frame) <= a.afsk_upper_bound_bits(len(frame))
        assert a.afsk_exact_frame_ms(frame) * 1_000_000 <= a.afsk_airtime_ns(len(frame), 1200, 0, 0)


def test_upper_bound_is_tight_enough_for_all_ones():
    # a frame of 0xFF is about the worst case for stuffing: the bound loses less than 2% there
    frame = ax25.from_tnc2("N0CALL>APRS:" + "<0xff>" * 400)
    exact = ax25.air_bits(frame)
    upper = a.afsk_upper_bound_bits(len(frame))
    assert exact <= upper < exact * Fraction(102, 100)


# ------------------------------------------------------------------ AFSK bound in integer nanoseconds

AFSK_VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "airtime", "afsk_bound.json")


def _fraction_ceil(frame_len, bps):
    """Second path for the frame term: exact Fraction, then ceil."""
    f = Fraction((frame_len + 4) * 8 * 6 * 10 ** 9, bps * 5)
    return -((-f.numerator) // f.denominator)


def test_unit_is_one_nanosecond_not_one_millisecond():
    # 17 bytes at 1200 bps is exactly 168 ms; 47 bytes 408 ms; the ceil is in ns
    assert a.afsk_frame_ns(17, 1200) == 168_000_000
    assert a.afsk_frame_ns(47, 1200) == 408_000_000
    # a bit rate that does not divide: 47 bytes at 7 bps = 69942857142.857... ns -> ...143
    assert a.afsk_frame_ns(47, 7) == 69_942_857_143
    # +1 ms margin of the old model would be 1_000_000 ns more; the new bound is within 1 ns
    n = Fraction(51 * 48 * 10 ** 9, 5 * 1201)
    assert a.afsk_frame_ns(47, 1201) - n < 1
    assert a.afsk_airtime_ns(47) == 408_000_000 + 300_000_000 + 30_000_000
    assert a.afsk_airtime_ns(47, 1200, 1, 2, 3) == 408_000_006


def test_airtime_ns_ceil_property_and_second_path():
    rng = random.Random(7)
    for _ in range(2000):
        length = rng.randrange(0, 5000)
        bps = rng.choice([1, 3, 7, 300, 1200, 1201, 2400, 4800, 9600, 19200, 5469, 4294967295, rng.randrange(1, 100000)])
        ns = a.afsk_frame_ns(length, bps)
        assert ns == _fraction_ceil(length, bps)
        # ceil: ns is the smallest integer with ns * bps * 5 >= (len + 4) * 8 * 6 * 10^9
        num = (length + 4) * 8 * 6 * 10 ** 9
        assert ns * bps * 5 >= num > (ns - 1) * bps * 5


def test_airtime_ns_errors():
    with pytest.raises(a.AirtimeError) as exc:
        a.afsk_airtime_ns(47, 0)
    assert exc.value.kind == "zero-bit-rate"
    with pytest.raises(a.AirtimeError) as exc:
        a.afsk_airtime_ns(2 ** 64 - 1, 1)
    assert exc.value.kind == "overflow"
    with pytest.raises(a.AirtimeError):
        a.afsk_airtime_ns(47, 1200, a.U64_MAX, 0, 0)
    assert a.afsk_airtime_ns(0, 1200, 0, 0, 0) == 32_000_000
    with pytest.raises(ValueError):
        a.afsk_airtime_ns(-1)
    with pytest.raises(ValueError):
        a.afsk_airtime_ns(47, 2 ** 32)


def test_old_ms_helper_is_never_below_the_ns_bound():
    for length in (0, 1, 17, 47, 100, 1100):
        for bps in (300, 1200, 9600, 1201):
            ns = a.afsk_airtime_ns(length, bps, 0, 0)
            assert a.afsk_upper_bound_ms(length, bps, 0, 0) * 1_000_000 >= ns


def test_afsk_vector_table_is_current_and_integer():
    if not os.path.exists(AFSK_VEC):
        pytest.skip("afsk_bound.json not generated")
    with open(AFSK_VEC, encoding="ascii") as fh:
        doc = json.load(fh)
    assert doc["schema"] == "dvt-airtime-afsk-bound/1"
    assert doc["rows"] == a.afsk_rows()                     # regenerate with python3 -m dvtest.airtime
    ids = [r["id"] for r in doc["rows"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 20
    errors = {r["error"] for r in doc["rows"] if "error" in r}
    assert errors == {"overflow", "zero-bit-rate"}
    for r in doc["rows"]:
        for key in ("frame_len", "bps", "txdelay_ns", "tail_ns", "preamble_ns", "expected_ns", "frame_ns"):
            if key in r:
                assert type(r[key]) is int, (r["id"], key)
        if "expected_ns" in r:
            # independent second path: Fraction ceil + the three additions
            want = _fraction_ceil(r["frame_len"], r["bps"]) + r["txdelay_ns"] + r["tail_ns"] + r["preamble_ns"]
            assert r["expected_ns"] == want <= a.U64_MAX, r["id"]


def test_afsk_vector_hand_values():
    rows = {r["id"]: r for r in a.afsk_rows()}
    assert rows["len47-1200-default"]["expected_ns"] == 738_000_000
    assert rows["len17-1200-bare"]["expected_ns"] == 168_000_000
    assert rows["len0-1200-bare"]["expected_ns"] == 32_000_000          # 4 * 8 * 6/5 = 38.4 bits / 1200
    assert rows["len47-7-bare"]["expected_ns"] == 69_942_857_143
    assert rows["len47-u32max"]["expected_ns"] == 114
    assert rows["overflow-frame"]["error"] == "overflow"
    assert rows["zero-bit-rate"]["error"] == "zero-bit-rate"
