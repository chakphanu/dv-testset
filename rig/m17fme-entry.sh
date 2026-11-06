#!/bin/sh
# SPDX-License-Identifier: MIT
# m17-fme container entry (rig/Dockerfile.m17fme): verify one IP-frame file.
#   entry.sh verify <ip-frame-file> <public-key-file>
#       prints everything m17-fme writes (it writes to stderr) on stdout and
#       returns when the file is read; exit code 0 means m17-fme ran, not that
#       a signature was valid - read the lines " Signature Valid;" /
#       " Signature Invalid;" (dvtest.oracle_m17.parse_fme_output)
#   entry.sh version     the binary's usage banner
# No network is needed; run the container with --network none and the two
# files mounted read-only.
set -eu

die() { echo "dvt-m17fme: $*" >&2; exit 2; }

mode="${1:-}"
case "$mode" in
  verify)
    [ "$#" -eq 3 ] || die "usage: verify <ip-frame-file> <public-key-file>"
    [ -r "$2" ] || die "cannot read $2"
    [ -r "$3" ] || die "cannot read $3"
    exec /usr/local/bin/m17-fme -^ "$2" -k "$3" 2>&1
    ;;
  version)
    exec /usr/local/bin/m17-fme -h 2>&1
    ;;
  *)
    die "usage: verify <ip-frame-file> <public-key-file> | version"
    ;;
esac
