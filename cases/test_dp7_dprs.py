# SPDX-License-Identifier: MIT
"""DV-Router D-STAR D-PRS position cases after DP7 (docs/dp7-cases.md "D-PRS cases"): what a position that arrives as a D-PRS GPS-A line
in D-STAR slow data becomes. The level-2 case of cases/test_dp15_positions.py (test_dp7_c_dprs_ambiguity_and_uplink) is the model; this
file adds the other ambiguity levels, the two non-plain APRS encodings, the M17 GNSS META side effect and the re-application of the level
by the ride-along and the APRS-IS uplink. Specification: DV-Router docs/design/data.md sections 5, 9.1, 9.5, 13.4; APRS 1.0.1 chapters 6,
9 and 10 for the vectors (cases/dprs_support.py).

  DP7-dprs-a  ambiguity levels 1, 3 and 4 of a D-PRS position are carried with that level, the box centre and the page text of the SENT
              precision on the public SSE, the anonymous public state and the operator feed (the checks of the level-2 case)
  DP7-dprs-b  a COMPRESSED position (APRS101 ch. 9 example `/5L!!<*e7>7P[`, 49.5 N 72.75 W) and a Mic-E position (APRS101 ch. 10 example,
              33 25.64 N 112 07.74 W, destination S32UVT = longitude offset +100) in a D-PRS line are admitted as positions with the decoded
              coordinates; the same information field with destination S32U6T (offset 0) is a valid frame too: 33 25.64 N 12 07.74 W
  DP7-dprs-c  for every ambiguity level 1..4 an M17 listener of the room gets NO GNSS META in the LSF META of the cross-mode M17 voice STREAM
              (the stream-plane ride-along of the fix) for the ambiguous D-PRS position, while an UNAMBIGUOUS D-PRS position of another station in
              the same room, sent first on the same listener, DOES produce one with its coordinates
  DP7-dprs-d  the D-PRS ride-along (the slow data another D-STAR listener receives) and the APRS-IS uplink (the line at the private aprsc)
              re-apply the SAME ambiguity level that was sent: blanked digits (spaces), neither refined nor coarsened

Run shapes and commands: docs/dp7-cases.md ("D-PRS cases"). Observations go to stdout and out/<run>/dp7-dprs.txt ("RECORD" = what was
seen, "FINDING" = differs from the documented behaviour). Every case uses its OWN base callsigns (module constants, distinct from every
other position case: a unit test checks it) because the controller admits 1 position per 30 s per base callsign, dedupes on (type, guard
callsign, body) for 60 s and the private aprsc dedupes for 30 s.

NOT RUN ON A RIG (written against the sources and the design only). Shapes that could not be verified are listed in docs/dp7-cases.md
("D-PRS cases", UNVERIFIED): the TYPE and META of the stream LSF that carries the GNSS ride-along, the D-STAR ride-along shape, the Mic-E destination handling of a
D-PRS line.
"""
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import dstar_sd, slowdata  # noqa: E402

import dprs_support as dp  # noqa: E402
import position_support as ps  # noqa: E402
import test_dp15_positions as tp  # noqa: E402
# fixtures of the first set, reused by name: the hub metrics, the web side, the run shape and the private aprsc
from test_dp15_positions import aprsc, hub, shape, web_ctx  # noqa: E402,F401
from data_support import PACER, build_over, ambe, dstar_listener, m17_client, pump_for, send_over_thread, write_note  # noqa: E402

pytestmark = pytest.mark.rig

MODULE = tp.MODULE
LOG = "dp7-dprs.txt"
SETTING = tp.SETTING

