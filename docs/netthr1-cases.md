<!-- SPDX-License-Identifier: MIT -->
# NETTHR1a / NETTHR1b: fabric session cap, HTTP admission caps, one DNS pool, thread ceilings

Cases for the candidate image `dv-router:wip-eeb44b4` (DV-Router code `a8692074`). WRITTEN OFFLINE, NOT RUN: the rig was busy with another chain when these were
written, so every time below is an estimate and every "expected" value comes from the DV-Router documents, not from a run. Files: `cases/test_netthr1_config.py`
(host side, `config check`), `cases/test_netthr1_rig.py` (rig), `cases/test_echo.py` (FLK11, F11a / F11b), pure helpers `dvtest/netthr1.py`, `dvtest/dnshole.py`
(unit tests `cases/unit/test_netthr1_lib.py`), snippets and templates `rig/config/dv-router/netthr1-*.toml`.

## What the commits say

| Commit | Subject | Documents read (all at `a8692074`) |
|--------|---------|------------------------------------|
| `fce97cd3` NETTHR1a | `node.fabric_max_sessions` (4..=128) with a derived default; the gauge `fabric.sessions {open, cap, peak}`; Net scheduler ceiling `cap + 2` | `docs/design/config.md` "Fabric session admission", `fabric.md` "Process runtime configuration", `status.md` "Fabric session gauge", `deployment.md` "Process schedulers", `docs/project/testing.md` (K6b, `session_admission`) |
| `c8f9b4ed` NETTHR1b | `web.` / `admin.` `max_file_streams` (1..=64) and `login_slots` (1..=16) from configuration, one cancellable DNS pool per edge process (vendored c-ares, 5 s), per-profile ceilings | `config.md` (HTTP caps, edge settings), `admin.md`, `web.md`, `status.md` "HTTP work admission", `fabric.md` "Edge resolver shell", `deployment.md` "Hotspot memory planning" |
| `caed3841` FLK11 | the echo admits an over by arrival order relative to a config change | `docs/design/echo.md` "Configuration and reload" |

Numbers used (each is a constant in `dvtest/netthr1.py` with its source): derived cap = `clamp(rooms + 19 edge kinds + 9 service kinds + data rows + transcoder rows + 8, 4, 128)`
(63 for 26 rooms with the default `pkt` row, 38 for the one-room hotspot, 41 for rooms A-D); a cap needs `planned children + 1`; per-process thread ceiling = 3 (main, async worker, TX) +
blockers: 4 for rooms, edges, data and codec workers, status; 5 recorder and basemap; 6 iddb (two seed readers); `7 + login_slots + max_file_streams` for admin and web
(27 for 4 / 16, 11 for 2 / 2); the launcher 6 + `cap + 2`; +2 `edge-dns-*` workers for an edge with host-name rows (xlx, nxdn, p25, m17, link). Headline totals: router K6b fixture
**251** (186 outside Net + 63 + 2), exact hotspot preset **95** (55 + 38 + 2), hotspot with one host-name link uplink **101** (61 + 38 + 2). 251 is that fixture's ceiling, not a node-wide
maximum: any other process set gets the SUM of its own per-process ceilings, which is what T1 judges (the headline is asserted only when the process set equals the documented one).

## Switches (tester environment, `rig/compose.yml`; `cases/unit/test_tester_env.py` guards it)

| Switch | Meaning |
|--------|---------|
| `DVT_NETTHR1=on` | gates every NETTHR1 case, host and rig (a skip naming it otherwise) |
| `DVT_NETTHR1_DNS=on` | adds D1 (needs `DVT_SUT_RESOLV=30`, host side) |
| `DVT_NETTHR1_SWEEPS` | proc-stat sweeps of the thread cases (default 4) |
| `DVT_FLK11=on` | gates F11a / F11b of `cases/test_echo.py` |
| `DVT_PIDS_SUT`, `DVT_CPUS_SUT` | host caps of the SUT container (compose `pids_limit`, `cpus`; passed to the tester only so that cases record them). `DVT_CPUS_SUT` is new: `cpus: ${DVT_CPUS_SUT:-${DVT_CPUS_MEDIUM:-2}}` on `xlxd-sut` alone, default 2 in `.env.example` |
| `DVT_SUT_RESOLV=<octet>` | host side, `scripts/run.sh`: the SUT's `/etc/resolv.conf` names only `<NET>.<octet>` (read-only file in `out/<run>/xlxd-config/xlxd-sut/`) |

