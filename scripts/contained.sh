#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# contained.sh: run heavy dv-testset work (the rig, host-side pytest levels,
# xlxd builds) inside a capped systemd user scope, so parallel workers cannot
# exhaust the host (on 2026-10-31 unbounded parallel builds and tests took a
# host to load 185 and 40 GB of swap).
#
# Usage:
#   scripts/contained.sh [--name NAME] [--mem SIZE] [--cpu PCT] [--jobs N]
#                        [--wait-sec N] -- CMD [ARG...]
#   scripts/contained.sh --install     # optional: write dvt-rig.slice (once per host)
#   scripts/contained.sh --status      # slice usage and running jobs
#   scripts/contained.sh --stop NAME   # stop job dvt-NAME.scope (and its tree)
#
# Each job is a transient scope dvt-<NAME>.scope in dvt-rig.slice with
# MemoryMax (--mem, default 4G), swap 0, CPUQuota (--cpu percent of one core,
# default 800) and TasksMax 1024. An overrun is an OOM kill inside the scope,
# never host-wide swapping. Everything the command starts stays in the scope,
# including detached children, until the scope is stopped.
#
# The scope sets DVT_CONTAINED=<unit> (scripts/run.sh checks it), DVT_BUILD_JOBS,
# CARGO_BUILD_JOBS, RUST_TEST_THREADS and RAYON_NUM_THREADS to --jobs (default 8).
#
# Admission: at most DVT_MAX_JOBS (default 2) scopes at once, never two with
# the same NAME (the rig is always NAME "rig": one rig run at a time, across
# DVT_INSTANCE values), and at least DVT_MIN_FREE_GIB (default 12) GiB
# MemAvailable on the host; otherwise it waits up to --wait-sec (default 3600)
# and then exits 75 (busy) or 74 (headroom).
# Exit codes: payload status | 74 headroom | 75 busy | 77 policy/setup
# | 137 killed by SIGKILL (usually the scope memory limit).
#
# Docker containers are not children of this scope (the daemon starts them):
# their caps are the per-service limits in rig/compose.yml (README, "Resource
# limits"). The optional slice (--install) adds a ceiling of 16G memory, swap 0,
# 1200% CPU and 2048 tasks over all dvt scopes; without it the per-scope
# limits and the admission rules above still apply.
set -euo pipefail

SLICE=dvt-rig.slice
SLICE_MEM_MAX=16G
SLICE_MEM_HIGH=14G
SLICE_CPU=1200%
SLICE_TASKS=2048
MAX_JOBS=${DVT_MAX_JOBS:-2}
MIN_FREE_GIB=${DVT_MIN_FREE_GIB:-12}

die() { echo "contained.sh: $2" >&2; exit "$1"; }

export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}
[ -S "$XDG_RUNTIME_DIR/bus" ] \
  || die 77 "no user bus at $XDG_RUNTIME_DIR/bus (inside a sandbox? run the whole job contained from outside)"

running_jobs() {
  systemctl --user list-units --type=scope --state=active --no-legend --plain 'dvt-*.scope' 2>/dev/null \
    | awk '{print $1}'
}

case "${1:-}" in
  --install)
    unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    [ ! -e "$unit_dir/$SLICE" ] || die 77 "$unit_dir/$SLICE already exists; remove it first to rewrite"
    cat > "$unit_dir/$SLICE" <<EOF
[Unit]
Description=dv-testset rig and host-side tests (resource-capped)

[Slice]
MemoryAccounting=yes
MemoryMax=$SLICE_MEM_MAX
MemoryHigh=$SLICE_MEM_HIGH
MemorySwapMax=0
CPUQuota=$SLICE_CPU
TasksMax=$SLICE_TASKS
EOF
    systemctl --user daemon-reload
    echo "installed $unit_dir/$SLICE"
    exit 0 ;;
  --status)
    systemctl --user show "$SLICE" -p MemoryCurrent -p MemoryMax -p MemorySwapMax -p CPUQuotaPerSecUSec -p TasksCurrent -p TasksMax
    echo "jobs (max $MAX_JOBS):"
    for u in $(running_jobs); do
      printf '  %-36s mem=%s tasks=%s\n' "$u" \
        "$(systemctl --user show "$u" -p MemoryCurrent --value)" \
        "$(systemctl --user show "$u" -p TasksCurrent --value)"
    done
    awk '/MemAvailable/ {printf "host MemAvailable %.1f GiB\n", $2/1048576}' /proc/meminfo
    exit 0 ;;
  --stop)
    [ -n "${2:-}" ] || die 77 "--stop needs a NAME"
    systemctl --user stop "dvt-$2.scope"
    exit 0 ;;
