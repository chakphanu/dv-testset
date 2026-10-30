<!-- SPDX-License-Identifier: MIT -->
# Yaesu IMRS: obligations of a reflector that DR-2X repeaters link to

IMRS ("Internet-linked Multi-site Repeater System", Yaesu's own name) links
System Fusion repeaters over UDP: a DR-2X / DR-2XE with the LAN-01A unit
keeps a list of up to 100 destination IP addresses and exchanges voice
with every listed peer that lists it back. A reflector joins such a group
by being one of those destinations: xlxd does this on UDP 21110 (since
xlxd 2.5, 2021), and DV-Router plans an `imrs` edge that does the same.
Each item is numbered `P-IMR-NN`; the testlist rows are at the end.

**Sources are thin, and this file says so row by row.** Yaesu documents
how to set IMRS up, not what goes on the wire. The only published wire
behaviour is code: xlxd's `cimrsprotocol` (the reference), a 2025 URFd
fork that ports it, and a 2020 G4KLX experiment that never transmitted.
So almost every network row is a `convention (xlxd)` and the "Clients"
column says what the DR-2X documents imply. The frame content (FICH, V/D
type 2 voice, callsign data) comes from Yaesu's air-interface standard,
shared with `ysf.md` ([Y1]; same terms: FI, DT, SQ = DG-ID, VD2).

**Level**: MUST / SHOULD only where a Yaesu document says so (port,
frame structure); `convention (xlxd)` where only xlxd (and the URFd fork
that copied it) backs it; `disputed` where implementations differ.

## Sources

Documents (all read):

- **[IM]** Yaesu, "IMRS (Internet-linked Multi-site Repeater System)
  Setup Software for the DR-2X/DR-2XE Instruction Manual", 1710-B, 2017,
  32 pages. https://yaesu.com/Files/DFDF4D53-1B78-7375-E6C7A6E4B192BE13/LAN-01A_IMRS_IM_ENG_1710-B.pdf
  - p.2: one UDP port, 21110 (default); p.12: "DO NOT CHANGE" it;
    static global IP; NAPT / port mapping of UDP 21110 to the LAN-01A.
  - p.17-19: destination list 001-100, IP address only (no port, no ID),
    each destination must list this repeater too.
  - p.21-23: "Search Repeaters" connection confirmation shows each
    peer's callsign, Radio ID and the DG-IDs it has IMRS enabled for.
- **[OM]** Yaesu, "DR-2X/DR-2XE Operating Manual", 2002H-DS-1 (2020-10),
  56 pages: p.15-20 DG-ID, Group DG-ID, IMRS TOT (0 s ... 10 min, CONT;
  default 1 min); p.24-25 "Advanced Set up: IMRS" (up to 99 group
  repeaters); p.26-27 remote commands sent to all linked repeaters.
- **[Y1]** Yaesu Musen, "Amateur Radio Digital Standards", rev 1.02
  2015-07-29 (see `ysf.md`): FICH 4.2.1 / Table 4-1, callsign data Table
  4-2, V/D type 2 4.2.3, start / keep / stop 4.6. No IMRS document cites
  it.
- **[WX]** Yaesu HRI-200 manuals (2510-P, 2509-L): WIRES-X ports
  (UDP 46100-46122, TCP 46190); they do not mention IMRS. [OM] p.39:
  HRI-200 and LAN-01A cannot be used together.
- Not found: any Yaesu or third-party description of the IMRS packet
  format, keepalive or registration; LAN-01A firmware notes; a DR-1X
  IMRS option (the YCS wiki says the DR-1X has no LAN interface).

Implementations (behaviour described, no code copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd, IMRS since PR #202 (2021-10-31), 2.5.1 fix for long QSOs (2021-11-07); rig copy `a978f03` (2.6.1) | reference (xlxd-ref): listens on UDP 21110 |
| UF | https://github.com/dbehnke/urfd branch dev @ `2a8b30aa` (2025-12-24) | URFd fork, IMRS ported from xlxd (same tags and sizes; terminator tag byte 6 = 0x07 where xlxd sends 0x00); upstream n7tae/urfd has no IMRS |
| DG | https://github.com/g4klx/YSFClients `DGIdGateway/IMRSNetwork` (2020-08/09) | experiment: a different framing (first byte 0x11 / 0x22 / 0x33, little-endian sequence, 47 B header/trailer, no ping); its send path is compiled out, so it never talked to a DR-2X |
| YCS | YCS server wiki (closed source) | claims IMRS on UDP 21110; DR-2X setup notes (destination 001 = server IP, LOCAL DG-ID 01, IMRS TOT 30 s, DG-ID 90 = disconnect) |
| DR-2X | Yaesu DR-2X/DR-2XE with LAN-01A ([IM], [OM]) | the client: what it needs is inferred from the manuals and from xlxd's comments that cite DR-2X behaviour |

## What the rig builds and what was measured

The rig builds xlxd-ref from LX3JL/xlxd `a978f03` and xlxd-sut from
chakphanu/xlxd `pr/pcm-usrp-slim` `d8ffa41` (`.cache/xlxd/{ref,sut}.fetched`);
their `cimrsprotocol.cpp` is identical. The xlxd column was measured on
2026-10-30:
- run `20261030T084017Z-i8`: 24 cases passed, sut == ref on every row,
  dvcheck 0 errors;
- run `20261030T085034Z-i8`: P-IMR-24.

Rows not measured say "source:" (code reading).

## How the cases judge

`cases/proto/test_imrs.py` probes xlxd-ref and the system under
test in parallel. The tester plays a DR-2X: IMRS has no port choice, so
the tester binds UDP 21110 itself and tells ref and sut apart by source
address. xlxd builds always enable IMRS (protocol slot 8, `IMRS_PORT`
21110); the rig needs no change.

- **hb**: a Yaesu-document requirement (port, frame structure); both sides.
- **diff**: sut == ref (an xlxd convention that a DR-2X relies on).
- **dev**: xlxd defect or choice where a different behaviour serves the
  repeaters better: sut == ref under `DVT_SUT_PROFILE=xlxd`, else
  `SPEC["imrs"]["P-IMR-NN"]`; "spec pending" skips.
- Under any other profile every case skips with "edge not ready" until
  `SPEC["imrs"]["server"]` is True.
- **Register first.** If a profile's `SPEC["imrs"]["P-IMR-04"]` says a
  non-peer's ping is not answered (`{"answered": False}`), the tester
  becomes a peer before it pings that sut:
  - The readiness wait does not ping that sut. The rig fixture has already
    seen it answer DExtra, and a fixed 5 s settle follows.
  - P-IMR-04 runs first, before anything registers the tester.
  - P-IMR-01, 03 and 05 send an info packet before their ping.

  Peer identity is the IP address, so another source port of the tester
  is not a stranger. xlxd answers anyone, so under the `xlxd` profile
  nothing registers first.

## Packet layout (xlxd, UDP 21110 both ways)

| Packet | Size | Layout |
|---|---|---|
| ping | 16 B | `00 00 07` + 13 x `00`; answered by any peer with pong + ping |
| info (connect / pong) | 60 B | `00 2C 08 00` + 12 x `00`; 16-19 firmware (LE); 20-25 MAC; 26-35 callsign (space padded); 36-40 radio id text; 41-45 DG-ID bitmap (LE, bit n = DG-ID n); 58-59 DG-ID bytes |
| header | 91 B | 19-byte prefix (`00`, `4B` = size - 16, 5 tag bytes, 24-bit time, 16-bit stream id, 4 x `00`, type `I`, `**`), then ASCII hex: frame id (4), FICH (8), then 60 B callsign data in plain ASCII ([Y1] Table 4-2: dest, src, down, up, rem1-4) |
| voice | 181 B | prefix (`A5`, type `2`), ASCII hex: frame id (4), FICH (8), DCH (20), five V/D type 2 voice channels of 13 B (26 hex each) = 100 ms |
| terminator | 31 B | prefix (`0F`, type `T`), ASCII hex: frame id (4), FICH (8) |

FICH ([Y1] Table 4-1) travels as 4 raw bytes in hex (no Golay, no CRC):
byte 0 FI/CS/CM/BN, byte 1 BT/FN/FT, byte 2 Dev/MR/VoIP/DT, byte 3
SQL + SQ (DG-ID). Every voice channel is [Y1] 4.2.3 V/D type 2 (49 voice
bits, 27 of them sent three times, interleaved and whitened); the 49 bits
are the same AMBE+2 frame as DMR and NXDN, so IMRS <-> DMR needs no
transcoding.

## 1. Transport and peers

| # | Obligation | Level | Spec / paper | Clients (DR-2X) | Other implementations | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-IMR-01 | Listen on UDP 21110 and send every packet to the peer's IP at port 21110 (not to the source port) | MUST ([IM] p.2, p.12: one fixed UDP port) | [IM] p.2, p.12 | a DR-2X sends from and listens on 21110; destinations have no port field | UF same; YCS 21110 | measured: a ping from another source port is answered at 21110 only, from 21110 | as xlxd (hb) |
| P-IMR-02 | A peer is identified by its IP address | convention (DR-2X list holds IPs only); xlxd keys by (callsign, IP) | [IM] p.17-19 | one DR-2X per public IP (NAPT of 21110) | UF as xlxd | measured: two callsigns registered from one address on one room still get one copy of a call | one session per IP, as measured (dev) |

## 2. Discovery and keepalive

| # | Obligation | Level | Spec / paper | Clients (DR-2X) | Other implementations | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-IMR-03 | A 16 B ping is answered with a 60 B info packet and a 16 B ping | convention (xlxd; UF) | [IM] p.21-23 ("Search Repeaters" confirmation) | the confirmation screen shows the peer's callsign, Radio ID and DG-IDs: some info exchange exists; its format is undocumented | UF same | measured: 60 B info + 16 B ping (76 B) per ping | as xlxd, but only to configured or known peers (P-IMR-04) |
| P-IMR-04 | Pings from addresses that are not peers are not answered (16 B in, 76 B out: a reflection amplifier otherwise) | disputed (security) | none | a DR-2X only talks to its configured list | UF as xlxd | measured: an address that never sent info gets the 76 B answer | answer only peers that sent info, or rate-limit (dev) |
| P-IMR-05 | The info packet carries the server's callsign and a DG-ID bitmap of exactly the DG-IDs it serves | convention (xlxd) | [IM] p.23 ("Only a DG-ID whose IMRS is set to ON is indicated") | the repeater shows the peer's DG-IDs | UF as xlxd | measured: callsign `XLX999`, bitmap DG-ID 2 and 10-35, bytes 58-59 = 02 02 (DG-ID 2 is not a room: P-IMR-08); radio-id text a fixed 5-character string | advertise exactly the mapped DG-IDs (dev) |
| P-IMR-06 | A 60 B info packet from a repeater with a valid callsign registers it (gatekeeper rules apply); default room | convention (xlxd) | none | sent by the repeater (presumably on startup and "Search Repeaters") | UF as xlxd | measured: 7- and 8-character callsigns both register (default room B) | as xlxd (dev) |
| P-IMR-07 | A peer silent for the keepalive timeout is dropped; activity of one peer does not keep another alive | timeout: convention (xlxd); isolation: convention | none ([OM] IMRS TOT is a group hang time, not a link timeout) | the repeater's own refresh period is undocumented | UF as xlxd | measured: dropped after 190 s idle; registered again, a call to it at 100 s kept it alive at 200 s without any info packet | per-peer refresh only (dev) |

## 3. DG-ID and room selection

| # | Obligation | Level | Spec / paper | Clients (DR-2X) | Other implementations | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-IMR-08 | The DG-ID (FICH SQ) of a call's header selects the room; the peer follows it | convention (xlxd); DG-ID semantics from [OM] p.15-20 | [Y1] Table 4-1 (SQ); [OM] p.15-20 (DG-ID 01-99, Group DG-ID, 00 = all) | a DR-2X sends the DG-ID the user set; Group DG-IDs link sets of repeaters | YCS: DG-ID 02-99, 90 disconnects | measured: DG-ID 20 is heard on room K and the peer follows to K; DG-ID 5 is not heard and the peer gets no more traffic of its room | a configured DG-ID -> room map (default 10-35 = A-Z); an unmapped DG-ID is dropped without unlinking (dev) |
| P-IMR-09 | Egress carries the room's DG-ID in FICH SQ, SQL 0 | convention (xlxd) | [Y1] Table 4-1 | the receiving repeater squelches on its registered DG-IDs ([OM] p.16) | UF as xlxd | measured: SQ 19 on every packet of room J, SQL 0 | as xlxd (diff) |
| P-IMR-24 | An empty call from another mode (header + terminator, no voice: a DMR join) is not sent to IMRS peers | convention (xlxd sends none of it to DMR listeners, P-DMR-33) | [Y1] 4.6 | a repeater keys up for it | - | measured: header + terminator sent | drop empty calls (dev) |
| P-IMR-10 | Egress goes only to peers on the stream's room | convention (xlxd) | none | - | UF as xlxd | measured: 0 packets of another room's call | as xlxd (diff) |

## 4. Frames

| # | Obligation | Level | Spec / paper | Clients (DR-2X) | Other implementations | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-IMR-11 | A call starts with a header: FICH FI = 0 (HC), DT = 2 (V/D type 2), callsign data with the source in the src field; dest = five asterisks + a 5-byte tail, rem4 = the same 5 bytes | structure: MUST ([Y1] 4.6, Table 4-2); the tail text: disputed | [Y1] 4.2.1, 4.6, Table 4-2 | - | UF as xlxd | measured: one 91 B header first, FI 0, CM 1, DT 2; src `N0CALL`, up `N0CALL B`; dest tail and rem4 hold the same fixed 5-character text (a real device's radio id, presumably copied from a DR-2X; not reproduced here) | structure, src, up, CM as xlxd (hb + diff); the tail text: a neutral constant, the same in dest tail and rem4, never a device's id; DV-Router uses `00000` (dev) |
| P-IMR-12 | Voice: FICH FI = 1, DT = 2; five V/D type 2 channels (100 ms) per packet; the 49-bit AMBE+2 frames unchanged across IMRS <-> DMR / NXDN (no gain step) | structure: MUST ([Y1] 4.2.3); unchanged: convention (xlxd) | [Y1] 4.2.3 | - | UF as xlxd | measured: 6 voice packets for 30 frames, FI 1, DT 2, FN 0 on every packet; 30/30 frames bit-exact DMR -> IMRS and IMRS -> DMR (no gain step) | as xlxd (FN 0 per the DR-2X note) (diff) |
| P-IMR-13 | A call ends with a terminator: FICH FI = 2 | MUST ([Y1] 4.6) | [Y1] 4.6 | - | UF same, tag byte 6 differs | measured: one 31 B terminator, last, FI 2 | as xlxd (hb) |
| P-IMR-14 | No voice is lost at the end of a call: a last group of fewer than five frames is sent (padded with silence) | convention (no loss) | none | - | UF as xlxd | measured: 9 frames in, 5 out (the last 4 dropped) | pad and send (dev) |
| P-IMR-15 | An ingress terminator adds no audio | convention | [Y1] 4.6 (the terminator carries no voice) | - | - | measured: 15 frames in, 15 out, no zero frame (the code-reading claim of an injected frame did not show on the DMR egress) | as xlxd: add nothing (dev) |
| P-IMR-16 | A missing voice channel (packet loss, a stream not starting on a five-frame boundary) is not replaced by stale audio | convention | none | - | - | source: the stale or zero slot is sent (not probed: needs a lossy path) | silence frames for missing slots (dev) |

## 5. Streams and peers

| # | Obligation | Level | Spec / paper | Clients (DR-2X) | Other implementations | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-IMR-17 | Each over gets its own stream id; peers in one /16 do not collide | convention (B8 admission, B10) | none | - | - | measured: two calls in a row from the peer carry the same DMR stream id at the listener | unique id per over (dev) |
| P-IMR-18 | The talking repeater gets nothing of its own call back, on any room | convention (B9) | none | a DR-2X would repeat its own call | UF as xlxd | measured: after the talker's terminator the last 2 voice packets and a terminator of its own call come back to it | no echo, including the tail (dev) |
| P-IMR-19 | A header from an address that never sent info is dropped | convention (xlxd) | none | the repeater exchanges info first (presumed) | UF as xlxd | measured: not routed (an address whose registration timed out) | as xlxd (diff) |
| P-IMR-20 | Header callsigns in lower case or with a NUL are accepted or rejected consistently | disputed | [Y1] Table 4-2 (ASCII) | radios send upper case | - | measured: a lower-case source callsign is not routed | accept printable ASCII (dev) |
| P-IMR-21 | A call whose terminator is lost ends after the silence timeout; every egress gets a terminator | judged by B3 | [Y1] 4.6 | [OM] IMRS TOT holds the group after the last TX | - | measured: the DMR listener gets a terminator; the room takes the next call | per B3 (spec) |
| P-IMR-22 | Two repeaters on one room at once: one winner, no mixing | judged by B1 | none | [OM] p.18: three beeps = peer busy | - | measured: an IMRS call and a DMR call at once: the DMR listener got all 30 frames of each (mixed) | per B1 (spec) |
| P-IMR-23 | IMRS <-> DMR / NXDN is passthrough (same AMBE+2); IMRS <-> D-STAR / M17 transcoded, sut == ref | convention | [Y1] 4.2.3 | - | - | measured: D-STAR and M17 both ways, sut == ref (counts and bytes) | as xlxd (diff) |

## Recommendations that differ from xlxd

| # | xlxd | Recommended | Why |
|---|---|---|---|
| P-IMR-04 | pings answered from anyone | peers only, or rate-limited | 16 B in, 76 B out to any address |
| P-IMR-05 | advertises DG-ID 2 and 10-35 | exactly the mapped DG-IDs | DG-ID 2 unlinks on xlxd |
| P-IMR-07 | any egress refreshes every client | per-peer refresh | dead peers otherwise linger while traffic flows |
| P-IMR-08 | unmapped DG-ID unlinks the peer | drop the call, keep the link | a wrong DG-ID should not disconnect |
| P-IMR-11 | a real device's radio id as the fixed text in the header dest tail and rem4 | a neutral constant (same in both) | no-PII rule; nothing in [Y1] requires that value |
| P-IMR-14 | last 1-4 frames dropped | pad and send | up to 80 ms lost per over |
| P-IMR-18 | the tail of the talker's own call (2 voice packets + terminator) is sent back to it | no echo | the repeater would transmit its own tail |
| P-IMR-24 | an empty DMR call (a join) goes out as header + terminator | drop empty calls | a key-up on every IMRS repeater (as NXDN P-NXD-30) |
| P-IMR-16 | stale or zero frame for a missing slot | silence frame | no replayed audio |
| P-IMR-17 | stream id from the /16 of the address | unique per over | collisions between peers and overs |
| P-IMR-20 | lower case rejects the header | accept printable ASCII | robustness |

## Testlist rows

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-IMR-01 | UDP 21110 both ways | test_imrs::test_imrs_port_21110 | hb | probe | covered |
| P-IMR-02 | One session per IP vs per (callsign, IP) | test_imrs::test_imrs_two_callsigns_one_ip | dev | probe | covered |
| P-IMR-03 | Ping -> info + ping | test_imrs::test_imrs_ping_answer | diff | probe | covered |
| P-IMR-04 | Ping from a non-peer | test_imrs::test_imrs_ping_from_stranger | dev | probe | covered |
| P-IMR-05 | Info content: callsign, DG-ID bitmap | test_imrs::test_imrs_info_content | dev | probe | covered |
| P-IMR-06 | Info registers the repeater; 7 / 8-character callsign | test_imrs::test_imrs_register[7\|8] | diff + dev | probe | covered |
| P-IMR-07 | Silent peer dropped; isolation | test_imrs::test_imrs_peer_timeout | dev | probe | covered (slow) |
| P-IMR-24 | Empty call from another mode not sent to IMRS | test_imrs::test_imrs_empty_stream_not_relayed | dev | probe | covered |
| P-IMR-08 | Header DG-ID selects the room; unmapped DG-ID | test_imrs::test_imrs_dgid_room[mapped\|unmapped] | diff + dev | probe | covered |
| P-IMR-09 | Egress DG-ID = room | test_imrs::test_imrs_egress_dgid | diff | probe | covered |
| P-IMR-10 | Egress only to the room's peers | test_imrs::test_imrs_egress_room_only | diff | probe | covered |
| P-IMR-11 | Header frame structure; dest tail / rem4 text | test_imrs::test_imrs_egress_header | hb + diff + dev | probe | covered |
| P-IMR-12 | Voice frames: VD2, five per packet, AMBE+2 unchanged | test_imrs::test_imrs_voice_passthrough | hb + diff | probe | covered |
| P-IMR-13 | Terminator FI = 2 | test_imrs::test_imrs_egress_terminator | hb | probe | covered |
| P-IMR-14 | Last partial group sent | test_imrs::test_imrs_partial_group | dev | probe | covered |
| P-IMR-15 | Terminator adds no audio | test_imrs::test_imrs_terminator_no_audio | dev | probe | covered |
| P-IMR-16 | Missing slot not replayed | - | dev | needs a lossy path (netem) | not covered |
| P-IMR-17 | Stream id per over | test_imrs::test_imrs_stream_id | dev | probe | covered |
| P-IMR-18 | No echo to the talking repeater (tail included) | test_imrs::test_imrs_no_echo | dev | probe | covered |
| P-IMR-19 | Header without info dropped | test_imrs::test_imrs_peer_timeout | diff | probe | covered (slow) |
| P-IMR-20 | Lower-case / NUL callsigns | test_imrs::test_imrs_callsign_case | dev | probe | covered |
| P-IMR-21 | Lost terminator: timeout, terminators | test_imrs::test_imrs_lost_terminator | spec (B3) | probe | covered |
| P-IMR-22 | Two repeaters at once: one winner | test_imrs::test_imrs_two_talkers | spec (B1) | probe | covered |
| P-IMR-23 | IMRS <-> DMR passthrough (P-IMR-12); D-STAR / M17 differential | test_imrs::test_imrs_cross_mode[dstar\|m17] | diff | probe | covered |
