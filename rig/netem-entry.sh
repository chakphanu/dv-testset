#!/bin/sh
# SPDX-License-Identifier: MIT
# netem sidecar entry. "dvt-netem" applies DVT_NETEM_ARGS (empty = no
# impairment) to eth0 egress and stays up; "dvt-netem set ARGS..." changes it
# (run.sh control channel); "dvt-netem show" prints the qdisc with counters.
set -eu

apply() {
  if [ "$#" -eq 0 ]; then
    tc qdisc del dev eth0 root 2>/dev/null || true
  else
    # shellcheck disable=SC2068
    tc qdisc replace dev eth0 root netem $@
  fi
  tc -s qdisc show dev eth0
}

case "${1:-}" in
  set) shift; apply "$@" ;;
  show) tc -s qdisc show dev eth0 ;;
  '')
    # shellcheck disable=SC2086
    apply ${DVT_NETEM_ARGS:-}
    exec sleep infinity
    ;;
  *) echo "usage: dvt-netem [set ARGS...|show]" >&2; exit 2 ;;
esac
