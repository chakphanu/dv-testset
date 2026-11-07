# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp7_positions_page.py (DP7 / MAP1 DOM case): the table text a state row must produce, the page-table reader, the
map-status poll and the end-of-case checks, against fakes of the Playwright objects. Needs the playwright package for the import of the case
module only (the tester image has it; skipped elsewhere). The selectors of cases/web_pages.py are checked against the page source strings."""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position2_support as p2  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_positions2 as t2  # noqa: E402
import test_dp7_positions_page as tp  # noqa: E402
import web_pages as pages  # noqa: E402

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000


def row(item_type="position", call="N0PGA", rid=1, lat=130000000, lon=1000000000, amb=0, name=None, state=1):
    body = {"lat_e7": lat, "lon_e7": lon, "ambiguity": amb, "comment": ""}
    if item_type == "object":
        body.update(name=name or "DVT7MAP", state=state)
    return {"type": "data", "id": rid, "item_type": item_type, "room": tp.DOM_ROOM, "callsign": call, "protocol": "m17",
            "ts_ms": NOW - 500, "ident": "stream", "via": "m17", "body": body}


def test_expected_table_texts():
    rows = [row(rid=1, call="N0PGA", lat=130583333, lon=1000291667),
            row("object", rid=2, call="N0PGD", amb=2, lat=130500000, lon=1000250000)]
    want = tp.expected_table(rows)
    assert want["N0PGA"] == ("13.05833", "100.02917", "As received")
    assert want["DVT7MAP (N0PGD)"] == ("13.05", "100.02", "Reduced by the sender (2 digits omitted)")


class FakeRow:
    def __init__(self, cells):
        self.cells = cells


class FakeLocator:
    def __init__(self, items=None, count=0, text=""):
        self.items, self._count, self.text = items or [], count, text

    def all(self):
        return self.items

    def count(self):
        return self._count

    def inner_text(self):
        return self.text

    def all_inner_texts(self):
        return self.items


def test_table_texts_reads_five_cells(monkeypatch):
    rows = [FakeRow([" N0PGA ", "13.05833", "100.02917", "As received", "12:00:00"]), FakeRow(["x", "y"])]
    monkeypatch.setattr(pages, "positions_rows", lambda page: FakeLocator(rows))
    monkeypatch.setattr(pages, "positions_cells", lambda r: FakeLocator(r.cells))
    assert tp.table_texts(SimpleNamespace(page=None)) == {"N0PGA": ("13.05833", "100.02917", "As received")}


def test_map_state_ready_note_and_timeout(monkeypatch):
    waits = []
    page = SimpleNamespace(wait_for_timeout=lambda ms: waits.append(ms))
    bp = SimpleNamespace(page=page)
    monkeypatch.setattr(pages, "positions_map_note", lambda p: FakeLocator(count=0))
    assert tp.map_state(bp, 1.0) == ("ready", "")
    monkeypatch.setattr(pages, "positions_map_note", lambda p: FakeLocator(count=1, text=" Basemap not installed. "))
    assert tp.map_state(bp, 1.0) == ("note", "Basemap not installed.")
    monkeypatch.setattr(pages, "positions_map_note", lambda p: FakeLocator(count=1, text=pages.POSITIONS_NOTE_LOADING))
    assert tp.map_state(bp, 0.0) == ("loading", pages.POSITIONS_NOTE_LOADING)


def test_finish_checks_flags_foreign_origins_and_page_errors():
    audit = p2.OriginAudit("https://dvt-web")
    audit.urls = ["https://dvt-web/", "https://dvt-web/basemap/MANIFEST"]
    bp = SimpleNamespace(log=SimpleNamespace(errors=[]), problems=lambda expected=(): [], engine="chromium")
    tp.finish_checks(bp, audit, "ok")
    audit.urls.append("https://tiles.example.invalid/0/0/0.pbf")
    with pytest.raises(AssertionError, match="foreign origin"):
        tp.finish_checks(bp, audit, "foreign")
    audit.urls.pop()
    bp.log.errors = ["TypeError: boom"]
    with pytest.raises(AssertionError, match="page error"):
        tp.finish_checks(bp, audit, "error")


def test_selectors_match_the_page_source_strings():
    assert pages.POSITIONS_TESTIDS == {"page": "positions-page", "room": "positions-room", "refresh": "positions-refresh",
                                       "map": "positions-map", "map_note": "positions-map-note", "attribution": "positions-attribution"}
    assert pages.positions_url() == "/#/positions"
    assert pages.POSITIONS_NOTE_MISSING.endswith(".") and pages.POSITIONS_ATTRIBUTION_HREF.startswith("https://www.openstreetmap.org/")


def test_the_seed_callsigns_are_distinct_from_the_other_position_cases():
    mine = list(tp.STATIONS) + [tp.OBJECT_REPORTER]
    others = {t2.CORR_OK, t2.CORR_NOCREDIT, t2.CORR_OFF, t2.CORR_TXOFF, t2.CORR_POSOFF, t2.CORR_HIDE, t2.HIDE_RF_CALL, t2.HIDE_RF_CONTROL,
              t2.SSE_MARK, t2.SSE_CONTROL, t2.SSE_HIDE, t2.RETENTION_CALL, *t2.ITEM_CALL.values(), "N0PEL", "N0PEP", "N0PEO",
              p1.OPT_CALL, p1.OPT_DMR, p1.HIDE_CALL, p1.OBJECT_REPORTER, p1.AMB_DPRS_CALL}
    for table in (p1.A_CALL, p1.CONTROL_CALL, p1.OFF_CALL, p1.AMB_M17_CALL, p1.AMB_RF_CALL):
        others |= set(table.values())
    assert len(set(mine)) == len(mine) and not set(mine) & others
    assert tp.OBJECT_NAME not in set(t2.ITEM_NAME.values()) and tp.OBJECT_NAME != p1.OBJECT_NAME
    assert tp.DOM_ROOM != p1.MODULE and re.fullmatch(r"[A-Z]", tp.DOM_ROOM)
