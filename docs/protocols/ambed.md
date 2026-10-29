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

The extended protocol (CAPS, the 24 B OPEN with a transaction, STD 18 /
BUSY 13 and PCM codec 5) is proposed in xlxd pull request #272
(https://github.com/LX3JL/xlxd/pull/272) and is not in upstream xlxd master.

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
| IMBE (4) | AMBE+, AMBE2+, Codec2 | 1+1+9 + 1+9 + 1+8 = 30 B (from the source; no IMBE source in the rig) | xlxd checks every codec
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

Run `out/20261029T153832Z-i5` (rig instance 5, addresses `172.30.5.x`):

```sh
TRANSCODER_IMAGE=vambed-neo:local DVT_INSTANCE=5 \
  scripts/run.sh --capture --profile allstar -m "wire or rig or allstar"
```

- Transcoder (both transcoder-ref and transcoder-sut): vambed-neo, the
  public reference transcoder, commit `90e5ab1a6a7924a03444403000d9b33f48c72ed3`,
  built with mbelib-neo `c94180bd925ea43771df09d1e9273e8fe620f928` and codec2
  `06d4c11e699b0351765f10398abb4f663a984f36`; default limit 16 streams.
- xlxd-sut: the PR #272 tree with USRP endpoints; xlxd-ref: upstream master
  `a978f03671d87c6d9ac13453689755991e5cb668`.
- Result: 45 passed, 1 failed (`test_node_crash_releases_module`, an
  AllStar case), 2 skipped.
- The backend-loss case stops transcoder-sut once, so its capture is
  `transcoder-sut.pcapng` + `transcoder-sut.2.pcapng`; xlxd-sut.pcapng has the
  xlxd side of the gap.

Message counts and UDP payload sizes (`udp.length` - 8), own traffic only:

```sh
scripts/tshark.sh -r out/20261029T153832Z-i5/capture/<file>.pcapng \
  -Y "ambed && !icmp && ip.addr == 172.30.5.21" -T fields -e ambed.msg -e udp.length \
  | sort -n | uniq -c
```

| Message | Size | transcoder-sut (both files) | xlxd-sut | xlxd-ref |
|---------|------|-----------------------------|----------|----------|
| PING | 17 | 80 | 80 | 82 |
| PONG | 9 | 80 | 78 | 82 |
| CAPS request | 9 | 3 | 1 | 0 |
| CAPS reply | 15 | 3 | 1 | 0 |
| OPEN legacy | 17 / 18 / 19 | 2 / 2 / 39 | 0 / 0 / 21 | 0 / 0 / 30 |
| OPEN extended | 24 | 47 | 27 | 0 |
| STD | 14 | 40 | 20 | 30 |
| STD extended | 18 | 41 | 26 | 0 |
| BUSY | 9 | 3 | 0 | 0 |
| BUSY extended | 13 | 6 | 0 | 0 |
| CS | 9 | 78 | 46 | 30 |
| data request / reply | 10..354 | 5427 / 5427 | 2761 / 2761 | 1116 / 1116 |

Field cross-checks (`-T fields` rows fed to a small script in `.cache/`):

- PING callsign `XLX999` from both reflectors, `N0TEST` in every tester
  open. PING period median 5.01 s (min 5.00, max 5.02) while the
  transcoder is up.
- CAPS reply (3 of 3, to xlxd-sut and the tester): version 1, max outputs
  4, mask `0x0000003e` (all five codecs). xlxd-sut sends its CAPS request
  right after the first PONG and extended-opens only the USRP modules;
  xlxd-ref (upstream) never sends CAPS.
- Extended opens: every reply echoes the transaction, none unanswered on
  transcoder-sut (first file 24 distinct (peer, transaction) = 18 STD 18 +
  6 BUSY 13; second file 19 / 19 STD 18). Open -> reply latency median
  0.4-0.7 ms, max 1.9 ms. xlxd transactions are consecutive. One xlxd-sut
  open falls in the window where transcoder-sut is stopped and gets no reply.
- Invalid extended opens from the wire cases, each answered with BUSY 13
  and the same transaction (`0x00007000`..`0x00007005`): duplicate output,
  output after an empty slot, output equal to input, no output, codec 6,
  blank callsign.
- Retransmit: `tx 0xa1b2c3d4` sent twice with the same codecs got stream 16
  both times; `tx 0x5a5a0001` with in 3 -> [5] and with in 5 -> [3] got
  streams 1 and 2, and resending either got the same stream again.
- BUSY 9, two ways: (a) the 17 B legacy open Codec2 -> [AMBE+] (twice): this
  transcoder refuses legacy combinations a hardware ambed could not serve;
  (b) `test_legacy_open_full_gets_busy9` sent 17 legacy 19 B opens within 16 ms:
  16 got STD 14 (stream ids 16, 1..15, ports 10100 + id), the 17th got the
  9 B BUSY, then 16 CS closed them.
- STD: stream id n always comes with port 10100 + n; codec in / out 1 equal
  the open's input and first output.
- Data: requests and replies pair up by (port, peer, pid) with nothing left
  over in every file. Reply layouts and sizes: legacy [2,3,4] = 32 B,
  [1,3,4] = 32 B, [1,2,4] = 33 B (xlxd-ref 366 / 378 / 372 replies);
  extended [1,2,3] = 30 B, [1,2,5] = 342 B, [4,3] = 22 B, [1,2,4,5] = 354 B
  (four outputs), [5] = 322 B; `ambed.order` warnings 0. PCM entries with
  signal (`ambed.entry.pcm_peak` > 0): 1307 of 1468 on xlxd-sut.
- xlxd fixed legacy outputs: in 1 -> [2,3,4], in 2 -> [1,3,4], in 3 ->
  [1,2,4]. No IMBE input occurs (the rig has no P25 client), so the 30 B
  reply to an IMBE-input legacy open is not observed.

```
AMBEd transcoder protocol, CAPS reply v1 max=4 mask=0x3e
    Tag: AMBEDCAPS
    CAPS version: 1
    Max outputs: 4
    Codec mask: 0x0000003e
AMBEd transcoder protocol, OPEN ext N0TEST in=3 outs=[5,5] tx=0x00007000
    [Expert Info (Note/Protocol): invalid open: duplicate output 5]
AMBEd transcoder protocol, BUSY tx=0x00007000
    Tag: AMBEDBUSY
    [Message: BUSY extended (10)]
    Transaction: 0x00007000
AMBEd transcoder protocol, OPEN legacy(17) N0TEST in=3 outs=[1]
AMBEd transcoder protocol, BUSY
    Tag: AMBEDBUSY
    [Message: BUSY (9)]
AMBEd transcoder protocol, STD id=16 port=10116 in=3 out0=5 tx=0xa1b2c3d4
    Stream id: 16
    Stream data port: 10116
AMBEd transcoder protocol, CS id=16
AMBEd transcoder protocol, data reply pid=0 outs=[1,2,3]
    Entry 0: AMBE+ (D-STAR), 9 bytes
    Entry 1: AMBE2+ (DMR/YSF), 9 bytes
    Entry 2: Codec2 3200 (M17), 8 bytes
```

Expert info (`-q -z expert`): no AMBEd errors or warnings in any file;
notes only on transcoder-sut.pcapng: 9 `ambed.busy` and 6
`ambed.invalid_open`. Other warnings are mDNS/ARP from the host bridge and
restarted containers.
