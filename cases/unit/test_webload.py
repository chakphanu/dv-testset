# SPDX-License-Identifier: MIT
"""dvtest/webload.py: markers, per-listener statistics, the adopted gate, pacing and the procfs readers of
the web load test (cases/test_web_load.py)."""
import pytest

from dvtest import webload as wl

pytestmark = pytest.mark.unit


# ---- G.711 and markers

def test_ulaw_known_values():
    assert wl.ulaw_encode(0) == 0xFF
    assert wl.ulaw_decode(0xFF) == 0 and wl.ulaw_decode(0x7F) == 0
    assert wl.ulaw_encode(32767) == 0x80 and wl.ulaw_decode(0x80) == 32124
    assert wl.ulaw_encode(-32768) == 0x00 and wl.ulaw_decode(0x00) == -32124
    assert wl.ulaw_encode(1000) == 0xCE


def test_every_code_but_minus_zero_survives_decode_encode():
    for code in range(256):
        if code != 0x7F:
            assert wl.ulaw_encode(wl.ulaw_decode(code)) == code, code
    assert wl.ulaw_encode(wl.ulaw_decode(0x7F)) == 0xFF        # the one aliased code, never a digit


def test_marker_digits_round_trip_and_bounds():
    for index in (0, 1, 126, 127, 128, 3249, 3299, 16129, wl.MAX_INDEX):
        assert wl.payload_index(wl.expected_pcmu(index)) == index
    assert max(wl.marker_digits(wl.MAX_INDEX)) == 126
    assert 0x7F not in wl.expected_pcmu(wl.MAX_INDEX)[:4]
    with pytest.raises(ValueError):
        wl.marker_digits(-1)
    with pytest.raises(ValueError):
        wl.marker_digits(wl.MAX_INDEX + 1)


def test_pcm_frame_companded_equals_expected_payload():
    """What the edge does (standard companding of the 320 byte frame) leaves the marker intact."""
    for index in (0, 5, 250, 3250, 3299, 99999):
        pcm = wl.frame_pcm(index)
        assert len(pcm) == 320
        samples = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, 320, 2)]
        assert bytes(wl.ulaw_encode(s) for s in samples) == wl.expected_pcmu(index)


def test_payload_index_rejects_non_markers():
    good = wl.expected_pcmu(77)
    assert wl.payload_index(good) == 77
    assert wl.payload_index(good[:-1]) is None
    assert wl.payload_index(good + b"\0") is None
    assert wl.payload_index(bytes([127]) + good[1:]) is None
    assert wl.payload_index(bytes(160)) == 0
    assert wl.tail_matches(good) and not wl.tail_matches(good[:4] + b"\xff" * 156)


def test_tone_frame_repeats_every_20_ms():
    """14 whole cycles per frame: every frame has the same tail, so a listener needs no frame position."""
    assert wl.BASE_PCMU == bytes(wl.ulaw_encode(wl._tone_samples()[i]) for i in range(160))
    assert wl.frame_pcm(1)[8:] == wl.frame_pcm(2)[8:]


# ---- plan

def test_plan_window():
    plan = wl.Plan()
    assert plan.warmup_frames == 250 and plan.window_frames == 3000 and plan.drain_frames == 50
    assert plan.window == (250, 3250) and plan.total_frames == 3300
    assert wl.Plan(listeners=1, window_s=1.0, warmup_s=0.0, drain_s=0.0).total_frames == 50
    with pytest.raises(ValueError):
        wl.Plan(rate=100)
    with pytest.raises(ValueError):
        wl.Plan(listeners=0)


# ---- statistics

def test_nearest_rank():
    v = list(range(1, 101))
    assert wl.nearest_rank(v, 50) == 50 and wl.nearest_rank(v, 99) == 99 and wl.nearest_rank(v, 100) == 100
    assert wl.nearest_rank(v, 1) == 1
    assert wl.nearest_rank([7.0], 99) == 7.0
    assert wl.nearest_rank([], 99) is None
    assert wl.nearest_rank([1, 2, 3, 4], 75) == 3


