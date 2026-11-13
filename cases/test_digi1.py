# SPDX-License-Identifier: MIT
"""DV-Router DIGI1 cases (DV-Router 22994519 + 588ffb4c, code a8692074, image dv-router:wip-eeb44b4): the OFF-by-default fill-in digipeater of the AX.25
edge (`digi = { enable = true }` on a KISS port), judged through fake KISS TNCs with Dire Wolf as the independent oracle. Case table, run commands,
times, the oracle and the GAPs: docs/digi1-cases.md. Pure helpers: dvtest/digi1.py (model, flood bounds, counters), dvtest/digi1_oracle.py (Dire
Wolf vectors), dvtest/ax25.py (frame and H bit builders). Unit tests: cases/unit/test_digi1_lib.py, test_digi1_oracle.py, test_ax25_digi.py.

The tester plays the TNCs (dvtest/kiss_tnc.py, as cases/test_dp15_rf.py): rf-a (digipeating, TX) and rf-b (receive only, no digi) connect to it; a
frame the tester injects at rf-a is "heard on the air", what the edge writes back is the transmitted repeat. Document references are data.md 8.3
("AX.25 fill-in digipeater (DIGI1)", the ten steps and G1 to G13), 10.8 (scheduler, counters, conservation) and config.md (`digi` keys). Numbering is
the one DV-Router asked for (data.md 15.5): DIGI-R1 to R5.

  DIGI-R1   the path vectors G1 to G13 and eleven more (X1 to X11): the edge repeats exactly what the ten steps say, byte for byte (our call in the one
            hop, H bit set, reserved bits set, the extension bit and every other byte untouched, the path never grows) and is silent otherwise; a frame
            via WIDE1-1 comes back ONCE; Dire Wolf's vectors (vectors/direwolf/digi_oracle.json) agree where the documents say they do; the same
            port only (rf-b never repeats, rf-a's repeat never reaches rf-b)
  DIGI-R2   the duplicate timeline 0 / 10 / 29 / 31 / 45 s against the 30 s window, and the key rules (path ignored, trailing CR LF space ignored,
            source SSID / destination SSID / information are part of the key)
  DIGI-R3   (digi1.toml, the rate floor) 60 eligible frames in a minute from 10 sources: only the digi class budget, the source quotas, the TTL and the
            minimum gap limit the copies; every copy is counted; our own message frame in the queue does not expire by TTL; conservation
  DIGI-R4   two fill-ins hearing each other, both orders, with WIDE1-1,WIDE1-1: each consumes one hop, the path never grows, one key never twice in its
            window (the second fill-in is the tester; the real two-node run is a GAP)
  DIGI-R5   restart of dv-edge-ax25 through the admin API: nothing is repeated during the startup guard, a guarded eligible frame is NOT remembered,
            its first retry after the guard repeats
  counters  dv_edge_digi_total{port,result} equals what was injected, kind digi on dv_edge_rf_tx_total equals the copies seen, label sets, conservation
  DIGI-R1b  (cases/test_digi1_dw.py) the same repeat through a real Dire Wolf TNC and a real modulator and demodulator

Gate: DVT_DIGI1=on (rig/compose.yml passes it); every case skips with the reason otherwise. Shapes (the rendered config says which): digi1-roomy.toml
for R1, R2, R4 and the counters (a class budget big enough for 15 copies), digi1.toml for R3 (the rate floor and the class defaults), R5 and the
counters run on either. Observations: stdout and out/<run>/digi1.txt ("RECORD", "FINDING"). NOT RUN ON A RIG: written offline for the next image.
"""
import os
import sys
import time
import types
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import ax25, digi1, digi1_oracle, kiss_tnc, metrics, txguard  # noqa: E402

import data_support as ds  # noqa: E402
from data_support import write_note  # noqa: E402
# the fake TNCs rf-a / rf-b and the web side (their `hub` argument is the fixture of THIS module)
from test_dp15_rf import (  # noqa: E402,F401
    EDGE_TOCALL, ROOM_NAME, post, read_rendered_doc, rf, web_ctx)

pytestmark = pytest.mark.rig

ROOT = Path(__file__).resolve().parents[1]
LOG = "digi1.txt"
SETTING = "digi1"
PORT = "rf-a"
OTHER = "rf-b"
STATION = digi1.OURS
NONCE = "%04d" % (int(time.time()) % 10000)       # keeps the keys of this run apart from those of an earlier run of the same edge process
REPEAT_WAIT_S = 6.0                 # a repeat is queued at once: the scheduler release is at most the minimum gap (1 s) away
AFTER_REPEAT_S = 1.8                # after the first repeat: long enough for a second copy (min gap 1 s) to show
SILENCE_S = 3.0                     # "no repeat": three times the time a repeat needs
KEY_SILENCE_S = 2.0                 # the same for the eight key vectors
SETTLE_S = 12.0                     # the edge publishes its counters about every 10 s
COUNT_WAIT_S = 75.0
OWN_WAIT_S = 125.0                  # the message class TTL is 120 s
ORACLE = digi1_oracle.load(root=str(ROOT))      # {} until `python3 -m dvtest.oracle_direwolf digi` has run and the file is committed
SIX_KINDS = ("ident", "ack", "digi", "message", "position", "beacon")

