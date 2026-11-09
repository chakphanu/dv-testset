# SPDX-License-Identifier: MIT
"""Pure helpers of DP15-3a (cases/test_dp15_worker.py): when is the "gap" web text really posted while the worker is down.

The launcher respawns a killed worker after 0.5 s, so the down window is about 0.5 s. A text posted after the `data_worker_up`
event is a new item that is rendered correctly; only a text published while the worker is down is never rendered (data.md 4.1).
The run 20261109T213056Z posted it 27 ms after the up event (a fixed sleep and the POST itself used up the window), so the
attempt must be judged by the observed events: the down event first, then the POST, whose response is complete before the up
event. An attempt that misses the window does not count; it is recorded and the kill is repeated. Stdlib only; unit tests:
cases/unit/test_worker_gap_support.py.
"""

MAX_ATTEMPTS = 3        # the first kill and up to two repeats
DOWN_SEEN_S = 2.0       # how long to wait for the down event after a kill before the attempt is a hard failure (not a race)


def judge(t_kill, t_down, t_post0, t_post1, t_up):
    """One attempt, all times on one monotonic clock. Returns a dict: valid (the POST began after the down event was observed and
    its response was complete before the up event), hard (no down event / no up event / no POST: not a timing race, the case's
    own assertions report it), reason, and the margins down_to_post = t_post0 - t_down, post_to_up = t_up - t_post1 (seconds, a
    negative value is the overlap) and kill_to_post, kill_to_up; None where a time is missing."""
    def diff(a, b):
        return None if a is None or b is None else a - b
    out = {"valid": False, "hard": False, "reason": "",
           "down_to_post": diff(t_post0, t_down), "post_to_up": diff(t_up, t_post1),
           "kill_to_post": diff(t_post1, t_kill), "kill_to_up": diff(t_up, t_kill)}
    if t_down is None:
        out.update(hard=True, reason="no data_worker_down event after the kill")
    elif t_post0 is None or t_post1 is None:
        out.update(hard=True, reason="the gap text was not posted")
    elif t_up is None:
        out.update(hard=True, reason="no data_worker_up event after the kill")
    elif t_post0 < t_down:
        out.update(reason="the POST began %.3f s before the down event was observed" % (t_down - t_post0))
    elif t_post1 >= t_up:
        out.update(reason="the up event came %.3f s before the POST response was complete" % (t_post1 - t_up))
    else:
        out.update(valid=True, reason="inside the down window")
    return out


def run_attempts(do_attempt, record, max_attempts=MAX_ATTEMPTS):
    """Call do_attempt(n) for n = 1.. until one is valid or hard, at most max_attempts times; every attempt is recorded
    through record(line). Returns the list of the attempts (dicts from do_attempt, each holding the judge() keys)."""
    attempts = []
    for n in range(1, max_attempts + 1):
        got = do_attempt(n)
        attempts.append(got)
        record(describe(n, got))
        if got["valid"] or got["hard"]:
            break
    return attempts


def _fmt(v):
    return "n/a" if v is None else "%.3f s" % v


def describe(n, got):
    fmt = _fmt
    return "gap attempt %d: %s (%s); down->POST %s, POST->up %s, kill->POST %s, kill->up %s" % (
        n, "valid" if got["valid"] else ("hard failure" if got["hard"] else "does not count"), got["reason"],
        fmt(got["down_to_post"]), fmt(got["post_to_up"]), fmt(got["kill_to_post"]), fmt(got["kill_to_up"]))


def verdict(attempts):
    """(valid attempt or None, skip reason or None): a hard failure is left to the case's own assertions (no skip)."""
    if attempts and attempts[-1]["valid"]:
        return attempts[-1], None
    if attempts and attempts[-1]["hard"]:
        return None, None
    return None, "the gap text could not be posted inside the worker down window in %d attempt(s): %s" % (
        len(attempts), "; ".join("attempt %d: %s (down->POST %s, POST->up %s)" % (
            i, a["reason"], _fmt(a["down_to_post"]), _fmt(a["post_to_up"])) for i, a in enumerate(attempts, 1)))
