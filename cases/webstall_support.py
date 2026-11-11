# SPDX-License-Identifier: MIT
"""Helpers of the WEBSTALL1 case (cases/test_web_stall.py, docs/webstall1-cases.md); not a test module.

dv-router analysis 252: dv-svc-web has ONE tokio worker and a login writes its audit record with a synchronous fsync on that worker, so a host
I/O stall holds every request (run 20261111T123937Z: all requests held for 5.6 s). The fix moves the write off the worker. The case needs a slow
disk under the SUT state directory without any privilege: a buffered writer on the same filesystem (IoStaller) makes fsync slow, and the verdict
is taken only over the logins that WERE slow (stall_windows); a run in which no login was slow proves nothing and is inconclusive.

  IoStaller        a thread that writes a capped file under the run directory, with an fsync every few hundred MiB, and removes it
  Recorder         threads that repeat one request (login, /rtc/alive, /api/v1/public/state, /health) and keep (kind, start, seconds, status)
  stall_windows    the [start, end] spans of the logins that took at least STALL_LOGIN_S
  verdict          pass / fail / inconclusive from the records, with the lines to print
Stdlib only.
"""
import os
import shutil
import threading
import time

ANSWER_LIMIT_S = 0.5            # during a stalled login rtc alive, public state and /health must answer within this
STALL_LOGIN_S = 1.0             # a login at least this slow counts as a stall that was seen
MIN_PROBES_PER_KIND = 2         # probes of each kind inside the stall windows needed for a verdict
BASELINE_LIMIT_S = 0.5          # probes outside any stall are slower than this: the host is not quiet enough to judge
KINDS = ("alive", "public", "health")


class IoStaller(threading.Thread):
    """Write up to `megabytes` MiB (buffered, 4 MiB blocks of random bytes so that no compression shrinks them) to <directory>/stall.bin for at most
    `seconds` s, fsync every `fsync_every_mb`, then delete the file. The writer shares the filesystem and the journal with the SUT state directory,
    which is what slows the SUT's fsync; it needs no privilege and changes no system setting."""

    def __init__(self, directory, megabytes=2048, seconds=25.0, fsync_every_mb=256):
        super().__init__(daemon=True, name="io-staller")
        self.directory, self.megabytes, self.seconds, self.fsync_every_mb = directory, megabytes, seconds, fsync_every_mb
        self.path = os.path.join(directory, "stall.bin")
        self.written_mb = 0
        self.started_at = self.ended_at = None
        self.error = None
        self._stop_flag = threading.Event()

    def run(self):
        block = os.urandom(4 * 1024 * 1024)
        self.started_at = time.time()
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(self.path, "wb") as fh:
                deadline = time.monotonic() + self.seconds
                since_sync = 0
                while not self._stop_flag.is_set() and time.monotonic() < deadline and self.written_mb < self.megabytes:
                    fh.write(block)
                    self.written_mb += 4
                    since_sync += 4
                    if since_sync >= self.fsync_every_mb:
                        fh.flush()
                        os.fsync(fh.fileno())
                        since_sync = 0
        except OSError as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.ended_at = time.time()

    def stop(self):
        self._stop_flag.set()
        if self.started_at is not None:
            self.join(60.0)


BTRFS_MIN_UNALLOCATED = 2 * 1024 ** 3      # a btrfs whose device has less than this unallocated cannot add a metadata chunk: the writer could push it to ENOSPC


def _mount_of(directory, mountinfo):
    """(mount point, fs type, source) of the mount that holds `directory`, from the text of /proc/self/mountinfo; None when none matches."""
    path, best = os.path.realpath(directory), None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5:
            continue
        point = fields[4].replace("\\040", " ")
        if (path == point or path.startswith(point.rstrip("/") + "/")) and (best is None or len(point) >= len(best[0])):
            rest = right.split()
            best = (point, rest[0] if rest else "", rest[1] if len(rest) > 1 else "")
    return best


