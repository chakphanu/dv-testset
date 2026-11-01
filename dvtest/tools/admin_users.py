# SPDX-License-Identifier: MIT
"""Write the admin users file of the rig's DV-Router and the matching logins.

DV-Router's admin service reads `<state>/admin/users`, one line
`name:role:$argon2id$...` (mode 0600, docs/design/admin.md "Auth"); only its
first admin can come from a plain password (`secrets/admin-bootstrap`), so the
viewer and operator accounts of the role matrix need hashes. The accounts have
no `:totp=` field: each one enrols at its first login, which the cases do.

Two routes, chosen per run and logged (never a password):

  image   the SUT's own account command (ADM1, DV-Router 30ad77a):
          `dv-svc-admin user add NAME ROLE --state-dir DIR`, password on stdin,
          run in a throw-away container of the SUT image (FROM scratch, so the
          binary is the entrypoint: ADMIN_BIN) with no network, the host uid and
          the limits of dvtest.limits.docker_run_args() (256 MB, 0.5 CPU, 128
          tasks by default; DVT_ADMIN_CTR_MEM / _CPUS / _PIDS). The image has the
          command when `hash-password` of a throw-away probe password answers
          an argon2id PHC string; an older binary refuses every argument (exit 2).
  python  dvtest/argon2_pure.py (stdlib Argon2id) writes the file itself: the
          fallback for an image without the command.

  python3 -m dvtest.tools.admin_users --users-file <state>/admin/users \\
      --env-file out/<run>/web/admin.env [--image IMAGE] [--route-file FILE]

DVT_ADMIN_USERS=auto (default: image when --image has the command, else
python), image (no fallback) or python. The env file (mode 0600) gets
DVT_ADMIN_<ROLE>=<name> and DVT_ADMIN_<ROLE>_PASSWORD=<random password> for
ADMIN, OPERATOR and VIEWER. One line on stdout names the route (and the
route file gets it). Python route cost: DVT_ADMIN_ARGON_M (KiB, default
19456), DVT_ADMIN_ARGON_T (default 2). The user add command needs the
service restarted to take effect; run.sh calls this before compose up.
"""
import argparse
import os
import secrets
import subprocess
import sys

from dvtest import argon2_pure, limits

ACCOUNTS = (("admin", "admin"), ("operator", "operator"), ("viewer", "viewer"))   # (name, role)
ADMIN_BIN = "/usr/local/bin/dv-svc-admin"     # DV-Router Dockerfile: every binary in /usr/local/bin
STATE_MOUNT = "/state"
CTR_DEFAULTS = {"DVT_CTR_MEM": ("DVT_ADMIN_CTR_MEM", "256m"), "DVT_CTR_CPUS": ("DVT_ADMIN_CTR_CPUS", "0.5"),
                "DVT_CTR_PIDS": ("DVT_ADMIN_CTR_PIDS", "128")}
TIMEOUT_S = 120


class RouteError(Exception):
    """The image route failed (the message carries no password)."""


def new_passwords(rng=secrets):
    """[(name, role, password)] with fresh random passwords."""
    return [(name, role, rng.token_urlsafe(24)) for name, role in ACCOUNTS]


def env_of(accounts):
    env = {}
    for name, role, password in accounts:
        env["DVT_ADMIN_%s" % role.upper()] = name
        env["DVT_ADMIN_%s_PASSWORD" % role.upper()] = password
    return env


def build(m_kib, t, rng=secrets):
    """([users file lines], {env key: value}) for fresh random passwords (python route)."""
    accounts = new_passwords(rng)
    lines = ["%s:%s:%s" % (name, role, argon2_pure.phc(password, rng.token_bytes(16), t=t, m_kib=m_kib, p=1))
             for name, role, password in accounts]
    return lines, env_of(accounts)


def write_private(path, text):
    """Create `path` with mode 0600 (never wider, not even briefly)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


# ------------------------------------------------------------------ image route

def caps(env=None):
    """docker run limit words: dvtest.limits.docker_run_args() with this tool's smaller defaults."""
    env = os.environ if env is None else env
    return limits.docker_run_args({k: env.get(own) or default for k, (own, default) in CTR_DEFAULTS.items()})


def docker_cmd(image, args, state_dir=None, uid=None, env=None):
    """The `docker run` argv of one dv-svc-admin command: throw-away, no network, capped, read-only root,
    no capabilities; the state dir (when given) mounted read-write at /state, run as the host uid:gid."""
    cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges"] + caps(env)
    if uid is not None:
        cmd += ["--user", uid]
    if state_dir is not None:
        cmd += ["-v", "%s:%s" % (os.path.abspath(state_dir), STATE_MOUNT)]
    return cmd + ["--entrypoint", ADMIN_BIN, image] + list(args)


