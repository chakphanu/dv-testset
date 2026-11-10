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
and the reload that re-reads the secrets without a TOML change (`SIGHUP`, the fabric `admin/config/reload`) had no HTTP route before SECRELOAD2 (merge `11e19ace`, section below). Every reload of the first case therefore carries one changed comment line
(`# secreload1 reload marker N`): the file hash changes, the configuration does not, and the launcher evaluates the secrets in the
same pass. The reload that re-reads the secrets without any TOML change is covered by the second case (SIGHUP, below). The same reload without a comment change is the SECRELOAD2 route (below). What is not covered: the file watcher; dv-router's own tests cover it.

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

## SECRELOAD2: the admin reload routes

DV-Router merge `11e19ace` (SECRELOAD2) adds the operator path that SECRELOAD1 lacked: a reload of the CURRENT file against the running generation,
over HTTP, without editing the TOML and without a history entry (`openapi-admin.yaml` ReloadPreview / ReloadApplied, `admin.md` "Secret reload").
Both routes need an admin session with step-up.

| Route | Reply |
|---|---|
| `GET /api/v1/admin/config/reload` | `{mode:"preview", etag, changed, no_change, actions, warnings}` and an `ETag` header; `changed` holds only the dotted key names of referenced secret files whose content changed (no bytes, no digests); nothing is applied |
| `POST /api/v1/admin/config/reload` | CSRF header, same origin, JSON body `{}`, `If-Match` = the current file ETag; `{status:"applied", etag, changed, no_change, inc, gen, actions, pending_restart}`; an unchanged reload has `no_change` true and empty `changed` / `actions` |
| errors | 401 no session; 403 `forbidden` (below admin, before anything else) or `step_up_required`; 428 `precondition_required` (no `If-Match`); 412 `etag_mismatch` with `current_etag` (stale or weak `If-Match`); 409 conflict; 422 `validation` (an unreadable secret: the key and path, never content); 429; 503 |

A missing `If-Match` is 428, not 412 (the openapi lists 428 for the POST and `admin.md` says "missing is 428 `precondition_required`, stale is 412"); the case asserts
428 for missing and 412 for stale and weak.

Audit (`GET /api/v1/admin/audit`, served NEWEST FIRST: `Audit::tail` reverses the last n lines; `admin.md` says "newest last" only for `/audit/logins`, so the order of `/audit` is undocumented and the first rig run found it; the case reads the tail oldest first, `dvtest.secret_reload.audit_chronological`; the records of one request share a `ts_ms`): `config.reload` records `requested` (before the launcher is asked, fail closed) and then `applied`, `unchanged`, `invalid` (`diff.changed` =
the key names), or `denied` for a missing / stale / weak `If-Match`; a session below admin is `config.reload` / `config.reload.preview` `denied`. Repeated denials of one kind
inside the denial interval are collapsed (`suppressed`), so the case asserts that a denied record exists, not how many.

### Cases (same file, same fixtures as SECRELOAD1; the second case needs no throwaway value)

The secret kind is the APRS-IS passcode file, as in the first case, NOT a node-link account key: the rig has no node-link client (a Python implementation of the link v1
handshake, link.md Appendix A, would have to be written and could not be run offline) and no `[edges.link]` snippet or key-file mount. The statement "old key refused, new key
accepted" is shown by the aprsc's verdict on the edge's login (`dv_edge_aprsis_verified` 1 with the fixture value, 0 with the throwaway one). The key name is
`edges.aprsis.passcode_file` instead of `edges.link.account[N].secret_file`. See GAPs.

| Case | Stages | Asserted |
|---|---|---|
| `test_secreload2_preview_and_reload_apply_a_replaced_secret` | R0 preview of the unchanged file | 200, exact keys, `mode` preview, `etag` = the `ETag` header = the ETag of `GET config/raw`, `no_change` true, `changed` and `actions` empty |
| | R1 file rewritten in place (same inode and size), preview | `changed` is exactly `["edges.aprsis.passcode_file"]`, `no_change` false, an action restarts `dv-edge-aprsis` and names `content changed`, the file ETag is unchanged, the edge process is still the same 3 s later (a preview applies nothing) |
| | R2 `POST` with `If-Match` = the preview's ETag | 200 `applied`, exact keys, `changed` = the key, `pending_restart` empty; the TOML ETag and the number of history entries (`GET /config/history` `items`) are unchanged; new `dv-edge-aprsis` process, its restart counter +1; `dv_edge_aprsis_verified` 1 -> 0 |
| | R3 second `POST` | 200, `no_change` true, `changed` and `actions` empty, `(inc, gen)` unchanged, nothing restarted; the preview agrees |
| | R4 fixture value back by rename, preview, `POST` | `changed` the key, restart again, verified 0 -> 1 |
| | R5 file mode 000, preview and `POST` | both 422 `validation`: `errors[]` of `{path, pointer, msg}` with `path` = the key, the file path in `msg`, `current_etag` unquoted, no `actions[]`; process, counters and the verified login unchanged; mode restored |
| | R6 audit tail | (a successful preview and a failing preview write no record; the PUT applies of the first case leave `config.reload` `ok` rows, which the subsequence ignores) the admin's `config.reload` records, oldest first, contain in order: requested, applied (`changed` = the key), requested, unchanged (`[]`), requested, applied, requested, invalid (key and path names) |
| | R7 secrecy | no value (old or new) in any reply, the audit tail, a note or any file of `out/<run>` visible now (the audit file is `sut-state/admin/audit.jsonl`); `run.sh` scans the collected logs |
| `test_secreload2_guards_and_preconditions` | G1 | no session: GET and POST 401 |
| | G2 | viewer and operator: GET and POST 403 `forbidden` |
| | G3 | admin POST without `If-Match` 428 `precondition_required`; a stale, a weak, a `*` and a malformed `If-Match` 412 `etag_mismatch`, each with `current_etag` = the file's ETag; the edge is not restarted |
| | G4 | audit: `config.reload` `denied` for the admin's refusals; at least one `config.reload` or `config.reload.preview` `denied` record for each of the viewer and the operator accounts; every `config.reload.preview` record is `denied`; no secret value in the tail |
| | G5 | a password-only login of the TOTP-enrolled admin account is refused: 401 `totp_required`, no session, no cookie (a login with a valid code counts as a step-up, so no session without a window can be made); then the 5 minute window of the stepped-up session is waited out (strictly more than 300 s, the session kept alive by a read every minute): GET and POST 403 `step_up_required`, nothing restarted; the audit has `step_up` `denied` |

