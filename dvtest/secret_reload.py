# SPDX-License-Identifier: MIT
"""Helpers of the SECRELOAD1 case (cases/test_secret_reload.py, docs/secreload-cases.md): a referenced secret file that is replaced
at the same path takes effect through a configuration reload, with the reload class of its key, and no secret value ever
reaches a log, an admin response or a file of out/<run>.

Stdlib only, no rig. The case uses the APRS-IS passcode file of the private aprsc (profile aprs): `edges.aprsis.passcode_file`
is Restart(dv-edge-aprsis) (config.md, reload table: "`edges.aprsis.passcode_file` contents").

  python3 -m dvtest.secret_reload scan --values-file F --root DIR [--skip REL]... [--skip-dir REL]...

scans DIR for the values listed in F (JSON, written by the case into out/<run>/secreload/scan.json with mode 0600): exit 0 when
no value is found, 1 when one is (paths and line numbers only, never a value). scripts/run.sh runs it after the logs are
collected, like the TURN secret scan.
"""
import argparse
import json
import os
import re
import secrets
import sys

MARKER = "# secreload1 reload marker"
RESTART_ACTION = "restart"
CONTEXT_PREFIX = r"(?i)(?:pass(?:code)?|secret|password)[\"']?\s*[=:]?\s*[\"']?"
TOKEN_BOUNDARY = r"A-Za-z0-9_.:/\-"
WIRE_SUFFIXES = (".pcap", ".pcapng")


# ---------------------------------------------------------------- values

def new_passcode(current, rng=None):
    """A throwaway passcode with the same number of digits as `current` (a string of ASCII digits, optional trailing newline),
    different from it, same trailing newline. The edge accepts at most five ASCII digits; the private aprsc logs in any other
    value than the public hash of the callsign as `unverified`, so the new value is observable as a verified -> unverified
    change. Raises ValueError for a value that is not 1 to 5 digits."""
    body = current.strip()
    if not re.fullmatch(r"[0-9]{1,5}", body):
        raise ValueError("the passcode file does not hold 1 to 5 digits")
    rng = rng or secrets.SystemRandom()
    low = 10 ** (len(body) - 1) if len(body) > 1 else 0
    high = 10 ** len(body) - 1
    while True:
        value = str(rng.randint(low, high))
        if value != body:
            return value + current[len(current.rstrip("\r\n")):]


def with_marker(text, n):
    """`text` (a TOML file) with exactly one reload marker comment, numbered `n`, as its last line. A comment changes the file
    hash and nothing else, so a reload follows (the admin API applies nothing for byte-identical text: `noop`)."""
    kept = [line for line in text.splitlines() if not line.startswith(MARKER)]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n\n%s %d\n" % (MARKER, n)


def digest_note(value):
    """What may be logged about a secret value: its length only."""
    return "%d characters" % len(value)


# ---------------------------------------------------------------- scanning

def patterns(value, token=False):
    """Compiled regexes that find `value`: after a word such as pass / passcode / secret / password, or alone on a line; with
    `token` also as a bare token (not inside a longer word, number, path, address or time stamp)."""
    quoted = re.escape(value)
    out = [re.compile(CONTEXT_PREFIX + quoted + r"(?![0-9A-Za-z])"), re.compile(r"(?m)^\s*" + quoted + r"\s*$")]
    if token:
        out.append(re.compile(r"(?<![%s])%s(?![%s])" % (TOKEN_BOUNDARY, quoted, TOKEN_BOUNDARY)))
    return out


def find_in_text(text, entries):
    """[(name, line number)] of the entries whose value is in `text`; entries are dicts {name, value, token}."""
    hits = []
    for entry in entries:
        for pattern in patterns(entry["value"], bool(entry.get("token"))):
            match = pattern.search(text)
            if match:
                hits.append((entry["name"], text.count("\n", 0, match.start()) + 1))
                break
    return hits


def scan_tree(root, entries, skip=(), skip_dirs=(), skip_suffixes=WIRE_SUFFIXES, max_bytes=64 << 20):
    """[(relative path, name, line)] of the files under `root` that hold a value of `entries`. Skipped: the relative paths in
    `skip`, whole directories in `skip_dirs`, files with a suffix in `skip_suffixes` (the wire carries a passcode by protocol),
    symbolic links, files over `max_bytes`. Binary files are read as latin-1 so that no byte sequence is missed."""
    skip = {os.path.normpath(s) for s in skip}
    skip_dirs = {os.path.normpath(s) for s in skip_dirs}
    hits = []
    for base, dirs, files in os.walk(root):
        rel_base = os.path.normpath(os.path.relpath(base, root))
        dirs[:] = sorted(d for d in dirs if os.path.normpath(os.path.join(rel_base, d)) not in skip_dirs)
        for name in sorted(files):
            rel = os.path.normpath(os.path.join(rel_base, name))
            path = os.path.join(base, name)
            if rel in skip or name.endswith(tuple(skip_suffixes)) or os.path.islink(path):
                continue
            try:
                if os.path.getsize(path) > max_bytes:
                    continue
                with open(path, "rb") as fh:
                    text = fh.read().decode("latin-1")
            except OSError:
                continue
            for found, line in find_in_text(text, entries):
                hits.append((rel, found, line))
    return hits


