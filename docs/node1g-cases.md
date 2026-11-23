<!-- SPDX-License-Identifier: MIT -->
# NODE1g cases: a hotspot linked to a central node over an emulated WAN

Status: RESULTS_PLACEHOLDER
Case file `cases/test_node1g.py`, pure helpers `dvtest/node1g.py` (hub counters, refusal rate, key files, arrival evidence, the plain-HTTP admin client, the WAN router client), unit tests
`cases/unit/test_node1g_lib.py`, switches `DVT_NODE1G` and `DVT_LINKOBS1` (documented in `dvtest/target.py`, passed to the tester by `rig/compose.yml`), templates
`rig/config/dv-router/node1g-hotspot.toml` (complete template of the hotspot) and `node1g-central.toml` (snippet of the central node), compose `rig/compose.node1g.yml` and the services `wan-router`,
`wan-edge`, `netem-wan` of `rig/compose.yml`. Notes: stdout and `out/<run>/node1g.txt` (`RECORD` = what was seen, `FINDING` = differs from the contract, `GAP` = could not be observed).
The contract is dv-router's answer `answers-364-node1g.md` (section numbers below are its own: 1 protocol, 2 credential, 3 recovery bounds, 4 overs during and after faults, 5 counters, 6 open
questions); its source references are DV-Router `docs/design/link.md` and `status.md`.

**Gate.** `DVT_NODE1G=on`, `--profile twonode`, `DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native`; without them every case skips with the reason. Not with `DVT_GUARDID2`, `DVT_RFPOS1B_LINK`
or the web, nat, interlink, asl-edge, aprs profiles (`run.sh` exits 2: the central node's admin service and state dir are its own, the NAT router is the WAN).

## The rig

See [rig.md](rig.md) "Hotspot link behind a WAN". In short: the SUT (`xlxd-sut`, `.11`) is the CENTRAL node, `dvr-peer` is the HOTSPOT on the inside network `172.31.0.0/24` behind `wan-router`
(`.98`). The hotspot dials `.11:31800` from an ephemeral socket (`[[edges.link.uplink]] name = "central"`, `account = "n0call-hs1"`, `rooms = { B = "C" }`); the central node has
`[[edges.link.account]] name = "n0call-hs1"` for room C with `talk = true`. Both nodes serve P25 (UDP 41000, TG 31672, `no_radio_id = "fallback"`; hotspot room B, central room C = one room through
the link), so the tester talks on one node and listens on the other with no transcoder; it has an address on the hotspot's LAN too. One key per run, `out/<run>/link-secrets/hs1.key`.

How a fault is made (only through the allow-listed channels, never by hand):

| Fault | How |
|-------|-----|
| delay, loss | control channel `netem wan <args>` (the sidecar shapes both interfaces of the router: each direction once) |
| blackout | router control `blackhole on|off` (UDP both ways between inside and outside; the tester's LAN to the hotspot and the TCP control are untouched) |
| NAT rebinding, new source port | router control `nat_mode symmetric` (MASQUERADE --random) and its flush of the UDP translations: the hotspot's next datagram gets a new random source port; `nat_mode port_restricted` puts it back |
| NAT rebinding, new source address | router control `readdress on|off` (SNAT of the hotspot's UDP to the router's alias address `.99`, flush) |
| rotated key | the tester replaces `hs1.key` by rename (the documented rotation, link.md) and sends `hup xlxd-sut` (control channel; SIGHUP is the documented "re-read every secret file" path) |

Evidence: media heard by P25 listeners with arrival times; the hubs' link counters (`metrics xlxd-sut` / `metrics dvr-peer`, refreshed about every 10 s: a read is taken 12 s after the last event);
the admin API of BOTH nodes over plain HTTP (`GET /api/v1/admin/status`, `link_edges`: `state`, `rtt_ms`, `loss_ppm`, `last_error`, `ip`, `port`); the router's `mappings` (the source address and port the
central node sees) and `firsts`. A link is "up" when an over crosses it: the arrival time of the first heard frame of a train of short probe overs (9 frames, a new unmapped radio id each so that no guard
strike builds up, every 0.4 s) is an UPPER bound of the moment the link carried media again.

## Why these netem values

| Setting | Value (each direction) | Why |
|---------|------------------------|-----|
| delay | `delay 60ms 10ms` | a consumer broadband or LTE uplink to a distant data centre: a round trip of about 120 ms (EU to US 100-150 ms). The 10 ms jitter stays below the 20 ms spacing of voice frames, so the shaper does not reorder; reordering is not what the cases test (a datagram with a counter at or below the highest is counted `replay`) |
| loss | `loss 3%` | the order of a poor WiFi or LTE uplink (1 to 5 percent). The session ends only when NO datagram arrives for 30 s, i.e. six keepalives in a row are lost: 0.03^6 = 7e-10, so the link must stay up; units have no repair (link.md: no ARQ), so an over must be heard at 97 percent in expectation, asserted at 85 |

## Cases and contract

| Case | What is done | Asserted (contract) |
|------|--------------|---------------------|
| NODE1g-0 | the link comes up through the NAT | both rows `connected` with a room map, the central node sees the hotspot at the router's address, an over each way heard whole (at least 90 percent), one session on each hub |
| NODE1g-a | `delay 60ms 10ms` for 25 s, one 10 s over each way | no handshake either side (section 3), both rows connected, `rtt_ms` 100..220 (about 2 x 60: section 5), each over heard at least 95 percent and its first frame 40..200 ms after it was sent, nothing on the must-not-move list (section 5) |
| NODE1g-b | `loss 3%` for 60 s, one 10 s over each way | no handshake, both rows connected, each over heard at least 85 percent, `loss_ppm` recorded (it is in the row, not a Prometheus series) |
| NODE1g-c | blackout of 20 s with the link idle | the session SURVIVES: no handshake (`welcomed` and `accepted` +0, section 3 "deterministic companions"), link up within 10 s of the end, overs resume both ways, `dv_status_room_onair` 0 on both rooms, one session per hub, collateral list |
| NODE1g-d | blackout of 60 s | 42 s in: rows `reconnecting` (hotspot) and `disconnected` (central) with `last_error` `timeout`, no session on either hub; after the end ONE handshake each side (`welcomed` +1, `accepted` +1), link up within 10 s, overs resume both ways, floors free, `last_error` clear |
| NODE1g-e1 | an over of 12 s from the hotspot, the WAN dark from 3 s for 20 s | a probe over of a central talker 3.0 s after the cut is granted (floor free about 1.6 s after the last Unit, not at the 30 s expiry: section 4), `dv_room_overs_total{room=C,result=end_missing}` +1, no handshake (the session survives 20 s), overs resume both ways |
| NODE1g-e2 | the same from the central node towards the hotspot | the probe over of a hotspot talker 3.0 s after the cut is granted, `dv_room_overs_total{room=B,result=end_missing}` +1 on the hotspot hub, no handshake, overs resume |
| NODE1g-f | a 20 s over each way; 8 s in the NAT mapping gets a NEW SOURCE PORT | no handshake (section 3: not a new peer), the central row shows the new port (the session was moved), media gap at most 5 s (+0.2 s of measurement slack), at least 90 percent of the over heard, the router's `mappings` differ before and after |
| NODE1g-g | the same with a NEW SOURCE ADDRESS (`readdress`) | the same; the source address differs; the router's `firsts` (first datagram of each side after the change) is recorded |
| NODE1g-h | the central node gets a new key (rename + `hup xlxd-sut`), the hotspot keeps the old one, 120 s; then the old key is put back | Reject{auth}: central `handshakes{auth}` grows and `handshakes{accepted}` does not, no session on either hub, no over crosses, both rows show `last_error` `refused_key`, the refusal RATE (growth between the first and the last hub refresh over the time between them) at most 1.1 per second (section 2: attempts 0.5 s doubling to 5 s, +-20 percent, Auth every 0.5 s: about 1.0), no count asserted; fixed key: link up within 10 s, one handshake each side, overs resume, `last_error` clear |
| NODE1g-j | ten faults: 4 x 8 s blackout, 3 x port rebinding, 2 x address rebinding, 1 x delay 3 s | overs resume after each; at the end the central node's process pids, the `dv-edge-link` thread and fd counts are unchanged (`proc-stat`), no child restart, one session per hub, floors free, the collateral list (section 5) |
| NODE1g-k | LINKOBS1 notes | `DVT_LINKOBS1=on` only: records the `dv_edge_link_*` series of both hubs and the four items below; without the switch it skips with the reason |

The collateral list (section 5, "must NOT move after recovery"): handshakes `auth_replay`, `auth_capacity`, `amplification`, `bad_proof`; datagram rx `bad_tag`, `unknown_session`, `malformed`; every
`dv_edge_link_refused_total`; `dv_launcher_child_restarts_total` of every child; a down row of `dv_status_process_up`. (NODE1g-h allows the restarts of `dv-edge-link` and `unknown_session` datagrams: the case
restarts that edge twice on purpose.)

Not asserted, on the contract's advice: whether a new handshake happened at exactly 30 s (a race, section 3); a count of refusals (a rate only, section 6 item 4); `dv_room_rts_total{result=link_down}` (that is the
fabric link to the room controller, not the node link).

