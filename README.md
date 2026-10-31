# dv-testset

Black-box tests for digital-voice reflectors and transcoders (xlxd, ambed and
compatible implementations). The tests talk to the system under test only over
the network, so the same cases run against any build or implementation.

Nothing third-party is shipped here: sources and inputs are fetched by
`scripts/pre-fetch-*.sh` into `.cache/` (see NOTICE).

## Layout

- `dvtest/` - Python clients (stdlib only): AMBEd transcoder protocol,
  DExtra, MMDVM/DMR, M17, USRP, IAX2, test signals and scoring.
- `cases/` - pytest cases, selected by marker.
- `rig/` - docker compose rig: reference xlxd, xlxd under test, transcoder,
  tester, optional AllStarLink node.
- `scripts/` - fetch, local setup, run and PII check scripts.
- `baselines/` - expected frame hashes and numbers per xlxd ref.

## Markers

| Marker    | Needs                                                        |
|-----------|--------------------------------------------------------------|
| `unit`    | nothing                                                      |
| `wire`    | a transcoder: `DVT_AMBED_ADDR=host` or `TRANSCODER_IMAGE`    |
| `netns`   | a built xlxd (`scripts/pre-fetch-xlxd.sh`, `make`) and `unshare -rn` |
| `rig`     | docker; `rig/compose.yml` profile `core`                     |
| `allstar` | docker; profile `allstar`                                    |

Cases whose prerequisites are missing are skipped with the reason.

## Quick start

```sh
python3 -m pip install -e '.[test]'
python3 -m pytest -m unit
cp .env.example .env                 # optional overrides
scripts/pre-fetch-xlxd.sh            # fetch xlxd sources into .cache/xlxd/
scripts/gen-local.sh                 # local secrets into .local/
scripts/run.sh -m rig                # rig up, run cases, results in out/<utc>/
```

## Settings

All pinned sources, refs, hashes and versions live in `.env.example`; put
local overrides (system under test, transcoder image) in `.env`. Secrets are
generated into `.local/rig.env` and never committed.

The transcoder is not part of this repository. Set `TRANSCODER_IMAGE` (rig)
or `DVT_AMBED_ADDR` (wire cases against a running transcoder). When the wire
cases start the image themselves, `DVT_TRANSCODER_ENV` and
`DVT_TRANSCODER_ARGS` pass extra environment and arguments to it.

## Resource limits

The rig and the host-side test levels run under hard caps (a host once reached
load 185 and 40 GB of swap from unbounded parallel builds and tests). These
rules are mandatory; do not remove them to make a run faster.

| What | Cap | Where |
|------|-----|-------|
| `scripts/run.sh` (docker CLI, pytest pipe, watcher, sampler) | scope `dvt-rig.scope`: 12G memory, swap 0, 800% CPU, 1024 tasks, `DVT_BUILD_JOBS` jobs | `run.sh` re-executes itself through `scripts/contained.sh --name rig` |
| One rig at a time | the unit name `rig` is unique, so `DVT_INSTANCE` values take turns; at most 2 scopes in all, and a scope waits while the host has under 12 GiB free (`DVT_MIN_FREE_GIB`) | `scripts/contained.sh` |
| Rig containers | every compose service has `mem_limit` = `memswap_limit` (no swap), `cpus` and `pids_limit`: small 256m / 0.5 / 128, medium 512m / 2 / 512, tester 2g / 2 / 512 | `rig/compose.yml`, sizes in `.env.example` (`DVT_MEM_*`, `DVT_CPUS_*`, `DVT_PIDS_*`) |
| Containers started by host-side cases and tools | `docker run --memory/--memory-swap/--cpus/--pids-limit`, default 1g / 2 / 512 | `dvtest/limits.py`, `DVT_CTR_*`, `scripts/tshark.sh` |
| Image builds and xlxd netns build | `make -j` follows `DVT_BUILD_JOBS` (default 4), never `nproc` | Dockerfiles (`MAKE_JOBS`), `scripts/build-xlxd-netns.sh` |

Worst case with every profile except `load` at once: 10 medium (5 GiB) +
tester (2 GiB) + 18 small (4.5 GiB) = 11.5 GiB of caps; the `load` profile
adds up to 26 nodes (6.5 GiB) to a core rig (4 GiB). Both stay under the 12G of
the rig scope. Measured peaks are far lower (xlxd about 70 MiB, tester about
90 MiB). The container caps are
separate from the scope: the docker daemon starts the containers, so the
compose limits are what bound them; image builds run in the daemon too, which
is why their parallelism is a build arg.

- `scripts/run.sh` refuses to start (exit 4) when any service of the selected
  profiles, including a prebuilt-image override or
  `.local/compose.override.yml`, lacks the four limits (`dvtest/limits.py`).
  A new compose service must take one of the `x-limits-*` sets.
- A prebuilt system under test that needs more raises `DVT_MEM_MEDIUM` (or the
  `*_CPUS_*`, `*_PIDS_*` values) in `.env`; an OOM kill shows as exit 137 of
  that service in the run log.
- Host-side levels: run `wire` and `netns` pytest, `scripts/build-xlxd-netns.sh`
  and any `docker run` loop inside the scope:
  `scripts/contained.sh --name test -- python3 -m pytest -m netns`. Unit tests
  are light and need no scope. pytest prints `resource scope: NONE` in its
  header when it runs outside one.
- A system-under-test build (for example `cargo build`) started from here also
  goes through `scripts/contained.sh --name build -- ...`; it exports
  `CARGO_BUILD_JOBS`, `RUST_TEST_THREADS` and `RAYON_NUM_THREADS`.
- `scripts/contained.sh --status` shows the running scopes and host memory;
  `--stop NAME` stops one. `--install` (optional, once per host) adds the
  slice `dvt-rig.slice` as a 16G / swap 0 / 1200% / 2048 tasks ceiling over all
  dvt scopes. Without a user systemd bus (a sandbox) the script exits 77;
  `DVT_UNCONTAINED=1` skips the scope for `run.sh` only when something else
  already caps the run.
- No uncapped parallel workers: do not add `pytest -n`, extra thread pools
  sized by `os.cpu_count()` or `make -j$(nproc)`; use `DVT_BUILD_JOBS` and
  fixed worker counts.

Before committing, run `scripts/pii-check.sh`.

`AGENTS.md`, `CLAUDE.md` and `.agents/skills/` are working notes for AI coding
agents; they repeat the rules above in a form those tools load automatically.
