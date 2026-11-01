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
