# SPDX-License-Identifier: MIT
"""DP19b (image after wip-7bd0082, merge 52db4641): every status row carries an explicit boolean `spatial` copied from the worker's classification of the original APRS information
bytes; the public state (`rooms[].spatial_status`), the live and replayed SSE and the user message history withhold a SPATIAL status under the same current hidden-base, opt-out and
positions-enabled policy as positions, and before the hidden set is known. Plain status and text (also the look-alike TEXT `IO91SX/G`) are unaffected. Specification: DV-Router
data.md 13.4, web.md (message panel, "Built (DP19b)"), openapi-web.yaml (StatusMessage, SpatialStatus, Rooms.spatial_status), review 247 "Rig case to request"; case table, run shapes,
commands and runtimes: docs/dp19b-cases.md. Probe, no switch: the cases skip on an image whose public state has no `spatial_status` key. NOT RUN ON A RIG yet.

  DP19b-1  from one base: a status `>IO91SX/G` (SSID 7), a plain status `>Ready` and the text message `IO91SX/G` (SSID 9): all three on the public SSE, in the user message history
           and the state's `spatial_status` holds only the first; only the first has spatial true, the plain status spatial false, the text no `spatial` key
  DP19b-2  hide_position of the base: within one state refresh (policy age at most 10 s) the spatial status leaves the state, a fresh SSE replay and the user history; the plain status
           and the text stay (the browser panel half is cases/test_dp19b_browser.py)
  DP19b-3  F1: after 60 s without any page traffic a position and a spatial status of an unhidden base both arrive LIVE on an open SSE stream
  DP19b-4  (opt-in, DVT_DP19B_ACCOUNTS_DOWN=on, LAST) dv-svc-web restarted while its account store is unreadable: spatial rows are withheld, text and plain status are delivered
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

CASE = "DP19b"
SPATIAL, PLAIN, TEXT = ">IO91SX/G", ">Ready", "IO91SX/G"         # the three items of review 247
SPATIAL_BODY, PLAIN_BODY = "IO91SX/G", "Ready"                 # the status text as the public rows carry it (without the `>`)
HIDE_WAIT_S = 25.0                                            # one state refresh: the policy is at most 10 s old, the state is cached for 2 s
LIVE_WAIT_S = 20.0
QUIET_S = 60.0
BASE_VISIBLE, BASE_HIDE, BASE_QUIET, BASE_DOWN = "N0DPA", "N0DPB", "N0DPC", "N0DPD"
ACCOUNTS_FILE = "web/accounts.jsonl"                          # out/<run>/sut-state/web/accounts.jsonl: the persisted accounts and hide flags of dv-svc-web
SWITCH = "DVT_DP19B_ACCOUNTS_DOWN"


@pytest.fixture(scope="module")
def image(web_ctx, shape, rf):
    """Skips on an image without DP19b (no `spatial_status` in the public state of the room) and without the bound RF port; returns the anonymous client."""
    if not shape.positions:
        pytest.skip("this run has positions = false: a spatial status is dropped for every base")
    anon = web_ctx.anon()
    if not sp.has_spatial_status_key(anon):
        pytest.skip("the public state of room %s has no `spatial_status`: the image has no DP19b (merge 52db4641)" % dp6c.ROOM)
    return anon


def inject_trio(rf, base, suffix=""):
    """The three items: a locator status from `base`-7, a plain status and the text message from `base`-9 (status 1 per 10 s per base callsign: paced)."""
    sp.send_rf(rf, base + "-7", SPATIAL + suffix, kinds=("status",))
    sp.send_rf(rf, base + "-9", PLAIN + suffix, kinds=("status",))
    sp.send_text_message(rf, base + "-9", TEXT + suffix)


def sse_items(watch, base, since=0.0):
    rows = [o for _t, o in watch.data_events(since)]
    return (sp.message_rows(rows, base, "status", SPATIAL_BODY), sp.message_rows(rows, base, "status", PLAIN_BODY), sp.message_rows(rows, base, "text", TEXT))


def wait_trio_on_sse(watch, base, since, timeout=sp.WAIT_S):
    end = time.monotonic() + timeout
    while True:
        got = sse_items(watch, base, since)
        if all(got) or time.monotonic() >= end:
            return got
        time.sleep(0.5)


def check_flags(spatial, plain, text):
    assert spatial[0].get("spatial") is True, "the locator status row does not carry spatial true: %s" % spatial[0]
    assert plain[0].get("spatial") is False, "the plain status row does not carry spatial false: %s" % plain[0]
    assert "spatial" not in text[0] and text[0].get("item_type") == "text", "the text row carries a spatial key or is not text: %s" % text[0]


# ================================================================ DP19b-1

def test_dp19b_1_three_items_are_visible_and_only_the_locator_status_is_spatial(image, target, web_ctx, rf):
    """review 247 case 1: `>IO91SX/G` from N0DPA-7, `>Ready` and the text `IO91SX/G` from N0DPA-9. All three reach the public SSE and the user message history (the data of the
    message panel); the first has spatial true, the plain status spatial false, the text no `spatial` key; the state's `spatial_status` holds the first only (plain status and text stay
    on the message history and the data feed: openapi SpatialStatus)."""
    case = CASE + "-1"
    base = BASE_VISIBLE
    web_ctx.user(base, may_tx=False)
    with sp.feeds(web_ctx) as fd:
        inject_trio(rf, base)
        spatial, plain, text = wait_trio_on_sse(fd.pub, base, fd.t0)
        assert spatial and plain and text, "on the public SSE: spatial status %s, plain status %s, text %s" % (bool(spatial), bool(plain), bool(text))
        check_flags(spatial, plain, text)
        _s, in_state = sp.wait_state_spatial(image, base, True)
        assert in_state, "the spatial status is not in the public state's spatial_status"
        assert not [r for r in in_state if r.get("spatial") is not True or r.get("body") != SPATIAL_BODY], "spatial_status holds more than the locator status: %s" % in_state
        _t, history = sp.wait_history(web_ctx.reader, base, lambda rows: len(rows) >= 3)
        kinds = sorted((r["item_type"], r.get("spatial")) for r in history)
        sp.note(target, case, "RECORD", "message history rows of %s: %s; state spatial_status: %s" % (base, kinds, [(r["callsign"], r["body"]) for r in in_state]))
        assert kinds == [("status", False), ("status", True), ("text", None)], "message history of %s: %s" % (base, kinds)


# ================================================================ DP19b-2

def test_dp19b_2_hide_position_withdraws_the_spatial_status_everywhere(image, target, web_ctx, rf):
    """review 247 case 2 (hide_position; the position_optout + reload variant is a GAP): once the account of the base is hidden, within one state refresh the spatial status is gone
    from the anonymous public state, from a fresh SSE replay and from the user message history; the plain status and the text stay in the history. Clearing the flag at the end."""
    case = CASE + "-2"
    base = BASE_HIDE
    web_ctx.user(base, may_tx=False)
    try:
        with sp.feeds(web_ctx) as fd:
            inject_trio(rf, base, " dpb")
            spatial, plain, text = wait_trio_on_sse(fd.pub, base, fd.t0)
            assert spatial and plain and text, "before the hide: spatial %s, plain %s, text %s" % (bool(spatial), bool(plain), bool(text))
            assert sp.wait_state_spatial(image, base, True)[1], "the spatial status never reached the public state"
            assert sp.wait_history(web_ctx.reader, base, lambda rows: len(rows) >= 3)[0] is not None
            cursor = sp.first_event_id(fd.pub)
        set_hide_position(web_ctx, base, True)
        t_state, left = sp.wait_state_spatial(image, base, False, timeout=HIDE_WAIT_S)
        t_hist, rows = sp.wait_history(web_ctx.reader, base, lambda r: not [x for x in r if x.get("spatial") is True], timeout=HIDE_WAIT_S)
        sp.note(target, case, "RECORD", "spatial status left the state after %s s and the user history after %s s" % (
            None if t_state is None else "%.1f" % t_state, None if t_hist is None else "%.1f" % t_hist))
        assert t_state is not None, "the spatial status of the hidden base is still in the public state after %.0f s: %s" % (HIDE_WAIT_S, left)
        assert t_hist is not None, "the spatial status of the hidden base is still in the user message history after %.0f s" % HIDE_WAIT_S
        kinds = sorted((r["item_type"], r.get("spatial")) for r in rows)
        assert kinds == [("status", False), ("text", None)], "after the hide the history of %s must keep the plain status and the text only: %s" % (base, kinds)
        if cursor:
            with sp.SseReplay(web_ctx.anon(), ps.PUBLIC_EVENTS, cursor, seconds=30.0) as replay:
                assert replay.ready(), "the replaying public SSE gave no frame: %s" % replay.error
                time.sleep(6.0)
                got = sse_items(replay, base)
            sp.note(target, case, "RECORD", "replay after %s: spatial %d, plain %d, text %d" % (cursor, len(got[0]), len(got[1]), len(got[2])))
            assert not got[0], "the SSE replay carries the spatial status of the hidden base: %s" % got[0]
        else:
            sp.note(target, case, "RECORD", "the public SSE frames carry no id: the replay check is skipped")
    finally:
        try:
            set_hide_position(web_ctx, base, False)
        except (AssertionError, web.WebError) as exc:
            sp.note(target, case, "RECORD", "could not clear hide_position at the end: %s" % exc)


# ================================================================ DP19b-3

def test_dp19b_3_a_quiet_node_still_delivers_position_and_spatial_status_live(image, target, web_ctx, rf):
    """review 247 case 3 (F1): the SSE callback used a policy cache that only requests refreshed, so on a quiet node every live position and spatial status was dropped. With an open SSE
    stream and NO other traffic to the web service for 60 s, a position and a spatial status of an unhidden base both arrive live within 20 s (the periodic refresh keeps the policy fresh)."""
    case = CASE + "-3"
    base = BASE_QUIET
    with sp.ds.SseWatch(web_ctx.anon(), ps.PUBLIC_EVENTS, seconds=QUIET_S + 90.0) as pub:
        assert pub.ready(), "the public SSE gave no hello: %s" % pub.error
        time.sleep(QUIET_S)                                                  # no state, history or page request meanwhile
        t0 = time.monotonic()
        sp.send_rf(rf, base + "-5", ps.position_info(ps.APRS_LAT, ps.APRS_LON, 0, "/>", "dp19b quiet"), kinds=("position",))
        sp.send_rf(rf, base + "-5", SPATIAL + " dpc", kinds=("status",))
        position = pub.wait_data(lambda o: o.get("item_type") == "position" and ps.base_call(str(o.get("callsign", ""))) == base, LIVE_WAIT_S, since=t0)
        status = pub.wait_data(lambda o: bool(sp.message_rows([o], base, "status", SPATIAL_BODY)), LIVE_WAIT_S, since=t0)
    sp.note(target, case, "RECORD", "after %.0f s without page traffic: position %s s, spatial status %s s on the open SSE" % (
        QUIET_S, None if position is None else "%.1f" % (position[0] - t0), None if status is None else "%.1f" % (status[0] - t0)))
    assert position is not None, "the position of an unhidden base never arrived live on the SSE after a quiet minute (F1: the policy cache was stale)"
    assert status is not None, "the spatial status of an unhidden base never arrived live on the SSE after a quiet minute (F1)"
    assert status[1].get("spatial") is True, status[1]


# ================================================================ DP19b-4

@pytest.fixture
def accounts_down(target, rig_control, web_base_url, web_ca):
    """The account store of dv-svc-web made unreadable (the file replaced by a DIRECTORY of the same name, so that a read fails instead of finding nothing) and the service restarted;
    undone at the end, the service restarted again. Opt-in: DVT_DP19B_ACCOUNTS_DOWN=on. Yields a function that restarts the service and waits for it."""
    if os.environ.get(SWITCH, "").lower() not in ("on", "1", "true", "yes"):
        pytest.skip("opt-in and destructive (web sessions end, the account store is swapped for a while): set %s=on and name this case LAST in the run" % SWITCH)
    store = Path(target.out) / "sut-state" / ACCOUNTS_FILE
    if not store.is_file():
        pytest.skip("no account store at %s as the tester sees it (the web profile mounts out/<run>/sut-state)" % store)
    held = store.with_name(store.name + ".dvt-hold")
    probe = web.WebClient(web_base_url, web_ca, timeout=4)

    def restart(budget=150.0):
        rig_control.kill_proc("xlxd-sut", "dv-svc-web", "KILL")
        time.sleep(2.0)
        end, good = time.monotonic() + budget, 0
        while good < 2:
            if time.monotonic() > end:
                pytest.fail("dv-svc-web did not come back within %d s" % budget)
            try:
                good = good + 1 if probe.get(ps.PUBLIC_STATE, pace=False).status == 200 else 0
            except web.WebError:
                good = 0
            time.sleep(0.5)

    swapped = False
    try:
        try:
            store.rename(held)
            store.mkdir()
            swapped = True
        except OSError as exc:
            pytest.skip("cannot swap the account store (%s)" % exc)
        yield restart
    finally:
        if swapped:
            try:
                store.rmdir()
                held.rename(store)
            finally:
                restart()


def test_dp19b_4_spatial_rows_are_withheld_while_accounts_are_unavailable(accounts_down, target, web_ctx, rf):
    """review 247 case 4: dv-svc-web restarted with its accounts unavailable has an UNKNOWN hidden set: spatial rows (status, and by the same predicate positions) are withheld, text and
    plain status are still delivered. After the account store is back and the service restarted, the control (a spatial status of another base) is delivered again. Message history
    needs a session and sessions end with the restart, so the surfaces are the anonymous SSE and state."""
    case = CASE + "-4"
    accounts_down()                                                              # the restart with the store unreadable
    anon = web_ctx.anon()
    with sp.ds.SseWatch(anon, ps.PUBLIC_EVENTS, seconds=200.0) as pub:
        assert pub.ready(), "the public SSE gave no hello after the restart: %s" % pub.error
        t0 = time.monotonic()
        inject_trio(rf, BASE_DOWN)
        end = time.monotonic() + sp.WAIT_S
        while time.monotonic() < end and not (sp.message_rows([o for _t, o in pub.data_events(t0)], BASE_DOWN, "text", TEXT)
                                              and sp.message_rows([o for _t, o in pub.data_events(t0)], BASE_DOWN, "status", PLAIN_BODY)):
            time.sleep(0.5)
        time.sleep(sp.ABSENT_S)
        spatial, plain, text = sse_items(pub, BASE_DOWN, t0)
    sp.note(target, case, "RECORD", "accounts unavailable: spatial %d, plain %d, text %d on the SSE; state spatial_status %s" % (
        len(spatial), len(plain), len(text), [r["callsign"] for r in sp.message_rows(sp.state_spatial(anon), BASE_DOWN)]))
    assert plain and text, "text and plain status must still be delivered while accounts are unavailable (plain %s, text %s)" % (bool(plain), bool(text))
    assert not spatial, "a spatial status was delivered with an unknown hidden set: %s" % spatial
    assert not sp.message_rows(sp.state_spatial(anon), BASE_DOWN), "a spatial status is in the public state with an unknown hidden set"