# one base callsign per case (the controller admits 1 position per 30 s per base callsign)
A_CALL = {1: "N0PQ1", 3: "N0PQ3", 4: "N0PQ4"}                       # DP7-dprs-a
COMPRESSED_CALL, MICE_CALL, MICE0_CALL = "N0PQC", "N0PQE", "N0PQF"        # DP7-dprs-b (Mic-E with offset +100, Mic-E with offset 0)
GNSS_CONTROL_CALL = {1: "N0PQB1", 2: "N0PQB2", 3: "N0PQB3", 4: "N0PQB4"}      # DP7-dprs-c, the unambiguous control of each level
GNSS_AMBIGUOUS_CALL = {1: "N0PQA1", 2: "N0PQA2", 3: "N0PQA3", 4: "N0PQA4"}    # DP7-dprs-c, the ambiguous position
RIDE_CALL = {1: "N0PQD1", 2: "N0PQD2", 3: "N0PQD3", 4: "N0PQD4"}              # DP7-dprs-d ride-along
UPLINK_CALL = {1: "N0PQU1", 2: "N0PQU2", 3: "N0PQU3", 4: "N0PQU4"}            # DP7-dprs-d uplink
M17_LISTENER = "N0PQL"                                              # the M17 client of DP7-dprs-c (no position, no rate bucket)
RIDE_LISTENER = "N0PQR"                                             # the second D-STAR client of DP7-dprs-d

SUPERFRAMES = 8                     # 21 frames of 20 ms each: the over lasts 3.4 s, long enough for a position to complete mid-over
TAIL_S = 6.0                        # after the over: the M17 / D-STAR listeners keep being pumped (the control proves the wait is enough)
MIN_VOICE_PACKETS = 20              # an M17 listener that saw fewer cross-mode voice packets of an over did not listen
UPLINK_WAIT_S = tp.UPLINK_WAIT_S


def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


# ---------------------------------------------------------------- the sender: one D-PRS over, listeners pumped meanwhile

def run_over(rig, call, info, watchers=(), tail=TAIL_S, superframes=SUPERFRAMES, dest="API510"):
    """A D-STAR talker (MY = the base of `call`, a DExtra client of module B) sends one over with a TX message and the D-PRS line
    `CALL>DEST,DSTAR*:<info>` (DEST API510; a Mic-E line carries the latitude there). The talker's frames are paced in a thread; the `watchers` (D-STAR or M17 clients already linked)
    are drained meanwhile and for `tail` seconds after the last frame so that their inbox times are real arrival times.
    Returns a namespace: t0 (monotonic, before the header), t_end (after the last frame), payload, sent_fields."""
    base = ps.base_call(call)
    for kind in ("status", "position"):
        PACER.wait(base, kind)
    payload = dp.dprs_payload(call, info, dest=dest)
    text = "DVT DPRS %04d" % (int(time.time()) % 10000)
    fields = dstar_sd.text_gps_fields(text, payload, superframes=superframes)
    _sid, packets = build_over(base, fields, ambe(len(fields), "dprs/" + call))
    with dstar_listener(rig.xlxd_sut, module=MODULE, callsign=base) as talker:
        out = []
        t0 = time.monotonic()
        thread = send_over_thread(talker, packets, out)
        while thread.is_alive():
            for watcher in watchers:
                watcher.client.drain()
            time.sleep(0.01)
        thread.join()
        t_end = time.monotonic()
        for kind in ("status", "position"):
            PACER.offered(base, kind)
        pump_for(tail, *watchers)
    return types.SimpleNamespace(t0=t0, t_end=t_end, payload=payload, fields=fields)


# ---------------------------------------------------------------- DP7-dprs-a: levels 1, 3, 4

