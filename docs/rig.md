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
| asl-reg / asl-a / asl-b / asl-e | 172.30.0.41 / .42 / .43 / .44 | asl-lab | AllStarLink test lab: registrar + DNS zone, private app_rpt nodes 199901, 199902, 299901 (e = stand-in for dv-edge-allstar), agent on :8099; see [asl-lab.md](asl-lab.md) |
| nxdn-ref / nxdn-sut | 172.30.0.50 / .51 | nxdn | NXDNReflector peered with xlxd-ref / xlxd-sut (module `DVT_NXDN_MODULE`, TG `DVT_NXDN_TG`) |
| p25-ref / p25-sut | 172.30.0.60 / .61 | p25 | P25Reflector peered with xlxd-ref / xlxd-sut (module `DVT_P25_MODULE`) |
| xlxd-peer-sut / xlxd-peer-ref | 172.30.0.12 / .13 | interlink | XLX998, interlinked with xlxd-sut / xlxd-ref on `DVT_INTERLINK_MODULES` |
| urfd | 172.30.0.70 | urfd | URFd (`URF999`, modules A-Z), a second reference reflector; see "URFd as the reference" |
| mrefd | 172.30.0.80 | mrefd | mrefd (`M17-REF`, modules `MNOP`), the M17 Project reflector, with interlink peers M17-DVT / M17-DVU at the tester; see "mrefd as an extra reference" |
| dvr-peer / transcoder-peer | 172.30.0.16 / .17 | twonode | a SECOND DV-Router node (XLX998, the image of `XLXD_SUT_IMAGE`, native launch, own rendered config) with its own transcoder, a plain XLX interlink peer of xlxd-sut on modules DE; the second node of the DP15 cases 7b-7d; see "Two DV-Router nodes (profile twonode)" |
| aprsc | 172.30.0.47 | aprs | a PRIVATE aprsc (APRS-IS server), standalone, client port TCP 14580, no uplink, no internet; the server side of the APRS-IS cases of DV-Router; see "Private APRS-IS server (profile aprs)" |
| dw-a / dw-b | 172.30.0.48 / .49 | dw | two real Dire Wolf TNCs on a modem loop (KISS TCP 8001 each, audio relayed over UDP 7355, no hardware); the TNC side of the DP15 RF tier B cases; see "Dire Wolf TNCs on a modem loop (profile dw)" |
| mmdvm-stack | 172.30.0.100 | mmdvm | the REAL client stack of a hotspot: upstream MMDVMHost with DMRGateway, YSFGateway, P25Gateway, NXDNGateway and DStarGateway against xlxd-sut, its modem a VIRTUAL MMDVM modem that the tester runs on UDP 3334 (no hardware, no internet); see "Real client stack (profile mmdvm)" |
| webload | 172.30.0.97 | webload | WebRTC load generator (aiortc), control server :3100 for the tester only; see "Web load profile" |
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
| UDP 4569 | IAX2 | allstar, asl-reg, asl-a, asl-b, asl-e |
| UDP 53 | DNS (lab zone) | asl-reg |
| TCP 14580 | APRS-IS (client port, user-defined filters) | aprsc |
| TCP 8099 | lab agent (JSON, tester only) | asl-reg, asl-a, asl-b, asl-e |
| UDP 41400 | NXDNReflector network (xlxd connects out) | nxdn-ref, nxdn-sut |
| UDP 41000 | P25Reflector network (xlxd connects out) | p25-ref, p25-sut |
| UDP 10002 | XLX interlink | xlxd-*, xlxd-peer-* |
| UDP 3334 / 3335 | MMDVM serial frames over UDP: the virtual modem (tester) / MMDVMHost (`[Modem] Protocol=udp`) | tester, mmdvm-stack (profile mmdvm) |
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
scripts/run.sh -m "rig or allstar" --profile allstar   # + AllStar, USRP on (native launch: usrp-rig.toml is added)
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
| `native` | the image's own entrypoint with `--config /etc/dv-router/dv-router.toml XLX999 <net>.11 <net>.21` (use this for every DV-Router run) |
| `import` | DEPRECATED, only for images before COMPAT1b part 2: `dv-xlxd-import --exec --dir /xlxd ...` (the converter shim of DV-Router), same three words; the product's import hand-off is being removed, `run.sh` prints a warning |

For a DV-Router image `direct` is the same import hand-off (the product hands the bare
words to the converter), so it goes away together with `import`: run DV-Router with `native`.
The converter itself (`dv-tool-xlxd-import`, an operator migrating from xlxd) stays; the
netns cases use it as a plain tool, see "netns cases" below.

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
| `<DP15_TX_ENABLE>` | `true` or `false`: `DVT_DP15_TX_ENABLE`, default `false` (any other value is exit 2); only `dp15-rf.toml` uses it (the AX.25 edge's `tx_enable` and the TX port's `tx`) |

Any other `<UPPER_CASE>` token left over is an error (exit 2, before anything
starts). Everything else stays as in the other modes: the `/xlxd` file mounts
(the template's `[ids]` reads `/xlxd/dmrid.dat` and `nxdn.csv`; the other files
are unused with a config file but harmless), `DVT_CONFIG_MOUNTED=1` and the
`DV_*` pins, which win over the file. With a file there is no importer, so
the `xlxd.interlink` / `xlxd.usrp` / `xlxd.whitelist` / `xlxd.blacklist` /
`xlxd.terminal` files are not read: the template itself must name the interlink,
NXDN and P25 peers. The USRP endpoints are the exception that `run.sh` fills in:
when `DVT_SUT_USRP` is on (`--profile allstar` and `--profile load` set it; `off`
keeps it out) it appends the snippet `rig/config/dv-router/usrp-rig.toml`
(next section) to `DVT_SUT_CONFIG_EXTRA`, which is what the AllStar application
cases (`cases/test_allstar.py`, `cases/proto/test_usrp.py`) need; it does not add
it when the template has `[[edges.usrp.endpoint]]` rows of its own, and it refuses
the combination with `echo-usrp-peer.toml` (both bind `<OWN_IP>:34003`).
`run.sh` warns when `DVT_NXDN`, `DVT_P25`, `DVT_INTERLINK` or
`DVT_XLXD_BLACKLIST` is set in this mode (`DVT_SUT_USRP` is no longer ignored,
so it draws no warning for `on` or `off`) and refuses
`DVT_UNSERVED_MODULE=M|N` (exit 2). The tester sees the mode in
`DVT_SUT_LAUNCH` (the M17 peer cases skip with the deprecated `import` only).

### netns cases with a DV-Router build (`scripts/dvr-import-wrap.sh`)

