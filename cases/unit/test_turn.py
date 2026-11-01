# SPDX-License-Identifier: MIT
"""dvtest/turn.py: coturn configuration of the NAT rig, the secret file and the credential checks."""
import base64
import hashlib
import hmac
import os
import stat

import pytest

from dvtest import turn

pytestmark = pytest.mark.unit

SECRET = "A" * 16 + "b-_" * 4


def test_credential_matches_the_rest_api_recipe():
    user = "1900000000:opaque-tag"
    want = base64.b64encode(hmac.new(SECRET.encode(), user.encode(), hashlib.sha1).digest()).decode()
    assert turn.credential(SECRET, user) == want


def test_check_ice_server_accepts_a_good_entry_and_names_problems():
    now = 1_900_000_000
    user = "%d:tagtag" % (now + 120)
    good = {"urls": ["turn:172.30.0.94:3478?transport=udp"], "username": user, "credential": turn.credential(SECRET, user)}
    assert turn.check_ice_server(good, SECRET, now=now, ttl=120) == []
    assert any("expiry" in p for p in turn.check_ice_server(dict(good, username="%d:t" % (now - 5)), SECRET, now=now))
    assert any("ahead" in p for p in turn.check_ice_server(good, SECRET, now=now, ttl=600))
    assert any("credential" in p for p in turn.check_ice_server(dict(good, credential="x"), SECRET, now=now))
    assert any("username" in p for p in turn.check_ice_server(dict(good, username="nocolon"), SECRET, now=now))
    assert any("urls" in p for p in turn.check_ice_server(dict(good, urls=["http://x"]), SECRET, now=now))
    # the secret never shows in a message
    assert not any(SECRET in p for p in turn.check_ice_server(dict(good, credential="x"), SECRET, now=now))


def test_denied_ranges_are_the_templates():
    assert turn.DENIED == ("0.0.0.0-255.255.255.255", "10.0.0.0-10.255.255.255", "127.0.0.0-127.255.255.255",
                           "169" + ".254.0.0-169" + ".254.255.255", "172.16.0.0-172.31.255.255", "192.168.0.0-192.168.255.255")


def test_render_conf_follows_the_template():
    text = turn.render_conf(SECRET, "172.30.0.94", ["172.30.0.11"])
    lines = text.splitlines()
    for want in ("use-auth-secret", "static-auth-secret=" + SECRET, "no-tcp-relay", "no-multicast-peers", "no-tls",
                 "allowed-peer-ip=172.30.0.11", "denied-peer-ip=172.16.0.0-172.31.255.255", "denied-peer-ip=192.168.0.0-192.168.255.255",
                 "listening-ip=172.30.0.94", "relay-ip=172.30.0.94", "min-port=49160", "max-port=49180", "user-quota=4"):
        assert want in lines, want
    assert "allowed-peer-ip=172.30.0.0-172.30.0.255" not in lines


@pytest.mark.parametrize("kw", [dict(listen_ip="x"), dict(allowed_peers=[]), dict(allowed_peers=["a"]), dict(realm="a b"),
                                dict(secret="short"), dict(min_port=60000, max_port=50000)])
def test_render_conf_refuses_bad_input(kw):
    args = dict(secret=SECRET, listen_ip="172.30.0.94", allowed_peers=["172.30.0.11"])
    args.update(kw)
    with pytest.raises(ValueError):
        turn.render_conf(**args)


def test_make_writes_private_files(tmp_path):
    s, c = tmp_path / "turn.secret", tmp_path / "turnserver.conf"
    assert turn.main(["make", "--secret-file", str(s), "--conf", str(c), "--listen-ip", "172.30.0.94", "--allowed-peer", "172.30.0.11"]) == 0
    for f in (s, c):
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    secret = s.read_text()
    assert len(secret) >= 32 and "\n" not in secret
    assert "static-auth-secret=" + secret in c.read_text()
    # a second run makes a different secret
    s2 = tmp_path / "turn2.secret"
    turn.main(["make", "--secret-file", str(s2), "--conf", str(tmp_path / "c2"), "--listen-ip", "172.30.0.94", "--allowed-peer", "172.30.0.11"])
    assert s2.read_text() != secret


def test_scan_finds_the_secret_without_printing_it(tmp_path, capsys):
    s = tmp_path / "web" / "turn.secret"
    s.parent.mkdir()
    secret = turn.new_secret()
    s.write_text(secret)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "clean.log").write_text("nothing here\n")
    (tmp_path / "logs" / "bad.log").write_text("x" * 5 + secret + "y")
    (tmp_path / "cap.pcapng").write_bytes(b"\x00" * 100 + secret.encode() + b"\x00")
    assert turn.scan(str(s), str(tmp_path), skip=["web/turn.secret"]) == ["cap.pcapng", "logs/bad.log"]
    assert turn.main(["scan", "--secret-file", str(s), "--root", str(tmp_path), "--skip", "web/turn.secret"]) == 1
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    (tmp_path / "logs" / "bad.log").unlink()
    (tmp_path / "cap.pcapng").unlink()
    assert turn.main(["scan", "--secret-file", str(s), "--root", str(tmp_path), "--skip", "web/turn.secret"]) == 0


def test_scan_sees_a_secret_split_across_read_chunks(tmp_path):
    s = tmp_path / "s"
    secret = turn.new_secret()
    s.write_text(secret)
    big = tmp_path / "big.bin"
    big.write_bytes(b"a" * ((1 << 20) - 10) + secret.encode() + b"b" * 50)
    assert turn.scan(str(s), str(tmp_path), skip=["s"]) == ["big.bin"]
