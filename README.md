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

Before committing, run `scripts/pii-check.sh`.

`AGENTS.md`, `CLAUDE.md` and `.agents/skills/` are working notes for AI coding
agents; they repeat the rules above in a form those tools load automatically.
