#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Bring the rig up, run pytest inside the tester, collect logs, bring it down.
#
#   scripts/run.sh [-m MARKERS] [--profile core|allstar|nxdn|p25|interlink|load|<comma list>] [--capture] [--keep] [-- PYTEST_ARGS...]
#   scripts/run.sh --down
#
#   -m MARKERS         pytest marker expression (default: rig)
#   --profile allstar  core plus the AllStar node; sets DVT_SUT_USRP=on
#   --profile nxdn     core plus NXDNReflectors nxdn-ref/nxdn-sut peered with
#                      xlxd-ref/xlxd-sut (DVT_NXDN=on); combine: allstar,nxdn
#   --profile p25      core plus P25Reflectors p25-ref/p25-sut peered with
#                      xlxd-ref/xlxd-sut (DVT_P25=on); e.g. allstar,nxdn,p25
#   --profile urfd     URFd (.70, no transcoder); DVT_REF_KIND=urfd also makes it
#                      the reference the cases compare with (compat data,
#                      xlxd profile only; scripts/compat-table.py)
#   --profile mrefd    mrefd (.80, M17-REF, interlink peers M17-DVT/M17-DVU at
#                      the tester); DVT_REF_KIND=mrefd makes it the reference
#                      the M17 cases compare with (compat data, xlxd profile only)
#   --profile interlink  XLX998 peers xlxd-peer-sut/-ref interlinked with
#                      xlxd-sut/-ref on DVT_INTERLINK_MODULES (default DE)
#   --profile load     DVT_LOAD_MODULES (1..26) modules, DVT_LOAD_NODES (0..26,
#                      default = modules) load-node replicas against xlxd-sut,
#                      first round at DVT_LOAD_START_AT (now + 30 s);
#                      DVT_SUT_USRP=on; host samples docker stats every ~1-2 s
#                      into out/<utc>/stats.jsonl while pytest runs
#   --profile g3       a second Icom G3 terminal: relay g3-term2 at .31 (DVT_G3=on)
#   --profile browser  the Playwright browser service (browser, .91, ws://<net>.91:3000/:
#                      chromium, firefox, WebKit of the pinned official Playwright image),
#                      used together with web (--profile web,browser; exit 2 without it);
#                      the tester gets DVT_BROWSER=on (docs/rig.md "Browser profile");
#                      out/<utc>/media is mounted read-only at /media of the browser service
#                      (the fake microphone file of the talk cases)
#   --profile web      DV-Router's web service behind a TLS proxy: web-proxy (.90,
#                      https://dvt-web, CA out/<utc>/web/ca.pem), a persistent state
#                      dir out/<utc>/sut-state for the sut and a random operator
#                      login (out/<utc>/web/operator.env, mode 600, never printed);
#                      needs DVT_SUT_LAUNCH=native; adds rig/config/dv-router/web.toml
#                      and admin.toml (admin host https://dvt-admin:8443, logins in
#                      out/<utc>/web/admin.env) to DVT_SUT_CONFIG_EXTRA by itself;
#                      the tester gets DVT_WEB=on
#                      (docs/rig.md "Web profile"). With rig/config/dv-router/recorder.toml in
#                      DVT_SUT_CONFIG_EXTRA the recorder is on too: host directory
#                      out/<utc>/recorder mounted read-write at /var/lib/dv-recorder (DVT_RECORDER=on)
#   --profile nat      NAT behaviour of the browser path (docs/rig.md "NAT profile"), used together with
#                      web and browser (--profile web,browser,nat; exit 2 otherwise): nat-router (.92,
#                      outside, alias .93) between the rig network and the internal network nat-inside
#                      (DVT_NAT_NET, default 172.31.0) where the browser lives alone (rig/compose.nat.yml),
#                      nat-edge (probe in the browser's namespace), reflectors nat-ref-a/-b (.95/.96) and
#                      coturn (.94, the image COTURN_IMAGE of .env.example; run.sh never pulls it). The
#                      tester reaches the browser server at <net>.92:3000 and gets DVT_NAT=on. run.sh makes
#                      the per-run TURN secret out/<utc>/web/turn.secret (mode 600, never printed) and
#                      coturn's config out/<utc>/coturn/turnserver.conf (mode 600). With
#                      rig/config/dv-router/turn.toml in DVT_SUT_CONFIG_EXTRA the SUT uses that secret as
#                      [web] turn_secret_file (mounted read-only at /run/secrets/turn-secret, DVT_TURN=on);
#                      without it GET /api/v1/rtc/ice answers 404. After the run the output directory is
#                      scanned for the secret: a hit fails the run
#   DVT_XLXD_BLACKLIST callsigns written to both reflectors' xlxd.blacklist
#                      (default empty; P-G3-05 uses N0G3X, no other suite does)
#   --capture          sniffers on xlxd-ref/xlxd-sut/transcoder-sut (+allstar/nxdn/p25): pcapng in
#                      out/<utc>/capture/<service>.pcapng (decode: scripts/tshark.sh)
#   --netem SPEC       impair the tester's egress: a preset (lan, good_internet,
#                      mobile_3g, bad_wifi; .env.example), "off" or netem
#                      arguments ("delay 100ms loss 1%"); DVT_NETEM_XLXD does the
#                      same for xlxd-ref and xlxd-sut alike; counters in netem.txt
#   --keep             leave the rig running (default: compose down at the end)
#   --down             take the lock, bring every profile down, exit (the only
#                      cleanup for a --keep rig)
#
# Instances: DVT_INSTANCE=<n> (1..99; default 0 = project dvt, net DVT_NET)
# uses project dvt<n>, network <a.b>.<n>.0/24 (first two octets of DVT_NET),
# lock .cache/rig<n>.lock and out/<utc>-i<n>. Images are shared; builds are
# serialized by .cache/build.lock. Instances take turns: the run holds the
# scope dvt-rig.scope (below), so only ONE rig runs at a time on the host.
#
# Resource limits (README, "Resource limits"): run.sh re-executes itself inside
# a capped systemd scope (scripts/contained.sh --name rig: DVT_RIG_MEM, default
# 12G memory, swap 0, DVT_RIG_CPU percent CPU, 1024 tasks, DVT_BUILD_JOBS
# compiler jobs) and waits while another rig or heavy job holds the host.
# Every compose service carries mem/swap/cpus/pids limits; run.sh checks that
# before "up" (dvtest/limits.py) and exits 4 for an uncapped service.
# DVT_UNCONTAINED=1 skips the scope (only when something else already caps the
# run, e.g. a CI runner without a user systemd bus).
#
# One run at a time per instance: the whole up..down span holds an flock on .cache/rig.lock;
# a second caller waits. If project containers are already up (a --keep rig),
# run.sh refuses with exit 3; their owner cleans up with run.sh --down.
#
# Settings: .env.example, then .env, then .local/rig.env (scripts/lib/env.sh);
# TRANSCODER_IMAGE must be set in .env or the shell; image-specific settings go
# into .local/compose.override.yml (COMPOSE_OVERRIDE), added with -f if present.
# Results: out/<utc>/ (pytest junit + log, compose ps/images, one log per service).
#
# DVT_TRANSCODER_FAKE=1: transcoder-sut is the fake extended AMBEd
# (dvtest/fake_ambed.py, docs/ambed-fake.md) instead of a real transcoder. run.sh
# builds rig/Dockerfile.ambed-fake (compose service ambed-fake-build, build only,
# never started) and sets TRANSCODER_SUT_IMAGE to it; transcoder-ref stays
# TRANSCODER_IMAGE (xlxd-ref needs the real one). The tester sees
# DVT_TRANSCODER_FAKE=1 and reaches the JSON control socket at <net>.21:10190
# (cases/crossmode_fake.py); the wire cases (-m wire) are not meant for it.
#
# DVT_SUT_LAUNCH selects how a prebuilt XLXD_SUT_IMAGE starts: direct (default,
# its own entrypoint, argv "XLX999 OWN_IP TRANSCODER_IP"), import (through the
# dv-xlxd-import shim) or native (DV-Router with its own TOML). native needs
# DVT_SUT_CONFIG=<path to a dv-router.toml TEMPLATE> (exit 2 when missing or
# unreadable). run.sh renders it into out/<utc>/xlxd-config/xlxd-sut/dv-router.toml
# (python3 -m dvtest.sut_config), replacing <OWN_IP> (<net>.11), <TRANSCODER_IP>
# (<net>.21), <TESTER_IP> (<net>.30) and <NET> (the /24 prefix, e.g. 172.30.0);
# any other <UPPER_CASE> token left over is an error (exit 2). The file is
# mounted read-only at /etc/dv-router/dv-router.toml and the image starts as
# "--config /etc/dv-router/dv-router.toml XLX999 <net>.11 <net>.21" (no importer).
# The /xlxd file mounts and the DV_* env stay (the template's [ids] reads
# /xlxd/dmrid.dat and nxdn.csv; the env pins win over the file).
# DVT_SUT_CONFIG_EXTRA=<file>[:<file>...] appends repo-owned snippets (rig/config/dv-router/)
# after the rendered template, with the same tokens plus <REQUIRE_IDENT> (env
# DVT_REQUIRE_IDENT, true or false, default true) and <WEB_PROXY_IP> (<net>.90);
# a plain [table] the template already has is merged key by key; docs/rig.md.
# The launcher reads no other xlxd.* file: DVT_NXDN, DVT_P25, DVT_INTERLINK, DVT_SUT_USRP and
# DVT_XLXD_BLACKLIST only draw a warning, and DVT_UNSERVED_MODULE=M|N is refused
# (exit 2; a template may name peers on other modules too).
#
# Control channel for cases that stop a backend (tester has no docker socket):
# while pytest runs, a host watcher polls out/<utc>/control/ ($DVT_OUT/control
# in the tester, DVT_RIG_CONTROL=1). A request is a file <id>.req (write .tmp,
# then rename) with one line "<action> <service>":
#   action  stop | start | restart | kill (SIGKILL)
#   service transcoder-ref | transcoder-sut | allstar | xlxd-ref | xlxd-sut
# or, with netem on: "netem tester|xlxd <preset|off|netem arguments>".
# or, with the allstar profile: "asl usrp-show" (chan_usrp counters into
# <id>.out) or "asl play-1901|play-1902" (app_rpt plays a short telemetry
# file on that node, i.e. keys its USRP channel toward xlxd-sut).
# or "kill-proc xlxd-sut <process> <signal>": signal one process INSIDE the sut
# container by exact /proc comm (scripts/lib/killproc.sh): dv-edge-xlx,
# dv-edge-nxdn, dv-edge-p25 with KILL or USR1 (quiet restart), dv-codec-ambed and dv-svc-web
# with KILL only (the web cases reset its in-memory limits); never dv-router; anything
# else is refused. <id>.out gets "pid=<n>" per process and "t_kill=<epoch s>";
# exit 3 (error 3) when no process has that name.
# or "metrics xlxd-sut" (no other word): <id>.out gets the Prometheus text of the
# sut hub, fetched from inside its network namespace by a throwaway tester-image
# container (scripts/lib/metrics.sh; the DV-Router image has no shell or curl and
# the hub listens on its loopback only). error 1 = the GET failed.
# "mark begin|end <tag>" only logs: a case brackets deliberately malformed
# traffic so the capture check can tell it from a fault.
# The answer is <id>.ack: "ok", "error <rc>" or "error refused" (anything else
# is refused and nothing runs); the request is renamed to <id>.done.
# control/control.log gets "<utc> request <line>" before the action and
# "<utc> <line> -> <rc>" after it.
#
# With --capture, scripts/dvcheck.sh checks out/<utc>/ after the rig is down
# (out/<utc>/check.txt); its non-zero exit fails the run when pytest passed.
set -euo pipefail

