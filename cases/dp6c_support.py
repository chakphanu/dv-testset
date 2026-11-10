# SPDX-License-Identifier: MIT
"""Shared pieces of the DP6c / DP19 rig cases (cases/test_dp6c_rf.py, test_dp6c_aprsis.py, test_dp19_locator.py, test_dp6c_browser.py): notes, pacing per base
callsign for position, weather and telemetry, injection at the fake KISS TNC, and reading the public map rows (state and SSE). The pure judging code is
dvtest/dp6c.py; the position helpers it builds on are cases/position_support.py. Case table, run commands and runtimes: docs/dp6c-cases.md."""
import contextlib
import os
import time
import types

from dvtest import dp6c, kiss_tnc
from dvtest.proto import aprs

import data_support as ds
import position_support as ps

LOG = "dp6c.txt"
SETTING = "require_ident=%s" % os.environ.get("DVT_REQUIRE_IDENT", "true")
WAIT_S = 25.0                       # a row on the public state or SSE after the injection: the hub and the web process take seconds
ABSENT_S = 8.0                      # how long "nothing appears" is watched after the counters moved
COUNTER_WAIT_S = 45.0               # the worker and the controller publish their counters every 10 s, the hub scrape lags by about 10 s
# weather and telemetry have their own buckets (1 per 30 s per base callsign, burst 1); the pacer keeps position, weather and telemetry apart
PACER = ds.Pacer(gaps=dict(ds.MIN_GAP_S, weather=ds.MIN_GAP_S["position"], telemetry=ds.MIN_GAP_S["position"]))


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


def pace(call, kinds):
    for kind in kinds:
        PACER.wait(call, kind)


def offered(call, kinds):
    for kind in kinds:
        PACER.offered(call, kind)


def send_rf(tnc, call, info, kinds=("position",), digis=("WIDE1-1",), spend=True):
    """The fake TNC rf-a hears `CALL>APDVT1,WIDE1-1:<info>`; the base callsign is paced per item kind first. Every call of a case sends a different
    information field (the worker deduplicates identical frames). `spend=False`: a frame that is DROPPED before the rate step (privacy) spends no credit,
    so it is not paced."""
    base = ps.base_call(call)
    if spend:
        pace(base, kinds)
    assert tnc.inject(kiss_tnc.ui_frame(call, dp6c.TOCALL, info, digis=digis)), "the fake TNC rf-a has no connection to inject into"
    if spend:
        offered(base, kinds)


def foreign_definitions(target_call):
    """Four definition messages for `target_call` with other names, units, coefficients and sense (dp6c.FOREIGN_*): sent by a DIFFERENT base they must change nothing."""
    return [aprs.definition_info(target_call, "PARM", dp6c.FOREIGN_PARM), aprs.definition_info(target_call, "UNIT", dp6c.FOREIGN_UNIT),
            aprs.definition_info(target_call, "EQNS", dp6c.FOREIGN_EQNS), aprs.definition_info(target_call, "BITS", dp6c.FOREIGN_BITS, dp6c.FOREIGN_PROJECT)]


def send_definitions(tnc, definer, target_call, infos=None):
    """The four G10 definition messages (or `infos`) from `definer`, one second apart so that the worker has stored each one when the next arrives. They never
    become items. A definition is accepted only when the sender's BASE equals the addressee's base (case-insensitive) and applies to the T# reports of that exact SSID."""
    for info in (aprs.telemetry_definitions(target_call) if infos is None else infos):
        assert tnc.inject(kiss_tnc.ui_frame(definer, dp6c.TOCALL, info, digis=("WIDE1-1",)))
        time.sleep(1.0)


def map_rows(anon, room=dp6c.ROOM):
    """Every row of the public state `rooms[].positions[]` of the room: positions, objects, weather and telemetry."""
    return ps.state_rows(ps.public_state(anon), room)


def wait_map(anon, pred, timeout=WAIT_S, poll=1.0, room=dp6c.ROOM):
    """(matching rows, all rows) of the public map once `pred(rows)` returns a non-empty list, else after the timeout (an empty list)."""
    end = time.monotonic() + timeout
    while True:
        rows = map_rows(anon, room)
        hits = pred(rows)
        if hits or time.monotonic() >= end:
            return hits, rows
        time.sleep(poll)