@pytest.mark.parametrize("level", [1, 3, 4])
def test_dp7_dprs_a_ambiguity_level(rig, target, hub, web_ctx, shape, level):
    """data.md 5 ("nonzero only for sources 1 D-PRS and 5 APRS") and 13.4: a D-PRS GPS-A line whose position (4903.50N/07201.75W, the spec's
    own example) has `level` blanked digits is source 1 with ambiguity `level`, coordinates at the BOX CENTRE (within 2 e7 units of the value
    derived from dvtest.aprs_expect's exact axis) and printed by the page at the sent precision (`4 - level` places, truncated). The same
    checks as the level-2 case (tp.ambiguity_case) on the public SSE, the anonymous public state and the operator feed. An ambiguous D-PRS
    position that is dropped (DP7-FIND1 of the first run) fails with the finding text."""
    case = "DP7-dprs-a[level %d]" % level
    if not shape.positions:
        pytest.skip("this run has positions = false")
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    try:
        tp.ambiguity_case(target, web_ctx, case, lambda c, lat, lon, lvl: tp.send_dstar(ctx, c, lat, lon, lvl, "amb%d" % lvl),
                          A_CALL[level], level, 1)
    except AssertionError as exc:
        raise AssertionError(tp.dprs_ambiguity_finding(str(exc))) from None


# ---------------------------------------------------------------- DP7-dprs-b: compressed and Mic-E

def decoded_position_case(target, web_ctx, case, kind, call, rig, info, decoded, dest="API510"):
    """Sends the D-PRS line `call>dest,DSTAR*:info`, waits for the position on the three surfaces and compares the coordinates with the exact
    decode `decoded` (lat, lon: Fractions of degrees, table, code) within ps.POSITION_E7. A missing or wrong position is recorded as a FINDING
    and fails; the symbol is recorded (a FINDING note when it differs, not asserted: the request is about the coordinates)."""
    lat_e7, lon_e7 = dp.e7(decoded["lat"]), dp.e7(decoded["lon"])
    symbol = decoded["table"] + decoded["code"]
    tag = "DP7-DPRS-" + kind.upper()
    with tp.feeds(web_ctx) as fd:
        run_over(rig, call, info, tail=2.0, superframes=3, dest=dest)
        got = tp.collect(web_ctx, fd, call)
    missing = [name for name, row in (("public SSE", got.sse), ("anonymous public state", got.state), ("operator feed", got.op)) if row is None]
    if missing:
        text = ("FINDING %s: the D-PRS line %s>%s,DSTAR*:%r gave no position on: %s (expected source 1 at %.5f,%.5f). A %s position inside a D-PRS "
                "line is a position in APRS 1.0.1 chapter %s; data.md 696 says the info field of a GPS-A line goes through the APRS position parser"
                % (tag, call, dest, info, ", ".join(missing), float(decoded["lat"]), float(decoded["lon"]), kind, "9" if kind == "compressed" else "10"))
        note(target, case, "FINDING", text)
        raise AssertionError(text)
    tp.assert_visible(got, case)
    for surface, row in (("public SSE", got.sse), ("public state", got.state), ("operator feed", got.op)):
        try:
            ps.check_position(row, source=1, lat_e7=lat_e7, lon_e7=lon_e7, tol=ps.POSITION_E7, ambiguity=0)
        except AssertionError as exc:
            text = "FINDING %s: %s on the %s: %s" % (tag, case, surface, exc)
            note(target, case, "FINDING", text)
            raise AssertionError(text) from None
        sym = row["body"].get("symbol")
        note(target, case, "RECORD" if sym == symbol else "FINDING",
             "%s: symbol %r (sent %r), lat_e7 %d lon_e7 %d (decoded %d %d), ambiguity %r, comment %r" % (
                 surface, sym, symbol, row["body"]["lat_e7"], row["body"]["lon_e7"], lat_e7, lon_e7, row["body"].get("ambiguity"),
                 row["body"].get("comment")))
    return got


