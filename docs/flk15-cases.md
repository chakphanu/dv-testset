<!-- SPDX-License-Identifier: MIT -->
# FLK15 cases: an outage of the position policy hides, never leaks, and resyncs without a restart

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`, unit FLK15 = `b8d9db72`, with the shared resync core of GUARDSYNC1 `c1b239d1`).
**Nothing here has run on a rig.** Case file `cases/test_flk15_policy.py`, pure helpers and the host log scan `dvtest/flk15.py`, unit tests
`cases/unit/test_flk15_lib.py`, switch `DVT_FLK15` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`; `DVT_FLK15_BOUND_S`
is the optional wait after a flag change). Notes: stdout and `out/<run>/flk15.txt` (`RECORD`).

**Gate.** `DVT_FLK15=on`; without it every case skips with the reason (an image before FLK15 has no resync after an outage, so the assertions would fail by design).

## What is asserted

Document: `docs/design/data.md` ("The web process owns persisted flags and publishes the complete private hidden-base-callsign set ...", `bc9af2b1`), `fabric.md`
("`Up` reports an accepted router transport, not application readiness", the link lines), `config.md` (the fabric client). The policy is the set of hidden base callsigns
(account flag `hide_position`); dv-svc-web owns it, the room controllers, the data workers and the basemap worker consume it.

| Case | Statement | Assertion |
|------|-----------|-----------|
| FLK15-1 | web (the authority) is unavailable: nothing leaks, positions come back | `dv-svc-web` is SIGKILLed every 0.3 s for 15 s (the launcher backs the respawn off 0.5 s doubling to 5 s: one kill is no outage). A sampler reads the anonymous public state every second over the WHOLE window: a hidden account's row (H1 had a retained public row before it was hidden, H2 and L1 never) is never in it, before, during and after (the web process filters retained rows with the hidden set it loads at start). A position of H2 (hidden) and of K1 (control) is sent DURING the outage: afterwards K1 is public (check_public_row), H1, H2, L1 are absent from the public state and from the operator feed. `dv-data-worker` and every `dv-room` keep pid and start time (`proc-stat`): the resync did not need a consumer restart |
| FLK15-1 (second half) | a change after web is back reaches the consumers without a restart, within 1 + 5 + 1 s | with web back, L1 is unhidden and L2 hidden by two PATCHes; after `resync_wait_s()` (7 s + 5 s slack, `DVT_FLK15_BOUND_S`) L1's next position is public (SSE, state, operator feed) and L2's next position is on none of the three. The positions are offered after the bound, so a stale set at a consumer shows as a leak (L2) or a stuck hide (L1) |
| FLK15-2 | a consumer restart fails closed, then the snapshot decides | `dv-data-worker`, then `dv-room` of module B (`rooms=B`), SIGKILLed every 0.3 s for 8 s (each incarnation is a cold start = unknown = `denied/13`). Hidden accounts send meanwhile; one more account is hidden WHILE the consumer is down. The sampler never sees a hidden row; none is public or in the operator feed afterwards; the consumer has a new pid (the kill landed) and dv-svc-web does not; after the kills stop a control position is public |

The log lines are judged on the host (the SUT logs are collected after pytest ends): `python3 -m dvtest.flk15 out/<run>` (exit 1 on a line of the family that breaks its shape):
`dv-svc-web: position policy published (<start|durable change|own link up>): revision R, N hidden accounts` (the reason set is fixed, no callsign on any line),
`<program>: position policy withheld after link loss L: ...` followed by `<program>: position policy reconciled after link loss: revision R, N hidden` of the same program,
`<program>: fabric: router link down (loss N)` / `up again after loss N` (a down without an up is a FINDING), and the GUARDSYNC1 line
`<program>: guard records reconciled after link loss N: K records from M controllers`. After FLK15-1 the log must hold one `published (start)` line per respawn of
dv-svc-web (the kill loop makes several).

## Run command

Common prefix as the web rows of `.agents/skills/dv-testset/SKILL.md`; `R` = `rig/config/dv-router`.

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_REQUIRE_IDENT=true DVT_FLK15=on \
DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_flk15_policy.py
python3 -m dvtest.flk15 out/<run>
```

Estimated time (not measured): about 12 minutes. FLK15-1 is about 5 minutes (five registrations behind the register budget, the 15 s outage, two positions per account 30 s apart,
the 12 s resync wait and the 6 s absence windows), each FLK15-2 about 3 minutes. Run it in its own rig run: the cases kill `dv-svc-web` and the consumers, and the operator
session of the session-scoped fixture is logged in again at the end of the module. No aprsc, no RF, no browser.

## GAPs (not covered, and why)

* **A consumer's OWN fabric link loss without a process restart** (the `position policy withheld after link loss` branch, the retention of the withholding while a put arrives, the 5 s
  refresh after an authority-only link loss, the `fabric: router link down (loss N)` lines). The router runs inside the launcher (`deployment.md`: "Fabric"); a router outage is a
  launcher restart, which restarts every consumer too, so it only reproduces the cold-start branch that FLK15-2 covers. `kill-proc` signals processes by name and never the launcher;
  `netem` impairs the tester's or the SUT container's egress, not the loopback fabric. The host log scan judges the lines should they ever appear (a run with the compose hub, below).
* **A change accepted while web alone is down**: no flag can change while web is down (the account store is web's), so the authority-only outage of the design text
  ("a change web accepts during its own link loss reaches them by the 5 s reconciliation") needs web's link loss without its exit.
* **Config clients** (the same shared resync for `admin/config/current`: a ban, opt-out or transit change made while an edge is disconnected) need an edge link loss with a change in
  between: same missing primitive. The recorder proxy and the room guard records (GUARDSYNC1) share the primitive.
* The basemap worker is a third consumer; map publication failing closed is not read here (the rig shape of MAP2d needs the basemap tree, `docs/map2d-cases.md`).

## Answers of dv-router (2026-11-22, `answers-342-dvt.md` Q5)

There is no primitive that cuts one process's fabric session without exiting it; the own-link-loss branch stays a GAP. FABCUT1 is recorded as a possible future seam. Nothing in the cases changes.
