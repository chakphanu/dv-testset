<!-- SPDX-License-Identifier: MIT -->
# IPSC1 phase 0: the IPSC protocol, a simulated repeater, the hardware risk

Offline preparation for black-box tests of an IPSC edge (Motorola MOTOTRBO
IP Site Connect). Nothing here ran on the rig, no DV-Router code was read,
nothing was captured: every field below comes from two public
reverse-engineering projects and is cited so that it can be checked.
Status: **not validated on hardware, not validated on the rig**. What is
checked is the packet layer (`dvtest/proto/ipsc.py`, 28 unit tests in
`cases/unit/test_ipsc.py`) against the published sample packets.

## 0. Sources, pins, licence

| Short name | Repository | Pinned commit | Commit date | Licence |
|------------|------------|---------------|-------------|---------|
| DMRlink | github.com/n0mjs710/DMRlink | `1f50db23d36f54574ea9c4318f33d064e420264e` | 2026-05-17 | GPL-3.0 (`LICENSE.txt`) |
| ipsc2hbp | github.com/n0mjs710/ipsc2hbp (Python; the C port `ipsc2hbpc` is "production", this one is "the development version" per its README) | `13abe77ebb61a59edb2fefe0f1f88c155b7f1e32` | 2026-10-21 | GPL-3.0-or-later (README notice, no `LICENSE` file in the tree) |

Both are shallow clones in `.cache/ipsc-src/<name>` (git-ignored, not
committed). They are **test references only**: no code or text of either is
in a committed file; field names, offsets and function names are cited in
prose. The few hex values in `cases/unit/test_ipsc.py` that are measurements
published in the DMRlink documentation (one HMAC digest, four RS(12,9)
parities) are marked there. ipsc2hbp itself cites a third source,
node-dmr-lib (rick51231), which was **not** fetched; statements that come
from it are marked "second hand".

The ipsc2hbp README states: IPSC is proprietary to Motorola Solutions, only
Group and Private (unit-to-unit) voice is supported, and no one should ask
for features that need information not obtained legally. This document
adds none; it records what the two projects already publish.

Citation form: `file` `function` (line at the pinned commit). Paths are
relative to the repository root. `P` = `ipsc2hbp/`, `D` = `DMRlink/`.

## 1. The protocol

### 1.1 Transport and authentication

- UDP only. The master listens on one configured port (50000 in both
  samples: `D/dmrlink_SAMPLE.cfg` `PORT`, `P/ipsc2hbp.toml.sample`
  `bind_port`); peers use their own source port and are reached at the
  address and port the master saw (`P/ipsc/protocol.py`
  `IPSCMasterProtocol._send_peer_list` L256, `_on_reg_req` L183).
- Master and peers form a **full mesh**: the master only bootstraps the
  membership; voice goes peer to peer (`D/DO_NOT_README.md` "CORE CONCEPTS";
  `P/ipsc/protocol.py` `IPSCPeerProtocol` L437 and `_service_mesh_peers`
  L667). At most 15 nodes including the master (`P/ipsc/protocol.py`
  `_MAX_PEERS = 14` L82 comment).
- Authentication is optional and all-or-nothing per system. The key is 20
  bytes, written as up to 40 hex characters and left padded with zeros
  (`D/ipsc/dmrlink_config.py` `AUTH_KEY ... rjust(40,'0')` L138;
  `P/config.py` L178-188). The digest is the **first 10 bytes of
  HMAC-SHA1(key, packet)**, appended after the last field of the packet
  (`D/dmrlink.py` `hashed_packet` L654, `validate_auth` L665, `send_packet`
  L491; `P/ipsc/protocol.py` `_check_auth` L353, `_auth_suffix` L361). The
  FLAGS byte 3 bit 0x10 announces it (`D/ipsc/ipsc_mask.py` `PKT_AUTH_MSK`).
  An authenticated master drops every datagram without a valid digest,
  voice included (`P/ipsc/protocol.py` `datagram_received` L128-132). There
  is no nonce or counter in the digest: a captured packet replays.
- Published test vector (`D/DO_NOT_README.md` "AUTHENTICATION"): key `12345`
  (= 17 zero bytes + `01 23 45`), packet `90 00000001 6a 000080dc 04030400`,
  digest `b0ec45f4c3f8fb0c0b1d`. Reproduced by `ipsc.digest()` (unit test
  `test_auth_digest_vector`).

### 1.2 Link-maintenance messages

Integers are big endian. "Len" excludes the 10 byte digest. Layouts are the
ones both projects build and parse; where they differ the cell says so.

