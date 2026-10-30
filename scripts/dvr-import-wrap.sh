#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# DVT_XLXD_BIN wrapper for the netns cases: start DV-Router through its
# converter shim, the way the rig does with DVT_SUT_LAUNCH=import.
#
#   DVT_DVR_BIN_DIR=.cache/dvr-<image id> DVT_XLXD_BIN=scripts/dvr-import-wrap.sh ...
#
# The netns harness runs "<bin> CALLSIGN OWN_IP TRANSCODER_IP" in a working
# directory holding xlxd.usrp and the other xlxd files, so the shim reads
# that directory (--dir "$PWD"), writes the converted config there and execs
# dv-router with the same words.
set -euo pipefail

dir="${DVT_DVR_BIN_DIR:?set DVT_DVR_BIN_DIR to the directory with dv-xlxd-import and dv-router}"
# the harness changes directory first: a relative dir is taken from the repository root
case "$dir" in /*) ;; *) dir="$(cd "$(dirname "$0")/.." && pwd)/$dir" ;; esac
[ "$#" -eq 3 ] || { echo "usage: $0 CALLSIGN OWN_IP TRANSCODER_IP" >&2; exit 2; }
# the converted config and the run state go to DV_RUN_DIR (explicit wins over
# /run/dv-router, /dev/shm/dv-router and $XDG_RUNTIME_DIR/dv-router): point it
# at the working directory, the netns scenarios run in parallel and must not
# share it
# the converter is dv-tool-xlxd-import in newer images (dv-xlxd-import is then a symlink to it)
shim="$dir/dv-tool-xlxd-import"
[ -x "$shim" ] || shim="$dir/dv-xlxd-import"
DV_RUN_DIR="$PWD" exec "$shim" --exec --dir "$PWD" "$@" -- "$dir/dv-router"
