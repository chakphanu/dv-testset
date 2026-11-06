<!-- SPDX-License-Identifier: MIT -->
# vectors/m17v3 - M17 v3 test vectors (P26j)

Text-only test vectors for an M17 v3 gate: TYPE and META encodings, the
legacy <-> v3 mapping, and signed streams with their negatives. A test in
another project can read the JSON verdict for verdict; none of it depends on
that project's code. Background, readings of the spec and the plan are in
`docs/m17v3-cases.md`.

## Files

| File | Content |
|------|---------|
| `manifest.json` | schema, generator, source pins, citation and oracle tables, file list with sha256 |
| `keys.json` | two P-256 test keys, `secret` (32 bytes), `public_xy` (x\|\|y, 64 bytes big endian, micro-ecc form), `public_sec1_uncompressed` (04\|\|x\|\|y, 65 bytes). DV-Router uses `public_xy` and prepends the 04 itself; the SEC1 form is kept for other consumers |
| `ecdsa.json` | digest + signature + public key + expected verify result; includes the two vectors published by RFC 6979 appendix A.2.5 (SHA-256 digests) |
| `signed_streams.json` | signed M17 streams as lists of 54-byte stream packets (hex, arrival order) with the expected verdict |
| `type_meta.json` | v3 TYPE values, the legacy <-> v3 mapping, META encodings (text v3 and v2, GNSS, ECD, AES IV) |

All hex is lower case, all multi-byte fields are big endian. One entry per
line; every entry has `id`, `kind`, `spec`, `oracle`, `oracle_verdict`. GNSS
entries carry `decoded` (every field as the integer the spec scales it to:
source, station, validity, radius n, bearing, latitude, longitude, altitude,
speed; the integers are the expectation of a decoder, no encoder rounding is
assumed) and `decoded_float` (the same values in degrees, metres, km/h; `null`
where the validity bit is clear). The `inputs` and `libm17_args` are what the
encoder was given; the decoded floats are the exact scaling of the integers and
can differ from the inputs by less than one LSB:

- `spec`: the file and line that were read (`draft l.N` = `M17_spec.tex` of the
  3.0.0 draft at the pin `M17_SPEC_REF`; `v2.0.10 l.N` = the same file at
  `M17_SPEC_V2_REF`), or `derived: <how>`, or `published by RFC 6979`.
- `oracle`: the independent implementation that can check the entry
  (`m17-fme`, `libm17`, `libm17+mrefd`, `libm17+m17-fme`, `ecdsa-verifier`,
  `spec-only`; described in `manifest.json`).
- `oracle_verdict`: `null` until that oracle has been run (m17-fme: `valid`,
  `invalid_signature`, `no_verdict` = no signature line printed, `decode_match`
  for the v2 text META; `docs/m17v3-cases.md`, "Build and run results"). The results come from
  the optional file `oracle_verdicts.json` (`{entry id: verdict}`, written by
  `python3 -m dvtest.oracle_m17 --verdicts-out ...`); the generator copies it
  into the entries, so the files stay reproducible.

## Signed streams

Packet layout: `"M17 "` | stream id (2) | DST (6) | SRC (6) | TYPE (2) | META (14) |
FN (2, MSB = EOT) | payload (16) | CRC16 (poly 0x5935, init 0xFFFF, bytes 0..51).
The signature covers the 16-byte payload digest only (not DST, SRC, TYPE or
META): `digest = 16 zero bytes; for each data frame: digest ^= payload;
digest = rotate-left-one-byte(digest)`; signature = ECDSA P-256 over the
digest read as one big-endian integer (left-padded to 32 bytes), `r||s`
(64 bytes), sent as four 16-byte chunks in frames FN 0x7FFC, 0x7FFD, 0x7FFE and
0xFFFF (0x7FFF with the EOT bit). Data frames are FN 0.., no EOT.

`expect.verdict` is one of