def make_arrivals(plan, shift=None, skip=(), dup=(), swap=None, step_ms=20.0):
    """Arrivals of a perfect listener: frame i at i*20 ms (+ shift[i] ms)."""
    shift = shift or {}
    out = []
    for i in range(plan.total_frames):
        if i in skip:
            continue
        t = int((i * step_ms + shift.get(i, 0.0)) * 1e6)
        out.append((t, i))
        if i in dup:
            out.append((t + 1000, i))
    if swap is not None:
        a, b = swap
        pos = {idx: k for k, (_t, idx) in enumerate(out)}
        out[pos[a]], out[pos[b]] = out[pos[b]], out[pos[a]]
        # keep the arrival times increasing: only the indices are exchanged
        ts = sorted(t for t, _i in out)
        out = [(t, i) for t, (_o, i) in zip(ts, out)]
    return out


def test_perfect_listener():
    plan = wl.Plan(listeners=1, window_s=10.0, warmup_s=1.0, drain_s=0.5)
    row = wl.analyse_listener(plan, make_arrivals(plan))
    assert row["received"] == row["expected"] == 500 and row["lost"] == 0
    assert row["duplicate"] == row["reordered"] == row["invalid"] == 0
    assert row["samples"] == 499 and row["p99_ms"] == 0.0 and row["max_ms"] == 0.0


def test_loss_counts_only_the_window():
    plan = wl.Plan(listeners=1, window_s=10.0, warmup_s=1.0, drain_s=0.5)
    first, end = plan.window
    row = wl.analyse_listener(plan, make_arrivals(plan, skip={0, 1, 10, first + 3, end - 1, end + 2}))
    assert row["lost"] == 2 and row["received"] == 498           # warmup and tail losses do not count
    assert row["max_ms"] == pytest.approx(20.0)                  # a hole is a 40 ms spacing; the edge of the window has no spacing


def test_duplicate_and_reorder():
    plan = wl.Plan(listeners=1, window_s=10.0, warmup_s=1.0, drain_s=0.5)
    first, _end = plan.window
    row = wl.analyse_listener(plan, make_arrivals(plan, dup={first + 5, first + 6}))
    assert row["duplicate"] == 2 and row["lost"] == 0 and row["reordered"] == 0
    row = wl.analyse_listener(plan, make_arrivals(plan, swap=(first + 10, first + 11)))
    assert row["reordered"] == 1 and row["duplicate"] == 0 and row["lost"] == 0


def test_jitter_is_the_deviation_from_20_ms():
    plan = wl.Plan(listeners=1, window_s=10.0, warmup_s=1.0, drain_s=0.5)
    first, _end = plan.window
    # one packet 7 ms late: spacings 27 ms before it and 13 ms after it -> deviations 7 and 7
    row = wl.analyse_listener(plan, make_arrivals(plan, shift={first + 100: 7.0}))
    assert row["max_ms"] == pytest.approx(7.0) and row["p50_ms"] == 0.0
    assert row["p99_ms"] == 0.0                                  # two samples of 499 are under the 99th rank (494)
    # a constant 25 ms cadence is a constant 5 ms deviation
    row = wl.analyse_listener(plan, make_arrivals(plan, step_ms=25.0))
    assert row["p50_ms"] == row["p99_ms"] == row["max_ms"] == pytest.approx(5.0)


def test_worst_listener_is_not_an_average():
    rows = [{"p99_ms": 1.0, "max_ms": 3.0}, {"p99_ms": 19.0, "max_ms": 59.0}, {"p99_ms": 2.0, "max_ms": 4.0}]
    assert wl.worst(rows, "p99_ms") == 19.0 and wl.worst(rows, "max_ms") == 59.0
    assert wl.worst([], "p99_ms") is None and wl.worst([{"p99_ms": None}], "p99_ms") is None


def test_aggregate_sums_and_takes_the_worst():
    plan = wl.Plan(listeners=2, window_s=2.0, warmup_s=0.0, drain_s=0.0)
    a = wl.analyse_listener(plan, make_arrivals(plan))
    b = wl.analyse_listener(plan, make_arrivals(plan, skip={3}, dup={4}), invalid=2)
    total = wl.aggregate([a, b])
    assert total["listeners"] == 2 and total["expected"] == 200 and total["received"] == 199
    assert total["lost"] == 1 and total["duplicate"] == 1 and total["invalid"] == 2
    assert total["worst_max_ms"] == b["max_ms"] == pytest.approx(20.0)


# ---- source

def test_source_lateness():
    sched = [i * 0.02 for i in range(100)]
    actual = [s + 0.0004 for s in sched]
    actual[50] += 0.0236                                          # 24 ms late: a missed slot
    stat = wl.source_lateness(sched, actual)
    assert stat["frames"] == 100 and stat["missed"] == 1
    assert stat["max_ms"] == pytest.approx(24.0, abs=0.01) and stat["min_ms"] == pytest.approx(0.4, abs=0.01)
    assert wl.source_lateness([], [])["max_ms"] is None


