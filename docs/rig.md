<!-- SPDX-License-Identifier: MIT -->
# Docker rig

`rig/compose.yml`, compose project `dvt`, started and stopped by
`scripts/run.sh`. The rig network is `internal`: nothing in it can reach the
internet, and every address is fixed.

## Topology

```
                         rig network 172.30.0.0/24 (internal)

  +-------------------+  10100 + stream ports  +--------------------+
  | xlxd-ref    .10   |<---------------------->| transcoder-ref .20 |
  | upstream master   |                        | TRANSCODER_*_IMAGE |
  +-------------------+                        +--------------------+
        ^  30001 DExtra, 62030 DMR, 17000 M17
        |
  +-------------------+                        +--------------------+
  | tester      .30   |-- 10100 + streams ---->| transcoder-sut .21 |
  | pytest + dvtest   |   (wire cases)         |                    |
  +-------------------+                        +--------------------+
        |  30001 / 62030 / 17000                        ^
        v                                               | 10100 + streams
  +-------------------+---------------------------------+
  | xlxd-sut    .11   |
  | build under test  |  USRP C  .11:34003 <-> .30:32003 (tester)
  +-------------------+  USRP A  .11:34001 <-> .40:32001 (node 1901)
        ^                USRP B  .11:34002 <-> .40:32002 (node 1902)
        | USRP A/B
  +-------------------+
  | allstar     .40   |<-- IAX2 4569 -- tester (profile allstar)
  | ASL3 app_rpt      |
  +-------------------+
```

| Service | Address | Profile | Role |
|---------|---------|---------|------|
| xlxd-ref | 172.30.0.10 | core | reference xlxd (`XLXD_REF_*`), transcoder .20, no USRP |
| xlxd-sut | 172.30.0.11 | core | xlxd under test (`XLXD_SUT_*`), transcoder .21, USRP when `DVT_SUT_USRP=on` |
| transcoder-ref | 172.30.0.20 | core | `TRANSCODER_REF_IMAGE` or `TRANSCODER_IMAGE`, peers .10 and .30 |
| transcoder-sut | 172.30.0.21 | core | `TRANSCODER_SUT_IMAGE` or `TRANSCODER_IMAGE`, peers .11 and .30 |
| tester | 172.30.0.30 | core | runs pytest; repository mounted at `/dvt` |
| allstar | 172.30.0.40 | allstar | AllStarLink 3 node, chan_usrp to xlxd-sut A/B, IAX2 4569 |
| nxdn-ref / nxdn-sut | 172.30.0.50 / .51 | nxdn | NXDNReflector peered with xlxd-ref / xlxd-sut (module `DVT_NXDN_MODULE`, TG `DVT_NXDN_TG`) |
| p25-ref / p25-sut | 172.30.0.60 / .61 | p25 | P25Reflector peered with xlxd-ref / xlxd-sut (module `DVT_P25_MODULE`) |
| xlxd-peer-sut / xlxd-peer-ref | 172.30.0.12 / .13 | interlink | XLX998, interlinked with xlxd-sut / xlxd-ref on `DVT_INTERLINK_MODULES` |
| urfd | 172.30.0.70 | urfd | URFd (`URF999`, modules A-Z), a second reference reflector; see "URFd as the reference" |
| mrefd | 172.30.0.80 | mrefd | mrefd (`M17-REF`, modules `MNOP`), the M17 Project reflector, with interlink peers M17-DVT / M17-DVU at the tester; see "mrefd as an extra reference" |
| web-proxy | 172.30.0.90 (aliases `dvt-web`, `dvt-admin`) | web | TLS reverse proxy: HTTPS 443 to dv-svc-web of the SUT (`<net>.11:8081`) and HTTPS 8443 to dv-svc-admin (`<net>.11:8080`), self-signed CA in `out/<run>/web/ca.pem`; see "Web profile" |
| browser | 172.30.0.91 | browser | Playwright browsers (`playwright run-server`, chromium + firefox + WebKit) for the browser cases, used with `web`; see "Browser profile" |
| nat-router | 172.30.0.92 (alias .93; inside 172.31.0.2) | nat | NAT router in front of the browser, control server (JSON lines) on TCP 9100; see "NAT profile" |
| coturn | 172.30.0.94 | nat | TURN server (official image, pinned), per-run shared secret |
| nat-ref-a / nat-ref-b | 172.30.0.95 / .96 | nat | UDP reflectors of the NAT classification self-test |
| load nodes | 172.30.0.128/25 | load | `DVT_LOAD_MODULES` tester replicas running `DVT_LOAD_CMD` against xlxd-sut |
| capture-* | (shares the netns) | capture | dumpcap on xlxd-ref, xlxd-sut, transcoder-sut, allstar, nxdn-*, p25-*, urfd, mrefd |

| Port | Protocol | Where |
|------|----------|-------|
| UDP 10100 | AMBEd control | transcoders |
| UDP 10101.. | AMBEd stream data (reference ambed: 10100 + stream id) | transcoders and their clients |
| UDP 30001 | DExtra | xlxd |
| UDP 62030 | MMDVM/Homebrew DMR | xlxd |
| UDP 17000 | M17 | xlxd |
| UDP 34001-34003 | USRP, xlxd-sut side | xlxd-sut |
| UDP 32001-32002 / 32003 | USRP, peer side | allstar / tester |
| UDP 4569 | IAX2 | allstar |
| UDP 41400 | NXDNReflector network (xlxd connects out) | nxdn-ref, nxdn-sut |
| UDP 41000 | P25Reflector network (xlxd connects out) | p25-ref, p25-sut |
| UDP 10002 | XLX interlink | xlxd-*, xlxd-peer-* |
| TCP 443 | HTTPS (TLS proxy, `dvt-web`) | web-proxy (profile web) |
| TCP 8443 | HTTPS (TLS proxy, admin host `dvt-admin:8443`) | web-proxy (profile web) |
| TCP 3000 | Playwright websocket server (`DVT_BROWSER_PORT`) | browser (profile browser) |
| TCP 8081 | HTTP, dv-svc-web (plain; only the proxy is trusted) | xlxd-sut native with `web.toml` |
| TCP 8080 | HTTP, dv-svc-admin (plain; answers only for the host `dvt-admin:8443`) | xlxd-sut native with `admin.toml` |
| TCP 9100 | NAT router control (JSON lines, tester only) | nat-router (profile nat) |
| UDP 3478, 49160-49180 | TURN listener, relay ports | coturn (profile nat) |
| UDP 3479 / 3480 / 3481 | NAT reflector echo ports, poke source port | nat-ref-a, nat-ref-b |
| UDP 50000 | WebRTC media (one port, ICE-lite, `public_ips` = `<net>.11`) | xlxd-sut native with `web.toml` |

