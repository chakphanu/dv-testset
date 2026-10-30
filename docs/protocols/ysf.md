<!-- SPDX-License-Identifier: MIT -->
# YSF reflector network: obligations of the reflector side

What a YSF reflector owes a YSF client on UDP 42000. The clients are
YSFGateway (behind MMDVMHost on a hotspot or repeater; MMDVMHost is the
final consumer of what the reflector sends), DGIdGateway, DroidStar
(direct), and the MMDVM_CM cross-mode bridges (YSF2DMR, YSF2NXDN, YSF2P25).
Each item is numbered `P-YSF-NN`; the cases are listed at the end
("Testlist rows").

There is no published specification of the network protocol: `YSFP`,
`YSFU`, `YSFS` and `YSFD` were defined by G4KLX's YSFReflector and
YSFGateway, and the only written wire table is a community wiki page
[YCS]. Yaesu's air-interface standard [Y1] defines the 120-byte frame a
`YSFD` carries (frame sync, FICH, callsign data, the data types). So the
Level column is mostly `convention (impl)`, naming who agrees, or
`disputed`; MUST appears only for frame structure taken from [Y1].

Every row says where the obligation comes from, in separate columns: the
document (with version and section), what the clients send, expect and
tolerate, what the other reflectors do (project at a commit, behaviour in
our own words, no code copied), what xlxd-ref does, and the
recommendation for DV-Router (spec + client consensus first, other
reflectors second, xlxd last). The xlxd column is measured on the rig
(xlxd-ref and xlxd-sut, 2026-10-30, run `out/20261030T084520Z-i5`; all 34 cases
passed with sut == ref, dvcheck 0 errors); URFd and the other reflectors
are from reading their code.

xlxd loop guard: when one MY callsign opens three short streams (under 50
frames) in a row, xlxd blocks it (see docs/protocols/dplus.md). The cases
talk 65 frames, give every client of a probe its own callsign (STR001..),
and send at most two malformed polls per run.

## Sources

Documents:

- **[Y1]** Yaesu Musen, "Amateur Radio Digital Standards", v1.0
  2013-01-15, rev 1.02 2015-07-29, 38 pages: the C4FM/FDMA air interface
  (frame sync 4.2.1, FICH 4.2.1 and Table 4-1, callsign data Table 4-2,
  V/D type 1 4.2.2, V/D type 2 4.2.3, Data FR 4.3, Voice FR 4.4, start /
  keep / stop 4.6, synchronisation 4.7). Official, the only primary
  document found; it predates DG-ID (the same 7-bit FICH field is the
  "SQL code", 127 = break-out code). Mirror:
  https://www.rcqsl.com/manuals/78.pdf
- **[YCS]** YCS wiki, "YSF Protocol for MMDVM-HOTSPOTS" (server vendor
  page, undated, read 2026-10-30): packet table for `YSFP`, `YSFU`,
  `YSFO`, `YSFI`, `YSFD`, `YSFS`. Secondary.
  https://ycs-wiki.xreflector.net/doku.php?id=start:protocols:ysf
- **[BMY]** BrandMeister wiki, "YSF Direct", oldid 18908, last edited
  2026-01-08: BrandMeister's login extension (`YSFL`/`YSFK`,
  `YSFACK`/`YSFNAK`, `YSFO group=`). Not used by the clients above; listed
  so its tags are not mistaken for core protocol.
  https://wiki.brandmeister.network/index.php/YSF_Direct
- **[G4]** G4KLX YSFClients READMEs and `YSFGateway.ini` /
  `YSFReflector.ini` comments (prose, no wire format).
- Secondary, not normative: K9EQ, "Fusion from the Inside", TAPR DCC 2017
  (names DN/VW/DW modes);
  https://files.tapr.org/meetings/DCC_2017/DCC2017-Fusion_from_the_Inside_Chris_Petersen_K9EQ.pdf
- BlueDV (PA7LIM, closed source): only a third-party 2021 Windows manual
  (room selection) and a changelog note that it sends DN (V/D) only, not VW.
