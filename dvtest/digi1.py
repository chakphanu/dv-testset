# SPDX-License-Identifier: MIT
"""Pure helpers of the DIGI1 rig cases (cases/test_digi1.py, docs/digi1-cases.md): the fill-in digipeater of the AX.25 edge
(DV-Router 22994519 + 588ffb4c, code a8692074), judged black box. No network, no pytest, stdlib only.

What is modelled is what DV-Router STATES (docs/design/data.md 8.3 "AX.25 fill-in digipeater (DIGI1)", 10.8, config.md `digi`), not what it
does: the cases compare the edge's bytes with this model and, where the committed vectors exist, with Dire Wolf
(vectors/direwolf/digi_oracle.json, dvtest/digi1_oracle.py).

  GOLDENS, golden_frame, golden_expect   the path vectors G1 to G13 of data.md 8.3 (our names N0TEST-n instead of N0CALL-n)
  extra_vectors                          more path vectors (RELAY, WIDE2-2, TRACE, QAR, I, ...), all from the same rules
  decide                                 the ten step decision order of 8.3 (first match wins) on a frame
  DedupeTable                            the duplicate table: key, window from first acceptance, 256 live entries, fail closed
  TIMELINE_S, timeline_expect            DIGI-R2: 0 / 10 / 29 / 31 / 45 s against a 30 s window
  Expected                               the expected dv_edge_digi_total of everything a case injected
  flood_plan, flood_problems             DIGI-R3: 60 eligible frames in a minute from 10 sources, the bounds of 10.8
  digi_counts, rf_tx_digi, conservation_problems, restart_counts_match   the counters and their conservation (10.8)
  read_shape, class_capacity_s           the digi row of the RENDERED config and what airtime the digi class has
"""
from collections import namedtuple

from dvtest import airtime, ax25

OURS = "N0TEST-10"                  # [edges.ax25] callsign of rig/config/dv-router/digi1*.toml (the docs say N0CALL-10)
RESULTS = ("repeat", "duplicate", "own_source", "own_path", "internet_path", "bad_path", "no_match", "dedupe_full", "fenced")
# decision order of data.md 8.3, as the counter documentation lists it: no_match, bad_path, internet_path, own_source, own_path, duplicate,
# dedupe_full, fenced, repeat. RESULTS above is the SET (alphabetical order is not meaningful).
FILL_IN_HOP = "WIDE1-1"
WINDOW_S = 30.0                     # digi.dedupe_secs default
DEDUPE_CAPACITY = 256
TTL_S = 5.0                         # digi.ttl_secs default
TEXT_ROOM = "XLX999B"
INTERNET_BASES = ("TCPIP", "TCPXX", "I")


# ---------------------------------------------------------------- the path vectors

Golden = namedtuple("Golden", "id what src dest hops info result replaced dw reserved cr_src ours")
# result: one of RESULTS (what the edge decides); replaced: the index of the hop that becomes `ours` (None unless result == "repeat");
# dw: "equal" (Dire Wolf with DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$ gives the same: the same frame or silence) or "differs:<why>" (a documented
# deviation of data.md 8.3 / design-271 5.7 / lead decision D10: recorded, never asserted); reserved: the reserved bits of the received via addresses;
# ours: the station callsign the vector is written for (G2b needs a station without SSID: not reachable in the rig, a GAP)

def _g(gid, what, hops, result, replaced=None, src="N0TEST-9", dw="equal", reserved=0b11, ours=OURS, info=b">test", dest="APRS"):
    return Golden(gid, what, src, dest, tuple(hops), info, result, replaced, dw, reserved, False, ours)