def test_dp7_dprs_b_compressed_position(rig, target, hub, web_ctx, shape):
    """APRS 1.0.1 chapter 9: `!/5L!!<*e7>7P[` in a D-PRS line is a position (YYYY = 15427503 -> 49.5 N, XXXX = 20427156 -> -72.75000394 E,
    exact decode by dprs_support.compressed_decode from the base-91 formula, symbol `/>`, course 88 and speed 36.2 knots in the cs bytes).
    Admitted as source 1, ambiguity 0, at the decoded coordinates within the position tolerance on all three surfaces."""
    case = "DP7-dprs-b[compressed]"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    decoded = dp.compressed_decode(dp.SPEC_COMPRESSED)
    assert decoded["lat"] == dp.SPEC_COMPRESSED_LAT and abs(decoded["lon"] - dp.SPEC_COMPRESSED_LON_TEXT) < 1e-5     # the vector is the spec's
    info = dp.compressed_info(dp.SPEC_COMPRESSED, "!")
    note(target, case, "RECORD", "sent %r, decoded %.8f,%.8f (course/speed %s)" % (info, float(decoded["lat"]), float(decoded["lon"]), decoded["cs"]))
    decoded_position_case(target, web_ctx, case, "compressed", COMPRESSED_CALL, rig, info, decoded)


def test_dp7_dprs_b_mice_position(rig, target, hub, web_ctx, shape):
    """APRS 1.0.1 chapter 10: the Mic-E example. The D-PRS line is `CALL>S32UVT,DSTAR*:` + the nine byte information field of the spec's decode
    example (`` `(_fn"Oj/ ``): destination digits 3 3 2 5 6 4 = 33 25.64 N, North, West, and the fifth character `V` (P-Z) = longitude offset
    +100 degrees, so the longitude byte `(` is 100 + 12 = 112 degrees, 07.74 minutes W; message M3 Returning; speed 20 knots, course 251, symbol
    `/j` (a jeep). Admitted as source 1, ambiguity 0, at 33.42733 N 112.129 W within the position tolerance on all three surfaces. The expected
    values are computed by dprs_support.mice_decode from the Mic-E rules, not taken from the product."""
    case = "DP7-dprs-b[mic-e]"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    decoded = dp.mice_decode(dp.SPEC_MICE_DEST, dp.SPEC_MICE_INFO)
    assert decoded["lat"] == dp.SPEC_MICE_LAT and decoded["lon"] == dp.SPEC_MICE_LON      # the vector is the spec's
    note(target, case, "RECORD", "sent %r, decoded %.6f,%.6f speed %d course %d symbol %s%s" % (
        dp.dprs_payload(MICE_CALL, dp.SPEC_MICE_INFO, dest=dp.SPEC_MICE_DEST), float(decoded["lat"]), float(decoded["lon"]), decoded["speed"],
        decoded["course"], decoded["table"], decoded["code"]))
    decoded_position_case(target, web_ctx, case, "mice", MICE_CALL, rig, dp.SPEC_MICE_INFO, decoded, dest=dp.SPEC_MICE_DEST)


def test_dp7_dprs_b_mice_offset_zero_position(rig, target, hub, web_ctx, shape):
    """The same Mic-E information field with the destination `S32U6T` (the fifth character `6` is a digit with flag 0 = longitude offset +0
    degrees) is also a valid frame: it must be admitted, not rejected, and decode with the longitude byte `(` as 12 degrees: 33 25.64 N,
    12 07.74 W (the expected value is computed by dprs_support.mice_decode from the Mic-E longitude rules: d+28 = 40, minus 28 = 12, no +100;
    minutes 67 - 60 = 7, hundredths 74). NOTE: the destination PRINTED in APRS101 chapter 10 (`S32U6T`, "offset +0") is inconsistent with the
    longitude example of the same chapter (112 degrees needs +100, i.e. `S32UVT`); this case takes the destination literally, as a receiver
    must, and the main case (test_dp7_dprs_b_mice_position) is the one that reproduces the specification's 112 07.74 W."""
    case = "DP7-dprs-b[mic-e offset 0]"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    decoded = dp.mice_decode(dp.SPEC_MICE_DEST_SPEC, dp.SPEC_MICE_INFO)
    assert decoded["lat"] == dp.SPEC_MICE_LAT and decoded["lon"] == dp.SPEC_MICE_LON_OFFSET0      # 33 25.64 N, 12 07.74 W
    note(target, case, "RECORD", "sent %r, decoded %.6f,%.6f speed %d course %d symbol %s%s" % (
        dp.dprs_payload(MICE0_CALL, dp.SPEC_MICE_INFO, dest=dp.SPEC_MICE_DEST_SPEC), float(decoded["lat"]), float(decoded["lon"]), decoded["speed"],
        decoded["course"], decoded["table"], decoded["code"]))
    decoded_position_case(target, web_ctx, case, "mice", MICE0_CALL, rig, dp.SPEC_MICE_INFO, decoded, dest=dp.SPEC_MICE_DEST_SPEC)