# ---- the gate

def good_run():
    edge = {"cpu_pct": 16.2, "vmhwm_kib": 33.6 * 1024, "vmhwm_pre_kib": 14.0 * 1024}
    totals = {"worst_p99_ms": 4.6, "worst_max_ms": 12.5, "lost": 0, "duplicate": 0, "reordered": 0, "invalid": 0}
    return edge, totals, {"missed": 0, "max_ms": 1.2}


def verdicts(edge, totals, source, listeners=200):
    return {c.name: c.ok for c in wl.evaluate_gates(listeners, edge, totals, source)}


def test_c90_reference_passes():
    edge, totals, source = good_run()
    checks = wl.evaluate_gates(200, edge, totals, source)
    assert wl.all_ok(checks) and len(checks) == 10
    assert verdicts(edge, totals, source)["edge RSS growth over pre-client startup, KiB"]


@pytest.mark.parametrize("mutate,name", [
    (lambda e, t, s: e.update(cpu_pct=25.01), "edge cpu, percent of one core"),
    (lambda e, t, s: e.update(vmhwm_kib=64.01 * 1024, vmhwm_pre_kib=50.0 * 1024), "edge peak RSS (VmHWM), MiB"),
    (lambda e, t, s: t.update(worst_p99_ms=20.001), "worst-listener jitter p99, ms"),
    (lambda e, t, s: t.update(worst_max_ms=60.5), "worst-listener jitter max, ms"),
    (lambda e, t, s: t.update(lost=1), "lost packets"),
    (lambda e, t, s: t.update(duplicate=1), "duplicate packets"),
    (lambda e, t, s: t.update(reordered=1), "reordered packets"),
    (lambda e, t, s: t.update(invalid=1), "invalid packets"),
    (lambda e, t, s: s.update(missed=1), "source missed deadlines"),
])
def test_each_limit_fails_alone(mutate, name):
    edge, totals, source = good_run()
    mutate(edge, totals, source)
    got = verdicts(edge, totals, source)
    assert got[name] is False
    assert [n for n, ok in got.items() if not ok] == [name]


def test_limits_are_inclusive_and_growth_scales():
    edge, totals, source = good_run()
    edge.update(cpu_pct=25.0, vmhwm_kib=64.0 * 1024)
    totals.update(worst_p99_ms=20.0, worst_max_ms=60.0)
    edge["vmhwm_pre_kib"] = edge["vmhwm_kib"] - (200 * 100.0 + 4096)
    assert wl.all_ok(wl.evaluate_gates(200, edge, totals, source))
    edge["vmhwm_pre_kib"] -= 1
    assert not verdicts(edge, totals, source)["edge RSS growth over pre-client startup, KiB"]
    assert wl.growth_limit_kib(1) == 100.0 + 4096 and wl.growth_limit_kib(200) == 24096.0


def test_missing_numbers_fail():
    edge, totals, source = good_run()
    for key in ("cpu_pct", "vmhwm_kib"):
        e = dict(edge)
        e.pop(key)
        assert not wl.all_ok(wl.evaluate_gates(200, e, totals, source))
    t = dict(totals, worst_p99_ms=None)
    assert not wl.all_ok(wl.evaluate_gates(200, edge, t, source))
    assert not wl.all_ok(wl.evaluate_gates(200, edge, totals, {}))


# ---- pacing and set-up

def test_paced_offsets_respect_the_token_bucket():
    # 1/s burst 5, one token kept back, 5 % margin: 4 at once, then 1.05 s apart
    off = wl.paced_offsets(200, wl.RTC_IP_RATE, wl.RTC_IP_BURST)
    assert off[:4] == [0.0] * 4 and off[4] == pytest.approx(1.05) and off[5] == pytest.approx(2.10)
    assert off[-1] == pytest.approx(196 * 1.05)
    assert all(b >= a for a, b in zip(off, off[1:]))
    # simulate the server's bucket: no request is ever refused
    tokens, last = float(wl.RTC_IP_BURST), 0.0
    for t in off:
        tokens = min(wl.RTC_IP_BURST, tokens + (t - last) * wl.RTC_IP_RATE)
        last = t
        assert tokens >= 1.0
        tokens -= 1.0
    assert wl.paced_offsets(3, 5.0, 5) == [0.0, 0.0, 0.0]
    assert wl.paced_offsets(0, 1.0, 5) == []


