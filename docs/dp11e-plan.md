<!-- SPDX-License-Identifier: MIT -->
# DP11e plan: a headless KISS-TCP client for the KISS server of DV-Router

Status: plan and client helper only. **No rig case is written**: the case list below waits for DV-Router to confirm the design is final.
Design under test: DV-Router's `design-233-dp11e.md` (read only, cited by name; its numbered sections are written "design 1" to "design 11" below).
DP11e puts a KISS TCP server into `dv-edge-ax25` (`[[edges.ax25.kiss_server]]`): an APRS app uses the node as its TNC.
Helper: `dvtest/kiss_app.py` (`AppKissClient`, `aprsdroid_frames`, `aprsdroid_init_bytes`, `AprsdroidReader`, `pinpoint_tx`), unit tests `cases/unit/test_kiss_app.py`.

## 1. Research: do APRSdroid and PinPoint offer KISS over TCP?

Read only: public source and documents were read through the web and one `curl` of raw GitHub files and one PDF into the scratchpad; nothing was installed.

### APRSdroid (Android): YES, strong evidence (source code)

Repository `github.com/ge0rg/aprsdroid`, branch master; last commit on master 2025-02-05 (sha 65fb5fa). Files below are at `raw.githubusercontent.com/ge0rg/aprsdroid/master/`:

| What | Where (line numbers counted on the files as read on 2026-11-10) |
|------|--------------------------------------------------------------|
| Protocol "TNC (KISS)" with a link type chosen by the preference `link` | `src/backend/AprsBackend.scala:113-115` (`"kiss" -> new ProtoInfo(... R.xml.proto_kiss, "link")`), link default `DEFAULT_LINK = "tcpip"` at `:16`; the link list (`bluetooth`, `usb`, `tcpip`) in `res/values/arrays.xml` (`p_link_e` / `p_link_ev`) |
| The link `tcpip` is served by `TcpUploader` | `src/backend/AprsBackend.scala:86-91` (`"tcpip" -> new BackendInfo((s, p) => new TcpUploader(s, p), R.xml.backend_tcptnc, ...)`); the old name `tcptnc` maps to `kiss-tcpip-tcp` at `:50` |
| The host field is one string `host[:port]`, summary "KISS TCP server to contact" | `res/xml/backend_tcptnc.xml:8-15` (`tcp.server`), `res/values/strings.xml:411-412` (`p_tcptnc_server_summary`, `p_tcptnc_entry`) |
| The port: `host:port` parsed; **without a port the default is 14580** (the APRS-IS port, not a KISS port) | `src/AprsPacket.scala:96-105` (`parseHostPort`), `src/backend/TcpUploader.scala:117` (`parseHostPort(hostport, 14580)`) |
| Plain TCP socket for KISS (TLS only for `aprsis`); keepalive on; the KISS reader/writer is created on the socket streams | `src/backend/TcpUploader.scala:113-132` |
| KISS framing: sends `FEND 0x00 <AX.25> FEND` (**channel 0 only**) | `src/tncproto/KissProto.scala:83-87` (`writePacket`) |
| KISS reading: FEND ends a frame; the byte `0x00` at the START is the ignored data command, **any other byte is frame content**, so a frame of a channel other than 0 (type byte `0x10`, ...) corrupts the first address byte and is dropped | `src/tncproto/KissProto.scala:39-81` (`case 0 =>` at `:62`, "ignoring command byte" at `:67`) |
| Optional **TNC init string**: text lines (URL-decoded, split on newlines) written to the socket + CRLF at connect, with a delay after each line; default empty | `src/tncproto/KissProto.scala:22-32`, `res/xml/proto_kiss.xml:8-22` (`kiss.init`, `kiss.delay` 300 ms) |
| **Socket timeout 120 s by default** (`tcp.sotimeout`, "0 = disable"): a SocketTimeoutException makes the app drop the link and reconnect after `RECONNECT = 30` s | `src/backend/TcpUploader.scala:19-20,131,173-176`, `res/xml/backend_tcptnc.xml:17-23`, `res/values/strings.xml:348-350` |
| Transmitted frame: source = the app's callsign (more than 6 characters refused), destination `APDR` + app version (the design names `APDR16`), path = preference `digi_path`, default `WIDE1-1` | `src/tncproto/KissProto.scala:35-37`, `src/AprsService.scala:71` (`"APDR%s"`) and `:229` (`digi_path`, default `WIDE1-1`), `res/xml/preferences.xml` (`digi_path`) |

Weak points: no running app was tested; line numbers of the APRSdroid files were counted on the downloaded copy and can drift with later commits;
whether the Google Play build equals master is not checked; the third-party guide `rdz_ttgo_sonde` "Data feed configuration" (wiki page, found by a web search) describes the same settings ("Connections: TNC (KISS), Type of connection: TCP/IP") and agrees.

### PinPoint APRS: YES by its documents, but it is a **Windows desktop program, not a phone app**; weaker evidence (closed source)