Counts = namedtuple("Counts", "digi tx rejected fenced queue kinds modem_tx")


def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module", autouse=True)
def digi1_switch():
    if os.environ.get("DVT_DIGI1") != "on":
        pytest.skip("DVT_DIGI1 is not on: the DIGI1 statements are asserted only on an image that has the fill-in digipeater (DV-Router 588ffb4c or "
                    "later), with digi1.toml or digi1-roomy.toml in DVT_SUT_CONFIG_EXTRA (docs/digi1-cases.md)")


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


# ---------------------------------------------------------------- counters

def snapshot(hub, port=PORT):
    s = metrics.parse(hub.text())
    return Counts(digi1.digi_counts(s, port), digi1.rf_tx_digi(s, port), digi1.rejected_total(s), digi1.rf_fenced_total(s),
                  digi1.queue_len(s, port), digi1.rf_tx_kinds(s, port), int(metrics.total(s, "dv_edge_modem_total", port=port, result="tx")))


def settled(hub, quiet_s=SETTLE_S, limit_s=90.0):
    """A snapshot after the digi counters did not change for `quiet_s` seconds (the edge's own refresh of an earlier injection has landed)."""
    snap, changed, end = snapshot(hub), time.monotonic(), time.monotonic() + limit_s
    while time.monotonic() - changed < quiet_s and time.monotonic() < end:
        time.sleep(3.0)
        now = snapshot(hub)
        if (now.digi, now.tx, now.fenced, now.modem_tx) != (snap.digi, snap.tx, snap.fenced, snap.modem_tx):
            snap, changed = now, time.monotonic()
        else:
            snap = now
    return snap


def wait_counts(hub, pred, timeout_s=COUNT_WAIT_S, poll_s=3.0):
    """(snapshot, True) once pred(snapshot) holds, else (last snapshot, False)."""
    end = time.monotonic() + timeout_s
    while True:
        now = snapshot(hub)
        if pred(now):
            return now, True
        if time.monotonic() >= end:
            return now, False
        time.sleep(poll_s)


# ---------------------------------------------------------------- the digipeating port

@pytest.fixture(scope="module")
def digi(request, target, hub, rf):
    """The digipeating AX.25 port rf-a behind the fake TNC, after its startup guard, with the counters settled. Skips when the rendered config has
    no digi port with effective TX. Holds: tnc (rf-a), other (rf-b), shape, table (the model's duplicate table), expected (the model's
    dv_edge_digi_total of everything injected since), base (the settled counters), t0."""
    shape = digi1.read_shape(read_rendered_doc(target), PORT)
    if shape.name == "none" or shape.problems:
        pytest.skip("the rendered config has no digipeating AX.25 port %s with effective TX (%s): add digi1.toml or digi1-roomy.toml to "
                    "DVT_SUT_CONFIG_EXTRA instead of dp15-rf.toml" % (PORT, "; ".join(shape.problems)))
    res = rf.tx_guard("DIGI1")
    if getattr(res, "state", "no_reader") not in ("unfenced", "clear"):
        time.sleep(txguard.STARTUP_GUARD_S + 3.0)             # no way to read the fence: wait the longest guard there is
    base = settled(hub)
    ctx = types.SimpleNamespace(tnc=rf.a, other=rf.b, shape=shape, table=digi1.DedupeTable(), expected=digi1.Expected(), base=base,
                                t0=time.monotonic(), rf=rf)
    note(target, "digi", "RECORD", "shape %s: rf-a tx_per_min %d burst %d, digi %s, tx_budget %s, digi class bucket %.1f s; counters at the start: digi %s, tx %s, queue %s, kinds %s" % (
        shape.name, shape.tx_per_min, shape.tx_burst, shape.digi, shape.budget, shape.class_capacity_s,
        {r: n for r, n in base.digi.items() if n}, base.tx, base.queue, base.kinds))
    yield ctx


def need(digi, *shapes):
    if digi.shape.name not in shapes:
        pytest.skip("this case needs the %s shape of the digi snippet (rendered: %s): %s" % (
            " or ".join(shapes), digi.shape.name, {"floor": "digi1.toml (rate floor 10 / 3, class defaults)", "roomy": "digi1-roomy.toml (20 % duty, 30 / 10)"}[shapes[0]]))


def send(tnc, frame):
    t = time.monotonic()
    assert tnc.inject(frame), "the AX.25 edge is not connected to the fake TNC"
    return t


def run_frame(digi, frame, silence_s=None):
    """Inject one frame at rf-a, decide it with the model (the shared duplicate table), wait for the repeat or the silence, record the model's
    result in the ledger. Returns (model result, expected repeat or None, [frames the edge wrote], time of injection)."""
    silence_s = SILENCE_S if silence_s is None else silence_s
    result, want = digi1.decide(frame, STATION, digi.table, time.monotonic())
    digi.expected.add(result, want)
    t0 = send(digi.tnc, frame)
    if want is not None:
        digi.tnc.wait_data(count=1, timeout=REPEAT_WAIT_S, since=t0)
        time.sleep(AFTER_REPEAT_S)
    else:
        time.sleep(silence_s)
    return result, want, [f for _t, _c, f in digi.tnc.data_frames(t0)], t0