| Op | Name | From -> to | Layout | Len | Source |
|----|------|-----------|--------|-----|--------|
| 0x90 | MASTER_REG_REQ | peer -> master | op, id(4), mode(1), flags(4), version(4) | 14 | D `IPSC.__init__` L318, `master_reg_req` L583; P `IPSCPeerProtocol._register` L741, `IPSCMasterProtocol._on_reg_req` L183 (accepts >= 10) |
| 0x91 | MASTER_REG_REPLY | master -> peer | op, id(4), mode(1), flags(4), peers(2), version(4) | 16 | D `master_reg_req` L591; P `_on_reg_req` L233-240 |
| 0x92 | PEER_LIST_REQ | peer -> master | op, id(4) | 5 | D `IPSC.__init__` L320; P `_on_reg_reply` L597 |
| 0x93 | PEER_LIST_REPLY | master -> peer | op, id(4), length(2), { id(4), ip(4), port(2), mode(1) } x N | 7 + 11N | D `build_peer_list` L205, `process_peer_list` L378; P `_send_peer_list` L256, `_on_peer_list_reply` L603 |
| 0x94 | PEER_REG_REQ | peer -> peer | op, id(4), version(4) | 9 | D `IPSC.__init__` L321, `peer_reg_req` L532; P `_service_mesh_peers` L673 |
| 0x95 | PEER_REG_REPLY | peer -> peer | op, id(4), version(4) | 9 | D L322; P `_on_peer_reg_req` L640 |
| 0x96 | MASTER_ALIVE_REQ | peer -> master | as 0x90 | 14 | D L319, `peer_maintenance_loop` L727; P `_send_alive` L753 |
| 0x97 | MASTER_ALIVE_REPLY | master -> peer | op, id(4), mode(1), flags(4), version(4) (no peer count) | 14 | D L328; P `__init__` L103 |
| 0x98 | PEER_ALIVE_REQ | peer -> peer | op, id(4), mode(1), flags(4) | 10 | D L323; P `_service_mesh_peers` L681 |
| 0x99 | PEER_ALIVE_REPLY | peer -> peer | op, id(4), mode(1), flags(4) | 10 | D `peer_alive_req` L517; P `_on_peer_alive_req` L656 |
| 0x9A | DE_REG_REQ | peer -> master | op, id(4) | 5 | D L332, `de_register_self` L370; P `IPSCPeerProtocol.stop` L831 |
| 0x9B | DE_REG_REPLY | master -> peer | op, id(4) | 5 | D L333; P `_on_de_reg_req` L294 |

`id` is the sender's radio id, not the repeater's DMR id of a subscriber.
Nothing in either project checks that mode, flags and version of a keep-alive
equal those of the registration.

