# SPDX-License-Identifier: MIT
"""Check a rendered dv-router.toml of the rig against a DV-Router image WITHOUT the rig.

The image has no separate validate command; `dv-router --config FILE CALLSIGN OWN_IP TRANSCODER_IP` validates the file first and
exits 2 with `<file>: N error(s), not started` when it is invalid. This tool runs exactly that in a throw-away container
(`docker run --rm --network none`, the dvtest.limits caps, read-only root, all capabilities dropped, no-new-privileges, the caller's uid (so the 0600 stand-ins are readable without CAP_DAC_OVERRIDE), the file mounted read-only, no ports; the root is read-only, so no state can be written), reads the
startup lines, and kills the container as soon as the router is past validation (it is never left running). Usage:

  python3 -m dvtest.tools.config_check --image dv-router:wip-c165c93 --template TEMPLATE [--extra FILE ...] [--gen 71ab207|c165c93|c2465de|compat1] [--mode auto|start|check]

For `--gen compat1` (or `--mode check`) it runs `dv-router config check FILE` instead, which prints the errors and exits by itself
(`judge_check`; no network, no writes; exit 0 valid, 2 invalid on wip-488b89d). Exit 0 = valid (warnings are printed), 1 = invalid (the errors are printed), 2 = the check itself failed (no docker, no image).
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
INSIDE = "/etc/dv-router/dv-router.toml"
MODES = ("start", "check")
GENS_WITH_CHECK = ("compat1",)       # DVT_SUT_GEN values whose image has `dv-router config check FILE`
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


POSITIONED = re.compile(r"^(?P<file>\S+?):(?P<line>\d+):(?P<col>\d+): (?P<msg>.+)$")
KEYED = re.compile(r"^(?P<key>[A-Za-z0-9_.\[\]-]+): (?P<text>.+)$")


def judge_check(lines, exit_code):
    """The verdict of `dv-router config check FILE` (compat1 images) from its output lines and exit status. Format (checked on
    wip-488b89d): an invalid file prints one line per error, `[dv-router: ]FILE:LINE:COL: KEY: MESSAGE` (a TOML syntax error has no KEY),
    and exits 2; a valid file exits 0 and prints the same shape for warnings (no `config warning:` word) and then the plan
    (`configuration: ...`, `child: ...`). So the exit status, not the wording, tells a warning from an error. An image without the
    subcommand prints a usage text (`usage: dv-router --config FILE ...`) and exits 2 too; that is `unsupported`, not `invalid`.
    Returns {"valid", "unsupported", "errors", "warnings", "parsed"}: errors / warnings are the positioned lines (prefix cut), parsed
    is [{"file", "line", "col", "key", "message"}] of the errors (key is "" when the line has none)."""
    positioned, unsupported = [], False
    for raw in lines:
        body = raw.strip()
        if body.startswith("dv-router:"):
            body = body[len("dv-router:"):].strip()
        if body.startswith("usage:") or body.startswith('invalid CALLSIGN "config"'):
            unsupported = True
        m = POSITIONED.match(body)
        if m:
            positioned.append((body, m))
    valid = exit_code == 0 and not unsupported
    parsed = []
    if not valid and not unsupported:
        for _, m in positioned:
            k = KEYED.match(m.group("msg"))
            parsed.append({"file": m.group("file"), "line": int(m.group("line")), "col": int(m.group("col")),
                           "key": k.group("key") if k else "", "message": k.group("text") if k else m.group("msg")})
    texts = [body for body, _ in positioned]
    return {"valid": valid, "unsupported": unsupported, "errors": [] if valid else texts, "warnings": texts if valid else [],
            "parsed": parsed}


def secret_file_paths(text):
    """The absolute paths a config names in `*_file = "/abs/path"` keys (password_file, secret_file, turn_secret_file ...): the
    router refuses the file unless each is a readable regular file with mode 0600 (run.sh writes them per run), so the check
    mounts empty 0600 stand-ins at the same paths. Only the TOML is checked, never a real secret."""
    seen = []
    for path in SECRET_FILE.findall(text):
        if path not in seen:
            seen.append(path)
    return seen


def docker_command(image, config_path, name, net="172.30.0", stand_ins=(), uid=None, gid=None, mode="start"):
    """The `docker run` command line of the check (pure; unit-tested): a throw-away container with no network, a read-only root,
    no capabilities, no-new-privileges, the caller's uid:gid, the dvtest.limits caps, the TOML and the stand-ins mounted read-only
    and no ports; the image's own entrypoint (dv-router) with `--config FILE XLX999 OWN_IP TRANSCODER_IP` (mode "start", the
    older images) or with `config check FILE` (mode "check", the compat1 images: no network, no writes, exits by itself)."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s (got %r)" % ("|".join(MODES), mode))
    mounts = []
    for host, inside in stand_ins:
        mounts += ["-v", "%s:%s:ro" % (host, inside)]
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "%d:%d" % (uid, gid), *limits.docker_run_args(),
            "-v", "%s:%s:ro" % (config_path, INSIDE), *mounts, image,
            *(["config", "check", INSIDE] if mode == "check" else ["--config", INSIDE, "XLX999", net + ".11", net + ".21"])]


def run_check(image, config_path, net="172.30.0", timeout_s=25.0, stand_ins=(), mode="start"):
    """Run the router on `config_path` (an absolute path on the host) and return (lines, exit_code or None). `stand_ins` are
    (host path, container path) pairs mounted read-only (empty 0600 files for the secret files). In mode "check" the container
    ends by itself and the output is read to the end; the exit code is always set unless the timeout kills it (code None)."""
    name = "dvt-cfgcheck-%s" % uuid.uuid4().hex[:8]
    cmd = docker_command(image, config_path, name, net, stand_ins, mode=mode)
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
                if mode == "start" and PAST_VALIDATION.search(line) and any("config" in x for x in lines):
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
    ap.add_argument("--mode", choices=("auto",) + MODES, default="auto",
                    help="check = `dv-router config check FILE` (compat1 images); start = `--config FILE ...` (older images); "
                         "auto = check when the gen is in GENS_WITH_CHECK, else start")
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
            gen = sut_config.gen_value(args.gen)
            mode = args.mode if args.mode != "auto" else ("check" if gen in GENS_WITH_CHECK else "start")
            lines, code = run_check(args.image, out, args.net, stand_ins=stand_ins, mode=mode)
    except (sut_config.ConfigError, OSError, subprocess.SubprocessError) as exc:
        print("config_check: %s" % exc, file=sys.stderr)
        return 2
    verdict = judge_check(lines, code) if mode == "check" else judge(lines, code)
    if verdict.get("unsupported"):
        print("config_check: %s has no `config check` (use --mode start or an older --gen)" % args.image, file=sys.stderr)
        return 2
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
