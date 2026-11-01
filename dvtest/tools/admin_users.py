# SPDX-License-Identifier: MIT
"""Write the admin users file of the rig's DV-Router and the matching logins.

DV-Router's admin service reads `<state>/admin/users`, one line
`name:role:$argon2id$...` (mode 0600, docs/design/admin.md "Auth"); only its
first admin can come from a plain password (`secrets/admin-bootstrap`), so the
viewer and operator accounts of the role matrix need hashes. dvtest/argon2_pure.py
makes them with the stdlib. The accounts have no `:totp=` field: each one enrols
at its first login, which the cases do.

  python3 -m dvtest.tools.admin_users --users-file <state>/admin/users \\
      --env-file out/<run>/web/admin.env

The env file (mode 0600) gets DVT_ADMIN_<ROLE>=<name> and
DVT_ADMIN_<ROLE>_PASSWORD=<random password> for ADMIN, OPERATOR and VIEWER.
Nothing is printed. Memory and time cost: DVT_ADMIN_ARGON_M (KiB, default
19456), DVT_ADMIN_ARGON_T (default 2).
"""
import argparse
import os
import secrets
import sys

from dvtest import argon2_pure

ACCOUNTS = (("admin", "admin"), ("operator", "operator"), ("viewer", "viewer"))   # (name, role)


def build(m_kib, t, rng=secrets):
    """([users file lines], {env key: value}) for fresh random passwords."""
    lines, env = [], {}
    for name, role in ACCOUNTS:
        password = rng.token_urlsafe(24)
        salt = rng.token_bytes(16)
        lines.append("%s:%s:%s" % (name, role, argon2_pure.phc(password, salt, t=t, m_kib=m_kib, p=1)))
        env["DVT_ADMIN_%s" % role.upper()] = name
        env["DVT_ADMIN_%s_PASSWORD" % role.upper()] = password
    return lines, env


def write_private(path, text):
    """Create `path` with mode 0600 (never wider, not even briefly)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.tools.admin_users", description=__doc__.split("\n\n")[0])
    ap.add_argument("--users-file", required=True)
    ap.add_argument("--env-file", required=True)
    args = ap.parse_args(argv)
    try:
        m_kib = int(os.environ.get("DVT_ADMIN_ARGON_M", "19456"))
        t = int(os.environ.get("DVT_ADMIN_ARGON_T", "2"))
        lines, env = build(m_kib, t)
        os.makedirs(os.path.dirname(args.users_file), mode=0o700, exist_ok=True)
        write_private(args.users_file, "".join(line + "\n" for line in lines))
        write_private(args.env_file, "".join("%s=%s\n" % kv for kv in env.items()))
    except (ValueError, OSError) as exc:
        print("admin_users: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
