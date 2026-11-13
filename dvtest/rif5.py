# SPDX-License-Identifier: MIT
"""Pure judgements of the RIF5 cases (DV-Router `mmdvm-serial` radio adapter; cases/test_rif5_mmdvm_serial.py, docs/rif5-cases.md).
No sockets, no clock, nothing that needs the rig; unit tests: cases/unit/test_rif5_lib.py. The fake modem and the envelope are in
dvtest/mmdvm_serial.py.

Every judge takes the host frames the fake modem logged (mmdvm_serial.HostFrame: t, mono, ftype, payload) and returns a Verdict:

  problems  statements of DV-Router's documents (docs/design/radio.md 7.1-7.3, config.md "[edges.radio]", status.md) that the frames
            break: a case asserts this list is empty
  findings  a difference the documents do not settle (written as a FINDING line, never asserted)
  facts     numbers for the RECORD line

Sources are cited as "radio.md 7.1" (DV-Router docs/design/radio.md at c017e491, section number) and file:line of the Rust where the
document refers to it.
"""
import types
from collections import Counter, namedtuple

from . import crossaudio, metrics
from . import mmdvm_rf as rf
from . import mmdvm_serial as ms
from .proto import dmr as dmrp
from .proto import nxdn

Verdict = namedtuple("Verdict", "problems findings facts")

GATE = "DVT_RIF5"                       # on: the RIF5 cases run (rig/compose.yml passes it; dvtest/target.py documents it)
# the rig shape of rig/config/dv-router/rif5-mmdvm-udp.toml
PORT_NAME = "rif5-modem"
STATION = "N0CALL"
ROOMS = {"dstar": "P", "dmr": "Q", "ysf": "R"}
MODEM_PORT = 3340                       # the tester side of the UDP byte port ([[edges.radio.port]] udp)
EDGE_PORT = 3341                        # the SUT side (udp_bind)
CHILD = "dv-edge-radio"                 # the launcher child (admin status launcher.children[].name)
LEVEL_OFFSETS_UNSTATED = (15, 16, 18, 19)   # SET_CONFIG v2: P25, NXDN, POCSAG, FM levels (radio.md 7.1 names them as levels, gives no percent)
DEFAULT_LEVEL_PERCENT = 50
# the observer fields of a port row (radio.md 12.3 RadioPortInfo scope obs, status.md "Radio port telemetry"; fabric OBSERVER_FIELDS)
OBSERVER_FIELDS = ("name", "kind", "module", "pattern", "modes", "mode", "duplex", "tx", "band", "state", "tx_state", "cos", "ptt", "phys_busy",
                   "fence", "guard_ms", "rssi_dbm", "rx_freq_hz", "tx_freq_hz")
GUARD_S = 13.0                          # radio.md 10.4 rule 4: G, the startup guard
POLL_S = 0.25                           # radio.md 7.1: GET_STATUS every 250 ms
INACTIVE_S = 2.0                        # radio.md 7.1: 2000 ms without a valid status after Up gives Down
VERSION_TRIES, VERSION_GAP_S = 6, 1.8   # radio.md 7.1: at most six GET_VERSION attempts 1800 ms apart

# what must not reach a modem while the RF gate is closed: voice, EOT, CW ID, the DMR slot 1 / duplex controls
RF_TYPES = frozenset((ms.SEND_CWID, 0x10, 0x11, 0x12, 0x13, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x20, 0x21))
SETUP_TYPES = frozenset((ms.GET_VERSION, ms.GET_STATUS, ms.SET_CONFIG, ms.SET_FREQ))


def verdict(problems=None, findings=None, **facts):
    return Verdict(list(problems or []), list(findings or []), facts)


def gate_on(environ):
    return str(environ.get(GATE, "")).strip().lower() == "on"


ENV_FW = "DVT_RIF5FW"                   # on: the fake modem is firmware-like (dvtest/mmdvm_fw.py): FIFO bytes, preamble, NAK 5, underrun
ENV_FW_PROFILE = "DVT_RIF5FW_PROFILE"   # mmdvm (default) or hs: the FIFO sizes and the D-STAR preamble of that board
ENV_FW_DELAY = "DVT_RIF5FW_TXDELAY_MS"  # the TX delay of the preamble; unset: the SET_CONFIG value of the row (0-500)
ENV_J = "DVT_RIF5J"                     # on: the image has RIF5j (flow control, O3 frequencies, RXD silence fill): the fix-image cases run


def _switch(environ, name):
    return str(environ.get(name, "")).strip().lower() == "on"


def fw_on(environ):
    return _switch(environ, ENV_FW)


def j_on(environ):
    return _switch(environ, ENV_J)


def fw_settings(environ):
    """(profile, tx_delay_ms or None) of the firmware-like fake from the environment."""
    profile = str(environ.get(ENV_FW_PROFILE, "") or "mmdvm").strip().lower()
    raw = str(environ.get(ENV_FW_DELAY, "") or "").strip()
    return profile, (int(raw) if raw.isdigit() else None)


# --- the port row of the rendered config -----------------------------------------------------------------------------

def _addr(text):
    host, _, port = str(text).rpartition(":")
    return (host, int(port)) if host and port.isdigit() else None


def port_from_config(doc, name=None):
    """The first `mmdvm-serial` row with a `udp` address of a rendered dv-router.toml (a dict), as a namespace; None without one.
    `name` picks the row by name. Keys follow config.md "[edges.radio]" (defaults in the same table)."""
    radio = ((doc or {}).get("edges") or {}).get("radio")
    if not isinstance(radio, dict) or not radio.get("enable"):
        return None
    for row in radio.get("port") or []:
        if row.get("kind") != "mmdvm-serial" or not row.get("udp") or (name and row.get("name") != name):
            continue
        levels, dstar, dmr, ysf = (row.get(k) or {} for k in ("levels", "dstar", "dmr", "ysf"))
        hang = row.get("hang") or {}
        return types.SimpleNamespace(
            name=row.get("name"), callsign=str(radio.get("callsign", "")).upper(), tx=row.get("tx"), tx_enable=bool(radio.get("tx_enable")),
            peer=_addr(row["udp"]), bind=_addr(row.get("udp_bind", "")), modes=dict(row.get("modes") or {}),
            rx_freq=row.get("rx_freq"), tx_freq=row.get("tx_freq"), model=row.get("model", "generic"), physical=row.get("physical", ""),
            levels={"rx_level": levels.get("rx_level", 50), "tx_level": levels.get("tx_level", 50),
                    "cw_id_level": levels.get("cw_id_level", 50), "rf_level": levels.get("rf_level", 100),
                    "dstar_tx_level": levels.get("dstar_tx_level"), "dmr_tx_level": levels.get("dmr_tx_level"),
                    "ysf_tx_level": levels.get("ysf_tx_level"), "tx_delay_ms": levels.get("tx_delay_ms", 100),
                    "tx_dc": levels.get("tx_dc_offset", 0), "rx_dc": levels.get("rx_dc_offset", 0),
                    "rx_invert": bool(levels.get("rx_invert")), "tx_invert": bool(levels.get("tx_invert")),
                    "ptt_invert": bool(levels.get("ptt_invert"))},
            rpt_module=str(dstar.get("rpt_module", "B")), color_code=dmr.get("color_code", 1), dmr_delay=dmr.get("delay", 0),
            dmr_tg=dmr.get("tg", 9), ysf_lo_dev=bool(ysf.get("lo_dev")), use_cos_as_lockout=bool(row.get("use_cos_as_lockout")),
            rf_hang=hang.get("rf_mode_secs", 10), net_hang=hang.get("net_mode_secs", 3),
            id_mode=(row.get("id") or {}).get("mode", "cw"))
    return None


