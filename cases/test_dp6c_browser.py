# SPDX-License-Identifier: MIT
"""DP6c in real browsers (Playwright Chromium, Firefox and Playwright WebKit; image 4ac79f26 and later): the public `#/positions` page shows weather and telemetry as text
only, in English and Thai, and the operator event view shows none of them. Needs `scripts/run.sh --profile web,browser` with the data worker on and the RF shape
(dp6c-rf.toml): the fake KISS TNC rf-a feeds room B. Page knowledge: web/site/positions.js, positions-station.js, shared/event-display.js (DV-Router, read only), cases/web_pages.py
("#/positions"); the expected texts come from dvtest/dp6c.py (the JavaScript conversions) and the catalog shared/i18n/public-th.js. NOT RUN ON A RIG yet.

  DP6c-4  EN and TH, three engines: the table has one row per station (callsign with its SSID), the Weather column shows the metric summary, a station with no position shows
          "No position received" in both coordinate cells, "Show details" opens a native <details> whose weather values are metric first with the native unit in parentheses and
          whose telemetry table has the names, calibrated values, units and raw values; received names and the project (`<b>`, `<i>`) stay literal text (no element is created);
          the page asks only its own origin and raises no CSP report beyond the documented CSP2/CSP3 WebKit signatures
  DP6c-9  the operator events page (#/events) shows no weather or telemetry row: the weather-only station has no row at all, the stations with a position show their position
          event only; the operator API still carries the typed items (data.md 13.2)
GAPs: the marker click that focuses the details (a WebGL marker has no DOM element; docs/dp6c-cases.md) and the admin SPA event view (admin host sign-in with TOTP).
"""
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import dp6c  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

import dp6c_support as sp  # noqa: E402
import position2_support as p2  # noqa: E402
import position_support as ps  # noqa: E402
import test_web_browser as twb  # noqa: E402
import web_pages as pages  # noqa: E402
from test_dp15_positions import hub, rf, shape, web_ctx  # noqa: E402,F401

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

LOAD_S = 20.0
WX_CALL, TLM_CALL, SOLO_CALL = "N0WBA-13", "N0WBB-11", "N0WBC-5"
LAT_E7, LON_E7 = 490583333, -720291667
WX = dict(rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900)
NAME_BOLD, PROJECT_ITALIC = "<b>Bat</b>", "N0WBB <i>x</i>"          # received strings that must stay literal text


def thai(codepoints):
    """A catalog string from its code points (the source files stay ASCII)."""
    return "".join(chr(int(h, 16)) for h in codepoints.split())


TH = {   # shared/i18n/public-th.js, as code points
    "Show details": thai("0e41 0e2a 0e14 0e07 0e23 0e32 0e22 0e25 0e30 0e40 0e2d 0e35 0e22 0e14"),
    "No position received": thai("0e22 0e31 0e07 0e44 0e21 0e48 0e44 0e14 0e49 0e23 0e31 0e1a 0e15 0e33 0e41 0e2b 0e19 0e48 0e07"),
    "Weather": thai("0e2a 0e20 0e32 0e1e 0e2d 0e32 0e01 0e32 0e28"),
    "Station details": thai("0e23 0e32 0e22 0e25 0e30 0e40 0e2d 0e35 0e22 0e14 0e2a 0e16 0e32 0e19 0e35"),
}
LANG_STORAGE_KEY = "dv-lang"
COL_CALLSIGN, COL_LAT, COL_LON, COL_WEATHER, COL_DETAILS = 0, 1, 2, 5, 6        # positions.js table: ... Time, Weather, Station details


def station_details_id(key):
    """positions-station.js stationDetailsId: 'position-details-' + encodeURIComponent(room:CALLSIGN)."""
    from urllib.parse import quote
    return "position-details-" + quote(key, safe="!'()*-._~")


