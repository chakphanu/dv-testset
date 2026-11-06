# SPDX-License-Identifier: MIT
"""dvtest.aprs_cases and dvtest.oracle_aprs: the spec-derived APRS strings, the encoders behind
them, the decode_aprs output parser (synthetic text) and the committed vector file
vectors/direwolf/aprs_decode.json. Offline: no container is run."""
import json
import os

import pytest

from dvtest import aprs_cases as c
from dvtest import ax25
from dvtest import oracle_aprs as o

VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "direwolf")
REQUIRED_GROUPS = ["message", "ack", "reject", "bulletin", "announcement", "position", "compressed", "mic-e",
                   "timestamp", "extension", "weather", "dao", "ambiguity", "status", "object", "item", "third-party"]


def _vec():
    with open(os.path.join(VEC, "aprs_decode.json")) as fh:
        return json.load(fh)


def _by_case():
    return {r["case"]: r for r in _vec()["records"]}


# ------------------------------------------------------------------ case table

def test_every_required_type_has_two_strings():
    groups = {}
    for name, group, _d, _i, _n in c.cases():
        groups.setdefault(group, []).append(name)
    for g in REQUIRED_GROUPS:
        assert len(groups.get(g, [])) >= 2, g


def test_names_unique_and_frames_buildable():
    allc = o.all_cases()
    names = [x[0] for x in allc]
    assert len(names) == len(set(names))
    for name, _g, dest, info, _note in allc:
        frame = o.frame_for(dest, info)
        assert ax25.parse_frame(frame)["info"] == bytes(info), name
    assert len(o.existing_cases()) == 17


def test_only_documentation_callsigns_and_no_real_data():
    for name, _g, dest, info, _n in c.cases():
        text = bytes(info).decode("latin-1")
        assert "@" not in text.replace("@0", "").replace("@1", ""), name      # no e-mail address (timestamps only)
        assert "http" not in text, name
    assert c.SRC.startswith("N0CALL")


def test_edge_cases_the_lead_asked_for_exist():
    names = {x[0] for x in c.cases()}
    for needed in ["amb-1", "amb-4", "comp-cs", "comp-nocs", "mice-offduty", "mice-emergency", "wx-pos", "wx-positionless",
                   "ts-dhm-z", "ts-hms", "ts-mdhm", "obj-kill", "tp-position", "tp-message"]:
        assert needed in names, needed


# ------------------------------------------------------------------ encoders

def test_compressed_matches_the_specification_example():
    # APRS 1.0.1 chapter 9: 49 30.00 N, 72 45.00 W, course 88, speed 36 knots (1.08^47 - 1)
    assert c.compressed(49.5, -72.75, course=88, speed_kn=1.08 ** 47 - 1, t_byte=0x3A) == c.POS_C


def test_base91_roundtrip():
    for v in (0, 90, 91, 8280, 8281, 20427156, 91 ** 4 - 1):
        out = c.base91(v, 4)
        back = 0
        for ch in out:
            back = back * 91 + (ch - 33)
        assert back == v


@pytest.mark.parametrize("lat,lon,amb,lat_s,lon_s", [
    (49.0583333, -72.0291667, 0, "4903.50N", "07201.75W"),
    (49.0583333, -72.0291667, 1, "4903.5 N", "07201.7 W"),
    (49.0583333, -72.0291667, 2, "4903.  N", "07201.  W"),
    (49.0583333, -72.0291667, 3, "490 .  N", "0720 .  W"),
    (49.0583333, -72.0291667, 4, "49  .  N", "072  .  W"),
    (-30.2056667, 140.0016667, 0, "3012.34S", "14000.10E"),
])
def test_position_text_and_ambiguity(lat, lon, amb, lat_s, lon_s):
    assert c.lat_text(lat, amb) == lat_s
    assert c.lon_text(lon, amb) == lon_s


