<!-- SPDX-License-Identifier: MIT -->
# The real client stack: MMDVMHost and its gateways on a virtual modem

The main real-world client of a reflector is a Pi-Star style hotspot: the upstream **MMDVMHost** talking to one **gateway** per mode
(DStarGateway, DMRGateway, YSFGateway, P25Gateway, NXDNGateway). The profile `mmdvm` runs exactly that software, unmodified, against the
reflector under test, and replaces the radio by a **virtual MMDVM modem** written for dv-testset (`dvtest/mmdvm_modem.py`). It is an
addition to the protocol clients of `dvtest/`: those speak one protocol each; this is the whole client, with its own link management,
keepalives, header rewriting and reconnect logic. WRITTEN, NOT RUN ON A RIG: the image has never been built (see "UNVERIFIED").

```
  tester .30                                        mmdvm-stack .100 (one container, profile mmdvm)
  +---------------------------+  UDP 3334 / 3335   +----------------------------------------------+
  | VirtualModem (pytest)     |<------------------>| MMDVMHost  [Modem] Protocol=udp              |
  |  answers version/status   |   MMDVM serial     |   |20011 |62032 |3200  |32010 |14021        |
  |  key-up: D-STAR DMR YSF   |   frames, one per  |   v      v      v      v      v             |
  |          P25 NXDN         |   datagram         | DStarGW DMRGW  YSFGW  P25GW  NXDNGW (+ mosquitto on loopback)
  |  records what the host    |                    +-------|-------|------|------|------|--------+
  |  writes down to "RF"      |                            | DExtra| XLX  | YSF  | P25  | NXDN    <- the reflector under test
  +---------------------------+                            v 30001 v 62030 v 42000 v 41000 v 41400   xlxd-sut .11
```

## Files

| File | What |
|------|------|
| `dvtest/mmdvm_modem.py` | the virtual modem: frames, UDP and pty transports, version / status / config answers, buffer space model, per-mode recording with timestamps, `keyup()`; CLI `python3 -m dvtest.mmdvm_modem --udp IP:PORT \| --pty` |
| `dvtest/mmdvm_rf.py` | RF frames and downlink decoders for D-STAR, DMR and YSF (valid FEC: Golay, BPTC (196,96), RS (12,9), slot type, CRC-16, FICH, CSD, VCH) |
| `dvtest/mmdvm_rf_p25.py`, `dvtest/mmdvm_rf_nxdn.py` | the same for P25 (HDU, LDU1 / LDU2 with link control, IMBE FEC, TDU) and NXDN (LICH, SACCH, FACCH1, AMBE+2, convolutional code) |
| `cases/unit/test_mmdvm_modem.py`, `test_mmdvm_rf_p25.py`, `test_mmdvm_rf_nxdn.py`, `test_mmdvmstack_config.py` | offline tests (`-m unit`): constants against the pinned MMDVMHost (file:line in the comments), a host emulator over UDP and over a pty, key-up timing, recording and decoding |
| `rig/Dockerfile.mmdvmstack`, `rig/mmdvmstack-entry.sh`, `rig/config/mmdvmstack/*.tmpl`, `rig/mmdvmstack/shim/` | the image (separate GPL image like `Dockerfile.nxdngw` and `Dockerfile.dstarsd`), the supervisor entry script, the configuration templates, a libcurl stand-in for DStarGateway |
| `scripts/pre-fetch-mmdvmstack.sh`, pins `MMDVMSTACK_*` in `.env.example` | fetch of the six pinned repositories into `.cache/mmdvmstack/` |
| `rig/compose.yml` profile `mmdvm`, `scripts/run.sh --profile mmdvm` | the service `mmdvm-stack` (.100, `x-limits-small` with 256 tasks), the tester variables, `stop`/`start mmdvm-stack` through the control channel |
| `cases/test_mmdvm_stack.py` | MMDVM1 uplink, MMDVM2 downlink, MMDVM3 link lifecycle (5 + 5 + 1 cases) |

