# SPDX-License-Identifier: MIT
"""Pure parts of the WebKit talk repro (dvtest/talk_cycles.py)."""
import pytest

from dvtest import talk_cycles as tc

pytestmark = pytest.mark.unit


def test_plan_alternates_the_holds():
    assert tc.plan(5) == [2.0, 3.0, 2.0, 3.0, 2.0]
    assert len(tc.plan()) == 30


def test_early_end_ignores_the_release_and_finds_a_revoke():
    ok = [(0.1, "On air"), (0.6, "On air"), (1.1, "On air"), (1.6, "On air"), (1.9, "Ended: Button released.")]
    assert tc.early_end(ok, 2.0) is None                      # the status after the planned end minus the margin is the release
    bad = [(0.1, "On air"), (0.6, "On air"), (1.1, "On air"), (1.6, "Ended: Transmit permission was revoked.")]
    assert tc.early_end(bad, 3.0) == (1.6, "Ended: Transmit permission was revoked.")
    assert tc.early_end([], 2.0) is None


def test_keep_pairs_failed_or_every_tenth():
    assert [i for i in range(25) if tc.keep_pairs(i, False)] == [0, 10, 20]
    assert tc.keep_pairs(7, True)


def test_unrouted_delta_and_absent_families():
    before = [{"name": "dv_edge_webrtc_unrouted_total", "labels": {"proc": "webrtc", "reason": "no_source"}, "value": 20.0},
              {"name": "dv_edge_webrtc_unrouted_total", "labels": {"proc": "webrtc", "reason": "wrong_peer"}, "value": 11.0},
              {"name": "dv_edge_webrtc_audio_dropped_total", "labels": {"proc": "webrtc"}, "value": 250.0}]
    after = [{"name": "dv_edge_webrtc_unrouted_total", "labels": {"proc": "webrtc", "reason": "no_source"}, "value": 26.0},
             {"name": "dv_edge_webrtc_unrouted_total", "labels": {"proc": "webrtc", "reason": "wrong_peer"}, "value": 14.0},
             {"name": "dv_edge_webrtc_audio_dropped_total", "labels": {"proc": "webrtc"}, "value": 333.0}]
    assert tc.unrouted_delta(before, after) == {"no_source": 6.0, "wrong_peer": 3.0, "audio_dropped": 83.0}
    assert tc.unrouted_delta([], []) == {"audio_dropped": 0}
    assert tc.unrouted_delta(None, None) == {"audio_dropped": 0}


def test_ptt_frames_window():
    digest = [{"t_ms": -100, "op": "ptt", "state": "granted", "code": "ok"}, {"t_ms": 900, "op": "ptt", "state": "renewed", "code": "ok"},
              {"t_ms": 950, "op": "selected"}, {"t_ms": 5000, "op": "ptt", "state": "ended", "code": "released"}]
    assert tc.ptt_frames(digest, -200, 1000) == [(-100, "granted/ok"), (900, "renewed/ok")]


def test_summarize():
    rows = [{"index": 0}, {"index": 1, "early": [1.6, "x"]}, {"index": 2, "no_on_air": True}]
    assert tc.summarize(rows) == {"cycles": 3, "early": 1, "no_on_air": [2], "failed": [1, 2]}
