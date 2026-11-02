# SPDX-License-Identifier: MIT
"""Pure parts of the web load test (W8, cases/test_web_load.py, docs/web-load-cases.md): the audio markers
that make loss, duplicates, reordering and invalid payloads countable, the per-listener interarrival
statistics, the adopted gate and its evaluation, the join and login pacing, and the readers of the
procfs samples the host returns. Stdlib only; the WebRTC client itself is dvtest/tools/webload_gen.py.

Measurement scope, as in DV-Router's own harness (docs/project/testing.md "Gates"): 200 listeners x 50 packets/s
(20 ms PCMU), a steady window of 60 s after a 5 s warmup; per listener the absolute deviation of the spacing
of successive unique authenticated RTP packets from 20 ms, taken where the client reads the datagram (before
any jitter buffer); nearest-rank p50 / p99 / max per listener, and the gate takes the WORST listener, never an
average of percentiles.

Markers. The source is a USRP talker: 320 bytes of PCM (160 samples at 8 kHz) every 20 ms. Frame `i` is a
700 Hz tone (14 whole cycles, so every frame is the same) whose first four samples are replaced by exactly
representable mu-law levels that spell `i` in base 127. The edge companding is the standard G.711 table, so
the listener receives 160 bytes whose first four are those digits. A listener therefore knows which source
frame every packet is.
"""
import math
import time
from dataclasses import dataclass, field

FRAME_SAMPLES = 160
FRAME_MS = 20.0
TONE_HZ = 700.0
TONE_AMP = 8000
BASE = 127                      # digits 0..126 are the mu-law codes 0x00..0x7E (0x7F is "minus zero")
MARKER_BYTES = 4
MAX_INDEX = BASE ** MARKER_BYTES - 1

# adopted gate (docs/project/testing.md "Gates", "Browser/NAT rig criteria")
GATE = {
    "cpu_pct_of_one_core": 25.0,
    "peak_rss_mib": 64.0,
    "growth_kib_per_listener": 100.0,
    "growth_fixed_mib": 4.0,
    "jitter_p99_ms": 20.0,
    "jitter_max_ms": 60.0,
}

# DV-Router limits the set-up has to respect (docs/design/web.md "Resource bounds (MS9)", "Signalling handoff")
CIRCUITS_PER_LOGIN = 8
LOGINS_PER_ACCOUNT = 3          # sessions per account
LISTENERS_PER_ROOM = 100
RTC_IP_RATE = 1.0               # offers per second per client address ...
RTC_IP_BURST = 5                # ... with this burst
LOGIN_RATE = 1.0
LOGIN_BURST = 10


# ---------------------------------------------------------------- G.711 mu-law

_BIAS = 0x84
_CLIP = 32635


def ulaw_encode(sample):
    """G.711 mu-law code of a 16 bit sample (the Sun reference algorithm)."""
    neg = sample < 0
    s = -sample if neg else sample
    s = min(s, _CLIP) + _BIAS
    exp = max((s >> 7).bit_length() - 1, 0)
    mant = (s >> (exp + 3)) & 0x0F
    return ((0x80 if neg else 0) | (exp << 4) | mant) ^ 0xFF


def ulaw_decode(code):
    c = ~code & 0xFF
    s = (((c & 0x0F) << 3) + _BIAS) << ((c >> 4) & 7)
    s -= _BIAS
    return -s if c & 0x80 else s


def _tone_samples():
    return [int(round(TONE_AMP * math.sin(2.0 * math.pi * TONE_HZ * n / 8000.0))) for n in range(FRAME_SAMPLES)]


_TONE = _tone_samples()
BASE_PCMU = bytes(ulaw_encode(s) for s in _TONE)


def marker_digits(index):
    """The four base-127 digits (least significant first) of a frame index."""
    if not 0 <= index <= MAX_INDEX:
        raise ValueError("frame index %r outside 0..%d" % (index, MAX_INDEX))
    out = []
    for _ in range(MARKER_BYTES):
        out.append(index % BASE)
        index //= BASE
    return out


def frame_pcm(index):
    """The 320 byte S16LE PCM frame `index` of the source."""
    samples = list(_TONE)
    for pos, digit in enumerate(marker_digits(index)):
        samples[pos] = ulaw_decode(digit)
    return b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples)


