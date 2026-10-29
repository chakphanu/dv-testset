#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Bring the rig up, run pytest inside the tester, collect logs, bring it down.
#
#   scripts/run.sh [-m MARKERS] [--profile core|allstar] [--keep] [-- PYTEST_ARGS...]
#
#   -m MARKERS         pytest marker expression (default: rig)
#   --profile allstar  core plus the AllStar node; sets DVT_SUT_USRP=on
#   --keep             leave the rig running (default: compose down at the end)
#
# Settings: .env.example, then .env, then .local/rig.env (scripts/lib/env.sh);
# TRANSCODER_IMAGE must be set in .env or the shell.
# Results: out/<utc>/ (pytest junit + log, compose ps/images, one log per service).
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
dvt_load_env
cd "$DVT_ROOT"

markers="rig"
profile="core"
keep=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -m) [ "$#" -ge 2 ] || { echo "run.sh: -m needs a value" >&2; exit 2; }; markers="$2"; shift 2 ;;
    --profile) [ "$#" -ge 2 ] || { echo "run.sh: --profile needs a value" >&2; exit 2; }; profile="$2"; shift 2 ;;
    --keep) keep=1; shift ;;
    --) shift; break ;;
    -h|--help) sed -n '3,14p' "$0"; exit 0 ;;
    *) echo "run.sh: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
done

profiles=(--profile core)
case "$profile" in
  core) ;;
  allstar)
    profiles+=(--profile allstar)
    export DVT_SUT_USRP="${DVT_SUT_USRP:-on}"
    [ -f .local/rig.env ] || { echo "run.sh: .local/rig.env missing; run scripts/gen-local.sh" >&2; exit 2; }
    ;;
  *) echo "run.sh: --profile must be core or allstar" >&2; exit 2 ;;
esac

if [ -z "${TRANSCODER_IMAGE:-}" ]; then
  echo "run.sh: TRANSCODER_IMAGE is not set; set it in .env (TRANSCODER_IMAGE=<your-ambed-image>)" >&2
  exit 2
fi

# DVT_SUBNET (a /24), when set, wins over DVT_NET.
if [ -n "${DVT_SUBNET:-}" ]; then
  case "$DVT_SUBNET" in
    *.0/24) export DVT_NET="${DVT_SUBNET%.0/24}" ;;
    *) echo "run.sh: DVT_SUBNET must be a /24 like 172.30.0.0/24" >&2; exit 2 ;;
  esac
fi

for name in "${XLXD_REF_DIR:-ref}" "${XLXD_SUT_DIR:-sut}"; do
  [ -f ".cache/xlxd/$name/src/main.h" ] || { echo "run.sh: .cache/xlxd/$name missing; run scripts/pre-fetch-xlxd.sh" >&2; exit 2; }
done
[ -f .cache/codec2/CMakeLists.txt ] || { echo "run.sh: .cache/codec2 missing; run scripts/pre-fetch-codec2.sh" >&2; exit 2; }

scripts/pii-check.sh

DVT_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
DVT_UID="$(id -u)"
DVT_GID="$(id -g)"
export DVT_RUN_ID DVT_UID DVT_GID
out="out/$DVT_RUN_ID"
mkdir -p "$out/logs"

dvt_env_files
compose=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" -f rig/compose.yml "${DVT_ENV_FILES[@]}" "${profiles[@]}")

collect() {
  "${compose[@]}" ps -a > "$out/compose-ps.txt" 2>&1 || true
  # images only: "compose config" would expand .local/rig.env (the IAX secret)
  "${compose[@]}" images > "$out/compose-images.txt" 2>&1 || true
  local svc
  for svc in $("${compose[@]}" config --services 2>/dev/null); do
    "${compose[@]}" logs --no-color --timestamps "$svc" > "$out/logs/$svc.log" 2>&1 || true
  done
  if [ "$keep" = 1 ]; then
    echo "run.sh: --keep, rig left running (stop it: docker compose -p ${COMPOSE_PROJECT_NAME:-dvt} -f rig/compose.yml ${profiles[*]} down)"
  else
    "${compose[@]}" down --remove-orphans || true
  fi
}
trap collect EXIT

printf 'markers=%s profile=%s xlxd_ref=%s xlxd_sut=%s transcoder=%s\n' \
  "$markers" "$profile" "${XLXD_REF_DIR:-ref}" "${XLXD_SUT_DIR:-sut}" "$TRANSCODER_IMAGE" \
  > "$out/run.txt"
cat ".cache/xlxd/${XLXD_REF_DIR:-ref}.fetched" ".cache/xlxd/${XLXD_SUT_DIR:-sut}.fetched" >> "$out/run.txt" 2>/dev/null || true

"${compose[@]}" up -d --build

rc=0
"${compose[@]}" exec -T -e DVT_OUT="/dvt/$out" tester \
  python -m pytest -m "$markers" -p no:cacheprovider -rs \
  --junitxml="/dvt/$out/junit.xml" "$@" 2>&1 | tee "$out/pytest.log" || rc=$?
echo "run.sh: pytest exit $rc, results in $out/"
exit "$rc"
