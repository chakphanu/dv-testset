<!-- SPDX-License-Identifier: MIT -->
# NODE1g cases: a hotspot linked to a central node over an emulated WAN

Status: rig runs on image `dv-router:wip-42d5473` (`DVT_SUT_GEN=compat1`, `--profile twonode`, `DVT_NODE1G=on`): **`20261123T225943Z`: 11 passed, 2 failed (NODE1g-f and -g, FINDING 1), 1 skipped (the LINKOBS1
note, no switch)** in 1092 s of pytest (about 21 minutes with the rig start and stop); **`20261123T231838Z`**: the cases f, g and h again after a harness fix (the row of the central node is waited for by
address AND port): f and g fail the same way (FINDING 1), h passes. Two earlier runs (`20261123T220327Z`, `20261123T224316Z`) are what the harness was debugged with; they saw FINDING 2 once each. **`20261123T234228Z`** (with `--capture`, `DVT_NODE1G_FOLD=4`, cases a and m
only): the follow-up to dv-router's answers (FINDING 2 reproduced and judged against the captures, the reordering of case a counted).
Case file `cases/test_node1g.py`, pure helpers `dvtest/node1g.py` (hub counters, refusal rate, key files, arrival evidence, the plain-HTTP admin client, the WAN router client), unit tests
`cases/unit/test_node1g_lib.py`, switches `DVT_NODE1G` and `DVT_LINKOBS1` (documented in `dvtest/target.py`, passed to the tester by `rig/compose.yml`), templates
`rig/config/dv-router/node1g-hotspot.toml` (complete template of the hotspot) and `node1g-central.toml` (snippet of the central node), compose `rig/compose.node1g.yml` and the services `wan-router`,
`wan-edge`, `netem-wan` of `rig/compose.yml`. Notes: stdout and `out/<run>/node1g.txt` (`RECORD` = what was seen, `FINDING` = differs from the contract, `GAP` = could not be observed).
The contract is dv-router's answer `answers-364-node1g.md` (its section numbers below: 1 protocol, 2 credential, 3 recovery bounds, 4 overs during and after faults, 5 counters, 6 open questions); its source
references are DV-Router `docs/design/link.md` and `status.md`.

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
| NAT rebinding, new source port | router control `nat_mode symmetric` (MASQUERADE --random) and its flush of the UDP translations: the hotspot's next datagram gets a new random source port; `nat_mode port_restricted` puts it back (a second rebinding) |
| NAT rebinding, new source address | router control `readdress on|off` (SNAT of the hotspot's UDP to the router's alias address `.99`, flush) |
| rotated key | the tester replaces `hs1.key` by rename (the documented rotation, link.md) and sends `hup xlxd-sut` (control channel; SIGHUP is the documented "re-read every secret file" path; the action exists for SECRELOAD1 and is used here as it is, no allow-list entry was added) |

Evidence: media heard by P25 listeners with arrival times; the hubs' link counters (`metrics xlxd-sut` / `metrics dvr-peer`, refreshed about every 10 s: a read is taken 12 s after the last event; the series
of `dv-edge-link` restart with the process, so a case that reloads the key reads the central node's link series from the restart on, `dvtest.node1g.rebase`); the admin API of BOTH nodes over plain HTTP
(`GET /api/v1/admin/status`, `link_edges`: `state`, `rtt_ms`, `loss_ppm`, `last_error`, `ip`, `port`); the router's `mappings` (the source address and port the central node sees) and `firsts` (the first
datagram of each side after an address change). A link is "up" when an over crosses it: the arrival time of the first heard frame of a train of short probe overs (9 frames, a new unmapped radio id each so
that no guard strike builds up, every 0.4 s) is an UPPER bound of the moment the link carried media again.

## Why these netem values

| Setting | Value (each direction) | Why |
|---------|------------------------|-----|
| delay | `delay 60ms 10ms` | a consumer broadband or LTE uplink to a distant data centre: a round trip of about 120 ms (EU to US 100-150 ms). The 10 ms jitter stays below the 20 ms spacing of voice frames, so the shaper should not reorder; reordering is not what the cases test (observed anyway, see the RECORD of NODE1g-a) |
| loss | `loss 3%` | the order of a poor WiFi or LTE uplink (1 to 5 percent). The session ends only when NO datagram arrives for 30 s, i.e. six keepalives in a row are lost: 0.03^6 = 7e-10, so the link must stay up; units have no repair (link.md: no ARQ), so an over must be heard at 97 percent in expectation, asserted at 85 |

