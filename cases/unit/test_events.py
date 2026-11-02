# SPDX-License-Identifier: MIT
"""dvtest/events.py (per-minute binning, the tally with reconnect gaps, the estimate and ratio, the note, the streams record, the hub
/metrics event families) and cases/soak_support.EventsReader against scripted fake clients. No rig."""
import threading
import time

import pytest

import soak_support as ss
from dvtest import events as ev
from dvtest import metrics

pytestmark = pytest.mark.unit


def frames(*names, start=1):
    return [{"event": n, "id": "e-%d" % (start + i)} for i, n in enumerate(names)]


# ------------------------------------------------------------------ binning

def test_bin_per_minute_whole_and_partial_minutes():
    t0 = 1000.0
    assert ev.bin_per_minute([], t0, t0 + 180) == [0, 0, 0]
    times = [t0, t0 + 59.9, t0 + 60, t0 + 61, t0 + 179.9, t0 + 180]
    assert ev.bin_per_minute(times, t0, t0 + 180) == [2, 2, 2]                    # a stamp at the end goes to the last bin
    assert ev.bin_per_minute(times, t0, t0 + 150) == [2, 2, 0]                    # a partial last minute is its own bin; later stamps are not counted
    assert ev.bin_per_minute(times + [t0 + 120.5], t0, t0 + 150) == [2, 2, 1]
    assert ev.bin_per_minute([t0 - 1, t0 + 500], t0, t0 + 120) == [0, 0]         # outside the interval
    assert ev.bin_per_minute([t0 + 1], t0, t0) == [0]                             # an empty interval still has one bin


def test_gap_seconds_per_minute():
    gaps = [{"from": 1050.0, "to": 1070.0, "reason": "x"}, {"from": 1170.0, "to": 1200.0, "reason": "y"}]
    assert ev.gap_seconds(gaps, 1000.0, 1060.0) == 10.0
    assert ev.gap_per_minute(gaps, 1000.0, 1200.0) == [10.0, 10.0, 10.0, 20.0]       # the partial last bin is clipped to the end
    assert ev.gap_per_minute([], 0.0, 90.0) == [0.0, 0.0]


# ------------------------------------------------------------------ the tally

def test_tally_counts_names_per_kind_and_skips_hello_and_replays():
    tally = ev.EventTally(100.0)
    assert tally.add(101.0, {"event": "hello", "id": "h"}) is False
    assert tally.add(102.0, {"data": "x"}) is False                              # no event name
    assert tally.add(103.0, {"event": "status", "id": "e-1"}) is True
    assert tally.add(104.0, {"event": "room", "id": "e-2"}) is True
    assert tally.add(105.0, {"event": "room", "id": "e-2"}) is False             # a replay after a resume
    assert tally.add(106.0, {"event": "room"}) is True                           # no id: cannot be told from a new one
    events_, gaps, first, last = tally.snapshot(110.0)
    assert [n for _t, n in events_] == ["status", "room", "room"] and gaps == []
    assert first["id"] == "e-1" and last["event"] == "room" and tally.duplicates == 1 and tally.hellos == 1


def test_tally_gap_opens_once_and_closes_on_back():
    tally = ev.EventTally(0.0)
    tally.lost(10.0, "reset")
    tally.lost(12.0, "again")                                                     # the same gap
    tally.back(15.0)
    tally.back(16.0)                                                              # nothing open
    tally.lost(40.0, "ended")
    _e, gaps, _f, _l = tally.snapshot(50.0)                                       # a gap still open is closed at the end of reading
    assert [(g["from"], g["to"], g["reason"]) for g in gaps] == [(10.0, 15.0, "reset"), (40.0, 50.0, "ended")]


# ------------------------------------------------------------------ the estimate, the ratio and the note

def tally_with(rate_per_min, minutes, t0=0.0, gap=None):
    tally = ev.EventTally(t0)
    n = int(rate_per_min * minutes)
    for i in range(n):
        tally.add(t0 + 60.0 * minutes * (i + 0.5) / n, {"event": "room" if i % 2 else "status", "id": "e-%d" % i})
    if gap:
        tally.lost(gap[0], "reset")
        tally.back(gap[1])
    return tally