@pytest.fixture(scope="module")
def seeds(rf, web_ctx, target, shape):
    """Three DP6c stations in room B, public in the anonymous state before any page opens: N0WBA-13 (G5 weather with its position), N0WBB-11 (a position, definitions with
    markup in a name and in the project, then G9) and N0WBC-5 (positionless G1 weather)."""
    if not shape.positions:
        pytest.skip("this run has positions = false")
    sp.send_rf(rf, WX_CALL, aprs.weather_position_info(LAT_E7, LON_E7, 220, 4, 5, 77, **WX), kinds=("position", "weather"))
    sp.send_rf(rf, TLM_CALL, ps.position_info(ps.APRS_LAT, ps.APRS_LON, 0, "/>", "dp6c browser"), kinds=("position",))
    parm = [NAME_BOLD] + aprs.G10_PARM[1:]
    for kind, fields in (("PARM", parm), ("UNIT", aprs.G10_UNIT), ("EQNS", aprs.G10_EQNS)):
        sp.send_rf(rf, "N0WBB-1", aprs.definition_info(TLM_CALL, kind, fields), kinds=(), spend=False)
        time.sleep(1.0)
    sp.send_rf(rf, "N0WBB-1", aprs.definition_info(TLM_CALL, "BITS", aprs.G10_BITS, PROJECT_ITALIC), kinds=(), spend=False)
    time.sleep(1.0)
    sp.send_rf(rf, TLM_CALL, aprs.telemetry_info(*aprs.G9_REPORT), kinds=("telemetry",))
    sp.send_rf(rf, SOLO_CALL, aprs.weather_info(11, 10, 5, 56, 220, 4, 5, 77, **WX, software="w", unit="RSW"), kinds=("weather",))
    anon = web_ctx.anon()
    for call, kind in ((WX_CALL, "weather"), (TLM_CALL, "telemetry"), (SOLO_CALL, "weather")):
        assert sp.wait_row(anon, call, kind) is not None, "the seed %s %s never became public in room B" % (call, kind)
    sp.note(target, "DP6c-browser", "RECORD", "seeds: %s" % sorted((r["callsign"], r["item_type"]) for r in sp.map_rows(anon) if r["callsign"].startswith("N0WB")))
    return {"anon": anon}


def open_positions(bp, lang):
    """Opens #/positions in the language `lang` and selects room B (the stored language is read at page start: shared/i18n.js)."""
    if lang != "en":
        bp.context.add_init_script("try { localStorage.setItem('%s', '%s'); } catch (e) {}" % (LANG_STORAGE_KEY, lang))
    bp.goto(pages.positions_url())
    expect(pages.positions_page(bp.page)).to_be_visible(timeout=int(LOAD_S * 1000))
    expect(pages.positions_room_option(bp.page, dp6c.ROOM)).to_be_attached(timeout=int(LOAD_S * 1000))
    pages.positions_room(bp.page).select_option(dp6c.ROOM)


def row_of(bp, call):
    return pages.positions_rows(bp.page).filter(has=bp.page.locator("td", has_text=call)).first


def cell_texts(row):
    return [c.strip() for c in row.locator("td").all_inner_texts()]


def details_of(bp, room, call):
    return bp.page.locator('[id="%s"]' % station_details_id("%s:%s" % (room, call)))


