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
| capture-* | (shares the netns) | capture | dumpcap on xlxd-ref, xlxd-sut, transcoder-sut, allstar |

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
