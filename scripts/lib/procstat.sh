# SPDX-License-Identifier: MIT
# Sourced by scripts/run.sh (bash). The control-channel action
#
#   proc-stat <service> <process>
#
# READS the CPU ticks and the memory of one process INSIDE a rig container (the web load test measures the
# WebRTC edge this way, the web soak test every child of the launcher). The DV-Router image is scratch (no
# shell, no ps), its processes live in the container's pid namespace, so a throwaway container of the tester
# image joins that namespace (docker run --pid container:<id>, no network, no capability, read-only root, the
# dvtest.limits caps) and reads /proc/<pid>/{comm,stat,status,fd}. The helper runs as the user of the SUT
# container (docker inspect Config.User; root when the image names none; the host uid for a user given by
# name), because /proc/<pid>/fd of a process is readable only for the same uid (no capability is granted: the
# uid match is enough); when the fd directory is still unreadable the reply says fds=-1. A process is reported
# only when its comm equals <process> exactly and it is not a zombie. Nothing is signalled, nothing is written.
#
# Allow-list (anything else is "error refused", nothing runs): service xlxd-sut only; the exact process names
# of DVT_PROCSTAT_NAMES below (the launcher dv-router included: it is only READ here, kill-proc never signals
# it) and the word "list". dvtest/control.py (PROC_STAT_NAMES) mirrors the table for the tester;
# cases/unit/test_procstat.py checks that the two agree.
#
# "proc-stat xlxd-sut list" prints one line "proc=<pid> <ppid> <comm>" for EVERY process of the container (names
# only, never a command line or an environment), so that a test can see children that are not in the table.
#
# dvt_procstat_allowed SERVICE PROCESS   exit 0 when allow-listed
# dvt_procstat_parse LINE                prints "SERVICE PROCESS" or exit 1
# dvt_procstat_user CONTAINER            prints the "uid:gid" the helper runs as
# dvt_procstat_exec CONTAINER IMAGE PROCESS
#   stdout: pid=, comm=, utime=, stime= (ticks), clk_tck=, threads=, vmhwm_kb=, vmrss_kb=, fds= (-1 when not
#   readable), starttime= (clock ticks after boot: with the pid it names the process), t=<epoch seconds, taken
#   right after the reads>; exit 0 ok, 3 no such process, 5 no limits.
#   Optional, from /proc/<pid>/smaps_rollup (KiB, a key is left out when the file or the line is missing or
#   unreadable): smaps_rss_kb= (Rss), smaps_anon_kb= (Anonymous), smaps_private_dirty_kb= (Private_Dirty),
#   smaps_pss_anon_kb= (Pss_Anon). dvtest.webload.parse_procstat_out reads them into ProcSample.smaps.

# Explicit list, never a pattern: every edge is its own binary (no combined edge); aprsis, ax25, codec-ambed and
# the svc-* ones exist only when enabled. Keep in step with PROC_STAT_NAMES of dvtest/control.py (unit test).
DVT_PROCSTAT_NAMES="dv-router \
  dv-edge-dextra dv-edge-dplus dv-edge-dcs dv-edge-mmdvm dv-edge-dmrplus dv-edge-ysf \
  dv-edge-m17 dv-edge-g3 dv-edge-imrs dv-edge-xlx dv-edge-nxdn dv-edge-p25 dv-edge-usrp dv-edge-webrtc \
  dv-edge-aprsis dv-edge-ax25 \
  dv-room dv-data-worker dv-codec-ambed \
  dv-svc-status dv-svc-web dv-svc-admin dv-svc-iddb dv-svc-idsync dv-svc-callhome dv-svc-recorder"

dvt_procstat_allowed() {
  [ "$#" -eq 2 ] || return 1
  [ "$1" = xlxd-sut ] || return 1
  [ "$2" = list ] && return 0
  local n
  for n in $DVT_PROCSTAT_NAMES; do
    [ "$2" = "$n" ] && return 0
  done
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
SMAPS = (("Rss", "smaps_rss_kb"), ("Anonymous", "smaps_anon_kb"), ("Private_Dirty", "smaps_private_dirty_kb"), ("Pss_Anon", "smaps_pss_anon_kb"))
if want == "list":
    for name in sorted((d for d in os.listdir("/proc") if d.isdigit()), key=int):
        try:
            with open("/proc/%s/comm" % name) as fh:
                comm = fh.read().strip()
            with open("/proc/%s/stat" % name) as fh:
                rest = fh.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(name) != me and rest[0] != "Z":
            print("proc=%s %s %s" % (name, rest[1], comm))
    sys.exit(0)
found = 0
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
    try:
        fds = len(os.listdir("/proc/%d/fd" % pid))
    except OSError:
        fds = -1
    smaps = {}
    try:
        with open("/proc/%d/smaps_rollup" % pid) as fh:
            for line in fh:
                key, _, value = line.partition(":")
                for want_key, out_key in SMAPS:
                    if key == want_key and value.split():
                        smaps[out_key] = value.split()[0]
    except OSError:
        pass
    now = time.time()
    # one block of key=value lines per process, each starting with pid= (dv-room runs one process per module)
    print("pid=%d" % pid)
    print("comm=%s" % comm)
    print("utime=%s" % rest[11])
    print("stime=%s" % rest[12])
    print("clk_tck=%d" % os.sysconf("SC_CLK_TCK"))
    print("threads=%s" % rest[17])
    print("vmhwm_kb=%s" % mem.get("VmHWM", "0"))
    print("vmrss_kb=%s" % mem.get("VmRSS", "0"))
    print("fds=%d" % fds)
    for _want_key, out_key in SMAPS:
        if out_key in smaps:
            print("%s=%s" % (out_key, smaps[out_key]))
    print("starttime=%s" % rest[19])
    print("t=%.6f" % now)
    found += 1
if found:
    sys.exit(0)
print("none=" + want)
sys.exit(3)
'

dvt_procstat_user() {
  local u
  u="$(docker inspect -f '{{.Config.User}}' "$1" 2>/dev/null || true)"
  if [ -z "$u" ]; then
    printf '0:0\n'
  elif [[ "$u" =~ ^[0-9]+:[0-9]+$ ]]; then
    printf '%s\n' "$u"
  elif [[ "$u" =~ ^[0-9]+$ ]]; then
    printf '%s:%s\n' "$u" "$u"
  else
    printf '%s:%s\n' "${DVT_UID:-1000}" "${DVT_GID:-1000}"
  fi
}

dvt_procstat_exec() {
  local cid="$1" img="$2" proc="$3" lim suser
  # defense in depth: the caller checked the allow-list, the helper only ever sees plain names
  [[ "$proc" =~ ^[a-z0-9-]+$ ]] || return 1
  mapfile -t lim < <(python3 -c 'from dvtest.limits import docker_run_args; print("\n".join(docker_run_args()))') || return 5
  [ "${#lim[@]}" -gt 0 ] || return 5
  suser="$(dvt_procstat_user "$cid")"
  docker run --rm --pull never --network none --pid "container:$cid" \
    --user "$suser" \
    --cap-drop ALL --security-opt no-new-privileges --read-only "${lim[@]}" \
    --entrypoint python3 "$img" -c "$DVT_PROCSTAT_SCRIPT" "$proc"
}