**MODE byte** (four 2-bit fields; `D/ipsc/ipsc_mask.py` L12-24,
`D/ipsc/dmrlink_config.py` L172-187, `P/ipsc_packet_reference.md` "MODE
Byte"): bits 7-6 operational (01 = enabled, the only value both call valid),
5-4 radio (00 none, 01 analog, 10 digital, 11 mixed), 3-2 slot 1 and 1-0
slot 2 (10 linked to the IPSC, 01 local, 00 disabled). Both projects send
`0x6A`. `PeerSession` sends `0x6A`; a software application is `0x40`
(P reference).

**FLAGS (4 bytes)**: bytes 0-1 are unknown (the reference doc names a few
bits, partly unsure), byte 2: `0x80` CSBK, `0x40` repeater call monitor,
`0x20` third-party console app; byte 3: `0x80` XNL connected, `0x40` XNL
master, `0x20` XNL slave, `0x10` packets authenticated, `0x08` data calls,
`0x04` voice calls, `0x01` master (`D/ipsc/ipsc_mask.py` L26-52). ipsc2hbp's
"proven" values are `00 00 00 04` as a peer and `00 00 00 05` as a master,
`|0x10` with auth (`P/config.py` L196-206). The DMRlink example packet
carries `00 00 80 DC`: CSBK, XNL connected, XNL master, auth, data, voice.

**VERSION (4 bytes)**: two (6-bit type, 10-bit version) pairs, the current
protocol first, the backward compatible one second (`P/ipsc/const.py`
IPSC_VER comment L54-64; type values 1 IPSC, 2 Capacity Plus, 3
Application, 4 Linked Capacity Plus come second hand from node-dmr-lib via
`P/ipsc_packet_reference.md`). Both projects send `04 02 04 01` = IPSC v2 /
v1 (`D/ipsc/ipsc_const.py` L57, built from the table of L36-43 whose
version names are guesses). The DMRlink documentation shows a captured
`04 03 04 00` and the P reference a second-hand `04 07 04 01`
(`D/DO_NOT_README.md` "PROTOCOL VERSION": "pure guesses").

### 1.3 Sessions

Peer to master (what a simulated repeater does):

```
peer                                master
 | MASTER_REG_REQ (0x90) ---------->|
 |<------------- MASTER_REG_REPLY (0x91)
 | PEER_LIST_REQ (0x92) ----------->|      (DMRlink peer: only if the reply says peers > 0)
 |<------------- PEER_LIST_REPLY (0x93)
 | every N s: MASTER_ALIVE_REQ ---->|
 |<------------ MASTER_ALIVE_REPLY (0x97)
 | for each listed peer: PEER_REG_REQ / PEER_REG_REPLY, then PEER_ALIVE_REQ / REPLY every N s
 | DE_REG_REQ (0x9A) -------------->|
```

Cited: D `peer_maintenance_loop` L715-796 (state machine, flow chart in
`D/DO_NOT_README.md` "CONNECTION CREATION AND MAINTENANCE"); P
`IPSCPeerProtocol._registration_and_keepalive` L770-803.

Differences between the two projects that matter for a test (they are two
implementations of one undocumented protocol; which one a Motorola repeater
tolerates is unknown, section 3):

| Topic | DMRlink (master) | ipsc2hbp (master) |
|-------|------------------|-------------------|
| Answer to MASTER_REG_REQ | MASTER_REG_REPLY only (`master_reg_req` L583-614) | MASTER_REG_REPLY **then** an unsolicited PEER_LIST_REPLY, and the new list to every other peer (`_on_reg_req` L241-251) |
| Peer count in the reply | counted **before** the new peer is added (L591 builds it, L613 updates the count afterwards) | counted **with** the new peer (L238) |
| Answer to PEER_LIST_REQ | `send_to_ipsc`: the list goes to the master socket and **every connected peer** (L627-632, L501-511) | to the requester only (`_on_peer_list_req` L286) |
| Peer timeout | no keep-alive for 120 s (`master_maintenance_loop` L708) | `keepalive_watchdog` (sample 60 s, minimum 5; `_watchdog_loop` L375) |
| Who may register | any | `allowed_peer_ids`, `allowed_peer_ips`, at most 14, a second source address for a known id is refused (`_on_reg_req` L194-222) |
| Voice from an unknown id | warning, dropped (`datagramReceived` L830) | dropped (`_on_voice` L305-308) |
| Keep-alive from an unregistered id | warning, no reply (L623) | ignored (`_on_alive_req` L278) |

Defaults of a peer: DMRlink `ALIVE_TIMER` 5 s, `MAX_MISSED` 20
(`D/dmrlink_SAMPLE.cfg`); ipsc2hbp `keepalive_interval` 5 s,
`keepalive_missed_max` 3 (`P/config.py` L173-176). The interval is the
codeplug's "firewall open timer", typically 5 to 30 s
(`D/DO_NOT_README.md`).

### 1.4 Voice: GROUP_VOICE (0x80) and PVT_VOICE (0x81)

One packet is one 60 ms DMR burst. Same layout for both opcodes (a private
call has FLCO 3 in the LC). Header, bytes 0-30 (`P/ipsc/const.py` L84-95;
`P/ipsc_packet_reference.md` "GROUP_VOICE Common Header"; D
`datagramReceived` L837-858; `D/documents/voice_burst_decoding.txt`):

| Bytes | Field | Notes |
|-------|-------|-------|
| 0 | opcode | 0x80 group, 0x81 private |
| 1-4 | source peer id | the repeater's (outbound) or the bridge's master id |
| 5 | call stream id | constant for one call, +1 per call (DMRlink doc: "incremented with each call"); newer Motorola firmware changes it per superframe when Talker Alias is interleaved (`P/translate/translator.py` L90-97) |
| 6-8 | source subscriber id | |
| 9-11 | destination group (or unit) id | |
| 12 | call type | 0x02 group voice, echoed |
| 13-16 | call control | opaque; 0x000043E2 is the Motorola default the bridge sends (`translator.py` L248) |
| 17 | call info | 0x20 = slot 2, 0x40 = call end (`ipsc_mask.py` `TS_CALL_MSK`, `END_MSK`) |
| 18 | RTP byte 0 | 0x80 (V=2) |
| 19 | RTP marker + payload type | 0xDD (marker) on the first header of a call, 0x5D on everything else, 0x5E on the terminator (samples: `D/documents/voice_packets.txt`; bridge: `translator.py` `_emit_in` callers L797, L668, L678) |
| 20-21 | RTP sequence | +1 per packet |
| 22-25 | RTP timestamp | +480 per packet (8 kHz x 60 ms); the Motorola samples advance it on the headers too |
| 26-29 | RTP SSRC | 0 |
| 30 | burst type | 0x01 header, 0x02 terminator, 0x0A slot 1 voice, 0x8A slot 2 voice, 0x13 (bit 7 = slot) "sync not detected" placeholder, 34 bytes |

Voice header (0x01) and terminator (0x02), 54 bytes (`translator.py`
`_build_ipsc_voice_payload` L170-203; `P/ipsc_packet_reference.md`):

| Bytes | Field |
|-------|-------|
| 31 | bit 7 = slot 2 |
| 32-33 | length to follow in 16-bit words (10 -> 54 bytes; a 50 byte variant with 8 words is accepted by the decoder, section 1.7) |
| 34 | bit 7 = RSSI present (0x80) |
| 35 | slot type byte 0x0A |
| 36-37 | data size in bits, 0x0060 |
| 38-46 | the 9 byte LC: FLCO, FID, service options, destination, source |
| 47-49 | RS(12,9) parity of the LC, XOR 0x96 (header) or 0x99 (terminator) |
| 50-51 | 0x00, 0x11 (header) or 0x12 (terminator) |
| 52-53 | RSSI, 8.8 fixed point in -dBm, 0 = none |

The RS(12,9) code is over GF(256) (x^8+x^4+x^3+x^2+1), generator
(x+a)(x+a^2)(x+a^3). `ipsc.rs129()` reproduces the parity of all four
captured headers / terminators in `D/documents/` (the DMRlink text says the
sample "doesn't come out right"; with these masks it does) and the headers
of the bridge's test inputs, including private-call LCs.

Voice bursts (0x0A / 0x8A) (`translator.py` `_build_slot_voice_payload`
L704-723; samples in `voice_packets.txt`): byte 31 = length to follow in
bytes, byte 32 = control, bytes 33-51 = the 19 byte AMBE field, then a
burst specific tail:

| Burst | Total | Byte 31 | Byte 32 | After the AMBE field |
|-------|-------|---------|---------|----------------------|
| A (voice sync) | 52 | 0x14 | 0x40 | nothing |
| B, C, D | 57 | 0x19 | 0x06 | embedded LC fragment (4), EMB header (1): 0x12, 0x16, 0x16 |
| E | 66 | 0x22 | 0x16 | fragment 4 (4), LC FLCO/FID/options (3), destination (3), source (3), EMB 0x14 |
| F | 57 | 0x19 | 0x06 | 00 00 00 00, 0x10 |

Control bits (`P/ipsc/const.py` L95-103): 0x80 embedded LC failed its check,
0x40 voice sync (burst A), 0x10 reassembled LC present (burst E), 0x01 AMBE
frame a uncorrectable. The EMB header is colour code 1, PI 0 and the LCSS of
the burst (1, 3, 3, 2, 0), lowest bit cleared (`ipsc.emb_header()` derives it
from `dvtest/proto/dmr.py`; it equals the sample values).

AMBE field (19 bytes = 152 bits; `translator.py` L15-22, `_extract_ambe_from_dmr`
L124-146): frame a at bit 0 (49 bits), a bad-frame flag for b at bit 49,
frame b at 50, flag for c at 99, frame c at 100, 3 pad bits; the flag of a is
bit 0 of the control byte. `ipsc.pack_ambe()` / `unpack_ambe()`.

Timeslot appears twice: bit 5 of byte 17, and bit 7 of byte 30 on voice
bursts. Real hardware agrees; DMRlink's confbridge rewrites only byte 17, so
the bridge has `ts_prefer_call_info` (`translator.py` L344-366).

A call, as the Motorola samples show it: 3 headers 60 ms apart (marker on the
first), bursts A..F repeated every 60 ms, one terminator
(`D/documents/voice_burst_decoding.txt`, "sent 3 times"). The DMRlink notes
also say a repeater should get a wake-up (0x85) before a call; not
confirmed (section 3, H11).

### 1.5 Other opcodes seen on the wire

All "received, not processed" by the bridge (`P/ipsc/protocol.py`
`_KNOWN_UNHANDLED` L57-79; `P/ipsc/const.py` L4-43): 0x05 CALL_CONFIRMATION,
0x54 TXT_MESSAGE_ACK, 0x61/0x62/0x63 call monitor status / slot report /
repeater blocked (layouts in `P/ipsc_packet_reference.md`), **0x70 XCMP/XNL
(the bridge's comment: "NEVER TOUCH - can damage repeaters")**, 0x83/0x84
group / private data, 0x85 RPT_WAKE_UP (op, id(4), seq(4), slots(1), type(1)),
0x86 call interrupt (DMRlink: "seen when two dmrlinks try to transmit at
once"), 0x9C/0x9D system map request / reply (**payload format unknown**; a
CPS hangs without a 0x9D), 0x9E unknown, 0xB2 wireline (MNIS), 0xE0/0xE1
remote programming, 0xF0 (9 bytes: op, id(4), 00000000; "later firmware",
follows every MASTER_ALIVE_REPLY). `decode()` names them and returns the raw
body.

### 1.6 What the bridge does with it: IPSC -> Homebrew (DMRD)

All in `P/translate/translator.py` `CallTranslator.ipsc_voice_received` L338-564.
DMRD layout: `P/hbp/const.py` L56-69 and the `dvtest/proto/dmr.py` packet
builder (55 bytes: "DMRD", seq, src(3), dst(3), repeater(4), flags,
stream(4), 33 byte payload, BER, RSSI).

| IPSC | DMRD | Where |
|------|------|-------|
| voice header (0x01) | flags = 0x80 (if slot 2) + 0x40 (if opcode 0x81) + 0x20 (data sync) + 1 (voice LC header); payload = BPTC(196) of the LC with the data-sync pattern; stream id = 4 fresh random bytes; a second header of the same call (stream id byte 5 unchanged) keeps the stream and is forwarded again | L411-454 |
| burst A | flags 0x10 (voice sync), low nibble 0, embed = BS voice sync | L530-537 |
| bursts B..F | flags 0x00 + position 1..5; AMBE 49 -> 72 bits per frame (dmr_utils3 conversion); embed = EMB header + **its own regenerated** embedded LC + EMB tail | L533-538, `_build_embed` L566 |
| terminator (0x02) | flags 0x20 + 2 (voice LC terminator); BPTC of the stored LC; the call state is cleared after sending | L455-469, L560-564 |
| source / destination | taken from the LC locked at the header (or burst E on late entry), **never** from later packets (alias bytes on Talker Alias superframes) | L540-544 |
| repeater id | `hbp_repeater_id` of the config | L218 |
| BER, RSSI | BER 0; RSSI from header / terminator (`_ipsc_rssi` L149), 0 on voice | L554-555 |
| DMRD seq | one counter for both slots, +1 per packet | L547 |
| uncorrectable AMBE flags (control 0x01, bits 49 / 99) | that frame becomes the MMDVMHost silence frame | L518-529 |
| 0x13 placeholder | dropped, logged | `_on_voice` L328 |
| late entry (no header seen) | waits for a burst E with control 0x10 set, control 0x80 clear, and FLCO at byte 56 = 0 (group) or 3 (private); earlier bursts are dropped | L485-510 |
| call boundary | RTP timestamp jump of more than 8000 ticks (or backward) ends the call without a terminator; byte 5 is NOT used; silence for 2 s ends it via the 5 s watchdog | L90-98, `_out_call_continues` L321, `check_call_timeouts` L882 |
| two sources on one slot | per-slot lock, first source wins; duplicate tails dropped for 2 s (peer mode only) | `P/ipsc/protocol.py` L45-53, L704-725 |
| HBP not logged in | the burst is dropped silently | `translator.py` L339 |

The Homebrew side the bridge needs: connect to a master with the usual
RPTL / RPTK / RPTC (RPTO) / RPTPING handshake (`P/hbp/protocol.py`
`_on_rptack` L179-224; `P/hbp/const.py` L72-110); mode `TRACKING` (default)
logs in only while an IPSC peer is registered, `PERSISTENT` always.

Homebrew -> IPSC (`hbp_voice_received` L725-860, `_deliver_slot` L638,
`_emit_in` L624): a 120 ms jitter buffer, delivery on a 60 ms grid anchored
at the first voice burst, headers forwarded at once (each with marker 0xDD
and the timestamp **not** advanced, unlike the Motorola samples), AMBE
silence for missing positions, a terminator only after the buffered tail
drained (or after 6 synthesized bursts). Source/destination go into the call
header from the LC; FLCO 3 makes the opcode 0x81.

### 1.7 Where the sources disagree

| Item | Statement A | Statement B | Used by the module |
|------|-------------|-------------|--------------------|
| RTP payload type of the terminator | 0xDE in the prose of `voice_burst_decoding.txt` | 0x5E in the sample lines of both DMRlink documents and in ipsc2hbp | 0x5E (samples) |
| RTP timestamp unit | "assumed 16.16 seconds, 4.8 ms apart... seems wrong" (DMRlink doc) | +480 per burst, 8 kHz (ipsc2hbp, and the samples: the deltas are 480) | 480 |
| Marker on the second and third header | cleared in the samples | set on every header by the bridge | samples |
| RTP timestamp on headers | advances 480 per header in the samples | not advanced by the bridge | samples |
| Peer count in MASTER_REG_REPLY | excludes the new peer (DMRlink, doc: "not including us") | includes it | the decoder reports it, the sim ignores it |
| LC FID / options in a header | `00 00` in one DMRlink sample, `10 20` in the other | `0x00 0x00` (bridge) | default `00 00`, parameters `lc_fid`, `lc_options` |
| Short header | not in the DMRlink documents | a 50 byte header with length-to-follow 8 words in `P/tests/synclost_ipsc_in.txt` (a test input of the bridge; origin unknown) | decoder accepts the length rule (34 + 2 x words); the RS parity of that input does not match, so its field layout is not the 54 byte one: hardware origin and layout unverified |

### 1.8 What `dvtest/proto/ipsc.py` implements and what was checked

| Part | API | Check |
|------|-----|-------|
| auth | `auth_key`, `digest`, `sign`, `strip_auth` | published digest vector, tamper, short packet |
| fields | `mode_byte`, `flags_word`, `parse_*`, `version_field` | hand-built values, the flags of the DMRlink example |
| link packets | `master_reg_req` ... `de_reg_reply`, `peer_list_reply` | hand-built hex per packet, decode round trip, bad lengths |
| RS(12,9) | `rs129` | 4 captured vectors; the bridge's test inputs (private calls too) |
| AMBE | `pack_ambe`, `unpack_ambe`, `ambe_from_dmr`, `ambe_to_dmr`, `valid_dmr_frames` | bit positions, 72 -> 49 -> 72 round trip of valid codewords from `nxdn.py` |
| voice | `head_term_payload`, `slot_voice_payload`, `voice_packet`, `call_packets`, `decode` | hand-built header hex; burst lengths, control bytes, EMB bytes, embedded LC rebuilt from B..E fragments |
| simulated repeater | `PeerSession` (no socket), `IpscPeer` (UDP) | state machine against hand-fed replies, auth, loopback with a stub master |

Local checks, not committed: the decoder parsed every packet of
`D/documents/voice_packets.txt` and `voice_burst_decoding.txt` (20 packets of
real hardware: lengths 54, 52, 57, 66, 57, 54, RS parity correct) and the
voice packets of the bridge's `pvt_ipsc_in.txt`, `lateentry_ipsc_in.txt`,
`synclost_ipsc_in.txt` (123 packets) without error, RS parity correct for
122 of them (the exception is the 50 byte header of section 1.7). The
AMBE bits cannot be compared with those captures (no reference decoder
here).

## 2. Plan: a simulated MOTOTRBO peer on the rig

### 2.1 Design (written)

`dvtest/proto/ipsc.py`, stdlib only, SPDX MIT, one module per protocol as
the repository rule says. Layers:

1. packet layer: encoders and a strict `decode()` (exact lengths, length
   byte consistency, HMAC check), all pure functions;
2. `PeerSession`: the repeater state machine without a socket (register,
   peer list, mesh registration and keep-alive, answers to peers, voice
   capture, a trace of every packet); testable offline;
3. `IpscPeer`: a UDP socket around a session, a keep-alive timer, a call
   sender paced at 60 ms from `time.monotonic()`.

Deterministic voice: `signals.prng_frames("dmr", n, seed)` gives 9 byte
frames; `ipsc.valid_dmr_frames()` makes them exact AMBE+2 codewords through
`nxdn.to_dmr(nxdn.from_dmr(f))` so that the 72-bit DMR form and the 49-bit
IPSC form are equivalent; `ambe_from_dmr()` packs them into the IPSC AMBE
field. The expected 72-bit frames on the Homebrew side are the same
`valid_dmr_frames()` list (**open**: the bridge converts with the dmr_utils3
tables, the module with the golay/interleave of `nxdn.py`; they must agree
bit for bit, which only the first rig run can show. If they do not, the
comparison falls back to the 49 data bits through `nxdn.from_dmr`).

### 2.2 Phase 1 work (not done)

| Item | What |
|------|------|
| Homebrew sink | a stdlib HBP **master** in the tester process (`dvtest/proto/hbp_master.py`): RPTL -> RPTACK + 4 byte salt, RPTK (sha256 of salt + passphrase, `dmr.auth_hash`) -> RPTACK, RPTC (302 bytes, `dmr.parse` has no RPTC parser: split by `dmr.RPTC_FIELDS`) -> RPTACK, RPTO, RPTPING -> MSTPONG, RPTCL, a switch for MSTNAK; records every DMRD with a monotonic time; `send()` of DMRD for the other direction. `dvtest/proto/dmr.py` has the client half only (`rptl`, `rptk`, `rptc`, `rptping`, `parse_control`). |
| Master role of the simulator | `MasterSession` for tests where the bridge is the peer (`mode = "PEER"`); reuse the encoders |
| Trace tool | `dvtest/tools/ipsc_trace.py`: `dvtest/pcap.udp_datagrams()` + `ipsc.decode()` -> one line per packet (below) |
| Cases | `cases/test_ipsc_bridge.py` (marker `rig`, profile `ipsc`), `docs/ipsc-cases.md` |
| Run.sh | the profile, the per-run auth key (like the TURN secret: never printed, scanned for in `out/<run>`) |

### 2.3 Case list (proposal; assertions that name the bridge are for the stand-in, the others are protocol obligations)

| Id | Scenario | Assert |
|----|----------|--------|
| IPSC1-01 | register, no auth | MASTER_REG_REPLY (16 bytes, op 0x91, mode and flags decode), then the peer list contains the peer (ipsc2hbp pushes it unasked); version field echo not required |
| IPSC1-02 | register with auth, then with a wrong key | reply authenticated and valid; wrong key: silence, no state change (peer list unchanged) |
| IPSC1-03 | keep-alive | MASTER_ALIVE_REPLY within 1 s for 3 consecutive requests; no reply for an unregistered id |
| IPSC1-04 | watchdog | stop all packets for `keepalive_watchdog` + 5 s: the Homebrew login ends (TRACKING), a new MASTER_REG_REQ works |
| IPSC1-05 | de-registration | DE_REG_REQ -> DE_REG_REPLY, HBP logout |
| IPSC1-06 | group call slot 2 | the sink sees per header packet one DMRD with flags 0xA1, then the voice DMRD with flags 0x90 (A) and 0x81 to 0x85 (B to F) in that cycle, then one with 0xA2 (terminator); src, dst and stream id constant within the call and different in the next call; DMRD seq +1 per packet |
| IPSC1-07 | same on slot 1, and byte 17 vs byte 30 disagreement | slot follows byte 30 on bursts (flag `ts_prefer_call_info` off) |
| IPSC1-08 | private call (0x81) | DMRD bit 0x40 set, LC FLCO 3 |
| IPSC1-09 | late entry | call without header: nothing forwarded before burst E, identity from burst E |
| IPSC1-10 | no terminator | RTP jump of 20000 ticks starts a new call; 2 s silence ends the old one |
| IPSC1-11 | sync-undetected placeholder (0x13) between bursts | nothing forwarded for it, the call continues |
| IPSC1-12 | bad AMBE flags | the frame is the silence frame |
| IPSC1-13 | HBP -> IPSC | the sink sends head, 18 voice, terminator at 60 ms; the simulator receives 0x80 packets, marker only where the bridge puts it, +480 timestamps, terminator after the tail |
| IPSC1-14 | two simulated peers (second at the tester alias address, as `tester-addr` does for mrefd) | peer lists pushed to both; a call of one is not echoed back to its sender; duplicate source lock (peer mode only) |
| IPSC1-15 | robustness | truncated, oversize, unknown opcode 0xEE, 0xF0, 0x70 (XNL), flood of 1000 keep-alives: process stays up, no reply for garbage |
| IPSC1-16 | Homebrew master gone | calls are dropped without a crash; recovery after the sink returns |

Wire level of each result: sink side compares DMRD fields (`dmr.parse`) and
the 33 byte payload structure with `dmr.extract`, `dmr.superframe_lc`,
`dmr.parse_emb`; IPSC side compares decoded packets. Report numbers and
hashes only (the repository rule); a failing case lists the first
differing field.

### 2.4 Capture points

1. **pcap on the bridge**: sidecar `capture-ipsc-bridge` (profile
   `capture-ipsc`, `network_mode: "service:ipsc-bridge"`, the `*capture`
   anchor of `rig/compose.yml`), file `out/<run>/capture/ipsc-bridge.pcapng`.
   It sees both legs (IPSC on 50000, Homebrew on 62031) in one file. The
   sink is in the tester, so the tester's own sockets are not captured; the
   bridge's view is authoritative for timing.
2. **Decoded trace**, short and committed-size: `PeerSession.trace` (every
   packet sent and received by the simulator, one line each) plus the
   `ipsc_trace` output for the pcap. Line format:

   ```
   +12.480 rx 172.30.0.98:50000 MASTER_REG_REPLY id=1000000 peers=1 mode=0x6A
   +12.481 rx 172.30.0.98:50000 PEER_LIST_REPLY id=1000000 peers=1
   +13.020 tx 172.30.0.98:50000 GROUP_VOICE id=1000001 ts=2 call=1 src=1234567 dst=91 HEAD rtp=0/0
   ```

   (what `ipsc.describe()` prints). Voice is summarised: first and last
   packet of a call plus counts per burst kind, not one line per burst.
3. The Homebrew side: the sink's DMRD log (count, flags histogram,
   first/last time) in `out/<run>/ipsc/sink.json`.

Not captured: the real repeater's RF side, and any traffic of a repeater
(section 3).

## 3. What a real Motorola repeater sends that the simulator cannot

The simulator reproduces what the two open projects know. A statement is
marked **from source** (with the file) when a project states or shows it,
**from knowledge, unverified** when it comes from general knowledge of
MOTOTRBO and this document did not find it in the sources. Everything in
this section is a hardware risk: a green run with the simulator is not a
statement about a real repeater.

| Id | Statement | Basis | What the test can and cannot say |
|----|-----------|-------|----------------------------------|
| H1 | A repeater sends three voice headers 60 ms apart; only the first has the RTP marker and the RTP timestamp advances on each | from source (`D/documents/voice_burst_decoding.txt`, `voice_packets.txt`) | the simulator does exactly this; a bridge that expects one header is untested against hardware variants |
| H2 | Repeaters ignore keep-alives while they transmit, so a master must take voice as proof of life | from source (`D/dmrlink.py` comment at `reset_keep_alive` L635-637; ipsc2hbp does it in `datagram_received` L146-152) | the simulator never goes silent on keep-alive during a call; add a case (keep-alive stops while a call runs) |
| H3 | Newer firmware (named XPR8400 in the bridge) changes the call stream id every superframe when Talker Alias is interleaved and puts alias bytes in the header source / destination on those superframes | from source (`P/translate/translator.py` L90-97, `P/ipsc/const.py` L84-90, test `P/tests/test_ta_ghost_streams.py`) | not simulated: the alias burst layout is not documented (burst E with FLCO 4..8 is all the sources say); the stream-id rule is the only part a test could model |
| H4 | Later firmware sends opcode 0xF0 (9 bytes, zero tail) after every MASTER_ALIVE_REPLY | from source (`P/ipsc/const.py` L38-43) | the simulator can send it (case IPSC1-15); real timing unknown |
| H5 | Programming software (CPS) asks for SYSTEM_MAP (0x9C) after registration if other peers exist and hangs without a 0x9D reply; the payload is unknown | from source (`P/ipsc_packet_reference.md` "SYSTEM_MAP") | cannot be simulated or answered; a DV-Router IPSC edge cannot serve CPS until a capture exists |
| H6 | Remote programming (0xE0 / 0xE1), wireline / MNIS (0xB2), XCMP / XNL (0x70), call monitor (0x61-0x63), wake-up (0x85), confirmation (0x05), text ack (0x54), data calls (0x83 / 0x84) exist on a real system | from source (`P/ipsc/const.py`) | only the opcodes are known, most layouts are not; XNL carries keys and is deliberately never produced (bridge comment "can damage repeaters", DMRlink FAQ) |
| H7 | The "sync not detected" placeholder 0x13 is 34 bytes and marks a burst the repeater lost on RF | from source (`P/ipsc/const.py` L52, `_on_voice` L328) | the simulator can send it; the 3 bytes after the header are not documented, any content is a guess |
| H8 | The version field, mode and flags of real devices vary: `04 03 04 00` with flags `00 00 80 DC` (an authenticated XNL repeater), `04 07 04 01` (second hand), `04 02 04 01` sent by both open projects | from source (`D/DO_NOT_README.md`, `P/ipsc_packet_reference.md`) | the simulator defaults to the bridge's own values; which values a master must accept is unknown; build cases with the 3 variants as input, not as expected output |
| H9 | Which firmware revisions send which variant (opcodes, version bytes, header lengths, the 50 byte header, the Talker Alias behaviour) | from knowledge, unverified (only "later firmware" and "current XPR8400 firmware" appear in the sources) | no table exists in the sources; do not claim support for a firmware |
| H10 | A real master (repeater as IPSC master) sends the peer list only on request or when the membership changes, and a real peer waits for it before PEER_REG_REQ; DMRlink waits for a non-empty peer count, the bridge pushes unasked | from source for the two open implementations (section 1.3), the order a Motorola repeater needs is from knowledge, unverified | the simulator accepts both orders; a repeater may accept only one |
| H11 | A repeater needs a RPT_WAKE_UP (0x85, type from a short table) before it keys a call it receives over IP | from source as a remark (`D/documents/voice_burst_decoding.txt`: "Send a wakeup before starting a call"), effect unverified; the bridge never sends it | not simulated; a real repeater may drop the first calls of an IPSC edge that never sends 0x85 |
| H12 | The repeater is "markedly less tolerant of jitter, lost frames and reordering" than the Homebrew side; it wants a continuous 60 ms grid | from source (claim of the author in `P/theory_of_operation.md` section 7.8, `translate/const.py` comment L5-15), no hardware measurement shown | the simulator paces from `time.monotonic()` (about a millisecond of jitter in a container); it measures the bridge's grid (case IPSC1-13: gap statistics), not what a repeater tolerates |
| H13 | Embedded LC fragments in real bursts B..E have values like `05 05 06 06` in the DMRlink sample, which do not look like the ETSI computation the simulator uses; their byte order and meaning are not documented | from source (the sample lines) and from knowledge, unverified (the conclusion) | the bridge ignores inbound fragments (identity is locked at the header or burst E) so it is unaffected; an edge that reads them would be |
| H14 | The LC FID / service option bytes differ between sites (`00 00` and `10 20` both appear); `10` is the Motorola feature id in DMR | from source (the two DMRlink sample sets); the meaning of 0x10 and 0x20 from knowledge, unverified | parameterised in `call_packets()` |
| H15 | Call control (bytes 13-16) is Motorola's priority / interrupt field; the bridge sends `00 00 43 E2` | from source (`P/ipsc_packet_reference.md`: "opaque... safe to echo") | the simulator sends the same; real call interrupts (0x86) are not simulated |
| H16 | A repeater is a peer in a full mesh: voice also arrives from other repeaters, with several sources for one slot, duplicates through a c-Bridge or DMRgateway, and a lagging duplicate tail | from source (`P/ipsc/protocol.py` L45-53) | one simulator is one source; IPSC1-14 uses two sessions but not the timing of a real WAN |
| H17 | IPSC is limited to 15 nodes including the master; Capacity Plus and Linked Capacity Plus use other protocol type values | from source, second hand (`P/ipsc/protocol.py` L81, `P/ipsc_packet_reference.md` version table) | only IPSC (type 1) is simulated |
| H18 | Keep-alive and watchdog times are codeplug values (the "firewall open timer"); 5 to 30 s typical; DMRlink drops a peer after 120 s, the bridge after 60 s | from source (`D/DO_NOT_README.md`, `D/dmrlink.py` L708, `P/config.py` L173) | tests use 1 to 5 s for speed; real values and the repeater's retry behaviour after a lost master (how often, with what back-off) are unknown |
| H19 | Digital privacy (basic / enhanced, a PI header and encrypted voice) exists; the call monitor table names `Security Type` 0/1/2 | from source (`D/ipsc/ipsc_const.py` SEC table L106-110) for the existence; how voice is carried is from knowledge, unverified | not simulated |
| H20 | Emergency, alarm, ARS / GPS, text and radio-check use data / CSBK calls (0x83 / 0x84, call monitor types 0x30-0x54, 0x84 "ARS/GPS?") | from source (`D/ipsc/ipsc_const.py` TYPE table L83-104), layouts unknown | not simulated; the bridge ignores them |
| H21 | The real RTP clock, sequence numbers and burst timing come from the repeater's TDMA timing and its RF receive path; bursts can arrive early or in a pair after a stall | from knowledge, unverified | the simulator's clock is a software timer |
| H22 | Source port of a repeater and its NAT behaviour: peers use their own source port (sample peer list: 50001, 50010, 50012 against a master on 50000); the master must answer to that port | from source (`D/DO_NOT_README.md` peer list sample, `P/ipsc/protocol.py` `_send` to `p['port']`) | the simulator uses one ephemeral port; port changes mid-session (a NAT rebinding) are a case not yet written |
| H23 | The legal status: IPSC is proprietary and protected; interoperability work rests on captures | from source (`P/README.md` notice, `D/documents/FAQ.md`) | no capture of a licensed system is used or kept here |

## 4. Compose profile sketch (`ipsc`, not validated)

Names and pins follow the repository rules (every pin in `.env.example`,
`x-limits-*` on every service, `cap_drop`, read-only root, the rig network
is `internal`, builds are the only internet use). Free rig addresses after
`docs/rig.md`: `.98` (bridge) and `.99` (sink, option B); `.31`, `.33` to
`.39` and `.45` to `.49` are also free. None of it was started.

```yaml
  # IPSC bridge (profile ipsc): ipsc2hbp in MASTER mode, the system under test of
  # IPSC1 until DV-Router has an IPSC edge. The tester is the repeater (simulated
  # peer, 172.30.0.30) and, option A, the Homebrew master (UDP 62031).
  ipsc-bridge:
    <<: *limits-small                       # 256m, 0.5 cpu, 128 pids; asyncio, one process
    profiles: [ipsc]
    init: true
    restart: "no"
    image: dvt-ipsc2hbp:local
    build:
      context: .
      dockerfile: Dockerfile.ipsc2hbp       # TESTER_BASE_IMAGE + pip install of IPSC2HBP_PIP_PINS
      args:                                 # (bitarray, dmr-utils3; wheels only, --no-deps, like webload)
        TESTER_BASE_IMAGE: ${TESTER_BASE_IMAGE:-error-set-tester-base-image-see-env-example}
        IPSC2HBP_PIP_PINS: ${IPSC2HBP_PIP_PINS:-error-set-ipsc2hbp-pip-pins-see-env-example}
      additional_contexts:
        src: ../.cache/ipsc2hbp             # IPSC2HBP_REPO / IPSC2HBP_COMMIT (13abe77e...) fetched by scripts/pre-fetch-ipsc2hbp.sh
    user: "${DVT_UID:-1000}:${DVT_GID:-1000}"
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    read_only: true
    tmpfs:
      - /tmp
    environment:
      DVT_IPSC_BIND: ${DVT_NET:-172.30.0}.98
      DVT_IPSC_PORT: ${DVT_IPSC_PORT:-50000}
      DVT_IPSC_MASTER_ID: ${DVT_IPSC_MASTER_ID:-1000000}      # placeholder id
      DVT_IPSC_ALLOWED_IDS: ${DVT_IPSC_ALLOWED_IDS:-1000001,1000002}
      DVT_IPSC_AUTH: ${DVT_IPSC_AUTH:-off}                    # off | on; the key is a per-run secret file
      DVT_IPSC_WATCHDOG: ${DVT_IPSC_WATCHDOG:-15}
      DVT_HBP_MASTER: ${DVT_NET:-172.30.0}.30:62031          # option A: the sink is the tester
      DVT_HBP_REPEATER_ID: ${DVT_HBP_REPEATER_ID:-1000010}
      DVT_HBP_MODE: ${DVT_HBP_MODE:-TRACKING}
    volumes:
      - ../out/${DVT_RUN_ID:-manual}/ipsc:/ipsc:ro            # rendered ipsc2hbp.toml, key file (run secrets, never printed)
    networks:
      rig: {ipv4_address: "${DVT_NET:-172.30.0}.98"}

  # Option B only: the Homebrew sink as its own container (dvtest/proto/hbp_master.py serve),
  # when the tester must not own port 62031. Records DMRD to /ipsc/sink.jsonl.
  hbp-sink:
    <<: *limits-small
    profiles: [ipsc-sink]
    ...                                      # image: the tester image, command: python -m dvtest.proto.hbp_master serve
    networks:
      rig: {ipv4_address: "${DVT_NET:-172.30.0}.99"}

  capture-ipsc-bridge:
    <<: *capture
    profiles: [capture-ipsc]
    network_mode: "service:ipsc-bridge"
    depends_on:
      ipsc-bridge: {condition: service_started, restart: false}
    command: ["ipsc-bridge"]
```

Recommendation: option A (sink in the tester). The cases assert on DMRD
packets, a sink in another container would have to ship them back; the
only price is that the tester's HBP socket is not in the bridge capture's
view (it is, as the bridge's peer: the capture sees both legs because it
shares the bridge's namespace).

Limits: the bridge is one asyncio process with no disk; `x-limits-small`
should be enough, **not measured**. A load case (many peers, 14 maximum,
and a call every 60 ms per slot) would show whether it holds `0.5` cpu.

## 5. Open questions

1. Is the target an IPSC **master** (repeaters register with DV-Router) or
   a **peer** (DV-Router joins an existing repeater master)? The simulator
   is the repeater; the opposite needs `MasterSession` (phase 1).
2. Does the dmr_utils3 AMBE conversion equal `nxdn.to_dmr()`? (section 2.1)
3. Is there a lawful capture of a real repeater (even one registration and
   one call) that can calibrate H1, H3, H4, H7, H13? Without it the
   simulator is only as correct as two open projects.
4. Which IPSC system flags must a DV-Router edge advertise (`data`, XNL,
   CSBK)? The bridge's "proven" `00 00 00 04` is what is tested here.
5. The C port `ipsc2hbpc` (dependency free, the "production" version per
   the README) was not cloned (two clones were allowed); a rig image
   would need it instead of pip packages. It claims config compatibility.
6. `dvtest/proto/ipsc.py` strictness: `decode()` rejects a control packet
   whose length differs from the table. Is a longer packet from a newer
   firmware a protocol violation or an extension? Unknown; relax with a
   `strict=False` flag when the first real capture shows it.