dv-testset stays MIT: the GPL programs run as processes in their own image from fetched sources (`NOTICE`), and nothing of their code or
text is copied. The modem and the RF builders re-express formats; where they had to agree with MMDVMHost to the bit, the comments name
the file and line of the pinned commit and the agreement was checked against the compiled upstream decoders (below).

## The protocol path: UDP, with a pty as the second way

MMDVMHost opens its modem in `CMMDVMHost::createModem` (MMDVM-Host.cpp:1668-1678): `Protocol=uart` (a serial port), `udp`, `i2c` or `null`.
The pinned commit supports `[Modem] Protocol=udp` (`CUDPController`, UDPController.cpp): the **same frames as on the serial line** (0xE0,
length, type, payload), one frame per datagram, MMDVMHost binds `LocalAddress:LocalPort` and sends to `ModemAddress:ModemPort`, and reads
only datagrams whose source is exactly that address and port (UDPController.cpp:55-60). So the rig uses **UDP**:

* the modem lives in the tester (the process that runs pytest) and binds `DVT_MMDVM_MODEM_PORT` (3334) on its own address; MMDVMHost in
  `mmdvm-stack` has `ModemAddress` = the tester, `LocalAddress` = `.100`, `LocalPort` 3335. Compose needs no device, no privilege, no shared
  volume; the two containers only share the rig network. A pty cannot cross containers (the slave path exists in the tester only).
* the modem can be restarted without the host noticing more than a retry (MMDVMHost reopens the modem every 5 s while it does not answer).

The **pty** path (`PtyTransport`, `os.openpty`, raw line, slave path printed) is for a MMDVMHost that runs in the same machine or container as
the modem: `python3 -m dvtest.mmdvm_modem --pty` prints the path for `[Modem] Protocol=uart` / `UARTPort`. It is covered by a unit test that
plays the host on the slave side; it is not used by the rig and was not run against a real MMDVMHost.

## What the modem does

* **Handshake** like a board: GET_VERSION answered with protocol version 2, capability bits for D-STAR, DMR, YSF, P25 and NXDN, CPU type 2, a
  16 byte UDID and the text `dvtest virtual MMDVM 1.0 (no radio)` (Modem.cpp:2031-2119); SET_CONFIG, SET_FREQ and SET_MODE answered with ACK
  (the config is parsed: enabled modes, colour code, simplex/duplex); an unknown command gets a NAK with reason 1.
