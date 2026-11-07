#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# DVT_XLXD_BIN wrapper for the netns cases: start a DV-Router build from the
# xlxd files in the working directory, the way a DV-Router operator migrates
# from xlxd: convert once, then run the product on the TOML.
#
#   DVT_DVR_BIN_DIR=.cache/dvr-<image id> DVT_XLXD_BIN=scripts/dvr-import-wrap.sh ...
#
# The netns harness runs "<bin> CALLSIGN OWN_IP TRANSCODER_IP" in a working
# directory holding xlxd.usrp and the other xlxd files. This wrapper then
#
#   1. converts that directory with the standalone converter (one-shot mode):
#        <dir>/dv-tool-xlxd-import --dir "$PWD" --out "$PWD/dv-router.toml" CALLSIGN OWN_IP TRANSCODER_IP
#      (dv-xlxd-import in builds before the rename; conversion notes go to stderr),
#   2. execs the product on that file (the TOML carries the endpoints, the
#      three words only repeat the callsign / bind IP / transcoder as overrides):
#        DV_CONFIG_DIR="$PWD" DV_RUN_DIR="$PWD" <dir>/dv-router --config "$PWD/dv-router.toml" CALLSIGN OWN_IP TRANSCODER_IP
#
# No importer hand-off and no file poller: the harness writes the xlxd files
# before the start and never changes them. DV_CONFIG_DIR keeps the local id
# seeds readable from the xlxd directory, DV_RUN_DIR keeps the run state in
# the working directory (explicit wins over /run/dv-router, /dev/shm/dv-router
# and $XDG_RUNTIME_DIR/dv-router; the netns scenarios run in parallel and must
# not share it).
#
# Which path was taken is printed on stderr ("dvr-import-wrap: path=..."),
# so it lands in the scenario's xlxd.log:
#   convert   the converter lists --out in its --help (one-shot mode): steps 1 and 2
#   shim      older build: the converter only has the --exec shim
#             (<dir>/dv-xlxd-import --exec --dir "$PWD" ... -- <dir>/dv-router)
#   direct    older build without any converter: dv-router reads the xlxd
#             files itself (<dir>/dv-router CALLSIGN OWN_IP TRANSCODER_IP)
#
# Getting the binaries: DVT_DVR_BIN_DIR is a host directory with dv-router,
# the converter and the children (dv-edge-*, dv-room, ...) copied out of the
# image under test (the image has /usr/local/bin only, no shell):
#   id=$(docker create dv-router:<tag>) && docker cp "$id":/usr/local/bin/. .cache/dvr-<tag> && docker rm "$id"
# (dv-xlxd-import in the image is a relative symlink; docker cp keeps it).
set -euo pipefail

dir="${DVT_DVR_BIN_DIR:?set DVT_DVR_BIN_DIR to the directory with the converter and dv-router}"
# the harness changes directory first: a relative dir is taken from the repository root
case "$dir" in /*) ;; *) dir="$(cd "$(dirname "$0")/.." && pwd)/$dir" ;; esac
[ "$#" -eq 3 ] || { echo "usage: $0 CALLSIGN OWN_IP TRANSCODER_IP" >&2; exit 2; }
router="$dir/dv-router"
[ -x "$router" ] || { echo "dvr-import-wrap: $router is not executable" >&2; exit 2; }

# the converter is dv-tool-xlxd-import in newer images (dv-xlxd-import is then a symlink to it)
conv="$dir/dv-tool-xlxd-import"
[ -x "$conv" ] || conv="$dir/dv-xlxd-import"

export DV_RUN_DIR="$PWD"

help=""
[ ! -x "$conv" ] || help="$("$conv" --help 2>&1 || true)"
if [ -x "$conv" ] && printf '%s\n' "$help" | grep -q -e '--out'; then
  toml="$PWD/dv-router.toml"
  echo "dvr-import-wrap: path=convert: $conv --dir $PWD --out $toml $*" >&2
  "$conv" --dir "$PWD" --out "$toml" "$@"
  echo "dvr-import-wrap: path=convert: $router --config $toml $*" >&2
  DV_CONFIG_DIR="$PWD" exec "$router" --config "$toml" "$@"
elif [ -x "$conv" ]; then
  echo "dvr-import-wrap: path=shim (no one-shot --out in $conv): $conv --exec --dir $PWD $* -- $router" >&2
  exec "$conv" --exec --dir "$PWD" "$@" -- "$router"
else
  echo "dvr-import-wrap: path=direct (no converter in $dir): $router $*" >&2
  DV_CONFIG_DIR="$PWD" exec "$router" "$@"
fi
