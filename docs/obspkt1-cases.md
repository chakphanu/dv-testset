<!-- SPDX-License-Identifier: MIT -->
# OBSPKT1 cases: the admin packet monitor

Status: written offline for the image after wip-8891cc7 (merge `b9a6839e`); **nothing here has run on a rig**. Sources: DV-Router `docs/design/data.md` 13.5 ("Admin packet monitor"),
`docs/design/admin.md` ("Packet monitor (OBSPKT1)", the routes table) and `docs/api/openapi-admin.yaml` (`PktMonRow`, `PacketSnapshot`, the two routes), read at that commit.
Files: `cases/test_obspkt1_monitor.py`, helpers `dvtest/obspkt1.py`, snippet `rig/config/dv-router/obspkt1-hidden.toml`, unit tests `cases/unit/test_obspkt1_support.py`.

**Skip rule.** The module skips with the reason when the rendered config lacks shape K of DP11e, the hidden base callsign, or the transit path, and when
`GET /api/v1/admin/packets` answers 404 (an image without the monitor).

## The run

The packets come from paths the other groups already use: the fake modems rf-a / rf-b (as DP15 / DP11e), the private aprsc, the transit path to APRS-IS (`dp11e-kiss-igate.toml`), a web
room text to RF (`dp15-rf-out.toml`) and a local KISS client (the listener `apps` of `dp11e-kiss.toml`). The position policy hides the base callsign `N0HID` (`obspkt1-hidden.toml`).

```
R=rig/config/dv-router
E=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml:$R/dp15-aprsis.toml
# about 14 minutes (ESTIMATE): the traffic fixture about 2 minutes, OBSPKT1-f waits up to 5 minutes for a step-up window to close, the restarts about 3 minutes
XLXD_SUT_IMAGE=<image after wip-8891cc7> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_RIG_CONTROL=1 \
DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss.toml:$R/dp11e-kiss-igate.toml:$R/dp15-rf-out.toml:$R/obspkt1-hidden.toml \
scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_obspkt1_monitor.py
```

The order of the file matters: the module fixture injects the traffic once; `-e` reads the logs before `-d` restarts processes; `-f` waits for the second admin login's window; `-d` is last.
Sources of the injected frames are synthetic: `N0OBA-1` (status, gated), `N0OBB-1` (NOGATE), `N0OBC-1` (general query), `N0HID-7` / `N0HID-9` (hidden position, hidden Mic-E), `N0OBD-1` / `N0OBE-1`
(visible position, visible Mic-E), `N0OBH-5` (the account that gets `hide_position` later), the client `N0TEST-7`. Every payload carries a unique marker `OBSPKT1X<k><hex>`.

## Case table

| Case | Rule (source) | Steps | Assert | Record |
|------|---------------|-------|--------|--------|
| OBSPKT1-a1 | viewer and operator get 403 `forbidden` on both routes (admin.md) | each role (with a step-up window, so the window is not the reason) calls `GET /api/v1/admin/packets` and opens `/packets/stream` | HTTP 403 and code `forbidden` on the snapshot, HTTP 403 on the stream | |
| OBSPKT1-a2 | admin with a step-up window: the documented snapshot and stream (openapi-admin.yaml) | snapshot with `limit=1000`, `limit=1`, `limit=5000`, `dir=rx`, `port=rf-a`, bad `dir`, a 200-character `port`; open the stream | 200, `Cache-Control: no-store`, `{rows, ports, producers}` with the PktMonRow keys, stages and bounds, newest first, `limit` honoured (5000 clamps or is a 400), filters honoured, bad parameters 400; the stream is 200 `text/event-stream` and its first frame is `hello` with `producers[{id, boot (16 bytes)}]` | the keys seen, the hello producers, `retry` |
| OBSPKT1-b1 | RF rx rows with the gate decision (data 13.5: worker parse / RF policy / route; edge modem / raw) | a status packet (gated), a NOGATE packet, a general query heard on rf-a | worker rx row on rf-a with the TNC2 line and route outcome `forwarded` / `no_gate` / `filtered` (the counter label sets of `dv_pkt_path_total`); an edge rx row of the same source with a `header`, no `line`, the same `ref`; the APRS-IS edge tx row of the gated frame has a `write` outcome and no line | the outcomes and refs |
| OBSPKT1-b2 | APRS-IS rx and RF tx rows | a message to the room name from the tester's login through the aprsc; a web room text to module B (leaves over rf-a) | `is` rx: worker row with the line holding the message body (directed bodies are available in the privileged monitor), edge row header only; rf-a tx: edge row header only with `txcore` / `sched` outcomes, the worker row of the same `ref` has the line | outcomes |
| OBSPKT1-b3 | a local KISS client row keeps its line except a position (data 13.5) | the client sends a message and a position through `apps` | edge tx row with the message line in full and a `client` (or `txcore`) outcome; the position row has an empty information field after the header and `redacted: "position_policy"`; its coordinates appear in no row | |
| OBSPKT1-c1 | a hidden station's position is blank in every row (data 13.4 / 13.5) | position from `N0HID-7` and a visible control from `N0OBD-1` | `N0HID-7` worker row: empty information field, `redacted: "position_policy"`; its coordinates and comment marker appear in NO row; the visible row shows its coordinates, no `redacted` | the two lines |
| OBSPKT1-c2 | Mic-E destinations (data 13.5) | Mic-E packets from the hidden `N0HID-9` and the visible `N0OBE-1` | the hidden worker row is `SRC>(mic-e),PATH:` with an empty information field, `redacted`; every edge row of both shows `(mic-e)` in the header and not the real destination | the visible worker row |
| OBSPKT1-c3 | edge rows of worker-decided packets carry no line (data 13.5) | all rows | no edge row has a `line` unless it has a `client` outcome; edge rows do carry headers | |
| OBSPKT1-c4 | retained rows are re-checked against the CURRENT policy (data 13.5) | a visible position from the web account `N0OBH` is retained; the operator sets `hide_position` | after the policy push the retained row is blank and `redacted`, and its coordinates are in no row | the row before and after |
| OBSPKT1-e | no packet body anywhere else (data 13.5; openapi) | markers of packets that never become room items (RF status, NOGATE status, the client message) | each appears in the monitor (control) and in none of: the SUT logs of the run, the public state, `/api/v1/admin/{status,clients,events,lastheard}`, the hub metrics | the scanned sources and sizes |
| OBSPKT1-f | admin without a fresh step-up: 403 `step_up_required` (admin.md) | a second admin login made at the start of the module; the case waits until its window (a login opens one) has closed, 5 minutes plus 15 s | 403 `step_up_required` on the snapshot, 403 on the stream; after `step_up()` the snapshot is 200 | the wait |
| OBSPKT1-d | a process restart clears the rings (data 13.5: "a new process boot starts empty") | kill `dv-data-worker` (allowed kill-proc); then restart `dv-edge-ax25` through the admin API (as DP15-6g) with the stream open | after the kill the worker boot changed, no worker row with a marker survives, the edge rows stay and the edge boot is unchanged; after the restart the AX.25 boot changed and its old rows are gone; the stream carried a `gap` event | the boots, the stream events |

