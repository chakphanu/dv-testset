<!-- SPDX-License-Identifier: MIT -->
# GUARD1 cases: a strike-3 block survives the restart of a room controller (GUARDSYNC1, GUARDMERGE1)

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`; `c1b239d1` GUARDSYNC1 + GUARDMERGE1, `da900207` GUARDSYNC1b). **Nothing here has run on a rig.**
Case file `cases/test_guard1.py` (reuses the ladder helpers of `cases/test_short_stream_guard.py`), pure judge `dvtest/guard1.py`, unit tests `cases/unit/test_guard1_lib.py`,
switch `DVT_GUARD1` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`). Notes: stdout and `out/<run>/guard1.txt` (`RECORD`, `FINDING`).

**Gate.** `DVT_GUARD1=on`; without it every case skips with the reason.

## What is asserted

Document: `docs/design/floor-control.md` ("Guard penalties", cross-room state: each controller keeps the short-stream history of its own room, puts `guard/<callsign>`, a 44 B
GuardRec; DENY 3), `fabric.md` (GUARDSYNC1: a restarted process or one that lost its link asks the other controllers' `guard-snapshot/<zid>` at startup, after link loss, after lost
input and every 30 s; applied by 7 s on the pending fast path, otherwise 31 s; GUARDMERGE1: the merge keeps the highest strike count, the latest block and strike times, the OR of
`link_blocked`; a record is active until max(last strike + 300 s, block_until): a merge never lowers a penalty in force).

| Case | Statement | Assertion |
|------|-----------|-----------|
| GUARD1-a | a block earned before the restart is reconciled | callsign C earns strikes 1, 2, 3 in module U (three short overs each, waits of 3 s and 10 s: the B18 ladder, so strike 3 = 300 s); precondition: C is refused in module T before the kill (the guard is node-wide; a FINDING and a failure otherwise); `dv-room` of T is SIGKILLed (new `room_epoch`, empty guard cache); a control callsign is probed in T until it is heard (`t_up`, budget 90 s); C is probed every 0.5 s: no probe heard from `t_up` + 31 s + 4 s slack. A probe heard earlier is a FINDING (the window the bound allows), recorded with its second |
| GUARD1-b | strikes earned while a room is down arrive by the snapshot | `dv-room` of module R is SIGKILLed every second for 45 s; meanwhile C2 climbs the ladder in module S (R misses every put); then the same probes and the same bound in R. Strike 3 with its 300 s block must arrive as the highest record, never as nothing |
| GUARD1-c | the edge-side `link_blocked` survives an edge restart | callsign C earns strike 3 in module M; precondition: a new DExtra link of C gets NAK; `dv-edge-dextra` is SIGKILLed (`scripts/lib/killproc.sh`, `dvtest/control.py`: KILL only, since dv-router's answer of 2026-11-22; every DExtra session drops, run alone); after the respawn answers anything, new links of C are probed every 0.5 s: NAK at and after `t_up` + 31 s + 4 s slack, an ACK earlier is a FINDING |

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_GUARD1=on \
scripts/run.sh -m rig -- cases/test_guard1.py
```

No web profile, no `--capture`, no config extra (the template must serve modules M, R, S, T, U: `rooms = "A-Z"` does). Estimated time (not measured): about 8 minutes (GUARD1-c about 2 minutes: ladder 35 s, the respawn, the 45 s window; GUARD1-a about 3 minutes:
the ladder is about 35 s, the room restart and the 45 s probe window; GUARD1-b about 3 minutes). Strike 3 lasts 300 s per callsign: the callsigns `N0GDA` to `N0GDO` are used once per run,
and a second run within 5 minutes finds them blocked (the first assertion of each case says so). One rig, nothing else at the same time.

## GAPs

* **Own link loss without a restart** (the `guard records reconciled after link loss N: K records from M controllers` line, the 30 s periodic query, the 7 s fast path) needs the router session
  cut: not producible (the router runs in the launcher). `python3 -m dvtest.flk15 out/<run>` judges the line shape should one appear.
* **Records lost with the only holding process** ("remain unrecoverable"): killing the room that earned the strike while no other process holds the record is documented as a loss, so it is
  not asserted. Each controller and edge caches every put, so the earn-room kill is not a loss in practice, but it is not part of these cases either.
* Merge internals (the highest strike, the OR of `link_blocked`, expired older record replaced), the 1024-record bundle cap and the query backoff are not observable.

## Answers of dv-router (2026-11-22, `answers-342-dvt.md` Q7) built in

* The guard is node-wide per BASE callsign (every room's cache feeds the decision, `room.rs:1049-1056`); a callsign talks in one room at a time (DENY 6). GUARD1-a asserts the block in another module before the kill.
* A respawned room fails OPEN until its first snapshot (empty cache, asynchronous startup query; records arrive within 7 s on the pending fast path, 31 s at the latest). The cases tolerate that window as a FINDING and
  fail only after 31 s + 4 s slack, and they need the other room processes alive (the holders of the record). The same holds for the respawned edge (GUARD1-c).
* The six protocol edges are KILL-only in the control allow-list now (`dv-edge-dextra`, `-dplus`, `-dcs`, `-m17`, `-ysf`, `-dmrhb`); only DExtra is probed (GUARD1-c); the other protocols' link refusals (DPlus `BUSY`, DCS `NAK`,
  MMDVM `MSTNAK`, M17 NACK, YSF no poll reply) after their edge's restart are the same shape with `sg.new_links` and are not written. An alternative with less disruption is SIGUSR1 (every edge restarts quietly,
  `runtime.rs:1727`): not allow-listed for these edges.
