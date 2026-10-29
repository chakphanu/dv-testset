#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Send a Morse ID transmission to a reflector module over DExtra.
#
#   scripts/live-id.sh --reflector-ip IP --reflector-name XLX999 --module E \
#       --callsign <yours> [--transcoder IP[:PORT]|IMAGE] [--text ...] [--dry-run]
#
# Settings come from .env.example / .env (TRANSCODER_IMAGE is the default
# --transcoder, DVT_TRANSCODER_ENV / DVT_TRANSCODER_ARGS apply to a started
# image). --help lists every option; --dry-run sends nothing.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"
exec python3 -m dvtest.tools.live_id "$@"