`cases/test_xlxd_usrp_netns.py` and `cases/test_xlxd_transcoder_lifecycle.py` run
`$DVT_XLXD_BIN XLX999 127.0.0.1 127.0.0.2` in a working directory that holds `xlxd.usrp`
(no docker, no rig). For a DV-Router build `DVT_XLXD_BIN=scripts/dvr-import-wrap.sh` and
`DVT_DVR_BIN_DIR=<dir>` (the binaries of the image under test, copied out with
`docker create` / `docker cp <id>:/usr/local/bin/. <dir>`) start it the way an operator migrating
from xlxd does, without the product's import hand-off:

1. `dv-tool-xlxd-import --dir $PWD --out $PWD/dv-router.toml XLX999 127.0.0.1 127.0.0.2`
   (the standalone converter, one-shot mode; `dv-xlxd-import` in older builds);
2. `DV_CONFIG_DIR=$PWD DV_RUN_DIR=$PWD dv-router --config $PWD/dv-router.toml XLX999 127.0.0.1 127.0.0.2`.

The wrapper prints `dvr-import-wrap: path=convert|shim|direct` on stderr (into the scenario's
`xlxd.log`); `shim` / `direct` are the fallbacks for builds whose converter has no `--out` or
that have no converter at all. Offline test: `cases/unit/test_dvr_import_wrap.py`.

Scenarios, waves and the task limit. Both files run one scenario per `-k` group of cases
("cases" name prefix of each scenario: only the scenarios that a selected case reads are
launched, in both files). One idle DV-Router scenario (launcher, 8 rooms, edges,
`dv-codec-ambed`, status, web, ...) peaks near 435 tasks (threads), and `scripts/contained.sh`
puts the job in a scope with `TasksMax=1024`. Starting all scenarios at once (8 in the USRP
file, 5 of them under the xlxd profile: `slow_open_listener`, `pre_ready_overflow` and
`open_late_std` have `profile="dvrouter"` and are not started under another `DVT_SUT_PROFILE`;
4 in the lifecycle file) needs about 2200 and starves the launcher (`clone()` fails with
`os error 11`); the cases then looked like a product fault ("never sent AMBEDPING"). So the
scenarios start in waves:

| Variable | Meaning |
|----------|---------|
| `DVT_NETNS_PARALLEL` | scenarios run at the same time, a positive integer, default `1`. The next wave starts when the previous one is finished; the wait limit (`TIMEOUT`) is per wave. `2` fits the scope on paper (870 of 1024) but leaves no headroom next to pytest and is not safe; do not use more than 1 with a DV-Router build. An xlxd reference run (`DVT_SUT_PROFILE` other than `dvrouter`, real `DVT_XLXD_BIN`) may raise it: results do not depend on it, only the wall time. |

Recipe (the default is the safe one; no scratch plugin is needed any more):

```
DVT_DVR_BIN_DIR=.cache/dvr-<tag> DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns \
  cases/test_xlxd_usrp_netns.py -q                    # whole file: 8 scenarios (5 under xlxd), one after the other
  # one scenario: add -k test_extended_ (or test_lost_listener_ for the lifecycle file)
  # slow STD over a short USRP burst: -k test_slow_open_ (no listener, no OPEN) or
  # -k test_open_wait_ (D-STAR listener, DV-Router profile only: the burst must be heard in full)
  # the documented limits of the opening session (DV-Router profile only, one scenario each):
  # -k test_pre_ready_cap_ (200 units faster than real time over a 1.0 s STD: the oldest 72 dropped)
  # -k test_open_refused_ (STD 1.5 s after the OPEN: open given up, the next over heard)
```

The H5 / H6 scenarios read the hub counters of the system under test from inside the namespace:
the converted config enables `dv-svc-status` with `metrics_bind = 127.0.0.1:19108`, the harness
GETs `/metrics` after the step and keeps the `dv_xcode_*` lines in `result.json` ("metrics").
The controllers put their counters on the fabric every 10 s, so the harness polls (at most
`METRICS_WAIT` = 15 s) until `dv_xcode_sessions_total` reached the value the scenario names
("metrics_wait"). `dvtest/proto/ambed.py` `FakeAmbed(open_delays=[...])` sets the STD delay per
OPEN (a timer thread, the control loop keeps answering PING and CAPS).

Wall time of a whole-file run with the default: the USRP file runs its 6 scenarios one after
the other instead of together: about 5 x 30 s instead of about 30 s; the lifecycle file
4 x about 55 s (`lost_and_back`, `listener_lost_and_back`, `fast_swap`, `legacy_swap`), about
3.5 minutes instead of about 1 minute. Results are the same.

