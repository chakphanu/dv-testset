<!-- SPDX-License-Identifier: MIT -->
# RESTART1 / RESTART1b cases: the node restart ordering

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`, units RESTART1 = `f34eb5ba`, RESTART1b = `348f984f`). **Nothing here has run on a rig.** Case file `cases/test_restart1.py`,
pure judgements `dvtest/restart1.py`, unit tests `cases/unit/test_restart1_lib.py`, switch `DVT_RESTART1` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`).
Notes: stdout and `out/<run>/restart1.txt` (`RECORD`).

**Gate.** `DVT_RESTART1=on`; without it every case skips with the reason. Needs `--profile web` (the admin API), an admin login with step-up, `DVT_SUT_LAUNCH=native`.

## What RESTART1 is (read from `docs/design/deployment.md`, "Launcher node restart")

`POST /api/v1/admin/system/restart-node` (admin, step-up, body `{"confirm": <reflector callsign>}`, 202; "the accepted response may be dropped because this process stops") makes the launcher: send the
reply; stop and join every child; (RESTART1b) record every outstanding child pid and require a separate reaped event for each before going on (survivors are killed after the 10 s bulk-stop bound, then
up to 1 s for collection; an error or timeout refuses the re-exec and exits 1); close its embedded fabric session and await it (the SDK's close timeout is 10 s); check that the Unix listener's lock
descriptor is closed; seal every descriptor from 3 up close-on-exec (`close_range`, fail closed); re-execute. The exec keeps the launcher pid and parenthood. At native start the new image collects
inherited zombies, and while running it collects unknown children of every launcher thread (needs `CONFIG_PROC_CHILDREN`). No bind retry, no fixed delay, no forced lock file removal takes part.
Shutdown budget: up to 23 s after the signal. A stop signal during the restart cancels it.

## What is asserted

| Case | Statement | Assertion |
|------|-----------|-----------|
| RESTART1-a | one restart is clean | 202 (or a dropped reply); `/ready` goes down and comes back within 120 s (a restart that never shows a not-ready poll is a failure); a fresh admin login works (the session is gone); `launcher.started_ms` advances and `node_ready` is true; the set of children is the same and EVERY child has a new pid; no process of the old incarnation is in `proc-stat xlxd-sut list`; the launcher `dv-router` keeps its pid; the count of children per name is the same; no `dv-*` process outside the launcher's children; NO process in state Z (the `zombie=` lines of the same `list`) |
| RESTART1-b | three restarts in a row | the same checks after each, each ready within the budget: a listener or lock of the old incarnation that was not released would fail the next start |
| RESTART1-c | the node serves | a probe over on module Q (D-STAR) is relayed within 90 s and a long over in full |

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_RESTART1=on \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml \
scripts/run.sh -m rig --profile web -- cases/test_restart1.py
```

Estimated time (not measured): about 6 minutes (four restarts of 20 to 60 s each, the admin logins with TOTP, the probe of RESTART1-c). The template must serve module Q. One rig, nothing else at the same
time (the restarts reset every hub counter and every session). No `--capture`.

## GAPs

* **No per-pid "reaped" counter or log line exists** (answer-342; not a product gap): a completed restart (`started_ms` advanced, same launcher pid) proves that every child was reaped, because the
  launcher needs a `ChildReaped` event per pid before the re-exec. The cases add a state sweep: `proc-stat xlxd-sut list` now prints `zombie=<pid> <ppid> <comm>` for every process in state Z
  (`scripts/lib/procstat.sh`, `dvtest.control.parse_zombie_list`, `RigControl.proc_snapshot`; the `proc=` lines and `parse_proc_list` are unchanged), and RESTART1-a / b fail on any. A zombie that is
  reaped between the exit and the sweep is not seen, which is the intended state.
* **Inherited children** (orphans collected after the exec) cannot be produced from outside, and in the rig they are not the launcher's to collect: `init: true` makes docker-init (tini) PID 1 and the
  launcher runs below it, so adopted orphans go to tini. The bare image (launcher = PID 1) is not what the rig runs.
* **The refusal paths** (router close error, release-check error, collection timeout: log `node restart refused: not re-executing`, exit 1) need a fault hook and are not reachable. The SUT log
  is read only after the run, so the host could scan for `node restart refused` (none expected); `python3 -m dvtest.logscan` style step not written.
* **A stop signal during the restart** (cancels it) needs a signal to the launcher; the control channel never signals `dv-router`.
* The cases assert nothing about PID 1: only that the launcher pid is unchanged across the restart, which holds with or without an init.
* The 10 s SDK close timeout and the 23 s shutdown budget are upper bounds; the cases only require the node ready within 120 s.

## Questions for the DV-Router team

None open (answer-342 Q3 and Q4: no reaped counter exists and a completed restart implies it; PID 1 is tini in the rig compose).
