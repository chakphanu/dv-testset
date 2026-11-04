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

About 9 minutes a tag (30 overs per source protocol, three source protocols, then the load case).

## Results

(to be filled in by the run)