Rig plumbing added with the cases (all small, all inert without the variable): `proc-stat` also reads `dv-edge-radio`, `dv-edge-link`, `dv-svc-basemap`, `dv-svc-echo`
(`scripts/lib/procstat.sh`, `dvtest/control.py`, `cases/unit/test_procstat.py`; read-only, the hotspot preset needs the first three); `DVT_CPUS_SUT`; `DVT_SUT_RESOLV`.

## Cases

Ask (a) = thread ceilings under a cgroup `TasksMax` (T1 to T3). Ask (b) = the time of one argon2 login hash on a small board (H4, H2). Document references name the DV-Router file at `a8692074`.

### Host side (`cases/test_netthr1_config.py`, marker `cfgload`, no rig)

| Case | Asserts | Document |
|------|---------|----------|
| C1 | `config check` prints `fabric_max_sessions: N (default)` with N equal to the formula over the rendered rig template; the hotspot preset prints `38 (default)` and exactly the eight children `dv-room[B] dv-edge-radio dv-data-worker[pkt] dv-svc-iddb dv-svc-status dv-svc-basemap dv-svc-admin dv-svc-web` | config.md "Fabric session admission", deployment.md table, `real_children/hotspot.rs` CHILDREN |
| C2 | cap 3, 0, 129, 1000 refused with `node.fabric_max_sessions: must be 4..=128 (the router Net blocker pool supports at most 128 sessions); got N`; -1 refused | config.md, `fabric_sessions.rs` |
| C3 | cap 4 names the requirement `cap 4 is below R required for P planned children plus 1 reserved session` with R = P + 1; cap R accepted and printed `(configured)`, cap R - 1 refused with the same numbers; 128 accepted; the hotspot preset needs 9 (8 + 1) | config.md, testing.md ("cap 9 ... eight children plus one observer") |
| C4 | `web.` and `admin.` `max_file_streams` 1 and 64 accepted, 0 and 65 refused (`must be 1..=64 (one reader thread per streamed body); got N`); `login_slots` 1 and 16 accepted, 0 and 17 refused (`must be 1..=16 (about 19 MiB per password job); got N`) | config.md HTTP caps, `http_caps.rs` |

### Rig (`cases/test_netthr1_rig.py`, marker `rig`)

