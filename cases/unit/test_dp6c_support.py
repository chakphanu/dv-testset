# SPDX-License-Identifier: MIT
"""dvtest/dp6c.py, cases/dp6c_support.py, the dp6c-*.toml snippets and the case helpers of the DP6c / DP19 rig cases against fakes (no rig, no sleeps beyond
milliseconds). Expected values are hand-computed from APRS101 and DV-Router's WXTLM.md / web.md (the JavaScript conversions of positions-station.js)."""
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dp6c_support as sp  # noqa: E402
import position_support as ps  # noqa: E402
import test_dp19_locator as t19  # noqa: E402
import test_dp6c_aprsis as tis  # noqa: E402
import test_dp6c_rf as trf  # noqa: E402

from dvtest import dp6c, sut_config  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered(*names, text=None):
    extras = [(SNIPPETS / n).read_text() for n in names] if text is None else [text]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", extras=extras))


# ---------------------------------------------------------------- the snippets

def test_the_rf_snippet_binds_one_receive_only_port_to_room_b():
    ax = rendered("dp6c-rf.toml")["edges"]["ax25"]
    port = ax["port"][0]
    assert ax["enable"] is True and ax["tx_enable"] is False and ax["callsign"] == "N0TEST-1"
    assert len(ax["port"]) == 1 and (port["name"], port["kind"], port["room"], port["tx"]) == ("rf-a", "kiss-tcp", "B", False) and port["port"] == 8101
    assert sut_config.render(BASE, "172.30.0", extras=[(SNIPPETS / "dp6c-rf.toml").read_text()]).count("<TESTER_IP>") == 0


def test_the_aprsis_snippets_add_the_typed_path_and_the_station_filter():
    doc = rendered("data-ident.toml", "dp15-aprsis.toml", "dp6c-aprsis-filter.toml", "dp6c-aprsis-wx.toml")
    assert doc["edges"]["aprsis"]["filter_extra"] == "p/N0WXI/N0WXJ" and doc["edges"]["aprsis"]["callsign"] == "N0TEST-5"
    paths = {p["name"]: p for p in doc["data"]["path"]}
    wx = paths["aprsis-wx"]
    assert (wx["from"], wx["to"], wx["types"]) == ("edge:aprsis", "room:B", ["weather", "telemetry"]) and "aprsis-down" in paths
    only_filter = rendered("data-ident.toml", "dp15-aprsis.toml", "dp6c-aprsis-filter.toml")
    assert [p["name"] for p in only_filter["data"]["path"]] == ["aprsis-down"]                  # the "no weather types" shape


def test_every_config_mutation_applies_renders_and_changes_the_wx_snippet():
    text = (SNIPPETS / "dp6c-aprsis-wx.toml").read_text()
    for case in dp6c.CONFIG_CASES:
        mutated = case.mutate(text)
        assert (mutated != text) == (case.verdict != "accept"), case.id
        extras = [(SNIPPETS / n).read_text() for n in ("data-ident.toml", "dp15-aprsis.toml", "dp6c-aprsis-filter.toml")] + [mutated]
        doc = tomllib.loads(sut_config.render(BASE, "172.30.0", extras=extras))
        assert any(p["name"] in ("aprsis-wx", "dp6c-room-wx", "dp6c-room-to-is") for p in doc["data"]["path"]), case.id
    assert len({c.id for c in dp6c.CONFIG_CASES}) == len(dp6c.CONFIG_CASES) == 5


def verdict(valid, parsed=()):
    return {"valid": valid, "unsupported": False, "errors": [] if valid else ["x"], "parsed": list(parsed)}


def test_config_problems_judge_accept_and_reject():
    case = {c.id: c for c in dp6c.CONFIG_CASES}
    assert dp6c.config_problems(case["accept-weather-telemetry"], verdict(True), 0) == []
    assert "expected to load" in dp6c.config_problems(case["accept-weather-telemetry"], verdict(False), 1)[0]
    unknown = case["unknown-type"]
    assert any("SILENT SUCCESS" in p for p in dp6c.config_problems(unknown, verdict(True), 0))
    assert any("no `FILE:LINE:COL" in p for p in dp6c.config_problems(unknown, verdict(False), 1))
    good = [{"key": "data.path[1].types", "message": "unknown type `bogus` (type must be one of ...)"}]
    assert dp6c.config_problems(unknown, verdict(False, good), 1) == []
    assert any("does not name the data path" in p for p in dp6c.config_problems(unknown, verdict(False, [{"key": "edges.aprsis", "message": "type bogus"}]), 1))
    assert any("does not mention" in p for p in dp6c.config_problems(unknown, verdict(False, [{"key": "data.path[0]", "message": "refused"}]), 1))
    assert dp6c.config_problems(unknown, {"unsupported": True}, 1) == ["the image has no `config check` (usage text printed)"]