`DVT_SUBNET` (a /24) or `DVT_NET` (the prefix) moves the whole rig, and
`DVT_INSTANCE=<n>` moves it to `172.30.<n>.0/24`; the host offsets stay the
same, so captures of instance n show `172.30.<n>.x`.

## Settings

Load order, later wins (`scripts/lib/env.sh`, the same order is passed to
compose as `--env-file`):

1. `.env.example` (tracked): every pin - repositories, commits, base images,
   package versions, download hashes.
2. `.env` (gitignored): local overrides, e.g. `TRANSCODER_IMAGE`,
   `XLXD_SUT_REPO` / `XLXD_SUT_REF`.
3. `.local/rig.env` (gitignored, mode 600, from `scripts/gen-local.sh`):
   secrets only (the IAX2 secret). Never printed or logged.

A variable already set in the calling shell wins over all three files.

Transcoder interface: `rig/compose.yml` gives each transcoder
`DVT_TRANSCODER_BIND` (its own address) and `DVT_TRANSCODER_PEERS` (its
xlxd and the tester). An image that needs other variables or arguments gets
them from `.local/compose.override.yml` (gitignored; template
`rig/compose.override.example.yml`), which `run.sh` adds with `-f` when it
exists.

Fake transcoder: `DVT_TRANSCODER_FAKE=1 scripts/run.sh ...` makes `transcoder-sut`
the fake extended AMBEd (`dvtest/fake_ambed.py`, `docs/ambed-fake.md`). `run.sh`
builds `rig/Dockerfile.ambed-fake` through the compose service `ambed-fake-build`
(profile `ambed-fake-build`: built on request, never started, limits set,
`MAKE_JOBS` build arg) from `TESTER_BASE_IMAGE` plus the stdlib-only `dvtest`
package, and sets `TRANSCODER_SUT_IMAGE` to `dvt-ambed-fake:local`.
`transcoder-ref` stays `TRANSCODER_IMAGE` (xlxd-ref needs the real one), so the
two never share `<net>.21`. The container listens on `DVT_TRANSCODER_BIND:10100`,
the JSON control socket on `<net>.21:10190` (reachable from the tester) and
serves stream data on 10200-10299. The tester gets `DVT_TRANSCODER_FAKE=1`;
`cases/crossmode_fake.py` skips unless the fake answers `stats`. The `wire`
cases are for a real transcoder, not for the fake.

Run-time variables (not pins): `DVT_SUT_USRP`, `DVT_RUN_ID`,
`DVT_UID`/`DVT_GID`, `DVT_AMBED_ADDR`, `DVT_START_DELAY`; see the header of
`rig/compose.yml`. `dvtest/target.py` documents every `DVT_*` variable the
cases read.

## Running

The run is capped (scope, container limits, one rig at a time): README, "Resource limits".

```sh
scripts/run.sh -m rig                                  # core profile
scripts/run.sh -m "rig or allstar" --profile allstar   # + AllStar, USRP on
scripts/run.sh -m wire                                 # transcoder wire cases
scripts/run.sh --capture -m wire                       # + pcapng per service
```