When `xlxd.log` of a scenario shows `Resource temporarily unavailable (os error 11)` or
`can't spawn worker thread` (or `harness.err` shows `can't start new thread`) and the run did
not get as far as the first `AMBEDPING`, `get()` fails with
`environment: task limit exhausted (os error 11 / can't spawn worker thread in xlxd.log)`
instead of "xlxd never sent AMBEDPING"; that is the harness, not the product. A run that
pinged is never turned red by an old restart line. Logic and offline tests:
`dvtest/netns_runs.py`, `cases/unit/test_netns_runs.py`; classification of the red results of
`wip-0348b98`: `docs/netns-dvrouter-classification.md`.

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
| `dp15-paths.toml` | `[[data.path]]` `dp15-b-to-c` (class 0 text of room B forwarded to room C); name it after `data-ident.toml`. Only rows valid on the plain template: the launcher refuses a path to an edge that is not enabled (`docs/dp15-cases.md`) |
| `dp15-rf.toml` | the AX.25 edge for the DP15 RF cases of tier A (`cases/test_dp15_rf.py`, `docs/dp15-cases.md`): `[edges.ax25]` enabled, station callsign `N0TEST-1`, `tx_enable = <DP15_TX_ENABLE>`, two `kiss-tcp` ports to the tester (`rf-a` `<TESTER_IP>:8101` with `tx = <DP15_TX_ENABLE>`, `rf-b` `<TESTER_IP>:8102` receive only; the edge is the CLIENT, the tester plays the TNCs, reconnect 1 to 5 s) and the path `rf-in` (all ports to room B, text to `XLX999B`). Name it after `data-ident.toml` |
| `dp15-rf-out.toml` | the path `rf-out` (room B to `edge:ax25/rf-a`, text, addressee `XLX999B`, `min_ident = "session"`). ONLY with `dp15-rf.toml` and `DVT_DP15_TX_ENABLE=true`: the launcher refuses a path to an RF port without effective TX |
| `dp15-rf-igate.toml` | the transit path `dp15-igate` (`edge:ax25/rf-a` to `edge:aprsis`, `types = ["transit"]`, `per_min` 20) for the RF-to-APRS-IS igate cases (`cases/test_dp15_igate.py`, `docs/dp15-cases.md` "RF to APRS-IS igate"). ONLY with `dp15-rf.toml` AND `dp15-aprsis.toml` (the launcher refuses a path whose endpoint is not enabled); `DVT_DP15_TX_ENABLE=true` gives `qAR`, `false` gives `qAO` |
| `dp18-origin.toml` | DP18 node beacon (interval 600 s) and one node object `DVTOBJ1` to APRS-IS, no RF copies (`cases/test_dp18_origin.py`, `docs/dp18-cases.md`). ONLY with `dp15-aprsis.toml` and `data-ident.toml` |
| `dp18-origin-withheld.toml` | `[data] position_optout = ["N0TEST"]`: the position policy withholds the gateway call (DP18-e). ONLY with `dp18-origin.toml` |
| `dp18-rf.toml` | DP12c: `[edges.ax25]` callsign `N0TEST-5`, ports `rf-a` (`tx = true`) and `rf-b` (`tx = "simulate"`) on one physical, paths `rf-out` / `rf-out-sim` (`cases/test_dp18_rf.py`). INSTEAD of `dp15-rf.toml`; needs `dp15-aprsis.toml` |
| `dp18-rf-objects.toml` | DP18: the beacon and ten objects with RF copies on `rf-a` (DP18-o). INSTEAD of `dp18-origin.toml`; needs `dp18-rf.toml` and `dp15-aprsis.toml` |
| `xlx-peer.toml` | `[[edges.xlx.peer]]` XLX998 at `<NET>.12` on modules `DE` |
| `xlx-peer-host.toml` | the same peer named by HOST NAME (`xlxd-peer-sut`, resolved by the rig network DNS), for SANSIO4; `xlx-peer-host-bad.toml` adds a second row `no-such-peer.example.invalid` that never resolves (must stay pending, no start-up failure). Use one of the three XLX peer snippets, never two |
| `xlx-peer2.toml` / `xlx-peer2-host.toml` | the second XLX peer XLX997 (`xlxd-peer2-sut`, `<NET>.14`) by IP / by host name, for `test_interlink_no_xlx_transit` (it needs two XLX peers); add next to `xlx-peer.toml` / `xlx-peer-host*.toml` |
| `xlx-peer-tester.toml` | `[[edges.xlx.peer]]` XLX996 at `<TESTER_IP>` on modules `DE`: the tester plays the peer (`dvtest.proto.xlx.XlxPeer`), used by the xlx kind of E9 (`docs/echo-cases.md`) and by P19; use it instead of `xlx-peer.toml`, or next to it |
| `nxdn-peer-host.toml` / `-bad.toml` | the NXDN probe row of the rig (the tester as NXDN reflector: UDP 41411, TG 31673, module I) named by host name `tester`; `-bad` adds a row `no-such-peer.example.invalid`. For P19 `cases/test_edge_relink.py -k nxdn`, profile `nxdn` |
| `p25-peer-host.toml` / `-bad.toml` | the rig's P25Reflector `p25-sut` (id 31672, UDP 41000, module G) named by host name; `-bad` adds an unresolvable row. For P19 `-k p25`, profile `p25` |
| `usrp-rig.toml` | the three static USRP endpoints of the rig, what the (deprecated) import launch derived from `xlxd.usrp`: A `<OWN_IP>:34001` <-> `<NET>.40:32001` (AllStar node 1901), B `:34002` <-> `<NET>.40:32002` (node 1902), C `:34003` <-> `<TESTER_IP>:32003` (the tester), callsign `N0TEST`, `[edges.usrp] enable = "auto"`. Added by `run.sh` itself when `DVT_SUT_USRP` is on (see above); the `rig_usrp` fixture skips the AllStar application cases with the reason when the rendered config lacks these binds. Independent of the dynamic hub (`DVT_USRP_DYNAMIC=1` pins `edges.usrp.dynamic` through `DV_USRP_DYNAMIC`, UDP 34101) |
| `web.toml` | the web service, a persistent state dir and the WebRTC edge; added by `--profile web` itself (next section) |
| `recorder.toml` | the recorder (K14b): `[recorder] dir = "/var/lib/dv-recorder"`, `keep_days = 7`, `max_gb = 1`; naming it in `DVT_SUT_CONFIG_EXTRA` is the switch (see "Recorder" in the web profile section) |
| `guest.toml` | `[web] guest_listen = true`: anonymous guests may Listen (two per IPv4, no Talk); used by the K22 cases (`docs/web-k22-cases.md`); name it after `web.toml` |
| `totp-required.toml` | `[web] totp_required = true`, `[admin] totp_required = true`: operator and admin accounts must enrol TOTP. DV-Router TOTP1 made TOTP optional by default (first operator login answers totp `off`; the old keys `web.operator_totp` and `admin.totp` no longer force it), and the web fixtures start from "first login = enrol", so every web run on an image with TOTP1 names this file after `web.toml` (and `admin.toml`) |
| `echo.toml`, `echo-short.toml`, `echo-off.toml`, `echo-peers.toml`, `echo-usrp-peer.toml` (a static USRP peer on E) | `[echo] modules = { E = true }` (the built-in echo on module E, defaults 30 s / 1000 ms), then variants named after it: `max_seconds = 5`, `modules = { E = false }`, `modules = { E = true, M = true }` (`docs/echo-cases.md`) |
| `debug-log.toml` | `[log] level = "debug"`: debug lines of every SUT process in `out/<run>/logs/xlxd-sut.log` (chatty; short reruns only) |
| `soak-edges.toml` | `[edges.nxdn]` (`enable`, `tg_modules = "31672:F"`) and `[edges.p25]` (`enable`, `server_module = "G"`, `server_tg = 31672`): the SUT's own NXDN (41400) and P25 (41000) client ports for the soak (`docs/web-soak-cases.md`); the template's own tables are merged key by key |
| `mmdvm-edges.toml` | the same `[edges.nxdn]` and `[edges.p25]` values as `soak-edges.toml` (TG 31672 as NXDN module F and P25 module G) for the gateways of `--profile mmdvm`; `run.sh` adds it to a native launch with that profile unless it or `soak-edges.toml` is listed (`docs/mmdvm-stack-cases.md`) |

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

### Image generations (`dv-router:wip-c165c93` is the default; knobs for `wip-71ab207` and older)

Which DV-Router image is under test changes a few expectations. The defaults are those of `dv-router:wip-c165c93` and later; an older
image (`wip-71ab207`) needs the knobs set to the old value. run.sh passes `DVT_SUT_GEN` to the host-side render (it must be exported),
the tester gets the other two through `rig/compose.yml` (empty = the default).

