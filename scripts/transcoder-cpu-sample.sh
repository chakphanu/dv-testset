#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Sample the CPU and memory of the rig's transcoder-sut (container dvt-transcoder-sut-1, project dvt only) on the host while
# cases/test_transcoder_levels.py runs: one line "<epoch seconds> <cpu %> <memory>" about every 2 s into OUTFILE, until the file
# OUTFILE.stop exists or the container is gone for 60 s. Read only (docker stats); nothing is started, stopped or changed.
#   scripts/transcoder-cpu-sample.sh out/transcoder-cpu-<tag>.txt &
set -euo pipefail
out="${1:?usage: transcoder-cpu-sample.sh OUTFILE}"
name="dvt-transcoder-sut-1"
gone=0
: > "$out"
while [ ! -e "$out.stop" ]; do
  if line="$(docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' "$name" 2>/dev/null)" && [ -n "$line" ]; then
    gone=0
    printf '%s %s\n' "$(date +%s.%N)" "$line" >> "$out"
  else
    gone=$((gone + 1))
    [ "$gone" -lt 30 ] || break
    sleep 2
  fi
done