- Not found: any official document of the network protocol, of DG-ID on
  air, or of the WIRES-X command format (the `5D xx 5F` codes are known
  from implementations only); the reflector registry's status-poll
  description (register.ysfreflector.de no longer resolves; the registry
  moved to DVRef).

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| YR | https://github.com/g4klx/YSFClients @ `3731800` (2023-02-17) | reference reflector (YSFReflector, removed from the repo by `9eb6d396` on 2023-02-23) |
| GW | https://github.com/g4klx/YSFClients @ `6e9359d` (2026-10-23) | client: YSFGateway, DGIdGateway (DGW); YSFParrot (a mini server) |
| MH | https://github.com/g4klx/MMDVMHost @ `9384d68` (2026-10-18) | consumer of the reflector's frames, through YSFGateway |
| DS | https://github.com/nostar/DroidStar @ `4ef18be` (2026-10-29) | client, direct to the reflector |
| CM | https://github.com/juribeparada/MMDVM_CM @ `7328c9e` (2024-09-07) | clients: YSF2DMR, YSF2NXDN, YSF2P25 |
| PY | https://github.com/iu5jae/pYSFReflector @ `54db5d9` (2024-12-29) | reflector, one stream |
| PY3 | https://github.com/iu5jae/pYSFReflector3 @ `8a9dbd7` (2026-04-15) | reflector, one stream per DG-ID room |
| URF | https://github.com/n7tae/urfd @ `bd0c114` (2025-12-29) | XLX-family reflector |
| YN | https://github.com/dbehnke/ysf-nexus @ `66109b1` (2026-10-26) | reflector (Go) |
| PS | https://github.com/AndyTaylorTweet/Pi-Star_DV_Dash @ `ca50804` (2026-10-22), Pi-Star_Binaries_sbin @ `3c73612` | hotspot image: YSFGateway configuration and host list |
| WP | https://github.com/W0CHP/YSFClients-W0CHP @ `3d203db` (2026-10-18) | WPSD's YSFGateway (protocol as GW) |
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference reflector on the rig (xlxd-ref) |

Hotspot images change nothing on the wire: Pi-Star and WPSD run
YSFGateway with the bare callsign (no suffix) in `YSFP`/`YSFU`, polls every
5 s (3 on link), no `[Network] Options` (so no `YSFO`), WiresX passthrough
as a user switch, and a host list where every reflector has its own port
(42000 by convention).

## How the cases judge

`cases/proto/test_ysf.py` probes xlxd-ref and the system under test in
parallel, as `test_mmdvm.py` does:

- **hb**: a MUST from [Y1] (frame structure only); asserted on both sides.
- **diff**: a convention the reflectors and clients agree on; sut == ref.
- **dev**: disputed, or xlxd deviates from what clients need. Under
  `DVT_SUT_PROFILE=xlxd` sut == ref (xlxd's behaviour is recorded as
  data); under another profile the profile's spec entry
  (`dvtest/specs/<profile>.py`, `"ysf"` dict `{"P-YSF-NN": {key: value}}`)
  decides, and the case skips with "spec pending" while there is none.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

Conventions: `YSFD` byte 34 is the "status" byte: bit 0 = end of
transmission, bits 7-1 = a 7-bit net frame counter. "Silently ignored"
means no reply and no state change. Voice payload bits are compared after
removing the AMBE+2 gain index where a reflector is known to re-encode
gain (xlxd; see P-YSF-18).

## Packet layout

| Tag | Size | Direction | Layout |
|---|---|---|---|
| `YSFP` | 14 B | client -> reflector | `YSFP` + callsign (10, space padded) |
| poll reply | 14 B | reflector -> client | `YSFP` + 10 bytes, by convention `REFLECTOR ` |
| `YSFU` | 14 B | client -> reflector | `YSFU` + callsign (10) |
| `YSFS` | 4 B | registry -> reflector | `YSFS` |
| status | 42 B | reflector -> requester | `YSFS` + id (5 digits) + name (16) + description (14) + client count (3 digits, zero padded) |
| `YSFD` | 155 B | both | `YSFD` + gateway (10) + source (10) + destination (10) + status byte + 120 B frame: sync `D4 71 C9 63 4D` (5), FICH (25), payload (90) |
| `YSFO` | 50 B | client -> reflector | `YSFO` + callsign (10) + options (36, e.g. a DG-ID list `20;21;`) |
| `YSFI` | 80 B | client -> reflector | info (frequencies, locator, name, type); not sent by current YSFGateway |

