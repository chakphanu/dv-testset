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
from collections import namedtuple

from . import metrics
from . import mmdvm_rf as rf
from . import mmdvm_serial as ms
from .proto import dmr as dmrp

Verdict = namedtuple("Verdict", "problems findings facts")

GATE = "DVT_RIF5"                       # on: the RIF5 cases run (rig/compose.yml passes it; dvtest/target.py documents it)
# the rig shape of rig/config/dv-router/rif5-mmdvm-udp.toml
PORT_NAME = "rif5-modem"
STATION = "N0CALL"
ROOMS = {"dstar": "P", "dmr": "Q", "ysf": "R"}
MODEM_PORT = 3340                       # the tester side of the UDP byte port ([[edges.radio.port]] udp)
EDGE_PORT = 3341                        # the SUT side (udp_bind)
CHILD = "dv-edge-radio"                 # the launcher child (admin status launcher.children[].name)
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
    problems, findings = [], []
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
            unnamed = [(at, got[at]) for at in (15, 16, 18, 19)]
            findings.append("SET_CONFIG bytes the document does not give a value for (offset, value): %s" % unnamed)
    before = []
    for f in dialogue.frames:
        if f.ftype == ms.GET_STATUS:
            break
        if f.ftype in RF_TYPES or f.ftype == ms.SET_MODE:
            before.append(ms.name_of(f.ftype))
    if before:
        problems.append("frames %s before the first GET_STATUS (radio.md 7.1: no frame transmits voice during open)" % before)
    return verdict(problems, findings, versions=len(dialogue.versions), statuses=len(dialogue.statuses))


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


def _prefix_check(label, sent, got, problems, facts):
    sent, got = list(sent), list(got)
    facts["%s_sent" % label], facts["%s_written" % label] = len(sent), len(got)
    first = next((i for i in range(min(len(sent), len(got))) if sent[i] != got[i]), None)
    if first is not None:
        problems.append("%s word %d written to the modem differs from the one sent" % (label, first))
    elif len(got) < len(sent):
        problems.append("%d %s words written, %d sent (the first %d agree)" % (len(got), label, len(sent), len(got)))


def _foreign(frames, mode, problems):
    """No frame of another mode, no CW ID (the snippet switches the ID off), no DMR slot 1 or duplex control."""
    own = ms.TX_TYPES[mode]
    stray = sorted({ms.name_of(f.ftype) for f in frames if f.ftype in RF_TYPES and f.ftype not in own})
    if stray:
        problems.append("frames %s written during a %s over (one mode per over, radio.md 7.2; no CW ID with id.mode none; "
                        "simplex DMR sends no START, ABORT or SHORTLC, radio.md 7.3)" % (stray, mode))


def _first_mode(frames, mode, problems):
    modes = [f for f in frames if f.ftype == ms.SET_MODE]
    if not modes or modes[0].payload != bytes((ms.MODE_ID[mode],)):
        problems.append("the first SET_MODE is %s, wanted %02x (the circuit claims its mode first, radio.md 7.2)" %
                        (modes[0].payload.hex() if modes else "absent", ms.MODE_ID[mode]))
        return
    first_native = next((f for f in frames if f.ftype in ms.TX_TYPES[mode]), None)
    if first_native is not None and modes[0].mono > first_native.mono:
        problems.append("a %s frame was written before SET_MODE" % mode)


def judge_tx_dstar(frames, sent, my, station=STATION, rpt_module="B"):
    """A D-STAR over from the room: SET_MODE 1, one header (41 B, repeater flag clear, RPT2 = station + RF module, RPT1 = station + G, UR CQCQCQ,
    MY the talker, CRC), the 9 byte voice words in order with the sync triple on frame 0 of every 21, one EOT (radio.md 7.1, 7.3; the
    wire literal of DV-Router's own test crates/router/tests/real_children/radio/mmdvm.rs TX_HEADER)."""
    problems, findings, facts = [], [], {}
    _first_mode(frames, "dstar", problems)
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
    voice = [bytes(f.payload[:9]) for f in data if len(f.payload) == 12]
    _prefix_check("voice", sent, voice, problems, facts)
    for i, f in enumerate(data):
        if i % 21 == 0 and len(f.payload) == 12 and bytes(f.payload[9:]) != rf.DSTAR_SYNC:
            problems.append("data frame %d (frame 0 of a superframe) carries slow data %s, not the sync triple 55 2D 16 (radio.md 7.3)" %
                            (i, f.payload[9:].hex()))
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


def judge_tx_dmr(frames, sent, src, dst, color_code, headers_n=8, terminators_n=3):
    """A DMR over from the room (radio.md 7.3 "Built DMR simplex"): SET_MODE 2, every frame `E0 25 1A 00` + a 33 B burst, eight voice LC headers
    (MS data sync, LC group src -> dst, the row's colour code), the voice bursts A to F (A with the MS voice sync, three words per burst),
    three terminators with LC; no slot 1 or duplex control."""
    problems, findings, facts = [], [], {}
    _first_mode(frames, "dmr", problems)
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
    for i, k in enumerate(voice_kinds):
        if (i % 6 == 0) != (k == "voice_sync"):
            problems.append("voice burst %d is %s, burst A of a superframe (index %% 6 == 0) alone carries the voice sync" % (i, k))
            break
    for f in good[:headers_n] + good[-terminators_n:]:
        if burst_center(f.payload[1:]) != int.from_bytes(rf.MS_DATA_SYNC, "big"):
            problems.append("a header or terminator burst does not carry the MS data sync D5 D7 F7 7F D7 57 (radio.md 7.3)")
            break
    voice_bursts = good[headers_n:len(good) - terminators_n] if len(good) >= headers_n + terminators_n else []
    if voice_bursts and burst_center(voice_bursts[0].payload[1:]) != int.from_bytes(rf.MS_VOICE_SYNC, "big"):
        problems.append("burst A does not carry the MS voice sync 7F 7D 5D D5 7D FD (radio.md 7.3)")
    _prefix_check("voice", sent, slot["voice"], problems, facts)
    return Verdict(problems, findings, facts)


def judge_tx_ysf(frames, sent, src, lo_dev=False):
    """A YSF V/D mode 2 over from the room (radio.md 7.3 "Built YSF"): SET_MODE 3, frames `E0 7C 20 00` + 120 B, one header and one terminator,
    FICH data type 2 (V/D mode 2), the Dev flag the row's `ysf.lo_dev`, five 9 byte words per voice frame, the header names the talker."""
    problems, findings, facts = [], [], {}
    _first_mode(frames, "ysf", problems)
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
    _prefix_check("voice", sent, decoded["voice"], problems, facts)
    return Verdict(problems, findings, facts)


JUDGE_TX = {"dstar": judge_tx_dstar, "dmr": judge_tx_dmr, "ysf": judge_tx_ysf}


def dmr_damaged_copy(src, dst, color_code):
    """A voice LC header copy of the modem (control byte 41, data-sync burst) whose slot type carries the colour code `color_code + 1`: the adapter's
    Golay decode of the slot type does not match the row's colour code, so the copy fails its checks (radio.md 7.3: "A header or terminator copy that
    fails its checks inside an open over is ignored ... and refreshes the native-over timeout"; mode/dmr.rs `parse`)."""
    lc = dmrp.full_lc(dmrp.FLCO_GROUP, src, dst)
    return bytes((0x41,)) + rf.data_burst(lc, rf.DT_VOICE_LC_HEADER, (color_code + 1) % 16)


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
