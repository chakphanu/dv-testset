<!-- SPDX-License-Identifier: MIT -->
# Test list

What has to be verified, and which case verifies it. Status:
**covered** (an automated case asserts it), **planned** (being written),
**gap** (nothing checks it yet). Test ids are `file::function`; `[..]` means
the case is parametrized.

Levels: `unit` offline, `wire` against a transcoder, `netns` xlxd in user
namespaces, `rig` docker rig, `allstar` / `nxdn` / `p25` rig profiles.
Every `--capture` run is also checked passively by `dvcheck` (section H).

## A. Transcoder link conformance (`wire`)

| # | Check | Case | Status |
|---|---|---|---|
| A1 | PING is answered with PONG | test_ambed_wire::test_ping_pong | covered |
| A2 | CAPS reply: 15 bytes, version 1, max outputs, codec mask | test_ambed_wire::test_caps_reply | covered |
| A3 | Legacy 17/18/19-byte opens get a legacy reply (STD14 or BUSY9), never 18/13 bytes | test_ambed_wire::test_legacy_open_gets_legacy_reply[..] | covered |
| A4 | Legacy replies equal a reference transcoder's | test_ambed_wire::test_legacy_open_matches_reference[..] | covered (needs DVT_AMBED_REF) |
| A5 | Legacy opens beyond capacity get BUSY9 | test_ambed_wire::test_legacy_open_full_gets_busy9 | covered |
| A6 | Extended open with a codec outside the CAPS mask gets BUSY13 | test_ambed_wire::test_ext_open_unserved_codec_busy | covered (skipped when every codec is served) |
| A7 | Invalid extended opens (duplicate, gap, out = in, no outputs, unknown codec, bad callsign) get BUSY13 with the transaction | test_ambed_wire::test_ext_open_invalid_busy[..] | covered |
| A8 | Extended open gets STD18; an exact retransmit gets the same STD18 and no new stream | test_ambed_wire::test_ext_open_std18_and_retransmit | covered |
| A9 | Same transaction with other codecs is a new open; the earlier stream is kept | test_ambed_wire::test_ext_open_same_tx_other_codecs | covered |
| A10 | Stream cap: N opens succeed, N+1 gets BUSY13 | test_ambed_wire::test_stream_cap | covered (needs DVT_AMBED_STREAM_CAP) |
| A11 | PCM -> Codec2 produces Codec2 frames | test_ambed_wire::test_pcm_to_codec2 | covered |
| A12 | Codec2 -> PCM is not silent | test_ambed_wire::test_codec2_to_pcm_nonzero | covered |
| A13 | Multi-output replies keep open order and widths; round trip correlates | test_ambed_wire::test_codec2_multi_output_round_trip | covered |
| A14 | Two outputs keep the open order in every reply | test_ambed_wire::test_pcm_two_outputs_keep_order | covered |
| A15 | IMBE follows CAPS: served when advertised (non-zero payloads), refused when not | test_ambed_wire::test_imbe_follows_caps | covered |
| A16 | Morse survives PCM -> AMBE+ / AMBE2+ / Codec2 -> PCM (decoded text exact) | test_morse_codecs::test_morse_through_transcoder[..] | covered |
| A17 | Morse survives PCM -> IMBE -> PCM | - | gap (add IMBE to test_morse_through_transcoder) |
| A18 | CLOSE releases the stream; data after CLOSE is ignored | dvcheck `ambed.data_after_close`; no active case | gap (active case) |
| A19 | Idle stream times out (reference: 3 s) | - | gap |
| A20 | Data from an address other than the opener is ignored | - | gap |
| A21 | Concurrent streams do not mix payloads | - | gap |

## B. xlxd with a transcoder (`netns`)