def expected_pcmu(index):
    """The 160 PCMU bytes a listener must receive for source frame `index`."""
    return bytes(marker_digits(index)) + BASE_PCMU[MARKER_BYTES:]


def payload_index(payload):
    """Source frame index spelled by a received 160 byte payload, or None when it is not a marker frame
    (wrong length, a digit outside 0..126)."""
    if len(payload) != FRAME_SAMPLES:
        return None
    index = 0
    for pos in range(MARKER_BYTES - 1, -1, -1):
        digit = payload[pos]
        if digit >= BASE:
            return None
        index = index * BASE + digit
    return index


def tail_matches(payload):
    """True when the bytes after the marker equal the reference encoder's tone (informational: a different
    but consistent companding table would show here and nowhere else)."""
    return payload[MARKER_BYTES:] == BASE_PCMU[MARKER_BYTES:]


# ------------------------------------------------------------------ the plan

@dataclass(frozen=True)
class Plan:
    """Frame budget of one run. Frames are numbered from 0; the warmup is the first `warmup_frames`, the
    measured window the next `window_frames`, then a short tail the listeners may still receive."""
    listeners: int = 200
    rate: int = 50
    warmup_s: float = 5.0
    window_s: float = 60.0
    drain_s: float = 1.0

    def __post_init__(self):
        if self.rate != 50:
            raise ValueError("20 ms PCMU packets: rate is 50 pps")
        if self.listeners < 1 or self.window_s <= 0 or self.warmup_s < 0 or self.drain_s < 0:
            raise ValueError("bad plan %r" % (self,))

    @property
    def warmup_frames(self):
        return int(round(self.warmup_s * self.rate))

    @property
    def window_frames(self):
        return int(round(self.window_s * self.rate))

    @property
    def drain_frames(self):
        return int(round(self.drain_s * self.rate))

    @property
    def window(self):
        """(first, end) frame indices of the measured window; end is exclusive."""
        return self.warmup_frames, self.warmup_frames + self.window_frames

    @property
    def total_frames(self):
        return self.warmup_frames + self.window_frames + self.drain_frames


# ------------------------------------------------------- per-listener statistics

