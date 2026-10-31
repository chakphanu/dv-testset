#!/bin/sh
# SPDX-License-Identifier: MIT
# mrefd container entry: render mrefd.cfg, mrefd.interlink and the black and
# white lists for this rig, run mrefd in the foreground (log on stdout).
# No DHT, dashboard, ID database or network lookup is configured.
#   env DVT_IP                    own address (required)
#   env DVT_MREFD_CALLSIGN        reflector designation (default M17-REF)
#   env DVT_M17_MODULES           enabled modules (default MNOP)
#   env DVT_M17_PEER_IP           address of the configured interlink peers
#                                 (the tester); empty = no peers
#   env DVT_M17_PEER_DVU_HOST     address of peer M17-DVU (default: DVT_M17_PEER_IP).
#                                 mrefd keys peers by IP only, so DVU needs an
#                                 address of its own (rig: tester-addr sidecar)
#   env DVT_M17_PEER_DVT_PORT     peer M17-DVT port (default 17011)
#   env DVT_M17_PEER_DVT_MODULES  peer M17-DVT shared modules (default M)
#   env DVT_M17_PEER_DVU_PORT     peer M17-DVU port (default 17012)
#   env DVT_M17_PEER_DVU_MODULES  peer M17-DVU shared modules (default MN)
#   env DVT_BLACKLIST             callsigns for mrefd.blacklist (space separated)
#   env DVT_START_DELAY           seconds to wait first (sniffers attach; default 0)
#   env DVT_MREFD_DIR             where the files are written (default /tmp/mrefd)
#   env DVT_MREFD_RENDER_ONLY     1 = write the files and stop (unit tests)
set -eu

die() { echo "dvt-mrefd: $*" >&2; exit 2; }

: "${DVT_IP:?DVT_IP is required}"
callsign="${DVT_MREFD_CALLSIGN:-M17-REF}"
modules="${DVT_M17_MODULES:-MNOP}"
peer_ip="${DVT_M17_PEER_IP:-}"
dvt_port="${DVT_M17_PEER_DVT_PORT:-17011}"
dvt_mods="${DVT_M17_PEER_DVT_MODULES:-M}"
dvu_host="${DVT_M17_PEER_DVU_HOST:-$peer_ip}"
dvu_port="${DVT_M17_PEER_DVU_PORT:-17012}"
dvu_mods="${DVT_M17_PEER_DVU_MODULES:-MN}"
dir="${DVT_MREFD_DIR:-/tmp/mrefd}"

case "$callsign" in
  M17-[A-Z0-9][A-Z0-9][A-Z0-9]) ;;
  *) die "DVT_MREFD_CALLSIGN must look like M17-ABC (got '$callsign')" ;;
esac
check_modules() {
  case "$2" in
    ''|*[!A-Z]*) die "$1 must be one or more letters A-Z (got '$2')" ;;
  esac
}
check_port() {
  case "$2" in
    ''|*[!0-9]*) die "$1 must be a port number 1024..49000 (got '$2')" ;;
  esac
  [ "$2" -ge 1024 ] && [ "$2" -le 49000 ] || die "$1 must be a port number 1024..49000 (got '$2')"
}
check_modules DVT_M17_MODULES "$modules"
if [ -n "$peer_ip" ]; then
  case "$peer_ip" in
    *[!0-9.]*) die "DVT_M17_PEER_IP must be an IPv4 address (got '$peer_ip')" ;;
  esac
  case "$dvu_host" in
    *[!0-9.]*) die "DVT_M17_PEER_DVU_HOST must be an IPv4 address (got '$dvu_host')" ;;
  esac
  check_modules DVT_M17_PEER_DVT_MODULES "$dvt_mods"
  check_modules DVT_M17_PEER_DVU_MODULES "$dvu_mods"
  check_port DVT_M17_PEER_DVT_PORT "$dvt_port"
  check_port DVT_M17_PEER_DVU_PORT "$dvu_port"
fi

mkdir -p "$dir"
cat > "$dir/mrefd.cfg" <<CFG
# rendered by rig/mrefd-entry.sh
Callsign = $callsign
Modules = $modules
IPv4BindAddr = $DVT_IP
Port = 17000
PidPath = $dir/mrefd.pid
JsonPath = $dir/mrefd.json
WhitelistPath = $dir/mrefd.whitelist
BlacklistPath = $dir/mrefd.blacklist
InterlinkPath = $dir/mrefd.interlink
CFG
: > "$dir/mrefd.whitelist"
: > "$dir/mrefd.blacklist"
# shellcheck disable=SC2086
[ -z "${DVT_BLACKLIST:-}" ] || printf '%s\n' $DVT_BLACKLIST > "$dir/mrefd.blacklist"
: > "$dir/mrefd.interlink"
if [ -n "$peer_ip" ]; then
  {
    echo "M17-DVT $peer_ip $dvt_port $dvt_mods"
    echo "M17-DVU $dvu_host $dvu_port $dvu_mods"
  } > "$dir/mrefd.interlink"
fi

[ "${DVT_MREFD_RENDER_ONLY:-0}" != 1 ] || exit 0
sleep "${DVT_START_DELAY:-0}"
exec /mrefd/mrefd "$dir/mrefd.cfg"
