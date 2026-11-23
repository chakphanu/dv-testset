<!-- SPDX-License-Identifier: MIT -->
# SMALL5 and RFPOS1 step B: the image `dv-router:wip-c3b24d2` (DV-Router `5333e4fc`)

Status: written offline, **NOT run on a rig**. Sources read at `d09e388f` (SMALL5) and `47dac7b4` (RFPOS1 step B), both in `5333e4fc`. Two switches, both passed to the tester by `rig/compose.yml`
and documented in `dvtest/target.py`; every new assertion is behind one of them, so an older image still runs the same cases and only records.

| Switch | Meaning | Unset |
|--------|---------|-------|
| `DVT_SMALL5=on` | the image has SMALL5 (`d09e388f`) | RIF5-M1 keeps its FINDING; the reload replies may omit `truncated` |
| `DVT_RFPOS1B=on` | the image has RFPOS1 step B (`47dac7b4`) | `cases/test_rfpos1b.py` records the counters and writes FINDING lines; nothing fails |

## SMALL5

### 1. Radio TX frames of every mode (RIF5-M1)

`dv_edge_radio_frames_total{port,dir=tx,result=ok}` counts each native MMDVM frame accepted by the byte link (status.md "Built (TXFRAMES1)"): D-STAR header / data / end, DMR slot-2 burst (repeated
copies and silence fill too), YSF frame, and now also NXDN frame and P25 header / LDU (`crates/lib/radioport/src/mmdvm/port.rs`). Before it the counter left out NXDN and P25: the rig run of
`wip-f1f3777` recorded `+1073 = D-STAR + DMR + YSF, excludes NXDN 55 and P25 24` (finding RIF5-M1).

`test_m1_counters_account_for_the_overs` (`cases/test_rif5_mmdvm_serial.py`): under `DVT_SMALL5=on` it asserts that the growth of `frames_total{dir=tx,result=ok}` since the counters' baseline equals the
sum over the five modes of the native frames the fake modem logged in the same time (`rif5.native_by_mode`, `rif5.small5_problems`; the five modes are `ms.TX_TYPES`), and records the frames by mode.
A growth short by exactly the NXDN and P25 frames is named as the image before SMALL5. Unset: the old FINDING line, nothing asserted.

The case is a tally of the whole run (the baseline is taken at the first over, the counters are read after the last), so it needs the TX overs of all five modes before it:

```sh
R=rig/config/dv-router
B=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml
XLXD_SUT_IMAGE=dv-router:wip-c3b24d2 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> DVT_SUT_GEN=compat1 \
DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true DVT_SMALL5=on \
DVT_RIF5=on DVT_RIF5J=on DVT_RIF5N=on DVT_RIF5FW=on DVT_P25_MODULE=T DVT_P25_TG=31681 DVT_SUT_NAME=dv-router \
DVT_SUT_CONFIG_EXTRA=$B:$R/rif5n-modes.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_rif5_mmdvm_serial.py -rA -k "test_tx_room_to_modem or test_n_tx or test_m1_counters"
```

(`test_tx_room_to_modem` is D-STAR, DMR and YSF, `test_n_tx_room_to_modem` and `test_n_tx_dmr_talker_into_the_nxdn_room` are NXDN and P25; about 6 minutes, estimate. Without `rif5-two-ports.toml`: port B's TX
would add frames the tallies of port A do not know, `docs/radioseq1-cases.md`.) Notes: `out/<run>/rif5.txt`, line `[RECORD] RIF5-M1: SMALL5: native frames by mode since the baseline ...`.
The whole file in one run (as rif5n-A) also holds the check.

### 2. Admin RF ports card, column labels

The System page's RF ports card (`web/admin/admin.js` `RfPorts`) has one column "Transmit mode" before and two after: "Configured transmit mode" (AX.25 rows; the other cell is `-`) and "Effective
transmit mode" (radio rows). Strings (`web/shared/i18n/admin-en.js`, `admin-th.js`):

| Key | EN | TH |
|-----|----|----|
| "Configured transmit mode" | Configured transmit mode | `\u0e42\u0e2b\u0e21\u0e14\u0e01\u0e32\u0e23\u0e2a\u0e48\u0e07\u0e15\u0e32\u0e21\u0e01\u0e32\u0e23\u0e15\u0e31\u0e49\u0e07\u0e04\u0e48\u0e32` |
| "Effective transmit mode" | Effective transmit mode | `\u0e42\u0e2b\u0e21\u0e14\u0e01\u0e32\u0e23\u0e2a\u0e48\u0e07\u0e17\u0e35\u0e48\u0e21\u0e35\u0e1c\u0e25` |
| "Transmit mode" (the old single column) | Transmit mode | `\u0e42\u0e2b\u0e21\u0e14\u0e01\u0e32\u0e23\u0e2a\u0e48\u0e07` |

