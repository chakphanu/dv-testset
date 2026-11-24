# K9C: endpoint direction is enforced (DV-Router K9c, 39337fb5; images from wip-ae11b8f)

DV-Router K9c makes the `direction` of an endpoint row real (config.md "Configured endpoint bindings"; floor-control.md; status.md):

| direction | the endpoint | the edge |
|-----------|--------------|----------|
| `to_room` | talks into the room, hears nothing | membership and ordinary ingress stay; no listener leg, no room egress, no transcoding demand |
| `from_room` | hears the room, cannot talk | the listener leg stays; a new RTS is refused as TxAcl (DENY 2) before protocol talker admission, counted once per over as `dv_edge_drops_total{reason="deny_tx_acl"}`, no ingress over, no busy tone |
| `both` | both | unchanged |

Valid on radio ports, USRP, NXDN, P25, XLX and M17 rows, AllStar node rows and outbound rows (outbound `from_room` is a Transceive call whose far-node
audio is refused). A direction change applies without a restart (Bindings), also in the middle of an over: the core leaves the memberships first, ends
the ingress over through the ordinary LEAVE settlement and rejoins with the new direction. A radio port hears the room only when its own TX gate allows
TX; no direction turns TX on. An outbound direction change is Restart(child) (the direction selects the call mode). K9ab2 (0d1ba106): `dv-xlxd-import`
names unnamed USRP rows `usrp-<module>`; the removed outbound `mode` key is reported as "mode was replaced by direction (...)".

## The case (`cases/test_k9c.py`, host-side library `dvtest/k9c.py`, snippet `rig/config/dv-router/k9c-usrp.toml`)

One edge: USRP. The image allows ONE USRP row per module ("module K already used by endpoint[0]"), so the two directions live in two rooms. Row
`k9c-from` (from_room) is alone on K, row `k9c-to` (to_room) alone on M; the tester is the far end of both (UDP 32022 / 32023 to the rows' binds
34022 / 34023) and each room has one client of the dynamic hub (class 0) as the third party. Every number goes to `out/<run>/k9c.txt` as `[RECORD]` or
`[FINDING]`; the deny counters are the exact series of the hub's /metrics before and after (`k9c.deny_text`, `delta`).

