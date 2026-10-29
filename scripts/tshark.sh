#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Run tshark from the rig capture image (no host install needed).
#
#   scripts/tshark.sh ARGS...      e.g. -r out/<utc>/capture/xlxd-sut.pcapng -q -z io,phs
#
# The repository is mounted read-only at /dvt (working directory) and out/
# read-write, so paths must be relative to the repository root. When
# tools/wireshark/dvtest.lua exists it is loaded (-X lua_script:...).
# The image (dvt-capture:local) is built from rig/Dockerfile.capture on first use.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

image="dvt-capture:local"
if ! docker image inspect "$image" > /dev/null 2>&1; then
  echo "tshark.sh: building $image" >&2
  dvt_env_files
  docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" -f rig/compose.yml "${DVT_ENV_FILES[@]}" \
    --profile capture build capture-xlxd-sut >&2
fi

lua=()
[ -f tools/wireshark/dvtest.lua ] && lua=(-X lua_script:/dvt/tools/wireshark/dvtest.lua)

mkdir -p out
tty=()
[ -t 0 ] && [ -t 1 ] && tty=(-t)
# .local (secrets) is hidden behind an empty tmpfs
exec docker run --rm -i "${tty[@]}" --network none \
  --user "$(id -u):$(id -g)" \
  -v "$DVT_ROOT:/dvt:ro" -v "$DVT_ROOT/out:/dvt/out" --tmpfs /dvt/.local:ro \
  --entrypoint tshark "$image" "${lua[@]}" "$@"