# ---------------------------------------------------------------- weather display values (web/site/positions-station.js)

def test_metric_weather_and_the_weather_column_follow_the_javascript():
    body = dp6c.G5_WEATHER
    metric = dp6c.metric_weather(body)
    assert metric == {"temp_c": 25.0, "wind_kmh": 6, "gust_kmh": 8, "rain_1h_mm": 0.0, "rain_24h_mm": 0.0, "rain_midnight_mm": 0.0, "pressure_hpa": 990.0}
    assert dp6c.weather_summary(body) == "25.0 C 6 km/h" and dp6c.weather_summary(dp6c.G7Z_WEATHER) == "25.0 C" and dp6c.weather_summary({}) == ""
    assert dp6c.weather_fields(body) == [
        ("Temperature", "25.0 C (77 F)"), ("Wind", "6 km/h (4 mph)"), ("Wind direction", "220 deg"), ("Gust", "8 km/h (5 mph)"),
        ("Rain, last hour", "0.0 mm (0.00 in)"), ("Rain, last 24 hours", "0.0 mm (0.00 in)"), ("Rain since midnight", "0.0 mm (0.00 in)"),
        ("Humidity", "50%"), ("Pressure", "990.0 hPa"), ("Software", "w"), ("Weather station unit", "RSW")]
    assert ("Wind", "42 km/h (42 mph)") not in dp6c.weather_fields(dp6c.G7_WEATHER) and ("Wind", "68 km/h (42 mph)") in dp6c.weather_fields(dp6c.G7_WEATHER)
    assert not [f for f in dp6c.weather_fields(dp6c.G7Z_WEATHER) if f[0] == "Wind"]                  # no wind speed line without the speed
    # JavaScript Math.round rounds half up, also for negatives: (-7 F - 32) x 50 / 9 = -216.67 -> -217 -> -21.7 C
    assert dp6c.js_round(2.5) == 3 and dp6c.js_round(-2.5) == -2 and dp6c.metric_weather({"temp_f": -7})["temp_c"] == -21.7
    assert dp6c.metric_weather({"snow_24h_in100": 100})["snow_24h_cm"] == 2.5 and dp6c.metric_weather({"rain_1h_in100": 100})["rain_1h_mm"] == 25.4


def test_weather_problems_name_each_difference():
    assert dp6c.weather_problems(dict(dp6c.G5_WEATHER), dp6c.G5_WEATHER) == []
    bad = dict(dp6c.G5_WEATHER, temp_f=76, extra=1)
    problems = dp6c.weather_problems(bad, dp6c.G5_WEATHER)
    assert any("temp_f = 76" in p for p in problems) and any("keys outside" in p for p in problems)
    assert any("wind_mph" in p for p in dp6c.weather_problems(dp6c.G7Z_WEATHER, dp6c.G7_WEATHER))
    assert dp6c.G1_WEATHER["form"] == "positionless" and dp6c.G7Z_WEATHER.get("wind_dir_deg") == 88 and "wind_mph" not in dp6c.G7Z_WEATHER


# ---------------------------------------------------------------- telemetry G9 under G10

def good_telemetry():
    analog = [{"ch": i + 1, "raw": raw, "value": aprs.G9_CALIBRATED[i], "name": dp6c.G9_ANALOG_NAMES[i], "unit": dp6c.G9_ANALOG_UNITS[i]} for i, raw in enumerate(dp6c.G9_RAW)]
    digital = [{"ch": i + 1, "bit": dp6c.G9_DIGITAL_BITS[i], "active": dp6c.G9_DIGITAL_ACTIVE[i],
                **({"name": dp6c.G9_DIGITAL_NAMES[i]} if dp6c.G9_DIGITAL_NAMES[i] else {}), **({"label": dp6c.G9_DIGITAL_LABELS[i]} if dp6c.G9_DIGITAL_LABELS[i] else {})} for i in range(8)]
    return {"form": "numbered", "seq": 5, "analog": analog, "digital": digital, "project": "N0CALL Balloon"}