def collision_free_passcode(current, root, rng=None, tries=200, skip=(), skip_dirs=()):
    """A new_passcode(current) that does not occur as a bare token in any text file of `root` yet, so that a later hit is the
    value and not a coincidence (a port, a pid, a time). ValueError when none is found in `tries` draws."""
    for _ in range(tries):
        value = new_passcode(current, rng)
        if not scan_tree(root, [{"name": "candidate", "value": value.strip(), "token": True}], skip=skip, skip_dirs=skip_dirs):
            return value
    raise ValueError("no collision-free passcode in %d draws" % tries)


def entries_for(old, new):
    """The scan entries of the old and the new passcode: both after a pass / secret word or alone on a line, the new one (a draw
    that is collision free at the start of the case) also as a bare token."""
    return [{"name": "old", "value": old.strip(), "token": False}, {"name": "new", "value": new.strip(), "token": True}]


class Leaks:
    """Collects the places a value shows up while the case runs (admin responses, the notes the case wrote): check(label, text)
    keeps a label and a line number, never the value."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.found = []

    def check(self, label, text):
        if isinstance(text, bytes):
            text = text.decode("latin-1")
        for name, line in find_in_text(text, self.entries):
            self.found.append("%s: the %s value at line %d" % (label, name, line))
        return not self.found

    def report(self):
        return "; ".join(self.found)


# ---------------------------------------------------------------- admin replies

def reload_actions(reply, child_needle="aprsis"):
    """The `actions` of an admin config reply (preview or apply) that concern `child_needle` and restart a child:
    [dict]. An action is {class / kind, child, path, impact} (shapes differ by version, so the whole action is matched as
    lower-case JSON text)."""
    actions = reply.get("actions") if isinstance(reply, dict) else None
    out = []
    for action in actions or []:
        text = json.dumps(action, sort_keys=True).lower()
        if child_needle in text and RESTART_ACTION in text:
            out.append(action)
    return out


def names_secret_change(action, key="passcode_file"):
    """True when the action's text names the dotted key and says `content changed` (config.md: an action preview names the key and
    reveals no value)."""
    text = json.dumps(action, sort_keys=True).lower()
    return key in text and "content changed" in text


def error_names(problem_json, key, path):
    """(names the key, names the path) of a refusal body (an RFC 7807 problem with `errors[]`, or any text)."""
    text = json.dumps(problem_json, sort_keys=True) if not isinstance(problem_json, str) else problem_json
    return key in text, path in text


def capability(preview, strict):
    """(has SECRELOAD1, reason). The signal is the one config.md defines: an admin PREVIEW of an unchanged TOML, taken while a
    referenced secret file holds other bytes than the running generation read, lists the Restart of the owning child with the
    reason `<key> content changed`. An image without the rule lists nothing (a secret is not part of the reload plan). `strict`
    (DVT_SECRELOAD1=on) turns "absent" into a failure for the caller to raise; the reason says what was seen."""
    found = reload_actions(preview)
    if found and any(names_secret_change(a) for a in found):
        return True, "preview lists the restart of the owning child with `passcode_file content changed`"
    if found:
        return False, "preview restarts the child but does not name the key and `content changed` (config.md, reload table)"
    return False, ("preview of an unchanged TOML with a changed passcode file lists no restart of the APRS-IS edge: the image "
                   "does not treat a referenced secret's content as part of the reload plan (config.md, reload table)")


def read_only_reply(status, body):
    """True when an admin apply was refused because the configuration file cannot be replaced (the rig mounts it read-only unless
    run.sh gets DVT_SUT_CONFIG_RW=1)."""
    text = body if isinstance(body, str) else json.dumps(body, sort_keys=True)
    return status >= 400 and "read-only" in text.lower().replace("read_only", "read-only")


# ---------------------------------------------------------------- hub metrics

def child_restarts(samples, needle="aprsis"):
    """The sum of dv_launcher_child_restarts_total of the children whose `child` label contains `needle` (None when no such series
    is exported), and the total over all children. `samples` is a list from dvtest.metrics.parse()."""
    own, everything, seen = 0.0, 0.0, False
    for s in samples:
        if s.name != "dv_launcher_child_restarts_total":
            continue
        everything += s.value
        if needle in s.labels.get("child", ""):
            own += s.value
            seen = True
    return (own if seen else None), everything


def verified(samples):
    """dv_edge_aprsis_verified as True / False, None when the series is absent."""
    values = [s.value for s in samples if s.name == "dv_edge_aprsis_verified"]
    return None if not values else max(values) >= 1


# ---------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.secret_reload")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--values-file", required=True)
    sc.add_argument("--root", required=True)
    sc.add_argument("--skip", action="append", default=[])
    sc.add_argument("--skip-dir", action="append", default=[])
    args = ap.parse_args(argv)
    with open(args.values_file, "r", encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) and e.get("value") for e in entries):
        print("secret_reload scan: the values file holds no values", file=sys.stderr)
        return 2
    hits = scan_tree(args.root, entries, skip=args.skip, skip_dirs=args.skip_dir)
    for rel, name, line in hits:
        print("secret_reload scan: %s holds the %s value (line %d)" % (rel, name, line))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
