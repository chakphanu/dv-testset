<!-- SPDX-License-Identifier: MIT -->
# AMBEd transcoder protocol

The link between an xlxd reflector and its transcoder (ambed or a compatible
implementation). Plain UDP, no authentication. The reflector is the client;
the transcoder listens on UDP 10100 for control packets and opens one UDP
data port per stream.

Two generations share the control port:

- **legacy**: PING/PONG, a 17/18/19 B OPEN, a 14 B STD or 9 B BUSY reply,
  CS. Up to 3 outputs, AMBE+/AMBE2+/Codec2/IMBE only.
- **extended**: adds CAPS, a 24 B OPEN with up to 4 outputs and a
  transaction id, and 18/13 B replies that echo the transaction. PCM
  (codec 5) is only reachable through the extended open.

A transcoder that predates the extension never answers CAPS and ignores a
24 B OPEN; the reflector then keeps using the legacy forms.

Dissector: `tools/wireshark/dvtest.lua`, protocol `ambed` (filter `ambed`).
Client: `dvtest/proto/ambed.py` (encoders, parsers and `FakeAmbed`).

## Codec ids

| Id | Name | Frame bytes (20 ms) | Used by |
|----|------|---------------------|---------|
| 0 | none | - | unused output slot |
| 1 | AMBE+ | 9 | D-STAR |
| 2 | AMBE2+ | 9 | DMR, YSF |
| 3 | Codec2 3200 | 8 | M17 |
| 4 | IMBE | 11 | P25 |
| 5 | PCM S16LE 8 kHz, 160 samples | 320 | USRP (extended open only) |

The CAPS mask uses bit n for codec n, so all five codecs are `0x3E`.

## Byte order

- Tags are ASCII without a terminator.
- Callsign: 8 bytes, space padded.
- Stream id (STD, CS) and data port (STD) are written with `memcpy` of a host
  `uint16` on both sides. Every known deployment is x86/ARM little endian, so
  they are **little endian** on the wire.
- CAPS mask and transaction are **big endian** (`htonl`).

## Control packets (UDP 10100)

### PING (reflector -> transcoder), 17 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 9 | ASCII | `AMBEDPING` |
| 9 | 8 | ASCII | reflector callsign, space padded |

### PONG (transcoder -> reflector), 9 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 9 | ASCII | `AMBEDPONG` |

### CAPS request (reflector -> transcoder), 9 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 9 | ASCII | `AMBEDCAPS` |

### CAPS reply (transcoder -> reflector), 15 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 9 | ASCII | `AMBEDCAPS` |
| 9 | 1 | u8 | version, 1 |
| 10 | 1 | u8 | max outputs per extended open, 1..4 |
| 11 | 4 | u32 BE | codec mask, bit n = codec n (bits 1..5) |

xlxd accepts the reply only when the version is 1, max outputs is 1..4 and
the mask (ANDed with `0x3E`) is non-zero.

### OPEN legacy (client -> transcoder), 17 / 18 / 19 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 7 | ASCII | `AMBEDOS` |
| 7 | 8 | ASCII | client callsign |
| 15 | 1 | u8 | codec in (1..4) |
| 16 | 1 | u8 | codec out 1 |
| 17 | 1 | u8 | codec out 2 (18 and 19 B forms) |
| 18 | 1 | u8 | codec out 3 (19 B form) |

xlxd always sends the 19 B form (three outputs, fixed per input codec, e.g.
AMBE+ -> AMBE2+, Codec2, IMBE). The 17 and 18 B forms come from older
clients; the wire cases send all three.

### OPEN extended (client -> transcoder), 24 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 7 | ASCII | `AMBEDOS` |
| 7 | 8 | ASCII | client callsign |
| 15 | 1 | u8 | codec in (1..5) |
| 16 | 4 | u8[4] | codec out 1..4, unused slots 0 |
| 20 | 4 | u32 BE | transaction id, never 0 from xlxd |

