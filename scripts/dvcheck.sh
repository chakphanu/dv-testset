#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Passive wire check of capture files (python3 -m dvtest.check).
#
#   scripts/dvcheck.sh out/<utc> [--strict] [--per-code N] [--jitter-ms MS]
#   scripts/dvcheck.sh FILE.pcapng [--json OUT] [--ports-from rig]
#
# A run directory (one with capture/) defaults to --ports-from rig and
# --json out/<utc>/check.json; findings during a control-channel stop of a
# service are listed as warnings. Exit 0 clean, 1 anomalies, 2 usage/read error.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
args=("$@")
if [ "$#" -ge 1 ] && [ -d "$1/capture" ]; then
  case " $* " in *" --json "*) ;; *) args+=(--json "$1/check.json") ;; esac
  case " $* " in *" --ports-from "*) ;; *) args+=(--ports-from rig) ;; esac
fi
PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m dvtest.check "${args[@]}"
