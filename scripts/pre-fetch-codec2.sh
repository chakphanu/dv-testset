#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch upstream codec2 (LGPL-2.1) into .cache/codec2/ at CODEC2_REPO @
# CODEC2_COMMIT (.env.example / .env). rig/Dockerfile.tester builds
# c2enc/c2dec from it; nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${CODEC2_REPO:-}"
pin="${CODEC2_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-codec2: CODEC2_REPO and a 40-hex CODEC2_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/codec2"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-codec2: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/CMakeLists.txt" ] || { echo "pre-fetch-codec2: $dest/CMakeLists.txt missing" >&2; exit 1; }
echo "pre-fetch-codec2: $dest = $got"
