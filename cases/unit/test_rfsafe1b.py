# SPDX-License-Identifier: MIT
"""dvtest/rfsafe1b.py: the RFSAFE1b bounds of the fence view after a healthy AX.25 child restart (DV-Router review 243), strict and lenient."""
import pytest

from dvtest import rfsafe1b as rb
from dvtest.txguard import Observation

pytestmark = pytest.mark.unit

S = rb.Sample


def healthy(end=14.0, cause="startup_guard", step=0.5):
    """Fenced reads every `step` s with the guard counting down to 13.0 s after the origin, then the first unfenced read at `end`."""
    out, t = [], 0.1
    while t < end - 0.01:
        out.append(S(t, True, cause, max(0, int(round((13.0 - t) * 1000)))))
        t += step
    out.append(S(end, False, None, None))
    return out


def test_a_healthy_trace_is_clean_in_both_modes():
    for strict in (True, False):
        j = rb.judge(healthy(), strict=strict)
        assert j.problems == [] and j.findings == [] and j.causes == ["startup_guard"] and j.end_t == 14.0, j


def test_strict_asserts_the_clear_time_both_ways():
    late = rb.judge(healthy(end=16.6), strict=True)
    assert any("expected within 16.0 s" in p for p in late.problems)
    assert rb.judge(healthy(end=14.5), strict=True).problems == []
    early = rb.judge(healthy(end=12.0), strict=True)
    assert any("before the guard" in p for p in early.problems)
    assert rb.judge(healthy(end=12.6), strict=True).problems == []                       # the slack for an origin taken at the restart return
    assert rb.judge(healthy(end=12.0), strict=True, check_early=False).problems == []     # an origin that is not the child start
    assert rb.judge(healthy(end=16.6), strict=False).problems == []                       # lenient: nothing asserted
    still = rb.judge([S(0.1, True, "startup_guard", 12900), S(19.9, True, "startup_guard", 0)], strict=True)
    assert any("still fenced" in p for p in still.problems)
    assert any("never showed a fence" in p for p in rb.judge([S(0.1, False, None, None)], strict=True).problems)


def test_strict_asserts_the_guard_value_range_and_monotony_within_an_epoch():
    base = healthy()
    assert any("above 13000" in p for p in rb.judge([S(0.1, True, "startup_guard", 13500)] + base[1:], strict=True).problems)
    assert any("not an int" in p for p in rb.judge([S(0.1, True, "startup_guard", 12.5)] + base[1:], strict=True).problems)
    assert any("not an int" in p for p in rb.judge([S(0.1, True, "startup_guard", True)] + base[1:], strict=True).problems)
    up = [S(0.1, True, "startup_guard", 12900), S(0.6, True, "startup_guard", 12950)] + base[2:]
    assert any("increased within one epoch" in p for p in rb.judge(up, strict=True).problems)
    equal = [S(0.1, True, "startup_guard", 12900), S(0.6, True, "startup_guard", 12900)] + base[2:]
    assert not any("increased" in p for p in rb.judge(equal, strict=True).problems)       # equal values in successive reads are expected
    reset = [S(0.1, True, "recovery_probe", 5000), S(0.6, True, "startup_guard", 12900)] + [s for s in base if s.t > 1.0]
    assert not any("increased" in p for p in rb.judge(reset, strict=True).problems)       # a new epoch (another cause) may reset it


def test_strict_predicts_the_end_from_read_time_plus_the_guard_within_1_5_s():
    drift = healthy(end=14.0)
    drift[2] = S(drift[2].t, True, "startup_guard", 9000)                                # predicts 10.5 s, the end is 14.0 s
    j = rb.judge(drift, strict=True)
    assert any("predicts the end" in p for p in j.problems)
    assert rb.judge(drift, strict=False).problems == []
    near = healthy(end=14.3)                                                             # 1.3 s of view lag: inside the 1.5 s tolerance
    assert rb.judge(near, strict=True).problems == []
    assert rb.judge(healthy(end=15.0), strict=True).problems != []                       # 2.0 s after the prediction


def test_other_causes_are_findings_and_unknown_labels_fail_strict_only():
    j = rb.judge(healthy(cause="recovery_probe"), strict=True)
    assert j.causes == ["recovery_probe"] and len(j.findings) == 1 and "recovery_probe" in j.findings[0] and j.problems == []
    j = rb.judge(healthy(cause="own_token_pending"), strict=True)
    assert j.problems == [] and "own_token_pending" in j.findings[0]                      # the new fixed label is accepted
    unknown = rb.judge(healthy(cause="mystery"), strict=True)
    assert any("outside the fixed label set" in p for p in unknown.problems)
    assert rb.judge(healthy(cause="mystery"), strict=False).problems == []
    assert rb.FENCE_LABELS == ("startup_guard", "session_loss", "foreign_holder", "recovery_probe", "own_token_pending")


def obs(fenced, fence=None, guard=None, found=True, scope=None):
    return Observation(found, fenced, fenced, fence, guard, ["k"], "via", scope)


def test_collect_polls_until_the_port_is_unfenced_and_reports_scope_and_timeouts():
    now = {"t": 100.0}
    clock = lambda: now["t"]  # noqa: E731
    sleep = lambda s: now.__setitem__("t", now["t"] + s)  # noqa: E731
    reads = iter([obs(True, "startup_guard", 12000)] * 3 + [obs(False)])
    trace = rb.collect(lambda: next(reads), 99.0, clock=clock, sleep=sleep, poll_s=0.5)
    assert trace.state == "unfenced" and [round(s.t, 1) for s in trace.samples] == [1.0, 1.5, 2.0, 2.5] and trace.end_t == 2.5
    assert trace.samples[0] == S(1.0, True, "startup_guard", 12000) and trace.samples[-1].fenced is False
    assert rb.collect(lambda: obs(False), 99.0, clock=clock, sleep=sleep).state == "clear"
    assert rb.collect(lambda: obs(False, scope="no_modem_object", found=False), 99.0, clock=clock, sleep=sleep).state == "scope"
    forever = rb.collect(lambda: obs(True, "startup_guard", 5000), now["t"], clock=clock, sleep=sleep, max_wait_s=3.0)
    assert forever.state == "timeout" and forever.end_t is None
    assert rb.collect(lambda: obs(False, found=False), now["t"], clock=clock, sleep=sleep, max_wait_s=2.0).state == "not_found"


def test_the_switch_reads_dvt_rfsafe1b():
    assert rb.strict_on({"DVT_RFSAFE1B": "on"}) and rb.strict_on({"DVT_RFSAFE1B": "1"}) and not rb.strict_on({}) and not rb.strict_on({"DVT_RFSAFE1B": "off"})
