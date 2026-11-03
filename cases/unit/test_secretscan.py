# SPDX-License-Identifier: MIT
"""dvtest.secretscan: finds a secret in a directory tree and in pending git changes, never prints it."""
import subprocess

import pytest

from dvtest import secretscan

pytestmark = pytest.mark.unit

SECRET = b"Zq7-fixture-secret"


def test_scan_root_finds_across_chunk_boundary(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "clean.log").write_bytes(b"nothing here\n")
    (tmp_path / "sub" / "hit.log").write_bytes(b"x" * ((1 << 20) - 5) + SECRET + b"tail")
    (tmp_path / "other.bin").write_bytes(SECRET[:-1] + b"?")
    assert secretscan.scan_root(str(tmp_path), SECRET) == ["sub/hit.log"]


def test_main_exit_codes_and_no_value_in_output(tmp_path, capsys):
    f = tmp_path / "s"
    f.write_bytes(SECRET + b"\n")
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.txt").write_bytes(b"clean")
    assert secretscan.main(["--secret-file", str(f), "--root", str(root)]) == 0
    (root / "b.txt").write_bytes(b"prefix " + SECRET)
    assert secretscan.main(["--secret-file", str(f), "--root", str(root)]) == 1
    seen = capsys.readouterr()
    assert SECRET.decode() not in seen.out + seen.err
    assert "b.txt" in seen.err


def test_short_secret_is_refused(tmp_path):
    f = tmp_path / "s"
    f.write_bytes(b"short\n")
    assert secretscan.main(["--secret-file", str(f), "--root", str(tmp_path)]) == 2


def test_git_diff_and_untracked(tmp_path, monkeypatch):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "n0call")
    git("config", "user.name", "n0call")
    (tmp_path / "t.txt").write_text("one\n")
    git("add", "t.txt")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    assert secretscan.scan_git(SECRET) == []
    (tmp_path / "t.txt").write_bytes(b"one\n" + SECRET + b"\n")
    assert secretscan.scan_git(SECRET) == ["diff"]
    (tmp_path / "t.txt").write_text("one\n")
    (tmp_path / "new.txt").write_bytes(SECRET)
    assert secretscan.scan_git(SECRET) == ["new.txt"]
    (tmp_path / ".gitignore").write_text("new.txt\n")
    assert secretscan.scan_git(SECRET) == []
