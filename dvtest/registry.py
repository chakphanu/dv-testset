# SPDX-License-Identifier: MIT
"""Pure judging and timing arithmetic of the LEAK2 stream-registry cases (cases/test_stream_registry.py,
docs/stream-registry-cases.md). No I/O, no clock: the case measures, these functions decide.

DV-Router design (fabric.md "streamev", floor-control.md "Remote-origin loop detection"): a room controller
announces an open stream on `streamev/<sid>/<R>/<epoch>` and re-puts it every REFRESH_S while the stream is
open; every other controller drops a registry entry ENTRY_TTL_S after the last refresh it received (a close
removes it at once; a killed controller sends none). An RTS for a REMOTE-origin stream (over an XLX interlink
peer) whose sid is open in another room gets DENY 5: silent, no frame is delivered. Known and accepted: if
four or more refreshes are lost a looped stream may be admitted for 0 to 1 s.

  L1  loop refused while the first stream is open       judge_silent / judge_delivered
  L2  the 1 s refresh keeps an entry older than 5 s      judge_silent (probe later than ENTRY_TTL_S)
  L3  a killed controller's entry expires in 5 s         plan_after_kill, check_plan, judge_l3, pick_t_kill
  L4  the restarted controller works, nobody else does   judge_poll, compare_children
"""

REFRESH_S = 1.0             # the controller re-puts the open announcement every second while the stream is open
ENTRY_TTL_S = 5.0           # an entry expires this long after its last open / refresh
DELIVERED_MIN = 0.8         # a probe that must pass delivers at least this share of its frames
POLL_BUDGET_S = 30.0        # the restarted controller must serve a fresh local over within this time
CLOCK_SLACK_S = 0.5         # the helper's t_kill (container clock) may differ from the tester's by this much

# the L3 plan, seconds after the kill (t_kill): P1 must reach the controller while the entry is certainly alive,
# P2 after it has certainly expired
P1_AFTER_KILL_S = 1.5
P2_AFTER_KILL_S = 6.5


def entry_alive_until(kill_s=0.0):
    """(earliest, latest) time, in seconds on the scale of kill_s, at which the registry entry of a killed
    controller expires: its last refresh came between REFRESH_S before the kill and the kill itself."""
    return kill_s - REFRESH_S + ENTRY_TTL_S, kill_s + ENTRY_TTL_S


def check_plan(p1_after_kill=P1_AFTER_KILL_S, p2_after_kill=P2_AFTER_KILL_S):
    """Problems (list of text) with an L3 plan: the RTS of P1 must come before the earliest possible expiry
    (the entry certainly lives), the RTS of P2 after the latest one (it certainly expired), each with 0.5 s of
    margin for scheduling and the interlink hop. Empty list when the plan is sound."""
    early, late = entry_alive_until()
    problems = []
    if p1_after_kill > early - 0.5:
        problems.append("P1 at kill+%.1f s is not before the earliest expiry kill+%.1f s (0.5 s margin)"
                        % (p1_after_kill, early))
    if p2_after_kill < late + 0.5:
        problems.append("P2 at kill+%.1f s is not after the latest expiry kill+%.1f s (0.5 s margin)"
                        % (p2_after_kill, late))
    if p1_after_kill >= p2_after_kill:
        problems.append("P1 (kill+%.1f s) must come before P2 (kill+%.1f s)" % (p1_after_kill, p2_after_kill))
    return problems


def pick_t_kill(helper_mono, before, after, slack=CLOCK_SLACK_S):
    """The kill time on the tester's monotonic scale. `helper_mono` is the t_kill the helper printed (converted
    to that scale), `before` / `after` the monotonic times around the control call. The kill happened inside
    that window, so a helper time outside it by more than `slack` means the two clocks disagree: ValueError."""
    if helper_mono < before - slack or helper_mono > after + slack:
        raise ValueError("the helper's t_kill is %.2f s outside the control call [%.2f, %.2f]: the clocks of the "
                         "container and the tester disagree" % (
                             before - helper_mono if helper_mono < before else helper_mono - after, before, after))
    return helper_mono


def share(heard, sent):
    return 0.0 if sent <= 0 else heard / float(sent)


def judge_silent(label, heard, sent, detail=""):
    """Problems for a stream that DENY 5 must keep silent: not one frame may be delivered."""
    if heard == 0:
        return []
    return ["%s: %d of %d frames delivered, expected 0 (the looped stream was admitted)%s"
            % (label, heard, sent, (" " + detail) if detail else "")]


