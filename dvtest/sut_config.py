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
  <DP15_TX_ENABLE>  true or false: the value of --dp15-tx-enable, else the env DVT_DP15_TX_ENABLE, else
                    false (rig/config/dv-router/dp15-rf.toml: [edges.ax25] tx_enable and the port tx)
  <WEB_PROXY_IP>    the TLS proxy in front of dv-svc-web, <net>.<web_proxy> (rig: .90,
                    profile web; rig/config/dv-router/web.toml)
  <BROWSER_IP>      the browser service, <net>.<browser> (rig: .91, profile browser;
                    rig/config/dv-router/admin.toml lets it reach the admin host)
  <COTURN_IP>       the TURN server of the NAT profile, <net>.<coturn> (rig: .94;
                    rig/config/dv-router/turn.toml)
  <CENTRAL_IP>      only with --central-ip ADDR (an IPv4 address, whatever network): the central node a NODE1g hotspot dials
                    (rig/config/dv-router/node1g-hotspot.toml; the hotspot itself is rendered with the inside network as --net)

AX25ACK1 (DV-Router 345f1ebe, images from wip-f1f3777): a transmitting AX.25 edge needs the exact control-operator sentence in
`[edges.ax25] tx_ack` (the same sentence as `[edges.radio] tx_ack`, with the station callsign in upper case, SSID included), else
transmit stays dormant ("no control-operator acknowledgement: RX only", a warning). Older images refuse the key (unknown field), so
the snippets do not carry it: with env DVT_AX25ACK1=on the renderer adds `tx_ack = "<sentence for the callsign>"` to the
`[edges.ax25]` table of the rendered file when it has a `callsign` and `tx_enable = true` and no `tx_ack` of its own (`apply_ax25_ack`).
Unset or any other value: nothing is added (the rendering of every older image is unchanged).

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
KNOWN = ("OWN_IP", "TRANSCODER_IP", "TESTER_IP", "NET", "REQUIRE_IDENT", "DP15_TX_ENABLE", "WEB_PROXY_IP", "BROWSER_IP", "COTURN_IP", "CENTRAL_IP")
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


def dp15_tx_enable_value(value=None):
    """"true" or "false" from `value`, else env DVT_DP15_TX_ENABLE, else "false" (RF transmit stays off unless a run asks
    for it); ConfigError for anything else."""
    if value is None:
        value = os.environ.get("DVT_DP15_TX_ENABLE") or "false"
    if value not in BOOLS:
        raise ConfigError("dp15 tx_enable must be true or false (got %r)" % (value,))
    return value


# DV-Router generations the rig knows, oldest first. DVT_SUT_GEN selects the config dialect and the names of the image under test.
# The default stays wip-c165c93 (ACL0c-f, NOX1, CFG2, TOTP1b), which turns keys that were warnings into errors: the `[fabric]` table
# (the broker ACL is gone), access.allow_file / deny_file, callhome interlink_file. `71ab207` renders the template as it was (c165c93
# accepts the c165c93 rendering too: dvtest/tools/config_check.py). `c2465de` (tags >= wip-c2465de) also renames the DMR Homebrew edge
# "mmdvm" to "dmrhb": child dv-edge-dmrhb, edge id dmrhb-<inc>, API/events edge "dmrhb", metrics label proc="dmrhb", config table
# [edges.dmrhb] ([edges.mmdvm] still loads for one release, with a warning), env DV_MMDVM_* -> DV_DMRHB_*; wire id 2 and UDP 62030 are
# unchanged. The MMDVM PROTOCOL name (spec entry "mmdvm", dvtest/proto/dmr.py, the xlxd reference side) is not renamed.
# `compat1` (COMPAT1, the backward-compat shims are gone; a superset of c2465de) makes the shim keys errors: `web.operator_totp` /
# `admin.totp` are rejected (the key is `totp_required`), every `[[edges.xlx.peer]]` row needs `kind = "xlx"`, env DV_MMDVM_* is
# gone (DV_DMRHB_* only), `[edges.mmdvm]` no longer loads. The repo snippets keep the keys of the older images; the renames happen at
# render time for compat1 only (`apply_gen`, `adapt_text`), so every older generation renders as before.
# `k9` (images from wip-4732c43, DV-Router 0a6ec3e5: K9a + K9b; a superset of compat1) gives every endpoint row `direction` and `enabled` and
# rejects what the old dialect said: the AllStar outbound key `mode` is gone (`direction`: default `to_room` = the listen-only Monitor call,
# a call transmits room audio and keying only with an explicit `direction = "both"`), and several `[[edges.usrp.endpoint]]` rows with one
# callsign need a unique `name` (the routing key; without it the load fails with "duplicate endpoint key"). The repo snippets keep the keys of
# the older images (mode = "transceive", repeated callsigns); `apply_k9` rewrites them at render time for k9 only: `mode = "transceive"` becomes
# `direction = "both"`, `mode = "monitor"` (or no mode) `direction = "to_room"`, a repeated USRP callsign gets `name = "usrp-<module letter>"`.
# A non-both direction on any other row stays refused by the image until K9c ("direction enforcement is not built yet"): the renderer never
# writes `direction` there, the default `both` is what the old behaviour was.
GENS = ("71ab207", "c165c93", "c2465de", "compat1", "k9")
DEFAULT_GEN = "c165c93"
RENAMES = {"mmdvm": "dmrhb"}          # edge name before -> from generation c2465de on
RENAMED_FROM = "c2465de"
COMPAT1 = "compat1"
K9 = "k9"
KEY_RENAMES = {("web", "operator_totp"): "totp_required", ("admin", "totp"): "totp_required"}   # (table, old key) -> new key, compat1


