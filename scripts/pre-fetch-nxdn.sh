#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch g4klx/NXDNClients (GPL-2.0) into .cache/nxdnclients/ at
# NXDNCLIENTS_REPO @ NXDNCLIENTS_COMMIT (.env.example / .env).
# rig/Dockerfile.nxdnreflector builds NXDNReflector from it; nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${NXDNCLIENTS_REPO:-}"
pin="${NXDNCLIENTS_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-nxdn: NXDNCLIENTS_REPO and a 40-hex NXDNCLIENTS_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/nxdnclients"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-nxdn: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/NXDNReflector/NXDNReflector.ini" ] || { echo "pre-fetch-nxdn: $dest/NXDNReflector missing" >&2; exit 1; }
echo "pre-fetch-nxdn: $dest = $got"
