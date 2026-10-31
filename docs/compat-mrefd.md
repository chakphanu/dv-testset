<!-- SPDX-License-Identifier: MIT -->
# mrefd in the rig

This is a derived note, not a specification, and holds no run results. It
says what the `mrefd` rig service is for and how it is set up; see
"mrefd as an extra reference" in `docs/rig.md` for the mechanics.

mrefd is n7tae/mrefd, the M17 Project reflector, pinned by `MREFD_COMMIT` in
`.env.example` (GPL-3.0, fetched into the gitignored `.cache/mrefd`, built
without DHT as `dvt-mrefd:local`). It serves M17 only, on UDP 17000 at
`<net>.80` (profile `mrefd`). It is the reference for M17 behaviour that
xlxd cannot show: reflector-to-reflector peering, module trailers across one
hop, and M17P (packet mode). Cases compare a system under test with it in
the same run, never against a stored value.

Setup the cases can rely on:

| Item | Value (setting) |
|---|---|
| designation | `M17-REF` |
| modules | `MNOP` (`DVT_M17_MODULES`) |
| peer `M17-DVT` | tester address `<net>.30`, UDP 17011 (`DVT_M17_PEER_DVT_PORT`), shares `M` (`DVT_M17_PEER_DVT_MODULES`) |
| peer `M17-DVU` | second tester address `<net>.32` (`DVT_M17_PEER_DVU_HOST`), UDP 17012 (`DVT_M17_PEER_DVU_PORT`), shares `MN` (`DVT_M17_PEER_DVU_MODULES`) |

mrefd tells peers apart by IP only (the port is ignored), so the two peers
cannot share an address: a DISC or PING from one would remove or refresh the
other. The `tester-addr` sidecar (profile `mrefd`) adds `<net>.32` to the
tester for M17-DVU; the system under test keeps `<net>.30` for both peers.
mrefd connects out to a configured peer, so the tester plays each peer by
listening on its port and answering the reflector `CONN`. A client `CONN`
for a configured module is answered `ACKN`, for any other module `NACK`.
Not available: encryption (no `EncryptionAllowed` module), IPv6, the
dashboard and the DHT, so cases must not expect them from this reference.

To run a system under test against it as the reference (compatibility data,
not a gate): `DVT_REF_KIND=mrefd scripts/run.sh -m "proto and not slow"`,
or only start it with `--profile mrefd`.
