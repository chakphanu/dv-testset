<!-- SPDX-License-Identifier: MIT -->
# BUD1 cases: no legitimate body is refused by a byte budget; the admin shows "and N more"

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`; BUD1 `a15b08fa`, BUD1b `364a5760`). **Nothing here has run on a rig.**
Files: checker `dvtest/bud1.py`, session hook `dvtest/bud1_plugin.py` (wired in `cases/conftest.py`: `pytest_sessionstart` / `pytest_sessionfinish`), cases `cases/test_bud1.py`, unit tests
`cases/unit/test_bud1_lib.py`, switch `DVT_BUD1` (and `DVT_BUD1_EVERY_S`, `DVT_BUD1_SETTLE_S`; passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`).
Notes: stdout and `out/<run>/bud1.txt` (`RECORD`, `FINDING`).

## The rule, and the checker for every stage of a chain

Document: `status.md` (`dv_fabric_body_rejected_total{reason}`, `dv_status_body_rejected_total`, `dv_edge_drops_total{reason=body_oversized}`, `dv_codec_body_dropped_total`,
`dv_pkt_body_dropped_total{reason=oversized}`), K6b `crates/router/tests/real_children/body_budgets.rs` (the exact process set; "a legitimate smoke body was refused by its byte budget";
a process that published without the complete family fails). Every process of the node exports `dv_fabric_body_rejected_total` with the reasons `query_reply_oversized`,
`query_request_oversized`, `subscription_oversized` (admin adds `hub_reply_oversize`, `admin_reply_oversize`; web `hub_reply_oversize`, `web_reply_oversize`); the hub re-exports each process's
newest record (every 10 s) with a `proc` label and adds `proc="status"` for its own. A suite of legitimate traffic must leave all of them at 0 in every process.

**Where the data is.** The control channel's `metrics xlxd-sut` is the only reader of the hub, and run.sh saves nothing of it by itself (only the reply file of a request a case made). So with
`DVT_BUD1=on` the tester's session hook samples the hub itself:

* every `DVT_BUD1_EVERY_S` seconds (default 60; each read starts a throwaway container, `scripts/lib/metrics.sh`) a record in `out/<run>/bud1/samples.jsonl` (time, service, the non-zero watched counters, the
  process set; an unreachable hub is an `error` record, never a failure of the run). A counter restarts with its process, so a refusal in a process that a case restarts later is only seen by a sample in between;
* at the end of the session, after `DVT_BUD1_SETTLE_S` seconds (default 11: one telemetry period, so the last put is in), the full snapshot `out/<run>/bud1/final-xlxd-sut.prom` (and `final-dvr-peer.prom`
  with `--profile twonode`, `DVT_TWONODE=on`).

**The checker, on the host, once per stage or once for the chain:**

```
python3 -m dvtest.bud1 out/<run>                          # one stage
python3 -m dvtest.bud1 out/<run1> out/<run2> out/<run3>   # a chain: the stage directories in one call
python3 -m dvtest.bud1 --lenient out/<run>                # an image before BUD1 (no family) is a note, not a failure
```

Exit 0: every sample and every final snapshot clean, every process of the final snapshot with the complete adapter family. Exit 1: a refusal counter above 0 (named with service, series and the time of the first
sample that showed it), or a process without the complete family, or no BUD1 family at all. Exit 2: no `bud1/` data in a directory (the run had no `DVT_BUD1=on`, no control channel, or the session did not
finish). Put `DVT_BUD1=on` into every stage's command of a chain and the checker after it; no case file is needed for the check itself, any pytest stage gets it (the hook is in `cases/conftest.py`).

## Cases (`cases/test_bud1.py`, the web profile with `admin.toml` and `totp-required.toml` for BUD1-b and BUD1-c)