def gen_value(value=None):
    """The generation from `value`, else env DVT_SUT_GEN, else DEFAULT_GEN; ConfigError for an unknown one."""
    if value is None:
        value = os.environ.get("DVT_SUT_GEN") or DEFAULT_GEN
    if value not in GENS:
        raise ConfigError("DVT_SUT_GEN must be one of %s (got %r)" % ("|".join(GENS), value))
    return value


def gen_at_least(gen, minimum):
    """True when generation `gen` is `minimum` or newer."""
    return GENS.index(gen_value(gen)) >= GENS.index(minimum)


def edge_name(name, gen=None):
    """The name of an edge (child dv-edge-<name>, API / events edge id, metrics label proc, [edges.<name>]) on generation `gen` (else
    env DVT_SUT_GEN, else the default): "mmdvm" is "dmrhb" from c2465de on, every other name is unchanged."""
    return RENAMES.get(name, name) if gen_at_least(gen, RENAMED_FROM) else name


def env_prefix(name, gen=None):
    """The DV_<NAME>_ environment prefix of an edge on generation `gen`: DV_MMDVM_ before c2465de, DV_DMRHB_ from then on."""
    return "DV_%s_" % edge_name(name, gen).upper()


_EDGES_HEADER = re.compile(r"^(\s*\[\[?\s*)edges\.mmdvm(\b)")
_XLX_PEER_HEADER = re.compile(r"^\s*\[\[\s*edges\.xlx\.peer\s*\]\]\s*(#.*)?$")
_ALLSTAR_OUT_HEADER = re.compile(r"^\s*\[\[\s*edges\.allstar\.outbound\s*\]\]\s*(#.*)?$")
_USRP_ENDPOINT_HEADER = re.compile(r"^\s*\[\[\s*edges\.usrp\.endpoint\s*\]\]\s*(#.*)?$")
_MODE_LINE = re.compile(r'^(\s*)mode\s*=\s*"([^"]*)"(\s*#.*)?\s*$')
_MODULE_LINE = re.compile(r'^\s*module\s*=\s*"([A-Za-z0-9])"')
_STR_VALUE_LINE = re.compile(r'^\s*%s\s*=\s*"([^"]*)"')


def adapt_text(text, gen):
    """One TOML text (the template or one extra) in the key dialect of generation `gen`, BEFORE the extras are merged. Before compat1:
    unchanged. compat1: `[web] operator_totp` and `[admin] totp` are errors there. `= true` (the old "totp on" posture, which did not force
    enrolment) is dropped: the new default is optional TOTP, and totp-required.toml sets `totp_required = true` when a run wants it
    mandatory. Any other value becomes `totp_required` with the value kept; a line whose new key is already in the same table is dropped
    instead, so no key is repeated."""
    if not gen_at_least(gen, COMPAT1):
        return text
    parts = _split_tables(text)
    for part in parts:
        name, body = part[0], part[3]
        olds = [old for (table, old) in KEY_RENAMES if table == name]
        if not olds:
            continue
        have = {m.group(1) for m in map(_KEY.match, body) if m}
        out = []
        for line in body:
            m = _KEY.match(line)
            new = KEY_RENAMES.get((name, m.group(1))) if m else None
            if new is None:
                out.append(line)
            elif line[m.end(1):].lstrip(" \t=").lower().startswith("true"):
                pass        # old `= true` meant "totp available" (the c165c93 default posture), NOT "required": the new default is optional, so no key
            elif new not in have:
                have.add(new)
                out.append(line[:m.start(1)] + new + line[m.end(1):])
            # else: the new key is there already, the old line is dropped
        part[3] = out
    return _join_tables(parts)


