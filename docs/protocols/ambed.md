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
same stream instead of opening a second one; the same transaction with other
codecs is a new open and gets its own stream. An extended open always gets a
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

Size = 1 + sum(1 + w(out)); Codec2 -> [PCM] is 322 B. Replies to the
19 B legacy opens xlxd sends (reference ambed `CStream`, same in the
capture):

| Input | Outputs | Reply |
|-------|---------|-------|
| AMBE+ (1) | AMBE2+, Codec2, IMBE | 1+1+9 + 1+8 + 1+11 = 32 B |
| AMBE2+ (2) | AMBE+, Codec2, IMBE | 32 B |
| Codec2 (3) | AMBE+, AMBE2+, IMBE | 1+1+9 + 1+9 + 1+11 = 33 B |
| IMBE (4) | AMBE+, AMBE2+, Codec2 | 1+1+9 + 1+9 + 1+8 = 30 B | xlxd checks every codec
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

Run `out/20261029T145759Z-i5` (rig instance 5, so addresses are
`172.30.5.x`): `DVT_INSTANCE=5 scripts/run.sh --capture --profile allstar -m
"wire or rig or allstar"`, xlxd-sut = PR tree with USRP, xlxd-ref = upstream
master, transcoder = the image under test (`TRANSCODER_IMAGE`); 45 passed,
2 skipped. The backend-loss case stops transcoder-sut once, so its capture
is `transcoder-sut.pcapng` + `transcoder-sut.2.pcapng` (gap = the 21 s it
was stopped; xlxd-sut.pcapng has the xlxd side of the gap).

Message counts and UDP payload sizes (`udp.length` - 8), own traffic only
(`ip.addr == 172.30.5.<host>`):

```sh
scripts/tshark.sh -r out/20261029T145759Z-i5/capture/<file>.pcapng \
  -Y "ambed && !icmp && ip.addr == 172.30.5.21" -T fields -e ambed.msg -e udp.length \
  | sort -n | uniq -c
```

| Message | Size | transcoder-sut (both files) | xlxd-sut | xlxd-ref |
|---------|------|-----------------------------|----------|----------|
| PING | 17 | 86 | 88 | 88 |
| PONG | 9 | 86 | 84 | 88 |
| CAPS request | 9 | 3 | 1 | 0 |
| CAPS reply | 15 | 3 | 1 | 0 |
| OPEN legacy | 17 / 18 / 19 | 2 / 2 / 87 | 0 / 0 / 22 | 0 / 0 / 30 |
| OPEN extended | 24 | 49 | 28 | 0 |
| STD | 14 | 90 | 20 | 30 |
| STD extended | 18 | 41 | 27 | 0 |
| BUSY | 9 | 1 | 0 | 0 |
| BUSY extended | 13 | 8 | 0 | 0 |
| CS | 9 | 128 | 47 | 30 |
| data request / reply | 10..342 | 5383 / 5383 | 2767 / 2767 | 1104 / 1104 |

Field cross-checks (`-T fields` rows fed to a small script in `.cache/`):

- PING callsign `XLX999` from both reflectors, `N0TEST` in every tester
  open; xlxd-sut PING period median 5.01 s (min 5.00, max 5.02, n 88).
- CAPS reply (3 of 3): version 1, max outputs 4, mask `0x0000002e`
  (AMBE+, AMBE2+, Codec2, PCM; IMBE not announced). xlxd-sut sends its CAPS
  request right after the first PONG, then extended-opens only the USRP
  modules; xlxd-ref (upstream) never sends CAPS.
- Extended opens: every reply echoes the transaction; transcoder-sut first
  file 26 distinct (peer, transaction) = 18 STD 18 + 8 BUSY 13, none
  unanswered; second file 19 / 19 STD 18. Open -> reply latency median
  0.1-0.2 ms, max 0.4 ms. xlxd transactions are consecutive. One xlxd-sut
  open is unanswered: it falls in the window where transcoder-sut is stopped.
- Invalid extended opens from the wire cases, each answered with BUSY 13
  and the same transaction (`0x00007000`..`0x00007005`): duplicate output,
  output after an empty slot, output equal to input, no output, codec 6,
  blank callsign. Two valid opens for codecs outside the CAPS mask
  (IMBE <-> PCM) also got BUSY 13.
- Retransmit: `tx 0xa1b2c3d4` sent twice with the same codecs got stream 19
  both times; `tx 0x5a5a0001` with in 3 -> [5] and with in 5 -> [3] got
  streams 20 and 21, and resending either got the same stream again.
- BUSY 9: the wire case `test_legacy_open_full_gets_busy9` sent 65 legacy
  19 B opens in 5 ms; 64 got STD 14 (stream ids 19..64 then 1..18, ports
  10100 + id, 10101..10164), the 65th got the 9 B BUSY, then 64 CS closed
  them. The transcoder under test serves at most 64 streams.
- STD: stream id n always comes with port 10100 + n; codec in / out 1 equal
  the open's input and first output.
- Data: requests and replies pair up by (port, peer, pid) with nothing left
  over in every file. Reply layouts and sizes, e.g. legacy [2,3,4] = 32 B,
  [1,3,4] = 32 B, [1,2,4] = 33 B (xlxd-ref 366 / 366 / 372 replies),
  extended [1,2,3] = 30 B, [1,2,5] = 342 B, [3,2] = 20 B, [5] = 322 B;
  `ambed.order` warnings 0. PCM entries with signal
  (`ambed.entry.pcm_peak` > 0): 1348 of 1494 on xlxd-sut.
- xlxd fixed legacy outputs: in 1 -> [2,3,4], in 2 -> [1,3,4], in 3 ->
  [1,2,4].

```
AMBEd transcoder protocol, CAPS reply v1 max=4 mask=0x2e
    Tag: AMBEDCAPS
    CAPS version: 1
    Max outputs: 4
    Codec mask: 0x0000002e
AMBEd transcoder protocol, OPEN ext N0TEST in=3 outs=[5,5] tx=0x00007000
    [Expert Info (Note/Protocol): invalid open: duplicate output 5]
AMBEd transcoder protocol, BUSY tx=0x00007000
    Tag: AMBEDBUSY
    [Message: BUSY extended (10)]
    Transaction: 0x00007000
AMBEd transcoder protocol, OPEN legacy(19) N0TEST in=1 outs=[2,3,4]
AMBEd transcoder protocol, BUSY
    Tag: AMBEDBUSY
    [Message: BUSY (9)]
AMBEd transcoder protocol, STD id=19 port=10119 in=3 out0=5 tx=0xa1b2c3d4
    Stream id: 19
    Stream data port: 10119
AMBEd transcoder protocol, CS id=19
AMBEd transcoder protocol, data reply pid=0 outs=[1,2,3]
    Entry 0: AMBE+ (D-STAR), 9 bytes
    Entry 1: AMBE2+ (DMR/YSF), 9 bytes
    Entry 2: Codec2 3200 (M17), 8 bytes
```

Expert info (`-q -z expert`): no AMBEd errors or warnings in any file;
notes only on transcoder-sut.pcapng: 9 `ambed.busy` and 6
`ambed.invalid_open`. Other warnings are mDNS/ARP from the host bridge and
restarted containers.

Implementation vs this document: the CAPS mask of the transcoder under test
leaves IMBE out, yet legacy opens with an IMBE output are served (904
replies with outs [2,3,4]). Not a conformance error: CAPS describes the
extended open.

An earlier run with a previous image (`out/20261029T135314Z`) got no reply
to five invalid extended opens and STD 18 for the blank callsign; the image
used above answers all six with BUSY 13.
