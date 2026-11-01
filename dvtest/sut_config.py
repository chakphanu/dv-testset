# SPDX-License-Identifier: MIT
"""Render the dv-router.toml of the native SUT launch (DVT_SUT_LAUNCH=native).

DVT_SUT_CONFIG names a TEMPLATE; scripts/run.sh renders it on the host with

  python3 -m dvtest.sut_config TEMPLATE OUTPUT --net 172.30.0 [--own 11]
                               [--transcoder 21] [--tester 30]
                               [--require-ident true|false] [--extra FILE ...]

Plain text replacement of these tokens (a token is <UPPER_CASE_NAME>):

  <OWN_IP>          the SUT address, <net>.<own>          (rig: .11)
  <TRANSCODER_IP>   the SUT's transcoder, <net>.<transcoder> (rig: .21)
  <TESTER_IP>       the tester, <net>.<tester>            (rig: .30)
  <NET>             the /24 prefix, e.g. 172.30.0
  <REQUIRE_IDENT>   true or false: the value of --require-ident, else the env
                    DVT_REQUIRE_IDENT, else true (rig/config/dv-router/data-ident.toml)
  <WEB_PROXY_IP>    the TLS proxy in front of dv-svc-web, <net>.<web_proxy> (rig: .90,
                    profile web; rig/config/dv-router/web.toml)
  <BROWSER_IP>      the browser service, <net>.<browser> (rig: .91, profile browser;
                    rig/config/dv-router/admin.toml lets it reach the admin host)
  <COTURN_IP>       the TURN server of the NAT profile, <net>.<coturn> (rig: .94;
                    rig/config/dv-router/turn.toml)

Extras (DVT_SUT_CONFIG_EXTRA=<file>[:<file>...], --extra): each file is appended
after the template, in order, and goes through the same token substitution. A
repo-owned snippet adds a table to a template that lives outside the repository
without copying it (rig/config/dv-router/).

A plain table `[name]` of an extra that the template (or an earlier extra) already
defines is MERGED, not repeated (TOML refuses a duplicate table): the extra's keys
are inserted into the existing table and replace a key of the same name, every
other key of the template stays. This is how a snippet switches on what the
template switches off (`[web] enable = false` -> `true`) or adds one key to `[node]`.
Only single-line values can be replaced (a multi-line array or string in the
template under a key the extra sets is a ConfigError); `[[array]]` tables and
sub-tables are always appended; a merged result must parse as TOML (checked here).

Any other <UPPER_CASE_NAME> left over is an error (exit 2, nothing written),
so a template that wants a rig address not listed above is refused instead of
being mounted half rendered. Lower case text such as <net> is not a token.
"""
import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

TOKEN = re.compile(r"<([A-Z][A-Z0-9_]*)>")
KNOWN = ("OWN_IP", "TRANSCODER_IP", "TESTER_IP", "NET", "REQUIRE_IDENT", "WEB_PROXY_IP", "BROWSER_IP", "COTURN_IP")
BOOLS = ("true", "false")
_OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
_NET = re.compile(r"^%s\.%s\.%s$" % (_OCTET, _OCTET, _OCTET))


class ConfigError(ValueError):
    """A template that cannot be rendered (message is for the operator)."""


def tokens(text):
    """The distinct <TOKEN> names in the text, in order of first use."""
    seen = []
    for name in TOKEN.findall(text):
        if name not in seen:
            seen.append(name)
    return seen


def require_ident_value(value=None):
    """"true" or "false" from `value`, else env DVT_REQUIRE_IDENT, else
    "true" (the DV-Router default); ConfigError for anything else."""
    if value is None:
        value = os.environ.get("DVT_REQUIRE_IDENT") or "true"
    if value not in BOOLS:
        raise ConfigError("require_ident must be true or false (got %r)" % (value,))
    return value


def split_extras(value):
    """The files of a DVT_SUT_CONFIG_EXTRA value ("a:b", empty items skipped)."""
    return [item for item in (value or "").split(":") if item]

_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")
_KEY = re.compile(r'^\s*([A-Za-z0-9_-]+|"[^"]*")\s*=')


def _split_tables(text):
    """[[name, head, header, body]] of a TOML text: `header` is the header line,
    `head` the comment lines directly above it (they belong to the table), `body`
    the lines below it up to the next head. The first entry is the preamble (name
    and header None). `name` is the table name of a plain `[name]`, None for an
    `[[array]]` table. Joining head, header and body of all entries gives the text
    back."""
    parts = [[None, [], None, []]]
    for line in text.split("\n"):
        m = _HEADER.match(line)
        if m:
            body = parts[-1][3]
            head = []
            while body and body[-1].lstrip().startswith("#"):
                head.insert(0, body.pop())
            parts.append([m.group(2) if m.group(1) == "[" else None, head, line, []])
        else:
            parts[-1][3].append(line)
    return parts


def _join_tables(parts):
    lines = []
    for _, head, header, body in parts:
        lines.extend(head)
        if header is not None:
            lines.append(header)
        lines.extend(body)
    return "\n".join(lines)


