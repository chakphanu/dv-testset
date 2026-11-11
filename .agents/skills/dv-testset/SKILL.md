---
name: dv-testset
description: Run, extend or debug the dv-testset black-box tests for digital-voice protocols and the servers that speak them (reflectors, transcoders, gateways), first of all the DV-Router project, with xlxd, URFd and mrefd as references. Use when asked to test a server build, add a protocol client or test case, update baselines, or read rig results in out/.
---

# dv-testset

Black-box tests: every case talks to the system under test over UDP only, so
the same cases run against any implementation. The focus is the protocols and
the servers that speak them, first of all DV-Router; xlxd, URFd and mrefd are
references to calibrate against, not the goal: write a case per protocol
obligation (`docs/protocols/`) and assert on the system under test through its
profile spec (`dvtest/specs/`), using the references only where they agree.
Python 3.11+, stdlib-only library (`dvtest/`), pytest cases (`cases/`), docker
rig (`rig/`; the service names `xlxd-ref` / `xlxd-sut` are historical).

## Pick the level

| Goal | Command | Needs |
|------|---------|-------|
| Library sanity | `python3 -m pytest -m unit` | nothing |
| Transcoder conformance | `DVT_AMBED_ADDR=<ip> python3 -m pytest -m wire` | a running transcoder (or `TRANSCODER_IMAGE` + optional `DVT_TRANSCODER_ENV`/`DVT_TRANSCODER_ARGS`) |
| xlxd vs fake transcoder/USRP peer | `scripts/build-xlxd-netns.sh`, then `DVT_XLXD_BIN=<path> python3 -m pytest -m netns` (a DV-Router build: `DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_DVR_BIN_DIR=<image binaries>`, converter + `dv-router --config`, `docs/rig.md` "netns cases with a DV-Router build") | `unshare -rn` (user namespaces); scenarios start in waves of `DVT_NETNS_PARALLEL` (default 1, a DV-Router scenario peaks near 435 tasks of the 1024-task scope; `-k` launches only the scenarios of the selected cases; `docs/rig.md` "netns cases") |
| netns, DV-Router protocol-edge fuzz (13 ports, seeded mutators, valid client served, RSS and counters; `docs/fuzz-cases.md`) | `DVT_DVR_BIN_DIR=<image binaries> DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter scripts/contained.sh --name test -- python3 -m pytest -m netns cases/test_fuzz_netns.py -q` (about 7 to 9 minutes, estimate; `DVT_FUZZ_N` / `DVT_FUZZ_SEED`; a failure writes `fuzz-replay-<p>.json`, resend with `python3 -m dvtest.tools.fuzz_replay`) | as the netns row; `DVT_NETNS_PARALLEL` stays 1; skips under the xlxd profile |
| Full rig | `scripts/run.sh -m rig` | docker, `TRANSCODER_IMAGE` in `.env` |
| Rig, byte-exact cross-mode (DV-Router) | `DVT_TRANSCODER_FAKE=1 DVT_SUT_PROFILE=dvrouter scripts/run.sh -m crossmode --capture -- cases/test_crossmode_exact.py cases/test_crossmode_failure.py` | as above; transcoder-sut becomes the fake (`docs/ambed-fake.md`) |
| Rig, DV-Router web service (native launch) | `XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> scripts/run.sh -m rig --capture --profile web -- cases/test_web_smoke.py` | as above; `docs/rig.md` "Web profile" (HTTPS proxy `https://dvt-web`, CA and operator login in `out/<run>/web/`) |
| Rig, DV-Router API cases (native launch) | same as the web row with `cases/test_web_api.py` (about 4 minutes; `docs/web-api-cases.md`) | as above; also restarts `dv-svc-web` through the control channel |
| Rig, DV-Router optional TOTP (TOTP1, default policy; WITHOUT `totp-required.toml`) | `... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml scripts/run.sh -m rig --capture --profile web -- cases/test_web_totp_optional.py` (about 8 minutes, its own rig run; `docs/web-totp-optional-cases.md`) | as the web row; never name `totp-required.toml` for this file |
| Rig, DV-Router web UI in browsers (native launch) | `... scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_browser.py` (Playwright Chromium, Firefox and Playwright WebKit; `docs/web-browser-cases.md`) | as above; `docker pull` of `BROWSER_BASE_IMAGE` once (the only internet use); `docs/rig.md` "Browser profile" |
| Rig, DV-Router WebRTC Listen/Talk and recorder in browsers (native launch) | `DVT_USRP_DYNAMIC=1 ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/recorder.toml scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_webrtc.py cases/test_web_recorder.py` (`docs/web-browser-cases.md`, "Stage 3b") | as above; a USRP dynamic hub (`DVT_USRP_DYNAMIC=1`) for the protocol talker; `recorder.toml` for the recorder cases |
| Rig, DV-Router NAT behaviour of WebRTC in browsers (native launch) | `DVT_USRP_DYNAMIC=1 ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py` (NAT router, coturn, per-run TURN secret; about 1 hour; `docs/web-nat-cases.md`) | as above; `docker pull` of `COTURN_IMAGE` once; without `turn.toml` only the direct and 404 cases run; `docs/rig.md` "NAT profile" |
| Rig, DV-Router K22 / K22b circuit and guest-lease cases (native launch) | `DVT_USRP_DYNAMIC=1 ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/guest.toml scripts/run.sh -m rig --profile web,browser -- cases/test_web_k22.py` (about 30 minutes with the 10 minute idle sample; `docs/web-k22-cases.md`) | as above; kills `dv-edge-webrtc` and `dv-svc-status` through the control channel; do not combine with `cases/test_web_webrtc.py` (guests off there) |
| Rig, DV-Router W8 load: 200 WebRTC listeners x 50 pps through the HTTP relay (native launch) | `DVT_USRP_DYNAMIC=1 ... scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py` (calibration 1/10/50, the 200 x 50 pps gate, a short bounded flood; about 15 minutes; `docs/web-load-cases.md`) | as above; the service `webload` (aiortc, pinned by `WEBLOAD_PIP_PINS`, image built from the base image with the one internet use of pip); JSON summary `out/<run>/web/load.json`; reads the SUT edge's cpu and VmHWM through the read-only `proc-stat` control action |
| Rig, DV-Router soak: 60 minutes, 100 WebRTC listeners with churn + protocol clients on 6 to 8 modules (native launch) | `DVT_SOAK=1 DVT_USRP_DYNAMIC=1 ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/soak-edges.toml scripts/run.sh -m rig --profile web,webload[,interlink] -- cases/test_web_soak.py` (about 70 minutes, hard cap 75; `DVT_SOAK_SHORT=1` is the 10 minute validation; `docs/web-soak-cases.md`) | as the W8 row; reads every child of the launcher (each edge is its own binary) through `proc-stat` every 30 s; raw samples `out/<run>/web/soak-samples.jsonl`, verdict table `out/<run>/web/soak.json`; stop early with `touch out/<run>/web/soak.stop`; one rig, nothing else at the same time |
| Rig, DV-Router RSS isolation: ONE activity per run (A idle, B webrtc-churn, C voice, D proto-churn, E webrtc-voice, F voice-x3 = C with 3x the overs), 5 + 10 minutes (native launch) | `... scripts/run.sh -m rig --profile web,webload -- -p dvtest.isolation_plugin --isolation=A cases/test_web_rss_isolation.py` (about 20 minutes a mode; `--isolation-short` is the 2 + 3 minute smoke; `docs/web-isolation-cases.md`) | as the soak row (`soak-edges.toml` for modes C and D); data not verdicts: `out/<run>/web/isolation-<letter>.json` (also `hub_events` / `streams`: events per minute against the dv-svc-status slope, KiB per stream; SOAK1 records them too), tables with `python3 -m dvtest.isolation out/2*` |
| Rig, DV-Router built-in echo (ECHO1 / ECHO1c: E1 to E7, telemetry) | `... DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/echo.toml[:echo-usrp-peer.toml][:echo-peers.toml][:xlx-peer.toml] scripts/run.sh -m rig --profile interlink -- cases/test_echo.py` (about 8 to 10 minutes, no `--capture`; `docs/echo-cases.md`) | as the web row without web; `echo.toml` is the switch (the file skips with a reason without it); the cases edit and restore the run's own rendered `dv-router.toml` for E3 / E7; `DVT_RIG_CONTROL=1` for the telemetry check |
| Rig, DV-Router RFSAFE1c (RF1c-4a / 4b: the launcher start in the hub's procs[] rows, also after a `dv-edge-ax25` restart; RF1c-2: no session/update hint item in the event history and the public feed; RF1c-3: the `probe failed part=` log line; RF1c-1 mid-run fence latency is a GAP; written offline) | `... DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=true DVT_RFSAFE1C=on DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf.toml:rig/config/dv-router/dp15-rf-out.toml scripts/run.sh -m rig --capture --profile web -- cases/test_rfsafe1c.py`, then on the host `python3 -m dvtest.rfsafe1c out/<run>` (about 6 minutes; `docs/rfsafe1c-cases.md`) | as the DP15 RF row (fake TNCs, web profile); restarts `dv-edge-ax25` once through the admin API; every case skips without `DVT_RFSAFE1C=on`; the log verdict is a host step because the SUT logs are collected after pytest |
| Rig, DV-Router open-stream registry (LEAK2: L1 to L4, kills one `dv-room` by module letter) | `... DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/xlx-peer.toml scripts/run.sh -m rig --profile interlink -- cases/test_stream_registry.py` (about 2 minutes; `docs/stream-registry-cases.md`) | as the web row without web; `kill-proc xlxd-sut dv-room KILL rooms=<LETTER>` through the control channel (the launcher respawns it); one rig, nothing else at the same time |
| Rig, DV-Router secret-file reload (SECRELOAD1: the APRS-IS passcode file replaced at the same path, admin reload, restart of `dv-edge-aprsis`, refusal when unreadable or missing, no secret value in any reply or log) | `DVT_SUT_CONFIG_RW=1 DVT_SECRELOAD1=on ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-aprsis.toml scripts/run.sh -m rig --profile web,aprs -- cases/test_secret_reload.py` (about 4 minutes; `docs/secreload-cases.md`) | as the DP15-5a row (private aprsc) plus the web profile for the admin API; `DVT_SUT_CONFIG_RW=1` makes the SUT config replaceable by dv-svc-admin; restarts `dv-edge-aprsis` three times; the SIGHUP variant (`-k sighup`, no web profile, no `DVT_SUT_CONFIG_RW`) uses the control action `hup xlxd-sut`; SECRELOAD2 (`-k secreload2`, `DVT_SECRELOAD2=on`, web profile, no `DVT_SUT_CONFIG_RW`, about 12 minutes with the 5 minute step-up wait) uses the admin routes `GET` / `POST /api/v1/admin/config/reload`; values are throwaway, never printed, scanned in the collected output by `run.sh` (exit 5) |
| Rig, DV-Router WEBSID1 browser cases (a step-up keeps the operator SSE, the Listen circuit and a slow WAV download; caps do not refill; logout / revoke / role / disable end every handle; denial coalescing; written offline) | `DVT_USRP_DYNAMIC=1 DVT_WEBSID1=on ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/recorder.toml scripts/run.sh -m rig --profile web,browser -- cases/test_web_session_rotation.py` (about 20 minutes; `docs/websid1-cases.md`) | as the recorder / WebRTC rows (Playwright engines, USRP dynamic hub, recorder.toml); a 150 s over is sent once (`DVT_WEBSID_OVER_S`); the TURN part needs `--profile nat` with turn.toml; skips on an image without WEBSID1, `DVT_WEBSID1=on` fails instead |
| Rig, DV-Router lazy basemap regions (MAP2d: a region published for a public position outside coverage, hidden / ambiguity 3-4 start no job, the daily cap holds; the source is the fixture in the SUT's own network namespace) | `... DVT_BASEMAP_FILE=.local/basemap-map2d DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml scripts/run.sh -m rig --capture --profile web,mapsrc -- cases/test_map2d_regions.py` (about 6 minutes; `docs/map2d-cases.md`) | as the web row; the tree made by `scripts/basemap-install-fixture.sh` with the `dv-svc-basemap` of the image under test (MAP2d INDEX shape); the service `basemap-src` (tester image, `network_mode: service:xlxd-sut`) serves 127.0.0.1:18700 inside the SUT; never restart `xlxd-sut` in that run |
| Rig, DV-Router APRS-IS against a PRIVATE aprsc (DP15-5a / 5b: message to the room acked once, a repeat acked again and delivered once) | `... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-aprsis.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp15_aprsis.py` (about 8 to 12 minutes; `docs/dp15-cases.md`, `docs/rig.md` "Private APRS-IS server") | as the web row; `scripts/pre-fetch-aprsc.sh` once (the only internet use); the service `aprsc` (.47, standalone, no internet); the cases skip without `--profile aprs` |
| Rig, DV-Router AX.25 edge against two REAL Dire Wolf TNCs on a modem loop (DP15-6l / 6a / 6b / 6c, RF tier B; two runs: `DVT_DP15_TX_ENABLE=false`, then `true` plus `dp15-rf-out.toml`) | `... DVT_DP15_TX_ENABLE=false DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf-dw.toml scripts/run.sh -m rig --capture --profile web,dw -- cases/test_dp15_rf_dw.py` (about 4 minutes a run; `docs/dp15-cases.md` "RF tier B", `docs/direwolf-cases.md` "Modem loop") | as the web row; `scripts/pre-fetch-direwolf.sh` once (the only internet use); services `dw-a` .48 and `dw-b` .49 (image `dvt-direwolf:local`, no hardware, no internet); the loop alone: `python3 -m dvtest.oracle_direwolf loop`; the cases skip without `--profile dw` |
| Rig, TWO DV-Router nodes: the remote floor, recording and cross-mode effects of a synthetic text over on an XLX peer (DP15-7b / 7c / 7d; node 2 = the service `dvr-peer` .16 with `transcoder-peer` .17) | `... DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 scripts/run.sh -m rig --capture --profile twonode -- cases/test_dp15_peer.py` (about 6 to 8 minutes, no web; `docs/dp15-cases.md` "Two DV-Router nodes", `docs/rig.md`) | as the web row without web; `XLXD_SUT_IMAGE` is both nodes (no build); `run.sh` adds `dp15-twonode-a.toml` and renders node 2 from the same template with `dp15-twonode-b.toml`; hubs of both nodes through `metrics xlxd-sut` / `metrics dvr-peer`; never with `--profile interlink` or `xlx-peer.toml` |
| Rig, the REAL client stack of a hotspot: upstream MMDVMHost + DMRGateway, YSFGateway, P25Gateway, NXDNGateway, DStarGateway (service `mmdvm-stack`) driven by the VIRTUAL MMDVM modem in the tester (MMDVM1 uplink, MMDVM2 downlink, MMDVM3 link lifecycle) | `... DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> scripts/run.sh -m rig --capture --profile web,mmdvm -- cases/test_mmdvm_stack.py` (about 6 to 8 minutes; `docs/mmdvm-stack-cases.md`; the modem alone: `python3 -m dvtest.mmdvm_modem --udp 0.0.0.0:3334`) | as the web row; `scripts/pre-fetch-mmdvmstack.sh` once (the only internet use) and the image `dvt-mmdvmstack:local` (built by `run.sh` on first use, about 132 MB estimated, never built yet: show the owner the size first); no radio, no internet; without `--profile web` only the frame cases run (the public state is not read); the case skips without `DVT_MMDVM=on` |
| Host side, DV-Router COMPAT1 config load: old keys / shapes must fail `dv-router config check` with a clear error (`cfgload`, NOT rig) | `DVT_CFGLOAD_IMAGE=dv-router:wip-488b89d DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_config_load.py` (about 10 s, 19 throw-away containers one after the other; `docs/config-load-cases.md`) | docker and a compat1 image; never the rig; results `$DVT_OUT/config-load/results.json`; skips without `DVT_CFGLOAD_IMAGE`, `DVT_SUT_CONFIG` or `DVT_SUT_GEN=compat1` |
| Rig, real-transcoder cross-mode audio into YSF (DMR, D-STAR, M17 talker, tone, decoded-audio check) | `... DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> scripts/run.sh -m rig -- cases/test_crossmode_real_ysf.py` (about 80 s; `docs/echo-cases.md`, last section) | the real transcoder (skips with `DVT_TRANSCODER_FAKE=1`); results `out/<run>/crossmode-real-ysf.txt` |
| EXTERNAL: production voice check of a live reflector (echo module E, one over per protocol) and one anonymous WebRTC Listen | `python3 -m dvtest.tools.prod_echo --host HOST --callsign PROTO=N0CALL --dry-run`, then with `--i-have-owner-approval`; `python3 -m dvtest.tools.prod_listen --url https://WEB-HOST --print-docker` (`docs/prod-checks.md`) | only with the owner's approval; sends packets to the host you name, never a default; the listen runs in the `dvt-webload` image |
| EXTERNAL: cross-mode levels through the LIVE reflector, module E only (M17 talker / D-STAR listener and back, tone and speech, against the rig unity rows) | `DVT_PROD_CROSS=1 DVT_PROD_CROSS_DRY=1 DVT_PROD_HOST=HOST DVT_PROD_CALL_DSTAR="N0CALL T" DVT_PROD_CALL_M17=N0CALL python3 -m pytest cases/test_prod_cross.py -s -q` (plan only), then without `DVT_PROD_CROSS_DRY` and with `DVT_AMBED_REF`, `DVT_PROD_CROSS_REF` (`docs/prod-checks.md`) | only with the owner's approval; sends packets to the host you name, never a default; the local transcoder (rig `transcoder-ref`) is only the codec; run on the host, the rig network has no route out |
| EXTERNAL: deploy checks f and g on the production host: an AllStar phone call to module E (`prod_iax`) and a browser Hold Talk on module E (`prod_talk`) | `python3 -m dvtest.tools.prod_iax --account ACCOUNT --callsign N0CALL --secret-file PATH --dry-run`; `python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file PATH --dry-run` or `--print-docker`, then with `--i-have-owner-approval` (`docs/prod-checks.md`) | only with the owner's approval, module E only, on the host by the deploy session; secrets only as 0600 files (or `DVT_PROD_IAX_SECRET` / `DVT_PROD_TALK_PASSWORD`), never printed; `prod_talk` needs Playwright (the tester image + `dvt-browser:local`, or a local install) |
| Rig + AllStarLink | `scripts/run.sh -m "rig or allstar" --profile allstar` (DV-Router native launch: add `DVT_USRP_DYNAMIC=1 DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template>`; `run.sh` appends `rig/config/dv-router/usrp-rig.toml`, the USRP endpoints A/B/C 34001-34003, because no importer reads `xlxd.usrp` there; `DVT_SUT_LAUNCH=import` is DEPRECATED, only for images before COMPAT1b part 2, the product's import hand-off is being removed) | as above, `scripts/gen-local.sh` once; the `rig_usrp` fixture skips the application cases with a reason when the rendered config lacks those binds; `docs/rig.md` "Extending the template" |
| AllStarLink test lab (private app_rpt nodes, registrar, DNS zone, IAX2 pcaps) | `scripts/run.sh --profile asl-lab -m asl_lab` (`DVT_ASL_LONG=1` adds the 75 s call; `docs/asl-lab.md`) | only the tester and the four lab hosts start (no reflector, no `TRANSCODER_IMAGE`); the image is built FROM the allstar image |
| EXTERNAL: real AllStarLink node 400169 registers and dials real nodes, listen only (needs the owner's go and a password file) | `DVT_ASL_EXT_GO=400169 scripts/run.sh --profile asl-ext -m asl_ext -- cases/test_asl_ext.py` (about 5 minutes; `docs/asl-edge-results.md`, External dial T12) | `.local/secrets/asl-400169.pw` (0600, never printed); the only profile with internet; only with explicit user approval |

First time: `scripts/pre-fetch-xlxd.sh`, `scripts/pre-fetch-codec2.sh`,
`scripts/gen-local.sh`. A skipped case prints what is missing; fix that rather
than editing the case. `scripts/run.sh --help` lists its options.

## Resource limits (mandatory)

A host crashed (load 185, 40 GB swap) from unbounded parallel builds and
tests, so everything heavy here is capped; keep it that way (README, "Resource
limits"):

- `scripts/run.sh` runs inside the scope `dvt-rig.scope` (12G, swap 0, 800% CPU,
  1024 tasks) by re-executing itself through `scripts/contained.sh`; one rig at
  a time, across `DVT_INSTANCE` values. Never bypass it (`DVT_UNCONTAINED=1`
  only when something else caps the run).
- Every compose service needs `mem_limit` = `memswap_limit`, `cpus`,
  `pids_limit` (the `x-limits-*` sets); `run.sh` exits 4 otherwise. Host-side
  `docker run` takes `dvtest.limits.docker_run_args()`.
- `wire` / `netns` pytest, `build-xlxd-netns.sh` and any system-under-test
  build: `scripts/contained.sh --name test|build -- <command>`.
- Compiler jobs come from `DVT_BUILD_JOBS` (never `nproc`); no `pytest -n`.

## Settings

- `.env.example` (tracked): every repo URL, commit, base image, package
  version and download hash. Never hardcode a pin in a script or Dockerfile.
- `.env` (gitignored): local overrides, e.g. `TRANSCODER_IMAGE`,
  `XLXD_SUT_REPO` / `XLXD_SUT_REF` for the build under test.
- `.local/rig.env` (gitignored, mode 600): generated secrets. Never print,
  log or copy secret values; `Target.iax_secret` is hidden from repr.
- `dvtest/target.py` documents every `DVT_*` variable. Rig addresses default
  to 172.30.0.0/24: xlxd-ref .10, xlxd-sut .11, transcoder-ref .20,
  transcoder-sut .21, tester .30, allstar .40, asl-lab asl-reg .41 / asl-a .42 / asl-b .43 / asl-e .44, web-proxy .90, browser .91 (profile nat: router .92 / alias .93, coturn .94, reflectors .95 / .96), webload .97, aprsc .47 (profile aprs; .46 is the T5n alias address), dw-a .48 / dw-b .49 (profile dw), dvr-peer .16 / transcoder-peer .17 (profile twonode), mmdvm-stack .100 (profile mmdvm).

## Results

`out/<utc>/`: pytest log + junit, per-service logs, case JSON. Report numbers
and hashes; never publish raw captures.

## Writing or changing cases

- Build packets only through `dvtest/proto/*` (one module per protocol; the
  AMBEd transcoder protocol, including `FakeAmbed`, lives in
  `dvtest/proto/ambed.py`; the extended-protocol fake with the bit-exact transform
  is `dvtest/fake_ambed.py`, see `docs/ambed-fake.md`). Add a round-trip test in `cases/unit/` for any
  new encoder/parser.
- Inputs come from `dvtest/audio/signals.py` (deterministic tones and PRNG
  frames). Never commit audio or bitstreams (`*.pcm *.bin *.wav *.raw
  *.frames` are ignored); larger inputs are fetched by a `scripts/pre-fetch-*`
  script with `NAME_URL` / `NAME_SHA256` in `.env.example` and
  `fetch_verified` from `scripts/lib/env.sh`.
- Expected values: passthrough cells compare against
  `baselines/<ref-commit>/` (hashes and counts only; rewrite only with
  `DVT_UPDATE_BASELINE=1` and say why). Transcoded cells are differential:
  system under test vs reference in the same run.
- Derive expectations from what the target advertises (e.g. the CAPS codec
  mask), not from one implementation's internals.
- Mark every case (`unit`, `wire`, `netns`, `rig`, `allstar`) and skip with a
  reason in a fixture when its prerequisite is missing.
- Stopping a backend during a rig case: use the control channel described in
  the header of `scripts/run.sh` (allow-listed services and actions only).
  The same channel reads the hub metrics of the DV-Router SUT (`metrics
  xlxd-sut`, `dvtest/metrics.py`). `hup xlxd-sut` (SIGHUP to the SUT launcher, no other service or signal)
  exists for the SECRELOAD1 case only, allowed by dv-router (`docs/secreload-cases.md`); extra config for the native launch comes from
  `DVT_SUT_CONFIG_EXTRA` snippets in `rig/config/dv-router/` (`docs/rig.md`),
  never from a copy of the template.

## Known traps

- xlxd drops a D-STAR header whose stream id equals the module's last
  outbound id: use a fresh random stream id for every transmission.
- A plain `make` of xlxd gives a daemon with absolute paths; the netns cases
  need the build from `scripts/build-xlxd-netns.sh`.
- Docker compose looks for `.env` next to `rig/compose.yml`; call compose via
  `scripts/run.sh`, or pass `--env-file .env.example --env-file .env`.
- Use only the compose project `dvt`; never stop, remove or prune other
  containers, images or volumes.

- Load case (`cases/test_web_load.py`, `dvtest/webload.py`, `dvtest/tools/webload_gen.py`): disk is checked with `touch` before every run, no `--capture`, one JSON summary and no per-listener files, at most a few runs; the load generator is the only third-party client (aiortc) and lives only in its own image.

- Web unit: use `dvtest/web.py` (HTTPS client with the run CA, TOTP, `Pacer` for the
  login/register budgets, account helpers) and the fixtures `web_operator` /
  `web_user` / `web_client` / `web_operator2` / `admin_session` (`cases/web_fixtures.py`; route tables `dvtest/api_web.py`,
  `dvtest/api_admin.py`, admin client `dvtest/admin.py`); never print a password,
  invite code, TOTP secret, CSRF token or cookie, and record a difference from the
  specs with `web.finding()` instead of asserting it. `out/<run>/web/operator.env`
  and `out/<run>/sut-state/` hold run secrets: local to `out/`, never copied out.

- Browser cases: the rig service `browser` (`--profile web,browser`) runs `playwright run-server`; cases use
  the fixtures of `cases/browser_fixtures.py` (`browser_page`, `user_page`, `operator_page`, parametrized over
  chromium / firefox / webkit) and put every selector in `cases/web_pages.py`. WebKit is "Playwright WebKit",
  never "Safari". The Playwright pin (`PLAYWRIGHT_VERSION`) feeds both the server and the tester client; keep
  the two equal. Failure artifacts (`out/<run>/web/browser/`) can hold typed run passwords: local to `out/`.

- NAT cases: `cases/test_web_nat.py` and `cases/nat_support.py`; the NAT type is set through the router control
  channel (`dvtest/nat.py`), never by editing iptables by hand. The TURN secret (`out/<run>/web/turn.secret`) is
  never printed, logged or copied; `run.sh` scans the run directory for it and fails the run on a hit. Record
  differences from DV-Router's documents with `web.finding()`; the adopted criteria of `docs/project/testing.md`
  ("Browser/NAT rig criteria") are asserted.

## Before committing

Run `scripts/pii-check.sh`: no home paths, personal callsigns (use N0CALL,
N0TEST, XLX999), public IPs (use 127/8, 192.0.2/24 or the rig subnet),
emails, device paths or serials, ELF files. Put personal words to check in
`.local/pii-words`. Git identity is the noreply address; commit trailer
`Co-Authored-By` only. Tests for other projects belong here, not in those
projects' repositories.
