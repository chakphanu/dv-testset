<!-- SPDX-License-Identifier: MIT -->
# OBSPKT1 cases: the admin packet monitor

Status: written offline for the image after wip-8891cc7 (merge `b9a6839e`); **nothing here has run on a rig**. The response shapes were checked by DV-Router against the code at that commit (section "Confirmed shapes"). Sources: DV-Router `docs/design/data.md` 13.5 ("Admin packet monitor"),
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
| OBSPKT1-a1 | viewer and operator get 403 `forbidden`, no session gets 401 `unauthorized` (admin.md) | each role (with a step-up window, so the window is not the reason) and a logged-out client call `GET /api/v1/admin/packets` and open `/packets/stream` | 403 `forbidden` for viewer and operator, 401 `unauthorized` without a session, on the snapshot and on the stream | |
| OBSPKT1-a2 | admin with a step-up window: the documented snapshot and stream (openapi-admin.yaml) | snapshots with `limit` 1000, 1, 0, 5000, default, `dir` rx / tx / both / empty, `port` rf-a / is / 64 characters, and bad `dir`, 65-character or `a/b` or `a b` `port`, `limit` `abc` or `-1`; open the stream | 200, `Cache-Control: no-store`, `{rows, ports, producers}` with the PktMonRow keys, stages and bounds, newest first; `limit` 5000 is CLAMPED to 1000, the default is 100, 0 becomes 1; the bad values are 400 `bad_request`; the stream is 200 `text/event-stream`, `retry: 3000` and a first frame `hello` with `producers[{id, boot (16 integers)}]` | the keys seen, the hello producers |
| OBSPKT1-b1 | RF rx rows with the gate decision (data 13.5: worker parse / RF policy / route; edge modem / raw) | a status packet (gated), a NOGATE packet, a general query heard on rf-a | worker rx row on rf-a with the TNC2 line and route outcome `forwarded` / `no_gate` / `filtered` (the counter label sets of `dv_pkt_path_total`); an edge rx row of the same source with a `header` (equal to the worker line's header: `SRC>DST,DIGI,DIGI*`, no colon), no `line`, the same `ref` (the raw unit seq); the delivery to an attached KISS client is a LATER body-free row (dir rx, same port, `of` = the seq of the edge rx row, the same `ref`, stage `client` result `sent`; kiss_server.rs `output_sent_observed`), not an outcome of the rx row; the APRS-IS tx of the gated frame is two rows: the edge start row (`ref`, `header`, no line, stage `render` result `received`) and a later row with `of` = its seq and the same ref with stage `write` and a result of the fixed set (`framed` expected; another value in the set is recorded as a FINDING) | the outcomes, refs and the write result |
| OBSPKT1-b2 | APRS-IS rx and RF tx rows | a message to the room name from the tester's login through the aprsc; a web room text to module B (leaves over rf-a) | `is` rx: worker row with the line holding the message body (directed bodies are available in the privileged monitor), edge row header only; rf-a tx: the edge start row has the header, the `ref` and NO outcome, and no line; its `txcore` / `sched` (and `modem`) outcomes are LATER body-free rows with `of` = its seq and the same `ref` (tx.rs `finish_monitor`, shell.rs `release_monitor`); the worker row of the same `ref` has the line | outcomes |
| OBSPKT1-b3 | a local KISS client row keeps its line except a position (data 13.5) | the client sends a message, a position and a frame with a source that is not in `allow_src` through `apps` | edge tx rows with NO row-level stage and NO `ref`: the message line in full with outcome stage `client` result `queued`; the position row ends at the colon (empty information field) with `redacted: "position_policy"` and its coordinates are in no row; the refused frame is a BODY-FREE tx row (no line, no header, no `ref`; kiss_server.rs `monitor_refused`, the line is built only after the admission checks) with stage `client` result `src` and no `sched` outcome, and its payload marker is in no row | |
| OBSPKT1-c1 | a hidden station's position is blank in every row (data 13.4 / 13.5) | position from `N0HID-7` and a visible control from `N0OBD-1` | `N0HID-7` worker row: empty information field, `redacted: "position_policy"`; its coordinates and comment marker appear in NO row; the visible row shows its coordinates, no `redacted` | the two lines |
| OBSPKT1-c2 | Mic-E destinations (data 13.5) | Mic-E packets from the hidden `N0HID-9` and the visible `N0OBE-1` | the hidden worker row is `SRC>(mic-e),PATH:` with an empty information field, `redacted`; every edge row of both shows `(mic-e)` in the header and not the real destination; the visible station's worker line shows the REAL destination (a worker line shows `(mic-e)` only when redacted) | |
| OBSPKT1-c3 | edge rows of worker-decided packets carry no line (data 13.5) | all rows | no edge row has a `line` unless it has a `client` outcome; edge rows do carry headers | |
| OBSPKT1-c4 | retained rows are re-checked against the CURRENT policy (data 13.5) | a visible position from the web account `N0OBH` is retained; the operator sets `hide_position` | after the policy push the retained row is blank and `redacted`, and its coordinates are in no row | the row before and after |
| OBSPKT1-e | no packet body anywhere else (data 13.5; openapi) | markers of packets that never become room items (RF status, NOGATE status, the client message) | each appears in the monitor (control) and in none of: the public state, `/api/v1/admin/{status,clients,events,lastheard}`, the hub metrics (in the case); the SUT logs are collected on the host after pytest ends, so the case writes `out/<run>/logscan/OBSPKT1-e.json` and SKIPS after the other checks pass, and the host runs `python3 -m dvtest.logscan out/<run>` (exit 0 = clean) | the scanned sources and sizes |
| OBSPKT1-f | admin without a fresh step-up: 403 `step_up_required` (admin.md) | a second admin login made at the start of the module; the case waits until its window (a login opens one) has closed, 5 minutes plus 15 s | 403 `step_up_required` on the snapshot, 403 on the stream; after `step_up()` the snapshot is 200 | the wait |
| OBSPKT1-d | a process restart clears the rings (data 13.5: "a new process boot starts empty"); worker id stays, edge id changes | with the stream open: kill `dv-data-worker` (allowed kill-proc); then restart `dv-edge-ax25` through the admin API (as DP15-6g) | worker: same id, new boot, no worker row with a marker survives, edge rows and edge boots unchanged; AX.25 edge: a NEW id (`ax25-<edge_inc>`), no row of the old id survives; the stream carried `gap` {id} for the worker and for the OLD AX.25 id, none for the new id, and the new id was not in `hello`; the old id's gap comes when the producer list (the hub process table, lagging about 10 s) stops naming it (`retire`), so the case keeps the stream open up to 40 s for it | the boots, the stream events, a timeline of hello / gap / bye frames with seconds |

## GAPs and limits

| GAP | Why | Covered by |
|-----|-----|------------|
| A whole-container restart | NOT REQUIRED (DV-Router): the rings live in the child processes, so the worker kill and the AX.25 child restart of OBSPKT1-d prove the lifetime | - |
| The APRS-IS edge ring after a restart of `dv-edge-aprsis` | the child is not restarted by the case | OBSPKT1-d covers the AX.25 edge and the worker |
| Ring bounds (256 rows, 128 KiB, 30 minutes, eviction, `dv_*_monitor_evicted_total`) | needs hundreds of packets or half an hour of waiting | DV-Router's `dv_lib_pkt::monitor` tests |
| The stream limits (four per service, one per session, 30 minutes, `resync` after 512 rows, `bye`) | needs several sessions and long runs | DV-Router's admin tests |
| Cursor loss and 503 `unavailable` during a boot change in a pagination | a race in the SUT | DV-Router's admin tests |
| Audit rows `packets.snapshot` / `packets.open` (user, port filter, result, never rows) | the audit route is outside this case | DV-Router's admin tests |
| Raw TNC2 modem adapters give no header on receive rows | the rig has no raw TNC2 port | DV-Router's tests |
| Retired port rings after a configuration reload | needs a config change | DV-Router's tests |

## Confirmed shapes (DV-Router, checked against the code at b9a6839e; the unit tests pin them)

* **Producer ids.** An edge id is `<name>-<edge_inc as 16 lower case hex digits>` (`ax25-0000000000000007`, `aprsis-...`); `edge_inc` is new per process start, so an edge restart CHANGES the id.
  The worker id is the configured `[[data.worker]]` id, default `pkt`, without a prefix. Every row carries its producer as `id`; `boot` is a JSON array of 16 integers.
* **`ref`.** For worker-rendered TX the edge start and outcome rows and the worker TX row share `ref`; for RX both rows use the raw unit seq. Exceptions: KISS client TX rows have no `ref`; worker refusal
  rows use the item ordinal; late rows add `of`.
* **`header`** is `SRC>DST,DIGI,DIGI*` with no colon, and `line` = header + ":" + info. Mic-E edge headers always show `SRC>(mic-e),PATH`; a worker line shows `(mic-e)` only when redacted.
* **Port name** of APRS-IS rows: `is`.
* **KISS rows.** There is no row-level stage. A client TX row has `dir` `tx` and `out[].stage` `client` with result `queued`; refusal results are tx_off, tx_addr, channel, bad_frame, not_ui, src, path,
  too_long, third_party, rate, command; delivery to clients is `dir` `rx`, stage `client`, result `sent` or `slow`. All stages: modem, raw, parse, rf, route, item, render, txcore, sched, write, client.
* **Errors** (problem+json): 403 `forbidden` for non-admin, 403 `step_up_required`, 401 `unauthorized` without a session, 400 `bad_request`, 503 `unavailable` with `Retry-After: 1` only when a boot changes during
  the snapshot's own pagination.
* **Query.** `limit` is clamped to 1000 (default 100, 0 becomes 1; non-numeric or negative is 400); `port` up to 64 characters of `[A-Za-z0-9_.-]`; `dir` rx, tx, both or empty.
* **SSE.** First line `retry: 3000`; events `hello` {producers:[{id,boot}]}, `rows` {id, boot, rows:[...]} (the inner rows carry no id / boot), `gap` {id}, `resync` {reason} (then the stream closes), `bye`
  {reason: "session ended" | "stream lifetime" | "replaced"}; the heartbeat is the comment `: hb`.
* **Restarts.** Worker: the id stays, the boot is new; `gap` {id}, then rows with the new boot. Edge: a `gap` for the OLD id; the new id then appears in rows without a gap and was not in `hello`.
* **Row fields.** Always `seq`, `t_ms`, `port`, `dir`, `out[{stage,result,route?,class?}]`; optional `ref`, `of`, `line`, `header`, `redacted`, `more`. The only redaction marker is `redacted` = `position_policy`;
  a redacted line ends at ":" with an empty information field. Snapshot: `{"rows":[row + id, boot], "ports":[{id,port,first_seq,last_seq}], "producers":[{id,boot}]}`, newest first.

* **Route results** are exactly the `dv_pkt_path_total` result set (`router/types.rs:20`): forwarded, seen, hops, rate, filtered, no_gate, not_heard, unreachable, dedupe, overflow, unknown_origin. The cases check
  every `route` outcome against it; a gated packet is `forwarded`, a NOGATE one `no_gate`, a general query `filtered`.
* **APRS-IS TX comes in two rows.** The start row has `dir` `tx`, port `is`, `ref` and `header`, and the outcome stage `render` result `received`. A later body-free row with `of` = the start row's monitor `seq`
  and the same `ref` carries stage `write` with a result of the fixed set (`monitor/codes.rs:265-277`): framed, bad_line, banner_garbage, stale, regression, gap, unsupported, unverified, expired, no_gate, overflow,
  disconnected. For a gated packet on a verified login the expected result is `framed`: DV-Router read that from the constants, not from a run, so another value inside the set is recorded as a FINDING (not a
  failure) and a value outside the set fails the case.
