#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch the speech corpus for cases/test_speech_quality.py: CMU ARCTIC
# voices bdl and slt (CMU free licence, see NOTICE) from SPEECH_*_URL,
# verified against SPEECH_*_SHA256 (.env.example), into .cache/speech/.
# Then writes the 20 utterances listed in dvtest/audio/speech.py (IDS) per
# voice as 8 kHz S16LE raw files to .cache/speech/8k/<voice>/ with a
# SHA256SUMS file. Nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

dest=".cache/speech"
for voice in bdl slt; do
  key="SPEECH_${voice^^}"
  url="$(printenv "${key}_URL" || true)"
  sha="$(printenv "${key}_SHA256" || true)"
  if [ -z "$url" ] || ! [[ "$sha" =~ ^[0-9a-f]{64}$ ]]; then
    echo "pre-fetch-speech: ${key}_URL and a 64-hex ${key}_SHA256 are required (see .env.example)" >&2
    exit 2
  fi
  tarball="$dest/cmu_us_${voice}_arctic.tar.bz2"
  fetch_verified "$url" "$sha" "$tarball"
  python3 -m dvtest.audio.speech prepare "$tarball" "$voice" "$dest/8k"
done
