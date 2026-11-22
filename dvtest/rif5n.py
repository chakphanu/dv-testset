# SPDX-License-Identifier: MIT
"""RIF5 judgements for the image dv-router:wip-f1f3777 (DV-Router bc9af2b1), switch DVT_RIF5N=on (docs/rif5-cases.md "The image wip-f1f3777").

What is new in that image, read from DV-Router docs/design/radio.md at bc9af2b1 (section names; the file has no stable line numbers across
revisions, so the cases and this module cite the section and the sentence):

  RIF5d NXDN (7.0, 7.1 "Native sizes", "NXDN (Built, RIF5d S2)")   mode 5, enable 0x10, TX level at SET_CONFIG v2 byte 16, status space at frame
        byte 11, host frame type 40 (LOST 41), payload = one flag byte + 48 air bytes (`E0 34 40 00 <48 B>`); TX = one VCALL header frame (SACCH_NS,
        FACCH1 in both halves), four 49-bit AMBE units per 80 ms voice frame (SACCH_SS parts 3, 2, 1, 0), one TX_REL frame, all with the inbound RDCH
        LICH and the row's RAN; "a partial last frame is padded with AMBE silence ... a full last frame adds no silence frame"; Q = 4, T = 80 ms.
  RIF5e P25 (7.0 "P25 codec library", "P25 (Built, RIF5e S2)")      mode 4, enable 0x08, TX level at byte 15, status space at frame byte 10, host
        frame types 30 (HDU), 31 (LDU and TDU), 32 (LOST); serial TX `E0 67 30 00` + 99 B HDU, `E0 DC 31 00` + 216 B LDU, `E0 16 31 00` + 18 B TDU;
        one clear HDU held until the first complete voice LDU exists, alternating LDU1 / LDU2, "End pads only a partial final LDU, then sends exactly
        one TDU; no extra full silence LDU follows", NAC and TG from the row, source = the real source id, MFID / service / reserved zero, ALGID 0x80,
        zero MI and KID, LSD zero; Q = 2, T = 180 ms.
  RIF5h D-STAR ride-along (7.7 "Data side", data.md 7.3)           per port `ride_along = true`: the cross-mode talker's own status and D-PRS ride in
        the unused slow-data slots of an over already on air; sync, every voice byte, EOT, frame order and timing unchanged; nothing keys without voice.
  RIF5k / RIF5l (7.1 "Built (RIF5k P3)", "Built (RIF5l)")       no synthetic end frame (D-STAR: last real DATA then the EOT; YSF: no extra comms frame after a
        full group); a stream without voice never keys a port (counted `over_total{dir=tx,result=no_voice}`); `tx_underrun` only mid-over.
  RFHEARD1 (data.md "RF origin", radio.md 7.7)                     radio and AX.25 origins are never transmitted on any RF port, also not as a rider.

Pure: every function takes the host frame log (`mmdvm_serial.HostFrame`) or heard datagrams and returns a rif5.Verdict. Unit tests:
cases/unit/test_rif5n_lib.py.
"""
import math
from collections import namedtuple

from . import dstar_sd, echo, metrics
from . import mmdvm_rf as rf
from . import mmdvm_rf_nxdn as nxrf
from . import mmdvm_rf_p25 as p25rf
from . import mmdvm_serial as ms
from . import rif5
from .proto import nxdn
from .proto import p25 as p25p

Verdict = rif5.Verdict

# the rooms and talk groups of rig/config/dv-router/rif5n-modes.toml (the cases read the RF values from the rendered config)
ROOM_NXDN, ROOM_P25 = "S", "T"
NXDN_ROOM_TG = 31680                  # [edges.nxdn] tg_modules = "31680:S": what an NXDN client of the rig links to
P25_ROOM_TG = 31681                  # [edges.p25] server_tg: the talk group a P25 client of the rig uses
NXDN_IDS = (1001, 1002)              # N0CALL, N0TEST in rig/config/nxdn.csv
P25_IDS = (1023007, 1023008)         # N0CALL, N0TEST in rig/config/dmrid.dat (the DMR namespace)
CALLSIGNS = ("N0CALL", "N0TEST")

