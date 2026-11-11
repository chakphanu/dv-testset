<!-- SPDX-License-Identifier: MIT -->
# DP12c and DP18 cases: TX simulate per RF port, the node beacon and objects

Status: written offline for the next DV-Router image (DP12c + DP18, wip 33a6d6e, after SECRELOAD1 and RFWD1). **Nothing here has run on a rig**: there is no image yet.
What the cases check comes from the DV-Router documents (`data.md` 9.5 "Built (DP18)", 10.x "Built (DP12c)", 13.1, `config.md` `[edges.aprsis.beacon]` and
`[[edges.aprsis.objects]]`, `radio.md`, `admin.md`, `status.md`) and from the DV-Router review 232 of the change (`review-232-dp18.md`, read only, cited by name).
Case files `cases/test_dp18_origin.py` (private aprsc) and `cases/test_dp18_rf.py` (AX.25 edge, two fake TNCs), helpers `cases/dp18_support.py`, builders in
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
```

Times are ESTIMATES. The first beacon is due one full interval (600 s, the floor) after the data worker started, the object 35 s later (one earlier report), object k at 600 + 35 k s.
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