## Cases and contract

| Case | What is done | Asserted (contract) |
|------|--------------|---------------------|
| NODE1g-0 | the link comes up through the NAT | both rows `connected` with a room map, the central node sees the hotspot at the router's address, an over each way heard whole (at least 90 percent), one session on each hub |
| NODE1g-a | `delay 60ms 10ms` for 25 s, one 10 s over each way | no handshake either side (section 3), both rows connected, `rtt_ms` 100..220 (about 2 x 60: section 5), each over heard at least 95 percent and its first frame 40..200 ms after it was sent, nothing on the must-not-move list (section 5); with `DVT_LINKSEQ1=on` also the LINKSEQ1 asserts below |
| NODE1g-b | `loss 3%` for 60 s, one 10 s over each way | no handshake, both rows connected, each over heard at least 85 percent, `loss_ppm` recorded (it is in the row, not a Prometheus series) |
| NODE1g-c | blackout of 20 s with the link idle | the session SURVIVES: no handshake (`welcomed` and `accepted` +0, section 3 "deterministic companions"), link up within 10 s of the end, overs resume both ways, `dv_status_room_onair` 0 on both rooms, one session per hub, collateral list |
| NODE1g-d | blackout of 60 s | 42 s in: rows `reconnecting` (hotspot) and `disconnected` (central) with `last_error` `timeout`, no session on either hub; after the end ONE handshake each side (`welcomed` +1, `accepted` +1), link up within 10 s, overs resume both ways, floors free, `last_error` clear |
| NODE1g-e1 | an over of 12 s from the hotspot, the WAN dark from 3 s for 20 s | a probe over of a central talker 3.0 s after the cut is granted (floor free about 1.6 s after the last Unit, not at the 30 s expiry: section 4), `dv_room_overs_total{room=C,result=end_missing}` +1, no handshake (the session survives 20 s), overs resume both ways |
| NODE1g-e2 | the same from the central node towards the hotspot | the probe over of a hotspot talker 3.0 s after the cut is granted, `dv_room_overs_total{room=B,result=end_missing}` +1 on the hotspot hub, no handshake, overs resume |
| NODE1g-e3, e4 | e1 and e2 with two early probe overs of the talker of the receiving node (9 frames, 1.0 s and 2.0 s after the cut) | the 3.0 s probe is asserted as before; which early probe is granted narrows the release time (RECORD); how the over is folded is only recorded (a second talker competes for the floor) |
| NODE1g-f | NAT rebinding to a NEW SOURCE PORT in the middle of one 20 s over from the hotspot and three 8 s overs from the central node (rebinding 3 s in), then the way back with a train of probe overs started at it | no handshake (section 3: not a new peer), the central row shows the new address and port, the longest silence inside each over (a lost tail counts) at most 5 s, at least 90 percent of each over heard (only the gap may be lost), the first probe over after the way back heard within 5 s |
| NODE1g-g | the same with a NEW SOURCE ADDRESS (`readdress`) | the same; the router's `firsts`: the first datagram of the hotspot from the new address and the first datagram of the central node to it come within 5 s |
| NODE1g-h | the central node gets a new key (rename + `hup xlxd-sut`), the hotspot keeps the old one, 120 s; then the old key is put back | Reject{auth}: central `handshakes{auth}` grows and `handshakes{accepted}` does not, no session on either hub, no over crosses, both rows show `last_error` `refused_key`, the refusal RATE (growth between the first and the last hub refresh over the time between them) at most 1.1 per second (section 2: attempts 0.5 s doubling to 5 s, +-20 percent, Auth every 0.5 s: about 1.0), no count asserted; fixed key: link up within 10 s, one handshake each side, overs resume, `last_error` clear |
| NODE1g-j | ten faults: 4 x 8 s blackout, 3 x port rebinding, 2 x address rebinding, 1 x delay 3 s | overs resume after each; at the end the central node's process pids, the `dv-edge-link` thread and fd counts are unchanged (`proc-stat`), no child restart, one session per hub, floors free, the collateral list (section 5) |
| NODE1g-m | opt-in (`DVT_NODE1G_FOLD=<n>`, run with `--capture`): n isolated 12 s overs per direction, the WAN dark from 3 s for 20 s, nothing else on the link until the hubs have counted the over | the receiving node's room folds exactly one over and it is `end_missing` (the End and the later units were lost in the blackout); `python3 -m dvtest.node1g_capture out/<run>` then judges every window against the captured link datagrams with dv-router's rule (complete only when the End arrived outside the blackout AND no unit position is missing) and names the stream of a defect |
| NODE1g-k | LINKOBS1 notes | `DVT_LINKOBS1=on` only: records the `dv_edge_link_*` series of both hubs and the four items below; without the switch it skips with the reason |

