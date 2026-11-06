<!-- SPDX-License-Identifier: MIT -->
# NXDN GPS and data calls (DP14b): generator, oracles, planned cases

Offline part of DP14b. What exists today is the generator, the parsers and the
oracle plumbing, all covered by unit tests (`cases/unit/test_nxdn_l3.py`,
`cases/unit/test_oracle_nxdn.py`). No decoder has been built or run, no rig
case exists yet. HWFREE1 item 1, stage 1 is done: the NXDNGateway decoder was
built as a Docker image (`rig/Dockerfile.nxdngw`, tag `dvt-nxdngw:local`, 128 MB
on disk) and run over the whole table; the results are in "Gateway oracle
result" below and in `vectors/nxdn_gps/gateway-b4b041d.jsonl`. Everything about
dsd-fme (build, input, output format) is still UNVERIFIED.

Licence rule: dv-testset is MIT. NXDNClients (NXDNGateway) and MMDVMHost are
GPL-2.0-or-later and dsd-fme is ISC-style with a few GPL-2.0-only files; they
are test references run as separate processes in their own images. Nothing of
their code or text is in this repository: they are cited by file, function and
line number, and the byte layouts below are facts about a format. The NXDN
Forum specs are copyright: the fields are described here in our own words.

## Files

| File | Contents |
|---|---|
| `dvtest/proto/nxdn_l3.py` | message CRC-32, layer 3 DCALL header / user data / TX_REL octets, packet information, 33-byte UDCH frame, NXDND flags for data, packetiser (`data_call_packets`) and parser (`parse_call`), Kenwood FULL / SHORT builders and parsers with two selectable layouts, Icom NMEA RMC form, the table of known positions |
| `dvtest/oracle_nxdn.py` | parsers for the NXDNGateway APRS line and the dsd-fme report line, reduction to common fields, `build_record` (JSON disagreement record), ICOM packet framing for the gateway's local side, `run_command` subprocess wrapper, command line `python3 -m dvtest.oracle_nxdn` |
| `cases/unit/test_nxdn_l3.py`, `cases/unit/test_oracle_nxdn.py` | round trips, boundaries, synthetic decoder outputs |
| `rig/Dockerfile.nxdngw`, `rig/nxdngw-entry.sh`, `scripts/pre-fetch-nxdngw.sh` | the gateway oracle image (multi-stage, runtime only), its entry script (broker, ini, ICOM sender, capture) and the source fetch at `NXDNGW_REPO` / `NXDNGW_COMMIT` of `.env.example` |
| `vectors/nxdn_gps/gateway-b4b041d.jsonl` | one record per case of the gateway run (87 lines), written by `python3 -m dvtest.oracle_nxdn gateway --out FILE` |

## What is read from a spec and what is derived

Four-level clause numbers are written `6.4.1/3` (sub-clause 3 of 6.4.1), as in
`protocols/nxdn.md`. Source keys: SPEC = NXDN Forum TS 1-A / 1-B v1.3 (public mirror, see
`protocols/nxdn.md` Sources; v2.0 not read), GW = NXDNGateway `GPSHandler.cpp`
at NXDNClients `b4b041d` (Kenwood branch, about lines 170-252), DSD = dsd-fme
`nxdn_gps_report` (`src/dsd_gps.c` about 724-795) and its caller in
`src/nxdn_element.c` (about 2895-2975), crc in `src/nxdn_deperm.c` (about
273-285). "Research" is item 1 of the DP14b research note.

