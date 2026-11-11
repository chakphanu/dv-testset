<!-- SPDX-License-Identifier: MIT -->
# RFSAFE1c cases: a mid-run fence in the status view, named probe failures, the launcher start in the hub rows

Status: written offline for the next DV-Router image (RFSAFE1c, merge `63fecd9f`, after RFSAFE1b). **Nothing here has run on a rig**: there is no image yet.
What the cases check is what the DV-Router team stated for the change, read against the documents at that commit (`radio.md` 10.4, `status.md` "Process up and down" and the
modem `update` paragraph, `data.md` 10.x, `fabric.md` "Events", `testing.md`, `openapi-admin.yaml`) and the status hub source (`procs.rs`, read only, for the shapes).
Case file `cases/test_rfsafe1c.py`, pure helpers `dvtest/rfsafe1c.py`, unit tests `cases/unit/test_rfsafe1c_lib.py` and `cases/unit/test_rfsafe1c_cases.py`, switch
`DVT_RFSAFE1C` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`). Notes: stdout and `out/<run>/rfsafe1c.txt` (`RECORD`, `FINDING`).

**Gate.** `DVT_RFSAFE1C=on` asserts the statements below; without it every case skips with the reason (an image before RFSAFE1c has no `update` event and a
different log line, so a lenient mode would only record noise). The RFSAFE1b switch `DVT_RFSAFE1B=on` is independent and can be set in the same run (an RFSAFE1c image has 1b).

## What is asserted and what is a GAP

| # | Statement of the DV-Router team | Case | Status |
|---|---------------------------------|------|--------|
| 1 | a fence that BEGINS mid-run (the observer's session cut, a foreign holder injected) shows in the status view within 2 s of the cause, and TX is refused from the first moment regardless of the view | RF1c-1 | **GAP**, the case skips and says why (below); `dvtest.rfsafe1c.view_latency` / `judge_latency` are written and unit-tested |
| 2 | no `session` / `update` hint item in status/events or the public change feed | RF1c-2 | asserted over one restart of `dv-edge-ax25` (the fence of the new process begins and ends inside the window) |
| 3 | the log line is `probe failed part=<own_token\|link_check\|holder_watch\|holder_query\|task>` once per failure run, with the configured physical only | RF1c-3 | shape, fixed part set, configured physical and "no other text" asserted on every `probe failed` line; "once per run" is a FINDING (below); the SUT log is read on the host after the run |
| 4 | edge procs rows: `since_ms` equals the launcher child's `since_ms` once the pid matches | RF1c-4a, RF1c-4b; also DP18-r1 and DP18-r2 | asserted; a stale row before the pid match is recorded and waited out |

### GAP: RF1c-1, why the cause cannot be produced

The cases talk to the system under test through UDP, the HTTP APIs and the control channel of `scripts/run.sh` (its header). The two causes need:

* **a foreign holder**: another fabric client that declares an `rf/<physical>/ptt` token on the same physical. The tester has no fabric client (the fabric listens on the SUT's private
  trust network), and a second radio or AX.25 edge on the same physical does not exist in the rig (no radio ports at all);
* **a cut observer session** (`session_loss`): the fabric session of `dv-edge-ax25` to the launcher's router. `kill-proc` signals `dv-edge-xlx`, `dv-edge-nxdn`, `dv-edge-p25`,
  `dv-codec-ambed`, `dv-svc-web`, `dv-edge-webrtc`, `dv-svc-status`, `dv-data-worker` and `dv-room` only (never `dv-edge-ax25` or the launcher); `netem` impairs the tester's or
  the SUT container's egress, not the loopback fabric.

Restarting `dv-edge-ax25` (admin API) is not such a cause: the new process starts with the cause `startup_guard`, the view row disappears and comes back, and RFSAFE1b already
measures that. No new control action is invented here. What is needed from DV-Router (or the rig): a documented admin or control primitive that holds a throw-away `rf/<physical>/ptt`
token, or cuts the AX.25 edge's fabric session for a bounded time. Then RF1c-1 becomes: read the view (unfenced), apply the cause, `view_latency(read, cause)` from the cause, assert
the latency is at most 2 s, and offer a frame to the fake TNC at the cause: ZERO bytes (TX refused from the first moment, whatever the view says).

### Other limits

* **(3) once per failure run.** A run ends with a successful probe, which resets the state before the guard `G` ends; so two runs can follow each other with no
  `RF TX observation on <physical> recovered` line between them and look like one. Two lines for one physical without a recovery line between are therefore a **FINDING**,
  not a failure. The failure cases cannot be produced either (same GAP as (1)): on a healthy run the line must NOT appear, and any line is recorded as a FINDING (RFSAFE1c: the
  new process declares its token before probing, so a healthy start has no failed probe).
* **(3) the log is collected after pytest ends.** The case then SKIPS with the host command; run `python3 -m dvtest.rfsafe1c out/<run>` (exit 1 on a line that breaks the shape, `FINDING`
  lines for the rest). `python3 -m dvtest.logscan out/<run>` also scans for the needle `probe failed` (the needle file `logscan/RF1c-probe-failed.json` is written by the case).
* **(2) the admin SSE is not read**, the admin event history (`GET /api/v1/admin/events`, the hub's event ring that the admin feed fills) and the public SSE are. The statement covers
  "status/events" and "the public change feed". A hint is an item with `type` `session` and `ev` `update`; an `ev` `update` of another type is recorded, not failed. The `snapshot`
  item of the repaired snapshot is allowed.
* No radio edge or radio port is in the rig; the same fields exist on radio ports (`procs[].radio.ports[]`), they are not covered.

## Run commands

The image with RFSAFE1c is `<image>`; the environment lines of `docs/dp15-cases.md` "Run commands" give the rest (template, fake transcoder).

```
R=rig/config/dv-router
E=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml
# A: RF1c-4a, 4b, 2, 3, 1 on the DP15 RF-ON shape (about 6 minutes, estimate: 3 for the rig and the web enrolment, about 1.5 for the cases)
XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=true DVT_RFSAFE1C=on DVT_RFSAFE1B=on \
DVT_SUT_CONFIG_EXTRA=$E:$R/dp15-rf.toml:$R/dp15-rf-out.toml scripts/run.sh -m rig --capture --profile web -- cases/test_rfsafe1c.py
# host, after the run: the SUT log
python3 -m dvtest.rfsafe1c out/<run> ; python3 -m dvtest.logscan out/<run>

