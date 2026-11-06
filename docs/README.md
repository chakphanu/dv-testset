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
| [config-load-cases.md](config-load-cases.md) | DV-Router COMPAT1 config-load cases (`-m cfgload`, host side): an old key / shape must fail `config check` with a clear error; table, how to run, findings |
| [stream-registry-cases.md](stream-registry-cases.md) | DV-Router open-stream registry (LEAK2): loop refusal, 1 s refresh, 5 s expiry after a controller kill, controller restart (L1 to L4) |
| [ipsc-phase0.md](ipsc-phase0.md) | IPSC1 phase 0 (offline): IPSC message table with sources, simulated MOTOTRBO peer plan, hardware-risk list, compose profile sketch |
| [nxdn-gps-cases.md](nxdn-gps-cases.md) | NXDN GPS / data calls (DP14b offline): layer 3 generator and parser, Kenwood FULL / SHORT layouts (NXDNGateway vs dsd-fme readings), two-decoder oracle record, planned rig cases NXG1 to NXG7, NXDNGateway oracle image, run and per-row results (stage 1), dsd-fme recipe UNVERIFIED, open questions |
| [m17v3-cases.md](m17v3-cases.md) | M17 v3 test sources for DV-Router (P26j offline): pure-Python P-256 ECDSA, v3 TYPE/META and signed-stream generator, `vectors/m17v3/` verdict files, m17-fme verifier image (built and run: micro-ecc digest convention accepted), research-versus-spec discrepancies, readings of the spec ambiguities, planned cases P26j-1 to P26j-9, build and run results |
| [direwolf-cases.md](direwolf-cases.md) | Dire Wolf as the independent AX.25 / APRS / KISS reference and LoRa airtime anchors (HWFREE1 item 3, DP11b / DP11d / DP9 offline): deliverable decision, `dvtest/ax25.py` + `dvtest/airtime.py`, `dvt-direwolf:local` image (built and run), `vectors/direwolf/` and `vectors/airtime/`, facts read from the oracle, what is VERIFIED and what is not, open questions for the DV-Router lead |
| [dstar-slowdata-cases.md](dstar-slowdata-cases.md) | D-STAR slow data (HWFREE1 item 4, feeds DP5f, offline): deliverable decision, spec-derived generator and parser `dvtest/dstar_sd.py` (every message type, offsets, spill over a sync, D-PRS / NMEA, header, squelch, fast data), `dvt-dstarsd:local` image with MMDVMHost and DStarGateway decoders as oracles (built and run), 43-case corpus `vectors/dstar_sd/`, measured MMDVMHost-resets / DStarGateway-accumulates difference, VERIFIED / UNVERIFIED split, open questions for the DV-Router lead |
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