* Documents: `https://pinpointaprs.com/PinPoint-v2.2-Quick-Start-Guide.pdf` (45 pages, last updated July 2026; TNC tab, "Network KISS mode (like DireWolf, SoundModem, VARA). Make sure you check the IP address and the Port. Some ports use 8001 (PinPoint default), others use 8100"; "Using a KISS TNC over a network connection is also supported"; requirements list Windows 7/10/11 and .NET 4.7.2) and `https://www.pinpointaprs.com/faq.html` ("any KISS TNC via network or serial port", "applications that have a built-in soft-TNC via the network KISS interface"; the FAQ names no Android or iOS version).
* What it does with KISS (Quick Start Guide, TNC tab): it listens to **all KISS port numbers 0-15** and transmits on a chosen "KISS TX port" (default 0, "only used when using a TNC that handles multiple clients"); no digipeating for KISS TNCs; it reconnects to a network TNC by itself after an interruption; SSIDs only 0-15 with KISS.
* Weak: the same guide says once "You may configure the TCP/IP address and the UDP port number in the lower section" while the Network KISS bullet speaks of a port such as 8001 (a TCP port of Dire Wolf). That sentence is most likely a wording slip, not a UDP KISS option, but it is not proven: there is no source. Nothing was run.
* **Design correction:** design 3 groups "Phone/LAN apps (APRSdroid, PinPoint ...)": PinPoint is a LAN client on Windows. Its KISS TCP use matches what a Dire Wolf-like server offers (port 8001 default, channel 0 for transmit by default).

### What this means for the design (for the DV-Router lead; each is a finding, not a verdict)

