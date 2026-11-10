# SPDX-License-Identifier: MIT
"""The bounds of the AX.25 fence view after a healthy child restart on an image with RFSAFE1b (DV-Router review 243, "Final bounds per cause for the rig").

Switch: DVT_RFSAFE1B=on asserts them (strict). Off (the default) keeps the lenient behaviour of dvtest/txguard.py (the view may show a fence for up to about
24.5 s and the cause recovery_probe is only recorded).

What a strict run asserts, in seconds since the child start (or, where the case cannot know it, since the restart call returned; END_EARLY_SLACK_S covers that):
* the fence view clears within VIEW_CLEAR_MAX_S (15.1 s expected: 13.0 s guard + 0.5 s declare and probe + 1.1 s refresh and edge loop + 0.5 s poll);
* it never clears before STARTUP_GUARD_S (no TX before P + G, so the end is not before the child start + 13.0 s);
* tx_guard_ms is an int <= 13000 and does not increase within one fence epoch (consecutive fenced reads with the same cause; a new epoch may reset it);
* read time + tx_guard_ms predicts the end of the last epoch within PREDICT_TOL_S (the view lags the real end by up to about 1.1 s);
* every cause is one of FENCE_LABELS (own_token_pending is the new fixed label).
A FINDING (never a failure): a cause other than startup_guard after a healthy restart (recovery_probe, own_token_pending, session_loss, foreign_holder), or a
"probe failed" line in the SUT log (dvtest/logscan.py, scanned on the host after the run). A fence that begins mid-run on AX.25 may still show up to 10 s late in
the view (RFSAFE1c): not asserted here.
"""
import os
import time
from collections import namedtuple

STARTUP_GUARD_S = 13.0
VIEW_CLEAR_MAX_S = 16.0
END_EARLY_SLACK_S = 0.5
GUARD_MS_MAX = 13000
PREDICT_TOL_S = 1.5
POLL_S = 0.5
MAX_WAIT_S = 20.0
HEALTHY_CAUSES = ("startup_guard",)
FENCE_LABELS = ("startup_guard", "session_loss", "foreign_holder", "recovery_probe", "own_token_pending")
PROBE_FAILED_NEEDLE = "probe failed"

Sample = namedtuple("Sample", "t fenced fence guard_ms")           # t: seconds since the origin; fence: the cause or None; guard_ms: int or None
Trace = namedtuple("Trace", "samples end_t state")                 # state: unfenced | clear (no fenced read) | timeout | scope | not_found
Judgement = namedtuple("Judgement", "problems findings summary end_t causes")


def strict_on(environ=None):
    """DVT_RFSAFE1B=on: the image has RFSAFE1b, so the bounds above are asserted."""
    return (environ if environ is not None else os.environ).get("DVT_RFSAFE1B", "").lower() in ("on", "1", "true", "yes")


CHILD_AGE_MAX_S = 120.0                 # a child start older than this when the case reads it is not "the restart just made"
CHILD_AGE_SKEW_S = 1.0                  # tolerated clock difference between the launcher (a container) and the tester


def child_start_origin(since_ms, now_unix_s, now_mono):
    """(origin on the monotonic clock, age in seconds) of the launcher's current child start, or (None, None) when `since_ms` cannot be it. `since_ms` is the Unix
    milliseconds of the current child start in the admin status `launcher.children[]` entry (launcher shell.rs:223; 0 only if the child never started). It is NOT an uptime, and the
    hub's procs[] row of a token-bearing edge has none by design (status.md 599-606, procs.rs:120-148). An implausible value (not a number, 0, in the future by more than the
    skew, older than CHILD_AGE_MAX_S) gives (None, None): the caller falls back to the time it saw the pid change and skips the 13.0 s lower-bound check."""
    if isinstance(since_ms, bool) or not isinstance(since_ms, (int, float)) or since_ms <= 0:
        return None, None
    age = now_unix_s - since_ms / 1000.0
    if age < -CHILD_AGE_SKEW_S or age > CHILD_AGE_MAX_S:
        return None, None
    age = max(age, 0.0)
    return now_mono - age, age


