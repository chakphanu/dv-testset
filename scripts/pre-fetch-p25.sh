#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch g4klx/P25Clients (GPL-2.0) into .cache/p25clients/ at
# P25CLIENTS_REPO @ P25CLIENTS_COMMIT (.env.example / .env).
# rig/Dockerfile.p25reflector builds P25Reflector from it; nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${P25CLIENTS_REPO:-}"
pin="${P25CLIENTS_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-p25: P25CLIENTS_REPO and a 40-hex P25CLIENTS_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/p25clients"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-p25: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/P25Reflector/P25Reflector.ini" ] || { echo "pre-fetch-p25: $dest/P25Reflector missing" >&2; exit 1; }
echo "pre-fetch-p25: $dest = $got"
