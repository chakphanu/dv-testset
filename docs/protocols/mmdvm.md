<!-- SPDX-License-Identifier: MIT -->
# MMDVM Homebrew / DMR: obligations of the master (reflector) side

What a reflector acting as an MMDVM Homebrew **master** owes an MMDVM
client (MMDVMHost, DMRGateway, a hotspot) on UDP 62030. Each item is
numbered `P-DMR-NN` and marked **MUST** (a client breaks or misroutes
without it) or **SHOULD** (clients cope, but behaviour differs from
real masters). The conformance cases are planned in `docs/testlist.md`
section K.

Sources, per item:

- **[HB]** the public Homebrew repeater protocol description (DL5DI,
  G4KLX, DG1HT) and how MMDVMHost / DMRGateway behave on the wire as
  clients.
- **[XR]** xlxd-ref (LX3JL `a978f03`), behaviour observed on the rig or
  read from its source and described here in our own words.
- **[DV]** the DV-Router design where it deliberately differs; judged
  through `DVT_SUT_PROFILE=dvrouter` (`dvtest/specs/dvrouter.py`).

How the cases judge (`cases/proto/test_mmdvm.py`): an **[HB] MUST** item
that xlxd-ref meets is asserted on the system under test against the
obligation itself (and recorded for both sides). Where xlxd-ref deviates
from [HB] (P-DMR-03, 04, 05, 11, 15, 25, 28) the case records xlxd's
behaviour as data and judges the system under test by `DVT_SUT_PROFILE`:
under `xlxd` differentially (sut == ref), under another profile by its
spec entry (`dvtest/specs/<profile>.py`, `"mmdvm"`), skipping with "spec
pending" while there is none. A non-xlxd system is never forced to copy an
xlxd deviation. Lenient authentication (a zero RPTK hash accepted) stays
the default expectation for every profile: the tester and real clients
send zero hashes; a strict-hash probe is recorded, not failed, unless a
profile spec asks for strict. Egress embedded LC and sync patterns are
checked passively by dvcheck (testlist H3), not repeated here.

Conventions: ids and the 32-bit fields of control packets are big endian
on the wire unless noted. "Silently ignored" means no reply and no state
change. Our tester (`dvtest/proto/dmr.py`) sends a **zero** RPTK hash and
no RPTC; masters that verify the hash or require RPTC reject it.

## 1. Login

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-01 | `RPTL` + repeater id (8 B) from a known id is answered with `RPTACK` + a 4-byte salt | MUST | HB, XR |
| P-DMR-02 | The salt differs between master starts (not a constant); xlxd draws one random salt per start and reuses it for every login | SHOULD | HB, XR |
| P-DMR-03 | `RPTL` from an id the master does not know: xlxd sends nothing (the id fails its DMR id lookup); a master that refuses a known id by policy answers `MSTNAK` + id | MUST (no login) | XR, HB |
| P-DMR-04 | `RPTK` + id + 32-byte SHA-256(salt + password) (40 B) completes the login with `RPTACK`. Strict masters reject a wrong hash with `MSTNAK`; xlxd does not check the hash (any 32 bytes, including our zeros, are accepted) | MUST (ack); hash check: lenient on xlxd | HB, XR |
| P-DMR-05 | `RPTK` without a preceding `RPTL`: strict masters ignore or NAK it; xlxd accepts it and logs the client in | SHOULD | HB, XR |
| P-DMR-06 | `RPTC` + id + configuration (302 B: callsign, frequencies, power, colour code, position, height, location, description, URL, software and package ids) is answered with `RPTACK` (xlxd: the bare 6-byte tag, no id; the same for RPTK and RPTO, measured) | MUST | HB, XR |
| P-DMR-07 | `RPTC` of another size (short or long) is not acknowledged; xlxd ignores it and keeps the login | SHOULD | XR |
| P-DMR-08 | Login is not gated on `RPTC`: xlxd routes voice for a client that never sent one (our tester relies on this) | SHOULD (lenient) | XR |
| P-DMR-09 | `RPTO` + id + options (8..512 B) is answered with `RPTACK` | SHOULD | HB, XR |
| P-DMR-10 | Re-login while logged in (`RPTL`/`RPTK` again from the same id and address) refreshes the session and creates no second client; traffic keeps flowing once | MUST | HB, XR |

## 2. Keepalive and close

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-11 | `RPTPING` + id (11 B) from a logged-in client is answered with `MSTPONG` + id. xlxd writes that id in host byte order (little endian on x86), not big endian | MUST (reply); id byte order: deviation on xlxd | HB, XR |
| P-DMR-12 | `RPTPING` from an address/id that is not logged in gets no `MSTPONG` | SHOULD | XR |
| P-DMR-13 | A client silent for the server timeout is dropped and sent `MSTCL`; xlxd checks every 10 s and drops after 100 s without a keepalive or voice | MUST (drop); timeout value informative | HB, XR |
| P-DMR-14 | After the drop the client gets no further voice | MUST | XR |
| P-DMR-15 | `RPTCL` + id closes the session (the client gets no further traffic; a floor it held is released). xlxd: only a 13 B form is parsed and the id is read one byte early, so neither the 13 B nor the MMDVMHost 9 B form closes anything | MUST; xlxd ignores it (DV-Router closes, B15) | HB, XR, DV |
| P-DMR-16 | `RPTINTR` (17 B, interrupt/RSSI) is accepted and silently ignored | SHOULD | XR |