def test_estimate_ratio_and_ring_note():
    tally = tally_with(100, 15)                                                   # 100 events/min for 15 minutes
    slopes = {"dv-svc-status": {"rss": 116.0, "anon": 140.0}}
    block = ev.hub_events_block(tally, 900.0, 300.0, 900.0, slopes)
    assert block["events_total"] == 1500 and block["by_kind"] == {"room": 750, "status": 750}
    assert len(block["events_per_min_series"]) == 15 and all(x["events"] == 100 for x in block["events_per_min_series"])
    m = block["measured"]
    assert m["events"] == 1000 and m["observed_s"] == 600.0 and m["mean_per_min"] == 100.0
    assert block["events_kib_per_min"] == pytest.approx(116.0)                    # 100 x 1.16
    assert block["ratio_to_rss"] == 1.0 and block["ratio_to_anon"] == pytest.approx(0.829, abs=1e-3) and block["match"] == "matches"
    assert block["cumulative_at_end"] == 1500 and block["ring_full"] is False
    assert block["minutes_to_fill"] == pytest.approx(85.0)                        # (10000 - 1500) / 100
    note = block["events_note"]
    assert "100.0 events/min" in note and "116.0 KiB/min" in note and "ratio 1.00" in note and "would NOT be full" in note and "85 more minutes" in note


def test_ring_full_part_and_above_and_unknown():
    full = ev.hub_events_block(tally_with(250, 50), 3000.0, 600.0, 3000.0, {"dv-svc-status": {"rss": 113.0, "anon": None}})
    assert full["ring_full"] is True and full["cumulative_at_end"] == 12500 and full["minutes_to_fill"] is None
    assert full["ratio_to_rss"] == pytest.approx(290.0 / 113.0, abs=1e-3) and full["match"] == "above" and "would be FULL" in full["events_note"]
    part = ev.hub_events_block(tally_with(20, 10), 600.0, 120.0, 600.0, {"dv-svc-status": {"rss": 113.0, "anon": 100.0}})
    assert part["match"] == "part" and part["ratio_to_rss"] < 0.7 and "only part" in part["events_note"]
    none = ev.hub_events_block(tally_with(20, 10), 600.0, 120.0, 600.0, {"dv-svc-status": {"rss": 0.0, "anon": None}})
    assert none["ratio_to_rss"] is None and none["match"] == "unknown" and "no positive dv-svc-status slope" in none["events_note"]
    assert none["status_anon_slope_kib_per_min"] is None and ev.hub_events_block(tally_with(20, 10), 600.0, 120.0, 600.0)["match"] == "unknown"


def test_a_reconnect_gap_is_not_counted_as_quiet_time():
    tally = ev.EventTally(0.0)
    for i in range(60):                                                           # 1 a second for 60 s inside the measured part, nothing in a 60 s gap
        tally.add(300.0 + i, {"event": "room", "id": "a%d" % i})
    tally.lost(360.0, "reset")
    tally.back(420.0)
    for i in range(60):
        tally.add(420.0 + i, {"event": "room", "id": "b%d" % i})
    block = ev.hub_events_block(tally, 600.0, 300.0, 600.0)
    assert block["reconnects"] == 1 and block["gap_s_total"] == 60.0 and block["gaps"][0]["reason"] == "reset"
    assert block["measured"]["observed_s"] == 240.0 and block["measured"]["mean_per_min"] == 30.0         # 120 events in 240 observed seconds
    gap_minutes = [x for x in block["events_per_min_series"] if x["gap_s"] > 0]
    assert [(x["minute"], x["gap_s"]) for x in gap_minutes] == [(6, 60.0)]
    assert "1 reconnect gaps (60 s)" in block["events_note"]


