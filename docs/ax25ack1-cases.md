<!-- SPDX-License-Identifier: MIT -->
# AX25ACK1 and TXOFF2 cases: the AX.25 tx_ack and the transmit-off paths

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`, units AX25ACK1 and TXOFF2 = `345f1ebe`). **Nothing here has run on a rig.** Case file `cases/test_ax25ack1.py`, pure
helpers `dvtest/ax25ack1.py`, unit tests `cases/unit/test_ax25ack1_lib.py` and the renderer tests in `cases/unit/test_sut_config.py`, switch `DVT_AX25ACK1` (passed to the tester by `rig/compose.yml`,
guarded by `cases/unit/test_tester_env.py`). Notes: stdout and `out/<run>/ax25ack1.txt` (`RECORD`). The config part (the renderer adds the line, the cfgload expectations) is in `docs/rig.md` "AX.25 tx_ack".

**Gate.** `DVT_AX25ACK1=on`, which also makes the renderer write `tx_ack`; without it every case skips with the reason. The cases need the DP15 RF shape with TX on (`DVT_DP15_TX_ENABLE=true`,
`dp15-rf.toml` + `dp15-rf-out.toml`, fake KISS TNCs), `--profile web`, and `DVT_SUT_CONFIG_RW=1` (they edit the config through the admin API).

## What AX25ACK1 and TXOFF2 are (read from the commit, `config.md`, `radio.md` 10.1, `openapi-admin.yaml`)

* `[edges.ax25] tx_ack` (one key on the edge) must equal `I am the control operator of <CALLSIGN> and accept responsibility for its transmissions`, callsign upper case with SSID. Absent or empty: every
  port is RX only, `config check` WARNS `no control-operator acknowledgement: RX only`, the start line reads `RF TX off (no control-operator acknowledgement: RX only)`. A nonempty different sentence is an
  ERROR `RF TX acknowledgement must equal: <sentence>`, even with TX off. The AX.25 process row of the admin status carries `ax25: {callsign, tx_enable, acknowledged}`.
* TXOFF2: one effective-transmit predicate (`tx_enable` and `tx_ack` and the port's `tx`) for every RF producer. A digipeater, a beacon, KISS client TX, an RF data path and an APRS-IS RF copy no longer
  need TX intent to LOAD; off keeps them dormant with a warning (`port TX off: <list> send nothing (RX only)`). Start lines name the reason: `off (master off)`, `off (port off)`, `off (no control-operator
  acknowledgement: RX only)`, `off (edge disabled)`.
* `POST /api/v1/admin/radio/tx` `{kind: "ax25"|"radio", port?: 0..15, on, ack?}`: Off needs operator or admin without step-up; On needs admin, step-up and the exact `ack`; no client ETag; the file is edited
  under the config writer lock; the reply is a config result with `rebased` and `history_seq`. Off alone REBASES on the hash-verified accepted running source when the current file or the candidate is
  invalid (the replaced file stays in history); a missing port is the master gate. The edge restarts (a restart-class change).

## What is asserted

| Case | Statement | Assertion |
|------|-----------|-----------|
| AX25ACK1-a | the exact sentence transmits | the rendered config has `tx_ack` equal to the sentence of the callsign; the status row says `acknowledged` and `tx_enable` true, the port label of rf-a is `on`; one room text is transmitted at rf-a |
| AX25ACK1-b | an absent sentence is dormant | `replace /edges/ax25/tx_ack` with `""` through `POST /config/changes`: HTTP 200 (a warning, not an error); the edge restarts; `acknowledged` false (the port label of the AX.25 modem session is the configured intent, so it stays `on`: rig run 20261122, `view.rs` passes the raw row, only the radio edge shows the effective label); a room text is accepted (202) and NOTHING reaches either TNC for 20 s; the sentence put back (restore in a `finally`) transmits again |
| AX25ACK1-c | a wrong sentence is an error | the change of `tx_ack` to the sentence of another callsign answers >= 400 with `RF TX acknowledgement must equal`; the file etag does not move; the edge does not restart |
| TXOFF2-a | a per-port Off and On | operator, no step-up, `{kind: ax25, port: <rf-a>, on: false}`: 200 with `rebased` false and an integer `history_seq`, the WHOLE `dv-edge-ax25` restarts (the AX.25 section is one struct for the reload), the label of rf-a is `off`, nothing transmitted; a repeated Off is 200 `noop: true` with `changed: []` and no restart; On as an operator is 403; On by an admin with step-up but without `ack` is exactly 422; On with the sentence is 200 and the text is transmitted again |
| TXOFF2-b | the master Off and On | the same without `port`: `ax25.tx_enable` false after the restart, nothing transmitted; the master On with the sentence transmits again |
| TXOFF2-c | an Off on an INVALID file rebases | `dvt_not_a_key = 1` appended to the rw config file by the tester, then the master Off: 200, `rebased` true, `history_seq` >= 1, the file parses and has `tx_enable` false and no bad key, the history entry holds the invalid text, nothing transmitted; the master On restores the run (in a `finally`) |

The cfgload side (not rig, about 65 s, `docs/rig.md` "AX.25 tx_ack"): the four short files with `DVT_AX25ACK1=on`:

```
DVT_AX25ACK1=on DVT_CFGLOAD_IMAGE=dv-router:wip-f1f3777 DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \
scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_digi1_config.py cases/test_dp18_config.py cases/test_dp11e_config.py cases/test_dp6c_config.py -q
```

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_AX25ACK1=on DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=true DVT_SUT_CONFIG_RW=1 \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf.toml:rig/config/dv-router/dp15-rf-out.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_ax25ack1.py
```