def test_accounts_logins_and_rooms():
    assert wl.accounts_needed(200) == 9 and wl.accounts_needed(1) == 1 and wl.accounts_needed(24) == 1
    assert wl.accounts_needed(25) == 2 and wl.accounts_needed(50) == 3
    s = wl.slots(200)
    assert len(s) == 200 and s[0].login == 0 and s[199].login == 24 and s[199].account == 8
    assert sum(1 for x in s if x.room == "A") == 100 and sum(1 for x in s if x.room == "B") == 100
    per_login = {}
    for x in s:
        per_login.setdefault(x.login, []).append(x.index)
    assert max(len(v) for v in per_login.values()) == wl.CIRCUITS_PER_LOGIN
    per_account = {}
    for x in s:
        per_account.setdefault(x.account, set()).add(x.login)
    assert max(len(v) for v in per_account.values()) == wl.LOGINS_PER_ACCOUNT
    assert [x.room for x in wl.slots(1)] == ["A"]
    assert [x.room for x in wl.slots(3)] == ["A", "A", "B"]
    with pytest.raises(ValueError):
        wl.slots(201)


def test_workers_get_whole_logins():
    owner = {}
    for x in wl.slots(200):
        owner.setdefault(x.login, set()).add(wl.worker_of_login(x.login, 4))
    assert all(len(v) == 1 for v in owner.values())
    assert sorted({next(iter(v)) for v in owner.values()}) == [0, 1, 2, 3]


def test_alive_schedule_is_the_pages():
    it = wl.alive_schedule()
    got = [next(it) for _ in range(9)]
    assert got == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 15.0, 25.0, 35.0]


# ---- host samples

PROC_OUT = "pid=17\ncomm=dv-edge-webrtc\nutime=612\nstime=340\nclk_tck=100\nvmhwm_kb=34400\nvmrss_kb=33000\nthreads=9\nt=1000.5\n"


def test_parse_procstat_and_cpu_percent():
    a = wl.parse_procstat_out(PROC_OUT)
    assert a.pid == 17 and a.comm == "dv-edge-webrtc" and a.ticks == 952 and a.cpu_s == pytest.approx(9.52)
    assert a.vmhwm_kib == 34400 and a.threads == 9
    b = wl.parse_procstat_out(PROC_OUT.replace("utime=612", "utime=1000").replace("stime=340", "stime=500")
                              .replace("t=1000.5", "t=1060.5"))
    assert wl.cpu_percent(a, b) == pytest.approx(100.0 * (15.0 - 9.52) / 60.0)
    with pytest.raises(ValueError):
        wl.cpu_percent(b, a)


@pytest.mark.parametrize("text", ["", "pid=1\n", PROC_OUT.replace("utime=612\n", ""), PROC_OUT.replace("t=1000.5", "t=soon")])
def test_parse_procstat_refuses_bad_replies(text):
    with pytest.raises(ValueError):
        wl.parse_procstat_out(text)


def test_bucket_matches_the_session_budget():
    now = [0.0]
    b = wl.Bucket(2.0, 10, reserve=3, clock=lambda: now[0])
    waits = [b.take() for _ in range(7)]
    assert waits == [0.0] * 7                              # burst 10 minus 3 reserved
    assert b.take() == pytest.approx(0.5)                  # the eighth waits for one token at 2/s
    assert b.take() == pytest.approx(1.0)                  # and reservations queue behind it
    now[0] += 10.0
    assert b.take() == 0.0                                 # refilled, never above the cap
    now[0] += 100.0
    assert [b.take() for _ in range(8)] == [0.0] * 7 + [pytest.approx(0.5)]


def test_eight_circuits_at_the_page_schedule_do_not_fit_one_session():
    """Why the generator renews once per 10 s: the page's schedule (at once, then every second until six answers) for eight
    circuits is 48 requests in 5 s against a session budget of 10 + 2/s."""
    page = sum(1 for k, off in zip(range(40), wl.alive_schedule()) if off <= 5.0) * 8
    assert page == 48 and page > 10 + 2 * 5
    ours = sum(1 for off, _ in zip(wl.alive_schedule(first_seconds=1), range(3)) if off <= 5.0) * 8
    assert ours == 8 and ours <= 10
