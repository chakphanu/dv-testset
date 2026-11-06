<!-- SPDX-License-Identifier: MIT -->
# M17 v3 test sources for DV-Router (P26j, offline half)

Status: generator, vectors, unit tests and a DRAFT verifier image exist; the
image is NOT built and nothing here has run against DV-Router or m17-fme.
DV-Router's v3 gate has no runtime enable (Rust API only), so there is no rig
run for P26j: the evidence is the vector files in `vectors/m17v3/`, which a
test in the DV-Router repository reads verdict for verdict, plus a one-time
signature cross-check in m17-fme (Docker steps below).

What this does and does not prove (the honest wording for the tracker):
independent implementation of the signature scheme (m17-fme, micro-ecc),
independent GNSS decoders (libm17, mrefd) and spec vectors. It is NOT "a real
v3 client": no open-source implementation reads or writes the v3 TYPE yet
(per the research and the m17-fme source: m17-fme answers `Unknown LSF TYPE;`; per the research mrefd's `CFrameType` is buggy and
unused, pyM17 has the nibbles swapped).

## Files

| Path | What |
|------|------|
| `dvtest/ecdsa_p256.py` | pure-Python ECDSA P-256, RFC 6979 nonce, micro-ecc digest convention |
| `dvtest/proto/m17v3.py` | v3 TYPE, legacy mapping, META encoders and decoders, digest, signed-stream generator, negative builders, reference verifier |
| `dvtest/m17v3_vectors.py`, `scripts/gen-m17v3-vectors.py` | build and write the vector files |
| `vectors/m17v3/` | the vectors (`README.md` there: schema, verdicts, provenance) |
| `dvtest/oracle_m17.py` | m17-fme input files, output parser, comparison, thin Docker call |
| `rig/Dockerfile.m17fme`, `rig/m17fme-entry.sh` | verifier image (NOT built) |
| `scripts/pre-fetch-m17.sh`, `.env.example` (`M17_*`, `MICRO_ECC_*`, `LIBM17_*`) | one-time fetch and pins |
| `cases/unit/test_ecdsa_p256.py`, `test_m17v3.py`, `test_oracle_m17.py` | all pure |

## Sources fetched (read-only, `.cache/m17-src/`, git-ignored)

| Name | Ref (pin in `.env.example`) | Licence | Use |
|------|-----------------------------|---------|-----|
| M17_spec, branch dev (3.0.0-draft) | `e67ac209d0ec73c1bab12d56d67f1d22e4680d35` (`M17_SPEC_REF`) | GPL-2.0 | read; line numbers below |
| M17_spec, branch main (2.0.10) | `d3995d3131b2b0d15422624aff597e6ec50d2645` (`M17_SPEC_V2_REF`) | GPL-2.0 | read |
| m17-fme | `6a93a2abfc706eb1bb37adc89e1659cd417a9caa` (`M17_FME_REF`) | GPL-3.0 | verifier image |
| micro-ecc (M17-Project fork) | `31520c8f1f58920ef89086386d259640077ecb35` (`MICRO_ECC_REF`) | BSD-2-Clause | m17-fme dependency (the commit its submodule pins) |
| M17_Implementations | `f2000aec8a29b975aba2e80dd8c274afd6124421` (`M17_IMPL_REF`) | GPL-2.0 | read; optional symbol-level oracle |
| libm17 | `3684edce0866ce71b24a7be685673ca9999dd951` (`LIBM17_REF`) | GPL-2.0-or-later | read; GNSS / ECD / nonce reference |

GPL sources are test references: only protocol facts (field layouts,
constants, output strings) were taken; no code or text is copied. Spec lines
are cited, not quoted.

## Research versus spec

The research (`research-hwfree-clients.md`, item 2) was checked at the cited
lines. Everything not listed here matched (TYPE layout l.1114-1250, text
l.2203-2217, GNSS l.2081-2170, ECD l.2172-2201 and l.2176, signature
l.2051-2079, FN l.716, altitude l.2166 = 31767.5, libm17 and m17-fme line
numbers). Where they differ the spec (or the code) wins:

