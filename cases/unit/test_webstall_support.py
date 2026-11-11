# SPDX-License-Identifier: MIT
"""cases/webstall_support.py and the skip paths of cases/test_web_stall.py (WEBSTALL1, prepared offline): the verdict over the records of a
stalled login, the recorder, and the capped writer."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import webstall_support as st  # noqa: E402

pytestmark = pytest.mark.unit


def login(start, seconds):
    return {"kind": "login", "start": start, "seconds": seconds, "status": 200, "error": None}


def probe(kind, start, seconds=0.01, status=200, error=None):
    return {"kind": kind, "start": start, "seconds": seconds, "status": status, "error": error}


def probes_in(t0, t1, seconds=0.01, status=200):
    out, t = [], t0
    while t < t1:
        out += [probe(k, t, seconds, status) for k in st.KINDS]
        t += 0.5
    return out


QUIET = probes_in(100.0, 104.0)                       # before the stall: all fast


def test_no_slow_login_is_inconclusive():
    state, lines = st.verdict([login(100.0, 0.05), login(101.2, 0.06)], QUIET + probes_in(104.0, 108.0), io_some_ms=12.0)
    assert state == "inconclusive" and "no login was slow" in lines[-1] and "12.0 ms" in lines[0]


def test_every_probe_inside_a_stalled_login_answering_in_time_passes():
    logins = [login(100.0, 0.05), login(105.0, 4.0)]
    state, lines = st.verdict(logins, QUIET + probes_in(105.0, 109.0, seconds=0.03), io_some_ms=3000.0)
    assert state == "pass" and "answered within 500 ms" in lines[-1]


def test_a_probe_held_behind_the_login_fails():
    """The state of the images before the fix: the login takes 4 s and everything waits behind it."""
    logins = [login(105.0, 4.0)]
    held = [probe(k, 105.5 + i, seconds=3.6 - i) for i, k in enumerate(st.KINDS)] + probes_in(106.5, 109.0, seconds=0.02)
    state, lines = st.verdict(logins, QUIET + held)
    assert state == "fail" and "not answered within 500 ms" in lines[-1] and "worst" in lines[-1]


def test_a_server_error_or_a_missing_answer_inside_the_stall_fails_a_429_does_not():
    logins = [login(105.0, 3.0)]
    ok429 = probes_in(105.0, 108.0, seconds=0.02, status=429)
    assert st.verdict(logins, QUIET + ok429)[0] == "pass"
    broken = ok429 + [probe("health", 106.0, seconds=0.02, status=503)]
    assert st.verdict(logins, QUIET + broken)[0] == "fail"
    gone = ok429 + [probe("alive", 106.1, seconds=30.0, status=None, error="TimeoutError: x")]
    assert st.verdict(logins, QUIET + gone)[0] == "fail"


def test_too_few_probes_in_the_stall_or_a_noisy_baseline_is_inconclusive():
    logins = [login(105.0, 1.2)]
    few = probes_in(105.0, 105.4)                                   # one probe of each kind
    state, lines = st.verdict(logins, QUIET + few)
    assert state == "inconclusive" and "fewer than 2 probes" in lines[-1]
    noisy = [probe(k, 100.0 + i * 0.5, seconds=0.9) for i in range(8) for k in st.KINDS]
    state, lines = st.verdict(logins, noisy + probes_in(105.0, 107.0))
    assert state == "inconclusive" and "not quiet enough" in lines[-1]


def test_stall_windows_and_inside():
    windows = st.stall_windows([login(10.0, 0.4), login(20.0, 1.0), login(5.0, 2.5)])
    assert windows == [(5.0, 7.5), (20.0, 21.0)]
    assert st.inside(probe("health", 6.0), windows) and st.inside(probe("health", 21.0), windows) and not st.inside(probe("health", 8.0), windows)


def test_the_recorder_times_the_call_and_not_the_prepare_or_the_sleep():
    calls = []

    def fn():
        calls.append(1)
        time.sleep(0.05)
        if len(calls) == 3:
            raise OSError("boom")
        return 204

    rec = st.Recorder("alive", fn, 0.02, prepare=lambda: time.sleep(0.1))
    rec.start()
    time.sleep(0.8)
    rows = rec.stop()
    assert len(rows) >= 3 and rows[0]["status"] == 204 and 0.04 <= rows[0]["seconds"] < 0.09
    assert rows[2]["status"] is None and rows[2]["error"].startswith("OSError") and not rec.is_alive()
    assert st.Recorder("x", fn, 1.0).stop() == []                   # stop() of a recorder that never ran


def test_the_staller_writes_a_capped_file_and_removes_it(tmp_path):
    staller = st.IoStaller(str(tmp_path / "s"), megabytes=16, seconds=5.0, fsync_every_mb=8)
    staller.start()
    staller.join(30.0)
    assert staller.written_mb == 16 and staller.error is None and not os.path.exists(staller.path)
    assert staller.ended_at >= staller.started_at


def test_the_staller_stops_on_request_and_never_started_stop_is_harmless(tmp_path):
    staller = st.IoStaller(str(tmp_path / "s"), megabytes=100000, seconds=60.0)
    staller.stop()                                                  # never started
    staller = st.IoStaller(str(tmp_path / "t"), megabytes=100000, seconds=60.0)
    staller.start()
    time.sleep(0.3)
    staller.stop()
    assert not staller.is_alive() and staller.written_mb < 100000 and not os.path.exists(staller.path)


def test_enough_space_keeps_a_margin(tmp_path):
    assert st.enough_space(str(tmp_path), 1, margin_gb=0)
    assert not st.enough_space(str(tmp_path), 1, margin_gb=10 ** 9)
    assert not st.enough_space(str(tmp_path / "missing"), 1)


def test_the_case_is_skipped_without_the_owners_go(monkeypatch):
    import types
    sys.modules.setdefault("playwright", types.ModuleType("playwright"))
    import test_web_stall as case
    monkeypatch.delenv("DVT_WEBSTALL1_IO", raising=False)
    with pytest.raises(pytest.skip.Exception) as info:
        case.test_a_slow_disk_does_not_hold_the_other_requests(None, "https://x", None)
    assert "DVT_WEBSTALL1_IO=on" in str(info.value)


def test_inconclusive_is_a_skip_unless_the_fix_image_is_under_test(monkeypatch):
    import test_web_stall as case
    monkeypatch.delenv("DVT_WEBSTALL1", raising=False)
    with pytest.raises(pytest.skip.Exception):
        case.inconclusive("no stall")
    monkeypatch.setenv("DVT_WEBSTALL1", "on")
    with pytest.raises(pytest.fail.Exception):
        case.inconclusive("no stall")