def sig(frame):
    """What two digipeaters must agree on: source, destination and every via with its H bit (the information field and the command/response
    bits are not compared: the oracle frames carry another information suffix)."""
    f = ax25.parse_frame(frame)
    return f["src"]["text"], f["dest"]["text"], tuple((d["text"], d["bit7"]) for d in f["digis"])


def terminal(tx):
    return sum(n for r, n in tx.items() if r in digi1.TERMINAL_TX)


def tnc2s(frames):
    return [ax25.path_text(f) for f in frames]


# ================================================================ DIGI-R1: the path vectors

VECTORS = list(digi1.GOLDENS) + list(digi1.extra_vectors())


@pytest.mark.parametrize("g", VECTORS, ids=[g.id for g in VECTORS])
def test_digi1_r1_path_vector(rig, target, hub, digi, g):
    """data.md 8.3, the ten decision steps and the goldens G1 to G13 (the X vectors are more cases of the same steps): a frame whose next hop is
    WIDE1-1 (or our call) comes back ONCE on the receiving port, our call in that one hop, the H bit set, the reserved bits set, the address
    extension bit and every other byte exactly as received (the path never grows); a frame that fails a step is not repeated. G9, G10 and G11
    are the documented deviations from Dire Wolf; every other vector also has to agree with Dire Wolf's answer when the oracle vectors exist."""
    need(digi, "roomy")
    case = "DIGI-R1[%s]" % g.id
    frame = digi1.golden_frame(g, (" %s%s" % (NONCE, g.id)).encode("ascii"))
    result, want, got, _t0 = run_frame(digi, frame)
    assert result == g.result, "the model decides %s for %s, the document table says %s: fix dvtest/digi1.py" % (result, g.id, g.result)
    note(target, case, "RECORD", "%s: heard %s, expected %s: edge wrote %s" % (g.what, ax25.path_text(frame), g.result, tnc2s(got) or "nothing"))
    if want is None:
        assert got == [], "%s (%s): the edge wrote %s, expected silence (step result %s)" % (g.id, ax25.path_text(frame), tnc2s(got), g.result)
    else:
        assert len(got) == 1, "%s: %d frames written for one eligible frame, want exactly 1: %s" % (g.id, len(got), tnc2s(got))
        assert got[0] == want, "%s: wrote %s (%s), expected %s (%s)" % (g.id, got[0].hex(), ax25.path_text(got[0]), want.hex(), ax25.path_text(want))
        problems = digi1.repeat_problems(frame, got[0])
        assert not problems, "%s: %s" % (g.id, problems)
        assert ax25.parse_frame(got[0])["digis"][g.replaced]["bit7"] and len(ax25.hops(got[0])) == len(ax25.hops(frame))
    rec = ORACLE.get(g.id)
    if rec is None:
        note(target, case, "RECORD", "no Dire Wolf vector for %s (vectors/direwolf/digi_oracle.json absent): the answer is judged against the data.md 8.3 table only" % g.id)
        return
    problems, notes = digi1_oracle.oracle_problems(g, rec)
    for n in notes:
        note(target, case, "RECORD", n)
    assert not problems, problems
    if not g.dw.startswith("differs") and rec.get("b_received"):
        dw_kind, dw_frame = digi1_oracle.dw_outcome(rec, b"")
        assert (dw_kind == "repeat") == bool(got), "%s: Dire Wolf %s, the edge %s" % (g.id, dw_kind, "repeated" if got else "was silent")
        if got:
            assert sig(got[0]) == sig(dw_frame), "%s: Dire Wolf repeated %s, the edge %s" % (g.id, tnc2s([dw_frame]), tnc2s(got))


def test_digi1_r1_same_port_only(rig, target, hub, digi):
    """data.md 8.3 "receiving port only": a frame heard at rf-b (no `digi` on that port) is not repeated anywhere, and nothing the edge repeated at
    rf-a ever reaches rf-b's TNC. rf-b emits no dv_edge_digi_total series at all (only rows with digi.enable do)."""
    need(digi, "roomy")
    case = "DIGI-R1c"
    frame = digi1.golden_frame(digi1.GOLDENS[0], (" %sB" % NONCE).encode("ascii"))
    t0 = send(digi.other, frame)
    time.sleep(SILENCE_S)
    s = metrics.parse(hub.text())
    on_b = digi.tnc.data_frames(t0), digi.other.data_frames(t0)
    note(target, case, "RECORD", "frame heard at rf-b %s: written at rf-a %s, at rf-b %s; digi series for rf-b: %s" % (
        ax25.path_text(frame), len(on_b[0]), len(on_b[1]), [x.labels for x in s if x.name == "dv_edge_digi_total" and x.labels.get("port") == OTHER]))
    assert on_b == ([], []), "a frame heard at rf-b (no digi there) was written to a TNC: rf-a %s, rf-b %s" % (tnc2s([f for _t, _c, f in on_b[0]]), tnc2s([f for _t, _c, f in on_b[1]]))
    assert not [x for x in s if x.name == "dv_edge_digi_total" and x.labels.get("port") == OTHER], "dv_edge_digi_total has a series for rf-b, a port without digi.enable"
    everything = digi.other.data_frames(digi.t0)
    assert everything == [], "the edge wrote %d data frame(s) to rf-b since the digipeater was ready: %s" % (len(everything), tnc2s([f for _t, _c, f in everything]))