def judge_delivered(label, heard, sent, minimum=DELIVERED_MIN, detail=""):
    """Problems for a stream that must pass: at least `minimum` of its frames delivered."""
    if share(heard, sent) >= minimum:
        return []
    return ["%s: %d of %d frames delivered (%.0f %%), expected at least %.0f %%%s"
            % (label, heard, sent, 100.0 * share(heard, sent), 100.0 * minimum, (" " + detail) if detail else "")]


def offsets_text(**moments):
    """'name=+1.50 s, ...' in the order given: moments in seconds relative to the first value that is
    named 'kill' (or to 0 when there is none)."""
    base = moments.get("kill", 0.0)
    return ", ".join("%s=%+.2f s" % (name, value - base) for name, value in moments.items() if value is not None)


def judge_l3(p1_heard, p2_heard, sent, offsets):
    """Problems of the controller-kill case. `offsets` is the text of the measured moments (offsets_text)."""
    detail = "(%s)" % offsets
    problems = judge_silent("P1 (same sid, kill+%.1f s: the entry must still live, last refresh at most %.0f s "
                            "before the kill)" % (P1_AFTER_KILL_S, REFRESH_S), p1_heard, sent, detail)
    p2 = judge_delivered("P2 (same sid, kill+%.1f s: the entry must have expired, %.0f s after its last "
                         "refresh)" % (P2_AFTER_KILL_S, ENTRY_TTL_S), p2_heard, sent, detail=detail)
    problems += p2
    if p2 and p1_heard == 0:
        problems.append("P1 was silent and P2 too: the entry did not expire within %.0f s of the kill, or the "
                        "restarted controller re-admitted the holder's continuing over and refreshes again "
                        "(compare the L4 poll), or the XLX peer dropped the repeated sid %s" % (ENTRY_TTL_S, detail))
    return problems


def poll_summary(attempts, budget_s=POLL_BUDGET_S):
    """(first_ok_offset or None, text) of the L4 poll: attempts are (offset_s, heard, sent) with the offset of
    the start of the over after the kill. An attempt succeeds when it delivers DELIVERED_MIN of its frames."""
    first = None
    parts = []
    for offset, heard, sent in attempts:
        ok = share(heard, sent) >= DELIVERED_MIN
        parts.append("+%.1f s:%d/%d" % (offset, heard, sent))
        if ok and first is None:
            first = offset
    text = "%d attempts in %.0f s [%s]" % (len(attempts), budget_s, ", ".join(parts))
    return first, text


def judge_poll(attempts, relinked_ok=None, budget_s=POLL_BUDGET_S):
    """Problems of the poll: some over must have been heard within the budget. `relinked_ok` (None when not
    tried) says whether a freshly linked pair worked after the pre-linked pair failed."""
    first, text = poll_summary(attempts, budget_s)
    if first is not None and first <= budget_s:
        return []
    extra = ""
    if relinked_ok is not None:
        extra = "; a freshly linked pair %s" % ("WORKED (the pre-linked clients lost their membership)" if relinked_ok
                                              else "did not work either")
    return ["the restarted controller served no local over within %.0f s of the kill: %s%s" % (budget_s, text, extra)]


def compare_children(before, after):
    """Problems when anything but ONE dv-room changed. `before` / `after` are lists of (pid, starttime) of the
    dv-room processes (starttime in clock ticks after boot: with the pid it names a process). Exactly one
    process of `before` is gone and exactly one is new; every other one is the same process."""
    b, a = set(before), set(after)
    gone, new = sorted(b - a), sorted(a - b)
    problems = []
    if len(before) != len(b) or len(after) != len(a):
        problems.append("a (pid, starttime) pair occurs twice in a dv-room listing")
    if len(gone) != 1:
        problems.append("%d dv-room processes of %d are gone after the kill, expected exactly 1 (gone %s)"
                        % (len(gone), len(b), gone))
    if len(new) != 1:
        problems.append("%d new dv-room processes after the kill, expected exactly 1 (new %s): the launcher did not "
                        "respawn the controller once" % (len(new), new))
    if len(a) != len(b):
        problems.append("dv-room count %d before, %d after: another controller was lost or added" % (len(b), len(a)))
    return problems


def judge_launcher(before, after):
    """Problems when the launcher dv-router is not the same process: lists of (pid, starttime)."""
    if sorted(before) == sorted(after) and len(before) == 1:
        return []
    return ["launcher dv-router changed or is not unique: before %s, after %s" % (sorted(before), sorted(after))]
