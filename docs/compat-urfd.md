<!-- SPDX-License-Identifier: MIT -->
# Compatibility with URFd

This is a derived report, not a specification. It summarises rig runs in
which the same conformance cases compared the system under test once with
xlxd-ref and once with URFd (`DVT_REF_KIND=urfd`, rig service `urfd` at
<net>.70; see `scripts/run.sh` and `rig/compose.yml`). A "same" cell means
the system under test behaved like that reference, and "differs" means it
did not. In these runs the system under test was the xlxd pull-request tree
(xlxd-sut), so the urfd column reads as "where URFd differs from xlxd". The
judging was always the `xlxd` profile, and results are compatibility data,
not gates.

URFd in the rig: n7tae/urfd @ `bd0c114e`, built without DHT, callsign
URF999, modules A-Z, **no transcoder**. It is same-codec passthrough only:
cross-mode cases (the `CROSSMODE` table in `cases/conftest.py`) are "n/a".
The xlxd-only suites (XLX interlink, USRP rows, netns, transcoder wire,
matrix, NXDN/P25 peering) are "n/a" by file.

Runs (rig instance 2, transcoder vambed-neo, fast cases: `-m "proto and
not slow"`). The tables were made with `scripts/compat-table.py <run-xlxd>
<run-urfd>`.

| Set | ref xlxd | ref urfd |
|---|---|---|
| DExtra, MMDVM, M17 | `20261030T094103Z-i2`: 161 same, 2 skip | `20261030T092824Z-i2`: 110 same, 51 differs, 2 skip |
| DPlus, DCS, YSF, G3, DMR+ (profile g3) | `20261030T142923Z-i2`: 170 same, 3 skip | `20261030T145510Z-i2`: 109 same, 59 differs, 2 n/a, 3 skip |

Per suite, ref urfd:

| Suite | same | differs | n/a | skip |
|---|---|---|---|---|
| DExtra (`test_dextra.py`) | 44 | 15 | 0 | 1 |
| MMDVM (`test_mmdvm.py`) | 38 | 12 | 0 | 0 |
| M17 (`test_m17.py`) | 28 | 24 | 0 | 1 |
| DPlus (`test_dplus.py`) | 11 | 18 | 0 | 0 |
| DCS (`test_dcs.py`) | 32 | 12 | 0 | 1 |
| YSF (`test_ysf.py`) | 19 | 14 | 0 | 0 |
| G3 (`test_g3.py`) | 15 | 10 | 0 | 1 |
| DMR+ (`test_dmrplus.py`) | 32 | 5 | 2 | 1 |

## Main differences (URFd vs xlxd)

- **M17**: URFd takes the module from the M17 destination field, not from
  the CONN module. It drops a stream whose destination does not decode to a
  callsign plus module. Probed directly:
  - dropped: the broadcast address 0xFFFFFFFFFFFF (the tester default, and
    the M17 broadcast) and `M17-DVT C`;
  - relayed: `XLX999 C` and `URF-999 C`;
  - dropped silently, not yet explained: URFd's own callsign `URF999 C`.

  Most of the 24 M17 rows follow from this one rule. A URFd-aware
  destination variant of the relay probes is with the M17 suite owner.
- **DExtra**: no NAK for an invalid callsign or module link; reflector
  keepalive every 3 s (xlxd 10 s); egress header CRC not valid and the RPT
  suffix blank; slow data rewritten, not passed through; voice after the
  last frame still relayed; no header for a late listener.
- **MMDVM**: voice from a client that sent no RPTC is not routed; a header
  after an MS data sync is accepted; voice without a header, CSBK and data
  bursts are relayed; repeater id = login id; the salt does not change on
  a restart; no synthesized terminator when the terminator is lost.
- **DPlus**: no BUSY for an invalid callsign; drop timeout 10 s (xlxd 30 s);
  egress header CRC wrong; end frames and end forms differ; no late-listener
  header; one IP with two ports gets nothing.
- **DCS**: no NAK for an invalid link; blank suffix instead of `DCS `; slow
  data, last-packet payload and counter rewritten; the next stream after an
  end form is not heard; no synthesized last frame on a timeout.
- **YSF**: the client still hears after an unlink; another module hears the
  stream; same-mode voice not byte-exact; DG-ID and default-module rules
  differ; DMR to YSF egress is empty (no transcoder).
- **G3**: routing and presence answers carry URF999 in the RPT fields; a G3
  talk does not reach DExtra listeners; no 10 s G3 ping.
- **DMR+**: a connect to 4000/4500 gets only ACK (xlxd also sends NAK); a
  repeated header is not deduplicated; a busy module lets a second talker in.

To redo the tables, run the same selection twice with `DVT_REF_KIND=xlxd`
and `=urfd` (add `--profile g3` for G3). Then give both run directories to
`scripts/compat-table.py`.