. "$(dirname "$0")/lib/env.sh"
. "$(dirname "$0")/lib/killproc.sh"
. "$(dirname "$0")/lib/metrics.sh"
dvt_load_env
cd "$DVT_ROOT"

orig_args=("$@")
markers="rig"
profile="core"
keep=0
capture=0
netem_set=0
netem_cli=""
down_only=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -m) [ "$#" -ge 2 ] || { echo "run.sh: -m needs a value" >&2; exit 2; }; markers="$2"; shift 2 ;;
    --profile) [ "$#" -ge 2 ] || { echo "run.sh: --profile needs a value" >&2; exit 2; }; profile="$2"; shift 2 ;;
    --keep) keep=1; shift ;;
    --capture) capture=1; shift ;;
    --netem) [ "$#" -ge 2 ] || { echo "run.sh: --netem needs a preset or netem arguments" >&2; exit 2; }; netem_cli="$2"; netem_set=1; shift 2 ;;
    --down) down_only=1; shift ;;
    --) shift; break ;;
    -h|--help) sed -n '3,123p' "$0"; exit 0 ;;
    *) echo "run.sh: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
done

# Containment: the whole run (docker CLI, pytest pipe, watcher, sampler,
# capture check) executes in the capped scope dvt-rig.scope. --down is only
# cleanup and needs no scope. The docker daemon starts the containers, so their
# caps are the per-service limits in rig/compose.yml.
if [ "$down_only" != 1 ] && [ -z "${DVT_CONTAINED:-}" ]; then
  if [ "${DVT_UNCONTAINED:-}" = 1 ]; then
    echo "run.sh: warning: DVT_UNCONTAINED=1, running without the contained.sh scope" >&2
  else
    exec "$DVT_ROOT/scripts/contained.sh" --name rig --mem "${DVT_RIG_MEM:-12G}" \
      --cpu "${DVT_RIG_CPU:-800}" --jobs "${DVT_BUILD_JOBS:-4}" -- \
      "$DVT_ROOT/scripts/run.sh" "${orig_args[@]}"
  fi
fi

# DVT_SUBNET (a /24), when set, wins over DVT_NET.
if [ -n "${DVT_SUBNET:-}" ]; then
  case "$DVT_SUBNET" in
    *.0/24) export DVT_NET="${DVT_SUBNET%.0/24}" ;;
    *) echo "run.sh: DVT_SUBNET must be a /24 like 172.30.0.0/24" >&2; exit 2 ;;
  esac
fi
# DVT_NO_RADIO_ID: the sut's policy for a talker without a radio id (unset = its default)
case "${DVT_NO_RADIO_ID:-}" in
  ''|drop|fallback|local) ;;
  *) echo "run.sh: DVT_NO_RADIO_ID must be drop, fallback or local" >&2; exit 2 ;;
esac
# DVT_USRP_DYNAMIC=1: a prebuilt sut runs its dynamic USRP hub (DV_USRP_DYNAMIC)
case "${DVT_USRP_DYNAMIC:-}" in
  ''|1) ;;
  *) echo "run.sh: DVT_USRP_DYNAMIC must be 1 or unset" >&2; exit 2 ;;