Estimated time (not measured): about 15 minutes (every change restarts the AX.25 edge and waits out its 13 s TX guard; each transmit waits 31 s of `tx_per_min = 2` spacing; each silence check is 20 s; the
cases restore the run in a `finally`). One rig, nothing else at the same time. Do not combine with `cases/test_dp18_rf.py` (it also changes the AX.25 config).

## GAPs

* **The start line** (`RF TX off (<reason>)`) is only in the SUT log, which `run.sh` collects after pytest: `dvtest.ax25ack1.start_lines()` parses it, but no host step reads it yet; the cases judge the
  status labels instead. The host step would be `python3 -c` over `out/<run>/logs/dvt-xlxd-sut.log` with `start_line_problems`.
* **`[edges.radio]` TX Off/On** (`kind: radio`) needs the MMDVM serial shape (`rif5-mmdvm-udp.toml`); not covered here (the body builder takes `kind="radio"`).
* **The warning text of `config check`** was checked by hand on the image (every AX.25 snippet loads clean with the line and warns without it, the `config check` of `wip-f1f3777`): the admin reply shape of warnings is not specified, so
  AX25ACK1-b records the diagnostics and asserts only HTTP 200.
* **KISS client TX** with a port Off (`KISS client TX send nothing`) and a digipeater / beacon / RF copy dormant on an Off port: loaded in cfgload (`test_dp11e_config`, `test_digi1_config`,
  `test_dp18_config` under the switch), their silence on the wire is not asserted.
* **A dormant path to RF with the master off** (`dp15-rf-out.toml` with `DVT_DP15_TX_ENABLE=false` now loads) is not a case yet; `cases/test_dp15_rf.py` still skips 6b when the path exists.

## Questions for the DV-Router team

Answered (answer-342, `app.rs` / `app_radio_tx.rs`), now asserted: `rebased` and `history_seq` are top level (`history_seq` is there even when `rebased` is false); On without `ack` is 422, On by an
operator is 403; a per-port Off restarts the whole `dv-edge-ax25`; a repeated Off is 200 with `noop: true`. Still unverified: the exact text of the 422 body (one rig run settles it; the case
records it).