| Item | Source | Status |
|---|---|---|
| UDCH = 22 layer 3 octets; DCALL header 0x09 octets 0-10 (CC option, call type + data call option, src, dst, cipher/key, packet information); optional IV not generated | SPEC 6.4.1/3, Figure 6.4-3 | read |
| DCALL user data 0x0B: octet 1 = packet frame number (high nibble) and block number (low nibble), 20 user octets | SPEC 6.4.1/4 | read |
| Packet information bits (delivery, selective retry, block count - 1, pad count, start / circular fragment, 9-bit TX fragment count) | SPEC 6.5.19 | read |
| Both nibbles equal and counting down to 0 in a normal call; retry flag 1 with block numbers kept and frame numbers recounted over the retried blocks only | SPEC 6.5.20-21, TS 1-B 5.4 Figure 5.4-1 | read (also a unit test) |
| Message CRC-32 over the whole user data, preset ones, MSB first, polynomial 0x04C11DB7, no reflection, no final xor, in the last four octets of the user data | SPEC 9.3 (polynomial and preset); no-xor/no-reflection from DSD `nxdn_message_crc32` | read plus derived (checked against the CRC-32/MPEG-2 catalogue check value) |
| Pad octets follow the CRC; total bytes = blocks * 20 - pad | DSD (about 2751, 2915), TS 1-B 5.2 | derived |
| 33-byte reflector frame = LICH, RAN byte, 22 octets, 9 unused zero octets; flags 0x03 + 0x04 on the header and 0x03 + 0x08 on TX_REL | research 1.1 / 1.2 (MMDVMHost NXDNControl / NXDNUDCH, NXDNNetwork.cpp) | derived from code reads |
| LICH value of a data frame 0x90 (RDCH, UDCH, steal option 0) | `dvtest/proto/nxdn.py`; research 1.2 lists 0x90 / 0x92 / 0x9C / 0x9E | which one a radio or repeater sends: UNVERIFIED |
| F1 / F2 (octet 0 bits 7-6) | SPEC: spare | 0 |
| Kenwood FULL = 38 bytes, SHORT = 17, type byte 0x00 / 0x01 | GW | derived; GW side VERIFIED by the run (a FULL and a SHORT message of this size print a line) |
| Latitude: fraction 15 bits in bytes 9 (bits 7-1) and 10, hemisphere = byte 9 bit 0 (0 north); degrees*100+minutes 16 bits little endian in bytes 11-12 | GW, DSD agree | derived (both); GW side VERIFIED (17 rows, N and S) |
| Longitude: degrees*100+minutes in bytes 15-16 little endian, hemisphere = byte 13 bit 0 (0 east) | GW, DSD agree | derived (both); GW side VERIFIED (17 rows, E and W) |
| Fraction unit: 1e-4 minute (0..9999) | DSD divides by 10000; GW cuts it to 1/100 minute for APRS | derived; GW cut is a truncation: VERIFIED (1345.3780 prints .37, 15112.5516 prints .55) |
| Longitude fraction (15 bits) | GW: bytes 13 (bits 7-1) and 14. DSD: bytes 32 (bits 7-1) and 33 | DISAGREE; GW place VERIFIED (a frame with the fraction only at 32/33 prints `.00`); DSD place is probably a typo, UNVERIFIED |
| Course (tenths of a degree, 12 bits) | GW: byte 23 is the high 8 bits, byte 24 low nibble the low 4. DSD: byte 23 is the low 8 bits, byte 24 low nibble the high 4 | DISAGREE; GW reading VERIFIED (printed = tenths / 10 truncated, 0..359 round trip); the DSD side is open |
| Speed | GW: 10-bit whole part (byte 25 high nibble = bits 9-6, byte 26 low 6 bits = bits 5-0) and tenths in byte 25 low nibble. DSD: 14-bit tenths, byte 25 low 8 bits, byte 26 low 6 bits high | DISAGREE; GW reading VERIFIED (prints the whole part only, never the tenths; 0, 10, 30, 55, 120 round trip); unit (knots or km/h) UNVERIFIED |
| Elevation: signed 16 bit, byte 27 low, byte 28 high | DSD only (GW ignores) | derived, one source |
| Time: hour (5 bits) and minute (6 bits) over bytes 5 / 4 / 3 (hour = byte 5 bit 0 and byte 4 high nibble, minute = byte 4 low nibble and byte 3 top two bits); no seconds | DSD only | derived, one source |
| Date: byte 18 = year - 2000 in the top 7 bits, month 4 bits (byte 18 bit 0 and byte 17 top 3 bits), byte 17 low 5 bits = day - 1 | DSD only | derived, one source |
| Bytes 0-2 zero, byte 36 = 0xFC, byte 37 = 0xFF (the 0xFFFC marker dsd-fme looks for after reversing the bytes) | DSD | derived; marker meaning is open even for its author |
| "No fix" sentinels (fraction 0x7FFF, degrees/minutes 0xFFFF), no course / speed sentinels 1023 and 15 | GW | derived; the no-fix FULL and SHORT print no line at all: VERIFIED; the course / speed sentinels were not run |
| Unmapped bytes (6-8, 19-22, 29-35 except 32-33 in the DSD layout) | none | zero; UNKNOWN |
| Status field | nothing in any source | NOT implemented |
| VERY_SHORT (type 0x02) | GW reads one longitude byte twice (line 211) | NOT generated, parse refuses it |
| Icom: opcode 0x06 then NMEA 0183 `$GPRMC` with XOR checksum | GW, DSD, public NMEA rule | GW side VERIFIED (17 rows print a line, so the checksum and the layout are accepted); the GW Icom branch rounds the minute fraction where the Kenwood branch truncates, see below; the dsd-fme side is not run |