def _spans_lines(line):
    """True when the value of a `key = value` line may continue on the next line."""
    value = line.split("=", 1)[1]
    if '"""' in value or "'''" in value:
        return True
    value = re.sub(r'"(?:[^"\\]|\\.)*"' + "|'[^']*'", "", value)   # drop one-line strings
    value = value.split("#", 1)[0]
    return value.count("[") != value.count("]") or value.count("{") != value.count("}")


def merge_extra(base, extra):
    """`base` with the text `extra` added. A plain `[name]` table of the extra that
    `base` already defines is merged key by key (module doc); everything else is
    appended after a blank line, as it was written. ConfigError for a multi-line
    value that would have to be replaced, or a merged text that is not TOML."""
    tables = _split_tables(base)
    leftover, merged = [], False
    for name, head, header, body in _split_tables(extra):
        target = next((t for t in tables if name is not None and t[0] == name), None)
        if target is None:
            leftover.append([name, head, header, body])
            continue
        merged = True
        new_keys = {m.group(1) for m in map(_KEY.match, body) if m}
        kept = []
        for line in target[3]:
            m = _KEY.match(line)
            if m and m.group(1) in new_keys:
                if _spans_lines(line):
                    raise ConfigError("cannot merge key %s into [%s]: the existing value spans lines"
                                      % (m.group(1), name))
                continue
            kept.append(line)
        # the new keys go after the last value line, not after trailing blank lines
        at = max([i for i, line in enumerate(kept) if line.strip() and not line.lstrip().startswith("#")] or [-1]) + 1
        add = head + body
        while add and not add[-1].strip():
            add.pop()
        target[3] = kept[:at] + add + kept[at:]
    out = _join_tables(tables)
    text = _join_tables(leftover)
    if text.strip():
        out = out.rstrip("\n") + "\n\n" + text
    if merged:
        try:
            tomllib.loads(out)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("merged extra is not valid TOML: %s" % exc) from exc
    return out


def render(text, net, own=11, transcoder=21, tester=30, require_ident=None, extras=(), web_proxy=90, browser=91, coturn=94):
    """The template text with the tokens replaced; `extras` are texts appended
    after it (each after a newline, in order) before the substitution.
    ConfigError for a bad /24 prefix, a host octet outside 1..254, a bad
    require_ident or an unknown token."""
    if not _NET.match(net):
        raise ConfigError("net must be a /24 prefix like 172.30.0 (got %r)" % net)
    values = {"NET": net, "REQUIRE_IDENT": require_ident_value(require_ident)}
    for extra in extras:
        text = merge_extra(text.rstrip("\n") + "\n", extra)
    for name, octet in (("OWN_IP", own), ("TRANSCODER_IP", transcoder), ("TESTER_IP", tester),
                        ("WEB_PROXY_IP", web_proxy), ("BROWSER_IP", browser), ("COTURN_IP", coturn)):
        if not isinstance(octet, int) or isinstance(octet, bool) or not 1 <= octet <= 254:
            raise ConfigError("%s octet must be 1..254 (got %r)" % (name, octet))
        values[name] = "%s.%d" % (net, octet)
    unknown = [name for name in tokens(text) if name not in values]
    if unknown:
        raise ConfigError("unknown token%s %s (known: %s)" % (
            "s" if len(unknown) > 1 else "", ", ".join("<%s>" % n for n in unknown),
            ", ".join("<%s>" % n for n in KNOWN)))
    return TOKEN.sub(lambda m: values[m.group(1)], text)


def _read(path, what):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("cannot read %s %s: %s" % (what, path, exc)) from exc


def render_file(template, output, net, own=11, transcoder=21, tester=30, require_ident=None, extras=(),
                web_proxy=90):
    """Read TEMPLATE (and the files of `extras`), write OUTPUT (mode 0644; the
    container reads it as any user). Returns the output path; ConfigError when
    a file is missing, unreadable, not UTF-8 or the text has an unknown token."""
    text = _read(template, "template")
    texts = [_read(path, "extra") for path in extras]
    rendered = render(text, net, own, transcoder, tester, require_ident, texts, web_proxy)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    out.chmod(0o644)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m dvtest.sut_config", description=__doc__.split("\n\n")[0])
    ap.add_argument("template")
    ap.add_argument("output", help="rendered file ('-' = check only, write nothing)")
    ap.add_argument("--net", required=True)
    ap.add_argument("--own", type=int, default=11)
    ap.add_argument("--transcoder", type=int, default=21)
    ap.add_argument("--tester", type=int, default=30)
    ap.add_argument("--web-proxy", type=int, default=90)
    ap.add_argument("--require-ident", default=None, help="true or false (default: env DVT_REQUIRE_IDENT, else true)")
    ap.add_argument("--extra", action="append", default=[], metavar="FILE",
                    help="appended after the template, token substituted (repeatable)")
    args = ap.parse_args(argv)
    try:
        if args.output == "-":
            text = _read(args.template, "template")
            render(text, args.net, args.own, args.transcoder, args.tester, args.require_ident,
                   [_read(path, "extra") for path in args.extra], args.web_proxy)
        else:
            render_file(args.template, args.output, args.net, args.own, args.transcoder, args.tester,
                        args.require_ident, args.extra, args.web_proxy)
    except (ConfigError, OSError, UnicodeDecodeError) as exc:
        print("sut_config: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
