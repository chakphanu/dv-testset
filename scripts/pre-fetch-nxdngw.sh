#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch g4klx/NXDNClients (GPL-2.0) into .cache/nxdngw/ at NXDNGW_REPO @ NXDNGW_COMMIT
# (.env.example / .env). rig/Dockerfile.nxdngw builds NXDNGateway from it as the
# Kenwood GPS oracle (docs/nxdn-gps-cases.md); nothing is committed. A separate
# directory from .cache/nxdnclients (that pin is the last commit with NXDNReflector).
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${NXDNGW_REPO:-}"
pin="${NXDNGW_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-nxdngw: NXDNGW_REPO and a 40-hex NXDNGW_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/nxdngw"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-nxdngw: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/NXDNGateway/GPSHandler.cpp" ] || { echo "pre-fetch-nxdngw: $dest/NXDNGateway missing" >&2; exit 1; }
echo "pre-fetch-nxdngw: $dest = $got"