GOLDENS = (
    _g("G1", "WIDE1-1 then WIDE2-1: our call replaces WIDE1-1, WIDE2-1 stays", ["WIDE1-1", "WIDE2-1"], "repeat", 0),
    _g("G2", "WIDE1-1 alone: replaced, the address extension bit stays", ["WIDE1-1"], "repeat", 0),
    _g("G3", "seven used hops then WIDE1-1: the eighth hop is replaced, still eight hops",
       ["N0TEST-1*", "N0TEST-2*", "N0TEST-3*", "N0TEST-4*", "N0TEST-5*", "N0TEST-6*", "N0TEST-7*", "WIDE1-1"], "repeat", 7),
    _g("G4", "WIDE2-1 first, WIDE1-1 second: the first unused hop is not WIDE1-1", ["WIDE2-1", "WIDE1-1"], "no_match"),
    _g("G5", "WIDE1 without SSID, WIDE2-1", ["WIDE1", "WIDE2-1"], "no_match"),
    _g("G6", "our call already used, WIDE1-1 next", [OURS + "*", "WIDE1-1"], "own_path"),
    _g("G7", "explicit routing: our call is the next unused hop", [OURS, "WIDE2-1"], "repeat", 0),
    _g("G8", "source N0TEST-7 (not in the own set), WIDE1-1 then WIDE2-2", ["WIDE1-1", "WIDE2-2"], "repeat", 0, src="N0TEST-7"),
    _g("G9", "the source is our own call", ["WIDE1-1"], "own_source", src=OURS, dw="differs:own source via WIDE1-1 is refused by lead decision D10, stricter than Dire Wolf (its answer to G9 as written is information only)"),
    _g("G10", "WIDE1-1 not used, the next hop used (non-contiguous H bits)", ["WIDE1-1", "N0TEST-1*"], "bad_path",
       dw="differs:Dire Wolf repeats, DV-Router refuses a non-contiguous path"),
    _g("G11", "WIDE1-1 then TCPIP (an Internet path)", ["WIDE1-1", "TCPIP"], "internet_path",
       dw="differs:Dire Wolf repeats, DV-Router refuses an Internet marker"),
    _g("G12", "G1 with the reserved bits of the received via addresses clear: only our hop gets them set", ["WIDE1-1", "WIDE2-1"],
       "repeat", 0, reserved=0b00),
    _g("G13", "WIDE1-2: the SSID must be exactly 1", ["WIDE1-2"], "no_match"),
)
G2B = _g("G2b", "G2 with a station call without SSID (N0TEST): SSID zero in the repeat", ["WIDE1-1"], "repeat", 0, ours="N0TEST")
# G2b cannot run in the rig (the station call of a run is fixed): the model and the unit tests cover it, the doc says so.


def extra_vectors():
    """More path vectors (not in data.md 8.3's table), each decided by one rule of its list: (id, what, hops, result, replaced). The ids
    are X-prefixed; none is a Dire Wolf comparison (the oracle job runs them too and the vectors file records them)."""
    return (
        _g("X1", "RELAY is not accepted", ["RELAY"], "no_match"),
        _g("X2", "WIDE2-2 alone", ["WIDE2-2"], "no_match"),
        _g("X3", "TRACE1-1 alone", ["TRACE1-1"], "no_match"),
        _g("X4", "no hops at all (direct)", [], "no_match"),
        _g("X5", "every hop already used", ["WIDE1-1*", "WIDE2-1*"], "no_match"),
        _g("X6", "a QAR q-construct hop (starts with QA)", ["WIDE1-1", "QAR"], "internet_path", dw="differs:Dire Wolf may repeat, DV-Router refuses an Internet marker"),
        _g("X7", "the Internet marker I", ["WIDE1-1", "I"], "internet_path", dw="differs:Dire Wolf may repeat, DV-Router refuses an Internet marker"),
        _g("X8", "TCPXX used earlier in the path, WIDE1-1 next", ["TCPXX*", "WIDE1-1"], "internet_path", dw="differs:Dire Wolf may repeat"),
        _g("X9", "a third party relay (not ours) used, then WIDE1-1", ["N0TEST-3*", "WIDE1-1"], "repeat", 1),
        _g("X10", "an unused hop of another station before WIDE1-1", ["N0TEST-3", "WIDE1-1"], "no_match"),
        _g("X11", "destination SSID does not matter (APRS-1)", ["WIDE1-1"], "repeat", 0, dest="APRS-1"),
    )