## 3. Malformed and unknown packets

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-17 | Unknown tags, short packets (e.g. 3 B, truncated `RPTL`) and oversize packets are silently ignored; the master keeps serving other clients | MUST | HB, XR |
| P-DMR-18 | A `DMRD` packet of a size other than 55 B is ignored | MUST | XR |
| P-DMR-19 | `DMRD` from an address that is not logged in is not relayed | MUST | HB, XR |

## 4. DMRD layout (55 B)

Offsets: 0 `DMRD`, 4 sequence, 5-7 source id, 8-10 destination (TG), 11-14
repeater id, 15 flags (bit 7 slot 2, bit 6 private call, bits 5-4 frame
type: 0 voice, 1 voice sync, 2 data sync; bits 3-0 voice sequence A-F or
data type: 1 voice LC header, 2 terminator), 16-19 stream id, 20-52 the
33-byte DMR burst, 53 BER, 54 RSSI.

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-20 | Only slot 2 is a reflector slot: headers, voice and terminators on slot 1 are ignored | MUST (xlxd rule) | XR |
| P-DMR-21 | Group calls are relayed; a private-call (bit 6) voice burst is not | MUST | XR |
| P-DMR-22 | A stream opens with a data-sync burst of data type 1 (voice LC header) carrying a valid BS or MS data-sync pattern; a header with another sync pattern is ignored | MUST | HB, XR |
| P-DMR-23 | Voice comes as superframes of 6 bursts A-F (A = voice sync, B-F = voice with embedded signalling); each burst carries three AMBE+2 frames around the sync gap; the master relays every frame unchanged | MUST | HB, XR |
| P-DMR-24 | A stream ends with a data-sync burst of data type 2 (terminator with LC) | MUST | HB, XR |
| P-DMR-25 | Egress: the master sends every listener a header, the voice bursts with voice sequence A-F (A as voice sync), and a terminator, all on slot 2, group call, same stream id per stream; xlxd fills embedded LC and writes destination TG 9 on egress (not the module TG) | MUST (structure); TG 9: xlxd convention | XR |
| P-DMR-26 | The packet sequence byte counts up per stream on egress | SHOULD | HB, XR |
| P-DMR-27 | Egress source id = the talker's DMR id (or the id mapped from the talker's callsign for cross-mode streams); xlxd writes that source id as the repeater id too, not the talker's login (repeater) id (measured) | SHOULD | XR |
| P-DMR-28 | Stream id: xlxd folds the 32-bit ingress id to 16 bits (high half XOR low half, 0 -> 1) and writes that fold on egress (measured); DV-Router keeps the ingress id (B10) | SHOULD; spec under DV | XR, DV |

## 5. Talkgroup routing

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-29 | A header to TG 4001..4026 links the client to module A..Z (when the module exists) | MUST | XR |
| P-DMR-30 | A header to TG 4000 unlinks the client from its module; it gets no further voice | MUST | XR |
| P-DMR-31 | A linked client that talks to another module's TG stays on its module (xlxd rewrites the destination to the linked module); a module change needs TG 4000 first | MUST (xlxd rule) | XR |
| P-DMR-32 | TG 9 (and any TG that maps to no module) from a linked client goes to its linked module; from an unlinked client it is ignored | MUST | XR |
| P-DMR-33 | A client links on the header only: an empty stream (header + terminator) is enough to join, and the join stream is not relayed as voice (measured on xlxd: other listeners get no packet of it) | SHOULD | XR |

## 6. Stream lifetime

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-34 | A stream whose terminator is lost ends after the silence timeout (1.6 s on xlxd); the module is then free (measured: xlxd still sends DMR listeners a terminator) | MUST | XR |
| P-DMR-35 | A second header on a busy module is not mixed into the running stream (floor rules: section B of the DV-Router profile) | MUST | XR, DV |
| P-DMR-36 | Voice bursts whose stream id has no header on that master are not relayed | MUST | HB, XR |
| P-DMR-37 | The talker never receives its own stream back | MUST | XR |
| P-DMR-41 | Data-sync bursts that are not voice (CSBK, data header, rate 1/2 or 3/4 data: data type >= 3) are not relayed as voice and do not open a stream | MUST | HB, XR |
| P-DMR-42 | The ingress sequence byte wraps 255 -> 0 during a long over without the master treating it as loss or a new stream | MUST | HB |

## 7. Several clients

| # | Obligation | Level | Source |
|---|---|---|---|
| P-DMR-38 | Two clients with different ids, logged in from the same IP (different ports), are two sessions: each gets the module's traffic once | MUST | XR |
| P-DMR-39 | The same id from two ports: xlxd keys sessions by id and address, so both log in; each must still get the traffic exactly once (no duplicate per port) | SHOULD | XR |
| P-DMR-40 | A keepalive from one client never refreshes another client's session | SHOULD | XR |
