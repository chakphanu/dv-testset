# SPDX-License-Identifier: MIT
"""cases/data_support.py: the pacer, the timeline, the SSE watcher and the helpers moved out of test_data_ident.py."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import data_support as ds  # noqa: E402

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_pacer_waits_per_base_callsign_and_kind():
    clock = Clock()
    pacer = ds.Pacer(clock=clock, sleep=clock.sleep)
    assert pacer.wait("N0TEST", "status") == 0.0
    clock.now += 4
    assert pacer.wait("N0TEST-7", "status") == pytest.approx(ds.MIN_GAP_S["status"] - 4)       # SSID dropped
    assert pacer.wait("N0TEST", "position") == 0.0                                            # other kind
    assert pacer.wait("N0CALL", "status") == 0.0                                              # other callsign
    clock.now += 100
    assert pacer.wait("N0TEST", "position") == 0.0


def test_timeline_first_and_render():
    tl = ds.Timeline()
    tl.add("a", 1, at=tl.t0 + 1.0)
    tl.add("b", 2, at=tl.t0 + 0.5)
    tl.add("a", 3, at=tl.t0 + 2.0)
    assert tl.first("a")[1] == 1 and tl.first("a", pred=lambda d: d == 3)[1] == 3 and tl.first("c") is None
    assert tl.render()[0].startswith("+0.500 b")


def test_snapshot_delta_moved():
    from dvtest import metrics
    samples = metrics.parse('dv_room_data_total{room="B",result="accepted"} 3\ndv_pkt_items_total{kind="status",result="accepted"} 2\n')
    snap = ds.snapshot(samples)
    assert snap["room.accepted"] == 3 and snap["pkt.status.accepted"] == 2 and snap["pkt.text.all"] == 0
    after = dict(snap, **{"room.accepted": 4})
    assert ds.moved(ds.delta(after, snap)) == {"room.accepted": 1}


def test_sms_packet_and_nonce():
    packet = ds.sms_packet("hi", "N0CALL")
    from dvtest.proto import m17
    parsed = m17.parse_m17p(packet)
    assert parsed["src"] == "N0CALL" and parsed["payload_crc_ok"] and parsed["payload"][:1] == b"\x05"
    assert ds.nonce_body("X").startswith("X ")


def test_sse_watch_collects_data_events():
    class Client:
        def sse(self, path, max_events, timeout, on_frame, stop):
            on_frame({"event": "hello", "data": "{}"})
            on_frame({"event": "data", "data": json.dumps({"item_type": "status", "callsign": "N0TEST"})})
            return 200, "text/event-stream", []

    with ds.SseWatch(Client(), "/x") as watch:
        assert watch.ready(2.0)
        got = watch.wait_data(lambda o: o.get("item_type") == "status", 2.0)
    assert got is not None and got[1]["callsign"] == "N0TEST"
    assert watch.wait_data(lambda o: False, 0.05) is None
