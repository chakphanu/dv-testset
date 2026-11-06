# Config-load cases (COMPAT1: an old key must fail with a clear error)

DV-Router COMPAT1 removed the backward-compat shims of its config. A key or shape of an older image must now be REFUSED at load time with an
error that names the file, the line and the key; it must never be ignored. These cases check exactly that, on the real image, without the rig.

- Case file: `cases/test_config_load.py` (marker `cfgload`; not `-m rig`, no compose project, no ports).
- Pure part (mutation table, verdict rules): `dvtest/cfgload.py`; unit tests `cases/unit/test_cfgload.py`, `cases/unit/test_config_check.py`.
- Runner: `dvtest.tools.config_check` in mode `check` = `dv-router config check FILE` in a throw-away container (`docker run --rm --network none`,
  read-only root, all capabilities dropped, `no-new-privileges`, the caller's uid, the `dvtest.limits` caps, the TOML mounted read-only,
  empty 0600 stand-ins for `*_file` secrets, no ports). One container at a time, 60 s timeout each; the run is about 10 s for 19 cases.

## How to run

```
DVT_CFGLOAD_IMAGE=dv-router:wip-488b89d DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=/path/to/rig-dv-router.toml \
  scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_config_load.py -s
```

| Env | Meaning |
|-----|---------|
| `DVT_CFGLOAD_IMAGE` | the compat1 image (needs the `config check` subcommand); unset = skip |
| `DVT_SUT_GEN` | must be `compat1`; anything else = skip (the old keys load on older images, with a warning) |
| `DVT_SUT_CONFIG` | the rig's template (the same file the rig uses); unset or missing = skip |
| `DVT_CFGLOAD_EXTRA` | optional `a:b` list of snippets merged like `DVT_SUT_CONFIG_EXTRA` (default none) |
| `DVT_OUT` | results directory; the exact error text of every case goes to `$DVT_OUT/config-load/results.json` (else the pytest tmp dir) |

One case = the template rendered for `compat1` through `dvtest.sut_config.render` (which already writes the NEW spellings for the rig), one mutation
that puts the old spelling back, the check. A single file can also be checked by hand:
`python3 -m dvtest.tools.config_check --image dv-router:wip-488b89d --template T --gen compat1`.

## What a case asserts

- REJECT: the exit status is not 0 (0 = SILENT SUCCESS, the loudest failure) and one of 1 or 2 (wip-488b89d: 2); at least one
  `FILE:LINE:COL: KEY: MESSAGE` line; the file is `dv-router.toml`; the line is the line of the mutation; the key is the one named in the table
  (a TOML syntax error has none); the message is not empty and contains the hint (usually the new spelling).
- ACCEPT (baseline and the controls = the same thing in its new spelling): exit status 0 and no error line.

## Cases (wip-488b89d, the COMPAT1 image)

| Case id | Mutation | Verdict | Message (key; text after the key, cut) |
|---------|----------|---------|----------------------------------------|
| `baseline` | the rendered rig template | loads (2 `ids.*` seed-file warnings) | |
| `admin-totp` | `[admin] totp = true` | rejected, exit 2 | `admin.totp: unknown field `totp`, expected one of ..., `totp_required`, ...` |
| `web-operator_totp` | `[web] operator_totp = true` | rejected, exit 2 | `web.operator_totp: unknown field ...` (lists `totp_required`) |
| `xlx-peer-no-kind` | `[[edges.xlx.peer]]` row without `kind` | rejected, exit 2 | `edges.xlx.peer[0]: missing field `kind`` (line of the row header) |
| `edges-mmdvm` | `[edges.mmdvm]` | rejected, exit 2 | `edges.mmdvm: unknown field `mmdvm`, expected one of ..., `dmrhb`, ...` |
| `fabric-acl` | `[fabric]` with `acl = true` | rejected, exit 2 | `fabric: unknown field `fabric`, expected one of `schema`, `node`, ...` (line of the header, column 2) |
| `fabric-empty` | an empty `[fabric]` table | rejected, exit 2 | as above |
| `access-allow_file`, `access-deny_file` | `[access] allow_file` / `deny_file` | rejected, exit 2 | `access.allow_file: unknown field `allow_file`, expected `allow` or `deny`` |
| `callhome-interlink_file` | `[callhome] interlink_file` | rejected, exit 2 | `callhome.interlink_file: unknown field ...` |
| `web-unknown-key` | typo `enabel` in `[web]` | rejected, exit 2 | `web.enabel: unknown field `enabel`, expected one of `enable`, ...` |
| `web-wrong-type` | `[web] enable = "yes"` | rejected, exit 2 | `web.enable: invalid type: string "yes", expected a boolean` (column of the value) |
| `log-level-wrong-type` | `[log] level = 5` | rejected, exit 2 | `log.level: wanted string or table` |
| `toml-syntax` | `enable = = false` | rejected, exit 2 | `extra `=`, expected nothing` (no key; a second error line follows) |
| `admin-totp_required`, `web-totp_required`, `xlx-peer-kind`, `access-allow-inline`, `access-deny-inline` | the new spelling | loads | control |

The full text per case is in `results.json` (`error_text`, `error_key`, `error_line`, `error_col`, `exit_code`, `loaded`, `problems`).

## Not a case, and notes

- `DV_MMDVM_*` environment names are silently ignored on compat1 (only `DV_DMRHB_*` counts). There is no file to refuse, so no load test can
  show it; `scripts/run.sh` writes `DV_DMRHB_*` only for compat1 (docs/rig.md, "compat1").
- Removed keys are reported as generic `unknown field` errors with the list of the accepted fields; there is no "was renamed to ..." text
  (the pre-COMPAT1 warning said `web.operator_totp is ignored; use web.totp_required`). The new spelling shows only in the accepted list
  (`totp_required`, `dmrhb`, `allow` / `deny`); for `fabric.acl`, `callhome.interlink_file` and `access.*_file` the list has no successor.
- The exit status of an invalid file is 2 (not 1); `evaluate` accepts 1 or 2 and records the real one.
- `config check` on an older image (wip-dce9d0a) is not a subcommand there: it prints a usage text and exits 2; `judge_check` reports that as
  `unsupported`, so a wrong image never reads as "every old key was rejected".
- Warnings in `config check` carry no `config warning:` word (unlike the start path); a positioned line is a warning only when the exit status is 0.