No browser or web case of this tree reads the card (searched `cases/`, `dvtest/`, `cases/web_pages.py` for the headers, "RF ports", "Transmit mode", "Simulate", the Thai strings): `web_pages.OPS_HEADINGS`
covers the ops pages only, not the admin SPA. Nothing to change, nothing to run for it; a case that reads the card later has to accept both generations (4 columns before, 5 after).

### 3. Reload schemas carry `truncated`

`openapi-admin.yaml` `ReloadPreview` and `ReloadApplied` now list `truncated` (a `Truncation` object, list name -> `{dropped?, cut?}` with integers of at least 1) and REQUIRE it (the admin
`app_reload.rs` always emits an object, `{}` when nothing was cut). Our check (`dvtest/secret_reload.py` `preview_problems` / `applied_problems`, used by `cases/test_secret_reload.py` R0 to R3) already
accepted the key as optional (BUD1b) and validated its shape, so no case breaks. New: `require_truncated`, passed as `sr.small5_on()` by the case: under `DVT_SMALL5=on` a reply without `truncated` is a
problem; unset, it stays optional. `cases/test_bud1.py` reads `truncated` of the validate / preview replies as before.

## RFPOS1 step B: an end-hint unit never widens the egress width

The edge core (`crates/lib/edge/src/core.rs` `x_unit_p`) renders each Unit of `n` units at positions `media_seq * width + i`, `width` being the largest `n` seen for the stream and profile. A YSF voice frame with
the end bit is one Unit of 5 units plus the end unit (6 units, media-format.md "End unit"); the 6 widened the width from 5 to 6 and moved the last frame, which showed as a false end-of-over gap on every
egress edge (`dv_edge_over_units_total{dir=tx,result=lost}`, the over folded `lossy`). Since `47dac7b4` the end-hint unit keeps the stream's width. "No change for D-STAR link streams."

The YSF proto cases (`cases/proto/test_ysf.py`) talk YSF -> YSF and DMR -> YSF but compare ref against sut and read no counters (the xlxd reference has none), so they cannot see it. The counters are the
hub's; the new case file reads them:

| Case (`cases/test_rfpos1b.py`) | Over | Egress edges judged |
|------|------|------|
| `RFPOS1B-ysf` | a YSF talker on module Y, 65 frames (13 packets), a YSF listener on Y | `ysf` |
| `RFPOS1B-dmr` | the same, plus a DMR listener on Y | `ysf`, `dmrhb` (a fold is required) |
| `RFPOS1B-dstar` | the same, plus a DExtra listener on Y | `ysf`, `dextra` (a fold is required) |

Asserted under `DVT_RFPOS1B=on` (verdict `dvtest/rfpos1b.py` `judge`; unit tests `cases/unit/test_rfpos1b_lib.py`): the listeners heard the over (harness check; the YSF egress structure: header first, terminator
last with the end bit only on it); the room closed one over (`dv_room_overs_total{room=y}`), `dv_edge_over_units_total{dir=rx,proc=ysf,result=gap}` 0 and `dv_room_over_units_total{room=y,result=gap|lost}` 0 (YSF
counts no source gap), the over not `lossy` or `end_missing`; on every egress edge `dv_edge_over_units_total{dir=tx,proc=...,result=lost}` 0 and no `dv_edge_overs_total{dir=tx,result=lossy|end_missing}`; the cross
edge folded the over. `late` and `dropped` are recorded as FINDING lines (a CTS stall or a starved host, not what the fix changes). Unset: the same over, the same reads, every problem a
`FINDING ... not asserted (DVT_RFPOS1B is off)` line. The counters are the hub's (a record per edge process every 10 s): a baseline that did not move for 12 s, one over, wait for the room close and the cross
fold, one more read 12 s later (about 40 s a case). The case skips without the MEDLOAD1 series (an image before `wip-f1f3777`) and fails when an expected `proc` has no series (the message lists the procs seen).
Notes: `out/<run>/rfpos1b.txt`.

### Run it (the YSF proto cases and the new check, one stage)

```sh
XLXD_SUT_IMAGE=dv-router:wip-c3b24d2 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> DVT_SUT_GEN=compat1 \
DVT_TRANSCODER_FAKE=1 DVT_RIG_CONTROL=1 DVT_RFPOS1B=on \
  scripts/run.sh -m rig --capture --profile core -- cases/proto/test_ysf.py cases/test_rfpos1b.py -rA
```

No `DVT_SUT_CONFIG_EXTRA` is needed (the YSF, DMR and DExtra edges are in the template, as for `cases/test_medload1.py`); the new check alone: `... -- cases/test_rfpos1b.py -rA` (about 3 minutes, estimate).
`--profile core` as the M17 proto stage of the chain; the YSF proto cases need no other service. Evidence for the before / after pair: the same command without `DVT_RFPOS1B` on the previous image
(`wip-37b7960` or `wip-f1f3777`) records the false gap as FINDING lines.

Not covered: the link-stream side of RFPOS1 (`crates/edge/link`, `RTS_FRAME_POSITIONS` in the RTS of an `ambe-dstar` source, `link.md` "sequence is the sender's 20 ms position"), which needs a two-node rig.