def golden_frame(g, suffix=b""):
    """The AX.25 frame bytes of a vector (no FCS): the H bit of every via as written (a `*` marks exactly that hop), the reserved bits
    of `g.reserved`, the source command/response bit as in the hand-built bytes of DV-Router (clear) unless `g.cr_src`. `suffix` is
    appended to the information field: in a rig run every vector needs its own duplicate key (G1 and G2 differ only in the path, which the
    key ignores), the unit tests use the bare `>test` of the hand-built bytes."""
    hops = []
    for text in g.hops:
        used = text.endswith("*")
        hops.append((text[:-1] if used else text, used, g.reserved))
    return ax25.build_ui(g.dest, g.src, hops, bytes(g.info) + bytes(suffix), cr_dest=True, cr_src=g.cr_src)


def golden_expect(g, frame=None):
    """The repeated frame bytes of a vector whose result is "repeat" (the one hop replaced by `g.ours`), else None."""
    if g.result != "repeat":
        return None
    return ax25.replace_hop(golden_frame(g) if frame is None else frame, g.replaced, g.ours)


# ---------------------------------------------------------------- the decision (data.md 8.3, first match wins)

def _is_internet(call):
    base = call.split("-")[0]
    return base in INTERNET_BASES or base.startswith("QA")


class DedupeTable:
    """The duplicate table of one physical transmitter and mode. A key is live while now - accepted < window (exactly at the boundary the
    key is new); duplicate observations do not refresh it; 256 live entries, then `full` without evicting; a key is remembered only
    when `remember` is called (at a repeat). Times are seconds on any clock."""

    def __init__(self, window_s=WINDOW_S, capacity=DEDUPE_CAPACITY):
        self.window_s, self.capacity = float(window_s), capacity
        self.entries = {}

    def _live(self, now):
        self.entries = {k: t for k, t in self.entries.items() if now - t < self.window_s}

    def seen(self, key, now):
        """"duplicate", "full" or "new" for a key at `now` (expired entries are dropped first)."""
        self._live(now)
        if key in self.entries:
            return "duplicate"
        if len(self.entries) >= self.capacity:
            return "full"
        return "new"

    def remember(self, key, now):
        self._live(now)
        self.entries.setdefault(key, now)


def dedupe_key(frame):
    """Source plus destination (both with SSID) plus the information bytes without trailing CR, LF and space; path and H bits ignored."""
    f = ax25.parse_frame(frame)
    return (f["src"]["text"], f["dest"]["text"], bytes(f["info"]).rstrip(b"\r\n "))


def decide(frame, ours=OURS, table=None, now=0.0, fenced=False, own_sources=()):
    """(result, repeated frame or None) for one received frame, in the order of data.md 8.3: no_match (no unused via), bad_path (an
    unused via before a used one), internet_path, own_source, own_path, no_match (the first unused via is not WIDE1-1 or our call),
    duplicate, dedupe_full, fenced (not remembered), repeat. `table` (DedupeTable) is consulted and, at a repeat, written; None means
    an empty table of the default window that is not kept."""
    table = DedupeTable() if table is None else table
    f = ax25.parse_frame(frame)
    vias = f["digis"]
    unused = next((i for i, d in enumerate(vias) if not d["bit7"]), None)
    if unused is None:
        return "no_match", None
    if any(d["bit7"] for d in vias[unused:]):
        return "bad_path", None
    if any(_is_internet(d["call"]) for d in vias):
        return "internet_path", None
    own = {ours, *own_sources}
    if f["src"]["text"] in own:
        return "own_source", None
    if any(d["bit7"] and d["text"] == ours for d in vias):
        return "own_path", None
    hop = vias[unused]["text"]
    if hop != FILL_IN_HOP and hop != ours:
        return "no_match", None
    key = dedupe_key(frame)
    state = table.seen(key, now)
    if state == "duplicate":
        return "duplicate", None
    if state == "full":
        return "dedupe_full", None
    if fenced:
        return "fenced", None
    table.remember(key, now)
    return "repeat", ax25.replace_hop(frame, unused, ours)


