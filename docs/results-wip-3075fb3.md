<!-- SPDX-License-Identifier: MIT -->
# LEAK2 acceptance of dv-router:wip-3075fb3

Image `dv-router:wip-3075fb3` (local build). Acceptance of the LEAK2 fix (room controller: no liveliness token per over; the open
announcement `streamev/<sid>/<R>/<epoch>` is re-put every 1 s while a stream is open; observers expire an entry 5 s after the last
refresh). Native launch, `DVT_SUT_GEN=c2465de`, `DVT_USRP_DYNAMIC=1`, no `--capture`, one rig run at a time, run from a separate worktree.
The case file is `cases/test_stream_registry.py` (`docs/stream-registry-cases.md`); the judge functions are `dvtest/registry.py`.

## Summary

| # | Run | Out dir | Result | Verdict |
|---|-----|---------|--------|---------|
| 1 | SOAK1 standard (600 s warmup + 3000 s measured; `web.toml:totp-required.toml:soak-edges.toml:admin.toml`, `--profile web,webload`) | `20261105T024735Z` | 1 failed, 69 min 21 s | only `rss:dv-svc-status` fails (117.6 KiB/min, 5882 of 5120 KiB); every other row ok |
| 2 | RSS isolation mode D (protocol churn) | `20261105T105819Z` | 1 passed, 16 min 41 s | data: `dv-router` 8.2 KiB/min |
| 3 | RSS isolation mode F (voice x3) | `20261105T111511Z` | 1 passed, 17 min 16 s | data: `dv-router` 5.6 KiB/min |
| 4 | Stream registry L1, L2, L4 (loop detection) and L3 (restart bound) | see `docs/stream-registry-cases.md`, "First runs on wip-3075fb3" | pass | L3 asserts the 5 s bound only (see below) |

## SOAK1 numbers

| Child | before the fix (be207d3) | wip-3075fb3 |
|-------|--------------------------|-------------|
| `dv-router` RSS slope | 137 to 170 KiB/min | 3.69 KiB/min |
| every `dv-room.*` | about 18 KiB/min | -0.28 to +0.3 KiB/min (flat) |
| `dv-svc-status` | about 110 to 125 KiB/min | 117.6 KiB/min |

`dv-svc-status` is the fill of the hub event ring (10000 events of about 1.16 KiB): 7487 events in the measured part (149.7 per minute),
9206 counted by the end, so the ring was not full and a plateau could not be shown in a 50 minute measured part. The plateau is shown by the
95 minute run on the next image (`docs/results-wip-239382b.md`). Threads and fds are flat for every child, the counters are flat, 0 generator
sessions failed.

## Isolation (data, not verdicts)

| Mode | `dv-router` | `dv-svc-status` | events per minute (measured) |
|------|-------------|-----------------|------------------------------|
| D (protocol churn) | 8.2 KiB/min | 531 KiB/min | 716 |
| F (voice x3) | 5.6 KiB/min | 263 KiB/min | 321 |

The `dv-svc-status` slope follows the event rate (estimate / slope 1.4 to 1.6): it is the ring filling, not a leak, in both modes. In D the
ring was full by the end (10690 events counted); the slope after that is not the ring filling.

## Loop detection and restart (L1 to L4)

L1, L2 and L4 pass. L3 (controller killed with a continuing over) is asserted only as the 5 s bound: after the restart (about 500 ms) a stale
IngressEnd or terminator reaching the new controller closes the old entry, and a continuing over is re-admitted, so the entry of the first
stream (P1) is recorded in `stream-registry-l3.txt` and the second (P2) must be admitted at kill + 6.5 s. A pure 5 s expiry cannot be isolated
black-box (the room unit tests cover it). Agreed with dv-router.

## Notes

- The run ended with `run.sh` exit code 127 ("etem_snapshot: command not found"): a helper edited `scripts/run.sh` while the run was active
  and bash reads a running script incrementally. The pytest result is intact. Rule: never edit `scripts/run.sh` during a run.
- Building `dvt-webload` in a fresh worktree failed twice with the buildkit error "failed to prepare extraction snapshot ... parent snapshot
  does not exist"; the fix is to rebuild only that own image with `--no-cache` through `scripts/contained.sh` (never a prune).
