<!-- SPDX-License-Identifier: MIT -->
# Transcoder levels per image tag (cases/test_transcoder_levels.py)

Question from dv-router: which transcoder image tag to deploy. Three tags, the rig's `transcoder-ref` and `transcoder-sut` both on the same one:

| Tag | What it is |
|-----|------------|
| `<transcoder image>:64s-pcm-823ce72a` | production / rollback |
| `<transcoder image>:64s-pcm-688d9bf7-gain` | xlxd-net per-route gains (D-STAR -> DMR -10 dB, DMR -> D-STAR +10, D-STAR -> Codec2 -10, DMR -> Codec2 +10, Codec2 -> D-STAR +3, Codec2 -> DMR -7) and the Codec2 -> AMBE calibration; IMBE 0 |
| `<transcoder image>:64s-pcm-688d9bf7-unity` | `GAIN_UNITY=1`, calibration off = the levels of 823ce72a with the newer code |

(the delivery notes README.txt and LEVELS.md of the transcoder image; the tags are in the local docker store, nothing is deployed.)

## Method

- Image of a run: `TRANSCODER_IMAGE=<tag>` in the calling shell (it wins over `.env`; `rig/compose.yml` gives the tester `DVT_TRANSCODER_TAG`).
  One `run.sh` run per tag, `DVT_TRANSCODER_LEVELS=1` switches the case on, the real transcoder (no `DVT_TRANSCODER_FAKE`).
- Nine pairs: M17 -> DMR, D-STAR, YSF; DMR -> D-STAR, M17, YSF; D-STAR -> DMR, M17, YSF. Per source protocol one talker and one listener of each target protocol
  on module H of the SUT; every listener hears every over.
- Signals: the 700 Hz tone (amp 8000, active level -15.2 dBFS, 3 s) and **real speech**: the three Thai clips the vocoder session measured
  (`thai_fixed`, `porjai02`, `porjai03`; 8 kHz S16LE, the first 6.0 s of each; `thai_fixed` is the `thai_fixed_pk12.pcm` of the vocoder project's level
  scratch directory, the other two its `tests/differential/ambe2400/crossdecode/porjai_thai_0{2,3}/source.pcm`), each scaled to an active level of
  -26, -20 and -14 dBFS (frames above -50 dBFS count as active; peaks are clipped at 16 bit). Provenance and licence: copied from material that is
  already on this host (nothing was downloaded); the repository records no licence for it, so the clips are **not committed** (git-ignored
  `.cache/speech-levels/`); sha256 of the 8 kHz files used: `thai_fixed` 4f16b002...92c (96000 bytes), `porjai02` 56a20255...d63 (96000 bytes),
  `porjai03` b3f4c9a2...446c (65280 bytes). Without them the case falls back to CMU ARCTIC utterances (`scripts/pre-fetch-speech.sh`; the rows are then
  labelled `arctic_*`). Each signal is encoded into the talker's codec by transcoder-ref (so the talker's frames are real vocoder output of the same tag).
- Measurement: the listener's frames of the over are decoded by transcoder-ref ("reference decoder = transcoder-ref direct", the same tag) to PCM; reported
  per (pair, tag, signal): `rms` (16-bit units, whole signal), active dBFS, delta to the source's active dBFS, frames received / sent (drops), content:
  tone = dominant frequency within 80 Hz of 700 and at least half of the spectrum within 60 Hz; speech = log-energy envelope correlation with the source
  (best of +-4 frames of delay; `score.envelope_correlation`, a proxy for "the stream still carries the speech", not an intelligibility score) of at least 0.5.
- Load: `test_load_16_streams` runs 16 streams at once (the nine pairs in turn), a clip repeated for about 22 s; frames received per stream. The transcoder
  CPU is sampled on the host with `scripts/transcoder-cpu-sample.sh` (docker stats of `dvt-transcoder-sut-1`, read only) for the same window
  (`out/<run>/transcoder-levels-load.json` has its start and end).

The YSF listener hears the SUT's YSF egress, which changes the AMBE+2 gain index b2 by -0.4 dB (xlxd parity, `docs/echo-cases.md`); the rows into YSF include it.

## Run it

```sh
export XLXD_SUT_IMAGE=dv-router:wip-c165c93 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_LEVELS=1
for tag in 823ce72a 688d9bf7-gain 688d9bf7-unity; do
  TRANSCODER_IMAGE=<transcoder image>:64s-pcm-$tag scripts/run.sh -m rig -- cases/test_transcoder_levels.py
done
python3 -m dvtest.tools.levels_table out/<run1>/transcoder-levels.jsonl out/<run2>/transcoder-levels.jsonl out/<run3>/transcoder-levels.jsonl
```

About 10 minutes a tag (30 overs per source protocol, three source protocols, then the load case).

### Recipe: one tag on the current rig (candidate or production)

The vocoder image of a run is selected by one variable, `TRANSCODER_IMAGE` (`.env.example`; `rig/compose.yml` services `transcoder-ref` and
`transcoder-sut`). Do not set the per-side overrides `TRANSCODER_REF_IMAGE` / `TRANSCODER_SUT_IMAGE` for this case (both must run the tag, and the
tester reads the tag for its rows from `TRANSCODER_IMAGE` through `DVT_TRANSCODER_TAG`). `VAMBED_IMAGE` is DV-Router's production compose variable
and is not read by this repository. The tags in the local docker store: `vocoder-ambed:64s-pcm-823ce72a` (production, rollback),
`vocoder-ambed:64s-pcm-688d9bf7-unity`, `vocoder-ambed:64s-pcm-688d9bf7-gain`; later images (49f237f0) are loaded with `docker load` first. The SUT
image must be the same for both runs of a comparison. Run the two tags one after the other (one rig at a time), the production tag and the candidate:

