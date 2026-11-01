# SPDX-License-Identifier: MIT
# Sourced by scripts/run.sh (bash). The control-channel action
#
#   metrics xlxd-sut
#
# fetches the Prometheus text of the DV-Router hub from INSIDE the system-under-test
# container. The image is scratch (no shell, no curl) and the hub listens on
# 127.0.0.1:19108 of the container's own network namespace, which the tester cannot
# reach. The host starts a throwaway container of the tester image that joins the
# target's network namespace (docker run --network container:<id>,
# dvtest.limits.docker_run_args() caps, no capabilities, read-only root) and does one
# GET of exactly http://127.0.0.1:19108/metrics with python urllib (5 s timeout, no
# proxy). The text goes to <id>.out.
#
# Allow-list (anything else is "error refused", nothing runs): service xlxd-sut, no
# further word. The URL is fixed in the helper; a request cannot name another one.
# dvtest/control.py (METRICS_ALLOWED) mirrors the table for the tester;
# cases/unit/test_control.py checks that the two agree.
#
# dvt_metrics_allowed SERVICE [WORD...]   exit 0 when allow-listed
# dvt_metrics_parse LINE                  prints "SERVICE" or exit 1
# dvt_metrics_exec CONTAINER IMAGE
#   stdout: the metrics text; exit 0 ok, 1 the GET failed (hub off, 401 with a token,
#   timeout), 5 no limits.

DVT_METRICS_URL="http://127.0.0.1:19108/metrics"

dvt_metrics_allowed() {
  [ "$#" -eq 1 ] || return 1
  [ "$1" = xlxd-sut ] || return 1
  return 0
}

dvt_metrics_parse() {
  local action svc extra
  action="" svc="" extra=""
  read -r action svc extra <<< "$1" || true
  [ "$action" = metrics ] && [ -z "$extra" ] || return 1
  dvt_metrics_allowed "$svc" || return 1
  printf '%s\n' "$svc"
}

# Runs inside the helper container (python3 of the tester image): argv[1] is the URL.
DVT_METRICS_SCRIPT='
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(sys.argv[1], timeout=5) as reply:
    sys.stdout.write(reply.read().decode("utf-8", "replace"))
'

dvt_metrics_exec() {
  local cid="$1" img="$2" lim
  mapfile -t lim < <(python3 -c 'from dvtest.limits import docker_run_args; print("\n".join(docker_run_args()))') || return 5
  [ "${#lim[@]}" -gt 0 ] || return 5
  docker run --rm --pull never --network "container:$cid" \
    --user "${DVT_UID:-1000}:${DVT_GID:-1000}" \
    --cap-drop ALL --security-opt no-new-privileges --read-only "${lim[@]}" \
    --entrypoint python3 "$img" -c "$DVT_METRICS_SCRIPT" "$DVT_METRICS_URL"
}
