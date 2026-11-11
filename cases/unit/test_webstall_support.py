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


# ---------------------------------------------------------------- the writer must not start where it could push a btrfs towards ENOSPC

def fake_host(tmp_path, fstype="btrfs", source="/dev/mapper/root", unalloc_mib=1, meta_free_gib=13, extra_fs=False):
    """A fake /proc/self/mountinfo and /sys: one btrfs on dm-0 (4000 GB device) with `unalloc_mib` MiB unallocated."""
    sysfs = tmp_path / "sys"
    dev_bytes = 4_000_000_000_000
    chunks = {"data": 3_900_000_000_000, "metadata": 80_000_000_000}
    chunks["system"] = dev_bytes - chunks["data"] - chunks["metadata"] - unalloc_mib * 1048576
    base = sysfs / "fs" / "btrfs" / "8447b98d-1b22-4ebc-930a-0224b9e3f435"
    (base / "devices" / "dm-0").mkdir(parents=True)
    for kind, size in chunks.items():
        (base / "allocation" / kind).mkdir(parents=True)
        (base / "allocation" / kind / "disk_total").write_text("%d\n" % size)
    (base / "allocation" / "metadata" / "total_bytes").write_text("%d\n" % 40_000_000_000)
    (base / "allocation" / "metadata" / "bytes_used").write_text("%d\n" % (40_000_000_000 - meta_free_gib * 1024 ** 3 - 536870912))
    (base / "allocation" / "global_rsv_size").write_text("536870912\n")
    (sysfs / "block" / "dm-0" / "dm").mkdir(parents=True)
    (sysfs / "block" / "dm-0" / "dm" / "name").write_text("root\n")
    (sysfs / "block" / "dm-0" / "size").write_text("%d\n" % (dev_bytes // 512))
    if extra_fs:
        other = sysfs / "fs" / "btrfs" / "11111111-2222-3333-4444-555555555555"
        (other / "devices" / "sdb1").mkdir(parents=True)
        (other / "allocation").mkdir(parents=True)
    work = tmp_path / "work" / "run"
    work.mkdir(parents=True)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/sda1 rw\n40 1 0:50 /@home %s rw,relatime - %s %s rw,subvol=/@home\n" % (tmp_path / "work", fstype, source))
    return str(work), str(mountinfo), str(sysfs)


def test_a_fully_allocated_btrfs_is_refused(tmp_path):
    work, mountinfo, sysfs = fake_host(tmp_path, unalloc_mib=1)
    room = st.btrfs_headroom(work, mountinfo, sysfs)
    assert room["unallocated"] == 1048576 and room["metadata_free"] == 13 * 1024 ** 3
    problem = st.room_problem(work, 1, margin_gb=0, mountinfo_path=mountinfo, sysfs=sysfs)
    assert problem and "1.0 MiB unallocated" in problem and "ENOSPC" in problem and "13.0 GiB of metadata room" in problem


def test_a_btrfs_with_room_to_grow_is_accepted_and_the_owner_can_override(tmp_path):
    work, mountinfo, sysfs = fake_host(tmp_path, unalloc_mib=4096)
    assert st.room_problem(work, 1, margin_gb=0, mountinfo_path=mountinfo, sysfs=sysfs) is None
    work, mountinfo, sysfs = fake_host(tmp_path / "b", unalloc_mib=1)
    assert st.room_problem(work, 1, margin_gb=0, mountinfo_path=mountinfo, sysfs=sysfs, ignore_btrfs=True) is None


def test_another_file_system_needs_only_the_statfs_margin(tmp_path):
    work, mountinfo, sysfs = fake_host(tmp_path, fstype="ext4", source="/dev/sda2")
    assert st.btrfs_headroom(work, mountinfo, sysfs) == "not_btrfs"
    assert st.room_problem(work, 1, margin_gb=0, mountinfo_path=mountinfo, sysfs=sysfs) is None
    assert "would stay free" in st.room_problem(work, 1, margin_gb=10 ** 9, mountinfo_path=mountinfo, sysfs=sysfs)


def test_unreadable_or_ambiguous_btrfs_numbers_mean_do_not_start(tmp_path):
    work, mountinfo, sysfs = fake_host(tmp_path)
    for name in ("global_rsv_size",):
        (tmp_path / "sys" / "fs" / "btrfs" / "8447b98d-1b22-4ebc-930a-0224b9e3f435" / "allocation" / name).unlink()
    assert st.btrfs_headroom(work, mountinfo, sysfs) is None
    assert "cannot be read" in st.room_problem(work, 1, margin_gb=0, mountinfo_path=mountinfo, sysfs=sysfs)
    work, mountinfo, sysfs = fake_host(tmp_path / "c", source="/dev/mapper/other", extra_fs=True)               # two btrfs, the source matches neither
    assert st.btrfs_headroom(work, mountinfo, sysfs) is None
    assert st.btrfs_headroom(work, str(tmp_path / "no-mountinfo"), sysfs) is None
    assert "cannot be read" in st.room_problem(work, 1, margin_gb=0, mountinfo_path=str(tmp_path / "no-mountinfo"), sysfs=sysfs)


def test_the_source_picks_its_file_system_among_several(tmp_path):
    work, mountinfo, sysfs = fake_host(tmp_path, extra_fs=True)
    room = st.btrfs_headroom(work, mountinfo, sysfs)
    assert room["uuid"].startswith("8447b98d")                                                                  # "root" is the dm name of dm-0


def test_the_mount_with_the_longest_matching_point_wins(tmp_path):
    text = "1 0 0:1 / / rw - ext4 /dev/sda1 rw\n2 1 0:2 / /srv rw - btrfs /dev/b rw\n3 1 0:3 / /srv2 rw - xfs /dev/c rw\n"
    assert st._mount_of("/srv/a/b", text) == ("/srv", "btrfs", "/dev/b")
    assert st._mount_of("/srv2", text)[1] == "xfs" and st._mount_of("/other", text)[1] == "ext4"
