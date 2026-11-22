# SPDX-License-Identifier: MIT
"""RADIOSEQ1 judges (docs/radioseq1-cases.md): DV-Router's radio hub numbers received DMR, YSF, NXDN and P25 voice by FRAME ORDINAL (one step per DMR
burst, YSF frame, NXDN frame, P25 LDU) instead of in 20 ms units (bc9af2b1: += 3, 5, 4, 9). Pure functions over the datagrams a protocol client
heard and over parsed hub metrics; the switch DVT_RADIOSEQ1=on decides whether the cases assert or only record (dvtest/target.py).

  on(environ)                              the switch
  dmrd_bursts(datagrams)                   the DMRD datagrams as Burst rows (flags byte 15: frame type bits 5-4, voice sequence bits 3-0)
  letters(bursts) / histogram(bursts)      what a DMR listener saw: "ABCDEF..." (a letter in lower case = its sync flag disagrees), counts per letter
  superframe_verdict(bursts)               voice bursts A..F in order per superframe, sync on A only, none repeated or skipped, one stream
  cyclic_progression(values, moduli, step) a counter that must advance by `step` modulo one of `moduli` (YSF FN, NXDN SACCH structure)
  ysf_fn_values(datagrams)                 FN of the FICH of every YSF voice packet (ysf_ft_values: FT)
  ysf_fn_check(values, fts)                FN of a YSF egress: +1 modulo 7 (FN 0..6, FT 6); FN 7 on a voice packet is a FINDING, the modulus seen is recorded
  nxdn_sacch_structures(datagrams)         the two-bit SACCH structure of every NXDN voice packet
  counts(samples, room) / delta(a, b)      the gap and over counters of the radio edge and of one room, from dvtest.metrics samples
  judge_counts(change)                     gap 0 at the edge and in the room, the over not counted lossy
  port_pair(doc) / pair_problems(a, b)     the two mmdvm-serial rows of RADIOSEQ1-C (rif5-two-ports.toml) and what is wrong with the pair
  dmr_tx_letters(frames) / relay_verdict   a DMR TX log of the second fake board: voice bursts A..F in order, sync on A, every word (case C)
  fixed_mode(port) / fixed_mode_problems   a row with ONE mode claims it once at Up (radio.md 7.2), not per over: the SET_MODE rule of case C for such a board
"""
import os
from collections import Counter, namedtuple

from . import medload1, metrics, rif5
from . import mmdvm_rf as rf
from . import mmdvm_serial as ms
from .proto import dmr, nxdn, ysf

ENV = "DVT_RADIOSEQ1"
LETTERS = "ABCDEF"
MAX_LISTED = 6                                   # positions named in one problem line
YSF_FN_MODULUS = 7                               # dv-router: a voice packet carries FT 6 and FN 0..=FT (crates/edge/ysf/src/wire.rs VOICE_FT, voice_fn); FT 7 is header / terminator
YSF_VOICE_FT = 6
YSF_FN_MODULI = (YSF_FN_MODULUS, 8)              # the modulus the egress may be seen with: 7 (the answer), 8 (the 3-bit field, a FINDING)
NXDN_SACCH_STEP = -1                             # NXDN [1-B] 4.1.6: the structure of the SACCH superframe is 3, 2, 1, 0 (dvtest/proto/nxdn.py voice_payload)

Burst = namedtuple("Burst", "seq stream frame_type vseq slot")
Verdict = namedtuple("Verdict", "problems facts")
Checked = namedtuple("Checked", "problems findings facts")       # a verdict that also names what is only a FINDING