xlxd uses the extended open only when PCM is the input or one of the needed
outputs (a module with a USRP endpoint); every other stream keeps the 19 B
legacy open, even when CAPS was answered. It lists the outputs in ascending
codec order, without the input codec and
limited to the CAPS mask and max outputs. It starts the transaction at a
random value per process and increments it per open. The reference ambed
answers a retransmitted open (same peer, transaction and codecs) with the
same stream instead of opening a second one. An extended open always gets a
reply: invalid ones (unknown or duplicate codec, output equal to the input,
no output, invalid callsign) get BUSY 13.

### STD stream descriptor (transcoder -> client), 14 / 18 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 8 | ASCII | `AMBEDSTD` |
| 8 | 2 | u16 LE | stream id |
| 10 | 2 | u16 LE | stream data port on the transcoder |
| 12 | 1 | u8 | codec in |
| 13 | 1 | u8 | codec out 1 (first output only) |
| 14 | 4 | u32 BE | transaction of the extended open (18 B form only) |

The reference ambed uses data port 10100 + stream id.

### BUSY (transcoder -> client), 9 / 13 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 9 | ASCII | `AMBEDBUSY` |
| 9 | 4 | u32 BE | transaction of the extended open (13 B form only) |

### CS close stream (client -> transcoder), 9 B

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 7 | ASCII | `AMBEDCS` |
| 7 | 2 | u16 LE | stream id |

No reply. The reference ambed accepts 9 B or longer.

## Data packets (stream port)

Both ends use the stream port number from STD: the client binds that port
locally and the transcoder replies to the client address at the same port
(UDP port N <-> port N).

### Request (client -> transcoder)

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 1 | u8 | codec in |
| 1 | 1 | u8 | packet id (pid), wraps at 256 |
| 2 | w(in) | bytes | one frame of the input codec |

### Reply (transcoder -> client)

Ordered entries, one per output in open order, with the pid after the first
codec byte:

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 1 | u8 | codec out 1 |
| 1 | 1 | u8 | pid of the request |
| 2 | w(out1) | bytes | frame 1 |
| 2+w1 | 1 | u8 | codec out 2 |
| 3+w1 | w(out2) | bytes | frame 2 |
| ... | | | further entries: codec byte + frame |

Size = 1 + sum(1 + w(out)). Examples: Codec2 -> [PCM] is 322 B, AMBE+ ->
[AMBE2+, Codec2, IMBE] (legacy 19 B open) is 32 B. xlxd checks every codec
byte against the open order; a legacy transcoder may return only the first n
outputs, and the missing ones are zero filled.

The dissector tells a request from a reply by the first byte: a reply never
starts with the input codec, because an output equal to the input is refused
at open time.

## Message flows

Keepalive and capability probe (probe once per connection, and only when the
reflector has a PCM/USRP endpoint configured):

```
xlxd                                   transcoder
 |-- AMBEDPING XLX999 (17) ------------->|   every 5 s
 |<-------------------- AMBEDPONG (9) ---|
 |-- AMBEDCAPS (9) --------------------->|   after the first PONG
 |<--------- AMBEDCAPS v1 max mask (15) -|   no reply within 1 s = legacy
```

Legacy open, stream, close:

```
xlxd                                   transcoder
 |-- AMBEDOS XLX999 in out1 out2 out3 (19) ->|
 |<------- AMBEDSTD id port in out1 (14) ----|   or AMBEDBUSY (9)
 |== port P: in pid frame (2+w) ============>|   every 20 ms
 |<= port P: out1 pid f1 out2 f2 out3 f3 ====|
 |-- AMBEDCS id (9) ------------------------>|
```

Extended open (after a CAPS reply):

```
xlxd                                   transcoder
 |-- AMBEDOS XLX999 in o1..o4 tx (24) ------>|
 |<---- AMBEDSTD id port in o1 tx (18) ------|   or AMBEDBUSY tx (13)
 |== port P: in pid frame ===================>|
 |<= port P: o1 pid f1 o2 f2 ... ============|
 |-- AMBEDCS id (9) ------------------------>|
```