FICH fields [Y1 Table 4-1]: FI (0 header HC, 1 communication CC, 2
terminator TC, 3 test), CS, CM (0 group/CQ, 3 individual), BN/BT, FN/FT,
Dev, MR, VoIP, DT (0 V/D type 1 "DN1", 1 Data FR, 2 V/D type 2 "DN2", 3
Voice FR "VW"), SQL, SQ (7 bits, used as DG-ID).

## 1. Link and keepalive

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-01 | A 14 B `YSFP` from a new address links it and is answered at once with 14 B starting `YSFP`; every later poll is answered too | convention (impl, agreed) | [YCS] (poll / reply); none normative | GW: link up on any datagram tagged `YSFP` from the reflector (content and length unchecked), 3 polls on link then one per 5 s; DGW: linked only after a reply; DS: sends ONE poll, connected on any 14 B datagram (a reflector that drops the first poll strands it); CM: never reads replies | YR, PY, PY3, URF, YN answer every poll | measured: every poll answered with `YSFPREFLECTOR `, the first one included, in about 0.1 ms | answer every poll, the first one included |
| P-YSF-02 | The reply comes from the address and port the client sent to | convention (client requirement) | none | GW, DGW, CM drop datagrams whose source is not exactly the reflector IP and port; DS does not filter | all reply from their listening socket | measured: from 42000, the addressed port | reply from the receiving socket |
| P-YSF-03 | The 10 bytes after `YSFP` in the reply | disputed (informational) | [YCS] gives `ONLINE` (10 or 14 B) for its server | no client checks them (GW, DGW tag only; DS length 14) | YR, PY, PY3, URF `REFLECTOR `; YSFParrot `PARROT    `; YCS `ONLINE` | measured: `REFLECTOR ` | `REFLECTOR ` (14 B), the form every client was tested against |
| P-YSF-04 | Callsign in the poll: a suffixed or slashed callsign (`N0CALL-1`, `N0CALL/P`), 8-10 characters | disputed | none | GW sends its configured callsign (no suffix); DS sends the user's callsign as typed (longer than 10 makes a longer poll); CM a configured callsign | YR, PY no validation (address is the key); URF and xlxd keep 7 characters and silently ignore `-` or `/` in them, plus a gatekeeper | measured: `<call>-1` not answered; a 10-character callsign answered (source: `/` treated like `-`) | accept any printable callsign; key by address (P-YSF-35) |
| P-YSF-05 | A poll that is not 14 B (short, or long from a long callsign) | disputed | none | DS makes a longer poll for a callsign over 10 characters | YR, PY any length; xlxd, URF exactly 14 B | measured: a 15 B poll not answered (only 14 B) | answer polls of 14 B or more (callsign = bytes 4-13); ignore shorter |
| P-YSF-06 | The reflector polls the client on its own | disputed (not needed) | none | no client needs it: all poll every 5 s; GW drops the link after 120 s, DGW after 60 s with nothing received | YR polls every client every 5 s; PY, PY3, xlxd, URF, YN never | measured: no reflector poll in 12 s | not required; answering the 5 s polls keeps every client up |
| P-YSF-07 | A client silent past the timeout is dropped (no more voice); polls alone keep it linked | drop: convention (impl, agreed); timeout: disputed | none | GW, DGW, DS, CM poll every 5 s; GW, CM, DS on shutdown send nothing | YR 60 s after the last poll (only polls refresh); PY about 61 s; xlxd, URF 30 s (3 s check); YN 5 min, any packet refreshes | measured: a client silent for 35 s gets no voice; a polling one keeps it at 35 s and 70 s (source: 30 s) | keep on polls alone; drop after 30-60 s silence (60 s like the reference); judged by the case at 35 s and 70 s |