# ================================================================ DIGI-R2: the duplicate window

def test_digi1_r2_dedupe_timeline(rig, target, hub, digi):
    """data.md 8.3 "The window starts at first acceptance; duplicate observations do not refresh it": one frame sent again at 0, 10, 29, 31 and 45 s
    against digi.dedupe_secs = 30 is repeated at 0, a duplicate at 10 and 29, repeated again at 31 (30 s after the FIRST acceptance have passed:
    29 s was not enough, the duplicate at 10 and 29 did not extend the window) and a duplicate at 45 (14 s after the second acceptance). The exact
    30 s boundary is not probed (a black box cannot hit a millisecond; data.md says the key is new exactly at the boundary, Dire Wolf's is inclusive
    in whole seconds): GAP. Dire Wolf's timeline (vectors) is compared when it exists."""
    need(digi, "roomy")
    case = "DIGI-R2[timeline]"
    frame = ax25.build_ui("APDVT1", "N0TEST-8", ["WIDE1-1"], (">r2 %s" % NONCE).encode("ascii"), cr_src=False)
    want = digi1.timeline_expect()
    seen, t_ref = [], time.monotonic()
    for i, at in enumerate(digi1.TIMELINE_S):
        lead = t_ref + at - time.monotonic()
        if lead > 0:
            time.sleep(lead)
        digi.expected.add(want[i], ax25.replace_hop(frame, 0, STATION) if want[i] == "repeat" else None)
        t = send(digi.tnc, frame)
        digi.tnc.wait_data(count=1, timeout=SILENCE_S, since=t)
        time.sleep(0.5)
        got = digi.tnc.data_frames(t)
        seen.append("repeat" if got else "duplicate")
        if got:
            assert [f for _t, _c, f in got] == [ax25.replace_hop(frame, 0, STATION)], "step %d wrote %s" % (i, tnc2s([f for _t, _c, f in got]))
    note(target, case, "RECORD", "frame sent at %s s: edge %s (window 30 s from the first acceptance)" % (digi1.TIMELINE_S, seen))
    dw = digi1_oracle.timeline_outcomes(ORACLE)
    if dw:
        note(target, case, "RECORD", "Dire Wolf (DEDUPE 30) on the same timeline: %s (inclusive whole-second boundary, not probed here)" % dw)
        assert ["repeat" if x == "repeat" else "duplicate" for x in dw] == seen, "Dire Wolf %s, the edge %s" % (dw, seen)
    else:
        note(target, case, "RECORD", "no Dire Wolf timeline (vectors/direwolf/digi_oracle.json absent)")
    assert seen == want, "decisions at %s s: %s, expected %s" % (digi1.TIMELINE_S, seen, want)


def test_digi1_r2_key_rules(rig, target, hub, digi):
    """data.md 8.3 / 8.4 "Edge fill-in digipeater": the duplicate key is source plus destination (both with SSID) plus the information bytes without
    trailing CR, LF and space; the path and the H bits are ignored, there is no APRS reply-ack folding. Eight frames two seconds apart inside one
    window: the reference repeats; the same frame with another path, and with a trailing space + CR + LF, is a duplicate; another source SSID,
    another destination SSID, another information byte and a leading space each repeat; the reference sent again is a duplicate."""
    need(digi, "roomy")
    case = "DIGI-R2[key]"
    problems = []
    for kid, what, frame, expect in digi1.key_vectors(tag=(" %s" % NONCE).encode("ascii")):
        result, want, got, _t0 = run_frame(digi, frame, silence_s=KEY_SILENCE_S)
        line = "%s %s: model %s, expected %s, edge wrote %s" % (kid, what, result, expect, tnc2s(got) or "nothing")
        note(target, case, "RECORD", line)
        if result != expect:
            problems.append("%s: the model says %s, the table says %s" % (kid, result, expect))
        elif (want is None) != (not got) or (want is not None and got != [want]):
            problems.append("%s (%s): the edge wrote %s, expected %s" % (kid, what, tnc2s(got) or "nothing", tnc2s([want]) if want else "silence"))
    assert not problems, "; ".join(problems)


# ================================================================ DIGI-R4: two fill-ins hearing each other

