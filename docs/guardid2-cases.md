<!-- SPDX-License-Identifier: MIT -->
# GUARDID2 cases: the guard source identity crosses the router link

Status: rig run `20261123T113412Z` (testset of this commit, image `dv-router:wip-3f142cd`, DV-Router `9a8d1cb4`, `--profile twonode`, real transcoders, not used by these cases): **2 passed in 201 s**.
Case file `cases/test_guardid2.py`, pure judge `dvtest/guardid2.py` (plus the StreamStart / Refused reading added to the link relay of `dvtest/rfpos1.py`), unit tests
`cases/unit/test_guardid2_lib.py`, switch `DVT_GUARDID2` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`), snippet
`rig/config/dv-router/guardid2-p25.toml`. Notes: stdout and `out/<run>/guardid2.txt` (`RECORD`, `FINDING`); numbers and the link records of each case in `out/<run>/guardid2/<case>.json`.

**Gate.** `DVT_GUARDID2=on`, `--profile twonode`, `DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native`; without them every case skips with the reason (the module fixture `link` also skips when the node
link does not come up within 150 s, naming what the relay saw).

## What GUARDID2 promises (floor-control.md "Guard penalties", link.md Appendix A, media-format.md)

* An unmapped P25 talker (source id in no directory, `no_radio_id = "fallback"`) is shown under the placeholder callsign `P31672` (the `P%05u` of the talkgroup), which every such talker shares, and its RTS
  carries `RTS_SOURCE_ID`: the guard then keys on the DECIMAL SOURCE ID, not on the callsign, so two unmapped talkers never strike each other (FUZZP25A).
* The router link relays that choice: bit 0 of the `carried` u16 of the StreamStart record (link: kind 19; fabric: the same bit) is the `source_id` flag, with no byte added (no profile has id 0, so the bit is
  outside every profile mask). The receiving link edge states `RTS_SOURCE_ID` for such a stream, so node 2's guard keys on the same source id as node 1's. A mapped callsign has the flag clear and keeps its callsign key.
* Without the flag node 2 would key the relayed streams on `P31672`: a strike earned by one unmapped talker would deny every other unmapped talker on node 2 while node 1 still granted it.
* Radio FM ports, USRP rows and AllStar links share one guard identity by design (floor-control.md); not tested.

## What the rig has

`DVT_GUARDID2=on` makes `scripts/run.sh` link the twonode pair by the node link of RFPOS1 step B (`rfpos1-link-a.toml` uplink of node 1 at the tester's relay, `rfpos1-link-b.toml` account on node 2, a per-run key; the
relay is `dvtest.rfpos1.LinkRelay`, which logs the cleartext record headers and forwards every datagram unchanged) and adds `guardid2-p25.toml` to BOTH nodes: a P25 server port (UDP 41000, TG 31672 -> room G, the room of the link)
with `no_radio_id = "fallback"`. Talkers and listeners are ordinary P25 clients (`dvtest/proto/p25.py`), so no transcoder is involved: node 2's listener hears node 1's IMBE units unchanged and each over is identified by its
own PRNG frames (`signals.prng_frames("imbe", ...)`, one seed per over). Without the switch nothing changes for any other run (the case patterns in `run.sh` match exactly what they matched before when `DVT_GUARDID2` is unset).
The unmapped talkers are the source ids 1023961 (X) and 1023962 (Y), in no directory; the mapped ones 1023007 (N0CALL) and 1023008 (N0TEST), in `rig/config/dmrid.dat` (a unit test checks both).

## What is asserted

Each case runs one ladder in room G for a struck talker S and an other talker O, with a P25 listener on node 1 and one on node 2:

1. S sends a long over (54 frames, 1.1 s): relayed and heard whole on both nodes.
2. S sends three short overs (9 frames, 0.5 s apart): strike 1 (B18 ladder), a 3 s TX block on S's guard key.
3. S sends a probe over (9 frames) inside the block: refused on node 1 (`dv_edge_drops_total{proc="p25",reason="deny_guard_block"}` +1), nothing of it crosses the link.
4. O sends a long over inside the same block (about 1 s after the strike).

| Case | Talkers | Assertion |
|------|---------|-----------|
| GUARDID2-a | X (1023961), Y (1023962): unmapped, both shown as `P31672` | every StreamStart on the link (X 4, Y 1) has bit 0 of `carried` set, callsign `P31672`, the talker's own radio id, and a datagram of exactly 62 + `raw_len` bytes; the listeners on BOTH nodes hear at least 80 percent of X's first over and of Y's over, none of X's probe; no `Refused` record on the link; node 1: `dv_room_rts_total{result=grant}` rises by at least 5 and the guard denial counter by at least 1; node 2: grant rises by at least 5, `dv_room_rts_total{result=deny}` and every guard denial counter (`deny_guard_block`, `guard_blocked`, any proc) stay 0 |
| GUARDID2-b | N0CALL (1023007), N0TEST (1023008): mapped | the same ladder: every StreamStart has the flag CLEAR and its own callsign, the same fixed size as case a (no bytes added by the flag); N0CALL's strike blocks N0CALL's probe on node 1 and does not block N0TEST, which is heard whole on both nodes; node 2 counts no denial. The control: the mapped talker behaves as before |

The hub counters refresh about every 10 s, so each node is read until three reads 6 s apart agree, before the traffic and again at least 12 s after it (`settled` in the case file).

### GAPs

* The fabric StreamStart (the same bit, per the document) is internal to a node; only the link record can be seen black-box.
* Node 2's own strike on X cannot be observed separately: X's overs after the strike are refused on node 1 before they reach the link, and node 2 exports no per-key guard record count. What the case shows is the
  consequence the document promises (Y, behind the same shown callsign, is granted on node 2 inside the window in which node 2 also counted X's three short overs); a lost flag would show as `Refused` busy on the link and
  `deny_guard_block` on node 2 (`proc="link"`). No mutated image exists to prove that detection.
* DMR talkers: a DMR talker without a callsign is shown under its decimal id, which is its own guard key, so two DMR talkers never share a placeholder; not a separate case.
* Radio FM ports, USRP rows, AllStar links: one shared identity by design.

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-3f142cd DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 \
DVT_GUARDID2=on scripts/run.sh -m rig --profile twonode -- cases/test_guardid2.py -rA
```

