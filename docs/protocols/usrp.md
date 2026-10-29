<!-- SPDX-License-Identifier: MIT -->
# USRP (app_rpt chan_usrp)

Raw 8 kHz PCM over UDP between an AllStarLink (app_rpt) node and xlxd. Each
xlxd module with a USRP endpoint binds its own UDP port and talks to exactly
one peer; there is no login or keepalive. xlxd turns received PCM into a
stream on the module (transcoded to AMBE+/AMBE2+/Codec2 through the extended
AMBEd open with PCM input) and sends PCM from other modes' streams back.

Dissector: `tools/wireshark/dvtest.lua`, protocol `usrp` (filter `usrp`).
Client: `dvtest/proto/usrp.py`.

## Rig endpoints (`rig/config/xlxd.usrp.in`)

| Module | xlxd-sut bind | Peer | Peer |
|--------|---------------|------|------|
| A | 172.30.0.11:34001 | 172.30.0.40:32001 | AllStar node 1901 (profile allstar) |
| B | 172.30.0.11:34002 | 172.30.0.40:32002 | AllStar node 1902 (profile allstar) |
| C | 172.30.0.11:34003 | 172.30.0.30:32003 | tester (no AllStar needed) |

All three use source callsign N0TEST. The file is rendered only when
`DVT_SUT_USRP=on` (`scripts/run.sh --profile allstar` sets it).

## Frame layout

Header, 32 B, all integers big endian:

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 4 | ASCII | eye `USRP` |
| 4 | 4 | u32 BE | sequence, +1 per frame sent on this endpoint |
| 8 | 4 | u32 BE | memory, 0 |
| 12 | 4 | u32 BE | keyup: 1 = PTT on (voice follows), 0 = unkey |
| 16 | 4 | u32 BE | talkgroup, 0 |
| 20 | 4 | u32 BE | type: 0 voice, 1 DTMF, 2 text, 3 ping, 4 TLV, 5 ADPCM, 6 ulaw |
| 24 | 4 | u32 BE | mpxid, 0 |
| 28 | 4 | u32 BE | reserved, 0 |

Payload of a keyed voice frame (type 0, keyup 1):

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 32 | 320 | s16 LE x 160 | 20 ms of PCM, 8 kHz mono |

| Frame | Size |
|-------|------|
| voice (keyup 1) | 352 B |
| unkey (keyup 0) | 32 B |

xlxd accepts only voice frames: size 32 or 352, every header byte from
offset 8 zero except the keyup low byte (offset 15) which is 0 or 1, and
keyup 1 exactly when the size is 352. Anything else (DTMF, text, TLV) is
dropped and logged once per endpoint. It sends frames in the same strict
form.

## Message flows

AllStar (or tester) keys up into xlxd module C:

```
peer (32003)                            xlxd-sut (34003)
 |-- USRP seq=n keyup=1 + PCM (352) --->|   first frame opens a stream
 |-- USRP seq=n+1 keyup=1 + PCM ------->|   every 20 ms
 |   ...                                 |
 |-- USRP seq=m keyup=0 (32) ---------->|   unkey ends the stream
 |   (or 100 ms without voice ends it)  |
```

Another mode talks on the module, xlxd keys the peer:

```
peer (32003)                            xlxd-sut (34003)
 |<-- USRP seq=k keyup=1 + PCM (352) ---|   PCM from the transcoder
 |<-- ... every 20 ms ------------------|
 |<-- USRP seq=j keyup=0 (32) ----------|   last frame, or 100 ms dry
```

xlxd never echoes a stream back to the USRP peer it came from.

## Timing (xlxd side)

From the xlxd source (`main.h`, `ctranscoder.cpp`, `cusrpprotocol.cpp`); the
frame and PING periods are confirmed in the Evidence section.

| Item | Value |
|------|-------|
| frame period | 20 ms |
| RX: no voice for | 100 ms = end of stream |
| TX: no PCM for | 100 ms = send unkey |

## Evidence

Run `out/20261029T135314Z` (`scripts/run.sh --capture --profile allstar -m
"wire or rig or allstar"`, xlxd-sut = PR tree with USRP endpoints A/B/C).

```sh
scripts/tshark.sh -r out/20261029T135314Z/capture/xlxd-sut.pcapng -Y "usrp && !icmp" \
  -T fields -e ip.src -e udp.srcport -e ip.dst -e udp.dstport -e udp.length \
  -e usrp.seq -e usrp.memory -e usrp.keyup -e usrp.talkgroup -e usrp.type \
  -e usrp.mpxid -e usrp.reserved -e usrp.pcm_peak
```

xlxd-sut.pcapng (whole run), per direction:

| Direction | Voice 352 B | Unkey 32 B | Seq | Voice period (median) |
|-----------|-------------|------------|-----|-----------------------|
| xlxd .11:34001 -> node .40:32001 | 333 | 4 | 0..336, always +1 | 20.1 ms |
| xlxd .11:34002 -> node .40:32002 | 543 | 9 | 0..551, always +1 | 20.1 ms |
| xlxd .11:34003 -> tester .30:32003 | 50 | 1 | 0..50, always +1 | 20.1 ms |
| node .40:32001 -> xlxd .11:34001 | 337 | 2 | 0..338, always +1 | 20.0 ms |
| node .40:32002 -> xlxd .11:34002 | 155 | 1 | 0..155, always +1 | 20.0 ms |
| tester .30:32003 -> xlxd .11:34003 | 100 | 2 | two talks, 0..50 each | 25.2 ms |

- All 1537 frames are type 0 (voice); memory, talkgroup, mpxid and reserved
  are 0 in every frame from xlxd, AllStar and the tester; keyup is 1 exactly
  in the 352 B frames and 0 in the 32 B frames. No other size was seen.
- The sequence runs on per endpoint and direction across key/unkey cycles
  (xlxd: one counter per endpoint for its whole life; AllStar likewise); the
  tester starts at 0 per talk.
- 1386 of 1518 voice frames carry signal (`usrp.pcm_peak` > 0).
- allstar.pcapng (its sniffer stops at the crash case's `kill allstar`, 86 s)
  shows the same frames from the node side: 303 + 3 from xlxd on 32001, 331
  + 2 and 155 + 1 from the node, same seq rules.

```
USRP (app_rpt chan_usrp), voice seq=0 keyup=1 peak=0
    Eye: USRP
    Sequence: 0
    Memory: 0
    Keyup: 1
    Talkgroup: 0
    Type: voice (0)
    MPX id: 0
    Reserved: 0x00000000
USRP (app_rpt chan_usrp), unkey seq=100
    Keyup: 0
```

Expert info: no USRP errors or warnings in xlxd-sut.pcapng or
allstar.pcapng. Not observed: DTMF, text, TLV, ping, ADPCM and ulaw frames
(neither xlxd nor the rig AllStar node sends them; xlxd drops them on
receive).