def repeat_problems(sent, got):
    """Problems ([] = fine) of a frame the edge wrote against the frame that was heard: the same length (the path never grows), at most
    the 7 bytes of one hop differ, the information field, source and destination are untouched and exactly one hop has changed to
    a used one."""
    problems = []
    if len(sent) != len(got):
        return ["the repeat has %d bytes, the heard frame %d: the path must not grow or shrink" % (len(got), len(sent))]
    diff = ax25.diff_offsets(sent, got)
    if not diff:
        return ["the repeat is byte-identical to the heard frame (no hop was replaced)"]
    if diff[0] < 14 or diff[-1] >= 14 + 7 * 8 or diff[-1] - diff[0] >= 7:
        problems.append("bytes outside one via address differ (offsets %s)" % diff)
    return problems


# ---------------------------------------------------------------- DIGI-R2: the duplicate timeline

TIMELINE_S = (0.0, 10.0, 29.0, 31.0, 45.0)


def timeline_expect(times=TIMELINE_S, window_s=WINDOW_S):
    """The decisions of one frame (one key) sent again at each of `times` seconds against a duplicate window: repeat, then duplicates
    until the window since the FIRST acceptance has passed, then a repeat again (the window restarts), then duplicates."""
    table = DedupeTable(window_s)
    out = []
    for t in times:
        key = ("S", "D", b"x")
        state = table.seen(key, t)
        if state == "new":
            table.remember(key, t)
            out.append("repeat")
        else:
            out.append("duplicate")
    return out


def key_vectors(src="N0TEST-9", tag=b""):
    """DIGI-R2 key cases, in the order they are sent a second apart in one window: (id, what, frame, expect). The first frame is the
    reference (a repeat); every other row is decided by the key rules: the path and H bits are ignored (a duplicate), trailing CR / LF /
    space of the information field are ignored (a duplicate), but the source SSID, the destination SSID and any information byte are part
    of the key (a repeat). Every frame has the fill-in hop, so only the key decides. `tag` (bytes) is appended to every information field
    before any trailing characters: one tag per run keeps the keys of a run apart from those of an earlier run of the same edge."""
    def f(info, source=src, dest="APRS", hops=("WIDE1-1",)):
        return ax25.build_ui(dest, source, list(hops), info, cr_src=False)
    base = b">key" + tag
    return (
        ("K0", "the reference frame", f(base), "repeat"),
        ("K1", "the same frame with another path (WIDE1-1,WIDE2-1): path ignored", f(base, hops=("WIDE1-1", "WIDE2-1")), "duplicate"),
        ("K2", "trailing space, CR and LF on the information field", f(base + b" \r\n"), "duplicate"),
        ("K3", "another source SSID", f(base, source="N0TEST-8"), "repeat"),
        ("K4", "another destination SSID", f(base, dest="APRS-2"), "repeat"),
        ("K5", "another information byte", f(b">kez" + tag), "repeat"),
        ("K6", "a leading space is part of the key", f(b"> key" + tag), "repeat"),
        ("K7", "the reference frame again, byte for byte (still inside the window)", f(base), "duplicate"),
    )


# ---------------------------------------------------------------- the counters

class Expected:
    """The dv_edge_digi_total{result} a case should have caused: add(result) for every frame injected on the digipeating port (a frame that
    does not parse as AX.25 is not counted by the edge: do not add it)."""

    def __init__(self):
        self.counts = {r: 0 for r in RESULTS}
        self.repeats = []

    def add(self, result, repeated=None):
        if result not in self.counts:
            raise ValueError("not a digi result: %r" % (result,))
        self.counts[result] += 1
        if repeated is not None:
            self.repeats.append(repeated)

    def total(self):
        return sum(self.counts.values())

    def nonzero(self):
        return {r: n for r, n in self.counts.items() if n}


