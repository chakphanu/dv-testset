<!-- SPDX-License-Identifier: MIT -->
# dvtest clients

What each client in `dvtest/` sends and accepts. They are test endpoints:
enough of each protocol to link, talk, listen and leave, nothing more. All
are stdlib Python over UDP. Identities are synthetic: callsigns N0CALL and
N0TEST, DMR ids 1023007 and 1023008 (both in `rig/config/dmrid.dat`),
reflector XLX999.

Dissectors for the captures: `tools/wireshark/dvtest.lua` (`ambed`, `usrp`,
`dextra`, `dplus`, `dcs`, `mmdvm`, `m17ref`, `ysfref`, `nxdnref`,
`p25ref`); IAX2 uses the Wireshark built-in `iax2`. Wireshark 4.4 has no
built-in dissector for the others, so minimal ones are in the Lua file.

## Summary

| Module | Protocol | Port | Implements | Does not implement |
|--------|----------|------|------------|--------------------|
| `proto/ambed.py` | AMBEd transcoder | 10100 + stream ports | every control message (legacy and extended), data request/reply, `FakeAmbed` server | retransmission, keepalive loop (callers ping) |
| `proto/usrp.py` | USRP | per endpoint | voice/unkey header + PCM, strict voice check, any-type frame builder | DTMF/text/TLV content |
| `proto/dextra.py` | DExtra (D-STAR) | 30001 | link, unlink, keepalive answer, DSVT header/voice/last frame, collect AMBE | header CRC (sent as spaces), slow data content, DPlus/DCS |
| `proto/dmr.py` | MMDVM/Homebrew DMR | 62030 | RPTL/RPTK/RPTPING login, TG join/unlink by data-sync header/terminator, voice bursts with 3 AMBE2+ frames | RPTC config, real SHA256 auth (sends zeros, xlxd does not check), embedded LC, BER/RSSI |
| `proto/m17.py` | M17 reflector link | 17000 | CONN/DISC, PONG answer to PING, 54 B voice with two Codec2 3200 frames | LSF/META content, CRC (zero), packet mode |
| `proto/iax2.py` | IAX2 (RFC 5456) | 4569 | NEW (CALLTOKEN when asked), MD5 auth, ACCEPT of SLIN, ANSWER, `!NEWKEY!` text link-up, radio key/unkey (control 12/13), SLIN full and mini voice frames, PING/LAGRQ answers, HANGUP | other codecs, trunking, encryption, transfers |
| `proto/nxdn.py` | NXDNReflector network | 41400 | NXDNP poll/link, NXDNU unlink, NXDND header (FACCH1 VCALL), voice (4 x 49-bit AMBE+2, SACCH VCALL in 4 parts), trailer (FACCH1 TX_REL), CRC-6/CRC-12, 49-bit <-> DMR 72-bit frame conversion | data calls, private calls, RAN, Icom/Kenwood links |
| `proto/p25.py` | P25Reflector network | 41000 | poll/unlink, LDU1/LDU2 voice records with one IMBE each, LCF, TG, source id, ALGID clear, terminator, silence padding to whole LDUs | HDU/TDU with LC, LC/ES parity, low-speed data, encryption |
| `proto/dplus.py` | DPlus (REF, D-STAR) | 20001 | connect/login/keepalive/disconnect, 58 B header, 29 B voice, 32 B last, module join by an empty stream | real login auth, slow data content, header CRC |
| `proto/dcs.py` | DCS (D-STAR) | 30051 | 519 B link, ACK/NAK, unlink, keepalive answer, 100 B voice (header in every packet, u24 counter) | text area content, header CRC |
| `proto/ysf.py` | YSF | 42000 | YSFP poll, YSFS status, 155 B YSFD V/D mode 2 header/voice/terminator with FICH, VCH, DCH, CSD coding, DG-ID module select, WiresX disconnect | V/D mode 1, voice FR, data FR other than WiresX DISC, GPS |
| `clients.py` | - | - | one make/talk/collect API over dstar, dmr and m17 | - |