def test_digi1_r4_two_fill_ins_consume_one_hop_each(rig, target, hub, digi):
    """data.md 8.3 "Floods and loops": "With two fill-ins and WIDE1-1,WIDE1-1, each can consume one hop; no hop is inserted and no more than eight
    repeats are possible for any path. The duplicate table prevents our consuming another hop of the same frame in the window." The second fill-in is
    the TESTER (it replaces a hop the way a fill-in does: ax25.replace_hop); two DV-Router nodes on a loop need the twonode profile (GAP). Order A-B:
    N0TEST-9>APDVT1,WIDE1-1,WIDE1-1 is repeated by us as `N0TEST-10*,WIDE1-1`; B repeats that (`N0TEST-10*,B*`) and the edge hears it: all hops used, no
    repeat. The edge also hears its own copy (what a neighbour that heard it would hand back): our call is used in the path, no repeat. The original
    heard again: duplicate. Order B-A: B repeats first (`B*,WIDE1-1`), the edge consumes the second hop (`B*,N0TEST-10*`), hears its own copy: no repeat.
    Never more repeats than WIDE1-1 hops, the path never grows, a key repeated at most once."""
    need(digi, "roomy")
    case = "DIGI-R4"
    other = "N0TEST-11"
    t_r4 = time.monotonic()
    a_first = ax25.build_ui("APDVT1", "N0TEST-9", ["WIDE1-1", "WIDE1-1"], (">r4a %s" % NONCE).encode("ascii"), cr_src=False)
    steps = []
    result, want, got, _t = run_frame(digi, a_first)
    steps.append(("A-B: heard with WIDE1-1,WIDE1-1", result, want, got))
    first_copy = want
    assert first_copy is not None and got == [first_copy], "the first copy of %s is %s, expected %s" % (ax25.path_text(a_first), tnc2s(got), tnc2s([first_copy]) if first_copy else "none")
    assert [h[0] for h in ax25.hops(first_copy)] == [STATION, "WIDE1-1"] and ax25.hops(first_copy)[0][1], "first copy %s" % ax25.path_text(first_copy)
    second_copy = ax25.replace_hop(first_copy, 1, other)
    for what, frame in (("A-B: the second fill-in repeats our copy (all hops used)", second_copy),
                        ("A-B: our own copy heard again", first_copy),
                        ("A-B: the original heard again", a_first)):
        result, want, got, _t = run_frame(digi, frame)
        steps.append((what, result, want, got))
        assert want is None and got == [], "%s: the edge wrote %s (%s), expected silence" % (what, tnc2s(got), ax25.path_text(frame))
    b_first = ax25.build_ui("APDVT1", "N0TEST-9", [(other, True), "WIDE1-1"], (">r4b %s" % NONCE).encode("ascii"), cr_src=False)
    result, want, got, _t = run_frame(digi, b_first)
    steps.append(("B-A: B repeated first, we consume the second hop", result, want, got))
    assert got == [want] and want is not None, "B-A: wrote %s for %s, expected %s" % (tnc2s(got), ax25.path_text(b_first), tnc2s([want]) if want else "a repeat")
    assert [h[0] for h in ax25.hops(want)] == [other, STATION] and all(h[1] for h in ax25.hops(want)), "B-A copy %s" % ax25.path_text(want)
    result, want2, got2, _t = run_frame(digi, want)
    steps.append(("B-A: our own copy heard again", result, want2, got2))
    assert got2 == [], "B-A: our own copy heard again was repeated: %s" % tnc2s(got2)
    writes = digi.tnc.data_frames(t_r4)
    note(target, case, "RECORD", "; ".join("%s -> %s, wrote %s" % (w, r, tnc2s(g) or "nothing") for w, r, _x, g in steps))
    for _t, _c, f in writes:
        assert len(ax25.hops(f)) <= 2 and sum(1 for h in ax25.hops(f) if h[1]) <= 2, "a path grew or has more used hops than it had: %s" % ax25.path_text(f)
    keys = [digi1.dedupe_key(f) for _t, _c, f in writes]
    assert len(keys) == len(set(keys)), "one key was repeated twice inside its window: %s" % tnc2s([f for _t, _c, f in writes])


# ================================================================ DIGI-R3: the flood at the rate floor