def run_cmd(cmd, stdin_text):
    """(exit code, stdout, first stderr line) of a command fed `stdin_text`; a timeout is exit -1."""
    try:
        proc = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=TIMEOUT_S,
                              check=False)
    except subprocess.TimeoutExpired:
        return -1, "", "timeout after %d s" % TIMEOUT_S
    except OSError as exc:
        return -1, "", "%s" % exc
    err = (proc.stderr or "").strip().splitlines()
    return proc.returncode, proc.stdout or "", err[0][:200] if err else ""


def image_has_command(image, runner=run_cmd, rng=secrets):
    """(True, "") when the image's dv-svc-admin hashes a throw-away probe password, else (False, reason)."""
    rc, out, err = runner(docker_cmd(image, ["hash-password"]), rng.token_urlsafe(18) + "\n")
    if rc == 0 and out.startswith("$argon2id$"):
        return True, ""
    return False, "hash-password exit %d%s" % (rc, (": " + err) if err else "")


def add_with_image(image, state_dir, accounts, runner=run_cmd, uid=None):
    """`user add` of every account through the image; RouteError on any failure."""
    uid = uid or "%d:%d" % (os.getuid(), os.getgid())
    for name, role, password in accounts:
        cmd = docker_cmd(image, ["user", "add", name, role, "--state-dir", STATE_MOUNT], state_dir, uid)
        rc, _out, err = runner(cmd, password + "\n")
        if rc != 0:
            raise RouteError("dv-svc-admin user add %s %s: exit %d%s" % (name, role, rc, (": " + err) if err else ""))


def check_users_file(path, accounts):
    """The users file has each account with its role and an argon2id hash, mode 0600 (RouteError otherwise)."""
    try:
        mode = os.stat(path).st_mode & 0o777
        with open(path, encoding="ascii") as fh:
            rows = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    except (OSError, UnicodeDecodeError) as exc:
        raise RouteError("users file %s: %s" % (path, exc)) from exc
    if mode & 0o077:
        raise RouteError("users file mode %o, wanted 0600" % mode)
    got = {}
    for row in rows:
        parts = row.split(":", 2)
        if len(parts) == 3:
            got[parts[0]] = (parts[1], parts[2].startswith("$argon2id$"))
    for name, role, _pw in accounts:
        if got.get(name) != (role, True):
            raise RouteError("users file has no %s:%s argon2id line" % (name, role))


def state_dir_of(users_file):
    """<state> of <state>/admin/users (the user add command takes the state dir)."""
    admin_dir = os.path.dirname(os.path.abspath(users_file))
    if os.path.basename(users_file) != "users" or os.path.basename(admin_dir) != "admin":
        raise ValueError("--users-file must be <state>/admin/users for the image route, got %s" % users_file)
    return os.path.dirname(admin_dir)


def main(argv=None, runner=run_cmd):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.tools.admin_users", description=__doc__.split("\n\n")[0])
    ap.add_argument("--users-file", required=True)
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--image", help="SUT image whose dv-svc-admin may have `user add` (image route)")
    ap.add_argument("--route-file", help="write the chosen route (one line, no secret) here as well")
    args = ap.parse_args(argv)
    mode = os.environ.get("DVT_ADMIN_USERS", "auto")
    try:
        if mode not in ("auto", "image", "python"):
            raise ValueError("DVT_ADMIN_USERS must be auto, image or python, got %r" % mode)
        route, why = "python", "no --image" if not args.image else "DVT_ADMIN_USERS=python"
        if args.image and mode != "python":
            has, why = image_has_command(args.image, runner)
            if has:
                route = "image"
            elif mode == "image":
                raise RouteError("DVT_ADMIN_USERS=image but %s has no account command (%s)" % (args.image, why))
        if route == "image":
            state = state_dir_of(args.users_file)
            os.makedirs(state, mode=0o700, exist_ok=True)
            accounts = new_passwords()
            add_with_image(args.image, state, accounts, runner)
            check_users_file(args.users_file, accounts)
            env = env_of(accounts)
            line = "route=image (%s %s user add, password on stdin, %s)" % (args.image, ADMIN_BIN, " ".join(caps()))
        else:
            m_kib = int(os.environ.get("DVT_ADMIN_ARGON_M", "19456"))
            t = int(os.environ.get("DVT_ADMIN_ARGON_T", "2"))
            lines, env = build(m_kib, t)
            os.makedirs(os.path.dirname(args.users_file), mode=0o700, exist_ok=True)
            write_private(args.users_file, "".join(row + "\n" for row in lines))
            line = "route=python (dvtest/argon2_pure.py; %s)" % why
        write_private(args.env_file, "".join("%s=%s\n" % kv for kv in env.items()))
        if args.route_file:
            with open(args.route_file, "w", encoding="ascii") as fh:
                fh.write(line + "\n")
        print("admin_users: %s" % line)
    except (ValueError, OSError, RouteError) as exc:
        print("admin_users: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