def _read_int(path):
    try:
        with open(path, encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def btrfs_headroom(directory, mountinfo_path="/proc/self/mountinfo", sysfs="/sys"):
    """For a directory on btrfs: {"unallocated", "metadata_free", "uuid"} in bytes from sysfs (the device size minus what the data, metadata and
    system chunks hold; the metadata room left after the global reserve). "not_btrfs" when the directory is on another file system; None when it is
    btrfs and the numbers cannot be read (then nothing is known and the writer must not start)."""
    try:
        with open(mountinfo_path, encoding="utf-8") as fh:
            mount = _mount_of(directory, fh.read())
    except OSError:
        return None
    if mount is None:
        return None
    if mount[1] != "btrfs":
        return "not_btrfs"
    base = os.path.join(sysfs, "fs", "btrfs")
    try:
        uuids = [n for n in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, n, "allocation"))]
    except OSError:
        return None
    wanted = os.path.basename(os.path.realpath(mount[2])) if mount[2].startswith("/") else mount[2]
    chosen = []
    for uuid in uuids:
        try:
            devs = os.listdir(os.path.join(base, uuid, "devices"))
        except OSError:
            continue
        names = set(devs)
        for dev in devs:                                               # a device-mapper node is known by its dm name too ("root" for dm-0)
            try:
                with open(os.path.join(sysfs, "block", dev, "dm", "name"), encoding="ascii") as fh:
                    names.add(fh.read().strip())
            except OSError:
                pass
        if wanted in names:
            chosen = [(uuid, devs)]
            break
        chosen.append((uuid, devs))
    if len(chosen) != 1:
        return None                                                    # no match, or several file systems and none is the source: unknown
    uuid, devs = chosen[0]
    alloc = os.path.join(base, uuid, "allocation")
    held = [_read_int(os.path.join(alloc, kind, "disk_total")) for kind in ("data", "metadata", "system")]
    sizes = [_read_int(os.path.join(sysfs, "block", dev, "size")) for dev in devs]
    rsv = _read_int(os.path.join(alloc, "global_rsv_size"))
    total, used = _read_int(os.path.join(alloc, "metadata", "total_bytes")), _read_int(os.path.join(alloc, "metadata", "bytes_used"))
    if None in held or None in sizes or not sizes or rsv is None or total is None or used is None:
        return None
    return {"uuid": uuid, "unallocated": sum(sizes) * 512 - sum(held), "metadata_free": total - used - rsv}


