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
| c | k9c-from over of 500 frames; 2 s in, the rendered config is edited to `both` and `hup xlxd-sut` | the third party hears nothing before the file write and voice later than the write plus the SIGHUP delay plus 4 s; same process set (pid and name) before and after; an over of 75 frames after the switch is heard (80 percent); `deny_tx_acl` +1 in all |
| d | k9c-to over of 500 frames; 2 s in, edited to `from_room` and `hup` | the third party hears at least 25 packets before the write and none later than the write plus the SIGHUP delay plus 4 s; same processes; the row now hears the third party (80 percent); a new over of the row is not heard and `deny_tx_acl` moves by exactly 2 in all (the rest of the first over, the second over) |
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

## Results

None yet (offline part only).