def test_digi1_r3_flood_obeys_the_quotas_and_own_frames_survive(request, rig, target, hub, digi, web_ctx):
    """data.md 8.3 / 10.8 (DIGI-R3), shape digi1.toml (tx_per_min 10, tx_burst 3, tx_budget {}): 60 eligible frames in one minute from 10 sources
    (distinct information, so no duplicate). Asserted: every copy is the repeat of one injected frame, the hop replaced and nothing else; no frame twice;
    no copy leaves more than the digi TTL (5 s, plus 1 s of timing) after its frame was heard; copies are at least the minimum gap (1 s) apart; the airtime
    the copies charge is within the digi class bucket (4/12 of 30 s = 10 s, refilling) plus one frame, within the whole port (5 % of 600 s) and within
    each source's quota (25 %); the counters: digi{repeat} = 60 (every frame was accepted by the duplicate table), the conservation of 10.8 holds, no
    copy waits in the queue afterwards. OWN FRAMES (the review finding of DIGI1): a web text is posted into room B 5 s into the flood and leaves rf-a as a
    group message while the digi class is spent; it is NOT dropped by TTL (message TTL 120 s; dv_edge_rf_tx_total{kind=message,result=dropped_ttl} and
    dropped_queue do not move) and arrives within that TTL. The oracle for the OFFERED frames is the model (all 60 are eligible: Dire Wolf would repeat
    them all); DV-Router's quotas are its own policy, so no Dire Wolf number is asserted. This case runs FIRST in its run (it reads the counters from a
    snapshot taken at its start)."""
    need(digi, "floor")
    case = "DIGI-R3"
    plan = digi1.flood_plan(NONCE)
    body = ds.nonce_body("R3")
    before = settled(hub)
    sample_before = metrics.parse(hub.text())
    t_start = time.monotonic()
    injected, posted = [], None
    for offset, src, info, frame in plan:
        lead = t_start + offset - time.monotonic()
        if lead > 0:
            time.sleep(lead)
        if posted is None and offset >= 5.0:
            posted = post(web_ctx.posters[0], body)
            assert posted[2] == 202, "POST answered %s" % posted[2]
        result, _want = digi1.decide(frame, STATION, digi.table, time.monotonic())
        assert result == "repeat"
        digi.expected.add(result, None)
        injected.append((send(digi.tnc, frame), src, info, frame))
    t_end = time.monotonic()
    assert posted is not None, "the flood ended before the own text was posted"
    own, copies = None, []
    deadline = posted[0] + OWN_WAIT_S
    while time.monotonic() < deadline:
        copies, own = [], None
        for t, _c, f in digi.tnc.data_frames(t_start):
            msg = kiss_tnc.decode_message(f)
            if msg is not None and msg["addressee"] == ROOM_NAME and body in msg["text"]:
                own = (t, msg)
            else:
                copies.append((t, f))
        if own is not None and time.monotonic() - t_end > 8.0:
            break
        time.sleep(1.0)
    note(target, case, "RECORD", "flood: %d frames in %.0f s from %d sources; the edge wrote %d copies; own message frame %s after %s s" % (
        len(injected), t_end - t_start, len(digi1.flood_sources()), len(copies), "arrived" if own else "NEVER arrived",
        None if own is None else "%.1f" % (own[0] - posted[0])))
    problems = digi1.flood_problems(injected, copies, budget=digi.shape.budget, ttl_s=digi1.TTL_S)
    after, ok = wait_counts(hub, lambda c: c.digi.get("repeat", 0) - before.digi.get("repeat", 0) >= len(plan) and c.queue in (0, None) and
                            terminal(c.tx) - terminal(before.tx) + (c.rejected - before.rejected) >= len(plan))
    sample_after = metrics.parse(hub.text())
    d_digi, d_tx = digi1.delta(after.digi, before.digi), digi1.delta(after.tx, before.tx)
    note(target, case, "RECORD", "counters (delta): digi %s; rf_tx{kind=digi} %s; rejected %d; rf_fenced %d; queue %s; modem tx %d" % (
        {r: n for r, n in d_digi.items() if n}, {r: n for r, n in d_tx.items() if n}, after.rejected - before.rejected, after.fenced - before.fenced, after.queue,
        after.modem_tx - before.modem_tx))
    own_bad = {}
    for kind in ("ack", "message", "position", "beacon"):
        for result in ("dropped_ttl", "dropped_queue", "dropped_exceeds_quota", "dropped_too_long"):
            n = digi1.rf_tx_kind_result(sample_after, PORT, kind, result) - digi1.rf_tx_kind_result(sample_before, PORT, kind, result)
            if n:
                own_bad["%s/%s" % (kind, result)] = n
    sent_msg = digi1.rf_tx_kind_result(sample_after, PORT, "message", "sent") - digi1.rf_tx_kind_result(sample_before, PORT, "message", "sent")
    note(target, case, "RECORD", "own frames: message sent %d, own frames dropped %s" % (sent_msg, own_bad or "none"))
    assert not problems, "flood bounds: %s" % "; ".join(problems)
    assert own is not None, "the own message frame never reached the TNC in %.0f s (message TTL 120 s)" % OWN_WAIT_S
    assert own[0] - posted[0] < 120.0, "the own message took %.1f s, longer than its class TTL of 120 s" % (own[0] - posted[0])
    assert own[1]["src"] == STATION and own[1]["dst"] == EDGE_TOCALL, "own frame %s>%s" % (own[1]["src"], own[1]["dst"])
    assert not own_bad, "own frames were dropped by a digi flood: %s (a flood of eligible frames must not expire or evict the station's own frames)" % own_bad
    assert ok, "the counters did not settle in %.0f s: digi %s, tx %s, queue %s" % (COUNT_WAIT_S, d_digi, d_tx, after.queue)
    assert d_digi.get("repeat", 0) == len(plan) and sum(d_digi.values()) == len(plan), "dv_edge_digi_total grew by %s for %d eligible frames" % (
        {r: n for r, n in d_digi.items() if n}, len(plan))
    cons = digi1.conservation_problems(d_digi, d_tx, after.rejected - before.rejected, after.queue or 0, after.fenced - before.fenced)
    assert not cons, cons
    accounted = {r: n for r, n in d_tx.items() if n}
    assert len(copies) == d_tx.get("sent", 0), "the TNC got %d copies but rf_tx{kind=digi,result=sent} is %s" % (len(copies), accounted)