esac

name="job-$$"; mem=4G; cpu=800; jobs=8; wait_sec=3600
while [ $# -gt 0 ]; do
  case "$1" in
    --name) [ $# -ge 2 ] || die 77 "--name needs a value"; name=$2; shift 2 ;;
    --mem) [ $# -ge 2 ] || die 77 "--mem needs a value"; mem=$2; shift 2 ;;
    --cpu) [ $# -ge 2 ] || die 77 "--cpu needs a value"; cpu=$2; shift 2 ;;
    --jobs) [ $# -ge 2 ] || die 77 "--jobs needs a value"; jobs=$2; shift 2 ;;
    --wait-sec) [ $# -ge 2 ] || die 77 "--wait-sec needs a value"; wait_sec=$2; shift 2 ;;
    --) shift; break ;;
    *) die 77 "unknown option $1 (use -- before the command)" ;;
  esac
done
[ $# -gt 0 ] || die 77 "no command given"
[[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || die 77 "bad --name $name"
[[ "$mem" =~ ^[0-9]+[KMGkmg]?$ ]] || die 77 "bad --mem $mem (e.g. 4G)"
[[ "$cpu" =~ ^[0-9]+$ ]] || die 77 "bad --cpu $cpu (percent of one core, e.g. 800)"
[[ "$jobs" =~ ^[0-9]+$ ]] && [ "$jobs" -ge 1 ] || die 77 "bad --jobs $jobs"
[[ "$wait_sec" =~ ^[0-9]+$ ]] || die 77 "bad --wait-sec $wait_sec"
[ -z "${DVT_CONTAINED:-}" ] || die 77 "already inside scope $DVT_CONTAINED; do not nest contained.sh"
unit="dvt-$name.scope"

[ -f "$HOME/.config/systemd/user/$SLICE" ] \
  || echo "contained.sh: note: $SLICE not installed (scripts/contained.sh --install); per-scope limits still apply" >&2

# Admission: wait for a free job slot, a free NAME and host memory headroom.
deadline=$(( $(date +%s) + wait_sec ))
while :; do
  n=$(running_jobs | wc -l)
  free_gib=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)
  same=0
  systemctl --user is-active --quiet "$unit" && same=1
  if [ "$n" -lt "$MAX_JOBS" ] && [ "$same" = 0 ] && [ "$free_gib" -ge "$MIN_FREE_GIB" ]; then break; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    { [ "$same" = 1 ] || [ "$n" -ge "$MAX_JOBS" ]; } && die 75 "busy: $unit active=$same, $n jobs running (max $MAX_JOBS)"
    die 74 "headroom: ${free_gib} GiB available (need $MIN_FREE_GIB)"
  fi
  echo "contained.sh: waiting (jobs $n/$MAX_JOBS, $unit active=$same, free ${free_gib} GiB)" >&2
  sleep 20
done

# Keep parallelism inside the limits: builds (make via DVT_BUILD_JOBS, cargo
# of a system under test) and common thread pools.
export DVT_CONTAINED=$unit DVT_BUILD_JOBS=$jobs
export CARGO_BUILD_JOBS=$jobs RUST_TEST_THREADS=${RUST_TEST_THREADS:-$jobs}
export RAYON_NUM_THREADS=${RAYON_NUM_THREADS:-$jobs}

set +e
systemd-run --user --scope --quiet --collect --slice="$SLICE" --unit="$unit" \
  -p MemoryMax="$mem" -p MemorySwapMax=0 -p CPUQuota="${cpu}%" -p TasksMax=1024 \
  -- "$@"
rc=$?
set -e
[ "$rc" -eq 137 ] && echo "contained.sh: $unit killed by SIGKILL (likely memory limit $mem)" >&2
exit "$rc"
