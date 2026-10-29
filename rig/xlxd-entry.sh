#!/bin/sh
# SPDX-License-Identifier: MIT
# xlxd container entry: optional USRP endpoint file, then xlxd in the
# foreground (built without RUN_AS_DAEMON, so its log goes to docker logs).
#   env DVT_CALLSIGN     reflector callsign (default XLX999)
#   env DVT_IP           own address (required)
#   env DVT_TRANSCODER   transcoder address (required)
#   env DVT_USRP         "on" renders /xlxd/xlxd.usrp from the template
#   env DVT_NET          /24 prefix substituted for @NET@ in the template
#   env DVT_NXDN         "on" peers DVT_NXDN_MODULE with the NXDNReflector at
#                        DVT_NXDN_PEER:41400 on talkgroup DVT_NXDN_TG
#                        (xlxd.interlink row "NX<tg> <ip>:<port> <module>")
#   env DVT_P25          "on" peers DVT_P25_MODULE with the P25Reflector at
#                        DVT_P25_PEER:41000 (row "P25<id> <ip>:<port> <module>",
#                        id = DVT_P25_TG)
#   env DVT_INTERLINK    "on" interlinks DVT_INTERLINK_MODULES with the XLX
#                        reflector DVT_INTERLINK_CALLSIGN at DVT_INTERLINK_PEER
#                        (row "<callsign> <ip> <modules>"; both sides list each other)
#   env DVT_START_DELAY  seconds to wait before xlxd starts (sniffers attach
#                        to this network namespace first; default 0)
#
# Other modes:
#   DVT_RENDER_DIR=<dir> only write xlxd.usrp and xlxd.interlink into <dir>
#                        (template DVT_USRP_TEMPLATE) and exit; run.sh uses it
#                        on the host for a prebuilt XLXD_*_IMAGE
#   DVT_CONFIG_MOUNTED=1 the config files are mounted read-only: render
#                        nothing, run xlxd with the arguments given
#                        ("CALLSIGN OWN_IP TRANSCODER_IP")
set -eu

if [ "${DVT_CONFIG_MOUNTED:-0}" = 1 ]; then
  sleep "${DVT_START_DELAY:-0}"
  exec /xlxd/xlxd "$@"
fi

dir="${DVT_RENDER_DIR:-/xlxd}"
if [ -z "${DVT_RENDER_DIR:-}" ]; then
  : "${DVT_IP:?DVT_IP is required}"
  : "${DVT_TRANSCODER:?DVT_TRANSCODER is required}"
fi

if [ "${DVT_USRP:-off}" = on ]; then
  : "${DVT_NET:?DVT_NET is required with DVT_USRP=on}"
  sed "s/@NET@/${DVT_NET}/g" "${DVT_USRP_TEMPLATE:-/xlxd/xlxd.usrp.in}" > "$dir/xlxd.usrp"
  echo "dvt: USRP endpoints:"
  grep -v '^#' "$dir/xlxd.usrp"
else
  : > "$dir/xlxd.usrp"
fi

# Reflector peers (NXDN, P25) are xlxd.interlink rows; xlxd connects out.
: > "$dir/xlxd.interlink"
if [ "${DVT_NXDN:-off}" = on ]; then
  : "${DVT_NXDN_PEER:?}" "${DVT_NXDN_TG:?}" "${DVT_NXDN_MODULE:?}"
  printf 'NX%05d %s:41400 %s\n' "$DVT_NXDN_TG" "$DVT_NXDN_PEER" "$DVT_NXDN_MODULE" >> "$dir/xlxd.interlink"
fi
if [ "${DVT_P25:-off}" = on ]; then
  : "${DVT_P25_PEER:?}" "${DVT_P25_TG:?}" "${DVT_P25_MODULE:?}"
  printf 'P25%05d %s:41000 %s\n' "$DVT_P25_TG" "$DVT_P25_PEER" "$DVT_P25_MODULE" >> "$dir/xlxd.interlink"
fi
if [ "${DVT_INTERLINK:-off}" = on ]; then
  : "${DVT_INTERLINK_PEER:?}" "${DVT_INTERLINK_CALLSIGN:?}" "${DVT_INTERLINK_MODULES:?}"
  printf '%s %s %s\n' "$DVT_INTERLINK_CALLSIGN" "$DVT_INTERLINK_PEER" "$DVT_INTERLINK_MODULES" >> "$dir/xlxd.interlink"
fi
[ ! -s "$dir/xlxd.interlink" ] || { echo "dvt: interlink:"; cat "$dir/xlxd.interlink"; }
[ -z "${DVT_RENDER_DIR:-}" ] || exit 0

sleep "${DVT_START_DELAY:-0}"
exec /xlxd/xlxd "${DVT_CALLSIGN:-XLX999}" "$DVT_IP" "$DVT_TRANSCODER"