Skips: the route answers 404 (an image without SECRELOAD2; `DVT_SECRELOAD2=on` turns that into a failure); the edge is not running; no aprs profile. Run (about 12 minutes: the first
case about 4 minutes, the second dominated by the 5 minute step-up wait; `DVT_SUT_CONFIG_RW` is NOT needed, the routes do not write the file):

```sh
DVT_SECRELOAD2=on DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-aprsis.toml \
  scripts/run.sh -m rig --profile web,aprs -- cases/test_secret_reload.py -k secreload2
```

Unit tests: `cases/unit/test_secret_reload2_case.py` runs both cases against a fake admin service that follows the documents (roles, step-up window on a fake clock, `If-Match`, audit, the
launcher's restart), and checks that a service that accepts a weak `If-Match`, lets a viewer reload or restarts nothing for a replaced secret fails them; reply shapes:
`dvtest/secret_reload.py` (`preview_problems`, `applied_problems`, `reload_audit`) with `cases/unit/test_secret_reload_helpers.py`.

Shapes confirmed by dv-router (merge `11e19ace`, reply of 2026-11-10): `changed` is a sorted, de-duplicated list of dotted keys, array rows as `[N]`
(`edges.aprsis.passcode_file`, `edges.link.account[N].secret_file`, `edges.link.uplink[N].secret_file`, `edges.allstar.account[N].secret_file`,
`edges.allstar.node[N].password_file`, `web.turn_secret_file`, `callhome.hash_file`); the reload is a quiet restart of `dv-edge-aprsis` (SIGUSR1, about 1 s), not live; an
`actions[]` item is `{class:"restart", child, path, changed_secret_keys:[...], impact}` (the case accepts the key in `changed_secret_keys` or in the `impact` text); problem+json is
`{type, title, status, detail, instance, code, errors?, current_etag?, retry_after?}` with `current_etag` UNQUOTED (`f-<16 hex>`) and the `ETag` header quoted; a 422 is `code`
`validation`, `errors[]` of `{path, pointer, msg}` plus `current_etag` and no `actions[]`, the file path only inside `msg`; 412 covers a stale, weak, `*` and malformed `If-Match`, 428 a
missing one; `GET /config/history` is `{total, items:[{seq, etag, replaced_by, ts_ms, user, comment, source}]}` newest first; a login with a valid TOTP code opens the 300 s step-up
window, a password-only login does not. Audit: a successful preview writes NO record; `config.reload.preview` appears only as `denied` (a role below admin); a POST writes
`config.reload` `requested` and then `applied` / `unchanged` (also `conflict`, `refused`, `invalid`, `unavailable`); `denied` for a missing or stale `If-Match` or a role below admin;
a missing step-up is the action `step_up`, result `denied`; denials are coalesced per session (the case asserts that a record exists, not how many). Two product text defects
are NOT asserted (the 428 `detail` mentions `base_etag`; the unreadable-file `msg` says mode 0600).

Not confirmed on an image: the exact `msg` text of the 422.

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
`dv_launcher_child_restarts_total{child="dv-edge-aprsis"}` in the hub metrics 60 s after the case started polling (the case polls: the launcher families reach the hub with the first 10 s
publication, run `20261110T085417Z` read them 7 s after the SUT start and skipped both cases for nothing); with `DVT_SECRELOAD1=on` or `DVT_SECRELOAD2=on` a missing series after the bound is a FAILURE, not a skip; the capability probe finds no
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
* Not covered: the watcher, a node-link account key (see SECRELOAD2 above: no link client in the rig), other secret kinds (link, AllStar, TURN, callhome),
  the "absent default passcode" rule (an unset `passcode_file` selects `<state>/secrets/aprs-passcode`), a symlink, FIFO or oversize file.
* The restart counter and the verified gauge reach the hub every 10 s: "nothing restarted" is judged 12 s after the reload, together with the
  process identity (`proc-stat`, immediate). A restart of the same child for another reason during a wait would fail the case.
* The scan of the collected logs and the launcher-log line of H4 are verdicts of `run.sh` (exit 5), not pytest results.
* The admin reply shapes (`actions[]`, the problem body of a refusal) are matched as text; they are not specified field by field
  (`openapi-admin.yaml`: "ConfigResult" has no schema).
