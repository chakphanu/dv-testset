#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch n7tae/urfd (GPL-3.0) into .cache/urfd/ at
# URFD_REPO @ URFD_COMMIT (.env.example / .env).
# rig/Dockerfile.urfd builds urfd from it; nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${URFD_REPO:-}"
pin="${URFD_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-urfd: URFD_REPO and a 40-hex URFD_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/urfd"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-urfd: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/reflector/Makefile" ] || { echo "pre-fetch-urfd: $dest/reflector missing" >&2; exit 1; }
echo "pre-fetch-urfd: $dest = $got"
