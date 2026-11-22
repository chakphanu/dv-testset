# SPDX-License-Identifier: MIT
"""What a DV-Router image with RTS1 lets a black-box case assert (DV-Router b64733cd, merged in bc9af2b1; docs floor-control.md "DENY reasons" and
"RTS", status.md; image dv-router:wip-f1f3777).

Switch: DVT_RTS1=on asserts (strict); off (the default) skips the cases with a reason.

RTS1 (read from the sources, not guessed): an RTS reaches a room controller that is connected and not fenced but is NOT the controller-election winner (the highest live
controller token of the room is None or another id: the "winner gap", a few ms wide) and is answered with CTS DENY, reason 12 (`no_winner`; `Cts.reason`, `grant_id` 0), at once;
the edge drops the over silently (no Abort, no re-RTS) and counts dv_edge_drops_total{reason="deny_no_winner"}; an edge built before RTS1 maps the unknown code to Busy
(deny_busy). A room whose router link is down gets no CTS at all and counts the waiting RTS in dv_room_rts_total{result="link_down"}. A fenced but connected room still answers
DENY 9. The room counters are `dv_room_rts_total{room,result=grant|deny|busy_delayed|duplicate|clock_domain}` (present at zero) plus `no_winner` and `link_down`, sparse:
they appear after their first count. RTS and CTS are fabric-internal (a query on key `rts`); no legacy client protocol carries them, and a WebRTC talker reads reason 12 as the
status `absent` ("The controller is unavailable"). The winner gap cannot be provoked from outside the launcher: a GAP (docs/rts1-cases.md).

What the cases CAN assert: the image has RTS1 (the HELP text of dv_room_rts_total is "RTS processing, by result", it was "RTS answered, by result"), the fixed series are
zero or absent in normal traffic, the counters conserve (no_winner is part of deny), and a room restart leaves no silent loss: the next over is relayed. Pure and stdlib
only (cases/unit/test_rts1_lib.py).
"""
import os
import re

NEW_HELP = "RTS processing, by result"
OLD_HELP = "RTS answered, by result"
FIXED_RESULTS = ("grant", "deny", "busy_delayed", "duplicate", "clock_domain")      # present at zero from the first record
SPARSE_RESULTS = ("no_winner", "link_down")                                         # appear after their first count
DENY_NO_WINNER = 12
EDGE_REASON = "deny_no_winner"
HELP_RE = re.compile(r"^# HELP dv_room_rts_total (?P<help>.*)$", re.M)


def strict_on(environ=None):
    """DVT_RTS1=on: the image has RTS1."""
    return (environ if environ is not None else os.environ).get("DVT_RTS1", "").lower() in ("on", "1", "true", "yes")


def help_text(prometheus_text):
    """The HELP line of dv_room_rts_total in a hub /metrics text, or None."""
    m = HELP_RE.search(prometheus_text)
    return m.group("help").strip() if m else None


def image_has_rts1(prometheus_text):
    """True / False by the HELP text; None when the family is absent (the controller has not published yet)."""
    text = help_text(prometheus_text)
    if text is None:
        return None
    return text == NEW_HELP


def room_results(samples, room):
    """{result: total over procs} of dv_room_rts_total of one room letter (lower case)."""
    out = {}
    for s in samples:
        if s.name == "dv_room_rts_total" and s.labels.get("room") == room:
            out[s.labels.get("result")] = out.get(s.labels.get("result"), 0.0) + s.value
    return out


def conservation_problems(results):
    """Problems of one room's result map: no_winner is counted INSIDE deny, and deny cannot be smaller; grant and deny are never negative."""
    problems = []
    for key, value in results.items():
        if value < 0:
            problems.append("dv_room_rts_total{result=%s} is negative: %s" % (key, value))
    if results.get("no_winner", 0.0) > results.get("deny", 0.0):
        problems.append("no_winner (%g) exceeds deny (%g): status.md counts no_winner inside deny" % (results["no_winner"], results.get("deny", 0.0)))
    return problems


def fixed_series_missing(results):
    """The fixed results that a controller record must carry at zero from its first record."""
    return [r for r in FIXED_RESULTS if r not in results]
