<!-- SPDX-License-Identifier: MIT -->
# Production checks of a live reflector: echo voice check, public web listen, AllStar phone call, browser Talk

Four small tools for the owner-approved check of a LIVE DV-Router reflector (the production host), as opposed to
the rig. They are stdlib or image-only code, unit-tested offline against loopback fakes
(`cases/unit/test_prod_echo.py`, `test_prod_listen.py`, `test_prod_iax.py`, `test_prod_talk.py`), and none has a
host name, an address, a callsign, an account or a secret built in: they are arguments. In this document the host is
`HOST` ("the production host"), callsigns are `N0CALL` / `N0TEST`, addresses `192.0.2/24`.

| Tool | What it does |
|------|--------------|
| `python3 -m dvtest.tools.prod_echo` | per protocol: link one client to the echo module E, send ONE over of 3.0 s, judge the replay, unlink |
| `python3 -m dvtest.tools.prod_listen` | ONE anonymous WebRTC Listen on module E of the public web service, measures what arrives (run it during one echo over) |
| `python3 -m dvtest.tools.prod_iax` | deploy check **f**: an AllStar phone call on the host (IAX2 to 127.0.0.1:4569): register, dial module E, 3 s of 700 Hz, judge the echo replay, hang up ([below](#production-check-f-allstar-phone-call-prod_iax)) |
| `python3 -m dvtest.tools.prod_talk` | deploy check **g**: a browser Hold Talk on module E with a fake 700 Hz microphone in headless Chromium, judge the replay in the page's analyser ([below](#production-check-g-browser-hold-talk-prod_talk)) |

Checks f and g are the two voice-path deploy checks of the DV-Router lead's decision (production tool tests, deploy
checks a-e plus f and g): they run on the host, by the deploy session, only for images that change the voice path, the
AllStar edge, the WebRTC edge, the web service or the launcher, always against module E (the built-in echo) and with an
account kept for the purpose. Both take under two minutes, print one JSON line per check plus a summary line, and keep no
audio.

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

## Production check f: AllStar phone call (prod_iax)

What a phone client (DVSwitch Mobile in "Phone mode IAX2 connection") does, as a tester endpoint
(`dvtest.proto.iax2_reg` for the registration, `dvtest.proto.iax2.Client` for the call; docs/asl-dvswitch.md). Run it ON
the host, against its own IAX2 port; the account secret is a 0600 file the deploy session already holds.

| Step | On the wire | Asserted (check name) |
|------|-------------|-----------------------|
| register | REGREQ (empty CALLTOKEN), CALLTOKEN, REGREQ (token), REGAUTH (md5 challenge), REGREQ (MD5_RESULT) | `regack`: the answer is REGACK |
| dial | NEW (VERSION 2, CALLED_NUMBER = the dial string, CALLING_NAME = the callsign, USERNAME = the account, FORMAT and CAPABILITY ulaw, no CALLING_NUMBER), CALLTOKEN, NEW (token), AUTHREQ, AUTHREP (md5), ACCEPT, CONTROL ANSWER | `answer`: ACCEPT and ANSWER within 8 s |
| talk | CONTROL RADIO_KEY, 150 ulaw frames of 160 bytes every 20 ms (3.0 s of a pure 700 Hz sine, amplitude 8000: the first a full voice frame, then mini frames), CONTROL RADIO_UNKEY | |
| replay | the edge sends the echo about 1 s after the unkey; frames counted from the unkey on, decoded from ulaw | `replay_frames`: at least 140 of 150 (scaled for another `--seconds`: ceil(sent x 140 / 150)); `replay_frequency`: dominant frequency 700 Hz within 20 Hz; `replay_level`: rms above 300 of 32768 |
| hang up | IAX HANGUP, socket closed (also after an error or Ctrl-C) | `hangup` |
| optional | the hub metrics text read before and after (GET), the public state polled during the call (GET) | `hub_metrics`, `public_state` |

The dominant frequency is a stdlib Goertzel scan (`dvtest.tools.prod_common.dominant_frequency`): a Hann window over the
middle 0.25 s in 10 Hz steps from 100 to 3400 Hz, then 1 Hz steps within +-15 Hz of the peak over the middle 1 s; unit
tests run it on synthetic tones (300 to 3300 Hz, a mix, noise, silence) and on the ulaw round trip.

**The dial string (an assumption, `--dial`).** The phone client dials the NODE NUMBER of the module, and the edge maps it to
the module of the matching `[[node]]` row, allowed by the account's rooms list (DV-Router ASL1f design: "CALLED_NUMBER = the
node number mapped to the module of that [[node]] row"). The production host has one node number per module: A..H are
400161..400168 and X is 400169 (ASL1b facts), so the default for module E is `400165`
(`dvtest.tools.prod_iax.node_for_module`). If the deployed config numbers its nodes differently, pass `--dial NUMBER`; a
dial string that is not the default for E needs `--allow-other-module` too (the tool cannot know which module it reaches,
so it never accepts one silently). The tool could not be tried against the real edge offline: the first real run confirms
the form.

### Safety rails of prod_iax

Enforced before the first packet, each with a unit test:

- `--i-have-owner-approval` is required for every run that is not `--dry-run`; without it exit 2 and nothing is sent.
- Module E only. `--module X` needs `--allow-other-module`, and so does a `--dial` that is not the default node of E; the
  flag alone changes nothing. The tool never keys another module otherwise.
- `--host` must be a loopback address (default 127.0.0.1) unless `--allow-non-loopback-host` is given.
- The secret: `--secret-file PATH` (a regular file, not a symlink, mode 0600 or stricter: any group or other bit is refused,
  with the path and mode in the message, never the content) or the environment variable `DVT_PROD_IAX_SECRET`. There is no
  `--secret VALUE` option (it would show in `ps`), and option abbreviations are off (`--secret` is an error, not
  `--secret-file`). The secret is read only for a real run, goes only into the MD5 digest of the IAX2 authentication, and is
  registered with a redactor that replaces it by `***` in every line printed and every file written; a unit test runs the
  whole flow and greps stdout, stderr, `results.json`, `summary.txt` and the md5 of it for the secret, also after a refused
  registration.
- `--seconds` 3.0 by default, cap 5.0, floor 1.2 (the echo never replays under 50 frames); a global timeout (`--timeout`, 120 s).
- `--dry-run` opens no socket, resolves no name, reads no secret and writes no file; the file mode is still checked.
- `--hub-metrics` and `--public-state-url` are plain GETs without redirects; an https certificate and host name are always
  verified (`--ca-file` replaces the system roots, for a lab CA); the query part of a URL is never printed.

### Run (f)

```sh
# 1. the plan: first REGREQ / NEW / voice frame as hex, nothing is sent
python3 -m dvtest.tools.prod_iax --account ACCOUNT --callsign N0CALL --secret-file /path/to/iax.secret --dry-run

# 2. the check (the owner approved it), on the host, with the optional reads
python3 -m dvtest.tools.prod_iax --account ACCOUNT --callsign N0CALL --secret-file /path/to/iax.secret \
  --i-have-owner-approval \
  --hub-metrics http://127.0.0.1:METRICS-PORT/metrics \
  --public-state-url https://WEB-HOST/api/v1/public/state
```

`ACCOUNT` is the IAX2 account name (USERNAME of REGREQ and NEW), `--callsign` the CALLING_NAME and the callsign the public
state must show. Output: one JSON line per check on stdout (`{"tool": "prod_iax", "check": "regack", "pass": true,
"detail": ..., ...}`), a last `{"summary": true, "pass": ..., "passed": N, "failed": N, "skipped": [...]}` line, a progress
log and the table below on stderr, and in `--out` (default `out/prod-iax-<utc>/`) `results.json` and `summary.txt`. Exit code 0:
all passed (or dry run), 1: a check failed, 2: usage error.

```
regack            PASS  REGACK after 0.12 s (REGREQ > CALLTOKEN > REGREQ:token > REGAUTH > REGREQ:md5 > REGACK)
answer            PASS  ACCEPT and ANSWER after 0.21 s
hangup            PASS  HANGUP sent, socket closed
replay_frames     PASS  150 of 150 frames back (need 140), first 1.03 s after the unkey, over 2.98 s
replay_frequency  PASS  dominant 700.0 Hz (want 700 +- 20), tone fraction 0.99
replay_level      PASS  rms 5560 (floor 300)
hub_metrics       PASS  deltas {"setup_ok": 1, "voice_rx": 152, "voice_tx": 150}
public_state      PASS  named N0CALL on module E in 6 polls; protocol allstar
result: 8 passed, 0 failed, 0 skipped
```

(The block shows the shape and the order of the lines, not a result. Without `--hub-metrics` / `--public-state-url` the two
lines read `skip  no ... given`.)

`hub_metrics` reads the Prometheus text before the call and after it (the edge publishes its counters to the hub about every
10 s and the hub caches them, so it polls up to `--metrics-wait` 30 s) and passes when
`dv_edge_allstar_setup_total{kind="client",result="ok"}` rose by at least 1 and `dv_edge_allstar_frames_total{dir="rx",kind="voice"}`
and `{dir="tx",kind="voice"}` both rose. `public_state` polls `GET /api/v1/public/state` twice a second from the answer to the
end of the replay and passes when `rooms[].onair.callsign` of module E is the callsign (compared by base callsign; a
different suffix on both sides does not match). Both are skipped with a note when the URL is not given.

## Production check g: browser Hold Talk (prod_talk)

What a signed-in user does on the dashboard (DV-Router `web/README.md`, "Stable browser rig hooks"; the selectors are
imported from `cases/web_pages.py`, not copied): headless Chromium, a fake microphone that plays a 700 Hz WAV in a loop,
sign in, Listen on module E, Hold Talk for 3 s, release, hear the echo.

| Step | Page side | Asserted (check name) |
|------|-----------|-----------------------|
| sign in | `POST /api/v1/auth/session` from inside the page (the session is bound to the browser's User-Agent), then `/#/` | `signin`: HTTP 200 (the check user must be an ordinary user: no authenticator step) |
| Listen | `listen-room` = E, `listen-toggle`, `listen-connection` `data-state` | `listen`: data-state `connected` and the line "Listening on module E"; `talk_button`: `talk-ptt` offered (the user has may_tx on E) |
| press | the RTC probe's audio tap started (`dvtest.browser.RTC_PROBE_JS`), mouse down on `talk-ptt` | `on_air`: `#talk-status` is "On air" within 30 s (a "Denied: ..." text or the microphone message is named) |
| hold | the button stays down for `--hold` seconds (3.0) | |
| release | mouse up | `released`: the status is "Ended: Button released." |
| replay | the tap is cleared at the release; 1 s windows of the received audio are analysed (Hann + Goertzel scan, the page's own algorithm) for up to 9 s | `replay_level`: a window with rms above the floor (0.01 of full scale 1.0); `replay_frequency`: that window's peak 700 Hz within 20 Hz and tone fraction at least 0.1 (echo cancellation and AGC stay on, so the tone is judged loosely, as in the browser cases) |
| optional | `--public-state-url` polled while the button is held; `--recorder-url` read from the signed-in page | `public_state`: `rooms[].onair.callsign` of E names the account callsign; `recorder`: a row of the user's recordings for this over (callsign, start after the press) with `protocol` `webrtc` |
| end | Disconnect, sign out, context and browser closed (in a `finally`) | |

Selectors used (all from `cases/web_pages.py`): `listen_panel` / `listen-connection`, `listen_room_select` / `listen-room`,
`listen_button` / `listen-toggle`, `listen_status` (`p[role=status]` of the card, fallback), `talk_button` / `talk-ptt`,
`talk_status` (`#talk-status`, fallback), texts `TALK_ON_AIR_TEXT`, `TALK_RELEASED_TEXT`, path `LOGIN`. They were checked
against the current `web/README.md` hooks and `web/site/public.js` of the DV-Router working tree (read only); if the front end
renames a hook, `web_pages.py` is the one place to change and the browser cases follow.

### Safety rails of prod_talk

- `--i-have-owner-approval` is required for every run that is not `--dry-run` or `--print-docker`.
- Module E only (`--room X` needs `--allow-other-module`); `--hold` 3.0 by default, cap 5.0, floor 1.5; a global timeout (150 s).
- The password: `--password-file PATH` (0600 or stricter, a regular file, no symlink) or `DVT_PROD_TALK_PASSWORD`; no `--password`
  option, no option abbreviations. It is passed to the page's login `fetch` and nowhere else, scrubbed from every line and
  file (exceptions included), and no trace, HAR or video is recorded. A failed run keeps one screenshot, `failure.png`. Do not
  set `DEBUG=pw:api` for the run (Playwright's own debug log prints call arguments).
- The certificate and host name of the web service are verified. `--ca-file FILE` (the lab CA) is the only change: the browser
  cannot be given a CA through Playwright, so with it the tool first GETs the site with that CA verified and then lets the
  browser ignore certificate errors. Never use it for the public host.
- The browser endpoint must be loopback or a single-label container name on a local docker network, unless
  `--allow-remote-browser` (a `run-server --unsafe` accepts launch arguments from any client and must not be reachable from
  outside).
- `--recorder-url` must be on the same origin as `--url` (it is read from the signed-in page), `--public-state-url` is https only.
- `--dry-run` opens no connection and writes no file; `--print-docker` prints the docker lines below and runs nothing.
- The microphone WAV (`<out>/media/fake-mic.wav`, 10 s of 700 Hz, 48 kHz mono 16 bit, written with the stdlib `wave` module)
  is generated at run time and removed at the end; `*.wav` and `out/` are git-ignored, no audio is committed.

### Two ways to get a browser

**(1) The browser image through `--ws-endpoint` (the default way on the host).** `rig/Dockerfile.browser` is the official
Playwright image with `playwright run-server`; the rig's `browser` profile builds it as `dvt-browser:local` (`docs/rig.md`
"Browser profile"), and the tester image `dvt-tester:local` carries the matching Playwright Python client (one
`PLAYWRIGHT_VERSION` pin feeds both). Both images must exist on the host before the first run; this tool builds nothing. The browser runs on a private docker network and publishes no port;
the tool runs in the tester image on the same network and connects to `ws://dvt-prod-browser:3000`. `--print-docker` prints
exactly these lines (NOT run by the tool; `$OUT` is the run directory, its `media/` sub-directory is mounted read-only into the
browser at `/media` and receives the generated WAV while the browser is up; the limits are `dvtest.limits.docker_run_args()`,
so `DVT_CTR_MEM`, `DVT_CTR_CPUS` and `DVT_CTR_PIDS` apply; the browser defaults to 3g / pids 1024 like `rig/compose.yml`,
the client to 1g / 512):

```sh
python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file ./pw --print-docker
# prints:
OUT="$PWD/out/prod-talk-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$OUT/media"
docker network create dvt-prod
docker run -d --rm --name dvt-prod-browser --init --network dvt-prod --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 3g --memory-swap 3g --cpus 2 --pids-limit 1024 --shm-size 1g --tmpfs /tmp:rw,size=512m --tmpfs /var/tmp:rw,size=256m \
  -v "$OUT/media:/media:ro" dvt-browser:local --host 0.0.0.0 --port 3000 --unsafe
docker run --rm --network dvt-prod --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 1g --memory-swap 1g --cpus 2 --pids-limit 512 --tmpfs /tmp:rw,size=64m -e HOME=/tmp --user "$(id -u):$(id -g)" \
  -v "$PWD:/dvt:ro" -v "$OUT:/out" -v "$PWD/pw:/run/pw:ro" -w /dvt dvt-tester:local \
  python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file /run/pw \
  --ws-endpoint ws://dvt-prod-browser:3000 --out /out --room E --hold 3 --i-have-owner-approval
docker stop dvt-prod-browser; docker network rm dvt-prod
```

The password file keeps its host mode (0600, owned by the host user, who is also the container user), so the tool's mode check
runs inside the container too. If the host cannot reach its own public address from a bridge network (no NAT hairpin), run
both containers with `--network host` instead of `--network dvt-prod`, drop the network commands, start the browser with
`--host 127.0.0.1 --port 3000` and use `--ws-endpoint ws://127.0.0.1:3000`. (Neither variant could be run offline; see "Not
covered offline".)

**(2) A local Playwright install.** With `pip install playwright==<PLAYWRIGHT_VERSION from .env.example>` and
`playwright install chromium` on the host, leave out `--ws-endpoint`: the tool launches a headless Chromium itself with the
fake-microphone flags (`--use-fake-ui-for-media-stream --use-fake-device-for-media-stream
--use-file-for-fake-audio-capture=<out>/media/fake-mic.wav --autoplay-policy=no-user-gesture-required`).

### Run (g)

```sh
# 1. the plan, nothing is opened or written
python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file /path/to/pw --dry-run

# 2. the check (local Chromium, way 2); with way 1 add --ws-endpoint ws://dvt-prod-browser:3000
python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file /path/to/pw --i-have-owner-approval \
  --public-state-url https://WEB-HOST/api/v1/public/state \
  --recorder-url https://WEB-HOST/api/v1/user/recordings
```

`USER` is the login callsign of the check user; `--callsign` names the callsign the public state and the recorder must show if
it differs from the login. Output as for f (one JSON line per check, a summary line, `results.json` and `summary.txt` in
`--out`, default `out/prod-talk-<utc>/`; `facts` in `results.json` has the browser build, the audio tap rate, what the page heard
during the hold and the analyser windows' verdict):

```
signin            PASS  HTTP 200
listen            PASS  Listening on module E
talk_button       PASS  Hold Talk offered and enabled
on_air            PASS  On air
released          PASS  talk status 'Ended: Button released.' after the release
replay_level      PASS  best window rms 0.1712 (floor 0.01), heard 1.2 s after the release, 4 loud of 9 windows
replay_frequency  PASS  dominant 700.0 Hz (want 700 +- 20), tone fraction 0.84
public_state      PASS  named USER on module E in 6 polls; protocol webrtc
recorder          PASS  row protocol 'webrtc', reason 'terminator', 2980 ms
result: 9 passed, 0 failed, 0 skipped
```

(Shape, not a result.) The recorder line is only present with `--recorder-url`; it polls the user's recordings (2 requests per
second are allowed per login) for up to 30 s and fails with "is recording enabled?" when no row appears. The recorder needs
the check user to be allowed to browse recordings (an active user).

## Report table for f and g (one row per run)

| Check | Tool | Pass / fail | Counts | Measured | Out dir |
|-------|------|-------------|--------|----------|---------|
| f AllStar phone call | `prod_iax` | | regack, answer, hangup | frames back / 150, first delay (s), Hz, rms | `out/prod-iax-<utc>/` |
| f optional | `prod_iax` | | hub_metrics, public_state | deltas setup_ok / voice_rx / voice_tx, protocol | |
| g browser Hold Talk | `prod_talk` | | signin, listen, on_air, released | rms, first heard (s), Hz, tone fraction | `out/prod-talk-<utc>/` |
| g optional | `prod_talk` | | public_state, recorder | protocol seen, row protocol and reason | |

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

For f and g: `cases/unit/test_prod_iax.py` runs the whole prod_iax flow (registration, call, key, 150 voice frames, unkey, replay
judgement, hang up, the optional metric and public-state reads, every refusal and rail, the secret grep) against a small fake
edge and fake HTTP servers on 127.0.0.1; `cases/unit/test_prod_talk.py` runs the whole prod_talk flow against a SCRIPTED FAKE of
Playwright's sync API (so the order of the steps, the verdicts, the cleanup, the options handed to Playwright, the docker lines
and the secret grep are tested) and the pure parts for real (argument rules, file modes, the WAV, the audio verdict, the
recorder-row pick). Not covered, because it needs the production host or a real browser: the real edge's answer to this
dial string and account (the form of `--dial`, REGACK/ANSWER timing, the real echo delay, the counters' names and publication
delay), a real Chromium with the fake microphone (that Playwright is not installed in the unit environment, the real page's
`data-testid` hooks and status texts, the audio tap in headless Chromium, the real WebRTC path through the public proxy and
TURN, the echo-processed tone's fraction against the 0.1 threshold), the `docker run` lines (written from `rig/compose.yml`,
`rig/Dockerfile.browser` and `limits.docker_run_args()`, never executed in this work), the hairpin from a container to the host's
own public address, and the recorder row on a recorder-enabled build. Both tools are meant to be run once under supervision
(`--dry-run`, then a real run) before they join a checklist.