Fallback (transcoder without the extension):

```
xlxd                                   transcoder
 |-- AMBEDCAPS (9) --------------------->|   (ignored)
 |   1 s timeout: extended open off for this connection
 |-- AMBEDOS ... (19) ------------------>|   legacy open for AMBE/Codec2/IMBE
 |   PCM input: not transcoded (logged once)
```

## Timing (xlxd side)

From the xlxd source (`main.h`, `ctranscoder.cpp`, `cusrpprotocol.cpp`); the
frame and PING periods are confirmed in the Evidence section.

| Item | Value |
|------|-------|
| PING period | 5 s |
| keepalive timeout (no PONG) | 30 s |
| OPEN reply wait | 1000 ms + 50 ms grace; a late STD is closed with CS |
| CAPS reply wait | 1000 ms |
| frame period | 20 ms |
| data reply timeout | 400 ms |

## Legacy vs extended

| | legacy | extended |
|---|---|---|
| OPEN size | 17 / 18 / 19 B | 24 B |
| outputs | 1..3 | 1..4 (CAPS max outputs) |
| codecs | 1..4 | 1..5 (CAPS mask) |
| transaction | none | BE32, echoed in STD/BUSY |
| STD / BUSY size | 14 / 9 B | 18 / 13 B |
| invalid open | may get no reply | always BUSY 13 |
| retransmit | opens a second stream | same stream returned |

## Evidence

Run `out/20261029T135314Z`: `scripts/run.sh --capture --profile allstar -m
"wire or rig or allstar"`, xlxd-sut = PR tree with USRP, xlxd-ref = upstream
master, transcoder = the image under test (`TRANSCODER_IMAGE`); 33 passed,
6 failed (see below), 2 skipped.

Message counts and UDP payload sizes (`udp.length` - 8):

```sh
scripts/tshark.sh -r out/20261029T135314Z/capture/<svc>.pcapng -Y "ambed && !icmp" \
  -T fields -e ambed.msg -e udp.length | sort -n | uniq -c
```

| Message | Size | transcoder-sut | xlxd-sut | xlxd-ref |
|---------|------|----------------|----------|----------|
| PING | 17 | 25 | 71 | 75 |
| PONG | 9 | 25 | 67 | 71 |
| CAPS request | 9 | 2 | 1 | 0 |
| CAPS reply | 15 | 2 | 1 | 0 |
| OPEN legacy | 17 / 18 / 19 | 2 / 2 / 4 | 0 / 0 / 21 | 0 / 0 / 32 |
| OPEN extended | 24 | 26 | 28 | 1 (flooded from xlxd-sut) |
| STD | 14 | 8 | 19 | 30 |
| STD extended | 18 | 19 | 27 | 0 |
| BUSY | 9 | not observed | 0 | 0 |
| BUSY extended | 13 | 2 | 0 | 0 |
| CS | 9 | 26 | 46 | 30 |
| data request | 10 / 11 / 322 | 202 / 347 / 647 | 474 / 1140 / 547 | 372 / 732 / 0 |
| data reply | 10..342 | 1196 | 1750 | 1104 |

transcoder-sut.pcapng covers the first 118 s only: its sniffer stops when
the backend-loss case stops transcoder-sut (see `docs/rig.md`). xlxd-ref
PING/PONG include 4 flooded xlxd-sut packets.

Field cross-checks (`-T fields` rows fed to a small script in `.cache/`):

- PING callsign `XLX999` from both reflectors, `N0TEST` in every tester
  open; PING period from xlxd-sut: median 5.01 s (min 5.00, max 5.02, n 71).
- CAPS reply from the transcoder under test: version 1, max outputs 4, mask
  `0x0000002e` (AMBE+, AMBE2+, Codec2, PCM; IMBE not announced). xlxd-sut
  sent its single CAPS request 30 us after the first PONG, then
  extended-opened only the USRP modules.
