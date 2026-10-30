#!/bin/sh
# SPDX-License-Identifier: MIT
# URFd container entry: render urfd.ini for this rig, check it with URFd's
# own inicheck, run urfd in the foreground (log on stdout).
#   env DVT_IP          own address (required)
#   env DVT_NXDN_TG     NXDN reflector id (default 31672)
#   env DVT_P25_TG      P25 reflector id (default 31672)
#   env DVT_BLACKLIST   callsigns for urfd.blacklist (space separated)
#   env DVT_START_DELAY seconds to wait first (sniffers attach; default 0)
set -eu

: "${DVT_IP:?DVT_IP is required}"
sed -e "s/@IP@/${DVT_IP}/" -e "s/@NXDN_TG@/${DVT_NXDN_TG:-31672}/" -e "s/@P25_TG@/${DVT_P25_TG:-31672}/" \
    -e "s/@YSF_MODULE@/${DVT_YSF_MODULE:-B}/" /urfd/urfd.ini.in > /tmp/urfd.ini
: > /urfd/urfd.blacklist
# shellcheck disable=SC2086
[ -z "${DVT_BLACKLIST:-}" ] || printf '%s\n' $DVT_BLACKLIST > /urfd/urfd.blacklist
/urfd/inicheck -q /tmp/urfd.ini || { echo "dvt-urfd: urfd.ini rejected by inicheck" >&2; exit 2; }
sleep "${DVT_START_DELAY:-0}"
exec /urfd/urfd /tmp/urfd.ini