One run at a time per instance: `run.sh` holds an flock on the instance's
lock for the whole up..down span and a second caller waits. If the project
already has containers (someone's `--keep` rig) it exits 3;
`scripts/run.sh --down` is the only cleanup.

Parallel rigs: `DVT_INSTANCE=<n>` (1..99) runs project `dvt<n>` on
`172.30.<n>.0/24` (same host offsets), lock `.cache/rig<n>.lock`, results
in `out/<utc>-i<n>/`. Instance 0 (the default) is project `dvt` on
`DVT_NET`. Images are shared; builds are serialized by `.cache/build.lock`. Results go to `out/<utc>/`:
`pytest.log`, `junit.xml`, `compose-ps.txt`, `compose-images.txt`,
`logs/<service>.log`, `run.txt` (markers, profile, xlxd commits), and with
`--capture` `capture/<service>.pcapng`.

## URFd as the reference

URFd (https://github.com/n7tae/urfd, GPL-3.0) is built from fetched source:
`scripts/pre-fetch-urfd.sh` puts the pinned `URFD_COMMIT` (bd0c114e, commit
verified) into `.cache/urfd`, and `rig/Dockerfile.urfd` builds it without
DHT as `dvt-urfd:local`. Service `urfd`, profile `urfd`, address `.70`.

Configuration (`rig/config/urfd/urfd.ini.in`, rendered by
`rig/urfd-entry.sh` and checked with URFd's own `inicheck` before start):

- callsign `URF999`, modules A-Z; no transcoder (same-codec passthrough
  only, no cross-mode audio); BrandMeister off; USRP off (URFd needs a
  transcoded module for it); no XLX interlink (URFd cannot interlink with
  xlxd); G3 on;
- YSF: AutoLinkModule B (xlxd's default), DG-ID selection off;
- NXDN 41400 and P25 41000 open natively (reflector id = `DVT_NXDN_TG` /
  `DVT_P25_TG`); no case targets them yet;
- ID databases in file mode from the rig's `dmrid.dat` / `nxdn.csv`;
  blacklist from `DVT_XLXD_BLACKLIST`; `IPv4External` set to the rig
  address (URFd will not start if it cannot look its address up, and the
  rig has no internet). Log: `out/<run>/logs/urfd.log`.

`DVT_REF_KIND=xlxd|urfd` (default `xlxd`, in `.env.example`) picks the
reference. With `urfd`, `run.sh` starts the `urfd` profile and points the
tester's `DVT_XLXD_REF` at `.70`, so every sut == ref case compares the
system under test with URFd. Such runs are compatibility data, not gates:
`run.sh` accepts them only with `DVT_SUT_PROFILE=xlxd` (rc 2 otherwise).
`run.txt` records `ref_kind=`; the tester also gets `DVT_REF_KIND` and
`DVT_URFD`. `--capture` adds `capture/urfd.pcapng`.

`scripts/compat-table.py out/<run, ref xlxd> out/<run, ref urfd> ...` prints
one row per case with its result per reference kind: same, differs, skip,
error, or n/a for suites that need xlxd itself as the reference
(interlink, USRP, netns, AMBEd wire, matrix, NXDN/P25 peer, backend loss,
transcoder lifecycle, AllStar).

## mrefd as an extra reference

mrefd (https://github.com/n7tae/mrefd, GPL-3.0) is the M17 Project reflector.
It is the real implementation the M17 cases (stage 2: configured reflector
peers, one-hop module trailer, M17P) can be judged against, since xlxd has no
M17 peering and drops M17P. `scripts/pre-fetch-mrefd.sh` puts the pinned
`MREFD_COMMIT` into `.cache/mrefd`, and `rig/Dockerfile.mrefd` builds it
without DHT (no OpenDHT, no libcurl, no network lookup) as `dvt-mrefd:local`.
Service `mrefd`, profile `mrefd`, address `.80`, UDP 17000; the container has
the medium resource limits and no restart. Log: `out/<run>/logs/mrefd.log`.

Configuration (rendered by `rig/mrefd-entry.sh` into `/tmp/mrefd/` at start;
unit-tested in `cases/unit/test_mrefd_entry.py`):

- designation `M17-REF`, modules `DVT_M17_MODULES` (default `MNOP`: the cases'
  `MOD` and `OTHER` plus two more), bound to the rig address; no IPv6, no
  dashboard, no ID database, no MQTT; blacklist from `DVT_XLXD_BLACKLIST`;
- two configured interlink peers at the tester, which plays them: `M17-DVT`
  at `<net>.30` on UDP `DVT_M17_PEER_DVT_PORT` (17011) sharing
  `DVT_M17_PEER_DVT_MODULES` (`M`), and `M17-DVU` at `DVT_M17_PEER_DVU_HOST`
  (default `<net>.32`) on `DVT_M17_PEER_DVU_PORT` (17012) sharing
  `DVT_M17_PEER_DVU_MODULES` (`MN`). Why a second address: mrefd identifies a
  peer by IP only (its address compare ignores the port), so two peers on one
  IP merge and a DISC or PING of one removes or refreshes the other (DVU was
  dropped every ~35-40 s). The sut is not affected (it keys peers by ip:port)
  and keeps `<net>.30`:17011 / :17012. The sidecar `tester-addr` (profile
  `mrefd`, netem image, `NET_ADMIN` only, shares the tester namespace) adds
  the address with `ip addr add` and sleeps; the tester has no `NET_ADMIN`
  itself. The `m17_peers` fixture binds the "ref" hubs accordingly
  (`dvtest.m17_peer.peer_binds`; "sut" hubs stay on 0.0.0.0, or on `<net>.30`
  when both sides are judged). A bind before the address exists is retried
  for 15 s. mrefd connects out: it
  sends a 37-byte reflector `CONN` (designation, module list) to each port,
  so a case that plays a peer listens on that port. The tester container gets
  the same `DVT_M17_*` values.

Start it with `scripts/run.sh --profile mrefd -m rig` (tester reaches it as
`DVT_MREFD`), or make it the reference with `DVT_REF_KIND=mrefd`: `run.sh`
starts the profile and points `DVT_XLXD_REF` at `.80`, as for URFd (compat
data, `DVT_SUT_PROFILE=xlxd` only; `run.txt` records `ref_kind=mrefd`).
`--capture` adds `capture/mrefd.pcapng`. Only the M17 cases mean anything
against it (no other protocol is served), see `docs/compat-mrefd.md`.

## Prebuilt reflector image

`XLXD_SUT_IMAGE` / `XLXD_REF_IMAGE` (empty by default) run a reflector
image as is instead of building that service from `.cache/xlxd/<dir>`; the
`.cache` tree is then not needed for it, and `run.txt` records the image
(`xlxd_sut=<image>`). `run.sh` writes `out/<run>/xlxd-image.override.yml`
(added with `-f`) and renders the configuration on the host into
`out/<run>/xlxd-config/<service>/` with `rig/xlxd-entry.sh` in render-only
mode.

Contract for such an image:

1. Arguments `CALLSIGN OWN_IP TRANSCODER_IP` (e.g. `XLX999 <net>.11
   <net>.21`). The rig sets no entrypoint, so the image's own entrypoint
   takes these three words.
2. Configuration mounted read-only in `/xlxd/` (the image must not write
   there): `xlxd.usrp` (`module bind_ip bind_port peer_ip peer_port
   callsign`, empty unless `DVT_SUT_USRP=on`), `xlxd.interlink` (XLX rows
   `<callsign> <ip> <modules>`, `NX<tg> <ip>:41400 <module>`,
   `P25<id> <ip>:41000 <module>`, each only with its profile on),
   `xlxd.whitelist`, `xlxd.blacklist`, `xlxd.terminal` (empty = everyone),
   `dmrid.dat` (`id;callsign;x`), `nxdn.csv` (`id,callsign,x`), `ysfnode.dat`
   (empty). Env `DVT_CONFIG_MOUNTED=1` is set and may be ignored.
3. Listens on OWN_IP: DExtra 30001, MMDVM 62030, M17 17000, DMR+ 8880, XLX
   interlink 10002, the USRP bind ports of `xlxd.usrp`; talks AMBEd to
   TRANSCODER_IP:10100 and the stream ports. NXDN and P25 peers are reached
   from an ephemeral port. DPlus 20001, DCS 30051 and YSF 42000 are not used
   by the cases yet.
4. Logs on stdout/stderr (`out/<run>/logs/<service>.log`).
5. No internet: the rig network is internal, so ID databases come from the
   mounted files.

### Launch modes of the system under test (`DVT_SUT_LAUNCH`)

| Value | xlxd-sut starts as |
|-------|--------------------|
| `direct` (default) | the image's own entrypoint with `XLX999 <net>.11 <net>.21` |
| `import` | `dv-xlxd-import --exec --dir /xlxd ...` (legacy converter of DV-Router), same three words |
| `native` | the image's own entrypoint with `--config /etc/dv-router/dv-router.toml XLX999 <net>.11 <net>.21` |

`native` needs `XLXD_SUT_IMAGE` and `DVT_SUT_CONFIG=<path to a dv-router.toml
TEMPLATE>` (exit 2 when either is missing or the file is not readable).
`run.sh` renders the template on the host with `python3 -m dvtest.sut_config`
into `out/<run>/xlxd-config/xlxd-sut/dv-router.toml` and mounts it read-only at
`/etc/dv-router/dv-router.toml`. Plain text tokens:

| Token | Becomes |
|-------|---------|
| `<OWN_IP>` | `<net>.11`, the SUT |
| `<TRANSCODER_IP>` | `<net>.21`, the SUT's transcoder |
| `<TESTER_IP>` | `<net>.30`, the tester (plays the peers) |
| `<NET>` | the /24 prefix, e.g. `172.30.0` (rig peers are `<NET>.12` XLX, `<NET>.51` NXDN, `<NET>.61` P25) |
| `<REQUIRE_IDENT>` | `true` or `false`: `DVT_REQUIRE_IDENT`, default `true` (any other value is exit 2) |

Any other `<UPPER_CASE>` token left over is an error (exit 2, before anything
starts). Everything else stays as in the other modes: the `/xlxd` file mounts
(the template's `[ids]` reads `/xlxd/dmrid.dat` and `nxdn.csv`; the other files
are unused with a config file but harmless), `DVT_CONFIG_MOUNTED=1` and the
`DV_*` pins, which win over the file. With a file there is no importer, so
the `xlxd.interlink` / `xlxd.usrp` / `xlxd.whitelist` / `xlxd.blacklist` /
`xlxd.terminal` files are not read: the template itself must name the interlink,
NXDN and P25 peers. `run.sh` warns when `DVT_NXDN`, `DVT_P25`, `DVT_INTERLINK`,
`DVT_SUT_USRP` or `DVT_XLXD_BLACKLIST` is set in this mode and refuses
`DVT_UNSERVED_MODULE=M|N` (exit 2). The tester sees the mode in
`DVT_SUT_LAUNCH` (the M17 peer cases skip with `import` only).

### Extending the template (`DVT_SUT_CONFIG_EXTRA`)

The DV-Router template lives outside this repository and is never copied in.
`DVT_SUT_CONFIG_EXTRA=<file>[:<file>...]` (paths relative to the repository root
or absolute) appends repo-owned snippets after the rendered template, in the
order given, with the same token substitution (`dvtest/sut_config.py`, option
`--extra`; `run.sh` checks them before anything starts: a missing file or an
unknown token is exit 2). A plain table `[name]` of a snippet that the template
already defines is merged into it key by key (the snippet's keys replace the same
keys, everything else of the template stays; a multi-line value that would have to
be replaced is exit 2); `[[arrays]]` and other tables are appended. This is how
`web.toml` switches `[web]` on although the template has `enable = false`. The
rendered file must parse as TOML (checked before anything starts). Extra token:
`<WEB_PROXY_IP>` = `<net>.90`. Only `native` reads a config file; with
another launch mode the variable draws a warning. Snippets in
`rig/config/dv-router/`:

| File | Adds |
|------|------|
| `data-ident.toml` | `[data] enable = true`, `require_ident = <REQUIRE_IDENT>`, `xlx_text_overs = true`: the data worker on, with the identity policy of the run |
| `xlx-peer.toml` | `[[edges.xlx.peer]]` XLX998 at `<NET>.12` on modules `DE` |
| `web.toml` | the web service, a persistent state dir and the WebRTC edge; added by `--profile web` itself (next section) |
| `recorder.toml` | the recorder (K14b): `[recorder] dir = "/var/lib/dv-recorder"`, `keep_days = 7`, `max_gb = 1`; naming it in `DVT_SUT_CONFIG_EXTRA` is the switch (see "Recorder" in the web profile section) |

The XLX peer row names `xlxd-peer-sut` (`<NET>.12`, XLX998), not `xlxd-peer-ref`
(`.13`): `xlxd-peer-sut` is configured to link with `xlxd-sut` (`.11`, XLX999),
`xlxd-peer-ref` only with `xlxd-ref` (`.10`), so only `.12` can answer the SUT.
It needs `--profile interlink` (native launch ignores `DVT_INTERLINK` for the
SUT, but the tester still gets it from the profile) and its modules must be
served by `[node] rooms`. The remote port is `edges.xlx.port` (10002); there is
no per-peer port key. Run with and without the identity requirement:

```sh
XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml:rig/config/dv-router/xlx-peer.toml \
  DVT_REQUIRE_IDENT=true scripts/run.sh -m rig --capture --profile interlink -- cases/test_data_ident.py
# again with DVT_REQUIRE_IDENT=false
```

`cases/test_data_ident.py` (DV-Router MS3: the identity of data items) skips
unless the profile is `dvrouter`, the launch `native` and the hub metrics have
a `dv_room_data_total` or `dv_pkt_items_total` family (the `[data]` table is on).
It reads the setting from `DVT_REQUIRE_IDENT` in the tester environment
(`rig/compose.yml`, default `true`) and writes what it observed to
`out/<run>/data-ident.txt`.

## Web profile

`scripts/run.sh --profile web` makes DV-Router's web service (`dv-svc-web`) usable
from the rig: HTTPS in front of it, accounts that work (a persistent state dir
and a first operator), and the WebRTC edge up. It is the base of the web unit
(smoke cases now; API probes, browsers, NAT and load build on it). It needs
`DVT_SUT_LAUNCH=native` (the web service is DV-Router's; exit 2 otherwise).

```sh
XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router \
  DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> \
  scripts/run.sh -m rig --capture --profile web -- cases/test_web_smoke.py
```

`--profile web` adds `rig/config/dv-router/web.toml` and `admin.toml` to `DVT_SUT_CONFIG_EXTRA`
(naming them yourself is harmless; the admin service is always on in this profile) and does six things:

| What | Detail |
|------|--------|
| config | `web.toml` merges into the template: `[node] state_dir = "/var/lib/dv"`; `[web] enable = true`, `bind = "0.0.0.0:8081"`, `trusted_proxies = ["<WEB_PROXY_IP>"]`, `operator_totp = true` (false would make dv-svc-web exit 2 on a non-loopback bind); `[edges.webrtc] enable = true`, `public_ips = ["<OWN_IP>"]` (the edge is ICE-lite and advertises exactly that; media is the one UDP port 50000, no range, no remap) |
| state dir | `out/<utc>/sut-state/` (mode 0700, created by `run.sh`) mounted read-write at `/var/lib/dv`; the SUT container runs as the host uid/gid (`user:` in the generated override) so the files stay removable. Without a persistent state dir that is not the run dir, accounts are off and RTC offers answer 503. It holds `accounts.jsonl` (password hashes, TOTP secrets), `audit.jsonl` and `web/` circuits: local to `out/`, never publish it |
| first operator | `sut-state/secrets/web-bootstrap` (`N0CALL:<password>`, mode 0600; dv-svc-web consumes and removes it at its first account) and the same login for the tester in `out/<utc>/web/operator.env` (`DVT_WEB_OPERATOR`, `DVT_WEB_OPERATOR_PASSWORD`, mode 0600). The password is `secrets.token_urlsafe(24)` per run and appears in no log, argument or compose file |
| admin service | `admin.toml` merges `[admin] enable = true`, `bind = "0.0.0.0:8080"`, `trusted_proxies = [<WEB_PROXY_IP>]`, `host = "dvt-admin:8443"`, `allow_from = [<TESTER_IP>, <BROWSER_IP>]` (the `/admin/` prefix mount is served only to listed addresses), `totp = true`. Users: `run.sh` writes `sut-state/admin/users` (mode 0600; `admin`, `operator`, `viewer`, random passwords, pure-Python Argon2id hashes from `dvtest/argon2_pure.py`, no `:totp=` field so each enrols at its first login) and the same logins for the tester in `out/<utc>/web/admin.env` (mode 0600). The bootstrap file `secrets/admin-bootstrap` is not used: it creates only one admin, and the role matrix needs a viewer and an operator too |
| `web-proxy` | service at `<net>.90`, network aliases `dvt-web` (`DVT_WEB_HOST`) and `dvt-admin` (`DVT_ADMIN_HOST`), HTTPS on 443 (web) and `DVT_ADMIN_PORT` 8443 (admin, `--route`, same certificate and CA); limits set `medium` (`init: true`, read-only root, all capabilities dropped, runs as the host uid). Image `dvt-web-proxy:local` from `rig/Dockerfile.web-proxy`: `TESTER_BASE_IMAGE` (python and the openssl CLI are in it) plus the stdlib `dvtest` package; no new pin, no new image pulled |
| tester | `DVT_WEB=on`, `DVT_WEB_URL=https://dvt-web`, `DVT_ADMIN_URL=https://dvt-admin:8443`, `DVT_WEB_PROXY=<net>.90` |

**The proxy** (`dvtest/tools/tls_proxy.py`, unit tests `cases/unit/test_tls_proxy.py`
against a fake backend) is repo-owned stdlib Python rather than nginx or caddy: it
adds no image and no pin, and what the cases need is small and exactly testable:
it streams both ways without buffering (SSE frames reach the client as they
leave the SUT; request bodies of any size go through with their length or
chunked), keeps method, path, query, headers and `Host` (the service compares
`Origin` with the effective host), drops hop-by-hop headers, sets
`X-Forwarded-Proto: https`, `X-Forwarded-Host` and `X-Forwarded-For` (the client
address; a client-sent value is discarded, the proxy is the edge), passes any
status (304, 204, 404, several `Set-Cookie`), answers 502 for an unreachable
SUT and 501 for `Upgrade` (the web service has no WebSocket), and caps open
connections (`DVT_WEB_PROXY_CONNS`, 256, then 503; one thread each). If a later
stage needs HTTP/2, WebSocket or very many connections, put a pinned nginx or
caddy image in its place; the address, alias, CA path and headers are the
contract. At start it makes a throwaway CA and a server certificate (SAN:
`dvt-web`, `localhost`, `<net>.90`, `127.0.0.1`; RSA 2048, 2 days) with the openssl CLI,
deletes the CA key at once and writes the CA certificate to
`out/<utc>/web/ca.pem` (mode 0644): trust that file (Python
`ssl.create_default_context(cafile=...)`; a later Playwright container mounts
`out/<utc>/web` and sets the CA or `ignoreHTTPSErrors`). The server key stays in
the container's tmpfs. The log (`logs/web-proxy.log`) has one line per request:
method, path without the query, status, bytes, ms.

**Recorder (stage 3b).** `DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/recorder.toml` switches the
recorder of DV-Router on (it is not part of the web profile by default: stage 2 expects 503 from the recordings routes). `run.sh`
then makes the host directory `out/<utc>/recorder` (mode 0700), mounts it read-write at `/var/lib/dv-recorder` (the SUT runs as
the host uid, like the state dir) and the tester gets `DVT_RECORDER=on`. The directory is a sibling of the state dir
`/var/lib/dv`, never inside it (DV-Router refuses a recorder dir that is inside or a parent of the state or run dir).
`[recorder] dir` is the only key that matters for the switch; `keep_days = 7` is what the dashboard notice says ("Recordings are
kept for 7 days."). It needs the fabric ACL on (the template's default). Archives hold the source profile of an over
(`pcm-s16le-8k` for USRP and WebRTC overs, served as WAV with no codec worker); overs of the vocoder profiles need the
decoder worker (K14c, the fake AMBEd, later).

**Tester side**
- `dvtest/web.py` (stdlib): `WebClient` (HTTPS with the run's CA, cookie jar with
  the cookie attributes, CSRF token, Origin, JSON, SSE reader, `WebError` with
  status and a body scrubbed of every secret the client was told about),
  `Authenticator`/`totp` (RFC 6238, unit-tested with the RFC vectors; each counter
  used once because the server accepts a code once), `Pacer` (the server's budgets
  as token buckets: login 1/s burst 10 per client address, register 0.2/s burst 2
  with at least 2 s between registrations, public reads 5/s burst 20,
  authenticated reads 20/s burst 20, unsafe requests 2/s burst 10; one instance for
  the process because the server counts per address), the account flow
  (`login`, `enrol_totp`, `step_up_headers`, `create_invite`, `register_user`,
  `user_session`) and `finding()`.
- `cases/web_fixtures.py` (loaded by `conftest.py`): `web_base_url`
  (`https://dvt-web`, waits up to 90 s for `/api/v1/public/state` = 200), `web_ca`
  (`out/<utc>/web/ca.pem` as the tester sees it, `DVT_WEB_CA`), `web_client()` (an
  anonymous client), `web_operator` (logged in as `N0CALL`, TOTP enrolled,
  step-up window open) and `web_user(callsign=None, may_tx=False)` (registers and
  logs in `N0CALL1`, `N0CALL2`, ...). All skip with the reason unless
  `DVT_WEB=on` and the service answers.
- `dvtest/admin.py` (`AdminClient`: host mount `/api/v1/admin/...` with `__Host-dv_admin`, prefix mount `/admin/api/v1/admin/...` with `__Secure-dv_admin` by presenting another host name, login with TOTP enrolment), fixtures `web_url_admin`, `admin_session(role, mount)` and `web_operator2` (a second operator, `N0OP`), and the API cases `cases/test_web_api.py` (stage 2; route tables `dvtest/api_web.py` and `dvtest/api_admin.py`, verdicts `dvtest/api_probe.py`; results in `docs/web-api-cases.md`).
- `cases/test_web_smoke.py` (markers `rig`, `web`; about 20 s of cases): what it checks
  and what it only records is in its header. Differences from the documents are
  printed as `FINDING:` warnings and appended to `out/<utc>/web-findings.txt`;
  they are never encoded as assertions.

**Limits.** The TLS proxy and lockouts: login/register limits and lockouts live
in the SUT's memory, so a case that needs a clean budget restarts the web service
(control channel `kill-proc xlxd-sut dv-svc-web KILL`; the launcher respawns it
within a second, sessions are gone, RTC answers 503 for about 36 s). The WebRTC
media path (UDP 50000 on `<net>.11`) is exercised by the browser cases of stage 3b (`cases/test_web_webrtc.py`). The
legacy direct admin mount (no `[admin] host`, loopback only, cookie `dv_admin`)
cannot be reached from the tester and is not covered. State is per run: a new
`out/<utc>/sut-state` every time, so the first-operator flow always starts from a
bootstrap file.

## Browser profile

`scripts/run.sh --profile web,browser` adds the service `browser` (`172.30.0.91`) to the web profile, for
the browser cases of the web unit (`cases/test_web_browser.py`, `docs/web-browser-cases.md`). `browser`
alone is exit 2: the browsers load the pages of the web service.

```sh
XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router \
  DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_browser.py
```

| What | Detail |
|------|--------|
| why a service | The tester container has no docker socket and no browsers, and browsers do not belong in the tester image (size, memory cap). The browsers run in their own container, the tester drives them over a websocket with the Playwright Python client, and the browsers load `https://dvt-web` from `<net>.91`, so the web service sees a client address that is neither the tester's nor the proxy's (the NAT stage needs that) |
| image | `rig/Dockerfile.browser` on `BROWSER_BASE_IMAGE` = the official `mcr.microsoft.com/playwright/python:v1.64.0-noble` pinned by tag AND digest in `.env.example` (about 1.0 GB; chromium 1248, firefox 1555, webkit 2370 and every system library). That image has no Playwright package, so the build installs `playwright==PLAYWRIGHT_VERSION` (build time only; the rig network has no internet) and fails when the browsers of the image are not the ones that version expects. `docker pull` of the base image is the one internet use; pull it once before the first run |
| server | `playwright run-server --host 0.0.0.0 --port 3000` (ws://`<net>.91`:3000/). One connection per engine and run (`browser_type.connect`), the server starts that browser for the connection and stops it at disconnect; a new context per case separates the cases |
| client | the tester image has `playwright==PLAYWRIGHT_VERSION` (client only, no browsers downloaded; `rig/Dockerfile.tester`). The client and the server must be the same version: one pin feeds both images. `DVT_BROWSER=on` and `DVT_BROWSER_WS` reach the tester; the cases skip with the reason otherwise |
| caps | own set `x-limits-browser`: memory 3g = swap 3g (`DVT_MEM_BROWSER`; three browsers at once, each about 0.5-1 GiB with a page open), cpus 2, pids 1024 (browsers are multi-process; chromium, firefox and WebKit together stayed below that), `shm_size` 1g (`DVT_SHM_BROWSER`, chromium needs a large /dev/shm; it is memory and counted in the 3g; `dvtest/limits.py` refuses a `shm_size` larger than `mem_limit`), tmpfs `/tmp` 512m. Sizes are settings in `.env.example`; `run.sh` refuses an uncapped service as for every other one |
| hardening | non-root `pwuser` of the image, `init: true`, `cap_drop: ALL`, `no-new-privileges`, read-only root filesystem. Chromium runs without its own sandbox (Playwright's default `chromiumSandbox=false`; the container is the boundary) so no capability or seccomp profile is needed |
| trust | the proxy certificate is self-signed and the browsers are not given the run CA: the cases create every context with `ignore_https_errors=True`, and that is the only TLS relaxation (the Python test clients keep verifying against `out/<run>/web/ca.pem`) |
| launch options | the cases connect with `browser_type.connect(ws://<net>.91:3000/?launch-options=<json>)`; `playwright run-server` takes `args` and `firefoxUserPrefs` from a client only when started with `--unsafe` (the `browser` service is: its network is internal and the container is the boundary). Used for the fake microphone of the talk cases: Chromium `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream --use-file-for-fake-audio-capture=/media/fake-mic.wav --autoplay-policy=no-user-gesture-required`; Firefox prefs `media.navigator.streams.fake`, `media.navigator.permission.disabled`, `media.autoplay.default=0`; Playwright WebKit has no such option (microphone permission is granted per context; whether a fake device exists is recorded by the cases). `dvtest/browser.py` `launch_options()` |
| media dir | `out/<utc>/media/` (made by `run.sh`) is mounted read-only at `/media` of the browser service. The tester writes the fake microphone file `fake-mic.wav` there at run time (a 700 Hz sine, 48 kHz mono 16 bit, 30 s, looped by Chromium); it is generated, never committed (`*.wav` is ignored) |
| artifacts | on a failed case: `out/<run>/web/browser/<engine>-<case>/` with `page.png`, `trace.zip` (Playwright trace viewer), `page.har.zip` (HAR without bodies) and `log.json` (console, page errors, request list); `engines.txt` lists the browser builds that answered. Local to `out/`, never committed; a trace holds the values the case typed (throwaway run passwords) |
| engine names | the WebKit of Playwright is the engine of Safari but not Safari (a Linux build without Safari's behaviour and without iOS): results are labelled "Playwright WebKit" everywhere |

The fixtures (`cases/browser_fixtures.py`): `engine` (chromium, firefox, webkit), `browser_page`
(a new context per case, anonymous), `user_page` and `operator_page` (signed in), each with the page
log (console, page errors, every request with its status, dialogs, CSP reports) and `problems()`.
All selectors are in `cases/web_pages.py` (role, text, aria-label, ids; DV-Router's `data-testid`
hooks, WEB2, are not on `wip-f3adb62` yet: a later switch is a change of that one module).

## NAT profile

`scripts/run.sh --profile web,browser,nat` puts the browser behind a Linux NAT router and adds a TURN server, for the
NAT cases of the web unit (`cases/test_web_nat.py`, results and findings in `docs/web-nat-cases.md`). `nat` alone is
exit 2: it needs `web` and `browser`. With `rig/config/dv-router/turn.toml` in `DVT_SUT_CONFIG_EXTRA` the SUT also
hands out TURN credentials (`GET /api/v1/rtc/ice`); without it that route answers 404 (the "no TURN" run).

```sh
XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path> \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml \
  scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py
```

```
   rig network 172.30.0.0/24 (internal)                                nat-inside 172.31.0.0/24 (internal)
   ------------------------------------------------------------        ---------------------------------
   xlxd-sut .11  UDP 50000 (ICE-lite edge)
   web-proxy .90 TCP 443   (https://dvt-web)
   coturn    .94 UDP 3478, relay 49160-49180
   nat-ref-a .95 / nat-ref-b .96  UDP 3479 3480 (3481 poke)
   tester    .30 --- ws :3000 and probe :3001 (DNAT) ---+
                 --- control :9100 ------------------+   |
                                                      v   v
                                           +-----------------------+            +--------------------+
                                           | nat-router            |   inside   | browser  .91       |
                                           |  outside .92 (+ .93)  |<---------->|  chromium, firefox,|
                                           |  inside  .2           |   .2       |  WebKit            |
                                           |  iptables NAT         |            | + nat-edge (route  |
                                           +-----------------------+            |   via .2, probe)   |
                                                                                +--------------------+
   media, direct:  browser -> router (NAT) -> xlxd-sut:50000
   media, TURN:    browser -> router (NAT) -> coturn:3478 ... coturn relay -> xlxd-sut:50000
```

| Service | Address | Role |
|---------|---------|------|
| `nat-router` | outside `<net>.92` (alias `<net>.93`), inside `<nat-net>.2` | iptables NAT between the two networks, forwarding on (`net.ipv4.ip_forward=1` is a sysctl of the service only), JSON-line control server on `<net>.92:9100` (tester only), TCP forwards `<net>.92:3000` and `:3001` to the browser. Only capability: `NET_ADMIN` |
| `browser` | `<nat-net>.91` (`DVT_NAT_NET`, default `172.31.0`) | the stage 3a browser service moved to the internal network `nat-inside` by `rig/compose.nat.yml` (`networks: !override`, `extra_hosts` for `dvt-web` / `dvt-admin` since compose DNS does not cross networks). It cannot reach anything except through the router |
| `nat-edge` | shares the namespace of `browser` | sets the browser's default route via `<nat-net>.2` (Docker gives an internal network none), then runs the probe server `:3001` as nobody; caps `NET_ADMIN SETUID SETGID` (the last two only to drop root) |
| `nat-ref-a`, `nat-ref-b` | `<net>.95`, `<net>.96` | UDP reflectors for the classification self-test: they answer with the source address they saw, and send a datagram from a second port on request |
| `coturn` | `<net>.94` | the official coturn image, UDP 3478, relay ports 49160-49180, `use-auth-secret`, no TLS (the rig is offline), user quota 4 / total 20, RFC 1918 ranges denied and `allowed-peer-ip` = the SUT address only (it is in 172.16.0.0/12; coturn lets an explicit allow entry override a denied range). Capability `NET_BIND_SERVICE` only: `/usr/bin/turnserver` carries it as a file capability and cannot be executed without it |

Images: `dvt-nat:local` from `rig/Dockerfile.nat` (`TESTER_BASE_IMAGE` + iptables, conntrack, iproute2 at the exact versions
`NAT_*_VERSION` of `.env.example` + the stdlib `dvtest` package); coturn is `COTURN_IMAGE`, tag AND digest, pulled once
by hand (`docker pull coturn/coturn:4.18.0`), never by `run.sh`. All five services have limit sets (`x-limits-small`,
coturn `x-limits-medium`).

**NAT types** (`dvtest/nat.py`, rules in `mode_commands`; switched at run time with the control op `nat_mode`):

| Type | iptables | Mapping / filtering (RFC 4787) |
|------|----------|--------------------------------|
| `full_cone` | `SNAT -s <browser> --to-source <net>.93` + `DNAT -d <net>.93 --to-destination <browser>` (static 1:1) | endpoint-independent / endpoint-independent |
| `port_restricted` (default) | `MASQUERADE` | endpoint-independent (source port kept) / address-and-port-dependent |
| `symmetric` | `MASQUERADE --random` for UDP, plain for TCP | address-and-port-dependent / address-and-port-dependent |
| `none` | no rules | nothing is routable back: the browser reaches nothing |

Control ops (one JSON line per TCP connection, `dvtest/nat.py` docstring): `nat_mode`, `flush` (deletes the UDP conntrack entries
only: `conntrack -F` would also kill the Playwright websocket and the page's HTTPS flows), `blackhole on|off|for N` (drops all UDP
between inside and outside, both directions, TCP untouched; `for` switches itself off and records the dropped datagrams),
`drop_direct on|off` (drops UDP from inside to the SUT's media address:port, so only the TURN path can work), `status`, `reset`.
The control server listens on the outside address only and answers the tester's address only; the browser cannot reconfigure it.

**TURN secret.** `run.sh` makes `out/<utc>/web/turn.secret` (random, mode 0600, no trailing newline, never printed) and coturn's
private `out/<utc>/coturn/turnserver.conf` (mode 0600, holds the same secret as `static-auth-secret`) with `python3 -m dvtest.turn make`.
With `turn.toml` the secret is mounted read-only into the SUT at `/run/secrets/turn-secret` (the SUT runs as the host uid, the file is
owned by it with mode 0600) and `[web] turn_urls = ["turn:<net>.94:3478?transport=udp"]`, `turn_secret_file`, `turn_ttl = 120`
come from the snippet. After the run `dvtest.turn scan` looks for the secret in every file under `out/<utc>/` except those two
(captures included) and fails the run (exit 5) if it finds it.

**Tester side.** `DVT_NAT=on`, `DVT_NAT_CTL`, `DVT_NAT_PROBE`, `DVT_NAT_REFLECTORS`, `DVT_NAT_ALIAS`, `DVT_NAT_INSIDE`, `DVT_TURN`,
`DVT_TURN_SECRET_FILE`, `DVT_TURN_TTL`, `DVT_NAT_EXPIRY_S`; `DVT_BROWSER_WS` points at `ws://<net>.92:3000/` (the router's forward).
`cases/nat_support.py` has the client and the page helpers. Unit tests: `cases/unit/test_nat.py`, `cases/unit/test_turn.py`.

## Control channel

The tester has no docker socket. Cases that stop a backend write a request
file and `run.sh` executes it on the host:

```
tester (DVT_RIG_CONTROL=1)                 run.sh watcher (host)
 |-- $DVT_OUT/control/<id>.req.tmp           |
 |-- rename to <id>.req  "stop transcoder-sut" ->|  polls every 0.2 s
 |                                           |-- docker compose stop transcoder-sut
 |<-- <id>.ack "ok" | "error <rc>" | "error refused"
 |                                           |-- <id>.req renamed to <id>.done
```

Actions `stop`, `start`, `kill` (SIGKILL); services `transcoder-ref`,
`transcoder-sut`, `allstar`. Anything else is refused and nothing runs.
`control/control.log` gets `<utc> request <line>` before and
`<utc> <line> -> <rc>` after each action.

`kill-proc xlxd-sut <process> <signal>` signals one process inside the
system-under-test container (the DV-Router image is `FROM scratch`: no shell,
`ps` or `pkill`, and the host user may not signal root's processes). The host
starts a throwaway container of the tester image that joins the target's pid
namespace (`docker run --pid container:<id>`, `dvtest.limits.docker_run_args()`
caps, no network, only CAP_KILL) and walks `/proc/<pid>/comm`: a process is
signalled only when its comm equals `<process>` exactly and it is not a zombie.
Allow-list: `dv-edge-xlx`, `dv-edge-nxdn`, `dv-edge-p25` with `KILL` or `USR1` (quiet restart: the edge saves its sessions, exits and the
launcher respawns it) the codec worker `dv-codec-ambed` and the web service `dv-svc-web` (its in-memory login/register limits and lockouts) with `KILL` only; the launcher `dv-router` is never signalled (no handler, a
stray signal kills the node); anything else
is `error refused`. `<id>.out` has `pid=<n>` per process and `t_kill=<epoch s>`
(taken in the container right after the signals); `error 3` = no such process.
Tester side: `rig_control.kill_proc(service, process, signal)` returns the pids
and `t_kill` (`dvtest/control.py`; `KILL_PROC_ALLOWED` mirrors
`scripts/lib/killproc.sh`, a unit test checks both agree).

`metrics xlxd-sut` returns the Prometheus text of the system under test's hub in
`<id>.out`. The DV-Router image is `FROM scratch` (no shell, no `curl`) and the
hub listens on `127.0.0.1:19108` of the container's own network namespace, which
the tester cannot reach. The host starts a throwaway container of the tester
image that joins that namespace (`docker run --network container:<id>`,
`dvtest.limits.docker_run_args()` caps, all capabilities dropped, read-only root,
no new privileges) and does one `GET http://127.0.0.1:19108/metrics` with python
`urllib` (5 s timeout, no proxy). Allow-list: service `xlxd-sut` and no further
word (`scripts/lib/metrics.sh`); `error 1` = the GET failed (hub off, or a
`<state>/secrets/metrics-token` file makes it answer 401). Tester side:
`rig_control.metrics("xlxd-sut")` returns the text (`dvtest/control.py`,
`METRICS_ALLOWED` mirrors the bash function, a unit test checks both agree);
`dvtest/metrics.py` reads the format (`total(text, name, **labels)` sums the
samples whose labels contain the given ones, in any label order). The hub
refreshes the counters every 10 s, so a case polls for a change for up to about
25 s.

## Capture

`scripts/run.sh --capture` adds one sniffer container per watched service.
Each shares that service's network namespace (`network_mode: service:<svc>`)
and runs `dumpcap -i eth0`, so it sees exactly that service's traffic.
xlxd waits `DVT_START_DELAY` seconds (3 with `--capture`) so the sniffers
are up before the first packet. dumpcap stops at `CAPTURE_FILESIZE_KB`.

| File | Contains |
|------|----------|
| `capture/xlxd-ref.pcapng` | xlxd-ref <-> transcoder-ref, tester clients on the reference |
| `capture/xlxd-sut.pcapng` | xlxd-sut <-> transcoder-sut, tester clients, USRP A/B/C |
| `capture/transcoder-sut.pcapng` | everything to the transcoder under test, wire cases included |
| `capture/allstar.pcapng` | USRP A/B from the node side, IAX2 from the tester (profile allstar) |

The tester has no sniffer of its own; its traffic is seen at the other end.
transcoder-ref has no sniffer either: xlxd-ref.pcapng holds both directions
of xlxd-ref <-> transcoder-ref, and the only other traffic it gets (the wire
cases that compare a reply with the reference transcoder) is not captured.
The protocol docs take their evidence from the system under test.

Backend restarts: a sniffer lives in its service's network namespace, so a
control-channel `stop` or `kill` ends its file, and nothing is captured on
that service until `start`. After a successful `start` the watcher brings
`capture-<svc>` up again, writing the next free name: `<svc>.pcapng`, then
`<svc>.2.pcapng`, `<svc>.3.pcapng`, ... (dumpcap never appends). The xlxd
side of the gap is still in `xlxd-*.pcapng`; xlxd services are never stopped,
so their files are the complete record.

Every file also holds traffic that is not the rig's: multicast from the host
side of the bridge (`.1`: mDNS, SSDP, WS-Discovery), ARP "duplicate IP"
after a container restart, a few flooded unicast packets for other hosts,
and ICMP port unreachable for packets sent to a closed client port. Count
with an address filter and `!icmp`.

After the rig is down, `--capture` also runs `scripts/dvcheck.sh` on the run
directory (`out/<utc>/check.txt`); a failing check fails a run whose pytest
passed.

Decode with `scripts/tshark.sh` (tshark in the capture image, no host
install, dissectors from `tools/wireshark/dvtest.lua` loaded): see
`.agents/skills/dv-capture/SKILL.md` and `docs/protocols/`. Captures stay in
`out/` and are never committed; docs quote counts, sizes and header fields
only.