def _add_peer_kind(text):
    """`kind = "xlx"` as the first line of every `[[edges.xlx.peer]]` row that has no `kind` key."""
    parts = _split_tables(text)
    for part in parts:
        if part[2] is not None and _XLX_PEER_HEADER.match(part[2]):
            if not any(m and m.group(1) == "kind" for m in map(_KEY.match, part[3])):
                part[3].insert(0, 'kind = "xlx"')
    return _join_tables(parts)


def _k9_outbound(text):
    """Every `[[edges.allstar.outbound]]` row with an explicit `direction` (K9a): the old key `mode` is refused by k9 images. `mode =
    "transceive"` becomes `direction = "both"` (the only value that transmits), `mode = "monitor"` and a row without `mode` get
    `direction = "to_room"` (the listen-only Monitor call, also the image default, written out so the intent is visible). A row that
    has a `direction` already keeps it and loses a leftover `mode` line. Another `mode` value is left alone (the image names it)."""
    parts = _split_tables(text)
    for part in parts:
        if part[2] is None or not _ALLSTAR_OUT_HEADER.match(part[2]):
            continue
        body = part[3]
        has_direction = any(m and m.group(1) == "direction" for m in map(_KEY.match, body))
        out, done = [], has_direction
        for line in body:
            m = _MODE_LINE.match(line)
            if not m or m.group(2) not in ("transceive", "monitor"):
                out.append(line)
            elif not done:
                done = True
                out.append('%sdirection = "%s"%s' % (m.group(1), "both" if m.group(2) == "transceive" else "to_room", m.group(3) or ""))
            # else: a direction is there already, the old mode line is dropped
        if not done:
            at = max([i for i, line in enumerate(out) if line.strip() and not line.lstrip().startswith("#")] or [-1]) + 1
            out.insert(at, 'direction = "to_room"')
        part[3] = out
    return _join_tables(parts)


def usrp_endpoint_name(module, taken):
    """A routing name for a USRP endpoint row of module letter `module`: `usrp-<letter>` in lower case (a digit module works too), `-2`,
    `-3`, ... when `taken` (a set of lower case names) has it already; the result is added to `taken`. DV-Router wants 1..32 lower case
    ASCII letters, digits or dash, starting with a letter."""
    base = "usrp-%s" % (module or "x").lower()
    name, n = base, 1
    while name in taken:
        n += 1
        name = "%s-%d" % (base, n)
    taken.add(name)
    return name


def _k9_usrp_names(text):
    """A unique `name` for every `[[edges.usrp.endpoint]]` row that shares its callsign (compared in upper case) with another row and has
    no `name` of its own (K9a: the routing key of an unnamed row is its callsign, and a repeated key is a load error). Rows with a
    unique callsign stay unnamed; a name the snippet wrote itself is kept and never reused."""
    parts = _split_tables(text)
    rows = [p for p in parts if p[2] is not None and _USRP_ENDPOINT_HEADER.match(p[2])]

    def field(part, key):
        rx = re.compile(_STR_VALUE_LINE.pattern % re.escape(key))
        return next((m.group(1) for m in map(rx.match, part[3]) if m), None)

    count = {}
    for part in rows:
        call = (field(part, "callsign") or "").upper()
        count[call] = count.get(call, 0) + 1
    taken = {(field(part, "name") or "").lower() for part in rows} - {""}
    for part in rows:
        if field(part, "name") is None and count[(field(part, "callsign") or "").upper()] > 1:
            module = _MODULE_LINE.match(next((line for line in part[3] if _MODULE_LINE.match(line)), ""))
            part[3].insert(0, 'name = "%s"' % usrp_endpoint_name(module.group(1) if module else None, taken))
    return _join_tables(parts)


def apply_k9(text):
    """The rendered text in the k9 dialect (see GENS): `_k9_outbound`, then `_k9_usrp_names`."""
    return _k9_usrp_names(_k9_outbound(text))


