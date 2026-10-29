#!/bin/sh
# SPDX-License-Identifier: MIT
# AllStar container entry: render the private node config, load chan_usrp,
# keep USB drivers and public registration off, run asterisk in the foreground.
#   env DVT_NET          rig /24 prefix (required)
#   env DVT_IAX_SECRET   IAX2 secret from .local/rig.env (scripts/gen-local.sh)
set -eu

: "${DVT_NET:?DVT_NET is required}"
case "${DVT_IAX_SECRET:-}" in
  ''|*[!0-9a-f]*) echo "dvt-allstar: DVT_IAX_SECRET missing or not hex; run scripts/gen-local.sh" >&2; exit 2 ;;
esac

src=/etc/asterisk/dvt
etc=/etc/asterisk
sed "s/@NET@/${DVT_NET}/g" "$src/rpt.conf.in" > "$etc/rpt.conf"
cp "$src/extensions.conf" "$etc/extensions.conf"
# The secret is written to the config only, never echoed.
sed -e "s/@NET@/${DVT_NET}/g" -e "s/@IAX_SECRET@/${DVT_IAX_SECRET}/" "$src/iax.conf.in" > "$etc/iax.conf"
chmod 640 "$etc/iax.conf"
chown asterisk:asterisk "$etc/rpt.conf" "$etc/extensions.conf" "$etc/iax.conf"

# modules.conf: chan_usrp on; USB radio drivers and node registration off.
sed -i -E \
  -e 's/^(noload|load)( *= *chan_usrp\.so)/require\2/' \
  -e 's/^(load|require)( *= *(chan_simpleusb|res_usbradio|res_rpt_http_registrations)\.so)/noload\2/' \
  "$etc/modules.conf"

mkdir -p /var/run/asterisk
chown asterisk:asterisk /var/run/asterisk
echo "dvt-allstar: $(tr '\n' ' ' < "$etc/dvt-versions.txt")"
exec asterisk -f -n -U asterisk -G asterisk -vvv
