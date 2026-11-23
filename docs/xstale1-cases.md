<!-- SPDX-License-Identifier: MIT -->
# DV-Router XSTALE1: the room's copy of an edge's own talker stays open for the whole over

Status: DV-Router `b849a082` (image `dv-router:wip-2ec9be4`, code `ee4aae71`); first rig run `20261123T023414Z` (5 / 4), harness fixed, re-run `20261123T034743Z` (b[p25] and b/c/d[usrp]) 4 / 0 with the earlier passes of c/d p25 and nxdn (see "Results"). Cases: `cases/test_xstale1.py` (kits in `cases/xstale1_support.py`), the soak
switch in `cases/test_web_soak.py` / `dvtest/soak.py` (`judge_xstale`), arithmetic and verdicts `dvtest/xstale1.py`; offline tests `cases/unit/test_xstale1_lib.py` and `cases/unit/test_soak.py`
(section XSTALE1). Switch for both: `DVT_XSTALE1=on` (the image has XSTALE1); off, the rig cases skip with the reason and the soak keeps its old behaviour.

## The defect and the fix

The edges without a same-protocol fast path (usrp, p25, nxdn) get the room's copy of their own talker's over as a cross stream (origin = this edge, so the talker hears no echo). With nobody else
listening at that edge the stream has no listener leg. Before `b849a082` the 1.6 s silence timer closed it 1.6 s into the over, because only copies for an intended listener refreshed it, so

* every later unit of the over counted `dv_edge_drops_total{reason="x_stale"}` (the short soak of `dv-router:wip-37b7960` counted about 23 a second over all edges),
* the edge's tx accounting folded the over with `end_missing` / `late`,
* a listener of the same edge that joined later heard nothing (its first unit hit a closed stream).

After the fix matching unexpired units refresh the running cross stream and its accounting horizon even when no copy is needed; a stream without a listener leg is no loss; a leg that joins such an over
accounts from its first arrival and folds as `joined` (status.md "Built (S3)", media-format.md cross-mode rendering); real silence of 1.6 s without a matching unit still ends the stream.
YSF's `x_stale` (about 29 in the short soak) has another cause and is recorded, not asserted.

## Soak: `DVT_XSTALE1=on`

The sampler keeps `dv_edge_drops_total{reason="x_stale"}` per `proc` at every counter snapshot (`counters.xstale` in `soak.json`; `soak.drops_by_proc`). `dvtest.soak.judge_xstale` compares the end of
warmup with the end of the measured part:

| Edge (`proc=`) | Judged | Bound |
|----------------|--------|-------|
| `usrp`, `p25`, `nxdn` (those whose lane is in the mix: usrp when a `usrp-*` lane is, p25 / nxdn when their probe over worked) | check `xstale:<proc>` with the switch on | growth <= max(3, 0.1 per second x window): 24 in the 240 s of `DVT_SOAK_SHORT=1`, 300 in the 3000 s run |
| `ysf` and every other proc | recorded (table `dv_edge_drops_total x_stale` of `counters_table`, `counters.xstale.others`) | none |

Why 0.1/s: the old behaviour counted every unit after the first 1.6 s of each over, about 8.4 s of units in a 10 s over and one over a minute per lane: usrp and p25 50 units a second (7 a second per lane, two
usrp lanes 14), nxdn 12.5 (1.75 a second). 0.1/s is under a tenth of the weakest of them, and still leaves 6 a minute for strays (a unit behind its `StreamEnd`); the expected value is 0. The absolute floor of 3
keeps a very short window from failing on one straggler. An edge with no `x_stale` series at either snapshot fails its check (the detail names the procs seen: the label may differ); without the switch the numbers
are recorded and a `FINDING: SOAK1: XSTALE1 (not asserted ...)` line names a judged edge above the bound (nothing fails; the old rules, `drops-reasons` with `x_stale` legit and the 1/s flag, are unchanged either way).

