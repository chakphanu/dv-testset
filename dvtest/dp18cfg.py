# SPDX-License-Identifier: MIT
"""Pure helpers of the DP18 configuration cases (cases/test_dp18_config.py; docs/dp18-cases.md, "Configuration"): mutations of the snippet
rig/config/dv-router/dp18-origin.toml that `dv-router config check` must refuse or load. No network, no pytest.

The rules come from DV-Router's `crates/lib/config/src/aprsis_origin.rs` (HEAD f00079f4) and `config.md` (`[edges.aprsis.beacon]`,
`[[edges.aprsis.objects]]`):

  * object name: 1-9 printable ASCII bytes, no outer spaces, and no name that parses as a station callsign-SSID other than the gateway's
    own call (case-insensitive); names are unique; at most 16 objects
  * interval_secs 600-86400 (beacon and objects); lat_e7 and lon_e7 not both 0 (enabled beacon, every object)
  * frequency empty or `ddd.dddMHz`
  * the comment AS SENT (objects: frequency, `room=<R>`, comment joined by single spaces) at most 43 printable ASCII bytes; beacon comment 43

  CONFIG_CASES        the mutation table (id, what, key, mutate, hint, verdict)
  config_problems     problems of a `config check` verdict for one entry
  object_comment      the object comment as DV-Router sends it (for the boundary cases)
"""
import types

OBJECT_MARK = "\n[[edges.aprsis.objects]]\n"
GATEWAY = "N0TEST-5"                    # [edges.aprsis] callsign of dp15-aprsis.toml
FREQUENCY = "144.800MHz"
ROOM = "B"
COMMENT_MAX = 43

CONFIG_CASES = []


def stand_in_text(path):
    """The content of the secret-file stand-in at `path` inside the check container: the APRS-IS passcode file is validated for its content
    (digits, at most 5, or -1 = unverified); every other secret file only needs to be non-empty (None = the default stand-in)."""
    return "-1\n" if path.rsplit("/", 1)[-1] == "passcode" else None


def object_comment(frequency, room, comment):
    """The comment of an object as sent: non-empty parts of frequency, `room=<R>` and comment joined by single spaces."""
    parts = [frequency, "room=%s" % room if room else "", comment]
    return " ".join(p for p in parts if p)


def _case(cid, what, key, mutate, hint=None, verdict="reject"):
    CONFIG_CASES.append(types.SimpleNamespace(id=cid, what=what, key=key, mutate=mutate, hint=hint, verdict=verdict))


def _split(text):
    """(preamble with its comments, beacon part, object part): the header comments name the tables and the keys too, so the split is at
    the table header LINES."""
    beacon_mark = "\n[edges.aprsis.beacon]\n"
    pre, sep, rest = text.partition(beacon_mark)
    if not sep:
        raise ValueError("the snippet has no [edges.aprsis.beacon]")
    beacon, sep2, obj = rest.partition(OBJECT_MARK)
    if not sep2:
        raise ValueError("the snippet has no [[edges.aprsis.objects]]")
    return pre + beacon_mark, beacon, OBJECT_MARK + obj


def _sub(part, old, new):
    """Substitute `old` once in the beacon part ("beacon") or the object part ("object") of the snippet; ValueError when absent."""
    def f(text):
        pre, beacon, obj = _split(text)
        target = beacon if part == "beacon" else obj
        if target.count(old) != 1:
            raise ValueError("anchor %r found %d times in the %s part" % (old, target.count(old), part))
        target = target.replace(old, new)
        return pre + target + obj if part == "beacon" else pre + beacon + target
    return f


def _name(value):
    return _sub("object", 'name = "DVTOBJ1"', 'name = "%s"' % value)


def _comment_for_total(total):
    """An object comment such that the sent comment (snippet frequency and room) is exactly `total` bytes."""
    prefix = len(object_comment(FREQUENCY, ROOM, "x")) - 1
    return "c" * (total - prefix)