def test_the_g9_expectation_under_g10():
    assert dp6c.G9_DIGITAL_BITS == [0, 1, 1, 0, 1, 0, 0, 1] and dp6c.G9_DIGITAL_ACTIVE == [False, False, True, False, False, True, True, False]
    assert dp6c.G9_ANALOG_NAMES == ["Battery", "Btemp", "ATemp", "Pres", "Alt"] and dp6c.G9_DIGITAL_NAMES[:5] == ["Camra", "Chut", "Sun", "10m", "ATV"]
    assert dp6c.telemetry_problems(good_telemetry()) == []


def test_telemetry_problems_name_each_defect_and_accept_raw_values_without_definitions():
    body = good_telemetry()
    body["analog"][1]["value"] = "-32"                                      # the same number written with fewer places is still the value
    assert dp6c.telemetry_problems(body) == []
    for mutate, text in (
            (lambda b: b["analog"][0].update(value="1034.7"), "A1 value"), (lambda b: b["analog"][2].update(name="Pres"), "A3 name"),
            (lambda b: b["analog"][4].update(unit="Mbar"), "A5 unit"), (lambda b: b["digital"][2].update(active=False), "B3 active"),
            (lambda b: b["digital"][0].update(label="x"), "B1 label"), (lambda b: b.update(project="x"), "project"), (lambda b: b.update(seq=6), "seq"),
            (lambda b: b.update(form="mic"), "form"), (lambda b: b["analog"].pop(), "no analog channel 5"), (lambda b: b["digital"].pop(), "no digital channel 8"),
            (lambda b: b["analog"][3].update(raw="74"), "A4 raw")):
        bad = good_telemetry()
        mutate(bad)
        assert any(text in p for p in dp6c.telemetry_problems(bad)), text
    raw = {"form": "numbered", "seq": 5, "analog": [{"ch": i + 1, "raw": r} for i, r in enumerate(dp6c.G9_RAW)], "digital": [{"ch": i + 1, "bit": b, "active": b == 1}
                                                                                                                        for i, b in enumerate(dp6c.G9_DIGITAL_BITS)]}
    assert dp6c.telemetry_problems(raw, defined=False) == []
    assert any("without definitions" in p for p in dp6c.telemetry_problems(good_telemetry(), defined=False))


# ---------------------------------------------------------------- public rows

def row(call, kind, **extra):
    base = {"type": "data", "id": 1, "item_type": kind, "room": "B", "callsign": call, "protocol": "aprs", "ts_ms": 1, "ident": "none", "via": "rf", "hops": 0,
            "forwarded": False, "body": {"form": "position"} if kind == "weather" else {}}
    base.update(extra)
    return base


def test_row_selection_and_the_public_row_shape():
    rows = [row("N0WXA-13", "position"), row("N0WXA-13", "weather"), row("N0WXA-11", "telemetry"), row("N0WXA-9", "position"), row("N0WXAB-1", "weather"),
            {"item_type": "text", "callsign": "N0WXA-1"}]
    assert [r["callsign"] for r in dp6c.station_rows(rows, "n0wxa-13")] == ["N0WXA-13", "N0WXA-13"]
    assert [r["item_type"] for r in dp6c.annex_rows(rows, "N0WXA-13")] == ["weather"] and len(dp6c.annex_rows(rows)) == 3
    assert [r["callsign"] for r in dp6c.base_rows(rows, "N0WXA")] == ["N0WXA-13", "N0WXA-13", "N0WXA-11", "N0WXA-9"]       # N0WXAB-1 is another base
    assert dp6c.station_rows(rows, "N0WXA") == [] and dp6c.row_problems(rows[0]) == []
    assert any("keys outside" in p for p in dp6c.row_problems(row("N0WXA-9", "position", ip="x")))
    assert any("keys missing" in p for p in dp6c.row_problems({k: v for k, v in row("N0WXA-9", "position").items() if k != "hops"}))
    assert any("hops" in p for p in dp6c.row_problems(row("N0WXA-9", "position", hops=256))) and any("forwarded" in p for p in dp6c.row_problems(row("N0WXA-9", "position", forwarded=0)))
    assert any("coordinates" in p for p in dp6c.row_problems(row("N0WXA-9", "weather", body={"lat_e7": 1})))


# ---------------------------------------------------------------- the support helpers

class FakeTnc:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def inject(self, frame):
        self.sent.append(frame)
        return self.ok