| # | Check | Case | Status |
|---|---|---|---|
| B1 | Without USRP endpoints xlxd sends no CAPS | test_xlxd_usrp_netns::test_no_usrp_sends_no_caps | covered |
| B2 | Without USRP endpoints xlxd uses legacy opens | test_xlxd_usrp_netns::test_no_usrp_legacy_open | covered |
| B3 | Without USRP endpoints the transcoder link bytes equal upstream master | test_xlxd_usrp_netns::test_no_usrp_link_bytes_equal_reference | covered |
| B4 | Legacy transcoder: a single CAPS probe, then legacy opens | test_xlxd_usrp_netns::test_legacy_ambed_single_caps_probe, ::test_legacy_ambed_digital_uses_legacy_open | covered |
| B5 | Legacy transcoder: PCM streams are not transcoded (and logged once) | test_xlxd_usrp_netns::test_legacy_ambed_pcm_not_transcoded | covered |
| B6 | Extended transcoder: a single CAPS probe and extended opens for PCM | test_xlxd_usrp_netns::test_extended_single_caps_probe, ::test_extended_opens | covered |
| B7 | D-STAR -> USRP peer: voice then unkey | test_xlxd_usrp_netns::test_extended_dstar_to_usrp | covered |
| B8 | USRP peer -> digital listeners | test_xlxd_usrp_netns::test_extended_usrp_to_digital | covered |
| B9 | No echo back to the originating USRP peer | test_xlxd_usrp_netns::test_extended_no_echo_to_usrp_peer | covered |
| B10 | Malformed frames from a USRP peer are dropped; xlxd survives | test_xlxd_usrp_netns::test_extended_bad_frame_dropped | covered |
| B11 | Transcoder that serves one output: separate opens, distinct transactions | test_xlxd_usrp_netns::test_one_output_opens | covered |
| B12 | Slow open reply: a short burst is still delivered | test_xlxd_usrp_netns::test_slow_open_short_burst_delivered | covered |
| B13 | Transcoder lost > 30 s and back: CAPS is probed again, PCM transcoding resumes | - (seen in a triage run only) | gap |
| B14 | Transcoder replaced within 30 s: stale CAPS is not used | - | gap (known PR edge case) |

## C. Reflector routing (`rig`)

| # | Check | Case | Status |
|---|---|---|---|
| C1 | Same-mode passthrough is byte-exact on both reflectors | test_xlxd_matrix::test_passthrough_exact[..] | covered |
| C2 | Same-mode passthrough equals the recorded baseline for the reference commit | test_xlxd_matrix::test_passthrough_baseline | covered |
| C3 | Cross-mode cells (D-STAR <-> DMR, M17 -> DMR / D-STAR, DMR / D-STAR -> M17): system under test == reference | test_xlxd_matrix::test_cross_sut_equals_ref[..] | covered |
| C4 | Cross-mode on a USRP-bridged module | test_xlxd_matrix (module B with profile allstar) | covered |
| C5 | Morse ID through a reflector decodes exact on a listener | test_morse_codecs::test_live_id_through_reflector | covered |
| C6 | Interlink between two reflectors (XLX peer) | - | gap |
| C7 | Module change / unlink / relink of clients | - | gap |
| C8 | Stream timeout without a last frame (reference 1.6 s) | dvcheck `header_only` (passive) | gap (active case) |
| C9 | Many modules talking at once (load) | - | gap (street profile, later) |
| C10 | YSF, DPlus, DCS clients | - | gap (no clients yet) |

## D. Resilience (`rig`)

| # | Check | Case | Status |
|---|---|---|---|
| D1 | Transcoder stopped: native passthrough unchanged | test_backend_loss::test_native_passthrough_while_down | covered |
| D2 | Transcoder stopped: no invented voice on other modes | test_backend_loss::test_usrp_pcm_not_fabricated | covered |
| D3 | Transcoder started again: cross-mode recovers | test_backend_loss::test_cross_path_recovers | covered |
| D4 | AllStar node killed while keyed: stream ends, module released | test_allstar::test_node_crash_releases_module | covered |
| D5 | xlxd restart with clients connected | - | gap |

## E. AllStarLink / USRP (`allstar`)

| # | Check | Case | Status |
|---|---|---|---|
| E1 | IAX2 caller reaches M17, D-STAR and DMR listeners at once | test_allstar::test_iax_to_all_digital_listeners | covered |
| E2 | Digital talker reaches the IAX2 caller (tone, KEY/UNKEY) | test_allstar::test_digital_to_iax[..] | covered |
| E3 | Two rooms at once without crosstalk | test_allstar::test_rooms_no_crosstalk | covered |
| E4 | No echo to the USRP origin | test_allstar::test_usrp_no_echo | covered |
| E5 | Morse through AllStar decodes exact | - | gap |

## F. NXDN and P25 peers (`nxdn`, `p25`)