## AMBEd (`proto/ambed.py`)

See [ambed.md](ambed.md) for the wire format.

- `AmbedClient`: one control socket, `rpc()` = send and wait for one reply.
- `ping`, `caps_reply`, `open_legacy` (17/18/19 B), `open_ext` (24 B),
  `std` (14/18 B), `busy` (9/13 B), `close`, `data_request`, `data_reply`,
  and a parser for each.
- `run_stream` binds the stream port locally (the transcoder replies to that
  port) and sends one request per frame every 20 ms.
- `transcode` = extended open + stream + CS, returns payloads per codec.
- `FakeAmbed`: a server in `extended` mode (answers CAPS, both open forms) or
  `legacy` mode (no CAPS reply, ignores 24 B opens), fixed payloads, optional
  STD delay, logs everything it receives.

## USRP (`proto/usrp.py`)

See [usrp.md](usrp.md). `voice(seq, pcm)` 352 B, `unkey(seq)` 32 B,
`frame(seq, kind, payload)` for non-voice frames a voice-only peer must drop,
`is_voice` mirrors the xlxd acceptance rule.

## DExtra (`proto/dextra.py`)

| Packet | Size | Layout |
|--------|------|--------|
| link | 11 B | callsign(8) + own module + reflector module + 0x00 |
| link ACK (from xlxd) | 14 B | link packet without its last byte + `ACK\0` |
| unlink | 11 B | callsign(8) + module + `' '` + 0x00 |
| keepalive (client) | 9 B | callsign(8) + 0x00 |
| keepalive (xlxd) | 9 B | `XRF999` + two spaces + 0x00 (DExtra name of the reflector) |
| DSVT header | 56 B | `DSVT` 0x10 00 00 00 20 00 01 02, stream id u16 LE, 0x80, flags(3), RPT2(8), RPT1(8), UR(8), MY(8), suffix(4), CRC(2) |
| DSVT voice | 27 B | `DSVT` 0x20 00 00 00 20 00 01 02, stream id u16 LE, seq (mod 21, 0x40 = last), AMBE(9), slow data(3) |

