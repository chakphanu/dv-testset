<!-- SPDX-License-Identifier: MIT -->
# DV-Router AllStar edge against the asl-lab (ASL1 phase 2)

`dv-edge-allstar` running inside the asl-lab (`docs/asl-lab.md`), profile `asl-edge`: the edge is node 1999,
room L, next to the lab's app_rpt nodes A (199901), B (199902) and E (299901). Cases:
`cases/test_asl_edge.py`, marker `asl_lab`. Interop testing of builds under review: findings go to dv-router as
pcap and note, never as patches to their source.

## Run

```
DVT_ASL_EDGE_OUT=1 DVT_ASL_EDGE_LOOKUP=1 XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter \
  DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<dv-router.toml template> \
  scripts/run.sh --profile asl-edge -m asl_lab -- cases/test_asl_edge.py
# + DVT_ASL_LONG=1 for T1 (10 min hold) and T2 (3 min over, TOT)
```

Three rig variants, because the settings that matter are start-up configuration of the edge:

| Variant | Environment | Cases |
|---------|-------------|-------|
| main | `DVT_ASL_EDGE_OUT=1` (+ `DVT_ASL_EDGE_LOOKUP=1` on images that accept it) | everything except T4b and R1-R7 |
| no outbound rows | `DVT_ASL_EDGE_OUT` unset | `-k t4b` (B and E are not linked to the edge, so app_rpt has no way to see the edge through them) |
| registration | `DVT_ASL_EDGE_REG=1`, no outbound rows | `-k "test_r1 or ... or test_r7"` (own rig: the cases look at the request cadence since the edge started) |

Rig details are in `docs/asl-lab.md` ("Profile asl-edge"). `rig/config/dv-router/allstar-lab.toml` is the base
config; `-out`, `-lookup` and `-reg` snippets are added by `run.sh` from the flags above. The stand-in registrar
(`rig/asl-lab-registrar.py`, plain HTTP on asl-reg:8082) answers six public nodes one way each (1991 ok, 1992 other
port, 1993 HTTP 500, 1994 200 without the success text, 1995 reply after 20 s, 1996 reply over 4 KiB) and logs every
request in full to `out/<run>/asl-lab/register/requests.jsonl`. Fixture secrets only (`test-secret-not-real`).

Images (all local, none pulled): **c199** = `dv-router:asl-c199` (the tag was removed afterwards), **c199b** =
`dv-router:asl-c199b` (the fix image: listener egress clock, `[edges.allstar.lookup]`, register server/insecure keys,
monitor listen-only, loop hold-down). A column for `dv-router:wip-7efdd33` is added when its run is done.

## Results

Result codes: pass, FAIL, XFAIL (an expected failure that documents a finding, marker removed on c199b), - (not run
on that image). "probes" is the attempt count of the working-over probe (`hear_tone`: a 3 s tone, retried every 45 s,
at most 6 times, because of finding 1); 1 means the first over was heard. Host load average (1 min) at the start
of each case is in `out/<run>/asl-lab/loadavg.jsonl`; cases run under load above 6 are flagged `L`.