```sh
export XLXD_SUT_IMAGE=dv-router:wip-2ec9be4 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_XSTALE1=on DVT_SOAK=1 DVT_SOAK_SHORT=1 \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/soak-edges.toml \
  scripts/run.sh -m rig --profile web,webload -- cases/test_web_soak.py
```

(About 14 minutes; drop `DVT_SOAK_SHORT=1` for the full run, `docs/web-soak-cases.md` "Run it". The RSS slopes of a short run are leads, the `xstale:*` checks are a verdict.)

## Rig cases: `cases/test_xstale1.py`

Talker A and listener B are two clients of the SAME server port of the system under test (USRP dynamic hub, the NXDN and P25 server ports of `soak-edges.toml`), so the room's copy of A's over is rendered at A's own
edge. Per protocol (`usrp`, `p25`, `nxdn`: the parameter `proto`); the counters are the hub's, read with the control channel (`DVT_RIG_CONTROL=1`, set by `scripts/run.sh`): a baseline that did not move for 12 s,
ONE scenario, wait for the fold count of the edge (`dv_edge_overs_total{proc,dir="tx"}`) to rise, one more read 12 s later (a record every 10 s per edge process).

| Case | Scenario | Asserted |
|------|----------|----------|
| XS1-b | A talks one 8 s over alone on its edge, then the endpoints stay linked 3 s (the edge folds 1.6 s after the End) | one fold at the edge; x_stale <= 2; tx `end_missing`, `lossy`, `late`, `dropped`, `lost` all 0; not `joined`; A hears none of its own over |
| XS1-c | A talks 8 s; B links to the same edge 3 s into the over | B hears at least 80 percent of the units still to come after its link completed (`joined_at` is measured, the link must complete within 3 + 1.5 s or the case fails as a harness problem) and no more than was sent (+ padding: P25 9, NXDN 4); the over folds as `joined`; lost, late, end_missing, lossy, dropped 0; x_stale <= 2 |
| XS1-d | B linked from the start; A talks 4 s, is silent 2.4 s (over the 1.6 s limit) with no terminator, unkey or trailer, then talks 4 s again | B hears at least 80 percent of each burst and no more than was sent (the first stream ended, the second is a new one); the edge folds two overs; x_stale <= 2; late, dropped, lost 0. `end_missing` / `lossy` of the first over (a stream that ended by silence) is RECORDed in `out/<run>/xstale1.txt`, not asserted |

Bounds and why: 80 percent of the rest because the join itself (USRP registration and DTMF, an LDU or NXDN packet boundary) costs a few units, while the old edge delivered none of the rest; 4 s bursts in d) because the old edge
closed the stream 1.6 s in (40 percent heard); x_stale <= 2 per case because the expected value is 0 and the old count was about 320 (usrp, p25: 6.4 s at 50 units a second) and 80 (nxdn) in one 8 s over.

**What d) can and cannot show from outside.** The ingress edge ends an over by silence on its own before the cross stream's 1.6 s limit would matter: P25 and NXDN after 1.6 s (it synthesizes the terminator, spec
`test_p25_stream_timeout` `timeout_s` 1.6), USRP after 100 ms of ingress silence. So d) checks the observable outcome (two streams, both heard whole, two folds, no x_stale) and not the cross stream's own 1.6 s boundary;
that boundary (1599 ms retained, 1600 ms closed, anchored to the last unit) is the Rust unit test `genuine_sixteen_hundred_ms_silence_closes_own_over`.

Diagnosis switch: `DVT_XSTALE1_WITNESS=dmr` (or `dstar`, `m17`, `ysf`, `dplus`, `dcs`) links a client of that other protocol to the room of the edge for the whole of XS1-b and records how many datagrams it heard; it asserts nothing. When an edge
never folds the over (the egress over count does not rise), the failure names what the clients heard and every series of the edge that moved (`series_diff`); the witness tells a talk that never reached the room from a room that never
published the own copy back.

### Run

Modules: A for USRP (`DVT_XSTALE1_USRP_MODULE`), P25 and NXDN by TG 31672 (`DVT_P25_TG` / `DVT_NXDN_TG`, the module follows `soak-edges.toml`: P25 G, NXDN F). No web profile, no `--capture` needed.

