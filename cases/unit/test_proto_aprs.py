# SPDX-License-Identifier: MIT
"""dvtest/proto/aprs.py: positionless weather, telemetry and general query fields. Round trips plus the vectors printed in
APRS Protocol Reference 1.0.1 (chapter 12 weather example, chapter 13 telemetry example), and the AX.25 frame the DP15 igate
case builds from them. Offline."""
import pytest

from dvtest import ax25, kiss_tnc
from dvtest.proto import aprs

pytestmark = pytest.mark.unit

WX_SPEC = "_10090556c220s004g005t077r000p000P000h50b09900wRSW"
TLM_SPEC = "T#005,199,000,255,073,123,01101001"


def test_weather_matches_the_chapter_12_example():
    built = aprs.weather_info(10, 9, 5, 56, 220, 4, 5, 77, rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900,
                              software="w", unit="RSW")
    assert built == WX_SPEC
    got = aprs.parse_weather(WX_SPEC)
    assert got["wind_dir"] == 220 and got["gust"] == 5 and got["temp_f"] == 77 and got["pressure"] == 9900
    assert (got["month"], got["day"], got["hour"], got["minute"]) == (10, 9, 5, 56)
    assert (got["software"], got["unit"]) == ("w", "RSW")


@pytest.mark.parametrize("temp", [-99, -5, 0, 7, 77, 100, 999, None])
def test_weather_round_trip_for_temperatures(temp):
    info = aprs.weather_info(1, 2, 3, 4, 90, 10, 15, temp, humidity=85, rain_1h=1, rain_24h=2, rain_midnight=3, pressure=10132)
    got = aprs.parse_weather(info)
    assert got["temp_f"] == temp and got["humidity"] == 85 and got["pressure"] == 10132
    assert got["rain_1h"] == 1 and got["rain_24h"] == 2 and got["rain_midnight"] == 3


def test_weather_optional_blocks_and_missing_values():
    short = aprs.weather_info(1, 1, 0, 0, None, None, None, 60)
    assert short == "_01010000c...s...g...t060dvt"
    got = aprs.parse_weather(short)
    assert got["wind_dir"] is None and got["gust"] is None and got["rain_1h"] is None and got["humidity"] is None
    assert aprs.weather_info(1, 1, 0, 0, 0, 0, 0, 0, humidity=100).count("h00") == 1
    assert aprs.parse_weather(aprs.weather_info(1, 1, 0, 0, 0, 0, 0, 0, humidity=100))["humidity"] == 100
    assert aprs.parse_weather(aprs.weather_info(1, 1, 0, 0, 0, 0, 0, 0, rain_24h=1, pressure=10000))["rain_24h"] == 1
    with pytest.raises(ValueError):
        aprs.weather_info(13, 1, 0, 0, 0, 0, 0, 0)
    with pytest.raises(ValueError):
        aprs.weather_info(1, 1, 0, 0, 0, 0, 0, 1000)
    with pytest.raises(ValueError):
        aprs.weather_info(1, 1, 0, 0, 0, 0, 0, 0, unit="V")
    with pytest.raises(ValueError):
        aprs.parse_weather("_garbage")


def test_telemetry_matches_the_chapter_13_example():
    assert aprs.telemetry_info(5, (199, 0, 255, 73, 123), "01101001") == TLM_SPEC
    got = aprs.parse_telemetry(TLM_SPEC)
    assert got == {"seq": 5, "analog": (199, 0, 255, 73, 123), "bits": "01101001", "comment": ""}


def test_telemetry_round_trip_comment_mic_and_integer_bits():
    info = aprs.telemetry_info("MIC", (1, 2, 3, 4, 5), 0xA5, comment="dvt")
    assert info == "T#MIC,001,002,003,004,005,10100101dvt"
    got = aprs.parse_telemetry(info)
    assert got["seq"] == "MIC" and got["bits"] == "10100101" and got["comment"] == "dvt"
    for bad in [lambda: aprs.telemetry_info(1000, (0,) * 5, "0" * 8), lambda: aprs.telemetry_info(1, (0,) * 4, "0" * 8),
                lambda: aprs.telemetry_info(1, (0, 0, 0, 0, 256), "0" * 8), lambda: aprs.telemetry_info(1, (0,) * 5, "0101"),
                lambda: aprs.parse_telemetry("T#1,2"), lambda: aprs.parse_telemetry("T#001,999,000,000,000,000,00000000")]:
        with pytest.raises(ValueError):
            bad()


def test_general_query():
    assert aprs.query_info() == "?APRS?" and aprs.query_info("IGATE") == "?IGATE?"
    assert aprs.parse_query("?APRS?") == "APRS"
    with pytest.raises(ValueError):
        aprs.query_info("XYZ")
    with pytest.raises(ValueError):
        aprs.parse_query("?APRS")


def test_the_frames_the_igate_case_injects_are_accepted_by_the_edge_rules():
    for info, digis in [(WX_SPEC, ("WIDE1-1*",)), (TLM_SPEC, ("WIDE1-1*", "WIDE2-1")), (aprs.query_info(), ("WIDE1-1",)),
                        (WX_SPEC, ("NOGATE",)), (WX_SPEC, ("RFONLY",))]:
        frame = kiss_tnc.ui_frame("N0IGW-3", "APDVT1", info, digis=digis)
        assert ax25.dvrouter_accepts_frame(frame)
        parsed = ax25.parse_frame(frame)
        assert parsed["info"].decode("ascii") == info
        assert ax25.to_tnc2(frame) == "N0IGW-3>APDVT1%s:%s" % ("".join("," + d for d in digis), info)