## 2. Unlink and status

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-08 | `YSFU` from a linked client removes it (no further voice); no reply | convention (impl); xlxd deviates | [YCS] | GW sends 3 `YSFU` on a reflector change, WIRES-X disconnect, inactivity revert (not on process exit); DGW on unlinking a non-static network; DS one on user disconnect; CM never | YR, PY, PY3, URF, YN remove the client, no reply | measured: `YSFU` ignored, no reply; the client still hears the module | remove at once, no reply |
| P-YSF-09 | `YSFS` (4 B or longer, any source, linked or not) is answered with the 42 B status | convention (impl, agreed) | [YCS] ("server messages to the registry"); [G4] ini | sent by the registry, no client | YR, PY, PY3, URF answer any `YSFS`; YN only 4 or 14 B | measured: 42 B answered to an unlinked address | answer with 42 B |
| P-YSF-10 | Status fields: id 5 decimal digits, name 16 and description 14 space padded and truncated, count 3 digits zero padded (at most 999) | convention (impl); id and count semantics disputed | [YCS] (id 5 + name 30 + count 3) | registry only | YR configured id or a name hash, count of YSF clients; PY may exceed 42 B (6-digit id, no truncation); xlxd, URF id = name hash, count = clients of all protocols | measured: id 5 digits, name `XLX999`, description `XLX reflector`, count 3 digits and includes a DMR client | exact 42 B form; count = linked YSF clients |

## 3. Data path

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-11 | A `YSFD` from a linked client reaches every other linked client of its module / room, once | convention (impl, agreed) | [YCS] | MH, DS, CM play what they receive | YR every linked client (one global room); PY all; PY3 same DG-ID room; URF same module | measured: both module listeners got the whole over once (plus one packet of padding), another module nothing | relay to the room's other clients |
| P-YSF-12 | The talker never receives its own stream back | convention (impl, agreed) | none | no client filters its own echo (DS, CM would play it) | all skip the sender address | measured: 0 packets to the talker | never echo |
| P-YSF-13 | A `YSFD` from an address that is not linked is not relayed | convention (impl, agreed) | none | GW always polls before sending | YR, PY, PY3, URF require a linked address | measured: 0 packets relayed | drop |
| P-YSF-14 | Egress `YSFD`: 155 B, frame sync `D4 71 C9 63 4D`, a FICH that decodes (CRC good), header (FI 0) first and terminator (FI 2) last | MUST (frame structure) | [Y1] 4.1 (HC, CC..., TC), 4.2.1 (FS, FICH coding), Table 4-1 | MH decodes the FICH of every frame; DS starts on FI header and stops on FI terminator; CM decodes the FICH | YR, PY forward the client's frames unchanged; PY3, xlxd, URF check the FICH | measured: 155 B, sync and FICH good, header first, terminator last (DMR and YSF talkers); DN2, CS 2, CM 0 | as [Y1] |
| P-YSF-15 | Egress gateway field (bytes 4-13) stays the same for a whole stream | convention (client requirement) | none | MH locks onto the gateway field of the first frame and drops frames with another until the end bit | YR, PY pass the talker's field; PY3 may rewrite payload text; xlxd, URF the RPT1 base callsign | measured: constant (the talker's callsign) | constant per stream |
| P-YSF-16 | Egress status byte: the counter differs between consecutive frames (counting up by one, 7 bits, wrapping) and the first frame has the end bit clear | convention (client requirement); xlxd deviates | [YCS] (status = end flag + count << 1) | MH drops a frame whose counter equals the previous one and ignores a stream whose first frame has the end bit set; DS ignores the byte | YR, PY pass the client's byte; xlxd, URF compute it (URF: the first voice frame of a non-YSF stream repeats the header's 0, inferred, not run) | measured: DMR talker: header 0, then changing; YSF talker: 0, 1..n, then a padding packet and the terminator both with counter 0 (a repeat MMDVMHost drops) | count up one per frame from header 0, no repeat at the end |
| P-YSF-17 | The last frame a stream sends has the end bit set | convention (client requirement) | [YCS] | MH ends a stream only on the end bit or its 1.5 s watchdog (FI terminator alone does not end it) | YR, PY pass it; xlxd sets it; URF's terminator never sets it (MH then waits for the watchdog) | measured: end bit on the terminator only (DMR and YSF talkers) | set it on the last frame |
| P-YSF-18 | Same-mode relay keeps the voice and the callsigns: source (bytes 14-23), destination, DCH/CSD text, AMBE frames | disputed | [Y1] 4.2.3 (DN2 voice), Table 4-2 (CSD) | MH, DS show the source from bytes 14-23 | YR byte for byte; PY cleans non-printable bytes in 4-33; YN cuts the callsign fields at the first `-`, `/` or space; PY3 may prefix the DG-ID to the source text; xlxd, URF rebuild the frame (destination `ALL`, DG-ID 0, own DCH text) | measured: voice bits kept apart from the gain index (one extra padding packet); source and gateway kept; destination rewritten to `ALL` | keep source, destination and voice bits unchanged for YSF -> YSF (same as DV-Router sid/byte preservation, B10) |
| P-YSF-19 | Egress source callsign = the talker; destination `ALL` (group) for a cross-mode stream | convention (impl, agreed) | [Y1] Table 4-2 (Dest, Src) | MH, DS log bytes 14 and 24; blank destination with group CM reads as `ALL` | xlxd, URF talker + `ALL`; YR, PY pass the client's | measured: source `N0CALL` (the DMR id's callsign), destination `ALL` | talker + `ALL` |

