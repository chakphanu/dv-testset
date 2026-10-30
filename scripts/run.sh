#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Bring the rig up, run pytest inside the tester, collect logs, bring it down.
#
#   scripts/run.sh [-m MARKERS] [--profile core|allstar|nxdn|p25|interlink|load|<comma list>] [--capture] [--keep] [-- PYTEST_ARGS...]
#   scripts/run.sh --down
#
#   -m MARKERS         pytest marker expression (default: rig)
#   --profile allstar  core plus the AllStar node; sets DVT_SUT_USRP=on
#   --profile nxdn     core plus NXDNReflectors nxdn-ref/nxdn-sut peered with
#                      xlxd-ref/xlxd-sut (DVT_NXDN=on); combine: allstar,nxdn
#   --profile p25      core plus P25Reflectors p25-ref/p25-sut peered with
#                      xlxd-ref/xlxd-sut (DVT_P25=on); e.g. allstar,nxdn,p25
#   --profile interlink  XLX998 peers xlxd-peer-sut/-ref interlinked with
#                      xlxd-sut/-ref on DVT_INTERLINK_MODULES (default DE)
#   --profile load     DVT_LOAD_MODULES (1..26) modules, DVT_LOAD_NODES (0..26,
#                      default = modules) load-node replicas against xlxd-sut,
#                      first round at DVT_LOAD_START_AT (now + 30 s);
#                      DVT_SUT_USRP=on; host samples docker stats every ~1-2 s
#                      into out/<utc>/stats.jsonl while pytest runs
#   --capture          sniffers on xlxd-ref/xlxd-sut/transcoder-sut (+allstar/nxdn/p25): pcapng in
#                      out/<utc>/capture/<service>.pcapng (decode: scripts/tshark.sh)
#   --netem SPEC       impair the tester's egress: a preset (lan, good_internet,
#                      mobile_3g, bad_wifi; .env.example), "off" or netem
#                      arguments ("delay 100ms loss 1%"); DVT_NETEM_XLXD does the
#                      same for xlxd-ref and xlxd-sut alike; counters in netem.txt
#   --keep             leave the rig running (default: compose down at the end)
#   --down             take the lock, bring every profile down, exit (the only
#                      cleanup for a --keep rig)
#
# Parallel rigs: DVT_INSTANCE=<n> (1..99; default 0 = project dvt, net DVT_NET)
# uses project dvt<n>, network <a.b>.<n>.0/24 (first two octets of DVT_NET),
# lock .cache/rig<n>.lock and out/<utc>-i<n>. Images are shared; builds are
# serialized by .cache/build.lock.
#
# One run at a time per instance: the whole up..down span holds an flock on .cache/rig.lock;
# a second caller waits. If project containers are already up (a --keep rig),
# run.sh refuses with exit 3; their owner cleans up with run.sh --down.
#
# Settings: .env.example, then .env, then .local/rig.env (scripts/lib/env.sh);
# TRANSCODER_IMAGE must be set in .env or the shell; image-specific settings go
# into .local/compose.override.yml (COMPOSE_OVERRIDE), added with -f if present.
# Results: out/<utc>/ (pytest junit + log, compose ps/images, one log per service).
#
# Control channel for cases that stop a backend (tester has no docker socket):
# while pytest runs, a host watcher polls out/<utc>/control/ ($DVT_OUT/control
# in the tester, DVT_RIG_CONTROL=1). A request is a file <id>.req (write .tmp,
# then rename) with one line "<action> <service>":
#   action  stop | start | restart | kill (SIGKILL)
#   service transcoder-ref | transcoder-sut | allstar | xlxd-ref | xlxd-sut
# or, with netem on: "netem tester|xlxd <preset|off|netem arguments>".
# or, with the allstar profile: "asl usrp-show" (chan_usrp counters into
# <id>.out) or "asl play-1901|play-1902" (app_rpt plays a short telemetry
# file on that node, i.e. keys its USRP channel toward xlxd-sut).
# "mark begin|end <tag>" only logs: a case brackets deliberately malformed
# traffic so the capture check can tell it from a fault.
# The answer is <id>.ack: "ok", "error <rc>" or "error refused" (anything else
# is refused and nothing runs); the request is renamed to <id>.done.
# control/control.log gets "<utc> request <line>" before the action and
# "<utc> <line> -> <rc>" after it.
#
# With --capture, scripts/dvcheck.sh checks out/<utc>/ after the rig is down
# (out/<utc>/check.txt); its non-zero exit fails the run when pytest passed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

