# SPDX-License-Identifier: MIT
"""Find a secret (the bytes of a file) in directories and in the pending git changes, without ever printing it.

  python3 -m dvtest.secretscan --secret-file F [--root DIR ...] [--skip REL ...] [--git]

Exit 0: not found; 1: found (the relative paths go to stderr, never the value); 2: usage or read error.
--root DIR   every regular file under DIR (symlinks skipped), read in chunks
--skip REL   a path relative to DIR (a file, or a directory with all below it) that is not read: where the secret is meant to be
--git        `git diff HEAD`, `git diff --cached`, and every untracked file that is not ignored (cwd must be the repo)
The secret is the file content with surrounding whitespace removed; it must be at least 8 bytes (a shorter value
would match by accident). Stdlib only. Used by scripts/run.sh (profile asl-ext) and before committing its results.
"""
import argparse
import os
import subprocess
import sys

MIN_LEN = 8


def load_secret(path):
    with open(path, "rb") as fh:
        secret = fh.read().strip()
    if len(secret) < MIN_LEN:
        raise ValueError("secret file is empty or shorter than %d bytes" % MIN_LEN)
    return secret


def file_has(path, secret):
    """True when the file's bytes contain the secret (chunked, overlap kept)."""
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                return False
            if secret in tail + chunk:
                return True
            tail = chunk[-len(secret):]


def scan_root(root, secret, skip=()):
    """Relative paths under root whose bytes contain the secret. `skip`: relative paths (a file, or a directory with everything below
    it) that are not read: the files that legitimately hold the secret."""
    hits = []
    skip = tuple(os.path.normpath(x) for x in skip)
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            rel = os.path.normpath(os.path.relpath(path, root))
            if os.path.islink(path) or any(rel == x or rel.startswith(x + os.sep) for x in skip):
                continue
            try:
                if file_has(path, secret):
                    hits.append(os.path.relpath(path, root))
            except OSError:
                continue
    return sorted(hits)


def _git(args):
    return subprocess.run(["git"] + args, capture_output=True, check=True).stdout


def scan_git(secret):
    """Labels ("diff", "diff-cached", untracked paths) of the pending git changes that contain the secret."""
    hits = []
    for label, args in (("diff", ["diff", "HEAD"]), ("diff-cached", ["diff", "--cached"])):
        if secret in _git(args):
            hits.append(label)
    for rel in _git(["ls-files", "--others", "--exclude-standard", "-z"]).split(b"\0"):
        if not rel:
            continue
        path = rel.decode("utf-8", "surrogateescape")
        if os.path.isfile(path) and not os.path.islink(path):
            try:
                if file_has(path, secret):
                    hits.append(path)
            except OSError:
                continue
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.secretscan")
    ap.add_argument("--secret-file", required=True)
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--git", action="store_true")
    ap.add_argument("--skip", action="append", default=[], help="path relative to every --root that is not scanned (repeatable)")
    args = ap.parse_args(argv)
    try:
        secret = load_secret(args.secret_file)
        hits = []
        for root in args.root:
            hits += ["%s: %s" % (root, h) for h in scan_root(root, secret, args.skip)]
        if args.git:
            hits += ["git: %s" % h for h in scan_git(secret)]
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("secretscan: %s" % exc.__class__.__name__, file=sys.stderr)
        return 2
    for h in hits:
        print("secretscan: secret found in %s" % h, file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
