<!-- SPDX-License-Identifier: MIT -->
# Production checks of a live reflector: echo voice check and public web listen

Two small tools for the owner-approved check of a LIVE DV-Router reflector (the production host), as opposed to
the rig. Both are stdlib or image-only code, both are unit-tested offline against loopback fakes
(`cases/unit/test_prod_echo.py`, `cases/unit/test_prod_listen.py`), and neither has a host name, an address, a
callsign or a secret built in: they are arguments. In this document the host is `HOST` ("the production
host"), callsigns are `N0CALL` / `N0TEST`, addresses `192.0.2/24`.

| Tool | What it does |
|------|--------------|
| `python3 -m dvtest.tools.prod_echo` | per protocol: link one client to the echo module E, send ONE over of 3.0 s, judge the replay, unlink |
| `python3 -m dvtest.tools.prod_listen` | ONE anonymous WebRTC Listen on module E of the public web service, measures what arrives (run it during one echo over) |

## Safety rails of prod_echo

The tool keys a module of a live reflector, so every one of these is enforced before the first packet and has a
unit test:

- `--i-have-owner-approval` is required for every run that is not `--dry-run`; without it the tool exits with 2 and
  an explanation, and sends nothing.
- The module is `E` (the echo module). Any other module needs `--module X --allow-other-module`; the flag alone
  changes nothing, and `--module` must be one capital letter.
- `--seconds` is 3.0 by default (150 frames), hard cap 5.0, floor 1.2 (an over under 50 frames is never replayed).
- Protocols run one after the other (`--one-at-a-time` is accepted for explicitness), with a settle pause of at
  least 6 s between two of them (`--settle`, minimum 6).
- A global timeout (`--timeout`, default 300 s): a protocol not started by then is reported as skipped. Every wait
  is bounded; the unlink is sent in a `finally` block, also after a failed link, an error or Ctrl-C.
- `--dry-run` builds the packets without opening a socket and without resolving the host name, and writes no file.
- Callsigns are never truncated silently: a callsign longer than the protocol field (DCS 7, D-STAR 8, YSF 10,
  M17 9) is a usage error.
- The DMR Homebrew password is read from `--dmr-pass-file`; it is not printed, logged or written (a unit test greps
  the output and every file of the run for it).

## Run

```sh
# 1. see the plan: packets first and last of each step, nothing is sent
python3 -m dvtest.tools.prod_echo --host HOST --dry-run \
  --callsign dextra=N0CALL-T --callsign dplus=N0CALL --callsign dcs=N0CALL --callsign ysf=N0CALL --callsign m17=N0CALL

# 2. the check (the owner approved it); the default protocols are dextra,dplus,dcs,ysf,m17
python3 -m dvtest.tools.prod_echo --host HOST --i-have-owner-approval \
  --callsign dextra=N0CALL-T --callsign N0CALL --reflector XLX999

# one protocol, a 4 s over
python3 -m dvtest.tools.prod_echo --host HOST --i-have-owner-approval --protocol ysf --callsign ysf=N0CALL --seconds 4
```

`--callsign VALUE` is the default and `--callsign PROTO=VALUE` (repeatable) overrides it for one protocol, so each
protocol can get its own form (a D-STAR gateway form with a suffix, a plain callsign for YSF and M17).
`--port PROTO=N` overrides a port. `--reflector NAME` is the name written into RPT2 of the D-STAR headers (default
`XLX999`; the reflector takes any). Exit code: 0 all passed (or dry run), 1 a protocol failed, 2 usage error.

Output: one JSON line per protocol on stdout as soon as it is judged, a progress log and a table on stderr, and in
`--out` (default `out/prod-echo-<utc>/`, ignored by git) `<protocol>.json`, `results.json` and `summary.txt`.

```json
{"protocol": "dextra", "host": "HOST", "port": 30001, "module": "E", "callsign_used": "N0CALL-T",
 "sent_frames": 150, "received_frames": 150, "duration_s": 2.98, "replay_delay_s": 1.04, "byte_identical": true,
 "rms": null, "correlation": null, "pass": true, "reason": "ok", "...": "linked, unlinked, replays, stream_id, ..."}
```

## What is measured and when it passes

The over is the one of E1 (`cases/test_echo.py`): deterministic PRNG codewords of the protocol's codec
(`dvtest.echo.make_frames`, seed `prod-echo-<protocol>`), sent by the repository's own client of the protocol
(`dvtest.proto.*`), paced at the protocol's rate. The same client listens: the echo replays to the sender too (E1d).
The replay is judged from the packets that arrived after the last packet of the over:

| Check | Pass when |
|-------|-----------|
| link | the protocol's acknowledgement arrives (DExtra / DCS ACK, DPlus login OK, YSF poll ack, M17 ACKN, DMR RPTACK x3) |
| replays | exactly one over starts after the end of ours (`Over` of `dvtest.echo.split_overs`, gap 0.4 s) |
| `replay_delay_s` | 0.8 to 2.5 s from the last packet sent to the first packet of the replay (design `delay_ms` 1000; E1 allows 0.8 to 2.0) |
| `duration_s` | first to last voice packet of the replay within -0.5 s / +0.6 s of the over length (E1: 2.5 to 3.6 for 3 s) |
| `received_frames` | all sent frames heard, at most 12 more (M17 repeats, DMR pads to bursts of 3, YSF to packets of 5) |
| `byte_identical` | the sent frames sit contiguously and in order in the replay (offset reported as `payload_offset`; D-STAR 0) |
| end | the replay ends with the protocol's end of stream |
| unlink | the unlink packets were sent (and the client is closed) |

`rms` and `correlation` are `null` for every protocol: there is no codec decoder in `dvtest/audio` (decoding AMBE
and Codec2 goes through the AMBEd transcoder or an external codec2 / mbelib, neither of which belongs to this
stdlib-only tool), so the verdict is frame count, duration, timing and byte identity. The result says so in
`metric_note`. (The frames are PRNG codewords, so decoded they are noise, not speech; a decoded-audio metric would
only prove non-silence. That is what `prod_listen` measures at the web edge.)

YSF: the payload is recorded but NOT part of the verdict. The YSF egress of a foreign-origin (replayed) stream
re-encodes the AMBE+2 frames and steps the gain index b2 (docs/echo-cases.md, E1c); PRNG frames are not valid AMBE+2
codewords, and encoding valid ones needs the transcoder (E1 uses it on the rig). Frame count, duration and timing
still count. The note is added to `metric_note` when the payload differs.

## Protocols

| Protocol | Default port | In the default list | Notes |
|----------|--------------|---------------------|-------|
| `dextra` | 30001 | yes | callsign up to 8; `--callsign dextra=N0CALL-T` style |
| `dplus` | 20001 | yes | callsign up to 8; the reflector may need the callsign registered for DPlus; the login OK comes after 0 to 2 s |
| `dcs` | 30051 | yes | callsign up to 7 (the DCS wire field), ACK after 0 to 2 s |
| `ysf` | 42000 | yes | callsign up to 10; poll until acknowledged, join by DG-ID of the module; payload not asserted (above) |
| `m17` | 17000 | yes | callsign up to 9; the replay carries the repeat of packet 0 (payload offset 2) |
| `dmrhb` | 62030 | no | DMR Homebrew (MMDVM) with a real login: needs `--dmr-id N` (a repeater id the owner added) and `--dmr-pass-file FILE`; RPTL, RPTK = sha256(salt + password), RPTC, keepalive, join of the talkgroup 4001 + module index (E = 4005); unlink = an empty stream to TG 4000 and RPTCL |
| `g3` | 40000 | not supported | the reflector sends voice to UDP 40000 of the CLIENT address, so the client must be reachable from the internet; a normal test host is behind NAT or a firewall, and the G3 presence handshake has no unlink packet |
| `imrs` | 21110 | not supported | a peer list: the reflector needs the tester's address in its own IMRS destination list (the owner edits the reflector); no link or unlink packet, no client role |

Timing of one protocol with the defaults: link 1 to 5 s, 0.5 s settle, the over (3.0 s; DMR waits 1.2 s after the
header, D-STAR 0.3 s), the replay starts about 1 s after the last packet and lasts about 3 s, 1.5 s of quiet watch for
a second replay, 1 s after the unlink. About 12 to 20 s per protocol and 6 s between them: five protocols take
about two minutes.

## Public web listen (prod_listen)

What the dashboard's Listen button does for a guest (DV-Router `web/README.md`, "Public Listen and Talk panel";
`docs/design/web.md`, "Signalling handoff"): a receive-only audio transceiver, `POST /api/v1/rtc/offer {sdp, room}`
with no login and no cookie, the answer applied, `POST /api/v1/rtc/alive {sess_id}` at once and every second until
six, then every 10 s (a guest circuit has a 15 s lease), and `POST /api/v1/rtc/close` at the end. The page offers
Listen to a guest only when the public state says `info.guest_listen = true` (shipped OFF, `[web] guest_listen`);
with it off the offer is refused and the tool reports that in `error` (and `guest_listen: false`, read from
`GET /api/v1/public/state` first). Limits that apply to this tool: 2 guests per address, 50 in all, 4 h per session.

