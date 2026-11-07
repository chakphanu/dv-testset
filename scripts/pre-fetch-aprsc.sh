#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch hessu/aprsc (BSD-3-Clause) into .cache/aprsc/ at APRSC_REPO @
# APRSC_COMMIT (.env.example / .env). rig/Dockerfile.aprsc builds the server from it as
# the PRIVATE APRS-IS server of the rig (docs/dp15-cases.md, section DP15-5a / 5b); nothing is
# committed. The only internet use is this one-time source fetch.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

url="${APRSC_REPO:-}"
pin="${APRSC_COMMIT:-}"
if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  echo "pre-fetch-aprsc: APRSC_REPO and a 40-hex APRSC_COMMIT are required (see .env.example)" >&2
  exit 2
fi

dest=".cache/aprsc"
mkdir -p "$dest"
[ -d "$dest/.git" ] || git -C "$dest" init -q
git -C "$dest" fetch -q --depth 1 "$url" "$pin"
git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD

got="$(git -C "$dest" rev-parse HEAD)"
if [ "$got" != "$pin" ]; then
  echo "pre-fetch-aprsc: $dest is at $got, expected pin $pin" >&2
  exit 1
fi
[ -f "$dest/src/configure" ] && [ -f "$dest/src/aprsc.c" ] || { echo "pre-fetch-aprsc: $dest/src/configure or $dest/src/aprsc.c missing" >&2; exit 1; }
echo "pre-fetch-aprsc: $dest = $got"