def test_send_rf_paces_per_base_and_kind_and_spend_false_skips_the_pacer(monkeypatch):
    slept = []
    clock = {"t": 0.0}
    monkeypatch.setattr(sp, "PACER", sp.ds.Pacer(clock=lambda: clock["t"], sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)), gaps={"position": 31.0, "weather": 31.0}))
    tnc = FakeTnc()
    sp.send_rf(tnc, "N0WXA-13", aprs.weather_position_info(490583333, -720291667, 220, 4, 5, 77), kinds=("position", "weather"))
    sp.send_rf(tnc, "N0WXA-11", "!4903.50N/07201.75W>x", kinds=("position",))                # same base, position again: waits 31 s
    sp.send_rf(tnc, "N0WXA-9", "!4903.50N/07201.75W>y", kinds=("position",), spend=False)    # a dropped frame spends no credit and waits for nothing
    assert slept == [31.0] and len(tnc.sent) == 3
    with pytest.raises(AssertionError, match="no connection"):
        sp.send_rf(FakeTnc(ok=False), "N0WXQ-1", "!4903.50N/07201.75W>z", spend=False)


def test_the_pacer_of_the_support_module_knows_weather_and_telemetry_and_a_status_gap():
    assert sp.PACER.gaps["weather"] == sp.PACER.gaps["telemetry"] == sp.ds.MIN_GAP_S["position"] and sp.PACER.gaps["status"] == sp.ds.MIN_GAP_S["status"]


class FakeHub:
    def __init__(self):
        self.values = {}

    def total(self, family, **labels):
        return self.values.get((family, tuple(sorted(labels.items()))), 0)

    def bump(self, family, **labels):
        key = (family, tuple(sorted(labels.items())))
        self.values[key] = self.values.get(key, 0) + 1


def test_optout_counters_wait_for_every_kind_and_the_room_counter(monkeypatch):
    hub = FakeHub()
    counters = sp.OptoutCounters(hub, ("weather", "telemetry"))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    assert counters.wait(timeout=0.0) == ({"weather": 0, "telemetry": 0}, 0, False)
    hub.bump("dv_pkt_items_total", kind="weather", result="optout")
    hub.bump("dv_room_data_total", result="optout")
    assert counters.wait(timeout=0.0)[2] is False                                            # telemetry has not moved
    hub.bump("dv_pkt_items_total", kind="telemetry", result="optout")
    hub.bump("dv_room_data_total", result="optout")
    assert counters.wait(timeout=0.0) == ({"weather": 1, "telemetry": 1}, 2, True)
    assert counters.wait(minimum=2, timeout=0.0)[2] is False
    # a private locator status is withheld by the worker: the controller counter stays 0 and DP19 does not wait for it
    hub = FakeHub()
    status = sp.OptoutCounters(hub, ("status",))
    for _ in range(4):
        hub.bump("dv_pkt_items_total", kind="status", result="optout")
    assert status.wait(minimum=4, timeout=0.0) == ({"status": 4}, 0, False)                     # with the room counter required it never completes
    assert status.wait(minimum=4, timeout=0.0, room_required=False) == ({"status": 4}, 0, True)


def test_status_helpers_select_by_base_and_word():
    rows = [{"item_type": "status", "callsign": "N0OPT-4", "body": "IO91SX/- space"}, {"item_type": "status", "callsign": "N0OPT-5", "body": "plainword"},
            {"item_type": "text", "callsign": "N0OPT-4", "body": "space"}, {"item_type": "status", "callsign": "N0LOP-3", "body": "space"}]
    assert [r["callsign"] for r in sp.status_rows(rows, "N0OPT", "space")] == ["N0OPT-4"] and len(sp.status_rows(rows)) == 3 and len(sp.status_rows(rows, "n0opt")) == 2


class FakeWatch:
    def __init__(self, events):
        self.events = events

    def data_events(self, since=0.0):
        return [(t, o) for t, o in self.events if t >= since]

    def wait_data(self, pred, timeout, since=0.0, poll=0.02):
        for t, o in self.data_events(since):
            if pred(o):
                return t, o
        return None

    def snapshot(self):
        return [(1.0, {"event": "hello"}), (2.0, {"event": "data", "id": "7-1"})]


