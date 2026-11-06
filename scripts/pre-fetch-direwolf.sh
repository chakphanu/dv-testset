#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch wb2osz/direwolf (GPL-2.0-or-later) into .cache/direwolf/ at DIREWOLF_REPO @
# DIREWOLF_COMMIT (.env.example / .env). rig/Dockerfile.direwolf builds the Dire Wolf
# tools from it as the independent AX.25 / APRS / KISS reference (docs/direwolf-cases.md);
# nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${DIREWOLF_REPO:-}"
pin="${DIREWOLF_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-direwolf: DIREWOLF_REPO and a 40-hex DIREWOLF_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/direwolf"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-direwolf: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/CMakeLists.txt" ] && [ -f "$dest/src/gen_packets.c" ] || { echo "pre-fetch-direwolf: $dest/src/gen_packets.c missing" >&2; exit 1; }
echo "pre-fetch-direwolf: $dest = $got"