def _mice_decode(dest, info):
    """A second, independent reading of the Mic-E rules (the way a receiver reads them)."""
    d = dest.encode("ascii")
    digit = [(ch - 0x30 if ch <= 0x39 else ch - 0x50 if ch >= 0x50 else ch - 0x41) for ch in d]
    north = d[3] >= 0x50
    offset = d[4] >= 0x50
    west = d[5] >= 0x50
    lat = digit[0] * 10 + digit[1] + (digit[2] * 10 + digit[3] + digit[4] / 10.0 + digit[5] / 100.0) / 60.0
    lat = lat if north else -lat
    dd = info[1] - 28 + (100 if offset else 0)
    if 190 <= dd <= 199:
        dd -= 190
    elif 180 <= dd <= 189:
        dd -= 80
    mm = info[2] - 28
    mm -= 60 if mm >= 60 else 0
    hh = info[3] - 28
    lon = dd + (mm + hh / 100.0) / 60.0
    lon = -lon if west else lon
    speed = (info[4] - 28) * 10 + (info[5] - 28) // 10
    course = ((info[5] - 28) % 10) * 100 + (info[6] - 28)
    speed -= 800 if speed >= 800 else 0
    course -= 400 if course >= 400 else 0
    bits = [(0x41 <= d[i] <= 0x4B) or d[i] >= 0x50 for i in range(3)]
    msg = 7 - sum(int(b) << (2 - i) for i, b in enumerate(bits))
    return lat, lon, speed, course, msg


@pytest.mark.parametrize("name,params,_note", c.MICE_CASES)
def test_mice_encoder_roundtrips(name, params, _note):
    lat, lon, speed, course, msg, kw = params
    dest, info = c._mice(lat, lon, speed, course, msg, **kw)
    got = _mice_decode(dest, info)
    assert got[0] == pytest.approx(lat, abs=1.0 / 6000)
    assert got[1] == pytest.approx(lon, abs=1.0 / 6000)
    assert got[2:] == (speed, course, msg)
    assert len(dest) == 6 and len(info) == 9


def test_mice_altitude_chars():
    assert c.mice_altitude(0) == b'"3r}'                 # 10000 = 1*91^2 + 18*91 + 81
    assert c.mice_altitude(250)[-1:] == b"}"


# ------------------------------------------------------------------ output parser (synthetic text)

TEXT = """
\x1b[0J
82 a0 b4 60 60 60 e0
--- AX.25 frame ---
-------------------
N0CALL-9>APZ000:!4903.50N/07201.75W>088/036 hello
Position, normal car (side view), UNKNOWN vendor/model
N 49 03.5000, W 072 01.7500, 67 km/h (41 MPH), course 88, alt 376 m (1234 ft)
hello
"""


def test_parser_on_synthetic_text():
    p = o.parse_output(TEXT)
    f = p["fields"]
    assert p["lines"][0].startswith("N0CALL-9>APZ000")
    assert f["data_type"] == "Position" and f["symbol"] == "normal car (side view)" and f["vendor"] is None
    pos = f["position"]
    assert pos["lat"] == pytest.approx(49.058333, abs=1e-6) and pos["lon"] == pytest.approx(-72.029167, abs=1e-6)
    assert (pos["speed_kmh"], pos["speed_mph"], pos["course_deg"], pos["alt_m"], pos["alt_ft"]) == (67, 41, 88, 376, 1234)
    assert f["comment"] == "hello"
    assert f["warnings"] == [] and f["extra_lines"] == []


def test_parser_message_ack_bulletin_phg_and_warnings():
    t = lambda *lines: "-------------------\n" + "\n".join(lines) + "\n"       # noqa: E731
    f = o.parse_output(t("a>b:x", 'APRS Message, number "7", from "N0A" to "N0B-1", UNKNOWN vendor/model', "text"))["fields"]
    assert (f["message_number"], f["message_from"], f["message_to"], f["comment"]) == ("7", "N0A", "N0B-1", "text")
    f = o.parse_output(t("a>b:x", '"N0A" ACKnowledged message number "9" from "N0B", UNKNOWN vendor/model'))["fields"]
    assert (f["data_type"], f["by_station"], f["of_station"], f["message_number"]) == ("ACKnowledged message", "N0A", "N0B", "9")
    f = o.parse_output(t("a>b:x", 'Group Bulletin with identifier "1", group name "WX", UNKNOWN vendor/model', "t"))["fields"]
    assert (f["bulletin_id"], f["bulletin_group"]) == ("1", "WX")
    f = o.parse_output(t("a>b:x", "Position, Generic digipeater, UNKNOWN vendor/model, 25 W height(HAAT)=20ft=6m 3dBi E",
                         "N 00 00.0000, E 000 00.0000"))["fields"]
    assert f["phg"] == {"power_w": 25, "height_ft": 20, "height_m": 6, "gain_dbi": 3, "directivity": "E"}
    f = o.parse_output(t("a>b:x", "Error: 'X' found for latitude hemisphere.  Specification requires upper case N or S.",
                         "Position, House, UNKNOWN vendor/model", "Invalid Latitude, W 072 01.7500"))["fields"]
    assert f["position"]["lat"] is None and f["position"]["lon"] == pytest.approx(-72.029167, abs=1e-6)
    assert len(f["warnings"]) == 1
    f = o.parse_output(t("a>b:x", "ERROR!!!  Unknown APRS Data Type Indicator \"~\", UNKNOWN vendor/model"))["fields"]
    assert f["error"] is True