The collateral list (section 5, "must NOT move after recovery"): handshakes `auth_replay`, `auth_capacity`, `amplification`, `bad_proof`; datagram rx `bad_tag`, `unknown_session`, `malformed`; every
`dv_edge_link_refused_total`; `dv_launcher_child_restarts_total` of every child; a down row of `dv_status_process_up`. (NODE1g-h allows the restarts of `dv-edge-link` and `unknown_session` datagrams: the case
restarts that edge twice on purpose.)

Not asserted, on the contract's advice: whether a new handshake happened at exactly 30 s (a race, section 3); a count of refusals (a rate only, section 6 item 4); `dv_room_rts_total{result=link_down}` (that is the
fabric link to the room controller, not the node link).

## Results (run `20261123T225943Z` unless another run is named)

| Case | Result | Key numbers |
|------|--------|-------------|
| 0 | pass | both rows connected, the central node sees the hotspot at `172.30.0.98` (the router), `handshakes` welcomed 1 / accepted 1, an over each way heard whole |
| a | pass | rows `rtt_ms` 129 (central) / 130 (hotspot) for 2 x 60 ms; overs heard 99.0 / 99.2 percent; first frame 160 ms after sending (100 ms without delay, case b); no handshake |
| b | pass | overs heard 96.4 / 97.6 percent (3 percent loss each way), `loss_ppm` 21645 (central) / 18998 (hotspot); no handshake, link stayed up 60 s |
| c | pass | blackout 20.0 s: no handshake; the first probe over was heard 0.1 s after the end (survived: no relink needed); floors free |
| d | pass | 42 s into a 60 s blackout: central `disconnected`, hotspot `reconnecting`, `last_error` `timeout` on both, no session on either hub; after the end `welcomed` +1, `accepted` +1; time to relink (first probe over heard after the end): **0.68 s** (this run; 1.85 s and 0.68 s in the earlier runs) |
| e1, e2 | pass | the last Unit reached the receiving node 0.0 s after the cut; the 3.0 s probe over was heard 100 percent (floor free); the over in flight was heard 25 percent (the 3 s before the cut of 12 s); `end_missing` +1 on the receiving node's room (C for e1, B for e2); no handshake; first probe over after the end 0.1 s; FINDING 2 |
| e3, e4 | pass | probe at 1.0 s after the cut: not heard (0 percent, floor held); probe at 2.0 s: heard 100 percent (floor free) in both directions: the floor was freed between 1.0 s and 2.0 s after the last Unit, the contract says 1.6 s; the 3.0 s probe 100 percent |
| f | FAIL (FINDING 1) | `20261123T231838Z`: port rebinding: the first probe over after the way back 0.11 s (hotspot to central), 4.78 / 4.19 / 2.45 s (central to hotspot; limit 5.2 s); no handshake; central row follows the new port; but 2 of 3 central-to-hotspot overs were heard 38 percent (cut) |
| g | FAIL (FINDING 1) | `20261123T231838Z`: address rebinding: first datagram of the hotspot from the new address 0.058 s (h2c); 3.307 s, 0.224 s, 4.914 s (c2h); the central node sent to the new address 0.007 s later each time; 2 of 3 central-to-hotspot overs cut (heard 38 percent), the one with 0.224 s survived (98 percent); first probe over after the way back 0.32 / 4.41 / 2.66 / 4.98 s |
| h | pass | Reject{auth} rate **1.021 per second** over 110 s (112 refusals; 1.027, 1.021 and 0.998 in the other three runs), both rows `refused_key` all the time (the hotspot's `reconnecting`, the central node's `disconnected`), no session, no accepted; just after the reload the central row is `disconnected` and the hotspot still shows `connected`; key fixed: first probe over heard **7.85 s** after the fix (`20261123T231838Z`; 9.04 s, 6.11 s, 2.61 s in the other runs), one handshake each side |
| j | pass | ten faults: central node processes 48 -> 48 pids, `dv-edge-link` threads 4 -> 4, fds 17 -> 17, no restart, `overs.C.complete` +40 and `overs.B.complete` +40 (overs resumed after every fault), one session per hub |
| m | FAIL (FINDING 2) | `20261123T234228Z`: 8 isolated overs cut by a blackout: 6 `end_missing`, 2 `complete` (hotspot to central, link streams 2 and 3, 151 units each with no End); the capture judge agrees with the other six |
| k | skip | `DVT_LINKOBS1=on` is not set |

