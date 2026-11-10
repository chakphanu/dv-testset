<!-- SPDX-License-Identifier: MIT -->
# DV-Router secret-file reload (SECRELOAD1): black-box case

NOT RUN ON A RIG yet. Written against the DV-Router documents of the SECRELOAD1 work (config.md, "Actions" and the paragraph
after the reload table; status.md, launcher metrics; testing.md, "Secret-file reload regression"), for the next image.

## What is specified

A configuration reload is the unit that applies a changed referenced secret. config.md: "Referenced secret content is part of the
key that names the file, so a content change gets exactly that key's reload class." The launcher's configuration authority reads the
bytes during every evaluation (start, reload, admin check, offline `config check`), keeps only private SHA-256 fingerprints in
memory, and does not use size, mtime or inode as identity: "a same-size replacement or rewrite within one timestamp tick must still
be detected." Consequences, each asserted here for one secret kind:

| Rule | Where |
|---|---|
| a secret file replaced at the same path and a reload restart the owning child, here `dv-edge-aprsis` (`edges.aprsis.passcode_file` contents are Restart(child)); no `restart_child` is needed | config.md, reload table |
| the preview names the dotted key and says `content changed`, and reveals no value | config.md, after the table |
| an unchanged secret (and no other change) restarts nothing | config.md: "An unchanged secret causes no restart" |
| an unreadable or missing explicit secret refuses the reload with the key and the path and keeps the running generation | config.md: "a missing, unreadable or invalid secret refuses the reload with its key and path and keeps the running generation" |
| fingerprints and secret bytes never enter a snapshot, status, log, preview or diff | config.md |

The secret kind of this case is the APRS-IS passcode of the private aprsc (profile `aprs`): cheap, no extra container, and the value in
effect is observable. TURN (profile `nat`, Restart(dv-svc-web) and a 36 s RTC recovery after a web restart with a journal) is not covered.

## The observation

