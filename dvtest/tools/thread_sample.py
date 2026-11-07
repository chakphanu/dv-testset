# SPDX-License-Identifier: MIT
"""Host-side per-thread CPU sampler (THREADS1b evidence): reads /proc/<pid>/task/*/stat of one process of a rig
container at 10 Hz and appends one line per thread and sample to a file:

    <epoch ms> <tid> <comm> <utime ticks> <stime ticks> <state>

The process is found with `docker top <container> -eo pid,comm` (read only) and re-resolved every second, so a
restart is followed. Stops when the container is gone for 5 s or after --max-s seconds. Read only, no signals.

    python3 -m dvtest.tools.thread_sample --container dvt-xlxd-sut-1 --comm dv-edge-webrtc --out threads.txt
    python3 -m dvtest.tools.late_report --late out/<run>/web/load.json --threads threads.txt
"""

import argparse
import os
import subprocess
import time


def find_pid(container, comm):
    try:
        out = subprocess.run(["docker", "top", container, "-eo", "pid,comm"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() == comm and parts[0].isdigit():
            return int(parts[0])
    return None


def read_threads(pid):
    """[(tid, comm, utime, stime, state)] of the process, [] when it is gone."""
    rows = []
    try:
        tids = os.listdir("/proc/%d/task" % pid)
    except OSError:
        return rows
    for tid in tids:
        try:
            with open("/proc/%d/task/%s/stat" % (pid, tid)) as handle:
                text = handle.read()
        except OSError:
            continue
        left, right = text.index("("), text.rindex(")")
        fields = text[right + 2:].split()
        rows.append((int(tid), text[left + 1:right].replace(" ", "_"), int(fields[11]), int(fields[12]), fields[0]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--container", required=True)
    ap.add_argument("--comm", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--max-s", type=float, default=1800.0)
    args = ap.parse_args(argv)
    period = 1.0 / args.hz
    start = time.monotonic()
    pid = None
    last_find = 0.0
    missing_since = None
    with open(args.out, "a", buffering=1) as out:
        while time.monotonic() - start < args.max_s:
            tick = time.monotonic()
            if pid is None or tick - last_find >= 1.0:
                found = find_pid(args.container, args.comm)
                last_find = tick
                if found is None:
                    pid = None
                    missing_since = missing_since or tick
                    if tick - missing_since > 5.0 and tick - start > 5.0:
                        break
                    time.sleep(0.5)
                    continue
                pid = found
                missing_since = None
            now_ms = int(time.time() * 1000)
            rows = read_threads(pid)
            if not rows:
                pid = None
                continue
            for tid, comm, ut, st, state in rows:
                out.write("%d %d %s %d %d %s\n" % (now_ms, tid, comm, ut, st, state))
            time.sleep(max(0.0, period - (time.monotonic() - tick)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
