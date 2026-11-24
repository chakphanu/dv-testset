# SPDX-License-Identifier: MIT
"""Pure helpers of the K9C case (cases/test_k9c.py, docs/k9c-cases.md): DV-Router K9c (39337fb5, images from wip-ae11b8f) enforces
the `direction` of an endpoint row. `to_room` supplies the room and hears nothing, `from_room` hears the room and every new RTS is refused
(deny reason TxAcl, counted once per over as dv_edge_drops_total{reason="deny_tx_acl"}), `both` is unchanged.

Offline parts: the direction edit of a rendered config, the deny counters of a /metrics text and their delta, the judgement of what a
listener heard around a switch time. No rig, no network.
"""
import re

from dvtest import metrics

# the static USRP rows of rig/config/dv-router/k9c-usrp.toml: row name -> (bind port of the SUT, port of the tester's socket, start direction, module).
# One module per USRP row (the image refuses a second row on a module), so each row has its own room and its own third party.
ROWS = {
    "k9c-from": (34022, 32022, "from_room", "K"),
    "k9c-to": (34023, 32023, "to_room", "M"),
}
DIRECTIONS = ("to_room", "from_room", "both")
DENY_REASON = "deny_tx_acl"
DROPS_FAMILY = "dv_edge_drops_total"

_HEADER = re.compile(r"^\s*\[\[\s*edges\.usrp\.endpoint\s*\]\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_NAME = re.compile(r'^\s*name\s*=\s*"([^"]*)"')
_DIRECTION = re.compile(r'^(\s*)direction\s*=\s*"[^"]*"(\s*#.*)?\s*$')


def set_direction(text, name, direction):
    """`text` (a rendered dv-router.toml) with `direction = "<direction>"` in the [[edges.usrp.endpoint]] row that has `name = "<name>"`: the
    existing direction line is replaced in place, else a line is added after the name line. ValueError for an unknown direction, no row of
    that name, or a name that two rows carry."""
    if direction not in DIRECTIONS:
        raise ValueError("direction must be one of %s (got %r)" % ("|".join(DIRECTIONS), direction))
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if _HEADER.match(line)]
    hits = []
    for i in starts:
        end = next((j for j in range(i + 1, len(lines)) if _ANY_HEADER.match(lines[j])), len(lines))
        for j in range(i + 1, end):
            m = _NAME.match(lines[j])
            if m and m.group(1) == name:
                hits.append((i, j, end))
    if len(hits) != 1:
        raise ValueError("%d [[edges.usrp.endpoint]] rows named %r (want exactly 1)" % (len(hits), name))
    _, at, end = hits[0]
    for j in range(at, end):
        m = _DIRECTION.match(lines[j])
        if m:
            lines[j] = '%sdirection = "%s"%s' % (m.group(1), direction, m.group(2) or "")
            return "\n".join(lines)
    lines.insert(at + 1, 'direction = "%s"' % direction)
    return "\n".join(lines)


def row_direction(text, name):
    """The direction of the USRP row `name` in a rendered config (the default `both` when the row has no direction line), or None without such a row."""
    import tomllib
    rows = ((tomllib.loads(text).get("edges") or {}).get("usrp") or {}).get("endpoint") or []
    for row in rows:
        if row.get("name") == name:
            return row.get("direction", "both")
    return None


def deny_series(samples):
    """{(family, ((label, value), ...)): value} of every sample whose family name has `deny` in it or that carries a label value starting with
    `deny`: the exact deny counters and reasons of a /metrics text."""
    out = {}
    for s in samples:
        labels = tuple(sorted(s.labels.items()))
        if "deny" in s.name or any(str(v).startswith("deny") for _, v in labels):
            out[(s.name, labels)] = s.value
    return out


def deny_text(text):
    """deny_series of a /metrics text."""
    return deny_series(metrics.parse(text))


def tx_acl(series):
    """The sum of dv_edge_drops_total{reason="deny_tx_acl"} over every process of a deny_series dict (0 without a series)."""
    return sum(v for (name, labels), v in series.items() if name == DROPS_FAMILY and dict(labels).get("reason") == DENY_REASON)


def delta(before, after):
    """{key: after - before} of the keys that moved (a key missing before counts from 0; a counter that fell is reported negative)."""
    out = {}
    for key in set(before) | set(after):
        d = after.get(key, 0.0) - before.get(key, 0.0)
        if d:
            out[key] = d
    return out


def describe(moved):
    """A short sorted text of a delta dict: `dv_edge_drops_total{proc=usrp,reason=deny_tx_acl} +1`."""
    items = []
    for (name, labels), d in sorted(moved.items()):
        body = ",".join("%s=%s" % (k, v) for k, v in labels)
        items.append("%s{%s} %+g" % (name, body, d))
    return "; ".join(items) or "nothing moved"


def judge_switch(heard, t_switch, wait, expect_after):
    """What a listener heard (list of arrival times of voice packets, epoch seconds) of one over around the moment `t_switch` (the host epoch time of the
    SIGHUP) when the rows' direction changes mid-over. `wait` seconds after the signal are allowed for the edge to apply the change.
    expect_after True: the voice must start only after the signal and run on after it (a from_room talker switched to both); False: the voice
    must stop within `wait` seconds of the signal (a to_room talker switched to from_room). Returns (problems, facts): facts has `before` /
    `after` counts (packets before the signal, after the allowed wait), `first_after` (seconds after the signal of the first packet, or None) and
    `last` (seconds after the signal of the last packet, or None)."""
    before = [t for t in heard if t < t_switch]
    late = [t for t in heard if t >= t_switch + wait]
    facts = {"total": len(heard), "before": len(before), "after": len(late),
             "first_after": None, "last": None}
    after = [t for t in heard if t >= t_switch]
    if after:
        facts["first_after"] = round(after[0] - t_switch, 3)
    if heard:
        facts["last"] = round(heard[-1] - t_switch, 3)
    problems = []
    if expect_after:
        if before:
            problems.append("%d packets were heard BEFORE the switch (the from_room over must not be heard)" % len(before))
        if not late:
            problems.append("no packet was heard later than %.1f s after the switch: the over in progress did not follow the new direction" % wait)
    else:
        if len(before) < 25:
            problems.append("only %d packets were heard before the switch (the to_room over must be heard)" % len(before))
        if late:
            problems.append("%d packets were heard later than %.1f s after the switch (the over must stop at the switch)" % (len(late), wait))
    return problems, facts


def heard_share(heard, sent):
    """heard / sent as a float (0 when nothing was sent)."""
    return (heard / float(sent)) if sent else 0.0


# ---------------------------------------------------------------- what a cut over leaves in /metrics and the log (case d)

FOLD_WORDS = ("over", "revok", "cut", "leave", "end_missing", "lossy", "complete", "policy", "direction", "tx_acl", "abort", "settle")


def all_series(text):
    """{(family, ((label, value), ...)): value} of every sample of a /metrics text (NaN samples left out)."""
    out = {}
    for s in metrics.parse(text):
        if s.value == s.value:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def is_fold(key):
    """True for a series whose family or label values name an over fold, a revoke, a cut or a leave (FOLD_WORDS)."""
    name, labels = key
    hay = [name] + [str(v) for _, v in labels]
    return any(w in h.lower() for h in hay for w in FOLD_WORDS)


def folds(moved):
    """The part of a delta dict that is about overs, revokes, cuts and leaves, sorted by key text."""
    return {k: v for k, v in sorted(moved.items(), key=str) if is_fold(k)}


def fold_results(moved, family, **labels):
    """{result label: delta} of a family (dv_room_overs_total, dv_edge_overs_total) summed over the other labels, restricted to the given labels
    (dir="rx"); empty without such a moved series."""
    out = {}
    for (name, lab), d in moved.items():
        d_lab = dict(lab)
        if name == family and all(d_lab.get(k) == v for k, v in labels.items()):
            out[d_lab.get("result", "")] = out.get(d_lab.get("result", ""), 0.0) + d
    return out


def new_lines(before, after, words=("direction", "cut", "revok", "leave", "tx_acl", "txacl", "over", "binding", "k9")):
    """The lines of the log text `after` that `before` does not have (prefix compared by line count), reduced to those naming one of `words`."""
    old = before.splitlines()
    new = after.splitlines()[len(old):]
    return [ln for ln in new if any(w in ln.lower() for w in words)]