# the over sizes: whole units of the mode, so that no padding word is in play (NXDN four words per frame, P25 nine per LDU)
SHORT = {"nxdn": 24, "p25": 36}      # 6 NXDN frames (0.48 s), 4 LDUs (0.72 s)
LONG = {"nxdn": 148, "p25": 144}     # 37 NXDN frames (2.96 s), 16 LDUs (2.88 s)
RX_WORDS = {"nxdn": 148, "p25": 144}
PAD_TO = {"nxdn": 4, "p25": 9, "dstar": 1, "dmr": 3, "ysf": 5}
NO_VOICE = ("dv_edge_radio_over_total", {"dir": "tx", "result": "no_voice"})
RTS_SUPPRESSED = ("dv_edge_radio_over_total", {"dir": "rx", "result": "rts_suppressed"})
RF_DROP = ("dv_pkt_rf_drop_total", {"reason": "rf_origin"})

DSTAR_FILLER = rf.DSTAR_NULL_SLOW     # the scrambled filler 16 29 f5 of an unused slow-data slot


def gate_text(environ):
    return "DVT_RIF5N is not on: this case is for dv-router:wip-f1f3777 (DV-Router bc9af2b1: RIF5d NXDN, RIF5e P25, RIF5h ride-along, RIF5k, RIF5l; docs/rif5-cases.md)"


# ---------------------------------------------------------------- NXDN, room to modem

def _nxdn_info(frame):
    try:
        return nxrf.decode_frame(nxrf.from_payload(bytes(frame.payload)))
    except (ValueError, IndexError, KeyError):
        return None


def nxdn_kind(info):
    """header (a VCALL in a FACCH1 half), trailer (TX_REL), voice, or other."""
    msgs = [b["msg"] for b in info["facch1"] if b is not None]
    if nxdn.VCALL in msgs:
        return "header"
    if nxdn.TX_REL in msgs:
        return "trailer"
    if info["voice"]:
        return "voice"
    return "other"


