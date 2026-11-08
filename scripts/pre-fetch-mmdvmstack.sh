#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch the real client stack (GPL-2.0 family programs, run as processes in a separate image, never
# copied into this repository): g4klx/MMDVMHost, g4klx/DMRGateway, g4klx/YSFClients, g4klx/P25Clients,
# g4klx/NXDNClients and F4FXL/DStarGateway, each at MMDVMSTACK_*_REPO @ MMDVMSTACK_*_COMMIT
# (.env.example / .env), into .cache/mmdvmstack/<MMDVMHost|DMRGateway|YSFClients|P25Clients|NXDNClients|DStarGateway>.
# rig/Dockerfile.mmdvmstack builds them (one named build context per directory); nothing is committed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

fetch_one() {   # $1 = label, $2 = url, $3 = pin, $4 = dest, $5 = a file that must exist
  local label="$1" url="$2" pin="$3" dest="$4" marker="$5"
  if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
    echo "pre-fetch-mmdvmstack: MMDVMSTACK_${label}_REPO and a 40-hex MMDVMSTACK_${label}_COMMIT are required (see .env.example)" >&2
    exit 2
  fi
  mkdir -p "$dest"
  [ -d "$dest/.git" ] || git -C "$dest" init -q
  git -C "$dest" fetch -q --depth 1 "$url" "$pin"
  git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD
  local got
  got="$(git -C "$dest" rev-parse HEAD)"
  if [ "$got" != "$pin" ]; then
    echo "pre-fetch-mmdvmstack: $dest is at $got, expected pin $pin" >&2
    exit 1
  fi
  [ -f "$dest/$marker" ] || { echo "pre-fetch-mmdvmstack: $dest/$marker missing" >&2; exit 1; }
  echo "pre-fetch-mmdvmstack: $dest = $got"
}

fetch_one MMDVMHOST "${MMDVMSTACK_MMDVMHOST_REPO:-}" "${MMDVMSTACK_MMDVMHOST_COMMIT:-}" \
  .cache/mmdvmstack/MMDVMHost MMDVM-Host.cpp
fetch_one DMRGW "${MMDVMSTACK_DMRGW_REPO:-}" "${MMDVMSTACK_DMRGW_COMMIT:-}" \
  .cache/mmdvmstack/DMRGateway DMRGateway.cpp
fetch_one YSFCLIENTS "${MMDVMSTACK_YSFCLIENTS_REPO:-}" "${MMDVMSTACK_YSFCLIENTS_COMMIT:-}" \
  .cache/mmdvmstack/YSFClients YSFGateway/YSFGateway.cpp
fetch_one P25CLIENTS "${MMDVMSTACK_P25CLIENTS_REPO:-}" "${MMDVMSTACK_P25CLIENTS_COMMIT:-}" \
  .cache/mmdvmstack/P25Clients P25Gateway/P25Gateway.cpp
fetch_one NXDNCLIENTS "${MMDVMSTACK_NXDNCLIENTS_REPO:-}" "${MMDVMSTACK_NXDNCLIENTS_COMMIT:-}" \
  .cache/mmdvmstack/NXDNClients NXDNGateway/NXDNGateway.cpp
fetch_one DSTARGW "${MMDVMSTACK_DSTARGW_REPO:-}" "${MMDVMSTACK_DSTARGW_COMMIT:-}" \
  .cache/mmdvmstack/DStarGateway DStarGateway/DStarGatewayApp.cpp