def digi_counts(samples, port):
    """{result: n} of dv_edge_digi_total{port=port} (every fixed result present, 0 when no series)."""
    out = {r: 0 for r in RESULTS}
    for s in samples:
        if s.name == "dv_edge_digi_total" and s.labels.get("port") == port:
            r = s.labels.get("result", "")
            out[r] = out.get(r, 0) + int(s.value)
    return out


def rf_tx_digi(samples, port):
    """{result: n} of dv_edge_rf_tx_total{port=port,kind=digi} (only the series that exist)."""
    out = {}
    for s in samples:
        if s.name == "dv_edge_rf_tx_total" and s.labels.get("port") == port and s.labels.get("kind") == "digi":
            r = s.labels.get("result", "")
            out[r] = out.get(r, 0) + int(s.value)
    return out


def rf_tx_kinds(samples, port):
    """Sorted set of the `kind` labels of dv_edge_rf_tx_total{port=port}."""
    return sorted({s.labels.get("kind", "") for s in samples if s.name == "dv_edge_rf_tx_total" and s.labels.get("port") == port})


def rf_tx_kind_result(samples, port, kind, result):
    return sum(int(s.value) for s in samples if s.name == "dv_edge_rf_tx_total" and s.labels.get("port") == port
               and s.labels.get("kind") == kind and s.labels.get("result") == result)


def rejected_total(samples):
    """Sum of dv_edge_rf_rejected_total over its fixed `result` labels (no port label): refusals before the scheduler."""
    return sum(int(s.value) for s in samples if s.name == "dv_edge_rf_rejected_total")


def rf_fenced_total(samples):
    return sum(int(s.value) for s in samples if s.name == "dv_edge_drops_total" and s.labels.get("reason") == "rf_fenced")


def queue_len(samples, port):
    hits = [s for s in samples if s.name == "dv_edge_rf_queue" and s.labels.get("port") == port]
    return int(sum(s.value for s in hits)) if hits else None


TERMINAL_TX = ("sent", "simulated", "sent_over_budget", "dropped_ttl", "dropped_queue", "dropped_exceeds_quota", "dropped_too_long")


def delta(now, before):
    """Per-key difference of two count maps (a key missing in `before` counts from 0)."""
    return {k: now.get(k, 0) - before.get(k, 0) for k in set(now) | set(before)}


def restart_counts_match(before, after, expected):
    """How the {result: n} counters of the edge after a restart relate to what the case made them: "reset" when `after` itself equals
    `expected` (the new process started at 0 and the hub shows it), "continued" when `after - before` does (the hub kept the series of the
    old process and the new one adds to it), else None. Both are the same observation for a run that has not counted anything before."""
    for mode, got in (("reset", dict(after)), ("continued", delta(after, before))):
        if all(got.get(r, 0) == expected.get(r, 0) for r in RESULTS):
            return mode
    return None


