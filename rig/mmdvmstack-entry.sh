#!/bin/bash
# SPDX-License-Identifier: MIT
# Entry point of the real client stack image (rig/Dockerfile.mmdvmstack): the Pi-Star hotspot software in ONE
# container, driven by a VIRTUAL MMDVM modem that the tester runs over UDP ([Modem] Protocol=udp).
#
#   mmdvmstack-entry.sh [run]     render the templates, start everything, run until SIGTERM / SIGINT
#   mmdvmstack-entry.sh render    only render the templates into $MMDVM_RUN and print the file names
#
# Templates: /mmdvm/tmpl/*.tmpl (rig/config/mmdvmstack/, copied into the image; a read-only bind mount over the
# directory overrides them). Every @NAME@ in a template is replaced from the environment; an unreplaced
# placeholder, a missing required variable or an odd value stops the container (exit 2) before anything starts.
# The rendered files go to /tmp/mmdvm (read-only root file system, tmpfs /tmp).
#
# Placeholders. Set by compose (required):
#   @MODEM_ADDR@       tester address where the virtual modem listens (UDP), an IPv4 literal
#   @MODEM_PORT@       virtual modem UDP port (compose default 3334)
#   @LOCAL_ADDR@       this container's own address on the rig network (172.30.0.100), MMDVMHost binds it
#   @LOCAL_PORT@       MMDVMHost UDP port the virtual modem sends to (compose default 3335)
#   @REFLECTOR_IP@     the reflector under test (172.30.0.11); the only host any file points at
#   @REFLECTOR_NAME@   its name for the logs and the YSF host description (XLX999)
#   @CALLSIGN@         hotspot callsign (N0CALL)
#   @DMR_ID@           DMR id of the hotspot, also the Homebrew login id and the P25 id (1023007)
#   @MODULE_DSTAR@     reflector module DStarGateway links to at start (DExtra, "XRF<num> <module>")
#   @MODULE_DMR@       reflector module DMRGateway links to at start (XLX link, private call to 4001..4026)
#   @MODULE_YSF@       reflector module the YSF side expects (the YSF link has no module: shown in the host
#                      description only; the module is picked by the DG-ID of the frames)
#   @P25_TG@           P25 talkgroup P25Gateway links to at start (static TG, 31672)
#   @NXDN_TG@          NXDN talkgroup NXDNGateway links to at start (static TG, 31672)
# Optional (defaults in this script):
#   @DUPLEX@           0 = simplex hotspot (default), 1 = duplex
#   @RF_MODE_HANG@     seconds MMDVMHost keeps one mode after RF activity (default 10)
#   @NET_MODE_HANG@    seconds MMDVMHost keeps one mode after network activity (default 3)
#   @LOG_LEVEL@        display level of every program: 0 none, 1 debug, 2 message, 3 info, 4 warning, 5 error
#                      (default 2)
# Derived by this script (do not set):
#   @REFLECTOR_NUM@    the digits of @REFLECTOR_NAME@ (999): XLX hosts file id, DMRGateway Startup, DExtra name XRF999
#   @DSTARGW_LOG_LEVEL@  @LOG_LEVEL@ in the words DStarGateway wants (debug, info, warning, error, fatal, none)
#
# Programs (each under a supervisor loop that restarts it 5 s after it exits; MMDVMHost exits when the modem
# does not answer, so it is retried for ever; every line of output is prefixed "[Name] "; stdout only):
#   mosquitto      MQTT broker on 127.0.0.1:1883: P25Gateway and NXDNGateway refuse to start without one
#   DMRGateway, YSFGateway, P25Gateway, NXDNGateway, DStarGateway, MMDVMHost
# Start order: broker, then the five gateways, then MMDVMHost. The gateways come first so their loopback
# sockets exist and their startup links to the reflector are already going when the modem answers; MMDVMHost
# opens the modem (about 2 s wait + up to 6 version tries) before it opens its network sockets.
# Stop: SIGTERM or SIGINT is forwarded as SIGTERM to MMDVMHost and the gateways, which are awaited (up to 8 s)
# so they send their disconnect packets; the broker goes last.
set -u

RUN="${MMDVM_RUN:-/tmp/mmdvm}"
TMPL="${MMDVM_TMPL:-/mmdvm/tmpl}"
DMRID_SRC="${MMDVM_DMRID:-/mmdvm/dmrid.dat}"
BIN="${MMDVM_BIN:-/usr/local/bin}"
RESTART_DELAY=5
STOP_WAIT=8

die() { echo "mmdvmstack: $*" >&2; exit 2; }

# ---- the values -------------------------------------------------------------------------------------------
REQUIRED=(MODEM_ADDR MODEM_PORT LOCAL_ADDR LOCAL_PORT REFLECTOR_IP REFLECTOR_NAME CALLSIGN DMR_ID
          MODULE_DSTAR MODULE_DMR MODULE_YSF P25_TG NXDN_TG)
