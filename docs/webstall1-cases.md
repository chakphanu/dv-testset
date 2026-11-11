# WEBSTALL1: a slow disk must not hold the other requests of dv-svc-web

Prepared offline for the fix image; NOT run. Cases: `cases/test_web_stall.py` (helpers `cases/webstall_support.py`, unit tests `cases/unit/test_webstall_support.py`).
Evidence tooling for load runs: `python3 -m dvtest.stallscan out/<run>` (`dvtest/stallscan.py`).

## The defect (dv-router analysis 252, run `out/20261111T123937Z`, wip-7bd0082)

dv-svc-web has ONE tokio worker. A login writes its audit record with a synchronous `fsync` on that worker, so a host I/O stall holds every request behind it: in the load run
all requests (logins, `/rtc/alive`, offers) waited 5.6 s (12:49:12.3 to 12:49:18.86). The fix moves the write off the worker. Acceptance: while a login is stalled on the disk,
`/rtc/alive`, `/api/v1/public/state` and `/health` answer within 500 ms.

## What the load case records now (every load run)

* `io_pressure` in `web/load.json`: `/proc/pressure/io` of the host once a second (cumulative `some` / `full` stall time, microseconds), for the whole run.
* For every request slower than 1 s (`slow_requests.items`): the host I/O stall in its window (`io_some_ms`, `io_full_ms`), the nearest login in
  `sut-state/web/audit.jsonl` (`nearest_login`: epoch, signed distance in seconds, result; read at the end of the run when the file is there) and the dv-svc-web cpu while it ran.
  FINDING WEBSTALL1 prints the longest with these. After a run the same table comes from `python3 -m dvtest.stallscan out/<run>`.
* Reading: a stall with `io_some_ms` of the order of its length and a login within a second of its start is the audit fsync; a stall without I/O pressure is something else.

## The case: `test_web_stall::test_a_slow_disk_does_not_hold_the_other_requests`

Mechanism, no sudo and no system change: a tester-side writer (`IoStaller`) writes a capped file (`DVT_WEBSTALL_MB`, default 2048 MiB; `DVT_WEBSTALL_S`, default 25 s; random
blocks, an fsync every 256 MiB) into `out/<run>/stall/` and deletes it. That directory is on the filesystem of the SUT state directory (the same bind-mounted `out/<run>`), so the
SUT's fsync shares its journal and writeback with the writer. Meanwhile four recorders run: a login every 1.2 s (the login budget is 1/s), `POST /rtc/alive` (a dead session id: any
answer below 500 counts, a 429 is an answer), `GET /api/v1/public/state` and `GET /health` every 0.5 to 0.7 s, each on its own connection. 6 s baseline, the writer, 5 s after.

Verdict (`webstall_support.verdict`) is taken over the logins that WERE slow (>= 1 s) and nothing else:
* some login slow and every probe that started inside a slow login answered below 500 within 500 ms: PASS;
* a probe inside a slow login late, a 5xx, or no answer: FAIL (on wip-7bd0082 this is the reproduction);
* no login slow (the filesystem absorbed the writer), fewer than 2 probes of a kind inside the stall windows, or the probes slow outside any stall (the host is not quiet): INCONCLUSIVE =
  skip, or a failure with `DVT_WEBSTALL1=on` (set it for the fix image). The `web/stall.json` of the run holds the records and the I/O pressure.

It needs `DVT_WEBSTALL1_IO=on` (the owner's go: the writer loads the host disk for about half a minute) and 20 GiB free after the file. Run it alone, on a quiet host, with
`--profile web`. The rig filesystem is btrfs on dm-crypt; the stall length depends on the host, which is why the case judges only a stall it has seen, and says so when it saw none.

## Why not the other mechanisms

| Mechanism | Verdict |
|---|---|
| `LD_PRELOAD` shim that delays `fsync` | Impossible: `dv-svc-web` of the image is a static-pie executable (no interpreter), and the image has no libc and no shell. |
| `strace -e inject=fsync:delay_enter`, gdb | Needs `ptrace` on a root process of another container: privileges. |
| `fsfreeze` | Freezes the whole filesystem (the rig, this session, the desktop): refused. |
| cgroup `io.max` by hand | Works, needs root: see the GAP below. |
| `device_write_bps` in the compose file | Applies for the life of the container, not for a few seconds, and the audit write is a few hundred bytes: a byte-rate limit does not stall it. |
| FUSE stall file system | Needs `user_allow_other` in `/etc/fuse.conf` (docker's root must read the mount), a FUSE binding (none installed), and the rig to mount it before the SUT starts. |
| `dmsetup` delay target | Root and a dedicated block device. |

## GAP: a deterministic stall (needs the owner's go)

The writer cannot give a stall of a chosen length. A deterministic one is a cgroup v2 write throttle on the SUT container for a few seconds:

```
CID=$(docker inspect -f '{{.Id}}' dvt-xlxd-sut-1)
CG=/sys/fs/cgroup/system.slice/docker-$CID.scope
MM=$(lsblk -dno MAJ:MIN "$(findmnt -no SOURCE /home)")      # the device under the rig filesystem (dm-crypt: the throttle must be on the device that backs it)
echo "$MM wbps=1" | sudo tee "$CG/io.max"                     # stall: dirty data of the container is written at 1 B/s, its fsync waits
sleep 5
echo "$MM wbps=max" | sudo tee "$CG/io.max"                   # release
```

To use it from the case without the tester holding any privilege: a new control-channel action `io-stall xlxd-sut on|off|<seconds>` in `scripts/lib/` (allow-listed like `kill-proc`
and `proc-stat`) that runs exactly the two `tee` lines above, always runs the release (a trap and a hard 30 s ceiling), and is limited to the SUT container. That needs a sudoers entry
for the two writes (or a root helper). Nothing of this is installed or run; the case would call `rig_control.io_stall(5)` around one login and keep the same verdict.