def conservation_problems(digi, tx, rejected=0, queued=0, rf_fenced=None, rejected_exact=True):
    """Problems of data.md 10.8 "for each digipeating port": digi{repeat} = pre-scheduler digi refusals + the terminal rf_tx{kind=digi}
    results (sent, simulated, sent_over_budget, dropped_*; never delayed) + digi copies canceled by a fence + digi copies still queued.
    `digi` and `tx` are DELTA maps ({result: n}); `queued` is the digi part of the queue gauge (the gauge counts every class: pass the whole
    gauge when the port has no other traffic). `rf_fenced` is the delta of dv_edge_drops_total{reason=rf_fenced} (None: not read): the canceled
    copies are at most what rf_fenced counts beyond digi{fenced}. Also digi{fenced} <= rf_fenced.
    `rejected` is the delta of dv_edge_rf_rejected_total, which has NO port label and counts the refusals of ALL classes of the edge
    (answer-279 item 1; the missing label is the product item DIGI1b). With `rejected_exact` (the default; right when `rejected` is 0, i.e.
    nothing else was refused, an absent series being 0) it is a term of the equation. With rejected_exact False it is only an UPPER BOUND on the
    digi share of it: 0 <= unaccounted <= rejected + the fence slack."""
    problems = []
    repeat = digi.get("repeat", 0)
    terminal = sum(n for r, n in tx.items() if r in TERMINAL_TX)
    slack = 0 if rf_fenced is None else max(0, rf_fenced - digi.get("fenced", 0))
    if rejected_exact:
        unaccounted = repeat - rejected - terminal - queued
        counted = rejected
    else:
        unaccounted = repeat - terminal - queued
        counted = 0
        slack += rejected
    if unaccounted < 0 or unaccounted > slack:
        problems.append("digi{repeat} = %d but rejected %d%s + terminal rf_tx{kind=digi} %d (%s) + still queued %d = %d: %d unaccounted (a fence can cancel at most %d)" % (
            repeat, rejected, "" if rejected_exact else " (all classes: only a bound)", terminal,
            ", ".join("%s %d" % kv for kv in sorted(tx.items())) or "none", queued, counted + terminal + queued, unaccounted, slack))
    if rf_fenced is not None and digi.get("fenced", 0) > rf_fenced:
        problems.append("digi{fenced} = %d but dv_edge_drops_total{reason=rf_fenced} grew by only %d: every fenced candidate is also a fence refusal" % (
            digi.get("fenced", 0), rf_fenced))
    return problems


def count_problems(expected, got):
    """Problems of dv_edge_digi_total deltas `got` against the Expected counts: every result must match exactly."""
    problems = []
    for r in RESULTS:
        want = expected.counts.get(r, 0)
        have = got.get(r, 0)
        if want != have:
            problems.append("dv_edge_digi_total{result=%s} grew by %d, expected %d" % (r, have, want))
    extra = {r: n for r, n in got.items() if r not in RESULTS and n}
    if extra:
        problems.append("dv_edge_digi_total has result label(s) outside the documented nine: %s" % extra)
    return problems


# ---------------------------------------------------------------- DIGI-R3: the flood

FLOOD_SOURCES = 10
FLOOD_PER_MIN = 60
FLOOD_S = 60.0


def flood_sources(count=FLOOD_SOURCES):
    """Ten distinct AX.25 sources (call plus SSID): N0TEST-1 ... N0TEST-9 and N0TEST (SSID 0). None is the station call (N0TEST-10)."""
    names = ["N0TEST-%d" % n for n in range(1, count)]
    names.append("N0TEST")
    return names[:count]


def flood_plan(nonce, sources=None, per_min=FLOOD_PER_MIN, seconds=FLOOD_S):
    """[(offset seconds, source, info bytes, frame)] of `per_min * seconds / 60` eligible frames (path WIDE1-1, a distinct information field
    each: no duplicate), spread evenly over `seconds`, the sources taking turns. Every frame is a repeat for the model."""
    sources = flood_sources() if sources is None else list(sources)
    total = int(round(per_min * seconds / 60.0))
    out = []
    for i in range(total):
        src = sources[i % len(sources)]
        info = (">R3 %s s%d n%d" % (nonce, i % len(sources), i)).encode("ascii")
        out.append((i * seconds / total, src, info, ax25.build_ui("APDVT1", src, ["WIDE1-1"], info, cr_src=False)))
    return out


# the afsk1200-shared class row of data.md 10.8 (the shape the digi1 snippets use)
AFSK_DEFAULTS = {"duty_pct": 5, "window_secs": 600, "max_ptt_ms": 2000, "min_gap_ms": 1000, "per_source_pct": 25, "queue": 8,
                 "class_shares": (2, 4, 1, 1), "digi_share": 4, "digi_ttl_s": 5}      # shares: ack, message, position, beacon (the four-entry array)
