# SPDX-License-Identifier: MIT
"""DP6c from APRS-IS (private aprsc, image 4ac79f26 and later): weather and telemetry enter a room only through an `edge:aprsis -> room:B` path whose types name them
(data.md 8.2, 9.2), WITHOUT the position that may come with them; IS positions stay refused. Run shapes (docs/dp6c-cases.md): dp15-aprsis.toml + dp6c-aprsis-filter.toml +
dp6c-aprsis-wx.toml (the typed path) or dp15-aprsis.toml + dp6c-aprsis-filter.toml alone (no weather types). NOT RUN ON A RIG yet.

  DP6c-2   G5 (complete weather with a position) from N0WXI-3, the G10 definitions from N0WXI-1 and G9 from N0WXI-4, G1 (positionless) from N0WXJ-3, each from its own
           verified login on the aprsc: weather and telemetry rows of the exact guard identity with the values, NO position row and no marker; with the typed path removed
           the same packets give no row at all (the edge counted them, so the filter delivered them)
"""
import contextlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprsis_client as ai, dp6c  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

import dp6c_support as sp  # noqa: E402
from test_dp15_positions import aprsc, hub, read_config, shape, web_ctx  # noqa: E402,F401

pytestmark = pytest.mark.rig

CASE = "DP6c-2"
LAT_E7, LON_E7 = 490583333, -720291667
WX = dict(rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900)
IS_TOCALL, IS_PATH = "APDVT1", "TCPIP*"
IS_WEATHER_BASE, IS_OTHER_BASE = "N0WXI", "N0WXJ"


@pytest.fixture(scope="module")
def wx_path(target):
    """True when the rendered config has the typed path edge:aprsis -> room:B with weather and telemetry; the aprsis filter must also name the sources."""
    doc = read_config(target)
    paths = (doc.get("data") or {}).get("path") or []
    typed = any(p.get("from") == "edge:aprsis" and p.get("to") == "room:B" and {"weather", "telemetry"} <= set(p.get("types") or []) for p in paths)
    filt = ((doc.get("edges") or {}).get("aprsis") or {}).get("filter_extra", "")
    if "p/N0WXI" not in filt:
        pytest.skip("the aprsis filter_extra does not name p/N0WXI: add rig/config/dv-router/dp6c-aprsis-filter.toml (after dp15-aprsis.toml)")
    return typed


def line(src, info):
    return "%s>%s,%s:%s" % (src, IS_TOCALL, IS_PATH, info)


@contextlib.contextmanager
def login(host, call):
    """A verified login of the tester AS `call` (the public passcode hash is computable: dvtest/aprs_passcode.py) with an empty filter."""
    with ai.AprsisClient(host, login=call, filter="") as client:
        resp = client.connect()
        assert resp["verified"], "the login %s is not verified on the private aprsc: %s" % (call, resp)
        yield client


def send_is(host, src, infos, settle=1.0):
    with login(host, src) as client:
        for info in infos:
            client.send_line(line(src, info))
            time.sleep(settle)
        time.sleep(settle)


def raw_total(hub):
    return hub.total("dv_edge_raw_total", proc="aprsis")


def send_all(host, hub):
    """The DP6c packets over the private aprsc: G5, G10, G9, G1. Returns the number of lines the edge counted (dv_edge_raw_total{proc=aprsis})."""
    before = raw_total(hub)
    send_is(host, IS_WEATHER_BASE + "-3", [aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX)])                                             # G5
    send_is(host, IS_WEATHER_BASE + "-1", aprs.telemetry_definitions(IS_WEATHER_BASE + "-4"))                                                           # G10
    send_is(host, IS_WEATHER_BASE + "-4", [aprs.telemetry_info(*aprs.G9_REPORT)])                                                                       # G9
    send_is(host, IS_OTHER_BASE + "-3", [aprs.weather_info(11, 10, 5, 56, 220, 4, 5, 77, **WX, software="w", unit="RSW")])                              # G1
    return before