def test_no_event_read_says_so():
    tally = ev.EventTally(0.0)
    tally.lost(1.0, "OSError: refused")
    block = ev.hub_events_block(tally, 600.0, 300.0, 600.0)
    assert block["events_total"] == 0 and block["measured"]["mean_per_min"] is None            # the whole window is a gap
    assert block["events_note"].startswith("no event was read from the public stream (OSError: refused)")
    short = ev.hub_events_block(ev.EventTally(0.0), 10.0, 20.0, 20.0)
    assert short["measured"]["mean_per_min"] is None and "no events per minute" in short["events_note"]


# ------------------------------------------------------------------ slopes of the child records

def test_slopes_of_reads_soak_and_isolation_records():
    soak_rec = {"rss_kib": {"slope_kib_per_min": 110.0}, "smaps": {"rss": {"slope_per_min": 113.0}, "anon": {"slope_per_min": 99.0}}}
    iso_rec = {"vmrss": {"slope_per_min": 5.0}, "smaps": {}}
    assert ev.slopes_of(soak_rec) == {"vmrss": 110.0, "rss": 113.0, "anon": 99.0}
    assert ev.slopes_of(iso_rec) == {"vmrss": 5.0, "rss": 5.0, "anon": None}            # no smaps keys: Rss falls back to VmRSS
    assert ev.slopes_of(None) == {"vmrss": None, "rss": None, "anon": None}
    zero = ev.slopes_of({"vmrss": {"slope_per_min": 0.0}})
    assert zero["vmrss"] == 0.0 and zero["rss"] == 0.0                                  # a flat child is 0, not missing


def test_child_slopes_sums_the_dv_room_ranks():
    children = {"dv-router": {"vmrss": {"slope_per_min": 137.0}},
                "dv-room.01": {"vmrss": {"slope_per_min": 10.0}}, "dv-room.02": {"vmrss": {"slope_per_min": 2.5}},
                "dv-roomy": {"vmrss": {"slope_per_min": 999.0}}}
    got = ev.child_slopes(children)
    assert got["dv-router"]["rss"] == 137.0 and got["dv-svc-status"]["rss"] is None
    assert got["dv-room"]["rss"] == 12.5 and got["dv-room"]["ranks"] == {"dv-room.01": 10.0, "dv-room.02": 2.5}


# ------------------------------------------------------------------ streams

def test_streams_per_module_per_minute_and_slope_per_stream():
    t0 = 1000.0
    results = [{"lane": "dstar", "ok": True, "t_start": t0 + 10}, {"lane": "dstar", "ok": True, "t_start": t0 + 70},
               {"lane": "dstar", "ok": False, "t_start": t0 + 80},                    # not delivered
               {"lane": "usrp-a", "ok": True, "t_start": t0 + 100}, {"lane": "usrp-a", "ok": True, "t_start": t0 + 400},      # outside
               {"lane": "m17", "ok": True, "t_start": t0 + 150}]
    slopes = {"dv-router": {"rss": 137.0, "anon": 60.0}, "dv-svc-status": {"rss": 113.0, "anon": None}, "dv-room": {"rss": 0.0, "anon": None}}
    block = ev.streams_block(results, {"dstar": "D", "usrp-a": "A", "m17": "J"}, t0 + 60, t0 + 360, slopes)
    assert block["streams_total"] == 3 and block["streams_per_min"] == 0.6 and block["run_total"] == 5 and block["scheduled_total"] == 6
    assert block["per_lane"] == {"dstar": 1, "m17": 1, "usrp-a": 1} and block["per_module"] == {"A": 1, "D": 1, "J": 1} and block["per_room"] == block["per_module"]
    assert block["window_s"] == 300.0
    assert block["slope_per_stream"]["dv-router"] == {"rss_kib_per_min": 137.0, "anon_kib_per_min": 60.0, "rss_kib_per_stream": pytest.approx(228.333, abs=1e-3),
                                                      "anon_kib_per_stream": 100.0}
    assert block["slope_per_stream"]["dv-svc-status"]["anon_kib_per_stream"] is None and block["slope_per_stream"]["dv-room"]["rss_kib_per_stream"] == 0.0