# ================================================================ the counters

def test_digi1_counters_and_conservation(rig, target, hub, digi):
    """data.md 10.8, status.md (edge AX.25 fill-in): after the cases above, dv_edge_digi_total{port=rf-a,result} grew by exactly what the model decided
    for every frame injected (each candidate increments exactly one of the nine fixed results; rf-b has no series), the label set is the documented
    nine; dv_edge_rf_tx_total{kind=digi,result=sent} grew by the number of copies the TNC saw, equals the edge's modem tx count, and the kinds present
    are a subset of the six fixed ones; the conservation: digi{repeat} = pre-scheduler refusals + terminal rf_tx{kind=digi} results + canceled + still
    queued. Needs an earlier case to have injected frames (else it skips)."""
    case = "DIGI1[counters]"
    exp = digi.expected
    if exp.total() == 0:
        pytest.skip("no frame was injected before this case in this run")
    written = [f for _t, _c, f in digi.tnc.data_frames(digi.t0)]
    copies = len([f for f in written if ax25.parse_frame(f)["src"]["text"] != STATION])        # a repeat keeps the source; the station's own frames (R3's text) carry its call
    done = lambda c: (digi1.delta(c.digi, digi.base.digi) == exp.counts and (c.queue in (0, None)) and  # noqa: E731
                      digi1.delta(c.tx, digi.base.tx).get("sent", 0) >= copies)
    now, ok = wait_counts(hub, done)
    d_digi, d_tx = digi1.delta(now.digi, digi.base.digi), digi1.delta(now.tx, digi.base.tx)
    note(target, case, "RECORD", "injected since the start %s; counters (delta): digi %s; rf_tx{kind=digi} %s; rejected %d; rf_fenced %d; queue %s; kinds %s; modem tx %d; copies at the TNC %d of %d frames written" % (
        exp.nonzero(), {r: n for r, n in d_digi.items() if n}, {r: n for r, n in d_tx.items() if n}, now.rejected - digi.base.rejected,
        now.fenced - digi.base.fenced, now.queue, now.kinds, now.modem_tx - digi.base.modem_tx, copies, len(written)))
    problems = digi1.count_problems(exp, d_digi)
    assert not problems, problems
    assert ok, "the counters did not settle in %.0f s" % COUNT_WAIT_S
    assert set(now.kinds) <= set(SIX_KINDS), "dv_edge_rf_tx_total{port=rf-a} has kind label(s) %s outside the six fixed kinds %s" % (sorted(set(now.kinds) - set(SIX_KINDS)), SIX_KINDS)
    assert "digi" in now.kinds, "dv_edge_rf_tx_total has no kind=digi series after %d repeats" % exp.counts["repeat"]
    assert d_tx.get("sent", 0) == copies, "rf_tx{kind=digi,result=sent} grew by %d, the TNC saw %d copies" % (d_tx.get("sent", 0), copies)
    assert now.modem_tx - digi.base.modem_tx == len(written), "dv_edge_modem_total{result=tx} grew by %d, the TNC saw %d frames" % (now.modem_tx - digi.base.modem_tx, len(written))
    cons = digi1.conservation_problems(d_digi, d_tx, now.rejected - digi.base.rejected, now.queue or 0, now.fenced - digi.base.fenced)
    assert not cons, cons
    assert d_digi.get("fenced", 0) == 0 and now.fenced - digi.base.fenced == 0, "a fence refused frames in a run without a restart: digi fenced %d, rf_fenced %d" % (
        d_digi.get("fenced", 0), now.fenced - digi.base.fenced)


# ================================================================ DIGI-R5: the startup guard