| Case | Config | Asserts | Document |
|------|--------|---------|----------|
| G1 | web profile | `dv_fabric_sessions{kind}` has exactly `open cap peak`, whole numbers, `open <= peak <= cap`; `cap` equals the derived default of the rendered config; `open` covers the children of the launcher (one allowed to be starting); more than two tool sessions above the children is a FINDING | status.md "Fabric session gauge" |
| G1b | web profile | the admin status body carries `sessions {open, cap, peak}` somewhere (`summary.fabric` / `data.fabric` / `launcher.fabric`; the nesting is searched, not fixed) with the same cap | status.md, `status/testdata/contracts/README.md` |
| G2 | web profile | `kill-proc dv-edge-xlx KILL`: the child is respawned, `open` is back within 1, `peak` did not fall, `cap` did not move | status.md ("`peak == cap` records saturation even after tools disconnect"), fabric.md |
| G4 | web + `data-ident.toml` | two more `[[data.worker]]` rows fit: both spawn without a restart and the RUNNING cap does not move (a cap change is `pending_restart`) | config.md "Reload class is Refuse", `native/fabric_session_tests.rs` |
| G3 | web + `data-ident.toml` | `cap + 5` more rows do not fit: the reload is invalid, NOT ONE new data worker is spawned, cap and peak unchanged; the text `cap N is below M required for K planned children plus 1 reserved session` is looked for in the admin status (a FINDING when absent) | config.md, `fabric_session_tests.rs` |
| H0 | web profile | `dv_web_capacity_refused_total{reason}` exists with exactly `login_slots` and `file_streams`, zero-valued from the start, for web and admin (a hub label that cannot tell the two apart is a FINDING) | status.md "HTTP work admission" |
| H1 web / admin | `netthr1-http-caps.toml` | with cap 1 (web) / 2 (admin): that many held bodies above 8 MiB (the tester holds a raw HTTP GET unread, 40 MiB sparse file in `static_dir`), the next two answer 503 + `Retry-After: 1` + `Cache-Control: no-store`; a HEAD of the file still answers 200; closing one stream frees a slot; the counter moves by exactly the 2 refusals, not for the other service or reason | admin.md, web.md, config.md, deployment.md |
| H2 web / admin | `netthr1-http-caps.toml` | 9 concurrent logins at 1 (web) / 2 (admin) slots: only 200 / 429 / 503 (401 allowed for admin, whose password-only login answers 401 once TOTP is enrolled), every 503 has `Retry-After: 1`, at least one 503 when the measured hash is slow enough (otherwise SKIP with the reason: set `DVT_CPUS_SUT=0.25`), `login_slots` moves by exactly the 503 count, `file_streams` does not, a login afterwards still works (a busy refusal is not a failure and does not lock) | admin.md "Rate limits", web.md |
| H4 | any web run | median of 7 logins and of 7 requests that hash nothing (`/api/v1/public/state`); their difference is one argon2 hash as the rig sees it; recorded with `DVT_CPUS_SUT` in `out/<run>/web/netthr1-login-time.json` | deployment.md "Hotspot memory planning" (19 MiB per hash) |
| T1 | any | `SWEEPS` sweeps of `proc-stat` over every process name: each process under its documented ceiling, the sum under the sum of ceilings (the headline 251 / 95 / 101 when the process set is the K6b one), no process gone, new or restarted | deployment.md "Process schedulers", crates/README.md "Process thread budgets" |
| T2 | `netthr1-http-caps.toml` | the same under logins at 1 per 1.1 s and held streams: web at most `7 + 1 + 1 = 9`, admin at most `7 + 2 + 2 = 11` (the old 27 would hide a cap that does nothing) | deployment.md |
| T3 | pids limit at the ceiling | only when `DVT_PIDS_SUT` is the documented total + 1 (docker-init) + at most 2: nothing restarts, the sum stays under the limit - 1. A thread beyond a ceiling fails to spawn and shows as a restart (ask a) | testing.md "Set `TasksMax` from 95 ... 101" (deployment.md) |
| D1 | `DVT_NETTHR1_DNS=on`, `DVT_SUT_RESOLV=30` | the tester answers `*.ok.dvt.test` and never answers anything else. Five M17 peer rows are appended live (four never-answered names, one answered name) and the edge restarts. The first two hung lookups start together, the next two start at 4.6 to 5.9 s (the first two were cancelled at 5 s), the answered name is asked at 9.6 to 11.2 s (those were cancelled at 10 s); `dv-edge-m17` never has more than 6 threads (4 + 2 workers; one per row would be 9+); the answered peer is contacted by the SUT. c-ares alone would give up at about 6 s: the window 4.6 to 5.9 s tells the deadline from the library's own end | deployment.md ("Each running lookup is cancelled at 5 s ... Two dead names therefore release both workers by their deadlines"), fabric.md "Edge resolver shell", m17.md |

### FLK11 (`cases/test_echo.py`, `docs/echo-cases.md` "FLK11")

| Case | Asserts |
|------|---------|
| F11a | E off, a 10 s over, E turned on 1.5 s in: the over is not replayed; the next over is replayed once (0.8 to 2.0 s after its end) |
| F11b | E on, a 10 s over, E turned off 1.5 s in: that over is replayed once (10 s +- 1 s); the next over is not replayed |

## Commands and times (all estimates; none was run)

Image `dv-router:wip-eeb44b4`, template `<template>` = the rig's `rig-dv-router.toml`. `scripts/run.sh` takes the rig lock and runs inside `dvt-rig.scope`; wait for the other chain to finish.