# ---------------------------------------------------------------- DP7-dprs-c: no M17 GNSS META for an ambiguous position

def gnss_over(rig, mlist, call, info, near):
    """One D-PRS over with the M17 listener `mlist` pumped. Returns the M17 STREAM packets the listener got during it (each carries a copy of
    the stream's LSF, read by dp.lsf_view / dp.gnss_in_lsf), the M17 packet-mode datagrams (not where the GNSS META is expected: kept to
    explain a missing control), and the scan for GNSS-typed and stray GNSS META."""
    n0 = len(mlist.client.inbox)
    ran = run_over(rig, call, info, watchers=[mlist])
    packets = dp.stream_packets(mlist.client.inbox, mlist.client.inbox_times, n0)
    m17p = dp.m17p_packets(mlist.client.inbox, mlist.client.inbox_times, n0)
    scan = dp.gnss_scan(packets, near=near)
    return types.SimpleNamespace(run=ran, packets=packets, m17p=m17p, scan=scan, m17p_scan=dp.gnss_scan(m17p, near=near))


def lsf_text(view):
    """For the log: per stream id the packet count, the number of distinct (TYPE, META) LSF copies and the TYPES."""
    return "; ".join("stream %#06x: %d packets, %d distinct LSF META, TYPES %s, GNSS-typed %d, META %s" % (
        sid, v["packets"], len(v["lsfs"]), sorted({hex(t) for t, _m in v["lsfs"]}), len(v["typed"]),
        [(hex(t), bytes(m).hex()) for t, m in v["lsfs"]][:4]) for sid, v in sorted(view.items())) or "no stream"


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_dp7_dprs_c_ambiguous_position_gives_no_m17_gnss_meta(rig, target, hub, web_ctx, shape, level):
    """DV-Router (data.md, the stream plane): a D-PRS fix rides along as GNSS META in the LSF META field of the cross-mode M17 voice STREAM
    the room's M17 listeners receive during the D-STAR over (legacy GNSS META in production; v3 only where a P26j gate enables it); it is NOT
    a packet. Only an exact position is sent so; an ambiguous one (a box, not a point) must not be. One M17 client of module B listens throughout
    and the LSF copies in the frames of every stream it receives are read (dp.lsf_view; the TYPE announces the META type, the GNSS layout is
    dvtest.proto.m17v3.decode_gnss, written from the M17 specification).
    1) CONTROL: a D-PRS over of N0PQB<level> with a full precision position (10.5 N 20.25 E): the LSF META of the voice stream must carry a
       GNSS-typed META with the lat/lon valid bit whose coordinates equal the position within ps.POSITION_E7 (2e-5 degrees; the raw GNSS step is
       1e-5 degrees in latitude, 2e-5 in longitude). If it does not (no GNSS TYPE, undecodable, wrong coordinates, a position only in an M17P
       packet, or a position under another TYPE) the case FAILS with a FINDING that says which: nothing is accepted silently and the negative has
       no control.
    2) the same listener, the same room, an over of N0PQA<level> with the ambiguous position (4903.50N/07201.75W with `level` blanked digits):
       the listener must get cross-mode voice (at least half the control's stream packets: it listened) and NO GNSS-typed LSF META with nonzero
       content in any stream (text META may change), no other TYPE whose META decodes as a GNSS position within 1 degree of the box, and no
       M17P packet carrying one. Both overs are watched for the same time (the over, then 6 s). 3) The ambiguous position was admitted (public
       state, ambiguity = level): without it the absence is explained by a drop (DP7-FIND1), not by the rule."""
    case = "DP7-dprs-c[level %d]" % level
    if not shape.positions:
        pytest.skip("this run has positions = false")
    control_info = ps.position_info(ps.LAT, ps.LON, 0, "/>", "dp7c")
    ambiguous_info = ps.position_info(ps.APRS_LAT, ps.APRS_LON, level, "/>", "dp7c%d" % level)
    with m17_client(rig.xlxd_sut, callsign=M17_LISTENER, module=MODULE) as mlist:
        control = gnss_over(rig, mlist, GNSS_CONTROL_CALL[level], control_info, near=(ps.LAT, ps.LON))
        look = dp.gnss_in_lsf(control.packets, ps.LAT, ps.LON, ps.POSITION_E7)
        note(target, case, "RECORD", "control: %d M17 stream packets (%s); GNSS-typed LSF copies %d, decodable with a position %d, matching %d, off %s; "
             "stray %d; M17P datagrams %d (position-like %d); first GNSS fn/time %s" % (
                 len(control.packets), lsf_text(look["streams"]), len(look["typed"]), len(look["decoded"]), len(look["matches"]),
                 [(round(a, 5), round(b, 5)) for a, b in look["off"]][:3], len(control.scan["stray"]), len(control.m17p),
                 len(control.m17p_scan["typed"]) + len(control.m17p_scan["stray"]),
                 None if not look["typed"] else (look["typed"][0]["fn"], None if look["typed"][0]["at"] is None else round(look["typed"][0]["at"] - control.run.t0, 2))))
        assert len(control.packets) >= MIN_VOICE_PACKETS, \
            "%s: the M17 listener got %d cross-mode stream packets of the control over (transcoder up, DVT_TRANSCODER_FAKE=1? M17 module %s?)" % (
                case, len(control.packets), MODULE)
        if not look["matches"]:
            if look["off"]:
                why = "GNSS META in the LSF but at the wrong place %s" % [(round(a, 5), round(b, 5)) for a, b in look["off"]][:3]
            elif look["typed"]:
                why = "GNSS-typed LSF META that does not decode as a position (reserved bytes or the lat/lon valid bit clear)"
            elif control.scan["stray"]:
                why = "the position is in the LSF META of %d stream packets under a TYPE that does not announce GNSS (%s)" % (
                    len(control.scan["stray"]), sorted({hex(p["type"]) for p in control.scan["stray"]}))
            elif control.m17p_scan["typed"] or control.m17p_scan["stray"]:
                why = "the position only arrives as an M17P packet-mode datagram, not in the LSF META of the voice stream"
            else:
                why = "no GNSS META anywhere (no GNSS TYPE in the stream LSF, no position in any META, no M17P position)"
            text = ("FINDING DP7-DPRS-GNSS: the unambiguous D-PRS position of %s (10.5 N 20.25 E) did not appear as GNSS META in the LSF META of the M17 "
                    "voice stream on a listener of the room: %s. %s. The negative of this case has no control." % (
                        GNSS_CONTROL_CALL[level], why, lsf_text(look["streams"])))
            note(target, case, "FINDING", text)
            raise AssertionError(text)
        near = (float(ps.APRS_LAT), float(ps.APRS_LON))
        ambiguous = gnss_over(rig, mlist, GNSS_AMBIGUOUS_CALL[level], ambiguous_info, near=near)
    amb_look = dp.lsf_view(ambiguous.packets)
    note(target, case, "RECORD", "ambiguous: %d M17 stream packets (%s); GNSS-typed with nonzero META %d, stray %d; M17P datagrams %d" % (
        len(ambiguous.packets), lsf_text(amb_look), len(ambiguous.scan["typed"]), len(ambiguous.scan["stray"]), len(ambiguous.m17p)))
    assert len(ambiguous.packets) * 2 >= len(control.packets), \
        "%s: the listener got %d stream packets of the ambiguous over against %d of the control: it did not listen, the absence proves nothing" % (
            case, len(ambiguous.packets), len(control.packets))
    assert not ambiguous.scan["typed"], "%s: an ambiguous D-PRS position (level %d) produced GNSS META in the LSF of the voice stream: %d copies, positions %s" % (
        case, level, len(ambiguous.scan["typed"]), [(round(a, 5), round(b, 5)) for a, b in ambiguous.scan["positions"]][:3])
    assert not ambiguous.scan["stray"], "%s: a GNSS position near the ambiguous box rides in the LSF META under another TYPE: %d packets (types %s)" % (
        case, len(ambiguous.scan["stray"]), sorted({hex(p["type"]) for p in ambiguous.scan["stray"]}))
    assert not (ambiguous.m17p_scan["typed"] or ambiguous.m17p_scan["stray"]), \
        "%s: an M17P packet carries a GNSS position of the ambiguous box" % case
    row, _rows = ps.wait_public_row(web_ctx.anon(), GNSS_AMBIGUOUS_CALL[level], ps.PUBLIC_WAIT_S, room=MODULE)
    note(target, case, "RECORD", "the ambiguous position in the anonymous public state: %s" % (None if row is None else row["body"]))
    assert row is not None, ("%s: no absence evidence: the ambiguous D-PRS position of %s is not in the public state either (dropped, DP7-FIND1?), "
                             "so no GNSS META is not a decision of the rule" % (case, GNSS_AMBIGUOUS_CALL[level]))
    assert row["body"].get("ambiguity") == level, "%s: the admitted position carries ambiguity %r, want %d" % (case, row["body"].get("ambiguity"), level)