def test_parser_weather():
    t = "-------------------\na>b:x\nWeather Report, WEATHER Station (blue), UNKNOWN vendor/model\n" \
        "N 49 03.5000, W 072 01.7500\n" \
        'wind 4.6 mph, direction 220, gust 5, temperature -5, rain 0.00 in last hour, humidity 85, barometer 29.92, 123 watts/m^2, "wRSW"\n'
    w = o.parse_output(t)["fields"]["weather"]
    assert w == {"wind_mph": 4.6, "wind_dir_deg": 220, "gust_mph": 5, "temp_f": -5, "rain_1h_in": 0.0, "humidity_pct": 85,
                 "barometer_inhg": 29.92, "luminosity_wm2": 123, "software": "wRSW"}


def test_aprs_job_shape():
    job = o.aprs_job()
    assert job["mode"] == "aprs" and len(job["cases"]) == len(o.all_cases())
    for case in job["cases"]:
        bytes.fromhex(case["frame_hex"])


# ------------------------------------------------------------------ the committed vectors

def test_vector_file_covers_every_case_and_info_matches():
    by = _by_case()
    for name, _g, _d, info, _n in o.all_cases():
        assert name in by, name
        assert by[name]["info_hex"] == bytes(info).hex(), name
        assert by[name]["rc"] == 0 and len(by[name]["dw_lines"]) >= 2, name
    groups = _vec()["groups"]
    for g in REQUIRED_GROUPS:
        assert len(groups[g]) >= 2, g
    assert len(groups["existing"]) == 17


def test_existing_frames_are_the_rx_frames_vector():
    with open(os.path.join(VEC, "rx_frames.json")) as fh:
        rx = {r["case"]: r for r in json.load(fh)["records"]}
    by = _by_case()
    for name, tnc2, _note in __import__("dvtest.oracle_direwolf", fromlist=["x"]).RX_CASES:
        frame = bytes.fromhex(rx[name]["dw_ax25_hex"])
        assert by["ex-" + name]["info_hex"] == ax25.parse_frame(frame)["info"].hex(), name


def test_decoder_agrees_with_our_encoders_on_positions():
    by = _by_case()
    for name, (lat, lon, _s, _c, msg, kw), _n in c.MICE_CASES:
        f = by[name]["fields"]
        assert f["position"]["lat"] == pytest.approx(lat, abs=2e-4), name
        assert f["position"]["lon"] == pytest.approx(lon, abs=2e-4), name
        status = f.get("mic_e_status")
        assert status == ("Custom-%d" % msg if kw.get("custom") else c.MICE_STD[msg]), name
    for name in ("comp-cs", "comp-nocs", "comp-ts", "obj-compressed", "item-compressed"):
        pos = by[name]["fields"]["position"]
        assert pos["lat"] == pytest.approx(49.5, abs=1e-3) and pos["lon"] == pytest.approx(-72.75, abs=1e-3), name
    pos = by["comp-custom"]["fields"]["position"]
    assert pos["lat"] == pytest.approx(-30.2056, abs=1e-3) and pos["lon"] == pytest.approx(-140.0017, abs=1e-3)
    assert by["comp-cs"]["fields"]["position"]["course_deg"] == 88
    assert "course_deg" not in by["comp-nocs"]["fields"]["position"]
    assert by["comp-alt"]["fields"]["position"]["alt_ft"] in range(990, 1010)