def _second_object(text):
    return text.rstrip("\n") + '''

[[edges.aprsis.objects]]
name = "DVTOBJ1"
frequency = "144.800MHz"
comment = "dvt"
lat_e7 = 105600000
lon_e7 = 202500000
symbol = "/r"
interval_secs = 600
'''


def _n_objects(count):
    def f(text):
        pre, beacon, _ = _split(text)
        rows = []
        for i in range(count):
            rows.append('[[edges.aprsis.objects]]\nname = "DVTOBJ%02d"\ncomment = "dvt"\nlat_e7 = %d\nlon_e7 = 202500000\nsymbol = "/r"\ninterval_secs = 600\n'
                        % (i, 105000000 + 1000000 * i))
        return pre + beacon + "\n".join(rows)
    return f


_case("accept-baseline", "the snippet as shipped loads", None, lambda t: t, verdict="accept")

# ---- object names
_case("name-callsign-ssid", "an object name that is a station callsign-SSID (N0CALL-7)", "name", _name("N0CALL-7"), hint="impersonate")
_case("name-callsign-lower", "the same, lower case (the rule is case-insensitive)", "name", _name("n0call-7"), hint="impersonate")
_case("name-callsign-bare", "a bare station callsign without SSID (N0CALL)", "name", _name("N0CALL"), hint="impersonate")
_case("name-10-chars", "an object name of 10 characters (the limit is 9)", "name", _name("DVTOBJ1234"), hint="1-9")
_case("name-leading-space", "an object name with a leading space", "name", _name(" DVTOBJ1"), hint="outer spaces")
_case("name-trailing-space", "an object name with a trailing space", "name", _name("DVTOBJ1 "), hint="outer spaces")
_case("name-empty", "an empty object name", "name", _name(""), hint="1-9")
_case("name-non-ascii", "an object name with a non-ASCII character", "name", _name("DVTOBJ\\u00e9"), hint="printable")
_case("name-duplicate", "two objects with one name", "name", _second_object, hint="duplicate")
_case("accept-name-9-chars", "an object name of exactly 9 characters", None, _name("DVTOBJ123"), verdict="accept")
_case("accept-name-inner-space", "an object name with an inner space", None, _name("DVT OBJ1"), verdict="accept")
_case("accept-name-gateway-call", "an object named like the gateway call itself (the one callsign a name may equal)", None, _name(GATEWAY), verdict="accept")
_case("accept-name-gateway-lower", "the gateway call in lower case (case-insensitive)", None, _name(GATEWAY.lower()), verdict="accept")

# ---- the interval floor (600-86400)
_case("object-interval-599", "an object interval_secs of 599 (the floor is 600)", "interval_secs",
      _sub("object", "interval_secs = 600", "interval_secs = 599"), hint="600-86400")
_case("beacon-interval-599", "a beacon interval_secs of 599", "interval_secs",
      _sub("beacon", "interval_secs = 600", "interval_secs = 599"), hint="600-86400")
_case("object-interval-86401", "an object interval_secs of 86401 (the ceiling is 86400)", "interval_secs",
      _sub("object", "interval_secs = 600", "interval_secs = 86401"), hint="600-86400")
# the floor itself, 600 on the beacon and on the object, is the baseline
_case("accept-interval-86400", "an object interval_secs of 86400", None, _sub("object", "interval_secs = 600", "interval_secs = 86400"), verdict="accept")

# ---- position
_case("object-position-zero", "an object at lat 0 and lon 0", "edges.aprsis.objects[0]",
      _sub("object", "lat_e7 = 105500000\nlon_e7 = 202500000", "lat_e7 = 0\nlon_e7 = 0"), hint="non-null")
_case("beacon-position-zero", "an enabled beacon at lat 0 and lon 0", "edges.aprsis.beacon",
      _sub("beacon", "lat_e7 = 105000000\nlon_e7 = 202500000", "lat_e7 = 0\nlon_e7 = 0"), hint="non-null")