No fake transcoder, no web profile, no `--capture` needed. About 3.5 minutes. One rig, nothing else at the same time. Strike 1 decays after 300 s and the callsigns / ids are used once per run, so a second run within 5 minutes of
the first may find X or N0CALL already struck (a block of 10 s on the next ladder, which the case would report as a failure of its first assertion).

## Results

| Run | Image | Result |
|-----|-------|--------|
| `20261123T113412Z` | `dv-router:wip-3f142cd` | 2 passed in 201 s |
| `20261123T113744Z` | `dv-router:wip-3f142cd` | 2 passed in 212 s; every number below identical (a repeat, started by a stray waiter of the first launch) |

Numbers (case a, X = 1023961, Y = 1023962): link StreamStart records X 4, Y 1, all `carried = 0x0001`, callsign `P31672`, 79 bytes (`raw_len` 17); steps at 0.0 / 1.48 / 2.17 / 2.85 / 3.33 / 3.81 s (base, three shorts, probe, Y), Y's over starts 0.5 s after the probe and about 1 s after the strike. Heard (frames of
54 / 9 / 9 / 9 / 9 / 54 sent): node 1 54 / 9 / 9 / 9 / 0 / 54, node 2 54 / 9 / 9 / 9 / 0 / 54 (egress source ids 1023961 and 1023962). Counters moved: node 1 `rts.grant` +5, `rts.deny` +1,
`p25.deny_guard_block` +1, `p25.dropped_frame` +5, `p25.id_held` +3; node 2 `rts.grant` +5, `p25.id_held` +1, no deny, no guard denial, no Refused. Case b: StreamStart `carried = 0x0000`, callsigns N0CALL x4 and N0TEST x1, 79 bytes;
heard identical (54 / 9 / 9 / 9 / 0 / 54 on both nodes); node 1 `rts.grant` +5, `rts.deny` +1, `p25.deny_guard_block` +1; node 2 `rts.grant` +5, no deny.