def test_digi1_r5_nothing_repeated_in_the_startup_guard(request, rig, target, hub, digi):
    """data.md 8.3 "TX off, a startup guard or any fence never permits repeating, and fenced frames are not remembered" / radio.md 10.4: the AX.25
    child is restarted through the audited admin API (POST /api/v1/admin/children/dv-edge-ax25/restart, as DP15-6g); the new process starts fenced
    (startup_guard, about 13 s). As soon as it has reconnected to the fake TNC two eligible frames are heard INSIDE the guard (the fence is read after
    the injection; a late attempt is repeated once with new keys). Asserted: not one data frame reaches the TNC during the guard or in the 2 s after
    it; after the guard the FIRST retry of the guarded frame IS repeated (a fenced candidate was not remembered: no `duplicate`), byte for byte; the
    same frame again is a duplicate. Counters of the new process: fenced 2, repeat 1, duplicate 1, and the fence refusals equal digi{fenced}
    (dv_edge_drops_total{reason=rf_fenced}); conservation. This case RESTARTS the edge and must be the last of its run."""
    case = "DIGI-R5"
    admin_session = request.getfixturevalue("admin_session")
    operator = admin_session("operator")
    status = operator.get(operator.path("/status"), expect=None)
    names, seen_names = txguard.ax25_child_names(status.json() if status.status == 200 else {})
    if names != [txguard.AX25_CHILD]:
        pytest.skip("the launcher child %r is not listed by /api/v1/admin/status; children seen: %s" % (txguard.AX25_CHILD, seen_names))
    tnc = digi.tnc
    reader = txguard.ax25_reader(operator, PORT)
    before = settled(hub)
    window = None
    for attempt in (1, 2):
        f1 = ax25.build_ui("APDVT1", "N0TEST-9", ["WIDE1-1"], (">r5 %s a%d" % (NONCE, attempt)).encode("ascii"), cr_src=False)
        f2 = ax25.build_ui("APDVT1", "N0TEST-8", ["WIDE1-1"], (">r5 %s b%d" % (NONCE, attempt)).encode("ascii"), cr_src=False)
        connections = tnc.connections
        admin = admin_session("admin")
        admin.step_up()
        resp = admin.post(admin.path("/children/%s/restart" % txguard.AX25_CHILD), {}, expect=None)
        assert resp.status == 200, "POST children/%s/restart answered HTTP %d, want 200" % (txguard.AX25_CHILD, resp.status)
        t_restart = time.monotonic()
        assert tnc.wait(lambda t: t.connections > connections and len(t.startup_params(t.connections)) >= 5, 30.0), \
            "the restarted AX.25 edge did not reconnect to the fake TNC with its KISS parameters in 30 s"
        t1 = send(tnc, f1)
        time.sleep(0.4)
        t2 = send(tnc, f2)
        obs = reader()
        note(target, case, "RECORD", "attempt %d: %.1f s after the restart returned the edge was connected; frames heard %.1f s and %.1f s after; fence now: %s, guard_ms %s" % (
            attempt, t1 - t_restart, t1 - t_restart, t2 - t_restart, obs.fence if obs.found else "port row not found", obs.guard_ms if obs.found else None))
        if obs.found and obs.fenced:
            window = (f1, f2, t_restart)
            break
        note(target, case, "RECORD", "attempt %d was too late (the port is not fenced); %s" % (attempt, "trying once more with new keys" if attempt == 1 else "giving up"))
    if window is None:
        pytest.skip("could not inject inside the startup guard in two attempts (the fence was already over at the read)")
    f1, f2, t_restart = window
    res = txguard.wait_unfenced(reader, kind="ax25")
    note(target, case, "RECORD", "fence wait: " + txguard.describe(res, "ax25", PORT))
    assert res.state in ("unfenced", "clear"), "the startup guard did not end: %s" % (res,)
    time.sleep(2.0)
    during = tnc.data_frames(t_restart)
    assert during == [], "the edge wrote %d data frame(s) during the startup guard or right after it: %s" % (len(during), tnc2s([f for _t, _c, f in during]))
    t3 = send(tnc, f1)
    tnc.wait_data(count=1, timeout=REPEAT_WAIT_S, since=t3)
    time.sleep(AFTER_REPEAT_S)
    retry = [f for _t, _c, f in tnc.data_frames(t3)]
    want = ax25.replace_hop(f1, 0, STATION)
    assert retry == [want], "the first retry of the guarded frame after the guard wrote %s, expected %s: a fenced candidate must not be remembered" % (
        tnc2s(retry) or "nothing", tnc2s([want]))
    t4 = send(tnc, f1)
    time.sleep(SILENCE_S)
    again = tnc.data_frames(t4)
    assert again == [], "the frame retried a second time was repeated again: %s (it is a duplicate now)" % tnc2s([f for _t, _c, f in again])
    exp = {"fenced": 2, "repeat": 1, "duplicate": 1}

    def reached(c):
        return digi1.restart_counts_match(before.digi, c.digi, exp) is not None

    now, ok = wait_counts(hub, reached)
    mode = digi1.restart_counts_match(before.digi, now.digi, exp)
    note(target, case, "RECORD", "counters after the restart: digi %s (before %s), mode %s; rf_tx{kind=digi} %s; rf_fenced %d (before %d); rejected %d; queue %s" % (
        {r: n for r, n in now.digi.items() if n}, {r: n for r, n in before.digi.items() if n}, mode, now.tx, now.fenced, before.fenced, now.rejected, now.queue))
    assert ok and mode is not None, "dv_edge_digi_total after the restart is %s (before %s), expected fenced 2, repeat 1, duplicate 1" % (
        {r: n for r, n in now.digi.items() if n}, {r: n for r, n in before.digi.items() if n})
    if mode == "reset":
        d_digi, d_tx, d_rej, d_fenced = now.digi, now.tx, now.rejected, now.fenced
    else:
        d_digi, d_tx = digi1.delta(now.digi, before.digi), digi1.delta(now.tx, before.tx)
        d_rej, d_fenced = now.rejected - before.rejected, now.fenced - before.fenced
    assert d_fenced == exp["fenced"], "dv_edge_drops_total{reason=rf_fenced} counted %d, digi{fenced} is %d: a fenced candidate is counted once in both" % (d_fenced, exp["fenced"])
    cons = digi1.conservation_problems(d_digi, d_tx, d_rej, now.queue or 0, d_fenced)
    assert not cons, cons
