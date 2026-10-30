<!-- SPDX-License-Identifier: MIT -->
# DMRplus "dongle" (UDP 8880): obligations of the reflector side

What a reflector owes a DMRplus dongle client on UDP 8880: openSPOT's
"DMRplus Dongle" connector and the DV4mini Control Center (older
releases), both closed source. The same protocol is served by DMR+ IPSC2
masters for hotspots (port 8880 in network setup pages). xlxd and URFd
serve it as "DMRPlus (dongle)" / "DMR+ DMO mode". Each item is numbered
`P-DMP-NN`; the cases are in `cases/proto/test_dmrplus.py` (markers
`proto`, `rig`), rows at the end of this file.

This is **not** the DMR+ variant of the Homebrew protocol (RPTL, MSTACK,
MSTPING/RPTPONG, 53-byte DMRD, RPTO options) that DMRGateway, DroidStar
and MMDVM_Bridge speak toward IPSC2 masters on UDP 55555; that one is in
[HB15] and in `mmdvm.md`. The 8880 protocol has no login, no RPTO and no
DMRD: a 31-byte connect/keepalive in ASCII and 72-byte voice packets
carrying one DMR burst. No document of its wire format was found
(DMR+ / IPSC2 / DV4mini sites, HB15, SharkRF material); the only full
descriptions are the reflector implementations, so most items are
`convention (impl)`. ETSI TS 102 361 defines the burst content carried
inside (sync, LC header, embedded LC, terminator), which grounds the
MUST rows.

Every row says where the obligation comes from: the spec text (version
and section), what the clients send / expect (only their documented
settings are known), what the other servers do (project at a commit,
behaviour in our own words, no code copied), what xlxd-ref does, and
what DV-Router should do. The xlxd column was measured on 2026-10-30
(`out/20261030T083029Z-i10`, instance 10, all 39 cases passed with sut ==
ref; P-DMP-03 in its own run `out/20261030T083817Z-i10` with a
blacklisted id).

**Level**: MUST / SHOULD only where a spec says so; `convention (impl)`
where only implementations back it, naming the ones that agree;
`disputed` where they differ, naming who does what.

## Sources

Specs and documents (clause numbers with four levels are written
`5.1.2/3`, i.e. sub-clause 3 of 5.1.2, so the PII scanner does not read
them as IPv4 addresses):

- **[ETSI-1]** ETSI TS 102 361-1 V2.7.1 (2026-05), DMR Air Interface
  protocol: 4.2.2 (burst: 108 + 48 + 108 bits), 5.1.2/1 (superframe),
  5.1.2/2 (voice initiation), 5.1.2/3 (voice termination), 7.1.1 (voice
  LC header), 7.1.2 (terminator with LC), 7.1.3 (embedded signalling),
  9.1.1 table 9.2 (sync patterns), 9.1.2 (EMB), 9.3.1 (colour code), 9.3.6
  table 9.22 (data type), B.1.1 (BPTC(196,96)).
  https://www.etsi.org/deliver/etsi_ts/102300_102399/10236101/02.07.01_60/ts_10236101v020701p.pdf
- **[ETSI-2]** ETSI TS 102 361-2 V2.5.1 (2023-05): 5.2.1 group call,
  5.2.2 individual call.
  https://www.etsi.org/deliver/etsi_ts/102300_102399/10236102/02.05.01_60/ts_10236102v020501p.pdf