| # | Check | Case | Status |
|---|---|---|---|
| F1 | NXDN reflector peered with xlxd; polls both ways | rig profile nxdn (manual check in a capture run) | planned |
| F2 | NXDN <-> DMR passthrough (same AMBE2+), both directions | cases/test_nxdn.py | planned |
| F3 | NXDN <-> D-STAR / M17 / AllStar through the transcoder; system under test == reference | cases/test_nxdn.py | planned |
| F4 | P25 reflector peered with xlxd | rig profile p25 | planned |
| F5 | P25 (IMBE) <-> DMR / D-STAR / M17 / AllStar through the transcoder | cases/test_p25.py | planned |
| F6 | NXDN and P25 client codecs (offline) | cases/unit/test_nxdn.py, cases/unit/test_p25.py | covered |

## G. Audio content

| # | Check | Case | Status |
|---|---|---|---|
| G1 | Morse encoder/decoder: speeds, tones, noise, jitter, dropouts, gain, DC, silence | cases/unit/test_morse.py | covered |
| G2 | Morse through Codec2 3200 / 1600 | test_morse_codecs::test_codec2_round_trip[..] | covered (needs c2enc/c2dec) |
| G3 | Speech quality (PESQ) | - | gap (phase 3) |

## H. Passive wire checks (`dvcheck` on every `--capture` run)

| # | Protocol | What is checked | Status |
|---|---|---|---|
| H1 | Transcoder link | request/reply pairing by transaction, STD/BUSY form, open within CAPS, retransmit keeps the stream, every data request answered in open order with correct widths, packet id order, no data after CLOSE, unknown peers | covered |
| H2 | D-STAR (DExtra) | header -> voice -> last frame, frame counter 0..20, constant stream id, sync position, loss/dup/reorder, jitter | covered |
| H3 | DMR (MMDVM) | sequence, voice header -> bursts A..F -> terminator, sizes, traffic after close (with the 1 s upstream client-cache window) | covered |
| H4 | M17 | frame number, last-frame bit, constant LICH/stream id, sizes | covered |
| H5 | USRP | sequence, 352-byte keyed / 32-byte unkey, no voice after unkey | covered |
| H6 | IAX2 | oseqno/iseqno, ACK for every full frame, timestamps, mini-frame continuity, voice size per codec, call lifecycle | covered |
| H7 | NXDN | poll/echo pairing, frame layout, sequence, header -> terminator | planned |
| H8 | P25 | poll, LDU1/LDU2 sequence and sizes, header/terminator | planned (checker written, needs a capture) |
| H9 | pcap/pcapng reader | link types, byte orders, timestamp units, skips | covered (cases/unit/test_pcap.py) |

## I. Library (`unit`)

| # | Check | Case | Status |
|---|---|---|---|
| I1 | Transcoder protocol encoders/parsers and FakeAmbed modes | cases/unit/test_ambed.py | covered |
| I2 | DExtra, DMR, M17, USRP, UDP client | cases/unit/test_protocols.py | covered |
| I3 | IAX2 client | cases/unit/test_iax2.py | covered |
| I4 | Signals, scoring (Goertzel, LSD, spectrum) | cases/unit/test_audio.py | covered |
| I5 | Target settings from env/TOML | cases/unit/test_target.py | covered |
| I6 | Client helpers (fresh stream ids, wire frames) | cases/unit/test_clients.py | covered |
| I7 | dvcheck rules with injected faults | cases/unit/test_check_*.py | covered |

## J. Repository hygiene

| # | Check | How | Status |
|---|---|---|---|
| J1 | No home paths, emails, public IPs, device paths/serials, ELF files, local words (callsigns, private names) in files or git metadata | scripts/pii-check.sh (also run by run.sh) | covered |
| J2 | Pinned sources verified (git commit, sha256 for downloads) | scripts/pre-fetch-*.sh, fetch_verified | covered |
| J3 | No audio, bitstreams or captures committed | .gitignore + pii-check ELF rule | covered (audio by .gitignore only) |

## Upstream behaviour the cases accept

Known xlxd behaviour that is not a defect of the build under test; cases and
dvcheck report it as a warning with a note:

- M17 -> D-STAR drops the two Codec2 frames of M17 frame 0.
- A stream that times out is closed without a last frame.
- After MSTCL a DMR peer can still receive up to 1 s of stream (client cache).
- A 9-byte RPTCL is ignored by xlxd.
- With the transcoder down, cross-mode frames are all zero and D-STAR frame 0 has no sync.
- Legacy opens always ask for IMBE as the third output.