- Extended opens: transaction echoed in every STD 18 / BUSY 13 reply (25
  distinct transactions on transcoder-sut: 18 STD 18, 2 BUSY 13, 5 no reply;
  xlxd-sut: 27 of 28 answered, the one unanswered falls in the backend-loss
  window and matches the xlxd log line "ambed openstream timeout").
  Open -> reply latency median 0.2 ms, max 0.4 ms. xlxd transactions are
  consecutive (`0xae73911f` .. `0xae73913a`). The retransmitted open
  (`tx 0xa1b2c3d4` twice) got the same stream (id 20, port 10120) both times.
- STD: stream id n always came with port 10100 + n (ports 10101..10132
  seen); codec in / out 1 equal the open's input and first output.
- Data: every request pid got a reply on the same port and peer (1196 / 1196
  on transcoder-sut, 1750 / 1750 on xlxd-sut, 1104 / 1104 on xlxd-ref).
  Reply entries are in open order with the expected sizes, e.g. outs
  [1,2,3] = 30 B (496), [2,3,4] = 32 B, [1,2,4] = 33 B, [1,2,5] = 342 B,
  [3,2] = 20 B, [5] = 322 B; `ambed.order` warnings 0. PCM entries
  carrying signal (`ambed.entry.pcm_peak` > 0): 988 of 1135.
- xlxd fixed legacy outputs seen: in 1 -> [2,3,4], in 2 -> [1,3,4],
  in 3 -> [1,2,4].

```
AMBEd transcoder protocol, CAPS reply v1 max=4 mask=0x2e
    Tag: AMBEDCAPS
    CAPS version: 1
    Max outputs: 4
    Codec mask: 0x0000002e
AMBEd transcoder protocol, OPEN ext XLX999 in=2 outs=[1,5] tx=0xae73911f
    Tag: AMBEDOS
    Callsign: XLX999
    Codec in: AMBE2+ (DMR/YSF) (2)
    [0] Codec out: AMBE+ (D-STAR) (1)
    [1] Codec out: PCM S16LE 8 kHz (5)
    [2] Codec out: none (0)
    [3] Codec out: none (0)
    Transaction: 0xae73911f
AMBEd transcoder protocol, STD id=1 port=10101 in=2 out0=1 tx=0xae73911f
    Stream id: 1
    Stream data port: 10101
AMBEd transcoder protocol, BUSY tx=0x11223344
AMBEd transcoder protocol, OPEN legacy(19) N0TEST in=1 outs=[2,3,4]
AMBEd transcoder protocol, STD id=13 port=10113 in=1 out0=2
AMBEd transcoder protocol, CS id=1
AMBEd transcoder protocol, data request in=5 pid=0
AMBEd transcoder protocol, data reply pid=0 outs=[1,2,3]
    Entry 0: AMBE+ (D-STAR), 9 bytes
    Entry 1: AMBE2+ (DMR/YSF), 9 bytes
    Entry 2: Codec2 3200 (M17), 8 bytes
```

Expert info (`-q -z expert`): no AMBEd errors or warnings in any file; notes
only: 2 `ambed.busy` and 6 `ambed.invalid_open` (the wire cases' invalid
opens). The other warnings are mDNS/ARP from the host bridge and restarted
containers.

Not observed:

- BUSY 9: the transcoder under test accepted all 8 legacy opens the wire
  cases sent (STD 14 each) and xlxd only sends valid 19 B opens; it needs a
  transcoder that is out of channels.

Implementation vs this document (the transcoder under test, reported, not
changed):

- Invalid extended opens (duplicate output, output after an empty slot,
  output equal to input, no output, codec 6) got no reply at all, and an
  extended open with a blank callsign got STD 18 (stream 19). The reference
  ambed source answers all six with BUSY 13 (`IsValidExtOpenstreamPacket`
  sets codec in to none and the reply carries the transaction). These are
  the 6 failing `test_ext_open_invalid_busy` cases.
- The CAPS mask leaves IMBE out, yet legacy opens with an IMBE output are
  served (61 replies with outs [2,3,4] on transcoder-sut).
