#!/bin/sh
# SPDX-License-Identifier: MIT
# xlxd container entry: optional USRP endpoint file, then xlxd in the
# foreground (built without RUN_AS_DAEMON, so its log goes to docker logs).
#   env DVT_CALLSIGN     reflector callsign (default XLX999)
#   env DVT_IP           own address (required)
#   env DVT_TRANSCODER   transcoder address (required)
#   env DVT_USRP         "on" renders /xlxd/xlxd.usrp from the template
#   env DVT_NET          /24 prefix substituted for @NET@ in the template
#   env DVT_START_DELAY  seconds to wait before xlxd starts (sniffers attach
#                        to this network namespace first; default 0)
set -eu

: "${DVT_IP:?DVT_IP is required}"
: "${DVT_TRANSCODER:?DVT_TRANSCODER is required}"

if [ "${DVT_USRP:-off}" = on ]; then
  : "${DVT_NET:?DVT_NET is required with DVT_USRP=on}"
  sed "s/@NET@/${DVT_NET}/g" /xlxd/xlxd.usrp.in > /xlxd/xlxd.usrp
  echo "dvt: USRP endpoints:"
  grep -v '^#' /xlxd/xlxd.usrp
else
  : > /xlxd/xlxd.usrp
fi

sleep "${DVT_START_DELAY:-0}"
exec /xlxd/xlxd "${DVT_CALLSIGN:-XLX999}" "$DVT_IP" "$DVT_TRANSCODER"