esac
# DVT_SUT_LAUNCH: how a prebuilt xlxd-sut image starts. direct = its own
# entrypoint with "CALLSIGN OWN_IP TRANSCODER_IP"; import = through the
# converter shim dv-xlxd-import --exec (DV-Router), same three words;
# native = "--config FILE XLX999 OWN_IP TRANSCODER_IP" with FILE rendered from
# the template DVT_SUT_CONFIG (header; the render is checked after the subnet
# is known, below).
case "${DVT_SUT_LAUNCH:-direct}" in
  direct) ;;
  import) [ -n "${XLXD_SUT_IMAGE:-}" ] \
            || { echo "run.sh: DVT_SUT_LAUNCH=import needs XLXD_SUT_IMAGE (a prebuilt image with the shim)" >&2; exit 2; } ;;
  native) [ -n "${XLXD_SUT_IMAGE:-}" ] \
            || { echo "run.sh: DVT_SUT_LAUNCH=native needs XLXD_SUT_IMAGE (a prebuilt DV-Router image)" >&2; exit 2; }
          [ -n "${DVT_SUT_CONFIG:-}" ] \
            || { echo "run.sh: DVT_SUT_LAUNCH=native needs DVT_SUT_CONFIG=<path to a dv-router.toml template>" >&2; exit 2; }
          [ -f "$DVT_SUT_CONFIG" ] && [ -r "$DVT_SUT_CONFIG" ] \
            || { echo "run.sh: DVT_SUT_CONFIG '$DVT_SUT_CONFIG' is not a readable file" >&2; exit 2; } ;;
  *) echo "run.sh: DVT_SUT_LAUNCH must be direct, import or native" >&2; exit 2 ;;
esac
# DVT_TRANSCODER_FAKE=1: transcoder-sut runs the fake extended AMBEd (header)
case "${DVT_TRANSCODER_FAKE:-}" in
  ''|1) ;;
  *) echo "run.sh: DVT_TRANSCODER_FAKE must be 1 or unset" >&2; exit 2 ;;
esac
if [ "${DVT_TRANSCODER_FAKE:-}" = 1 ]; then
  export DVT_TRANSCODER_FAKE TRANSCODER_SUT_IMAGE="dvt-ambed-fake:local"
fi
instance="${DVT_INSTANCE:-0}"
[[ "$instance" =~ ^([0-9]|[1-9][0-9])$ ]] || { echo "run.sh: DVT_INSTANCE must be 0..99" >&2; exit 2; }
lock=".cache/rig.lock"
suffix=""
if [ "$instance" != 0 ]; then
  export COMPOSE_PROJECT_NAME="dvt$instance"
  net="${DVT_NET:-172.30.0}"
  export DVT_NET="${net%.*}.$instance"
  lock=".cache/rig$instance.lock"
  suffix="-i$instance"
fi
export DVT_SUBNET="${DVT_NET:-172.30.0}.0/24"
# profile nat: the network behind the NAT router; an instance replaces its third octet like the rig network's
export DVT_NAT_NET="${DVT_NAT_NET:-172.31.0}"
[ "$instance" = 0 ] || export DVT_NAT_NET="${DVT_NAT_NET%.*}.$instance"
# DVT_REQUIRE_IDENT (true | false, default true): value of the <REQUIRE_IDENT> token
# of the native config (rig/config/dv-router/data-ident.toml) and the setting the
# tester's data-ident cases expect
case "${DVT_REQUIRE_IDENT:-true}" in
  true|false) ;;
  *) echo "run.sh: DVT_REQUIRE_IDENT must be true or false" >&2; exit 2 ;;
esac
# --profile web: dv-svc-web is DV-Router's, so the launch is native and the snippet
# rig/config/dv-router/web.toml is part of the config (added unless already listed)
has_web=0
has_recorder=0
case ",$profile," in *,web,*) has_web=1 ;; esac
if [ "$has_web" = 1 ]; then
  [ "${DVT_SUT_LAUNCH:-direct}" = native ] \
    || { echo "run.sh: --profile web needs DVT_SUT_LAUNCH=native (the web service is DV-Router's)" >&2; exit 2; }
  case ":${DVT_SUT_CONFIG_EXTRA:-}:" in
    *:rig/config/dv-router/web.toml:*) ;;
    *) DVT_SUT_CONFIG_EXTRA="${DVT_SUT_CONFIG_EXTRA:+$DVT_SUT_CONFIG_EXTRA:}rig/config/dv-router/web.toml" ;;
  esac
  # the admin service (dv-svc-admin, host dvt-admin:8443 behind the same proxy) is always on in the web profile
  case ":${DVT_SUT_CONFIG_EXTRA:-}:" in
    *:rig/config/dv-router/admin.toml:*) ;;
    *) DVT_SUT_CONFIG_EXTRA="$DVT_SUT_CONFIG_EXTRA:rig/config/dv-router/admin.toml" ;;
  esac
  export DVT_SUT_CONFIG_EXTRA
  # rig/config/dv-router/recorder.toml named in DVT_SUT_CONFIG_EXTRA switches the recorder on: its
  # directory is a host directory out/<run>/recorder (separate from the state dir), mounted read-write
  case ":${DVT_SUT_CONFIG_EXTRA:-}:" in
    *:rig/config/dv-router/recorder.toml:*) has_recorder=1 ;;
  esac
fi
# --profile nat: needs web and browser; rig/config/dv-router/turn.toml in DVT_SUT_CONFIG_EXTRA switches the SUT's TURN on
has_nat=0
has_turn=0
case ",$profile," in *,nat,*) has_nat=1 ;; esac
case ":${DVT_SUT_CONFIG_EXTRA:-}:" in *:rig/config/dv-router/turn.toml:*) has_turn=1 ;; esac
if [ "$has_nat" = 1 ]; then
  case ",$profile," in
    *,web,*,browser,*|*,browser,*,web,*|*,web,browser,*|*,browser,web,*) ;;
    *) echo "run.sh: --profile nat needs --profile web,browser,nat (the NAT router sits in front of the browsers)" >&2; exit 2 ;;
  esac
fi
if [ "$has_turn" = 1 ] && [ "$has_nat" = 0 ]; then
  echo "run.sh: rig/config/dv-router/turn.toml needs --profile nat (coturn and the secret file come with it)" >&2; exit 2
fi
# DVT_SUT_CONFIG_EXTRA=<file>[:<file>...]: repo-owned snippets (rig/config/dv-router/)
# appended after the rendered template, token substituted like the template
cfg_opts=(--require-ident "${DVT_REQUIRE_IDENT:-true}")
if [ -n "${DVT_SUT_CONFIG_EXTRA:-}" ]; then
  IFS=: read -r -a cfg_extra_files <<< "$DVT_SUT_CONFIG_EXTRA"
  for f in "${cfg_extra_files[@]}"; do
    [ -z "$f" ] || cfg_opts+=(--extra "$f")
  done
  if [ "${DVT_SUT_LAUNCH:-direct}" != native ]; then
    echo "run.sh: warning: DVT_SUT_CONFIG_EXTRA is set but only DVT_SUT_LAUNCH=native reads a config file" >&2
  fi
fi
# native launch: refuse a template with an unknown token before anything starts
if [ "${DVT_SUT_LAUNCH:-direct}" = native ]; then
  python3 -m dvtest.sut_config "$DVT_SUT_CONFIG" - --net "${DVT_NET:-172.30.0}" "${cfg_opts[@]}" || exit 2
  # the template's M17 peer rows name modules M and N: the sut would exit 2 without them
  case "${DVT_UNSERVED_MODULE:-}" in
    M|N) echo "run.sh: DVT_UNSERVED_MODULE=$DVT_UNSERVED_MODULE is refused with DVT_SUT_LAUNCH=native (the peer rows need M and N)" >&2; exit 2 ;;
  esac
fi
# DVT_UNSERVED_MODULE (P-M17-03, P-DXT-03, P-DCS-04): one module a prebuilt
# xlxd-sut must not serve (DV_ROOMS); the tester gets the letter
if [ -n "${DVT_UNSERVED_MODULE:-}" ] && ! [[ "$DVT_UNSERVED_MODULE" =~ ^[A-Z]$ ]]; then
  echo "run.sh: DVT_UNSERVED_MODULE must be one letter A-Z" >&2; exit 2
fi

# Serialize users of this instance: project and subnet are shared. The lock
# is released when this script exits, after compose down.
take_lock() {
  mkdir -p .cache
  exec 9>> "$lock"
  if ! flock -n 9; then
    echo "run.sh: waiting for rig lock $lock held by pid $(head -n 1 "$lock" 2>/dev/null || echo '?')"
    flock 9
  fi
  printf '%s\n' "$$" > "$lock"
}
# Every profile, so "down" also removes allstar/capture containers
dvt_env_files
compose_files=(-f rig/compose.yml)
if [ -n "${COMPOSE_OVERRIDE:-}" ] && [ -f "$COMPOSE_OVERRIDE" ]; then
  compose_files+=(-f "$COMPOSE_OVERRIDE")
