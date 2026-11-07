<!-- SPDX-License-Identifier: MIT -->
# DV-Router AllStar edge against the asl-lab (ASL1 phase 2)

`dv-edge-allstar` running inside the asl-lab (`docs/asl-lab.md`), profile `asl-edge`: the edge is node 2999 (1999 until wip-1590f26),
room L, next to the lab's app_rpt nodes A (199901), B (199902) and E (299901). Cases:
`cases/test_asl_edge.py`, marker `asl_lab`. Interop testing of builds under review: findings go to dv-router as
pcap and note, never as patches to their source.

**Renumbered for wip-1590f26 (2026-11).** That build refuses `[[edges.allstar.node]] public = true` with a number below 2000 ("public node
numbers must be at least 2000; private numbers cannot be registered") and a `[edges.allstar.register] interval` below 120 s. The DV-Router lead
decided to keep the nodes public (so the public-node path stays tested: registry identity, caller IP check) and not to switch to
`public = false`. The lab therefore uses the edge node **2999** (was 1999), the six registration nodes **2991..2996** (were 1991..1996, same
answer variants one to one) and the register interval **"120s"** (was "60s"); the lab registrar is our own fake, so any number works, and
numbers from 2000 up are valid on older images too. The app_rpt lab nodes 199901 / 199902 / 299901 / 299902 are unchanged. Everything below
that was recorded before this change (runs up to `20261104T001310Z`, the tables, the F-R1 finding, the DVSwitch recordings) names the
old numbers (1999, 1991..1996, T1999) and the 60 s timing on purpose: they are results of those runs, not of the current config. The registration cases
now derive their expectations from the rendered config: slot spacing S = 120 / 7 = 17.14 s (node k first at start + k * S), a successful
refresh max(60, min(120, 180)) = 120 s (window 110..150 s, `asl_register.refresh_gap`), the fixture waits up to max(240 s, 2 * interval + 60 s)
and for a span of interval + 15 s; the failure back-offs (24..36 s, slow reply 44..56 s) do not depend on the interval. The R1-R7 run is
longer (about 3 minutes of waiting instead of 100 s). Not yet run on a rig with the new numbers. Guard: `cases/unit/test_asl_lab_node_numbers.py`.

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
(`rig/asl-lab-registrar.py`, plain HTTP on asl-reg:8082) answers six public nodes one way each (2991 ok, 2992 other
port, 2993 HTTP 500, 2994 200 without the success text, 2995 reply after 20 s, 2996 reply over 4 KiB; 1991..1996 before wip-1590f26) and logs every
request in full to `out/<run>/asl-lab/register/requests.jsonl`. Fixture secrets only (`test-secret-not-real`).

Images (all local, none pulled; the DVSwitch Mobile app run is described in [asl-dvswitch.md](asl-dvswitch.md)): **c199** = `dv-router:asl-c199` (the tag was removed afterwards), **c199b** =
`dv-router:asl-c199b` (the fix image: listener egress clock, `[edges.allstar.lookup]`, register server/insecure keys,
monitor listen-only, loop hold-down). `wip-7efdd33` = the ASL1f fix image (its column is partial: that run was aborted when `wip-71ab207` arrived, everything before restart x5 had passed, `20261103T183151Z`); `wip-71ab207` = the overnight image (ACL0a/b, TOTP1, I18N1/b, IDS1, ECHO1c, ASL tests).

## Results

Result codes: pass, FAIL, XFAIL (an expected failure that documents a finding, marker removed on c199b), - (not run
on that image). "probes" is the attempt count of the working-over probe (`hear_tone`: a 3 s tone, retried every 45 s,
at most 6 times, because of finding 1); 1 means the first over was heard. Host load average (1 min) at the start
of each case is in `out/<run>/asl-lab/loadavg.jsonl`; cases run under load above 6 are flagged `L`.

| Case | What it checks | c199 | c199b | wip-7efdd33 (partial) | wip-71ab207 | wip-c165c93 | Out dir (c199b) / pcap | Notes |
|------|------|------|------|------|------|------|------|------|
| T1 | node A dials the edge, 10 min hold, audio edge to client at start and end, PING/PONG through the hold | pass (probes 2, 1) | pass (probes 1, 1) | pass | pass | pass (611.9 s, `20261103T232753Z`, DVT_ASL_LONG=1) | `20261103T071151Z/asl-lab/t1-hold/edge.pcap` (480 KB) | load 6.8 at start, 2.2 at end |
| T1b | the other direction: client keys, edge relays to A | FAIL (6 probes, over cut) | pass (rerun in its own run after a test fix) | pass | pass | pass | `20261103T115747Z/asl-lab/t1b-client-to-node/` | the first c199b try failed in the test (it looked at RPT_RXKEYED), not in the edge: now read from the capture |
| T2 | one 190 s over; TOT at 180 s | FAIL (probes 4, then 0 s of audio) | pass: 179.0 s of audio, over 193 s, probes 1 | pass | pass | pass (202.1 s, `20261103T232753Z`, DVT_ASL_LONG=1) | `20261103T071151Z/asl-lab/t2-tot/edge.pcap` (6.2 MB) | TOT cut at about 180 s as designed |
| T3 | hub of A, B and E: per-destination L lists | pass | pass | pass | pass | pass (OUT=1) | `.../t3-hub/` | `L T199902,R299901` to A, `L R299901,T199901` to B, `L T199902,T199901` to E: split horizon and T/R letters correct |
| T4 | A, B each dial the edge and A dials B directly | pass | pass | pass | pass | pass (OUT unset, `20261103T231326Z`) | `.../t4-loop/` | app_rpt refuses the third link itself (`T 199901 REMALREADY`) before any packet leaves A: its DNS lookup of B is slower than the edge's L list reaching A. The edge's own guard is not what acts here: see T4b |
| T4b | loop through the edge: chain A-E-B, then A and B dial the edge together | - | pass | superseded (aborted for wip-71ab207) | pass | pass (OUT unset, `20261103T231326Z`; skips with OUT=1 by design) | `20261103T120058Z/asl-lab/t4b-loop/edge.pcap` | edge drops the youngest branch: see "Loop guard" below |
| T5 | wrong source address for a statically known node: ACCEPT then HANGUP, no ANSWER; right address answered | pass (2 cases) | pass (2 cases) | pass | pass | pass (2 cases) | (no capture; checked from the client's side) | identity check as designed |
| T6 | outbound rows: permanent transceive to B, monitor to E, established after an edge restart | pass | pass | pass | pass | pass (OUT=1) | | |
| T6m | monitor row is listen only: no voice, no RADIO_KEY to E while the room talks | XFAIL (finding 3) | pass | pass | pass | pass (OUT=1) | `.../t6-monitor/` | fixed in c199b |
| T7 | edge restart (SIGUSR1) under A's link: A's link comes back | pass | pass | pass | pass | pass | | launcher backs off 500 ms, 1 s, ... between consecutive restarts of the edge child: a kill-proc in the gap gets "error 3" (the cases keep 3 s apart) |
| T8 | asl-b container restart: the edge redials B | pass | pass | pass | pass | pass (OUT=1) | | |
| T8b | HANGUP versus DISCONNECT on the permanent row | - | pass | pass | pass | pass (OUT=1) | `20261103T115747Z/asl-lab/t8b-disconnect/edge.pcap` | **answer to the DISCONNECT question below** |
| T9 | edge egress 80 ms +/-20 ms and 5 % loss: link and tone survive | pass (probes 2) | pass (probes 1) `L` | pass | pass | pass | `.../t9-netem/` | netem reset afterwards (always, in `finally`) |
| T10 | client account `acct`: md5, call token exchange, RADIO_KEY, no REGREQ | pass | pass | pass | pass | pass | `.../t10-client/` |  |
| T11 | 60 NEW and 20 POKE from one address in 1.2 s: same pid, cpu bounded, a real node still links | pass | pass `L` | pass | pass | pass | | no source-address spoofing was attempted (raw sockets are not available to the tester); the per-IP limits are what one address triggers |
| back-to-back | three overs 1 s apart each reach the client | XFAIL (finding 1) | pass | pass | pass | pass | | |
| restart x5 | 5 x SIGUSR1, one over after each, no probe retry | - | pass | superseded (aborted for wip-71ab207) | pass | pass | | |
| R1 | success: shape, one node per request, refresh at 60 s | - | pass | superseded (aborted for wip-71ab207) | pass | pass (fresh rig `20261104T001310Z`; the first try `20261103T232417Z` failed on a lab artifact, F-R1) | `20261103T120131Z/asl-lab/register/requests.jsonl` | gap 60.0 s |
| R2 | success with another port: mismatch counted | - | pass | superseded (aborted for wip-71ab207) | pass | pass | | refresh 60 s |
| R3 | HTTP 500 retried with backoff | - | pass | superseded (aborted for wip-71ab207) | pass | pass | | first retry 24.1 s (30 s -20 %) |
| R4 | 200 without "successfully registered" is a failure | - | pass | superseded (aborted for wip-71ab207) | pass | pass | | retry 26.4 s |
| R5 | reply after 20 s: client times out | - | pass | superseded (aborted for wip-71ab207) | pass | pass | | next request 47.8 s after the first |
| R6 | reply over 4 KiB refused | - | pass | superseded (aborted for wip-71ab207) | pass | pass | | retry 25.2 s |
| R7 | `dv_edge_allstar_register_total{result}` counts ok, mismatch and the failures | - | pass | superseded (aborted for wip-71ab207) | pass | pass | `.../register/metrics.txt` |  |
| DVSwitch Mobile 2.0.7, radio mode (`tools/dvswitch_radio_case.py`) | the real Android app connects, keys, hangs up; the edge relays its voice to A | - | - | pass (by hand) | pass | pass (radio mode, and phone mode via `tools/dvswitch_phone_case.py`, `out/20261103T223757Z`) | `20261103T192450Z/asl-lab/dvswitch/dvswitch-radio-mode-wip71ab207c/` | `docs/asl-dvswitch.md`: phone mode still needs REGACK; radio mode works |

wip-71ab207 runs: main `20261103T184952Z` (17 passed, 8 skipped by design, 17:55 min: T4b and R1-R7 need other rig
variants), T4b `20261103T190801Z`, R1-R7 `20261103T190835Z`, all passed; the edge started normally (no ACL0a fabric
listen refusal, the override was not set). Loadavg per case in `20261103T184952Z/asl-lab/loadavg.jsonl`: the first
cases ran at 16-17 (merge worker), the timing-sensitive T9 at 5.4, T11 at 5.0 and the 10 minute hold at 4.8, so
nothing was rerun for load.

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

## External dial T12 (ASL1b)

The only test that leaves the rig. The user authorised exactly this, in the main session: register the real node
400169 (XLX345's node number) with the real AllStarLink registry and dial XLX345 module A (node 400161) and module H
(node 400168), listen only. Profile `asl-ext`, service `asl-ext` (the lab image, role `node-ext`), case
`cases/test_asl_ext.py`, run `20261103T182029Z` (UTC 2026-11-03).

```
DVT_ASL_EXT_GO=400169 scripts/run.sh --profile asl-ext -m asl_ext -- cases/test_asl_ext.py     # about 4.5 minutes
```

### How the node is built and fenced

| Point | What was done | pass | pass |
| -------|--------------- | --- | --- |
| node | ASL3 Asterisk 22.10.1 (`2:22.10.1+asl3-3.10.8-1.deb13`), app_rpt, node 400169 (`rpt.conf`: `node_lookup_method = dns`, the default domain `nodes.allstarlink.org`), `chan_usrp` to a dummy local address (no radio), `idrecording` empty, telemetry 0, no statpost | pass | pass |
| no transmit path | no tone playback, no MixMonitor, no dialplan beyond hang-up; functions 1 (disconnect), 3 (connect) and 6 (disconnect all) only; no IAX2 user or account at all (an inbound NEW is refused) | pass | pass |
| registration | the ASL3 way: `res_rpt_http_registrations` (HTTPS to `register.allstarlink.org`, `register => 400169:<password>@register.allstarlink.org`, interval 180 s), not IAX2 `register =>` | pass | pass |
| password | a host file (`.local/secrets`, dir 0700, file 0600), mounted read-only into the container; `rig/asl-lab-entry.sh` writes the registration line into a root-only (0600) staged file inside the container; the agent moves it into `rpt_http_registrations.conf` (0600, owner asterisk) when the case asks, so the node does not register at start. It was never an argument, an environment variable or a log line; `scripts/run.sh` scans the whole output directory for it after every asl-ext run (`dvtest.secretscan`, paths only, exit 5 on a hit): clean | pass | pass |
| network | `asl-ext` is on the internal `rig` network (the tester's agent calls) and on `asl-ext-out`, a plain bridge network (docker NAT, nothing published); every other host stays internal; normal runs never create the network. The profile needs `DVT_ASL_EXT_GO=400169`, must be the only profile, and checks the file modes | pass | pass |
| limits | `limits-medium` (512 MiB, 2 cpus, 512 pids) inside the `dvt-rig.scope` of `scripts/run.sh` | pass | pass |
| capture | `tcpdump` on the default-route interface, filter `udp or tcp port 443 or tcp port 53`: IAX2 and the TLS flow to the registry (the docker resolver is not on eth0, so DNS is not in the pcap: the agent asks the same resolver itself and the case records SRV, A and TXT of each node in `dns.jsonl`) | pass | pass |
| limits of the test | at most two attempts per node, 60 s apart (not needed), one rig, no loop; `out/<run>/asl-ext/stop` ends it at the next check | pass | pass |

### Timeline (UTC, 2026-11-03)

| Time | Event | pass | pass |
| ------|------- | --- | --- |
| 18:20:31.8 | case starts, capture running; 18:20:31.9 registration switched on (module reload with the real line) | pass | pass |
| 18:20:33.9 | **registered**: the module's own table (`rpt show registrations`) says `Registered`, 2.0 s after the request, refresh 179 s; the registry reported the node's public address with port 4569 (the node's IAX2 bindport, sent in the request) | pass | pass |
| 18:20:33 to 18:21:24 | the registry's DNS A / TXT record of 400169 still named the previous holder only | pass | pass |
| 18:21:34 | two A / TXT records for 400169 (the previous holder and this node) | pass | pass |
| 18:21:44 | only this node's address: DNS agrees with the registration, 70 s after it | pass | pass |
| 18:21:44.7 | dial 400161 (`rpt fun 400169 *3400161`) | pass | pass |
| 18:21:46.3 | link ESTABLISHED (`rpt lstats`) | pass | pass |
| 18:22:48.9 | after 60 s ESTABLISHED, disconnect (`*1400161`), link gone, channel gone | pass | pass |
| 18:23:34.5 | dial 400168 | pass | pass |
| 18:23:35.5 | link ESTABLISHED | pass | pass |
| 18:24:38.2 | after 60 s ESTABLISHED, disconnect (`*1400168`) | pass | pass |
| 18:24:39.7 | registration withdrawn (module reloaded with an empty list) | pass | pass |
| 18:24:45.9 | Asterisk stopped (`core stop gracefully`), containers and `asl-ext-out` removed; node held 18:20:31 to 18:24:46 | pass | pass |

### Result per dial

| Dial | Lookup | IAX2 setup | Link | pass | pass | Result |
| ------|--------|-----------|------|-------- | --- | --- |
| 400161 (module A) | SRV `10 10 4569 <node>.nodes.allstarlink.org`, A one address, TXT `NN=400161 IP=<ip> PT=4569`, all rcode 0 (SRV points at port 4569, UDP; address in `out/` only) | NEW (empty call token), CALLTOKEN 9 ms later, NEW with the token, ACCEPT 9 ms after that (format ulaw), ANSWER 0.2 s after the first NEW | ESTABLISHED for the whole 60 s (6 samples, `rpt lstats` / `iax2 show channels` / `rpt nodes`: peer `T400161`, user `radio`, 1 IAX2 channel) | pass | pass | success, first attempt |
| 400168 (module H) | same shape, SRV port 4569 | same, ANSWER 0.11 s after the first NEW | ESTABLISHED for the whole 60 s | pass | pass | success, first attempt |

NEW carried VERSION 2, CALLED_NUMBER, CALLING_NUMBER `400169`, USERNAME `radio`, FORMAT ulaw, CAPABILITY gsm / ulaw /
adpcm and the call token, like the lab's nodes. No error text on either call.

### Link text exchange and what arrived

Both calls show the same pattern (t in seconds from the NEW, call 1; call 2 is the same within 0.2 s):

| t | From | Text | pass | pass |
| ---|------|------ | --- | --- |
| 0.22 | far node | `!NEWKEY1!` (right after ANSWER) | pass | pass |
| 0.26 | this node | `!NEWKEY1!`, `T 400169 COMPLETE`, `L ` (empty), `T 400169 CONNECTED,400169,<far>` (one burst) | pass | pass |
| 0.38 / 0.54 | far / this | `L ` (empty) each | pass | pass |
| 30.4 / 30.5, 60.4 / 60.5 | far / this | `L ` (empty) every 30 s from both ends (the ASL3 default `linkpost_time`; the lab uses 10 s) | pass | pass |
| 61.68 | this node | `!!DISCONNECT!!`, `T 400169 COMPLETE`; the far node's HANGUP follows 9 ms later | pass | pass |

The far node's own link list is empty (`L `), so neither module had another link during the hold. LAGRQ / LAGRP every
10 s and PING / PONG every 21 s from both ends, as in the lab. **No voice, no CNG, no RADIO_KEY or RADIO_UNKEY
arrived on either call**: nobody keyed during the 2 x 60 s, so there is no received audio (`rx_bytes` 0, no
`rx-*.ulaw` file). This node sent no voice frame either (it cannot). Asterisk logged "Silently dropping frame without
existent call number" once per call for a late frame after the hang-up; nothing else abnormal.

### Findings

1. **A stock ASL3 node needs no tweak to dial XLX345.** The lab's findings hold on the real network: call token
   exchange first, ulaw only, `!NEWKEY1!` both ways, no RADIO_KEY in new mode, `L` lists, `!!DISCONNECT!!` then
   HANGUP. The far side behaves like the lab's ASL3 node, except that its `L` period is 30 s.
2. **DNS lags the registration by about 70 s.** After the registration the registry's DNS A / TXT record kept the
   previous holder's address for 50 s, then listed both, then only the new one. The far node validates the caller by
   that lookup, so a dial placed earlier would risk the "does not match link IP" refusal that the lab reproduced
   (`minimal-new`); the case waits for agreement before dialling.
3. **Outbound-only works through docker NAT with nothing published**: the replies to the NEW came back on the same
   UDP flow, 4569 on both ends, and no inbound packet was needed for the calls to stay up for 60 s.
4. **Reload starts a registration at once.** `module reload res_rpt_http_registrations.so` made the first request
   within milliseconds (dry run: 2 ms after the reload; the module otherwise waits for the 180 s interval); an empty
   list withdraws it. The module has no
   deregister request: the registry forgets the node when the next holder registers or the entry ages out.
5. The docker embedded resolver answers app_rpt's SRV / A queries, but its upstream queries are not on the container's
   eth0, so a pcap taken there holds no DNS; the agent's `/resolve` asks the same resolver and the case records it.

Files (local, never committed): `out/20261103T182029Z/asl-ext/` holds `timeline.jsonl`, `summary.json`, `frames.json`,
`dns.jsonl` and `dns-self.json` (addresses), `registrations-*.txt`, `state-*.txt`, `asl-ext.pcap` (56 KB) and
`out/20261103T182029Z/logs/asl-ext.log`.

Secret check: the exact-value scan of the password file ran on the whole output directory at the end of the run (clean).
The password file was removed by its owner right after the node was released, so the staged diff could only be checked
by heuristic (no `.local` or `out` path staged, no added line giving a 12 character alphanumeric value to a
password-like key); the author never read the value. A dry run of the same rig against a registry name that does not resolve
(`DVT_ASL_EXT_REGISTRY=register.invalid`, no dial) came first and is `out/20261103T181938Z`.

## wip-c165c93 (ASL1g / ASL1h), run 2026-11-04

`cases/test_asl_edge.py` on `dv-router:wip-c165c93` with `DVT_ASL_EDGE_OUT=1`: **20 passed, 10 skipped** (`out/20261103T230702Z`; the first
run `out/20261103T223757Z` had R8-R12 pass except that the metric counters lag the hub snapshot by about 10 s: R8 now polls for them).
Skips: T-case without the outbound rows (needs `DVT_ASL_EDGE_OUT` unset), T1 / T2 (`DVT_ASL_LONG=1`) and R1-R7 (`DVT_ASL_EDGE_REG=1`, the
HTTPS registrar stand-in). Nothing from the c199b / 7efdd33 / 71ab207 columns regressed.

### wip-c165c93: all 30 cases accounted

Four runs of `cases/test_asl_edge.py` on `dv-router:wip-c165c93` (OUT = `DVT_ASL_EDGE_OUT`):

| Run | Variant | Result |
|-----|---------|--------|
| `20261103T230702Z` | OUT=1 (the main variant) | 20 passed, 10 skipped by design |
| `20261103T231326Z` | OUT unset | T4, T4b, T5 x2, T7, T9, T10, T11, T1b, back-to-back, restart x5, R8-R12 pass; T3, T6, T6m, T8, T8b fail only because they need the outbound rows (now `needs_out`: they skip when OUT is unset) |
| `20261103T232417Z` | `DVT_ASL_EDGE_REG=1`, `-k test_r` | R2-R7 pass, R8-R12 pass, restart x5 pass, R1 FAIL (lab artifact: the restart test ran first) |
| `20261104T001310Z` | `DVT_ASL_EDGE_REG=1`, fresh rig, R1-R7 alone (after the fixture fix `43f8577`) | R1-R7 pass (R10-R12 too) |
| `20261103T232753Z` | `DVT_ASL_LONG=1`, OUT=1, T1 + T2 | T1 pass (611.9 s), T2 pass (202.1 s) |

So 30 cases: all pass in their variant (R1 after the lab fix, below), nothing is skipped in the variant that runs it. The R8-R12 rows are the new phone-mode cases
(next section); DVSwitch Mobile radio and phone mode both pass (`docs/asl-dvswitch.md`).

**F-R1 (resolved: lab artifact, dv-router ruling 2026-11-04; `debug-asl-r1-cadence`).** The 13-case selection ran `test_restart_then_over_five_times` first: five quiet SIGUSR1 restarts at 2.4 / 18.5 / 34.6 / 50.7 / 66.9 s inside the recording, each new edge process starting a fresh spread (S = 60 / 7 = 8.57 s, slot k at start + k * S; schedule state does not survive a restart); the registration code is byte-identical in 71ab207 and c165c93. The fixture now trims to the last edge process and asserts the slots (`docs/asl-lab.md`, "Registration cases R1-R7: one edge process only"); **clean run on a fresh rig, `out/20261104T001310Z` (`-k` R1-R7, 10 passed incl. R10-R12, 104 s): R1-R7 all pass.** Slots of the seven nodes at 0.0 / 8.6 / 17.1 / 25.7 / 34.3 / 42.9 / 51.4 s (S = 60 / 7 = 8.57 s), first refreshes at 60.0 s (1999), 68.6 s (1991), 77.2 s (1992); http 500 retry 61.2 - 25.7 = 35.5 s, no-text retry 62.9 - 34.3 = 28.6 s, oversize 75.6 - 51.4 = 24.2 s, slow reply 90.4 - 42.9 = 47.5 s. Original observation, kept for the record: `test_r1_success_registers_and_refreshes` asserts a first refresh gap of 50..90 s
(`max(60, min(interval, refresh))` with `interval = "60s"` and the stand-in's `refresh` 180); on c165c93 node 1991 registered at 11.0, 27.1, 43.2,
59.3 and 75.4 s (gaps 16.1 s x4) and then at 135.4 s (60.0 s). The registrations are serialised across all registering nodes at about one request
every 8.5 s (the edge's own node 1999 registers too: 0.0, 2.4, 18.5, 34.6, 50.7, 66.9 s, ...; nodes 1992..1996 start at 84 to 110 s), and the log
line says `refresh_s=180`. Earlier images (c199b, 71ab207) gave 60.0 s at the first gap. The assertion is unchanged; `requests.jsonl` and
`metrics.txt` are in `out/20261103T232417Z/asl-lab/register/`.

## Phone-mode registration, R8-R12 (ASL1g, wip-c165c93)

Tester-side cases without the app (`cases/test_asl_edge.py`, on by default, `DVT_ASL_PHONE_REG=0` skips them for an older image; client
`dvtest/proto/iax2_reg.py`, unit tested against a fake registrar). All five PASS on `wip-c165c93`. Contract from dv-router's ASL1g review: an empty CALLTOKEN on a REGREQ gets the CALLTOKEN reply, a REGREQ
with no CALLTOKEN IE is dropped silently, REGAUTH has the same fields for a known and an unknown user, a wrong proof is REGREJ (CAUSE
"authentication failed", CAUSECODE 29), the account must be `kind = client`, REFRESH is clamped to `refresh_min_s` / `refresh_max_s` (60 / 300).

| Case | Asserts |
|------|---------|
| R8 `test_r8_phone_registration_md5_regack` | REGREQ, CALLTOKEN, REGREQ with token, REGAUTH (md5), REGREQ with MD5_RESULT, REGACK for `acct`; the same order on the wire; REFRESH 60..300; `dv_edge_allstar_client_reg_total` counts it, `dv_edge_allstar_client_registrations` >= 1 |
| R9 `test_r9_wrong_secret_is_regrej` | REGREJ with CAUSE "authentication failed" and CAUSECODE 29 within a few seconds, no REGACK |
| R10 `test_r10_unknown_user_is_indistinguishable_from_a_wrong_secret` | the same steps, cause and REGAUTH IEs (but the USERNAME echo) for a known and an unknown user |
| R11 `test_r11_regreq_without_calltoken_is_dropped` | no reply of any kind to a REGREQ without a CALLTOKEN IE |
| R12 `test_r12_node_account_cannot_register_as_a_phone` | the node account 2999 (the edge's own number; 1999 before wip-1590f26) is refused (REGREJ) |

The ASL1h log lines are `level=info allstar_call event=setup|hangup direction=in|out kind=node|client peer=... module=L result=ok|rejected:ip|
rejected:refused ... cause_code=... duration_ms=...` and `allstar_client_registration ...`. They cannot be read during a run (run.sh writes
`out/<run>/logs/xlxd-sut.log` at the end): `python3 -m dvtest.tools.asl_reg_log out/<run>/logs/xlxd-sut.log --expect-registrations 1`
counts them by class and fails when the fixture secret or a typed wrong secret appears in any line. On `out/20261103T230702Z`: 5 registration
lines, no secret in the log.