def judge_tx_nxdn(frames, sent, src, dst, ran, lossy=False):
    """An NXDN over from the room (radio.md "NXDN (Built, RIF5d S2)", 7.1 "Native sizes"): SET_MODE 5 first, every frame type 40 with a 49 byte payload
    (flag 00 + 48 air bytes), exactly one VCALL header frame (inbound RDCH LICH, SACCH_NS, the same FACCH1 in both halves: group call, src, dst = the
    row's tg), then the voice frames (LICH SACCH_SS, no steal, four 49-bit words each, the SACCH structure 3, 2, 1, 0), then exactly one TX_REL frame;
    every SACCH carries the row's RAN with a valid CRC; the voice words are the words sent, in order, byte exact (NXDN <-> NXDN and NXDN <-> DMR pass the
    49 parameter bits unchanged: media-format.md "AMBE+2 / NXDN"); "a full last frame adds no silence frame": no word after the sent ones but the
    padding of a partial last frame."""
    problems, findings, facts = [], [], {}
    rif5._first_mode(frames, "nxdn", problems, facts)
    rif5._foreign(frames, "nxdn", problems)
    native = rif5._native(frames, ms.TX_TYPES["nxdn"])
    wrong = sorted({len(f.payload) for f in native if len(f.payload) != ms.NXDN_LEN})
    if wrong:
        problems.append("NXDN frames of payload %s bytes, native size 49 (flag + 48 B, radio.md 7.1)" % wrong)
    good = [f for f in native if len(f.payload) == ms.NXDN_LEN]
    if good and sorted({f.payload[0] for f in good}) != [0]:
        problems.append("the flag byte of the NXDN frames is not 00 (radio.md 7.3 NXDN: `E0 34 40 00`)")
    views = [(f, _nxdn_info(f)) for f in good]
    if any(info is None for _f, info in views):
        problems.append("an NXDN frame that does not decode")
        views = [(f, info) for f, info in views if info is not None]
    kinds = [nxdn_kind(info) for _f, info in views]
    facts.update(frames=len(views), headers=kinds.count("header"), trailers=kinds.count("trailer"), voice_frames=kinds.count("voice"))
    want_kinds = ["header"] + ["voice"] * math.ceil(len(sent) / 4) + ["trailer"]
    if kinds != want_kinds and not lossy:
        problems.append("the frames are %s, wanted one header, %d voice frames and one trailer (radio.md NXDN: header and terminator are single, "
                        "a full last frame adds no silence frame)" % (_runs(kinds), math.ceil(len(sent) / 4)))
    elif kinds[:1] != ["header"] or kinds[-1:] != ["trailer"] or kinds.count("header") != 1 or kinds.count("trailer") != 1:
        problems.append("the frames are %s: one header first and one trailer last were expected" % _runs(kinds))
    words, structures, bad_lich = [], [], []
    for (f, info), kind in zip(views, kinds):
        lich = info["lich"]
        want_fct, want_opt = ((nxdn.FCT_SACCH_NS, nxdn.OPTION_STEAL_FACCH) if kind in ("header", "trailer")
                              else (nxdn.FCT_SACCH_SS, nxdn.OPTION_STEAL_NONE))
        if not (lich["rfct"] == nxdn.RFCT_RDCH and lich["fct"] == want_fct and lich["option"] == want_opt and not lich["outbound"] and lich["parity_ok"]):
            bad_lich.append((kind, lich))
        if not info["fsw_ok"]:
            problems.append("an NXDN frame without the frame sync word")
            break
        sacch = info["sacch"]
        if sacch is None:
            problems.append("the SACCH of a %s frame fails its CRC" % kind)
        elif sacch["ran"] != ran:
            problems.append("the SACCH of a %s frame carries RAN %d, the row's RAN is %d (radio.md NXDN: all TX uses the configured RAN)" % (kind, sacch["ran"], ran))
        if kind in ("header", "trailer"):
            blocks = info["facch1"]
            if any(b is None for b in blocks) or len({(b["msg"], b["src"], b["dst"], b["group"]) for b in blocks if b}) != 1:
                problems.append("the two FACCH1 halves of the %s frame are not one valid message" % kind)
            else:
                b = blocks[0]
                if (b["src"], b["dst"], b["group"]) != (src, dst, True):
                    problems.append("the %s names src %s dst %s group %s, wanted src %s dst %s group True (the talker and the row's nxdn.tg)" % (
                        kind, b["src"], b["dst"], b["group"], src, dst))
        elif kind == "voice":
            words += info["voice"]
            structures.append(None if sacch is None else sacch["structure"])
    if bad_lich:
        problems.append("%d frames whose LICH is not the inbound RDCH of its kind (SACCH_NS + FACCH1 steal for header and trailer, SACCH_SS + no steal "
                        "for voice, radio.md NXDN); first %s" % (len(bad_lich), bad_lich[0]))
    if structures and structures != [3 - (i % 4) for i in range(len(structures))]:
        findings.append("the SACCH structure of the voice frames is %s, the document says parts 3, 2, 1, 0" % structures[:8])
    index = rif5._word_check("voice", sent, words, problems, findings, facts, lossy)
    extra = len(words) - len(sent)
    facts["extra_words"] = max(0, extra)
    pad = (-len(sent)) % 4
    if extra > pad:
        problems.append("%d voice words after the %d sent (a partial last frame pads at most %d): an extra silence frame at the end of the over "
                        "(radio.md NXDN: a full last frame adds none; RIF5k)" % (extra, len(sent), pad))
    elif extra > 0 and any(w != bytes(nxdn.FRAME_BYTES) and w != nxdn.from_dmr(nxdn.SILENCE_DMR) for w in words[len(sent):]):
        findings.append("the %d padding word(s) are not the AMBE silence unit" % extra)
    return Verdict(problems, findings, facts)


def _runs(kinds):
    """['header', 'voice' x 6, 'trailer'] as text."""
    out, last, n = [], None, 0
    for kind in kinds + [None]:
        if kind == last:
            n += 1
            continue
        if last is not None:
            out.append("%s x %d" % (last, n) if n > 1 else last)
        last, n = kind, 1
    return "[%s]" % ", ".join(out)


# ---------------------------------------------------------------- P25, room to modem