fi
compose_all=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" "${compose_files[@]}" "${DVT_ENV_FILES[@]}"
  --profile core --profile allstar --profile nxdn --profile capture --profile capture-allstar
  --profile capture-nxdn --profile p25 --profile capture-p25 --profile interlink
  --profile capture-interlink --profile load --profile netem --profile g3
  --profile urfd --profile capture-urfd --profile mrefd --profile capture-mrefd --profile web --profile browser
  --profile nat)

if [ "$down_only" = 1 ]; then
  take_lock
  "${compose_all[@]}" down --remove-orphans
  exit 0
fi

profiles=(--profile core)
has_allstar=0
has_nxdn=0
has_p25=0
has_interlink=0
has_urfd=0
has_mrefd=0
has_load=0
IFS=, read -r -a wanted <<< "$profile"
for p in "${wanted[@]}"; do
  case "$p" in
    core) ;;
    allstar)
      has_allstar=1
      profiles+=(--profile allstar)
      export DVT_SUT_USRP="${DVT_SUT_USRP:-on}"
      [ -f .local/rig.env ] || { echo "run.sh: .local/rig.env missing; run scripts/gen-local.sh" >&2; exit 2; }
      ;;
    nxdn)
      has_nxdn=1
      profiles+=(--profile nxdn)
      export DVT_NXDN=on
      [ -f .cache/nxdnclients/NXDNReflector/Makefile ] \
        || { echo "run.sh: .cache/nxdnclients missing; run scripts/pre-fetch-nxdn.sh" >&2; exit 2; }
      ;;
    p25)
      has_p25=1
      profiles+=(--profile p25)
      export DVT_P25=on
      [ -f .cache/p25clients/P25Reflector/Makefile ] \
        || { echo "run.sh: .cache/p25clients missing; run scripts/pre-fetch-p25.sh" >&2; exit 2; }
      ;;
    interlink)
      has_interlink=1
      profiles+=(--profile interlink)
      export DVT_INTERLINK=on
      export XLXD_PEER_DIR="${XLXD_PEER_DIR:-${XLXD_REF_DIR:-ref}}"
      [ -f ".cache/xlxd/$XLXD_PEER_DIR/src/main.h" ] \
        || { echo "run.sh: .cache/xlxd/$XLXD_PEER_DIR missing; run scripts/pre-fetch-xlxd.sh" >&2; exit 2; }
      ;;
    load)
      profiles+=(--profile load)
      export DVT_LOAD=on
      has_load=1
      # module C of xlxd-sut is a USRP endpoint towards the tester
      export DVT_SUT_USRP="${DVT_SUT_USRP:-on}"
      [[ "${DVT_LOAD_NODES:-${DVT_LOAD_MODULES:-26}}" =~ ^([0-9]|1[0-9]|2[0-6])$ ]] \
        || { echo "run.sh: DVT_LOAD_NODES must be 0..26" >&2; exit 2; }
      [[ "${DVT_LOAD_MODULES:-26}" =~ ^([1-9]|1[0-9]|2[0-6])$ ]] \
        || { echo "run.sh: DVT_LOAD_MODULES must be 1..26" >&2; exit 2; }
      # every node starts its first round at the same time, after the rig is up
      export DVT_LOAD_START_AT="${DVT_LOAD_START_AT:-$(( $(date +%s) + ${DVT_LOAD_LEAD_S:-30} ))}"
      ;;
    g3)
      profiles+=(--profile g3)
      export DVT_G3=on
      ;;
    web)
      profiles+=(--profile web)
      export DVT_WEB=on
      ;;
    browser)
      # the browsers load the pages of the web service: it only makes sense together with web
      case ",$profile," in *,web,*) ;; *) echo "run.sh: --profile browser needs --profile web (--profile web,browser)" >&2; exit 2 ;; esac
      profiles+=(--profile browser)
      export DVT_BROWSER=on
      ;;
    nat)
      # checked above (needs web and browser); the browser moves behind the router (rig/compose.nat.yml)
      profiles+=(--profile nat)
      compose_files+=(-f rig/compose.nat.yml)
      export DVT_NAT=on DVT_BROWSER_HOST="${DVT_NET:-172.30.0}.92"
      ;;
    urfd)
      has_urfd=1
      ;;
    mrefd)
      has_mrefd=1
      ;;
    *) echo "run.sh: --profile takes core, allstar, nxdn, p25, interlink, load, g3, urfd, mrefd, web, browser, nat or a comma list" >&2; exit 2 ;;
  esac
done
# native launch reads no xlxd.* file: these settings only change the reference side
if [ "${DVT_SUT_LAUNCH:-direct}" = native ]; then
  for v in DVT_NXDN DVT_P25 DVT_INTERLINK DVT_SUT_USRP DVT_XLXD_BLACKLIST; do
    case "${!v:-}" in
      ''|off) ;;
      *) echo "run.sh: warning: $v is set but DVT_SUT_LAUNCH=native ignores it for xlxd-sut (peers come from the template)" >&2 ;;
    esac
  done
fi
# DVT_REF_KIND=urfd / mrefd: URFd (.70) or mrefd (.80) becomes the reference
# the cases compare the sut with. Such runs are compatibility data, judged as
# differential only, so they are held to the xlxd profile.
case "${DVT_REF_KIND:-xlxd}" in
  xlxd) export DVT_REF_OCTET=10 ;;
  urfd)
    has_urfd=1
    export DVT_REF_OCTET=70
    [ "${DVT_SUT_PROFILE:-xlxd}" = xlxd ] \
      || { echo "run.sh: DVT_REF_KIND=urfd runs are compatibility data: use DVT_SUT_PROFILE=xlxd" >&2; exit 2; }
    ;;
  mrefd)
    has_mrefd=1
    export DVT_REF_OCTET=80
    [ "${DVT_SUT_PROFILE:-xlxd}" = xlxd ] \
      || { echo "run.sh: DVT_REF_KIND=mrefd runs are compatibility data: use DVT_SUT_PROFILE=xlxd" >&2; exit 2; }
    ;;
  *) echo "run.sh: DVT_REF_KIND must be xlxd, urfd or mrefd" >&2; exit 2 ;;
esac
if [ "$has_urfd" = 1 ]; then
  profiles+=(--profile urfd)
  [ -f .cache/urfd/reflector/Makefile ] || { echo "run.sh: .cache/urfd missing; run scripts/pre-fetch-urfd.sh" >&2; exit 2; }
fi
if [ "$has_mrefd" = 1 ]; then
  profiles+=(--profile mrefd)
  [ -f .cache/mrefd/Makefile ] || { echo "run.sh: .cache/mrefd missing; run scripts/pre-fetch-mrefd.sh" >&2; exit 2; }
fi
# netem: a preset name (NETEM_PRESET_<NAME>), "off", or netem arguments
netem_args() {
  local spec="$1" var
  case "$spec" in ''|off|none) return 0 ;; esac
  var="NETEM_PRESET_$(printf '%s' "$spec" | tr '[:lower:]' '[:upper:]')"
  if [[ "$spec" =~ ^[a-z0-9_]+$ ]] && [ -n "${!var:-}" ]; then spec="${!var}"; fi
  [[ "$spec" =~ ^[A-Za-z0-9\ .%_-]+$ ]] || { echo "run.sh: invalid netem arguments '$spec'" >&2; return 1; }
  printf '%s' "$spec"
}
netem_svcs=()
[ "$netem_set" = 0 ] || DVT_NETEM_TESTER="$netem_cli"
if [ "$netem_set" = 1 ] || [ -n "${DVT_NETEM_TESTER:-}${DVT_NETEM_XLXD:-}" ]; then
  profiles+=(--profile netem)
  netem_svcs=(netem-tester netem-xlxd-ref netem-xlxd-sut)
  DVT_NETEM_ARGS_TESTER="$(netem_args "${DVT_NETEM_TESTER:-}")" || exit 2
  DVT_NETEM_ARGS_XLXD="$(netem_args "${DVT_NETEM_XLXD:-}")" || exit 2
  export DVT_NETEM_ARGS_TESTER DVT_NETEM_ARGS_XLXD
