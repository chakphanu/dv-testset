#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch DSDcc (f4exb/dsdcc, GPL-3.0-or-later per its file headers; a test oracle only, run as a
# process) into .cache/dsdcc at DSDCC_REPO @ DSDCC_COMMIT (.env.example / .env) and check the two
# published D-STAR recordings of its samples/ folder against DSDCC_SAMPLE_*_SHA256.
# rig/Dockerfile.dsdcc builds from it (docs/dstar-slowdata-cases.md, stage 2); nothing is committed.
# DSDCC_REPO may be a local clone for an offline fetch; the commit pin is checked either way.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${DSDCC_REPO:-}"
pin="${DSDCC_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-dsdcc: DSDCC_REPO and a 40-hex DSDCC_COMMIT are required (see .env.example)" >&2
  exit 2
fi
dest=.cache/dsdcc
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD
got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-dsdcc: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/dstar.cpp" ] || { echo "pre-fetch-dsdcc: $dest/dstar.cpp missing" >&2; exit 1; }
check_sample() {   # $1 = file, $2 = pinned sha256
  local want="$2" have
  [[ "$want" =~ ^[0-9a-f]{64}$ ]] || { echo "pre-fetch-dsdcc: a 64-hex sample hash is required for $1" >&2; exit 2; }
  have="$(sha256sum "$dest/samples/$1" | cut -d' ' -f1)"
  [ "$have" = "$want" ] || { echo "pre-fetch-dsdcc: $1 has sha256 $have, expected $want" >&2; exit 1; }
}
check_sample dstar_f1zil_1.dis "${DSDCC_SAMPLE_WITH_HEADER_SHA256:-}"
check_sample dstar_f1zil_2.dis "${DSDCC_SAMPLE_ON_THE_FLY_SHA256:-}"
echo "pre-fetch-dsdcc: $dest = $got (samples verified)"
