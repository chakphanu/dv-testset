# SPDX-License-Identifier: MIT
"""Check a rendered dv-router.toml of the rig against a DV-Router image WITHOUT the rig.

The image has no separate validate command; `dv-router --config FILE CALLSIGN OWN_IP TRANSCODER_IP` validates the file first and
exits 2 with `<file>: N error(s), not started` when it is invalid. This tool runs exactly that in a throw-away container
(`docker run --rm --network none`, the dvtest.limits caps, read-only root, all capabilities dropped, no-new-privileges, the caller's uid (so the 0600 stand-ins are readable without CAP_DAC_OVERRIDE), the file mounted read-only, no ports; the root is read-only, so no state can be written), reads the
startup lines, and kills the container as soon as the router is past validation (it is never left running). Usage:

  python3 -m dvtest.tools.config_check --image dv-router:wip-c165c93 --template TEMPLATE [--extra FILE ...] [--gen 71ab207|c165c93]

Exit 0 = valid (warnings are printed), 1 = invalid (the errors are printed), 2 = the check itself failed (no docker, no image).
`judge` is the pure part (unit-tested in cases/unit/test_config_check.py).
"""
import argparse
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import uuid

from dvtest import limits, sut_config

NOT_STARTED = re.compile(r"(\d+) error\(s\), not started")
SECRET_FILE = re.compile(r'^\s*[A-Za-z0-9_]+_file\s*=\s*"(/[^"]+)"', re.M)
PAST_VALIDATION = re.compile(r"(?:fabric: |callhome: |edges: |wrote .*config-entries)")


def judge(lines, exit_code=None):
    """{"valid", "errors", "warnings", "started"} from the output lines of the router. An error is any line before `N error(s),
    not started` that names a file:line (the router prints them first) or that summary line itself; a warning is a `config
    warning:` line. `started` is True when the router got past validation (a line like `fabric:`/`edges:`)."""
    errors, warnings, started, refused = [], [], False, False
    for raw in lines:
        line = raw.strip()
        body = line[len("dv-router:"):].strip() if line.startswith("dv-router:") else line
        if "config warning:" in body:
            warnings.append(body.split("config warning:", 1)[1].strip())
        elif NOT_STARTED.search(body):
            refused = True
            errors.append(body)
        elif PAST_VALIDATION.search(body):
            started = True
    if refused:
        # the error lines are the ones above the summary that carry a position (file:line:col: message)
        pos = [ln.strip()[len("dv-router:"):].strip() if ln.strip().startswith("dv-router:") else ln.strip() for ln in lines]
        errors = [e for e in pos if re.match(r"^\S+:\d+:\d+: ", e) and "config warning:" not in e] + errors
    valid = not refused and exit_code != 2
    return {"valid": valid, "errors": errors, "warnings": warnings, "started": started}


def secret_file_paths(text):
    """The absolute paths a config names in `*_file = "/abs/path"` keys (password_file, secret_file, turn_secret_file ...): the
    router refuses the file unless each is a readable regular file with mode 0600 (run.sh writes them per run), so the check
    mounts empty 0600 stand-ins at the same paths. Only the TOML is checked, never a real secret."""
    seen = []
    for path in SECRET_FILE.findall(text):
        if path not in seen:
            seen.append(path)
    return seen


def docker_command(image, config_path, name, net="172.30.0", stand_ins=(), uid=None, gid=None):
    """The `docker run` command line of the check (pure; unit-tested): a throw-away container with no network, a read-only root,
    no capabilities, no-new-privileges, the caller's uid:gid, the dvtest.limits caps, the TOML and the stand-ins mounted read-only
    and no ports; the image's own entrypoint (dv-router) with `--config FILE XLX999 OWN_IP TRANSCODER_IP`."""
    mounts = []
    for host, inside in stand_ins:
        mounts += ["-v", "%s:%s:ro" % (host, inside)]
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "%d:%d" % (uid, gid), *limits.docker_run_args(),
            "-v", "%s:/etc/dv-router/dv-router.toml:ro" % config_path, *mounts, image,
            "--config", "/etc/dv-router/dv-router.toml", "XLX999", net + ".11", net + ".21"]


def run_check(image, config_path, net="172.30.0", timeout_s=25.0, stand_ins=()):
    """Run the router on `config_path` (an absolute path on the host) and return (lines, exit_code or None). `stand_ins` are
    (host path, container path) pairs mounted read-only (empty 0600 files for the secret files)."""
    name = "dvt-cfgcheck-%s" % uuid.uuid4().hex[:8]
    cmd = docker_command(image, config_path, name, net, stand_ins)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, end, code = [], time.monotonic() + timeout_s, None
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    code = proc.wait()
                    break
                lines.append(line.rstrip("\n"))
                if PAST_VALIDATION.search(line) and any("config" in x for x in lines):
                    time.sleep(0.5)                       # a few more lines (warnings come before, errors never after)
                    break
            elif proc.poll() is not None:
                code = proc.returncode
                break
    finally:
        if proc.poll() is None:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    return lines, code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.tools.config_check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--extra", action="append", default=[])
    ap.add_argument("--gen", default=None, help="DVT_SUT_GEN of the render (default: env, else c165c93)")
    ap.add_argument("--net", default="172.30.0")
    args = ap.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="dvt-cfgcheck-") as tmp:
            out = os.path.join(tmp, "dv-router.toml")
            sut_config.render_file(args.template, out, args.net, extras=args.extra, gen=args.gen)
            stand_ins = []
            with open(out, encoding="utf-8") as fh:
                for n, inside in enumerate(secret_file_paths(fh.read())):
                    host = os.path.join(tmp, "secret-%d" % n)
                    with open(host, "w", encoding="utf-8") as sf:
                        sf.write("standin\n")
                    os.chmod(host, 0o600)
                    stand_ins.append((host, inside))
            lines, code = run_check(args.image, out, args.net, stand_ins=stand_ins)
    except (sut_config.ConfigError, OSError, subprocess.SubprocessError) as exc:
        print("config_check: %s" % exc, file=sys.stderr)
        return 2
    verdict = judge(lines, code)
    for text in verdict["errors"]:
        print("ERROR   " + text)
    for text in verdict["warnings"]:
        print("warning " + text)
    print("%s: %s" % (args.image, "valid" if verdict["valid"] else "INVALID"))
    if not lines:
        print("config_check: the container printed nothing (is the image present?)", file=sys.stderr)
        return 2
    return 0 if verdict["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