The passcode of an APRS-IS login is a public function of the callsign (`dvtest/aprs_passcode.py`); the private aprsc logs a login with
any other value in as `unverified` (and drops its packets). So the fixture value makes the edge verified and a throwaway value of the
same length makes it unverified, with the same TCP login accepted: `dv_edge_aprsis_verified` (hub metrics, the edge's own view: the
server's login response said `verified`) is 1 with the fixture value and 0 with the throwaway one. The aprsc has no HTTP port and its
log shows no login line, so the edge's metric is the observation of "the new value was used for a fresh login".

## Files and rig changes

* `cases/test_secret_reload.py`: one case, `test_secreload1_passcode_file_replaced_at_the_same_path`; unit tests of its file and
  step-up helpers: `cases/unit/test_secret_reload_case.py`.
* `dvtest/secret_reload.py`: pure helpers (throwaway value of the same size, comment marker, scanning, admin reply readers, metrics
  readers) and the scanner CLI `python3 -m dvtest.secret_reload scan ...`; unit tests `cases/unit/test_secret_reload_helpers.py`.
* `scripts/run.sh`: `DVT_SUT_CONFIG_RW=1` (below), the control action `hup xlxd-sut` (`scripts/lib/hup.sh`, `dvtest/control.py` `HUP_ALLOWED`;
  below), and two end-of-run checks over the collected output: the scan of `out/<run>/secreload/scan.json` and the launcher-log check of
  `out/<run>/secreload/log-expect.json`.

The reload is the admin API's: `PUT /api/v1/admin/config/raw?mode=apply` with `If-Match: <ETag of GET /config/raw>` after a step-up
(TOTP; the case renews the 5 minute window only when it is old). dv-svc-admin applies nothing for byte-identical text (`noop: true`),
and the reload that re-reads the secrets without a TOML change (`SIGHUP`, the fabric `admin/config/reload`) has no HTTP route yet (dv-router is
opening a unit for one). Every reload of the first case therefore carries one changed comment line
(`# secreload1 reload marker N`): the file hash changes, the configuration does not, and the launcher evaluates the secrets in the
same pass. The reload that re-reads the secrets without any TOML change is covered by the second case (SIGHUP, below). What is not covered: the file watcher and `admin/config/reload` through fabric; dv-router's own tests cover them.

`DVT_SUT_CONFIG_RW=1` (native launch only): the admin service writes the configuration by writing a temporary file and renaming it over
the config, which the usual single-file read-only mount refuses. With the variable `run.sh` copies the rendered config to
`out/<run>/xlxd-config/xlxd-sut/rw/dv-router.toml`, mounts that directory read-write at `/etc/dv-router-rw` and starts the image with
`--config /etc/dv-router-rw/dv-router.toml`. The rendered file next to it is no longer the live one: read the live one through the admin
API. Do not combine with cases that edit the rendered file (echo E3 / E7). The aprs secrets stay at `/etc/dv-router/aprs-secrets`
(read-only for the SUT; the tester rewrites the file in `out/<run>/aprs-secrets/`, same uid).

## The SIGHUP variant

`test_secreload1_sighup_reload_of_unchanged_toml` is the documented operator path for "same TOML, referenced secret file replaced"
(config.md: SIGHUP, and `admin/config/reload`, re-read every referenced secret even when the TOML hash is unchanged). It needs no admin
API, no TOML change and no `DVT_SUT_CONFIG_RW`: the case rewrites the passcode file and sends SIGHUP to the launcher through a new,
narrow control action.

`hup xlxd-sut` (`scripts/lib/hup.sh`, mirrored by `dvtest.control.HUP_ALLOWED` / `rig_control.hup()`, checked by `cases/unit/test_control.py`):
`docker kill --signal=HUP` on the dvt project's `xlxd-sut` container, that is PID 1, the launcher. Nothing else: no other service
(`dvr-peer` included), no other signal, no word after the service, a container id that is not hex is refused; `kill-proc` still never signals
the launcher. DV-Router allowed it for this case only; do not use it elsewhere. `<id>.out` has `t_hup=<epoch s>`.

| Stage | Action | Asserted |
|---|---|---|
| H0 | baseline (a SIGHUP repair first if an earlier case left the throwaway value running) | the edge is verified; process, restart counters read |
| H1 | file rewritten in place (same inode, same size) with the throwaway value, TOML untouched, `hup xlxd-sut` | a new `dv-edge-aprsis` process within 30 s (no restart: skip, or fail with `DVT_SECRELOAD1=on`); its restart counter +1 and the sum over all children +1 (nothing else restarted); `dv_edge_aprsis_verified` 1 -> 0 |
| H2 | a second SIGHUP, nothing changed | nothing restarted, login unchanged |
| H3 | the fixture value back by rename, SIGHUP | restart again (counter +1, nothing else), verified 0 -> 1 |
| H4 | file mode 000, SIGHUP | process, counters and the verified login unchanged (the old login stays up); after the run `run.sh` finds a launcher-log line at or after the signal that names the key `passcode_file` and the path, and records its shape (digits replaced, no value) in `out/<run>/secreload/log-shape.txt`; the mode is put back |
| H5 | file readable again, SIGHUP | nothing restarted (same content as the running generation) |

The launcher log is not readable in-case (no control action reads it), so H4's log line is an end-of-run verdict of `run.sh` (exit 5), like the value
scan; a SIGHUP that was ignored would show as a missing line. The case writes `out/<run>/secreload/log-expect.json` ({t_hup, key, path}, no value).

## Run it

```sh
DVT_SUT_CONFIG_RW=1 DVT_SECRELOAD1=on \
XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-aprsis.toml \
  scripts/run.sh -m rig --profile web,aprs -- cases/test_secret_reload.py
```

The SIGHUP variant runs in the same rig run after the first case and needs none of `DVT_SUT_CONFIG_RW`, the web profile or the admin accounts: `DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-aprsis.toml scripts/run.sh -m rig --profile aprs -- cases/test_secret_reload.py -k sighup` (about 3 minutes; `-k` leaves the first case out; the data worker of `data-ident.toml` is what `dp15-aprsis.toml` needs, the web profile is not). About 4 minutes for the first case (five reloads, four 12 s waits for a hub refresh, restarts). One rig, nothing else at the same time (the case restarts
`dv-edge-aprsis` three times). `DVT_SECRELOAD1=on` turns "image without SECRELOAD1" from a skip into a failure; leave it unset on an
image that is not expected to have the rule.

Skips (with the reason in the report): no `out/<run>/aprs-secrets/passcode` (no `--profile aprs`); no `dv_edge_aprsis_verified` or
`dv_launcher_child_restarts_total{child="dv-edge-aprsis"}` in the hub metrics (the edge is not running); the capability probe finds no
rule (below); the configuration file is read-only (no `DVT_SUT_CONFIG_RW=1`). Fails: the edge is not verified at the start.

## Stages

| Stage | Action | Asserted |
|---|---|---|
| 0 | baseline; capability probe | the edge process, `dv_launcher_child_restarts_total{child=dv-edge-aprsis}` and the verified gauge are read; with the throwaway bytes in the file an admin PREVIEW of the unchanged TOML lists a restart of `dv-edge-aprsis` whose action names `passcode_file` and `content changed`; the file is put back at once (a preview applies nothing) |
| 1 | reload with a comment change only | HTTP 200; no restart action in the reply; after 12 s (one hub refresh) the process is the same, the restart counters (this child, all children) did not move, the login is still verified |
| 2a | file mode 000, reload | refused (HTTP >= 400); the reply names the key `passcode_file` and the path as the SUT sees it; process, counters and verified login unchanged (the old login stays up) |
| 2b | file renamed away, reload | the same |
| 2c | file back, reload | HTTP 200, no restart action, nothing restarted (same content) |
| 3 | file rewritten in place (same inode, same size) with the throwaway value, reload | HTTP 200; the restart action names the key and `content changed`; a new `dv-edge-aprsis` process within 30 s; the restart counter rose by exactly 1; `dv_edge_aprsis_verified` becomes 0 (the new value is what the edge logged in with) |
| 4 | reload again, same secret | HTTP 200, no restart action, nothing restarted |
| 5 | file replaced atomically (new inode, rename) with the fixture value, reload | restart again (action, new process, counter +1) and `dv_edge_aprsis_verified` is 1 again |
| 6 | secrecy | no value (old or new) in any admin reply, in any note, or in any file of `out/<run>` visible at that moment (the tree scan skips the wire captures: a passcode crosses the wire to the aprsc by protocol); the values are left in `out/<run>/secreload/scan.json` (mode 0600) and `run.sh` scans the collected logs and files after the run, like the TURN secret: exit 5 when found, paths and line numbers only |

Values: the throwaway value is drawn at run time with the same number of digits as the fixture one, different from it, and not present as
a bare token in any text file of `out/<run>` at the start (so that a later hit is the value and not a port or a pid). The old value is
searched after a word (`pass`, `passcode`, `secret`, `password`) or alone on a line, the new one also as a bare token. Notes carry
lengths only, and a note that would contain a value stops the case. Nothing here is a credential: the passcode is a public function of a
synthetic callsign; the handling follows the secret rules of the repository so that the same code can carry a real secret kind.

## Limits

* One secret kind (APRS-IS passcode). A `restart` of the owning child is proven by a new process and the restart counter, not by the child's
  other effects. The value in effect is shown by the aprsc's verdict on the login as seen by the edge.
* Not covered: `admin/config/reload` through fabric (no HTTP route yet), the watcher, other secret kinds (link, AllStar, TURN, callhome),
  the "absent default passcode" rule (an unset `passcode_file` selects `<state>/secrets/aprs-passcode`), a symlink, FIFO or oversize file.
* The restart counter and the verified gauge reach the hub every 10 s: "nothing restarted" is judged 12 s after the reload, together with the
  process identity (`proc-stat`, immediate). A restart of the same child for another reason during a wait would fail the case.
* The scan of the collected logs and the launcher-log line of H4 are verdicts of `run.sh` (exit 5), not pytest results.
* The admin reply shapes (`actions[]`, the problem body of a refusal) are matched as text; they are not specified field by field
  (`openapi-admin.yaml`: "ConfigResult" has no schema).