# ---------------------------------------------------------------- DP7-dprs-d: the level is re-applied

def check_line_level(info, level):
    """The information field `info` (bytes) of a D-PRS line / uplinked report starts with `!` or `=` and then the position text with `level`
    blanked digits, exactly as sent: returns the empty string when right, else the reason."""
    want = ps.plain_text(ps.APRS_LAT, ps.APRS_LON, level, "/>")
    text = info if isinstance(info, str) else bytes(info).decode("latin-1")
    if not text or text[0] not in "!=":
        return "the report starts with %r, want ! or =" % text[:1]
    if text[1:1 + len(want)] != want:
        return "the position text is %r, want %r (level %d: %d blanks)" % (text[1:1 + len(want)], want, level, want.count(" "))
    return ""


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_dp7_dprs_d_ride_along_keeps_the_level(rig, target, hub, shape, level):
    """data.md 1647 / 2339 (level carried and re-applied by the D-PRS ride-along): a second D-STAR client of module B (N0PQR) receives the over of
    the talker. The slow data it receives is decoded with dvtest.dstar_sd.Decoder: every D-PRS line of the talker's callsign must carry the
    position text `!4903.50N/07201.75W>` with `level` blanked digits as sent (spaces; no refined digits, no coarser level), with a valid line CRC.
    The line is expected: a D-STAR to D-STAR over keeps its slow data (DP15-1d) and a re-emitted ride-along must keep the level. A listener that
    gets no D-PRS line of the talker is a FINDING (nothing to check)."""
    case = "DP7-dprs-d[ride-along level %d]" % level
    if not shape.positions:
        pytest.skip("this run has positions = false")
    call = RIDE_CALL[level]
    info = ps.position_info(ps.APRS_LAT, ps.APRS_LON, level, "/>", "ride%d" % level)
    with dstar_listener(rig.xlxd_sut, module=MODULE, callsign=RIDE_LISTENER) as ride:
        ran = run_over(rig, call, info, watchers=[ride], tail=3.0)
        overs = slowdata.overs(ride.client.inbox)
    events = dp.dprs_events(overs, dstar_sd.Decoder)
    lines = []
    for index, event in events:
        parsed = dp.parse_dprs_line(event["line"])
        if parsed is not None and ps.same_base(parsed[0], call):
            lines.append((index, event, parsed))
    note(target, case, "RECORD", "ride-along listener: %d overs, %d D-PRS events, %d of %s; first info %r" % (
        len(overs), len(events), len(lines), call, lines[0][2][3][:30] if lines else None))
    if not lines:
        text = ("FINDING DP7-DPRS-RIDE: the second D-STAR listener received no D-PRS line of %s (%d overs, %d D-PRS events of other sources); "
                "the ride-along of the level cannot be checked" % (call, len(overs), len(events)))
        note(target, case, "FINDING", text)
        raise AssertionError(text)
    for index, event, parsed in lines:
        assert event["crc_ok"], "%s: the ride-along D-PRS line of over %d has a bad CRC" % (case, index)
        reason = check_line_level(parsed[3], level)
        if reason:
            text = "FINDING DP7-DPRS-RIDE: %s: %s (line source %s dest %s path %s)" % (case, reason, parsed[0], parsed[1], parsed[2])
            note(target, case, "FINDING", text)
            raise AssertionError(text)
    sent = dp.parse_dprs_line((dp.dprs_payload(call, info)).encode("latin-1"))
    note(target, case, "RECORD", "ride-along line identical to the sent one: %s" % (lines[0][2] == sent))