markers="rig"
profile="core"
keep=0
capture=0
netem_set=0
netem_cli=""
down_only=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -m) [ "$#" -ge 2 ] || { echo "run.sh: -m needs a value" >&2; exit 2; }; markers="$2"; shift 2 ;;
    --profile) [ "$#" -ge 2 ] || { echo "run.sh: --profile needs a value" >&2; exit 2; }; profile="$2"; shift 2 ;;
    --keep) keep=1; shift ;;
    --capture) capture=1; shift ;;
    --netem) [ "$#" -ge 2 ] || { echo "run.sh: --netem needs a preset or netem arguments" >&2; exit 2; }; netem_cli="$2"; netem_set=1; shift 2 ;;
    --down) down_only=1; shift ;;
    --) shift; break ;;
    -h|--help) sed -n '3,63p' "$0"; exit 0 ;;
    *) echo "run.sh: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
done

# DVT_SUBNET (a /24), when set, wins over DVT_NET.
if [ -n "${DVT_SUBNET:-}" ]; then
  case "$DVT_SUBNET" in
    *.0/24) export DVT_NET="${DVT_SUBNET%.0/24}" ;;
    *) echo "run.sh: DVT_SUBNET must be a /24 like 172.30.0.0/24" >&2; exit 2 ;;
  esac
fi
instance="${DVT_INSTANCE:-0}"
[[ "$instance" =~ ^([0-9]|[1-9][0-9])$ ]] || { echo "run.sh: DVT_INSTANCE must be 0..99" >&2; exit 2; }
lock=".cache/rig.lock"
suffix=""
if [ "$instance" != 0 ]; then
  export COMPOSE_PROJECT_NAME="dvt$instance"
  net="${DVT_NET:-172.30.0}"
  export DVT_NET="${net%.*}.$instance"
  lock=".cache/rig$instance.lock"
  suffix="-i$instance"
fi
export DVT_SUBNET="${DVT_NET:-172.30.0}.0/24"

# Serialize users of this instance: project and subnet are shared. The lock
# is released when this script exits, after compose down.
take_lock() {
  mkdir -p .cache
  exec 9>> "$lock"
  if ! flock -n 9; then
    echo "run.sh: waiting for rig lock $lock held by pid $(head -n 1 "$lock" 2>/dev/null || echo '?')"
    flock 9
  fi
  printf '%s\n' "$$" > "$lock"
}
# Every profile, so "down" also removes allstar/capture containers
dvt_env_files
compose_files=(-f rig/compose.yml)
if [ -n "${COMPOSE_OVERRIDE:-}" ] && [ -f "$COMPOSE_OVERRIDE" ]; then
  compose_files+=(-f "$COMPOSE_OVERRIDE")
fi
compose_all=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" "${compose_files[@]}" "${DVT_ENV_FILES[@]}"
  --profile core --profile allstar --profile nxdn --profile capture --profile capture-allstar
  --profile capture-nxdn --profile p25 --profile capture-p25 --profile interlink
  --profile capture-interlink --profile load --profile netem)

if [ "$down_only" = 1 ]; then
  take_lock
  "${compose_all[@]}" down --remove-orphans
  exit 0
fi

