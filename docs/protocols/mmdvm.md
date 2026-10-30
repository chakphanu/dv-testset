<!-- SPDX-License-Identifier: MIT -->
# MMDVM Homebrew / DMR: obligations of the master (reflector) side

What a reflector acting as an MMDVM Homebrew **master** owes an MMDVM
Homebrew client on UDP 62030. Today that client is DMRGateway (its XLX
mode in particular): MMDVMHost dropped its own Homebrew client in 2023
and talks to DMRGateway instead. Each item is numbered `P-DMR-NN`; the
conformance cases are in `docs/testlist.md` section K1.

Every row says where the obligation comes from, in separate columns:
the spec text (with version and section), what the other masters and the
client do (project at a commit, behaviour in our own words, no code
copied), and what xlxd-ref did on the rig (measured 2026-10-30; all 51
cases passed with sut == ref; P-DMR-43 added later that day, 2 cases passed).

**Level**: MUST / SHOULD only where a spec says so; `convention (impl)`
where only implementations back it, naming the ones that agree;
`disputed` where masters differ, naming who does what. `convention (XLX
family)` marks routing rules that xlxd and URFd share and DMRGateway's
XLX mode relies on; the TG-bridge masters (HBlink, FreeDMR) route by
their own rules and are listed for completeness.

## Sources

Specs and published notes (four-level ETSI clause numbers are written
`5.1.2/3`, i.e. sub-clause 3 of 5.1.2, so the PII scanner does not read
them as IPv4 addresses):

- **[ETSI-1]** ETSI TS 102 361-1 V2.7.1 (2026-05), DMR Air Interface
  protocol. https://www.etsi.org/deliver/etsi_ts/102300_102399/10236101/02.07.01_60/ts_10236101v020701p.pdf
- **[ETSI-2]** ETSI TS 102 361-2 V2.5.1 (2023-05), DMR voice and generic
  services and facilities. https://www.etsi.org/deliver/etsi_ts/102300_102399/10236102/02.05.01_60/ts_10236102v020501p.pdf
- **[ETSI-3]** ETSI TS 102 361-3 V1.3.1 (2017-10), DMR packet data
  protocol. https://www.etsi.org/deliver/etsi_ts/102300_102399/10236103/01.03.01_60/ts_10236103v010301p.pdf