```sh
export XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_LEVELS=1
mkdir -p out/voc1
for tag in 823ce72a 688d9bf7-unity; do                  # production first, then the candidate
  # CPU of transcoder-sut for the load case (read only, docker stats); it starts when the container exists and ends 60 s after it is gone
  ( until [ -n "$(docker ps -q --filter name=dvt-transcoder-sut-1)" ]; do sleep 2; done
    scripts/transcoder-cpu-sample.sh out/voc1/cpu-$tag.txt ) &
  TRANSCODER_IMAGE=vocoder-ambed:64s-pcm-$tag scripts/run.sh -m rig -- cases/test_transcoder_levels.py
  run="$(ls -d out/2* | tail -1)"                       # the run just finished
  cp "$run/transcoder-levels.jsonl" out/voc1/levels-$tag.jsonl
  cp "$run/transcoder-levels-load.json" out/voc1/load-$tag.json
  wait
done
```

Both result files per tag are plain text (JSON lines): `transcoder-levels.jsonl` (one row per pair and signal, keys `tag`, `pair`, `signal`, `sent`, `got`,
`rms`, `delta_db`, `ok`, ...) and `transcoder-levels-load.json` (`tag`, `start`, `end`, `streams`). The CPU file has one `<epoch> <cpu %> <mem>` line per sample.

### Decision rule VOC1 (dvtest/tools/voc1_rule.py)

For a vocoder image to replace the production image (DV-Router decision VOC1, 2026-11-07) the candidate run and the production run, same SUT image, must
satisfy ALL four rules; `dvtest.tools.voc1_rule` prints the table with PASS / FAIL per rule and the verdict:

```sh
python3 -m dvtest.tools.voc1_rule \
  --candidate out/voc1/levels-688d9bf7-unity.jsonl --production out/voc1/levels-823ce72a.jsonl \
  --candidate-load out/voc1/load-688d9bf7-unity.json --candidate-cpu out/voc1/cpu-688d9bf7-unity.txt \
  --production-load out/voc1/load-823ce72a.json --production-cpu out/voc1/cpu-823ce72a.txt
```

