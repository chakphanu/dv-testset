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


# ---------------------------------------------------------------- DP6c / DP19 (golden frames of DV-Router's WXTLM.md)

G5 = "!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW"
G6 = "@092345z4903.50N/07201.75W_220/004g005t-07r000p000P000h50b09900wRSW"
G7 = "=/5L!!<*e7_7P[g005t077r000p000P000h50b09900wRSW"
G7Z = "=/5L!!<*e7_7z[g005t077r000p000P000h50b09900wRSW"
WX_VALUES = dict(rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900)
LAT_E7, LON_E7 = 490583333, -720291667            # 49 deg 03.50' N, 72 deg 01.75' W


def test_g5_and_g6_weather_with_an_uncompressed_position():
    assert aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX_VALUES) == G5
    assert aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, -7, dti="@", timestamp="092345z", **WX_VALUES) == G6
    got = aprs.parse_weather_position(G6)
    assert (got["dti"], got["timestamp"], got["lat"], got["lon"], got["wind_dir"], got["wind_speed"]) == ("@", "092345z", "4903.50N", "07201.75W", 220, 4)
    assert got["tail"] == "g005t-07r000p000P000h50b09900wRSW" and aprs.parse_weather_position(G5)["timestamp"] is None
    with pytest.raises(ValueError):
        aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, dti="@")                    # @ needs the timestamp
    with pytest.raises(ValueError):
        aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, timestamp="092345z")        # ! has none
    with pytest.raises(ValueError):
        aprs.parse_weather_position("!4903.50N/07201.75W>Sunny")


def test_g7_compressed_weather_and_the_out_of_range_speed_byte():
    assert aprs.compressed_position(495000000, -727500000) == ("5L!!", "<*e7")
    assert aprs.weather_compressed_info(495000000, -727500000, "7", "P", 5, 77, **WX_VALUES) == G7
    assert aprs.weather_compressed_info(495000000, -727500000, "7", "z", 5, 77, **WX_VALUES) == G7Z
    ok, bad = aprs.parse_weather_compressed(G7), aprs.parse_weather_compressed(G7Z)
    assert (ok["wind_dir"], ok["wind_mph"]) == (88, 42)                                        # APRS101: c = 22 x 4 degrees, s = 1.08^47 - 1 knots
    assert bad["wind_dir"] == 88 and bad["wind_mph"] is None and bad["tail"] == ok["tail"]      # z is out of range: no wind speed, the weather stays
    assert aprs.compressed_course_speed(88, 36) == "7P" and aprs.unbase91("5L!!") == 380926 * (90 - 49.5)
    assert aprs.base91(0) == "!!!!" and aprs.unbase91(aprs.base91(123456)) == 123456
    with pytest.raises(ValueError):
        aprs.base91(91 ** 4)


def test_g9_g10_telemetry_report_and_definitions():
    seq, analog, bits = aprs.G9_REPORT
    assert aprs.telemetry_info(seq, analog, bits) == TLM_SPEC
    assert aprs.telemetry_definitions("N0CALL-11") == [
        ":N0CALL-11:PARM.Battery,Btemp,ATemp,Pres,Alt,Camra,Chut,Sun,10m,ATV",
        ":N0CALL-11:UNIT.v/100,deg.F,deg.F,Mbar,Kft,Click,OPEN,on,on,hi",
        ":N0CALL-11:EQNS.0,5.2,0,0,.53,-32,3,4.39,49,-32,3,18,1,2,3",
        ":N0CALL-11:BITS.10110000,N0CALL Balloon"]
    assert aprs.parse_definition(":N0CALL-11:EQNS.0,5.2,0") == ("N0CALL-11", "EQNS", ["0", "5.2", "0"])
    assert aprs.parse_definition(aprs.definition_info("N0CALL-11", "BITS", "10110000", "N0CALL Balloon")) == ("N0CALL-11", "BITS", ("10110000", "N0CALL Balloon"))
    for bad in (lambda: aprs.definition_info("N0CALL-11", "NAME", ["a"]), lambda: aprs.definition_info("N0CALL-11", "EQNS", ["1", "2"]),
                lambda: aprs.definition_info("N0CALL-11", "PARM", ["a,b"]), lambda: aprs.definition_info("N0CALL-11", "BITS", "1011", "p"),
                lambda: aprs.definition_info("N0CALL-11", "PARM", ["x"] * 14), lambda: aprs.parse_definition(":N0CALL-11:hello")):
        with pytest.raises(ValueError):
            bad()
    # hand arithmetic of WXTLM.md: a x^2 + b x + c for the five analog values 199, 0, 255, 73, 123
    from decimal import Decimal as D
    eq = [D(v) for v in aprs.G10_EQNS]
    got = [eq[3 * i] * x * x + eq[3 * i + 1] * x + eq[3 * i + 2] for i, x in enumerate(analog)]
    assert [D(v) for v in aprs.G9_CALIBRATED] == got == [D("1034.8"), D("-32.00"), D("196243.45"), D("-170291"), D("15378")]
    # the definition messages are valid AX.25 frames from the same base callsign as the telemetering station
    for info in aprs.telemetry_definitions("N0CALL-11"):
        assert ax25.dvrouter_accepts_frame(kiss_tnc.ui_frame("N0CALL-1", "APDVT1", info, digis=("WIDE1-1",)))


def test_dp19_locator_statuses_and_look_alikes():
    assert aprs.locator_status("IO91", "/-", "locator4") == aprs.LOCATOR_VALID["four"]
    assert aprs.locator_status("IO91SX", "/-", "locator6") == aprs.LOCATOR_VALID["six"]
    assert aprs.locator_status("io91sx", "\\-", "lower6") == aprs.LOCATOR_VALID["six_lower"]
    assert aprs.locator_status("JN58", "/-") == aprs.LOCATOR_VALID["four_no_text"]
    assert all(aprs.is_locator_status(v) for v in aprs.LOCATOR_VALID.values())
    assert not any(aprs.is_locator_status(v) for v in aprs.LOCATOR_LOOKALIKES.values()), [k for k, v in aprs.LOCATOR_LOOKALIKES.items() if aprs.is_locator_status(v)]
    assert not aprs.is_locator_status(">On the air from the hill") and not aprs.is_locator_status(">Test status")
    for bad in (lambda: aprs.locator_status("IO9", "/-"), lambda: aprs.locator_status("ZZ91", "/-"), lambda: aprs.locator_status("IO91SX", "/"),
                lambda: aprs.locator_status("IO91SX", " -"), lambda: aprs.locator_status("IO91SX12", "/-")):
        with pytest.raises(ValueError):
            bad()
    for info in list(aprs.LOCATOR_VALID.values()) + list(aprs.LOCATOR_LOOKALIKES.values()):
        assert ax25.dvrouter_accepts_frame(kiss_tnc.ui_frame("N0LOC-3", "APDVT1", info, digis=("WIDE1-1",)))


def test_the_dp6c_frames_are_accepted_by_the_edge_rules():
    for info in (G5, G6, G7, G7Z, aprs.telemetry_info(*aprs.G9_REPORT)):
        frame = kiss_tnc.ui_frame("N0WXA-13", "APDVT1", info, digis=("WIDE1-1",))
        assert ax25.dvrouter_accepts_frame(frame)
        assert ax25.parse_frame(frame)["info"].decode("ascii") == info
