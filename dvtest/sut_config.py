# SPDX-License-Identifier: MIT
"""Render the dv-router.toml of the native SUT launch (DVT_SUT_LAUNCH=native).

DVT_SUT_CONFIG names a TEMPLATE; scripts/run.sh renders it on the host with

  python3 -m dvtest.sut_config TEMPLATE OUTPUT --net 172.30.0 [--own 11]
                               [--transcoder 21] [--tester 30]

Plain text replacement of these tokens (a token is <UPPER_CASE_NAME>):

  <OWN_IP>          the SUT address, <net>.<own>          (rig: .11)
  <TRANSCODER_IP>   the SUT's transcoder, <net>.<transcoder> (rig: .21)
  <TESTER_IP>       the tester, <net>.<tester>            (rig: .30)
  <NET>             the /24 prefix, e.g. 172.30.0

Any other <UPPER_CASE_NAME> left over is an error (exit 2, nothing written),
so a template that wants a rig address not listed above is refused instead of
being mounted half rendered. Lower case text such as <net> is not a token.
"""
import argparse
import re
import sys
from pathlib import Path

TOKEN = re.compile(r"<([A-Z][A-Z0-9_]*)>")
KNOWN = ("OWN_IP", "TRANSCODER_IP", "TESTER_IP", "NET")
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


def render(text, net, own=11, transcoder=21, tester=30):
    """The template text with the four tokens replaced; ConfigError for a
    bad /24 prefix, a host octet outside 1..254 or an unknown token."""
    if not _NET.match(net):
        raise ConfigError("net must be a /24 prefix like 172.30.0 (got %r)" % net)
    values = {"NET": net}
    for name, octet in (("OWN_IP", own), ("TRANSCODER_IP", transcoder), ("TESTER_IP", tester)):
        if not isinstance(octet, int) or isinstance(octet, bool) or not 1 <= octet <= 254:
            raise ConfigError("%s octet must be 1..254 (got %r)" % (name, octet))
        values[name] = "%s.%d" % (net, octet)
    unknown = [name for name in tokens(text) if name not in values]
    if unknown:
        raise ConfigError("unknown token%s %s (known: %s)" % (
            "s" if len(unknown) > 1 else "", ", ".join("<%s>" % n for n in unknown),
            ", ".join("<%s>" % n for n in KNOWN)))
    return TOKEN.sub(lambda m: values[m.group(1)], text)


def render_file(template, output, net, own=11, transcoder=21, tester=30):
    """Read TEMPLATE, write OUTPUT (mode 0644; the container reads it as any
    user). Returns the output path; ConfigError when TEMPLATE is missing,
    unreadable, not UTF-8 or has an unknown token."""
    try:
        text = Path(template).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("cannot read template %s: %s" % (template, exc)) from exc
    rendered = render(text, net, own, transcoder, tester)
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
    args = ap.parse_args(argv)
    try:
        if args.output == "-":
            text = Path(args.template).read_text(encoding="utf-8")
            render(text, args.net, args.own, args.transcoder, args.tester)
        else:
            render_file(args.template, args.output, args.net, args.own, args.transcoder, args.tester)
    except (ConfigError, OSError, UnicodeDecodeError) as exc:
        print("sut_config: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
