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
