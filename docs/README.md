<!-- SPDX-License-Identifier: MIT -->
# dv-testset docs

| Document | Contents |
|----------|----------|
| [rig.md](rig.md) | docker rig: topology, addresses, ports, settings load order, control channel, capture |
| [asl-lab.md](asl-lab.md) | AllStarLink test lab (profile `asl-lab`): private app_rpt nodes, registrar and DNS zone, per-scenario pcaps, IAX2 frame extractor, surprises |
| [web-api-cases.md](web-api-cases.md) | web unit stage 2: API cases for DV-Router's HTTP APIs (role matrix, removed URLs, limits, admin), findings, gaps |
| [web-browser-cases.md](web-browser-cases.md) | web unit stages 3a and 3b: browser cases (UI, XSS, CSP) and WebRTC Listen/Talk, recorder |
| [web-nat-cases.md](web-nat-cases.md) | web unit stage 4: NAT behaviour of the browser WebRTC path (NAT router, coturn, direct/TURN/blackhole), findings, gaps |
| [web-k22-cases.md](web-k22-cases.md) | web unit: K22 / K22b cases (RTC circuit identity, revoke, guest lease, edge and hub kills), findings, gaps |
| [echo-cases.md](echo-cases.md) | DV-Router built-in echo (ECHO1 / ECHO1c): cases E1 to E9 (E9: D-STAR echo return integrity), snippets, live config edit, what could not be reached, findings |
| [dp15-plan.md](dp15-plan.md) | DP15 plan (planning only): the data-channel cases of DV-Router data.md 15.5 in seven groups, per-case steps, observables, reuse, new rig pieces (worker kill action, fake KISS TNC, private aprsc, Dire Wolf loop, second DV-Router peer), verdicts, ordered implementation units, questions for the DV-Router lead |
| [prod-checks.md](prod-checks.md) | owner-approved checks of a LIVE reflector: `dvtest.tools.prod_echo` (one over per protocol through echo module E, replay judged, unlink; safety rails, dry run) and `dvtest.tools.prod_listen` (one anonymous WebRTC Listen in the `dvt-webload` image, docker line), report table, what is not covered offline |
| [config-load-cases.md](config-load-cases.md) | DV-Router COMPAT1 config-load cases (`-m cfgload`, host side): an old key / shape must fail `config check` with a clear error; table, how to run, findings |
| [dp15-cases.md](dp15-cases.md) | DP15 stream-metadata cases (written, not run on a rig): D-STAR TX message / D-PRS, M17 SMS and META, YSF GPS, DMRG / DMRA, XLX peer wire; 15.5 wording, bounds, case table, byte-layout sources (VERIFIED), blocked cases, the run command |
| [stream-registry-cases.md](stream-registry-cases.md) | DV-Router open-stream registry (LEAK2): loop refusal, 1 s refresh, 5 s expiry after a controller kill, controller restart (L1 to L4) |
| [ipsc-phase0.md](ipsc-phase0.md) | IPSC1 phase 0 (offline): IPSC message table with sources, simulated MOTOTRBO peer plan, hardware-risk list, compose profile sketch |
| [nxdn-gps-cases.md](nxdn-gps-cases.md) | NXDN GPS / data calls (DP14b offline): layer 3 generator and parser, Kenwood FULL / SHORT layouts (NXDNGateway vs dsd-fme readings), two-decoder oracle record, planned rig cases NXG1 to NXG7, NXDNGateway oracle image, run and per-row results (stage 1), dsd-fme recipe UNVERIFIED, open questions |
| [m17v3-cases.md](m17v3-cases.md) | M17 v3 test sources for DV-Router (P26j offline): pure-Python P-256 ECDSA, v3 TYPE/META and signed-stream generator, `vectors/m17v3/` verdict files, m17-fme verifier image (built and run: micro-ecc digest convention accepted), research-versus-spec discrepancies, readings of the spec ambiguities, planned cases P26j-1 to P26j-9, build and run results |
| [direwolf-cases.md](direwolf-cases.md) | Dire Wolf as the independent AX.25 / APRS / KISS reference and LoRa airtime anchors (HWFREE1 item 3, DP11b / DP11d / DP9 offline): deliverable decision, `dvtest/ax25.py` + `dvtest/airtime.py`, `dvt-direwolf:local` image (built and run), `vectors/direwolf/` and `vectors/airtime/`, facts read from the oracle, what is VERIFIED and what is not, open questions for the DV-Router lead |
| [lora-gw-cases.md](lora-gw-cases.md) | `dvt-lora-gw`: scripted hardware-free raw TNC2 and u16 binary source for the LoRa APRS gateway path (HWFREE1 item 3, DP11d): decision (pure Python, no image), 85 scenarios x 2 prefix profiles in `vectors/lora_gw/`, expectations VERIFIED by reading the DV-Router source (file:line per scenario, nothing run), corrections of the first guesses, open questions for the DV-Router lead |
| [dstar-slowdata-cases.md](dstar-slowdata-cases.md) | D-STAR slow data (HWFREE1 item 4, feeds DP5f, offline): deliverable decision, spec-derived generator and parser `dvtest/dstar_sd.py` (every message type, offsets, spill over a sync, D-PRS / NMEA, header, squelch, fast data), `dvt-dstarsd:local` image with MMDVMHost and DStarGateway decoders as oracles (built and run), 63-case corpus `vectors/dstar_sd/` (DV-Router unit `unit66_hex`, little endian bitmap; `expected_dvrouter` = the DV-Router code today read from `dstar.rs`, `expected_dvrouter_after_dp5f` = the planned rules, unit level reject vectors, named differences: length nibble, GPS limits, frame-pair holes, CR/LF, checksum rejects), measured MMDVMHost-resets / DStarGateway-accumulates difference, stage 2: DSDcc as a third, non-G4KLX decoder (`dvt-dsdcc:local`, the two published `.dis` recordings summarized without content, the corpus on a synthetic RF stream), VERIFIED / UNVERIFIED split, open questions for the DV-Router lead |
| [testlist.md](testlist.md) | what has to be verified, the case that verifies it, and the gaps |
| [protocols/ambed.md](protocols/ambed.md) | AMBEd transcoder link: legacy and extended open, CAPS, data packets, flows, timing |
| [ambed-fake.md](ambed-fake.md) | fake extended AMBEd transcoder with a bit-exact transform: protocol, modes, control JSON, K table |
| [protocols/usrp.md](protocols/usrp.md) | USRP (app_rpt chan_usrp) voice framing between xlxd and AllStar |
| [protocols/clients.md](protocols/clients.md) | what each `dvtest` client implements: DExtra, MMDVM DMR, M17, IAX2, AMBEd, USRP |

Related:

- `tools/wireshark/` - Lua dissectors for the protocols above, loaded by
  `scripts/tshark.sh`.
- `.agents/skills/dv-testset/SKILL.md` - running and extending the tests.
- `.agents/skills/dv-capture/SKILL.md` - capturing and decoding rig traffic,
  and turning captures into the Evidence sections of the protocol docs.

Every field and message in `docs/protocols/` was checked against a real rig
capture decoded with tshark; each document ends with the run id, the
commands and the observed counts.
