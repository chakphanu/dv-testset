# SPDX-License-Identifier: MIT
"""dvtest/argon2_pure.py (pure Python Argon2id) against the RFC 9106 test vector, and
dvtest/tools/admin_users.py (the admin users file of the rig)."""
import base64
import os
import stat

import pytest

from dvtest import argon2_pure
from dvtest.tools import admin_users

pytestmark = pytest.mark.unit


def test_rfc9106_argon2id_vector():
    # RFC 9106, section 5.3: Argon2id, t=3, m=32 KiB, p=4, 32-byte tag
    tag = argon2_pure.argon2id(b"\x01" * 32, b"\x02" * 16, 3, 32, 4, 32, b"\x03" * 8, b"\x04" * 12)
    assert tag.hex() == "0d640df58d78766c08c037a34a8b53c9d01ef0452d75b65eb52520e96b01e659"


def test_parameters_and_phc_format():
    with pytest.raises(ValueError):
        argon2_pure.argon2id(b"pw", b"short", 1, 64, 1)           # salt under 8 bytes
    with pytest.raises(ValueError):
        argon2_pure.argon2id(b"pw", b"saltsalt", 1, 4, 1)         # under 8 blocks per lane
    text = argon2_pure.phc("pw", b"saltsalt", t=1, m_kib=64, p=1)
    head, ver, params, salt, tag = text[1:].split("$")
    assert head == "argon2id" and ver == "v=19" and params == "m=64,t=1,p=1"
    assert base64.b64decode(salt + "=" * (-len(salt) % 4)) == b"saltsalt"
    assert len(base64.b64decode(tag + "=" * (-len(tag) % 4))) == 32
    assert text == argon2_pure.phc(b"pw", b"saltsalt", t=1, m_kib=64, p=1)           # deterministic
    assert text != argon2_pure.phc("pw2", b"saltsalt", t=1, m_kib=64, p=1)


def test_long_tags_use_the_variable_length_hash():
    assert len(argon2_pure.argon2id(b"pw", b"saltsalt", 1, 16, 1, taglen=100)) == 100
    assert len(argon2_pure.argon2id(b"pw", b"saltsalt", 1, 16, 1, taglen=1024)) == 1024


def test_admin_users_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DVT_ADMIN_ARGON_M", "64")
    monkeypatch.setenv("DVT_ADMIN_ARGON_T", "1")
    users, env = tmp_path / "state" / "admin" / "users", tmp_path / "admin.env"
    assert admin_users.main(["--users-file", str(users), "--env-file", str(env)]) == 0
    for path in (users, env):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    rows = users.read_text().splitlines()
    assert [r.split(":")[0:2] for r in rows] == [["admin", "admin"], ["operator", "operator"], ["viewer", "viewer"]]
    assert all(r.split(":", 2)[2].startswith("$argon2id$v=19$m=64,t=1,p=1$") for r in rows)
    logins = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert set(logins) == {"DVT_ADMIN_%s%s" % (r, s) for r in ("ADMIN", "OPERATOR", "VIEWER") for s in ("", "_PASSWORD")}
    for role in ("ADMIN", "OPERATOR", "VIEWER"):
        assert len(logins["DVT_ADMIN_%s_PASSWORD" % role]) >= 24
        assert logins["DVT_ADMIN_%s_PASSWORD" % role] not in users.read_text()
    assert len({logins["DVT_ADMIN_%s_PASSWORD" % r] for r in ("ADMIN", "OPERATOR", "VIEWER")}) == 3
    # a password verifies against its hash
    for row, role in zip(rows, ("ADMIN", "OPERATOR", "VIEWER")):
        _name, _role, phc = row.split(":", 2)
        _e, _a, _v, params, salt, tag = phc.split("$")
        salt_b = base64.b64decode(salt + "=" * (-len(salt) % 4))
        want = argon2_pure.argon2id(logins["DVT_ADMIN_%s_PASSWORD" % role].encode(), salt_b, 1, 64, 1)
        assert base64.b64encode(want).decode().rstrip("=") == tag
    monkeypatch.setenv("DVT_ADMIN_ARGON_M", "x")
    assert admin_users.main(["--users-file", str(users), "--env-file", str(env)]) == 2


class FakeDocker:
    """Stands in for `docker run ... dv-svc-admin`: `hash-password` answers a PHC string when `has_command`,
    `user add` appends a line to <state>/admin/users like the real command (mode 0600)."""

    def __init__(self, has_command=True, add_rc=0):
        self.has_command, self.add_rc, self.calls = has_command, add_rc, []

    def __call__(self, cmd, stdin_text):
        self.calls.append((cmd, stdin_text))
        args = cmd[cmd.index(admin_users.ADMIN_BIN) + 2:]
        if not self.has_command:
            return 2, "", "usage: dv-svc-admin (configuration from DV_STATE_DIR, DV_NODE and DV_ADMIN_* variables)"
        if args == ["hash-password"]:
            return 0, "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA\n", ""
        if self.add_rc:
            return self.add_rc, "", "dv-svc-admin: account command failed: private file mode or I/O error"
        _user, _add, name, role, _flag, _mount = args
        host = cmd[cmd.index("-v") + 1].split(":")[0]
        path = os.path.join(host, "admin", "users")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write("%s:%s:$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA\n" % (name, role))
        return 0, "", ""