```sh
# 0. unit (a few seconds, no docker)
python3 -m pytest cases/unit/test_netthr1_lib.py cases/unit/test_procstat.py cases/unit/test_tester_env.py cases/unit/test_echo_lib.py -q

# 1. config check, host side, about 1 minute (about 45 throw-away containers one after the other)
DVT_NETTHR1=on DVT_CFGLOAD_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \
  scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_netthr1_config.py -q

# 2a. rig, web profile at the default cpu: gauge, reload, counters, large-body caps, threads under load; about 14 minutes (3 for G1 to G2, 5 for G3/G4,
#     3 for H0 to H1, 3 for T1, T2)
XLXD_SUT_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_USRP_DYNAMIC=1 DVT_TRANSCODER_FAKE=1 \
  DVT_NETTHR1=on DVT_SUT_CONFIG=<template> \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/data-ident.toml:rig/config/dv-router/netthr1-http-caps.toml \
  scripts/run.sh -m rig --profile web -- cases/test_netthr1_rig.py -k "not h2 and not h4"

# 2b. the same command at a small board's cpu, only the logins (ask b): about 6 minutes (the 45 processes of the rig template start slowly at 0.25 cpu;
#     the cases wait up to 90 s for the first gauge)
... DVT_CPUS_SUT=0.25 ... scripts/run.sh -m rig --profile web -- cases/test_netthr1_rig.py -k "h4 or h2"

# 3. thread ceilings of the documented process sets, no web profile, no extras (each about 4 minutes); then the same with the pids limit at the ceiling (T3)
#    router K6b fixture: 251 total  (DVT_PIDS_SUT=252 = 251 + docker-init)
#    hotspot preset:      95 total  (DVT_PIDS_SUT=96)
XLXD_SUT_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_TRANSCODER_FAKE=1 DVT_NETTHR1=on \
  DVT_SUT_CONFIG=rig/config/dv-router/netthr1-router.toml  scripts/run.sh -m rig -- cases/test_netthr1_rig.py -k "g1 or t1"
  # ... DVT_PIDS_SUT=252 DVT_SUT_CONFIG=rig/config/dv-router/netthr1-router.toml  ... -k "t3"
  # ... DVT_SUT_CONFIG=rig/config/dv-router/netthr1-hotspot.toml ... -k "g1 or t1"      then DVT_PIDS_SUT=96 ... -k "t3"

# 4. the DNS cancel, default template (its M17 edge), no web profile, about 3 minutes
XLXD_SUT_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_TRANSCODER_FAKE=1 DVT_NETTHR1=on DVT_NETTHR1_DNS=on \
  DVT_SUT_RESOLV=30 DVT_SUT_CONFIG=<template> scripts/run.sh -m rig -- cases/test_netthr1_rig.py -k d1

# 5. FLK11: the echo command of docs/echo-cases.md plus the switch; about 2 minutes for F11a / F11b, 10 to 12 minutes with E1 to E7
DVT_FLK11=on ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/echo.toml scripts/run.sh -m rig --profile interlink -- cases/test_echo.py -k "f11 or e7a"
```

What a run decides: a run of step 3 whose process set is not the documented one (the rig's argv names a transcoder address; if that adds a `dv-codec-ambed` child the set is no longer K6b and
the case says so and judges the sum of the per-process ceilings, 4 threads more) is still a valid ceiling check, but not the headline 251 / 95. T3 asks for the SUT at the
ceiling: if the container crash-loops at `ceiling + 1`, bisect upwards one at a time (252, 253, 254) and report where it stops restarting; that number is the answer to ask (a).

## Ask (b): the time of one argon2 login hash on a small board

Built (small, no install): H4 and the burst of H2 measure it as the rig sees it, with the SUT capped by `DVT_CPUS_SUT` (a CFS quota; `0.25` is one quarter of a core).
Command: step 2b above with `DVT_CPUS_SUT=0.25`; repeat with `1` and `0.5` for the slope; the answer is `hash_ms_estimate` in `out/<run>/web/netthr1-login-time.json`
(login median minus the median of a request that hashes nothing, through the same TLS proxy; one default-cost hash is 19 MiB, `argon2id` parameters of the stored hash).
Limits of the method, to put next to the number: a quota throttles in 100 ms periods (a 35 ms hash at 0.25 cpu runs 25 ms, waits, runs 10 ms: the wall time is about 110 ms),
it does not slow memory latency or IPC (a small ARM core is several times slower per clock; argon2 at 19 MiB is memory-bound), `DVT_MEM_SUT=512m` adds the memory cap of a 512 MiB board.
So the figure is a lower bound for the board; the board measurement (NODE1i) stays the acceptance.

