<!-- SPDX-License-Identifier: MIT -->
# DP12c and DP18 cases: TX simulate per RF port, the node beacon and objects

Status: written offline for the next DV-Router image (DP12c + DP18, wip 33a6d6e, after SECRELOAD1 and RFWD1). **Nothing here has run on a rig**: there is no image yet.
What the cases check comes from the DV-Router documents (`data.md` 9.5 "Built (DP18)", 10.x "Built (DP12c)", 13.1, `config.md` `[edges.aprsis.beacon]` and
`[[edges.aprsis.objects]]`, `radio.md`, `admin.md`, `status.md`) and from the DV-Router review 232 of the change (`review-232-dp18.md`, read only, cited by name).
Case files `cases/test_dp18_origin.py` (private aprsc), `cases/test_dp18_rf.py` (AX.25 edge, two fake TNCs) and `cases/test_dp18_config.py` (host side, `cfgload`, no rig; helpers `dvtest/dp18cfg.py`), helpers `cases/dp18_support.py`, builders in
`dvtest/proto/aprs.py` (`position_report`, `object_report`, `lat_text_e7`, `lon_text_e7`, `parse_object`), snippets in `rig/config/dv-router/`, unit tests
`cases/unit/test_dp18_support.py` and `cases/unit/test_proto_aprs.py`. Expected lines are built from the RENDERED config of the run with hand-written APRS101 shapes, never from
DV-Router's encoder.

**Skip rule.** Every case skips with the reason on an image without DP12c / DP18. The probe is a capability, not the image tag: the served configuration schema
(`GET /api/v1/admin/schema`) must contain `simulate`, `beacon` and `objects` (`dp18_support.schema_has_dp18`). A run that lacks the shape (snippet) skips as well.

## Snippets (`docs/rig.md`)

| Snippet | What | Needs |
|---------|------|-------|
| `dp18-origin.toml` | `[edges.aprsis.beacon]` (interval 600 s, 10.5 N 20.25 E) and one `[[edges.aprsis.objects]]` `DVTOBJ1` (room B, 144.800MHz, 10.55 N); no RF copies | `dp15-aprsis.toml`, `data-ident.toml` |
| `dp18-origin-withheld.toml` | `[data] position_optout = ["N0TEST"]`: the position policy withholds the gateway call (login `N0TEST-5`) | `dp18-origin.toml` |
| `dp18-rf.toml` | `[edges.ax25]` callsign `N0TEST-5` (equal to the APRS-IS login, a rule for RF copies), `tx_enable` true; ports `rf-a` (`tx = true`) and `rf-b` (`tx = "simulate"`) on ONE physical `radio1`, band 2m, class `afsk1200-shared`, `tx_per_min` 2 burst 1; paths `rf-out` (room B to rf-a) and `rf-out-sim` (room B to rf-b). INSTEAD of `dp15-rf.toml` | `dp15-aprsis.toml` |
| `dp18-rf-objects.toml` | the beacon and ten objects `DVTOB01..DVTOB10` with `rf_ports = ["rf-a"]`, interval 600 s; INSTEAD of `dp18-origin.toml` | `dp18-rf.toml`, `dp15-aprsis.toml` |
| `dp18-rf-floor.toml` | the beacon and TWO objects `DVTOB01`, `DVTOB02` with `rf_ports = ["rf-a"]`, interval 600 s (three reports fit the class share of about four frames, so nothing is dropped and every report is transmitted once per cycle); INSTEAD of `dp18-origin.toml` and `dp18-rf-objects.toml` | `dp18-rf.toml`, `dp15-aprsis.toml` |

## Run commands (the image with DP12c / DP18 is `<image>`; the first lines of `docs/dp15-cases.md` "Run" give the rest of the environment)