def expected_setup(port):
    """(SET_FREQ bytes, {offset: value} of the documented SET_CONFIG offsets) of a port namespace (port_from_config)."""
    lv = port.levels
    freq = ms.expected_set_freq(port.rx_freq or 0, port.tx_freq or 0, lv["rf_level"])
    modes = [m for m in ("dstar", "dmr", "ysf") if m in port.modes]
    cfg = ms.expected_set_config_v2(
        modes=modes, tx_delay_ms=lv["tx_delay_ms"], rx_level=lv["rx_level"], tx_level=lv["tx_level"], cw_id_level=lv["cw_id_level"],
        dstar_tx_level=lv["dstar_tx_level"], dmr_tx_level=lv["dmr_tx_level"], ysf_tx_level=lv["ysf_tx_level"], tx_dc=lv["tx_dc"],
        rx_dc=lv["rx_dc"], color_code=port.color_code, dmr_delay=port.dmr_delay, rx_invert=lv["rx_invert"], tx_invert=lv["tx_invert"],
        ptt_invert=lv["ptt_invert"], ysf_lo_dev=port.ysf_lo_dev, use_cos_as_lockout=port.use_cos_as_lockout)
    return freq, cfg


# --- the log, cut into dialogues ---------------------------------------------------------------------------------------

Dialogue = namedtuple("Dialogue", "frames versions freq config statuses")


def dialogues(frames, gap=3.5):
    """Split a host frame log into open dialogues. A GET_VERSION starts a new one when the previous dialogue already went past the
    version step (it has a SET_FREQ, SET_CONFIG or GET_STATUS) or its last GET_VERSION is more than `gap` seconds old (the retries are
    1.8 s apart; the next try of a failed open comes at least 1.8 + 2 + 2 s later). Frames before the first GET_VERSION are dropped."""
    out, cur = [], None
    for f in frames:
        if f.ftype == ms.GET_VERSION:
            progressed = cur is not None and (cur["freq"] or cur["config"] or cur["statuses"])
            if cur is None or progressed or f.mono - cur["versions"][-1].mono > gap:
                cur = {"frames": [], "versions": [], "freq": None, "config": None, "statuses": []}
                out.append(cur)
            cur["versions"].append(f)
        if cur is None:
            continue
        cur["frames"].append(f)
        if f.ftype == ms.SET_FREQ and cur["freq"] is None:
            cur["freq"] = f
        elif f.ftype == ms.SET_CONFIG and cur["config"] is None:
            cur["config"] = f
        elif f.ftype == ms.GET_STATUS:
            cur["statuses"].append(f)
    return [Dialogue(d["frames"], d["versions"], d["freq"], d["config"], d["statuses"]) for d in out]


def wire(f):
    """The whole frame (envelope included) of a HostFrame."""
    return ms.encode_frame(f.ftype, f.payload)


def judge_open(dialogue, port):
    """The open dialogue of radio.md 7.1: GET_VERSION first, then SET_FREQ, then SET_CONFIG (v2, 40 B), then GET_STATUS; the two setup frames
    carry the row's values at the documented offsets; no voice or CW ID byte before the first status."""
    problems, findings, facts_open = [], [], {}
    if not dialogue.versions:
        return verdict(["no GET_VERSION in the dialogue"])
    if len(dialogue.versions) > VERSION_TRIES:
        problems.append("%d GET_VERSION attempts, at most %d (radio.md 7.1)" % (len(dialogue.versions), VERSION_TRIES))
    gaps = [b.mono - a.mono for a, b in zip(dialogue.versions, dialogue.versions[1:])]
    bad = [round(g, 2) for g in gaps if not VERSION_GAP_S - 0.35 <= g <= VERSION_GAP_S + 0.35]
    if bad:
        problems.append("GET_VERSION retries %s s apart, the document says 1.8 s" % bad)
    order = [f.ftype for f in dialogue.frames if f.ftype not in (ms.GET_VERSION,)]
    want_head = [ms.SET_FREQ, ms.SET_CONFIG, ms.GET_STATUS]
    if order[:3] != want_head:
        problems.append("after GET_VERSION the frames are %s, the document says SET_FREQ, SET_CONFIG, GET_STATUS" %
                        [ms.name_of(t) for t in order[:3]])
    if dialogue.freq is not None:
        want, _ = expected_setup(port)
        got = wire(dialogue.freq)
        if got != want:
            problems.append("SET_FREQ is %s, the document gives %s" % (got.hex(), want.hex()))
        info = ms.parse_set_freq(dialogue.freq.payload)
        if info and info["pocsag_hz"] != info["tx_hz"]:
            problems.append("SET_FREQ POCSAG Hz %d is not the adjusted TX %d" % (info["pocsag_hz"], info["tx_hz"]))
    if dialogue.config is not None:
        _, want_cfg = expected_setup(port)
        got = wire(dialogue.config)
        if len(got) != ms.V2_FRAME_LEN:
            problems.append("SET_CONFIG is %d bytes, a version 2 modem is sent 40 (radio.md 7.1)" % len(got))
        else:
            diff = ms.set_config_differences(got, want_cfg)
            if diff:
                problems.append("SET_CONFIG differs at (offset, got, wanted): %s" % diff)
            if not got[3] & 0x80:
                problems.append("SET_CONFIG lacks the simplex flag 0x80 (radio.md 7.1: both versions always set it)")
            # radio.md 7.1: 12-16 are mode levels, 18 / 19 the POCSAG / FM levels, and "levels use (percent * 255 + 50) / 100". The percent of the
            # P25, NXDN, POCSAG and FM levels is not stated; the default level of the other mode levels is 50 %, pct(50) = 128. A value that is
            # that default is consistent with the level rule (a RECORD fact); anything else is a FINDING.
            unnamed = [(at, got[at]) for at in LEVEL_OFFSETS_UNSTATED]
            odd = [(at, value) for at, value in unnamed if value != ms.pct(DEFAULT_LEVEL_PERCENT)]
            facts_open["unstated_level_bytes"] = dict(unnamed)
            if odd:
                findings.append("SET_CONFIG level bytes the document gives no percent for (offset, value): %s; the default level 50 %% would be %d" % (
                    odd, ms.pct(DEFAULT_LEVEL_PERCENT)))
    before = []
    for f in dialogue.frames:
        if f.ftype == ms.GET_STATUS:
            break
        if f.ftype in RF_TYPES or f.ftype == ms.SET_MODE:
            before.append(ms.name_of(f.ftype))
    if before:
        problems.append("frames %s before the first GET_STATUS (radio.md 7.1: no frame transmits voice during open)" % before)
    return verdict(problems, findings, versions=len(dialogue.versions), statuses=len(dialogue.statuses), **facts_open)


