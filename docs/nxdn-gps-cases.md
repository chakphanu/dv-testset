<!-- SPDX-License-Identifier: MIT -->
# NXDN GPS and data calls (DP14b): generator, oracles, planned cases

Offline part of DP14b. What exists today is the generator, the parsers and the
oracle plumbing, all covered by unit tests (`cases/unit/test_nxdn_l3.py`,
`cases/unit/test_oracle_nxdn.py`). No decoder has been built or run, no rig
case exists yet, and every Docker step below is UNVERIFIED until it has been
done once.

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
| Kenwood FULL = 38 bytes, SHORT = 17, type byte 0x00 / 0x01 | GW | derived |
| Latitude: fraction 15 bits in bytes 9 (bits 7-1) and 10, hemisphere = byte 9 bit 0 (0 north); degrees*100+minutes 16 bits little endian in bytes 11-12 | GW, DSD agree | derived (both) |
| Longitude: degrees*100+minutes in bytes 15-16 little endian, hemisphere = byte 13 bit 0 (0 east) | GW, DSD agree | derived (both) |
| Fraction unit: 1e-4 minute (0..9999) | DSD divides by 10000; GW cuts it to 1/100 minute for APRS | derived |
| Longitude fraction (15 bits) | GW: bytes 13 (bits 7-1) and 14. DSD: bytes 32 (bits 7-1) and 33 | DISAGREE; DSD place is probably a typo, UNVERIFIED |
| Course (tenths of a degree, 12 bits) | GW: byte 23 is the high 8 bits, byte 24 low nibble the low 4. DSD: byte 23 is the low 8 bits, byte 24 low nibble the high 4 | DISAGREE |
| Speed | GW: 10-bit whole part (byte 25 high nibble = bits 9-6, byte 26 low 6 bits = bits 5-0) and tenths in byte 25 low nibble. DSD: 14-bit tenths, byte 25 low 8 bits, byte 26 low 6 bits high | DISAGREE; unit (knots or km/h) UNVERIFIED |
| Elevation: signed 16 bit, byte 27 low, byte 28 high | DSD only (GW ignores) | derived, one source |
| Time: hour (5 bits) and minute (6 bits) over bytes 5 / 4 / 3 (hour = byte 5 bit 0 and byte 4 high nibble, minute = byte 4 low nibble and byte 3 top two bits); no seconds | DSD only | derived, one source |
| Date: byte 18 = year - 2000 in the top 7 bits, month 4 bits (byte 18 bit 0 and byte 17 top 3 bits), byte 17 low 5 bits = day - 1 | DSD only | derived, one source |
| Bytes 0-2 zero, byte 36 = 0xFC, byte 37 = 0xFF (the 0xFFFC marker dsd-fme looks for after reversing the bytes) | DSD | derived; marker meaning is open even for its author |
| "No fix" sentinels (fraction 0x7FFF, degrees/minutes 0xFFFF), no course / speed sentinels 1023 and 15 | GW | derived |
| Unmapped bytes (6-8, 19-22, 29-35 except 32-33 in the DSD layout) | none | zero; UNKNOWN |
| Status field | nothing in any source | NOT implemented |
| VERY_SHORT (type 0x02) | GW reads one longitude byte twice (line 211) | NOT generated, parse refuses it |
| Icom: opcode 0x06 then NMEA 0183 `$GPRMC` with XOR checksum | GW, DSD, public NMEA rule | builder only, GW's Icom branch (lines 88-153) not re-read |

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

## Docker recipe skeleton (UNVERIFIED, from the research note)

Nothing here has been built.

NXDNGateway as the GPSHandler oracle (no radio, no sound; UDP and MQTT only):

1. Image `debian:12-slim`, packages build-essential, git, ca-certificates,
   libmosquitto-dev, mosquitto, mosquitto-clients. Pins come from `.env.example`
   (`NXDNCLIENTS_REPO` / ref `b4b041d`; add the variables when the image is
   written; nothing is hardcoded in the Dockerfile).
2. `git clone` NXDNClients, checkout the pin, `make -C NXDNGateway`
   (links pthread and mosquitto).
3. Gateway ini: `[General]` repeater protocol Icom, repeater address = the
   test script, repeater and local ports; `[APRS]` Enable=1; `[MQTT]` the local
   broker. The gateway also needs the id-to-callsign file (`NXDN.csv`) with the
   source id of the test (`processHeader` looks the callsign up) and a hosts
   file. The key names are in the repo's `NXDNGateway.ini`.
4. Start `mosquitto`, then the gateway; subscribe with
   `mosquitto_sub -t aprs-gateway/aprs` into a file.
5. The test plays the repeater over UDP: answer or start with the connect
   request (`oracle_nxdn.icom_connect_request`, the gateway answers it with the
   byte at index 5 changed from 0x61 to 0x62), then send
   `oracle_nxdn.icom_packets(packets)` for each case, then read the file.
   The gateway feeds GPSHandler only from this local (repeater) side, not from
   frames that arrive from a reflector.
6. Cut the capture per case and call `oracle_nxdn.build_record` with
   `gateway_text=`.

dsd-fme as the second decoder:

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

- NXDNGateway builds at the pin and prints one APRS line per table row with the
  values `predict()["gateway"]` gives (`prediction_ok` true for GW); the Icom
  form too.
- The ICOM framing bytes at indexes 37-39 (23 02 18) are accepted as written.
- dsd-fme builds, and for a replayable input prints the line the regex in
  `parse_dsdfme_log` expects (the format string was read in source, not seen in
  a run); the degree glyph and the line wrapping on a real terminal.
- The ini keys, the `NXDN.csv` entry and the broker settings of the gateway.
- That a CRC-checked call from this generator is accepted by dsd-fme (the
  independent CRC check), once an RF input exists.