| Knob | Values | What it selects |
|------|--------|-----------------|
| `DVT_SUT_GEN` | `c165c93` (default), `71ab207`, `c2465de`, `compat1` | the config dialect and the edge names of the image under test (`dvtest/sut_config.py`, `apply_gen`, `edge_name`, `env_prefix`). `c165c93` (ACL0c-f) removes the `[fabric]` table of the template: it is an unknown table of that validator (its only key `acl` was ignored with a warning before), so the file is refused without this. `71ab207` renders the template as it was. Both older images accept the `c165c93` rendering (checked), so the default is safe for either. **`c2465de` (tags >= wip-c2465de)** adds the rename of the DMR Homebrew edge `mmdvm` to `dmrhb`: the renderer turns `[edges.mmdvm]` / `[edges.mmdvm.x]` / `[[edges.mmdvm.x]]` into `edges.dmrhb`, `scripts/run.sh` writes `DV_DMRHB_NO_RADIO_ID` instead of `DV_MMDVM_NO_RADIO_ID`, and `edge_name("mmdvm")` / `env_prefix("mmdvm")` give `dmrhb` / `DV_DMRHB_` for cases and tools that name the edge. `dv-edge-dmrhb` is in `PROC_STAT_NAMES` / `scripts/lib/procstat.sh` next to `dv-edge-mmdvm` (the image has one of them). Child `dv-edge-dmrhb`, edge id `dmrhb-<inc>`, the API / events edge `dmrhb` and the metrics label `proc="dmrhb"` follow; wire id 2 and UDP 62030 are unchanged, and the MMDVM PROTOCOL name (spec entry `mmdvm`, `dvtest/proto/dmr.py`, the xlxd reference) is not renamed. On `wip-c2465de` the c165c93 rendering still loads (`[edges.mmdvm]` is a warning for one release). **`compat1`** is a superset of `c2465de`, see "compat1" below |
| `DVT_TOTP1B` | unset / `1` (default), `0` | the TOTP1b behaviour of the web side (`dvtest/totp_optional.py`, `web_wrong_password()`): a wrong password at the step-up and at disable is 403 `totp_invalid` (as on admin), and the login page clears a stale wrong-code banner (`test_operator_login_with_totp_step`). `0` = `wip-71ab207`: 401 `unauthorized`, banner not asserted |
| `DVT_ASL_PHONE_REG` | unset / `1` (default), `0` | the phone-mode IAX2 registration cases R8-R12 of `cases/test_asl_edge.py` (REGREQ / REGAUTH / REGACK, REGREJ for a wrong secret; ASL1g). `0` skips them for an image before ASL1g (it answers a REGREQ with REGREJ) |

Keys that became validator ERRORS in `wip-c165c93` and are not in the rig's snippets or in the template apart from `[fabric]`:
`fabric.acl` / `[fabric]` (ACL0c-f; the ACL matrix gate is gone), `access.allow_file` / `access.deny_file` (CFG2: lists go inline in
`access.allow` / `access.deny`), `[callhome] interlink_file` (NOX1; the interlink list comes from the `[[edges.xlx.peer]]` rows,
`DV_CALLHOME_INTERLINK_FILE` only warns). `web.operator_totp` / `admin.totp` are only warnings (ignored since TOTP1); `web.toml` and
`admin.toml` keep them for 71ab207.

**Checking a rendered config without the rig.** The image has no separate validate command: `dv-router --config FILE ...` validates
first and exits 2 with `N error(s), not started`. `python3 -m dvtest.tools.config_check --image dv-router:wip-c165c93 --template
<template> [--extra rig/config/dv-router/web.toml ...] [--gen 71ab207|c165c93]` renders as run.sh does and runs exactly that in a throw-away
container (`--network none`, the `dvtest.limits` caps, read-only root, `--cap-drop ALL`, `no-new-privileges`, the TOML mounted read-only, empty 0600 stand-ins for the
`*_file` secrets, no ports), prints the errors and warnings and kills the container once the router is past validation. Exit 0 valid,
1 invalid, 2 the check itself failed. Checked on `wip-c2465de` with `DVT_SUT_GEN=c2465de` (no errors, no warnings about names) and with the c165c93 rendering (only the `edges.mmdvm is renamed` warning), and on `wip-c165c93` with the default render (`c165c93`; `wip-71ab207` accepts it too): the template with `web.toml` + `admin.toml`
(+ `totp-required.toml`, `guest.toml`, `recorder.toml`, `soak-edges.toml`, `turn.toml`), `data-ident.toml`, `debug-log.toml`,
`xlx-peer.toml`, `allstar-lab.toml` (+ `-lookup`, `-out`, `-reg`) are valid; rendered with `--gen 71ab207` the template is refused by c165c93
(`fabric.acl was removed with the broker ACL`). Warnings only: `admin.totp` / `web.operator_totp` ignored, the `ids.dmrid` / `ids.nxdn`
seed files not found (no mount in the check), `node.state_dir` unset. The ASL1h registration log lines are checked after a run:
`python3 -m dvtest.tools.asl_reg_log out/<run>/logs/xlxd-sut.log --expect-registrations 1` (run.sh writes the log at the end).

#### compat1 (DV-Router COMPAT1: the backward-compat shims are removed)

`DVT_SUT_GEN=compat1` is a superset of `c2465de`; the repo default stays `c165c93` and every older value renders exactly as before. The
snippets in `rig/config/dv-router/` keep the keys of the older images; the renames happen at render time for compat1 only
(`sut_config.adapt_text` per template / extra text before the merge, `apply_gen` after it). `DVT_SUT_GEN` also reaches the tester now
(`rig/compose.yml`, empty = default) for the two gen-dependent expectations below.