_case("accept-lat-zero-only", "lat 0 with a non-zero lon (only BOTH zero is refused)", None,
      _sub("object", "lat_e7 = 105500000", "lat_e7 = 0"), verdict="accept")

# ---- frequency
_case("frequency-short", "a frequency of 9 characters (144.80MHz)", "frequency", _sub("object", 'frequency = "144.800MHz"', 'frequency = "144.80MHz"'), hint="ddd.dddMHz")
_case("frequency-lower-unit", "a frequency with a lower case unit (144.800mhz)", "frequency", _sub("object", 'frequency = "144.800MHz"', 'frequency = "144.800mhz"'), hint="ddd.dddMHz")
_case("frequency-letter", "a frequency with a letter in the digits (14a.800MHz)", "frequency", _sub("object", 'frequency = "144.800MHz"', 'frequency = "14a.800MHz"'), hint="ddd.dddMHz")

# ---- comment length (43 bytes as sent)
_case("object-comment-44", "an object comment that makes the sent comment 44 bytes (frequency + room + comment)", "comment",
      _sub("object", 'comment = "dvt"', 'comment = "%s"' % _comment_for_total(COMMENT_MAX + 1)), hint="43")
_case("beacon-comment-44", "a beacon comment of 44 bytes", "comment",
      _sub("beacon", 'comment = "dvt dp18 beacon"', 'comment = "%s"' % ("b" * (COMMENT_MAX + 1))), hint="43")
_case("accept-object-comment-43", "an object comment that makes the sent comment exactly 43 bytes", None,
      _sub("object", 'comment = "dvt"', 'comment = "%s"' % _comment_for_total(COMMENT_MAX)), verdict="accept")
_case("accept-beacon-comment-43", "a beacon comment of exactly 43 bytes", None,
      _sub("beacon", 'comment = "dvt dp18 beacon"', 'comment = "%s"' % ("b" * COMMENT_MAX)), verdict="accept")

# ---- the object count (16)
_case("objects-17", "17 configured objects (the limit is 16)", "objects", _n_objects(17), hint="16")
_case("accept-objects-16", "16 configured objects", None, _n_objects(16), verdict="accept")


def config_problems(case, verdict, exit_code):
    """Problems ([] = as expected) of a `dv-router config check` verdict (dvtest.tools.config_check.judge_check) for a CONFIG_CASES entry:
    reject = a non-zero exit status and a positioned error under `edges.aprsis` whose key (or message) names the offending field and whose
    message holds the hint when the source states one; accept = exit status 0."""
    if verdict.get("unsupported"):
        return ["the image has no `config check` (usage text printed)"]
    if case.verdict == "accept":
        return [] if exit_code == 0 and verdict.get("valid") else ["expected to load, got exit status %r: %s" % (exit_code, "; ".join(verdict.get("errors") or ["no error line"]))]
    if exit_code == 0 or verdict.get("valid"):
        return ["SILENT SUCCESS: %s loaded (exit status 0); expected an error naming %s" % (case.what, case.key)]
    parsed = verdict.get("parsed") or []
    if not parsed:
        return ["no `FILE:LINE:COL: message` error line (exit status %r only)" % (exit_code,)]
    mine = [e for e in parsed if "edges.aprsis" in e["key"] or "edges.aprsis" in e["message"]]
    if not mine:
        return ["no error names edges.aprsis (first: %r %r)" % (parsed[0]["key"], parsed[0]["message"])]
    named = [e for e in mine if case.key in e["key"] or case.key in e["message"]]
    if not named:
        return ["no error names %r (edges.aprsis errors: %s)" % (case.key, "; ".join("%s: %s" % (e["key"], e["message"]) for e in mine))]
    if case.hint and not any(case.hint in e["message"] for e in named):
        return ["the message does not mention %r: %s" % (case.hint, "; ".join(e["message"] for e in named))]
    if not all(e["message"].strip() for e in named):
        return ["the error message is empty"]
    return []