def wait_row(anon, callsign, kind, timeout=WAIT_S):
    """The first public map row of the exact guard identity and kind, else None."""
    hits, _rows = wait_map(anon, lambda rows: dp6c.station_rows(rows, callsign, kind), timeout)
    return hits[0] if hits else None


def sse_rows(watch, since=0.0, callsign=None, kind=None):
    """The map `data` events (position, object, weather, telemetry) of a SseWatch after `since`, optionally of one exact guard identity and kind."""
    return dp6c.station_rows([o for _t, o in watch.data_events(since)], callsign, kind)


@contextlib.contextmanager
def feeds(web_ctx, seconds=600.0):
    """The public SSE (anonymous) open and the operator feed baseline taken first."""
    baseline = ps.operator_baseline(web_ctx.op)
    with ds.SseWatch(web_ctx.anon(), ps.PUBLIC_EVENTS, seconds=seconds) as pub:
        assert pub.ready(), "the public SSE gave no hello: %s" % pub.error
        yield types.SimpleNamespace(pub=pub, baseline=baseline, t0=time.monotonic())


def operator_rows(web_ctx, fd, kind=None, callsign=None):
    """The map rows of the operator feed (observer scope: `data` events, the weather and telemetry ones included) after the baseline of `feeds`."""
    _hits, rows = ps.wait_operator_rows(web_ctx.op, fd.baseline, lambda r: False, timeout=1.0)
    return dp6c.station_rows(rows, callsign, kind)


def counter_total(hub, name, **labels):
    return hub.total(name, **labels)


class OptoutCounters:
    """dv_pkt_items_total{kind, result=optout} per kind and the controller's dv_room_data_total{result=optout} (denied/13) before a send."""

    def __init__(self, hub, kinds):
        self.hub, self.kinds = hub, tuple(kinds)
        self.items = {k: hub.total("dv_pkt_items_total", kind=k, result="optout") for k in self.kinds}
        self.room = hub.total("dv_room_data_total", result="optout")

    def delta(self):
        return {k: self.hub.total("dv_pkt_items_total", kind=k, result="optout") - self.items[k] for k in self.kinds}, \
            self.hub.total("dv_room_data_total", result="optout") - self.room

    def wait(self, minimum=1, timeout=COUNTER_WAIT_S, room_required=True):
        """(per-kind deltas, room delta, reached): until every kind rose by `minimum` and, when `room_required`, the room counter by the number of kinds times it. A private
        LOCATOR STATUS is withheld by the WORKER before submission (data.md 13.4, DP19): the worker counter rises and the controller's never does, so DP19 waits with
        `room_required=False`."""
        end = time.monotonic() + timeout
        while True:
            items, room = self.delta()
            reached = all(v >= minimum for v in items.values()) and (not room_required or room >= minimum * len(self.kinds))
            if reached or time.monotonic() >= end:
                return items, room, reached
            time.sleep(2.0)


def assert_absent_everywhere(web_ctx, fd, base, label, window=ABSENT_S):
    """After `window` s: no public map row of the BASE callsign (any SSID, any kind) in the public state, on the public SSE or in the operator feed."""
    time.sleep(window)
    state = dp6c.base_rows(map_rows(web_ctx.anon()), base)
    sse = dp6c.base_rows([o for _t, o in fd.pub.data_events(fd.t0)], base)
    op = dp6c.base_rows(operator_rows(web_ctx, fd), base)
    assert not state, "%s: rows of the dropped base %s are in the anonymous public state: %s" % (label, base, [(r["callsign"], r["item_type"]) for r in state])
    assert not sse, "%s: rows of the dropped base %s reached the public SSE: %s" % (label, base, [(r["callsign"], r["item_type"]) for r in sse])
    assert not op, "%s: rows of the dropped base %s reached the operator feed: %s" % (label, base, [(r["callsign"], r["item_type"]) for r in op])