## 4. Stream start and end (ingress)

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-20 | A stream ends on the end bit, or on an FI terminator without the end bit, or after about 1.5 s without frames; the module is then free | end bit / silence: convention (impl, agreed); terminator without end bit: disputed | [Y1] 4.6 (stop condition "not specified") | MH sends the end bit with the terminator; CM's terminator sets the end bit only when its raw count is odd; DS hotspot frames never set it | YR, PY end bit or 1.5 s; PY3 FI terminator (end bit ignored) or 2.0 s; xlxd, URF FI terminator or 1.6 s (URF merges an over that starts within 1.6 s) | measured: egress stream ends 0.25 s after an end-bit-only over, 0.2 s after a terminator without end bit, 0.76 s after an over with neither | end on either marker or 1.5 s silence; send the egress terminator promptly |
| P-YSF-21 | A stream that starts without a header (late entry, CC first) is carried | disputed | [Y1] 4.6 (header sent once; receivers re-synchronise on their own) | MH relays RF late entry (no header); DS starts on a CC while idle | YR, PY no header needed; URF synthesises a header (source); PY3 needs a header | measured: not carried (no header synthesised) | carry it (B2 late-entry policy decides between talkers) |
| P-YSF-22 | A second talker on a busy module / room is not mixed into the running stream | disputed | none | MH locks on the first gateway field | YR, YN forward both (only the log / dashboard follows the first); PY, PY3, xlxd, URF first come | measured: a DMR talker 0.25 s into a YSF over is not mixed: the listener gets the first over whole, then the second; two YSF talkers behind one IP share one xlxd stream (xlxd keys YSF streams by source IP) | first talker wins (B1) |
| P-YSF-23 | A stream longer than the counter range (more than 128 frames) wraps the counter and stays one stream | convention (impl, agreed) | [YCS] | MH compares only consecutive counters | none count streams by the counter | measured: one stream, every frame once over 137 packets | one stream |