def apply_gen(text, gen):
    """The rendered text in the dialect of generation `gen`. 71ab207: unchanged. From c165c93 on: the `[fabric]` table is removed (the
    broker ACL is gone: its only key `acl` was ignored with a warning before, and the table itself is an unknown field now, so the
    validator refuses the file); sub-tables `[fabric.x]` go with it. From c2465de on: every `[edges.mmdvm]`, `[edges.mmdvm.x]` and
    `[[edges.mmdvm.x]]` header becomes `edges.dmrhb`. From compat1 on: every `[[edges.xlx.peer]]` row gets `kind = "xlx"` (the totp key
    renames are done per text before the merge: `adapt_text`). From k9 on: `apply_k9` (AllStar outbound `direction`, USRP names). Other removed keys (access.allow_file / deny_file, callhome interlink_file) are
    not rewritten: dvtest/tools/config_check.py reports them."""
    gen = gen_value(gen)
    if gen == "71ab207":
        return text
    parts = [p for p in _split_tables(text) if not (p[0] == "fabric" or (p[0] or "").startswith("fabric."))]
    text = _join_tables(parts)
    if gen_at_least(gen, RENAMED_FROM):
        text = "\n".join(_EDGES_HEADER.sub(lambda m: m.group(1) + "edges.dmrhb" + m.group(2), line) for line in text.split("\n"))
    if gen_at_least(gen, COMPAT1):
        text = _add_peer_kind(text)
    if gen_at_least(gen, K9):
        text = apply_k9(text)
    return text


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
            # tokens (`require_ident = <REQUIRE_IDENT>`) are not TOML before the substitution of render(): check
            # the text with every token replaced by a valid value, or an earlier extra that carries a bare token
            # made every later merge fail ("merged extra is not valid TOML") and the order of the extras mattered
            tomllib.loads(TOKEN.sub("0", out))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError("merged extra is not valid TOML: %s" % exc) from exc
    return out


AX25_ACK_ENV = "DVT_AX25ACK1"
_CALLSIGN_LINE = re.compile(r'^\s*callsign\s*=\s*"([^"]*)"')
_TX_ENABLE_TRUE = re.compile(r"^\s*tx_enable\s*=\s*true\b")


def ax25_ack_on():
    """True when the env DVT_AX25ACK1 is `on` (the image under test has AX25ACK1: wip-f1f3777 and later)."""
    return os.environ.get(AX25_ACK_ENV, "") == "on"


def tx_ack_sentence(callsign):
    """The exact control-operator sentence of DV-Router (`dv_lib_config::tx_ack`) for a station callsign (upper case, SSID included)."""
    call = callsign.strip().upper()
    return "I am the control operator of %s and accept responsibility for its transmissions" % call


def apply_ax25_ack(text):
    """The rendered text with `tx_ack` added to `[edges.ax25]` when that table has a callsign, `tx_enable = true` and no `tx_ack` key.
    Text without such a table is returned unchanged (also a table with tx_enable false: dormant anyway, and the admin TX-on route
    persists an absent acknowledgement itself)."""
    parts = _split_tables(text)
    for part in parts:
        if part[0] != "edges.ax25":
            continue
        body = part[3]
        keys = [m.group(1) for m in map(_KEY.match, body) if m]
        if "tx_ack" in keys or not any(_TX_ENABLE_TRUE.match(line) for line in body):
            continue
        for i, line in enumerate(body):
            m = _CALLSIGN_LINE.match(line)
            if m and m.group(1).strip():
                body.insert(i + 1, 'tx_ack = "%s"' % tx_ack_sentence(m.group(1)))
                break
    return _join_tables(parts)


