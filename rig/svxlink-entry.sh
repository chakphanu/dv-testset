#!/bin/sh
# SPDX-License-Identifier: MIT
# SvxLink container entry (profile svx). DRAFT, not validated.
# Renders the config template of this role from the environment into a
# private directory and runs svxreflector or svxlink in the foreground (log on
# stdout). Every double-percent token must be replaced; an unreplaced token or an
# empty required value stops the container (exit 2) so that no default
# password can ever be used.
#   env DVT_SVX_ROLE         reflector | node (required)
#   env DVT_SVX_DIR          render directory (default /tmp/svx, a tmpfs)
#   env DVT_SVX_RENDER_ONLY  1 = write the file and stop (unit tests)
#   role reflector:  DVT_SVX_IP, DVT_SVX_CALLSIGN_A, DVT_SVX_CALLSIGN_B,
#                    DVT_SVX_KEY_A, DVT_SVX_KEY_B (generated per run),
#                    DVT_SVX_CODEC (default OPUS), DVT_SVX_SQL_TIMEOUT (0),
#                    DVT_SVX_SQL_BLOCKTIME (60)
#   role node:       DVT_SVX_CALLSIGN, DVT_SVX_KEY, DVT_SVX_REFLECTOR_HOST,
#                    DVT_SVX_TESTER_IP, DVT_SVX_RX_PORT, DVT_SVX_TX_PORT,
#                    DVT_SVX_LOGIC_TYPE (Reflector | ReflectorV2, default
#                    Reflector), DVT_SVX_DEFAULT_TG (0), DVT_SVX_MONITOR_TGS
#                    (empty), DVT_SVX_TG_SELECT_TIMEOUT (30),
#                    DVT_SVX_CERT_EMAIL (node@example.org)
# Values are limited to letters, digits and . _ : , + @ - (they go through
# sed); the generated passwords are hex.
set -eu

die() { echo "dvt-svx: $*" >&2; exit 2; }

role="${DVT_SVX_ROLE:-}"
dir="${DVT_SVX_DIR:-/tmp/svx}"
tpl_dir="${DVT_SVX_TEMPLATES:-/svx/config}"

need() {
  eval "v=\${$1:-}"
  [ -n "$v" ] || die "$1 is required"
  printf '%s' "$v" | grep -Eq '^[A-Za-z0-9._:,+@-]+$' || die "$1 has a character outside [A-Za-z0-9._:,+@-]"
}

opt() {  # opt NAME DEFAULT : default when unset or empty, then the charset check
  eval "v=\${$1:-}"
  if [ -z "$v" ]; then eval "$1=\"\$2\""; v="$2"; fi
  printf '%s' "$v" | grep -Eq '^[A-Za-z0-9._:,+@-]+$' || die "$1 has a character outside [A-Za-z0-9._:,+@-]"
}

mkdir -p "$dir"
chmod 700 "$dir"
umask 077

case "$role" in
  reflector)
    for n in DVT_SVX_IP DVT_SVX_CALLSIGN_A DVT_SVX_CALLSIGN_B DVT_SVX_KEY_A DVT_SVX_KEY_B; do need "$n"; done
    opt DVT_SVX_CODEC OPUS
    opt DVT_SVX_SQL_TIMEOUT 0
    opt DVT_SVX_SQL_BLOCKTIME 60
    out="$dir/svxreflector.conf"
    sed -e "s|%%CODEC%%|$DVT_SVX_CODEC|g" \
        -e "s|%%SQL_TIMEOUT%%|$DVT_SVX_SQL_TIMEOUT|g" \
        -e "s|%%SQL_BLOCKTIME%%|$DVT_SVX_SQL_BLOCKTIME|g" \
        -e "s|%%REFLECTOR_IP%%|$DVT_SVX_IP|g" \
        -e "s|%%CALLSIGN_A%%|$DVT_SVX_CALLSIGN_A|g" \
        -e "s|%%CALLSIGN_B%%|$DVT_SVX_CALLSIGN_B|g" \
        -e "s|%%KEY_A%%|$DVT_SVX_KEY_A|g" \
        -e "s|%%KEY_B%%|$DVT_SVX_KEY_B|g" \
        "$tpl_dir/svxreflector.conf.in" > "$out"
    cmd="svxreflector --config=$out"
    ;;
  node)
    for n in DVT_SVX_CALLSIGN DVT_SVX_KEY DVT_SVX_REFLECTOR_HOST DVT_SVX_TESTER_IP DVT_SVX_RX_PORT DVT_SVX_TX_PORT; do need "$n"; done
    opt DVT_SVX_LOGIC_TYPE Reflector
    opt DVT_SVX_DEFAULT_TG 0
    opt DVT_SVX_TG_SELECT_TIMEOUT 30
    opt DVT_SVX_CERT_EMAIL node@example.org
    # MONITOR_TGS may stay empty: replaced by nothing
    mon="${DVT_SVX_MONITOR_TGS:-}"
    [ -z "$mon" ] || printf '%s' "$mon" | grep -Eq '^[0-9+,]+$' || die "DVT_SVX_MONITOR_TGS must be digits, plus and comma"
    case "$DVT_SVX_LOGIC_TYPE" in Reflector|ReflectorV2) ;; *) die "DVT_SVX_LOGIC_TYPE must be Reflector or ReflectorV2" ;; esac
    out="$dir/svxlink.conf"
    sed -e "s|%%LOGIC_TYPE%%|$DVT_SVX_LOGIC_TYPE|g" \
        -e "s|%%CALLSIGN%%|$DVT_SVX_CALLSIGN|g" \
        -e "s|%%KEY%%|$DVT_SVX_KEY|g" \
        -e "s|%%REFLECTOR_HOST%%|$DVT_SVX_REFLECTOR_HOST|g" \
        -e "s|%%TESTER_IP%%|$DVT_SVX_TESTER_IP|g" \
        -e "s|%%RX_PORT%%|$DVT_SVX_RX_PORT|g" \
        -e "s|%%TX_PORT%%|$DVT_SVX_TX_PORT|g" \
        -e "s|%%DEFAULT_TG%%|$DVT_SVX_DEFAULT_TG|g" \
        -e "s|%%MONITOR_TGS%%|$mon|g" \
        -e "s|%%TG_SELECT_TIMEOUT%%|$DVT_SVX_TG_SELECT_TIMEOUT|g" \
        -e "s|%%CERT_EMAIL%%|$DVT_SVX_CERT_EMAIL|g" \
        "$tpl_dir/svxlink-node.conf.in" > "$out"
    cmd="svxlink --config=$out"
    ;;
  *) die "DVT_SVX_ROLE must be reflector or node" ;;
esac

if grep -q '%%[A-Z_]*%%' "$out"; then
  rm -f "$out"
  die "unreplaced token in the rendered configuration of role $role"
fi

[ "${DVT_SVX_RENDER_ONLY:-0}" != 1 ] || { echo "dvt-svx: rendered $out"; exit 0; }

# Foreground, log on stdout. Neither program is started as a daemon.
exec $cmd
