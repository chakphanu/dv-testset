# SPDX-License-Identifier: MIT
# Sourced by scripts/run.sh (bash). The control-channel action
#
#   kill-proc <service> <process> <signal>
#
# signals a process INSIDE a rig container. The DV-Router image is scratch
# (no sh, ps, pkill), the container's processes belong to root, and the host
# user may not signal them, so the signal is sent by a throwaway container of
# the tester image that joins the target's pid namespace (docker run --pid
# container:<id>) and walks /proc/<pid>/comm: a process is signalled only when
# its comm equals <process> exactly (no pattern, no cmdline match) and it is
# not a zombie.
#
# Allow-list (anything else is "error refused", nothing runs):
#   service xlxd-sut only; dv-edge-xlx, dv-edge-nxdn, dv-edge-p25 with KILL or
#   USR1 (quiet restart: the edge saves its sessions, exits, the launcher
#   respawns it); dv-codec-ambed (the codec worker) with KILL only. The
#   launcher dv-router is never signalled: it has no handler and a stray signal
#   kills the node.
# dvtest/control.py (KILL_PROC_ALLOWED) mirrors the table for the tester;
# cases/unit/test_control.py checks that the two agree.
#
# dvt_killproc_allowed SERVICE PROCESS SIGNAL   exit 0 when allow-listed
# dvt_killproc_parse LINE                       prints "SERVICE PROCESS SIGNAL" or exit 1
# dvt_killproc_exec CONTAINER IMAGE PROCESS SIGNAL
#   stdout: "pid=<n>" per signalled process, then "t_kill=<epoch seconds>";
#   exit 0 ok, 3 no process of that comm, 4 a signal failed, 5 no limits.

dvt_killproc_allowed() {
  [ "$#" -eq 3 ] || return 1
  [ "$1" = xlxd-sut ] || return 1
  case "$2:$3" in
    dv-edge-xlx:KILL|dv-edge-xlx:USR1|dv-edge-nxdn:KILL|dv-edge-nxdn:USR1|dv-edge-p25:KILL|dv-edge-p25:USR1|dv-codec-ambed:KILL) return 0 ;;
  esac
  return 1
}

dvt_killproc_parse() {
  local action svc proc sig extra
  action="" svc="" proc="" sig="" extra=""
  read -r action svc proc sig extra <<< "$1" || true
  [ "$action" = kill-proc ] && [ -z "$extra" ] || return 1
  dvt_killproc_allowed "$svc" "$proc" "$sig" || return 1
  printf '%s %s %s\n' "$svc" "$proc" "$sig"
}

# Runs inside the helper container (sh = dash/bash of the tester image):
# $1 signal, $2 exact comm. The helper's own pid is skipped; its comm is "sh".
DVT_KILLPROC_SCRIPT='
sig=$1; want=$2; n=0; bad=0; st=
for d in /proc/[0-9]*; do
  pid=${d#/proc/}
  [ "$pid" != "$$" ] || continue
  { read -r comm < "$d/comm"; } 2>/dev/null || continue
  [ "$comm" = "$want" ] || continue
  # a killed process that its parent has not reaped yet keeps its comm: skip zombies
  st=
  { while read -r k v; do [ "$k" != "State:" ] || { st=${v%"${v#?}"}; break; }; done < "$d/status"; } 2>/dev/null || continue
  [ "$st" != Z ] || continue
  if kill -s "$sig" "$pid" 2>/dev/null; then echo "pid=$pid"; n=$((n + 1)); else echo "failed=$pid"; bad=$((bad + 1)); fi
done
echo "t_kill=$(date +%s.%N)"
[ "$bad" -eq 0 ] || exit 4
[ "$n" -gt 0 ] || { echo "none=$want"; exit 3; }
'

dvt_killproc_exec() {
  local cid="$1" img="$2" proc="$3" sig="$4" lim
  # defense in depth: the caller checked the allow-list, the helper only ever
  # sees names made of the characters of the allowed ones
  [[ "$proc" =~ ^[a-z0-9-]+$ ]] && [[ "$sig" =~ ^[A-Z0-9]+$ ]] || return 1
  mapfile -t lim < <(python3 -c 'from dvtest.limits import docker_run_args; print("\n".join(docker_run_args()))') || return 5
  [ "${#lim[@]}" -gt 0 ] || return 5
  docker run --rm --pull never --network none --pid "container:$cid" --user 0:0 \
    --cap-drop ALL --cap-add KILL --security-opt no-new-privileges --read-only "${lim[@]}" \
    --entrypoint sh "$img" -c "$DVT_KILLPROC_SCRIPT" dvt-killproc "$sig" "$proc"
}