fi

capture_svcs=()
if [ "$capture" = 1 ]; then
  profiles+=(--profile capture)
  capture_svcs=(capture-xlxd-ref capture-xlxd-sut capture-transcoder-sut)
  # xlxd waits so the sniffers see its first transcoder handshake
  export DVT_START_DELAY="${DVT_START_DELAY:-3}"
  if [ "$has_allstar" = 1 ]; then
    profiles+=(--profile capture-allstar)
    capture_svcs+=(capture-allstar)
  fi
  if [ "$has_nxdn" = 1 ]; then
    profiles+=(--profile capture-nxdn)
    capture_svcs+=(capture-nxdn-ref capture-nxdn-sut)
  fi
  if [ "$has_p25" = 1 ]; then
    profiles+=(--profile capture-p25)
    capture_svcs+=(capture-p25-ref capture-p25-sut)
  fi
  if [ "$has_interlink" = 1 ]; then
    profiles+=(--profile capture-interlink)
    capture_svcs+=(capture-xlxd-peer-sut)
  fi
  if [ "$has_urfd" = 1 ]; then
    profiles+=(--profile capture-urfd)
    capture_svcs+=(capture-urfd)
  fi
  if [ "$has_mrefd" = 1 ]; then
    profiles+=(--profile capture-mrefd)
    capture_svcs+=(capture-mrefd)
  fi
fi

if [ -z "${TRANSCODER_IMAGE:-}" ]; then
  echo "run.sh: TRANSCODER_IMAGE is not set; set it in .env (TRANSCODER_IMAGE=<your-ambed-image>)" >&2
  exit 2
fi

xlxd_dirs=()
[ -n "${XLXD_REF_IMAGE:-}" ] || xlxd_dirs+=("${XLXD_REF_DIR:-ref}")
[ -n "${XLXD_SUT_IMAGE:-}" ] || xlxd_dirs+=("${XLXD_SUT_DIR:-sut}")
for name in "${xlxd_dirs[@]}"; do
  [ -f ".cache/xlxd/$name/src/main.h" ] || { echo "run.sh: .cache/xlxd/$name missing; run scripts/pre-fetch-xlxd.sh" >&2; exit 2; }
done
[ -f .cache/codec2/CMakeLists.txt ] || { echo "run.sh: .cache/codec2 missing; run scripts/pre-fetch-codec2.sh" >&2; exit 2; }
# A prebuilt reflector image must exist locally: run.sh never builds or pulls it
for img in "${XLXD_REF_IMAGE:-}" "${XLXD_SUT_IMAGE:-}"; do
  [ -z "$img" ] || docker image inspect "$img" > /dev/null 2>&1 || {
    echo "run.sh: image '$img' (XLXD_REF_IMAGE/XLXD_SUT_IMAGE) not found locally; build or load it first" >&2
    exit 2
  }
done

scripts/pii-check.sh

take_lock
# Containers that are up while we hold the lock belong to a --keep rig (or a
# manual compose up) that someone may still use: never take it over.
if [ -n "$(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" ps -a -q 2>/dev/null)" ]; then
  echo "run.sh: project ${COMPOSE_PROJECT_NAME:-dvt} already has containers (a --keep rig?);" \
       "its owner must clean up first with scripts/run.sh --down" >&2
  exit 3
fi

DVT_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
DVT_UID="$(id -u)"
DVT_GID="$(id -g)"
DVT_RUN_ID="$DVT_RUN_ID$suffix"
export DVT_RUN_ID DVT_UID DVT_GID
out="out/$DVT_RUN_ID"
mkdir -p "$out/logs"
[ "$capture" = 0 ] || mkdir -p "$out/capture"

# --profile web: the sut's persistent state dir (host directory, mounted at /var/lib/dv,
# mode 0700, owned by the host user the sut container runs as) with the bootstrap secret
# of the first operator, and the same login for the tester. The password is random per
# run and goes only into those two files (mode 0600), never to a log or an argument.
if [ "$has_web" = 1 ]; then
  mkdir -p "$out/web"
  (umask 077; mkdir -p "$out/sut-state/secrets")
  chmod 700 "$out/sut-state" "$out/sut-state/secrets"
  # recorder dir (only with recorder.toml): host directory, own mount, never inside sut-state; the
  # sut container runs as the host uid so the files stay removable
  if [ "$has_recorder" = 1 ]; then
    (umask 077; mkdir -p "$out/recorder")
    chmod 700 "$out/recorder"
    export DVT_RECORDER=on
  fi
  # media dir: the tester writes the fake microphone file here at run time (never committed), the
  # browser service mounts it read-only (cases/browser_fixtures.py, dvtest/browser.py)
  mkdir -p "$out/media"
  web_pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  (umask 077
   printf 'N0CALL:%s\n' "$web_pw" > "$out/sut-state/secrets/web-bootstrap"
   printf 'DVT_WEB_OPERATOR=N0CALL\nDVT_WEB_OPERATOR_PASSWORD=%s\n' "$web_pw" > "$out/web/operator.env")
  unset web_pw
  # admin accounts (admin, operator, viewer; random passwords, pure-python argon2id hashes in the users
  # file, no totp field: each enrols at its first login); logins for the tester in web/admin.env
  (cd "$DVT_ROOT" && PYTHONPATH="$DVT_ROOT" python3 -m dvtest.tools.admin_users \
    --users-file "$DVT_ROOT/$out/sut-state/admin/users" --env-file "$DVT_ROOT/$out/web/admin.env") \
    || { echo "run.sh: could not write the admin users file" >&2; exit 2; }
  # --profile nat: the per-run TURN secret (also coturn's static-auth-secret) and coturn's private config, both
  # mode 600 and written by dvtest.turn (never printed). The SUT mounts the secret only with turn.toml.
  if [ "$has_nat" = 1 ]; then
    mkdir -p "$out/coturn"
    chmod 700 "$out/coturn"
    (cd "$DVT_ROOT" && PYTHONPATH="$DVT_ROOT" python3 -m dvtest.turn make --secret-file "$DVT_ROOT/$out/web/turn.secret" \
       --conf "$DVT_ROOT/$out/coturn/turnserver.conf" --listen-ip "${DVT_NET:-172.30.0}.94" --allowed-peer "${DVT_NET:-172.30.0}.11") \
      || { echo "run.sh: could not write the TURN secret and coturn config" >&2; exit 2; }
    [ "$has_turn" = 0 ] || export DVT_TURN=on
    echo "run.sh: nat profile: TURN secret in $out/web/turn.secret (mode 600, turn $([ "$has_turn" = 1 ] && echo on || echo off) for the SUT), coturn $COTURN_IMAGE"
  fi
  echo "run.sh: web profile: operator login in $out/web/operator.env, admin logins in $out/web/admin.env (mode 600), CA will be $out/web/ca.pem"
fi