def test_dp6c_2_is_weather_and_telemetry_without_a_position(rig, target, hub, web_ctx, shape, aprsc, wx_path):
    """data.md 8.2 and 9.2 (DP6c, review 244 D3): with `types = ["weather","telemetry"]` on the path edge:aprsis -> room:B the IS weather and telemetry become display-only items
    of the exact guard identity. N0WXI-3's complete weather (G5) gives a weather row and NO position row (an IS report never supplies an admitted position); N0WXI-4's G9 under the
    G10 definitions that N0WXI-1 (the same base) sent gives calibrated values; N0WXJ-3's positionless G1 gives a weather row too. The path is the one the launcher refused before
    the fix (F1): loading it through the real launcher is part of this case."""
    if not wx_path:
        pytest.skip("no typed path edge:aprsis -> room:B with weather and telemetry: add rig/config/dv-router/dp6c-aprsis-wx.toml")
    if not shape.positions:
        pytest.skip("this run has positions = false")
    with sp.feeds(web_ctx) as fd:
        before = send_all(aprsc, hub)
        weather = sp.wait_row(web_ctx.anon(), IS_WEATHER_BASE + "-3", "weather")
        telemetry = sp.wait_row(web_ctx.anon(), IS_WEATHER_BASE + "-4", "telemetry")
        other = sp.wait_row(web_ctx.anon(), IS_OTHER_BASE + "-3", "weather")
        time.sleep(sp.ABSENT_S)
        rows = sp.map_rows(web_ctx.anon())
        sse = {(r["callsign"], r["item_type"]) for r in sp.sse_rows(fd.pub, fd.t0)}
    sp.note(target, CASE, "RECORD", "dv_edge_raw_total{proc=aprsis} +%s; rows of N0WXI / N0WXJ: %s" % (
        raw_total(hub) - before, sorted((r["callsign"], r["item_type"]) for r in dp6c.base_rows(rows, IS_WEATHER_BASE) + dp6c.base_rows(rows, IS_OTHER_BASE))))
    assert weather is not None and telemetry is not None and other is not None, (
        "weather %s, telemetry %s, positionless weather %s: a row is missing from the anonymous public state" % (weather is not None, telemetry is not None, other is not None))
    for row, kind in ((weather, "weather"), (telemetry, "telemetry"), (other, "weather")):
        problems = dp6c.row_problems(row)
        assert not problems and row["item_type"] == kind and row["hops"] == 0 and row["forwarded"] is False, "%s row of %s: %s %s" % (kind, row["callsign"], problems, row)
        sp.note(target, CASE, "RECORD", "%s of %s: via %s, ident %s" % (kind, row["callsign"], row.get("via"), row.get("ident")))
    assert not dp6c.weather_problems(weather["body"], dp6c.G5_WEATHER), "weather of %s: %s" % (weather["callsign"], dp6c.weather_problems(weather["body"], dp6c.G5_WEATHER))
    assert not dp6c.weather_problems(other["body"], dp6c.G1_WEATHER), "positionless weather: %s" % dp6c.weather_problems(other["body"], dp6c.G1_WEATHER)
    assert not dp6c.telemetry_problems(telemetry["body"]), "telemetry of %s: %s" % (telemetry["callsign"], dp6c.telemetry_problems(telemetry["body"]))
    # no marker: IS positions stay refused, also the one inside the complete weather report
    positions = dp6c.station_rows(rows, kind="position")
    assert not [r for r in positions if r["callsign"].startswith((IS_WEATHER_BASE, IS_OTHER_BASE))], "an APRS-IS position was admitted: %s" % positions
    assert not [k for k in sse if k[1] == "position" and k[0].startswith((IS_WEATHER_BASE, IS_OTHER_BASE))], "an IS position reached the public SSE"
    assert (IS_WEATHER_BASE + "-3", "weather") in sse and (IS_WEATHER_BASE + "-4", "telemetry") in sse, "no public SSE data event for the IS weather / telemetry: %s" % sorted(sse)


def test_dp6c_2_is_without_weather_types_gives_no_rows(rig, target, hub, web_ctx, shape, aprsc, wx_path):
    """data.md 9.2 / 8.2: without a path that names weather and telemetry nothing of these packets enters a room (no annex row). The control is the edge's own counter:
    dv_edge_raw_total{proc=aprsis} rose, so the filter DID deliver the packets and the path rows refused them."""
    if wx_path:
        pytest.skip("this run has the typed path: use dp15-aprsis.toml + dp6c-aprsis-filter.toml WITHOUT dp6c-aprsis-wx.toml")
    with sp.feeds(web_ctx) as fd:
        before = send_all(aprsc, hub)
        end = time.monotonic() + sp.COUNTER_WAIT_S
        while raw_total(hub) - before < 1 and time.monotonic() < end:
            time.sleep(2.0)
        counted = raw_total(hub) - before
        sp.note(target, CASE, "RECORD", "dv_edge_raw_total{proc=aprsis} +%s" % counted)
        assert counted >= 1, "the APRS-IS edge counted none of the packets: the filter did not deliver them, so the absence below proves nothing"
        sp.assert_absent_everywhere(web_ctx, fd, IS_WEATHER_BASE, CASE)
        sp.assert_absent_everywhere(web_ctx, fd, IS_OTHER_BASE, CASE, window=1.0)