| Case | What | Asserts |
|------|------|---------|
| a | k9c-from keys up twice (75 frames each) | the third party hears 0 voice, the row gets 0 voice back (no busy tone), `deny_tx_acl` moves by exactly 2, no other deny counter moves; then the third party talks 75 frames and the row hears at least 80 percent |
| b | k9c-to keys up (75 frames), then the third party talks | the third party hears at least 80 percent, the row hears 0 voice of the room, no deny counter moves |
| c | k9c-from over of 500 frames; 2 s in, the rendered config is edited to `both` and `hup xlxd-sut` | same process set (pid and name) before and after; the over in progress stays refused until its unkey (the third party hears 0 of its 500 packets: floor decisions are made at RTS, dv-router's contract); an over of 75 frames after the switch is heard (80 percent); `deny_tx_acl` +1 in all |
| d | k9c-to over of 500 frames; 2 s in, edited to `from_room` and `hup` | the third party hears at least 25 packets before the write and none later than the write plus the SIGHUP delay plus 4 s (the over is cut at once); same processes; the row now hears the third party (80 percent); a new over of the row is not heard and `deny_tx_acl` moves by exactly 1 in all (it counts RTS refusals only). RECORDED, not asserted: the fold of the cut over, every over / revoke / cut / leave / direction series that moved and the new log lines (`k9c-d-metrics.json`) |
| e | end | the file and both rows are as they started, no process of xlxd-sut was replaced in the whole run |

The reload is the rig's existing path: the run's own rendered copy (`DVT_SUT_CONFIG_RW=1`, `out/<run>/xlxd-config/xlxd-sut/rw/dv-router.toml`) is
written in place and the launcher gets `hup xlxd-sut` (no new control action; the file is put back and signalled again after every case).

## How to run (about 5 minutes; a rig run of its own)

```
XLXD_SUT_IMAGE=dv-router:wip-ae11b8f DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=k9 \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_RW=1 DVT_K9C=on DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/k9c-usrp.toml \
  scripts/run.sh -m rig -- cases/test_k9c.py
```

Without `DVT_K9C=on`, a gen other than k9, a missing snippet or a read-only config the cases skip with the reason. Check a run with
`grep "run.sh: pytest exit" out/<run>/*.log` and `out/<run>/k9c.txt`.

Config check without the rig (host side, `-m cfgload`, `cases/test_k9_config.py`, `DVT_CFGLOAD_IMAGE=dv-router:wip-ae11b8f DVT_SUT_GEN=k9`): every
direction loads on a USRP row and on an outbound row, two USRP rows on one module are refused, the snippet loads, the `mode` message is checked.
`DVT_CFGLOAD_K9C=0` runs the same file against wip-4732c43 (K9a/K9b), where a non-both direction is refused.

## Results (image wip-ae11b8f, code 0d1ba106, DVT_SUT_GEN=k9; USRP only)

Run `20261124T115439Z`: 5 passed (161 s), `run.sh: pytest exit 0`. Earlier runs: `20261124T102040Z` (a failed on a harness bug, c and d on expectations that dv-router
then corrected) and `20261124T102410Z` (5 passed with c and d as observed, before dv-router's answers).

| Case | Result |
|------|--------|
| a | two overs of the from_room row: the third party heard 0 voice, the row got 0 datagrams back (no busy tone), `dv_edge_drops_total{proc="usrp",reason="deny_tx_acl"}` +2 exactly (once per over), no other deny series moved; the third party then talked 75 frames and the row heard 75 of 75 (run `20261124T102410Z`) |
| b | to_room row, 75 frames: the third party heard 75 of 75; the third party talked 75 frames: the row got 0 datagrams of any kind; no deny counter moved |
| c | from_room -> both mid-over (file write 1.97 s into the over, SIGHUP 0.14 s after it): same processes; the third party heard 0 of the 500 packets of the over in progress (asserted); the next over was heard; `deny_tx_acl` +1 |
| d | to_room -> from_room mid-over (write 1.98 s in, SIGHUP 0.34 s after it): same processes; 98 packets heard before the write, the last 0.345 s after it (the SIGHUP), none later; 116 of 500 packets heard in all; the row then heard the room; `deny_tx_acl` +1 for the next over only |
| e | rows and file back to the start, process set unchanged for the whole run |

### Fold of the cut over (case d, deltas one telemetry period after the cut, `out/20261124T115439Z/k9c-d-metrics.json`)

| Series | Delta |
|--------|-------|
| `dv_room_overs_total{room="m",result="complete"}` | +1 (no lossy, no end_missing) |
| `dv_edge_overs_total{dir="rx"}` | no series moved (the ingress over of the edge is not folded there) |
| `dv_edge_overs_total{proc="usrp",dir="tx",result="complete"}` | +1 (the egress over to the third party) |
| `dv_room_rts_total{result="grant"}` | +1; `dv_room_units_total{kind="published"}` +116 |
| `dv_edge_drops_total{proc="usrp",reason="dropped_frame"}` | +385 (the 500 - 115 frames after the cut, the same reason as any dropped frame) |
| any revoke, cut, leave or direction series | none exists or moved (17 series moved in all, the rest are status and id feed counters) |

Log: the reload logs `dv-router: config gen 4: bindings changed: published for immediate endpoint rebind` and `dv-edge-usrp: config generation 4: applied
(access/transit/data/bindings; startup settings unchanged)` at the SIGHUP. No log line names the cut over; the 10 s `RoomStats` and `sessions ... counters`
lines show `Deny(TxAcl)=3` and `DroppedFrame=1038` cumulatively for the whole run.

FINDING (for dv-router to open a row): nothing tells a policy cut from a normal end. The cut over folds as an ordinary `complete` on the room (and `complete`
on the egress edge), the ingress edge folds nothing in `dv_edge_overs_total{dir="rx"}`, no revoke or cut counter or log line exists for it, and `deny_tx_acl` does
not count it (by design). The only trace is the generic `dropped_frame` count of the frames after the cut and the config-generation log lines. An operator cannot
tell "the talker stopped" from "its direction was changed and it was cut" from the counters.

Other notes: the image allows one USRP row per module (config check: "module K already used by endpoint[0]"); the hub carries the deny counter in the `usrp`
edge process (`proc="usrp"`); a counter moved within one telemetry period (about 10 s) of the over; the config write alone applies nothing, the SIGHUP does.
