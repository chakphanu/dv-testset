#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Bring the rig up, run pytest inside the tester, collect logs, bring it down.
#
#   scripts/run.sh [-m MARKERS] [--profile core|allstar|nxdn|allstar,nxdn] [--capture] [--keep] [-- PYTEST_ARGS...]
#   scripts/run.sh --down
#
#   -m MARKERS         pytest marker expression (default: rig)
#   --profile allstar  core plus the AllStar node; sets DVT_SUT_USRP=on
#   --profile nxdn     core plus NXDNReflectors nxdn-ref/nxdn-sut peered with
#                      xlxd-ref/xlxd-sut (DVT_NXDN=on); combine: allstar,nxdn
#   --capture          sniffers on xlxd-ref/xlxd-sut/transcoder-sut (+allstar, +nxdn): pcapng in
#                      out/<utc>/capture/<service>.pcapng (decode: scripts/tshark.sh)
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
# TRANSCODER_IMAGE must be set in .env or the shell; TRANSCODER_COMPOSE (default
# .local/transcoder.yml) adapts the transcoder services to that image.
# Results: out/<utc>/ (pytest junit + log, compose ps/images, one log per service).
#
# Control channel for cases that stop a backend (tester has no docker socket):
# while pytest runs, a host watcher polls out/<utc>/control/ ($DVT_OUT/control
# in the tester, DVT_RIG_CONTROL=1). A request is a file <id>.req (write .tmp,
# then rename) with one line "<action> <service>":
#   action  stop | start | kill (SIGKILL)
#   service transcoder-ref | transcoder-sut | allstar
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
down_only=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -m) [ "$#" -ge 2 ] || { echo "run.sh: -m needs a value" >&2; exit 2; }; markers="$2"; shift 2 ;;
    --profile) [ "$#" -ge 2 ] || { echo "run.sh: --profile needs a value" >&2; exit 2; }; profile="$2"; shift 2 ;;
    --keep) keep=1; shift ;;
    --capture) capture=1; shift ;;
    --down) down_only=1; shift ;;
    --) shift; break ;;
    -h|--help) sed -n '3,44p' "$0"; exit 0 ;;
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
if [ -n "${TRANSCODER_COMPOSE:-}" ] && [ -f "$TRANSCODER_COMPOSE" ]; then
  compose_files+=(-f "$TRANSCODER_COMPOSE")
fi
compose_all=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" "${compose_files[@]}" "${DVT_ENV_FILES[@]}"
  --profile core --profile allstar --profile nxdn --profile capture --profile capture-allstar
  --profile capture-nxdn)

if [ "$down_only" = 1 ]; then
  take_lock
  "${compose_all[@]}" down --remove-orphans
  exit 0
fi

profiles=(--profile core)
has_allstar=0
has_nxdn=0
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
    *) echo "run.sh: --profile takes core, allstar, nxdn or a comma list (allstar,nxdn)" >&2; exit 2 ;;
  esac
done
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
fi

if [ -z "${TRANSCODER_IMAGE:-}" ]; then
  echo "run.sh: TRANSCODER_IMAGE is not set; set it in .env (TRANSCODER_IMAGE=<your-ambed-image>)" >&2
  exit 2
fi

for name in "${XLXD_REF_DIR:-ref}" "${XLXD_SUT_DIR:-sut}"; do
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

compose=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" "${compose_files[@]}" "${DVT_ENV_FILES[@]}" "${profiles[@]}")

watcher_pid=""
control="$out/control"

watch_control() {
  local req id line action svc extra rc
  while :; do
    for req in "$control"/*.req; do
      [ -e "$req" ] || continue
      id="$(basename "$req" .req)"
      line="$(head -n 1 -- "$req")"
      action="" svc="" extra=""
      read -r action svc extra <<< "$line" || true
      printf '%s request %s\n' "$(date -u +%FT%T.%3NZ)" "$line" >> "$control/control.log"
      rc=refused
      if [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ -z "$extra" ]; then
        case "$svc" in transcoder-ref|transcoder-sut|allstar) ;; *) action="" ;; esac
        rc=0
        case "$action" in
          stop)  "${compose[@]}" stop "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          start) "${compose[@]}" start "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          kill)  "${compose[@]}" kill -s SIGKILL "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          *)     rc=refused ;;
        esac
        # a sniffer shares the namespace of its service and dies with it:
        # restart it, it writes <service>.<n>.pcapng
        if [ "$action" = start ] && [ "$rc" = 0 ] && [[ " ${capture_svcs[*]} " == *" capture-$svc "* ]]; then
          "${compose[@]}" up -d --no-deps "capture-$svc" >> "$control/control.log" 2>&1 || true
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

collect() {
  if [ -n "$watcher_pid" ]; then
    kill "$watcher_pid" 2>/dev/null || true
    wait "$watcher_pid" 2>/dev/null || true
  fi
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

printf 'markers=%s profile=%s xlxd_ref=%s xlxd_sut=%s transcoder=%s\n' \
  "$markers" "$profile" "${XLXD_REF_DIR:-ref}" "${XLXD_SUT_DIR:-sut}" "$TRANSCODER_IMAGE" \
  > "$out/run.txt"
cat ".cache/xlxd/${XLXD_REF_DIR:-ref}.fetched" ".cache/xlxd/${XLXD_SUT_DIR:-sut}.fetched" >> "$out/run.txt" 2>/dev/null || true

# Images are shared by all instances: one build at a time (fd 8, own lock).
(
  exec 8>> .cache/build.lock
  flock 8
  "${compose[@]}" build
)
"${compose[@]}" up -d

for svc in "${capture_svcs[@]}"; do
  f="$out/capture/${svc#capture-}.pcapng"
  for _ in $(seq 50); do [ -s "$f" ] && break; sleep 0.2; done
  [ -s "$f" ] || echo "run.sh: warning: $f not started" >&2
done

mkdir -p "$control"
watch_control 9>&- &
watcher_pid=$!

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
