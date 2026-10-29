#!/bin/sh
# SPDX-License-Identifier: MIT
# NXDNReflector container entry: render the ini for DVT_NXDN_TG, run in the
# foreground (Daemon=0, log on stdout).
set -eu

case "${DVT_NXDN_TG:-}" in
  ''|*[!0-9]*) echo "dvt-nxdn: DVT_NXDN_TG must be a number 1..65535" >&2; exit 2 ;;
esac
sed "s/@TG@/${DVT_NXDN_TG}/" /nxdn/NXDNReflector.ini.in > /tmp/NXDNReflector.ini
exec /nxdn/NXDNReflector /tmp/NXDNReflector.ini