def p25_hdu_fields(payload):
    """The fields of the HDU a host wrote (payload = flag + 99 B): {mi, mfid, algid, kid, tgid}. Hard decision, no correction (the host writes clean frames)."""
    frame = bytes(payload[1:]) if len(payload) == ms.P25_HDU_LEN else bytes(payload)
    bits = p25rf.bytes_to_bits(frame)
    raw = p25rf.get_bits(bits, p25rf.HDR_RANGE[0], p25rf.HDR_RANGE[1])
    words = [p25rf.bits_to_int(raw[18 * i:18 * i + 6]) for i in range(36)][:20]
    data = p25rf.bits_to_bytes(p25rf._hex_to_bits(words))
    return {"mi": bytes(data[:9]), "mfid": data[9], "algid": data[10], "kid": (data[11] << 8) | data[12], "tgid": (data[13] << 8) | data[14]}


def judge_tx_p25(frames, sent, src, dst, nac, lossy=False):
    """A P25 over from the room (radio.md "P25 (Built, RIF5e S2)"): SET_MODE 4 first; one HDU (type 30, payload 100 = flag + 99 B) before any LDU, then
    ceil(n / 9) LDUs (type 31, payload 217) alternating LDU1 / LDU2 from LDU1, then exactly one TDU (type 31, payload 19) last; flag byte 00; the NID of
    every frame decodes to the row's NAC and the DUID of its kind; the HDU and LDU2 are clear (ALGID 0x80, MFID 0, KID 0, MI zero, HDU TGID = the row's
    tg); the LDU1 link control is a group call (LCF 0, MFID 0, service options 0) from `src` to `dst` (the row's p25.tg); LSD zero; the IMBE words are
    the words sent, in order, byte exact (the LDU decode removes the FEC and the PN whitening: "error-free parameters unchanged"); "End pads only a
    partial final LDU, then sends exactly one TDU; no extra full silence LDU follows"."""
    problems, findings, facts = [], [], {}
    rif5._first_mode(frames, "p25", problems, facts)
    rif5._foreign(frames, "p25", problems)
    native = rif5._native(frames, ms.TX_TYPES["p25"])
    lengths = {ms.P25_HEADER: (ms.P25_HDU_LEN,), ms.P25_LDU: (ms.P25_LDU_LEN, ms.P25_TDU_LEN)}
    wrong = sorted({(ms.name_of(f.ftype), len(f.payload)) for f in native if len(f.payload) not in lengths[f.ftype]})
    if wrong:
        problems.append("P25 frames of (type, payload bytes) %s; native sizes HDU 100, LDU 217, TDU 19 (radio.md 7.3 P25)" % wrong)
    good = [f for f in native if len(f.payload) in lengths[f.ftype]]
    if good and sorted({f.payload[0] for f in good}) != [0]:
        problems.append("the flag byte of the P25 frames is not 00 (radio.md 7.3 P25: `E0 67 30 00`, `E0 DC 31 00`, `E0 16 31 00`)")
    hdus = [f for f in good if f.ftype == ms.P25_HEADER]
    tdus = [f for f in good if f.ftype == ms.P25_LDU and len(f.payload) == ms.P25_TDU_LEN]
    ldus = [f for f in good if f.ftype == ms.P25_LDU and len(f.payload) == ms.P25_LDU_LEN]
    want_ldus = math.ceil(len(sent) / 9)
    facts.update(frames=len(good), hdus=len(hdus), ldus=len(ldus), tdus=len(tdus))
    if len(hdus) != 1:
        problems.append("%d HDUs, one clear HDU per over (radio.md P25)" % len(hdus))
    if len(tdus) != 1:
        problems.append("%d TDUs, exactly one (radio.md P25: End sends exactly one TDU)" % len(tdus))
    if len(ldus) != want_ldus and not lossy:
        problems.append("%d LDUs written, %d expected for %d words (no extra full silence LDU, radio.md P25)" % (len(ldus), want_ldus, len(sent)))
    if hdus and ldus and hdus[0].mono > ldus[0].mono or tdus and ldus and tdus[-1].mono < ldus[-1].mono:
        problems.append("the HDU is not before the first LDU, or the TDU is not after the last LDU")
    decoded = p25rf.decode_downlink([(f.ftype, bytes(f.payload)) for f in good])
    duids = decoded["duids"]
    if any(d is None for d in duids):
        problems.append("%d P25 frame(s) whose NID does not decode" % sum(1 for d in duids if d is None))
    if decoded["nac"] != nac:
        problems.append("NAC %s, the row's p25.nac is %d (radio.md P25: exact NAC on TX)" % (decoded["nac"], nac))
    want_duids = [p25rf.DUID_HDU] + [p25rf.DUID_LDU1 if i % 2 == 0 else p25rf.DUID_LDU2 for i in range(len(ldus))] + [p25rf.DUID_TDU]
    if duids != want_duids and not lossy:
        problems.append("the DUID sequence is %s, wanted HDU, LDU1 / LDU2 alternating, TDU: %s" % (duids[:6], want_duids[:6]))
    if (decoded["src"], decoded["dst"], decoded["group"]) != (src, dst, True):
        problems.append("the link control names src %s dst %s group %s, wanted src %s dst %s group True (the talker and the row's p25.tg)" % (
            decoded["src"], decoded["dst"], decoded["group"], src, dst))
    if (decoded["mfid"], decoded["service_options"]) != (0, 0):
        problems.append("link control MFID %s service options %s, the document says zero" % (decoded["mfid"], decoded["service_options"]))
    if (decoded["algid"], decoded["key_id"], decoded["mi"]) != (p25rf.ALGO_UNENCRYPTED, 0, bytes(9)):
        problems.append("ES: ALGID %s KID %s MI %s, wanted the clear 0x80, 0, zero" % (decoded["algid"], decoded["key_id"], decoded["mi"]))
    if hdus:
        hdu = p25_hdu_fields(hdus[0].payload)
        facts["hdu"] = {k: (v.hex() if isinstance(v, bytes) else v) for k, v in hdu.items()}
        if (hdu["algid"], hdu["kid"], hdu["mi"], hdu["mfid"], hdu["tgid"]) != (p25rf.ALGO_UNENCRYPTED, 0, bytes(9), 0, dst):
            problems.append("HDU fields %s, wanted clear ALGID 0x80, zero KID, MI and MFID, TGID %d" % (facts["hdu"], dst))
    if any(lsd != (0, 0) for lsd in decoded["lsd"]):
        problems.append("low speed data is not zero in an LDU (radio.md P25: LSD is zero with no data tap)")
    words = decoded["voice"]
    rif5._word_check("voice", sent, words, problems, findings, facts, lossy)
    extra = len(words) - len(sent)
    facts["extra_words"] = max(0, extra)
    pad = (-len(sent)) % 9
    if extra > pad:
        problems.append("%d IMBE words after the %d sent (a partial last LDU pads at most %d): an extra LDU (radio.md P25: no extra silence LDU)" % (
            extra, len(sent), pad))
    return Verdict(problems, findings, facts)