### LINKOBS1 (a future image): RECORD-only

| Item | Today | Where it is recorded |
|------|-------|----------------------|
| a refusal log and counter on the hotspot | the hotspot only sets `last_error` `refused_key` (a Reject is a hint); NODE1g-h asserts that | NODE1g-k notes the series names |
| a session-end counter with reasons | none; reconnects are derived from `handshakes{welcomed}` (NODE1g-d asserts +1) | NODE1g-k |
| `Bye{revoked}` | `ByeReason::Revoked` is never sent; a rekey shows as `Bye{reload}`, the hotspot row `closed_by_peer` | NODE1g-h records the row labels it saw |
| a bad-source counter | a reply from a changed CENTRAL source is dropped without a count (section 3 end, 6 item 8) | not built: moving the central side's source needs a second central address; GAP |

## Not built

* Rebinding DURING the first 5 s of the hotspot's start (section 6 item 7: one failed attempt, then success): the hotspot cannot be restarted through the control channel (`dvr-peer` is not in the
  restart allow-list, and the allow-list was not extended).
* A change of the CENTRAL side's source address (section 3: "the hotspot silently drops its replies, the session dies at 30 s"): the rig has one central address.
* The hotspot's own process counters (`proc-stat` is read-only for `xlxd-sut` only): the thread / fd / pid evidence of NODE1g-j is the central node's.

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-42d5473 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 \
DVT_NODE1G=on scripts/run.sh -m rig --profile twonode -- cases/test_node1g.py -rA
```

About RUNTIME_PLACEHOLDER (a case of this file alone: `-k node1g_c`). `DVT_TRANSCODER_FAKE=1` is not needed (no transcoder is involved; `transcoder-peer` starts unused).

## Results and findings

RESULTS_TABLE_PLACEHOLDER
