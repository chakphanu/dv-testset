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
