#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build xlxd for the netns cases (cases/test_xlxd_usrp_netns.py): foreground
# (no RUN_AS_DAEMON), config/XML paths relative to the working directory,
# local id databases instead of HTTP. Sources are the .cache/xlxd/<dir> trees
# of scripts/pre-fetch-xlxd.sh; the build goes to .cache/netns/<dir>/.
#
#   scripts/build-xlxd-netns.sh [DIR...]     default: $XLXD_REF_DIR $XLXD_SUT_DIR
#
# Prints one "DIR BINARY" line per build, e.g. for
#   DVT_XLXD_BIN=.cache/netns/sut/src/xlxd DVT_XLXD_REF_BIN=.cache/netns/ref/src/xlxd
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

[ "$#" -gt 0 ] || set -- "${XLXD_REF_DIR:-ref}" "${XLXD_SUT_DIR:-sut}"
for name in "$@"; do
  case "$name" in
    ''|*/*|.*) echo "build-xlxd-netns: invalid name '$name'" >&2; exit 2 ;;
  esac
  src=".cache/xlxd/$name"
  dest=".cache/netns/$name"
  [ -d "$src/.git" ] || { echo "build-xlxd-netns: $src missing; run scripts/pre-fetch-xlxd.sh" >&2; exit 2; }
  mkdir -p "$dest"
  git -C "$src" archive HEAD | tar -x -C "$dest"
  sed -i 's|^#define RUN_AS_DAEMON|//#define RUN_AS_DAEMON|; s|"/xlxd/|"./|g; s|"/var/log/|"./|g; s|_USE_RLX_SERVER\( *\)1|_USE_RLX_SERVER\10|' \
    "$dest/src/main.h"
  make -C "$dest/src" -j"$(nproc)" > "$dest/build.log" 2>&1 \
    || { echo "build-xlxd-netns: make failed for $name, see $dest/build.log" >&2; exit 1; }
  echo "$name $dest/src/xlxd"
done
