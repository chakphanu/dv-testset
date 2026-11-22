# SPDX-License-Identifier: MIT
"""What a DV-Router image with M17RL1 lets a black-box case assert about the M17 reflector interlink (DV-Router 307525dc, merged in bc9af2b1;
docs/design/m17.md "Peer handshake", image dv-router:wip-f1f3777).

Switch: DVT_M17RL1=on asserts; off (the default) skips the cases with a reason.

M17RL1 (read from the sources and m17.md, not guessed):
  * every valid CONN from a configured peer endpoint gets a 37 byte ACKN, linked or not (the ACKN is unconditional);
  * while UNLINKED, a valid inbound CONN also triggers a local CONN to that source, but only when at least 3 s (CONN_INTERVAL_S) have passed since the
    row last SENT any local CONN (timer or answer); one local CONN per peer row per 3 s from all sources and timers together;
  * a suppressed answer is DEFERRED, not dropped: the retry deadline moves to the end of the interval (last local CONN + 3 s), so the CONN leaves
    at that time (10 ms driver tick) instead of at the old, later backoff deadline (3 / 6 / 12 / 24 / 30 s);
  * a row that is still LINKED answers a CONN with the ACKN alone (no CONN);
  * mrefd (the reference) answers a CONN with the ACKN alone, links on a received ACKN and sends its own CONN only from a 10 s link check while the
    peer is not linked, so an mrefd that starts while we run unlinked must be answered at once (when our last CONN is >= 3 s old) or within 3 s of that
    CONN (deferral); a peer restart relinks within about 3 s.

Pure and stdlib only (cases/unit/test_m17rl1_lib.py). The cases (cases/test_m17rl1.py) play the peer with dvtest/m17_peer.py in the mrefd manner
(auto_conn off, CONN sent by hand), which is what a real mrefd does on the wire; the real mrefd ("ref" side) is only recorded.
"""
import os

CONN_INTERVAL_S = 3.0           # CONN_INTERVAL_NS of the M17 transport
EARLY_SLACK_S = 0.15            # packet transit and tester scheduling: a gap this much below 3 s is still a pass
LATE_SLACK_S = 0.8              # the 10 ms driver tick, DNS of a literal address (none) and the tester's own poll
IMMEDIATE_S = 1.2               # an allowed answer leaves "at once"
RELINK_BOUND_S = CONN_INTERVAL_S + LATE_SLACK_S


def strict_on(environ=None):
    """DVT_M17RL1=on: the image has M17RL1 (wip-f1f3777 and later)."""
    return (environ if environ is not None else os.environ).get("DVT_M17RL1", "").lower() in ("on", "1", "true", "yes")


def times(rxs):
    """The monotonic times of a list of Rx (dvtest/m17_peer.py), sorted."""
    return sorted(rx.t for rx in rxs)


def spacing_problems(conn_times, minimum=CONN_INTERVAL_S, slack=EARLY_SLACK_S):
    """Problems ([] = fine) of the local CONN times of ONE peer row seen at the peer: no two closer than `minimum` less `slack`."""
    problems = []
    ordered = sorted(conn_times)
    for a, b in zip(ordered, ordered[1:]):
        if b - a < minimum - slack:
            problems.append("two CONN %.2f s apart (the row sends one per %.0f s): at +%.2f s and +%.2f s" % (b - a, minimum, a - ordered[0], b - ordered[0]))
    return problems


def max_conn_in(window_s, minimum=CONN_INTERVAL_S):
    """The most local CONN that fit in `window_s` seconds at one per `minimum` (the first may be at any time): floor(window / minimum) + 1."""
    return int(window_s // minimum) + 1


def ack_problems(sent, ackn_times):
    """Problems of the rule "every valid CONN gets an ACKN": `sent` CONN were sent by the peer, `ackn_times` are the ACKN the row sent back."""
    if len(ackn_times) < sent:
        return ["%d CONN sent, only %d ACKN came back (the ACKN is unconditional for a valid CONN of a configured endpoint)" % (sent, len(ackn_times))]
    return []


def first_at_or_after(values, t):
    """The first value >= t, else None."""
    return next((v for v in sorted(values) if v >= t), None)


def deferral_problems(first_conn_t, peer_conn_t, conn_times_after, ackn_after_t):
    """A CONN of the peer at `peer_conn_t`, less than 3 s after the row's last CONN at `first_conn_t`, must get an ACKN at once, NO CONN before the end of
    the interval, and the deferred CONN right at its end (not at the later backoff deadline). `conn_times_after` are the row's CONN times after
    `first_conn_t`, `ackn_after_t` the first ACKN time after `peer_conn_t` (None when none came)."""
    problems = []
    if ackn_after_t is None or ackn_after_t - peer_conn_t > IMMEDIATE_S:
        problems.append("no ACKN within %.1f s of the peer CONN (got %s)" % (IMMEDIATE_S, None if ackn_after_t is None else "%.2f s" % (ackn_after_t - peer_conn_t)))
    if not conn_times_after:
        problems.append("no deferred CONN at all: the suppressed answer was dropped (expected one at +%.1f s of the last CONN)" % CONN_INTERVAL_S)
        return problems
    nxt = min(conn_times_after)
    gap = nxt - first_conn_t
    if gap < CONN_INTERVAL_S - EARLY_SLACK_S:
        problems.append("the next CONN left %.2f s after the last one (< %.0f s): the answer was not suppressed" % (gap, CONN_INTERVAL_S))
    elif gap > CONN_INTERVAL_S + LATE_SLACK_S:
        problems.append("the next CONN left %.2f s after the last one (> %.1f s): the deferral did not move the retry to the end of the interval" % (gap, CONN_INTERVAL_S + LATE_SLACK_S))
    return problems


def immediate_problems(peer_conn_t, conn_times_after):
    """A CONN of the peer when the row's last CONN is >= 3 s old is answered with a local CONN at once."""
    nxt = first_at_or_after(conn_times_after, peer_conn_t)
    if nxt is None:
        return ["no local CONN after the peer CONN (an allowed answer sends one at once)"]
    if nxt - peer_conn_t > IMMEDIATE_S:
        return ["the local CONN came %.2f s after the peer CONN (> %.1f s): an allowed answer leaves at once" % (nxt - peer_conn_t, IMMEDIATE_S)]
    return []


def relink_time(peer_conn_t, conn_times_after, ackn_times_after):
    """Seconds from the peer's CONN to the moment both handshake halves exist: the row's ACKN of that CONN and a row CONN that the peer answers with
    ACKN (the peer's auto ACKN is immediate). None when a half is missing."""
    ackn = first_at_or_after(ackn_times_after, peer_conn_t)
    conn = first_at_or_after(conn_times_after, peer_conn_t)
    if ackn is None or conn is None:
        return None
    return max(ackn, conn) - peer_conn_t