# Prebuilt reflector images (XLXD_REF_IMAGE / XLXD_SUT_IMAGE): another
# implementation can be the system under test. Its contract: argv
# "CALLSIGN OWN_IP TRANSCODER_IP"; config files mounted read-only under /xlxd/
# (rendered here on the host by rig/xlxd-entry.sh); log on stdout.
prebuilt=()
override="$out/xlxd-image.override.yml"
# All modules A-Z except $1, as DV_ROOMS ranges: Z -> A-Y, M -> A-L,N-Z, A -> B-Z
dvt_rooms_without() {
  local all=ABCDEFGHIJKLMNOPQRSTUVWXYZ part out=""
  for part in "${all%%"$1"*}" "${all#*"$1"}"; do
    [ -n "$part" ] || continue
    [ "${#part}" -eq 1 ] || part="${part:0:1}-${part: -1}"
    out="${out:+$out,}$part"
  done
  printf '%s' "$out"
}
render_xlxd() {   # $1 service, $2 image, $3 own, $4 transcoder, $5 usrp, $6 nxdn, $7 p25, $8 xlxd peer, $9 xlxd peer2
  local svc="$1" img="$2" net="${DVT_NET:-172.30.0}" cfg="$DVT_ROOT/$out/xlxd-config/$1" f p
  mkdir -p "$cfg"
  DVT_RENDER_DIR="$cfg" DVT_USRP_TEMPLATE=rig/config/xlxd.usrp.in DVT_USRP="$5" DVT_NET="$net" \
    DVT_NXDN_PEER="$net.$6" DVT_P25_PEER="$net.$7" DVT_INTERLINK_PEER="$net.$8" \
    DVT_INTERLINK_CALLSIGN=XLX998 DVT_INTERLINK_MODULES="${DVT_INTERLINK_MODULES:-DE}" \
    DVT_INTERLINK2_PEER="$net.$9" DVT_INTERLINK2_CALLSIGN=XLX997 DVT_INTERLINK_TESTER="$net.30" \
    DVT_NXDN_TG="${DVT_NXDN_TG:-31672}" DVT_NXDN_MODULE="${DVT_NXDN_MODULE:-F}" \
    DVT_NXDN_PROBE_PEER="$net.30:$([ "$1" = xlxd-ref ] && echo 41410 || echo 41411)" \
    DVT_NXDN_PROBE_TG="${DVT_NXDN_PROBE_TG:-31673}" DVT_NXDN_PROBE_MODULE="${DVT_NXDN_PROBE_MODULE:-I}" \
    DVT_P25_TG="${DVT_P25_TG:-31672}" DVT_P25_MODULE="${DVT_P25_MODULE:-G}" \
    sh rig/xlxd-entry.sh > "$cfg/render.log"
  cp rig/config/dmrid.dat rig/config/nxdn.csv "$cfg/"
  if [ "$svc" = xlxd-sut ] && [ "${DVT_SUT_LAUNCH:-direct}" = native ]; then
    python3 -m dvtest.sut_config "$DVT_SUT_CONFIG" "$cfg/dv-router.toml" --net "$net" --own "$3" --transcoder "$4" --tester 30 \
      "${cfg_opts[@]}" || exit 2
  fi
  for f in xlxd.whitelist xlxd.blacklist xlxd.terminal ysfnode.dat; do : > "$cfg/$f"; done
  [ -z "${DVT_XLXD_BLACKLIST:-}" ] || printf '%s\n' $DVT_XLXD_BLACKLIST > "$cfg/xlxd.blacklist"
  {
    if [ "$svc" = xlxd-sut ] && [ "${DVT_SUT_LAUNCH:-direct}" = native ]; then
      # the image's own entrypoint (dv-router) takes a TOML file; no importer
      printf '  %s:\n    image: "%s"\n' "$svc" "$img"
      printf '    command: ["--config", "/etc/dv-router/dv-router.toml", "XLX999", "%s.%s", "%s.%s"]\n' "$net" "$3" "$net" "$4"
    elif [ "$svc" = xlxd-sut ] && [ "${DVT_SUT_LAUNCH:-direct}" = import ]; then
      # the shim converts the mounted /xlxd files, then execs dv-router (same three words)
      printf '  %s:\n    image: "%s"\n    entrypoint: ["/usr/local/bin/dv-xlxd-import"]\n' "$svc" "$img"
      printf '    command: ["--exec", "--dir", "/xlxd", "XLX999", "%s.%s", "%s.%s", "--", "/usr/local/bin/dv-router"]\n' \
        "$net" "$3" "$net" "$4"
    else
      printf '  %s:\n    image: "%s"\n    command: ["XLX999", "%s.%s", "%s.%s"]\n' "$svc" "$img" "$net" "$3" "$net" "$4"
    fi
    # the image is used as it is: no build section, never pulled
    printf '    build: !reset null\n    pull_policy: never\n'
    # --profile web: the sut writes the persistent state dir of the host user
    if [ "$svc" = xlxd-sut ] && [ "$has_web" = 1 ]; then
      printf '    user: "%s:%s"\n' "$DVT_UID" "$DVT_GID"
    fi
    printf '    environment:\n      DVT_CONFIG_MOUNTED: "1"\n'
    # P25 gateway id for unknown talkers (P-P25-23; not in rig/config/dmrid.dat)
    [ "$svc" != xlxd-sut ] || printf '      DV_P25_GATEWAY_ID: "%s"\n' "${DV_P25_GATEWAY_ID:-1023000}"
    [ "$svc" != xlxd-sut ] || printf '      DV_DMRPLUS_GATEWAY_ID: "1023000"\n'
    # DVT_NO_RADIO_ID: no-radio-id policy of every radio-id protocol (unset = not written; xlxd ignores it)
    if [ "$svc" = xlxd-sut ] && [ -n "${DVT_NO_RADIO_ID:-}" ]; then
      for p in MMDVM DMRPLUS NXDN P25; do printf '      DV_%s_NO_RADIO_ID: "%s"\n' "$p" "$DVT_NO_RADIO_ID"; done
    fi
    # DVT_USRP_IDLE_TIMEOUT_S: dynamic USRP idle timeout of the sut (unset = not written; xlxd ignores it)
    [ "$svc" != xlxd-sut ] || [ -z "${DVT_USRP_IDLE_TIMEOUT_S:-}" ] ||
      printf '      DV_USRP_IDLE_TIMEOUT_S: "%s"\n' "$DVT_USRP_IDLE_TIMEOUT_S"
    # DVT_USRP_DYNAMIC=1: the sut runs its dynamic USRP hub (unset = not written; xlxd ignores it)
    [ "$svc" != xlxd-sut ] || [ "${DVT_USRP_DYNAMIC:-}" != 1 ] || printf '      DV_USRP_DYNAMIC: "1"\n'
    # P25 clients of a DV-Router sut link to its one served module on the rig's TG (xlxd ignores these)
    [ "$svc" != xlxd-sut ] || printf '      DV_P25_SERVER_MODULE: "%s"\n      DV_P25_SERVER_TG: "%s"\n' \
      "${DVT_P25_MODULE:-G}" "${DVT_P25_TG:-31672}"
    # DVT_UNSERVED_MODULE: the sut serves every module but that one (xlxd ignores DV_ROOMS)
    [ "$svc" != xlxd-sut ] || [ -z "${DVT_UNSERVED_MODULE:-}" ] ||
      printf '      DV_ROOMS: "%s"\n' "$(dvt_rooms_without "$DVT_UNSERVED_MODULE")"
    printf '    volumes:\n'
    for f in xlxd.usrp xlxd.interlink xlxd.whitelist xlxd.blacklist xlxd.terminal dmrid.dat nxdn.csv ysfnode.dat; do
      printf '      - "%s/%s:/xlxd/%s:ro"\n' "$cfg" "$f" "$f"
    done
    if [ "$svc" = xlxd-sut ] && [ "${DVT_SUT_LAUNCH:-direct}" = native ]; then
      printf '      - "%s/dv-router.toml:/etc/dv-router/dv-router.toml:ro"\n' "$cfg"
    fi
    if [ "$svc" = xlxd-sut ] && [ "$has_web" = 1 ]; then
      printf '      - "%s/%s/sut-state:/var/lib/dv"\n' "$DVT_ROOT" "$out"
    fi
    if [ "$svc" = xlxd-sut ] && [ "$has_recorder" = 1 ]; then
      printf '      - "%s/%s/recorder:/var/lib/dv-recorder"\n' "$DVT_ROOT" "$out"
    fi
    # --profile nat with turn.toml: the per-run TURN secret (mode 600, the SUT's uid) at the absolute path of turn_secret_file
    if [ "$svc" = xlxd-sut ] && [ "$has_turn" = 1 ]; then
      printf '      - "%s/%s/web/turn.secret:/run/secrets/turn-secret:ro"\n' "$DVT_ROOT" "$out"
    fi
  } >> "$override"
  prebuilt+=("$svc")
}
if [ -n "${XLXD_REF_IMAGE:-}${XLXD_SUT_IMAGE:-}" ]; then
  echo "services:" > "$override"
  [ -z "${XLXD_REF_IMAGE:-}" ] || render_xlxd xlxd-ref "$XLXD_REF_IMAGE" 10 20 off 50 60 13 15
  [ -z "${XLXD_SUT_IMAGE:-}" ] || render_xlxd xlxd-sut "$XLXD_SUT_IMAGE" 11 21 "${DVT_SUT_USRP:-off}" 51 61 12 14
  compose_files+=(-f "$DVT_ROOT/$override")
