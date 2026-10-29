---
name: dv-capture
description: Capture and decode dv-testset rig traffic (AMBEd transcoder link, USRP, DExtra, MMDVM DMR, M17, IAX2) with scripts/run.sh --capture, scripts/tshark.sh and the Lua dissectors in tools/wireshark/dvtest.lua. Use when asked to look at packets on the wire, confirm a protocol field or message flow, debug a transcoder or reflector exchange, or turn a capture into numbers for docs/protocols/.
---

<!-- SPDX-License-Identifier: MIT -->

# dv-capture

Read `.agents/skills/dv-testset/SKILL.md` first (levels, settings, traps,
PII check). Protocol layouts are in `docs/protocols/`, the rig in
`docs/rig.md`, dissector fields in `tools/wireshark/README.md`.

## Capture

```sh
scripts/run.sh --capture -m wire                                  # transcoder only
DVT_INSTANCE=5 scripts/run.sh --capture -m wire                   # parallel rig 5
scripts/run.sh --capture -m rig                                   # reflectors + transcoders
scripts/run.sh --capture --profile allstar -m "rig or allstar"    # + USRP A/B, IAX2
```

Writes `out/<utc>/capture/<service>.pcapng` for xlxd-ref, xlxd-sut,
transcoder-sut (+ allstar). Each sniffer sits in its service's network
namespace, so pick the file by the endpoint you care about:

| Traffic | File |
|---------|------|
| wire cases (tester -> transcoder) | `transcoder-sut.pcapng` |
| xlxd-sut <-> transcoder-sut | `xlxd-sut.pcapng` or `transcoder-sut.pcapng` |
| xlxd-ref <-> transcoder-ref | `xlxd-ref.pcapng` |
| DExtra / DMR / M17 clients | `xlxd-sut.pcapng`, `xlxd-ref.pcapng` |
| USRP C (tester) | `xlxd-sut.pcapng` |
| USRP A/B, IAX2 | `allstar.pcapng` |

Rules: the run lock serializes rig users, never bypass it or start compose
yourself; only project `dvt`; no host tshark/wireshark install (none is
needed); never commit or publish a pcap.

## Decode

`scripts/tshark.sh ARGS...` runs the pinned tshark of the capture image with
`tools/wireshark/dvtest.lua` loaded. Paths are relative to the repository
root (mounted read-only; `out/` writable).

```sh
f=out/<utc>/capture/xlxd-sut.pcapng
scripts/tshark.sh -r $f -q -z io,phs                 # protocol hierarchy
scripts/tshark.sh -r $f -q -z conv,udp               # who talked to whom
scripts/tshark.sh -r $f -q -z expert                 # must show no error/warn on valid traffic
scripts/tshark.sh -r $f -Y ambed                     # one line per AMBEd packet
scripts/tshark.sh -r $f -Y 'ambed.msg == 6' -V -O ambed   # extended opens, full decode
scripts/tshark.sh -r $f -Y usrp -O usrp -V | head -40 # first USRP frames
```

Traps:

- With `-Y`, `-c N` counts packets read, not matches: pipe to `head`.
- tshark 4.4 sets need commas or ranges: `{6,8,10}`, `{5..10}`, not `{6 8}`.
- Add `&& !icmp`: ICMP port-unreachable quotes a UDP packet and the quoted
  packet is dissected (and counted) too.
- Every file also has multicast noise from the host bridge (172.30.0.1:
  mDNS, SSDP, WS-Discovery 3702), ARP "duplicate IP" warnings after a
  container restart, and a few bridge-flooded packets for other hosts early
  on. Filter by address (`ip.addr == 172.30.0.21`) when counting. Never
  quote that noise; mDNS can carry host names.
- A sniffer shares its service's network namespace: a control-channel
  stop/kill ends that file, and after `start` the capture continues in
  `<svc>.2.pcapng`, `<svc>.3.pcapng`, ... Decode each, or merge them with
  `scripts/tshark.sh -r` per file and sum; the stop..start gap is only in
  the xlxd-side file.
- Parallel rigs: `DVT_INSTANCE=<n>` puts results in `out/<utc>-i<n>/` and
  addresses in `172.30.<n>.0/24`; adjust address filters.

Per-protocol display filters:

| Protocol | Filter examples |
|----------|-----------------|
| AMBEd | `ambed.msg == 4` (CAPS reply), `ambed.msg in {7,8}` (STD), `ambed.msg in {9..10}` (BUSY), `ambed.transaction == 0xa1b2c3d4`, `ambed.entry.codec == 5` |
| USRP | `usrp.keyup == 1`, `usrp.keyup == 0`, `usrp.pcm_peak == 0` (silent frames) |
| DExtra | `dextra.kind == 1` (header), `dextra.last == 1`, `dextra.my == "N0CALL  "` |
| MMDVM | `mmdvm.tag == "DMRD"`, `mmdvm.frame_type == 2`, `mmdvm.dst == 4002` |
| M17 | `m17ref.tag == "M17 "`, `m17ref.eot == 1` |
| IAX2 (built in) | `iax2`, `iax2.iax.subclass`, `iax2.voice.codec` |

Field extraction for counting (`-T fields`, one row per packet):

```sh
# message type histogram
scripts/tshark.sh -r $f -Y ambed -T fields -e ambed.msg | sort -n | uniq -c
# opens and their replies, side by side by transaction
scripts/tshark.sh -r $f -Y 'ambed.msg in {6,8,10}' -T fields -e frame.number -e ambed.msg -e ambed.transaction -e ambed.stream_id -e ambed.port
# UDP payload sizes per message type
scripts/tshark.sh -r $f -Y ambed -T fields -e ambed.msg -e udp.length | sort | uniq -c
# USRP sequence continuity per direction
scripts/tshark.sh -r $f -Y usrp -T fields -e ip.src -e usrp.seq -e usrp.keyup
```

Follow one exchange: `-q -z follow,udp,hex,<ip>:<port>,<ip>:<port>` (hex of
whole payloads; use for your own debugging, do not paste PCM or voice
payloads into docs).

## Turn a capture into doc evidence

1. Name the run id (`out/<utc>`), the file and the exact tshark command.
2. Quote numbers: packets per message type, UDP sizes, field values that
   cross-check what the case sent (callsign, codecs, transaction ids,
   stream ports, frame counts). `-T fields ... | sort | uniq -c` is the
   usual source.
3. Quote at most header lines of a `-V` decode; no payload hex beyond
   headers, no PCM, no AMBE frames.
4. State the `-z expert` result. Any `Malformed` or warning on valid traffic
   is a dissector or implementation bug: report it, do not hide it.
5. A message type that never shows up is written down as "not observed"
   with the reason, or a case is added that triggers it (dvtest owners).
6. No secrets (IAX2 auth payloads are fine to count, never to quote), no
   home paths, only N0CALL / N0TEST / XLX999 and rig addresses. Run
   `scripts/pii-check.sh`.