@pytest.mark.parametrize("lang", ["en", "th"])
def test_dp6c_4_station_details_are_text_only_in_both_languages(browser_page, seeds, lang, target):
    """web.md "Position map" (DP6c): per station one table row; the Weather column is the metric summary (`25.0 C 6 km/h`); a station with only weather shows "No position received"
    in the coordinate cells (no marker); the native details control opens the weather fields (metric first, the native unit in parentheses) and the telemetry channels (name,
    calibrated value, unit, raw) and the project; markup in received strings is shown literally (no <b> or <i> element); only the page's own origin is asked; no CSP report beyond
    the documented WebKit ones."""
    bp = browser_page
    audit = p2.OriginAudit(bp.base).install(bp.context)
    open_positions(bp, lang)
    th = lang == "th"
    wx_row, tlm_row, solo_row = row_of(bp, WX_CALL), row_of(bp, TLM_CALL), row_of(bp, SOLO_CALL)
    for row in (wx_row, tlm_row, solo_row):
        expect(row).to_be_visible(timeout=int(LOAD_S * 1000))
    wx_cells, solo_cells, tlm_cells = cell_texts(wx_row), cell_texts(solo_row), cell_texts(tlm_row)
    assert wx_cells[COL_CALLSIGN] == WX_CALL and wx_cells[COL_WEATHER] == dp6c.weather_summary(dp6c.G5_WEATHER) == "25.0 C 6 km/h", wx_cells
    assert tlm_cells[COL_CALLSIGN] == TLM_CALL and tlm_cells[COL_WEATHER] == "", tlm_cells                              # telemetry has no weather summary
    none_text = TH["No position received"] if th else "No position received"
    assert solo_cells[COL_LAT] == none_text and solo_cells[COL_LON] == none_text and solo_cells[COL_WEATHER] == "25.0 C 6 km/h", solo_cells
    assert wx_cells[COL_LAT] != none_text and tlm_cells[COL_LAT] != none_text, "a station with a position shows its coordinates"
    # the headings carry the new Weather and Station details columns
    heads = [h.strip() for h in pages.positions_page(bp.page).locator("table thead th").all_inner_texts()]
    assert (heads[COL_WEATHER], heads[COL_DETAILS]) == ((TH["Weather"], TH["Station details"]) if th else ("Weather", "Station details")), heads
    assert th or heads == pages.POSITIONS_COLUMNS_DP6C, "the English headings %s differ from cases/web_pages.py POSITIONS_COLUMNS_DP6C" % heads
    # weather details: open the native control by its summary
    summary = details_of(bp, dp6c.ROOM, WX_CALL).locator("summary")
    expect(summary).to_have_text(TH["Show details"] if th else "Show details")
    summary.click()
    expect(details_of(bp, dp6c.ROOM, WX_CALL)).to_have_attribute("open", "")
    values = [v.strip() for v in details_of(bp, dp6c.ROOM, WX_CALL).locator("section dl dd").all_inner_texts()]
    assert values == [v for _label, v in dp6c.weather_fields(dp6c.G5_WEATHER)], "weather values %s" % values
    if not th:
        labels = [v.strip() for v in details_of(bp, dp6c.ROOM, WX_CALL).locator("section dl dt").all_inner_texts()]
        assert labels == [label for label, _v in dp6c.weather_fields(dp6c.G5_WEATHER)], labels
    # telemetry details: names, calibrated values, units and raw values; the markup stays text
    tlm = details_of(bp, dp6c.ROOM, TLM_CALL)
    tlm.locator("summary").click()
    analog = tlm.locator("table").first.locator("tbody tr")
    expect(analog).to_have_count(5)
    rows = [[c.strip() for c in analog.nth(i).locator("td").all_inner_texts()] for i in range(5)]
    assert rows[0][0] == NAME_BOLD and rows[0][2] == aprs.G10_UNIT[0] and rows[0][3] == dp6c.G9_RAW[0], rows[0]
    for i, row in enumerate(rows):
        assert Decimal(row[1]) == Decimal(aprs.G9_CALIBRATED[i]), "channel %d shows %r, expected %s" % (i + 1, row[1], aprs.G9_CALIBRATED[i])
        if i:
            assert row[0] == aprs.G10_PARM[i] and row[2] == aprs.G10_UNIT[i] and row[3] == dp6c.G9_RAW[i], row
    assert PROJECT_ITALIC in tlm.inner_text(), "the project string is not shown literally: %s" % tlm.inner_text()[:200]
    assert tlm.locator("b").count() == 0 and tlm.locator("i").count() == 0, "received markup became an element in the details"
    assert bp.page.locator("table b, table i").count() == 0, "received markup became an element in the table"
    # origins and CSP
    bp.page.wait_for_timeout(1500)
    foreign = audit.blocked
    csp = [p for p in bp.problems() if "CSP violation" in p]
    sp.note(target, "DP6c-4[%s/%s]" % (bp.engine, lang), "RECORD", "origins %s, blocked %s, csp %s" % (audit.origins(), foreign, csp))
    assert not foreign, "the page requested a foreign origin: %s" % foreign
    assert not csp, "CSP reports beyond the documented signatures: %s" % csp


def test_dp6c_9_operator_event_view_shows_no_weather_or_telemetry(operator_page, web_ctx, seeds, target):
    """data.md 13.2 and web/shared/event-display.js: objects, weather and telemetry are display-only map rows, never in event views (a data row passes only with the known event item
    types text, status and position). The operator events page lists no row of the weather-only station N0WBC-5 and at most the position event of N0WBA-13 and N0WBB-11, and its
    text holds no weather or telemetry row; the operator API (the raw feed) still carries the typed items."""
    bp = operator_page
    api = [r for r in ps.wait_operator_rows(web_ctx.op, 0, lambda r: False, timeout=1.0)[1]]
    typed = sorted((r.get("callsign"), r.get("item_type")) for r in dp6c.annex_rows(api) if str(r.get("callsign", "")).startswith("N0WB"))
    sp.note(target, "DP6c-9", "RECORD", "operator API typed items of the seeds: %s" % typed)
    title = twb.open_ops(bp, "events", fresh=True)
    bp.page.wait_for_timeout(2000)
    table = pages.ops_table(bp.page, title)
    text = table.inner_text()
    rows = pages.ops_rows(bp.page, title)
    callsigns = [cells[6].strip() for cells in ([c for c in rows.nth(i).locator("td").all_inner_texts()] for i in range(rows.count())) if len(cells) > 6]
    assert SOLO_CALL not in callsigns and SOLO_CALL not in text, "the weather-only station %s is in the events view" % SOLO_CALL
    for call in (WX_CALL, TLM_CALL):
        assert callsigns.count(call) <= 1, "%s has %d event rows: more than its position event" % (call, callsigns.count(call))
    lowered = text.lower()
    assert "telemetry" not in lowered and "weather" not in lowered, "the events view holds a weather or telemetry row: %s" % text[:300]
    twb.report(bp, set(), "operator events view")