def on(environ=None):
    """DVT_RADIOSEQ1=on: the image has RADIOSEQ1 (DV-Router 917b9e3b, wip-37b7960 or later): the numbering assertions run."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


# ---------------------------------------------------------------- DMR letters

def dmrd_bursts(datagrams):
    """Burst rows of the DMRD datagrams among `datagrams` (anything else, MSTPONG and the like, is left out), in arrival order. frame_type: 0 voice,
    1 voice sync, 2 data sync (header, terminator), 3 reserved; vseq: the voice sequence 0..5 = A..F or, for frame type 2, the data type."""
    out = []
    for data in datagrams:
        info = dmr.parse(bytes(data))
        if info is None:
            continue
        bits = dmr.parse_bitfield(info["bitfield"])
        out.append(Burst(info["seq"], info["stream"], bits["frame_type"], bits["vseq"], bits["slot"]))
    return out


def voice_bursts(bursts):
    return [b for b in bursts if b.frame_type in (dmr.VOICE, dmr.VOICE_SYNC)]


def letter(burst):
    """The letter of a voice burst. Lower case: the sync flag disagrees with the letter (sync is carried by A only). "?" for a vseq above 5."""
    if burst.vseq >= len(LETTERS):
        return "?"
    char = LETTERS[burst.vseq]
    return char if (burst.frame_type == dmr.VOICE_SYNC) == (burst.vseq == 0) else char.lower()


def letters(bursts):
    return "".join(letter(b) for b in voice_bursts(bursts))


def histogram(bursts):
    """{letter: count} of the voice bursts, letters in A..F order first (the evidence of bc9af2b1 was {"A": 50, "D": 50} for a lossless 150 word over)."""
    seen = Counter(letter(b) for b in voice_bursts(bursts))
    return {k: seen[k] for k in sorted(seen, key=lambda c: (c.upper() not in LETTERS, LETTERS.find(c.upper()), c.islower()))}


def superframe_verdict(bursts):
    """The voice bursts of ONE lossless over, in arrival order: the first is A, each next one is the following letter (F is followed by A), the sync flag is
    set on A and on no other letter, and they all belong to one stream. A trailing partial superframe is fine (a pad), a repeated or skipped letter is not."""
    voice = voice_bursts(bursts)
    facts = {"voice_bursts": len(voice), "superframes": len(voice) // 6, "tail": len(voice) % 6, "data_bursts": len(bursts) - len(voice),
             "streams": len({b.stream for b in voice}), "histogram": histogram(bursts), "letters_head": letters(bursts)[:24]}
    problems = []
    if not voice:
        return Verdict(["no voice burst heard"], facts)
    if facts["streams"] != 1:
        problems.append("%d streams among the voice bursts, one over wanted" % facts["streams"])
    wrong = [i for i, b in enumerate(voice) if b.vseq != i % 6]
    repeated = sum(1 for a, b in zip(voice, voice[1:]) if a.vseq == b.vseq)
    skipped = sum(1 for a, b in zip(voice, voice[1:]) if a.vseq != b.vseq and (b.vseq - a.vseq) % 6 != 1)
    facts.update(out_of_order=len(wrong), repeated=repeated, skipped=skipped)
    if voice[0].vseq != 0:
        problems.append("the first voice burst is %s, wanted A" % letter(voice[0]))
    if wrong:
        shown = ", ".join("#%d %s (wanted %s)" % (i, letter(voice[i]), LETTERS[i % 6]) for i in wrong[:MAX_LISTED])
        problems.append("%d of %d voice bursts are not the letter of their position: %s%s; repeated %d, skipped %d" % (
            len(wrong), len(voice), shown, " ..." if len(wrong) > MAX_LISTED else "", repeated, skipped))
    bad_sync = [i for i, b in enumerate(voice) if (b.frame_type == dmr.VOICE_SYNC) != (b.vseq == 0)]
    facts["sync_misplaced"] = len(bad_sync)
    if bad_sync:
        problems.append("%d bursts with the sync flag on a letter other than A or without it on A (first at #%d)" % (len(bad_sync), bad_sync[0]))
    return Verdict(problems, facts)


# ---------------------------------------------------------------- counters that must advance by one

def cyclic_progression(values, moduli, step=1):
    """values advance by `step` modulo m for one m of `moduli` (the first that fits every value and every step) at every position: no repeat, no skip.
    Returns Verdict(problems, facts); facts: modulus (None when none fits), steps (histogram of the observed steps, modulo the largest modulus), head."""
    values = list(values)
    facts = {"n": len(values), "head": values[:16], "modulus": None}
    if len(values) < 2:
        return Verdict(["only %d value(s) heard, 2 or more wanted" % len(values)], facts)
    top = max(moduli)
    facts["steps"] = dict(sorted(Counter((b - a) % top for a, b in zip(values, values[1:])).items()))
    for modulus in sorted(moduli):
        if all(0 <= v < modulus for v in values) and all((b - a) % modulus == step % modulus for a, b in zip(values, values[1:])):
            facts["modulus"] = modulus
            return Verdict([], facts)
    return Verdict(["the values do not advance by %+d modulo %s: steps modulo %d %s, head %s" % (
        step, "/".join(str(m) for m in sorted(moduli)), top, facts["steps"], facts["head"])], facts)


def ysf_fn_values(datagrams):
    """FN of the FICH of every YSF V/D mode 2 voice packet, in arrival order."""
    out = []
    for data in datagrams:
        info = ysf.parse(bytes(data))
        if info is not None and info["kind"] == "voice":
            out.append(info["fich"]["fn"])
    return out


def ysf_ft_values(datagrams):
    """FT of the FICH of every YSF V/D mode 2 voice packet, in arrival order."""
    out = []
    for data in datagrams:
        info = ysf.parse(bytes(data))
        if info is not None and info["kind"] == "voice":
            out.append(info["fich"]["ft"])
    return out


def ysf_fn_check(values, fts=None):
    """The FN of a YSF egress (dv-router: voice packets carry FT 6 and FN 0..=6, FT 7 is for header and terminator): each voice packet advances FN by +1 modulo 7.
    FN 7 on a voice packet is a FINDING, not a problem, when the numbering still advances by +1 modulo 8: the modulus seen is facts["modulus"] (7, 8 or None when
    neither fits, which is a problem). `fts` (optional): the FT of the same packets; any other than 6 is a FINDING. Returns Checked(problems, findings, facts)."""
    values = list(values)
    problems, findings = [], []
    base = cyclic_progression(values, (YSF_FN_MODULUS,))
    facts = dict(base.facts)
    facts["fn7"] = sum(1 for v in values if v == 7)
    if len(values) < 2:
        return Checked(base.problems, findings, facts)
    if base.facts["modulus"] is None:
        wide = cyclic_progression(values, (8,))
        if wide.facts["modulus"] == 8:
            facts["modulus"] = 8
            findings.append("FN 7 on %d of %d voice packets: the egress cycles FN modulo 8, dv-router says voice carries FN 0..6 (FT 6); first values %s" % (
                facts["fn7"], len(values), values[:16]))
        else:
            problems.append("FN does not advance by +1 modulo %d (nor modulo 8): steps modulo 8 %s, head %s" % (YSF_FN_MODULUS, facts.get("steps"), values[:16]))
    if fts is not None:
        facts["ft"] = sorted(set(fts))
        if facts["ft"] != [YSF_VOICE_FT]:
            findings.append("FT of the voice packets %s, dv-router says %d (FT 7 is header and terminator)" % (facts["ft"], YSF_VOICE_FT))
    return Checked(problems, findings, facts)


def nxdn_sacch_structures(datagrams):
    """The SACCH structure field (bits 7-6 of the first SACCH byte, payload byte 1) of every NXDN voice packet, in arrival order."""
    out = []
    for data in datagrams:
        info = nxdn.parse(bytes(data))
        if info is not None and info["kind"] == "voice":
            out.append(nxdn.parse_sacch(info["payload"][1:5])["structure"])
    return out


# ---------------------------------------------------------------- hub counters

Counts = namedtuple("Counts", "edge_gap edge_late edge_dropped room_gap room_late room_lost overs_complete overs_lossy overs_end_missing "
                              "egress_lost egress_late egress_dropped")


def counts(samples, room, proc="radio"):
    """Counters of the radio edge (`proc`, ingress: dv_edge_over_units_total{dir=rx,proc=radio}) and of one room letter (lower case), from parsed hub samples
    (dvtest.metrics.parse). egress_*: dv_edge_over_units_total{dir=tx} summed over every edge (the listeners' edges: an egress drop is a word a listener
    did not get, the dmrhb EgressDropped 36 of the NXDN over of run 20261122T160037Z)."""
    def edge(direction, result, only=None):
        return sum(s.value for s in samples if s.name == medload1.EDGE_UNITS and s.labels.get("dir") == direction and s.labels.get("result") == result
                   and (only is None or s.labels.get("proc") == only))

    def room_units(result):
        return sum(s.value for s in samples if s.name == medload1.ROOM_UNITS and s.labels.get("room") == room and s.labels.get("result") == result)

    def overs(result):
        return sum(s.value for s in samples if s.name == medload1.ROOM_OVERS and s.labels.get("room") == room and s.labels.get("result") == result)

    return Counts(edge("rx", "gap", proc), edge("rx", "late", proc), edge("rx", "dropped", proc),
                  room_units("gap"), room_units("late"), room_units("lost"), overs("complete"), overs("lossy"), overs("end_missing"),
                  edge("tx", "lost"), edge("tx", "late"), edge("tx", "dropped"))


def delta(after, before):
    return Counts(*[a - b for a, b in zip(after, before)])


def judge_counts(change, over_expected=True):
    """A lossless radio over: no gap at the radio edge, none in the room, the over not counted lossy. `over_expected`: at least one over closed in the room
    (complete or end_missing; end_missing is a FINDING of the caller, not a problem here). Returns Verdict(problems, facts)."""
    facts = dict(change._asdict())
    problems = []
    if change.edge_gap:
        problems.append("dv_edge_over_units_total{dir=rx,proc=radio,result=gap} rose by %g, a lossless over has none" % change.edge_gap)
    if change.room_gap:
        problems.append("dv_room_over_units_total{result=gap} rose by %g in the room, a lossless over has none" % change.room_gap)
    if change.overs_lossy:
        problems.append("dv_room_overs_total{result=lossy} rose by %g: the lossless over was counted lossy" % change.overs_lossy)
    if over_expected and medload1.overs_total(change) < 1:
        problems.append("no over closed in the room (dv_room_overs_total did not move)")
    return Verdict(problems, facts)


# ---------------------------------------------------------------- (c) between two radio ports

PORT_B = "rif5-modem-b"                          # the second row of rig/config/dv-router/rif5-two-ports.toml; the first is rif5.PORT_NAME
SNIPPET_B = "rif5-two-ports.toml"


def port_pair(doc):
    """(A, B) of a rendered dv-router.toml: the row `rif5-modem` (else the first mmdvm-serial udp row that is not B) and the row `rif5-modem-b`, as
    rif5.port_from_config reads them; B is None without that row, A is None without any other row."""
    b = rif5.port_from_config(doc, PORT_B)
    a = rif5.port_from_config(doc, rif5.PORT_NAME)
    if a is None:
        first = rif5.port_from_config(doc)
        a = first if first is not None and first.name != PORT_B else None
    return a, b


def pair_problems(a, b, mode="dmr"):
    """What makes the two rows unfit for RADIOSEQ1-C (config.md and dv-router's answers): both transmit, the same room for `mode`, distinct names, byte links
    (udp_bind) and physicals (one physical is one transmitter), one band if a physical were shared. [] when fit."""
    if a is None or b is None:
        return ["two mmdvm-serial rows are needed (%s and %s)" % (rif5.PORT_NAME, PORT_B)]
    problems = []
    for label, port in (("A", a), ("B", b)):
        if port.tx not in (True, "true"):
            problems.append("port %s (%s) has tx = %r, an over is transmitted only by a port with tx on" % (label, port.name, port.tx))
        if mode not in port.modes:
            problems.append("port %s (%s) has no %s mode" % (label, port.name, mode))
    if mode in a.modes and mode in b.modes and a.modes[mode] != b.modes[mode]:
        problems.append("the %s room differs: %s serves %s, %s serves %s (the case is about ONE room)" % (mode, a.name, a.modes[mode], b.name, b.modes[mode]))
    for what, x, y in (("name", a.name, b.name), ("udp_bind", a.bind, b.bind), ("udp", a.peer, b.peer), ("physical", a.physical, b.physical)):
        if x == y:
            problems.append("both rows have the same %s %r (distinct values wanted%s)" % (what, x, ": one physical is one transmitter" if what == "physical" else ""))
    if not a.physical or not b.physical:
        problems.append("a port without a physical cannot transmit")
    if a.physical == b.physical and (a.rx_freq, a.tx_freq) != (b.rx_freq, b.tx_freq):
        problems.append("a shared physical must agree on band and frequency")
    return problems


def dmr_tx_letters(frames):
    """The voice bursts of a DMR TX log of a modem (host frames, type DMR_DATA2), as letters: a burst with the voice sync is A (and restarts the count), the next
    ones are B, C ... (counted from the last sync); "?" before the first sync or past F. Header and terminator bursts (data sync) are left out."""
    kinds = [rf.classify_burst(bytes(f.payload[1:])) for f in frames if f.ftype == ms.DMR_DATA2 and len(f.payload) == 1 + rf.DMR_BURST]
    out, count = [], None
    for kind in kinds:
        if kind == "data":
            continue
        if kind == "voice_sync":
            count = 0
        elif count is not None:
            count += 1
        out.append("?" if count is None or count >= len(LETTERS) else LETTERS[count])
    return "".join(out)


def fixed_mode(port):
    """True for a row with exactly one mode (`modes = { dmr = "Q" }`): radio.md 7.2 "Fixed mode ... selects the mode at Up and has no mode-hang switching", so the
    board gets its SET_MODE once at Up (and again after a Down / Up, or after an idle park for an owed CW ID) and is NOT claimed per over nor returned to idle."""
    return len(getattr(port, "modes", None) or {}) == 1


def fixed_mode_problems(frames, earlier, mode="dmr"):
    """The SET_MODE rule for a fixed-mode board over one over. `frames` are the host frames of the over window, `earlier` the SET_MODE frames of the same board
    before the window (the claim at Up is there). Returns (problems, findings, facts): the most recent SET_MODE before the first native frame (window or
    earlier) must be the row's mode (an idle park followed by a re-entry of the fixed mode is fine, an idle or another mode left standing is a problem), and
    no SET_MODE other than the row's mode may fall inside the over (mode switching never interrupts a stream). No SET_MODE at all, not even at Up, is a
    FINDING (a possible product bug), not a problem. No idle is expected after the over."""
    problems, findings, facts = [], [], {}
    want = bytes((ms.MODE_ID[mode],))
    natives = [f for f in frames if f.ftype in ms.TX_TYPES[mode]]
    modes = sorted([f for f in earlier if f.ftype == ms.SET_MODE] + [f for f in frames if f.ftype == ms.SET_MODE], key=lambda f: f.mono)
    start = natives[0].mono if natives else None
    before = [f for f in modes if start is None or f.mono <= start]
    facts["fixed_modes_before"] = [f.payload.hex() for f in before][-4:]
    if not before:
        findings.append("a fixed-mode board never got a SET_MODE, not even at Up, before its first %s frame (radio.md 7.2: a single-mode row selects its mode at Up)" % mode)
    elif before[-1].payload != want:
        problems.append("the last SET_MODE before the first %s frame is %s, a fixed-mode row wants %02x (selected at Up, an idle park is followed by a re-entry; radio.md 7.2)" % (
            mode, before[-1].payload.hex(), ms.MODE_ID[mode]))
    if natives:
        inside = [f for f in modes if f.mono > natives[0].mono and f.mono < natives[-1].mono and f.payload != want]
        if inside:
            problems.append("SET_MODE %s written %.2f s before the last native frame of the over (radio.md 7.2: mode switching never interrupts a stream)" % (
                inside[0].payload.hex(), natives[-1].mono - inside[0].mono))
    return problems, findings, facts


def relay_verdict(frames, sent, dst, color_code, fixed=False, earlier=()):
    """An over that came from ANOTHER radio port, as the second fake board saw it written (host frames since the start): SET_MODE 2, eight headers, voice
    bursts, three terminators (rif5.judge_tx_dmr: LC group dst and colour code of the row, one source id across headers and terminators, every word of `sent`
    in order, burst A with the voice sync) and, on top, the letters of the voice bursts in one lossless run A..F, A..F ... with the burst count of the words sent.
    The source id is whatever the headers carry (facts["src"]; comparing it with the id of the over played on the other board is the caller's business).
    `fixed` (the receiving row has one mode, fixed_mode) replaces the claim-per-over rule by fixed_mode_problems over `frames` and `earlier` (the SET_MODE frames
    of that board before the over). Returns Checked(problems, findings, facts)."""
    slot = rf.decode_dmr([(f.ftype, f.payload) for f in frames if f.ftype == ms.DMR_DATA2 and len(f.payload) == 1 + rf.DMR_BURST])[2]
    src = slot["headers"][0]["src"] if slot["headers"] else 0
    verdict = rif5.judge_tx_dmr(frames, sent, src, dst, color_code, mode_check=not fixed)
    problems, findings, facts = list(verdict.problems), list(verdict.findings), dict(verdict.facts)
    if fixed:
        more = fixed_mode_problems(frames, earlier)
        problems += more[0]
        findings += more[1]
        facts.update(more[2])
    letters_ = dmr_tx_letters(frames)
    nb = len(sent) // 3
    want = (LETTERS * (nb // 6 + 1))[:nb]
    facts.update(src=src, letters_head=letters_[:24], letters_n=len(letters_), bursts_wanted=len(want))
    if letters_ != want:
        at = next((i for i, (x, y) in enumerate(zip(letters_, want)) if x != y), min(len(letters_), len(want)))
        problems.append("voice burst letters differ from %d bursts A..F in order: %d bursts, first difference at #%d (got %r, wanted %r)" % (
            len(want), len(letters_), at, letters_[at:at + 8], want[at:at + 8]))
    return Checked(problems, findings, facts)