| Case | What it checks | c199 | c199b | Out dir (c199b) / pcap | Notes |
|------|----------------|------|-------|------------------------|-------|
| T1 | node A dials the edge, 10 min hold, audio edge to client at start and end, PING/PONG through the hold | pass (probes 2, 1) | pass (probes 1, 1) | `20261103T071151Z/asl-lab/t1-hold/edge.pcap` (480 KB) | load 6.8 at start, 2.2 at end |
| T1b | the other direction: client keys, edge relays to A | FAIL (6 probes, over cut) | pass (rerun in its own run after a test fix) | `20261103T115747Z/asl-lab/t1b-client-to-node/` | the first c199b try failed in the test (it looked at RPT_RXKEYED), not in the edge: now read from the capture |
| T2 | one 190 s over; TOT at 180 s | FAIL (probes 4, then 0 s of audio) | pass: 179.0 s of audio, over 193 s, probes 1 | `20261103T071151Z/asl-lab/t2-tot/edge.pcap` (6.2 MB) | TOT cut at about 180 s as designed |
| T3 | hub of A, B and E: per-destination L lists | pass | pass | `.../t3-hub/` | `L T199902,R299901` to A, `L R299901,T199901` to B, `L T199902,T199901` to E: split horizon and T/R letters correct |
| T4 | A, B each dial the edge and A dials B directly | pass | pass | `.../t4-loop/` | app_rpt refuses the third link itself (`T 199901 REMALREADY`) before any packet leaves A: its DNS lookup of B is slower than the edge's L list reaching A. The edge's own guard is not what acts here: see T4b |
| T4b | loop through the edge: chain A-E-B, then A and B dial the edge together | - | pass | `20261103T120058Z/asl-lab/t4b-loop/edge.pcap` | edge drops the youngest branch: see "Loop guard" below |
| T5 | wrong source address for a statically known node: ACCEPT then HANGUP, no ANSWER; right address answered | pass (2 cases) | pass (2 cases) | (no capture; checked from the client's side) | identity check as designed |
| T6 | outbound rows: permanent transceive to B, monitor to E, established after an edge restart | pass | pass | | |
| T6m | monitor row is listen only: no voice, no RADIO_KEY to E while the room talks | XFAIL (finding 3) | pass | `.../t6-monitor/` | fixed in c199b |
| T7 | edge restart (SIGUSR1) under A's link: A's link comes back | pass | pass | | launcher backs off 500 ms, 1 s, ... between consecutive restarts of the edge child: a kill-proc in the gap gets "error 3" (the cases keep 3 s apart) |
| T8 | asl-b container restart: the edge redials B | pass | pass | | |
| T8b | HANGUP versus DISCONNECT on the permanent row | - | pass | `20261103T115747Z/asl-lab/t8b-disconnect/edge.pcap` | **answer to the DISCONNECT question below** |
| T9 | edge egress 80 ms +/-20 ms and 5 % loss: link and tone survive | pass (probes 2) | pass (probes 1) `L` | `.../t9-netem/` | netem reset afterwards (always, in `finally`) |
| T10 | client account `acct`: md5, call token exchange, RADIO_KEY, no REGREQ | pass | pass | `.../t10-client/` | |
| T11 | 60 NEW and 20 POKE from one address in 1.2 s: same pid, cpu bounded, a real node still links | pass | pass `L` | | no source-address spoofing was attempted (raw sockets are not available to the tester); the per-IP limits are what one address triggers |
| back-to-back | three overs 1 s apart each reach the client | XFAIL (finding 1) | pass | | |
| restart x5 | 5 x SIGUSR1, one over after each, no probe retry | - | pass | | |
| R1 | success: shape, one node per request, refresh at 60 s | - | pass | `20261103T120131Z/asl-lab/register/requests.jsonl` | gap 60.0 s |
| R2 | success with another port: mismatch counted | - | pass | | refresh 60 s |
| R3 | HTTP 500 retried with backoff | - | pass | | first retry 24.1 s (30 s -20 %) |
| R4 | 200 without "successfully registered" is a failure | - | pass | | retry 26.4 s |
| R5 | reply after 20 s: client times out | - | pass | | next request 47.8 s after the first |
| R6 | reply over 4 KiB refused | - | pass | | retry 25.2 s |
| R7 | `dv_edge_allstar_register_total{result}` counts ok, mismatch and the failures | - | pass | `.../register/metrics.txt` | |

Load: the first 12 c199b cases ran at load 6.5 to 7.5 (dv-router's merge worker); all passed, so no case was rerun for load.
The c199 run's load was not recorded (the recorder was added afterwards).

### DISCONNECT on a permanent row (T8b, c199b)

Design 3.5: the edge must never redial after a received `!!DISCONNECT!!`, also on a permanent outbound row.
Observed (`t8b-disconnect/edge.pcap`):

- B drops the edge's call with a plain channel hangup (no DISCONNECT text): app_rpt keeps the link entry for 10.2 s,
  and the edge **redials**; B shows the link again 14.2 s after the hangup.
- B disconnects with `rpt fun *6` (`!!DISCONNECT!!` text, then HANGUP): **no NEW from the edge in the next 70 s**,
  as designed. Redial yes after HANGUP, no after DISCONNECT. (My earlier summary of the c199 run said the edge
  "does not honour DISCONNECT"; that wording was wrong: the observation was that it does not redial.)

### Loop guard (T4b, c199b)

`t4b-loop/edge.pcap`: A and B dial the edge in the same instant while A-E-B is linked. At t=0.040 A's L list
(`L T299901,T199902`) names B behind A; the edge sends `!!DISCONNECT!!` to B at 0.040 and HANGUP at 0.045 (the
youngest branch), within 0.1 ms of the list arriving. B (app_rpt) retries 10 s later: NEW, CALLTOKEN, NEW, ACCEPT, then
HANGUP at once: the hold-down refuses it. Observation worth a look: at 10.2 s A's list (`L T299901,T199902,T1999`,
now naming the edge itself after B's retry) makes the edge disconnect **A too**, in the middle of A's over (the client
heard 1.26 of 3 s). No audio was forwarded twice (B never sent voice into the edge).

## Findings (c199)

1. **Overs cut after the grant.** The edge granted an over and ended it at once (RADIO_KEY then RADIO_UNKEY to the
   listeners 0.1 ms later, no voice), intermittently, for about 3 minutes after an earlier over or an edge restart
   (clears after about 190 s idle). Evidence: `20261103T060725Z/asl-lab/b-callee/edge.pcap` frames 865-870,
   `20261103T055113Z/asl-lab/dbg*`. Fixed in c199b (back-to-back and restart x5 pass).
2. `[edges.allstar.lookup]` rejected at config load ("unknown field `lookup`"). Added in c199b.
3. A monitor-mode row received room audio. Fixed in c199b.

## Notes for the lab itself

- `lab.quiet()` (`rpt fun *6`) sends DISCONNECT to the edge, which then **stops redialling that permanent row** until
  the edge restarts (T8b confirms this is by design): cases that follow a quiet() restart the edge first.
- app_rpt runs `rpt fun *6` late (a call placed right after it is torn down); `Lab.quiet()` waits 1.2 s.
- Never edit `scripts/run.sh` while a run is going: bash reads the script as it executes (the c199 run printed
  `asl-e: command not found` and exit 127 at its end for that reason; the pytest results were complete).
- The tester cannot reach the control channel after its own `run.sh` pytest has ended, even under `--keep`.

## Not covered

- T5 with a real NAT in the path; T11 with real source-address spoofing (see the table).
- DNS lookup of nodes through the lab zone by the edge: static rows cover every node a case dials; the
  `-lookup` snippet only sets the domain. A lookup case without static rows is open.