# B: A plus the DP18 reload cases, which carry the same procs[] check after their two restarts (about 12 minutes, estimate: the DP18 S run is about 8 including the rig, plus about 1.5 for the RF1c cases; dp18-rf.toml INSTEAD of dp15-rf.toml, DVT_SUT_CONFIG_RW=1)
XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_RFSAFE1C=on DVT_RFSAFE1B=on DVT_SUT_CONFIG_RW=1 \
DVT_SUT_CONFIG_EXTRA=$E:$R/dp15-aprsis.toml:$R/dp18-rf.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_rfsafe1c.py cases/test_dp18_rf.py -k "rfsafe1c or dp18_r"
```

Times are ESTIMATES. One rig at a time. Run A needs `DVT_DP15_TX_ENABLE=true` and `dp15-rf-out.toml` (the effective-TX shape of `rf-a`); with `false` the cases skip naming the shape. The cases
restart `dv-edge-ax25` once through `POST /api/v1/admin/children/dv-edge-ax25/restart` (admin role, step-up), the audited admin API that DP15-6g also uses.

## Case table

| Case | Wording (source) | Steps | Assert | Record | Status |
|------|------------------|-------|--------|--------|--------|
| RF1c-4a | `status.md`: a token-bearing row "adds `state`, `restarts`, `since_ms` and `pid`" from the launcher child; `since_ms` is "the launcher's child start ... fresh telemetry does not replace it" | `GET /api/v1/admin/status`: `launcher.children[]` and `procs[]` of ONE reply, for every child of kind edge, codec-worker, data-worker, controller (`dv-room[AB]` gives rows A and B; the key is mapped as `procs.rs` `child_proc`) | once a row's `pid` equals its child's, `since_ms` equal (a zero launcher start is "unknown" and the row must omit it); the difference must persist over two reads (a launcher put between the halves of a reply is not a failure); every pair matched within 15 s | the stale rows met (pid, `up`, `since_ms`), polls, `restarts` of row and child | written, not run on a rig |
| RF1c-4b | the same, after `dv-edge-ax25` restarted: "the new token can appear before the launcher's state put lands, so for up to the 10 s query the row can show the new `up` with the PREVIOUS child's `since_ms` and pid" | status before (child pid), restart, poll the status every 0.5 s until the launcher child's pid is not the old one and the row's pid is the child's | as 4a for the new child; a launcher child that still has the old pid or a row that never reaches the child's pid within 15 s (10 s query + 5 s slack) fails | each stale row with the time since the restart and whether it still showed the PREVIOUS child | written, not run on a rig |
| RF1c-2 | `status.md` / `data.md` 10.x: the `update` hint "adds no `session` item to the change feed or event ring"; "the status hub forwards no change-feed item for it" | the public SSE `/api/v1/public/events` is open from before the restart; after the fence of the new process ended (and 4 s for the hub's refresh) the frames, and `GET /api/v1/admin/events?limit=1000` above the id read before the restart | no item `type` `session` with `ev` `update` in either | counts per type and `ev` of both feeds (the repaired `snapshot` items are visible there), other `ev` `update` items | written, not run on a rig |
| RF1c-3 | `radio.md` 10.4: `RF TX fenced on <physical>: probe failed part=<part>; recovery is automatic` once at the start of a run of failed probes; fixed parts; "No holder, token key or backend error text enters this line" | the SUT logs of the run (`logs/*sut*`), every line containing `probe failed` | the line has the shape (a prefix before `RF TX fenced` is allowed), `part` in the fixed set, the physical is a `physical` of the rendered config, nothing follows `; recovery is automatic` | every probe failed line (FINDING after a healthy restart), a repeat for one physical with no recovery line between (FINDING) | written, not run on a rig |
| RF1c-1 | statement (1) | none | none | the GAP text | GAP, skips |
| DP18-r1, DP18-r2 | RFSAFE1c (4) after the reload restarts | `procs_origin` in `cases/test_dp18_rf.py` after `wait_edge_restart` (r1: after the fence view bounds) | as RF1c-4b, only with `DVT_RFSAFE1C=on` | the same lines | written, not run on a rig |

## Shapes read from the sources and not seen on a rig (UNVERIFIED)

* **`procs` in the admin status.** `admin.md` says `GET /api/v1/admin/status` serves `status/state` `procs` as `procs` next to `launcher`; `openapi-admin.yaml` does not enumerate the
  `procs` row. The row fields (`kind`, `proc`, `up`, `source`, `state`, `restarts`, `since_ms`, `pid`) and the row key of the AX.25 edge (`edge` / `ax25`, from the launcher child `dv-edge-ax25`) come from
  `procs.rs` and `fabric/src/status.rs`. If the admin view hides `pid` or `since_ms` for the operator role the cases report "row pid None": the stale state until the 15 s bound, then a failure
  that names the row; read the record before calling it a product bug.
* **The event ring items.** `type`, `ev`, `id`, `ts_ms` are in `openapi-admin.yaml`; the public SSE frame names (`event:`) and whether the `type` is also in the data are not: the helper
  accepts the type from the data or from the frame name, and a frame named `update` is a hint whatever its data.
* **The log prefix.** The diagnostic is written by `PttEvents::diagnostic` without a process tag; the container log may add a timestamp: a prefix is allowed, a suffix is not.
* **The physical names.** The configured physical is the `physical` of each AX.25 / radio port of the rendered `dv-router.toml` (`radio1` in `dp18-rf.toml`); `dp15-rf.toml` ports without
  `physical` give no set and the physical is then not checked (recorded).
* Whether the fence of a restarted edge produces a modem `update` event at all (the first snapshot may already carry it) is not known; if it does not, RF1c-2 proves only that the restart added
  no hint, and the record says how many items were seen. The event the statement is about needs the GAP cause.