def test_sse_helpers_and_the_replay_cursor():
    events = [(1.0, row("N0WXA-13", "weather")), (2.0, row("N0WXA-13", "position")), (3.0, {"item_type": "status", "callsign": "N0OPT-4", "body": "word"})]
    watch = FakeWatch(events)
    assert [r["item_type"] for r in sp.sse_rows(watch)] == ["weather", "position"] and [r["item_type"] for r in sp.sse_rows(watch, 1.5, "N0WXA-13")] == ["position"]
    assert sp.sse_rows(watch, 0.0, "N0WXA-13", "weather")[0]["item_type"] == "weather" and sp.sse_status(watch, 0.0, "N0OPT", "word")
    assert sp.wait_status(watch, 0.0, "N0OPT", "word")["callsign"] == "N0OPT-4" and sp.wait_status(watch, 0.0, "N0OPT", "other", timeout=0.0) is None
    assert sp.first_event_id(watch) == "7-1"


def test_sse_replay_sends_the_last_event_id_header():
    seen = {}

    class Client:
        def sse(self, path, **kw):
            seen.update(path=path, **kw)
            return 200, "text/event-stream", []

    replay = sp.SseReplay(Client(), "/api/v1/public/events", "7-1")
    replay._run()
    assert seen["headers"] == {"Last-Event-ID": "7-1"} and seen["path"] == "/api/v1/public/events" and seen["stop"] is replay.stop


def test_wait_map_polls_until_a_row_matches(monkeypatch):
    states = iter([[row("N0WXA-13", "position")], [row("N0WXA-13", "position"), row("N0WXA-13", "weather")]])
    monkeypatch.setattr(sp, "map_rows", lambda anon, room="B": next(states))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    hits, rows = sp.wait_map(None, lambda rows: dp6c.station_rows(rows, "N0WXA-13", "weather"), timeout=5.0)
    assert [r["item_type"] for r in hits] == ["weather"] and len(rows) == 2
    monkeypatch.setattr(sp, "map_rows", lambda anon, room="B": [])
    assert sp.wait_row(None, "N0WXA-13", "weather", timeout=0.0) is None


# ---------------------------------------------------------------- the case modules

def test_the_rf_case_helpers():
    assert trf.plain_position("dp6c").startswith("!") and "dp6c" in trf.plain_position("dp6c")
    good = row("N0WXA-13", "weather")
    trf.check_row(good, "weather")
    with pytest.raises(AssertionError, match="hops"):
        trf.check_row(row("N0WXA-13", "weather", hops=300), "weather")
    with pytest.raises(AssertionError):
        trf.check_row(good, "position")
    with pytest.raises(pytest.skip.Exception):
        trf.need_positions(SimpleNamespace(positions=False))
    trf.need_positions(SimpleNamespace(positions=True))


def test_the_frames_of_the_rf_and_aprsis_cases_are_what_the_goldens_say():
    assert aprs.weather_position_info(trf.LAT_E7, trf.LON_E7, 220, 4, 5, 77, **trf.WX) == "!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW"
    assert aprs.weather_compressed_info(trf.Z_LAT_E7, trf.Z_LON_E7, "7", "z", 5, 77, **trf.WX) == "=/5L!!<*e7_7z[g005t077r000p000P000h50b09900wRSW"
    assert tis.line("N0WXI-3", "_x") == "N0WXI-3>APDVT1,TCPIP*:_x" and tis.IS_WEATHER_BASE == "N0WXI" and tis.LAT_E7 == trf.LAT_E7
    for call in (dp6c.RF_WX, dp6c.RF_TLM, dp6c.RF_POS, dp6c.RF_SOLO, dp6c.RF_DEFINER, dp6c.IS_SOLO, dp6c.IS_TLM, dp6c.Z_BASE + "-2", "N0WXY-2", "N0WXK-13"):
        assert 5 <= len(call.split("-")[0]) <= 6 and call.split("-")[1].isdigit() and 0 <= int(call.split("-")[1]) <= 15, call
    assert dp6c.RF_DEFINER.split("-")[0] == dp6c.RF_TLM.split("-")[0]                          # the definition authority is the same BASE


def test_the_locator_case_helpers():
    assert t19.word_of(">IO91SX/- six") == "six" and t19.word_of(aprs.LOCATOR_LOOKALIKES["no_symbol"]) == "KN07" and t19.word_of(">IO91SX/-nospace") == "IO91SX/-nospace"
    for shape, base in ((SimpleNamespace(positions=True, optout={"N0OPT"}), "N0OPT"), (SimpleNamespace(positions=False, optout=set()), "N0LOC")):
        assert t19.private_base(shape) == base
    with pytest.raises(pytest.skip.Exception):
        t19.private_base(SimpleNamespace(positions=True, optout=set()))
    words = [t19.word_of(v) for v in list(aprs.LOCATOR_LOOKALIKES.values()) + [">On the air plainword"]]
    assert len(set(words)) == len(words), words                                                  # every status is told apart by its last word
    assert t19.PUBLIC_BASE not in ("N0OPT", "N0LOC")