1. **GNSS radius rule is not in the draft.** The research put "smallest n with
   radius <= 2^n m, 7 above 128 m" under the draft l.2081-2170. The draft
   (e67ac209) only says the field is a coarse HDOP estimate (l.2153-2155); the
   rule is in 2.0.10 l.1397 (version history l.122, "Restore GNSS Radius
   encoding rule") and in libm17 `set_LSF_meta_position` (payload/lsf.c).
   Used: the 2.0.10 rule.
2. **AES counter order is settled in 2.0.10.** The research left it open.
   2.0.10 l.1537-1549 (history l.120, "Fixed contradiction in the AES CTR
   description") puts the timestamp in the most significant 32 bits, the 80
   random bits next, FN in the least significant 16 bits; the draft's
   l.2032 prose is the stale text. libm17 `set_LSF_meta_nonce` (timestamp first
   in META) and m17-fme (`m17_ipf_decoder.c`: IV bytes 14-15 = FN) agree.
3. **m17-fme `-N` enables the ncurses UI** (`src/main.c` case 'N'); the research
   recipe `m17-fme -u -k ... -N` read it as "no ncurses". Do not pass it; the
   image is built without curses anyway. The file input needs no audio: `-^`.
4. libm17 has `set_LSF_meta_nonce` (research listed only position and ECD): it is
   the oracle for the AES IV layout.
5. micro-ecc: `bits2int` is at uECC.c l.1211-1233 (research: 1222-1244); the
   big-endian copy applies because `uECC_VLI_NATIVE_LITTLE_ENDIAN` defaults to 0
   (uECC.h l.51-52) and no M17 project defines it; builds that do are
   incompatible by the header's own warning. Research's UNVERIFIED point is
   now read, not yet run (Docker steps).
6. Draft l.1156 says "15-byte META block" (typo, META is 14 bytes everywhere else).
7. m17-fme dates: head 6a93a2a is dated 2026-01-30 in the clone (research:
   2026-01-26). No effect.

## Readings of the three ambiguities (plus the two gaps)

| Question | Text | Reading used | Why |
|----------|------|--------------|-----|
| Text block index base | draft l.2209 "Possible values are 0..15", but "a 15 block message ... first 0xF1, last 0xFF" and packet mode "0x11" (l.2209, l.2215, l.1245) | 1-based: control = count<<4 \| index, index 1..15 | the examples fix it; the 0-based alternative is kept as a non-chosen vector (`text-v3-3-blocks-index-base-0`) |
| Altitude maximum | draft l.2166 "31767.5 m" | 32267.5 m (65535 * 0.5 - 500) | arithmetic; 2.0.10 l.1410 says 32267.5; libm17 clamps at 32267.5 |
| AES counter order | draft l.2032 prose vs l.2040-2046 table | timestamp, random, FN (FN last) | 2.0.10 l.1537-1549, libm17, m17-fme (both orders are in `meta_aes_iv` entries; only the first is "chosen") |
| Radius field (gap) | not in the draft | 2.0.10 rule | see above |
| Lat/lon rounding (gap) | only the scale is given (8388607 per 90 / 180 degrees) | cut toward zero, as libm17 | vectors use dyadic degrees so truncation and rounding agree except on the half-LSB; compare libm17 within 1 LSB (float32) |

## Derived versus read

Read in the spec (lines in each vector's `spec`): v3 TYPE bit layout, payload,
encryption, meta and CAN values, signed bit, text control byte and limits,
GNSS layout and scales, ECD layout and "never on the Internet", AES IV layout,
digest definition, FN schedule, EOT rule, "reduces the maximum length by 4
frames", "valid only if no frame is lost". Read in code (facts only):
micro-ecc digest-as-integer convention, key and signature byte order
(r||s, x||y), m17-fme digest accumulation, IP-frame file format, output
strings, libm17 truncation.

Derived (comment or `derived:` in each place): the TYPE integers (payload<<12 |
enc<<9 | signed<<8 | meta<<4 | can), the legacy TYPE integers and the mapping
(from the 2.0.10 table l.1157-1320), the legacy <-> v3 text conversion, the
"META omitted for a legacy receiver" policy (research brief; DV-Router's own
mapping may differ - compare), every raw GNSS value, the maximum of 0x7FFC data
frames (0x8000 - 4) and the 0x7FFD collision, all negative streams (what each
breaks is in its `title`), the digest variants (the wrong messages a faulty
signer might sign), key derivation (SHA-256 of a label), payload seeds, the
RFC 6979 nonce. Published by RFC 6979 A.2.5: the two P-256 vectors in
`ecdsa.json` and `test_ecdsa_p256.py`. Checked without micro-ecc: the
arithmetic against OpenSSL (`cryptography` package, skipped when absent) with
the 16-byte digest left-padded to 32 bytes.

## Strict collector expectations (`expect.collector`)

DV-Router's signature collector (`dv_lib_pkt::m17_signature`) is strict, so each
signed-stream entry also names its outcome there; `expect.verdict` stays the
spec-level verdict. `dvtest.proto.m17v3.collect_stream` is the reference of
the rules and the unit tests check every entry against it.

| `collector` | Entries |
|-------------|---------|
| `complete_valid` | valid, valid-meta-changes, boundary-max-frames |
| `complete_invalid_signature` | tampered-payload, wrong-key, digest-* (5) |
| `incomplete` | missing-tail-frame, missing-tail-two, missing-tail-all, truncated-mid-stream |
| `sequence_error` | lost-frame, reordered-frames, duplicate-data-frame, early-tail-chunk, eot-* (5), tail-duplicate-fn, tail-fn-shifted, boundary-one-too-many; signed-bit-no-tail (also acceptable: `incomplete`) |
| `size_error` | short-packet, short-data-packet, long-packet |
| `bad_public_key` | bad-key-off-curve, bad-key-63-bytes, bad-key-zero |
| `bad_signature_encoding` | bad-signature-r-zero, bad-signature-s-equals-n, bad-signature-s-max, bad-signature-r-equals-n |
| `null` (unsigned, signing bit clear, no tail expected) | unsigned-plain, unsigned-with-tail |

Assumption to confirm with the collector's author: the 16-bit FN is compared as
sent. Then EOT on any frame but the last chunk, or a last chunk of 7FFF instead
of FFFF, is a `sequence_error` (all `eot-*` entries and `signed-bit-no-tail`,
whose last data frame has FN 8005). If the collector masks the EOT bit of data
frames and reads it as "stream ended", `signed-bit-no-tail` is `incomplete`
(listed in `collector_also_acceptable`). `lost-frame` is a `sequence_error`,
not `complete_invalid_signature`, because FN 0, 1, 2, ... has a gap.

Added in this round: duplicate-data-frame, early-tail-chunk, short-data-packet,
long-packet, unsigned-plain, bad-key-off-curve, bad-key-63-bytes, bad-key-zero,
bad-signature-r-zero, bad-signature-s-equals-n, bad-signature-s-max,
bad-signature-r-equals-n. The GNSS entries gained `decoded` and
`decoded_float`; the key forms are unchanged (DV-Router uses `public_xy`).

## Planned cases P26j-1 .. P26j-9

All are driven by the vector files, not by the rig. "Assert" is what the
DV-Router Rust test compares; "oracle" is what checks the vector itself.

| Case | Asserts (file, entries) | Oracle |
|------|--------------------------|--------|
| P26j-1 TYPE codec | `type_meta.json` `v3_type` rows: TYPE integer and 2 bytes; `v3_type_invalid` rows are refused | spec only |
| P26j-2 legacy <-> v3 mapping | `map_legacy_to_v3` (20 rows incl. 1600 bps and reserved bits unmappable) and `map_v3_to_legacy` | spec only |
| P26j-3 gate matrix (test-only enable on, then off) | per `signed_streams.json` entry, the gate outcome next to the spec verdict: accept `valid`, `valid-meta-changes`, `boundary-max-frames`; `invalid_signature` entries (tampered payload, wrong key, lost frame, reordered, 5 digest variants); `incomplete` (missing tail frame x2, no tail, signed bit without tail, truncated); `malformed` (EOT x5, tail FN x2, short and long packets, early tail chunk, one-too-many); `unsigned-with-tail`, `unsigned-plain`; `bad-key-*`, `bad-signature-*` (see the collector table). With the gate off every v3 TYPE is refused. What "refuse" means (drop, strip signed flag, forward unsigned) is DV-Router's decision; the spec verdict is the vector | spec; signature rows also m17-fme |
| P26j-4 signature verification | `keys.json` + `ecdsa.json` + `valid`: the verifier accepts the 64-byte `r\|\|s` over the 16-byte digest with `public_xy` (or SEC1 `04\|\|x\|\|y`); RFC 6979 rows prove the arithmetic | m17-fme / micro-ecc (Docker), OpenSSL (done in unit test) |
| P26j-5 META encodings | `meta_text_v3` (13 bytes, 3 blocks, 15 blocks / 195 bytes, UTF-8, packet 0x11), `meta_text_v2`, `meta_gnss` (7 rows incl. extremes and nothing valid), `meta_ecd`, `meta_aes_iv` | libm17 / mrefd for GNSS, ECD, IV; spec only for text |
| P26j-6 legacy receiver gets META omitted | `map_v3_to_legacy` rows with policy `omit`: GNSS, text, ECD dropped (zero META, null-encryption text subtype), AES IV kept; policy `convert` rows are the alternative | spec + research brief |
| P26j-7 ECD refused on an Internet link | `meta_ecd` entries carry a full `packet` (TYPE 2021) that must not be forwarded | spec only (draft l.2176; 2.0.10 l.1420) |
| P26j-8 AES streams | `meta_aes_iv` `packet`s (TYPE 28f1): `gate_expectation` is null on purpose - the spec does not say what an Internet server does with encrypted streams | spec only |
| P26j-9 legacy re-label | every `valid` stream re-labelled with `legacy_type` also verifies (the signature covers payloads only) | m17-fme |

Not covered, by design: late-joiner timing of META inside a superframe (no v3
sender exists to copy), real v3 client interop, packet-mode v3.

## Docker steps to verify next (nothing here is built)

Do these when the rig is idle, inside `scripts/contained.sh --name build`.

1. `scripts/pre-fetch-m17.sh` (already done once in this worktree).
2. Build `rig/Dockerfile.m17fme` (command in its header). Things that may
   break: the named build contexts, `COPY --from=uecc *.c *.h *.inc` wildcards,
   `find_file(UECC ...)` seeing `src/micro-ecc/uECC.h` (otherwise `-- micro-ecc
   not found` and `Signature` lines say `uECC Support Not Compiled;`),
   `git_describe` without git, a missing `-lm`.
3. Smoke: `docker run --rm --network none dvt-m17fme:local version` prints the
   usage banner.
4. `python3 -m dvtest.oracle_m17 --image dvt-m17fme:local --out out/m17fme.json
   --verdicts-out out/oracle_verdicts.json`. One container per vector. Expect:
   `valid` and `valid-meta-changes` -> ` Signature Valid;`; the
   `invalid_signature` rows -> ` Signature Invalid;`; the `incomplete` rows -> no
   signature line; `malformed` and `unsigned` rows are reported with
   `agrees: null` (m17-fme keys on FN only).
5. If `valid` prints `Invalid`: the micro-ecc convention is wrong in
   `dvtest/ecdsa_p256.py` (check `uECC_VLI_NATIVE_LITTLE_ENDIAN`, left-padding);
   rerun by hand with `-v 1` (m17-fme then prints the four signature chunks)
   and compare with the vector's `signature`. This is the first thing to
   settle; everything else in P26j-4 depends on it.
6. Copy `out/oracle_verdicts.json` to `vectors/m17v3/oracle_verdicts.json`, run
   `scripts/gen-m17v3-vectors.py`; the unit test then keeps the files
   consistent.
7. Optional, GNSS: a ten-line C driver linking libm17 (`set_LSF_meta_position`
   with each entry's `libm17_args`, print `meta`) in a second build stage;
   compare within 1 LSB on latitude and longitude. Not written.
8. Re-check upstream (m17-fme, OpenRTX, DroidStar) for a v3 TYPE before closing
   P26j; open a spec issue for the draft's three ambiguities (text index base
   l.2209, altitude l.2166, AES prose l.2032; the last is already fixed in
   2.0.10 but not in the draft).

## What the DV-Router test needs to supply

- The name of the P26j gate test and the way it loads `vectors/m17v3/*.json`.
- The test-only way to enable the v3 gate (none in the image; the Rust API).
- Whether a failing-signature, incomplete or malformed stream is dropped,
  forwarded unsigned or forwarded as is, per `kind` of the vector: the files
  hold the spec verdict, the gate outcome column of P26j-3 is theirs to fill.
- The verifier's key format (the vectors give `public_xy` and SEC1 `04\|\|x\|\|y`).