def render(text, net, own=11, transcoder=21, tester=30, require_ident=None, extras=(), web_proxy=90, browser=91, coturn=94,
           gen=None, dp15_tx_enable=None, central_ip=None):
    """The template text with the tokens replaced; `extras` are texts appended
    after it (each after a newline, in order) before the substitution.
    ConfigError for a bad /24 prefix, a host octet outside 1..254, a bad
    require_ident, an unknown generation (`gen`, else env DVT_SUT_GEN) or an unknown token."""
    if not _NET.match(net):
        raise ConfigError("net must be a /24 prefix like 172.30.0 (got %r)" % net)
    values = {"NET": net, "REQUIRE_IDENT": require_ident_value(require_ident),
              "DP15_TX_ENABLE": dp15_tx_enable_value(dp15_tx_enable)}
    gen = gen_value(gen)
    text = adapt_text(text, gen)
    for extra in extras:
        text = merge_extra(text.rstrip("\n") + "\n", adapt_text(extra, gen))
    for name, octet in (("OWN_IP", own), ("TRANSCODER_IP", transcoder), ("TESTER_IP", tester),
                        ("WEB_PROXY_IP", web_proxy), ("BROWSER_IP", browser), ("COTURN_IP", coturn)):
        if not isinstance(octet, int) or isinstance(octet, bool) or not 1 <= octet <= 254:
            raise ConfigError("%s octet must be 1..254 (got %r)" % (name, octet))
        values[name] = "%s.%d" % (net, octet)
    if central_ip is not None:
        if not re.fullmatch(r"%s\.%s\.%s\.%s" % ((_OCTET,) * 4), central_ip):
            raise ConfigError("central ip must be an IPv4 address (got %r)" % (central_ip,))
        values["CENTRAL_IP"] = central_ip
    unknown = [name for name in tokens(text) if name not in values]
    if unknown:
        raise ConfigError("unknown token%s %s (known: %s)" % (
            "s" if len(unknown) > 1 else "", ", ".join("<%s>" % n for n in unknown),
            ", ".join("<%s>" % n for n in KNOWN)))
    text = apply_gen(TOKEN.sub(lambda m: values[m.group(1)], text), gen)
    return apply_ax25_ack(text) if ax25_ack_on() else text


def _read(path, what):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("cannot read %s %s: %s" % (what, path, exc)) from exc


def render_file(template, output, net, own=11, transcoder=21, tester=30, require_ident=None, extras=(),
                web_proxy=90, gen=None, dp15_tx_enable=None, central_ip=None):
    """Read TEMPLATE (and the files of `extras`), write OUTPUT (mode 0644; the
    container reads it as any user). Returns the output path; ConfigError when
    a file is missing, unreadable, not UTF-8 or the text has an unknown token."""
    text = _read(template, "template")
    texts = [_read(path, "extra") for path in extras]
    rendered = render(text, net, own, transcoder, tester, require_ident, texts, web_proxy, gen=gen, dp15_tx_enable=dp15_tx_enable,
                      central_ip=central_ip)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    out.chmod(0o644)
    return out


USRP_RIG_PORTS = (34001, 34002, 34003)     # xlxd-sut side of the rig's USRP endpoints A, B, C (rig/config/dv-router/usrp-rig.toml)


def usrp_bind_ports(text):
    """The UDP bind ports of the `[[edges.usrp.endpoint]]` rows of a RENDERED dv-router.toml, as a set; None when the text is not
    TOML or a row has no explicit `bind` (the port is then allocated by the router, so the text does not tell)."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    rows = doc.get("edges", {}).get("usrp", {}).get("endpoint", [])
    ports = set()
    for row in rows if isinstance(rows, list) else []:
        bind = row.get("bind") if isinstance(row, dict) else None
        port = str(bind).rsplit(":", 1)[-1] if bind else ""
        if not port.isdigit():
            return None
        ports.add(int(port))
    return ports


def missing_usrp_ports(text, expected=USRP_RIG_PORTS):
    """The sorted ports of `expected` that no `[[edges.usrp.endpoint]]` row of the rendered config binds (empty = all there, or the
    text cannot tell: see usrp_bind_ports). Used by the rig_usrp fixture (cases/conftest.py)."""
    ports = usrp_bind_ports(text)
    if ports is None:
        return []
    return sorted(set(expected) - ports)


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
    ap.add_argument("--dp15-tx-enable", default=None, help="true or false (default: env DVT_DP15_TX_ENABLE, else false)")
    ap.add_argument("--central-ip", default=None, help="value of <CENTRAL_IP> (NODE1g hotspot template)")
    ap.add_argument("--extra", action="append", default=[], metavar="FILE",
                    help="appended after the template, token substituted (repeatable)")
    args = ap.parse_args(argv)
    try:
        if args.output == "-":
            text = _read(args.template, "template")
            render(text, args.net, args.own, args.transcoder, args.tester, args.require_ident,
                   [_read(path, "extra") for path in args.extra], args.web_proxy, dp15_tx_enable=args.dp15_tx_enable,
                   central_ip=args.central_ip)
        else:
            render_file(args.template, args.output, args.net, args.own, args.transcoder, args.tester,
                        args.require_ident, args.extra, args.web_proxy, dp15_tx_enable=args.dp15_tx_enable,
                        central_ip=args.central_ip)
    except (ConfigError, OSError, UnicodeDecodeError) as exc:
        print("sut_config: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
