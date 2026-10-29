---
name: dv-testset
description: Run, extend or debug the dv-testset black-box tests for digital-voice reflectors and transcoders (xlxd, ambed/AMBEd-compatible transcoders, USRP/AllStarLink). Use when asked to test a reflector or transcoder build, add a protocol client or test case, update baselines, or read rig results in out/.
---

# dv-testset

Black-box tests: every case talks to the system under test over UDP only, so
the same cases run against any implementation. Python 3.11+, stdlib-only
library (`dvtest/`), pytest cases (`cases/`), docker rig (`rig/`).

## Pick the level

| Goal | Command | Needs |
|------|---------|-------|
| Library sanity | `python3 -m pytest -m unit` | nothing |
| Transcoder conformance | `DVT_AMBED_ADDR=<ip> python3 -m pytest -m wire` | a running transcoder (or `TRANSCODER_IMAGE` + optional `DVT_TRANSCODER_ENV`/`DVT_TRANSCODER_ARGS`) |
| xlxd vs fake transcoder/USRP peer | `scripts/build-xlxd-netns.sh`, then `DVT_XLXD_BIN=<path> python3 -m pytest -m netns` | `unshare -rn` (user namespaces) |
| Full rig | `scripts/run.sh -m rig` | docker, `TRANSCODER_IMAGE` in `.env` |
| Rig + AllStarLink | `scripts/run.sh -m "rig or allstar" --profile allstar` | as above, `scripts/gen-local.sh` once |

First time: `scripts/pre-fetch-xlxd.sh`, `scripts/pre-fetch-codec2.sh`,
`scripts/gen-local.sh`. A skipped case prints what is missing; fix that rather
than editing the case. `scripts/run.sh --help` lists its options.

## Settings

- `.env.example` (tracked): every repo URL, commit, base image, package
  version and download hash. Never hardcode a pin in a script or Dockerfile.
- `.env` (gitignored): local overrides, e.g. `TRANSCODER_IMAGE`,
  `XLXD_SUT_REPO` / `XLXD_SUT_REF` for the build under test.
- `.local/rig.env` (gitignored, mode 600): generated secrets. Never print,
  log or copy secret values; `Target.iax_secret` is hidden from repr.
- `dvtest/target.py` documents every `DVT_*` variable. Rig addresses default
  to 172.30.0.0/24: xlxd-ref .10, xlxd-sut .11, transcoder-ref .20,
  transcoder-sut .21, tester .30, allstar .40.

## Results

`out/<utc>/`: pytest log + junit, per-service logs, case JSON. Report numbers
and hashes; never publish raw captures.

## Writing or changing cases

- Build packets only through `dvtest/proto/*` (one module per protocol; the
  AMBEd transcoder protocol, including `FakeAmbed`, lives in
  `dvtest/proto/ambed.py`). Add a round-trip test in `cases/unit/` for any
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

## Known traps

- xlxd drops a D-STAR header whose stream id equals the module's last
  outbound id: use a fresh random stream id for every transmission.
- A plain `make` of xlxd gives a daemon with absolute paths; the netns cases
  need the build from `scripts/build-xlxd-netns.sh`.
- Docker compose looks for `.env` next to `rig/compose.yml`; call compose via
  `scripts/run.sh`, or pass `--env-file .env.example --env-file .env`.
- Use only the compose project `dvt`; never stop, remove or prune other
  containers, images or volumes.

## Before committing

Run `scripts/pii-check.sh`: no home paths, personal callsigns (use N0CALL,
N0TEST, XLX999), public IPs (use 127/8, 192.0.2/24 or the rig subnet),
emails, device paths or serials, ELF files. Put personal words to check in
`.local/pii-words`. Git identity is the noreply address; commit trailer
`Co-Authored-By` only. Tests for other projects belong here, not in those
projects' repositories.