```
R=rig/config/dv-router
E=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml:$R/dp15-aprsis.toml
# O: beacon, object, timing, timestamp, counters, worker kill (about 25 minutes: 11 minutes to the first lines, 10 more for the kill case)
XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_SUT_CONFIG_EXTRA=$E:$R/dp18-origin.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp18_origin.py
# W: the position policy withholds the gateway call (about 12 minutes)
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp18-origin.toml:$R/dp18-origin-withheld.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp18_origin.py
# S: simulate port, reload simulate <-> true, restore (about 8 minutes). DVT_SUT_CONFIG_RW=1 is REQUIRED for the reload cases r1 and r2: the rendered
# config is then a copy in out/<run>/xlxd-config/xlxd-sut/rw/, mounted read-write at /etc/dv-router-rw, so the admin API can replace it (the
# default single-file :ro mount answers 409 read_only and the cases skip naming the variable). Do not combine with the echo cases E3 / E7, which
# edit the rendered file directly.
... DVT_SUT_CONFIG_RW=1 DVT_SUT_CONFIG_EXTRA=$E:$R/dp18-rf.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp18_rf.py -k "dp18_s or dp18_r"
# R: eleven reports with RF copies (about 27 minutes for 2 cycles: DVT_DP18_CYCLES=2, the default)
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp18-rf.toml:$R/dp18-rf-objects.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp18_rf.py -k dp18_o
# R strict: the same run (the same 27 minutes: the strict cases read the one observation of the objects_run fixture) with the switch DVT_DP18STRICT=on; see "Strict" below.
# On the images where dv-edge-aprsis exits on rf_ports the no_edge case, the aprsc case and the conservation case FAIL there; that is what the switch is for.
... DVT_DP18STRICT=on DVT_SUT_CONFIG_EXTRA=$E:$R/dp18-rf.toml:$R/dp18-rf-objects.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp18_rf.py -k dp18_o
# F: the interval floor at the RF side, nothing dropped (about 24 minutes: first emission 600 s, last 670 s, 2 cycles, 60 s + 30 s of settling). Add DVT_DP18STRICT=on and
# -k "dp18_o_f or dp18_o_strict" for the strict cases on this shape too (never -k dp18_o here: the spread case asserts a drop, which three reports do not produce).
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp18-rf.toml:$R/dp18-rf-floor.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp18_rf.py -k dp18_o_f
# C: object and beacon configuration, host side (about 20 s measured on wip-495aae0: 30 throw-away containers one after the other; no rig, no network)
DVT_CFGLOAD_IMAGE=<image with DP18> DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_dp18_config.py
```

Times are ESTIMATES (C was measured: 18.5 s). The first beacon is due one full interval (600 s, the floor) after the data worker started, the object 35 s later (one earlier report), object k at 600 + 35 k s.
One rig at a time; the O and R runs hold the rig for the full time. Notes: stdout and `out/<run>/dp18-origin.txt`, `out/<run>/dp18-rf.txt`.

## Case table

