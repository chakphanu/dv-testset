# SPDX-License-Identifier: MIT
"""Pure helpers of the config-load cases (cases/test_config_load.py, docs/config-load-cases.md): COMPAT1 removed the backward-compat
shims of DV-Router's config, so an OLD key or shape must be refused with a clear error and never be ignored. No docker, no sockets.

  Case                       one mutation of the RENDERED rig template (compat1 dialect) and what the check must answer
  CASES                      the table (rejections first, then the valid controls: the same thing in its new spelling)
  apply(case, text)          (mutated text, expected line number or None)
  evaluate(case, verdict, exit_code)   the list of problems of a `dvtest.tools.config_check.judge_check` verdict (empty = as expected)
  record(case, ...)          the JSON row kept per case (exact error text, exit status, line, key)

The mutations work on the rendered text, after sut_config.render(gen="compat1"): the renderer already rewrites the old spellings to the
new ones for the rig, so the old spelling has to be put back by the case. Lines are 1-based like the router's `FILE:LINE:COL:` prefix.
Not a case (cannot be one): env DV_MMDVM_* is silently ignored on compat1 (there is no file to refuse), see docs/config-load-cases.md.
"""
import dataclasses
import os
import re

REJECT, ACCEPT = "reject", "accept"
REJECT_CODES = (1, 2)       # wip-488b89d exits 2 for an invalid file; 1 is accepted too, 0 never is
FILE_NAME = "dv-router.toml"


class MutationError(Exception):
    """The anchor of a mutation is not in the template (the rig template changed)."""


def _header_index(lines, header):
    want = re.compile(r"^\s*" + re.escape(header) + r"\s*(#.*)?$")
    for n, line in enumerate(lines):
        if want.match(line):
            return n
    raise MutationError("no %s table in the rendered template" % header)


def insert_after(text, header, new_lines):
    """New lines right after the `header` line; the expected error line is the first new line."""
    lines = text.split("\n")
    n = _header_index(lines, header) + 1
    lines[n:n] = list(new_lines)
    return "\n".join(lines), n + 1


def insert_before(text, header, new_lines):
    """New lines right before the `header` line; the expected error line is the first new line."""
    lines = text.split("\n")
    n = _header_index(lines, header)
    lines[n:n] = list(new_lines)
    return "\n".join(lines), n + 1


def rename_header(text, old, new):
    """The `old` table header line becomes `new`; the expected line is that line."""
    lines = text.split("\n")
    n = _header_index(lines, old)
    lines[n] = lines[n].replace(old, new, 1)
    return "\n".join(lines), n + 1


def replace_key(text, header, key, new_line):
    """The first `key = ...` line inside the `header` table becomes `new_line`; the expected line is that line."""
    lines = text.split("\n")
    n = _header_index(lines, header) + 1
    want = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    while n < len(lines) and not lines[n].lstrip().startswith("["):
        if want.match(lines[n]):
            lines[n] = new_line
            return "\n".join(lines), n + 1
        n += 1
    raise MutationError("no `%s` key in %s of the rendered template" % (key, header))


def unchanged(text):
    return text, None


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    what: str
    verdict: str                  # REJECT or ACCEPT
    mutate: object                # text -> (text, expected line or None)
    key: str = ""                 # REJECT: the key the router must name (e.g. "admin.totp"); "" = any (a syntax error has none)
    hint: str = ""                # REJECT: a substring the message must contain (the new spelling, the expected type ...)
    pair: str = ""                # the id of the control (new spelling) / of the rejection this one belongs to


def _peer(kind):
    return ([] if not kind else ['kind = "xlx"']) + ['callsign = "XLX998"', 'host = "172.30.0.12"', 'modules = "DE"']