| Verdict | Meaning |
|---------|---------|
| `valid` | complete tail, signature verifies under `verify_key` |
| `invalid_signature` | complete tail, the signature does not verify |
| `incomplete` | signed bit set, tail missing or short |
| `malformed` | FN or EOT rules broken, or a packet is not 54 bytes |
| `unsigned` | signed bit clear (the tail, if present, is ignored) |

`expect.also_acceptable` lists other verdicts another oracle may give
(reordered frames: a receiver that restores FN order verifies the stream).

`expect.collector` is the outcome of a strict collector (reference:
`dvtest.proto.m17v3.collect_stream`): data frames exactly FN 0, 1, 2, ..., the
tail exactly 7FFC, 7FFD, 7FFE, FFFF in order (the 16-bit FN compared as sent,
EOT bit included), the tail never in the digest.

| `expect.collector` | Meaning |
|--------------------|---------|
| `complete_valid` | all four chunks, signature verifies |
| `complete_invalid_signature` | all four chunks, verification returns false (tampered payload, wrong key, wrong message signed) |
| `incomplete` | stream ended before the fourth chunk |
| `sequence_error` | gap, duplicate, early tail chunk, reordered frame, wrong tail FN or EOT placement, packet after the tail |
| `size_error` | a packet that is not 54 bytes |
| `bad_public_key` | key not 64 bytes or not on the curve |
| `bad_signature_encoding` | r or s zero or not below n |
| `null` | unsigned stream: signing bit clear, no tail expected, the collector is not used |

`expect.collector_also_acceptable` lists other collector outcomes that follow
from a different but plausible reading (only `signed-bit-no-tail`: EOT on the
last data frame is `sequence_error` when the FN is compared as sent,
`incomplete` when the EOT bit of a data frame is read as "stream ended").

Every stream entry also has `signing_bit` (the TYPE signed bit; false only
for the `unsigned-*` entries), `tail_expected` (equal to `signing_bit`: an
unsigned stream has no tail to wait for) and `verify_public_xy` (the 64-byte
key to verify with, or the deliberately bad key of the `bad-key-*` entries).
Most streams carry TYPE `2101` (v3: voice, not encrypted, signed, no META,
CAN 1); `legacy_type` is the 2.0.x TYPE of the same stream (`0885`) for
decoders that do not know the v3 layout (`legacy_meta: null` = zero META).

Entries with `packets: null` are long streams given as a `recipe` (the two
frame-count boundary cases: 0x7FFC and 0x7FFD data frames, 3.5 MB as hex).
Rebuild them: packet `i` has FN `i`, payload `i` = first 16 bytes of
SHA-256 of the ASCII string `<seed>/<i>` (the seed is in `recipe.payload_i`),
the same TYPE, SRC `N0CALL`, DST broadcast, zero META, no EOT; the tail is
`recipe.tail_packets`. `recipe.head_packets` and `recipe.last_data_packet`
let a test check its own rebuild before using it.

## Provenance

Generated by `python3 scripts/gen-m17v3-vectors.py` (deterministic: ECDSA
nonces are RFC 6979, payloads are seeded hashes, there is no clock or random
source; `--check` compares with the committed files, and
`cases/unit/test_m17v3.py` regenerates them in memory and fails when they
differ). The generator is `dvtest/m17v3_vectors.py` on top of
`dvtest/proto/m17v3.py` and `dvtest/ecdsa_p256.py`. Source pins are in
`.env.example` (`M17_SPEC_REF`, `M17_SPEC_V2_REF`, `M17_FME_REF`,
`MICRO_ECC_REF`, `M17_IMPL_REF`, `LIBM17_REF`; fetched by
`scripts/pre-fetch-m17.sh`) and listed in `manifest.json`.

Licence: the vectors are MIT (the repository licence). They are derived from
the specification text and arithmetic and were not copied from GPL code; the
GPL projects (m17-fme, libm17, M17_Implementations, mrefd, the spec
repository itself) were read as test references to check the conventions and
are named only as oracles. The keys are throwaway test keys derived from a
label; never use them for anything else.