def room_problem(directory, megabytes, margin_gb=20, mountinfo_path="/proc/self/mountinfo", sysfs="/sys", ignore_btrfs=False):
    """Why the disk writer must NOT start in `directory` (a sentence for the skip), None when there is room: statfs must leave `margin_gb` GiB
    free after the file, and a btrfs device must still have BTRFS_MIN_UNALLOCATED bytes unallocated (a fully allocated btrfs has data room inside
    its chunks but cannot grow its metadata: the writer's metadata could push it towards ENOSPC for every process on the host).
    ignore_btrfs (DVT_WEBSTALL_IGNORE_BTRFS=on, the owner's decision) skips the second test."""
    if not enough_space(directory, megabytes, margin_gb):
        return "less than %d GiB would stay free on the filesystem of %s after a %d MiB writer" % (margin_gb, directory, megabytes)
    if ignore_btrfs:
        return None
    room = btrfs_headroom(directory, mountinfo_path, sysfs)
    if room == "not_btrfs":
        return None
    if room is None:
        return "%s is on btrfs (or its file system cannot be told) and its unallocated space cannot be read from sysfs: not starting a writer" % directory
    if room["unallocated"] < BTRFS_MIN_UNALLOCATED:
        return ("btrfs under %s has %.1f MiB unallocated (%.1f GiB of metadata room after the global reserve): it cannot add a chunk, and the writer could push "
                "its metadata towards ENOSPC; needs %d GiB unallocated (DVT_WEBSTALL_IGNORE_BTRFS=on only on the owner's word)" % (
                    directory, room["unallocated"] / 1048576.0, room["metadata_free"] / 1024 ** 3, BTRFS_MIN_UNALLOCATED // 1024 ** 3))
    return None


def enough_space(directory, megabytes, margin_gb=20):
    """True when the filesystem of `directory` keeps at least `margin_gb` GiB free after the writer's file."""
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return False
    return free - megabytes * 1024 * 1024 >= margin_gb * 1024 ** 3


class Recorder(threading.Thread):
    """Call `fn()` every `period_s` until stop(); fn returns the HTTP status (or raises). Records are dicts {kind, start (epoch), seconds, status,
    error}; the call is timed, the sleep between calls is not."""

    def __init__(self, kind, fn, period_s, prepare=None):
        super().__init__(daemon=True, name="rec-" + kind)
        self.kind, self.fn, self.period_s, self.prepare = kind, fn, period_s, prepare       # prepare() runs before the clock starts (a client side pacing wait)
        self.records = []
        self._stop_flag = threading.Event()

    def run(self):
        while not self._stop_flag.is_set():
            if self.prepare is not None:
                self.prepare()
            start, t0 = time.time(), time.monotonic()
            status = error = None
            try:
                status = self.fn()
            except Exception as exc:                                  # noqa: BLE001 - a failed call is a record, not the end of the recorder
                error = "%s: %s" % (type(exc).__name__, str(exc)[:100])
            self.records.append({"kind": self.kind, "start": start, "seconds": time.monotonic() - t0, "status": status, "error": error})
            self._stop_flag.wait(self.period_s)

    def stop(self):
        self._stop_flag.set()
        if self.is_alive():
            self.join(40.0)
        return list(self.records)


def stall_windows(logins, min_s=STALL_LOGIN_S):
    """[(start, end)] of the logins that took at least min_s, as epoch seconds, in start order."""
    return sorted((r["start"], r["start"] + r["seconds"]) for r in logins if r.get("seconds", 0.0) >= min_s)


def inside(record, windows):
    return any(a <= record["start"] <= b for a, b in windows)


def answered(record, limit_s=ANSWER_LIMIT_S):
    """A probe that got an HTTP answer below 500 within the limit (a 429 is an answer: the point is that the worker was not held)."""
    return record.get("error") is None and isinstance(record.get("status"), int) and record["status"] < 500 and record["seconds"] <= limit_s


def verdict(logins, probes, io_some_ms=None, limit_s=ANSWER_LIMIT_S, min_s=STALL_LOGIN_S, min_probes=MIN_PROBES_PER_KIND):
    """(state, lines): "fail" when a probe started inside a slow login did not answer in time, "pass" when every one did, "inconclusive" when
    no login was slow (the disk was not slow enough), too few probes fell into the stall windows, or the probes were slow outside a stall."""
    windows = stall_windows(logins, min_s)
    lines = ["logins %d, slow (>= %.1f s) %d, longest %.2f s; host io stall during the run: %s ms" % (
        len(logins), min_s, len(windows), max((r["seconds"] for r in logins), default=0.0), "?" if io_some_ms is None else io_some_ms)]
    outside = [p for p in probes if not inside(p, windows)]
    slow_outside = [p for p in outside if not answered(p, BASELINE_LIMIT_S)]
    if not windows:
        return "inconclusive", lines + ["no login was slow: the writer did not stall the disk, so the worker was never held and nothing can be judged"]
    inside_probes = [p for p in probes if inside(p, windows)]
    counts = {k: sum(1 for p in inside_probes if p["kind"] == k) for k in KINDS}
    lines.append("probes inside the stall windows: %s" % counts)
    if any(counts[k] < min_probes for k in KINDS):
        return "inconclusive", lines + ["fewer than %d probes of a kind fell inside the %d stall windows" % (min_probes, len(windows))]
    bad = [p for p in inside_probes if not answered(p, limit_s)]
    if bad:
        worst = max(bad, key=lambda p: p["seconds"])
        lines.append("%d of %d probes inside a stalled login were not answered within %.0f ms; worst %s %.2f s (status %s, %s)" % (
            len(bad), len(inside_probes), limit_s * 1000, worst["kind"], worst["seconds"], worst["status"], worst["error"]))
        return "fail", lines
    if len(slow_outside) > max(2, len(outside) // 10):
        return "inconclusive", lines + ["%d of %d probes outside a stall were slower than %.0f ms: the host is not quiet enough" % (
            len(slow_outside), len(outside), BASELINE_LIMIT_S * 1000)]
    lines.append("all %d probes inside a stalled login answered within %.0f ms (slowest %.0f ms)" % (
        len(inside_probes), limit_s * 1000, 1000 * max(p["seconds"] for p in inside_probes)))
    return "pass", lines
