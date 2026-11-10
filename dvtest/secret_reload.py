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

  python3 -m dvtest.secret_reload logcheck --log LOGFILE --expect F [--shape-file OUT]

checks the collected launcher log for what the SIGHUP variant expects (F: JSON {t_hup, key, path}, written by the case): a line at or
after t_hup (docker time stamps) that names the key and the path, i.e. the refusal of a reload whose secret file is unreadable.
Exit 0 and the SHAPE of the line (digits replaced by N, the service prefix cut) when there is one, 1 otherwise. Never a value.
"""
import argparse
import calendar
import json
import os
import re
import secrets
import sys
import time

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
    """True when the action names the key as a changed secret: its text says `<key> ... content changed` (config.md: an action preview
    names the key and reveals no value), or, as the admin HTTP reply of 11e19ace carries it, its `changed_secret_keys` list holds a
    dotted key ending in `key`."""
    text = json.dumps(action, sort_keys=True).lower()
    if key in text and "content changed" in text:
        return True
    keys = action.get("changed_secret_keys") if isinstance(action, dict) else None
    return isinstance(keys, list) and any(isinstance(k, str) and k.endswith(key) for k in keys)


def validation_problems(body, key=None):
    """What is wrong with the 422 body of a refused reload (admin.md, 11e19ace): problem+json `code` validation, `errors[]` of
    {path, pointer, msg} naming the dotted key, `current_etag` (unquoted), and NO `actions`. [] when it is as specified."""
    key = key or RELOAD_KEY
    if not isinstance(body, dict):
        return ["the 422 body is not a JSON object"]
    out = []
    if body.get("code") != "validation":
        out.append("code %r, want validation" % (body.get("code"),))
    if "actions" in body:
        out.append("a 422 carries no actions[]")
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return out + ["errors[] is missing or empty"]
    for index, error in enumerate(errors):
        if not isinstance(error, dict) or not all(isinstance(error.get(f), str) for f in ("path", "pointer", "msg")):
            out.append("errors[%d] is not {path, pointer, msg}" % index)
    if not any(isinstance(e, dict) and e.get("path") == key for e in errors):
        out.append("no error has path %s" % key)
    if "current_etag" in body and (not isinstance(body["current_etag"], str) or body["current_etag"].startswith(('"', "W/"))):
        out.append("current_etag %r is not an unquoted tag" % (body["current_etag"],))
    return out


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


# ---------------------------------------------------------------- SECRELOAD2: GET / POST /api/v1/admin/config/reload

PREVIEW_KEYS = {"mode", "etag", "changed", "no_change", "actions", "warnings"}
APPLIED_KEYS = {"status", "etag", "changed", "no_change", "inc", "gen", "actions", "pending_restart"}
RELOAD_KEY = "edges.aprsis.passcode_file"        # the dotted key name `changed` lists for the APRS-IS passcode file


def etag_value(header):
    """The bare entity tag of an ETag / If-Match header value: the W/ prefix and the quotes cut."""
    value = (header or "").strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"')


def stale_etag(current):
    """A well-formed entity tag that is not `current`: its last character changed."""
    bare = etag_value(current)
    if not bare:
        raise ValueError("no ETag to make stale")
    return '"%s%s"' % (bare[:-1], "0" if bare[-1] != "0" else "1")


def weak_etag(current):
    """The weak form of `current`: a weak tag never matches a write precondition (RFC 9110, 13.1.1)."""
    return 'W/"%s"' % etag_value(current)


def _list_of_str(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def preview_problems(body, header_etag=None):
    """What is wrong with a GET /config/reload 200 body (ReloadPreview: the exact keys, types, mode `preview`, and an `etag` equal to
    the ETag header when given; an empty `actions` goes with no_change true). [] when it is as specified."""
    if not isinstance(body, dict):
        return ["the preview is not a JSON object"]
    out = []
    if set(body) != PREVIEW_KEYS:
        out.append("keys %s, want %s" % (sorted(body), sorted(PREVIEW_KEYS)))
    if body.get("mode") != "preview":
        out.append("mode %r, want preview" % (body.get("mode"),))
    out += _common_problems(body, header_etag)
    if not isinstance(body.get("warnings"), list):
        out.append("warnings is not a list")
    return out


def applied_problems(body, header_etag=None):
    """The same for a POST /config/reload 200 body (ReloadApplied): status `applied`, inc and gen non-negative integers,
    pending_restart a list of strings."""
    if not isinstance(body, dict):
        return ["the reply is not a JSON object"]
    out = []
    if set(body) != APPLIED_KEYS:
        out.append("keys %s, want %s" % (sorted(body), sorted(APPLIED_KEYS)))
    if body.get("status") != "applied":
        out.append("status %r, want applied" % (body.get("status"),))
    for name in ("inc", "gen"):
        value = body.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            out.append("%s %r is not a non-negative integer" % (name, value))
    if not _list_of_str(body.get("pending_restart")):
        out.append("pending_restart is not a list of strings")
    out += _common_problems(body, header_etag)
    return out


def _common_problems(body, header_etag):
    out = []
    if not isinstance(body.get("etag"), str) or not body.get("etag"):
        out.append("etag is not a non-empty string")
    elif header_etag is not None and etag_value(header_etag) != body["etag"]:
        out.append("etag in the body differs from the ETag header")
    if not _list_of_str(body.get("changed")):
        out.append("changed is not a list of strings")
    if not isinstance(body.get("no_change"), bool):
        out.append("no_change is not a boolean")
    if not isinstance(body.get("actions"), list):
        out.append("actions is not a list")
    elif body.get("no_change") is True and (body.get("actions") or body.get("changed")):
        out.append("no_change is true but changed / actions are not empty")
    elif body.get("no_change") is False and not body.get("actions") and not body.get("changed"):
        out.append("no_change is false but changed and actions are empty")
    return out


def problem_code(body):
    """The `code` of a problem+json body, None when it has none."""
    return body.get("code") if isinstance(body, dict) else None


def audit_items(body):
    """The records of an audit tail ({total, items}), [] for anything else."""
    items = body.get("items") if isinstance(body, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def reload_audit(body, user=None, actions=("config.reload", "config.reload.preview")):
    """[(action, user, result, changed key names)] of the reload records of an audit tail (oldest first as served), optionally of
    one user."""
    out = []
    for item in audit_items(body):
        if item.get("action") not in actions or (user is not None and item.get("user") != user):
            continue
        diff = item.get("diff")
        changed = diff.get("changed") if isinstance(diff, dict) else None
        out.append((item.get("action"), item.get("user"), item.get("result"), changed))
    return out


def history_count(body):
    """The number of entries of a GET /config/history body, {total, items:[{seq, etag, replaced_by, ts_ms, user, comment, source}]} newest
    first (a bare list is accepted too); None when unrecognised."""
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return len(body["items"])
    return None


# ---------------------------------------------------------------- launcher log

LOG_TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(\.\d+)?Z")
LOG_PREFIX = re.compile(r"^\S+\s*\|\s*")


def log_epoch(line):
    """The epoch seconds of the docker time stamp in `line` (UTC), None without one."""
    m = LOG_TIME.search(line)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(m.group(i)) for i in range(1, 7))
    return calendar.timegm((y, mo, d, h, mi, sec)) + (float(m.group(7)) if m.group(7) else 0.0)


def line_shape(line, limit=200):
    """A log line without anything that varies or could carry a value: the compose prefix and the time stamp cut, every run of
    digits replaced by N, cut to `limit` characters."""
    text = LOG_PREFIX.sub("", line.rstrip("\n"))
    text = LOG_TIME.sub("", text, count=1).strip()
    return re.sub(r"[0-9]+", "N", text)[:limit]


def refusal_lines(text, t_hup, key, path, slack=1.0):
    """The shapes of the lines of a launcher log at or after t_hup - slack that name both the key and the path."""
    out = []
    for line in text.splitlines():
        at = log_epoch(line)
        if at is not None and at >= t_hup - slack and key in line and path in line:
            out.append(line_shape(line))
    return out


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

def logcheck(args):
    try:
        with open(args.expect, "r", encoding="utf-8") as fh:
            expect = json.load(fh)
        with open(args.log, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        t_hup, key, path = float(expect["t_hup"]), str(expect["key"]), str(expect["path"])
    except (OSError, ValueError, KeyError) as exc:
        print("secret_reload logcheck: cannot read the log or the expectation: %s" % type(exc).__name__, file=sys.stderr)
        return 2
    shapes = refusal_lines(text, t_hup, key, path)
    if not shapes:
        print("secret_reload logcheck: no launcher log line at or after the SIGHUP (%s) names %s and %s" % (
            time.strftime("%H:%M:%S", time.gmtime(t_hup)), key, path))
        return 1
    for shape in sorted(set(shapes)):
        print("secret_reload logcheck: %d line(s) of the shape: %s" % (shapes.count(shape), shape))
    if args.shape_file:
        with open(args.shape_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(set(shapes))) + "\n")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.secret_reload")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--values-file", required=True)
    sc.add_argument("--root", required=True)
    sc.add_argument("--skip", action="append", default=[])
    sc.add_argument("--skip-dir", action="append", default=[])
    lc = sub.add_parser("logcheck")
    lc.add_argument("--log", required=True)
    lc.add_argument("--expect", required=True)
    lc.add_argument("--shape-file")
    args = ap.parse_args(argv)
    if args.cmd == "logcheck":
        return logcheck(args)
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
