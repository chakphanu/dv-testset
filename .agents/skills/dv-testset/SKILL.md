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
| xlxd vs fake transcoder/USRP peer | `scripts/build-xlxd-netns.sh`, then `DVT_XLXD_BIN=<path> python3 -m pytest -m netns` | `unshare -rn` (user namespaces) |
| Full rig | `scripts/run.sh -m rig` | docker, `TRANSCODER_IMAGE` in `.env` |
| Rig, byte-exact cross-mode (DV-Router) | `DVT_TRANSCODER_FAKE=1 DVT_SUT_PROFILE=dvrouter scripts/run.sh -m crossmode --capture -- cases/test_crossmode_exact.py cases/test_crossmode_failure.py` | as above; transcoder-sut becomes the fake (`docs/ambed-fake.md`) |
| Rig, DV-Router web service (native launch) | `XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> scripts/run.sh -m rig --capture --profile web -- cases/test_web_smoke.py` | as above; `docs/rig.md` "Web profile" (HTTPS proxy `https://dvt-web`, CA and operator login in `out/<run>/web/`) |
| Rig, DV-Router API cases (native launch) | same as the web row with `cases/test_web_api.py` (about 4 minutes; `docs/web-api-cases.md`) | as above; also restarts `dv-svc-web` through the control channel |
| Rig, DV-Router web UI in browsers (native launch) | `... scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_browser.py` (Playwright Chromium, Firefox and Playwright WebKit; `docs/web-browser-cases.md`) | as above; `docker pull` of `BROWSER_BASE_IMAGE` once (the only internet use); `docs/rig.md` "Browser profile" |
| Rig + AllStarLink | `scripts/run.sh -m "rig or allstar" --profile allstar` | as above, `scripts/gen-local.sh` once |

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
  transcoder-sut .21, tester .30, allstar .40, web-proxy .90, browser .91.

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
  xlxd-sut`, `dvtest/metrics.py`); extra config for the native launch comes from
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

## Before committing

Run `scripts/pii-check.sh`: no home paths, personal callsigns (use N0CALL,
N0TEST, XLX999), public IPs (use 127/8, 192.0.2/24 or the rig subnet),
emails, device paths or serials, ELF files. Put personal words to check in
`.local/pii-words`. Git identity is the noreply address; commit trailer
`Co-Authored-By` only. Tests for other projects belong here, not in those
projects' repositories.