profiles=(--profile core)
has_allstar=0
has_nxdn=0
has_p25=0
has_interlink=0
has_load=0
IFS=, read -r -a wanted <<< "$profile"
for p in "${wanted[@]}"; do
  case "$p" in
    core) ;;
    allstar)
      has_allstar=1
      profiles+=(--profile allstar)
      export DVT_SUT_USRP="${DVT_SUT_USRP:-on}"
      [ -f .local/rig.env ] || { echo "run.sh: .local/rig.env missing; run scripts/gen-local.sh" >&2; exit 2; }
      ;;
    nxdn)
      has_nxdn=1
      profiles+=(--profile nxdn)
      export DVT_NXDN=on
      [ -f .cache/nxdnclients/NXDNReflector/Makefile ] \
        || { echo "run.sh: .cache/nxdnclients missing; run scripts/pre-fetch-nxdn.sh" >&2; exit 2; }
      ;;
    p25)
      has_p25=1
      profiles+=(--profile p25)
      export DVT_P25=on
      [ -f .cache/p25clients/P25Reflector/Makefile ] \
        || { echo "run.sh: .cache/p25clients missing; run scripts/pre-fetch-p25.sh" >&2; exit 2; }
      ;;
    interlink)
      has_interlink=1
      profiles+=(--profile interlink)
      export DVT_INTERLINK=on
      export XLXD_PEER_DIR="${XLXD_PEER_DIR:-${XLXD_REF_DIR:-ref}}"
      [ -f ".cache/xlxd/$XLXD_PEER_DIR/src/main.h" ] \
        || { echo "run.sh: .cache/xlxd/$XLXD_PEER_DIR missing; run scripts/pre-fetch-xlxd.sh" >&2; exit 2; }
      ;;
    load)
      profiles+=(--profile load)
      export DVT_LOAD=on
      has_load=1
      # module C of xlxd-sut is a USRP endpoint towards the tester
      export DVT_SUT_USRP="${DVT_SUT_USRP:-on}"
      [[ "${DVT_LOAD_NODES:-${DVT_LOAD_MODULES:-26}}" =~ ^([0-9]|1[0-9]|2[0-6])$ ]] \
        || { echo "run.sh: DVT_LOAD_NODES must be 0..26" >&2; exit 2; }
      [[ "${DVT_LOAD_MODULES:-26}" =~ ^([1-9]|1[0-9]|2[0-6])$ ]] \
        || { echo "run.sh: DVT_LOAD_MODULES must be 1..26" >&2; exit 2; }
      # every node starts its first round at the same time, after the rig is up
      export DVT_LOAD_START_AT="${DVT_LOAD_START_AT:-$(( $(date +%s) + ${DVT_LOAD_LEAD_S:-30} ))}"
      ;;
    *) echo "run.sh: --profile takes core, allstar, nxdn, p25, interlink, load or a comma list" >&2; exit 2 ;;
  esac
done
# netem: a preset name (NETEM_PRESET_<NAME>), "off", or netem arguments
netem_args() {
  local spec="$1" var
  case "$spec" in ''|off|none) return 0 ;; esac
  var="NETEM_PRESET_$(printf '%s' "$spec" | tr '[:lower:]' '[:upper:]')"
  if [[ "$spec" =~ ^[a-z0-9_]+$ ]] && [ -n "${!var:-}" ]; then spec="${!var}"; fi
  [[ "$spec" =~ ^[A-Za-z0-9\ .%_-]+$ ]] || { echo "run.sh: invalid netem arguments '$spec'" >&2; return 1; }
  printf '%s' "$spec"
}
netem_svcs=()
[ "$netem_set" = 0 ] || DVT_NETEM_TESTER="$netem_cli"
if [ "$netem_set" = 1 ] || [ -n "${DVT_NETEM_TESTER:-}${DVT_NETEM_XLXD:-}" ]; then
  profiles+=(--profile netem)
  netem_svcs=(netem-tester netem-xlxd-ref netem-xlxd-sut)
  DVT_NETEM_ARGS_TESTER="$(netem_args "${DVT_NETEM_TESTER:-}")" || exit 2
  DVT_NETEM_ARGS_XLXD="$(netem_args "${DVT_NETEM_XLXD:-}")" || exit 2
  export DVT_NETEM_ARGS_TESTER DVT_NETEM_ARGS_XLXD
fi