def status_cadence(frames, since=None, until=None):
    """GET_STATUS intervals (seconds) in a window. Judged: at least 8 intervals, median 0.20 to 0.32 s (radio.md 7.1: every 250 ms), no
    interval above 0.8 s (a dropped poll would be 0.5 s, two 0.75 s)."""
    times = [f.mono for f in frames if f.ftype == ms.GET_STATUS and (since is None or f.mono >= since) and (until is None or f.mono < until)]
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    if len(gaps) < 8:
        return verdict(["only %d GET_STATUS intervals in the window, wanted 8 or more" % len(gaps)], polls=len(times))
    median = gaps[len(gaps) // 2]
    problems = []
    if not 0.20 <= median <= 0.32:
        problems.append("median GET_STATUS interval %.3f s, the document says 0.250 s" % median)
    if gaps[-1] > 0.8:
        problems.append("longest GET_STATUS interval %.3f s" % gaps[-1])
    return verdict(problems, polls=len(times), median=round(median, 3), shortest=round(gaps[0], 3), longest=round(gaps[-1], 3))


# --- the startup guard ----------------------------------------------------------------------------------------------

def judge_guard(frames, t0, t1):
    """radio.md 7.2, 7.3 and 10.4: while the RF gate is closed (the startup guard, G = 13 s after the first successful probe) nothing that
    keys the modem is written: no voice, EOT, CW ID or DMR control frame. [t0, t1) in monotonic seconds. A SET_MODE in the window is a
    finding, not a problem: the document says "no keying bytes during a fence", and the code withholds SET_MODE while the gate is closed
    (core.rs `control`: `kind == SET_MODE && !rf_allowed`), but SET_MODE does not key."""
    window = [f for f in frames if t0 <= f.mono < t1]
    rf_frames = [f for f in window if f.ftype in RF_TYPES]
    modes = [f for f in window if f.ftype == ms.SET_MODE]
    other = sorted({f.ftype for f in window if f.ftype not in RF_TYPES and f.ftype not in SETUP_TYPES and f.ftype != ms.SET_MODE})
    problems = ["%s at +%.2f s inside the guard window" % (ms.name_of(f.ftype), f.mono - t0) for f in rf_frames]
    findings = []
    if modes:
        findings.append("SET_MODE %s written inside the guard window at +%s s" % ([f.payload.hex() for f in modes], [round(f.mono - t0, 2) for f in modes]))
    if other:
        findings.append("frame types %s inside the guard window" % [ms.name_of(t) for t in other])
    counts = {}
    for f in window:
        counts[ms.name_of(f.ftype)] = counts.get(ms.name_of(f.ftype), 0) + 1
    return verdict(problems, findings, window_s=round(t1 - t0, 2), frames=counts)


# --- an over from a room to the modem --------------------------------------------------------------------------------

def _native(frames, types_):
    return [f for f in frames if f.ftype in types_]


def align(sent, got):
    """For each word of `got`, in order, the index of the word of `sent` it equals (searched forward from the previous match), or None for a word
    that is not in `sent` after the previous match (a changed word). The host queue of the adapter drops the OLDEST words of a mode when it holds more
    than 16 (radio.md 7.1 "Flow control"), so a long over reaches the modem as a subsequence of the words sent: this is the alignment that tells a
    dropped word (a gap in the indices) from a changed one (None) and gives every written frame the position of the word it carries."""
    out, at = [], 0
    for word in got:
        found = next((j for j in range(at, len(sent)) if sent[j] == word), None)
        out.append(found)
        if found is not None:
            at = found + 1
    return out


def _word_check(label, sent, got, problems, findings, facts, lossy=False):
    """The words written to the modem against the words sent. A changed word is always a problem. A word missing (dropped by the host queue) is a
    problem for a short over (`lossy` False: the over fits what the flow control carries) and a FINDING for a long one. Words AFTER the last sent word
    that was found are `extra` (a fact, with the first one in hex): the end unit the room renders as a frame. Returns align(sent, got) (None for a
    changed or an extra word)."""
    sent, got = list(sent), list(got)
    facts["%s_sent" % label], facts["%s_written" % label] = len(sent), len(got)
    index = align(sent, got)
    kept = [j for j in index if j is not None]
    last = kept[-1] if kept else -1
    last_pos = max((i for i, j in enumerate(index) if j is not None), default=-1)
    changed = [i for i, j in enumerate(index) if j is None and i < last_pos]
    extra = [i for i, j in enumerate(index) if j is None and i > last_pos]
    if extra:
        facts["extra_first_hex"] = got[extra[0]].hex()
        facts["extra_all_equal"] = len({got[i] for i in extra}) == 1
    seen = set(kept)
    gaps = [j for j in range(last + 1) if j not in seen]
    tail = len(sent) - 1 - last
    if changed:
        problems.append("%s word %d written to the modem differs from the one sent" % (label, changed[0]))
    if gaps:
        facts["%s_dropped" % label] = len(gaps) + tail
        text = ("%s word %d written to the modem differs from the one sent: %d sent word(s) are not written before the end of the over, first at sent "
                "index %d (the host queue drops the oldest words when it holds more than 16, radio.md 7.1 flow control)" % (label, gaps[0], len(gaps), gaps[0]))
        (findings if lossy else problems).append(text)
    elif tail > 0 and not changed:
        facts["%s_dropped" % label] = tail
        text = "%d %s words written, %d sent (the first %d agree)" % (len(got) - len(extra), label, len(sent), len(got) - len(extra))
        (findings if lossy else problems).append(text)
    return index


def _foreign(frames, mode, problems):
    """No frame of another mode, no CW ID (the snippet switches the ID off), no DMR slot 1 or duplex control."""
    own = ms.TX_TYPES[mode]
    stray = sorted({ms.name_of(f.ftype) for f in frames if f.ftype in RF_TYPES and f.ftype not in own})
    if stray:
        problems.append("frames %s written during a %s over (one mode per over, radio.md 7.2; no CW ID with id.mode none; "
                        "simplex DMR sends no START, ABORT or SHORTLC, radio.md 7.3)" % (stray, mode))


def _first_mode(frames, mode, problems, facts=None):
    """radio.md 7.2: the circuit claims its mode with SET_MODE before PTT. A SET_MODE 00 may come BEFORE the claim: "a multi-mode modem returns to
    idle (SET_MODE 0) after its TX buffer has drained and the hang expired", and that hang of the PREVIOUS over (hang.net_mode_secs) can end while
    the next over starts. Anything else before the claim is a problem, and so is an idle between the claim and the last native frame ("mode switching
    never interrupts a stream")."""
    modes = [f for f in frames if f.ftype == ms.SET_MODE]
    want = bytes((ms.MODE_ID[mode],))
    claim = next((f for f in modes if f.payload == want), None)
    if claim is None:
        problems.append("no SET_MODE %02x among the SET_MODE frames %s (the circuit claims its mode first, radio.md 7.2)" % (
            ms.MODE_ID[mode], [f.payload.hex() for f in modes]))
        return
    leading = [f for f in modes if f.mono < claim.mono]
    odd = [f for f in leading if f.payload != bytes((ms.MODE_IDLE,))]
    if odd:
        problems.append("SET_MODE %s written before the claim %02x: only an idle (00, the hang of the previous over) may precede it (radio.md 7.2)" % (
            [f.payload.hex() for f in odd], ms.MODE_ID[mode]))
    if leading and facts is not None:
        facts["leading_idle_s"] = [round(claim.mono - f.mono, 3) for f in leading]
    natives = [f for f in frames if f.ftype in ms.TX_TYPES[mode]]
    if natives and claim.mono > natives[0].mono:
        problems.append("a %s frame was written before SET_MODE" % mode)
    inside = [f for f in modes if f.mono > claim.mono and natives and f.mono < natives[-1].mono and f.payload == bytes((ms.MODE_IDLE,))]
    if inside:
        problems.append("SET_MODE 00 written %.2f s before the last native frame of the over (radio.md 7.2: mode switching never interrupts a stream)" % (
            natives[-1].mono - inside[0].mono))


def judge_tx_dstar(frames, sent, my, station=STATION, rpt_module="B", lossy=False):
    """A D-STAR over from the room: SET_MODE 1, one header (41 B, repeater flag clear, RPT2 = station + RF module, RPT1 = station + G, UR CQCQCQ,
    MY the talker, CRC), the 9 byte voice words in order with the sync triple on the frames whose position in the over is 0 mod 21, one EOT
    (radio.md 7.1, 7.3; the wire literal of DV-Router's own test crates/router/tests/real_children/radio/mmdvm.rs TX_HEADER).

    The position of a frame is the position of the word it carries in the words SENT (mode/dstar.rs `tx_data(.., position)` takes the position of the
    unit in the over, not the number of frames written): when the host queue drops words (`lossy`, a long over) the frame written at index 42 is not
    the one at position 42. `lossy` False: a dropped word is a problem (the over was sized to fit the flow control); True: a FINDING."""
    problems, findings, facts = [], [], {}
    _first_mode(frames, "dstar", problems, facts)
    _foreign(frames, "dstar", problems)
    headers = _native(frames, (ms.DSTAR_HEADER,))
    data = _native(frames, (ms.DSTAR_DATA,))
    eots = _native(frames, (ms.DSTAR_EOT,))
    if len(headers) != 1:
        problems.append("%d D-STAR headers, one per over (radio.md 7.3)" % len(headers))
    for h in headers[:1]:
        if len(h.payload) != 41:
            problems.append("header payload %d bytes, native size 41 (radio.md 7.1)" % len(h.payload))
            continue
        info = rf.parse_dstar_header(h.payload)
        want = {"flags": (0, 0, 0), "rpt2": (station.ljust(7) + rpt_module), "rpt1": (station.ljust(7) + "G"), "ur": "CQCQCQ  "}
        for key, value in want.items():
            if info[key] != value:
                problems.append("header %s is %r, wanted %r (radio.md 7.3: egress rewrites RPT1 to station + G, RPT2 to station + RF module, UR CQCQCQ, "
                                "repeater flag clear)" % (key, info[key], value))
        if info["my"].strip() != my:
            problems.append("header MY is %r, the talker is %r" % (info["my"].strip(), my))
        if not info["crc_ok"]:
            problems.append("header CRC is wrong")
    bad_len = [len(f.payload) for f in data if len(f.payload) != 12]
    if bad_len:
        problems.append("D-STAR data frames of %s bytes, native size 12" % sorted(set(bad_len)))
    whole = [f for f in data if len(f.payload) == 12]
    voice = [bytes(f.payload[:9]) for f in whole]
    index = _word_check("voice", sent, voice, problems, findings, facts, lossy)
    for i, f in enumerate(whole):
        position = index[i] if index[i] is not None else i          # a changed word is a problem already; judge it where it stands
        if position % 21 == 0 and bytes(f.payload[9:]) != rf.DSTAR_SYNC:
            problems.append("data frame %d (position %d in the over, frame 0 of a superframe) carries slow data %s, not the sync triple 55 2D 16 "
                            "(radio.md 7.3)" % (i, position, f.payload[9:].hex()))
            break
    if len(eots) != 1:
        problems.append("%d D-STAR EOT frames, one per normal end (radio.md 10.3)" % len(eots))
    elif eots[0].payload != b"" or (data and eots[0].mono < data[-1].mono):
        problems.append("the EOT is not an empty frame after the last data frame")
    if headers and data and data[0].mono < headers[0].mono:
        problems.append("a data frame before the header")
    facts["extra_words"] = max(0, len(voice) - len(sent))
    return Verdict(problems, findings, facts)


def burst_center(burst):
    """The 48 bits at the middle of a 33 byte DMR burst (the sync / embedded signalling field) as an int."""
    return rf._burst_center(bytes(burst))


def judge_tx_dmr(frames, sent, src, dst, color_code, headers_n=8, terminators_n=3, lossy=False):
    """A DMR over from the room (radio.md 7.3 "Built DMR simplex"): SET_MODE 2, every frame `E0 25 1A 00` + a 33 B burst, eight voice LC headers
    (MS data sync, LC group src -> dst, the row's colour code), the voice bursts A to F (A with the MS voice sync, three words per burst),
    three terminators with LC; no slot 1 or duplex control. The letter of a burst is that of the burst of the words SENT it carries (a burst the host
    queue dropped leaves a gap in the letters, it does not renumber the next one); `lossy` as in judge_tx_dstar."""
    problems, findings, facts = [], [], {}
    _first_mode(frames, "dmr", problems, facts)
    _foreign(frames, "dmr", problems)
    bursts = _native(frames, (ms.DMR_DATA2,))
    short = sorted({len(f.payload) for f in bursts if len(f.payload) != 34})
    if short:
        problems.append("DMR frames of payload %s bytes, native size 34 (radio.md 7.1)" % short)
    ctl = sorted({f.payload[0] for f in bursts if f.payload})
    if ctl != [0] and bursts:
        problems.append("control byte of the DMR frames %s, the envelope is always `E0 25 1A 00` (radio.md 7.3)" % ctl)
    good = [f for f in bursts if len(f.payload) == 34]
    pairs = [(ms.DMR_DATA2, bytes(f.payload)) for f in good]
    slot = rf.decode_dmr(pairs)[2]
    facts.update(bursts=len(good), headers=len(slot["headers"]), terminators=len(slot["terminators"]), voice_bursts=slot["voice_bursts"],
                 other=slot["other"])
    if len(slot["headers"]) != headers_n:
        problems.append("%d voice LC headers, %d expected (radio.md 7.3: eight, MH/DMRSlot.cpp:70)" % (len(slot["headers"]), headers_n))
    if len(slot["terminators"]) != terminators_n:
        problems.append("%d terminators, %d expected (radio.md 7.3)" % (len(slot["terminators"]), terminators_n))
    for kind in ("headers", "terminators"):
        for lc in slot[kind]:
            if (lc["src"], lc["dst"], lc["flco"], lc["cc"]) != (src, dst, 0, color_code) or not lc["rs_ok"]:
                problems.append("%s LC is src %s dst %s flco %s colour code %s rs_ok %s, wanted src %s dst %s group colour code %s" % (
                    kind[:-1], lc["src"], lc["dst"], lc["flco"], lc["cc"], lc["rs_ok"], src, dst, color_code))
                break
    kinds = []
    for f in good:
        burst = bytes(f.payload[1:])
        kinds.append(rf.classify_burst(burst))
    lead = kinds[:headers_n]
    tail = kinds[-terminators_n:] if len(kinds) >= terminators_n else []
    if any(k != "data" for k in lead) or any(k != "data" for k in tail) or len(kinds) < headers_n + terminators_n:
        problems.append("the over is not %d data bursts, voice bursts, %d data bursts: %s" % (headers_n, terminators_n, kinds))
    voice_kinds = kinds[headers_n:len(kinds) - terminators_n] if len(kinds) >= headers_n + terminators_n else []
    index = _word_check("voice", sent, slot["voice"], problems, findings, facts, lossy)
    for i, k in enumerate(voice_kinds):
        first = index[3 * i] if 3 * i < len(index) and index[3 * i] is not None else 3 * i
        letter_index = first // 3                                   # the burst of the words sent that this burst carries
        if (letter_index % 6 == 0) != (k == "voice_sync"):
            problems.append("voice burst %d (burst %d of the words sent) is %s, burst A of a superframe (index %% 6 == 0) alone carries the voice sync" % (
                i, letter_index, k))
            break
    for f in good[:headers_n] + good[-terminators_n:]:
        if burst_center(f.payload[1:]) != int.from_bytes(rf.MS_DATA_SYNC, "big"):
            problems.append("a header or terminator burst does not carry the MS data sync D5 D7 F7 7F D7 57 (radio.md 7.3)")
            break
    voice_bursts = good[headers_n:len(good) - terminators_n] if len(good) >= headers_n + terminators_n else []
    if voice_bursts and burst_center(voice_bursts[0].payload[1:]) != int.from_bytes(rf.MS_VOICE_SYNC, "big"):
        problems.append("burst A does not carry the MS voice sync 7F 7D 5D D5 7D FD (radio.md 7.3)")
    return Verdict(problems, findings, facts)


YSF_GAIN_STEPS = ("p10", "m04")         # YSF ingress +1.0 dB, YSF egress -0.4 dB (decisions D1, RIF5 D3; media-format.md YSF)


def compare_ysf_words(sent, got, lossy=False):
    """The YSF voice words written to the modem (TX) or heard by a YSF client (RX) against the words sent.

    A YSF over does not come back bit exact: the YSF ingress steps the AMBE+2 gain index b2 by +1.0 dB and the egress by -0.4 dB (DV-Router
    decisions.md D1, and for the radio edge RIF5 D3: "the existing 49-bit to 72-bit layout and YSF gain-index steps only"; echo.md: "the talker hears
    every AMBE+2 bit unchanged except the gain index b2"; the same rule is dvtest/proto/ysf.py's and cases/test_clients2.py's). The words are aligned
    on everything but b2 (a word the host queue dropped leaves a gap, it does not shift the pairs after it). Problems: a word that differs in any bit but
    b2, a word missing (a FINDING when `lossy`). A b2 that is not the documented map (dvtest.crossaudio P10 then M04) is a FINDING, with the histogram
    of the steps in the facts. Words after the sent ones are a fact (`extra_words`, the first in hex)."""
    sent, got = list(sent), list(got)
    problems, findings, facts = [], [], {}
    key = lambda w: nxdn.without_gain(nxdn.from_dmr(w))           # noqa: E731
    skeys, gkeys = [key(w) for w in sent], [key(w) for w in got]
    inner = []
    index = _word_check("voice", skeys, gkeys, inner, findings, facts, lossy)
    for line in inner:
        problems.append(line.replace("differs from the one sent", "differs from the one sent in a bit other than the gain index b2", 1)
                        if "differs from the one sent" in line and "not written" not in line else line)
    facts["voice_sent"], facts["voice_written"] = len(sent), len(got)
    facts.pop("extra_first_hex", None)
    pairs = [(sent[j], got[i]) for i, j in enumerate(index) if j is not None]
    steps = Counter(nxdn.gain_index(nxdn.from_dmr(g)) - nxdn.gain_index(nxdn.from_dmr(s)) for s, g in pairs)
    facts["b2_steps"] = {str(k): v for k, v in sorted(steps.items())}
    if pairs and not any("differs" in p for p in problems):
        check = crossaudio.gain_check([s for s, _ in pairs], [g for _, g in pairs], YSF_GAIN_STEPS)
        if check["bad"]:
            findings.append("b2 of %d of %d words is not the documented map (+1.0 dB in, -0.4 dB out); first (index, sent b2, got b2, wanted b2): %s; "
                            "steps seen %s" % (len(check["bad"]), len(pairs), check["bad"][0][:4], facts["b2_steps"]))
    extra = [i for i, j in enumerate(index) if j is None and i > max((k for k, j in enumerate(index) if j is not None), default=-1)]
    facts["extra_words"] = len(extra)
    if extra:
        facts["extra_first_hex"] = got[extra[0]].hex()
        facts["extra_all_equal"] = len(set(got[i] for i in extra)) == 1
    return Verdict(problems, findings, facts)


def judge_tx_ysf(frames, sent, src, lo_dev=False, lossy=False):
    """A YSF V/D mode 2 over from the room (radio.md 7.3 "Built YSF"): SET_MODE 3, frames `E0 7C 20 00` + 120 B, one header and one terminator,
    FICH data type 2 (V/D mode 2), the Dev flag the row's `ysf.lo_dev`, five 9 byte words per voice frame, the header names the talker."""
    problems, findings, facts = [], [], {}
    _first_mode(frames, "ysf", problems, facts)
    _foreign(frames, "ysf", problems)
    native = _native(frames, (ms.YSF_DATA,))
    short = sorted({len(f.payload) for f in native if len(f.payload) != 121})
    if short:
        problems.append("YSF frames of payload %s bytes, native size 121 (radio.md 7.1)" % short)
    if native and sorted({f.payload[0] for f in native if f.payload}) != [0]:
        problems.append("the flag byte of the YSF frames is not 00 (radio.md 7.3: `E0 7C 20 00`)")
    good = [f for f in native if len(f.payload) == 121]
    decoded = rf.decode_ysf([(ms.YSF_DATA, bytes(f.payload)) for f in good])
    facts.update(frames=len(good), headers=len(decoded["headers"]), terminators=len(decoded["terminators"]))
    if len(decoded["headers"]) != 1 or len(decoded["terminators"]) != 1:
        problems.append("%d headers and %d terminators, one of each (radio.md 7.3)" % (len(decoded["headers"]), len(decoded["terminators"])))
    bad_fich = sum(1 for fich in decoded["fich"] if fich is None)
    if bad_fich:
        problems.append("%d YSF frames whose FICH does not decode" % bad_fich)
    fiches = [fich for fich in decoded["fich"] if fich]
    if any(fich["dt"] != 2 for fich in fiches):
        problems.append("a FICH data type other than V/D mode 2 (radio.md 7.3)")
    if any(fich["dev"] != bool(lo_dev) for fich in fiches):
        problems.append("the FICH Dev flag is not the row's ysf.lo_dev = %s (radio.md 7.3)" % lo_dev)
    if fiches and (fiches[0]["fi"], fiches[-1]["fi"]) != (0, 2):
        problems.append("the first YSF frame is FI %s and the last FI %s, wanted header (0) and terminator (2)" % (fiches[0]["fi"], fiches[-1]["fi"]))
    for item in decoded["headers"][:1]:
        if (item["src"] or "").strip() != src:
            problems.append("the header names %r as the source, the talker is %r" % ((item["src"] or "").strip(), src))
    words = compare_ysf_words(sent, decoded["voice"], lossy)
    problems += words.problems
    findings += words.findings
    facts.update(words.facts)
    return Verdict(problems, findings, facts)


JUDGE_TX = {"dstar": judge_tx_dstar, "dmr": judge_tx_dmr, "ysf": judge_tx_ysf}


def dmr_damaged_copy(src, dst, color_code):
    """A voice LC header copy of the modem (control byte 41, data-sync burst) whose slot type carries the colour code `color_code + 1`: the adapter's
    Golay decode of the slot type does not match the row's colour code, so the copy fails its checks (radio.md 7.3: "A header or terminator copy that
    fails its checks inside an open over is ignored ... and refreshes the native-over timeout"; mode/dmr.rs `parse`)."""
    lc = dmrp.full_lc(dmrp.FLCO_GROUP, src, dst)
    return bytes((0x41,)) + rf.data_burst(lc, rf.DT_VOICE_LC_HEADER, (color_code + 1) % 16)


EDGE_SILENCE_S = 0.2                    # radio.md 4.4: SILENCE = 200 ms for the radio edge (ten units); crates/edge/radio/src/protocol.rs
DMR_FILL_LIMIT = 13                     # RIF5j: silence fills in a row before the over ends as LOST (M/DMRDMORX.cpp:33,194-200; hub_dv.rs `rf_fill`)
YSF_FILL_LIMIT = 5                      # (M/YSFRX.cpp:42,185-203)
RXD_J_END_S = (0.2, 0.5)               # RIF5j: the room over ends 300 ms after the last damaged copy (a rig margin of one tick and the packet path)
NATIVE_OVER_TIMEOUT_S = 0.3             # radio.md 7.1: an RF over also ends 300 ms without a native frame (hub.rs `dv_last_ns`)


# --- a modem that goes silent ---------------------------------------------------------------------------------------

def judge_silence(frames, t_mute, tries=False):
    """radio.md 7.1 "Polling": the host polls every 250 ms, a valid status refreshes inactivity, 2000 ms without one after Up gives
    Down/inactive, closes the link and reopens after a backoff (2 s doubling to 60 s) with the whole dialogue.

    From the log of a modem that stopped answering (or answers with a short status) at `t_mute`: the polls the host went on sending, the time its
    last poll came after the last status the modem answered (about 2 s: the host does not stop at the first missed poll), and the silence that
    follows until the next GET_VERSION (the closed link, the backoff, the settle of the new dialogue). `tries`: the modem stays silent for the
    reopen too, so the six GET_VERSION tries 1.8 s apart are counted (a finding when there are not six)."""
    after = [f for f in frames if f.mono >= t_mute]
    nxt = next((f for f in after if f.ftype == ms.GET_VERSION), None)
    polls = [f for f in after if f.ftype == ms.GET_STATUS and (nxt is None or f.mono < nxt.mono)]
    problems, findings, facts = [], [], {}
    if not polls:
        return verdict(["the host sent no GET_STATUS after the modem went silent"])
    last_answered = max([f.mono for f in frames if f.ftype == ms.GET_STATUS and f.mono < t_mute] or [t_mute])
    facts["polls_unanswered"] = len(polls)
    last_poll = max(f.mono for f in polls)
    facts["last_poll_after_last_answer_s"] = round(last_poll - last_answered, 2)
    if not INACTIVE_S - 0.6 <= last_poll - last_answered <= INACTIVE_S + 0.3:
        problems.append("the host's last poll came %.2f s after the last status it got; the document says Down after 2000 ms without a valid status" %
                        (last_poll - last_answered))
    if len(polls) < 5:
        problems.append("only %d unanswered polls before Down, 250 ms polling for 2 s is about 8" % len(polls))
    if nxt is not None:
        facts["silent_s"] = round(nxt.mono - last_poll, 2)
        if nxt.mono - last_poll < 4.0 - 0.5:
            problems.append("the next dialogue started %.2f s after the last poll; the document gives a backoff of at least 2 s and a settle of 2 s" %
                            (nxt.mono - last_poll))
        versions = [f for f in after if f.ftype == ms.GET_VERSION and f.mono >= nxt.mono]
        run = [versions[0]]
        for f in versions[1:]:
            if f.mono - run[-1].mono > 3.5:
                break
            run.append(f)
        facts["version_tries"] = len(run)
        if tries and len(run) != VERSION_TRIES:
            findings.append("the first reopen sent %d GET_VERSION frames before giving up; the document says at most six, 1.8 s apart" % len(run))
    else:
        findings.append("no GET_VERSION followed within the log: the reopen had not come yet")
    return Verdict(problems, findings, facts)


def reopen_delays(frames, since, until=None):
    """[(start of dialogue, seconds since the end of the previous one)] for the dialogues in a window of a silent modem: the backoff
    sequence the host walks (2 s doubling to 60 s, plus the 2 s settle and the version tries). Used to record the doubling."""
    ds = dialogues([f for f in frames if f.mono >= since and (until is None or f.mono < until)])
    out = []
    for prev, cur in zip(ds, ds[1:]):
        out.append((round(cur.versions[0].mono - prev.frames[-1].mono, 2), len(cur.versions)))
    return out


# --- the port row in the public state / admin status -------------------------------------------------------------------

def public_port(state_body, name):
    """The `procs[].radio.ports[]` row of the public state with this port name (status.md "Radio port telemetry"), or None."""
    if not isinstance(state_body, dict):
        return None
    for proc in state_body.get("procs") or []:
        radio = proc.get("radio") if isinstance(proc, dict) else None
        for row in (radio or {}).get("ports") or []:
            if isinstance(row, dict) and row.get("name") == name:
                return row
    return None


def admin_port(admin_body, name):
    """The `radio_edges[].radio.ports[]` row of the admin status / clients reply with this port name (all fields), or None."""
    if not isinstance(admin_body, dict):
        return None
    for edge in admin_body.get("radio_edges") or []:
        for row in ((edge or {}).get("radio") or {}).get("ports") or []:
            if isinstance(row, dict) and row.get("name") == name:
                return row
    return None


# --- metrics ------------------------------------------------------------------------------------------------------

FAMILIES = ("dv_edge_radio_port_total", "dv_edge_radio_frames_total", "dv_edge_radio_modem_total", "dv_edge_radio_over_total",
            "dv_edge_radio_ptt_total", "dv_edge_radio_mode", "dv_edge_radio_modem_space")


def counters(text, port=PORT_NAME):
    """{(family, label items...): value} of the radio families of one port, for before / after deltas. None when the text has no radio family."""
    if not text:
        return None
    samples = [s for s in metrics.parse(text) if s.name in FAMILIES and s.labels.get("port") == port]
    if not samples:
        return None
    return {(s.name,) + tuple(sorted((k, v) for k, v in s.labels.items() if k != "port")): s.value for s in samples}


def delta(before, after, family, **labels):
    """Growth of the counter(s) of `family` whose labels contain `labels` between two counters() results; None when either is None."""
    if before is None or after is None:
        return None
    def pick(table):
        return sum(v for key, v in table.items() if key[0] == family and all((k, w) in key[1:] for k, w in labels.items()))
    return pick(after) - pick(before)


# --- the flow-control timeline of a TX case (out/<run>/rif5/timeline-<case>.jsonl) -----------------------------------------

TIMELINE_MAX_LINES = 2500               # a 3 s over is a few hundred lines; a longer one keeps its first lines and says so
_MODE_OF_TYPE = {ms.DSTAR_HEADER: "dstar", ms.DSTAR_DATA: "dstar", ms.DSTAR_EOT: "dstar", ms.DSTAR_LOST: "dstar", ms.DMR_DATA2: "dmr", ms.DMR_LOST2: "dmr",
                 ms.YSF_DATA: "ysf", ms.YSF_LOST: "ysf"}


def timeline_lines(events, t0, sent=None, summary=None, max_lines=TIMELINE_MAX_LINES):
    """JSON-ready dicts, in time order, for the events of Rif5Modem.events of one case. Times `t` are seconds since `t0` (monotonic, the case start).
    Every line also carries the fields of the format DV-Router proposes (design-276 1.8): `dir` (`h2m` host to modem, `m2h` modem to host), `type`, `len`,
    `event` (`write`, `pop`, `preamble_start`, `preamble_end`, `underrun`, `nak`, `status`, plus `tx_off`, `sent`), `tx` (the TX flag) and, with the
    firmware-like fake only, `fifo_bytes` per mode key.

      {"ev": "frame", "t", "dir": "h2m", "event": "write", "type", "mode", "len"}   a frame the host wrote to the modem (GET_STATUS left out); SET_MODE
                                                    also has "arg" (the mode byte, hex: 00 idle, 01 D-STAR, 02 DMR, 03 YSF)
      {"ev": "status", "t", "dir": "m2h", "event": "status", "type": "GET_STATUS", "len": 20, "mode", "flags", "tx", "space": {"dstar", "dmr1", "dmr2",
       "ysf"}, "space_triple": [dstar, dmr2, ysf], "fifo_bytes"?}   a GET_STATUS reply the fake sent, the free space as reported
      {"ev": "fw", "t", "event", ...}                 the firmware-like fake (DVT_RIF5FW): `write` / `pop` / `nak` with the FIFO bytes, `preamble_start`
                                                    (`duration_ms`), `preamble_end`, `tx_off` (the FIFO ran dry), `underrun` (`dry_s`)
      {"ev": "sent", "t", "event": "sent", "index", "est": true}   a word the room side sent; `sent` is (start, end, count): the time is ESTIMATED as an even
                                                    spread over the talk (the client paces 20 ms per frame), not observed
      {"ev": "summary", ...}                          the case's own numbers (`summary`), last line
    Anything beyond `max_lines` is cut with a {"ev": "truncated", "lines": n} line before the summary."""
    out = []
    for event in events:
        kind, t = event[0], round(event[1] - t0, 4)
        if kind == "frame":
            _, _, ftype, length, first = event
            row = {"ev": "frame", "t": t, "dir": "h2m", "event": "write", "type": ms.name_of(ftype), "mode": _MODE_OF_TYPE.get(ftype), "len": length}
            if ftype == ms.SET_MODE and first is not None:
                row["arg"] = "%02x" % first
            out.append(row)
        elif kind == "status":
            mode, flags, d, m1, m2, y = event[2:8]
            row = {"ev": "status", "t": t, "dir": "m2h", "event": "status", "type": "GET_STATUS", "len": 20, "mode": mode, "flags": flags, "tx": bool(flags & 1),
                   "space": {"dstar": d, "dmr1": m1, "dmr2": m2, "ysf": y}, "space_triple": [d, m2, y]}
            if len(event) > 8 and event[8] is not None:
                row["fifo_bytes"] = event[8]
            out.append(row)
        elif kind == "fw":
            row = {"ev": "fw", "t": t, "event": event[2]}
            row.update(event[3])
            out.append(row)
    if sent:
        start, end, count = sent
        step = (end - start) / count if count else 0.0
        out += [{"ev": "sent", "t": round(start - t0 + i * step, 4), "event": "sent", "index": i, "est": True} for i in range(count)]
    out.sort(key=lambda row: row["t"])
    if len(out) > max_lines:
        cut = len(out) - max_lines
        out = out[:max_lines] + [{"ev": "truncated", "lines": cut}]
    if summary is not None:
        out.append(dict(summary, ev="summary"))
    return out


def status_cadence_keyed(frames, t0, t1, median_max=0.065, longest_max=0.12):
    """GET_STATUS intervals while the modem is keyed (from the first native frame `t0` to the last `t1`, monotonic seconds). design-276 1.5 F2: the
    fixed image polls every 50 ms while keyed or draining (idle stays 250 ms). Judged: at least 8 intervals, median at most `median_max`, longest at
    most `longest_max` (the unit test of DV-Router asserts 60 ms in virtual time; the rig adds the tick and scheduling)."""
    times = [f.mono for f in frames if f.ftype == ms.GET_STATUS and t0 <= f.mono <= t1]
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    if len(gaps) < 8:
        return verdict(["only %d GET_STATUS intervals while keyed, wanted 8 or more (a 3 s over at 50 ms has about 60)" % len(gaps)], polls=len(times))
    median, longest = gaps[len(gaps) // 2], gaps[-1]
    problems = []
    if median > median_max:
        problems.append("median GET_STATUS interval %.3f s while keyed, the fixed image polls every 0.050 s (design-276 F2)" % median)
    if longest > longest_max:
        problems.append("longest GET_STATUS interval %.3f s while keyed" % longest)
    return verdict(problems, polls=len(times), median=round(median, 3), shortest=round(gaps[0], 3), longest=round(longest, 3))


def has_series(table, family, **labels):
    """True when a counters() table has a sample of `family` whose labels contain `labels` (a zero counts: the radio families keep their complete fixed
    label sets, status.md "Radio port telemetry"). None table: False."""
    return bool(table) and any(key[0] == family and all((k, v) in key[1:] for k, v in labels.items()) for key in table)


def dmr_garbled_copy(src, dst, color_code):
    """A voice LC header copy that the firmware would deliver (the right colour code and slot type, so the adapter does not take it for another station's
    burst) whose BPTC / LC is destroyed: the first 12 bytes of the burst inverted. dvtest's own decoder reports `rs_ok` False for it. RIF5j: this is a
    DAMAGED copy, filled with silence and refreshing the timeout; `dmr_damaged_copy` (another colour code, a clean codeword) is not."""
    lc = dmrp.full_lc(dmrp.FLCO_GROUP, src, dst)
    burst = bytearray(bytes((0x41,)) + rf.data_burst(lc, rf.DT_VOICE_LC_HEADER, color_code))
    for i in range(1, 13):
        burst[i] ^= 0xFF
    return bytes(burst)


def ysf_dch_garbled(payload):
    """A YSF modem payload (flag + 120 B frame) whose DCH is destroyed: the first 5 bytes of each of the five 18 byte blocks of the V/D mode 2 payload
    inverted, so the DCH CRC fails (`ysf.vd2_dch` is None) while sync, FICH and the VCH stay. The adapter takes it for a damaged frame of the open over."""
    from .proto import ysf as ysfp
    out = bytearray(payload)
    base = 1 + ysfp.FRAME_SIZE - ysfp.PAYLOAD_SIZE
    for block in range(ysfp.FRAMES_PER_PACKET):
        for k in range(5):
            out[base + ysfp.BLOCK_SIZE * block + k] ^= 0xFF
    return bytes(out)


def release_view(frames, tx_off_t=None, slow_gap_s=0.15):
    """RIF5j F1 (review-277): after a D-STAR EOT the core releases the physical only on a status reply that is more than the SECOND poll after the EOT, and
    only when the modem reports TX off, then the poll interval goes back to 250 ms. From the host frame log of an over: the polls after the last EOT
    write, the last one before the cadence slows down (the poll whose reply released), and the numbers. Problems: fewer than two polls after the EOT up
    to the release, a release poll sent before the fake's TX went off (`tx_off_t`, the firmware-like fake only), no slowing down at all."""
    eots = [f for f in frames if f.ftype == ms.DSTAR_EOT]
    if not eots:
        return verdict(["no D-STAR EOT in the log"])
    t_eot = eots[-1].mono
    polls = [f.mono for f in frames if f.ftype == ms.GET_STATUS and f.mono > t_eot]
    release = None
    for i in range(len(polls) - 1):
        if polls[i + 1] - polls[i] > slow_gap_s:
            release = i
            break
    if release is None:
        return verdict(["the status poll did not slow down after the EOT: %d polls, the last %.2f s after it" % (len(polls), (polls[-1] - t_eot) if polls else -1)],
                       polls_after_eot=len(polls))
    problems = []
    n = release + 1
    if n < 2:
        problems.append("the physical was released on the first poll after the EOT (the release needs a reply to the second: review-277 F1 eot_barrier)")
    t_release = polls[release]
    if tx_off_t is not None and t_release < tx_off_t - 0.005:
        problems.append("the cadence slowed down %.3f s before the fake's TX went off (the release needs a status with TX off)" % (tx_off_t - t_release))
    return verdict(problems, polls_after_eot=n, release_poll_after_eot_s=round(t_release - t_eot, 3),
                   tx_off_after_eot_s=None if tx_off_t is None else round(tx_off_t - t_eot, 3))
