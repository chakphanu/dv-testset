#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch n7tae/mrefd (GPL-3.0) into .cache/mrefd/ at
# MREFD_REPO @ MREFD_COMMIT (.env.example / .env).
# rig/Dockerfile.mrefd builds mrefd from it; nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${MREFD_REPO:-}"
pin="${MREFD_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-mrefd: MREFD_REPO and a 40-hex MREFD_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/mrefd"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-mrefd: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/Makefile" ] || { echo "pre-fetch-mrefd: $dest/Makefile missing" >&2; exit 1; }
echo "pre-fetch-mrefd: $dest = $got"