capture_svcs=()
if [ "$capture" = 1 ]; then
  profiles+=(--profile capture)
  capture_svcs=(capture-xlxd-ref capture-xlxd-sut capture-transcoder-sut)
  # xlxd waits so the sniffers see its first transcoder handshake
  export DVT_START_DELAY="${DVT_START_DELAY:-3}"
  if [ "$has_allstar" = 1 ]; then
    profiles+=(--profile capture-allstar)
    capture_svcs+=(capture-allstar)
  fi
  if [ "$has_nxdn" = 1 ]; then
    profiles+=(--profile capture-nxdn)
    capture_svcs+=(capture-nxdn-ref capture-nxdn-sut)
  fi
  if [ "$has_p25" = 1 ]; then
    profiles+=(--profile capture-p25)
    capture_svcs+=(capture-p25-ref capture-p25-sut)
  fi
  if [ "$has_interlink" = 1 ]; then
    profiles+=(--profile capture-interlink)
    capture_svcs+=(capture-xlxd-peer-sut)
  fi
fi

if [ -z "${TRANSCODER_IMAGE:-}" ]; then
  echo "run.sh: TRANSCODER_IMAGE is not set; set it in .env (TRANSCODER_IMAGE=<your-ambed-image>)" >&2
  exit 2
fi

xlxd_dirs=()
[ -n "${XLXD_REF_IMAGE:-}" ] || xlxd_dirs+=("${XLXD_REF_DIR:-ref}")
[ -n "${XLXD_SUT_IMAGE:-}" ] || xlxd_dirs+=("${XLXD_SUT_DIR:-sut}")
for name in "${xlxd_dirs[@]}"; do
  [ -f ".cache/xlxd/$name/src/main.h" ] || { echo "run.sh: .cache/xlxd/$name missing; run scripts/pre-fetch-xlxd.sh" >&2; exit 2; }
done
[ -f .cache/codec2/CMakeLists.txt ] || { echo "run.sh: .cache/codec2 missing; run scripts/pre-fetch-codec2.sh" >&2; exit 2; }

scripts/pii-check.sh

take_lock
# Containers that are up while we hold the lock belong to a --keep rig (or a
# manual compose up) that someone may still use: never take it over.
if [ -n "$(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" ps -a -q 2>/dev/null)" ]; then
  echo "run.sh: project ${COMPOSE_PROJECT_NAME:-dvt} already has containers (a --keep rig?);" \
       "its owner must clean up first with scripts/run.sh --down" >&2
  exit 3
fi

DVT_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
DVT_UID="$(id -u)"
DVT_GID="$(id -g)"
DVT_RUN_ID="$DVT_RUN_ID$suffix"
export DVT_RUN_ID DVT_UID DVT_GID
out="out/$DVT_RUN_ID"
mkdir -p "$out/logs"
[ "$capture" = 0 ] || mkdir -p "$out/capture"

