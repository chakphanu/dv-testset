# SPDX-License-Identifier: MIT
# Sourced by scripts/run.sh (bash). The control-channel action
#
#   proc-stat <service> <process>
#
# READS the CPU ticks and the memory high-water mark of one process INSIDE a rig container (the web load
# test measures the WebRTC edge this way). The DV-Router image is scratch (no shell, no ps), its processes
# belong to root and live in the container's pid namespace, so a throwaway container of the tester image joins
# that namespace (docker run --pid container:<id>, no network, no capability, read-only root, the
# dvtest.limits caps, the host uid) and reads /proc/<pid>/{comm,stat,status}, which are world readable. A
# process is reported only when its comm equals <process> exactly and it is not a zombie. Nothing is signalled.
#
# Allow-list (anything else is "error refused", nothing runs): service xlxd-sut only; the processes of the web
# stack named below. dvtest/control.py (PROC_STAT_ALLOWED) mirrors the table for the tester;
# cases/unit/test_procstat.py checks that the two agree.
#
# dvt_procstat_allowed SERVICE PROCESS   exit 0 when allow-listed
# dvt_procstat_parse LINE                prints "SERVICE PROCESS" or exit 1
# dvt_procstat_exec CONTAINER IMAGE PROCESS
#   stdout: pid=, comm=, utime=, stime= (ticks), clk_tck=, threads=, vmhwm_kb=, vmrss_kb=, t=<epoch seconds,
#   taken right after the reads>; exit 0 ok, 3 no such process, 5 no limits.

dvt_procstat_allowed() {
  [ "$#" -eq 2 ] || return 1
  [ "$1" = xlxd-sut ] || return 1
  case "$2" in
    dv-edge-webrtc|dv-svc-web|dv-svc-status) return 0 ;;
  esac
  return 1
}

dvt_procstat_parse() {
  local action svc proc extra
  action="" svc="" proc="" extra=""
  read -r action svc proc extra <<< "$1" || true
  [ "$action" = proc-stat ] && [ -z "$extra" ] || return 1
  dvt_procstat_allowed "$svc" "$proc" || return 1
  printf '%s %s\n' "$svc" "$proc"
}

# Runs inside the helper container (python3 of the tester image): argv[1] is the exact comm.
DVT_PROCSTAT_SCRIPT='
import os, sys, time
want = sys.argv[1]
me = os.getpid()
for name in sorted((d for d in os.listdir("/proc") if d.isdigit()), key=int):
    pid = int(name)
    if pid == me:
        continue
    try:
        with open("/proc/%d/comm" % pid) as fh:
            comm = fh.read().strip()
        if comm != want:
            continue
        with open("/proc/%d/stat" % pid) as fh:
            rest = fh.read().rsplit(")", 1)[1].split()
        if rest[0] == "Z":
            continue
        mem = {}
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key in ("VmHWM", "VmRSS"):
                    mem[key] = value.split()[0]
    except (OSError, IndexError):
        continue
    now = time.time()
    print("pid=%d" % pid)
    print("comm=%s" % comm)
    print("utime=%s" % rest[11])
    print("stime=%s" % rest[12])
    print("clk_tck=%d" % os.sysconf("SC_CLK_TCK"))
    print("threads=%s" % rest[17])
    print("vmhwm_kb=%s" % mem.get("VmHWM", "0"))
    print("vmrss_kb=%s" % mem.get("VmRSS", "0"))
    print("t=%.6f" % now)
    sys.exit(0)
print("none=" + want)
sys.exit(3)
'

dvt_procstat_exec() {
  local cid="$1" img="$2" proc="$3" lim
  # defense in depth: the caller checked the allow-list, the helper only ever sees plain names
  [[ "$proc" =~ ^[a-z0-9-]+$ ]] || return 1
  mapfile -t lim < <(python3 -c 'from dvtest.limits import docker_run_args; print("\n".join(docker_run_args()))') || return 5
  [ "${#lim[@]}" -gt 0 ] || return 5
  docker run --rm --pull never --network none --pid "container:$cid" \
    --user "${DVT_UID:-1000}:${DVT_GID:-1000}" \
    --cap-drop ALL --security-opt no-new-privileges --read-only "${lim[@]}" \
    --entrypoint python3 "$img" -c "$DVT_PROCSTAT_SCRIPT" "$proc"
}