## 5. Data types, DG-ID and module selection

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-24 | V/D type 2 (DN2) voice is carried | convention (impl, agreed) | [Y1] 4.2.3 | DS, CM YSF2DMR/YSF2NXDN send DN2; radios in DN mode | all carry DN2 | measured: carried | carry |
| P-YSF-25 | V/D type 1 (DN1) and Voice FR (VW) are carried between YSF clients | disputed | [Y1] 4.2.2 (DN1), 4.4 (VW) | radios in DN1/VW mode (FT-70D, FTM-400 settings), DS VW option, CM YSF2P25 sends VW; BlueDV DN only | YR, PY, PY3, YN carry every data type; xlxd, URF drop DN1 and VW | measured: DN1 and VW not relayed | carry same-mode (no transcoding needed between YSF clients) |
| P-YSF-26 | Data FR frames that are not WIRES-X commands | disputed | [Y1] 4.3 | radios send pictures/messages as Data FR | YR, PY carry; PY3 blocks FT 1-2; xlxd, URF drop | measured: not relayed | relay unchanged ([Y1] 4.3 defines Data FR as a normal mode; YR, PY, YN relay it, MH transmits it; YSFGateway filters it itself for `XLX*` names) |
| P-YSF-27 | A `YSFD` whose FICH does not decode is not relayed | disputed | [Y1] 4.5 (FICH error control) | MH, CM decode the FICH themselves and drop a bad one | YR, PY forward unchecked; PY3, xlxd, URF drop | measured: not relayed (header and its silence frames only) | drop |
| P-YSF-28 | A header with DG-ID 10..35 moves the client to module A..Z | convention (XLX family) | none (DG-ID is not in [Y1]; radio manuals: DG-ID 00-99, 00 = all) | DGW maps DG-IDs locally and sends DG-ID 0 to a YSF reflector; radios send the selected DG-ID | xlxd, URF 10..35 -> A..Z (a permanent move); PY3 per-DG-ID rooms; YR ignores | measured: DG-ID 23 (W) moved the client; a later DG-ID 0 over stayed on W | DG-ID 0 = no selection (all clients send 0); map 10..35 to A..Z only as an opt-in XLX-compatible feature; never filter delivery by DG-ID |
| P-YSF-29 | A new client's module before any selection | convention (XLX family) | none | GW, DS expect to hear the reflector after linking | xlxd auto-links B; URF the configured AutoLinkModule (none if unset); YR one room | measured: a client that only polled hears module B, not A | one configurable default module (a poll cannot choose); B for XLX compatibility |
| P-YSF-30 | Egress DG-ID (FICH SQ) | disputed | [Y1] Table 4-1 (SQL/SC) | radios with DG-ID set open squelch only on their DG-ID or 00; DGW rewrites | xlxd, URF 0; YR, PY pass the talker's | measured: 0 | 0 (opens every radio) |

## 6. WIRES-X

No document; the command codes are known from YSFGateway, xlxd and URFd.
YSFGateway handles WIRES-X locally and passes commands to the reflector
only when `WiresXCommandPassthrough` is on and the reflector's name
starts with `XLX` (it then sends one `DX_REQ` after linking).

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-31 | A WIRES-X `DX_REQ` (Data FR, `5D 71 5F`) is answered with a DX reply after about 1 s, as Data FR frames to the requester | disputed | none | GW passthrough to `XLX*` names; radios' WIRES-X button | xlxd, URF answer (single-frame commands only); YR, PY relay it to the room like data; PY3 blocks | measured: DX reply to the requester, nothing relayed to the module | answer DX/ALL/CONN/DISC in the xlxd/URFd form only on a multi-module server, after about 1 s, to the requester only; never relay command frames to the room |
| P-YSF-32 | `CONN_REQ` room n (1..26) selects module A..Z; `DISC_REQ` leaves the module (client stays linked) | convention (XLX family) | none | GW passthrough; DMR2YSF sends CONN/DISC to its gateway | xlxd, URF; YR, PY relay; PY3 blocks | measured: CONN room 2 answered and moved the client to B; after DISC it hears nothing | as xlxd (room n -> module n, DISC leaves the module, link kept) when WiresX is answered |

