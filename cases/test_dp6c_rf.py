# SPDX-License-Identifier: MIT
"""DP6c on an RF / KISS port bound to room B (image 4ac79f26 and later): weather (type 5) and telemetry (type 6) next to positions on the public map, keyed by the
GUARD identity (N0WXA-13, SSID kept, `hops` and `forwarded` on every row), privacy per BASE callsign. Specification: DV-Router docs/design/data.md 3.1, 5.3, 9.1,
13.2, 13.4 and web.md "Position map" at 4ac79f26; golden frames G1-G19 (dvtest/proto/aprs.py, cases/unit/test_proto_aprs.py); case table, run shapes, commands and
runtimes: docs/dp6c-cases.md. The fake KISS TNC plays the radio (cases/test_dp15_positions.py `rf`); NOT RUN ON A RIG yet.

  DP6c-1  separate stations on one base callsign (and a definition from ANOTHER base, N0WXB-1 for N0WXA-11, sent between the valid definition and the report: it must change nothing): N0WXA-13 position + weather (G5), N0WXA-11 position + telemetry (G9 under the G10 definitions, calibrated), N0WXA-9
          a separate position, N0WXC-5 positionless weather (G1: a row, no marker). Public state, public SSE and the operator feed carry the rows, the user message
          history carries none; weather bodies are the native ones; positions of one base are 31 s apart (the base bucket), weather and telemetry have their own
  DP6c-8  a compressed weather position with speed byte `z` keeps its marker and shows the weather without wind speed (control: `P` gives 42 mph)
  DP6c-3  hide_position of the base removes every SSID (all kinds) on the next state refresh and from SSE and its replay; new items of the base are dropped (13);
          positions=false and position_optout deny the typed items as 13 (counters), the other shapes skip
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import dp6c, web  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

import dp6c_support as sp  # noqa: E402
import position_support as ps  # noqa: E402
from test_dp15_positions import hub, rf, set_hide_position, shape, web_ctx  # noqa: E402,F401

pytestmark = pytest.mark.rig

CASE = "DP6c"
LAT_E7, LON_E7 = 490583333, -720291667                  # 49 deg 03.50' N, 72 deg 01.75' W (the G5 position)
Z_LAT_E7, Z_LON_E7 = 495000000, -727500000              # the compressed position of G7 (49.5 N, 72.75 W)
WX = dict(rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900)
POS_TOL_E7 = 3000                                       # a compressed position has a resolution of about 0.0001 degrees


def need_positions(shape):
    if not shape.positions:
        pytest.skip("this run has positions = false")


def plain_position(comment):
    return ps.position_info(ps.APRS_LAT, ps.APRS_LON, 0, "/>", comment)


def check_row(row, kind, hops=0, forwarded=False):
    problems = dp6c.row_problems(row)
    assert not problems, "%s row of %s: %s (row %s)" % (kind, row.get("callsign"), "; ".join(problems), sorted(row))
    assert row["item_type"] == kind and row["hops"] == hops and row["forwarded"] is forwarded, "row of %s: %s" % (row.get("callsign"), row)


# ================================================================ DP6c-1: separate stations, weather and telemetry values

def test_dp6c_1_stations_weather_and_telemetry_over_rf(rig, target, hub, web_ctx, shape, rf):
    """data.md 5.3, 13.2 and 13.4, web.md "Position map": on a room-bound port the fake TNC hears, from ONE base callsign N0WXA, N0WXA-13 (position + complete weather, G5),
    N0WXA-11 (a position, then the G10 definitions sent by N0WXA-1, then the G9 report), N0WXA-9 (a position), and from N0WXC-5 a positionless weather report (G1). Public
    map rows are per guard identity: N0WXA-13, -11 and -9 are three stations, and no row carries the bare base. Weather and telemetry have their own buckets, so they follow
    their position at once; the three positions of the base are 31 s apart (the position bucket is per base callsign)."""
    case = CASE + "-1"
    need_positions(shape)
    t0 = time.monotonic()
    with sp.feeds(web_ctx) as fd:
        sp.send_rf(rf, dp6c.RF_SOLO, aprs.weather_info(11, 10, 5, 56, 220, 4, 5, 77, **WX, software="w", unit="RSW"), kinds=("weather",))           # G1
        sp.send_rf(rf, dp6c.RF_WX, aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX), kinds=("position", "weather"))                   # G5
        sp.send_rf(rf, dp6c.RF_TLM, plain_position("dp6c tlm"), kinds=("position",))
        sp.send_definitions(rf, dp6c.RF_DEFINER, dp6c.RF_TLM)                                                    # the valid definition (same base)
        sp.send_definitions(rf, dp6c.FOREIGN_DEFINER, dp6c.RF_TLM, sp.foreign_definitions(dp6c.RF_TLM))         # a foreign definition (another base): changes nothing
        sp.send_rf(rf, dp6c.RF_TLM, aprs.telemetry_info(*aprs.G9_REPORT), kinds=("telemetry",))                                                       # G9
        sp.send_rf(rf, dp6c.RF_POS, plain_position("dp6c solo"), kinds=("position",))
        wx13 = sp.wait_row(web_ctx.anon(), dp6c.RF_WX, "weather")
        tlm11 = sp.wait_row(web_ctx.anon(), dp6c.RF_TLM, "telemetry")
        pos9 = sp.wait_row(web_ctx.anon(), dp6c.RF_POS, "position")
        solo = sp.wait_row(web_ctx.anon(), dp6c.RF_SOLO, "weather")
        rows = sp.map_rows(web_ctx.anon())
        sse = {(r["callsign"], r["item_type"]) for r in sp.sse_rows(fd.pub, fd.t0)}
        op = {(r["callsign"], r["item_type"]) for r in sp.operator_rows(web_ctx, fd)}
    note_rows = [(r["callsign"], r["item_type"]) for r in dp6c.base_rows(rows, dp6c.RF_BASE) + dp6c.base_rows(rows, "N0WXC")]
    sp.note(target, case, "RECORD", "public map rows of N0WXA / N0WXC after %.0f s: %s" % (time.monotonic() - t0, sorted(note_rows)))
    assert wx13 is not None, "no weather row for %s in the anonymous public state" % dp6c.RF_WX
    assert tlm11 is not None, "no telemetry row for %s" % dp6c.RF_TLM
    assert pos9 is not None, "no position row for %s" % dp6c.RF_POS
    assert solo is not None, "no weather row for the positionless station %s" % dp6c.RF_SOLO
    # weather: the native body of G5 (position form) and of G1 (positionless form); rows of the guard identity, hops 0, not forwarded
    check_row(wx13, "weather")
    assert not dp6c.weather_problems(wx13["body"], dp6c.G5_WEATHER), "weather of %s: %s" % (dp6c.RF_WX, dp6c.weather_problems(wx13["body"], dp6c.G5_WEATHER))
    check_row(solo, "weather")
    assert not dp6c.weather_problems(solo["body"], dp6c.G1_WEATHER), "positionless weather of %s: %s" % (dp6c.RF_SOLO, dp6c.weather_problems(solo["body"], dp6c.G1_WEATHER))
    # telemetry: G9 under the G10 definitions: calibrated values, names, units, digital bits with their sense, the project
    check_row(tlm11, "telemetry")
    assert not dp6c.telemetry_problems(tlm11["body"]), "telemetry of %s: %s (a definition from another base %s must change nothing)" % (
        dp6c.RF_TLM, dp6c.telemetry_problems(tlm11["body"]), dp6c.FOREIGN_BASE)
    foreign = [dp6c.FOREIGN_PARM[0], dp6c.FOREIGN_UNIT[0], dp6c.FOREIGN_PROJECT]
    assert not [f for f in foreign if f in repr(tlm11["body"])], "the foreign definition left a trace in the telemetry body: %s" % tlm11["body"]
    assert not any(k in tlm11["body"] for k in dp6c.COORDINATE_KEYS)
    # positions: three stations of one base, each its own row; the G5 position is at the sent precision
    positions = {r["callsign"]: r for r in dp6c.station_rows(rows, kind="position") if r["callsign"].startswith(dp6c.RF_BASE)}
    assert set(positions) == {dp6c.RF_WX, dp6c.RF_TLM, dp6c.RF_POS}, "position rows of the base %s: %s, expected one per SSID" % (dp6c.RF_BASE, sorted(positions))
    for row in positions.values():
        check_row(row, "position")
    assert ps.near(positions[dp6c.RF_WX]["body"]["lat_e7"], LAT_E7) and ps.near(positions[dp6c.RF_WX]["body"]["lon_e7"], LON_E7)
    # the annexes stay with their station: no weather on -11 or -9, no telemetry on -13, no row of the bare base, no marker for the positionless station
    assert not dp6c.station_rows(rows, dp6c.RF_TLM, "weather") and not dp6c.station_rows(rows, dp6c.RF_POS, "weather")
    assert not dp6c.station_rows(rows, dp6c.RF_WX, "telemetry") and not dp6c.station_rows(rows, dp6c.RF_POS, "telemetry")
    assert not dp6c.station_rows(rows, dp6c.RF_BASE), "a row keyed by the bare base callsign: %s" % dp6c.station_rows(rows, dp6c.RF_BASE)
    assert not dp6c.station_rows(rows, dp6c.RF_SOLO, "position"), "the positionless weather station %s has a position row" % dp6c.RF_SOLO
    # the other surfaces: the public SSE and the operator feed carry the weather and telemetry rows; the user message history carries none
    for key in ((dp6c.RF_WX, "weather"), (dp6c.RF_TLM, "telemetry"), (dp6c.RF_SOLO, "weather")):
        assert key in sse, "no public SSE data event for %s %s" % key
        if key not in op:
            sp.note(target, case, "FINDING", "no operator feed (observer scope) event for %s %s: data.md 13.2 says the change feed carries weather and telemetry" % key)
    history = web.room_messages(web_ctx.reader, dp6c.ROOM)
    assert not [r for r in history if r.get("item_type") in ("position", "object") + dp6c.ANNEX_TYPES], "the message history carries a map item: %s" % history[:2]
    sp.note(target, case, "RECORD", "weather %s, telemetry %s, summary %r" % (wx13["body"], tlm11["body"].get("analog", [])[:2], dp6c.weather_summary(wx13["body"])))


# ================================================================ DP6c-8: compressed weather with speed byte z

def test_dp6c_8_compressed_weather_with_an_out_of_range_speed_byte(rig, target, web_ctx, shape, rf):
    """data.md 9.1 weather: G7 `=/5L!!<*e7_7P[g005t077...` is a compressed position whose `cs` bytes carry the wind (88 degrees, 42 mph); with the speed byte `z` (out of
    range) the position keeps its marker and the weather its other values, without wind speed. Two bases, so that no position bucket is shared."""
    case = CASE + "-8"
    need_positions(shape)
    with sp.feeds(web_ctx):
        sp.send_rf(rf, dp6c.Z_BASE + "-2", aprs.weather_compressed_info(Z_LAT_E7, Z_LON_E7, "7", "z", 5, 77, **WX), kinds=("position", "weather"))        # G7 with z
        sp.send_rf(rf, "N0WXY-2", aprs.weather_compressed_info(Z_LAT_E7, Z_LON_E7, "7", "P", 5, 77, **WX), kinds=("position", "weather"))                  # G7 control
        z_pos, z_wx = sp.wait_row(web_ctx.anon(), dp6c.Z_BASE + "-2", "position"), sp.wait_row(web_ctx.anon(), dp6c.Z_BASE + "-2", "weather")
        y_pos, y_wx = sp.wait_row(web_ctx.anon(), "N0WXY-2", "position"), sp.wait_row(web_ctx.anon(), "N0WXY-2", "weather")
    assert y_pos is not None and y_wx is not None, "the control (speed byte P) gave no position and weather row"
    assert not dp6c.weather_problems(y_wx["body"], dict(dp6c.G7_WEATHER)), "control weather: %s" % dp6c.weather_problems(y_wx["body"], dp6c.G7_WEATHER)
    assert z_pos is not None, "the position of the compressed report with speed byte z is gone: it must keep its marker"
    assert ps.near(z_pos["body"]["lat_e7"], Z_LAT_E7, POS_TOL_E7) and ps.near(z_pos["body"]["lon_e7"], Z_LON_E7, POS_TOL_E7), z_pos["body"]
    assert z_wx is not None, "no weather row for the compressed report with speed byte z"
    problems = dp6c.weather_problems(z_wx["body"], dp6c.G7Z_WEATHER)
    sp.note(target, case, "RECORD", "z weather body %s; summary %r (%s)" % (z_wx["body"], dp6c.weather_summary(z_wx["body"]), dp6c.WIND_ABSENT_NOTE))
    assert not problems, "weather with speed byte z: %s" % problems
    assert "wind_mph" not in z_wx["body"] and dp6c.weather_summary(z_wx["body"]) == "25.0 C", "the Weather column of the z station shows a wind speed: %s" % z_wx["body"]


# ================================================================ DP6c-3: privacy

def test_dp6c_3_hide_position_removes_every_ssid_and_every_kind(rig, target, hub, web_ctx, shape, rf):
    """data.md 13.4 and 13.2: hide_position of an account hides its BASE callsign on every source, weather and telemetry included. N0WXH-13 (position + weather, G5) and N0WXH-9
    (a position and a telemetry report) are public; the operator sets hide_position: every row of the base leaves the anonymous public state on the next refresh, a reconnect of
    the public SSE with its Last-Event-ID replays none of them, and new items of the base are dropped (denied/13: the optout counters rise) and appear nowhere; clearing the flag
    brings new items back."""
    case = CASE + "-3[hide_position]"
    need_positions(shape)
    base = dp6c.HIDE_BASE
    web_ctx.user(base, may_tx=False)
    try:
        with sp.feeds(web_ctx) as fd:
            sp.send_rf(rf, base + "-13", aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX), kinds=("position", "weather"))
            sp.send_rf(rf, base + "-9", plain_position("dp6c hide"), kinds=("position",))
            sp.send_rf(rf, base + "-9", aprs.telemetry_info(*aprs.G9_REPORT), kinds=("telemetry",))
            seen, _rows = sp.wait_map(web_ctx.anon(), lambda rows: dp6c.base_rows(rows, base) if len(dp6c.base_rows(rows, base)) >= 4 else [])
            kinds = sorted((r["callsign"], r["item_type"]) for r in seen)
            sp.note(target, case, "RECORD", "rows of %s before the flag: %s" % (base, kinds))
            assert kinds == sorted([(base + "-13", "position"), (base + "-13", "weather"), (base + "-9", "position"), (base + "-9", "telemetry")]), kinds
            cursor = sp.first_event_id(fd.pub)
        set_hide_position(web_ctx, base, True)
        time.sleep(3.0)                                                                  # the policy is pushed on web/position-policy-updates
        gone, rows = sp.wait_map(web_ctx.anon(), lambda rows: ["gone"] if not dp6c.base_rows(rows, base) else [])
        sp.note(target, case, "RECORD", "after the flag the retained rows of %s are gone from the state: %s (left: %s)" % (base, bool(gone), [(r["callsign"], r["item_type"]) for r in dp6c.base_rows(rows, base)]))
        assert gone, "rows of the hidden base are still in the anonymous public state: %s" % [(r["callsign"], r["item_type"]) for r in dp6c.base_rows(rows, base)]
        # replay: reconnect with the cursor of the first event; nothing of the hidden base may come back
        if cursor:
            with sp.SseReplay(web_ctx.anon(), ps.PUBLIC_EVENTS, cursor, seconds=30.0) as replay:
                assert replay.ready(), "the replaying public SSE gave no frame: %s" % replay.error
                time.sleep(6.0)
                replayed = [o for _t, o in replay.data_events()]
            sp.note(target, case, "RECORD", "replay after the cursor %s carried %d data event(s)" % (cursor, len(replayed)))
            assert not dp6c.base_rows(replayed, base), "the SSE replay carries rows of the hidden base: %s" % dp6c.base_rows(replayed, base)
        else:
            sp.note(target, case, "RECORD", "the public SSE frames carry no id: the replay check is skipped")
        # new items of the hidden base: dropped before publication
        counters = sp.OptoutCounters(hub, ("position", "weather"))
        with sp.feeds(web_ctx) as after:
            sp.send_rf(rf, base + "-13", aprs.weather_position_info(LAT_E7 + 1000, LON_E7, 90, 7, 9, 61, **WX), kinds=("position", "weather"), spend=False)
            items, room, reached = counters.wait()
            sp.note(target, case, "RECORD", "optout counters +%s, dv_room_data_total +%s" % (items, room))
            assert reached, "the optout counters did not rise for the hidden base within %.0f s: %s / %s" % (sp.COUNTER_WAIT_S, items, room)
            sp.assert_absent_everywhere(web_ctx, after, base, case)
        set_hide_position(web_ctx, base, False)
        time.sleep(3.0)
        sp.send_rf(rf, base + "-13", aprs.weather_position_info(LAT_E7 + 2000, LON_E7, 45, 3, 4, 55, **WX), kinds=("position", "weather"))
        again = sp.wait_row(web_ctx.anon(), base + "-13", "weather")
        assert again is not None and again["body"].get("temp_f") == 55, "after clearing the flag a new weather report of %s is not public: %s" % (base, again)
    finally:
        try:
            set_hide_position(web_ctx, base, False)
        except (AssertionError, web.WebError) as exc:
            sp.note(target, case, "RECORD", "could not clear hide_position at the end: %s" % exc)


def drop_case(target, hub, web_ctx, rf, case, base):
    """Weather, telemetry and a position of `base` over RF: the optout counters of the three kinds rise and nothing of the base is public, on any surface."""
    counters = sp.OptoutCounters(hub, ("position", "weather", "telemetry"))
    with sp.feeds(web_ctx) as fd:
        sp.send_rf(rf, base + "-13", aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX), kinds=("position", "weather"), spend=False)       # G5: position + weather
        sp.send_rf(rf, base + "-11", aprs.telemetry_info(*aprs.G9_REPORT), kinds=("telemetry",), spend=False)                                     # G9
        sp.send_rf(rf, base + "-5", aprs.weather_info(11, 10, 5, 56, 220, 4, 5, 77, **WX, software="w", unit="RSW"), kinds=("weather",), spend=False)   # G1, other SSID
        items, room, reached = counters.wait()
        sp.note(target, case, "RECORD", "optout counters per kind %s, dv_room_data_total +%s" % (items, room))
        assert reached or all(items[k] >= 1 for k in ("weather", "telemetry")), ("the typed items were not denied/13: dv_pkt_items_total{result=optout} %s, "
                                                                                  "dv_room_data_total +%s" % (items, room))
        sp.assert_absent_everywhere(web_ctx, fd, base, case)


def test_dp6c_3_position_optout_denies_weather_and_telemetry(rig, target, hub, web_ctx, shape, rf):
    """data.md 13.4: `[data] position_optout` matches the base callsign on every source and covers weather and telemetry even without a position (the controller answers
    denied/13). Needs rig/config/dv-router/dp7-position-optout.toml; the control N0WXK (not listed) is public."""
    case = CASE + "-3[position_optout]"
    if not (shape.positions and dp6c.OPT_BASE in shape.optout):
        pytest.skip("position_optout does not name %s: add rig/config/dv-router/dp7-position-optout.toml" % dp6c.OPT_BASE)
    sp.send_rf(rf, "N0WXK-13", aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX), kinds=("position", "weather"))
    control = sp.wait_row(web_ctx.anon(), "N0WXK-13", "weather")
    assert control is not None, "the control weather of N0WXK-13 is not public: the opt-out check proves nothing"
    drop_case(target, hub, web_ctx, rf, case, dp6c.OPT_BASE)


def test_dp6c_3_positions_false_denies_weather_and_telemetry(rig, target, hub, web_ctx, shape, rf):
    """data.md 13.4: `[data] positions = false` drops all positions, objects, weather and telemetry (denied/13). Needs rig/config/dv-router/dp7-positions-off.toml."""
    case = CASE + "-3[positions_false]"
    if shape.positions:
        pytest.skip("this run has positions = true: add rig/config/dv-router/dp7-positions-off.toml")
    drop_case(target, hub, web_ctx, rf, case, "N0WXF")
