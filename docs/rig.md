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
