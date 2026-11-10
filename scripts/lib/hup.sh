# SPDX-License-Identifier: MIT
# Sourced by scripts/run.sh (bash). The control-channel action
#
#   hup xlxd-sut
#
# sends SIGHUP to the launcher of the DV-Router system under test (PID 1 of the dvt project's xlxd-sut container;
# `docker kill --signal=HUP <container id>`). It exists for the SECRELOAD1 case (cases/test_secret_reload.py,
# docs/secreload-cases.md) and DV-Router allowed it for that case only: SIGHUP is the documented operator path for
# "same TOML, referenced secret file replaced" (config.md: the launcher re-reads every referenced secret even when the
# TOML hash is unchanged); no HTTP route does that yet. kill-proc never signals the launcher; this action is the one
# exception, and a narrow one.
#
# Allow-list (anything else is "error refused", nothing runs): the service xlxd-sut and no further word; the signal is
# fixed here (HUP), a request cannot name another one, another service (dvr-peer, the references, the helpers) or a
# process. dvtest/control.py (HUP_ALLOWED) mirrors the table for the tester; cases/unit/test_control.py checks that the
# two agree. <id>.out gets "t_hup=<epoch seconds>" taken on the host right after the signal (same clock as the tester).
#
# dvt_hup_allowed SERVICE [WORD...]   exit 0 when allow-listed
# dvt_hup_parse LINE                  prints "SERVICE" or exit 1
# dvt_hup_exec CONTAINER              sends the signal; exit 0 ok, else docker's status

dvt_hup_allowed() {
  [ "$#" -eq 1 ] || return 1
  [ "$1" = xlxd-sut ] || return 1
  return 0
}

dvt_hup_parse() {
  local action svc extra
  action="" svc="" extra=""
  read -r action svc extra <<< "$1" || true
  [ "$action" = hup ] && [ -z "$extra" ] || return 1
  dvt_hup_allowed "$svc" || return 1
  printf '%s\n' "$svc"
}

dvt_hup_exec() {
  local cid="$1"
  [[ "$cid" =~ ^[0-9a-f]{12,64}$ ]] || return 1
  docker kill --signal=HUP "$cid" > /dev/null || return $?
  printf 't_hup=%s\n' "$(date +%s.%N)"
}