- **[HB15]** "IPSC Protocol Specs for homebrew DMR repeater", DL5DI,
  G4KLX, DG1HT, revision 2015-07-26 (7 pages; see `mmdvm.md` Sources for
  URLs). Not about 8880; its only related note (p. 7 "Additional
  Information") lists system ids 901xxx for dongle and hotspot networks
  ("901004 for DV4 dongle network").
- **[OSPAPI]** SharkRF openSPOT HTTP API, `dmrplussettings.cgi`
  (server_host, port 8880, dmr_id, reflector_id, keepalive_interval_sec 1,
  rx_timeout_sec 10), https://github.com/sharkrf/osp-http-api @ `cd7877db`
  (2019-03-13), README "DMRplus" sections.
- **[OSPMAN]** SharkRF openSPOT4 manual, "DMRplus dongle connector"
  (2023): networks DMRplus, Phoenix, DMR-MARC, XLX; reflector change by a
  short private call to 4000-5000.
  https://manuals.sharkrf.com/openspot4/en/dmrplus-dongle-connector.html
- **[DV4MAN]** DV4mini manual EN v2.0, 2016-07-23 (DMR reflectors
  4000-5000, 4000 = none; DMRPLUS module by DG1HT; no wire format).
  https://www.qsl.net/oz1dgq/ (DV4mini manual)
- **[NETS]** Network setup pages naming the dongle port: AREC NZ DMR+
  "openSPOT DMRplus Dongle connector, port 8880"
  (https://arec.site/dmrplus-openspot-setup/); VK-DMR+ master port 8880
  for hotspots (https://lyonscomputer.com.au/Digital-Modes/DMR/DMR/DMR.html,
  Ver gnl20190313); BrandMeister server configuration with a "DV4mini"
  block on 8880 (https://brandmeisternetwork.blogspot.com/2017/03/mmdvm-homebrew-dv4mini-brandmeister.html).
- **[XREADME]** xlxd README: "DMRPlus (dongle)", "8880 UDP DMR+ DMO mode"
  (https://github.com/LX3JL/xlxd); URFd README "UDP port 8880 (DMR+ DMO
  mode)" (https://github.com/n7tae/urfd).

Implementations read (behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference server (xlxd-ref); xlxd-sut (`d8ffa41`) has identical DMR+ files |
| URFd | https://github.com/n7tae/urfd @ `bd0c114` (2025-12-29) | server (DMR+ always on, port from `[DMRPlus] Port`, default 8880) |
| OSP | openSPOT 1-4 firmware, closed; settings from [OSPAPI], [OSPMAN] | client |
| DV4 | DV4mini Control Center (older releases), closed; [DV4MAN] | client (later releases moved to MMDVM/Homebrew) |
| IPSC2 | DMR+ IPSC2 masters, closed | server (8880 per [NETS]) |
| GW / DS | g4klx/DMRGateway @ `d751e20e`, nostar/DroidStar @ `becf1ded` | not clients of 8880: Homebrew + RPTO to 55555 (context only) |

## How the cases judge

`cases/proto/test_dmrplus.py` probes xlxd-ref and the system under
test in parallel on UDP 8880, with DMR+ clients built from
`dvtest/proto/dmrplus.py` and MMDVM / D-STAR clients as listeners and
talkers:

- **hb**: a spec MUST (ETSI burst content). P-DMP-23 / 24 / 25 are MUST
  rows xlxd fails by design: under the xlxd profile sut == ref (the
  deviation recorded), under another profile the ETSI value.
- **diff**: a convention the servers agree on; sut == ref.
- **dev**: disputed, or xlxd deviates from ETSI: sut == ref under
  `DVT_SUT_PROFILE=xlxd`, else the profile's `"dmrplus"` spec entry
  (`{"P-DMP-NN": {observed key: value}}`), skip "spec pending" without one.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

DV-Router's `dmrplus` edge is not built yet: under a non-xlxd profile the
cases skip "edge not ready" until `SPEC["dmrplus"]["server"]` is True
(same pattern as P25).

Conventions: the packet trailer ids are 32-bit little endian (24 bits
used); the 33-byte DMR burst is carried with each 16-bit pair swapped.

### Packet layouts (from the servers' code)

Connect / keepalive, 31 B: 0-7 DMR id in ASCII decimal; 8-11 reflector
number in ASCII (4001..4026 = module A..Z); 12-30 not read by either
server. Disconnect, 32 B: 0-7 DMR id in ASCII; 11 module as a digit
('0' = A); not compared by either server. ACK `ACK OK\n\0`, NAK
`NAK OK\n\0` (8 B each).

Voice, 72 B:

| Offset | Size | Field |
|---|---|---|
| 4 | 1 | sequence (egress: 4 after the header, then +1 mod 256; ignored on ingress) |
| 8 | 1 | type: 2 voice LC header, 1 voice, 3 voice terminator |
| 16-17 | 2 | slot, 0x2222 = TS2 |
| 18-19 | 2 | slot type: 0xEEEE header; voice burst A..F = 0x77..0xCC |
| 20-21 | 2 | colour code, 0x1111 = CC 1 |
| 22-23 | 2 | frame type (0x1111) |
| 26-59 | 34 | the 33-byte burst + 1 pad byte, 16-bit pairs swapped |
| 62 | 1 | call type, 1 = group |
| 64-67 | 4 | destination (TG), little endian |
| 68-71 | 4 | source DMR id, little endian |

## 1. Connect, keepalive, disconnect

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMP-01 | A 31-byte connect (ASCII DMR id + reflector number 4001..4026) from an id the server knows links the address to that module and is answered `ACK OK\n\0` | convention (impl, agreed) | none (no published wire format) | OSP: settings dmr_id + reflector_id, port 8880 [OSPAPI]; DV4: reflector 4000-5000 [DV4MAN] | URFd same packet and ACK | measured: `ACK OK\n\0`, the client hears an MMDVM talker on its module | ACK and link |
| P-DMP-02 | A connect from an id missing from the id database gets no reply and links nothing | convention (impl, agreed) | none | OSP: rx_timeout 10 s, behaviour on no reply unknown | URFd same (no callsign, packet invalid) | measured: no reply to id 1023999 | same (silence) |
| P-DMP-03 | A connect the access list refuses is answered `NAK OK\n\0` | convention (impl, agreed) | none | unknown | URFd NAK on whitelist / blacklist refusal | measured: `NAK OK\n\0` for a blacklisted callsign (separate run, `DVT_XLXD_BLACKLIST`) | NAK |
| P-DMP-04 | A connect to a reflector number that maps to no module | disputed | none | OSP may send 4000-5000 (DMR+ reflector range) | URFd ACK and a client without a module (its streams fail); xlxd ACK then NAK, no client | measured: 4000 and 4500 both get ACK then NAK; nothing heard | NAK only, no link |
| P-DMP-05 | The client repeats the 31-byte packet as its keepalive (every 1 s); each one is ACKed; the server sends no keepalive of its own | convention (impl, agreed) | none | OSP keepalive_interval_sec 1 [OSPAPI] | URFd same | measured: 3/3 keepalives ACKed; 0 packets in 3 s of client silence | same |
| P-DMP-06 | A keepalive / connect with another reflector number from a linked client | disputed (client behaviour unknown) | none | OSP changes reflector with a private call to 4000-5000 [OSPMAN]; how that reaches the server is not documented | URFd and xlxd keep the old module (the field is ignored for a known client) | measured: a keepalive naming J: the client stays on H (hears H, not J) | follow the reflector number (move the client), recorded until client captures exist |
| P-DMP-07 | A client silent for 10 s (no keepalive) is dropped; voice does not refresh it, a client that is talking is kept; nothing is sent on drop | convention (impl, agreed) | none | OSP rx_timeout_sec 10 [OSPAPI] | URFd same (10 s, checked every 1 s) | measured: heard 6 s after the connect, dropped by 13 s; nothing sent on drop | drop after 10 s, no packet |
| P-DMP-08 | A dropped client gets no further voice | convention (impl, agreed) | none | - | URFd same | measured: 0 frames after the drop | same |
| P-DMP-09 | A 32-byte disconnect from a linked address unlinks it; no reply | convention (impl, agreed) | none | unknown whether clients send it | URFd same (address only) | measured: no reply; 0 frames afterwards | same |
| P-DMP-10 | A disconnect from an address that is not linked is ignored | convention (impl, agreed) | none | - | URFd same | measured: no reply; a linked client still hears its module | same |

## 2. Voice from clients (ingress)

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMP-11 | A stream opens with a type-2 packet (voice LC header) from a linked client, TS2, CC 1, group call, source id known | header form: MUST (ETSI-1 5.1.2/2, 7.1.1); the gating fields: convention (XLX family) | ETSI-1 5.1.2/2, 7.1.1 | OSP / DV4: DMO dongles, TS2 CC1 (DV4 "all DMR CC set to 01" in later releases) | URFd same checks | measured: 36 frames + header + terminator: 39 frames at an MMDVM listener | open on the header |
| P-DMP-12 | Slot 1 packets (slot field not 0x2222) are ignored | convention (XLX family) | ETSI-1 4.2.2 (two slots; nothing on network use) | dongles are single slot | URFd same | measured: 0 frames | same |
| P-DMP-13 | A colour code other than 1 is ignored | convention (impl, agreed) | ETSI-1 9.3.1 (CC is local to the RF channel) | dongles fixed at CC 1 | URFd same | measured: 0 frames | same |
| P-DMP-14 | Private-call voice (call type not 1) is not relayed | convention (XLX family) | ETSI-2 5.2.2 | OSP uses short private calls to 4000-5000 as reflector commands [OSPMAN] | URFd same | measured: 0 frames | not relayed as voice |
| P-DMP-15 | The module of the stream: the header's TG or the linked module | disputed | none | the client talks to its reflector | xlxd opens on the header's TG (a client linked to B can talk into C by TG 4003); URFd opens on the TG but tags frames with the linked module | measured: a client linked to H talking to 4010: heard on J, not on H | the linked module (as MMDVM P-DMR-31) |
| P-DMP-16 | A header with a source id missing from the id database | disputed | none | dongle users are registered DMR ids | URFd and xlxd drop the stream | measured: source 1023999: not relayed | relay with the numeric id (callsign from the database when known) |
| P-DMP-17 | The three AMBE+2 frames of each burst reach listeners unchanged | MUST (ETSI-1 4.2.2: 3 x 72-bit vocoder frames); layout: convention | ETSI-1 4.2.2, 6.1 | - | URFd reads byte 32 of the burst from wire offset 58 (and swaps past its buffer) | measured: layout written by the server (byte 32 at wire 59): the last byte of every frame C lost (12/12); layout read by the server (byte 32 at 58): exact | read the burst with the same pair-swapped 34-byte layout the server writes (byte 32 at wire 59) |
| P-DMP-18 | Voice packets without a header (no stream) are not relayed | disputed (ETSI provides late entry) | ETSI-1 4.3, 7.1.3 (embedded LC for late entry) | - | URFd drops them | measured: 0 frames | as MMDVM P-DMR-36 (B16) |
| P-DMP-19 | A type-3 packet (voice terminator) ends the stream and frees the module at once | MUST (ETSI-1 5.1.2/3, 7.1.2: a terminator ends the transmission) | ETSI-1 5.1.2/3 | dongles send a terminator at PTT release (DMR air behaviour) | URFd: ends only by the 1.6 s timeout | measured: the MMDVM listener gets a terminator, but an MMDVM talker 0.3 s later is not heard (module held for the 1.6 s timeout) | end the stream at once; listeners get the terminator |
| P-DMP-20 | A second header within a running stream does not open a new one | convention (impl) | ETSI-1 5.1.2/2 | - | URFd retries the open on every header (fails, logs again) | measured: 39 frames, one header at the listener | keep the stream |
| P-DMP-21 | The talker never receives its own stream back | convention (impl, agreed) | none | - | URFd skips clients that are talking | measured: 0 packets back to the talker; another dongle hears it | no echo |

## 3. Voice to clients (egress)

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMP-22 | A stream from another client starts with a type-2 header: TS2, CC 1, group call, destination 4001 + module, source = the talker's DMR id | fields: convention (XLX family) | ETSI-1 5.1.2/2 | the dongle shows the source id | URFd same | measured: TS2, CC 1, group, dst 4008 (module H), src 1023007; a D-STAR talker without DMR id: src 0 | same; an unknown talker gets one configured gateway id, not 0 |
| P-DMP-23 | The header's burst is a real voice LC header (BPTC(196,96) LC with FLCO group, TG, source; data sync; slot type data type 1) | MUST (ETSI-1 7.1.1, 9.3.6, B.1.1); xlxd and URFd do not meet it (recorded upstream deviation) | ETSI-1 7.1.1, 9.1.1, 9.3.6 | radios behind an openSPOT need the LC to show the call | URFd same as xlxd | measured: header burst `f009c001f000` in the centre, slot type data type 0, LC all zero: not a voice LC header (the decoder reads xlxd's MMDVM egress header correctly) | a real voice LC header burst |
| P-DMP-24 | Voice bursts A-F: A with voice sync, B-F with EMB (CC 1) and the embedded LC fragments | sync / EMB: MUST; embedded LC content: MUST (ETSI-1 7.1.3); xlxd and URFd do not meet it (recorded upstream deviation) | ETSI-1 5.1.2/1, 7.1.3, 9.1.1, 9.1.2 | late entry on RF needs the embedded LC | URFd same as xlxd | measured: A carries BS voice sync, B-F valid EMB with CC 1, embedded LC all zero (checksum of a zero LC) | real embedded LC |
| P-DMP-25 | The stream ends with a type-3 terminator carrying LC | MUST (ETSI-1 5.1.2/3, 7.1.2); xlxd and URFd do not meet it (recorded upstream deviation) | ETSI-1 7.1.2 | dongles end the RF call on it; without it they wait for their own timeout | URFd never sends one | measured: no type-3 packet; the stream just stops | send the terminator |
| P-DMP-26 | One voice packet per burst (three frames, 60 ms); sequence byte 4 after the header, +1 mod 256 | convention (impl, agreed) | ETSI-1 5.1.2/1 (60 ms burst period) | - | URFd same | measured: sequence 4 for the first burst, steps of 1 (one step of 2 after the header); at most 2 packets in any 40 ms | one packet per 60 ms, counting sequence |
| P-DMP-27 | The burst on the wire: 33 bytes + 1 pad, 16-bit pairs swapped; ids little endian | convention (impl, agreed) | none | unknown (no capture of a real client) | URFd same | measured: MMDVM -> DMR+ 36/36 frames exact | same (bit-exact with xlxd) |

## 4. Malformed input

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMP-28 | Datagrams of another size than 31, 32 or 72 get no reply; the server keeps serving | convention (impl, agreed) | none | - | URFd logs and ignores (both read byte 8 of packets shorter than 9 bytes) | measured: no reply to 3, 30, 73 or 1400 B; keepalive still ACKed | ignore |
| P-DMP-29 | A 72-byte packet of a type other than 1, 2, 3 is ignored | convention (impl, agreed) | none | - | URFd same | measured: 5 type-9 packets + headerless voice: 0 frames | ignore |
| P-DMP-30 | Voice from an address that is not linked is not relayed | convention (impl, agreed) | none | - | URFd same (but updates its last-heard list) | measured: 0 frames | drop |

## 5. Several clients, other modes

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMP-31 | Two clients on one IP (two ports) are two sessions, each gets the traffic once | convention (impl, agreed) | none | several dongles behind one NAT | URFd keys by address:port | measured: 36/36 frames to each of two dongles on one IP | same |
| P-DMP-32 | The same DMR id from two addresses: two clients | convention (impl, agreed) | none | - | URFd same | measured: 36/36 frames to each of two ports with one id | same |
| P-DMP-33 | DMR+ <-> MMDVM (both AMBE+2): frames pass unchanged in both directions | convention (XLX family) | ETSI-1 4.2.2 | - | URFd same codec, no transcoding | measured: see P-DMP-17 (to MMDVM) and P-DMP-27 (from MMDVM) | bit-exact passthrough |
| P-DMP-34 | DMR+ <-> D-STAR / M17 / P25 / NXDN through the transcoder | convention (XLX family) | - | - | URFd on transcoded modules | measured: sut == ref: D-STAR 38 frames, M17 40 frames, same sha256 | as the matrix |
| P-DMP-35 | One talker per module: a second DMR+ header on a busy module is not mixed in | convention (impl); policy judged by B1 | none | - | URFd one stream per module | measured: the first (MMDVM) stream whole (120/120), the DMR+ header 1 s in not mixed; 12 of its 18 frames reach the listener after the first stream ends | B1 |

## Testlist rows

Rows for a K section of `docs/testlist.md` (the lead merges them). Covered
= passed on 2026-10-30 (`out/20261030T083029Z-i10`; P-DMP-03 in
`out/20261030T083817Z-i10`). Under a non-xlxd profile the cases skip
"edge not ready" until `SPEC["dmrplus"]["server"]` is True.

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DMP-01 | Connect -> ACK, linked to 4001 + n | test_dmrplus::test_dmp_connect_ack | diff | probe | covered |
| P-DMP-02 | Connect from an unknown id: no reply | test_dmrplus::test_dmp_connect_unknown_id | diff | probe | covered |
| P-DMP-03 | Refused connect -> NAK | test_dmrplus::test_dmp_connect_refused | diff | probe (needs `DVT_XLXD_BLACKLIST=N0DMP`, see Rig needs) | covered |
| P-DMP-04 | Connect to an unmapped reflector number | test_dmrplus::test_dmp_connect_no_module[4000\|4500] | dev | probe | covered |
| P-DMP-05 | Keepalive ACKed, no server keepalive | test_dmrplus::test_dmp_keepalive_ack | diff | probe | covered |
| P-DMP-06 | Keepalive with another reflector number | test_dmrplus::test_dmp_keepalive_other_module | dev | probe | covered |
| P-DMP-07 | Silent client dropped after 10 s, nothing sent | test_dmrplus::test_dmp_silent_client_dropped | diff | probe | covered |
| P-DMP-08 | Dropped client gets no voice | test_dmrplus::test_dmp_dropped_client_no_voice | diff | probe | covered |
| P-DMP-09 | Disconnect unlinks, no reply | test_dmrplus::test_dmp_disconnect | diff | probe | covered |
| P-DMP-10 | Disconnect from an unknown address ignored | test_dmrplus::test_dmp_disconnect_unknown | diff | probe | covered |
| P-DMP-11 | Header opens a stream (DMR+ -> MMDVM listener) | test_dmrplus::test_dmp_header_opens_stream | hb | probe | covered |
| P-DMP-12 | Slot 1 ignored | test_dmrplus::test_dmp_ingress_filtered[slot1] | diff | probe | covered |
| P-DMP-13 | Colour code other than 1 ignored | test_dmrplus::test_dmp_ingress_filtered[cc2] | diff | probe | covered |
| P-DMP-14 | Private-call voice not relayed | test_dmrplus::test_dmp_ingress_filtered[private] | diff | probe | covered |
| P-DMP-15 | Header TG of another module | test_dmrplus::test_dmp_header_other_module | dev | probe | covered |
| P-DMP-16 | Header with an unknown source id | test_dmrplus::test_dmp_unknown_source | dev | probe | covered |
| P-DMP-17 | AMBE+2 frames unchanged, DMR+ -> MMDVM (last byte of frame C recorded) | test_dmrplus::test_dmp_frames_exact_to_mmdvm | dev | probe | covered |
| P-DMP-18 | Voice without header not relayed | test_dmrplus::test_dmp_no_header_not_relayed | dev | probe | covered |
| P-DMP-19 | Terminator ends the stream at once | test_dmrplus::test_dmp_terminator_frees_module | dev | probe | covered |
| P-DMP-20 | Repeated header keeps the stream | test_dmrplus::test_dmp_repeated_header | diff | probe | covered |
| P-DMP-21 | No echo to the talker | test_dmrplus::test_dmp_no_echo | diff | probe | covered |
| P-DMP-22 | Egress header fields (TS2, CC1, group, 4001 + n, source id) | test_dmrplus::test_dmp_egress_header_fields[known\|unknown] | diff + dev (src) | probe | covered |
| P-DMP-23 | Egress header burst is a real voice LC header | test_dmrplus::test_dmp_egress_lc_header | hb (xlxd recorded) | probe | covered |
| P-DMP-24 | Egress A sync, B-F EMB + embedded LC | test_dmrplus::test_dmp_egress_embedded_lc | hb (xlxd recorded) | probe | covered |
| P-DMP-25 | Egress terminator with LC | test_dmrplus::test_dmp_egress_terminator | hb (xlxd recorded) | probe | covered |
| P-DMP-26 | Egress pacing and sequence | test_dmrplus::test_dmp_egress_sequence | diff | probe | covered |
| P-DMP-27 | Egress burst layout, MMDVM -> DMR+ frames exact | test_dmrplus::test_dmp_frames_exact_from_mmdvm | diff | probe | covered |
| P-DMP-28 | Wrong-size datagrams ignored | test_dmrplus::test_dmp_garbage_ignored[short\|30\|73\|oversize] | diff | probe | covered |
| P-DMP-29 | Unknown 72-byte type ignored | test_dmrplus::test_dmp_unknown_type | diff | probe | covered |
| P-DMP-30 | Voice from an unlinked address not relayed | test_dmrplus::test_dmp_unlinked_sender | diff | probe | covered |
| P-DMP-31 | Two ports on one IP: two sessions | test_dmrplus::test_dmp_two_clients[two_ports] | diff | probe | covered |
| P-DMP-32 | Same id from two addresses | test_dmrplus::test_dmp_two_clients[same_id] | diff | probe | covered |
| P-DMP-33 | DMR+ <-> MMDVM passthrough | test_dmrplus::test_dmp_frames_exact_to_mmdvm / _from_mmdvm | dev / diff | probe | covered |
| P-DMP-34 | DMR+ <-> D-STAR / M17 cross-mode | test_dmrplus::test_dmp_cross_mode[dstar\|m17] | diff | probe | covered |
| P-DMP-35 | Second header on a busy module not mixed | test_dmrplus::test_dmp_busy_module_no_mix | spec (B1) | probe | covered |

## Where the recommendation differs from xlxd

- P-DMP-04: NAK only (xlxd sends ACK, then NAK).
- P-DMP-06: follow a new reflector number from a linked client.
- P-DMP-15: route by the linked module, not by the header's TG.
- P-DMP-16: relay a talker whose id is not in the database.
- P-DMP-17: read the burst with the same layout the server writes.
- P-DMP-19: end the stream on the terminator.
- P-DMP-22: a configured gateway id instead of source 0.
- P-DMP-23 / 24 / 25: real voice LC header, embedded LC and terminator
  with LC on egress (ETSI); xlxd and URFd send none of them.

## Rig needs

- None for the suite: xlxd-ref (.10) and xlxd-sut (.11) bind UDP 8880 with
  the rig's empty access lists. Connect and voice work only for ids in
  `rig/config/dmrid.dat` (1023007 N0CALL, 1023008 N0TEST, 1023101..
  STR001..); 1023999 is the unknown id.
- P-DMP-03 runs on its own: `DVT_XLXD_BLACKLIST=N0DMP` with the
  `rig/config/dmrid.dat` row `1023230;N0DMP;dvt` (no other suite uses
  that id or callsign; the blacklist then never touches the ids the other
  suites use). The case takes any blacklisted callsign that has a dmrid.dat
  row (N0DMP first) and skips without one; it was verified with STR120
  before the row existed.
- Every test socket uses its own source port (on this subnet xlxd derives
  the DMR+ stream id from the source port only).
