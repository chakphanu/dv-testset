# SPDX-License-Identifier: MIT
"""Resource limits for the containers this repository starts.

Two jobs:

* docker_run_args(): the --memory/--memory-swap/--cpus/--pids-limit words for
  a host-side `docker run` (wire cases, live_id). Defaults come from
  DVT_CTR_MEM, DVT_CTR_CPUS and DVT_CTR_PIDS (.env.example).
* check_services(): verify a `docker compose config --format json` document:
  every service has a memory limit, a swap limit equal to it, a cpus quota
  and a pids limit. scripts/run.sh runs it before the rig is brought up:

      docker compose ... config --format json | python3 -m dvtest.limits
"""
import json
import os
import re
import sys

DEFAULT_MEM = "1g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS = "512"

_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
          "g": 1024 ** 3, "gb": 1024 ** 3, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}


def parse_bytes(value):
    """Bytes from an int, a digit string or a docker size like 512m / 1g.
    Raises ValueError for anything else (including 0 and negative)."""
    if isinstance(value, bool):
        raise ValueError("not a size: %r" % (value,))
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*", str(value))
        if not m or m.group(2).lower() not in _UNITS:
            raise ValueError("not a size: %r" % (value,))
        n = int(float(m.group(1)) * _UNITS[m.group(2).lower()])
    if n <= 0:
        raise ValueError("size must be positive: %r" % (value,))
    return n


def docker_run_args(env=None):
    """Limit words for `docker run`; swap equals memory, so no swap."""
    env = os.environ if env is None else env
    mem = env.get("DVT_CTR_MEM") or DEFAULT_MEM
    cpus = env.get("DVT_CTR_CPUS") or DEFAULT_CPUS
    pids = env.get("DVT_CTR_PIDS") or DEFAULT_PIDS
    parse_bytes(mem)
    if float(cpus) <= 0 or int(pids) <= 0:
        raise ValueError("DVT_CTR_CPUS and DVT_CTR_PIDS must be positive")
    return ["--memory", mem, "--memory-swap", mem, "--cpus", cpus, "--pids-limit", str(pids)]


def check_services(config):
    """Problems (list of strings) in a compose config document; empty = every
    service is capped."""
    problems = []
    services = config.get("services") or {}
    if not services:
        return ["no services in the compose config"]
    for name in sorted(services):
        svc = services[name]
        try:
            mem = parse_bytes(svc.get("mem_limit"))
        except ValueError:
            problems.append("%s: mem_limit missing or invalid (%r)" % (name, svc.get("mem_limit")))
            mem = None
        try:
            swap = parse_bytes(svc.get("memswap_limit"))
        except ValueError:
            problems.append("%s: memswap_limit missing or invalid (%r)" % (name, svc.get("memswap_limit")))
            swap = None
        if mem is not None and swap is not None and swap != mem:
            problems.append("%s: memswap_limit %r differs from mem_limit %r (swap must be off)"
                            % (name, svc.get("memswap_limit"), svc.get("mem_limit")))
        try:
            if float(svc.get("cpus")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("%s: cpus missing or invalid (%r)" % (name, svc.get("cpus")))
        try:
            if int(svc.get("pids_limit")) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("%s: pids_limit missing or invalid (%r)" % (name, svc.get("pids_limit")))
    return problems


def main(argv=None):
    config = json.load(sys.stdin)
    problems = check_services(config)
    for line in problems:
        print("limits: " + line, file=sys.stderr)
    if problems:
        print("limits: refusing to start an uncapped rig (README, 'Resource limits')", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
