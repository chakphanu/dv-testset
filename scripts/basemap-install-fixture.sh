#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# basemap-install-fixture.sh: produce a real installed basemap tree with no internet.
#
# Starts the loopback fixture server of dvtest/basemap_fixture.py (builds.json, a small PMTiles
# v3 archive, 260 glyph / licence / font files and their catalog), runs the given
# `dv-svc-basemap install` (the four MAP2g overrides: --source-index, --source-base,
# --glyph-base, --glyph-catalog): first `install --dry-run` (the plan is saved as
# <out>/install-plan.json), then the install and `dv-svc-basemap verify` into --out, and prints
#
#     DVT_BASEMAP_FILE=<path>
#
# as the last stdout line (relative to the repository when --out is below it: the tester
# container mounts the repository at /dvt). Everything else goes to stderr.
#
# Usage:
#   scripts/basemap-install-fixture.sh --bin PATH/dv-svc-basemap --out DIR \
#       [--profile hotspot|router] [--detail NAME=W,S,E,N]... [--build YYYYMMDD]
#
# Defaults: --profile hotspot --detail dvt=99.9,13.0,101.1,14.1 (the box docs/dp7-cases.md used).
# --bin must come from DV-Router MAP2g or later (an older binary has no --glyph-base and
# refuses the option). --out must not hold a basemap/ yet (nothing is overwritten); put it
# under .local/ (ignored by git), e.g. --out .local/basemap-map2.
#
# The binary is a system under test: the whole run is wrapped in scripts/contained.sh
# (--name test) unless something else already contains it (DVT_CONTAINED is set).
set -euo pipefail

DVT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [ "$#" -eq 0 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//' >&2
    exit 2
fi

if [ -z "${DVT_CONTAINED:-}" ]; then
    exec "$DVT_ROOT/scripts/contained.sh" --name test -- "${BASH_SOURCE[0]}" "$@"
fi

# no cd: a relative --out is relative to where the operator stands
PYTHONPATH="$DVT_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m dvtest.basemap_fixture install "$@"