# ---------------------------------------------------------------- the modem's RF over, in the shapes the host accepts

def rx_air(kind, air):
    """`air` ([(type, payload, delay_ms)] of mmdvm_rf_p25 / mmdvm_rf_nxdn) in the shape radio.md 7.3 gives for RX: "RX type 0x30 accepts native lengths
    19/46/55/100, type 0x31 accepts 217": a P25 TDU (flag + 18 B = 19) is delivered as type 30, not as the type 31 MMDVMHost writes it on TX (the builder of
    dvtest.mmdvm_rf_p25 follows that TX tag). NXDN needs no change."""
    if kind != "p25":
        return list(air)
    return [(ms.P25_HEADER if ftype == ms.P25_LDU and len(payload) == ms.P25_TDU_LEN else ftype, payload, delay) for ftype, payload, delay in air]


# ---------------------------------------------------------------- what a room client hears (modem to room)

def heard_nxdn(items):
    """The NXDND datagrams a room client received, [(t, bytes)] -> dict: headers (list of (src, dst)), voice (49-bit words in order), trailers, times
    (of the first and last packet), packets, other (data packets and anything else), groups (the group flag of every packet)."""
    out = {"headers": [], "voice": [], "trailers": 0, "t_first": None, "t_last": None, "packets": 0, "other": 0, "groups": set()}
    for t, data in items:
        info = nxdn.parse(data)
        if info is None:
            continue
        out["packets"] += 1
        out["t_first"] = t if out["t_first"] is None else out["t_first"]
        out["t_last"] = t
        out["groups"].add(info["group"])
        if info["kind"] == "header":
            out["headers"].append((info["src"], info["dst"]))
        elif info["kind"] == "trailer":
            out["trailers"] += 1
        elif info["kind"] == "voice":
            out["voice"] += nxdn.voice_frames(info["payload"])
        else:
            out["other"] += 1
    return out


