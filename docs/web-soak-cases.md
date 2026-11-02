<!-- SPDX-License-Identifier: MIT -->
# Web unit: SOAK1, the 60 minute soak (WebRTC listeners and protocol clients together)

`cases/test_web_soak.py` (profiles web and webload, native launch), `cases/soak_support.py` (sampler, protocol lanes, over runner),
`dvtest/soak.py` (pure: the traffic schedule, the sample file reader and the pass criteria; unit tests `cases/unit/test_soak.py`,
`cases/unit/test_soak_support.py`, and `cases/unit/test_soak_case.py`, which runs the real case function against fakes in seconds), the
generator's soak mode (`dvtest/tools/webload_gen.py`) and the read-only control action `proc-stat` (`scripts/lib/procstat.sh`,
`dvtest/control.py`). Spec from DV-Router (2026-11-02): 60 minutes = 10 minutes warmup + 50 minutes measured, ACL on (the launcher
default). **Status: written and tested offline against fakes; not yet run on a rig** (the host was reserved for DV-Router's builds). The first
run must be watched, see "What the first run must watch".

## Mix

| Part | What | Notes |
|------|------|-------|
| WebRTC listeners | 100 receive-only listeners (aiortc, rig service `webload`), 50 in room A and 50 in room B, 2 worker processes | join paced by the W8 offsets (4 at once, then one every 1.05 s: the 1 per second per address budget with burst 5 and one token kept back); 13 logins of 8 circuits on 5 accounts; every circuit renews through its login's bucket (2 per second, burst 10) at once and every 10 s |
| churn | about 5 a minute: a seat leaves (`POST /rtc/close`, then the peer closes) and the same seat rejoins 3 to 10 s later, seats taken in a shuffled round (about 20 minutes per seat) | first churn 30 s after the last initial join; rejoins keep the seat's login, account and room, so a login never holds more than 8 circuits; about 390 sessions in an hour, all checked by `soak.validate_sessions` before the run (also by the generator) |
| `usrp-a`, `usrp-b` | USRP dynamic client, a 700 Hz tone into module A and B | the audience are the WebRTC listeners of rooms A and B (one callsign per room, `docs/web-load-cases.md` F3) |
| `dstar` (module D) | DExtra talker, DPlus listener, DCS listener; with profile interlink also a DExtra listener on the XLX998 peer | one lane covers all three D-STAR link protocols; passthrough frames |
| `dmr` (E) | MMDVM talker and listener | passthrough |
| `ysf` (C), `m17` (J) | one talker and one listener each | passthrough |
| `nxdn` (F), `p25` (G) | only when the rig serves them (see below) | NXDN talks on the NXDNReflector peered with the SUT, DMR listener; P25 likewise, D-STAR listener (IMBE needs the codec worker) |

Every lane makes one over of 9 to 11 s per minute (`soak.build_overs`, deterministic from `DVT_SOAK_SEED`): the lanes are spread over the
first 45 s of the minute so that neighbours overlap, and every fifth minute all lanes start within a second of each other. Each over makes new
sockets, links, talks seeded PRNG frames, collects and unlinks (the clients of `cases/test_xlxd_matrix.py` / `dvtest/clients.py`,
`dvtest/proto/nxdn.py`, `p25.py` and the USRP peer of `cases/crossmode_fake.py`), so a leak in the SUT's client tables shows as a growing child
and no client has to keep a link alive. A short probe over per lane runs before the clock starts: a required lane (usrp-a, usrp-b, dstar, dmr,
ysf, m17) that does not work fails the case at once; nxdn and p25 are dropped with the reason (`soak.json` `lanes_dropped`).

**NXDN and P25 under the native launch.** `docs/rig.md`: with a config file there is no importer, so the `DVT_NXDN` / `DVT_P25` rig settings
are not read; the template itself must name the NXDN and P25 peers, and the template is DV-Router's (not in this repository). The web
profile needs the native launch (import launch has no web service), so the "import launch with the same profiles" alternative cannot carry
the WebRTC half. Decision: the soak runs native; the nxdn and p25 lanes exist when the tester sees `DVT_NXDN=on` / `DVT_P25=on`
(`--profile nxdn,p25`, which starts the reflectors `nxdn-sut` .51 and `p25-sut` .61) and their probe over reaches the SUT; otherwise they are
skipped and the reason is in `soak.json` and a `FINDING:` line. Open question for DV-Router: the template keys (or a snippet for
`DVT_SUT_CONFIG_EXTRA`) that name the NXDN peer (`<NET>.51`, TG `DVT_NXDN_TG`, module `DVT_NXDN_MODULE`) and the P25 peer (`<NET>.61`).

## Method

1. The case lists the processes of the SUT container (`proc-stat xlxd-sut list`: pid, ppid and name, nothing else) and takes the
   allow-listed children of the launcher plus the launcher itself; children outside the allow-list are reported (`children_unsampled`).
2. Before any traffic: three samples 10 s apart (phase `idle0`: the quiet baseline) and a counter snapshot.
3. Warmup 600 s, measured 3000 s, one sample of every child every 30 s on an absolute schedule in a thread (phases `warmup`, `measured`),
   counter snapshots at the end of warmup and of the measured part. Each sample: pid, start time, VmRSS, VmHWM, threads, open fds, cpu ticks.
   Also per tick: the edge listener gauge and, every five minutes, the generator's status.
4. Stop: the protocol overs end, the generator finishes (every listener revokes and closes), the gauge must read 0 within 150 s, 30 s of
   settling, then the idle window of 120 s (phase `idle`): `GET /api/v1/public/events` is sampled (resumed every 30 s with the last event
   id, as `test_status_stream_is_quiet_while_idle`), samples continue, a last sample and the counter snapshots close it.
5. The raw samples are ONE file, `out/<run>/web/soak-samples.jsonl` (one JSON object per line, kinds `proc`, `metrics`, `gauge`, `mark`,
   flushed per row, about 1500 rows). The verdict is computed by re-reading that file (`soak.parse_jsonl`, `soak.evaluate`), so the
   file is what was judged. `out/<run>/web/soak.json` holds the table per child, every check, the counter deltas, the traffic and generator
   summaries and the plan; it is rewritten at every milestone, also by an aborted run.
6. Abort and teardown: a hard wall clock cap of 75 minutes (`soak.WALL_CAP_S`), `touch out/<run>/web/soak.stop` for a clean stop, Ctrl-C or a
   failure: in every case the sampler stops, the over runner stops, and the generator is told to abort (it revokes and closes what it holds).
   A stopped or capped run fails with "no verdict".

## How it is judged (`dvtest/soak.py`, lead defaults)

| # | Criterion | Rule |
|---|-----------|------|
| 1 | no child restarts | one (pid, start time) per child over every phase; a child that vanishes or first appears after warmup fails |
| 2 | RSS | growth over the measured part = least-squares slope x the measured span, must be <= max(5 MiB, 10 percent of the RSS at the end of warmup, the median of the last 3 warmup samples); the last sample is not judged |
| 3 | threads, fds | median of the last 5 measured samples <= end of warmup + 10 percent, with floors of +2 threads and +4 fds (10 percent of 8 threads is 0.8); after the traffic stopped the fd count is <= the quiet baseline of `idle0` + 5 |
| 4 | error counters | families of the hub metrics whose name contains drop, refus, reject, denied, panic, error, overflow, overrun, lost, fail, timeout or discard are recorded with their deltas over the measured part, the run and the idle window. **Asserted:** panic families never grow (whole run); drop, refusal, reject and discard families do not grow in the idle window. The others are recorded only. No DV-Router document names the families, so the pattern stands in for the list (question below) |
| 5 | idle event stream | `k22.idle_stream_verdict`: no whole minute after the first holds more than 2 events |
| + | idle | the listener gauge reads 0 in every idle sample |
| + | generator | at most 2 percent of the sessions failed, at most 2 percent of the lease answers other than 204, and the listeners of each room heard at least 50 packets (this is how the USRP lanes are seen) |
| + | protocol mix | each required lane made at least 5 overs and delivered at least half of them to its listeners |
| + | sampling | no damaged line in the file, at most 3 sampling errors |

A child whose fds cannot be read (`fds=-1`) is listed under `unmeasured` and as a `FINDING:` line; it does not fail the run, but it is never reported as passed: it
shows in the report. The helper container of `proc-stat` runs as the user of the SUT container (root when the image names none, no capability)
because `/proc/<pid>/fd` is readable only for the same uid.

## Run it

```sh
export XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_SOAK=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml[:rig/config/dv-router/xlx-peer.toml] \
  scripts/run.sh -m rig --profile web,webload[,nxdn,p25,interlink] -- cases/test_web_soak.py
```

Without `DVT_SOAK=1` the case skips. Expected duration: 70 minutes (about 2 minutes of set-up and initial joins inside the warmup,
60 minutes of traffic, up to 3 minutes of stopping, 2 minutes idle). No `--capture` (a pcap of an hour would fill the disk); check the disk
with `touch` first. Optional settings (passed to the tester by `rig/compose.yml`): `DVT_SOAK_WARMUP_S`, `DVT_SOAK_MEASURED_S` (shorter
values are smoke runs and say so), `DVT_SOAK_SEED`, `DVT_SOAK_MODULES` (`dstar=D,dmr=E,ysf=C,m17=J,usrp-a=A,usrp-b=B,nxdn=F,p25=G`; two lanes
may not share a module). With `interlink`, add `xlx-peer.toml` to `DVT_SUT_CONFIG_EXTRA` and keep modules D and E in `DVT_INTERLINK_MODULES`.
Stop early: `touch out/<run>/web/soak.stop`. One rig at a time; nothing else on the host that needs the SUT's cpu.

## What the first run must watch

| Watch | Why it could not be verified offline |
|-------|---------------------------------------|
| `fds` in the first samples | the helper reads `/proc/<pid>/fd` as the SUT container's user; if every child shows `fds=-1`, the SUT container's user is a name the helper image cannot resolve (it then falls back to the host uid) |
| the output of `proc-stat xlxd-sut list` | the allow-list names (`dv-edge-usrp`, `dv-room`, `dv-recorder`, ...) are guesses for the children that have not been seen; `soak.json` `children` and `children_unsampled` show the real set |
| the probe overs | the protocol clients are driven for 10 s per minute for the first time; a lane that misbehaves in a way a 2 s probe does not show (a client table limit, a floor timeout between lanes on one module) will show as `traffic:<lane>` |
| cost of the sampling tick | about 10 children x one throwaway container per 30 s; `slow_ticks` in `soak.json` counts ticks longer than 30 s |
| web session lifetime | the generator logs in once per login and keeps the cookie for the hour; an expiry would show as lease answers other than 204 (`generator-leases`) |
| churn rate seen by the SUT | `generator.left` and the status notes in the sample file against the plan (`plan.leaves`) |
| the tester | 2 GiB and 512 pids carry about 16 over threads and the sampler; a limit hit shows as sampling errors |

## Questions for DV-Router

1. Which counter families should be asserted not to grow, per the soak spec? Until named, panics (whole run) and drop/refusal/reject
   families (idle window) are asserted and the rest recorded.
2. The exact process names of the children (comm, at most 15 characters): the allow-list guesses `dv-edge-usrp`, `dv-room`,
   `dv-recorder`; the DExtra/DPlus/DCS/MMDVM/YSF/M17 protocols are assumed to live in `dv-edge-xlx`.
3. How NXDN and P25 are served by the native template (keys, or a snippet to add to `DVT_SUT_CONFIG_EXTRA`).
4. Are floors of +2 threads and +4 fds acceptable beside the 10 percent rule? (A small child would otherwise fail on one thread.)