Proposed, NOT built: (1) a cgroup-level read: a read-only control action `cgroup xlxd-sut` that prints `pids.current`, `pids.peak`, `pids.events`, `memory.current`, `memory.peak`, `memory.events`
from the host's cgroup of the container (the document asks for these every 25 ms); it would turn the T1 lower bound into the true peak and make T3 a plain `pids.peak <= ceiling` check
(about 25 lines in `scripts/run.sh` plus the allow-list in `dvtest/control.py`; it needs the cgroup path of the container on the host). (2) qemu: `qemu-user-static` and an arm64 image would give
instruction-level emulation of an ARM core; it needs an internet install and is NOT done (the owner's go is needed). (3) There is no argon2 command line in the scratch image and Python's stdlib has no argon2, so
a host-side `docker run --cpus 0.25` (`dvtest.limits.docker_run_args` with `DVT_CTR_CPUS=0.25`) of the image cannot time one hash by itself: a login through the service is the only
route without an install.

## GAPs

| # | GAP | Why | Next step |
|---|-----|-----|-----------|
| 1 | open sessions up to the cap, the NEXT ONE refused, `open == peak == cap` (DV-Router `sessions::session_cap_refuses_a_tool_and_reports_open_cap_peak`, `session_admission`) | a refused connection needs a held-open Zenoh client; the tester is UDP / HTTP only, the fabric of the SUT listens on its own loopback, no `dv-router` subcommand holds a session (`config`, `hub`, `ready` only). What IS covered: G1 to G4 (the gauge, the derived cap, the peak, a reload over the running cap refused, nothing spawned) and C3 (the validator refuses `cap < children + 1` before any session exists) | a rig service running the same image as `dv-router hub --max-sessions 4 --listen tcp/<NET>.<n>:7447 --allow-public-listen` plus four short-lived member services (`dv-svc-status` with `DV_FABRIC_CONNECT`), the fifth must exit with a connect error, `dv_fabric_sessions{open,cap,peak}` read from the hub's own `/metrics`; about a day, a new profile in `run.sh` |
| 2 | the link variant, ceiling 101 | `[[edges.link.uplink]]` needs a `secret_file` that exists (mode 0600) when the launcher starts; the rig has no fixture-secret mount for it (the AllStar edge has `asl-secrets`) | a `link-secrets` directory written by `run.sh` like `asl-secrets` and a `netthr1-hotspot-link.toml`; until then the two resolver workers are covered by M17 host-name rows (D1: 4 + 2 threads) |
| 3 | the true peak of a short-lived thread burst (simultaneous child exits, group TERM / INT, the Net blockers) | `proc-stat` is a docker run per process name: sweeps are seconds apart; Tokio blocking threads live 10 s so most are seen, a closing-session blocker of a few milliseconds is not. The tester cannot send TERM / INT to the launcher (only `hup`) | T3 (kernel-enforced limit) and the `cgroup` action above |
| 4 | TCP fallback of the DNS cancel | the unanswering server answers nothing, so no truncated reply moves the query to TCP | `dvtest/dnshole.py`: answer a name with TC=1 over UDP and accept the TCP connection without replying |
| 5 | admin large-body cap path | the static tree of the admin mount (`/` on the admin host serves `admin/`) is a design reading; H1 tries `/netthr1-big.bin`, `/admin/netthr1-big.bin`, `/shared/netthr1-big.bin` with a HEAD and SKIPs naming the paths when none serves the file | confirm the mount with the DV-Router team (question 4) |
| 6 | the K6b activity profile (60 s idle, a paced 10 s over, three large downloads per service, basemap refresh, link DNS, simultaneous child failures, group TERM / INT) | T2 covers logins and held streams only | extend T2 with a USRP or protocol over and `kill-proc` of several children at once |
| 7 | the lost-update ordering inside ONE poll of the echo loop (FLK11) | the publication instant is internal; see `docs/echo-cases.md` "FLK11" | none (the unit tests of the commit) |
| 8 | memory acceptance (`memory.peak`, OOM events, 400 MiB hotspot experiment) | the SUT cgroup files are not readable from the tester | the `cgroup` action above; `DVT_MEM_SUT=400m` is already a compose knob |

## Questions for the DV-Router team

1. Where exactly does the gauge appear in the admin status body and in the hub state (`summary.fabric.sessions`, `launcher.fabric.sessions`, `data.fabric.sessions`)? G1b searches for a `sessions {open, cap, peak}` object anywhere.
2. Which label tells `dv_web_capacity_refused_total` of dv-svc-web from dv-svc-admin on the hub's `/metrics` (`proc`?). H0 / H1 / H2 match `dv-svc-web` / `dv-svc-admin` in any label value and record a FINDING otherwise.
3. The rig launches `--config FILE XLX999 <own> <transcoder>`: does the transcoder word create a `dv-codec-ambed` child when the file has no `[[transcoder.worker]]` row, and is it counted in the derived cap? It decides whether the hotspot and router templates reproduce the 95 / 251 process sets.
4. Static mounts: is `<admin.static_dir>/admin/x` served at `/x` on the admin host, and `<web.static_dir>/shared/x` at `/shared/x` on the web host? Is a missing `static_dir` at start (the tester creates it later) read per request?
5. Does adding or removing a `[[edges.m17.peers]]` row restart `dv-edge-m17` (Restart(child)), as D1 assumes?
6. Is there, or could there be, a way to hold N fabric sessions open from outside the process set (a documented `dv-router` tool command or a test client binary in the image)? GAP 1 needs it.
7. A busy login refusal: is the admin password-only login of an enrolled account counted as an authentication failure toward the lockout? H2 (admin) sends 9 such logins.