def heard_p25(items):
    """The P25 records a room client received, [(t, bytes)] -> dict: voice (11 byte IMBE words in order), src, dst (the 0x66 / 0x65 records, first seen),
    srcs (all distinct), terminators, types (the record types), lifecycle (p25.lifecycle problems), algid (set), t_first, t_last."""
    out = {"voice": [], "src": None, "dst": None, "srcs": set(), "terminators": 0, "types": [], "algid": set(), "t_first": None, "t_last": None}
    for t, data in items:
        rec = p25p.parse(data)
        if rec is None:
            continue
        out["t_first"] = t if out["t_first"] is None else out["t_first"]
        out["t_last"] = t
        out["types"].append(rec["type"])
        if rec["terminator"]:
            out["terminators"] += 1
            continue
        out["voice"].append(rec["imbe"])
        if "src" in rec:
            out["srcs"].add(rec["src"])
            out["src"] = rec["src"] if out["src"] is None else out["src"]
        if "dst" in rec and out["dst"] is None:
            out["dst"] = rec["dst"]
        if "algid" in rec:
            out["algid"].add(rec["algid"])
    out["lifecycle"] = p25p.lifecycle(out["types"])
    return out


# ---------------------------------------------------------------- RIF5k: the end of an over

def end_sequence_problems(kind, verdict, sent_n):
    """RIF5k (radio.md 7.1 "Built (RIF5k P3)"): the RF end sequence has no synthetic silent voice frame. D-STAR: the last real DATA frame then the EOT, no
    extra DATA frame; YSF: completes a partly filled group, then one terminator, no communications frame after a full group; DMR: a partial triplet is
    completed, nothing more. `verdict` is the one of rif5.judge_tx_<kind> (its facts)."""
    facts, out = verdict.facts, []
    pad = (-sent_n) % PAD_TO[kind]
    if facts.get("extra_words", 0) > pad:
        out.append("%d voice words after the %d sent (padding allowed %d)" % (facts["extra_words"], sent_n, pad))
    if kind == "ysf" and facts.get("frames") != 2 + math.ceil(sent_n / 5):
        out.append("%s YSF frames, wanted header + %d V/D mode 2 + terminator (no communications frame after a full group)" % (
            facts.get("frames"), math.ceil(sent_n / 5)))
    if kind == "dstar" and facts.get("voice_written") != sent_n:
        out.append("%s D-STAR data frames for %d words sent (the end unit is not rendered as DATA, the EOT follows the last real frame)" % (
            facts.get("voice_written"), sent_n))
    if kind == "dmr" and facts.get("voice_bursts") != math.ceil(sent_n / 3):
        out.append("%s DMR voice bursts for %d words sent, wanted %d" % (facts.get("voice_bursts"), sent_n, math.ceil(sent_n / 3)))
    return out


# ---------------------------------------------------------------- RIF5l: a stream without voice never keys