## GAPs and limits

| GAP | Why | Covered by |
|-----|-----|------------|
| A whole-container restart | the rings live in the child processes, so the two process restarts of OBSPKT1-d prove the lifetime; a container restart (`restart xlxd-sut`) would end the admin sessions and need a new enrolment | the same rule |
| The APRS-IS edge ring after a restart of `dv-edge-aprsis` | the child is not restarted by the case | OBSPKT1-d covers the AX.25 edge and the worker |
| Ring bounds (256 rows, 128 KiB, 30 minutes, eviction, `dv_*_monitor_evicted_total`) | needs hundreds of packets or half an hour of waiting | DV-Router's `dv_lib_pkt::monitor` tests |
| The stream limits (four per service, one per session, 30 minutes, `resync` after 512 rows, `bye`) | needs several sessions and long runs | DV-Router's admin tests |
| Cursor loss and 503 `unavailable` during a boot change in a pagination | a race in the SUT | DV-Router's admin tests |
| Audit rows `packets.snapshot` / `packets.open` (user, port filter, result, never rows) | the audit route is outside this case | DV-Router's admin tests |
| Raw TNC2 modem adapters give no header on receive rows | the rig has no raw TNC2 port | DV-Router's tests |
| Retired port rings after a configuration reload | needs a config change | DV-Router's tests |

## Shapes read from the documents and not confirmed (check on the first run, as in DP11e-h; a deviation fails with what was seen)

* The snapshot envelope `{rows, ports, producers}`, the row keys (`seq`, `t_ms`, `port`, `dir`, optional `ref`, `of`, `line`, `header`, `redacted`, `out[]`, `more`) plus `id` and `boot` on
  every snapshot row (openapi `PacketSnapshotRow`).
* The producer ids: the case tells edge rows from worker rows by the id prefix `ax25-` / `aprsis-` (the edge id form of the DV-Router tests); a worker id is anything else.
* The header text: it is assumed to start with `SRC>DST` (data 13.5: the already validated AX.25/TNC2 header); `ref` equal between an edge row and its worker row.
* The exact `result` of the route outcome (`forwarded`, `no_gate`, `filtered`) is derived from the counter label sets of the worker path (`router/transit.rs`); the stage names are the fixed
  list of the schema. The `client` stage name for the KISS client row is also from the schema; the case accepts `txcore` as well.
* The APRS-IS edge tx row: port name `is` (data 13.5), a `write` outcome.
* The error codes `forbidden` and `step_up_required` in the problem+json body (admin.md).
* `limit=5000` is clamped (200 with at most 1000 rows) or refused with 400: both are accepted.
* The SSE `gap` event name and `{id}` data for a producer restart (admin.md).