CASES = (
    Case("baseline", "the rendered rig template itself loads", ACCEPT, unchanged),
    # ---- old keys / shapes that must be refused
    Case("admin-totp", "[admin] totp (now totp_required)", REJECT,
         lambda t: insert_after(t, "[admin]", ["totp = true"]), key="admin.totp", hint="totp_required", pair="admin-totp_required"),
    Case("web-operator_totp", "[web] operator_totp (now totp_required)", REJECT,
         lambda t: insert_after(t, "[web]", ["operator_totp = true"]), key="web.operator_totp", hint="totp_required",
         pair="web-totp_required"),
    Case("xlx-peer-no-kind", "[[edges.xlx.peer]] row without kind = \"xlx\"", REJECT,
         lambda t: insert_before(t, "[edges.ysf]", ["[[edges.xlx.peer]]"] + _peer(False) + [""]), key="edges.xlx.peer[0]",
         hint="kind", pair="xlx-peer-kind"),
    Case("edges-mmdvm", "[edges.mmdvm] (now [edges.dmrhb])", REJECT,
         lambda t: rename_header(t, "[edges.dmrhb]", "[edges.mmdvm]"), key="edges.mmdvm", hint="dmrhb", pair="baseline"),
    Case("fabric-acl", "[fabric] acl = true (the fabric ACL is gone)", REJECT,
         lambda t: insert_before(t, "[log]", ["[fabric]", "acl = true", ""]), key="fabric", pair="baseline"),
    Case("fabric-empty", "an empty [fabric] table", REJECT,
         lambda t: insert_before(t, "[log]", ["[fabric]", ""]), key="fabric", pair="baseline"),
    Case("access-allow_file", "[access] allow_file (lists go inline in allow)", REJECT,
         lambda t: insert_after(t, "[access]", ['allow_file = "/etc/dv-router/allow.txt"']), key="access.allow_file", hint="allow",
         pair="access-allow-inline"),
    Case("access-deny_file", "[access] deny_file (lists go inline in deny)", REJECT,
         lambda t: insert_after(t, "[access]", ['deny_file = "/etc/dv-router/deny.txt"']), key="access.deny_file", hint="deny",
         pair="access-deny-inline"),
    Case("callhome-interlink_file", "[callhome] interlink_file (the list is the [[edges.xlx.peer]] rows)", REJECT,
         lambda t: insert_after(t, "[callhome]", ['interlink_file = "/etc/dv-router/interlink.txt"']), key="callhome.interlink_file",
         pair="baseline"),
    # ---- not a removed key: any unknown key and any wrong type is refused the same way
    Case("web-unknown-key", "a typo in [web]: enabel", REJECT,
         lambda t: insert_after(t, "[web]", ["enabel = true"]), key="web.enabel", hint="enable", pair="baseline"),
    Case("web-wrong-type", "[web] enable = \"yes\" (a string for a bool)", REJECT,
         lambda t: replace_key(t, "[web]", "enable", 'enable = "yes"'), key="web.enable", hint="boolean", pair="baseline"),
    Case("log-level-wrong-type", "[log] level = 5 (a number for a string)", REJECT,
         lambda t: replace_key(t, "[log]", "level", "level = 5"), key="log.level", pair="baseline"),
    Case("toml-syntax", "a TOML syntax error in [web]", REJECT,
         lambda t: insert_after(t, "[web]", ["enable = = false"]), key="", pair="baseline"),
    # ---- the same things in their new spelling must load
    Case("admin-totp_required", "[admin] totp_required", ACCEPT,
         lambda t: insert_after(t, "[admin]", ["totp_required = true"]), pair="admin-totp"),
    Case("web-totp_required", "[web] totp_required", ACCEPT,
         lambda t: insert_after(t, "[web]", ["totp_required = true"]), pair="web-operator_totp"),
    Case("xlx-peer-kind", "[[edges.xlx.peer]] row with kind = \"xlx\"", ACCEPT,
         lambda t: insert_before(t, "[edges.ysf]", ["[[edges.xlx.peer]]"] + _peer(True) + [""]), pair="xlx-peer-no-kind"),
    Case("access-allow-inline", "[access] allow = [...] inline", ACCEPT,
         lambda t: replace_key(t, "[access]", "allow", 'allow = ["N0XYZ"]'), pair="access-allow_file"),
    Case("access-deny-inline", "[access] deny = [...] inline", ACCEPT,
         lambda t: replace_key(t, "[access]", "deny", 'deny = ["N0XYZ"]'), pair="access-deny_file"),
)


def by_id(case_id):
    for case in CASES:
        if case.id == case_id:
            return case
    raise KeyError(case_id)


def apply(case, text):
    """(mutated text, expected error line or None)."""
    return case.mutate(text)


def evaluate(case, verdict, exit_code, expected_line=None):
    """Problems (list of strings) of the answer of `config check` for `case`; empty = as expected.
    REJECT: never a silent success (exit status 0), the exit status is one of REJECT_CODES, at least one error line, the first names the
    file (basename dv-router.toml), a line (the line of the mutation, when known), the key (when the case names one) and a message
    that is not empty (and contains the hint). ACCEPT: exit status 0 and no error line."""
    problems = []
    if verdict.get("unsupported"):
        return ["the image has no `config check` (usage text printed); use a compat1 image"]
    if case.verdict == ACCEPT:
        if exit_code != 0 or not verdict.get("valid"):
            problems.append("expected to load, got exit status %r: %s" % (exit_code, "; ".join(verdict.get("errors") or ["no error line"])))
        return problems
    if exit_code == 0 or verdict.get("valid"):
        return ["SILENT SUCCESS: the old key/shape loaded (exit status 0); expected an error naming %s" % (case.key or "the line")]
    if exit_code not in REJECT_CODES:
        problems.append("exit status %r, expected one of %r" % (exit_code, REJECT_CODES))
    parsed = verdict.get("parsed") or []
    if not parsed:
        return problems + ["no `FILE:LINE:COL: message` error line in the output (non-zero exit status %r only)" % (exit_code,)]
    first = parsed[0]
    if os.path.basename(first["file"]) != FILE_NAME:
        problems.append("the error does not name the file %s (got %r)" % (FILE_NAME, first["file"]))
    if first["line"] < 1:
        problems.append("the error has no line")
    if expected_line is not None and first["line"] != expected_line:
        problems.append("the error names line %d, the mutation is on line %d" % (first["line"], expected_line))
    if case.key and first["key"] != case.key:
        problems.append("the error names key %r, expected %r" % (first["key"], case.key))
    if not first["message"].strip():
        problems.append("the error message is empty")
    if case.hint and case.hint not in first["message"]:
        problems.append("the message does not mention %r: %s" % (case.hint, first["message"]))
    return problems


def record(case, verdict, exit_code, expected_line, problems):
    """The JSON row of one case (exact error text per case)."""
    first = (verdict.get("parsed") or [None])[0]
    return {"id": case.id, "what": case.what, "expect": case.verdict, "exit_code": exit_code, "loaded": exit_code == 0,
            "expected_line": expected_line, "error_line": first["line"] if first else None, "error_col": first["col"] if first else None,
            "error_key": first["key"] if first else None, "error_message": first["message"] if first else None,
            "error_text": verdict.get("errors") or [], "warnings": verdict.get("warnings") or [], "problems": problems,
            "ok": not problems}