# Prebuilt reflector images (XLXD_REF_IMAGE / XLXD_SUT_IMAGE): another
# implementation can be the system under test. Its contract: argv
# "CALLSIGN OWN_IP TRANSCODER_IP"; config files mounted read-only under /xlxd/
# (rendered here on the host by rig/xlxd-entry.sh); log on stdout.
prebuilt=()
override="$out/xlxd-image.override.yml"
render_xlxd() {   # $1 service, $2 image, $3 own, $4 transcoder, $5 usrp, $6 nxdn, $7 p25, $8 xlxd peer, $9 xlxd peer2
  local svc="$1" img="$2" net="${DVT_NET:-172.30.0}" cfg="$DVT_ROOT/$out/xlxd-config/$1" f
  mkdir -p "$cfg"
  DVT_RENDER_DIR="$cfg" DVT_USRP_TEMPLATE=rig/config/xlxd.usrp.in DVT_USRP="$5" DVT_NET="$net" \
    DVT_NXDN_PEER="$net.$6" DVT_P25_PEER="$net.$7" DVT_INTERLINK_PEER="$net.$8" \
    DVT_INTERLINK_CALLSIGN=XLX998 DVT_INTERLINK_MODULES="${DVT_INTERLINK_MODULES:-DE}" \
    DVT_INTERLINK2_PEER="$net.$9" DVT_INTERLINK2_CALLSIGN=XLX997 \
    DVT_NXDN_TG="${DVT_NXDN_TG:-31672}" DVT_NXDN_MODULE="${DVT_NXDN_MODULE:-F}" \
    DVT_P25_TG="${DVT_P25_TG:-31672}" DVT_P25_MODULE="${DVT_P25_MODULE:-G}" \
    sh rig/xlxd-entry.sh > "$cfg/render.log"
  cp rig/config/dmrid.dat rig/config/nxdn.csv "$cfg/"
  for f in xlxd.whitelist xlxd.blacklist xlxd.terminal ysfnode.dat; do : > "$cfg/$f"; done
  {
    printf '  %s:\n    image: "%s"\n    command: ["XLX999", "%s.%s", "%s.%s"]\n' "$svc" "$img" "$net" "$3" "$net" "$4"
    printf '    environment:\n      DVT_CONFIG_MOUNTED: "1"\n    volumes:\n'
    for f in xlxd.usrp xlxd.interlink xlxd.whitelist xlxd.blacklist xlxd.terminal dmrid.dat nxdn.csv ysfnode.dat; do
      printf '      - "%s/%s:/xlxd/%s:ro"\n' "$cfg" "$f" "$f"
    done
  } >> "$override"
  prebuilt+=("$svc")
}
if [ -n "${XLXD_REF_IMAGE:-}${XLXD_SUT_IMAGE:-}" ]; then
  echo "services:" > "$override"
  [ -z "${XLXD_REF_IMAGE:-}" ] || render_xlxd xlxd-ref "$XLXD_REF_IMAGE" 10 20 off 50 60 13 15
  [ -z "${XLXD_SUT_IMAGE:-}" ] || render_xlxd xlxd-sut "$XLXD_SUT_IMAGE" 11 21 "${DVT_SUT_USRP:-off}" 51 61 12 14
  compose_files+=(-f "$DVT_ROOT/$override")
fi

compose=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" "${compose_files[@]}" "${DVT_ENV_FILES[@]}" "${profiles[@]}")

watcher_pid=""
stats_pid=""

# Profile load: host-side resource samples for the rig containers (the tester
# has no docker socket): $out/stats.jsonl, one line per service and sample,
# {"t": unix, "service": name, "cpu_pct": float, "mem_bytes": int}.
sample_stats() {
  local project="${COMPOSE_PROJECT_NAME:-dvt}" ids
  while :; do
    ids="$("${compose[@]}" ps -q 2>/dev/null | tr '\n' ' ')"
    if [ -n "${ids// /}" ]; then
      # shellcheck disable=SC2086
      docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' $ids 2>/dev/null \
        | python3 -c '
import json, re, sys, time
units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
project, now = sys.argv[1], time.time()
for row in sys.stdin:
    name, cpu, mem = row.rstrip("\n").split("\t")
    service = re.sub(r"-[0-9]+$", "", name[len(project) + 1:] if name.startswith(project + "-") else name)
    m = re.match(r"([0-9.]+)([A-Za-z]+)", mem.split("/")[0].strip())
    mem_bytes = int(float(m.group(1)) * units.get(m.group(2), 1)) if m else 0
    print(json.dumps({"t": round(now, 3), "service": service, "cpu_pct": float(cpu.rstrip("%") or 0), "mem_bytes": mem_bytes}))
' "$project" >> "$out/stats.jsonl" || true
    fi
    # docker stats --no-stream itself takes about 2 s
    sleep 0.2
  done
}
control="$out/control"

