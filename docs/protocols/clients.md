<!-- SPDX-License-Identifier: MIT -->
# dvtest clients

What each client in `dvtest/` sends and accepts. They are test endpoints:
enough of each protocol to link, talk, listen and leave, nothing more. All
are stdlib Python over UDP. Identities are synthetic: callsigns N0CALL and
N0TEST, DMR ids 1023007 and 1023008 (both in `rig/config/dmrid.dat`),
reflector XLX999.

Dissectors for the captures: `tools/wireshark/dvtest.lua` (`ambed`, `usrp`,
`dextra`, `mmdvm`, `m17ref`); IAX2 uses the Wireshark built-in `iax2`.
Wireshark 4.4 has no built-in dissector for DExtra, MMDVM/Homebrew or the
M17 reflector link, so minimal ones are in the Lua file.

## Summary

| Module | Protocol | Port | Implements | Does not implement |
|--------|----------|------|------------|--------------------|
| `proto/ambed.py` | AMBEd transcoder | 10100 + stream ports | every control message (legacy and extended), data request/reply, `FakeAmbed` server | retransmission, keepalive loop (callers ping) |
| `proto/usrp.py` | USRP | per endpoint | voice/unkey header + PCM, strict voice check, any-type frame builder | DTMF/text/TLV content |
| `proto/dextra.py` | DExtra (D-STAR) | 30001 | link, unlink, keepalive answer, DSVT header/voice/last frame, collect AMBE | header CRC (sent as spaces), slow data content, DPlus/DCS |
| `proto/dmr.py` | MMDVM/Homebrew DMR | 62030 | RPTL/RPTK/RPTPING login, TG join/unlink by data-sync header/terminator, voice bursts with 3 AMBE2+ frames | RPTC config, real SHA256 auth (sends zeros, xlxd does not check), embedded LC, BER/RSSI |
| `proto/m17.py` | M17 reflector link | 17000 | CONN/DISC, PONG answer to PING, 54 B voice with two Codec2 3200 frames | LSF/META content, CRC (zero), packet mode |
| `proto/iax2.py` | IAX2 (RFC 5456) | 4569 | NEW (CALLTOKEN when asked), MD5 auth, ACCEPT of SLIN, ANSWER, `!NEWKEY!` text link-up, radio key/unkey (control 12/13), SLIN full and mini voice frames, PING/LAGRQ answers, HANGUP | other codecs, trunking, encryption, transfers |
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

Run `out/20261029T135314Z` (`scripts/run.sh --capture --profile allstar -m
"wire or rig or allstar"`), file `xlxd-sut.pcapng` (DExtra, MMDVM, M17) and
`allstar.pcapng` (IAX2, first 86 s: the sniffer stops at the crash case's
`kill allstar`). Counts are `-T fields -e <proto>.kind|tag -e udp.length -e
ip.src | sort | uniq -c`, sizes are UDP payload bytes; "client" = tester.

DExtra (`-Y "dextra && !icmp"`, 1752 packets):

| Packet | Size | client -> xlxd | xlxd -> client |
|--------|------|----------------|----------------|
| link / ACK | 11 / 14 | 23 | 23 |
| unlink | 11 | 23 | - |
| keepalive | 9 | 17 | 17 (`XRF999`) |
| DSVT header | 56 | 12 | 85 |
| DSVT voice | 27 | 720 | 807 |
| DSVT last voice | 27 | 12 | 13 |

MMDVM (`-Y "mmdvm && !icmp"`, 2638 packets):

| Packet | Size | client -> xlxd | xlxd -> client |
|--------|------|----------------|----------------|
| RPTL / RPTACK + salt | 8 / 10 | 17 | 17 |
| RPTK / RPTACK | 40 / 6 | 17 | 17 |
| RPTPING / MSTPONG | 11 | 17 | 17 |
| MSTCL | 5 | - | 11 |
| DMRD voice / voice sync / data sync | 55 | 131 / 23 / 65 | 1691 / 374 / 241 |

M17 (`-Y "m17ref && !icmp"`, 805 packets):

| Packet | Size | client -> xlxd | xlxd -> client |
|--------|------|----------------|----------------|
| CONN / ACKN | 11 / 4 | 17 | 17 |
| PING / PONG | 10 | 21 (PONG) | 21 (PING) |
| DISC | 10 / 4 | 17 | 17 |
| voice / voice EOT | 54 | 237 / 7 | 440 / 11 |

IAX2 (built-in dissector, `-Y "iax2 && !icmp"`, 2619 packets): 8 calls.
Per call NEW, AUTHREQ, AUTHREP, ACCEPT, ANSWER and control -1, one TEXT
from the tester and three from the node; key (control 12) 7 / 6 and unkey
(13) 18 / 17 tester / node; HANGUP 7 (the crash case's call ends with the
node killed); ACK 88 / 60. Voice (tester / node): full frames 332 B (12 + 320) 4 / 8,
mini frames 324 B (4 + 320) 474 / 1839; 3 CNG frames from the
node.

Expert info: no errors or warnings from `dextra`, `mmdvm`, `m17ref` or
`iax2` in either file.

Not observed: DExtra NAK and DISCONNECTED (every link was accepted), M17
NACK, IAX2 CALLTOKEN (the rig node does not ask for it), PING/PONG (IAX)
and LAGRP: the node's 8 LAGRQ arrived after the tester had hung up and got
ICMP port unreachable.