def _paths(tmp_path):
    return tmp_path / "state" / "admin" / "users", tmp_path / "admin.env", tmp_path / "route"


def test_admin_users_image_route(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DVT_ADMIN_USERS", raising=False)
    users, env, route = _paths(tmp_path)
    fake = FakeDocker()
    argv = ["--users-file", str(users), "--env-file", str(env), "--image", "dv-router:test", "--route-file", str(route)]
    assert admin_users.main(argv, runner=fake) == 0
    logins = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert [r.split(":")[0:2] for r in users.read_text().splitlines()] == [["admin", "admin"], ["operator", "operator"],
                                                                         ["viewer", "viewer"]]
    assert len(fake.calls) == 4 and fake.calls[0][0][-1] == "hash-password"
    for cmd, stdin_text in fake.calls:
        # throw-away, capped, no network; the password only on stdin, never in argv
        assert cmd[:4] == ["docker", "run", "--rm", "-i"] and "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
        for word in ("--memory", "--memory-swap", "--cpus", "--pids-limit"):
            assert word in cmd, (word, cmd)
        assert cmd[cmd.index("--memory") + 1] == cmd[cmd.index("--memory-swap") + 1] == "256m"
        assert cmd[cmd.index("--entrypoint") + 1] == "/usr/local/bin/dv-svc-admin"
        for value in logins.values():
            if len(value) > 16:
                assert all(value not in word for word in cmd)
    adds = fake.calls[1:]
    for (cmd, stdin_text), (name, role) in zip(adds, admin_users.ACCOUNTS):
        assert cmd[-6:] == ["user", "add", name, role, "--state-dir", "/state"]
        assert cmd[cmd.index("-v") + 1] == "%s:/state" % (tmp_path / "state")
        assert "--user" in cmd
        assert stdin_text == logins["DVT_ADMIN_%s_PASSWORD" % role.upper()] + "\n"
    out = capsys.readouterr().out
    assert out.startswith("admin_users: route=image") and route.read_text().startswith("route=image")
    assert all(v not in out for v in logins.values() if len(v) > 16)
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_admin_users_fallback_and_forced_routes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DVT_ADMIN_ARGON_M", "64")
    monkeypatch.setenv("DVT_ADMIN_ARGON_T", "1")
    monkeypatch.delenv("DVT_ADMIN_USERS", raising=False)
    users, env, route = _paths(tmp_path)
    argv = ["--users-file", str(users), "--env-file", str(env), "--image", "dv-router:old", "--route-file", str(route)]
    old = FakeDocker(has_command=False)
    assert admin_users.main(argv, runner=old) == 0
    assert len(old.calls) == 1, "only the probe runs on an image without the command"
    assert route.read_text().startswith("route=python") and "hash-password exit 2" in route.read_text()
    assert all(r.split(":", 2)[2].startswith("$argon2id$v=19$m=64,t=1,p=1$") for r in users.read_text().splitlines())
    # forced image route on an old image: no fallback
    monkeypatch.setenv("DVT_ADMIN_USERS", "image")
    assert admin_users.main(argv, runner=FakeDocker(has_command=False)) == 2
    # a failing user add is an error, not a silent fallback
    monkeypatch.setenv("DVT_ADMIN_USERS", "auto")
    users.unlink()
    assert admin_users.main(argv, runner=FakeDocker(add_rc=2)) == 2
    # forced python route never runs docker
    monkeypatch.setenv("DVT_ADMIN_USERS", "python")
    never = FakeDocker()
    assert admin_users.main(argv, runner=never) == 0 and never.calls == []
    monkeypatch.setenv("DVT_ADMIN_USERS", "sometimes")
    assert admin_users.main(argv, runner=never) == 2
    capsys.readouterr()


def test_admin_users_state_dir_and_caps():
    assert admin_users.state_dir_of("/x/state/admin/users") == "/x/state"
    with pytest.raises(ValueError):
        admin_users.state_dir_of("/x/state/users")
    words = admin_users.caps({"DVT_ADMIN_CTR_MEM": "128m"})
    assert words == ["--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5", "--pids-limit", "128"]
    with pytest.raises(ValueError):
        admin_users.caps({"DVT_ADMIN_CTR_PIDS": "0"})
