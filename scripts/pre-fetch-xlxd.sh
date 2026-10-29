#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch xlxd sources into .cache/xlxd/<dir>/ (gitignored, never committed).
#
#   scripts/pre-fetch-xlxd.sh                      reference and SUT trees
#   scripts/pre-fetch-xlxd.sh <name> <url> <ref>   any repo/branch/commit
#
# Without arguments the pins come from .env.example / .env:
#   XLXD_REF_REPO @ XLXD_REF_COMMIT          -> .cache/xlxd/$XLXD_REF_DIR
#   XLXD_SUT_REPO @ XLXD_SUT_REF (or the ref) -> .cache/xlxd/$XLXD_SUT_DIR
# A ref that is a full 40-hex commit must match the checkout, else it fails.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

fetch() {   # $1 = dir name, $2 = repo url, $3 = ref
  local name="$1" url="$2" ref="$3" dest got
  case "$name" in
    ''|*/*|.*) echo "pre-fetch-xlxd: invalid name '$name' (one path segment, no leading dot)" >&2; exit 2 ;;
  esac
  if [ -z "$url" ] || [ -z "$ref" ]; then
    echo "pre-fetch-xlxd: repo url and ref are required for '$name' (see .env.example)" >&2
    exit 2
  fi
  dest=".cache/xlxd/$name"
  mkdir -p "$dest"
  [ -d "$dest/.git" ] || git -C "$dest" init -q
  # A shallow fetch of exactly one ref; an existing checkout is updated in place.
  git -C "$dest" fetch -q --depth 1 "$url" "$ref"
  git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD
  got="$(git -C "$dest" rev-parse HEAD)"
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]] && [ "$got" != "$ref" ]; then
    echo "pre-fetch-xlxd: $dest is at $got, expected pin $ref" >&2
    exit 1
  fi
  [ -f "$dest/src/main.h" ] || { echo "pre-fetch-xlxd: $dest/src/main.h missing" >&2; exit 1; }
  printf '%s %s %s\n' "$url" "$ref" "$got" > ".cache/xlxd/$name.fetched"
  echo "pre-fetch-xlxd: $dest = $got ($ref)"
}

if [ "$#" -eq 3 ]; then
  fetch "$1" "$2" "$3"
elif [ "$#" -eq 0 ]; then
  fetch "${XLXD_REF_DIR:-ref}" "${XLXD_REF_REPO:-}" "${XLXD_REF_COMMIT:-}"
  fetch "${XLXD_SUT_DIR:-sut}" "${XLXD_SUT_REPO:-${XLXD_REF_REPO:-}}" "${XLXD_SUT_REF:-${XLXD_REF_COMMIT:-}}"
else
  echo "usage: $0 [<name> <repo-url> <ref>]" >&2
  exit 2
fi
