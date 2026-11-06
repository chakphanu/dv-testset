#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fetch the M17 v3 test sources (P26j, docs/m17v3-cases.md) into
# .cache/m17-src/<name>/ at the pins of .env.example / .env:
#   M17_spec            M17_SPEC_REF (dev, 3.0.0 draft) and M17_SPEC_V2_REF (main, 2.0.10)
#   m17-fme             M17_FME_REPO @ M17_FME_REF       (GPL-3.0, verifier oracle image)
#   micro-ecc           MICRO_ECC_REPO @ MICRO_ECC_REF   (BSD-2-Clause, m17-fme dependency)
#   M17_Implementations M17_IMPL_REPO @ M17_IMPL_REF     (GPL-2.0, optional second oracle)
#   libm17              LIBM17_REPO @ LIBM17_REF         (GPL-2.0-or-later, GNSS/ECD/nonce reference)
# rig/Dockerfile.m17fme builds m17-fme from the first two; the rest is read.
# GPL sources are test references only: nothing is copied into dv-testset.
# Same git pattern as the other pre-fetch scripts (shallow fetch of the exact
# commit, then a check of HEAD against the pin); no pin lives in this file.
# Usage: scripts/pre-fetch-m17.sh [name ...]   (default: all)
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

# fetch_pinned NAME REPO REF [EXTRA_REF ...]: .cache/m17-src/NAME at REF (HEAD),
# EXTRA_REFs fetched as objects only (readable with git show REF:path)
fetch_pinned() {
  local name="$1" url="$2" pin="$3" dest ref got
  shift 3
  if [ -z "$url" ] || ! [[ "$pin" =~ ^[0-9a-f]{40}$ ]]; then
    echo "pre-fetch-m17: repo URL and a 40-hex pin for $name are required (see .env.example)" >&2
    exit 2
  fi
  dest=".cache/m17-src/$name"
  mkdir -p "$dest"
  [ -d "$dest/.git" ] || git -C "$dest" init -q
  for ref in "$@"; do
    [[ "$ref" =~ ^[0-9a-f]{40}$ ]] || { echo "pre-fetch-m17: bad extra pin for $name" >&2; exit 2; }
    git -C "$dest" fetch -q --depth 1 "$url" "$ref"
  done
  git -C "$dest" fetch -q --depth 1 "$url" "$pin"
  git -C "$dest" -c advice.detachedHead=false checkout -q --force --detach FETCH_HEAD
  got="$(git -C "$dest" rev-parse HEAD)"
  if [ "$got" != "$pin" ]; then
    echo "pre-fetch-m17: $dest is at $got, expected pin $pin" >&2
    exit 1
  fi
  echo "pre-fetch-m17: $dest = $got"
}

selected=("$@")
wanted() {
  local n
  [ "${#selected[@]}" -eq 0 ] && return 0
  for n in "${selected[@]}"; do [ "$n" = "$1" ] && return 0; done
  return 1
}

wanted M17_spec            && fetch_pinned M17_spec "${M17_SPEC_REPO:-}" "${M17_SPEC_REF:-}" "${M17_SPEC_V2_REF:-}"
wanted m17-fme             && fetch_pinned m17-fme "${M17_FME_REPO:-}" "${M17_FME_REF:-}"
wanted micro-ecc           && fetch_pinned micro-ecc "${MICRO_ECC_REPO:-}" "${MICRO_ECC_REF:-}"
wanted M17_Implementations && fetch_pinned M17_Implementations "${M17_IMPL_REPO:-}" "${M17_IMPL_REF:-}"
wanted libm17              && fetch_pinned libm17 "${LIBM17_REPO:-}" "${LIBM17_REF:-}"

# the files the Docker build and the checks rely on
if wanted m17-fme; then
  [ -f .cache/m17-src/m17-fme/CMakeLists.txt ] || { echo "pre-fetch-m17: m17-fme/CMakeLists.txt missing" >&2; exit 1; }
fi
if wanted micro-ecc; then
  [ -f .cache/m17-src/micro-ecc/uECC.c ] || { echo "pre-fetch-m17: micro-ecc/uECC.c missing" >&2; exit 1; }
fi
if wanted M17_spec; then
  git -C .cache/m17-src/M17_spec cat-file -e "${M17_SPEC_V2_REF}:M17_spec.tex" \
    || { echo "pre-fetch-m17: 2.0.10 M17_spec.tex not readable" >&2; exit 1; }
fi