Two layouts exist in the code, `GATEWAY` (all three disputed fields where GW
reads them) and `DSDFME` (where dsd-fme reads them), plus `LON_BOTH`
(longitude fraction written at both places, course and speed as GW). A frame
in one layout read by the other decoder is expected to print different
course, speed and longitude fraction; `oracle_nxdn.predict()` states exactly
which values, and the record compares that prediction to the real output
(`prediction_ok`), so a wrong reading of a source shows up separately from a
real disagreement. Neither layout is "the truth": that needs a real Kenwood
capture (research section 4 leads).

## Disagreement record

`python3 -m dvtest.oracle_nxdn --case NAME --user-data HEX --layout gateway
--gateway-out gw.txt --dsdfme-log dsd.log --out rec.json` writes one JSON
object (schema `dvt-nxdn-gps-compare/1`):

- `verdict`: agree, disagree, no-output, no-output-gateway, no-output-dsdfme
  (exit status 0 / 1 / 2);
- `fields`: for latitude, latitude_fraction, longitude, longitude_fraction,
  course, speed: both decoders' reduced values, `agree`, the value of the
  encoded input, and which decoder matches the input;
- `gateway` / `dsdfme`: the exact printed line, the parsed numbers (dsd-fme
  numbers as the exact strings it printed), the reduced values and the
  prediction;
- `input`: user data hex, the same with CRC, optional packets hex.

Reduction rules (resolution of the weaker decoder wins): fraction of a minute
in 1/100 minute truncated, course and speed as whole numbers truncated, signs
from the hemisphere letters (gateway) or the sign of the printed number
(dsd-fme). The tracker entry takes `disputed_fields` and the two exact
output lines.

## Planned rig cases (names reserved, not written)

File `cases/test_nxdn_gps.py` (rig, profile `nxdn`; the DV-Router edge on the
reflector side); the table of positions is `KNOWN_POSITIONS`.

| Case | What |
|---|---|
| NXG1 | data call relay: header, three blocks, TX_REL sent through the NXDN reflector client; the other linked peer receives the same five NXDND packets byte for byte, no voice stream opens (P-NXD-24 style) |
| NXG2 | NXDNGateway oracle: the same five frames sent as ICOM packets to the gateway's local side; `aprs-gateway/aprs` line per table row, compared to the table (GW reading; Icom NMEA form as a second input) |
| NXG3 | dsd-fme oracle (blocked on the RF encoder, see open questions): same table, dsd-fme report line; record per row with `oracle_nxdn.build_record` |
| NXG4 | CRC: a call with one flipped CRC bit and one with a lost middle block; what DV-Router does is decided by its data design document (refuse expected), the generator already labels both (`corrupt_crc`, dropped packet) |
| NXG5 | negatives: selective retry (`retry_blocks`), circular / non-first fragment (`header_opts`), cipher type not 0, 17 blocks (over 316 bytes), second header, missing TX_REL |
| NXG6 | SHORT and no-fix reports, VERY_SHORT not accepted |
| NXG7 | pacing: one frame per 80 ms is the voice rate; the data rate is UNVERIFIED, measure with a capture of NXG2 |

## Gateway oracle: how it is built and run (VERIFIED)

Built and run once on 2026-11-06. Differences from the research recipe:

- Pin: `NXDNGW_REPO` / `NXDNGW_COMMIT` = NXDNClients `b4b041d2cce9e4e359963d1b8d36911e9c5052eb`
  (`.env.example`, fetched by `scripts/pre-fetch-nxdngw.sh` into `.cache/nxdngw`).
  It is NOT `NXDNCLIENTS_COMMIT` (`3bc2a14`, the last commit with
  NXDNReflector): that one has no MQTT output and a different GPSHandler, so the
  existing cache cannot be reused. `GPSHandler.cpp` at `b4b041d` is the file the
  table above was read from.