KISS_TXDELAY_MS = 300           # rendered by the edge as TXDELAY 0x1E (30 x 10 ms), docs/direwolf-cases.md; the scheduler charges the configured value
KISS_TXTAIL_MS = 30


def frame_airtime_ns(length, txdelay_ms=KISS_TXDELAY_MS, txtail_ms=KISS_TXTAIL_MS):
    """The airtime the scheduler charges for an AX.25 frame of `length` bytes at 1200 baud: the DV-Router bound (data.md 10.8 table:
    txdelay + (len + 4) * 8 * 1.2 / 1200 + txtail), dvtest.airtime.afsk_airtime_ns."""
    return airtime.afsk_airtime_ns(length, 1200, txdelay_ms * 1_000_000, txtail_ms * 1_000_000)


def class_capacity_s(budget=None, digi_share=None):
    """Airtime (seconds) of the digi class bucket: duty_pct / 100 * window_secs * digi share / sum of all resolved shares (data.md 10.8
    rule 1 and scheduler.rs class_capacity). `budget` overrides the afsk1200 defaults (keys of tx_budget)."""
    b = dict(AFSK_DEFAULTS)
    b.update({k: v for k, v in (budget or {}).items() if k in b})
    share = b["digi_share"] if digi_share is None else digi_share
    shares = list(b["class_shares"])
    total_shares = sum(shares) + share
    return b["duty_pct"] / 100.0 * b["window_secs"] * share / total_shares


def port_capacity_s(budget=None):
    b = dict(AFSK_DEFAULTS)
    b.update({k: v for k, v in (budget or {}).items() if k in b})
    return b["duty_pct"] / 100.0 * b["window_secs"]


def flood_problems(plan, written, budget=None, ttl_s=TTL_S, slack_s=1.0):
    """Problems ([] = fine) of what a digipeating port wrote during a flood. `plan` is flood_plan() with the injection times replaced by
    absolute monotonic times: [(t, source, info, frame)]; `written` is [(t, frame)] the edge wrote to the TNC (the repeats). Checks, each
    from data.md 8.3 / 10.8:
      * every written frame is the repeat of exactly one injected frame, the hop replaced by our call, nothing else changed, no frame
        twice (the duplicate table);
      * TTL: no copy left more than the digi TTL (plus `slack_s`) after its frame was injected ("an expired frame is dropped at dispatch,
        never transmitted late");
      * minimum gap: two writes at least min_gap_ms apart (less 0.15 s of timing noise);
      * airtime: the sum of the charged airtime of all copies is within the digi class bucket (it starts full and refills with the run
        time) plus one frame, and the airtime of one source within its quota (per_source_pct of the port capacity) plus one frame;
      * at most `digi share` copies are queued at any moment: not checkable from the outside, see docs."""
    b = dict(AFSK_DEFAULTS)
    b.update({k: v for k, v in (budget or {}).items() if k in b})
    problems = []
    by_key = {}
    for t, src, info, frame in plan:
        by_key[dedupe_key(frame)] = (t, src, frame)
    used = set()
    airtime_by_source = {}
    total_ns = 0
    longest_ns = 0
    last_t = None
    first_t = min((t for t, *_ in plan), default=0.0)
    end_t = max((t for t, _f in written), default=first_t)
    for t, frame in sorted(written, key=lambda x: x[0]):
        try:
            key = dedupe_key(frame)
        except ValueError:
            problems.append("a written frame does not parse as AX.25: %s" % frame.hex())
            continue
        if key not in by_key:
            problems.append("a written frame matches no injected frame: %s" % ax25.to_tnc2(frame))
            continue
        if key in used:
            problems.append("the same injected frame was repeated twice: %s" % ax25.to_tnc2(frame))
        used.add(key)
        t_in, src, sent = by_key[key]
        want = ax25.replace_hop(sent, ax25.first_unused_hop(sent), OURS)
        if frame != want:
            problems.append("the repeat of %s is %s, expected %s" % (src, ax25.path_text(frame), ax25.path_text(want)))
        late = t - t_in
        if late > ttl_s + slack_s:
            problems.append("a copy of %s left %.1f s after its frame was heard, the digi TTL is %.0f s (an expired copy is dropped, never sent late)" % (
                src, late, ttl_s))
        if last_t is not None and t - last_t < b["min_gap_ms"] / 1000.0 - 0.15:
            problems.append("two copies %.2f s apart, the minimum gap is %d ms" % (t - last_t, b["min_gap_ms"]))
        last_t = t
        ns = frame_airtime_ns(len(frame))
        total_ns += ns
        longest_ns = max(longest_ns, ns)
        airtime_by_source[src] = airtime_by_source.get(src, 0) + ns
    run_s = max(0.0, end_t - first_t)
    cap = class_capacity_s(budget)
    refill = cap / b["window_secs"]
    class_bound = (cap + refill * run_s) * 1e9 + longest_ns
    if total_ns > class_bound:
        problems.append("the copies charge %.2f s of airtime, the digi class holds %.2f s (+ %.2f s refill in %.0f s, + one frame)" % (
            total_ns / 1e9, cap, refill * run_s, run_s))
    port_cap = port_capacity_s(budget)
    port_bound = (port_cap + b["duty_pct"] / 100.0 * run_s) * 1e9 + longest_ns
    if total_ns > port_bound:
        problems.append("the copies charge %.2f s of airtime, the whole port holds %.2f s" % (total_ns / 1e9, port_cap))
    source_cap = port_cap * b["per_source_pct"] / 100.0
    for src, ns in sorted(airtime_by_source.items()):
        bound = (source_cap + source_cap / b["window_secs"] * run_s) * 1e9 + longest_ns
        if ns > bound:
            problems.append("source %s was given %.2f s of airtime, its quota is %.2f s (%d%% of the port)" % (src, ns / 1e9, source_cap, b["per_source_pct"]))
    return problems