@pytest.mark.parametrize("level", [1, 2, 3, 4])
def test_dp7_dprs_d_uplink_keeps_the_level(rig, target, hub, shape, request, level):
    """data.md 9.5 / 2339 (the uplink renders the position again with the level re-applied): with the room-to-APRS-IS path of dp7-aprsis-up.toml
    (profile aprs) the line the PRIVATE aprsc receives for the D-PRS beacon of N0PQU<level> is `!` or `=` followed by `4903.50N/07201.75W>` with
    `level` blanked digits at the same places as sent. The model is the uplink half of the level-2 case (tp.test_dp7_c_dprs_ambiguity_and_uplink)
    without the room-side checks, for every level. UNVERIFIED: the ident `stream` of a D-STAR talker (a missing line fails with that reason)."""
    case = "DP7-dprs-d[uplink level %d]" % level
    if not shape.positions:
        pytest.skip("this run has positions = false")
    if not shape.uplink:
        pytest.skip("no path room:B -> edge:aprsis: add rig/config/dv-router/dp15-aprsis.toml and dp7-aprsis-up.toml")
    host = request.getfixturevalue("aprsc")
    call = UPLINK_CALL[level]
    info = ps.position_info(ps.APRS_LAT, ps.APRS_LON, level, "/>", "up%d" % level)
    with tp.aprsc_login(host) as tester:
        mark = tester.mark()
        run_over(rig, call, info, tail=2.0)
        hit = tester.wait_packet(lambda p: ps.same_base(p["src"], call), UPLINK_WAIT_S, since=mark)
    note(target, case, "RECORD", "uplinked line at the aprsc: %s" % (None if hit is None else hit[2]))
    assert hit is not None, "%s: the D-PRS position of %s was not uplinked within %.0f s (ident stream? path dp7-aprsis-up?)" % (case, call, UPLINK_WAIT_S)
    reason = check_line_level(hit[1]["info"], level)
    if reason:
        text = "FINDING DP7-DPRS-UPLINK: %s: %s" % (case, reason)
        note(target, case, "FINDING", text)
        raise AssertionError(text)