- Build: Debian bookworm-slim, `g++ make libc6-dev libmosquitto-dev
  nlohmann-json3-dev` (the last one is needed by `Log.h`; the recipe missed it),
  plain `make NXDNGateway` without gpsd, stripped. Final stage: bookworm-slim +
  `mosquitto` + `mosquitto-clients` + `libmosquitto1`, the binary, the entry script,
  non-root user. Image size: 128 MB on disk (32 MB content). The apt packages are
  the only download of the build besides the source fetch.
- The broker is in the same image: the gateway exits at start without an MQTT
  connection. `rig/nxdngw-entry.sh` starts `mosquitto` on 127.0.0.1, then
  `mosquitto_sub` on `aprs-gateway/aprs` (the APRS line goes to that fixed topic,
  not under the `Name` prefix) and on the marker topic `dvt/case`, then the
  gateway, then feeds the packets. No MQTT port leaves the container.
- Gateway ini: written by the entry script into the container's tmpfs: Icom
  protocol, repeater address 127.0.0.1, `LocalPort` 14020 / `RptPort` 14021,
  `Daemon=0` (it forks and closes stdout otherwise), `[APRS] Enable=1`, `[Voice]
  Enabled=0`, `Latitude`/`Longitude` 0.0 (no beacon of its own), `Time=0` for the id
  file, parrot / NXDN2DMR ports 0, hosts files missing (the gateway logs an error
  and carries on). The id file has one synthetic line, `1001,N0CALL`; the printed
  source is therefore `N0CALL-N` (callsign from the id file, then `-` and the
  first letter of `[APRS] Suffix`), the digipeater part is the ini `Callsign`
  (`N0CALL`).
- Sender: the same container, `cat FILE > /dev/udp/127.0.0.1/14020` once per
  packet (source address 127.0.0.1 is what the gateway's address-only check
  wants; the connect request is not needed). Each case is the five (FULL) or
  four (SHORT) ICOM packets that `oracle_nxdn.icom_packets()` builds for
  `data_call_packets(1001, 31672, user)`, 50 ms apart, 0.3 s between cases.
- Container: `docker run --rm --network none`, `dvtest.limits.docker_run_args()`
  (1 GB, 2 CPUs, 512 pids), `--read-only`, 16 MB tmpfs, `--cap-drop ALL`,
  `no-new-privileges`, 180 s timeout (the container is removed by name on a
  timeout). A full run (87 cases) takes about 52 s.

  `python3 -m dvtest.oracle_nxdn gateway --out vectors/nxdn_gps/gateway-b4b041d.jsonl`

Trap found while building: writing the packet with `printf ... > /dev/udp/...`
sends two datagrams when the packet holds a 0x0a byte (the shell flushes at the
newline), the gateway drops both halves (length not 102) and silently loses
blocks. The first run lost the blocks of every row with a 0x0a byte (south-east,
lon-tiny-west, which carry 0x0a in the degrees/minutes) and printed garbage or
nothing for them; the entry script now writes the packet to a file and sends it
with one `cat`, and checks the file size.

## Gateway oracle result (b4b041d)

87 cases: FULL in the three layouts (`gateway`, `dsdfme`, `lon-both`) x the 17
rows of `KNOWN_POSITIONS`, SHORT x 17, the two no-fix messages, and the Icom
NMEA form x 17. The gateway printed exactly one line per case, except for the
two no-fix messages, which print nothing. Every printed line is
`N0CALL-N>APDPRS,NXDN*,qAR,N0CALL:!` + the position below + ` Kenwood NEXEDGE via
MMDVM` (Icom form: ` Icom IDAS via MMDVM`), CR LF at the end. The position is
`LATddmm.ff` N/S `/` `LONdddmm.ff` E/W `r` and, when course and speed are present,
`CCC/SSS`.