def judge_no_keying(frames, t0, t1):
    """RIF5l (radio.md "Built (RIF5l)"): inside [t0, t1) the host wrote nothing that keys the modem: no native frame of any mode, no CW ID, no SET_MODE
    other than the idle (a SET_MODE 00 is the hang of an earlier over); "Holding takes no PortCtl stream, no physical, no holder token and no modem mode"."""
    window = [f for f in frames if t0 <= f.mono < t1]
    keyed = [f for f in window if f.ftype in rif5.RF_TYPES]
    modes = [f for f in window if f.ftype == ms.SET_MODE and f.payload != bytes((ms.MODE_IDLE,))]
    problems = ["%s at +%.2f s: a stream without voice keyed the modem (radio.md RIF5l)" % (ms.name_of(f.ftype), f.mono - t0) for f in keyed]
    problems += ["SET_MODE %s at +%.2f s: a stream without voice claimed a mode (radio.md RIF5l)" % (f.payload.hex(), f.mono - t0) for f in modes]
    return Verdict(problems, [], {"window_s": round(t1 - t0, 2), "frames": len(window)})


# ---------------------------------------------------------------- RIF5h: the D-STAR rider

def dstar_data(frames):
    """The 12 byte D-STAR data frames of a host log, in order."""
    return [f for f in frames if f.ftype == ms.DSTAR_DATA and len(f.payload) == 12]


def rider_events(frames):
    """The slow data decoder's events over the data frames of ONE over (frame i is seq i % 21, the sync at 0)."""
    data = dstar_data(frames)
    return dstar_sd.Decoder().feed_all([(i % 21, bytes(f.payload[9:12])) for i, f in enumerate(data)])


def parse_dprs(line):
    """(source, destination, path, information bytes) of a D-PRS line (bytes after `$$CRCxxxx,`), or None."""
    line = bytes(line).rstrip(b"\r\n")
    head, sep, info = line.partition(b":")
    if not sep or b">" not in head:
        return None
    source, _gt, rest = head.partition(b">")
    dest, _comma, path = rest.partition(b",")
    return source.decode("latin-1"), dest.decode("latin-1"), path.decode("latin-1"), info


def dprs_lines(frames):
    """[(crc_ok, (source, dest, path, info) or None)] of every D-PRS line the slow data of one over carries."""
    return [(ev["crc_ok"], parse_dprs(ev["line"])) for ev in rider_events(frames) if ev["kind"] == "dprs"]


def compare_overs(base, other, rider=True):
    """Two D-STAR overs from the SAME cross-mode source (base: no position sent, other: a position sent), as host logs: the over is otherwise identical
    (data.md 7.3 and radio.md 7.7: the rider "preserves native slow data, frame 0 sync, every voice byte and EOT, frame order and timing"). Problems: a
    different number of data frames, headers or EOTs, a voice word that differs, a sync that is not 55 2D 16 at 0 mod 21, a changed slow-data field whose
    base field was not the unused filler. Facts: the indices of the changed fields. `rider` False is the negative control (ride_along off): nothing
    may change at all."""
    problems, facts = [], {}
    bd, od = dstar_data(base), dstar_data(other)
    facts["frames"] = (len(bd), len(od))
    if len(bd) != len(od):
        problems.append("%d data frames in the base over and %d in the other over" % (len(bd), len(od)))
    for label, a, b in (("header", [f.payload for f in base if f.ftype == ms.DSTAR_HEADER], [f.payload for f in other if f.ftype == ms.DSTAR_HEADER]),
                        ("EOT", [f.payload for f in base if f.ftype == ms.DSTAR_EOT], [f.payload for f in other if f.ftype == ms.DSTAR_EOT])):
        if a != b:
            problems.append("the %s frames differ between the two overs (%d against %d)" % (label, len(a), len(b)))
    changed = []
    for i, (x, y) in enumerate(zip(bd, od)):
        if bytes(x.payload[:9]) != bytes(y.payload[:9]):
            problems.append("voice word %d differs between the two overs" % i)
            break
        if i % 21 == 0 and (bytes(x.payload[9:]) != rf.DSTAR_SYNC or bytes(y.payload[9:]) != rf.DSTAR_SYNC):
            problems.append("frame %d (0 mod 21) does not carry the sync triple 55 2D 16 in both overs" % i)
            break
        if bytes(x.payload[9:]) != bytes(y.payload[9:]):
            changed.append(i)
    facts["changed"] = changed
    if rider:
        if not changed:
            problems.append("no slow-data field differs: nothing rode along")
        foreign = [i for i in changed if bytes(bd[i].payload[9:]) != DSTAR_FILLER]
        if foreign:
            problems.append("slow-data field(s) %s of the base over were not the unused filler and still changed (the rider may only use unused slots)" % foreign[:6])
    elif changed:
        problems.append("%d slow-data field(s) differ although ride_along is off (first at frame %d)" % (len(changed), changed[0]))
    return Verdict(problems, [], facts)