- **[HB15]** "IPSC Protocol Specs for homebrew DMR repeater" (file title
  "DMRplus IPSC Protocol for HB repeater"), DL5DI, G4KLX, DG1HT, revision
  2015-07-26 (the last one; 7 pages, no clause numbers, cited by page
  heading). https://wiki.brandmeister.network/images/5/54/DMRplus_IPSC_Protocol_for_HB_repeater.pdf
  (behind a bot check; archived copy
  https://web.archive.org/web/20250315074550/https://wiki.brandmeister.network/images/5/54/DMRplus_IPSC_Protocol_for_HB_repeater.pdf).
  It describes the DMR+ variant: `MSTACK` (not `RPTACK`), `MSTPING` /
  `RPTPONG`, a 53-byte `DMRD` without BER/RSSI, no `RPTO`, 32-bit values in
  "on air / ETSI" (big endian) order. No spec of the MMDVM variant
  (`RPTACK`, `RPTPING`/`MSTPONG`, 55-byte `DMRD`, `RPTO`) was published; its
  reference is G4KLX's code.
- **[BMW]** BrandMeister Wiki, "Homebrew repeater protocol", revision
  oldid 18983, last edited 2026-01-06: two variants exist (DL5DI spec vs
  G4KLX MMDVMHost, "alienated" over time); BM corrections (port 62030, ping
  every 5-15 s, 306-byte text-form config) and extensions `RPTSBKN`,
  `RPTRSSI`, `RPTINTR` (no acknowledge).
  https://wiki.brandmeister.network/index.php/Homebrew_repeater_protocol
- Secondary, not normative: HBlink4 `docs/protocol.md` (the MMDVM variant
  as HBlink4 implements it); OK-DMR `homebrew-2015.md` @ `bb3f1c8e`
  (2021-11-05, a transcription of HB15 that differs from the PDF in
  places); MMDVMHost `DMRplus_startup_options.md` (RPTO option syntax).

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference master (xlxd-ref) |
| URFd | https://github.com/n7tae/urfd @ `bd0c114e` (2025-12-29) | XLX-family master |
| HB3 | https://github.com/HBLink-org/hblink3 @ `1db27305` (2025-10-27) | TG-bridge master |
| HB4 | https://github.com/n0mjs710/hblink4 @ `2348c3bc` (2026-10-21, v4.8.0) | TG-bridge master |
| FDMR | https://gitlab.hacknix.net/hacknix/FreeDMR @ `546165eb` (2026-10-17, v1.6.2) | TG-bridge master behind a per-peer proxy |
| GW | https://github.com/g4klx/DMRGateway @ `d751e20e` (2026-10-22) | client (DMRNetwork, XLX mode) |
| MH | https://github.com/g4klx/MMDVMHost @ `9384d683` (2026-10-18) | no Homebrew client since `391e382a` (2023-06-05); talks to DMRGateway |

## How the cases judge

`cases/proto/test_mmdvm.py` probes xlxd-ref and the system under test in
parallel; the Judged column of testlist K1 says how:

- **hb**: a spec MUST (Level column); asserted on both sides.
- **diff**: a convention the masters agree on, or an XLX-family convention;
  sut == ref.
- **dev**: disputed between masters, a convention without agreement, or
  an xlxd deviation from a spec. Under `DVT_SUT_PROFILE=xlxd` sut == ref
  (xlxd's behaviour is recorded as data); under another profile the
  profile's spec entry (`dvtest/specs/<profile>.py`, `"mmdvm"` dict
  `{"P-DMR-NN": {observed key: value}}`) decides, and the case skips with
  "spec pending" while there is none. A non-xlxd system is never forced to
  copy an xlxd choice on a disputed item.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

Lenient authentication (a zero RPTK hash acked) stays the default
expectation for every profile: the tester sends zero hashes and no RPTC;
a strict-hash probe is recorded, not failed, unless a profile spec asks for
strict. Egress embedded LC and sync patterns are checked passively by
dvcheck (testlist H3), not repeated here.

Conventions: ids and 32-bit control fields are big endian on the wire
unless noted. "Silently ignored" means no reply and no state change.

## 1. Login

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-01 | `RPTL` + id (8 B) from a known id is answered with `RPTACK` + 4-byte salt | MUST (HB15) | HB15 p.3 "The Login Protocol": MSTNAK, or ack + id + random 32-bit value (MMDVM variant: `RPTACK` + salt, no id; BMW "G4KLX MMDVMHost version") | agreed: HB3, HB4, FDMR, URFd send `RPTACK` + 4 B salt; GW matches the 6-byte tag only and takes the salt from bytes 6-9 | measured: `RPTACK` + 4 B salt; same salt on a second RPTL |
| P-DMR-02 | The salt is random, not a constant | MUST random (HB15); per login vs per start: free | HB15 p.3 ("a random 32-bit integer", used as a bit pattern) | agreed it varies: HB3, FDMR new salt per RPTL; HB4 per login (reused on retry); URFd one per process start | measured: one salt per start, reused for every login; changed across a restart |
| P-DMR-03 | `RPTL` from an id the master does not accept never logs in | MUST (HB15: MSTNAK + id) | HB15 p.3 | disputed reply: HB3, FDMR `MSTNAK` + id (FDMR also blacklists the IP 300 s); URFd silent for an id missing from the DMR-id database, MSTNAK when the gatekeeper denies it; HB4 acks any RPTL and refuses at RPTK | measured: id 0 gets no reply (silence, not MSTNAK) |
| P-DMR-04 | `RPTK` + id + SHA-256(salt + passphrase) (40 B) completes the login with `RPTACK`; a wrong hash gets `MSTNAK` | MUST (HB15); hash check: disputed | HB15 p.3 (hash over the salt bytes followed by the passphrase; MSTNAK on reject); BMW: BM's own variant sends hex strings | disputed: HB3, HB4, FDMR verify the hash and MSTNAK (FDMR may allow an empty passphrase); URFd does not check | measured: zero hash and wrong hash both acked (bare `RPTACK`) |
| P-DMR-05 | `RPTK` without a preceding `RPTL` | disputed | not stated (HB15 p.3 only gives the order) | HB3, HB4, FDMR `MSTNAK` (state check); URFd acks and logs the client in | measured: `RPTACK`, then MSTPONG answered (logged in) |
| P-DMR-06 | `RPTC` + id + config (302 B: callsign, frequencies, power, colour code, position, height, location, description, URL, software and package ids) is acknowledged | MUST (HB15) | HB15 p.5 "The Configuration Protocol" (field table, 302 B; master replies with ack + id); BMW: BM text form 306 B | agreed ack: HB3, HB4, FDMR `RPTACK` + id; URFd bare `RPTACK`; GW sends 8 + 294 B and checks only the 6-byte tag | measured: bare 6-byte `RPTACK` (also for RPTK and RPTO) |
| P-DMR-07 | `RPTC` of another size (short or long) | disputed | HB15 p.5 gives 302 B; nothing on other sizes | xlxd, URFd ack exactly 302 B only; HB3, HB4, FDMR do not check the size (state only) | measured: 301 and 303 B not acked, login kept |
| P-DMR-08 | Voice is routed for a client that never sent `RPTC` | disputed | HB15 p.5: the repeater sends its config after login (client duty; master gating not stated) | HB3, HB4, FDMR drop DMRD until RPTC; URFd routes without it | measured: routed (the tester relies on this) |
| P-DMR-09 | `RPTO` + id + options is answered with `RPTACK` | convention (impl, agreed) | not in HB15 or BMW; option syntax in MMDVMHost `DMRplus_startup_options.md` | HB3 (only once connected, i.e. after RPTC), HB4, FDMR `RPTACK` + id; URFd bare `RPTACK`, options ignored; GW sends RPTO only for configured options, never on its XLX link | measured: `RPTACK` |
| P-DMR-10 | Re-login (`RPTL`/`RPTK` again, same id and address) keeps one session; traffic once | convention (impl, agreed) | not stated | HB3, FDMR overwrite the entry; HB4 refreshes it; URFd refreshes the (id, address) client | measured: one MSTPONG per RPTPING, 36/36 frames once |

## 2. Keepalive and close

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-11 | `RPTPING` + id (11 B) from a logged-in client is answered with `MSTPONG` + id, id big endian | MUST (HB15: reply; 32-bit values in on-air, big endian order) | HB15 p.4 "The Keep-Alive Protocol" (names MSTPING/RPTPONG; MMDVM uses RPTPING/MSTPONG) and revision note 2015-07-26; BMW: ping every 5-15 s | reply agreed; id order disputed: HB3, HB4, FDMR echo the received bytes (big endian); URFd host order (little endian on x86); GW checks only the 7-byte tag and re-logs in after 60 s without one (DMRD does not reset that timer) | measured: MSTPONG, id little endian |
| P-DMR-12 | `RPTPING` from an address/id that is not logged in gets no `MSTPONG` | MUST (HB15: packets after a dead link get MSTNAK) | HB15 p.4 | agreed no MSTPONG: HB3, HB4, FDMR answer `MSTNAK`; URFd silent | measured: no MSTPONG, no MSTNAK (silent) |
| P-DMR-13 | A client silent past the keepalive timeout is dropped; what it is sent | drop: MUST (HB15); notification and timeout: disputed | HB15 p.4: the master marks the link dead and answers later packets with MSTNAK; MSTCL is defined for a master that shuts down; no timeout value (pings every minute) | HB3 sends nothing (5 s x 3 missed); HB4 `MSTNAK` (about 90-120 s); FDMR `MSTCL` + id after 30 s (its proxy: 3 bare MSTCL); URFd an empty datagram after 100 s | measured: MSTCL after 100.7 s (checked every 10 s) |
| P-DMR-14 | After the drop the client gets no further voice | MUST (HB15) | HB15 p.4 | agreed: all remove the peer | measured: 0 frames |
| P-DMR-15 | `RPTCL` + id (9 B) closes the session (no further traffic, a floor it held is released) | SHOULD (HB15: a closing repeater should send RPTCL; the master's action is implied) | HB15 p.4 | disputed: HB3, FDMR delete the peer and reply MSTNAK + id; HB4 deletes, no reply; URFd parses only 13 B and reads the id one byte early, so neither form closes; GW sends 9 B on shutdown, reflector switch and unlink | measured: neither 13 B nor 9 B closes (voice still heard, MSTPONG answered); DV-Router closes (B15) |
| P-DMR-16 | `RPTINTR` (17 B) gets no reply; the login stays | MUST no reply (BMW: "no acknowledge"); acting on it: optional | BMW "Incoming call interrupt" (RPTINTR + 8 hex id + slot) | agreed no reply: HB3, HB4 log it; FDMR proxy drops it; URFd treats it as an RSSI report and ignores it; GW never sends it | measured: no reply, MSTPONG still answered |

## 3. Malformed and unknown packets

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-17 | Unknown tags, short packets (3 B, truncated RPTL) and oversize packets get no reply; the master keeps serving | convention (impl, agreed) | not stated | HB3, HB4 log and ignore; FDMR drops; URFd ignores; GW logs unknown tags | measured: no reply to `XXXX`, 3 B, 7 B RPTL, 1400 B DMRD; MSTPONG still answered |
| P-DMR-18 | A `DMRD` of a size other than 55 B is not relayed | disputed | HB15 p.6 "The Data Protocol": 53 B; the MMDVM variant appends BER and RSSI (55 B) | xlxd, URFd exactly 55 B; HB4 at least 55; FDMR at least 53 (pads); HB3 no check; GW (as receiver) 55 or 71 B | measured: 54/56 B voice not relayed (the 55 B header and terminator were) |
| P-DMR-19 | `DMRD` from an address that is not logged in is not relayed | MUST (HB15: no data before login; dead link gets MSTNAK) | HB15 p.3-4 | agreed: HB3, FDMR drop silently; HB4 MSTNAK; URFd ignores | measured: 0 packets relayed |

## 4. DMRD layout (55 B)

Offsets: 0 `DMRD`, 4 sequence, 5-7 source id, 8-10 destination (TG), 11-14
repeater id, 15 flags (bit 7 slot 2, bit 6 private call, bits 5-4 frame
type: 0 voice, 1 voice sync, 2 data sync; bits 3-0 voice sequence A-F or
data type: 1 voice LC header, 2 terminator), 16-19 stream id, 20-52 the
33-byte DMR burst, 53 BER, 54 RSSI. HB15 p.6 lists the same fields in a
53-byte packet (no BER/RSSI); its printed bit masks do not match the MMDVM
wire layout above, which follows MMDVMHost/DMRGateway.

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-20 | Only slot 2 is a reflector slot: header, voice and terminator on slot 1 are ignored | convention (XLX family) | ETSI-1 4.2.2 / 5.1 define the two time slots; nothing on network use | xlxd, URFd slot 2 only; GW XLX mode maps its RF slot to slot 2 TG 9; HB3, HB4, FDMR route both slots by their bridge rules | measured: 0 packets |
| P-DMR-21 | Group calls are relayed; private-call (bit 6) voice is not | convention (XLX family) | ETSI-2 5.2.1 (group call), 5.2.2 (individual call); ETSI-1 9.3.11 (FLCO) | xlxd, URFd drop private voice (URFd still acts on private 4000/400x headers); GW links with private-call headers; HB3, HB4 route unit calls (map, opt-in); FDMR uses private calls as dial-a-TG control | measured: 0 voice frames (private voice after a group header: header and terminator relayed) |
| P-DMR-22 | A stream opens with a data-sync burst of data type 1 (voice LC header) with a BS or MS data sync pattern; a header with another sync opens nothing | form: MUST (ETSI); rejecting another sync: disputed | ETSI-1 5.1.2/2 (voice initiation), 7.1.1 (LC header in a general data burst), 9.1.1 table 9.2 (sync patterns), 9.3.6 table 9.22 (data type 1) | xlxd, URFd check BS/MS data sync on the header, but URFd then opens the stream on the following voice (late entry); HB3, HB4, FDMR do not check the sync | measured: MS data sync header 36/36 frames; header with BS voice sync 0 frames |
| P-DMR-23 | Voice comes as superframes of bursts A-F (A voice sync, B-F embedded signalling), three 72-bit AMBE+2 frames per burst; the master relays every frame unchanged | structure: MUST (ETSI); unchanged relay: convention (impl, agreed; HB15 allows FEC fixes) | ETSI-1 4.2.2 (three 72-bit vocoder frames + 48-bit sync), 5.1.2/1 (superframe), 7.1.3 (embedded LC); HB15 p.6 "DMR Data" | agreed AMBE passthrough: HB3, FDMR rewrite only the embedded LC when the TG changes (HB3 also on data bursts); URFd null embedded LC; xlxd re-encodes it | measured: 60/60 frames byte exact |
| P-DMR-24 | A stream ends with a data-sync burst of data type 2 (terminator with LC), passed to listeners | MUST (ETSI) | ETSI-1 5.1.2/3 (voice termination), 7.1.2, 9.3.6 | agreed | measured: listeners get it; a second talker 100 ms later is relayed whole |
| P-DMR-25 | Egress: header, voice A-F (A voice sync), terminator, slot 2, group call, one stream id per stream; destination TG 9 | structure: MUST (ETSI); TG 9: convention (XLX family, the client requires it) | ETSI-1 5.1.2/1-5.1.2/3 | xlxd, URFd always TG 9, slot 2, group; GW XLX mode accepts only slot 2 group TG 9 from the reflector (anything else logged as unexpected and dropped) and rewrites it to its RF slot/TG; HB3, HB4, FDMR write the target bridge TG | measured: structure ok (14 packets), dst TG 9 |
| P-DMR-26 | The egress sequence byte starts at 0 and counts up per stream | MUST (HB15: starts at zero for each transmission, wraps) | HB15 p.6 "Seq No" | agreed counting; URFd per-module counter (header 0); HB3, HB4, FDMR pass the ingress byte through | measured: first 0, consecutive |
| P-DMR-27 | Egress source id = the talker's radio id; egress repeater id field | source id: convention (impl, agreed); repeater id: disputed | HB15 p.6 (RptrId defined for the sending repeater only) | source id kept by all; repeater id: HB3, FDMR the receiving peer's id; HB4 the source repeater's id; URFd the talker's login (repeater) id | measured: src = radio id; repeater id = the source id, not the login id |
| P-DMR-28 | Egress stream id | disputed | HB15 p.6: stream id stays the same from PTT press to release | HB3, HB4, FDMR pass it through; URFd keeps 16 bits (upper bytes zero); xlxd folds 32 to 16 bits (high XOR low, 0 -> 1); DV-Router keeps it (B10) | measured: folded, not kept |

## 5. Talkgroup routing

No spec covers module selection; these are the XLX conventions DMRGateway's
XLX mode is built on (unlink 4000, modules 4001-4026, voice on slot 2 TG 9;
P-DMR-43 is the private-call form DMRGateway actually sends).

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-29 | A header to TG 4001..4026 links the client to module A..Z | convention (XLX family) | not stated | xlxd; URFd (only to an existing module, only when unlinked); GW sends 3 private-call headers + 2 terminators to 400x, no voice; HB3, HB4 no meaning; FDMR dial-a-TG by private call | measured: 4001 and 4026 link (group-call header) |
| P-DMR-30 | A header to TG 4000 unlinks; no further voice | convention (XLX family) | not stated | xlxd, URFd; GW sends 4000 to unlink and before a module change; FDMR private 4000 unlinks its dial-a-TG | measured: the leaver gets no voice, a linked control does |
| P-DMR-31 | A linked client talking to another module's TG stays on its module; a module change needs 4000 first | convention (XLX family) | not stated | xlxd, URFd; GW sends 4000 before a new 400x | measured: own module hears it, the other module does not |
| P-DMR-32 | TG 9 (or any TG that maps to no module) from a linked client goes to its module; from an unlinked client it is ignored | convention (XLX family) | not stated | xlxd, URFd; GW sends its XLX voice as TG 9 slot 2 | measured: linked heard, unlinked not |
| P-DMR-33 | A header links; an empty stream (header + terminator) is enough to join and is not relayed as voice | convention (XLX family) | not stated | xlxd; URFd acts on the header (private headers never open a stream); GW's link bursts are header + terminator only | measured: joined; listeners got 0 packets of the join |
| P-DMR-43 | DMRGateway's XLX link form is honoured: 3 private-call voice LC headers + 2 terminators (one stream id, no voice) to TG 4001..4026 link the module, to TG 4000 unlink; nothing of the burst is relayed | convention (XLX family) | not stated: ETSI-2 5.2.2 defines the individual (private) call and ETSI-1 9.3.11 its FLCO, nothing on using it as a network command; HB15 and BMW: no link commands | GW (DMRGateway.cpp writeXLXLink) sends slot 2, private-call flag, BS data sync, headers seq 0-2 then terminators seq 3-4, back to back, one random stream id, repeater id = its login id, src = its login id (startup link, relink) or the RF user's id (user control, private call to the XLX base + 0..26), 4000 first when already linked; xlxd (cdmrmmdvmprotocol.cpp IsValidDvHeaderPacket / OnDvHeaderPacketIn) and URFd (DMRMMDVMProtocol.cpp, same functions) derive link/unlink from the header's TG whatever the call type (link only while unlinked, 4000 only while linked), open no stream for a private header and ignore the terminators (no stream); later headers of the burst change nothing | measured: linked (36/36 frames heard) and unlinked (0 frames, linked control heard), with src = login id and src = another radio id; listeners got 0 packets of the burst |

## 6. Stream lifetime

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-34 | A stream whose terminator is lost ends after a silence timeout; the module is then free | convention (impl, agreed); timeout and closing terminator: differ (judged by B3) | not stated (ETSI-1 5.2.1/4 hang times are air-interface only) | xlxd, URFd 1.6 s; HB3, FDMR 0.36 s contention and 5 s cleanup; HB4 2 s | measured: module free within 2.2 s; listeners got a terminator |
| P-DMR-35 | A second header on a busy module is not mixed into the running stream | convention (impl, agreed); late-entry policy judged by B1/B2 | not stated | URFd one stream per module; HB3, FDMR contention window plus hang time; HB4 busy-slot check, 200 ms | measured: first stream whole, no mixing, late talker 0 frames |
| P-DMR-36 | Voice bursts whose stream id had no header are not relayed | disputed (ETSI provides late entry) | ETSI-1 4.3 and 7.1.3 (embedded LC "to facilitate late entry"), 5.1.2/2 (trunked voice without header) | URFd builds a header from the voice and opens the stream; HB3, HB4 synthesise the LC and relay; FDMR not checked | measured: 0 frames (xlxd only buffers them for late-entry promotion) |
| P-DMR-37 | The talker never receives its own stream back | convention (impl, agreed) | not stated | HB3, HB4, FDMR skip the source system; URFd skips the stream owner | measured: 0 packets to the talker |
| P-DMR-41 | Data-sync bursts that are not voice (CSBK, data header, rate 1/2, 3/4: data type >= 3) open no voice stream; whether they or a following headerless voice are relayed | non-voice: MUST (ETSI); forwarding: disputed | ETSI-1 9.3.6 table 9.22, 7.2.1 (CSBK), 8.2.1 (data headers), 9.2.2 / 9.2.7 (rate 3/4, 1/2); ETSI-3 5.3.1 | xlxd, URFd, HB4 do not forward data (URFd opens a stream on the voice that follows); HB3 forwards data (and overwrites part of it with embedded LC); FDMR forwards group data | measured: 0 packets (data and the voice after it) |
| P-DMR-42 | The ingress sequence byte wraps 255 -> 0 in a long over without loss or a new stream | MUST (HB15: wraps to zero) | HB15 p.6 "Seq No" | agreed: xlxd, URFd ignore the byte; HB3, HB4 track streams by stream id | measured: 120/120 frames, one stream, one header |

## 7. Several clients

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DMR-38 | Two ids from one IP (different ports) are two sessions, each gets the module's traffic once | convention (impl, agreed) | not stated | HB3, HB4 key by id and check the address; FDMR one backend per id; URFd keys by (id, address) | measured: 36/36 frames once each |
| P-DMR-39 | The same id from two ports | disputed | not stated | URFd two sessions keyed by (id, address); HB3 a re-login overwrites (last address wins); HB4 the newcomer must pass a takeover login, then the old address gets MSTNAK; FDMR's proxy follows the latest address; GW re-binds a random port after every reconnect (Local=0) | measured: two sessions, 36/36 frames once each |
| P-DMR-40 | A keepalive from one client never refreshes another client's session | convention (impl, agreed) | not stated | all keep per-peer timers | measured: the pinging client kept voice and MSTPONG while another was dropped |