def nearest_rank(sorted_values, p):
    """Nearest-rank percentile (p in 0..100) of an ascending list; None when empty."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def deviations_ms(times_ns, interval_ms=FRAME_MS):
    """abs(spacing - interval) in ms between successive arrival times (ns)."""
    return [abs((b - a) / 1e6 - interval_ms) for a, b in zip(times_ns, times_ns[1:])]


def analyse_listener(plan, arrivals, invalid=0):
    """Counts and jitter of one listener.

    `arrivals` is [(arrival ns, source frame index)] in arrival order, valid markers only; `invalid` is the
    number of datagrams that failed authentication or carried no valid marker. Window = the plan's measured
    window. lost = window frames never received; duplicate = an index received again; reordered = a packet
    whose index is below the highest one already seen (a duplicate is counted once, as a duplicate). Jitter
    is taken over the unique in-window packets in arrival order."""
    first, end = plan.window
    seen = set()
    top = -1
    dup = reorder = 0
    in_window = []
    for t_ns, idx in arrivals:
        if idx in seen:
            dup += 1
            continue
        seen.add(idx)
        if idx < top:
            reorder += 1
        else:
            top = idx
        if first <= idx < end:
            in_window.append(t_ns)
    dev = sorted(deviations_ms(in_window))
    return {
        "expected": plan.window_frames,
        "received": len(in_window),
        "lost": plan.window_frames - len(in_window),
        "duplicate": dup,
        "reordered": reorder,
        "invalid": int(invalid),
        "samples": len(dev),
        "p50_ms": _r(nearest_rank(dev, 50)),
        "p99_ms": _r(nearest_rank(dev, 99)),
        "max_ms": _r(dev[-1] if dev else None),
    }


def _r(value, digits=3):
    return None if value is None else round(value, digits)


def worst(listeners, key):
    """Largest value of `key` over listener rows (None when no row has it): the WORST listener."""
    vals = [row[key] for row in listeners if row.get(key) is not None]
    return max(vals) if vals else None


def aggregate(listeners):
    """Totals and worst-listener numbers over a list of analyse_listener rows."""
    return {
        "listeners": len(listeners),
        "expected": sum(r["expected"] for r in listeners),
        "received": sum(r["received"] for r in listeners),
        "lost": sum(r["lost"] for r in listeners),
        "duplicate": sum(r["duplicate"] for r in listeners),
        "reordered": sum(r["reordered"] for r in listeners),
        "invalid": sum(r["invalid"] for r in listeners),
        "worst_p50_ms": worst(listeners, "p50_ms"),
        "worst_p99_ms": worst(listeners, "p99_ms"),
        "worst_max_ms": worst(listeners, "max_ms"),
        "min_samples": min((r["samples"] for r in listeners), default=0),
    }


# ------------------------------------------------------------- the source side

def source_lateness(scheduled, actual, interval_ms=FRAME_MS):
    """Lateness of a paced sender in ms (actual - scheduled, same clock, seconds in) and the number of
    missed slots (a frame sent a whole interval or more late)."""
    late = [(a - s) * 1000.0 for s, a in zip(scheduled, actual)]
    ordered = sorted(late)
    return {
        "frames": len(late),
        "missed": sum(1 for v in late if v >= interval_ms),
        "p50_ms": _r(nearest_rank(ordered, 50)),
        "p99_ms": _r(nearest_rank(ordered, 99)),
        "max_ms": _r(ordered[-1] if ordered else None),
        "min_ms": _r(ordered[0] if ordered else None),
    }


# ---------------------------------------------------------------- the gate

@dataclass
class Check:
    name: str
    measured: object
    limit: object
    ok: bool
    note: str = ""


def growth_limit_kib(listeners):
    return GATE["growth_kib_per_listener"] * listeners + GATE["growth_fixed_mib"] * 1024.0


def evaluate_gates(listeners, edge, listener_totals, source):
    """The adopted gate for `listeners` (a count). `edge`: cpu_pct (of ONE core), vmhwm_kib, vmhwm_pre_kib;
    `listener_totals`: aggregate();
    `source`: dict of source_lateness() per room (summed missed, worst max). Returns [Check]."""
    out = []
    cpu = edge.get("cpu_pct")
    out.append(Check("edge cpu, percent of one core", cpu, GATE["cpu_pct_of_one_core"],
                     cpu is not None and cpu <= GATE["cpu_pct_of_one_core"]))
    hwm = edge.get("vmhwm_kib")
    rss_mib = None if hwm is None else hwm / 1024.0
    out.append(Check("edge peak RSS (VmHWM), MiB", _r(rss_mib, 2), GATE["peak_rss_mib"],
                     rss_mib is not None and rss_mib <= GATE["peak_rss_mib"]))
    pre = edge.get("vmhwm_pre_kib")
    growth = None if hwm is None or pre is None else hwm - pre
    limit = growth_limit_kib(listeners)
    out.append(Check("edge RSS growth over pre-client startup, KiB", growth, round(limit, 1),
                     growth is not None and growth <= limit,
                     "%.1f KiB per listener" % (growth / listeners) if growth is not None else ""))
    p99 = listener_totals.get("worst_p99_ms")
    out.append(Check("worst-listener jitter p99, ms", p99, GATE["jitter_p99_ms"],
                     p99 is not None and p99 <= GATE["jitter_p99_ms"]))
    mx = listener_totals.get("worst_max_ms")
    out.append(Check("worst-listener jitter max, ms", mx, GATE["jitter_max_ms"],
                     mx is not None and mx <= GATE["jitter_max_ms"]))
    for key, label in (("lost", "lost packets"), ("duplicate", "duplicate packets"),
                       ("reordered", "reordered packets"), ("invalid", "invalid packets")):
        out.append(Check(label, listener_totals.get(key), 0, listener_totals.get(key) == 0))
    missed = source.get("missed")
    out.append(Check("source missed deadlines", missed, 0, missed == 0,
                     "worst lateness %s ms" % source.get("max_ms")))
    return out


def all_ok(checks):
    return all(c.ok for c in checks)


# ------------------------------------------------------------------- pacing

def paced_offsets(n, rate, burst, reserve=1, margin=1.05):
    """Seconds after the start at which the k-th request may go out under a token bucket (`rate` per second,
    `burst` tokens, `reserve` of them left unused, `margin` stretching the gaps): the first burst-reserve go at
    once, then one every margin/rate."""
    free = max(1, burst - reserve)
    gap = margin / rate
    return [0.0 if k < free else (k - free + 1) * gap for k in range(n)]


def accounts_needed(listeners):
    """Accounts for the listeners: 8 circuits per login, 3 sessions per account."""
    logins = -(-listeners // CIRCUITS_PER_LOGIN)
    return -(-logins // LOGINS_PER_ACCOUNT)


@dataclass(frozen=True)
class Slot:
    index: int
    login: int
    account: int
    room: str


def room_of(index, listeners):
    """Rooms A and B: the first half of the listeners listen to A, the rest to B (at most 100 per room, 200 in
    all as in DV-Router's harness); a single listener or a small run still uses both when it can."""
    if listeners > 2 * LISTENERS_PER_ROOM:
        raise ValueError("at most %d listeners (2 rooms x %d)" % (2 * LISTENERS_PER_ROOM, LISTENERS_PER_ROOM))
    half = (listeners + 1) // 2
    return "A" if index < half else "B"


def slots(listeners):
    """One Slot per listener: login = index // 8, account = login // 3, room by room_of."""
    out = []
    for j in range(listeners):
        login = j // CIRCUITS_PER_LOGIN
        out.append(Slot(j, login, login // LOGINS_PER_ACCOUNT, room_of(j, listeners)))
    return out


def worker_of_login(login, workers):
    return login % workers


class Bucket:
    """Reservation token bucket (rate per second, burst, `reserve` tokens kept back): take() returns the seconds
    to wait before the request may go out, so that callers can sleep without a lock. The web service keeps
    such a bucket per HTTP session for unsafe requests (2/s, burst 10: `Pacer.BUDGETS["action"]`); every offer
    and every /rtc/alive of a login is one of them, and a login may own eight circuits."""

    def __init__(self, rate, burst, reserve=1, clock=time.monotonic):
        self.rate = float(rate)
        self.cap = float(max(1, burst - reserve))
        self.tokens = self.cap
        self.t = None
        self.clock = clock

    def take(self):
        now = self.clock()
        if self.t is not None:
            self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now
        self.tokens -= 1.0
        return 0.0 if self.tokens >= 0 else -self.tokens / self.rate


def alive_schedule(first_seconds=6, step_early=1.0, step_late=10.0):
    """Offsets of the lease renewals after a connect, as the page makes them (web.md "Signalling handoff"):
    at once, every second until six successful requests, then every 10 s. Yields offsets forever."""
    t = 0.0
    k = 0
    while True:
        yield t
        k += 1
        t += step_early if k < first_seconds else step_late


# ------------------------------------------------- host samples (control channel)

@dataclass
class ProcSample:
    pid: int
    comm: str
    t: float                    # epoch seconds, taken inside the helper right after reading /proc
    ticks: int                  # utime + stime
    clk_tck: int
    vmhwm_kib: int
    vmrss_kib: int
    threads: int = 0
    fds: int = -1               # open file descriptors (/proc/<pid>/fd); -1 when the helper could not read them
    starttime: int = 0          # field 22 of /proc/<pid>/stat: start in clock ticks after boot (with pid: the process identity)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def cpu_s(self):
        return self.ticks / float(self.clk_tck)


def parse_procstat_out(text):
    """ProcSample of a `proc-stat` reply (lines key=value); ValueError when a key is missing."""
    kv = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            kv[key] = value
    try:
        return ProcSample(pid=int(kv["pid"]), comm=kv["comm"], t=float(kv["t"]),
                          ticks=int(kv["utime"]) + int(kv["stime"]), clk_tck=int(kv["clk_tck"]),
                          vmhwm_kib=int(kv["vmhwm_kb"]), vmrss_kib=int(kv["vmrss_kb"]),
                          threads=int(kv.get("threads", 0)), fds=int(kv.get("fds", -1)),
                          starttime=int(kv.get("starttime", 0)), raw=kv)
    except KeyError as exc:
        raise ValueError("proc-stat reply lacks %s: %r" % (exc, text[:200])) from exc
    except ValueError as exc:
        raise ValueError("proc-stat reply is malformed: %r" % text[:200]) from exc


def cpu_percent(a, b):
    """CPU of a process between two ProcSamples in percent of ONE core (100 = one core)."""
    dt = b.t - a.t
    if dt <= 0:
        raise ValueError("samples are not in order")
    return 100.0 * (b.cpu_s - a.cpu_s) / dt
