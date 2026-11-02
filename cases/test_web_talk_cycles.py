# SPDX-License-Identifier: MIT
"""Short repro of the WebKit Hold Talk failure seen on dv-router wip-d449742 in the long stage 3b session (the page left
"On air" 1.6 s after the press with "Transmit permission was revoked."): one Playwright WebKit session, the same page and
login flow as the talk cases of cases/test_web_webrtc.py, then 30 quick talk cycles (press, hold 2 s or 3 s alternately,
release, about 1 s of rest). Every cycle is recorded: the page status timeline, whether the page left "On air" before the
planned end and with which text, the ptt frames on the ctl channel, the increase of the edge counters
dv_edge_webrtc_unrouted_total (by reason) and dv_edge_webrtc_audio_dropped_total, and the getStats ICE pairs (kept for a
failed cycle and every 10th cycle). The table is $DVT_OUT/web/talk-cycles-<engine>.json (rewritten after every cycle); the
case fails when any cycle ended early, after all cycles ran. At most 6 minutes (no new cycle after 300 s).

Needs the browser profile (--profile web,browser) and DVT_TALK_CYCLES=1 (compose forwards it); the engine is
DVT_TALK_CYCLES_ENGINE (webkit by default; the other engines skip). With rig/config/dv-router/debug-log.toml in
DVT_SUT_CONFIG_EXTRA the debug lines of the SUT (dv-room rts / controller winner, dv-edge-webrtc) are kept in
out/<run>/logs/xlxd-sut.log; the per-cycle air_utc lines them up. Pure parts: dvtest/talk_cycles.py.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import web_pages as pages  # noqa: E402
from dvtest import browser as brlib  # noqa: E402
from dvtest import talk_cycles as tc  # noqa: E402
from test_web_webrtc import _digest, edge_counters, listening, pair_trace  # noqa: E402
from webrtc_support import MIC_UNAVAILABLE, Rtc, on_air_or_end, press, pump_while, release, usrp_client, wait_talk_text  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc, pytest.mark.slow]

ROOM = "K"
PROTO_CALL = "N0TEST"
CYCLES = 30
BUDGET_S = 300.0
REST_S = 1.0
POLL_S = 0.25


@pytest.fixture(autouse=True)
def cycles_gate(request):
    if os.environ.get("DVT_TALK_CYCLES") != "1":
        pytest.skip("set DVT_TALK_CYCLES=1 to run the talk cycles repro")
    params = request.node.callspec.params if hasattr(request.node, "callspec") else {}
    if params.get("engine") != (os.environ.get("DVT_TALK_CYCLES_ENGINE") or "webkit"):
        pytest.skip("the talk cycles repro runs on one engine (DVT_TALK_CYCLES_ENGINE, webkit by default)")


def utc(t):
    return time.strftime("%H:%M:%S", time.gmtime(t)) + ".%03d" % int((t % 1) * 1000)


def write_table(engine, rows, meta):
    path = Path(os.environ.get("DVT_OUT", "out")) / "web" / ("talk-cycles-%s.json" % engine)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"meta": meta, "summary": tc.summarize(rows), "cycles": rows}, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError:
        pass


def test_talk_cycles(engine, rtc_tx_page, target, rig_control):
    bp = rtc_tx_page
    rtc = Rtc(bp)
    rtc.open()
    assert pages.talk_button(bp.page).inner_text().strip() == "Hold Talk"
    listener = usrp_client(target, PROTO_CALL, ROOM)
    rows = []
    meta = {"engine": brlib.label(engine), "room": ROOM, "cycles": CYCLES, "image": os.environ.get("XLXD_SUT_IMAGE", "")}
    try:
        listening(rtc, ROOM)
        rtc.tap_start()
        page_zero = rtc.clock()
        t_begin = time.monotonic()
        for index, planned in enumerate(tc.plan(CYCLES)):
            if time.monotonic() - t_begin > BUDGET_S:
                meta["stopped_by_budget_at_cycle"] = index
                break
            row = {"index": index, "planned_s": planned}
            before = edge_counters(rig_control)
            page_start = rtc.clock()
            press(rtc)
            text, took = wait_talk_text(rtc, on_air_or_end, 30.0)
            row["press_to_status_s"] = round(took, 2) if took is not None else None
            if text.startswith(MIC_UNAVAILABLE):
                release(rtc)
                pytest.skip("%s: no fake microphone (talk status %r)" % (brlib.label(engine), text))
            if text != pages.TALK_ON_AIR_TEXT:
                row["no_on_air"] = text
                release(rtc)
                rows.append(row)
                write_table(engine, rows, meta)
                time.sleep(REST_S)
                continue
            t_air = time.monotonic()
            wall_air = time.time()
            page_air = rtc.clock()
            row["air_utc"] = utc(wall_air)
            timeline = []
            pump_while(listener, max(0.0, planned - (time.monotonic() - t_air)),
                       lambda: timeline.append((round(time.monotonic() - t_air, 2), rtc.talk_status())), poll_every=POLL_S)
            row["early"] = tc.early_end(timeline, planned)
            row["timeline_changes"] = [(t, s) for k, (t, s) in enumerate(timeline) if k == 0 or s != timeline[k - 1][1]]
            pairs_end = pair_trace(rtc) if (row["early"] or tc.keep_pairs(index, False)) else None
            page_release = rtc.clock()
            release(rtc)
            ended, took_end = wait_talk_text(rtc, lambda s: s != pages.TALK_ON_AIR_TEXT, 5.0)
            row["end_text"] = ended
            row["end_after_release_s"] = round(took_end, 2) if took_end is not None else None
            pump_while(listener, REST_S)
            frames = _digest(rtc.ctl_received(), page_zero)
            row["ptt_rx"] = tc.ptt_frames(frames, page_air - page_zero - 500.0, page_release - page_zero + 700.0)
            row["counter_delta"] = tc.unrouted_delta(before, edge_counters(rig_control))
            if pairs_end is not None or row["early"]:
                row["ice_pairs_at_end_of_hold"] = pairs_end if pairs_end is not None else pair_trace(rtc)
            row["page_ms"] = {"start": round(page_start - page_zero), "air": round(page_air - page_zero), "release": round(page_release - page_zero)}
            rows.append(row)
            write_table(engine, rows, meta)
    finally:
        listener.close()
        meta["seconds"] = round(time.monotonic() - t_begin, 1) if "t_begin" in locals() else None
        write_table(engine, rows, meta)
    summary = tc.summarize(rows)
    assert not summary["failed"], "%d of %d cycles ended early or never went on air: %s (table: talk-cycles-%s.json)" % (
        len(summary["failed"]), summary["cycles"], [(r["index"], r.get("early") or r.get("no_on_air")) for r in rows if r["index"] in summary["failed"]], engine)