# ---------------------------------------------------------------- the rendered config

Shape = namedtuple("Shape", "name port row digi budget tx_per_min tx_burst class_capacity_s problems")


def read_shape(doc, port="rf-a"):
    """The digi shape of the rendered dv-router.toml (a dict) for AX.25 port `port`: name "floor" (tx_budget {} and 10 / 3: the rate floor
    and class defaults), "roomy" (tx_per_min >= 30 and a class bucket of at least 25 s: the functional cases), "other", or "none" (no
    enabled digi on that port). `problems` lists what a case needs and the config lacks (effective TX)."""
    ax = ((doc or {}).get("edges") or {}).get("ax25")
    problems = []
    if not isinstance(ax, dict) or ax.get("enable") is not True:
        return Shape("none", port, None, None, None, 0, 0, 0.0, ["no enabled [edges.ax25]"])
    row = next((p for p in ax.get("port") or [] if p.get("name") == port), None)
    if row is None:
        return Shape("none", port, None, None, None, 0, 0, 0.0, ["no AX.25 port %s" % port])
    digi = row.get("digi") or {}
    if digi.get("enable") is not True:
        return Shape("none", port, row, digi, None, 0, 0, 0.0, ["port %s has no digi.enable" % port])
    if not ax.get("tx_enable") or not row.get("tx"):
        problems.append("port %s has no effective TX (tx_enable %r, tx %r)" % (port, ax.get("tx_enable"), row.get("tx")))
    budget = row.get("tx_budget") or {}
    per_min, burst = int(row.get("tx_per_min", 2)), int(row.get("tx_burst", 1))
    cap = 0.0
    if row.get("class", "afsk1200-shared") == "afsk1200-shared":
        cap = class_capacity_s(budget, digi.get("share"))
    name = "other"
    if not budget and per_min == 10 and burst == 3:
        name = "floor"
    elif per_min >= 30 and cap >= 25.0:
        name = "roomy"
    return Shape(name, port, row, digi, budget, per_min, burst, cap, problems)