The client answers the xlxd keepalive with its own 9 B keepalive (echoing
the reflector's does not refresh the link) and uses a fresh random stream id
per transmission (xlxd drops a header whose id equals the module's last
outbound id).

## MMDVM/Homebrew DMR (`proto/dmr.py`)

| Packet | Size | Layout |
|--------|------|--------|
| RPTL | 8 B | `RPTL` + repeater id u32 BE |
| RPTACK (xlxd) | 10 / 6 B | `RPTACK` + salt (4) after RPTL; bare `RPTACK` after RPTK |
| MSTCL (xlxd) | 5 B | `MSTCL`, sent when xlxd drops the client |
| RPTK | 40 B | `RPTK` + id + 32 B hash (zeros) |
| RPTPING / MSTPONG | 11 B | tag + id |
| DMRD | 55 B | `DMRD`, seq u8, src u24 BE, dst u24 BE, repeater id u32 BE, flags u8, stream id u32 (host order, LE), payload 33 B, BER + RSSI |

Flags byte: bit 7 slot (1 = TS2), bit 6 call type (0 group), bits 5-4
frame type (0 voice, 1 voice sync, 2 data sync), bits 3-0 data type or
voice sequence. The client joins module X by a voice LC header (0xA1) and a
terminator (0xA2) to TG 4001 + (X - 'A'); TG 4000 unlinks. Voice: three
9 B AMBE2+ frames per burst around the 48 bit sync gap, one burst every
60 ms.

## M17 (`proto/m17.py`)

| Packet | Size | Layout |
|--------|------|--------|
| CONN | 11 B | `CONN` + callsign (6 B base-40) + module |
| ACKN / NACK | 4 B | tag |
| PING (xlxd) / PONG | 10 B | tag + callsign |
| DISC | 10 B / 4 B | `DISC` + callsign from the client; bare `DISC` back from xlxd |
| voice | 54 B | `M17 `, stream id u16 BE, dst (6, 0xFF = broadcast), src (6), type 0x0005, meta (14), frame number u16 BE (0x8000 = EOT), two Codec2 3200 frames (8 + 8), CRC (2, zero) |

Packet 0 opens the stream and is not forwarded by xlxd; the EOT packet
repeats the last payload.

## DPlus (`proto/dplus.py`)

| Packet | Size | Layout |
|--------|------|--------|
| connect / echo | 5 B | 05 00 18 00 01; xlxd echoes it |
| login | 28 B | 1C C0 04 00 + callsign (16, zero padded) + `DV019999` |
| login reply | 8 B | 08 C0 04 00 + `OKRW` / `BUSY` |
| keepalive | 3 B | 03 60 00, both ways (xlxd every second) |
| disconnect / echo | 5 B | 05 00 18 00 00 |
| header | 58 B | 3A 80 + DExtra DSVT header (56) |
| voice | 29 B | 1D 80 + DExtra DSVT voice (27) |
| last voice | 32 B | 20 80 + DSVT prefix (12) + stream id + seq or 0x40 + end pattern 55 C8 7A 55 x 9 25 1A C6 |

The first byte is the packet length, the second the type (0x80 DV, 0xC0
login, 0x00 control, 0x60 keepalive). A DPlus client has no module after
login: xlxd forwards a module to it only after it transmitted there once,
so the client joins with an empty stream (header + last frame).

## DCS (`proto/dcs.py`)

| Packet | Size | Layout |
|--------|------|--------|
| link | 519 B | callsign (7) + own module + own module + reflector module + 0x00 + text, zero padded |
| link ACK / NAK | 14 B | callsign (7) + ' ' + own module + reflector module + `ACK\0` / `NAK\0` |
| unlink | 11 B | callsign (7) + own module + own module + ' ' + 0x00; xlxd answers NAK |
| keepalive (client) | 17 B | callsign (7) + own module + 0x00 + reflector (7) + reflector module |
| keepalive (xlxd) | 9 B + 22 B | `DCS999` + 2 spaces + 0x00; reflector (7) + module + ' ' + callsign (7) + module + module + 0A 00 20 20 |
| voice | 100 B | `0001`, D-STAR header without CRC (39: flags 3, RPT2, RPT1, UR, MY, suffix 4), stream id u16 LE, seq (0x40 = last), AMBE (9), slow data (3), packet counter u24 LE, 0x01, text area (38) |

xlxd takes the module from the link, not from RPT2.

## YSF (`proto/ysf.py`)

| Packet | Size | Layout |
|--------|------|--------|
| YSFP poll | 14 B | `YSFP` + callsign (10, space padded) |
| poll ack | 14 B | `YSFPREFLECTOR ` |
| YSFU | 14 B | `YSFU` + callsign; ignored by this xlxd |
| YSFS / status | 4 B / 42 B | `YSFS`; reply `YSFS` + hash (5) + name (16) + description (14) + client count (3) |
| YSFD | 155 B | `YSFD` + gateway (10) + source (10) + destination (10) + net frame counter << 1 / EOT bit + YSF frame (120: sync D4 71 C9 63 4D, FICH 25, payload 90) |

xlxd accepts only V/D mode 2 (DN) frames whose FICH decodes; a FICH DG-ID
of 10..35 selects module A..Z, new clients start on B; the unlink that
works is a WiresX disconnect request. The full FICH / VCH / DCH / CSD
coding is in the module docstring. The dissector shows the FICH coded
(decoding it needs Viterbi and Golay).

## NXDN (`proto/nxdn.py`)

xlxd talks NXDN only as a peer of an NXDNReflector (UDP 41400; xlxd
interlink row `NX<tg> <ip>:41400 <module>`), so the client links to the
reflector the way an NXDNGateway does. All integers big endian.

| Packet | Size | Layout |
|--------|------|--------|
| NXDNP poll / link | 17 B | `NXDNP` + callsign (10, space padded) + TG u16; the reflector echoes the same 17 B |
| NXDNU unlink | 17 B | `NXDNU` + callsign (10) + TG u16 |
| NXDND | 43 B | `NXDND` + source id u16 + destination id (TG) u16 + flags + payload (33) |

Flags: 0x01 group, 0x02 data, 0x04 header (VCALL), 0x08 trailer (TX_REL).
Payload: LICH (1: RF channel type, functional channel type, option,
direction, parity), SACCH (4), block 1 (14), block 2 (14). Voice: two 49-bit
AMBE+2 frames per block (four per packet, every 80 ms), SACCH carries the
VCALL message 18 bits at a time; header / trailer: FACCH1 (layer 3 message
type, options, 0x20 group, source u16, destination u16, CRC-12) in both
blocks. Identities 1001 N0CALL / 1002 N0TEST (`rig/config/nxdn.csv`).

## P25 (`proto/p25.py`)

xlxd talks P25 only as a peer of a P25Reflector (UDP 41000; interlink row
`P25<tg> <ip>:41000 <module>`, xlxd sends from an ephemeral port). The
client links like a P25Gateway; the reflector relays every record to all
other linked endpoints, never back to the sender.

| Packet | Size | Layout |
|--------|------|--------|
| poll | 11 B | 0xF0 + callsign (10, space padded); echoed by the reflector, every 5 s |
| unlink | 11 B | 0xF1 + callsign |
| LDU1 / LDU2 voice 1 | 22 B | 0x62 / 0x6B, IMBE (11) at offset 10 |
| voice 2 | 14 B | 0x63 / 0x6C, IMBE at 1 |
| voice 3..8 | 17 B | 0x64..0x69 / 0x6D..0x72, IMBE at 5 |
| voice 9 | 16 B | 0x6A / 0x73, IMBE at 4 |
| terminator | 17 B | 0x80 + 16 zero bytes |

Fields in the 17 B records: 0x64 [1] LCF (0 = group), [2] MFID, [3]
service options; 0x65 [1..3] destination TG u24; 0x66 [1..3] source id u24
(the DMR id of the MY callsign); 0x70 [1] ALGID (0x80 = clear), [2..3] key
id. 18 records per superframe, one per 20 ms; there is no header record on
this link, the first 0x62 opens the call. The client pads to whole LDUs with
the IMBE silence frame; xlxd does not pad (it ends a call where the source
ends) and sends a lone terminator for a stream without voice.

## IAX2 (`proto/iax2.py`)

Caller into the AllStar node (profile `allstar`). The secret comes from
`.local/rig.env` and is never logged.

```
tester                                   allstar (4569)
 |-- NEW (called number, SLIN) ---------->|
 |<----------------- CALLTOKEN -----------|   only if the node asks
 |-- NEW + token ------------------------>|   (the rig node does not)
 |<----------------- AUTHREQ (MD5) -------|
 |-- AUTHREP md5(challenge + secret) ---->|
 |<----------------- ACCEPT (SLIN) -------|
 |<----------------- ANSWER (+ control -1) |
 |-- TEXT "!NEWKEY!" -------------------->|
 |<----------------- TEXT x 3 ------------|   incl. "!NEWKEY!": link ready
 |-- CONTROL key (12) ------------------->|
 |-- VOICE SLIN full, then mini frames -->|   320 B every 20 ms
 |-- CONTROL unkey (13) ----------------->|
 |-- HANGUP ----------------------------->|
```

SLIN on IAX is big endian; the client swaps from and to the S16LE used
everywhere else.

## Evidence

Run `out/20261029T153832Z-i5` (`TRANSCODER_IMAGE=vambed-neo:local
DVT_INSTANCE=5 scripts/run.sh --capture --profile allstar -m "wire or rig or
allstar"`; transcoder, xlxd trees and result as in
[ambed.md](ambed.md#evidence); rig instance 5, so addresses are `172.30.5.x`).
DExtra, MMDVM and M17 come from `xlxd-sut.pcapng` (`ip.addr ==
172.30.5.11 && !icmp`), IAX2 from `allstar.pcapng` + `allstar.2.pcapng`.
Counts are `-T fields -e <proto>.kind|tag -e udp.length -e ip.src | sort |
uniq -c`, sizes are UDP payload bytes; "client" = tester.

DExtra (3020 packets):

| Packet | Size | client -> xlxd | xlxd -> client |
|--------|------|----------------|----------------|
| link / ACK | 11 / 14 | 25 | 25 |
| unlink | 11 | 25 | - |
| keepalive | 9 | 20 | 20 (`XRF999`) |
| DSVT header | 56 | 13 | 90 |
| DSVT voice | 27 | 1330 | 1441 |
| DSVT last voice | 27 | 13 | 18 |

MMDVM (2238 packets):

| Packet | Size | client -> xlxd | xlxd -> client |
|--------|------|----------------|----------------|
| RPTL / RPTACK + salt | 8 / 10 | 17 | 17 |
| RPTK / RPTACK | 40 / 6 | 17 | 17 |
| RPTPING / MSTPONG | 11 | 17 | 17 |
| MSTCL | 5 | - | 11 |
| DMRD voice / voice sync / data sync | 55 | 124 / 30 / 65 | 1358 / 326 / 222 |

M17 (796 packets):

| Packet | Size | client -> xlxd | xlxd -> client |
|--------|------|----------------|----------------|
| CONN / ACKN | 11 / 4 | 17 | 17 |
| PING / PONG | 10 | 22 (PONG) | 22 (PING) |
| DISC | 10 / 4 | 17 | 17 |
| voice / voice EOT | 54 | 237 / 7 | 427 / 13 |

IAX2 (built-in dissector): 8 calls. Per call NEW, AUTHREQ, AUTHREP, ACCEPT,
ANSWER and control -1, one TEXT from the tester and three from the node;
key (control 12) 7 / 6 and unkey (13) 18 / 17 tester / node; HANGUP 7 (the
crash case's call ends with the node killed); ACK 88 / 60. Voice (tester /
node): full frames 332 B (12 + 320) 4 / 8, mini frames 324 B (4 + 320)
476 / 1844; 3 CNG frames from the node.

Expert info: no errors or warnings from `dextra`, `mmdvm`, `m17ref` or
`iax2` in these files.

Not observed: DExtra NAK and DISCONNECTED (every link was accepted), M17
NACK, IAX2 CALLTOKEN (the rig node does not ask for it), PING/PONG (IAX)
and LAGRP: the node's LAGRQ arrive after the tester has hung up and get
ICMP port unreachable.

DPlus, DCS, YSF: run `out/20261029T161607Z-i5` (`TRANSCODER_IMAGE=vambed-neo:local
DVT_INSTANCE=5 scripts/run.sh --capture -m rig -- cases/test_clients2.py`;
24 passed), file `xlxd-sut.pcapng` (`ip.addr == 172.30.5.11 && !icmp`):

| Packet | client -> xlxd | xlxd -> client |
|--------|----------------|----------------|
| DPlus connect / disconnect 5 B | 9 / 9 | 9 / 9 (echo) |
| DPlus login 28 B / reply 8 B `OKRW` | 9 | 9 |
| DPlus keepalive 3 B | 54 | 54 |
| DPlus header 58 / voice 29 / last 32 B | 9 / 240 / 9 | 16 / 301 / 6 |
| DCS link 519 B / ACK 14 B | 9 | 9 |
| DCS unlink 11 B / NAK 14 B | 9 | 9 |
| DCS keepalive 17 B / 9 + 22 B | 6 | 6 + 6 |
| DCS voice 100 B / last voice | 240 / 4 | 301 / 6 |
| YSF poll 14 B / ack 14 B | 13 | 13 |
| YSF YSFU 14 B | 9 | - |
| YSFD 155 B (EOT bit set) | 110 (22) | 132 (17) |

Fields: DPlus/DCS headers sent by the client carry MY = its callsign, UR `CQCQCQ`, RPT2
`XLX999` + module; DCS packet counters start at 0 per stream; YSFD
destination `ALL`. The xlxd DCS keepalive is 9 B (8 B callsign + 0x00),
not 8 B. YSFS status was not exercised in this run. Expert info: no
`dplus`, `dcs` or `ysfref` errors or warnings.

NXDN: run `out/20261029T155701Z-i5` (`TRANSCODER_IMAGE=vambed-neo:local
DVT_INSTANCE=5 scripts/run.sh --capture --profile nxdn -m nxdn`; 7 passed,
3 failed, 2 skipped: the failures are Morse round trips nxdn -> dstar,
nxdn -> m17 and dstar -> nxdn, i.e. audio quality through this transcoder,
not framing), file `nxdn-sut.pcapng` (`-Y "nxdnref && !icmp"`; reflector .51,
tester .30, xlxd-sut .11; addresses `172.30.5.x`):

| Packet | tester -> reflector | reflector -> xlxd | xlxd -> reflector | reflector -> tester |
|--------|---------------------|-------------------|-------------------|---------------------|
| NXDND voice 43 B (flags 0x01, LICH 0xAC) | 309 | 309 | 314 | 314 |
| NXDND header 43 B (flags 0x05, LICH 0x81, FACCH1 VCALL) | 5 | 5 | 9 | 8 |
| NXDND trailer 43 B (flags 0x09, LICH 0x81, FACCH1 TX_REL) | 5 | 5 | 9 | 8 |
| NXDNP poll 17 B | 16 | 37 (echo) | 37 | 16 (echo) |
| NXDNU unlink 17 B | 11 | - | - | - |

Fields: destination id 31672 (the reflector TG) in every NXDND; source id
1001 (N0CALL) in every voice packet; FACCH1 source/destination equal the
NXDND header fields. xlxd polls every 5.01 s (median, n 37). xlxd also sends
header + trailer pairs without voice (one with source 1002 / N0TEST: an
empty stream such as the DMR join). The reflector relays unchanged and never
back to the sender. Expert info: no `nxdnref` errors or warnings.

P25: run `out/20261029T155148Z-i10` (`DVT_P25_MODULE=B DVT_INSTANCE=10
TRANSCODER_IMAGE=vambed-neo:local scripts/run.sh --capture --profile
allstar,p25 -m p25`, 9 passed, 1 xfailed), file `p25-sut.pcapng`
(`-Y "p25ref && !icmp"`; reflector .61, tester .30, xlxd-sut .11; instance 10
addresses `172.30.10.x`):

| Records | tester -> reflector | reflector -> xlxd | xlxd -> reflector | reflector -> tester |
|---------|---------------------|-------------------|-------------------|---------------------|
| LDU1 22 / 14 / 17 / 16 B | 53 / 53 / 318 / 53 | 53 / 53 / 318 / 53 | 57 / 57 / 335 / 55 | 57 / 57 / 335 / 55 |
| LDU2 22 / 14 / 17 / 16 B | 50 / 50 / 300 / 50 | 50 / 50 / 300 / 50 | 53 / 53 / 312 / 51 | 53 / 53 / 312 / 51 |
| terminator 17 B | 5 | 5 | 8 | 7 |
| poll 11 B | 20 | 35 (echo) | 35 | 20 (echo) |
| unlink 11 B | 11 | - | - | - |

Fields: LCF 0 (group) and ALGID 0x80 (clear) in every 0x64 / 0x70 record,
destination TG 31672 in every 0x65, source id 1023007 (N0CALL) or 1023008
(N0TEST) in every 0x66, from the tester, the reflector and xlxd alike. The
reflector relays records unchanged (its counts equal its sender's) and never
back to the sender. xlxd's 17 B record count is not a multiple of 6 per
LDU: it ends calls mid-LDU. Expert info: no `p25ref` errors or warnings.