| Case | Statement | Assertion |
|------|-----------|-----------|
| BUD1-a | no legitimate body is refused | after 12 s of settle: every watched refusal counter 0, every process that exports anything carries the complete adapter family; the process set is recorded |
| BUD1-b | a normal admin reply is not truncated | `GET /api/v1/admin/status`, `/rooms`, `/lastheard?limit=1000`, `/clients?limit=1000` (step-up) answer 200 with no `truncated` key and `total` >= the returned items |
| BUD1-c | "and N more" | the admin config VALIDATE (`PUT /config/raw?mode=validate`, `If-Match`, step-up) of the unchanged config has no `truncated` key; of a config with 40 invalid `[access] allow` entries it answers 422 with `errors` cut to 32 and `truncated.errors` = `{dropped >= 1, cut: 32}` (bounds.rs: 32 errors, 32 warnings, 144 actions, 16 restart paths, 80 pinned values, 128 children); 40 invalid `access.allow` entries give 40 diagnostics (dv-router, 2026-11-22: one error per invalid entry, validation does not stop at the first; UNVERIFIED end to end through the HTTP wire): `errors` must be exactly 32 long, so a shorter list fails |

The wire of "and N more": the admin replies carry `truncated.<collection>.{dropped,cut}` (omitted when 0; fixed totals are never reduced; `openapi-admin.yaml` `Truncation`), the admin UI string is
`andMore(dropped)` (`web/shared/i18n.js`: "and {count} more"; Thai "และอีก {count} รายการ"), the server appends `, and N more` to detail text (`crates/svc/admin/src/app.rs`).

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_BUD1=on \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_bud1.py
python3 -m dvtest.bud1 out/<run>
```

Estimated time (not measured): about 4 minutes (BUD1-a settles 12 s, the admin logins and step-ups, the hook adds 12 s at the end). For any other stage add `DVT_BUD1=on` to its command: the cost is one hub read a minute
and 12 s at the end.

## GAPs

* **The large-list cut of `/clients`, `/lastheard`, `/rooms`, `/status`** cannot be reached from outside: the HTTP `limit` clamps to 1000 rows, the byte caps (512 KiB clients, 944 KiB heard, 128 KiB procs) need rows of
  hundreds of bytes each beyond that, rooms are capped at 26 and always complete, and the per-edge session cut needs more than 2048 rows on one edge (the admission cap is 1024 clients) or more than 64
  edge incarnations. The unit tests `crates/svc/admin/tests/api.rs` `bud1b_*` drive it with a fake hub. BUD1-b therefore only asserts the absence of `truncated` on a small node.
* **The UI text "and N more" in a browser** (the admin config page) needs the same truncated reply; the string is covered by the web unit test `web/test/bud1-truncation.test.mjs` only.
* **601 legitimate RTC peers in the hub store** (the raised 200 to 640 cap) and the WebRTC signalling budget are not practical from the rig; byte budgets "before copy and decode", the 23 + 11 killed mutations,
  the fuzz runs and the `login.race` audit are not black-box.
* The K6b process set is asserted as "every process that publishes carries the complete family"; its exact list of 8 (work-units says 15) is not reproduced because the rig's process set depends on the config.

## Answers of dv-router (2026-11-22, `answers-342-dvt.md` Q8, Q10) and what changed

* Diagnostics: 40 invalid `access.allow` entries give 40 errors; BUD1-c asserts the cut (no more skip).
* No refused-body counter survives a process restart (the hub keeps one entry per process, the newest `start_ms` replaces it). So `RigControl.kill_proc` runs `kill_hooks` first, and the BUD1 hook
  (`Hook.pre_kill`) waits 11 s and samples before the FIRST kill of a process in a burst (kills of the same process within 30 s pass at once). With `DVT_BUD1=on` every first kill costs 11 s.
* K6b's exact set is the 8 identities of `MAIN` (`body_budgets.rs:13`: launcher, controller room-a, edge usrp, web, admin, recorder, basemap, data-worker); "15" is loose (six more fixtures use
  `assert_none` on one or two identities). A full-node rig expects no fixed number: BUD1-a asserts the zero family per process and RECORDS the set seen.