def test_the_foreign_definitions_differ_from_g10_in_every_field_and_come_from_another_base():
    foreign = sp.foreign_definitions(dp6c.RF_TLM)
    good = aprs.telemetry_definitions(dp6c.RF_TLM)
    assert len(foreign) == 4 and all(a != b for a, b in zip(foreign, good))
    assert [aprs.parse_definition(f)[1] for f in foreign] == ["PARM", "UNIT", "EQNS", "BITS"] and all(aprs.parse_definition(f)[0] == dp6c.RF_TLM for f in foreign)
    assert aprs.parse_definition(foreign[3])[2] == (dp6c.FOREIGN_BITS, dp6c.FOREIGN_PROJECT)
    assert dp6c.FOREIGN_DEFINER.split("-")[0] != dp6c.RF_TLM.split("-")[0] and dp6c.FOREIGN_DEFINER.split("-")[0] != dp6c.RF_DEFINER.split("-")[0]
    assert len(dp6c.FOREIGN_EQNS) == 15 and not set(dp6c.FOREIGN_PARM) & set(aprs.G10_PARM) and not set(dp6c.FOREIGN_UNIT) & set(aprs.G10_UNIT)
    # G9 under the foreign coefficients would give other values, so a leak would show in telemetry_problems
    body = {"form": "numbered", "seq": 5, "analog": [{"ch": i + 1, "raw": r, "value": str(r), "name": n, "unit": u} for i, (r, n, u) in enumerate(zip(
        dp6c.G9_RAW, dp6c.FOREIGN_PARM, dp6c.FOREIGN_UNIT))], "digital": [{"ch": i + 1, "bit": b, "active": True} for i, b in enumerate(dp6c.G9_DIGITAL_BITS)],
        "project": dp6c.FOREIGN_PROJECT}
    assert len(dp6c.telemetry_problems(body)) > 8


def test_the_definition_sender_helper_sends_the_given_messages_in_order(monkeypatch):
    class Tnc:
        def __init__(self):
            self.frames = []

        def inject(self, frame):
            self.frames.append(frame)
            return True

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    tnc = Tnc()
    sp.send_definitions(tnc, dp6c.RF_DEFINER, dp6c.RF_TLM)
    sp.send_definitions(tnc, dp6c.FOREIGN_DEFINER, dp6c.RF_TLM, sp.foreign_definitions(dp6c.RF_TLM))
    texts = [aprs_frame_text(f) for f in tnc.frames]
    assert len(texts) == 8 and texts[0].startswith(dp6c.RF_DEFINER + ">") and texts[4].startswith(dp6c.FOREIGN_DEFINER + ">")
    assert "Battery" in texts[0] and "Fx1" in texts[4] and "N0CALL Balloon" in texts[3] and "Foreign Project" in texts[7]


def aprs_frame_text(frame):
    from dvtest import ax25
    return ax25.to_tnc2(frame)


def test_the_identity_rules_agree_with_the_position_helpers():
    assert dp6c.ANNEX_TYPES == ps.ANNEX_TYPES
    for kind in ("position", "object", "weather", "telemetry", "text"):
        assert (kind in dp6c.ANNEX_TYPES) == ps.is_annex({"item_type": kind})
    for shown, want in (("N0WXA-13", "N0WXA-13"), ("n0wxa-13 ", "N0WXA-13"), ("N0WXA-13 extra", "N0WXA-13"), ("N0WXA", "N0WXA-13"), ("N0WXA-9", "N0WXA-13")):
        assert (dp6c.identity_key(shown) == dp6c.identity_key(want)) == ps.identity_matches(shown, want), (shown, want)
    assert [r["item_type"] for r in dp6c.annex_rows([row("N0WXA-13 x", "weather"), row("N0WXA", "weather")], "N0WXA-13")] == ["weather"]


def test_the_config_check_stand_in_for_the_aprsis_passcode_file_is_a_valid_passcode():
    import test_dp6c_config as tc
    from dvtest import aprs_passcode
    assert tc.stand_in_content("/etc/dv-router/aprs-secrets/passcode") == "%d\n" % aprs_passcode.aprs_passcode(aprs_passcode.DVR_LOGIN)
    assert int(tc.stand_in_content("/x/passcode")) >= 0                                          # numeric, as the router validates its content
    assert tc.stand_in_content("/etc/dv-router/web/password") == "standin\n"