## 7. Other tags and malformed packets

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-33 | `YSFO`, `YSFI`, `YSFV` and unknown tags get no reply; the link and service continue | convention (impl, agreed no reply) | [YCS] (YSFO/YSFI), [BMY] (BrandMeister tags) | GW sends `YSFO` after the first reply and with every poll when Options is set | YR, URF, xlxd ignore; PY3 takes the first DG-ID of `YSFO` as the home room; PY answers `YSFV` with its version | measured: no reply to `YSFO`, `YSFI`, `YSFV`, `XXXX`; polls still answered | ignore without reply; do not interpret `YSFO` (three incompatible meanings: YCS DG-ID list, PY3 home DG-ID, BrandMeister `group=`) |
| P-YSF-34 | Short or long `YSFD` (154/156 B), a bad frame sync, a short `YSFU`: no reply, not relayed, service continues | convention (impl) | none | none send them | xlxd, URF, PY, PY3 exact 155 B; YR no length check (relays a short packet padded with stale bytes) | measured: 154 / 156 B not relayed, short `YSFU` ignored, no error reply; a bad frame sync: xlxd-ref relays the packet with the sync repaired plus 2 extra trailing frames (16 frames out against 14; reported from a capture, 2026-10-31) | drop silently (dev) |

## 8. Several clients

| # | Obligation | Level | Spec / paper | Clients | Other reflectors | xlxd-ref | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-YSF-35 | Two ports on one IP are two clients; each gets the module's traffic once; the same callsign from two ports is two clients | convention (impl, agreed) | none | GW, DS, CM use one socket per link | YR, PY, PY3, URF key by address | measured: each port got the over once | key by address |
| P-YSF-36 | Two callsigns polling from the same address | disputed | none | not done by real clients (one gateway callsign per socket) | YR one client (the second poll refreshes the entry); xlxd two clients (duplicate delivery to that address) | measured: one copy (a second callsign joined to the module from the same socket did not double the delivery) | one client per address |
| P-YSF-37 | A poll from one client never refreshes another client's link | convention (impl, agreed) | none | - | per-client timers everywhere | measured: per client | per client |

## Recommendation differs from xlxd

| # | xlxd-ref | Recommended for DV-Router |
|---|---|---|
| P-YSF-04 | silence for `-`/`/` in the first 7 callsign characters | accept, key by address |
| P-YSF-05 | only 14 B polls | 14 B or more |
| P-YSF-07 | 30 s timeout | 30-60 s (60 s like the reference) |
| P-YSF-08 | `YSFU` ignored | remove the client at once |
| P-YSF-10 | count = clients of all protocols | count = YSF clients |
| P-YSF-18 | frame rebuilt, AMBE gain index moved | keep voice bits and callsigns for YSF -> YSF |
| P-YSF-25 | DN1 and VW dropped | carry same-mode |
| P-YSF-16 | YSF -> YSF: the padding packet and the terminator repeat counter 0 | count up one per frame |
| P-YSF-21 | headerless stream not carried | carry it |

xlxd meets the client-critical items P-YSF-01, 02, 15 and 17, and P-YSF-16
for cross-mode streams only (URFd breaks 16 and 17 by inference; the
cases measure xlxd only). P-YSF-36 was expected to differ from the source
reading but measured one copy.