def test_decoder_facts_per_type():
    by = _by_case()
    assert by["obj-kill"]["fields"]["data_type"] == "Killed Object" and by["obj-live"]["fields"]["data_type"] == "Object"
    assert by["obj-live"]["fields"]["name"] == "LEADER"
    assert by["item-kill"]["fields"]["data_type"] == "Killed Item" and by["item-live"]["fields"]["name"] == "AID #2"
    assert by["msg-number"]["fields"]["message_number"] == "123"
    assert by["msg-nonumber"]["fields"]["message_number"] is None
    assert by["msg-ack"]["fields"]["data_type"] == "ACKnowledged message"
    assert by["msg-rej"]["fields"]["data_type"] == "REJected message"
    assert by["bln-group"]["fields"]["bulletin_group"] == "WX"
    assert by["ts-mdhm"]["fields"]["data_type"] == "Positionless Weather Report"
    assert by["ts-dhm-z"]["fields"]["data_type"] == "Position with time"
    assert by["wx-pos"]["fields"]["weather"]["wind_dir_deg"] == 220
    assert by["wx-pos-negative"]["fields"]["weather"]["temp_f"] == -5
    assert by["amb-4"]["fields"]["position"]["lat"] == pytest.approx(49.0, abs=1e-6)
    assert by["amb-1"]["fields"]["position"]["lon"] == pytest.approx(-(72 + 1.7 / 60), abs=1e-6)
    assert by["phg-5132"]["fields"]["phg"]["power_w"] == 25
    assert by["rng-0050"]["fields"]["range_miles"] == 50.0
    assert by["tp-message"]["fields"]["message_to"] == "N0DEST-2"
    assert by["dao-human"]["fields"]["position"]["lat_text"] == "N 49 03.5080"
    assert by["dao-base91"]["fields"]["position"]["lat_text"] == "N 49 03.5076"
    assert by["dao-base91-bare"]["fields"]["position"]["lat_text"] == "N 49 03.5000"          # not taken as DAO
    assert by["neg-unknown-dti"]["fields"]["error"] is True


def test_vector_file_is_ascii_json_without_binary_blobs():
    with open(os.path.join(VEC, "aprs_decode.json"), "rb") as fh:
        raw = fh.read()
    raw.decode("ascii")
    assert b"/home/" not in raw


# ------------------------------------------------------------------ generator's exact values and the compare policy

from fractions import Fraction                                   # noqa: E402

from dvtest import aprs_expect as x                              # noqa: E402


def _info_text(case):
    return _by_case()[case]["info_text"]


def test_every_case_has_an_expectation_and_the_vector_file_is_current():
    names = [n[0] for n in o.all_cases()]
    assert set(x.table()) == set(names)
    doc = _vec()
    assert doc["compare_policy"] == x.POLICY and doc["expect_schema"] == x.SCHEMA
    for rec in doc["records"]:
        assert rec["expect"] == json.loads(json.dumps(x.expected(rec["case"], rec["frame_header"]))), rec["case"]


def test_policy_text_names_every_rule():
    for word in ("EXACT", "half", "Timestamps", "ambiguity", "dao-base91-bare", "generator"):
        assert word in x.POLICY
    with open(os.path.join(VEC, "README.md")) as fh:
        readme = fh.read()
    assert "compare policy" in readme.lower() and "half" in readme


def test_decode_aprs_agrees_with_the_generator_under_the_policy():
    for rec in _vec()["records"]:
        assert x.compare(rec["expect"], rec) == [], rec["case"]
        assert rec["expect_vs_decode_aprs"] == [], rec["case"]


def test_positions_are_within_half_the_printed_precision():
    seen = 0
    for rec in _vec()["records"]:
        exp = rec["expect"]
        if "position" not in exp:
            continue
        pos = x.dw_position(exp)
        p = rec["fields"]["position"]
        assert x.position_within_half_precision(pos, p["lat_text"], p["lon_text"]), rec["case"]
        # the half-precision rule is real: one encoded unit off (a hundredth of a minute is 200 printed units) fails
        shifted = json.loads(json.dumps(pos))
        shifted["lat"]["int"] += shifted["lat"]["per_degree"] // 6000 or 1
        assert not x.position_within_half_precision(shifted, p["lat_text"], p["lon_text"]), rec["case"]
        seen += 1
    assert seen >= 50


def test_exact_axes_are_integers_in_the_encoded_resolution():
    for rec in _vec()["records"]:
        pos = rec["expect"].get("position")
        if not pos:
            continue
        for a in (pos["lat"], pos["lon"]):
            assert type(a["int"]) is int and a["per_degree"] in (6000, 60000, 546000, 380926, 190463), rec["case"]
    # hand-computed anchors: 49 03.50 N = 49 * 6000 + 350, 72 01.75 W = -(72 * 6000 + 175)
    pos = x.expected("pos-plain", "A>B")["position"]
    assert pos["lat"] == {"int": 294350, "per_degree": 6000} and pos["lon"] == {"int": -432175, "per_degree": 6000}
    assert x.axis_degrees(pos["lat"]) == Fraction(49) + Fraction(350, 6000)