def test_no_streams_means_no_slope_per_stream():
    block = ev.streams_block([], {}, 0.0, 600.0, {"dv-router": {"rss": 5.0, "anon": None}})
    assert block["streams_total"] == 0 and block["streams_per_min"] == 0.0 and block["slope_per_stream"]["dv-router"]["rss_kib_per_stream"] is None
    assert ev.streams_block([], {}, 5.0, 5.0)["streams_per_min"] is None


# ------------------------------------------------------------------ hub /metrics families with "event" in the name

def test_event_families_of_the_hub_metrics():
    text = ("dv_status_events_total{kind=\"a\"} 4\ndv_status_events_total{kind=\"b\"} 6\ndv_status_events_len 120\n"
            "dv_status_events_bucket{le=\"1\"} 3\ndv_edge_drops_total{reason=\"event_dropped\"} 1\ndv_pkt_items_total 9\n")
    assert ev.events_metric_values(metrics.parse(text)) == {"dv_status_events_total": 10.0, "dv_status_events_len": 120.0}
    assert ev.events_metric_values(metrics.parse("dv_pkt_items_total 9\ndv_edge_drops_total{reason=\"event_dropped\"} 1\n")) == {}
    rows = [(100.0, {"dv_status_events_total": 10.0, "dv_status_events_len": 120.0}), (400.0, {"dv_status_events_total": 70.0, "dv_status_events_len": 180.0}),
            (900.0, {"dv_status_events_total": 500.0})]
    got = ev.events_metric_summary(rows, 100.0, 400.0)
    assert got["names_seen"] == ["dv_status_events_len", "dv_status_events_total"]
    assert got["series"]["dv_status_events_total"]["delta"] == 60.0 and got["series"]["dv_status_events_total"]["per_min"] == 12.0
    assert got["series"]["dv_status_events_len"]["kind"] == "gauge" and "per_min" not in got["series"]["dv_status_events_len"]
    none = ev.events_metric_summary([], 0.0, 1.0)
    assert none["names_seen"] == [] and "no family" in none["note"]


# ------------------------------------------------------------------ the reader against scripted clients

class Scripted:
    """sse() plays one script step per call: ("frames", [..], then) with then "raise" / "end" / "hold"; records the paths. Has on_frame."""

    def __init__(self, steps, hold_s=0.25):
        self.steps, self.paths, self.hold_s, self.n = steps, [], hold_s, 0

    def sse(self, path, max_events=1, timeout=15.0, until=None, on_frame=None):
        self.paths.append(path)
        step = self.steps[min(self.n, len(self.steps) - 1)]
        self.n += 1
        got = []
        for fr in step[0]:
            got.append(fr)
            if on_frame:
                on_frame(fr)
        if step[1] == "raise":
            raise ConnectionResetError("reset by peer")
        if step[1] == "hold":
            time.sleep(self.hold_s)
        return 200, "text/event-stream", got


def run_reader(client_factory, seconds, **kw):
    reader = ss.EventsReader(client_factory, backoff=(0.01, 0.02), **kw)
    reader.start()
    time.sleep(seconds)
    reader.stop()
    return reader


def test_reader_counts_resumes_with_the_last_id_and_records_a_reset_as_a_gap():
    hello = [{"event": "hello", "id": "h-1"}]
    client = Scripted([(hello + frames("status", "room"), "raise"),                         # a reset after two events
                       (hello + frames("room", start=2), "hold")], hold_s=0.3)                # resumed: e-2 replayed, then a quiet but healthy stream
    reader = run_reader(lambda: client, 0.5, chunk_s=0.3)
    tally = reader.tally
    names = [n for _t, n in tally.snapshot(time.time())[0]]
    assert names == ["status", "room"] and tally.duplicates >= 1 and tally.hellos >= 2          # e-2 replayed, counted once
    assert client.paths[0] == ss.EventsReader.PATH and client.paths[1].endswith("?last_event_id=e-2")
    gaps = tally.snapshot(time.time())[1]
    assert len(gaps) == 1 and gaps[0]["reason"].startswith("ConnectionResetError") and 0.0 <= gaps[0]["to"] - gaps[0]["from"] < 0.3
    assert tally.rollovers >= 1                                                               # the held stream ran its chunk: a planned roll-over, no gap


