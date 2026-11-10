# SPDX-License-Identifier: MIT
"""DP19b in real browsers (Playwright Chromium, Firefox and Playwright WebKit; image after wip-7bd0082, merge 52db4641): the OPEN message panel of the dashboard drops a hidden
base's spatial status at the same state refresh that removes its position markers, and keeps the plain status and the text. Review 247 case 2, browser half (the API half is
cases/test_dp19b_status.py). Needs `scripts/run.sh --profile web,browser` with the bound RF port and the text path (dp19b-rf.toml). Skips on an image without DP19b (no `spatial_status` in
the public state). The signed-out dashboard shows the rows that arrive on the public SSE; its panel reads `.message-text` of the room section (cases/web_pages.py).
GAP: the talker status (`talkerStatus` shows the newest status of the on-air talker's base within 10 s of the over): it needs a voice over of the same base; docs/dp19b-cases.md. NOT RUN ON A RIG yet.
"""
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import web  # noqa: E402

import dp6c_support as sp  # noqa: E402
import test_dp19b_status as st  # noqa: E402
import test_web_browser as twb  # noqa: E402
import web_pages as pages  # noqa: E402
from test_dp15_positions import hub, rf, set_hide_position, shape, web_ctx  # noqa: E402,F401
from test_dp19b_status import image  # noqa: E402,F401

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

BASES = {"chromium": "N0DPF", "firefox": "N0DPG", "webkit": "N0DPH"}      # one base per engine: the controller admits 1 status per 10 s per base and the worker deduplicates a repeated frame
SUFFIX = " dpe"
PANEL_S = 25.0
HIDE_WAIT_S = 25.0
BODY_SPATIAL = "IO91SX/G" + SUFFIX                              # the locator status and the look-alike text have the same body: the panel row tells them apart by count
BODY_PLAIN = "Ready" + SUFFIX


def panel_bodies(bp):
    return [t.strip() for t in pages.message_texts(bp.page, st.dp6c.ROOM).all_inner_texts()]


def wait_counts(bp, want_spatial, want_plain, timeout):
    """(spatial-body count, plain-body count) once they equal the wanted ones, else the last counts after the timeout."""
    end = time.monotonic() + timeout
    while True:
        bodies = panel_bodies(bp) if pages.room_messages(bp.page, st.dp6c.ROOM).count() else []
        got = (bodies.count(BODY_SPATIAL), bodies.count(BODY_PLAIN))
        if got == (want_spatial, want_plain) or time.monotonic() >= end:
            return got
        time.sleep(0.5)


def test_dp19b_2_the_open_message_panel_drops_the_hidden_bases_spatial_status(dashboard_page, image, target, web_ctx, rf):
    """The anonymous dashboard is open and Live. The three items of the base reach the panel of room B (the spatial status and the text share the body, so the panel holds two rows with
    it); after the account of the base is hidden, within one state refresh the panel keeps one row with that body (the text) and the plain status. Clearing the flag at the end."""
    bp = dashboard_page
    BASE = BASES[bp.engine]
    web_ctx.user(BASE, may_tx=False)
    live = twb.wait_live_after(bp)
    sp.note(target, "DP19b-2[%s]" % bp.engine, "RECORD", "dashboard live: %s" % live)
    try:
        st.inject_trio(rf, BASE, SUFFIX)
        before = wait_counts(bp, 2, 1, PANEL_S)
        if before != (2, 1):
            if not live:
                pytest.skip("the dashboard is not Live: the SSE rows do not reach the panel (%s)" % (before,))
            pytest.fail("the panel of room %s holds (spatial/text body, plain body) = %s, expected (2, 1): %s" % (st.dp6c.ROOM, before, panel_bodies(bp)[:6]))
        set_hide_position(web_ctx, BASE, True)
        after = wait_counts(bp, 1, 1, HIDE_WAIT_S)
        sp.note(target, "DP19b-2[%s]" % bp.engine, "RECORD", "panel counts before %s, after the hide %s" % (before, after))
        assert after == (1, 1), "after the hide the panel must keep the text and the plain status only: (spatial/text body, plain body) = %s, rows %s" % (after, panel_bodies(bp)[:6])
        expect(pages.room_messages(bp.page, st.dp6c.ROOM)).to_be_visible()
        problems = [p for p in bp.problems() if "CSP violation" in p]
        assert not problems, "CSP reports: %s" % problems
    finally:
        try:
            set_hide_position(web_ctx, BASE, False)
        except (AssertionError, web.WebError) as exc:
            sp.note(target, "DP19b-2[%s]" % bp.engine, "RECORD", "could not clear hide_position at the end: %s" % exc)