def test_plain_positions_match_their_strings():
    for rec in _vec()["records"]:
        exp = rec["expect"]
        if exp["kind"] not in ("position", "weather-position") or "dao" in exp or "compressed" in exp:
            continue
        pos = exp["position"]
        text = rec["info_text"]
        start = 1 + len(exp["timestamp"]["text"]) if exp.get("timestamp") else 1
        assert text[start:start + 19] == x.plain_text(pos), rec["case"]


def test_ambiguity_is_the_number_of_blanked_digits():
    for n in range(1, 5):
        rec = _by_case()["amb-%d" % n]
        assert rec["expect"]["position"]["ambiguity"] == n
        assert rec["info_text"][1:9].count(" ") == n and rec["info_text"][10:19].count(" ") == n
        # decode_aprs does not print the level: the blanked digits come out as zeros in the position
        assert "ambig" not in " ".join(rec["dw_lines"][1:3]).lower()
    for rec in _vec()["records"]:
        if rec["group"] != "ambiguity" and "position" in rec["expect"] and rec["expect"]["kind"] == "position":
            assert rec["expect"]["position"]["ambiguity"] == 0, rec["case"]


def test_timestamps_are_in_the_strings_and_not_printed_by_decode_aprs():
    stamped = 0
    for rec in _vec()["records"]:
        ts = rec["expect"].get("timestamp")
        if not ts:
            continue
        stamped += 1
        assert ts["text"] in rec["info_text"], rec["case"]
        for line in rec["dw_lines"][1:]:
            assert ts["text"] not in line, rec["case"]
    assert stamped >= 12
    assert x.dhm(9, 23, 45, "z")["text"] == "092345z" and x.hms(23, 45, 17)["text"] == "234517h"
    assert x.mdhm(10, 9, 5, 56)["text"] == "10090556"


def test_compressed_axes_follow_the_base91_values():
    pos = x.compressed_position(c.POS_C)
    # the specification's example is 49 30.00 N, 72 45.00 W within one encoded unit
    assert abs(x.axis_degrees(pos["lat"]) - Fraction(99, 2)) < Fraction(1, 380926)
    assert abs(x.axis_degrees(pos["lon"]) + Fraction(291, 4)) < Fraction(1, 190463)
    assert x.compressed_cs(c.POS_C) == {"course_deg": 88, "speed_code": 47}
    cust = c.compressed(-30.2056, -140.0017, "/", "k", course=180, speed_kn=45)
    p = x.compressed_position(cust)
    assert abs(float(x.axis_degrees(p["lat"])) + 30.2056) < 1 / 380926 + 1e-9
    assert abs(float(x.axis_degrees(p["lon"])) + 140.0017) < 1 / 190463 + 1e-9


def test_mic_e_expectations_equal_the_numbers_the_strings_were_built_from():
    for name, (lat, lon, spd, crs, msg, kw), _note in c.MICE_CASES:
        exp = x.table()[name]
        assert abs(float(x.axis_degrees(exp["position"]["lat"])) - lat) < 0.0001, name
        assert abs(float(x.axis_degrees(exp["position"]["lon"])) - lon) < 0.0001, name
        assert exp["speed_kn"] == spd and exp["course_deg"] == crs and exp["message_index"] == msg
        assert exp["custom"] is bool(kw.get("custom", False))


def test_dao_axes_and_the_bare_dao_difference():
    h = x.table()["dao-human"]["position"]
    assert x.axis_degrees(h["lat"]) - x.axis_degrees(x.POS["lat"]) == Fraction(8, 60000)
    assert x.axis_degrees(h["lon"]) - x.axis_degrees(x.POS["lon"]) == -Fraction(3, 60000)
    b = x.table()["dao-base91"]["position"]
    assert x.axis_degrees(b["lat"]) - x.axis_degrees(x.POS["lat"]) == Fraction(69, 546000)       # 'f' - '!' = 69
    assert x.axis_degrees(b["lon"]) - x.axis_degrees(x.POS["lon"]) == -Fraction(68, 546000)      # 'e' - '!' = 68
    rec = _by_case()["dao-base91-bare"]
    exp = rec["expect"]
    assert exp["position"] == x.table()["dao-base91"]["position"]                  # the generator value has the DAO
    assert exp["decode_aprs"]["applies_dao"] is False and exp["decode_aprs"]["comment"] == "!wfe!"
    printed = rec["fields"]["position"]
    assert printed["lat_text"] == "N 49 03.5000" and printed["lon_text"] == "W 072 01.7500"      # not applied
    assert rec["fields"]["comment"] == "!wfe!"
    # the generator value differs from the printed one by less than a hundredth of a minute but more than half the print precision
    assert not x.position_within_half_precision(exp["position"], printed["lat_text"], printed["lon_text"])
    assert x.position_within_half_precision(x.dw_position(exp), printed["lat_text"], printed["lon_text"])