def rf_dprs_fields(callsign, lat=10.5, lon=20.25, comment="rif5n"):
    """(seq, wire slow-data field) list of an RF D-STAR over whose slow data carries a 20 character text and the D-PRS line of `callsign`, for
    mmdvm_rf.dstar_keyup(slow=...): `slow(n)` = fields[n][1]. The position is 10 deg 30.00 N 20 deg 15.00 E."""
    payload = dstar_sd.aprs_position_payload(callsign, lat, lon, comment)
    return dstar_sd.text_gps_fields("RIF5N RF HEARD", payload, superframes=4)


# ---------------------------------------------------------------- metrics beside the radio families

def metric_total(text, family, **labels):
    """The summed value of the series of `family` with those labels in a hub text, or None when no series of the family exists."""
    if not text:
        return None
    samples = [s for s in metrics.parse(text) if s.name == family]
    if not samples:
        return None
    return metrics.total(samples, family, **labels)


# ---------------------------------------------------------------- PORTCTL1: a carrier that outlives the room's TOT

def stuck_carrier_air(rx_kwargs, voice_s=191.2, repeat_from_s=181.2, repeat_every=5):
    """The modem's D-STAR RF over of PORTCTL1 (docs/rif5-cases.md): header, one data frame per 20 ms until `voice_s`, no EOT, and from `repeat_from_s` on the
    same header again after every `repeat_every`-th frame (every 100 ms), at no extra air time: the scenario of DV-Router's floor.rs
    `portctl1_endless_stuck_headers_are_suppressed_until_the_carrier_stops`. Returns [(type, payload, delay_ms)] for Rif5Modem.play_air."""
    n = int(round(voice_s / 0.02))
    air = rf.dstar_keyup(echo.make_frames("dstar", n, "rif5-portctl1"), **rx_kwargs)
    header, data = air[0], air[1:-1]
    out = [header]
    for i, item in enumerate(data):
        out.append(item)
        if 0.06 + 0.02 * (i + 1) >= repeat_from_s and i % repeat_every == 0:
            out.append((rf.DSTAR_HEADER, header[1], 0.0))
    return out


# ---------------------------------------------------------------- evidence for a tx_underrun that grew

def underrun_evidence(events, limit=12):
    """What to send DV-Router when `tx_underrun` grew in an over that should not count one (radio.md 7.1: a count needs more DATA of the same over after a fresh
    TX-off status; a header write or unkey clears the candidate). `events` = Rif5Modem.events_between(since): the TX flag edges of the status replies, the
    first pops and the tx_off / underrun / nak events of the firmware-like fake, all in seconds from the first frame of the window."""
    if not events:
        return {"events": 0}
    t0 = min(e[1] for e in events)
    edges, last = [], None
    for e in events:
        if e[0] == "status":
            tx = bool(e[3] & 1)
            if tx != last:
                edges.append((round(e[1] - t0, 3), "tx_on" if tx else "tx_off"))
                last = tx
    fw = [e for e in events if e[0] == "fw"]
    pops = [round(e[1] - t0, 3) for e in fw if e[2] == "pop"]
    frames = [e for e in events if e[0] == "frame"]
    return {"events": len(events), "tx_flag_edges_of_the_status": edges[:limit],
            "fake_tx_off_underrun_nak": [(round(e[1] - t0, 3), e[2]) for e in fw if e[2] in ("tx_off", "underrun", "nak")][:limit],
            "first_pops": pops[:limit], "gap_between_first_pops_s": [round(b - a, 3) for a, b in zip(pops, pops[1:])][:limit],
            "native_frames_written": len(frames), "first_frames": [(round(e[1] - t0, 3), ms.name_of(e[2]), e[3]) for e in frames[:limit]]}