| Case | Wording (source) | Steps | Assert | Record | Status |
|------|------------------|-------|--------|--------|--------|
| DP18-a | "node beacon and object lines arrive with `TCPIP*` and the server's qAC" (review 232; `data.md` 9.5: path exactly `TCPIP*`, no client q construct) | the tester's verified login reads the aprsc; wait for the first beacon and the object | each line is `<gateway>>tocall,TCPIP*,qAC,<server>:<info>`: path before the q exactly `TCPIP*`, one q construct that starts with `qAC`, destination `tocall`, the information field byte for byte (beacon `!1030.00N/02015.00E&dvt dp18 beacon`; object `;DVTOBJ1  *HHMMSSh1033.00N/02015.00Er144.800MHz room=B dvt`); the first line comes at node uptime of at least one interval | the lines, the q tail, the node uptime at the first line | written, not run on a rig |
| DP18-b | "the object arrives 35 s after the beacon" | same lines | the object comes 35 s after the beacon, window 34..40 s (worker tick and link) | the measured delta | written, not run on a rig |
| DP18-c | "the object HMS timestamp equals the send time" | same lines | the UTC `HHMMSSh` stamp is within 1 s of the wall clock when the line was read (stamp whole seconds, 0.5 s transit allowance, midnight safe) | the offset | written, not run on a rig |
| DP18-n | `data.md` 13.1 `dv_pkt_origin_total{kind,result}` | counters after the lines | `generated` grows by one per report and kind; `withheld`, `no_edge`, `no_rf_edge` do not move | all deltas | written, not run on a rig |
| DP18-k | "after a dv-data-worker kill, nothing arrives before one full interval" (review 232; `data.md` 9.5: due reports are not replayed, the restarted worker waits a full interval) | `kill-proc xlxd-sut dv-data-worker KILL` (allow-listed, the launcher respawns it); watch 585 s; then wait for the next report | NO line from the gateway call within interval - 15 s of the kill (the unrestarted schedule would send the beacon about 565 s after its previous line); then the new worker's beacon arrives and is at least interval - 15 s after the kill | the delta to the kill | written, not run on a rig |
| DP18-e | "a position policy withholding the gateway call: no line on aprsc and `dv_pkt_origin_total{result=withheld}` grows" | run W; watch the first interval plus the stagger plus 60 s | no line from the gateway call; `withheld` grows for kind beacon and kind object; `generated` stays 0 | deltas | written, not run on a rig |
| DP18-s | "AX.25 simulate port next to a real port on one physical: zero bytes on the simulate port's TNC, ..., the real port keeps its timing, `dv_edge_rf_tx_total{result=simulated}` grows" (review 232; `data.md` 10.x DP12c) | run S; two room texts 31 s apart (module B, web posters) | exactly one frame per text at rf-a within 25 s; NO KISS frame at all at rf-b's TNC after the text (data or other) and no frame carrying the text; `dv_edge_rf_tx_total{port=rf-b,result=simulated}` +2, `{port=rf-a,result=sent}` +2, `{port=rf-b,result=sent}` unchanged; `dv_edge_modem_total{port=rf-b,result=tx}` and `dv_edge_rf_ptt_total{port=rf-b}` unchanged | latencies of the real port, the counter deltas | written, not run on a rig |
| DP18-r1 | "admin reload simulate -> true: the edge restarts and no frame queued before the change is transmitted after it" | three texts posted, then `POST /api/v1/admin/config/changes` (mode apply, `base_etag` from `GET /config` `meta.etag`, op `replace /edges/ax25/port/1/tx` true); the launcher child `dv-edge-ax25` | the child's `restarts` counter grew or its pid changed; the fence view of rf-b is read at once (`fence_view_bounds`, `dvtest/rfsafe1b.py`); the origin is the child start, `since_ms` (Unix ms of the CURRENT start) of the `dv-edge-ax25` entry of the admin status `launcher.children[]` (the hub's procs[] row of an edge has none by design; an implausible value falls back to the time the pid change was seen and skips the 13.0 s lower-bound check): with `DVT_RFSAFE1B=on` (RFSAFE1b image, passed through rig/compose.yml) only the cause `startup_guard` may show (`recovery_probe`, `own_token_pending` or a `probe failed` log line is a FINDING, the log part by `python3 -m dvtest.logscan out/<run>`: a FOUND for DP18-r1-probe-failed), the view clears within 16 s of the child start and not before 13.0 s, `tx_guard_ms` is <= 13000 and non-increasing within one fence epoch, and read time + `tx_guard_ms` predicts the end within 1.5 s; without the switch the view may lag up to about 24.5 s (the case waits up to 30 s) and only the cause and timing are recorded; with `DVT_RFSAFE1C=on` (RFSAFE1c image) the hub's procs[] row of the edge has the launcher child's `since_ms` once its pid is the child's (`procs_origin`, `docs/rfsafe1c-cases.md`; DP18-r2 carries the same check); the edge reconnects to rf-b; no frame carrying a queued text at rf-b in 45 s after the change; a control text then reaches rf-b (the port is live); the change is reverted (same API) and the TX guard of the new process waited out | the child state before and after | written, not run on a rig |
| DP18-r2 | "true -> simulate", "a restore" | one text on air at rf-a, two more queued (tx_per_min 2, burst 1), then `tx` of rf-a -> "simulate"; then `GET /config/history` and `POST /config/history/{seq}/restore` of the newest entry (the file before the apply) | restart as r1; no frame carrying a queued text at rf-a after the change; after the restore a control text is transmitted at rf-a | the restore status; a history list without sequence numbers is a FINDING and the reverse change is used | written, not run on a rig |
| DP18-o | "several RF objects on afsk1200-shared: releases spread over the interval; drops beyond the beacon share are counted (dropped_queue/quota/ttl), not always the same object" (review 232 finding 1) | run R; observe `DVT_DP18_CYCLES` (2) cycles | the gaps between releases at the TNC are at least 30 s (the 35 s stagger, never one tick); the TNC saw exactly the frames counted `sent` (+ `sent_over_budget`); at least one drop counted (eleven reports against a share of about four frames); which objects were transmitted per cycle is recorded, and equal sets in every cycle are a FINDING (not asserted: the share arithmetic is not specified to that depth) | frames, gaps, `dv_edge_rf_tx_total` and `dv_pkt_origin_total` deltas, objects per cycle | written, not run on a rig |
| DP18-o strict (3 cases) | `data.md` 13.1 `dv_pkt_origin_total`; 10.8 item 8 (`sent_over_budget` only for `ident`); 10.8 `dv_edge_rf_queue` | run R (or F) with `DVT_DP18STRICT=on` | (1) `dv_pkt_origin_total{result=no_edge}` and `{result=no_rf_edge}` are 0 (the KNOWN DEFECT fails this one, see "Strict"); (2) `dv_edge_rf_tx_total{result=sent_over_budget}` is 0 on the RF port; (3) conservation: `generated` copies = `sent` + `simulated` + `sent_over_budget` + every `dropped_*` + the change of the gauge `dv_edge_rf_queue`, re-read for up to 45 s (counters are published every 10 s) | the deltas and every unreadable term | written, not run on a rig |
| DP18-o-f | "RF copies obey the 600 s floor" (`data.md` 9.5: at most once per interval, no catch-up burst) | run F (or R: it asserts what repeats there); two cycles | for every OBJECT seen at least twice at the TNC the HMS stamps (the generation time) are at least `interval - 2` s apart, whatever the queue did (a retransmission shows gap 0); the beacon has no stamp. With `DVT_DP18STRICT=on` the release times at the TNC are at least `interval - 15` s apart too, the beacon included | per report: the stamp gaps and the arrival gaps; the reports NOT seen twice. Skips with the reason when nothing repeats (R: most are dropped) | written, not run on a rig |
| DP18-o aprsc | "the configured beacon and objects reach the private aprsc" with RF copies selected | run R or F with `DVT_DP18STRICT=on`; the tester stays logged in at the aprsc for the whole wait | more than one distinct OBJECT and every configured report at the aprsc at least once, each line as in DP18-a (source = gateway call, `TCPIP*` before the server's qAC, the information field byte for byte, the object stamp within 1 s of the read time) | lines per report | written, not run on a rig |
| DP18-cfg (30 params) | `config.md` `[edges.aprsis.beacon]`, `[[edges.aprsis.objects]]`; `aprsis_origin.rs` (DV-Router f00079f4) | `dv-router config check` on `dp18-origin.toml` (with `data-ident.toml`, `dp15-aprsis.toml`) with ONE mutation each | reject (non-zero exit, a positioned `edges.aprsis...` error that names the field and carries the source's wording): station callsign names `N0CALL-7` / `n0call-7` / `N0CALL`, name of 10 characters, leading and trailing space, empty name, non-ASCII name, duplicate name, `interval_secs` 599 (object and beacon) and 86401, lat and lon both 0 (object and beacon), frequency `144.80MHz` / `144.800mhz` / `14a.800MHz`, sent comment of 44 bytes (object: frequency + room + comment; beacon), 17 objects. Accept (exit 0): name of 9 characters, an inner space, the gateway call itself (`N0TEST-5`, also lower case), `interval_secs` 86400, lat 0 with lon not 0, sent comment of exactly 43 bytes (object and beacon), 16 objects, the shipped snippet | the exact error text per case in `$DVT_OUT/config-load/dp18-config.json` | all 30 passed on `dv-router:wip-495aae0` (host side, 2026) |

Limits and what is NOT covered:

* **The `rf/<physical>/ptt` token** of the simulate port is a fabric key; the control channel does not expose it. DP18-s infers "no token and no PTT" from the silence at the
  TNC, `dv_edge_modem_total{result=tx}` and `dv_edge_rf_ptt_total` of the simulate port staying unchanged. It is never asserted directly and is not guessed.
* **No radio ports in the rig.** A radio port (CM108 or serial PTT, audio) with `tx = "simulate"` never keys hardware and never sends audio, and a real radio port on the same
  physical is never blocked: not covered (review 232, "Radio rig"). The AX.25 cases above are the DP12c coverage this rig can give.
* "Zero bytes" at the simulate TNC is counted after the connect: the five KISS parameter frames written at connect (DP15-6s) are not a simulated release. Whether the edge writes them to a
  simulated port at all is recorded by the first connect of `rf-b`, not asserted.
* The spread assertion (gap at least 30 s) and "at least one drop" are derived from the stagger (`data.md` 9.5) and the class share arithmetic in review 232 (1/8 of 5 % per 600 s, about four 0.8 s
  frames on `afsk1200-shared`). Whether the dropped objects differ between cycles is recorded, not asserted.
* DP18-k kills the data worker, which is the only originator (`data.worker[0]`). It is the last case of its file because it needs one more interval.
* Shapes read from the sources and not seen on a rig (UNVERIFIED): the nesting of the admin `config` reply (`meta.etag`, `meta.writable`), the history list envelope (`seq`), HTTP 200 for
  a restart-class change without `accept_pending_restart`, a path to a simulated port being accepted by the launcher, the q tail the aprsc puts after `qAC`, the object `kind` labels of the
  scheduler counters. The first rig run compares them with the documents.
* The TX guard (RFSAFE1): `rf.tx_guard` waits for the fence after the start; after each edge restart the reload cases wait for `tx_fence` to end (`txguard.wait_unfenced`) before expecting a frame.
* **Config access.** The reload cases read and write the configuration only through the admin API (`GET /config` for `meta.etag` and `meta.writable`, `POST /config/changes`
  with `base_etag`, `GET /config/history`, `POST /config/history/{seq}/restore`) and the status route for the child's `restarts` and pid. The rendered `dv-router.toml` of the run is only
  READ at the start (the shape: port numbers and `tx` modes), never edited; with `DVT_SUT_CONFIG_RW=1` it stays the pre-reload copy, and both reload cases leave the SUT on the original shape.
  `cases/unit/test_dp18_support.py` scans the three DP18 modules for file writes.

## Strict (`DVT_DP18STRICT=on`)

The default run of R stays comparable across images. The switch adds the asserts that depend on the APRS-IS leg working TOGETHER with `rf_ports`, on counter conservation
and on the strict floor; it is passed to the tester by `rig/compose.yml`. Without it each strict case skips naming the switch.

* **Known product defect, the reason for the switch.** `dv-edge-aprsis` exits with a configuration error as soon as an object or the beacon has `rf_ports`: `crates/edge/aprsis/src/shell.rs` validates
  the file with `Config::default()` plus only `edges.aprsis`, so the AX.25 port the RF copy names does not exist in what it validates.
  The worker then finds no `aprsis-` edge and counts `dv_pkt_origin_total{result=no_edge}` for every report, and nothing reaches the aprsc. Confirmed by DV-Router; the fix is their unit DP18b, in a later image.
  On an image with the defect the strict `no_edge` case and the strict aprsc case FAIL; the conservation case is expected to pass there (the worker emits the RF copy whether or not an APRS-IS edge exists, `core_origination.rs`).
* **Conservation terms** (read per RF port, deltas since the start of the observation): copies = `dv_pkt_origin_total{result=generated}` of each kind times the RF ports of its reports (the
  case computes it from the rendered config and skips, saying so, when a report names a port other than the one read or the reports of a kind differ in their ports); terminal =
  `dv_edge_rf_tx_total` results `sent`, `simulated`, `sent_over_budget` and every result that starts with `dropped_`, summed over the scheduler classes; queued = the change of the gauge
  `dv_edge_rf_queue{port}` (frames queued for the physical transmitter). `delayed` is NOT terminal (the frame is still queued). A frame refused before the scheduler (an `rf_fenced` admission) would
  show as unaccounted; none can be due in the 13 s TX guard of a start because the first report is due after one full interval.
* **Hard asserts.** Under the switch `no_edge` / `no_rf_edge` = 0, `sent_over_budget` = 0 and the conservation are hard asserts. The conservation case writes ONE `RECORD` line before it asserts that names every term (generated per kind and result, copies expected, sent, simulated, sent_over_budget, delayed, each `dropped_*` and their sum, terminal, the queue gauge at the start and now, and the unaccounted difference), so on the DP18b image a mismatch is a product accounting finding that shows exactly which terms differ.
* **Terms that cannot be read** are recorded as `RECORD` lines and are not guessed: a missing `dv_edge_rf_queue` series for the port (only "terminal is not more than generated" is then checked); the frames
  that are in flight between a scheduler release and the modem adapter (counted `sent` at the release; the TNC count is compared in the spread case, not here); two RF ports on one physical (the gauge is
  per physical transmitter: the case reads the first RF port and its snippets use `rf-a` only).
* **The floor** is judged on the object's HMS stamp, which is the generation time, so a queue that delays one release cannot create a false failure; the release times at the TNC are judged only with the
  switch (a queue delay may legitimately shorten that gap). Observing the floor needs two cycles: at least 1330 s with `dp18-rf-floor.toml` (stage F), and R already runs two cycles
  (`DVT_DP18_CYCLES`, default 2), where most of the eleven reports are dropped and few repeat, so F is the clean run.

## Done when (DV-Router work unit DP18)

"The configured beacon and objects reach the private aprsc; RF copies obey the 600 s floor and the airtime budget; object names are validated; dv-testset aprsc case."

| Clause | Cases | Status |
|--------|-------|--------|
| beacon and objects reach the private aprsc | DP18-a, b, c, n (run O); DP18-o aprsc (run R / F, strict: with `rf_ports` set) | O green on wip-8891cc7 (work-units.md DP18 row); the strict case is written, expected to FAIL on images with the `rf_ports` defect |
| the 600 s floor on the APRS-IS side | DP18-k (no replay after a worker kill) and the first-line timing of DP18-a | O green on wip-8891cc7 (work-units.md DP18 row) |
| RF copies obey the 600 s floor | DP18-o-f (run F, optional long stage of about 24 minutes; R asserts what repeats) | written, not run |
| RF copies obey the airtime budget | DP18-o (spread, drops counted, TNC frames = counted `sent`), strict: nothing `sent_over_budget`, conservation | DP18-o green on wip-8891cc7 (R, work-units.md DP18 row); strict cases written, not run |
| object names are validated (and the beacon / object fields) | DP18-cfg (30 params, run C) | 30 of 30 on wip-495aae0 |
| a position policy withholds the gateway call | DP18-e (run W) | W green on wip-8891cc7 (work-units.md DP18 row) |
| no secret, no peer address in the logs | not a DP18 clause; DP11e-h2 covers the KISS side | n/a |
