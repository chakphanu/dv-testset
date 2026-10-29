<!-- SPDX-License-Identifier: MIT -->
# Wireshark dissectors

`dvtest.lua` is one Lua file with ten dissectors for the protocols the rig
speaks that Wireshark 4.4 does not decode itself:

| Protocol (filter) | Ports | Notes |
|-------------------|-------|-------|
| `ambed` AMBEd transcoder | UDP 10100 control, 10101-10199 data | follows the data port of every STD reply, also outside the range; heuristic on the `AMBED` tag on any UDP port |
| `usrp` USRP (chan_usrp) | UDP 32001-32003, 34001-34003 | heuristic on the `USRP` eye on any UDP port |
| `dextra` DExtra (D-STAR) | UDP 30001 | DSVT header/voice/last, link, ACK/NAK, unlink, keepalive |
| `dplus` DPlus (REF) | UDP 20001 | connect, login, keepalive, DSVT header/voice/last behind the 2-byte prefix (minimal) |
| `dcs` DCS | UDP 30051 | link, ACK/NAK, unlink, keepalives, 100 B voice with header and counter (minimal) |
| `ysfref` YSF reflector link | UDP 42000 | YSFP/ack/YSFU/YSFS/status, YSFD header fields; FICH shown coded (minimal) |
| `mmdvm` MMDVM/Homebrew DMR | UDP 62030 | login tags and DMRD fields (minimal) |
| `m17ref` M17 reflector link | UDP 17000 | CONN/ACKN/NACK/PING/PONG/DISC and 54 B voice (minimal) |
| `nxdnref` NXDNReflector link | UDP 41400 | NXDNP/NXDNU, NXDND flags, LICH, FACCH1 message/ids (minimal) |
| `p25ref` P25Reflector link | UDP 41000 | poll/unlink, LDU1/LDU2 records with LCF, TG, source id, ALGID, IMBE; terminator (minimal) |

IAX2 is built into Wireshark and is not duplicated. All ten are also in
Analyze > Decode As (UDP port).

## Use

In the rig, no host install needed:

```sh
scripts/tshark.sh -r out/<utc>/capture/xlxd-sut.pcapng -Y ambed
```

`scripts/tshark.sh` runs the pinned tshark of the capture image and loads
this file. With a local Wireshark, either pass it per run
(`tshark -X lua_script:tools/wireshark/dvtest.lua ...`) or copy it into the
personal Lua plugin folder (Help > About > Folders > Personal Lua Plugins).
Lua plugins are not loaded when tshark runs as root.

## Fields

Every message gets a generated `<proto>.msg` / `dextra.kind` field, so one
filter selects one message type:

| Filter | Selects |
|--------|---------|
| `ambed.msg == 1` / `2` | PING / PONG |
| `ambed.msg == 3` / `4` | CAPS request / reply |
| `ambed.msg == 5` / `6` | OPEN legacy / extended |
| `ambed.msg == 7` / `8` | STD 14 B / 18 B |
| `ambed.msg == 9` / `10` | BUSY 9 B / 13 B |
| `ambed.msg == 11` | CS |
| `ambed.msg == 20` / `21` | data request / reply |
| `ambed.entry.codec == 5` | data entries carrying PCM |
| `usrp.keyup == 1` / `usrp.keyup == 0` | voice / unkey |
| `dextra.kind == 1` / `2` / `3` | DSVT header / voice / last frame |
| `mmdvm.tag == "DMRD"` | DMR voice and data bursts |
| `m17ref.eot == 1` | last M17 voice packet |
| `nxdnref.flags.header == 1` / `nxdnref.tag == "NXDNP"` | NXDN header / poll |
| `p25ref.kind == "terminator"` / `p25ref.src` | P25 call end / records carrying the source id |

Generated helpers: `ambed.setup_frame` (frame of the STD that set the data
port up), `ambed.entry.pcm_peak` and `usrp.pcm_peak` (largest absolute
sample of a 320 B PCM payload, 0 = silence).

Expert info: `*.malformed` (error) for a wrong length or unknown tag or
codec, `ambed.order` (warning) when a reply entry differs from the opened
output list, `ambed.busy` (note) on every BUSY, `ambed.invalid_open` (note)
on an open the reference ambed refuses (blank callsign, bad codec, duplicate
or gap in the outputs, output equal to input, no output). Valid rig traffic shows no
errors or warnings: `scripts/tshark.sh -r <pcapng> -q -z expert`.

## Limits

- AMBEd data packets are told apart by the stream set up in an earlier STD
  in the same capture. Without it (capture started mid-stream) a packet is
  shown as `data` and parsed as a reply.
- Stream id and port are decoded little endian, like every known ambed build.
- MMDVM, M17, NXDN and P25 are minimal: no embedded LC, LSF, CRC, Golay or
  Reed-Solomon checks, and voice frames are shown as bytes.