* **Status polls** (every 250 ms) answered with mode, flags (TX while the host's frames play out, carrier detect while a key-up runs) and the
  buffer space of every mode (D-STAR, DMR slot 1 / 2, YSF, P25, NXDN). The space is a fill model: capacity (10, 10, 10, 8, 4, 8 frames) minus
  the air time of what the host wrote and that has not played out; the host only writes a D-STAR header with space > 4 and any other frame with
  space > 1 (Modem.cpp:1055-1150). The numbers are plausible, not the firmware's.
* **Key-up** `keyup(mode, seconds=3, ...)`: frames in real time with the air spacing of the mode (20 ms D-STAR, 60 ms DMR, 100 ms YSF, 180 ms
  P25 LDU, 80 ms NXDN). The voice words are deterministic (`dvtest/audio/signals` PRNG, seed per mode) or a supplied list; they are turned
  into valid codewords (`canonical_frames`) because MMDVMHost regenerates the FEC of D-STAR, DMR and P25 voice and would otherwise change them.
  `KeyUp.voice` is what a listener must hear, padding included (whole DMR bursts of 3, YSF frames of 5, P25 LDUs of 9, NXDN frames of 4).
* **Recording** of everything the host writes, per mode, with wall and monotonic time: `recording(mode)`, `pairs(mode)`, `wait_frames()`,
  `downlink(mode)` (decoded: D-STAR header fields and voice; DMR slot headers / terminators with LC source and destination and the voice
  words; YSF CSD callsigns, DG-ID and voice; P25 NAC, source, destination, voice; NXDN RAN, source, destination, voice).
* M17: **not supported**. The pinned MMDVMHost has no M17 mode (no M17 source file, no `M17` in `MMDVM-Host.ini`), and g4klx/M17Gateway
  (exists, commit `c72b9892`, 2025-07-09) is a gateway for such a host, so it is not built.

## Cases (`cases/test_mmdvm_stack.py`, marker `rig`, five modes each)

| Id | What | Asserts |
|----|------|---------|
| MMDVM1[mode] | uplink: the modem keys up 3 s on the mode; MMDVMHost and the gateway carry it to the reflector | our own client (D-STAR DExtra, DMR Homebrew, YSF, P25, NXDN) linked to the same module / TG receives the over with the exact frame count (after padding; slack per mode in `DVT_MMDVM_SLACK_<MODE>`, default 0) and the same voice words; with `--profile web` the public state shows N0CALL on air in the module (P25 / NXDN: the module of the TG) |
| MMDVM2[mode] | downlink: our client talks 3 s into the module | the modem recorded the mode's frames and no other mode's, the voice count after padding, and the talker's identity in the header / LC (D-STAR MY `N0TEST`, DMR source 1023008, YSF source `N0TEST`, P25 source 1023008, NXDN source 1002) |
| MMDVM3 | link lifecycle (`--profile web` and the control channel) | each gateway is a client row of its module in the public state; the rows stay for 65 s (keepalives answered); after `stop mmdvm-stack` D-STAR and DMR (which send an unlink at SIGTERM) are gone within 25 s, YSF, P25 and NXDN (which send nothing) within 180 s; after `start` MMDVMHost reconfigures the modem and all rows return |

The modules: D-STAR `H`, DMR `I`, YSF `J` (`DVT_MMDVM_MODULE_*`), P25 and NXDN talk group 31672 (`DVT_MMDVM_P25_TG`, `DVT_MMDVM_NXDN_TG`, the
module of those two is `DVT_P25_MODULE` G / `DVT_NXDN_MODULE` F as for the reflector peers). Identities: the radio is N0CALL (DMR 1023007,
NXDN 1001), our talker N0TEST (DMR 1023008, NXDN 1002); the hotspot logs in as DMR id 1023007. All synthetic.

MMDVMHost keeps one mode for `RF_MODE_HANG` (10 s) after RF activity and `NET_MODE_HANG` (3 s) after network activity and refuses data of other
modes meanwhile (MMDVM-Host.cpp:860-980: "modem data received when in mode N"): the cases wait 12 s after each over, which makes the whole file
long (about 6 to 8 minutes without MMDVM3, about 12 with it because of the 180 s window).

## Running

```
scripts/pre-fetch-mmdvmstack.sh                         # once: the only internet use (six repositories, pinned)
# run.sh builds dvt-mmdvmstack:local on first use (compose build inside the run scope, like dvt-direwolf and dvt-aprsc); the owner sees the size
# estimate below first. To build it ahead by hand, inside the build scope and never while the rig runs (header of rig/Dockerfile.mmdvmstack):
scripts/contained.sh --name build -- docker build -f rig/Dockerfile.mmdvmstack --build-arg MMDVMSTACK_BASE_IMAGE=$MMDVMSTACK_BASE_IMAGE \
    --build-arg MAKE_JOBS=$DVT_BUILD_JOBS --build-context mmdvmhost=.cache/mmdvmstack/MMDVMHost --build-context dmrgw=.cache/mmdvmstack/DMRGateway \
    --build-context ysfclients=.cache/mmdvmstack/YSFClients --build-context p25clients=.cache/mmdvmstack/P25Clients \
    --build-context nxdnclients=.cache/mmdvmstack/NXDNClients --build-context dstargw=.cache/mmdvmstack/DStarGateway -t dvt-mmdvmstack:local rig
# the run (DV-Router native launch with the web profile, so the public state is read):
XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> \
    scripts/run.sh -m rig --capture --profile web,mmdvm -- cases/test_mmdvm_stack.py
```

With DV-Router (native launch) `scripts/run.sh` adds `rig/config/dv-router/mmdvm-edges.toml` by itself: the template has the NXDN and P25 edges on
`enable = "auto"` without a served module, so they are not spawned and NXDNGateway / P25Gateway cannot link (first run). The snippet serves TG 31672 as
NXDN module `F` (UDP 41400) and P25 module `G` (UDP 41000), the modules and talk groups the cases use; it has the same values as `soak-edges.toml`
(not added twice when that is listed). To name it yourself:
`DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/mmdvm-edges.toml`.
A mode whose service the reflector does not offer is left out the same way everywhere: the fixture `offered` links our own client of each mode once;
MMDVM1 / MMDVM2 skip the modes that do not answer, and MMDVM3 (and the wait for the links) checks only the offered modes.

Without `--profile web` MMDVM1 / MMDVM2 still run (frame level, a fixed 40 s wait for the links) and MMDVM3 skips. Offline: `python3 -m pytest -m unit
cases/unit/test_mmdvm_modem.py cases/unit/test_mmdvm_rf_p25.py cases/unit/test_mmdvm_rf_nxdn.py cases/unit/test_mmdvmstack_config.py`. The modem alone
against any MMDVMHost: `python3 -m dvtest.mmdvm_modem --udp 0.0.0.0:3334 --keyup dmr --seconds 3` (waits for the host, keys up once).

## Pins and size

| Program | Repository @ commit (date) |
|---------|-----------------------------|
| MMDVMHost | https://github.com/g4klx/MMDVMHost @ `deb5efe6112863854f7fe7d5f94792d532feea35` (2026-11-05) |
| DMRGateway | https://github.com/g4klx/DMRGateway @ `054575693562ec65104e665f935b532226d50a9f` (2026-11-01) |
| YSFGateway | https://github.com/g4klx/YSFClients @ `6e8fe8f71301bfc718885348c32b552dd19ca3e7` (2026-10-31) |
| P25Gateway | https://github.com/g4klx/P25Clients @ `03e176b9e0a555d5cfce13ed7b2e4eace5b73901` (2026-10-18) |
| NXDNGateway | https://github.com/g4klx/NXDNClients @ `b4b041d2cce9e4e359963d1b8d36911e9c5052eb` (2026-10-25) |
| DStarGateway | https://github.com/F4FXL/DStarGateway @ `7b75de605dca542a2953dd74c16f0eac96a8944e` (2026-10-28) |

Image size, ESTIMATED (nothing built): about **132 MB** (+/- 8), against `dvt-nxdngw:local` 128 MB and `dvt-dstarsd:local` 116 MB: the base
`debian:bookworm-slim` as in those two, libmosquitto1 with libssl3 about 6 MB, the mosquitto broker about 3 MB (P25Gateway and NXDNGateway exit
without a broker), six stripped binaries about 6.4 MB (MMDVMHost 2.0, DStarGateway 2.0, DMRGateway 0.8, YSFGateway 0.65, NXDNGateway 0.5,
P25Gateway 0.4), entry script, templates and a libcurl stand-in for DStarGateway about 0.1 MB (the real libcurl and its dependencies would add about 15
MB; every host file download is switched off). The C++ runtime is linked dynamically (the base has it). Build stage: g++, make, libmosquitto-dev,
nlohmann-json3-dev, libboost-dev, about 200 MB of downloads, 0.8 to 1 GB of layers, 8 to 12 minutes at `-j4`.

## Cross-checks done against the compiled upstream code (scratch, nothing of it committed)

Pieces of the pinned MMDVMHost were compiled on the host with a stub `Log.h` (a few seconds each, no image, nothing run in the rig):

* **The real `CModem`** (Modem.cpp, UDPController.cpp, UDPSocket.cpp, timers) was driven against the Python modem over UDP loopback: it read protocol
  version 2 and all five capability bits, accepted SET_FREQ / SET_CONFIG, polled the status 55 times in 14 s, and read every frame of five key-ups
  (D-STAR 22, DMR slot 2 9, YSF 6, P25 5, NXDN 7 frames) per mode with the tags and first bytes the controllers expect. Frames the real `CModem`
  wrote down (D-STAR header, voice and end; DMR header, bursts and terminator; YSF header, voice and terminator) were recorded and decoded
  by `decode_dstar`, `decode_dmr`, `decode_ysf` with the right identity and voice words.
* DMR: `CDMRFullLC::decode` accepts our voice LC header and terminator (type masks, RS (12,9), BPTC) for group and private calls and returns the
  source and destination; `CDMRSlotType` returns colour code and data type; `CAMBEFEC::regenerateDMR` reports 0 errors and leaves 50 random bursts
  unchanged. The Golay (20,8) parity rows of `SLOT_TYPE_PARITY` were read off `CDMRSlotType` output.
* D-STAR: `CCRC::checkCCITT161` accepts the header CRC; `regenerateDStar` leaves 50 random words unchanged (the 24th bit of the B mask was wrong
  at first and found this way).
* YSF: `CYSFFICH::decode`, `CYSFPayload::processHeaderData` (source and destination as sent), `processVDMode2Data` and `processVDMode2Audio` accept
  the header, voice and terminator frames with 0 errors and do not change the payload.
* P25 and NXDN: the builders encode byte-identical to the upstream encoders (`CP25NID`, `CP25Data`, `CP25Audio`, `NXDNLICH`, `NXDNSACCH`,
  `NXDNFACCH1`, `NXDNAudio`) and the upstream decoders return our identities and voice words (see the module docstrings).

The controllers themselves (`CDStarControl::writeModem`, `CDMRSlot::writeModem`, ...) and their network side were **not** run: they were read.
Their acceptance rules are in the builders' defaults (see the next section).

## What MMDVMHost needs from a transmission (and what the defaults are)

D-STAR: header with the repeater flag, RPT1 `N0CALL B`, RPT2 `N0CALL G`, UR `CQCQCQ` (else "wrong repeater" or "non repeater header"), a sync frame
every 21 frames; DMR: colour code 1, group call to TG 6 on slot 2 (DMRGateway maps it to slot 2 TG 9 of the XLX link; `UserControl` private calls to
64001..64026 change the module, 64000 unlinks), burst A sync, B..F numbered 1..5; YSF: a decodable FICH, the DG-ID in the FICH picks the module on the
reflector (the gateway does not rewrite it), V/D mode 2; P25: NAC 0x293, group call, source id in the id lookup (the rig `dmrid.dat`); NXDN: RAN 1,
group call, LICH direction inbound. All of it from `docs/` of the builders and the entry script header.

## First run (`out/20261109T061354Z`, DV-Router `wip-caebc57`): 5 passed, 2 failed, 4 skipped, both failures were the harness

* **MMDVM1 dstar, "voice words changed (first differing frame 82)".** 150 words sent and heard; word 82 (seq 19 of the fourth superframe, not the
  last frame: the capture has 151 voice packets, 150 plus the end packet `seq|0x40` with the `55 55 55 55 c8 7a` pattern) was the null AMBE word
  `9e8d3288261a3f61e8`. The same bytes are in the capture between DStarGateway and the reflector and again from the reflector to our client (in both
  streams the other 149 words equal the modem's), so the change happened in the client stack, before the reflector. Cause:
  DStarGateway blanks a voice word that looks like a DTMF key (`CRepeaterHandler::processRepeater`, Common/RepeaterHandler.cpp:752-757, with
  `CDTMF::decode` and the masks of DStarBase/DTMF.h:49-68, on by default). Eight fixed bits of the nine bytes select it and every value of the four
  symbol bits is one of the 16 keys; the valid form of the PRNG word 82 (`dae8bcabee7f58741d`) is the key "2". Probability for a random word 1/256, for a 150 word
  over 44 %. So not a DV-Router change and not an off-by-one. Fixed in the generator: `default_frames("dstar", ...)` replaces such words
  (`rf.dstar_dtmf_like`, deterministic, the others stay as they were).
* **MMDVM3 "gateways not linked: ['nxdn', 'p25']" and the p25 / nxdn SKIPs.** The SUT's rendered `dv-router.toml` of the run had
  `[edges.nxdn] enable = "auto"` and `[edges.p25] enable = "auto"` and nothing that serves a module (the run used `web.toml:admin.toml:totp-required.toml`), so the
  edges were not spawned and both our own client link and the gateways got no answer. Fixed by `mmdvm-edges.toml` (above) and by the `offered` fixture.

## UNVERIFIED

1. **Only one run so far.** (First run: the image built and the stack ran, see above.) Originally: the image was never built: the six programs compile with the bookworm compiler (gcc 12) with `-std=c++0x` and
   nlohmann-json 3.11 only as far as the sources were read; DStarGateway builds with `-Werror` removed and the libcurl stand-in via the
   environment; the sub-make of DStarGateway is parallel. The first build may need a fix.
2. **DV-Router's client services for P25 (UDP 41000) and NXDN (UDP 41400)**: they need `mmdvm-edges.toml` (above; the template alone does not serve a
   module). Whether the gateways link with it is the next run to tell. If our own client cannot link, MMDVM1 / MMDVM2 of those modes skip with the reason and
   MMDVM3 leaves the mode out; if only the gateway cannot, MMDVM3 fails for it.
3. **XLX conventions for DMRGateway**: login with `passw0rd`, the startup link burst (3 headers and 2 terminators, private call to 4001+) and the slot 2
   TG 9 voice are taken from the sources of DMRGateway and `docs/protocols/mmdvm.md`; the module in `XLXHosts.txt` (third column) is a reading of the
   source. The room name `I` for DMR, `H` for D-STAR and `J` for YSF must exist in the template.
4. **The public state shape**: `clients.items[]` rows are matched by `room` / `module` and a protocol word in `protocol` / `edge` / `kind`;
   `rooms[].onair.callsign(_base)` as in `cases/test_dp15_streams.py`. The first run writes the first rows to `out/<run>/mmdvm-stack.txt`; adjust
   `PROTO_WORDS` and `row_matches` if the names differ. P25 / NXDN rows may sit in another room than `DVT_P25_MODULE` / `DVT_NXDN_MODULE`.
5. **Frame counts are asserted exact** (slack 0). Candidates for a measured difference: the D-STAR look-back of two frames and the silence insertion
   in `CDStarControl`, the last LDU / RF frame padding of P25 and NXDN, a first YSF frame used for the header, `AckReply` frames sent back to the modem
   after a D-STAR over (they are D-STAR frames in the recording of MMDVM2 only if the over came from the modem, MMDVM1).
6. **Voice words are asserted equal** in MMDVM1 (the words change only where MMDVMHost regenerates the FEC; the builders make fixed points of it). P25:
   MMDVMHost forwards each LDU one frame late, the TDU flushes the last one; the LDU2 message indicator, algorithm and key id are re-encoded by the host.
7. **Unlink at stop**: YSFGateway, P25Gateway and NXDNGateway send nothing at exit (read in the pinned sources); the 180 s window assumes the reflector
   removes a silent client by then (DV-Router: not known; xlxd drops YSF after 30 s, DMR after 100 s).
8. **Mode hang**: the 12 s wait after an over is derived from `RFModeHang=10` / `NetModeHang=3`; the first run may show a shorter wait is enough.
9. The buffer-space numbers and the status flags of the modem are a model; MMDVMHost accepts them (checked with the real `CModem`) but a real board
   differs. No RSSI, no BER feedback, no FM, no POCSAG.
10. `PtyTransport` against a real MMDVMHost (UART open, `termios` flags) was not tried.
11. Not covered at all: DPlus and DCS gateway links (DStarGateway is configured for DExtra only, no internet for the DPlus authentication), YSF V/D
    mode 1 and data modes, DMR slot 1, data calls, GPS, talker alias, slow data text from the modem side, ircDDB, APRS-IS, the remote-command ports.
