<!-- SPDX-License-Identifier: MIT -->
# dv-testset docs

| Document | Contents |
|----------|----------|
| [rig.md](rig.md) | docker rig: topology, addresses, ports, settings load order, control channel, capture |
| [web-api-cases.md](web-api-cases.md) | web unit stage 2: API cases for DV-Router's HTTP APIs (role matrix, removed URLs, limits, admin), findings, gaps |
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