watch_control() {
  local req id line action svc extra rc sniffer args target node
  while :; do
    for req in "$control"/*.req; do
      [ -e "$req" ] || continue
      id="$(basename "$req" .req)"
      line="$(head -n 1 -- "$req")"
      action="" svc="" extra=""
      read -r action svc extra <<< "$line" || true
      printf '%s request %s\n' "$(date -u +%FT%T.%3NZ)" "$line" >> "$control/control.log"
      rc=refused
      if [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = netem ] && [ "${#netem_svcs[@]}" -gt 0 ]; then
        # "netem tester|xlxd <preset|off|arguments>": xlxd = xlxd-ref and xlxd-sut alike
        case "$svc" in tester) target=(netem-tester) ;; xlxd) target=(netem-xlxd-ref netem-xlxd-sut) ;; *) target=() ;; esac
        if [ "${#target[@]}" -gt 0 ] && [ -n "$extra" ] && args="$(netem_args "$extra" 2>/dev/null)"; then
          rc=0
          # counters of the setting being replaced
          netem_snapshot "before netem $svc $extra"
          for t in "${target[@]}"; do
            # shellcheck disable=SC2086
            "${compose[@]}" exec -T "$t" dvt-netem set $args >> "$control/control.log" 2>&1 || rc=$?
          done
          # a restarted xlxd gets a new sidecar with the current setting
          [ "$svc" != xlxd ] || export DVT_NETEM_ARGS_XLXD="$args"
          netem_snapshot "netem $svc $extra"
        fi
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = mark ] && { [ "$svc" = begin ] || [ "$svc" = end ]; } \
           && [[ "$extra" =~ ^[A-Za-z0-9._-]+$ ]]; then
        rc=0
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = asl ] && [ -z "$extra" ]; then
        rc=0
        case "$svc" in
          usrp-show)
            "${compose[@]}" exec -T allstar asterisk -rx "usrp show" > "$control/$id.out" 2>&1 || rc=$? ;;
          play-1901|play-1902)
            # telemetry on only around the play, so no other tones reach xlxd
            node="${svc#play-}"
            { "${compose[@]}" exec -T allstar asterisk -rx "rpt cmd $node cop 33 1" \
                && "${compose[@]}" exec -T allstar asterisk -rx "rpt localplay $node rpt/connected" \
                && sleep 4 \
                && "${compose[@]}" exec -T allstar asterisk -rx "rpt cmd $node cop 34 1"; } \
              > "$control/$id.out" 2>&1 || rc=$? ;;
          *) rc=refused ;;
        esac
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ -z "$extra" ]; then
        case "$svc" in transcoder-ref|transcoder-sut|allstar|xlxd-ref|xlxd-sut) ;; *) action="" ;; esac
        rc=0
        sniffer=""
        [[ " ${capture_svcs[*]} " != *" capture-$svc "* ]] || sniffer="capture-$svc"
        # a sniffer shares the namespace of its service: close its file before
        # the service goes away (restart included)
        if [ -n "$sniffer" ] && [ "$action" != start ]; then
          "${compose[@]}" stop "$sniffer" >> "$control/control.log" 2>&1 || true
        fi
        case "$action" in
          stop)  "${compose[@]}" stop "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          start) "${compose[@]}" start "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          kill)  "${compose[@]}" kill -s SIGKILL "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          restart) "${compose[@]}" restart "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          *)     rc=refused ;;
        esac
        # back up: a new sniffer in the new namespace writes <service>.<n>.pcapng
        if [ -n "$sniffer" ] && { [ "$action" = start ] || [ "$action" = restart ]; } && [ "$rc" = 0 ]; then
          "${compose[@]}" up -d --no-deps --force-recreate "$sniffer" >> "$control/control.log" 2>&1 || true
        fi
        # the qdisc lived in the old namespace: a new netem sidecar applies it again
        if [[ " ${netem_svcs[*]} " == *" netem-$svc "* ]] && { [ "$action" = start ] || [ "$action" = restart ]; } && [ "$rc" = 0 ]; then
          "${compose[@]}" up -d --no-deps --force-recreate "netem-$svc" >> "$control/control.log" 2>&1 || true
        fi
      fi
      printf '%s %s\n' "$(date -u +%FT%T.%3NZ)" "$line -> $rc" >> "$control/control.log"
      if [ "$rc" = 0 ]; then printf 'ok\n'; else printf 'error %s\n' "$rc"; fi > "$control/$id.ack.tmp"
      mv -f -- "$control/$id.ack.tmp" "$control/$id.ack"
      mv -f -- "$req" "$control/$id.done"
    done
    sleep 0.2
  done
}

# netem.txt: the qdisc of every netem sidecar, with counters
netem_snapshot() {
  local t
  [ "${#netem_svcs[@]}" -gt 0 ] || return 0
  printf '==== %s %s\n' "$(date -u +%FT%T.%3NZ)" "$1" >> "$out/netem.txt"
  for t in "${netem_svcs[@]}"; do
    printf -- '-- %s\n' "$t" >> "$out/netem.txt"
    "${compose[@]}" exec -T "$t" dvt-netem show >> "$out/netem.txt" 2>&1 || true
  done
}

collect() {
  local pid
  for pid in "$watcher_pid" "$stats_pid"; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  netem_snapshot end
  # sniffers first, so every pcapng is closed before the rig goes down
  if [ "${#capture_svcs[@]}" -gt 0 ]; then
    "${compose[@]}" stop "${capture_svcs[@]}" > /dev/null 2>&1 || true
  fi
  "${compose[@]}" ps -a > "$out/compose-ps.txt" 2>&1 || true
  # images only: "compose config" would expand .local/rig.env (the IAX secret)
  "${compose[@]}" images > "$out/compose-images.txt" 2>&1 || true
  local svc
  for svc in $("${compose[@]}" config --services 2>/dev/null); do
    "${compose[@]}" logs --no-color --timestamps "$svc" > "$out/logs/$svc.log" 2>&1 || true
  done
  if [ "$keep" = 1 ]; then
    echo "run.sh: --keep, rig left running; bring it down with scripts/run.sh --down"
  else
    "${compose_all[@]}" down --remove-orphans || true
  fi
}
trap collect EXIT

printf 'markers=%s profile=%s xlxd_ref=%s xlxd_sut=%s transcoder=%s sut_divergences=%s sut_name=%s sut_profile=%s\n' \
  "$markers" "$profile" "${XLXD_REF_IMAGE:-${XLXD_REF_DIR:-ref}}" "${XLXD_SUT_IMAGE:-${XLXD_SUT_DIR:-sut}}" "$TRANSCODER_IMAGE" \
  "${DVT_SUT_DIVERGENCES:-}" "${DVT_SUT_NAME:-}" "${DVT_SUT_PROFILE:-xlxd}" \
  > "$out/run.txt"
cat ".cache/xlxd/${XLXD_REF_DIR:-ref}.fetched" ".cache/xlxd/${XLXD_SUT_DIR:-sut}.fetched" >> "$out/run.txt" 2>/dev/null || true

# Images are shared by all instances: one build at a time (fd 8, own lock).
(
  exec 8>> .cache/build.lock
  flock 8
  # prebuilt reflector images are used as they are, never built over
  mapfile -t to_build < <("${compose[@]}" config --services | grep -vxF -f <(printf '%s\n' "${prebuilt[@]}" "-"))
  "${compose[@]}" build "${to_build[@]}"
)
"${compose[@]}" up -d

for svc in "${capture_svcs[@]}"; do
  f="$out/capture/${svc#capture-}.pcapng"
  for _ in $(seq 50); do [ -s "$f" ] && break; sleep 0.2; done
  [ -s "$f" ] || echo "run.sh: warning: $f not started" >&2
done

netem_snapshot start

mkdir -p "$control"
watch_control 9>&- &
watcher_pid=$!
if [ "$has_load" = 1 ]; then
  sample_stats 9>&- &
  stats_pid=$!
fi

rc=0
"${compose[@]}" exec -T -e DVT_OUT="/dvt/$out" -e DVT_RIG_CONTROL=1 tester \
  python -m pytest -m "$markers" -p no:cacheprovider -rs \
  --junitxml="/dvt/$out/junit.xml" "$@" 2>&1 | tee "$out/pytest.log" || rc=$?
echo "run.sh: pytest exit $rc, results in $out/"

# sniffers stopped and rig down before the capture check reads the files
trap - EXIT
collect
if [ "$capture" = 1 ]; then
  if [ -x scripts/dvcheck.sh ]; then
    check_rc=0
    scripts/dvcheck.sh "$out" > "$out/check.txt" 2>&1 || check_rc=$?
    echo "run.sh: wire check exit $check_rc, see $out/check.txt"
    [ "$rc" != 0 ] || rc=$check_rc
  else
    echo "run.sh: scripts/dvcheck.sh missing, capture not checked" >&2
  fi
fi
exit "$rc"