It is a thin tool next to `webload_gen` (same aiortc, same `install_patches` receive path, same SDP fix for the relay,
same lease schedule), not a mode of it: the load generator logs in with accounts and counts marker frames, this one is
anonymous and decodes the audio. aiortc exists only in the image `dvt-webload:local` (`rig/Dockerfile.webload`, built
by the rig's `webload` profile); that image was built before this tool existed, so the repository's `dvtest` directory
is mounted read-only over the image's copy. TURN is not used (the page fetches `GET /api/v1/rtc/ice` first; this
tool needs direct UDP to the edge).

```sh
# from the root of the repository; normal network access (an external host), contained like docker_run_args()
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --memory-swap 512m --cpus 1 --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m --user 1000:1000 \
  -v "$PWD/dvtest:/app/dvtest:ro" \
  --entrypoint python dvt-webload:local -m dvtest.tools.prod_listen \
  --url https://WEB-HOST --room E --seconds 40
```

`python3 -m dvtest.tools.prod_listen --url https://WEB-HOST --print-docker` prints the same line from
`dvtest.limits.docker_run_args()` (so `DVT_CTR_MEM`, `DVT_CTR_CPUS`, `DVT_CTR_PIDS` apply; defaults 1g, 2, 512) and
`--dry-run` prints the plan. The certificate and the host name are always verified (system roots). `--ca-file FILE`
trusts only that CA instead of the system roots: the lab CA of the rig, never for a public host (in the container it
needs a second mount, `-v "$PWD/FILE:/ca.pem:ro"`, and `--ca-file /ca.pem`; `--print-docker` does both). There is no
switch to turn verification off.