| Row | FULL gateway layout and lon-both (identical) | FULL dsdfme layout | SHORT | Icom NMEA |
|---|---|---|---|---|
| `base-c0-s0` | `1345.37N/10030.10Er000/000` | `1345.37N/10030.00Er000/000` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er000/000` |
| `base-c0-s55` | `1345.37N/10030.10Er000/055` | `1345.37N/10030.00Er000/130` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er000/055` |
| `base-c0-s120` | `1345.37N/10030.10Er000/120` | `1345.37N/10030.00Er000/708` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er000/120` |
| `base-c90-s0` | `1345.37N/10030.10Er090/000` | `1345.37N/10030.00Er211/000` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er090/000` |
| `base-c90-s55` | `1345.37N/10030.10Er090/055` | `1345.37N/10030.00Er211/130` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er090/055` |
| `base-c90-s120` | `1345.37N/10030.10Er090/120` | `1345.37N/10030.00Er211/708` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er090/120` |
| `base-c180-s0` | `1345.37N/10030.10Er180/000` | `1345.37N/10030.00Er013/000` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er180/000` |
| `base-c180-s55` | `1345.37N/10030.10Er180/055` | `1345.37N/10030.00Er013/130` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er180/055` |
| `base-c180-s120` | `1345.37N/10030.10Er180/120` | `1345.37N/10030.00Er013/708` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er180/120` |
| `base-c359-s0` | `1345.37N/10030.10Er359/000` | `1345.37N/10030.00Er011/000` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er359/000` |
| `base-c359-s55` | `1345.37N/10030.10Er359/055` | `1345.37N/10030.00Er011/130` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er359/055` |
| `base-c359-s120` | `1345.37N/10030.10Er359/120` | `1345.37N/10030.00Er011/708` | `1345.37N/10030.10Er` | `1345.38N/10030.11Er359/120` |
| `south-east` | `3352.12S/15112.55Er045/010` | `3352.12S/15112.00Er310/384` | `3352.12S/15112.55Er` | `3352.13S/15112.56Er045/010` |
| `north-west` | `4042.76N/07400.36Wr270/030` | `4042.76N/07400.00Wr225/193` | `4042.76N/07400.36Wr` | `4042.77N/07400.36Wr270/030` |
| `origin` | `0000.00N/00000.00Er000/000` | `0000.00N/00000.00Er000/000` | `0000.00N/00000.00Er` | `0000.00N/00000.00Er000/000` |
| `pole-dateline` | `8959.99N/17959.99Wr359/000` | `8959.99N/17959.00Wr025/000` | `8959.99N/17959.99Wr` | `8959.99N/17959.99Wr359/000` |
| `lon-tiny-west` | `5130.00N/00000.00Wr012/001` | `5130.00N/00000.00Wr196/000` | `5130.00N/00000.00Wr` | `5130.00N/00000.01Wr012/001` |

What this shows, gateway side only (the dsd-fme side stays open):

- VERIFIED: with the `gateway` and `lon-both` layouts every printed value equals
  the encoded one (latitude, longitude, hemispheres, minute fraction truncated
  to 1/100, course in whole degrees, speed as the whole part), in all 17 rows,
  so `predict()["gateway"]` is right: `prediction_ok` is true for all 68 Kenwood
  rows with a fix. SHORT prints the position and `r` with no course / speed.
  The no-fix FULL and SHORT print nothing.
- The three disputed fields, read by the gateway from a frame written in the
  `dsdfme` layout (the bytes where dsd-fme reads them): the longitude fraction
  is read from bytes 13/14 where that layout writes zero, so every row prints
  `.00` (`10030.10` becomes `10030.00`; `15112.55` becomes `15112.00`). Course:
  `base-c90` (900 tenths, byte 23 = 0x84, byte 24 = 0x03) prints 211; 0 -> 0,
  180 -> 13, 359 -> 11, south-east 45 -> 310, north-west 270 -> 225,
  pole-dateline 359.9 -> 25, lon-tiny-west 12.3 -> 196. Speed: 55 prints 130,
  120 prints 708, 30.5 prints 193, 10 prints 384, 1.0 prints 0 (byte 25 = 0x0A,
  a tenths nibble the gateway ignores). `origin` (all zero) is the only row on
  which the two layouts agree. These are the exact disagreements the
  gateway reading predicts: the gateway side of the research claim holds; which
  layout is the real Kenwood one is still open.
- One bug found in the repository, fixed: `parse_kenwood_fields` folded a tenths
  nibble over 9 into the whole part of the speed, so `predict()["gateway"]` said 1
  where the gateway prints 0 (`dsdfme.lon-tiny-west`); the gateway reading now
  keeps the whole part (`raw["speed_whole"]`), unit test
  `test_gateway_reading_of_speed_nibble_over_9`.
- Icom NMEA form: all 17 rows print a line with course and speed as given. The
  gateway's Icom branch prints the minute with `%07.2lf` (rounds half to the
  nearest hundredth), the Kenwood branch cuts off: 15 of 17 rows differ from the
  Kenwood line by one hundredth in the latitude, the longitude or both
  (`1345.38N/10030.11E` against `1345.37N/10030.10E`); `origin` and
  `pole-dateline` do not, `north-west` differs in the latitude only and
  `lon-tiny-west` in the longitude only (`00000.01W`, from -0.0001 degrees =
  0.006 minute). `oracle_nxdn.icom_record` records these as `mismatches` against
  the truncated value; the rule is a property of the gateway, not a defect of the
  generator.
- The ICOM framing bytes at indexes 37-39 are accepted as written (23 02 18): the
  gateway's read path does not look at them (only at "ICOM", length 102 and the
  source address), and all 87 cases were accepted. Not exercised: the connect
  request answer (byte 5 0x61 to 0x62), which the oracle does not need.

## dsd-fme as the second decoder (UNVERIFIED, not part of stage 1)

Nothing here has been built.

1. Build per the repository's install notes: cmake, build-essential,
   libpulse-dev, libsndfile1-dev, libfftw3-dev, liblapack-dev, libncurses-dev,
   libcodec2-dev, and lwvmobile/mbelib built first.
2. Run `dsd-fme -fi -i FILE.wav -N 2> dsd.log` (NXDN48; `-fn` for NXDN96) or
   replay a symbol capture; take the last GPS report line of the log.
3. Input is the open problem (below): it needs a 4FSK signal or symbol
   capture, not our UDP frames.

## Open questions

1. Which layout is right for the longitude fraction, course and speed: only a
   real Kenwood capture with independently known values decides it. Ask the
   dsd-fme author and DSD+ users (research section 4); required: raw DCALL
   frames or a dsd-fme `-N` log with payload hex, model, firmware, and known
   position, speed and course.
2. Speed unit (knots or km/h) and whether 1023 / 15 and 0x3FFF are real "no
   value" markers; the table in the unit test uses 0 / 55 / 120 as wire units.
3. How to feed dsd-fme: a modulator (4FSK to 48 kHz audio or a symbol .bin)
   needs NXDN framing (FSW, LICH, UDCH convolution, interleave, scrambler) that
   nothing in this repository produces; options are the MMDVMHost
   network-to-modem path with a fake `udp` modem, or writing the encoder from
   the spec. The author flags wav input for NXDN as fragile. Cost 3 to 5 days,
   high risk; NXG3 stays deferred until then. The gateway oracle alone cannot
   settle the three disputed fields.
4. Seconds, status and the unmapped bytes of FULL: no source. Whether the
   bytes 0-2 (zero) and the 0xFFFC marker are fixed for every Kenwood radio.
5. LICH values and RAN used by real radios for data frames; data pacing.
6. TS 1-A / 1-B v2.0 (the version DP14b cites) was not read; the cited fields
   are expected to be identical to v1.3 (research section 0).
7. Pad octet value (zero here; dsd-fme does not look at it).
8. How DV-Router should treat a bad CRC, a retry, fragments and encrypted data
   (generator labels all of them; the decision belongs to its data design).

## What the Docker builds have to verify

NXDNGateway (stage 1, done):

- VERIFIED: it builds at the pin `b4b041d` and prints one APRS line per table row
  with the values `predict()["gateway"]` gives (`prediction_ok` true, 68 of 68
  Kenwood rows with a fix); the Icom form prints a line per row too (with the
  rounding difference above).
- VERIFIED: the ICOM framing bytes at indexes 37-39 (23 02 18) are accepted as written.
- VERIFIED: the ini keys, the id file entry and the broker setting of the gateway
  (section "Gateway oracle: how it is built and run").
- UNVERIFIED: the gateway's behaviour with a bad CRC, a lost block or a retry
  (it checks no CRC; not run), the course / speed sentinels (1023 / 15), VERY_SHORT.

dsd-fme (stage 2, open):

- dsd-fme builds, and for a replayable input prints the line the regex in
  `parse_dsdfme_log` expects (the format string was read in source, not seen in
  a run); the degree glyph and the line wrapping on a real terminal.
- That a CRC-checked call from this generator is accepted by dsd-fme (the
  independent CRC check), once an RF input exists.