: "${DUPLEX:=0}"
: "${RF_MODE_HANG:=10}"
: "${NET_MODE_HANG:=3}"
: "${LOG_LEVEL:=2}"
for v in "${REQUIRED[@]}"; do
  [ -n "${!v:-}" ] || die "$v is required"
done
for v in "${REQUIRED[@]}" DUPLEX RF_MODE_HANG NET_MODE_HANG LOG_LEVEL; do
  [[ "${!v}" =~ ^[A-Za-z0-9._-]+$ ]] || die "$v has characters outside A-Z a-z 0-9 . _ -"
done
for v in MODEM_ADDR LOCAL_ADDR REFLECTOR_IP; do
  [[ "${!v}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "$v must be an IPv4 address"
done
for v in MODEM_PORT LOCAL_PORT P25_TG NXDN_TG; do
  [[ "${!v}" =~ ^[0-9]{1,5}$ ]] && [ "${!v}" -ge 1 ] && [ "${!v}" -le 65535 ] || die "$v must be 1..65535"
done
[[ "$DMR_ID" =~ ^[0-9]{1,9}$ ]] || die "DMR_ID must be a number"
for v in MODULE_DSTAR MODULE_DMR MODULE_YSF; do
  [[ "${!v}" =~ ^[A-Z]$ ]] || die "$v must be one capital letter A-Z"
done
[[ "$DUPLEX" =~ ^[01]$ ]] || die "DUPLEX must be 0 or 1"
[[ "$RF_MODE_HANG" =~ ^[0-9]{1,4}$ && "$NET_MODE_HANG" =~ ^[0-9]{1,4}$ ]] || die "the mode hang times are seconds"
[[ "$LOG_LEVEL" =~ ^[0-6]$ ]] || die "LOG_LEVEL must be 0..6"
REFLECTOR_NUM="${REFLECTOR_NAME//[!0-9]/}"
[[ "$REFLECTOR_NUM" =~ ^[0-9]{3}$ ]] || die "REFLECTOR_NAME must hold a three digit number (XLX999)"
case "$LOG_LEVEL" in
  0) DSTARGW_LOG_LEVEL=none ;;
  1) DSTARGW_LOG_LEVEL=debug ;;
  2|3) DSTARGW_LOG_LEVEL=info ;;
  4) DSTARGW_LOG_LEVEL=warning ;;
  5) DSTARGW_LOG_LEVEL=error ;;
  *) DSTARGW_LOG_LEVEL=fatal ;;
esac

NAMES=("${REQUIRED[@]}" DUPLEX RF_MODE_HANG NET_MODE_HANG LOG_LEVEL REFLECTOR_NUM DSTARGW_LOG_LEVEL)

# ---- render -----------------------------------------------------------------------------------------------
shopt -u patsub_replacement 2>/dev/null || true