During one echo over: start `prod_listen` first with `--seconds` 30 or more (one protocol needs about 20 s), wait for
`prod_listen: connected after ...` on stderr, then start `prod_echo`; the listener hears the echo's replay (and the
live relay of the over, when the web edge forwards module E). Output is one JSON line on stdout:

```json
{"packets": 312, "bytes": 49920, "rms": 2101.4, "peak": 7903, "silence_ratio": 0.48, "first_audio_s": 9.12,
 "connected": true, "error": null, "first_packet_s": 9.1, "max_gap_ms": 21.3, "timeline": [{"s": 9, "packets": 50, "rms": 3011.2}],
 "connect_s": 1.4, "guest_listen": true, "rooms_served": ["E"], "alive": {"204": 5}, "url": "https://WEB-HOST", "room": "E", "seconds": 40.0}
```

| Field | Meaning |
|-------|---------|
| `packets`, `bytes` | PCMU RTP packets received after the connection was up, and their payload bytes (160 per 20 ms) |
| `rms`, `peak` | over all decoded samples, 16 bit units (full scale 32768) |
| `silence_ratio` | share of the packets whose own rms is under `--silence-rms` (default 30, about -60 dBFS); 1.0 when nothing arrived |
| `first_audio_s` | seconds from the connection to the first non-silent packet (null: none) |
| `connected`, `error` | ICE + DTLS done; the first error, `null` when none |
| `first_packet_s`, `max_gap_ms`, `timeline` | first packet of any kind, the largest gap between consecutive packets, packets and rms per second |
| `guest_listen`, `rooms_served`, `alive` | from the public state; HTTP status counts of the lease renewals |

## Report table (fill one row per protocol)

| Protocol | Sent / received frames | Duration (s) | Delay (s) | Metric | Pass / fail | Out dir |
|----------|------------------------|--------------|-----------|--------|-------------|---------|
| dextra | 150 / 150 | 2.98 | 1.04 | byte identical; rms n/a | pass | `out/prod-echo-<utc>/` |
| dplus | | | | | | |
| dcs | | | | | | |
| ysf | | | | payload recorded | | |
| m17 | | | | | | |
| web listen | packets / silence_ratio | seconds heard | first_audio_s | rms / peak | connected, error | JSON line |

(The first row shows the shape, not a result.)

## Not covered offline

The unit tests run the whole prod_echo flow (link, over, replay judgement, unlink, planned packets equal sent
packets) for DExtra, DPlus, DCS, YSF, M17 and DMR Homebrew against echo fakes on 127.0.0.1, and the safety rails
(approval, module, seconds, dry run with a socket spy, global timeout). Not covered, because it needs the live host or
a browser-grade WebRTC stack: the real reflectors' link rules (DPlus callsign registration, DCS / DExtra suffix
forms, the DMR id and password), the real echo timing, the YSF payload through the real egress, and the whole of
`prod_listen.listen()` (aiortc offer / answer, ICE, DTLS, PCMU timing; only the offer creation was exercised in the
image with `--network none`). The pure parts (RTP parsing, rms / silence math, options, result document, the docker
command line, no network on `--dry-run`) are tested.
