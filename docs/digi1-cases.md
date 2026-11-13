<!-- SPDX-License-Identifier: MIT -->
# DIGI1 cases: the fill-in digipeater of the AX.25 edge (DIGI-R1 to R5)

Status: written offline for DV-Router image `dv-router:wip-eeb44b4` (code `a8692074`; DIGI1 S1 `22994519` + S2 `588ffb4c`). **Nothing here has run on a rig**: the rig was busy with
another test chain, and no docker, no rig and no full unit suite was run (only the targeted unit tests listed at the end). Expect first-run corrections.

What is tested: `dv-edge-ax25` can repeat a modem-received UI frame on the receiving port, once, with its own call replacing the next hop `WIDE1-1` (or its own call) and the H bit set
(`docs/design/data.md` 8.3 "AX.25 fill-in digipeater (DIGI1)", 10.8, `config.md` `digi`). It is OFF by default, needs effective TX, `tx_per_min >= 10` and `tx_burst >= 3`, and passes
every TX gate of the port (startup guard, fence, class budget, TTL, minimum gap). The requests are `data.md` 15.5 "DIGI1 rig acceptance requests" and `design-271-digi1.md` 5.7; the
numbering DIGI-R1 to R5 is theirs.

## Gate and files

**Switch: `DVT_DIGI1=on`** (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`, documented in `dvtest/target.py`). Without it every rig case of
`cases/test_digi1.py` and `cases/test_digi1_dw.py` skips naming the switch. The configuration refusals (`cases/test_digi1_config.py`, `-m cfgload`) are host side and need an image with
DIGI1 but not the switch (on an image without it the baseline row fails with an unknown key, which is the answer).

| Path | What |
|------|------|
| `cases/test_digi1.py` | DIGI-R1 (24 path vectors, same port only), R2 (timeline, key rules), R3 (flood), R4 (two fill-ins), R5 (startup guard), the counters and conservation; fake KISS TNCs (`dvtest/kiss_tnc.py`) |
| `cases/test_digi1_dw.py` | DIGI-R1b: six vectors through the REAL Dire Wolf TNCs of profile `dw` (real KISS bytes, real modulation) |
| `cases/test_digi1_config.py` | `-m cfgload`: 20 `config check` cases for the validator rules (table below) |
| `rig/config/dv-router/digi1.toml` | the **rate floor** shape: `tx_per_min 10`, `tx_burst 3`, `tx_budget {}`, `digi = { enable = true }`, ports `rf-a` (digipeating) and `rf-b` (rx only), the room path `rf-out` for own frames |
| `rig/config/dv-router/digi1-roomy.toml` | the **roomy** shape for the functional cases: `tx_budget = { duty_pct = 20 }`, `tx_per_min 30`, `tx_burst 10` (digi class 40 s instead of 10 s), no room path |
| `rig/config/dv-router/digi1-dw.toml` | roomy, but `rf-a` / `rf-b` point at the Dire Wolf TNCs `dw-a` .48 / `dw-b` .49 (profile `dw`) |
| `dvtest/ax25.py` | AX.25 frame building with an explicit H bit and reserved bits per hop (`build_ui`), `hops`, `path_text`, `first_unused_hop`, `replace_hop`, `diff_offsets` |
| `dvtest/digi1.py` | the model of data.md 8.3 (`decide`, `DedupeTable`), the vectors G1 to G13 + X1 to X11, key vectors, timeline, flood plan and bounds, counters and conservation, `read_shape` |
| `dvtest/digi1_oracle.py`, `rig/direwolf-entry.py` (job `digi`), `dvtest/oracle_direwolf.py digi` | Dire Wolf as the oracle (offline job; see "The oracle") |
| `dvtest/digi1cfg.py` | the configuration mutations and the judge of a `config check` verdict |
| `cases/unit/test_ax25_digi.py`, `test_digi1_lib.py`, `test_digi1_oracle.py`, `test_digi1_cfg.py`, `test_digi1_cases.py` | unit tests (round trips, DV-Router's hand-built goldens, model, flood bounds, oracle, cfg, the case logic against a model edge) |

Notes go to stdout and `out/<run>/digi1.txt` / `digi1-dw.txt` (`RECORD`, `FINDING`).

## Why two shapes (and what a run needs)

The digi class has its own airtime bucket: `duty_pct / 100 * window_secs * digi share / sum of shares` = 5 % of 600 s = 30 s, times 4/12 = **10 s** at the class defaults, about 11 copies
of an APRS frame (`data.md` 8.3: "digi took 15 copies"). The functional cases send more than that in one run (8 repeating path vectors, 5 key repeats, the timeline, R4, R5), and a
copy over the budget waits and expires (`dropped_ttl`), which is correct and is exactly what R3 measures, but would only make a vector look wrong. So:

* **roomy** (`digi1-roomy.toml`): R1, R2, R4, counters, R5. R3 skips naming the shape.
* **floor** (`digi1.toml`): R3, counters, R5. R1, R2 and R4 skip naming the shape.

Each case reads the RENDERED config of the run (`out/<run>/xlxd-config/xlxd-sut/dv-router.toml`, `dvtest.digi1.read_shape`) and skips with the reason when its shape is not the one rendered.
The station call is `N0TEST-10` (the documents say `N0CALL-10`); the vectors use `N0TEST-n` names. `dvtest/ax25.py` + `digi1.py` reproduce DV-Router's hand-built G1 to G13 / G2b bytes
exactly when the names are turned back to `N0CALL-n` (`cases/unit/test_ax25_digi.py` holds the DV-Router fixtures as hex and checks builder and model against them).

## What each case asserts

| Case | Assertions | Document |
|------|-----------|----------|
| DIGI-R1 `[G1..G13, X1..X11]` (one test each) | the model decides each vector as the table row says (self check); a repeating vector: exactly ONE frame at the TNC, equal byte for byte to the expected one (our call in the one hop, H set, reserved bits set, extension bit kept, every other byte unchanged, same length), `repeat_problems` empty; a refusing vector: no frame within 3 s. With the oracle vectors present: `oracle_problems` empty and, where the document says Dire Wolf agrees, the same repeat/silence and the same path | `data.md` 8.3 steps 1 to 10 and G1 to G13; `design-271-digi1.md` 1.3, 5.7 (DIGI-R1) |
| DIGI-R1c same port only | a frame heard at `rf-b` (no digi) is written nowhere; the edge wrote no data frame at all to `rf-b` since the start; `dv_edge_digi_total` has no series for `rf-b` | `data.md` 8.3 "receiving port", status.md (only rows with `digi.enable` emit) |
| DIGI-R2 timeline | one frame sent at 0, 10, 29, 31, 45 s: repeat, duplicate, duplicate, repeat, duplicate (window from FIRST acceptance, duplicates do not refresh it); equals Dire Wolf's timeline when the vectors exist | `data.md` 8.3 duplicate paragraph, 8.4; `design-271` 1.4, 5.7 (DIGI-R2) |
| DIGI-R2 key rules (K0 to K7) | path and H bits ignored (duplicate); trailing space/CR/LF ignored (duplicate); source SSID, destination SSID, one information byte and a leading space are part of the key (repeat); the reference again is a duplicate | `data.md` 8.3, 8.4 |
| DIGI-R3 flood (floor) | 60 eligible frames in 60 s from 10 sources: every copy is the repeat of one injected frame (hop replaced, nothing else), none twice; no copy later than TTL 5 s (+1 s timing); copies at least 1 s apart (minimum gap); airtime of all copies within the digi class bucket (10 s, refilling) + one frame, within the port (30 s) and within each source's 25 %; `dv_edge_digi_total{repeat}` = 60 and nothing else moved; conservation; queue empty afterwards; **own message frame** (a web text posted 5 s into the flood) arrives at the TNC from the station call within its class TTL (120 s; a wait above 90 s is recorded as a FINDING) and `dv_edge_rf_tx_total{kind in ack,message,position,beacon,result in dropped_ttl,dropped_queue,dropped_exceeds_quota,dropped_too_long}` did not move | `data.md` 8.3 last paragraphs (flood, `tx_per_min >= 10`, "own frames left after 72 s, 84 s and 90 s, none expired"), 10.8 rules 1 to 5; `design-271` 5.7 (DIGI-R3) |
| DIGI-R4 two fill-ins | order A-B (`WIDE1-1,WIDE1-1`): our copy is `N0TEST-10*,WIDE1-1`; the tester (the second fill-in) repeats it (`N0TEST-10*,N0TEST-11*`) and the edge hears it: no repeat; the edge hears its own copy: no repeat (own path); the original again: duplicate. Order B-A: `N0TEST-11*,WIDE1-1` is repeated by us as `N0TEST-11*,N0TEST-10*`; our copy heard again: no repeat. Paths never grow, no key repeated twice | `data.md` 8.3 "A frame we repeated cannot loop back..."; `design-271` 3.3, 5.7 (DIGI-R4) |
| DIGI-R5 startup guard | `POST /api/v1/admin/children/dv-edge-ax25/restart`; after the edge reconnected two eligible frames are heard inside the guard (sent only after the status view shows the fence, read again after them; a late attempt is repeated once with new keys, else skip); NO data frame at the TNC during the guard or in the 2 s after; the retry of the guarded frame after the guard IS repeated, byte exact (a fenced frame is not remembered); the same frame again is a duplicate; new-process counters fenced 2, repeat 1, duplicate 1; `dv_edge_drops_total{rf_fenced}` = digi `fenced`; conservation | `data.md` 8.3 ("TX off, a startup guard or any fence never permits repeating, and fenced frames are not remembered"), 10.8; `radio.md` 10.4; `design-271` 5.7 (DIGI-R5) |
| counters and conservation | `dv_edge_digi_total{port=rf-a,result}` grew by exactly what the model decided for every injected frame (nine fixed results; none outside); `dv_edge_rf_tx_total{kind=digi,result=sent}` = copies seen at the TNC; `dv_edge_modem_total{result=tx}` = frames written; kinds present are a subset of the six fixed ones and include `digi`; conservation `digi{repeat}` = refusals before the scheduler + terminal `rf_tx{kind=digi}` results + canceled + queued; no fence refusals in a run without a restart | `data.md` 10.8 ("For each digipeating port in one edge lifetime"), 13.1; `status.md` edge AX.25 fill-in |
| DIGI-R1b `[G1, G2, G7, G12, G4, G10]` (profile `dw`) | the tester's frame sent into `dw-b` comes out of `dw-a`'s KISS port unchanged (the loop is intact); one repeat comes out of `dw-b`'s KISS port, equal to the model's, or nothing for 12 s for G4 and G10; latency recorded | as R1; `docs/direwolf-cases.md` "Modem loop" |

Counters are published by the edge about every 10 s: the cases poll (up to 75 s) instead of reading once, and take a baseline only after the counters stopped moving for 12 s.

### Configuration refusals (`-m cfgload`, `cases/test_digi1_config.py`)

One mutation of `digi1-roomy.toml` per case, rendered for gen compat1 with `data-ident.toml`, checked by `dv-router config check` in a throw-away container (`dvtest.tools.config_check`). Reject
rows need exit 2, a positioned error under `edges.ax25` that names the key and contains DV-Router's operator sentence (`crates/lib/config/src/ax25_digi.rs`, `config.md`); accept rows exit 0.

| Id | Mutation | Key / sentence (or accept) |
|----|----------|----------------------------|
| `accept-baseline` | the roomy snippet as shipped | accept |
| `digi-port-tx-off` | `tx = false` on the digipeating port | `digi.enable`: digipeating requires effective RF TX on this port |
| `digi-master-tx-off` | `[edges.ax25] tx_enable = false` | the same sentence |
| `accept-digi-simulate` | `tx = "simulate"` | accept (simulate is effective TX) |
| `digi-per-min-9`, `digi-per-min-2` (2 / 1, the old defaults), `digi-burst-2` | below the floor | `digi.enable`: digipeating requires tx_per_min >= 10 and tx_burst >= 3 on this port |
| `accept-rate-floor`, `accept-digi-off-low-rate` | exactly 10 / 3; digi off at 2 / 1 | accept |
| `digi-dedupe-9`, `digi-dedupe-601` | window out of range | `digi.dedupe_secs`: must be 10-600 seconds |
| `digi-dedupe-equals-ttl` | `dedupe_secs 10`, `ttl_secs 10` | `digi.dedupe_secs`: must be greater than digi.ttl_secs |
| `digi-ttl-0`, `digi-ttl-11` | TTL out of range | `digi.ttl_secs`: the digi TTL must be 1-10 seconds |
| `accept-dedupe-10`, `accept-dedupe-600`, `accept-ttl-10` | the boundaries | accept |
| `digi-share-0`, `digi-share-while-off` | share 0 on a digipeating port; share 4 with digi off | `digi.share`: a digipeating port needs a nonzero digi class share / a nonzero digi class share requires digi.enable |
| `digi-shared-physical` | `rf-b` on the same `physical` with digi off | `digi.enable`: ports on the same physical transmitter must all enable or all disable digipeating |

Not covered by cfgload: raw/LoRa ports (`digipeating requires a kiss-tcp, kiss-serial or agwpe port...`; needs a raw-port snippet with caps), `tx_budget.queue` not equal to the shares + digi.

## Run commands

The image is `dv-router:wip-eeb44b4`. Usual native-launch environment: `XLXD_SUT_IMAGE DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template>
DVT_TRANSCODER_FAKE=1` (as `docs/rfsafe1c-cases.md`). Beyond that every rig run needs `DVT_DIGI1=on`, `DVT_REQUIRE_IDENT=true` (the web enrolment of the admin API) and the extra snippets below.
One rig at a time (`docs/rig.md`); all times are ESTIMATES (the rig start and web enrolment take about 3 minutes of each).

```
R=rig/config/dv-router
E=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml
IMG="XLXD_SUT_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1"

# A. roomy: DIGI-R1 (24 vectors + same port), R2, R4, counters, R5 (about 8 minutes: 3 for the rig, about 5 for the cases; the cases alone: R1 about 65 s, R2 about 70 s, R4 about 20 s, R5 about 55 s)
env $IMG DVT_REQUIRE_IDENT=true DVT_DIGI1=on DVT_SUT_CONFIG_EXTRA=$E:$R/digi1-roomy.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_digi1.py

# B. floor: DIGI-R3 (flood + own frame), counters, R5 (about 9 minutes: 3 for the rig, about 6 for the cases; R3 alone about 4: 25 s settle, 60 s flood, up to 125 s for the own frame, counters)
env $IMG DVT_REQUIRE_IDENT=true DVT_DIGI1=on DVT_SUT_CONFIG_EXTRA=$E:$R/digi1.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_digi1.py

# C. tier B, real Dire Wolf TNCs (about 8 minutes: 3.5 for the rig, up to 2 for the TNC probe, about 2.5 for the six vectors)
env $IMG DVT_REQUIRE_IDENT=true DVT_DIGI1=on DVT_SUT_CONFIG_EXTRA=$E:$R/digi1-dw.toml \
  scripts/run.sh -m rig --capture --profile web,dw -- cases/test_digi1_dw.py

# D. configuration refusals (host, docker, no rig; about 15 s, 20 throw-away containers one after the other)
DVT_CFGLOAD_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \
  scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_digi1_config.py

# E. the oracle (host, docker, no rig; about 5 minutes: 257 s of schedule plus start): writes vectors/direwolf/digi_oracle.json
python3 -m dvtest.oracle_direwolf digi
```

The run shapes cannot be combined: A and B both define `[edges.ax25]` (as `dp15-rf.toml` and `dp18-rf.toml` do). Without `--profile web` the admin API is missing and R5 skips; the other cases fall back
to a 16 s wait for the startup guard and run. R5 restarts the edge and must be the last case of its run (it is, by file order). Run A and run B each end with the port unfenced.

## The oracle

Dire Wolf 1.8.1 (`dvt-direwolf:local`; compose services `dw-a` / `dw-b`, profile `dw`; built by `scripts/pre-fetch-direwolf.sh` + `rig/Dockerfile.direwolf`) was used in two ways before: as byte
vectors from a throw-away container (`oracle_direwolf run`, `docs/direwolf-cases.md`) and as the SUT's real TNC on the modem loop (`cases/test_dp15_rf_dw.py`). DIGI1 uses both:

* **Tier B** (`cases/test_digi1_dw.py`, run C): Dire Wolf is the edge's modem. It has no `DIGIPEAT` line, as the edge requires.
* **The digipeater oracle** (job `digi` of `rig/direwolf-entry.py`, `python3 -m dvtest.oracle_direwolf digi`): the modem loop of two Dire Wolf instances in one container, node B with `MYCALL N0TEST-10`,
  `DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$` and `DEDUPE 30` (User Guide 9.6.6). Node A sends the 24 path vectors, the 8 key vectors and the five timeline steps into its KISS port; B demodulates and repeats what
  its rules allow; A demodulates the repeat and hands it to its KISS client. The result is `vectors/direwolf/digi_oracle.json` (per step: the repeats heard, and whether B received the frame at all: a frame B
  never heard proves nothing about its rules). `dvtest/digi1_oracle.py` compares it with the model: `equal`, `equal_masked` (only the command/response bits differ), `differs`, `unusable`. The rig cases
  (R1, R2 timeline) read that file when it is in the repository and assert that the edge agrees with Dire Wolf where the documents say so; the documented deviations (G9 own source, G10 non-contiguous H,
  G11 and X6 to X8 Internet markers; G7 and the other explicit-routing rows agree) are recorded as notes, never asserted.

## GAPs and what is not asserted

1. **The Dire Wolf oracle vectors do not exist yet.** The job is written and its pure parts are unit-tested, but it needs docker and about 5 minutes and was not run (instruction: no docker). Until
   `vectors/direwolf/digi_oracle.json` is generated, reviewed and committed, R1 and R2 judge the edge against the data.md 8.3 table and DV-Router's own hand-built goldens only (which our builder and model reproduce
   byte for byte); the rig notes say `no Dire Wolf vector`. The first run of the job may also show that Dire Wolf's configuration lines or timing need a change (`DEDUPE`, the repeat latency, `b_received`).
2. **G2b** (a station call without SSID: the repeat has SSID 0) cannot run: the station call is fixed per run (`N0TEST-10`). The model and the unit tests cover it. A second snippet with `callsign = "N0TEST"` would do.
3. **The exact 30 s boundary** (the key is new exactly at 30.000 s; Dire Wolf's is inclusive in whole seconds) is not probed: a black box cannot hit a millisecond. The timeline stays 1 s away from it.
4. **DIGI-R4 with two real DV-Router fill-ins** (`--profile twonode`, the tester relaying the air between two fake TNCs) is not written; the second fill-in is the tester (`ax25.replace_hop`), which is exactly a
   fill-in's rewrite. What is shown: our edge consumes one hop, never grows a path, never repeats its own copy or a used path, never repeats one key twice in the window.
5. **DIGI-R3 against Dire Wolf**: the oracle only establishes the OFFERED frames (all 60 are eligible, the model says `repeat` for each, Dire Wolf would repeat them all); DV-Router's quotas are its own policy
   (`data.md` 15.5: "The oracle establishes the offered eligible frames, not DV-Router's quota policy"). No Dire Wolf flood is run. The digi queue cap (4 queued copies) and the per-source bucket are not
   observable from outside separately: they are bounded (`dropped_queue` / quota waits count in the conservation), not read.
6. **DIGI1b** (a separate digi packet bucket) is not built (work-units: required before production). R3 at the validator minimum 10 / 3 measures what the shipped scheduler does; its own-frame assertion (no TTL
   expiry, arrival within 120 s) matches DV-Router's virtual-time measurement ("72 s, 84 s and 90 s, none expired") and is NOT a guarantee. If the own frame expires, that is the DIGI1b finding, reported by R3.
7. **`dedupe_full`** (256 live keys, fail closed) is not exercised: it needs 257 distinct eligible frames inside 30 s (about 0.1 s apart); possible as an extension, the model and counters are ready.
8. **A fence other than the startup guard** (observer session cut, foreign holder) cannot be produced (the same GAP as RF1c-1, `docs/rfsafe1c-cases.md`); R5 covers the startup guard. The "fenced candidate is
   counted once in `digi{fenced}` and once in `rf_fenced`" equality is asserted there.
9. **KISS-server client frames, worker renders and APRS-IS inputs are never candidates** (`data.md` 8.3): no KISS server listener and no APRS-IS leg is in the snippets, so only modem RX is exercised.
10. **`tx = "simulate"`** (every decision and hypothetical release, `result=simulated`, no bytes) is only accepted by cfgload (`accept-digi-simulate`); no rig run with it.
11. **The digi monitor rows** (header only, `digi/<result>` stage on the RX row, OBSPKT1) are not read.
12. **Unverified assumptions of the cases** (a first run settles them): the duration of the first observer probe (R5 retries once if the fence was over before the frames); the scheduler charges
    `afsk_airtime_ns(len)` with TXDELAY 300 ms and TXtail 30 ms (the KISS parameters the edge writes); `config check` prints the sentence and the key `digi.enable` in the same line.

## Answers of the DV-Router team (answer-279, applied)

1. **`dv_edge_rf_rejected_total{result}`** has no port label and is one series per edge for ALL classes (`invalid`, `disabled`, `too_long`, `airtime`, `overflow`, ...); a zero series is not exported (absent = 0).
   The cases count its delta as a term of the digi conservation only when it did not move; when it moved they record it and use it as an upper bound of the digi share. The missing label is product item DIGI1b.
2. **R3 at 10 / 3** (lead decision): pass when the own frame is released within its class TTL (120 s) with no `dropped_*` for it (a drop FAILS the case); the measured wait is recorded; a wait above 90 s
   (DV-Router's virtual-time worst: 72 / 84 / 90 s) is a `FINDING` note, not a failure.
3. **Counter timing**: `dv_edge_digi_total` is incremented at the decision, `rf_tx{kind=digi}` at the scheduler release, both visible in the next 10 s telemetry record (zero series absent until the first count); a restarted
   edge starts from zero (the hub exports each incarnation's raw values). The cases poll up to 75 s; R5 accepts "reset" and "continued" and records which.
4. **Startup guard** = 2.5 + 10 + 0.5 = 13 s from the completion of the first successful observer probe (token declared, fabric up), not from the process start. R5 therefore waits for the fence to appear in the
   status view after the restart, sends its in-guard frames only after that (and reads the fence again after them), and waits for the fence to clear; the no-reader fallback of the fixtures waits 25 s.
5. **G9** (`N0TEST-10>APRS,WIDE1-1`): `own_source`, NOT repeated (lead decision D10, stricter than Dire Wolf, which repeats its own frames only with explicit routing). Asserted for the edge; Dire Wolf's answer is recorded as
   information only, and the data.md 15.5 sentence "repeated by both" is not asserted (being corrected).

## Targeted unit tests run for this change

`python3 -m pytest -m unit cases/unit/test_ax25_digi.py cases/unit/test_digi1_lib.py cases/unit/test_digi1_oracle.py cases/unit/test_digi1_cfg.py cases/unit/test_digi1_cases.py cases/unit/test_tester_env.py
cases/unit/test_direwolf_loop.py cases/unit/test_ax25.py cases/unit/test_oracle_direwolf.py cases/unit/test_dp15_rf_dw_config.py -q` and `python3 -m pytest --collect-only -q cases/test_digi1.py cases/test_digi1_dw.py cases/test_digi1_config.py`.
The cfgload and rig cases were only collected.