render_all() {
  local t out content n found
  mkdir -p "$RUN" "$RUN/custom" "$RUN/dgwlog" "$RUN/fifo" || die "cannot create $RUN"
  shopt -s nullglob
  local files=("$TMPL"/*.tmpl)
  shopt -u nullglob
  [ "${#files[@]}" -gt 0 ] || die "no templates in $TMPL"
  for t in "${files[@]}"; do
    out="$RUN/$(basename "$t" .tmpl)"
    content="$(<"$t")" || die "cannot read $t"
    for n in "${NAMES[@]}"; do
      content="${content//"@${n}@"/"${!n}"}"
    done
    found="$(printf '%s\n' "$content" | grep -nE '@[A-Z][A-Z0-9_]*@' || true)"
    [ -z "$found" ] || die "unreplaced placeholder in $t: $found"
    printf '%s\n' "$content" > "$out" || die "cannot write $out"
    echo "$out"
  done

  # The id lookup file: MMDVMHost, P25Gateway and NXDNGateway read "id<TAB>callsign<TAB>name" lines. The
  # synthetic rig file (compose mounts it read-only) has the same three fields separated by ';'.
  if [ -r "$DMRID_SRC" ]; then
    tr ';' '\t' < "$DMRID_SRC" > "$RUN/DMRIds.dat" || die "cannot write $RUN/DMRIds.dat"
  else
    : > "$RUN/DMRIds.dat"
  fi
  printf '%s\t%s\t%s\n' "$DMR_ID" "$CALLSIGN" "dvt" >> "$RUN/DMRIds.dat"
  echo "$RUN/DMRIds.dat"

  cat > "$RUN/mosquitto.conf" <<CONF || die "cannot write $RUN/mosquitto.conf"
listener 1883 127.0.0.1
allow_anonymous true
persistence false
log_dest stderr
log_type error
log_type warning
CONF
  echo "$RUN/mosquitto.conf"
}

case "${1:-run}" in
  render) render_all; exit 0 ;;
  run) ;;
  *) die "usage: [run|render]" ;;
esac

for b in MMDVM-Host DMRGateway YSFGateway P25Gateway NXDNGateway dstargateway; do
  [ -x "$BIN/$b" ] || die "$BIN/$b is not installed"
done
command -v mosquitto >/dev/null 2>&1 || die "mosquitto is not installed"

render_all > /dev/null
cd "$RUN" || die "cannot cd to $RUN"

# ---- supervisors ------------------------------------------------------------------------------------------
SUP_PIDS=()      # supervisors of MMDVMHost and the gateways
BROKER_PID=""
PREFIX_PIDS=()
STOPPING=0

prefix() {   # $1 = name; stdin -> stdout with "[name] " in front of every line
  local name="$1" line
  while IFS= read -r line || [ -n "$line" ]; do
    printf '[%s] %s\n' "$name" "$line"
  done
}

supervise() {   # $1 = name, rest = the command; restarts it until told to stop
  local name="$1" child="" stop=0 rc
  shift
  trap 'stop=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT
  while [ "$stop" = 0 ]; do
    echo "supervisor: starting $*"
    "$@" &
    child=$!
    wait "$child"
    rc=$?
    # a trapped signal ends wait early: wait again until the program is really gone
    while kill -0 "$child" 2>/dev/null; do
      wait "$child"
      rc=$?
    done
    child=""
    [ "$stop" = 0 ] || break
    echo "supervisor: $name exited with status $rc, restarting in ${RESTART_DELAY} s"
    sleep "$RESTART_DELAY" &
    child=$!
    wait "$child"
    child=""
  done
  echo "supervisor: $name stopped"
}

start() {   # $1 = name, rest = the command; sets LAST_PID to the supervisor
  local name="$1" fifo
  shift
  fifo="$RUN/fifo/$name"
  rm -f "$fifo"
  mkfifo "$fifo" || die "mkfifo $fifo"
  prefix "$name" < "$fifo" &
  PREFIX_PIDS+=($!)
  supervise "$name" "$@" > "$fifo" 2>&1 &
  LAST_PID=$!
}

alive() { kill -0 "$1" 2>/dev/null; }

stop_all() {
  local deadline p
  [ "$STOPPING" = 0 ] || return 0
  STOPPING=1
  echo "mmdvmstack: stopping"
  for p in "${SUP_PIDS[@]}"; do kill -TERM "$p" 2>/dev/null; done
  deadline=$((SECONDS + STOP_WAIT))
  for p in "${SUP_PIDS[@]}"; do
    while alive "$p" && [ "$SECONDS" -lt "$deadline" ]; do sleep 0.2; done
    if alive "$p"; then kill -KILL "$p" 2>/dev/null; fi
  done
  if [ -n "$BROKER_PID" ]; then
    kill -TERM "$BROKER_PID" 2>/dev/null
    deadline=$((SECONDS + 3))
    while alive "$BROKER_PID" && [ "$SECONDS" -lt "$deadline" ]; do sleep 0.2; done
    if alive "$BROKER_PID"; then kill -KILL "$BROKER_PID" 2>/dev/null; fi
  fi
  # let the prefixers print what is left
  deadline=$((SECONDS + 3))
  for p in "${PREFIX_PIDS[@]}"; do
    while alive "$p" && [ "$SECONDS" -lt "$deadline" ]; do sleep 0.1; done
  done
}

trap 'STOPPING_REQUEST=1' TERM INT
STOPPING_REQUEST=0

# ---- start: broker, gateways, MMDVMHost -------------------------------------------------------------------
start mosquitto mosquitto -c "$RUN/mosquitto.conf"
BROKER_PID=$LAST_PID
up=0
for _ in $(seq 1 50); do
  if (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null; then up=1; break; fi
  sleep 0.2
done
[ "$up" = 1 ] || { echo "mmdvmstack: the MQTT broker did not come up" >&2; STOPPING_REQUEST=1; }

if [ "$STOPPING_REQUEST" = 0 ]; then
  start DMRGateway "$BIN/DMRGateway" "$RUN/DMRGateway.ini";       SUP_PIDS+=("$LAST_PID")
  start YSFGateway "$BIN/YSFGateway" "$RUN/YSFGateway.ini";       SUP_PIDS+=("$LAST_PID")
  start P25Gateway "$BIN/P25Gateway" "$RUN/P25Gateway.ini";       SUP_PIDS+=("$LAST_PID")
  start NXDNGateway "$BIN/NXDNGateway" "$RUN/NXDNGateway.ini";    SUP_PIDS+=("$LAST_PID")
  start DStarGateway "$BIN/dstargateway" "$RUN/DStarGateway.cfg"; SUP_PIDS+=("$LAST_PID")
  sleep 1
  start MMDVMHost "$BIN/MMDVM-Host" "$RUN/MMDVM-Host.ini";        SUP_PIDS+=("$LAST_PID")
  echo "mmdvmstack: started, virtual modem at ${MODEM_ADDR}:${MODEM_PORT}, reflector ${REFLECTOR_NAME} at ${REFLECTOR_IP}"
fi

# ---- run until told to stop ---------------------------------------------------------------------------------
while [ "$STOPPING_REQUEST" = 0 ]; do
  wait
  [ "$STOPPING_REQUEST" = 0 ] || break
  sleep 1
done
stop_all
exit 0