| Rule | Check (candidate against the production tag) |
|------|-----------------------------------------------|
| 1 delivery | frames received >= sent on every row of every pair, content check ok on every row (tone and speech) |
| 2 speech | per pair the mean active-level delta to the source over the 9 speech signals is within 0.5 dB of production, or the pair moves TOWARD the source (the absolute mean delta decreases); no pair gets farther by more than 0.5 dB |
| 3 tone | Codec2 -> DMR and Codec2 -> D-STAR 700 Hz rms (amp 8000) rises above production and reaches at least 900 rms (target about 1260 = Codec2's own level); every pair with a non-Codec2 source (tone and speech rows) stays identical (0.0 dB, so a changed AMBE -> AMBE pair fails); the tone is delivered 150/150 frames |
| 4 load | the 16-stream case completes 16/16 and the mean transcoder CPU of its window is at most 2x the production mean |

For a carrier image that must not change anything (the unity tag), `--identical-ok` replaces rule 3: every row, the Codec2 tones included, must equal
production (delta within the identical tolerance, tone rms within 1 percent, all tone frames delivered). Recorded on wip-0348b98:
`vocoder-ambed:64s-pcm-688d9bf7-unity` against `64s-pcm-823ce72a` (out 20261107T091839Z and 20261107T090732Z): rules 1, 2, 4 pass, all 9 speech pairs
+0.0 dB, Codec2 tone rms 438 (m17>dmr) and 985 (m17>dstar) on both tags; with `--identical-ok` the verdict is SHIP as a carrier. Without the flag the same
files give "DO NOT SHIP (rule 3 FAIL)" because unity carries no tone fix: rule 3 in its normal form is the acceptance of the 49f237f0 tone-fix pair.

Exit status 0 = ship (all four PASS), 1 = do not ship (a rule failed, or rule 4 has no data because the four load files were not given: the decision needs
all four), 2 = unusable input (unreadable file, rows of more than one tag on a side; a pair without its 9 speech signals is a rule 2 problem).
`--json` prints the same result as JSON for the tracker row. Nothing is deployed by the tool; a failing candidate keeps the table as evidence.
Rule 3 does not judge `Codec2 -> YSF` (not named by the decision; rule 2 and rule 1 cover its speech and delivery). Calibration of the thresholds with the
2026-11-04 rows: the `unity` tag against `823ce72a` passes rules 1, 2 and 4 and fails rule 3 (it does not raise the Codec2 tone; that is the 49f237f0 fix),
the `gain` tag fails rule 2 on 8 of 9 pairs and rule 3 on the unchanged AMBE pairs.

## Results

Runs on `dv-router:wip-c165c93`, real transcoder (SUT and reference on the same tag), 2026-11-04: tag 823ce72a `out/20261104T081225Z`, 688d9bf7-gain
`out/20261104T082506Z`, 688d9bf7-unity `out/20261104T083527Z` (each 4 cases passed, about 10 minutes). 270 measurements (3 tags x 9 pairs x 10 signals).
Two earlier attempts of the first tag were lost to bugs of the case, not of the transcoder (clients without keepalive were dropped by the reflector between
overs; the 16 load streams shared one callsign and were refused with OriginHoldsFloor) and are not used.

### Conclusion

1. **`unity` is production.** Its 90 rows have exactly the levels of 823ce72a (largest difference 0.0 dB); frames and content are identical. It is a safe
   replacement for the rollback tag with the newer code.
2. **Production already keeps real speech close to the source.** Mean change of the active level over the nine speech signals per pair: M17 -> DMR -1.9 dB,
   M17 -> D-STAR -1.7, DMR -> D-STAR -0.7, DMR -> M17 +1.5, D-STAR -> DMR -0.9, D-STAR -> M17 -0.2; single signals between -4.5 and +3.3 dB. Into YSF it is
   lower: M17 -> YSF -4.8, DMR -> YSF -3.4, D-STAR -> YSF -4.1 (YSF egress: VCH and the b2 -0.4 dB step, the same in every tag).
3. **The `gain` tag is further from the source than production on 8 of 9 pairs and equal on one (DMR -> YSF, which has no route gain).** D-STAR sources are
   9 to 13 dB too quiet (D-STAR -> DMR -10.3, -> M17 -9.4, -> YSF -13.5); DMR sources into D-STAR and M17 are 5.5 to 5.9 dB too loud (single signals up to
   +11.8); M17 -> DMR is -5.4 dB against -1.9 on production, so the warning of the vocoder session holds: M17 -> DMR (and M17 -> YSF, -8.2 against -4.8) is
   quieter with the gain tag; M17 -> D-STAR overshoots (+3.3 against -1.7). No pair is closer to the source with `gain`. The tone shows the same
   (M17 -> DMR rms 267 against 438, M17 -> YSF 27 against 73, DMR -> D-STAR 18367 against 5865).
   What this does not say: the gain tag is built to match the levels of the xlxd network (per-route gains), and this table compares with the SOURCE
   signal, not with xlxd; if parity with xlxd is the goal, the gain tag may still be what is wanted. With the same transcoder on both sides, xlxd-ref is no
   reference for it either.
4. **No decode or delivery problem on any tag.** All 270 rows deliver every frame (0 lost; 0 to 5 frames more than sent: M17 EOT pair, DMR bursts, the YSF pad
   packet) and the content check holds (tone at 690 to 700 Hz; speech envelope correlation with the source 0.94 or better).
5. **Load is the same on all tags.** 16 simultaneous cross-mode streams of 22 s (the nine pairs in turn): 16 of 16 streams complete on each tag, 31224 of
   31200 frames, 0 lost; transcoder-sut CPU (docker stats, 2 s samples, 45 s window): 823ce72a mean 4.6 % / max 6.8 % of one core, gain mean 5.3 % / max 6.6 %,
   unity mean 5.2 % / max 6.5 %; memory at most 5.1 MiB. The gain tag costs no measurable CPU.

Caveats: the speech is three Thai clips (the clips of the vocoder session, first 6 s; see Method), no other language or speaker; the content
check is an envelope proxy, not an intelligibility score; the reference decoder is the same transcoder tag, so a decoder-side gain would show in all rows.

### Per pair (speech: mean level change over the nine signals, min..max; tone; frames lost; rows whose content check failed)

| pair | 688d9bf7-gain: speech delta dB mean (min..max) / tone delta dB, rms / lost frames / bad | 688d9bf7-unity: speech delta dB mean (min..max) / tone delta dB, rms / lost frames / bad | 823ce72a: speech delta dB mean (min..max) / tone delta dB, rms / lost frames / bad |
|---|---|---|---|
| m17>dmr | -5.4 (-7.9..-4.4) / -26.5, 267 / 0 / 0 | -1.9 (-4.5..-0.9) / -22.2, 438 / 0 / 0 | -1.9 (-4.5..-0.9) / -22.2, 438 / 0 / 0 |
| m17>dstar | +3.3 (+0.3..+5.2) / -9.1, 1980 / 0 / 0 | -1.7 (-4.2..-0.5) / -15.2, 985 / 0 / 0 | -1.7 (-4.2..-0.5) / -15.2, 985 / 0 / 0 |
| m17>ysf | -8.2 (-10.4..-6.9) / -28.3, 27 / 0 / 0 | -4.8 (-7.5..-3.7) / -28.3, 73 / 0 / 0 | -4.8 (-7.5..-3.7) / -28.3, 73 / 0 / 0 |
| dmr>dstar | +5.9 (+1.7..+9.6) / +10.3, 18367 / 0 / 0 | -0.7 (-2.6..+0.1) / +0.3, 5865 / 0 / 0 | -0.7 (-2.6..+0.1) / +0.3, 5865 / 0 / 0 |
| dmr>m17 | +5.5 (-0.8..+11.8) / +9.5, 16709 / 0 / 0 | +1.5 (-1.3..+3.3) / +0.1, 5678 / 0 / 0 | +1.5 (-1.3..+3.3) / +0.1, 5678 / 0 / 0 |
| dmr>ysf | -3.4 (-5.2..-2.5) / -0.8, 5145 / 0 / 0 | -3.4 (-5.2..-2.5) / -0.8, 5145 / 0 / 0 | -3.4 (-5.2..-2.5) / -0.8, 5145 / 0 / 0 |
| dstar>dmr | -10.3 (-11.9..-9.3) / -10.0, 1775 / 0 / 0 | -0.9 (-2.9..-0.1) / -0.1, 5584 / 0 / 0 | -0.9 (-2.9..-0.1) / -0.1, 5584 / 0 / 0 |
| dstar>m17 | -9.4 (-11.4..-8.4) / -12.4, 1346 / 0 / 0 | -0.2 (-2.5..+1.4) / -3.0, 3962 / 0 / 0 | -0.2 (-2.5..+1.4) / -3.0, 3962 / 0 / 0 |
| dstar>ysf | -13.5 (-15.0..-11.7) / -11.5, 1506 / 0 / 0 | -4.1 (-6.3..-2.9) / -1.5, 4740 / 0 / 0 | -4.1 (-6.3..-2.9) / -1.5, 4740 / 0 / 0 |


### Every measurement

Cell: `rms / active dBFS (delta to the source's active dBFS) / frames received/sent / content` (tone: dominant frequency; speech: envelope correlation
with the source). Source signal levels in column 3 are the active levels of the encoded input.

| pair | signal | in act dBFS | 688d9bf7-gain: rms / act dBFS (delta) / got/sent / content | 688d9bf7-unity: rms / act dBFS (delta) / got/sent / content | 823ce72a: rms / act dBFS (delta) / got/sent / content |
|---|---|---|---|---|---|
| m17>dmr | tone | -15.3 | 267 / -41.7 (-26.5) / 153/150 / ok 700 Hz | 438 / -37.4 (-22.2) / 153/150 / ok 700 Hz | 438 / -37.4 (-22.2) / 153/150 / ok 700 Hz |
| m17>dstar | tone | -15.3 | 1980 / -24.4 (-9.1) / 151/150 / ok 700 Hz | 985 / -30.4 (-15.2) / 151/150 / ok 700 Hz | 985 / -30.4 (-15.2) / 151/150 / ok 700 Hz |
| m17>ysf | tone | -15.3 | 27 / -43.5 (-28.3) / 155/150 / ok 700 Hz | 73 / -43.5 (-28.3) / 155/150 / ok 700 Hz | 73 / -43.5 (-28.3) / 155/150 / ok 700 Hz |
| m17>dmr | thai_fixed@-26 | -26.3 | 748 / -30.7 (-4.4) / 303/300 / ok env 0.99 | 1147 / -27.2 (-0.9) / 303/300 / ok env 0.99 | 1147 / -27.2 (-0.9) / 303/300 / ok env 0.99 |
| m17>dstar | thai_fixed@-26 | -26.3 | 2234 / -21.7 (+4.6) / 301/300 / ok env 0.99 | 1175 / -26.9 (-0.6) / 301/300 / ok env 0.99 | 1175 / -26.9 (-0.6) / 301/300 / ok env 0.99 |
| m17>ysf | thai_fixed@-26 | -26.3 | 518 / -33.2 (-6.9) / 305/300 / ok env 0.98 | 789 / -30.0 (-3.7) / 305/300 / ok env 0.98 | 789 / -30.0 (-3.7) / 305/300 / ok env 0.98 |
| m17>dmr | thai_fixed@-20 | -20.7 | 1330 / -26.0 (-5.3) / 303/300 / ok env 0.99 | 2045 / -22.6 (-1.8) / 303/300 / ok env 0.99 | 2045 / -22.6 (-1.8) / 303/300 / ok env 0.99 |
| m17>dstar | thai_fixed@-20 | -20.7 | 3613 / -17.9 (+2.8) / 301/300 / ok env 0.99 | 2050 / -22.5 (-1.7) / 301/300 / ok env 0.99 | 2050 / -22.5 (-1.7) / 301/300 / ok env 0.99 |
| m17>ysf | thai_fixed@-20 | -20.7 | 927 / -28.8 (-8.1) / 305/300 / ok env 0.99 | 1359 / -25.6 (-4.9) / 305/300 / ok env 0.99 | 1359 / -25.6 (-4.9) / 305/300 / ok env 0.99 |
| m17>dmr | thai_fixed@-14 | -15.2 | 1953 / -23.1 (-7.9) / 303/300 / ok env 0.98 | 2966 / -19.6 (-4.5) / 303/300 / ok env 0.98 | 2966 / -19.6 (-4.5) / 303/300 / ok env 0.98 |
| m17>dstar | thai_fixed@-14 | -15.2 | 5189 / -14.9 (+0.3) / 301/300 / ok env 0.98 | 3049 / -19.4 (-4.2) / 301/300 / ok env 0.98 | 3049 / -19.4 (-4.2) / 301/300 / ok env 0.98 |
| m17>ysf | thai_fixed@-14 | -15.2 | 1402 / -25.5 (-10.4) / 305/300 / ok env 0.98 | 2045 / -22.6 (-7.5) / 305/300 / ok env 0.98 | 2045 / -22.6 (-7.5) / 305/300 / ok env 0.98 |
| m17>dmr | porjai02@-26 | -25.6 | 684 / -30.1 (-4.5) / 303/300 / ok env 0.99 | 1051 / -26.5 (-0.9) / 303/300 / ok env 0.99 | 1051 / -26.5 (-0.9) / 303/300 / ok env 0.99 |
| m17>dstar | porjai02@-26 | -25.6 | 2144 / -20.7 (+5.0) / 301/300 / ok env 0.99 | 1086 / -26.3 (-0.6) / 301/300 / ok env 0.99 | 1086 / -26.3 (-0.6) / 301/300 / ok env 0.99 |
| m17>ysf | porjai02@-26 | -25.6 | 413 / -33.9 (-8.2) / 305/300 / ok env 0.98 | 675 / -29.8 (-4.1) / 305/300 / ok env 0.98 | 675 / -29.8 (-4.1) / 305/300 / ok env 0.98 |
| m17>dmr | porjai02@-20 | -19.9 | 1321 / -24.6 (-4.7) / 303/300 / ok env 0.99 | 2049 / -20.9 (-1.0) / 303/300 / ok env 0.99 | 2049 / -20.9 (-1.0) / 303/300 / ok env 0.99 |
| m17>dstar | porjai02@-20 | -19.9 | 4331 / -14.6 (+5.2) / 301/300 / ok env 0.99 | 2181 / -20.4 (-0.5) / 301/300 / ok env 0.99 | 2181 / -20.4 (-0.5) / 301/300 / ok env 0.99 |
| m17>ysf | porjai02@-20 | -19.9 | 866 / -27.8 (-7.9) / 305/300 / ok env 0.98 | 1425 / -23.8 (-3.9) / 305/300 / ok env 0.99 | 1425 / -23.8 (-3.9) / 305/300 / ok env 0.99 |
| m17>dmr | porjai02@-14 | -14.0 | 2670 / -18.7 (-4.7) / 303/300 / ok env 0.99 | 4115 / -15.1 (-1.1) / 303/300 / ok env 0.99 | 4115 / -15.1 (-1.1) / 303/300 / ok env 0.99 |
| m17>dstar | porjai02@-14 | -14.0 | 6811 / -10.8 (+3.2) / 301/300 / ok env 0.99 | 4412 / -14.6 (-0.6) / 301/300 / ok env 0.99 | 4412 / -14.6 (-0.6) / 301/300 / ok env 0.99 |
| m17>ysf | porjai02@-14 | -14.0 | 1866 / -21.6 (-7.6) / 305/300 / ok env 0.99 | 2910 / -17.8 (-3.8) / 305/300 / ok env 0.99 | 2910 / -17.8 (-3.8) / 305/300 / ok env 0.99 |
| m17>dmr | porjai03@-26 | -25.8 | 558 / -30.8 (-5.1) / 207/204 / ok env 0.99 | 856 / -27.4 (-1.6) / 207/204 / ok env 0.99 | 856 / -27.4 (-1.6) / 207/204 / ok env 0.99 |
| m17>dstar | porjai03@-26 | -25.8 | 1601 / -22.1 (+3.6) / 205/204 / ok env 0.99 | 814 / -27.7 (-1.9) / 205/204 / ok env 0.99 | 814 / -27.7 (-1.9) / 205/204 / ok env 0.99 |
| m17>ysf | porjai03@-26 | -25.8 | 367 / -33.1 (-7.3) / 205/204 / ok env 0.98 | 588 / -30.0 (-4.2) / 205/204 / ok env 0.99 | 588 / -30.0 (-4.2) / 205/204 / ok env 0.99 |
| m17>dmr | porjai03@-20 | -19.8 | 1104 / -25.2 (-5.4) / 207/204 / ok env 0.99 | 1710 / -21.5 (-1.7) / 207/204 / ok env 0.99 | 1710 / -21.5 (-1.7) / 207/204 / ok env 0.99 |
| m17>dstar | porjai03@-20 | -19.8 | 3263 / -16.1 (+3.8) / 205/204 / ok env 0.99 | 1660 / -21.8 (-1.9) / 205/204 / ok env 0.99 | 1660 / -21.8 (-1.9) / 205/204 / ok env 0.99 |
| m17>ysf | porjai03@-20 | -19.8 | 779 / -27.6 (-7.8) / 205/204 / ok env 0.99 | 1194 / -24.4 (-4.6) / 205/204 / ok env 0.99 | 1194 / -24.4 (-4.6) / 205/204 / ok env 0.99 |
| m17>dmr | porjai03@-14 | -14.1 | 1812 / -21.0 (-7.0) / 207/204 / ok env 0.99 | 2779 / -17.4 (-3.4) / 207/204 / ok env 0.99 | 2779 / -17.4 (-3.4) / 207/204 / ok env 0.99 |
| m17>dstar | porjai03@-14 | -14.1 | 4626 / -13.1 (+0.9) / 205/204 / ok env 1.00 | 2792 / -17.4 (-3.4) / 205/204 / ok env 1.00 | 2792 / -17.4 (-3.4) / 205/204 / ok env 1.00 |
| m17>ysf | porjai03@-14 | -14.1 | 1293 / -23.7 (-9.7) / 205/204 / ok env 0.99 | 1956 / -20.3 (-6.2) / 205/204 / ok env 0.99 | 1956 / -20.3 (-6.2) / 205/204 / ok env 0.99 |
| dmr>dstar | tone | -15.3 | 18367 / -5.0 (+10.3) / 150/150 / ok 690 Hz | 5865 / -14.9 (+0.3) / 150/150 / ok 690 Hz | 5865 / -14.9 (+0.3) / 150/150 / ok 690 Hz |
| dmr>m17 | tone | -15.3 | 16709 / -5.8 (+9.5) / 152/150 / ok 690 Hz | 5678 / -15.2 (+0.1) / 152/150 / ok 690 Hz | 5678 / -15.2 (+0.1) / 152/150 / ok 690 Hz |
| dmr>ysf | tone | -15.3 | 5145 / -16.1 (-0.8) / 150/150 / ok 690 Hz | 5145 / -16.1 (-0.8) / 150/150 / ok 690 Hz | 5145 / -16.1 (-0.8) / 150/150 / ok 690 Hz |
| dmr>dstar | thai_fixed@-26 | -26.3 | 3260 / -18.5 (+7.8) / 300/300 / ok env 0.98 | 1242 / -26.4 (-0.1) / 300/300 / ok env 0.99 | 1242 / -26.4 (-0.1) / 300/300 / ok env 0.99 |
| dmr>m17 | thai_fixed@-26 | -26.3 | 3994 / -16.9 (+9.4) / 302/300 / ok env 0.98 | 1679 / -23.9 (+2.4) / 302/300 / ok env 0.99 | 1679 / -23.9 (+2.4) / 302/300 / ok env 0.99 |
| dmr>ysf | thai_fixed@-26 | -26.3 | 874 / -29.2 (-2.9) / 300/300 / ok env 0.99 | 874 / -29.2 (-2.9) / 300/300 / ok env 0.99 | 874 / -29.2 (-2.9) / 300/300 / ok env 0.99 |
| dmr>dstar | thai_fixed@-20 | -20.7 | 4731 / -15.5 (+5.2) / 300/300 / ok env 0.97 | 2418 / -20.9 (-0.2) / 300/300 / ok env 0.98 | 2418 / -20.9 (-0.2) / 300/300 / ok env 0.98 |
| dmr>m17 | thai_fixed@-20 | -20.7 | 4890 / -15.3 (+5.4) / 302/300 / ok env 0.97 | 3069 / -19.0 (+1.7) / 302/300 / ok env 0.99 | 3069 / -19.0 (+1.7) / 302/300 / ok env 0.99 |
| dmr>ysf | thai_fixed@-20 | -20.7 | 1612 / -24.3 (-3.5) / 300/300 / ok env 0.98 | 1612 / -24.3 (-3.5) / 300/300 / ok env 0.98 | 1612 / -24.3 (-3.5) / 300/300 / ok env 0.98 |
| dmr>dstar | thai_fixed@-14 | -15.2 | 6088 / -13.4 (+1.7) / 300/300 / ok env 0.95 | 3564 / -17.8 (-2.6) / 300/300 / ok env 0.97 | 3564 / -17.8 (-2.6) / 300/300 / ok env 0.97 |
| dmr>m17 | thai_fixed@-14 | -15.2 | 5727 / -14.0 (+1.2) / 302/300 / ok env 0.94 | 4245 / -16.5 (-1.3) / 302/300 / ok env 0.98 | 4245 / -16.5 (-1.3) / 302/300 / ok env 0.98 |
| dmr>ysf | thai_fixed@-14 | -15.2 | 2608 / -20.4 (-5.2) / 300/300 / ok env 0.97 | 2608 / -20.4 (-5.2) / 300/300 / ok env 0.97 | 2608 / -20.4 (-5.2) / 300/300 / ok env 0.97 |
| dmr>dstar | porjai02@-26 | -25.6 | 3670 / -16.1 (+9.6) / 300/300 / ok env 1.00 | 1166 / -25.7 (-0.1) / 300/300 / ok env 1.00 | 1166 / -25.7 (-0.1) / 300/300 / ok env 1.00 |
| dmr>m17 | porjai02@-26 | -25.6 | 4762 / -13.9 (+11.8) / 302/300 / ok env 1.00 | 1714 / -22.5 (+3.2) / 302/300 / ok env 0.99 | 1714 / -22.5 (+3.2) / 302/300 / ok env 0.99 |
| dmr>ysf | porjai02@-26 | -25.6 | 792 / -28.5 (-2.9) / 300/300 / ok env 0.99 | 792 / -28.5 (-2.9) / 300/300 / ok env 0.99 | 792 / -28.5 (-2.9) / 300/300 / ok env 0.99 |
| dmr>dstar | porjai02@-20 | -19.9 | 6185 / -11.6 (+8.3) / 300/300 / ok env 1.00 | 2331 / -19.8 (+0.1) / 300/300 / ok env 1.00 | 2331 / -19.8 (+0.1) / 300/300 / ok env 1.00 |
| dmr>m17 | porjai02@-20 | -19.9 | 4572 / -14.3 (+5.6) / 302/300 / ok env 0.99 | 3474 / -16.5 (+3.3) / 302/300 / ok env 0.99 | 3474 / -16.5 (+3.3) / 302/300 / ok env 0.99 |
| dmr>ysf | porjai02@-20 | -19.9 | 1719 / -22.4 (-2.5) / 300/300 / ok env 0.99 | 1719 / -22.4 (-2.5) / 300/300 / ok env 0.99 | 1719 / -22.4 (-2.5) / 300/300 / ok env 0.99 |
| dmr>dstar | porjai02@-14 | -14.0 | 7537 / -10.0 (+4.0) / 300/300 / ok env 0.99 | 4545 / -14.3 (-0.3) / 300/300 / ok env 1.00 | 4545 / -14.3 (-0.3) / 300/300 / ok env 1.00 |
| dmr>m17 | porjai02@-14 | -14.0 | 4357 / -14.8 (-0.8) / 302/300 / ok env 0.98 | 4842 / -13.7 (+0.3) / 302/300 / ok env 0.99 | 4842 / -13.7 (+0.3) / 302/300 / ok env 0.99 |
| dmr>ysf | porjai02@-14 | -14.0 | 3309 / -16.8 (-2.8) / 300/300 / ok env 0.99 | 3309 / -16.8 (-2.8) / 300/300 / ok env 0.99 | 3309 / -16.8 (-2.8) / 300/300 / ok env 0.99 |
| dmr>dstar | porjai03@-26 | -25.8 | 2820 / -17.2 (+8.5) / 204/204 / ok env 0.99 | 890 / -26.9 (-1.1) / 204/204 / ok env 0.99 | 890 / -26.9 (-1.1) / 204/204 / ok env 0.99 |
| dmr>m17 | porjai03@-26 | -25.8 | 3740 / -14.8 (+11.0) / 206/204 / ok env 0.99 | 1312 / -23.7 (+2.1) / 206/204 / ok env 0.99 | 1312 / -23.7 (+2.1) / 206/204 / ok env 0.99 |
| dmr>ysf | porjai03@-26 | -25.8 | 672 / -28.8 (-3.1) / 205/204 / ok env 0.99 | 672 / -28.8 (-3.1) / 205/204 / ok env 0.99 | 672 / -28.8 (-3.1) / 205/204 / ok env 0.99 |
| dmr>dstar | porjai03@-20 | -19.8 | 4359 / -13.5 (+6.4) / 204/204 / ok env 0.99 | 1838 / -20.8 (-1.0) / 204/204 / ok env 0.99 | 1838 / -20.8 (-1.0) / 204/204 / ok env 0.99 |
| dmr>m17 | porjai03@-20 | -19.8 | 4176 / -13.9 (+5.9) / 206/204 / ok env 0.99 | 2600 / -18.0 (+1.9) / 206/204 / ok env 0.99 | 2600 / -18.0 (+1.9) / 206/204 / ok env 0.99 |
| dmr>ysf | porjai03@-20 | -19.8 | 1338 / -23.4 (-3.6) / 205/204 / ok env 0.99 | 1338 / -23.4 (-3.6) / 205/204 / ok env 0.99 | 1338 / -23.4 (-3.6) / 205/204 / ok env 0.99 |
| dmr>dstar | porjai03@-14 | -14.1 | 5039 / -12.3 (+1.8) / 204/204 / ok env 0.99 | 3482 / -15.4 (-1.4) / 204/204 / ok env 0.99 | 3482 / -15.4 (-1.4) / 204/204 / ok env 0.99 |
| dmr>m17 | porjai03@-14 | -14.1 | 4099 / -14.3 (-0.2) / 206/204 / ok env 0.99 | 4052 / -14.1 (-0.0) / 206/204 / ok env 0.99 | 4052 / -14.1 (-0.0) / 206/204 / ok env 0.99 |
| dmr>ysf | porjai03@-14 | -14.1 | 2471 / -18.2 (-4.1) / 205/204 / ok env 0.99 | 2471 / -18.2 (-4.1) / 205/204 / ok env 0.99 | 2471 / -18.2 (-4.1) / 205/204 / ok env 0.99 |
| dstar>dmr | tone | -15.3 | 1775 / -25.3 (-10.0) / 150/150 / ok 690 Hz | 5584 / -15.3 (-0.1) / 150/150 / ok 690 Hz | 5584 / -15.3 (-0.1) / 150/150 / ok 690 Hz |
| dstar>m17 | tone | -15.3 | 1346 / -27.7 (-12.4) / 152/150 / ok 690 Hz | 3962 / -18.3 (-3.0) / 152/150 / ok 690 Hz | 3962 / -18.3 (-3.0) / 152/150 / ok 690 Hz |
| dstar>ysf | tone | -15.3 | 1506 / -26.7 (-11.5) / 150/150 / ok 690 Hz | 4740 / -16.8 (-1.5) / 150/150 / ok 690 Hz | 4740 / -16.8 (-1.5) / 150/150 / ok 690 Hz |
| dstar>dmr | thai_fixed@-26 | -26.3 | 405 / -35.6 (-9.3) / 300/300 / ok env 0.98 | 1256 / -26.4 (-0.1) / 300/300 / ok env 0.98 | 1256 / -26.4 (-0.1) / 300/300 / ok env 0.98 |
| dstar>m17 | thai_fixed@-26 | -26.3 | 427 / -35.1 (-8.8) / 302/300 / ok env 0.97 | 1319 / -26.0 (+0.3) / 302/300 / ok env 0.98 | 1319 / -26.0 (+0.3) / 302/300 / ok env 0.98 |
| dstar>ysf | thai_fixed@-26 | -26.3 | 264 / -38.0 (-11.7) / 300/300 / ok env 0.97 | 871 / -29.2 (-2.9) / 300/300 / ok env 0.98 | 871 / -29.2 (-2.9) / 300/300 / ok env 0.98 |
| dstar>dmr | thai_fixed@-20 | -20.7 | 768 / -30.5 (-9.8) / 300/300 / ok env 0.98 | 2386 / -21.2 (-0.5) / 300/300 / ok env 0.98 | 2386 / -21.2 (-0.5) / 300/300 / ok env 0.98 |
| dstar>m17 | thai_fixed@-20 | -20.7 | 810 / -30.0 (-9.2) / 302/300 / ok env 0.98 | 2484 / -20.8 (-0.0) / 302/300 / ok env 0.98 | 2484 / -20.8 (-0.0) / 302/300 / ok env 0.98 |
| dstar>ysf | thai_fixed@-20 | -20.7 | 522 / -33.1 (-12.4) / 300/300 / ok env 0.98 | 1557 / -24.5 (-3.8) / 300/300 / ok env 0.98 | 1557 / -24.5 (-3.8) / 300/300 / ok env 0.98 |
| dstar>dmr | thai_fixed@-14 | -15.2 | 1181 / -27.1 (-11.9) / 300/300 / ok env 0.97 | 3565 / -18.0 (-2.9) / 300/300 / ok env 0.97 | 3565 / -18.0 (-2.9) / 300/300 / ok env 0.97 |
| dstar>m17 | thai_fixed@-14 | -15.2 | 1248 / -26.6 (-11.4) / 302/300 / ok env 0.97 | 3688 / -17.6 (-2.5) / 302/300 / ok env 0.97 | 3688 / -17.6 (-2.5) / 302/300 / ok env 0.97 |
| dstar>ysf | thai_fixed@-14 | -15.2 | 803 / -30.1 (-15.0) / 300/300 / ok env 0.98 | 2328 / -21.5 (-6.3) / 300/300 / ok env 0.97 | 2328 / -21.5 (-6.3) / 300/300 / ok env 0.97 |
| dstar>dmr | porjai02@-26 | -25.6 | 358 / -35.3 (-9.7) / 300/300 / ok env 0.99 | 1132 / -26.0 (-0.3) / 300/300 / ok env 0.99 | 1132 / -26.0 (-0.3) / 300/300 / ok env 0.99 |
| dstar>m17 | porjai02@-26 | -25.6 | 426 / -34.1 (-8.5) / 302/300 / ok env 0.98 | 1343 / -24.6 (+1.1) / 302/300 / ok env 0.99 | 1343 / -24.6 (+1.1) / 302/300 / ok env 0.99 |
| dstar>ysf | porjai02@-26 | -25.6 | 186 / -40.4 (-14.8) / 300/300 / ok env 0.97 | 697 / -29.6 (-4.0) / 300/300 / ok env 0.99 | 697 / -29.6 (-4.0) / 300/300 / ok env 0.99 |
| dstar>dmr | porjai02@-20 | -19.9 | 693 / -30.1 (-10.2) / 300/300 / ok env 0.99 | 2216 / -20.4 (-0.5) / 300/300 / ok env 0.99 | 2216 / -20.4 (-0.5) / 300/300 / ok env 0.99 |
| dstar>m17 | porjai02@-20 | -19.9 | 868 / -28.2 (-8.4) / 302/300 / ok env 0.98 | 2745 / -18.5 (+1.4) / 302/300 / ok env 0.99 | 2745 / -18.5 (+1.4) / 302/300 / ok env 0.99 |
| dstar>ysf | porjai02@-20 | -19.9 | 404 / -34.1 (-14.3) / 300/300 / ok env 0.98 | 1456 / -23.8 (-3.9) / 300/300 / ok env 0.99 | 1456 / -23.8 (-3.9) / 300/300 / ok env 0.99 |
| dstar>dmr | porjai02@-14 | -14.0 | 1331 / -24.7 (-10.7) / 300/300 / ok env 0.99 | 4241 / -14.9 (-0.9) / 300/300 / ok env 0.99 | 4241 / -14.9 (-0.9) / 300/300 / ok env 0.99 |
| dstar>m17 | porjai02@-14 | -14.0 | 1702 / -22.6 (-8.6) / 302/300 / ok env 0.99 | 4610 / -14.1 (-0.1) / 302/300 / ok env 0.99 | 4610 / -14.1 (-0.1) / 302/300 / ok env 0.99 |
| dstar>ysf | porjai02@-14 | -14.0 | 844 / -28.3 (-14.3) / 300/300 / ok env 0.98 | 2877 / -18.1 (-4.1) / 300/300 / ok env 0.99 | 2877 / -18.1 (-4.1) / 300/300 / ok env 0.99 |
| dstar>dmr | porjai03@-26 | -25.8 | 300 / -35.5 (-9.7) / 204/204 / ok env 0.99 | 949 / -26.4 (-0.6) / 204/204 / ok env 0.99 | 949 / -26.4 (-0.6) / 204/204 / ok env 0.99 |
| dstar>m17 | porjai03@-26 | -25.8 | 315 / -35.1 (-9.4) / 206/204 / ok env 0.98 | 1009 / -25.8 (-0.1) / 206/204 / ok env 0.99 | 1009 / -25.8 (-0.1) / 206/204 / ok env 0.99 |
| dstar>ysf | porjai03@-26 | -25.8 | 185 / -37.8 (-12.0) / 205/204 / ok env 0.96 | 664 / -28.8 (-3.0) / 205/204 / ok env 0.99 | 664 / -28.8 (-3.0) / 205/204 / ok env 0.99 |
| dstar>dmr | porjai03@-20 | -19.8 | 581 / -30.5 (-10.7) / 204/204 / ok env 0.99 | 1854 / -20.8 (-1.0) / 204/204 / ok env 0.99 | 1854 / -20.8 (-1.0) / 204/204 / ok env 0.99 |
| dstar>m17 | porjai03@-20 | -19.8 | 642 / -29.6 (-9.8) / 206/204 / ok env 0.98 | 2022 / -20.0 (-0.2) / 206/204 / ok env 0.99 | 2022 / -20.0 (-0.2) / 206/204 / ok env 0.99 |
| dstar>ysf | porjai03@-20 | -19.8 | 395 / -33.0 (-13.1) / 205/204 / ok env 0.98 | 1258 / -24.1 (-4.2) / 205/204 / ok env 0.99 | 1258 / -24.1 (-4.2) / 205/204 / ok env 0.99 |
| dstar>dmr | porjai03@-14 | -14.1 | 1133 / -25.0 (-10.9) / 204/204 / ok env 0.99 | 3626 / -15.2 (-1.2) / 204/204 / ok env 0.99 | 3626 / -15.2 (-1.2) / 204/204 / ok env 0.99 |
| dstar>m17 | porjai03@-14 | -14.1 | 1209 / -24.4 (-10.4) / 206/204 / ok env 0.99 | 3511 / -15.5 (-1.4) / 206/204 / ok env 0.99 | 3511 / -15.5 (-1.4) / 206/204 / ok env 0.99 |
| dstar>ysf | porjai03@-14 | -14.1 | 794 / -27.6 (-13.5) / 205/204 / ok env 0.99 | 2361 / -18.7 (-4.6) / 205/204 / ok env 0.99 | 2361 / -18.7 (-4.6) / 205/204 / ok env 0.99 |