| What | compat1 | older gens |
|------|---------|------------|
| `web.toml` `[web] operator_totp`, `admin.toml` `[admin] totp` | `= true` is DROPPED (the old key meant "totp on", it never forced enrolment; the compat1 default is optional TOTP and `totp-required.toml` sets `totp_required = true` when a run wants it mandatory: renaming `= true` to `totp_required = true` made the TOTP1 optional cases fail with totp `enrol` on the first compat1 run); any other value is rendered as `totp_required` with the value kept; a line whose `totp_required` is already in the table is dropped, never repeated | old keys (warnings since TOTP1) |
| `[[edges.xlx.peer]]` rows (`xlx-peer*.toml`, `xlx-peer-host*.toml`, `xlx-peer-tester.toml`, and the template's own rows) | `kind = "xlx"` added as the first key of a row that has no `kind` | no `kind` (unknown key there) |
| `DV_<EDGE>_NO_RADIO_ID` written by `scripts/run.sh` | `DV_DMRHB_*` only (the `hb=` case lists `c2465de` and `compat1`); no `DV_MMDVM_*` name is written | `DV_MMDVM_*` before c2465de |
| `[edges.mmdvm]` | renamed to `edges.dmrhb` as on c2465de | as before |
| web operations (`api_web.web_ops()` / `web_keys()`, `cases/test_web_api.py`) | `PUT /api/v1/auth/password` is gone: 47 audience operations, 51 with the four root probes (`POST` stays) | 48 / 52 |
| `dv_edge_data_total` (`cases/test_data_ident.py`, `metrics.edge_data_family()`) | removed; no successor name is known yet, so the edge counter is read as 0 and the records say "n/a" (nothing asserted it). Set `metrics.EDGE_DATA_SUCCESSOR` when DV-Router names the new family | read as before |

Offline check of any rendered TOML: `dv-router config check FILE` (no network, no writes; exit 0 valid, 2 invalid on wip-488b89d, one
`FILE:LINE:COL: KEY: MESSAGE` line per error). `python3 -m dvtest.tools.config_check --gen compat1` (or `--mode check`) runs it in the same
throw-away container as before (`dvtest.tools.config_check.judge_check` reads it; the older `--config FILE` start check stays for
`c165c93`, `71ab207`, `c2465de`, and an image without the subcommand answers with a usage text and is reported as "no `config check`").
The rig itself has no hook for it: `scripts/run.sh` has no config-check call. The "old key must fail with a clear error" cases are
`cases/test_config_load.py` (`-m cfgload`, host side, not `-m rig`): see [config-load-cases.md](config-load-cases.md).

Three host-state traps that DV-Router named for rig / dev hosts that keep a state directory across images (none is produced by the rig:
`grep` of `rig/`, `dvtest/` and `scripts/` finds no `limits` row, no managed header and no `accounts.jsonl` writer; the rig's `sut-state/`
is created fresh per run and dv-svc-web writes its accounts itself):

- a baseline `web/accounts.jsonl` with rows that carry `limits:{}` stops `dv-svc-web` from starting (fail-closed). Delete or fix the
  rows before reusing an old state dir;
- a baseline-imported state TOML that still has the old managed header is run as native (hand-written) config. Regenerate it or
  remove the old header, do not assume the import tool owns it;
- `dv-svc-callhome` on a fresh install needs `identity create` first, or it does not start.

## AllStarLink test lab (profile asl-lab)

`scripts/run.sh --profile asl-lab -m asl_lab` starts only the tester and four lab hosts (a
registrar with the lab DNS zone and three private app_rpt nodes), or joins them to any other
profile. No `TRANSCODER_IMAGE` is needed on its own. Scenarios, pcaps, recordings, versions and
the frame extractor are described in [asl-lab.md](asl-lab.md).

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
| admin service | `admin.toml` merges `[admin] enable = true`, `bind = "0.0.0.0:8080"`, `trusted_proxies = [<WEB_PROXY_IP>]`, `host = "dvt-admin:8443"`, `allow_from = [<TESTER_IP>, <BROWSER_IP>]` (the `/admin/` prefix mount is served only to listed addresses), `totp = true`. Users: `run.sh` writes `sut-state/admin/users` (mode 0600; `admin`, `operator`, `viewer`, random passwords; since ADM1 the accounts are added with the SUT image's own `dv-svc-admin user add NAME ROLE --state-dir DIR` (password on stdin, throw-away container with the `dvtest.limits` caps and no network; `DVT_ADMIN_USERS=auto|image|python`, the route is logged and kept in `out/<utc>/web/admin-users.route`), falling back to pure-Python Argon2id hashes from `dvtest/argon2_pure.py` when the image lacks the subcommand; no `:totp=` field so each enrols at its first login) and the same logins for the tester in `out/<utc>/web/admin.env` (mode 0600). The bootstrap file `secrets/admin-bootstrap` is not used: it creates only one admin, and the role matrix needs a viewer and an operator too |
| `web-proxy` | service at `<net>.90`, network aliases `dvt-web` (`DVT_WEB_HOST`) and `dvt-admin` (`DVT_ADMIN_HOST`), HTTPS on 443 (web) and `DVT_ADMIN_PORT` 8443 (admin, `--route`, same certificate and CA); limits set `medium` (`init: true`, read-only root, all capabilities dropped, runs as the host uid). Image `dvt-web-proxy:local` from `rig/Dockerfile.web-proxy`: `TESTER_BASE_IMAGE` (python and the openssl CLI are in it) plus the stdlib `dvtest` package; no new pin, no new image pulled |
| tester | `DVT_WEB=on`, `DVT_WEB_URL=https://dvt-web`, `DVT_ADMIN_URL=https://dvt-admin:8443`, `DVT_WEB_PROXY=<net>.90` |

**The proxy** (`dvtest/tools/tls_proxy.py`, unit tests `cases/unit/test_tls_proxy.py`
against a fake backend) is repo-owned stdlib Python rather than nginx or caddy: it
adds no image and no pin, and what the cases need is small and exactly testable:
it streams both ways without buffering (SSE frames reach the client as they
leave the SUT, and a client that goes away closes the upstream stream at once, so the service frees its stream slot then and not at its next heartbeat; request bodies of any size go through with their length or
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

## Web load profile

`scripts/run.sh --profile web,webload` adds the service `webload` (`172.30.0.97`, control server `http://<net>.97:3100`) for the
W8 load case (`cases/test_web_load.py`, `docs/web-load-cases.md`) and the 60 minute soak (`cases/test_web_soak.py`, `docs/web-soak-cases.md`; the generator then also leaves and rejoins listeners: `POST /run` with a `soak` table of sessions). `webload` alone is exit 2.

| What | Detail |
|------|--------|
| why a service | 200 receive-only WebRTC peers need a real WebRTC stack (ICE, DTLS-SRTP) and the tester image is stdlib only. The peers run in their own container, own address (the web service counts logins and joins per client address, so the generator has its own budgets), and the tester drives them over a small HTTP/JSON control server |
| image | `rig/Dockerfile.webload` on `TESTER_BASE_IMAGE` plus the pinned aiortc set `WEBLOAD_PIP_PINS` of `.env.example` (aiortc 1.14.0 and its resolved dependencies, `pip install --no-deps --only-binary`, `pip check`); the build is the only internet use. About 410 MB |
| caps | own set `x-limits-loadgen`: 3g = swap 3g (`DVT_MEM_LOADGEN`), 4 cpus (`DVT_CPUS_LOADGEN`), 512 pids; read-only root, tmpfs `/tmp`, all capabilities dropped, `out/<run>/web` mounted read-only (the run CA). The control server answers the tester address only |
| control | `POST /run` (accounts, listener count, workers), `GET /status`, `GET /sample` (cgroup and process cpu), `POST /finish` (statistics of every listener, one JSON document), `POST /abort`. Nothing is written to disk |
| SUT side | the read-only control action `proc-stat xlxd-sut <process>` (`scripts/lib/procstat.sh`, `dvtest.control.PROC_STAT_NAMES`: the launcher `dv-router`, `dv-edge-xlx`, `-nxdn`, `-p25`, `-usrp`, `-webrtc`, `dv-svc-status`, `-web`, `-admin`, `dv-room`, `dv-recorder`, `dv-codec-ambed`; the word `list` prints pid, ppid and comm of every process) returns the cpu ticks, thread count, VmHWM/VmRSS, the open fd count (`fds`, -1 when unreadable) and the start time of the process by exact `/proc` comm, read by a throwaway tester-image container in the SUT's pid namespace that runs as the SUT container's user (root when its image names none; no capability), because `/proc/<pid>/fd` is readable only for the same uid |

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
| `nat-router` | outside `<net>.92` (alias `<net>.93`), inside `<nat-net>.2` | iptables NAT between the two networks, forwarding on (`net.ipv4.ip_forward=1` is a sysctl of the service only), JSON-line control server on `<net>.92:9100` (tester only), TCP forwards `<net>.92:3000` and `:3001` to the browser, and a small AF_PACKET sniffer for the `firsts` evidence of the ICE1 cases. Capabilities: `NET_ADMIN`, and `NET_RAW` for the sniffer (this service only; without it only the `firsts` evidence is missing) |
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
`drop_direct on|off` (drops UDP from inside to the SUT's media address:port, so only the TURN path can work), `readdress on|off` (ICE1: moves the browser's UDP
to the other outside address, `<net>.92` <-> the alias `<net>.93`, TCP unchanged, and back; docs/web-nat-cases.md "ICE1 address change"), `mappings` (read only: the browser's UDP flows
to the SUT's media port as conntrack holds them), `firsts` (read only: the first datagram per direction and kind and the SUT's stale sends per second since the last
`readdress on`, from the sniffer), `status`, `reset`.
The control server listens on the outside address only and answers the tester's address only; the browser cannot reconfigure it.

**TURN secret.** `run.sh` makes `out/<utc>/web/turn.secret` (random, mode 0600, no trailing newline, never printed) and coturn's
private `out/<utc>/coturn/turnserver.conf` (mode 0600, holds the same secret as `static-auth-secret`) with `python3 -m dvtest.turn make`.
With `turn.toml` the secret is mounted read-only into the SUT at `/run/secrets/turn-secret` (the SUT runs as the host uid, the file is
owned by it with mode 0600) and `[web] turn_urls = ["turn:<net>.94:3478?transport=udp"]`, `turn_secret_file`, `turn_ttl = 120`
come from the snippet. After the run `dvtest.turn scan` looks for the secret in every file under `out/<utc>/` except those two
(captures included) and fails the run (exit 5) if it finds it.

**Tester side.** `DVT_NAT=on`, `DVT_NAT_CTL`, `DVT_NAT_PROBE`, `DVT_NAT_REFLECTORS`, `DVT_NAT_ALIAS`, `DVT_NAT_INSIDE`, `DVT_TURN`,
`DVT_TURN_SECRET_FILE`, `DVT_TURN_TTL`, `DVT_NAT_EXPIRY_S`, `DVT_NAT_SHORT` (the short profile, docs/web-nat-cases.md); `DVT_BROWSER_WS` points at `ws://<net>.92:3000/` (the router's forward).
`cases/nat_support.py` has the client and the page helpers. Unit tests: `cases/unit/test_nat.py`, `cases/unit/test_nat_readdress.py`, `cases/unit/test_ice1.py`, `cases/unit/test_turn.py`.

## Private APRS-IS server (profile aprs)

The APRS-IS cases of DV-Router (DP15-5a / 5b, [dp15-cases.md](dp15-cases.md)) need an APRS-IS server that is not the public network. The
service `aprsc` (`--profile aprs`, 172.30.0.47) is [aprsc](https://github.com/hessu/aprsc) 3.0.0 (BSD-3-Clause), pinned in `.env.example` (`APRSC_REPO`,
`APRSC_COMMIT` = the commit of the tag `release/3.0.0`, `APRSC_BASE_IMAGE`) and fetched once by `scripts/pre-fetch-aprsc.sh` into `.cache/aprsc` (the only
internet use; the image build itself installs Debian packages as every other image does). `rig/Dockerfile.aprsc` builds it (multi-stage, `./configure --disable-is2`,
a stripped binary, the runtime libraries by package name; about 123 MB on disk, 31 MB compressed) as `dvt-aprsc:local`, run as the non-root user `aprsc`
(uid 10004). To build it by hand, inside the build scope and never while the rig runs:

```
scripts/pre-fetch-aprsc.sh
scripts/contained.sh --name build -- docker build -f rig/Dockerfile.aprsc --build-arg APRSC_BASE_IMAGE=debian:bookworm-slim \
  --build-arg MAKE_JOBS=4 --build-context src=.cache/aprsc -t dvt-aprsc:local rig
```

`run.sh --profile aprs` builds it the same way when needed.

* The service is on the internal rig network only (no route out), nothing is published, `read_only` root with a tmpfs `/tmp`, `cap_drop: [ALL]`,
  `no-new-privileges`, the `x-limits-small` set. The fixture config `rig/config/aprsc/aprsc.conf` is mounted read-only (a copy is baked into the
  image and checked with `aprsc -y` at build time): `ServerId DVTEST`, a fixture `PassCode`, no `Uplink`, no HTTP listener, one client port
  `Listen "Client-Defined Filters" igate tcp 0.0.0.0 14580 noIS2upgrade` (user-defined filters allowed, plain text protocol). The log goes to stderr:
  `out/<run>/logs/aprsc.log`. An `ERROR: Language file search failed` line at start is harmless (the HTTP thread finds no web files; no HTTP port is configured).
* Address: .46 was taken (the alias address of `asl-nat-a`, profile `asl-nat`), so the server is at .47.
* Logins are verified against the public APRS passcode hash of the login callsign (`dvtest/aprs_passcode.py`; a wrong or missing passcode logs in
  `unverified` and aprsc drops that client's packets). Fixture logins, all synthetic: the DV-Router edge `N0TEST-5`, the tester `N0TEST-7`. aprsc drops
  packets whose SOURCE starts with `N0CALL`, `NOCALL` or `SERVER`, so every source here is `N0TEST-n` or `XLX999..`. The DV-Router passcode file is
  written by `run.sh` to `out/<run>/aprs-secrets/passcode` (mode 600; nothing secret, a function of a synthetic callsign) and mounted read-only at
  `/etc/dv-router/aprs-secrets`; the SUT then runs as the host user, like with the web and asl-edge profiles.
* `DVT_SUT_CONFIG_RW=1` (native launch, off by default): the rendered config is copied to `out/<run>/xlxd-config/xlxd-sut/rw/dv-router.toml` and that
  directory is mounted read-write at `/etc/dv-router-rw` (the image starts with `--config /etc/dv-router-rw/dv-router.toml`), so that dv-svc-admin can
  replace the file (it writes a temporary file and renames it; the usual single-file read-only mount refuses). For the cases that apply a configuration
  through the admin API, [secreload-cases.md](secreload-cases.md). The rendered file next to it is then not the live one.
* DV-Router side: the snippet `rig/config/dv-router/dp15-aprsis.toml` (`[edges.aprsis]` pointing at `<NET>.47:14580`, and the path row `aprsis-down`
  `edge:aprsis -> room:B`, addressee `XLX999B`) is added by `run.sh` when `--profile aprs` is given and it is not listed in `DVT_SUT_CONFIG_EXTRA`. The
  profile needs `DVT_SUT_LAUNCH=native`. The tester gets `DVT_APRS=on` and `DVT_APRSC=<net>.47`.
* The tester client is `dvtest/aprsis_client.py` (stdlib sockets: login with the computed passcode, filter, TNC2 message lines, a reader thread).

## Dire Wolf TNCs on a modem loop (profile dw)

The DP15 RF cases of tier B ([dp15-cases.md](dp15-cases.md), `cases/test_dp15_rf_dw.py`) connect DV-Router's AX.25 edge to REAL TNCs. The services
`dw-a` (172.30.0.48) and `dw-b` (172.30.0.49) are two containers of `dvt-direwolf:local` (Dire Wolf 1.8.1, pinned `DIREWOLF_*` in `.env.example`, source fetched once by
`scripts/pre-fetch-direwolf.sh` into `.cache/direwolf`, built by `rig/Dockerfile.direwolf`, 180 MB; to build by hand, inside the build scope and never while the rig
runs, see the header of the Dockerfile). Each runs `entry.py loop` ([direwolf-cases.md](direwolf-cases.md), "Modem loop"): one Dire Wolf with KISS TCP on 8001 (clients: the
edge and the tester, three at most), audio in on UDP 7355, and the audio it transmits relayed as UDP to the other one's port 7355. So a frame sent into one KISS port
is modulated, demodulated by the other and handed to the other's KISS clients, without a sound card or any privilege.

* Internal rig network only, nothing published, `read_only` root with a tmpfs `/tmp` (the FIFO and the configuration), `cap_drop: [ALL]`, `no-new-privileges`, the `x-limits-small`
  set (a node uses about 10 MB and 11 processes), `restart: "no"`; the node starts its Dire Wolf again if it exits. The log goes to stdout: `out/<run>/logs/dw-a.log`, `dw-b.log`.
* Needs `DVT_SUT_LAUNCH=native`; `run.sh --profile web,dw` adds `rig/config/dv-router/dp15-rf-dw.toml` to `DVT_SUT_CONFIG_EXTRA` when it is not listed (the edge's ports `rf-a` / `rf-b`
  point at `<NET>.48:8001` / `<NET>.49:8001`; the token `<DP15_TX_ENABLE>` and the file `dp15-rf-out.toml` work as in tier A). The tester gets `DVT_DW=on`, `DVT_DW_A`, `DVT_DW_B`.
* Free host numbers: .48 and .49 (.46 is the alias address of `asl-nat-a`, .47 is `aprsc`). The loop without the rig: `python3 -m dvtest.oracle_direwolf loop`.
* Traps: `KISSPORT 0` must come first in a Dire Wolf configuration that sets a port (otherwise the built-in 8001 stays); a TNC does not hear what arrives while its own transmission is
  still "on the air" (the cases pause); the UDP audio between the containers has no loss protection (an internal bridge dropped nothing in the tests).

## Two DV-Router nodes (profile twonode)

The DP15 cases 7b, 7c and 7d ([dp15-cases.md](dp15-cases.md), `cases/test_dp15_peer.py`) need a receiving node with DV-Router semantics (a floor, a recorder, a
transcoder), which the xlxd peer of the `interlink` profile (`xlxd-peer-sut`, `.12`, "no transcoder: the interlink carries every codec as is") cannot be. The profile `twonode` adds
a second DV-Router node next to the SUT. Nothing is built: it is the prebuilt image of `XLXD_SUT_IMAGE`.

| Service | Address | What it is |
|---------|---------|------------|
| `xlxd-sut` | `.11` | node 1, XLX999, as in every native run; `dp15-twonode-a.toml` (added by `run.sh`) gives it the peer row to node 2 and the recorder |
| `dvr-peer` | `.16` | node 2, XLX998: `command: --config /etc/dv-router/dv-router.toml XLX998 <net>.16 <net>.17` |
| `transcoder-peer` | `.17` | the transcoder of node 2, the same image as `transcoder-sut` (the fake with `DVT_TRANSCODER_FAKE=1`), peers `.16` and `.30` |

* `dvr-peer` is hardened like the helper services (host uid, `cap_drop: [ALL]`, `no-new-privileges`, `read_only`, tmpfs `/tmp` and `/run` for the run directory, `x-limits-medium` with the pids and memory
  caps of `xlxd-sut`, no published ports) and mounts three things: its rendered config (read-only), the id seed files `rig/config/dmrid.dat` / `nxdn.csv` at `/xlxd` (read-only) and the host
  directory `out/<run>/recorder-peer` (read-write) at `/var/lib/dv-recorder`.
* The config is rendered by `scripts/run.sh` (`render_dvr_peer`) from the SAME template as the SUT (`DVT_SUT_CONFIG`, so the same dialect and `DVT_SUT_GEN`), with `--own 16 --transcoder 17 --tester 30`
  and the snippet `rig/config/dv-router/dp15-twonode-b.toml` only (no web, data-ident or echo extras of the SUT): `[node] callsign = "XLX998"`, the peer row `XLX999` at `<NET>.11` on modules DE and a
  `[recorder]`. The result is `out/<run>/xlxd-config/dvr-peer/dv-router.toml`. The template's M17 peer rows (the tester) stay in it; nothing plays those peers in the two-node cases.
* The SUT side: `dp15-twonode-a.toml` has the peer row `XLX998` at `<NET>.16` on DE and a `[recorder]` (`out/<run>/recorder` mounted at `/var/lib/dv-recorder`; the SUT then runs as the host uid, as with the
  web profile). Both ends dial each other on UDP 10002 (`edges.xlx.port`; there is no per-peer port key).
* Control channel: `metrics dvr-peer` returns the hub metrics of node 2 (`dvtest.control.METRICS_ALLOWED`, `scripts/lib/metrics.sh`; the same fixed URL inside the container's namespace). No kill, no
  proc-stat, no stop/start of `dvr-peer`.
* The tester gets `DVT_TWONODE=on`, `DVT_DVR_PEER` (`.16`), `DVT_DVR_PEER_TRANSCODER` (`.17`). Needs `DVT_SUT_LAUNCH=native` and `XLXD_SUT_IMAGE` (`run.sh` exits 2 otherwise); `--profile interlink` with it is refused
  (the xlxd peer is also XLX998). Free host numbers used: `.16` and `.17` (`.12` to `.15` are the xlxd peers of `interlink`).
* Limits: the second node has no web service (the proxy `web-proxy` forwards to `<net>.11:8081` only); the web-side observations of 7b-7d are not part of the rig (dp15-cases.md).
* Run command: [dp15-cases.md](dp15-cases.md), "Two DV-Router nodes".

## Real client stack (profile mmdvm)

`cases/test_mmdvm_stack.py` ([mmdvm-stack-cases.md](mmdvm-stack-cases.md)) runs the software of a Pi-Star hotspot against the reflector under test. The service
`mmdvm-stack` (172.30.0.100, image `dvt-mmdvmstack:local`, built by `run.sh` on first use from `rig/Dockerfile.mmdvmstack` after `scripts/pre-fetch-mmdvmstack.sh`; the pins are `MMDVMSTACK_*` in
`.env.example`) is ONE container with MMDVMHost, DMRGateway, YSFGateway, P25Gateway, NXDNGateway, DStarGateway and a loopback-only mosquitto broker, each under a restart loop
(`rig/mmdvmstack-entry.sh`; every log line prefixed with the program name: `out/<run>/logs/mmdvm-stack.log`). Its modem is the VIRTUAL modem of `dvtest/mmdvm_modem.py` in the
tester.

* Configuration: the templates `rig/config/mmdvmstack/*.tmpl` are rendered at start from the compose environment (`MODEM_ADDR` = the tester, `MODEM_PORT` 3334, `LOCAL_ADDR` = `.100`,
  `LOCAL_PORT` 3335, `REFLECTOR_IP` = `.11`, `CALLSIGN` N0CALL, `DMR_ID` 1023007, the modules `MODULE_DSTAR`/`MODULE_DMR`/`MODULE_YSF` = `H`/`I`/`J`, the talk groups `P25_TG`/`NXDN_TG` 31672;
  tester side: `DVT_MMDVM_*`). Every host file points at `.11` and nothing needs the internet (D-STAR over DExtra, no ircDDB, no DPlus authentication, no id database download;
  the synthetic `rig/config/dmrid.dat` is mounted read-only).
* Hardening and limits: read-only root, tmpfs `/tmp`, no capabilities, `no-new-privileges`, uid 10005, `x-limits-small` with `pids_limit` 256 (`DVT_PIDS_MMDVM`), `stop_grace_period` 20 s.
* The gateways link at start; `scripts/run.sh` lets the control channel `stop` / `start` / `restart` / `kill` the service `mmdvm-stack` (case MMDVM3). D-STAR and DMR send an unlink at
  SIGTERM, YSF, P25 and NXDN do not.
* `scripts/run.sh --profile mmdvm` (use with `web` to read the public state) checks `.cache/mmdvmstack/MMDVMHost` exists (exit 2 otherwise) and gives the tester `DVT_MMDVM=on`,
  `DVT_MMDVM_STACK`, `DVT_MMDVM_MODEM_PORT` and the module / talk group variables. Free host numbers used: `.100` (`.98` to `.127` are free; `.128/25` is the load nodes).
* The wire checker knows `.100` as a rig host (`CHECK_HOST_OFFSETS`); the modem traffic (tester <-> `.100`, UDP 3334 / 3335) is not on any captured service.

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

`hup xlxd-sut` (SECRELOAD1 only, allowed by dv-router; `scripts/lib/hup.sh`, `dvtest.control.HUP_ALLOWED`,
`rig_control.hup()`) sends SIGHUP to the launcher, PID 1 of the dvt project's `xlxd-sut` container (`docker kill
--signal=HUP`): a reload of the unchanged TOML that re-reads every referenced secret file. No other service, no
other signal, no word after the service; `<id>.out` has `t_hup=<epoch s>`. `kill-proc` still never signals the
launcher.

`kill-proc xlxd-sut <process> <signal> [rooms=<LETTER>]` signals one process inside the
system-under-test container (the DV-Router image is `FROM scratch`: no shell,
`ps` or `pkill`, and the host user may not signal root's processes). The host
starts a throwaway container of the tester image that joins the target's pid
namespace (`docker run --pid container:<id>`, `dvtest.limits.docker_run_args()`
caps, no network, only CAP_KILL) and walks `/proc/<pid>/comm`: a process is
signalled only when its comm equals `<process>` exactly and it is not a zombie.
Allow-list: `dv-edge-xlx`, `dv-edge-nxdn`, `dv-edge-p25` with `KILL` or `USR1` (quiet restart: the edge saves its sessions, exits and the
launcher respawns it) the codec worker `dv-codec-ambed`, the web service `dv-svc-web` (its in-memory login/register limits and lockouts), the WebRTC edge `dv-edge-webrtc` and the status hub `dv-svc-status` (K22 cases, `docs/web-k22-cases.md`) and the data worker `dv-data-worker` (DP15 cases, `docs/dp15-cases.md`; the comm is the same for every worker id, the id is only in the env `DV_DATA_WORKER_ID`, the launcher label `dv-data-worker[<id>]` and the fabric token `pkt/<id>`; the launcher respawns it after 0.5 s doubling to 5 s) with `KILL` only; one room controller `dv-room` with `KILL` only and ONLY with the fifth word `rooms=<LETTER>` (one `A-Z`; the process must also carry `--rooms <LETTER>` in its NUL-separated `/proc/<pid>/cmdline`, which the launcher passes as two arguments; the single token `--rooms=<LETTER>` and a comma list holding the letter match too; LEAK2 cases, `docs/stream-registry-cases.md`; the launcher respawns it; without the selector a `dv-room` kill is refused so no blanket kill of every controller exists, and the selector is refused for every other process); the launcher `dv-router` is never signalled (no handler, a
stray signal kills the node); anything else
is `error refused`. `<id>.out` has `pid=<n>` per process and `t_kill=<epoch s>`
(taken in the container right after the signals); `error 3` = no such process.
Tester side: `rig_control.kill_proc(service, process, signal[, rooms="B"])` returns the pids
and `t_kill` (`dvtest/control.py`; `KILL_PROC_ALLOWED` mirrors
`scripts/lib/killproc.sh`, a unit test checks both agree).

`metrics xlxd-sut` (or `metrics dvr-peer`, the second node of `--profile twonode`) returns the Prometheus text of the system under test's hub in
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

The checker takes a peer for a rig host when its address is in `HOST_OFFSETS` or
`CHECK_HOST_OFFSETS` (`dvtest/target.py`; the second holds the hosts of the profiles
twonode, asl-lab, asl-ext, asl-nat, aprs and dw). A service added to
`rig/compose.yml` must be added to one of them (or to the excuse table of
`cases/unit/test_check_rig_hosts.py`, which fails otherwise); without it every
datagram to that host is an `unknown_peer` error (the first twonode run: 581).

Decode with `scripts/tshark.sh` (tshark in the capture image, no host
install, dissectors from `tools/wireshark/dvtest.lua` loaded): see
`.agents/skills/dv-capture/SKILL.md` and `docs/protocols/`. Captures stay in
`out/` and are never committed; docs quote counts, sizes and header fields
only.