1. **Silent server, 120 s**: APRSdroid's default read timeout drops the link after 120 s without a byte and reconnects 30 s later. A quiet RF channel gives exactly that, so an unadapted app reconnects every ~150 s (counted `accepted` and `closed` each time). Design 7 says "no idle timeout (RX apps are silent)". The operator can set the app's timeout to 0; a server could instead write a lone `FEND` (an empty KISS frame, ignored by every decoder, the app's `is.read()` returns and its timer restarts) every ~60 s. That is an inference from the app code, not a tested behaviour. Suggest an optional keepalive, or documenting the app setting.
2. **Channel 0 only for APRSdroid**: its reader keeps frames of channel 0 only (type byte 0x00). The listener mapping must put the exposed port on channel 0 (design 4 already says so); a case should show that a channel-1 frame is invisible to this reader (`AprsdroidReader` models it).
3. **Init string**: an app can write ASCII lines (default none) before its first frame; the server must absorb them without disturbing the next frame (design 7: FEND recovery, bad frames counted).
4. **Default port 14580** when the app's host has no `:port`: an operator typing only a host reaches whatever listens on 14580. Documentation point for the operator page.
5. **PinPoint** transmits on a KISS TX port that can be 0-15: with a one-port listener mapped to channel 0 any other TX port is refused `channel` (design 4/8); worth a line in the operator docs.

## 2. The client helper (`dvtest/kiss_app.py`)

Stdlib, reuses `dvtest/ax25.py` framing (`kiss_encode`, `KissDecoder`) and `dvtest/kiss_tnc.py` `ui_frame`. It is an APP: it connects to the server, it does not listen.
`kissutil` (Dire Wolf) is the design's slow-tier oracle. **It needs no new install**: `rig/Dockerfile.direwolf` already builds `kissutil` into the image `dvt-direwolf:local` (profile `dw`).
Running it against the node would be a host-side one-off container on the rig network (like the cfgload containers, with `dvtest.limits.docker_run_args()`), no internet. That is only a proposal here; nothing internet-facing is needed.

| Feature | Why |
|---------|-----|
| `connect()` one attempt, `source_ip` | a refusal must be observable; a bind address for `allow_from` cases |
| `closed_at`, `wait_closed`, `bytes_received` | refused/kicked/stalled clients, "zero bytes" as a counter |
| `send_frame(channel)`, `send_command`, `send_text`, `send_raw`, `send_split` | frames, the ignored commands (design 7), init text, partial writes |
| `pause_reading` / `resume_reading` | slow client drop and write stall (design 7) |
| `aprsdroid_frames`, `aprsdroid_init_bytes`, `AprsdroidReader`, `pinpoint_tx` | app models taken from the code and documents cited above |

## 3. Case plan (to write when the design is final)

Run shapes: the fake KISS modem of RF tier A (`cases/test_dp15_rf.py` `rf` fixture, the edge connects to the tester) plus one or more `[[edges.ax25.kiss_server]]` rows; the tester reaches the listener on the SUT's rig address. TX cases need `tx_enable`, the port `tx` true or `"simulate"` and the TX guard wait (`rf.tx_guard`). Config rejections use the launcher config check (like `cases/test_config_load.py`, host side).

| Check | Design | Observable black-box | Not observable / limit |
|-------|--------|----------------------|------------------------|
| Connect, banner-less accept; bytes only after a frame | 3, 7 | accept, no bytes from the server until a frame exists | |
| Modem frame reaches two clients with the mapped channel; the sender is not echoed; no KISSCOPY to other clients | 4, 5, 6 | `AppKissClient` x2, fake TNC `inject` | |
| Client frame reaches the modem only with `tx = true`; AX.25 addressing: source `N0TEST-7` (a base-callsign SSID), tocall `APDR16`, path `WIDE1-1`, `=` position then `:` message, class by first byte | 5 | fake TNC data frame; `dv_edge_rf_tx_total` kinds | the class names are in the scheduler counters only |
| Refusal order and labels: `tx_off`, `tx_addr`, `channel`, `bad_frame`, `not_ui`, `src`, `path` (used H bit, `TCPIP`, `qA*`, hops over `max_hops`), `too_long` (info over 256 B), `third_party` (`}`), `rate` (listener bucket) | 5, 8 | counters `dv_edge_kiss_frames_total{dir=in,result}`, silence at the fake TNC | the order of two simultaneous defects is visible only as the first counted result |
| Simulate port: `simulated`, no byte at the modem | 5 | `dv_edge_rf_tx_total{result=simulated}`, silence at the fake TNC | the PTT token (fabric key, as in DP18-s) |
| Commands ignored (TXDELAY, P, SlotTime, TXtail, FullDuplex, SetHardware, 0xFF), counted `command`; the modem parameters are not touched | 7 | counter; the fake TNC saw no new parameter frame | |
| Init text before the first frame is absorbed; the next frame is served | 7 | `aprsdroid_init_bytes` then a frame | whether the text counts as `bad_frame` or `command` |
| Partial frames (split writes), 1100 B limit, FEND recovery, bad escape | 7 | `send_split`, oversized frame | the exact counter for each |
| Allowed/refused peer: `allow_from` checked before any byte is read, `refused_addr` | 3 | two listeners, the tester inside and outside the CIDR: closed at once, zero bytes, counter | a second source address on the tester: none in the rig (an alias address as the T5n case would be a new rig piece) |
| `tx_from`: an RX-allowed peer cannot transmit (`tx_addr`) | 3, 5 | counter, silence at the modem | |
| `max_clients`: the next client is refused `full` | 7 | `wait_closed`, counter, gauge `dv_edge_kiss_clients` | |
| Slow client: output queue 64 frames / 64 KiB, newest dropped `slow`; 30 s write stall closes (`stalled`) | 7 | `pause_reading`, a burst of injected frames, counters, `closed_at` | queue depth itself |
| Read budget 16 KiB/s then reading pauses (TCP back-pressure); at most 8 frames per client per turn | 7 | throughput of `send_raw` flood | the per-turn cap |
| Sessions `kind: "kiss_client"` in the admin clients list (ip/port, admin scope) and `kick` (`kicked`) | 7 | `GET /api/v1/admin/clients`, `POST .../edges/{kind}/clients/{circuit}/kick` | |
| Reload and restart: a `tx` or listener change restarts the AX.25 child (Restart(child)): clients are closed, the listener rebinds, nothing queued before the change is transmitted after it (as DP18-r1/r2), a client frame right after the restart is held by the RFSAFE1 guard (`rf_fenced`) and goes out after the 13 s | 2, 5 | child `restarts`, `closed_at`, reconnect, fake TNC, `dv_edge_drops_total{reason=rf_fenced}`, the TX guard helper | |
| Config validation: non-loopback `listen` without `allow_from`, duplicate `listen`, `tx = true` without a TX port, `allow_src` base unequal to the station base, `tnc2` ports | 2 | launcher config check text per fixture (host side, `--network none`) | |
| Start log: bind, ports, TX state, allowed sources, never a peer address or frame content | 2 | scan `out/<run>/logs` after a run with clients for the tester's address | |
| APRSdroid script (`kiss-server-aprsdroid` of design 9): init text, `=` position, `:` message from `N0TEST-7` tocall `APDR16` path `WIDE1-1`; frames at the modem with tx true, `simulated` with simulate, `src` / `path` / `third_party` refusals counted, injected modem frames readable by `AprsdroidReader`, a non-allowed peer refused | 9 | all of the above | the real app is not run |
| Silent channel for 150 s: the connection stays up and the app model sees no byte (the 120 s timeout finding above) | 7 | a client that closes at 120 s of silence, as the app does | whether DV-Router adds a keepalive is a design question |
| Fuzz target `kiss_server` and Dire Wolf `kissutil` oracle | 9 | not a black-box case here | `kissutil` as a host-side one-off container is proposed in section 2 |

Not observable black-box, whatever the design: the sans-IO core, the admission code shared with `enqueue`, the exact scheduler class and the per-listener quota key `kiss/<listener>` (only the effect on `dv_edge_rf_tx_total{result=dropped_quota}` is visible), `Nagle off`, TCP keepalive options, the `PTT` holder token.

## 4. Open points for the DV-Router lead

* The design names the sample client source `N0CALL-7`; a station callsign for the node that also passes `allow_src` must have the same base, so the cases use `N0TEST-7` against the `N0TEST-1` / `N0TEST-5` rig callsign. (`N0CALL` as an AX.25 source is also refused by the igate rules.)
* Is a keepalive FEND wanted (finding 1 above)? Without it the Android app, with its defaults, will reconnect every 2.5 minutes on a quiet channel.
* Operator documentation for the host field of the apps: the app default port 14580 if no port is typed.
