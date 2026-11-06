#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch the two D-STAR slow data decoders (GPL-2.0-or-later files, used as test oracles only):
# g4klx/MMDVMHost into .cache/dstarsd-mmdvm and F4FXL/DStarGateway into .cache/dstarsd-dgw,
# each at DSTARSD_*_REPO @ DSTARSD_*_COMMIT (.env.example / .env). rig/Dockerfile.dstarsd
# builds the two drivers from them (docs/dstar-slowdata-cases.md); nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

fetch_one() {   # $1 = label, $2 = url, $3 = pin, $4 = dest, $5 = a file that must exist
  local label="$1" url="$2" pin="$3" dest="$4" marker="$5"
  if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
    echo "pre-fetch-dstarsd: DSTARSD_${label}_REPO and a 40-hex DSTARSD_${label}_COMMIT are required (see .env.example)" >&2
    exit 2
  fi
  mkdir -p "$dest"
  [ -d "$dest/.git" ] || git -C "$dest" init -q
  git -C "$dest" fetch -q --depth 1 "$url" "$pin"
  git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD
  local got
  got="$(git -C "$dest" rev-parse HEAD)"
  if [ "$got" != "$pin" ]; then
    echo "pre-fetch-dstarsd: $dest is at $got, expected pin $pin" >&2
    exit 1
  fi
  [ -f "$dest/$marker" ] || { echo "pre-fetch-dstarsd: $dest/$marker missing" >&2; exit 1; }
  echo "pre-fetch-dstarsd: $dest = $got"
}

fetch_one MMDVM "${DSTARSD_MMDVM_REPO:-}" "${DSTARSD_MMDVM_COMMIT:-}" .cache/dstarsd-mmdvm DStarSlowData.cpp
fetch_one DGW "${DSTARSD_DGW_REPO:-}" "${DSTARSD_DGW_COMMIT:-}" .cache/dstarsd-dgw Common/TextCollector.cpp