def test_reader_survives_a_client_factory_that_raises_and_a_stream_that_ends_early():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("no route")
        return Scripted([(frames("room"), "end"), ([], "end")])

    reader = run_reader(factory, 0.4, chunk_s=60.0)
    events_, gaps, _f, _l = reader.tally.snapshot(time.time())
    assert [n for _t, n in events_] == ["room"] and calls["n"] >= 3
    assert gaps[0]["reason"].startswith("OSError: no route") and gaps[-1]["reason"].startswith("stream ended")
    assert reader.tally.errors[0].startswith("OSError: no route")
    assert reader.thread is not None and not reader.thread.is_alive()


def test_reader_without_on_frame_stamps_the_chunk_when_it_ends():
    class Plain:
        def __init__(self):
            self.n = 0

        def sse(self, path, max_events=1, timeout=15.0, until=None):
            self.n += 1
            time.sleep(0.15)
            return 200, "text/event-stream", [{"event": "hello", "id": "h"}] + frames("room", "status", start=10 * self.n)

    client = Plain()
    reader = run_reader(lambda: client, 0.4, chunk_s=0.15)
    events_, gaps, first, _l = reader.tally.snapshot(time.time())
    assert len(events_) >= 2 and first["event"] == "room" and gaps == []
    assert reader.tally.rollovers >= 1


def test_reader_stop_is_idempotent_and_ignores_late_frames():
    gate = threading.Event()

    class Slow:
        def sse(self, path, max_events=1, timeout=15.0, until=None, on_frame=None):
            gate.wait(2.0)
            on_frame({"event": "room", "id": "late"})
            return 200, "text/event-stream", []

    reader = ss.EventsReader(lambda: Slow(), chunk_s=5.0)
    reader.start()
    t1 = reader.stop(join_s=0.05)
    t2 = reader.stop(join_s=0.05)
    gate.set()
    time.sleep(0.2)
    assert t1 == t2 and reader.tally.snapshot(time.time())[0] == []


def test_record_events_and_streams_never_raises_and_counts_only_overs():
    class Traffic:
        _lock = threading.Lock()
        results = [{"lane": "dstar", "ok": True, "t_start": 150.0}, {"lane": "ysf", "ok": True, "t_start": 160.0}]

    class Sampler:
        event_metric = [(100.0, {"dv_status_events_len": 3.0})]

    reader = ss.EventsReader(lambda: None)
    hub, streams = ss.record_events_and_streams(reader, Sampler(), Traffic(), {"dstar": "D", "ysf": "C"}, 100.0, 200.0, {"dv-router": {"vmrss": {"slope_per_min": 10.0}}})
    assert streams["streams_total"] == 2 and streams["per_module"] == {"C": 1, "D": 1} and streams["slope_per_stream"]["dv-router"]["rss_kib_per_stream"] == pytest.approx(8.333, abs=1e-3)      # 2 streams in 100 s = 1.2 a minute
    assert hub["hub_metric"]["names_seen"] == ["dv_status_events_len"] and "events_note" in hub
    hub2, streams2 = ss.record_events_and_streams(reader, Sampler(), Traffic(), {}, 100.0, 200.0, {"dv-router": {"vmrss": {"slope_per_min": 10.0}}}, method="cycle")
    assert streams2["streams_total"] == 0 and "no voice" in streams2["note"]
    bad_hub, bad_streams = ss.record_events_and_streams(reader, None, Traffic(), {}, 100.0, 200.0, {})
    assert "could not be computed" in bad_hub["events_note"] and bad_streams["streams_total"] is None
    none_hub, _s = ss.record_events_and_streams(None, Sampler(), None, {}, 100.0, 200.0, {})
    assert none_hub["events_note"] == "the events stream was not read"