fi

compose=(docker compose -p "${COMPOSE_PROJECT_NAME:-dvt}" "${compose_files[@]}" "${DVT_ENV_FILES[@]}" "${profiles[@]}")

# Never start an uncapped service: the base file, the prebuilt-image override
# and .local/compose.override.yml all have to leave mem/swap/cpus/pids set.
"${compose[@]}" config --format json | python3 -m dvtest.limits || exit 4
# the build-only service of the fake transcoder is never part of "up", but it keeps its limits too
"${compose[@]}" --profile ambed-fake-build config --format json | python3 -m dvtest.limits || exit 4

watcher_pid=""
stats_pid=""

# Profile load: host-side resource samples for the rig containers (the tester
# has no docker socket): $out/stats.jsonl, one line per service and sample,
# {"t": unix, "service": name, "cpu_pct": float, "mem_bytes": int}.
sample_stats() {
  local project="${COMPOSE_PROJECT_NAME:-dvt}" ids
  while :; do
    ids="$("${compose[@]}" ps -q 2>/dev/null | tr '\n' ' ')"
    if [ -n "${ids// /}" ]; then
      # shellcheck disable=SC2086
      docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' $ids 2>/dev/null \
        | python3 -c '
import json, re, sys, time
units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "MB": 1000**2, "GB": 1000**3}
project, now = sys.argv[1], time.time()
for row in sys.stdin:
    name, cpu, mem = row.rstrip("\n").split("\t")
    service = re.sub(r"-[0-9]+$", "", name[len(project) + 1:] if name.startswith(project + "-") else name)
    m = re.match(r"([0-9.]+)([A-Za-z]+)", mem.split("/")[0].strip())
    mem_bytes = int(float(m.group(1)) * units.get(m.group(2), 1)) if m else 0
    print(json.dumps({"t": round(now, 3), "service": service, "cpu_pct": float(cpu.rstrip("%") or 0), "mem_bytes": mem_bytes}))
' "$project" >> "$out/stats.jsonl" || true
    fi
    # docker stats --no-stream itself takes about 2 s
    sleep 0.2
  done
}
control="$out/control"