```sh
export XLXD_SUT_IMAGE=dv-router:wip-2ec9be4 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_XSTALE1=on DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/soak-edges.toml \
  scripts/run.sh -m rig -- cases/test_xstale1.py -rA
```

One protocol or case: add `-k "c_same and p25"`. Estimated time (not measured): per case a 12 to 24 s baseline, 8 to 10 s of traffic, 20 to 60 s for the fold and the quiet read, about 1 to 1.5 minutes each, 9 cases in 10 to 14
minutes. Run it alone: any other traffic on the edges of the three protocols moves the counters. Without `DVT_USRP_DYNAMIC=1` the usrp cases skip; without `soak-edges.toml` the p25 and nxdn cases fail with that hint (an
edge that is not serving). Results: the `[RECORD]` lines in `out/<run>/xstale1.txt`.

## Results (image dv-router:wip-2ec9be4)

| Run | Cases | Result |
|-----|-------|--------|
| `20261123T023414Z` | all 9 | 5 passed; b/c/d[usrp] failed (HARNESS, below); b[p25] failed (lossy 1, lost 392, PRODUCT, below) |
| `20261123T033751Z` | b[usrp] with `DVT_XSTALE1_WITNESS=dmr` | harness error (the witness had no module): found the empty-string trap below |
| `20261123T033822Z` | b[p25] cold, alone | passed (complete 1, lost 0) |
| `20261123T034743Z` | b[p25], b/c/d[usrp] after the fix | 4 / 0: usrp b complete 1; c joined 1, heard 224 of 223 to come; d complete 2, both bursts heard whole |

**HARNESS (usrp):** compose passes an unset `DVT_XSTALE1_USRP_MODULE` to the tester as an empty string, `env.get(name, default)` returned it, and the USRP clients linked to module "" (the DTMF named no room): nothing
reached a room, no fold, B heard 0. Fixed (`env.get(name) or default`, unit test). The same trap applies to every `DVT_*` switch read with a default: use `or`.

**PRODUCT (p25, intermittent, first over of a cold edge):** XS1-b[p25] in `20261123T023414Z` (the first P25 over after the SUT start): 396 units sent, tx fold `lossy` 1 with `lost` 392 (4 of 396 counted as arrived),
x_stale, late, dropped, end_missing 0. The SUT had one P25 edge (`[edges.p25]` server port, module G, no peer rows), the talker was its only client. The same signature (first P25 over `lossy`, `lost` 95) is in
the short soak `20261123T024326Z`. Not reproduced when the over came cold alone (`20261123T033822Z`) or first in `20261123T034743Z` (complete, lost 0), and not on nxdn or after a warm-up over (`d[p25]` then `b[p25]`: complete).
In every cold run the P25 edge counted `dv_edge_drops_total{reason=id_held}` 2 before the first fold: the StreamStart was held for a radio id lookup. In the source (`ee4aae71`, `on_media`) units behind a held start go
through `Accounting::observe`, which marks the profile wanted and counts them as arrived; the leg-less path of `b849a082` is skipped for them, so a leg-less over folds with `expected - arrived` lost when a few units
arrive during the hold (about 4 at 20 ms, matching 392 of 396). status.md after `b849a082` says a stream without a listener leg adds no loss. The case therefore keeps asserting tx `lost` / `lossy` 0 (a hold race, not an
XSTALE1 regression: end_missing, late and x_stale stayed 0).

## Open points

* Confirmed on the rig: the `proc` labels are `usrp`, `p25`, `nxdn`; both families carry them; dynamic USRP talker and listener fold as expected (b complete, c joined, d two overs).
* A `joined` fold needs the first unit of B's leg to be the first wanted; if the hub folds B's over as `complete` the case names it (`joined 0, complete 1`) and fails (not seen: c folded joined on usrp, p25 and nxdn).
* The first-over `lossy` of a cold P25 edge (id hold race, above) is intermittent: 1 of 3 cold runs.