class SseReplay(ds.SseWatch):
    """A SseWatch that reconnects with a `Last-Event-ID` request header: the replay of the public SSE after the event `last_id`."""

    def __init__(self, client, path, last_id, seconds=60.0):
        super().__init__(client, path, seconds)
        self.last_id = last_id

    def _run(self):
        try:
            self.client.sse(self.path, max_events=1 << 30, timeout=self.seconds, on_frame=self._on_frame, stop=self.stop,
                            headers={"Last-Event-ID": self.last_id})
        except Exception as exc:  # noqa: BLE001 - reported through .error
            self.error = exc


def first_event_id(watch):
    """The id of the first frame of a SseWatch that has one (the replay cursor to reconnect with), else None."""
    for _t, frame in watch.snapshot():
        if frame.get("id"):
            return frame["id"]
    return None


def status_rows(rows, base=None, word=None):
    """The `status` rows of a list (public SSE data events or operator feed rows), optionally of one BASE callsign and containing `word` in the text."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("item_type") != "status":
            continue
        if base is not None and ps.base_call(str(r.get("callsign", ""))) != base.upper():
            continue
        if word is not None and word not in str(r.get("body", "")):
            continue
        out.append(r)
    return out


def sse_status(watch, since=0.0, base=None, word=None):
    return status_rows([o for _t, o in watch.data_events(since)], base, word)


def wait_status(watch, since, base, word, timeout=WAIT_S):
    """The first public SSE status of the base callsign containing `word`, else None."""
    hit = watch.wait_data(lambda o: bool(status_rows([o], base, word)), timeout, since=since)
    return None if hit is None else hit[1]


# ---------------------------------------------------------------- DP19b: spatial status rows (public state `spatial_status`, SSE, message history)

def state_spatial(anon, room=dp6c.ROOM):
    """The `spatial_status` array of the room in the anonymous public state ([] when the room or the key is absent)."""
    for item in ps.public_state(anon).get("rooms", []) or []:
        if item.get("room") == room:
            return [r for r in item.get("spatial_status", []) or [] if isinstance(r, dict)]
    return []


def has_spatial_status_key(anon, room=dp6c.ROOM):
    """True when the public state of the room carries `spatial_status` (the probe for an image with DP19b: the key is required there)."""
    for item in ps.public_state(anon).get("rooms", []) or []:
        if item.get("room") == room:
            return "spatial_status" in item
    return False


def message_rows(rows, base, item_type=None, body=None):
    """The status / text rows of a list (SSE events, state array or message history) of one BASE callsign, optionally of one item type and with `body` in the text."""
    return [r for r in rows if isinstance(r, dict) and r.get("item_type") in ("status", "text") and ps.base_call(str(r.get("callsign", ""))) == base.upper()
            and (item_type is None or r.get("item_type") == item_type) and (body is None or body in str(r.get("body", "")))]


def wait_state_spatial(anon, base, present, timeout=WAIT_S, poll=1.0, room=dp6c.ROOM):
    """(seconds waited, rows) until the state's spatial_status holds (present=True) or no longer holds (present=False) a row of the base, else (None, rows)."""
    start = time.monotonic()
    while True:
        rows = message_rows(state_spatial(anon, room), base)
        if bool(rows) == present:
            return time.monotonic() - start, rows
        if time.monotonic() - start >= timeout:
            return None, rows
        time.sleep(poll)


def wait_history(reader, base, pred, timeout=WAIT_S, poll=1.0, room=dp6c.ROOM):
    """(seconds waited, rows of the base) until `pred(rows)` holds for the user message history of the room, else (None, rows)."""
    from dvtest import web
    start = time.monotonic()
    while True:
        rows = message_rows(web.room_messages(reader, room), base)
        if pred(rows):
            return time.monotonic() - start, rows
        if time.monotonic() - start >= timeout:
            return None, rows
        time.sleep(poll)


def send_text_message(tnc, call, body, room_name="XLX999B"):
    """An APRS message to the room name heard on the bound port: the room TEXT of the DP19b look-alike case (no message id, so never acked)."""
    from dvtest import kiss_tnc as kt
    send_rf(tnc, call, kt.aprs_message_info(room_name, body).decode("latin-1"), kinds=("text",))
