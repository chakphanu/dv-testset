#!/bin/sh
# SPDX-License-Identifier: MIT
# netem sidecar entry. "dvt-netem" applies DVT_NETEM_ARGS (empty = no
# impairment) to eth0 egress and stays up; "dvt-netem set ARGS..." changes it
# (run.sh control channel); "dvt-netem show" prints the qdisc with counters.
# DVT_NETEM_ALL=1 (the WAN router of NODE1g, which has two interfaces): every
# interface except lo gets the same qdisc, so a datagram is delayed once in
# each direction.
set -eu

devices() {
  if [ "${DVT_NETEM_ALL:-}" = 1 ]; then
    ip -o link show | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -v '^lo$'
  else
    echo eth0
  fi
}

apply() {
  for dev in $(devices); do
    if [ "$#" -eq 0 ]; then
      tc qdisc del dev "$dev" root 2>/dev/null || true
    else
      # shellcheck disable=SC2068
      tc qdisc replace dev "$dev" root netem $@
    fi
    tc -s qdisc show dev "$dev"
  done
}

show() {
  for dev in $(devices); do
    tc -s qdisc show dev "$dev"
  done
}

case "${1:-}" in
  set) shift; apply "$@" ;;
  show) show ;;
  '')
    # shellcheck disable=SC2086
    apply ${DVT_NETEM_ARGS:-}
    exec sleep infinity
    ;;
  *) echo "usage: dvt-netem [set ARGS...|show]" >&2; exit 2 ;;
esac
