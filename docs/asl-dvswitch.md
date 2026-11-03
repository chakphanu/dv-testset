<!-- SPDX-License-Identifier: MIT -->
# DVSwitch Mobile against dv-edge-allstar (lab)

The real Android app DVSwitch Mobile, run in an emulator against the edge of the asl-lab (`docs/asl-lab.md`,
`docs/asl-edge-results.md`): what a real phone client sends, and whether it connects. Nothing here touched the live
network: the emulator only talked to the rig's edge (`<net>.11`), the fixture account `phone-ro` / `acct` and the
fixture secret `test-secret-not-real`.

## What was run

| Item | Value |
|------|-------|
| App | DVSwitch Mobile 2.0.7, versionCode 205, package `org.dvswitch`, 3,419,316 bytes |
| sha256 | `6e54d61ac9bb071f6d566fbb16c7f8ae5fcf443cc49266b1bfd11002cebbca73` (APKPure mirror, `d.apkpure.net/b/APK/org.dvswitch?version=latest`; the mirror page lists no hash, so there was nothing to compare: the page shows version 2.0.7 and 3.3 MB, the file matches) |
| Signing certificate | CN=Mike Zingman, OU=INAD, O=DVSwitch, L=Marietta, ST=Ga, C=US; SHA-256 `8fcbeb104780b4d2c0e0e97eb121d0dc93159fc2c62211a2976e12fecd55787a` (`apksigner verify --print-certs`: valid) |
| Manifest | minSdk 21, targetSdk 35; permissions INTERNET, ACCESS_NETWORK_STATE, RECORD_AUDIO, MODIFY_AUDIO_SETTINGS, WAKE_LOCK, VIBRATE, POST_NOTIFICATIONS, Bluetooth (BLUETOOTH, ADMIN, CONNECT, SCAN), location (COARSE, FINE, BACKGROUND), storage read/write, REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, com.android.vending.BILLING |
| Emulator | own AVD (not the user's `Medium_Phone`): Android 36 x86_64 google_apis_playstore image from the SDK, headless (`-no-window -no-audio -no-snapshot-save -wipe-data`), 2 cores, 2048 MB, no sdcard, no Google account; installed with `adb -s emulator-5570 install` only |
| Permissions answered | microphone allowed, location and notifications denied; no licence or account screen appeared |
| Edge images | `dv-router:asl-c199b` (negative control), `dv-router:wip-7efdd33` (the ASL1f fix) |
| Config of the edge | `rig/config/dv-router/allstar-lab.toml`: node 1999 room L, accounts `acct` (talk) and `phone-ro` (listen only), md5 secret fixture |

Account in the app (Accounts tab): IAX2, host `<net>.11`, port 4569, username `phone-ro` (later `acct`), the fixture
password, CallerID `N0TEST`, Caller Number empty, node `1999`, public authentication off, and "Phone mode IAX2
connection" on or off as stated. The Config tab has no codec setting (macros, a Voice Call switch, UI options): the
codec is read from the wire. Pcaps are taken by the lab agent in the edge's namespace
(`out/<run>/asl-lab/dvswitch/<scenario>/edge.pcap`, `edge.frames.json`); screenshots stay in `out/dvswitch-dev/`
(not committed).

## Results

| Scenario | Image | Run | Result |
|----------|-------|-----|--------|
| Phone mode, Register | asl-c199b | `20261103T182519Z/.../dvs-neg-c199b` | **no connection**: the edge stays silent |
| Phone mode, Register | wip-7efdd33 | `20261103T182623Z/.../dvs-ro-wip7efdd33` | CALLTOKEN answered, then **REGREJ**: app stays "Unregistered", Connect greyed out |
| Radio mode (phone mode off), Connect, PTT, Hangup, account `phone-ro` | wip-7efdd33 | `.../dvs-nophone-wip7efdd33` | **connects** (ANSWER at 0.11 s), keys with RADIO_KEY, hangs up |
| Radio mode, account `acct` (talk), A linked to the edge, PTT 3.5 s | wip-7efdd33 | `.../dvs-acct-talk-wip7efdd33` | connects; the edge forwards the app's voice to A (28,160 bytes of ulaw, 3.5 s) |

### Negative control, asl-c199b (phone mode)

The app sends a **bare 14 byte NEW**, nothing but an empty CALLTOKEN IE, and retransmits it with the R bit at 0.5, 1.0,
2.0, 3.5 and 5.5 s (6 datagrams, one call number, `ab22 0000 0000 0000 0000 0601 3600`: source call, dest call 0,
timestamp 0, oseq 0, iseq 0, frame type 6 IAX, subclass 1 NEW, IE 0x36 length 0). The edge never answers (its
amplification rule: the CALLTOKEN reply is longer than the request): no connection. Vector:
`baselines/asl-lab/dvswitch-2.0.7-bare-new.json`.

### wip-7efdd33, phone mode: the app needs a registrar

1. bare NEW (14 B, empty token) -> CALLTOKEN (41 B, source call 1) -> ACK; the app sends a second bare NEW 31 ms later,
   gets another CALLTOKEN, then
2. `REGREQ` (USERNAME `phone-ro`, REFRESH 60, CALLTOKEN) -> the edge answers **REGREJ** (no information elements).
3. The app shows "Unregistered", keeps the **Connect button disabled** and repeats the sequence about every 50 s.

So in phone mode (the mode the owner's phone is configured for, "Phone mode IAX2 connection") the app cannot place a call
until REGACK arrives: the edge as built (registrar off, "REGREQ answered with REGREJ") cannot serve it. This settles
the open question T10 of the ASL1 design note ("whether DVSwitch Mobile refuses to call without a successful
registration"): **it does**. Vector: `baselines/asl-lab/dvswitch-2.0.7-phone-mode-register.json`.

### wip-7efdd33, radio mode (phone mode off): the full call

Timeline of `dvs-nophone-wip7efdd33` (seconds from the first frame):

| t | From | Frame |
|---|------|-------|
| 0.000 | app | NEW, 14 B, empty CALLTOKEN (call number 11042) |
| 0.000 | edge | CALLTOKEN (41 B, source call 1) |
| 0.002 | app | NEW on a **new call number (11043)** with the token the edge minted for 11042 |
| 0.002 | edge | AUTHREQ (md5, challenge 9 digits), repeated 109 ms later (the app had not answered yet) |
| 0.110 | app | AUTHREP (MD5_RESULT, REFRESH 60), sent three times (0.110, 0.1105, 0.70 s, the last with R) |
| 0.110 | edge | ACCEPT (FORMAT ulaw, FORMAT2 0), ANSWER |
| 10.0, 20.0, 30.0 | edge | LAGRQ every 10 s, PING at 20.0 s; the app answers LAGRP, PONG |
| 27.03 | app | PTT: CONTROL RADIO_KEY, then a full voice frame (ulaw) and mini frames every 20 ms |
| 30.03 | app | PTT released: CONTROL RADIO_UNKEY |
| 34.08 | app | Hangup: a new bare NEW probe (call 11044, answered with CALLTOKEN), and on the call **HANGUP with CAUSECODE 0x10 (16, normal clearing) and a CALLTOKEN IE** |
| 35.11 | app | another bare NEW probe (call 11045) |

The NEW of the real call (89 B), IEs in order: VERSION 2, CALLED_NUMBER `1999`, CALLING_NUMBER (**empty**),
CALLING_NAME `N0TEST` (the CallerID field), USERNAME `phone-ro`, CAPABILITY (ulaw | alaw | adpcm | slin),
FORMAT 0, CODEC_PREFS `48444745` ("HDGE"), CALLTOKEN. No FORMAT2 / CAPABILITY2. Codec on the wire: ulaw (the edge's
ACCEPT chooses it; voice frames are ulaw, 160 B per 20 ms). Keying: RADIO_KEY / RADIO_UNKEY control frames, **no DTMF and
no `!NEWKEY!`** text. `CALLED_NUMBER` is the plain node number (the field "Node"), not an extension string. Vector:
`baselines/asl-lab/dvswitch-2.0.7-radio-mode-call.json`.

With `acct` (`talk = true`) and A linked to the edge, the app's PTT audio reached A: `edge -> A` carried 28,160 ulaw bytes
(3.5 s). The emulator's virtual microphone is silent (rms 4), so no tone could be checked, only that the voice frames
are relayed. Not measured: the listen-only account's PTT against a linked node (the edge's refusal of a talk = false
account).

### Surprises

- The app's "Register" in phone mode is a REGREQ, so the edge needs REGAUTH / REGACK for phone mode to work at all.
- A fresh call number is used for the real call; the token minted for the probe's call number is accepted on it
  (token bound to the address, not the call number).
- The app answers AUTHREQ three times (the AUTHREQ is retransmitted by the edge after 109 ms, and the app's first reply
  crosses it); the edge handled the duplicates quietly (one ACCEPT).
- HANGUP carries a CALLTOKEN IE and CAUSECODE 0x10 (Q.850 cause 16, normal clearing).
- After every hangup the app fires bare NEW probes again.

## Phone mode against wip-c165c93 (ASL1g), run 2026-11-04

`tools/dvswitch_phone_case.py` (run on `dv-router:wip-c165c93`, rig `out/20261103T223757Z`, kept) is the phone-mode counterpart of
`tools/dvswitch_radio_case.py`. Same environment and emulator; the case sets "Phone mode IAX2 connection" ON in the app itself
(Accounts tab, first row, checkbox `phone_mode`, Save: `set_phone_mode`) and starts the capture on the edge BEFORE it restarts the app
(it registers by itself). **All 9 checks PASS**, one line each:

1. the registration on the wire is CALLTOKEN, REGREQ with the token, REGAUTH, REGREQ with MD5_RESULT, REGACK, no REGREJ. DVSwitch
   Mobile 2.0.7 does NOT open with a REGREQ: its bare NEW probe (14 B, empty CALLTOKEN) is answered with a CALLTOKEN and its first
   REGREQ already carries that token (so `APP_REGISTRATION`, not the Asterisk-style `PHONE_REGISTRATION` that the tester client of R8
   sends); the app registers again about every 60 s (REFRESH) and sends REGREL when it is stopped;
2. the Status tab says `Registration: Registered` (resource id `status_text`; it says "Unregistered" before) and the Dialer's Connect
   (`send_button`) is enabled;
3. Connect, PTT 3.5 s, Hangup works like radio mode: the Dialer line shows `ASL-PHONE Node: 1999`, the Status tab `Node: 1999 (Phone)`,
   ANSWER, AUTHREP (md5; a registration grants no call privilege, the call still goes NEW / AUTHREQ / AUTHREP), RADIO_KEY from the app,
   more than 20000 bytes of ulaw relayed to node A.

Radio mode (Phone mode OFF, account `acct`) was run again on the same image: ANSWER, AUTHREP, RADIO_KEY, more than 20000 bytes relayed:
all PASS. The tester-side twin without the app is R8-R12 of `cases/test_asl_edge.py` (on by default, `DVT_ASL_PHONE_REG=0` skips them,
`docs/asl-edge-results.md`).

UI notes for the emulator (1080 x 2400): the tab bar is at y 147 (a tap is sometimes swallowed, so `open_tab` verifies the tab by a
resource id it alone has); the battery notice's OK can land in a system settings page (back out and relaunch, `launch_app`); the
location notice comes a few seconds after the start.