def test_documented_decode_aprs_differences_are_the_only_ones():
    diffs = {r["case"]: r["expect"]["decode_aprs"] for r in _vec()["records"] if "decode_aprs" in r["expect"]}
    assert set(diffs) == {"msg-brace-in-text", "bln-announce-group", "wx-pos", "wx-pos-negative", "wx-pos-ts", "dao-base91-bare"}
    rec = _by_case()
    assert rec["msg-brace-in-text"]["fields"]["message_number"] == "with} brace"
    assert rec["bln-announce-group"]["fields"]["bulletin_id"] == "ZGRP"
    assert rec["wx-pos"]["fields"]["weather"]["wind_mph"] == 4.6


def test_exact_text_fields_match_the_strings():
    # message, ack, bulletin, status, object and item fields are literal slices of the strings
    for rec in _vec()["records"]:
        exp, text = rec["expect"], rec["info_text"]
        if exp["kind"] == "message" and "decode_aprs" not in exp:
            assert text[1:11] == ":" + exp["addressee"].ljust(9)[:9] + ":"[:1] or text.startswith(":" + exp["addressee"].ljust(9) + ":"), rec["case"]
            assert text.endswith(exp["text"] + ("{" + exp["message_id"] if exp["message_id"] else "")), rec["case"]
        if exp["kind"] in ("ack", "reject"):
            assert text.endswith(("ack" if exp["kind"] == "ack" else "rej") + exp["ack_id"]), rec["case"]
        if exp["kind"] == "status" and "grid" not in exp:
            assert text.endswith(exp["text"]), rec["case"]
        if exp["kind"] in ("object", "item"):
            assert exp["name"] in text and text[0] == exp["data_type"], rec["case"]
            flag = text[10] if exp["kind"] == "object" else text[1 + len(exp["name"])]
            assert flag == ("*" if exp["live"] else "_") or flag == ("!" if exp["live"] else "_"), rec["case"]
        if exp["kind"] == "unknown-dti" and exp["data_type"] != "0xff":
            assert text[0:1] == exp["data_type"], rec["case"]


def test_weather_and_phg_expectations_are_the_encoded_digits():
    t = x.table()
    w = t["wx-positionless"]["weather"]
    assert (w["wind_dir_deg"], w["wind_speed_mph"], w["gust_mph"], w["temp_f"], w["humidity_pct"], w["barometer_tenths_mbar"]) == \
        (220, 4, 5, 77, 50, 9900)
    assert all(v is None for k, v in t["wx-positionless-missing"]["weather"].items() if k != "temp_f")
    assert t["wx-pos-negative"]["weather"]["temp_f"] == -5
    g = x.phg(5, 1, 3, 2)
    assert (g["power_w"], g["height_ft"], g["gain_dbi"], g["directivity"]) == (25, 20, 3, "E")
    assert x.phg(2, 3, 6, 0)["height_ft"] == 80 and x.phg(0, 0, 0, 0)["power_w"] == 0
    assert t["rng-0050"]["range_mi"] == 50
    assert t["dfs-2360"]["dfs"]["height_ft"] == 80


def test_compare_detects_a_wrong_expectation():
    import copy
    rec = _by_case()["msg-number"]
    bad = copy.deepcopy(rec["expect"])
    bad["message_id"] = "124"
    assert x.compare(bad, rec)
    rec = _by_case()["obj-kill"]
    bad = copy.deepcopy(rec["expect"])
    bad["live"] = True
    assert x.compare(bad, rec)
    rec = _by_case()["comp-cs"]
    bad = copy.deepcopy(rec["expect"])
    bad["position"]["lon"]["int"] += 1
    assert x.compare(bad, rec)
