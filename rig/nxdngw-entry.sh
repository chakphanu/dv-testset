#!/bin/bash
# SPDX-License-Identifier: MIT
# NXDNGateway oracle entry (rig/Dockerfile.nxdngw): feed ICOM packets to the
# gateway's local side and print what it publishes on the APRS topic.
#   entry.sh run <packets-file>
#       packets-file: lines "NAME HEX" (HEX = one 102-byte ICOM packet in
#       hex; consecutive lines with the same NAME are one case). Comment
#       lines start with #.
#       stdout: tab separated capture lines "TOPIC<TAB>PAYLOAD", with the
#       markers "dvt/case<TAB>begin NAME" before every case and
#       "dvt/case<TAB>end" at the end; gateway log goes to stderr.
#   entry.sh version     the gateway's version line
# Needs no network: run the container with --network none; everything is on
# 127.0.0.1 (broker 1883, gateway local port 14020, "repeater" 127.0.0.1:14021).
set -eu

die() { echo "dvt-nxdngw: $*" >&2; exit 2; }

mode="${1:-}"
case "$mode" in
  version)
    exec /usr/local/bin/NXDNGateway -v
    ;;
  run) ;;
  *) die "usage: run <packets-file> | version" ;;
esac
[ "$#" -eq 2 ] || die "usage: run <packets-file>"
in="$2"
[ -r "$in" ] || die "cannot read $in"

work="$(mktemp -d /tmp/nxdngw.XXXXXX)"
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

# ---- broker
cat > "$work/mosquitto.conf" <<CONF
listener 1883 127.0.0.1
allow_anonymous true
persistence false
log_dest stderr
CONF
mosquitto -c "$work/mosquitto.conf" >/dev/null 2>"$work/mosquitto.log" &
pids+=($!)
up=0
for _ in $(seq 1 50); do
  if (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null; then up=1; break; fi
  sleep 0.1
done
[ "$up" = 1 ] || die "broker did not start"

# ---- capture: APRS line and the case markers, in broker order
mosquitto_sub -h 127.0.0.1 -t aprs-gateway/aprs -t dvt/case -F '%t\t%p' &
pids+=($!)
sleep 0.3

# ---- gateway config and id file (synthetic: N0CALL, source id 1001)
printf '1001,N0CALL,Test\n' > "$work/NXDN.csv"
cat > "$work/NXDNGateway.ini" <<CONF
[General]
Callsign=N0CALL
Suffix=NXDN
RptProtocol=Icom
RptAddress=127.0.0.1
RptPort=14021
LocalPort=14020
Debug=0
Daemon=0

[Info]
RXFrequency=430475000
TXFrequency=439475000
Power=1
Latitude=0.0
Longitude=0.0
Height=0
Name=Test
Description=Test

[Voice]
Enabled=0

[APRS]
Enable=1
Suffix=N
Description=test

[Id Lookup]
Name=$work/NXDN.csv
Time=0

[Log]
DisplayLevel=1
MQTTLevel=0

[MQTT]
Address=127.0.0.1
Port=1883
Keepalive=60
Auth=0
Name=nxdn-gateway

[Network]
Port=14050
HostsFile1=$work/none.json
HostsFile2=$work/none.txt
ReloadTime=0
ParrotAddress=127.0.0.1
ParrotPort=0
NXDN2DMRAddress=127.0.0.1
NXDN2DMRPort=0
RFHangTime=120
NetHangTime=60
Debug=0

[GPSD]
Enable=0

[Remote Commands]
Enable=0
CONF
/usr/local/bin/NXDNGateway "$work/NXDNGateway.ini" >&2 &
pids+=($!)
sleep 1.5
kill -0 "${pids[2]}" 2>/dev/null || die "gateway exited"

# One datagram per packet. The bytes go through a file and cat (one write of
# the whole packet): printf straight into /dev/udp flushes at every 0x0a byte
# and splits the packet in two datagrams, which the gateway drops.
send_hex() {
  local fmt
  fmt="$(printf '%s' "$1" | sed 's/../\\x&/g')"
  # shellcheck disable=SC2059
  printf "$fmt" > "$work/packet"
  [ "$(stat -c %s "$work/packet")" = "$((${#1} / 2))" ] || die "packet file has the wrong size"
  cat "$work/packet" > /dev/udp/127.0.0.1/14020
}

prev=""
while read -r name hex; do
  case "$name" in ''|'#'*) continue ;; esac
  [[ "$hex" =~ ^([0-9a-fA-F]{2})+$ ]] || die "bad hex for $name"
  if [ "$name" != "$prev" ]; then
    if [ -n "$prev" ]; then sleep 0.3; fi
    mosquitto_pub -h 127.0.0.1 -t dvt/case -m "begin $name"
    prev="$name"
  fi
  send_hex "$hex"
  sleep 0.05
done < "$in"
sleep 0.5
mosquitto_pub -h 127.0.0.1 -t dvt/case -m "end"
sleep 0.3