## Findings

1. **A NAT rebinding in the middle of an over from the central node to the hotspot cuts that over in about two cases out of three (NODE1g-f, -g; runs `20261123T225943Z` and `20261123T231838Z`, two of three overs
   each time, both NAT changes).** The central node moves the session as soon as a valid datagram from the new source arrives (0.007 to 0.019 s after the hotspot's first datagram: router evidence, address change), and that
   datagram is the hotspot's next keepalive, which comes 0.2 to 5.0 s after the change (observed 0.224, 0.308, 3.3, 3.5, 4.9 and 5.0 s). Until then the central node sends the over's units to the old
   mapping (166 to 250 datagrams to the old address in the cut overs, router evidence `to_old`). A gap longer than 1.6 s ends the stream at the hotspot's link edge by silence (the room folds the over `end_missing`/`lossy`), and the
   units that follow once the session has moved are refused as late datagrams of a closed over: the rest of the over is never heard (heard 38 percent of an 8 s over rebound at 3 s; the heard part stops at the
   change). The contract's "media gap at most 5 s" holds for the transport (largest first-datagram time 5.0 s, first probe over after a change within 4.98 s) but a perceivable gap above 1.6 s costs the whole
   remainder of an over. The hotspot-to-central direction is not affected (the hotspot sends continuously: first datagram 0.04 to 0.06 s after the change, the over heard 100 percent). Contract section 3 names the
   delay (the hotspot sends again within 5 s, sooner on a Demand change or StreamStart) but not the 1.6 s rule that turns it into a lost over; a hotspot datagram on receiving the first Unit that comes from a new
   address, or a central node that also tells the hotspot, would remove it. The cases keep the contract numbers (gap 5 s, 90 percent of the over) and fail. dv-router accepted it as a product defect (**LINKREBIND1**: while it receives a stream the hotspot sends a keepalive at
   most every 1 s). `DVT_LINKREBIND1=on` (default off, a future image) makes f and g assert the post-fix numbers as well: central-to-hotspot gap below 1.2 s, every over heard at least 97 percent, no cut over in
   any trial.
2. **An over cut by a blackout is folded `complete` although its End never arrived (a defect by dv-router's rule: `complete` only when the End arrived AND no unit was lost).** Eight such overs in the first
   four runs (no capture): six `end_missing`, two `complete` (e2 of `20261123T220327Z`, room B of the hotspot; e1 of `20261123T224316Z`, room C of the central node). By construction the End of a 12 s over is sent 9 s
   into a 20 s blackout, so it cannot have crossed. Reproduced with captures in **`20261123T234228Z`** (case m, four isolated overs per direction, the receiving room's hub delta is exactly this over): hotspot to
   central, **link stream 2 (m1) and link stream 3 (m2) folded `complete`** on the central node's room C; streams 4 and 5 (m3, m4) `end_missing`; central to hotspot four of four `end_missing`. The capture of the central
   node for streams 2 and 3: 151 units (positions 0 to 150), none missing, the last one 0.01 s before the cut, **no StreamEnd record at all** (it is lost in the blackout; the edge ends the stream by silence), so
   the rule says `end_missing`. `python3 -m dvtest.node1g_capture out/20261123T234228Z` prints `DEFECT` for m1 h2c and m2 h2c and `agree` for the other six. The pattern fits an edge that closes a silent stream with a
   summary that counts every unit it got as the expected number (`lost` 0) and so makes the over `complete`; the first two overs of the run were folded that way, the later ones not. NODE1g-e1 / -e2 keep asserting
   `end_missing` (the capture rule gives `end_missing` for them as well: End in the blackout), so a repeat of the intermittent fold fails them.
3. Observation (case a, run `20261123T234228Z`, delay only, 10 s over each way; captures of both nodes): the 10 ms jitter reordered datagrams. The central node received 525 datagrams of which **12 arrived
   out of order** (their session counter below the highest seen: back by up to 4 positions, late by up to 14 ms after the datagram that overtook them, 7 ms on average) and **4 of 505 units were out of order**
   (back by up to 4 positions = 80 ms); the hotspot received 525 datagrams, 7 out of order (up to 4 back, up to 11 ms late, 3 ms on average), 2 of 505 units out of order. The counters count them late, not lost:
   central node `dv_room_over_units_total{room=C,result=late}` +4 and `dv_edge_over_units_total{proc=link,dir=rx,result=late}` +4, lost 0, gap 0, one over `lossy`; hotspot room B late +1, edge late +1, lost 0,
   gap 0, one over `lossy`. The late units were not played: the listeners heard 99.2 percent (4 of 500 frames missing each way). The rows' `loss_ppm` (central 22058, hotspot 13157) is the share of datagrams that arrived
   out of order (12 / 525 = 22857 ppm, 7 / 525 = 13333 ppm): the row estimate counts a reordered datagram as a gap and never subtracts it (status.md), while the room and the edge count it late. At the hotspot the
   numbers do not add up: 2 units out of order in the capture, 1 counted late, 4 frames missing at the listener (the central node: 4, 4, 4); the hotspot's missing frames are not all explained by its late counter.
4. Observation: after the old key is put back on the central node the hotspot's link was up 2.61, 6.11, 7.85 and 9.04 s later (four runs): the hotspot is in an attempt that runs to its 5 s deadline or in a
   backoff of up to 6 s; the worst case of the two added is 11 s, above the 10 s used for the blackouts (the contract has no number for this case). No assertion above 10 s was needed in these runs.

Intermittent harness problems found on the way and fixed (not findings): a key held in a fixture namespace was printed by pytest (`run.sh` found it by its scan: now a `KeyBox`), the central node's link counters
restart with the edge on a reload (delta against zero: `rebase`), the media gap was measured between arrived frames only (a lost tail did not count: now the longest run of missing frames), the central row was waited
for by port only (an address change keeps the port), `--down` did not know the profile `node1g`.

### LINKOBS1 (a future image): RECORD-only

| Item | Today | Where it is recorded |
|------|-------|----------------------|
| a refusal log and counter on the hotspot | the hotspot only sets `last_error` `refused_key` (a Reject is a hint); NODE1g-h asserts that | NODE1g-k notes the series names |
| a session-end counter with reasons | none; reconnects are derived from `handshakes{welcomed}` (NODE1g-d asserts +1) | NODE1g-k |
| `Bye{revoked}` | `ByeReason::Revoked` is never sent; a rekey shows as `Bye{reload}`: NODE1g-h records the rows right after the reload (central `disconnected`, the hotspot still `connected`, then `refused_key`; no `closed_by_peer` was seen) | NODE1g-h |
| a bad-source counter | a reply from a changed CENTRAL source is dropped without a count (section 3 end, 6 item 8) | not built: moving the central side's source needs a second central address; GAP |

## The next image: LINKFOLD1, LINKSEQ1 and LINKREBIND1 (asserts prepared, not run)

dv-router opened LINKFOLD1 (FINDING 2) and LINKSEQ1 (the reordering of case a) and carries them with LINKREBIND1 (FINDING 1) in one image. Three gates, all default off (the current behaviour is kept):

| Gate | Row | What it turns on |
|------|-----|------------------|
| `DVT_LINKREBIND1=on` | LINKREBIND1 | NODE1g-f and -g also assert: central-to-hotspot gap below 1.2 s, every over heard at least 97 percent, no cut over in any trial (the contract asserts stay on regardless) |
| `DVT_LINKSEQ1=on` | LINKSEQ1 | NODE1g-a (delay only, 60 ms +- 10 ms) also asserts, in the case: (a) both overs heard 100 percent; (b) accounting: the frames the listeners miss, both directions, are at most the sum of the late / lost / gap / dropped unit counters of both nodes (the room's `dv_room_over_units_total` and the edges' `dv_edge_over_units_total`); (c) each row's `loss_ppm` is within 5000 ppm of the missing-unit share of the direction it receives (zero missing gives about 0), no longer the share of reordered datagrams. The host step then checks the lateness of every reorder against the capture: the link edge's late units must not exceed the number of units reordered by more than 40 ms (a reorder inside 40 ms is resequenced and played, so late 0 for it); units later than 40 ms may stay late |
| (LINKFOLD1) | LINKFOLD1 | no gate: NODE1g-e1 and -e2 already assert `end_missing` for an over whose End was lost; the opt-in case m (`DVT_NODE1G_FOLD=<n>`) and the capture judge repeat the check with isolated overs |

The exact command for the next run (one run, about 34 minutes: 21 for the cases, 6 for case m with `DVT_NODE1G_FOLD=4`, the rest rig start and stop; with `--capture` the wire check at the end fails as it did in
`20261123T234228Z`, which is expected for the hotspot's inside LAN and does not change the pytest result):

```
XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 \
DVT_NODE1G=on DVT_NODE1G_FOLD=4 DVT_LINKREBIND1=on DVT_LINKSEQ1=on \
scripts/run.sh -m rig --capture --profile twonode -- cases/test_node1g.py -rA
python3 -m dvtest.node1g_capture out/<run>
```

To stay under 30 minutes split it: the same command with `-k "not node1g_m"` (and without `DVT_NODE1G_FOLD`), and a second run with `DVT_NODE1G_FOLD=4 ... -k "node1g_a or node1g_m"` (about 8 minutes; case a is in it so that
its capture window is judged too). The host step prints, per window, the fold the rule gives against the fold the hub counted (`DEFECT` names the link stream id), and for case a the reordering with the lateness of
every reordered unit and the LINKSEQ1 check. It exits 1 on any defect.

## Not built

* Rebinding DURING the first 5 s of the hotspot's start (section 6 item 7: one failed attempt, then success): the hotspot cannot be restarted through the control channel (`dvr-peer` is not in the
  restart allow-list, and the allow-list was not extended).
* A change of the CENTRAL side's source address (section 3: "the hotspot silently drops its replies, the session dies at 30 s"): the rig has one central address.
* The hotspot's own process counters (`proc-stat` is read-only for `xlxd-sut` only): the thread / fd / pid evidence of NODE1g-j is the central node's.
* The key is rotated on the central node only (the hotspot keeps the old one and the fix puts the old key back on the central node): `hup` exists for `xlxd-sut` alone.
* No capture (`--capture`): the wire checker does not know the WAN router; the evidence is the hubs, the admin rows and the router's own counters.

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-42d5473 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 \
DVT_NODE1G=on scripts/run.sh -m rig --profile twonode -- cases/test_node1g.py -rA
```

About 21 minutes (a single case: `-k node1g_c`; the cases need their own rig start of about 3 minutes). `DVT_TRANSCODER_FAKE=1` is not needed (no transcoder is involved; `transcoder-peer` starts unused).
The investigation of FINDING 2 and the reordering of case a (about 7 minutes, with the wire check of `--capture` failing as it does for any rig with a host outside `<net>`: the hotspot's inside LAN):
```
... DVT_NODE1G=on DVT_NODE1G_FOLD=4 scripts/run.sh -m rig --capture --profile twonode -- cases/test_node1g.py -rA -k "node1g_a or node1g_m"
python3 -m dvtest.node1g_capture out/<run>
```
Checks without a rig: `python3 -m pytest -m unit cases/unit/test_node1g_lib.py`; both configs against the image:
`python3 -m dvtest.tools.config_check --image <image> --template rig/config/dv-router/node1g-hotspot.toml --gen compat1 --net 172.31.0 --own 16 --central-ip 172.30.0.11` (and the central node's
template with `--extra rig/config/dv-router/node1g-central.toml`).
