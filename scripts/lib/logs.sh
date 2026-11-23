# SPDX-License-Identifier: MIT
# Sourced by scripts/run.sh (bash). The control-channel action
#
#   logs xlxd-sut
#
# READS the log of the system-under-test container so far (docker logs --timestamps, stdout and
# stderr) into <id>.out. run.sh itself writes out/<run>/logs/<service>.log only after pytest ends;
# this lets a case judge log lines while the rig is up (ASL1j: the registration failure lines
# `allstar_registration_failed ... next_retry_delay_ms=`, case R13 of cases/test_asl_edge.py).
# Nothing is signalled or changed. The log is run output like every other file of out/<run>
# (the post-run secret scan covers it).
#
# Allow-list (anything else is "error refused", nothing runs): service xlxd-sut, no further word.
# dvtest/control.py (LOGS_ALLOWED) mirrors the table for the tester;
# cases/unit/test_control.py checks that the two agree.
#
# dvt_logs_allowed SERVICE [WORD...]   exit 0 when allow-listed
# dvt_logs_parse LINE                  prints "SERVICE" or exit 1
# dvt_logs_exec CONTAINER              stdout: the log text; exit 0 ok, else docker's status

dvt_logs_allowed() {
  [ "$#" -eq 1 ] || return 1
  [ "$1" = xlxd-sut ] || return 1
  return 0
}

dvt_logs_parse() {
  local action svc extra
  action="" svc="" extra=""
  read -r action svc extra <<< "$1" || true
  [ "$action" = logs ] && [ -z "$extra" ] || return 1
  dvt_logs_allowed "$svc" || return 1
  printf '%s\n' "$svc"
}

dvt_logs_exec() {
  docker logs --timestamps "$1" 2>&1
}