watch_control() {
  local req id line action svc extra rc sniffer args target node mt mcid mimg kp ksvc kproc ksig kcid kimg
  while :; do
    for req in "$control"/*.req; do
      [ -e "$req" ] || continue
      id="$(basename "$req" .req)"
      line="$(head -n 1 -- "$req")"
      action="" svc="" extra=""
      read -r action svc extra <<< "$line" || true
      printf '%s request %s\n' "$(date -u +%FT%T.%3NZ)" "$line" >> "$control/control.log"
      rc=refused
      if [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = netem ] && [ "${#netem_svcs[@]}" -gt 0 ]; then
        # "netem tester|xlxd <preset|off|arguments>": xlxd = xlxd-ref and xlxd-sut alike
        case "$svc" in tester) target=(netem-tester) ;; xlxd) target=(netem-xlxd-ref netem-xlxd-sut) ;; *) target=() ;; esac
        if [ "${#target[@]}" -gt 0 ] && [ -n "$extra" ] && args="$(netem_args "$extra" 2>/dev/null)"; then
          rc=0
          # counters of the setting being replaced
          netem_snapshot "before netem $svc $extra"
          for t in "${target[@]}"; do
            # shellcheck disable=SC2086
            "${compose[@]}" exec -T "$t" dvt-netem set $args >> "$control/control.log" 2>&1 || rc=$?
          done
          # a restarted xlxd gets a new sidecar with the current setting
          [ "$svc" != xlxd ] || export DVT_NETEM_ARGS_XLXD="$args"
          netem_snapshot "netem $svc $extra"
        fi
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = mark ] && { [ "$svc" = begin ] || [ "$svc" = end ]; } \
           && [[ "$extra" =~ ^[A-Za-z0-9._-]+$ ]]; then
        rc=0
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = kill-proc ]; then
        # "kill-proc xlxd-sut <process> <signal>": see scripts/lib/killproc.sh (allow-list, exact comm)
        if kp="$(dvt_killproc_parse "$line")"; then
          read -r ksvc kproc ksig <<< "$kp"
          kcid="$("${compose[@]}" ps -q "$ksvc" 2>/dev/null | head -n 1 || true)"
          kimg="$(docker inspect -f '{{.Image}}' "$("${compose[@]}" ps -q tester 2>/dev/null | head -n 1 || true)" 2>/dev/null || true)"
          rc=0
          if [ -z "$kcid" ] || [ -z "$kimg" ]; then
            echo "kill-proc: no running $ksvc or tester container" > "$control/$id.out"; rc=4
          else
            dvt_killproc_exec "$kcid" "$kimg" "$kproc" "$ksig" > "$control/$id.out" 2>> "$control/control.log" || rc=$?
          fi
        fi
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = metrics ]; then
        # "metrics xlxd-sut": see scripts/lib/metrics.sh (allow-list, fixed URL)
        if mt="$(dvt_metrics_parse "$line")"; then
          mcid="$("${compose[@]}" ps -q "$mt" 2>/dev/null | head -n 1 || true)"
          mimg="$(docker inspect -f '{{.Image}}' "$("${compose[@]}" ps -q tester 2>/dev/null | head -n 1 || true)" 2>/dev/null || true)"
          rc=0
          if [ -z "$mcid" ] || [ -z "$mimg" ]; then
            echo "metrics: no running $mt or tester container" > "$control/$id.out"; rc=4
          else
            dvt_metrics_exec "$mcid" "$mimg" > "$control/$id.out" 2>> "$control/control.log" || rc=$?
          fi
        fi
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$action" = asl ] && [ -z "$extra" ]; then
        rc=0
        case "$svc" in
          usrp-show)
            "${compose[@]}" exec -T allstar asterisk -rx "usrp show" > "$control/$id.out" 2>&1 || rc=$? ;;
          play-1901|play-1902)
            # telemetry on only around the play, so no other tones reach xlxd
            node="${svc#play-}"
            { "${compose[@]}" exec -T allstar asterisk -rx "rpt cmd $node cop 33 1" \
                && "${compose[@]}" exec -T allstar asterisk -rx "rpt localplay $node rpt/connected" \
                && sleep 4 \
                && "${compose[@]}" exec -T allstar asterisk -rx "rpt cmd $node cop 34 1"; } \
              > "$control/$id.out" 2>&1 || rc=$? ;;
          *) rc=refused ;;
        esac
      elif [[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] && [ -z "$extra" ]; then
        case "$svc" in transcoder-ref|transcoder-sut|allstar|xlxd-ref|xlxd-sut) ;; *) action="" ;; esac
        rc=0
        sniffer=""
        [[ " ${capture_svcs[*]} " != *" capture-$svc "* ]] || sniffer="capture-$svc"
        # a sniffer shares the namespace of its service: close its file before
        # the service goes away (restart included)
        if [ -n "$sniffer" ] && [ "$action" != start ]; then
          "${compose[@]}" stop "$sniffer" >> "$control/control.log" 2>&1 || true
        fi
        case "$action" in
          stop)  "${compose[@]}" stop "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          start) "${compose[@]}" start "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          kill)  "${compose[@]}" kill -s SIGKILL "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          restart) "${compose[@]}" restart "$svc" >> "$control/control.log" 2>&1 || rc=$? ;;
          *)     rc=refused ;;
        esac
        # back up: a new sniffer in the new namespace writes <service>.<n>.pcapng
        if [ -n "$sniffer" ] && { [ "$action" = start ] || [ "$action" = restart ]; } && [ "$rc" = 0 ]; then
          "${compose[@]}" up -d --no-deps --force-recreate "$sniffer" >> "$control/control.log" 2>&1 || true
        fi
        # the qdisc lived in the old namespace: a new netem sidecar applies it again
        if [[ " ${netem_svcs[*]} " == *" netem-$svc "* ]] && { [ "$action" = start ] || [ "$action" = restart ]; } && [ "$rc" = 0 ]; then
          "${compose[@]}" up -d --no-deps --force-recreate "netem-$svc" >> "$control/control.log" 2>&1 || true
        fi
      fi
      printf '%s %s\n' "$(date -u +%FT%T.%3NZ)" "$line -> $rc" >> "$control/control.log"
      if [ "$rc" = 0 ]; then printf 'ok\n'; else printf 'error %s\n' "$rc"; fi > "$control/$id.ack.tmp"
      mv -f -- "$control/$id.ack.tmp" "$control/$id.ack"
      mv -f -- "$req" "$control/$id.done"
    done
    sleep 0.2
  done
}

# netem.txt: the qdisc of every netem sidecar, with counters
netem_snapshot() {
  local t
  [ "${#netem_svcs[@]}" -gt 0 ] || return 0
  printf '==== %s %s\n' "$(date -u +%FT%T.%3NZ)" "$1" >> "$out/netem.txt"
  for t in "${netem_svcs[@]}"; do
    printf -- '-- %s\n' "$t" >> "$out/netem.txt"
    "${compose[@]}" exec -T "$t" dvt-netem show >> "$out/netem.txt" 2>&1 || true
  done
}

collect() {
  local pid
  for pid in "$watcher_pid" "$stats_pid"; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  netem_snapshot end
  # sniffers first, so every pcapng is closed before the rig goes down
  if [ "${#capture_svcs[@]}" -gt 0 ]; then
    "${compose[@]}" stop "${capture_svcs[@]}" > /dev/null 2>&1 || true
  fi
  "${compose[@]}" ps -a > "$out/compose-ps.txt" 2>&1 || true
  # images only: "compose config" would expand .local/rig.env (the IAX secret)
  "${compose[@]}" images > "$out/compose-images.txt" 2>&1 || true
  local svc
  for svc in $("${compose[@]}" config --services 2>/dev/null); do
    "${compose[@]}" logs --no-color --timestamps "$svc" > "$out/logs/$svc.log" 2>&1 || true
  done
  if [ "$keep" = 1 ]; then
    echo "run.sh: --keep, rig left running; bring it down with scripts/run.sh --down"
  else
    "${compose_all[@]}" down --remove-orphans || true
  fi
}
trap collect EXIT

printf 'markers=%s profile=%s xlxd_ref=%s xlxd_sut=%s transcoder=%s sut_divergences=%s sut_name=%s sut_profile=%s no_radio_id=%s ref_kind=%s sut_launch=%s\n' \
  "$markers" "$profile" "${XLXD_REF_IMAGE:-${XLXD_REF_DIR:-ref}}" "${XLXD_SUT_IMAGE:-${XLXD_SUT_DIR:-sut}}" "$TRANSCODER_IMAGE" \
  "${DVT_SUT_DIVERGENCES:-}" "${DVT_SUT_NAME:-}" "${DVT_SUT_PROFILE:-xlxd}" "${DVT_NO_RADIO_ID:-}" "${DVT_REF_KIND:-xlxd}" "${DVT_SUT_LAUNCH:-direct}" \
  > "$out/run.txt"
[ "$has_web" = 0 ] || echo "web=on sut_config_extra=${DVT_SUT_CONFIG_EXTRA}" >> "$out/run.txt"
[ "$has_nat" = 0 ] || echo "nat=on turn=$([ "$has_turn" = 1 ] && echo on || echo off) coturn_image=${COTURN_IMAGE} nat_net=${DVT_NAT_NET}" >> "$out/run.txt"
cat ".cache/xlxd/${XLXD_REF_DIR:-ref}.fetched" ".cache/xlxd/${XLXD_SUT_DIR:-sut}.fetched" >> "$out/run.txt" 2>/dev/null || true

# Images are shared by all instances: one build at a time (fd 8, own lock).
(
  exec 8>> .cache/build.lock
  flock 8
  # prebuilt reflector images are used as they are, never built over
  mapfile -t to_build < <("${compose[@]}" config --services | grep -vxF -f <(printf '%s\n' "${prebuilt[@]}" "-"))
  "${compose[@]}" build "${to_build[@]}"
  # DVT_TRANSCODER_FAKE=1: build-only service, built on request and never started
  if [ "${DVT_TRANSCODER_FAKE:-}" = 1 ]; then
    "${compose[@]}" --profile ambed-fake-build build ambed-fake-build
  fi
)
# everything was built above; up never builds (a prebuilt image is never replaced)
"${compose[@]}" up -d --no-build

for svc in "${capture_svcs[@]}"; do
  f="$out/capture/${svc#capture-}.pcapng"
  for _ in $(seq 50); do [ -s "$f" ] && break; sleep 0.2; done
  [ -s "$f" ] || echo "run.sh: warning: $f not started" >&2
done

netem_snapshot start

mkdir -p "$control"
watch_control 9>&- &
watcher_pid=$!
if [ "$has_load" = 1 ]; then
  sample_stats 9>&- &
  stats_pid=$!
fi

rc=0
"${compose[@]}" exec -T -e DVT_OUT="/dvt/$out" -e DVT_RIG_CONTROL=1 tester \
  python -m pytest -m "$markers" -p no:cacheprovider -rs \
  --junitxml="/dvt/$out/junit.xml" "$@" 2>&1 | tee "$out/pytest.log" || rc=$?
echo "run.sh: pytest exit $rc, results in $out/"

# sniffers stopped and rig down before the capture check reads the files
trap - EXIT
collect
# --profile nat: the TURN secret must be in no file of the output directory except its own two (nor in a capture:
# the secret never goes on the wire, only the HMAC credential does). The scan prints paths, never the value.
if [ "$has_nat" = 1 ]; then
  scan_rc=0
  PYTHONPATH="$DVT_ROOT" python3 -m dvtest.turn scan --secret-file "$out/web/turn.secret" --root "$out" \
    --skip web/turn.secret --skip coturn/turnserver.conf || scan_rc=$?
  if [ "$scan_rc" != 0 ]; then
    echo "run.sh: the TURN secret was found in the output above (exit $scan_rc)" >&2
    [ "$rc" != 0 ] || rc=5
  fi
fi
if [ "$capture" = 1 ]; then
  if [ -x scripts/dvcheck.sh ]; then
    check_rc=0
    scripts/dvcheck.sh "$out" > "$out/check.txt" 2>&1 || check_rc=$?
    echo "run.sh: wire check exit $check_rc, see $out/check.txt"
    [ "$rc" != 0 ] || rc=$check_rc
  else
    echo "run.sh: scripts/dvcheck.sh missing, capture not checked" >&2
  fi
fi
exit "$rc"