## Testlist rows

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-YSF-01 | YSFP answered at once, first poll included | test_ysf::test_ysf_poll_reply | diff | probe | covered |
| P-YSF-02 | Reply from the addressed port | test_ysf::test_ysf_poll_reply | diff | probe | covered |
| P-YSF-03 | Reply bytes 4-13 | test_ysf::test_ysf_poll_reply_text | dev | probe | covered |
| P-YSF-04 | Suffixed / 10-character callsign | test_ysf::test_ysf_poll_callsign_forms | dev | probe | covered |
| P-YSF-05 | Poll of 15 B | test_ysf::test_ysf_poll_length | dev | probe | covered |
| P-YSF-06 | Reflector-initiated polls | test_ysf::test_ysf_server_polls | dev | probe (20 s idle) | covered |
| P-YSF-07 | Timeout drop; polls alone keep the link | test_ysf::test_ysf_timeout_drop | dev | probe (70 s, slow) | covered |
| P-YSF-08 | YSFU removes the client | test_ysf::test_ysf_unlink | dev | probe | covered |
| P-YSF-09 | YSFS answered from an unlinked address | test_ysf::test_ysf_status | diff | probe | covered |
| P-YSF-10 | Status field forms; count includes other protocols | test_ysf::test_ysf_status_fields | dev | probe | covered |
| P-YSF-11 | Relay to every other linked client once | test_ysf::test_ysf_relay_once | diff | probe | covered |
| P-YSF-12 | No echo to the talker | test_ysf::test_ysf_no_echo | diff | probe | covered |
| P-YSF-13 | Unlinked sender not relayed | test_ysf::test_ysf_unlinked_sender | diff | probe | covered |
| P-YSF-14 | Egress frame structure (sync, FICH, HC first, TC last) | test_ysf::test_ysf_egress_structure | hb | probe + dvcheck | covered |
| P-YSF-15 | Gateway field constant per stream | test_ysf::test_ysf_egress_gateway_constant | diff | probe | covered |
| P-YSF-16 | Counter changes every frame (DMR and YSF talker); header end bit clear | test_ysf::test_ysf_egress_counter | dev | probe + dvcheck | covered |
| P-YSF-17 | End bit on the last frame (DMR and YSF talker) | test_ysf::test_ysf_egress_end_bit | diff | probe + dvcheck | covered |
| P-YSF-18 | Same-mode relay keeps voice and callsigns | test_ysf::test_ysf_same_mode_kept | dev | probe | covered |
| P-YSF-19 | Cross-mode egress source and destination | test_ysf::test_ysf_crossmode_callsigns | diff | probe | covered |
| P-YSF-20 | Ingress end markers: end bit only, TC only, neither (egress end delay) | test_ysf::test_ysf_stream_end_markers | dev | probe | covered |
| P-YSF-21 | Headerless stream carried | test_ysf::test_ysf_late_entry | dev | probe | covered |
| P-YSF-22 | Second (DMR) talker not mixed into a YSF over | test_ysf::test_ysf_second_talker | spec (B1) | probe | covered |
| P-YSF-23 | Counter wrap in a long over | test_ysf::test_ysf_counter_wrap | diff | probe | covered |
| P-YSF-24 | DN2 carried | test_ysf::test_ysf_relay_once | diff | probe | covered |
| P-YSF-25 | DN1 and VW between YSF clients | test_ysf::test_ysf_dn1_vw_carried | dev | probe | covered |
| P-YSF-26 | Non-WIRES-X Data FR | test_ysf::test_ysf_data_fr | dev | probe | covered |
| P-YSF-27 | Bad FICH not relayed | test_ysf::test_ysf_bad_fich | dev | probe | covered |
| P-YSF-28 | DG-ID 10..35 selects the module | test_ysf::test_ysf_dgid_module | dev | probe | covered |
| P-YSF-29 | Default module of a new client | test_ysf::test_ysf_default_module | dev | probe | covered |
| P-YSF-30 | Egress DG-ID | test_ysf::test_ysf_egress_dgid | dev | probe | covered |
| P-YSF-31 | WIRES-X DX_REQ | test_ysf::test_ysf_wiresx_dx | dev | probe | covered |
| P-YSF-32 | WIRES-X CONN_REQ / DISC_REQ | test_ysf::test_ysf_wiresx_conn_disc | dev | probe | covered |
| P-YSF-33 | YSFO / YSFI / YSFV / unknown tags ignored | test_ysf::test_ysf_other_tags | diff | probe | covered |
| P-YSF-34 | Malformed packets dropped | test_ysf::test_ysf_malformed | diff + dev (bad sync) | probe | covered |
| P-YSF-35 | Two ports one IP; same callsign two ports | test_ysf::test_ysf_two_ports | diff | probe | covered |
| P-YSF-36 | Two callsigns from one address | test_ysf::test_ysf_two_callsigns_one_address | dev | probe | covered |
| P-YSF-37 | Per-client keepalive | test_ysf::test_ysf_timeout_drop | diff | probe (slow) | covered |