def collect(read, origin, *, clock=time.monotonic, sleep=time.sleep, poll_s=POLL_S, max_wait_s=MAX_WAIT_S):
    """Poll `read()` (a txguard Observation) every poll_s until the port is unfenced after having been fenced, at most max_wait_s after `origin` (a clock value:
    the child start, or the return of the restart call). Returns a Trace."""
    samples, was_fenced = [], False
    while True:
        obs = read()
        now = clock()
        if obs.scope:
            return Trace(samples, None, "scope")
        if obs.found:
            samples.append(Sample(now - origin, bool(obs.fenced), obs.fence if obs.fenced else None, obs.guard_ms if obs.fenced else None))
            if obs.fenced:
                was_fenced = True
            else:
                return Trace(samples, now - origin, "unfenced" if was_fenced else "clear")
        if now - origin >= max_wait_s:
            return Trace(samples, None, "timeout" if samples else "not_found")
        sleep(poll_s)


def judge(samples, *, strict, early_slack_s=END_EARLY_SLACK_S, check_early=True):
    """Judge a list of Sample (t since the origin). Returns Judgement(problems, findings, summary, end_t, causes): `problems` are failures (strict only), `findings`
    are recorded. Lenient (strict False): no problems; the summary and the causes are still returned for the record."""
    fenced = [s for s in samples if s.fenced]
    causes = []
    for s in fenced:
        if s.fence not in causes:
            causes.append(s.fence)
    end_t = None
    if samples and not samples[-1].fenced and fenced:
        end_t = samples[-1].t
    guards = [(round(s.t, 1), s.fence, s.guard_ms) for s in fenced if s.guard_ms is not None]
    shown = guards if len(guards) <= 6 else guards[:3] + ["..."] + guards[-3:]
    summary = "causes %s; fenced reads %d, first at %.1f s, view clear at %s; tx_guard_ms (t, cause, ms) %s" % (
        causes, len(fenced), fenced[0].t if fenced else -1.0, "%.1f s" % end_t if end_t is not None else "never", shown)
    findings = []
    if any(c not in HEALTHY_CAUSES for c in causes):
        findings.append("after a healthy restart the view showed %s, expected only startup_guard (RFSAFE1b)" % [c for c in causes if c not in HEALTHY_CAUSES])
    if not strict:
        return Judgement([], findings, summary, end_t, causes)
    problems = []
    if not fenced:
        problems.append("the view never showed a fence after the restart")
    unknown = [c for c in causes if c not in FENCE_LABELS]
    if unknown:
        problems.append("fence cause(s) %s are outside the fixed label set %s" % (unknown, list(FENCE_LABELS)))
    if fenced and end_t is None:
        problems.append("still fenced at the last read, %.1f s after the origin (must clear within %.1f s)" % (samples[-1].t, VIEW_CLEAR_MAX_S))
    if end_t is not None:
        if end_t > VIEW_CLEAR_MAX_S:
            problems.append("the view cleared %.1f s after the origin, expected within %.1f s" % (end_t, VIEW_CLEAR_MAX_S))
        if check_early and end_t < STARTUP_GUARD_S - early_slack_s:
            problems.append("the view cleared %.1f s after the origin: before the guard (%.1f s) ended, so TX could have happened in the guard" % (end_t, STARTUP_GUARD_S))
    prev = None
    for s in samples:
        if s.fenced and s.guard_ms is not None:
            if isinstance(s.guard_ms, bool) or not isinstance(s.guard_ms, int) or s.guard_ms < 0:
                problems.append("tx_guard_ms %r at %.1f s is not an int >= 0" % (s.guard_ms, s.t))
            elif s.guard_ms > GUARD_MS_MAX:
                problems.append("tx_guard_ms %d at %.1f s is above %d" % (s.guard_ms, s.t, GUARD_MS_MAX))
            elif prev is not None and prev.fence == s.fence and prev.guard_ms is not None and s.guard_ms > prev.guard_ms:
                problems.append("tx_guard_ms increased within one epoch (%s): %d at %.1f s, then %d at %.1f s" % (s.fence, prev.guard_ms, prev.t, s.guard_ms, s.t))
        prev = s if s.fenced else None
    if end_t is not None and fenced:
        last_cause = fenced[-1].fence
        epoch = []
        for s in reversed(samples[:-1]):
            if not s.fenced or s.fence != last_cause:
                break
            epoch.append(s)
        for s in epoch:
            if isinstance(s.guard_ms, int) and not isinstance(s.guard_ms, bool) and abs(s.t + s.guard_ms / 1000.0 - end_t) > PREDICT_TOL_S:
                problems.append("read time %.1f s + tx_guard_ms %d predicts the end at %.1f s, the view cleared at %.1f s (tolerance %.1f s)" % (
                    s.t, s.guard_ms, s.t + s.guard_ms / 1000.0, end_t, PREDICT_TOL_S))
                break
    return Judgement(problems, findings, summary, end_t, causes)
