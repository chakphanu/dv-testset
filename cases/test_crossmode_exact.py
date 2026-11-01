# SPDX-License-Identifier: MIT
"""Cross-mode traffic of DV-Router checked byte for byte against the fake
extended AMBEd (DVT_TRANSCODER_FAKE=1 scripts/run.sh, docs/ambed-fake.md).

The fake's transform is deterministic (out[i] = in[i mod w] ^ K(src,dst) ^ i),
so the codec units a listener receives are predictable from the units the
talker sent: every payload byte, the count, the order, no duplicate and exactly
one transform per listener codec (a wrong hop count or direction shows as a
different K). The expectation comes from DV-Router's written design
(media-format.md "Unit granularity", "End unit", "Cross-mode egress",
"Expected counts", "USRP"; transcoding.md "Controller transcoding", "Media")
through dvtest.crossmode; nothing is derived from observed behaviour. Where
the documents are silent the case records a FINDING instead of asserting.

XC1 (DV-Router 2c937c3): the transcode session (the AMBEd OPEN) opens on the
first voice unit; a header-only or end-only stream, the DPlus link header
included, opens none; a USRP listener gets exactly the voice PCM units, then
one unkey. The XC1 section at the end also measures what the deferred OPEN
costs after a voiceless header (transcoding.md: min(OPEN time, header gap)
against opening at grant).

Profile dvrouter only; the fixtures skip with the reason otherwise.
"""
import time

import pytest

from dvtest import clients, crossmode, specs
from dvtest.proto import dcs, dextra, dplus, m17

from crossmode_fake import dstar_voice, first_diff, paced_drain, prng, pump_all, scenario, units_sent

pytestmark = [pytest.mark.rig, pytest.mark.crossmode]

CELLS = [("dstar", "dmr"), ("dmr", "dstar"), ("m17", "dmr"), ("m17", "dstar"), ("dmr", "m17"), ("dstar", "m17"),
         ("dcs", "dmr"), ("dmr", "dplus"), ("dplus", "m17"), ("m17", "dcs"),
         ("usrp", "dstar"), ("usrp", "dmr"), ("usrp", "m17"), ("dstar", "usrp"), ("dmr", "usrp"), ("m17", "usrp")]
PASSTHROUGH = [("dstar", "dstar"), ("dmr", "dmr"), ("m17", "m17"), ("dstar", "dcs"), ("dstar", "dplus"),
               ("dcs", "dstar"), ("dplus", "dcs")]


def cell_id(cell):
    return "%s-%s" % cell


def slow_for(index):
    """Distinct slow data per D-STAR frame; sync stays at superframe position 0."""
    if index % 21 == 0:
        return dextra.SYNC_DATA
    return bytes([(index * 7 + 1) & 0xFF, (index * 13 + 5) & 0xFF, (index * 31 + 9) & 0xFF])


def talk_dstar_slow(frames):
    """DExtra talker that sends its own slow data bytes (talk(talker, kind))."""
    def talk(talker, kind):
        sid = clients.fresh_stream_id(kind)
        packets = [talker.header(sid)]
        packets += [dextra.voice(sid, i, f, False, slow_for(i)) for i, f in enumerate(frames)]
        packets.append(dextra.voice(sid, len(frames), bytes(9), True))
        talker.client.send(talker.dest, packets[0])
        talker.client.pump(0.3)
        talker.client.paced(talker.dest, packets[1:], 0.020)
        talker.client.pump(2.0)
        return sid
    return talk


def verify_link(fake, src, frames, dsts, count_sessions=1):
    """The transcoder link as the fake saw it: one OPEN with the listeners'
    codecs ascending (input excluded), exactly the source units as data
    requests in order (pid = index mod 256), one reply each, one CS.

    A DPlus talker first links by a header-only stream (dvtest.proto.dplus
    join: header + last frame, no voice). Under XC1 that stream opens no
    session (transcoding.md Controller transcoding), so the talk is the only
    session; test_cross_pair_exact also checks the counters right after the
    join (join_opened_nothing)."""
    outs = tuple(sorted({crossmode.CODEC[d] for d in dsts} - {crossmode.CODEC[src]}))
    sessions, strays = fake.sessions(expect_cs=count_sessions)
    assert len(sessions) == count_sessions, "%d OPENs, expected %d; strays %d; sessions %s" % (
        len(sessions), count_sessions, len(strays),
        [(x.open[3], x.open[1], x.open[2], len(x.requests), len(x.replies), x.cs, x.busy) for x in sessions])
    problems = crossmode.check_link(sessions[0], crossmode.CODEC[src], units_sent(src, frames), outs)
    assert not problems, "; ".join(problems)
    assert len(sessions[0].cs) == 1, "CS for the stream sent %d times" % len(sessions[0].cs)
    assert not strays, "%d datagrams the fake dropped or could not place" % len(strays)
    return sessions[0]


def talk_after_link_snapshot(fake, frames, seen):
    """The client library's talk, with the fake's counters taken first: what
    linking (for DPlus: the header-only join stream) cost at the fake."""
    def talk(talker, kind):
        seen.update(fake.counters())
        return clients.talk(talker, kind, frames)
    return talk


def join_opened_nothing(counters):
    """XC1: the DPlus link header (header + last frame, no voice) sends no
    OPEN, no data and no CS (transcoding.md Controller transcoding)."""
    got = {k: counters.get(k, 0) for k in ("rx_open", "rx_data_request", "rx_cs")}
    assert got == {"rx_open": 0, "rx_data_request": 0, "rx_cs": 0}, \
        "the DPlus link header reached the transcoder (XC1: no session without voice): %r" % got


def assert_listener(label, got, expect):
    """Exact comparison: every unit, the count and the order are documented."""
    diff = first_diff(got, expect.frames)
    assert diff is None, "%s: %s (%s)" % (label, diff, expect.note)


def check_usrp_cross(label, raw):
    """USRP listener framing (media-format.md USRP, XC1): the voice frames,
    then exactly one 32 B unkey; no synthetic PCM end unit or extra keyed frame."""
    problems = crossmode.usrp_tail_problems(raw)
    assert not problems, "%s: %s" % (label, "; ".join(problems))


def check_dstar_cross(fake, inbox, count):
    """Cross-mode D-STAR output: sequence = media_seq mod 21, slow data sync at
    position 0 and filler elsewhere (media-format.md, D-STAR (DExtra) voice)."""
    voice = dstar_voice(inbox)
    assert [v["counter"] for v in voice][:count] == crossmode.dstar_cross_seq(count)
    assert [v["slow"] for v in voice][:count] == crossmode.dstar_cross_slow(count)


def check_m17_cross(fake, inbox):
    """Cross-mode M17 output: packet n carries units 2n, 2n+1 (frame number n,
    no gap), the last packet is the EOT, TYPE 0x0005 and META zero."""
    packets = [m17.parse_stream(d) for d in inbox]
    packets = [p for p in packets if p]
    assert packets, "no M17 stream packets"
    assert [p["fn"] for p in packets] == list(range(len(packets))), [p["fn"] for p in packets][:8]
    assert [p["last"] for p in packets] == [False] * (len(packets) - 1) + [True]
    assert {p["type"] for p in packets} == {0x0005}, {p["type"] for p in packets}
    assert {p["meta"] for p in packets} == {bytes(14)}


@pytest.mark.parametrize("cell", CELLS, ids=cell_id)
def test_cross_pair_exact(rig, fake, cell):
    """Talker src, listener dst on one module: every unit the listener gets is
    the one-hop transform of the unit the talker sent, in order, counted as
    the document's Expected counts, and the fake saw exactly those units."""
    src, dst = cell
    frames = prng(src, seed="pair/%s" % cell_id(cell))
    after_link = {}
    talk = talk_after_link_snapshot(fake, frames, after_link) if src == "dplus" else None
    res = scenario(rig, src, [dst], frames, talk=talk)
    if src == "dplus":
        join_opened_nothing(after_link)
    expect = crossmode.expected_listener(src, dst, frames)
    assert_listener(cell_id(cell), res.frames[dst], expect)
    verify_link(fake, src, frames, [dst])
    if dst == "usrp":
        check_usrp_cross(cell_id(cell), res.raw[dst])
    if dst == "dstar":
        check_dstar_cross(fake, res.raw[dst], len(expect.frames))
    if dst == "m17":
        check_m17_cross(fake, res.raw[dst])


@pytest.mark.parametrize("cell", PASSTHROUGH, ids=cell_id)
def test_same_codec_not_transformed(rig, fake, cell):
    """Same codec (any protocol pair): the payload equals the input, the
    transcoder is not involved at all (no OPEN, no data)."""
    src, dst = cell
    frames = prng(src, seed="same/%s" % cell_id(cell))
    fn0 = specs.m17_fn0_relayed(rig)
    res = scenario(rig, src, [dst], frames)
    expect = crossmode.expected_listener(src, dst, frames, m17_fn0=fn0)
    diff = first_diff(res.frames[dst], expect.frames)
    assert diff is None, diff
    counters = fake.counters()
    assert counters.get("rx_open", 0) == 0, "an OPEN for a same-codec stream: %r" % counters
    assert counters.get("rx_data_request", 0) == 0


def test_dstar_slow_data_untouched(rig, fake):
    """D-STAR to D-STAR keeps the received slow data bytes of every frame
    (media-format.md, Unit extensions): DExtra to DExtra and to DCS."""
    frames = prng("dstar", seed="slow")
    res = scenario(rig, "dstar", ["dstar", "dcs"], frames, talk=talk_dstar_slow(frames))
    assert first_diff(res.frames["dstar"], frames) is None
    assert first_diff(res.frames["dcs"], frames) is None
    got = [v["slow"] for v in dstar_voice(res.raw["dstar"])]
    want = [slow_for(i) for i in range(len(frames))]
    assert got == want, "DExtra slow data differs at %s" % [i for i, (a, b) in enumerate(zip(got, want)) if a != b][:5]
    dcs_slow = [p["slow"] for p in (dcs.parse(d) for d in res.raw["dcs"]) if p and not p["last"]]
    if dcs_slow != want:
        fake.finding("dstar-dcs-slow", "slow data carried across D-STAR edges (media-format.md Unit extensions)",
                     "DCS listener slow data differs at %s" % [i for i, (a, b) in enumerate(zip(dcs_slow, want)) if a != b][:5])
    assert fake.counters().get("rx_open", 0) == 0


def test_m17_meta_untouched(rig, fake):
    """M17 to M17 keeps TYPE, META, frame numbers and payload of every
    relayed packet (media-format.md, M17 edge mapping)."""
    frames = prng("m17", seed="meta")
    sid = clients.fresh_stream_id("m17")
    meta = bytes((0xA0 + j * 3) & 0xFF for j in range(14))        # one META for the whole stream (as an LSF)
    packets = []
    for n in range(len(frames) // 2):
        packets.append(m17.stream_packet(sid, n, frames[2 * n] + frames[2 * n + 1], src=clients.CALLSIGNS[0],
                                         meta=meta))
    packets.append(m17.stream_packet(sid, len(packets), packets_payload(packets[-1]), src=clients.CALLSIGNS[0],
                                     meta=meta, last=True))

    def talk(talker, _kind):
        talker.client.send(talker.dest, packets[0])
        talker.client.pump(0.3)
        talker.client.paced(talker.dest, packets[1:], 0.040)
        talker.client.pump(2.0)
        return sid

    res = scenario(rig, "m17", ["m17"], frames, talk=talk)
    got = [m17.parse_stream(d) for d in res.raw["m17"]]
    got = {p["fn"] | (0x8000 if p["last"] else 0): p for p in got if p}
    sent = {m17.parse_stream(p)["fn"] | (0x8000 if m17.parse_stream(p)["last"] else 0): m17.parse_stream(p)
            for p in packets}
    relayed = [fn for fn in sent if fn in got]
    missing = [fn for fn in sent if fn not in got]
    assert missing in ([], [0]), "packets not relayed: %s" % missing
    for fn in relayed:
        a, b = got[fn], sent[fn]
        for field in ("meta", "payload", "type"):
            assert a[field] == b[field], "fn %d %s: got %s, sent %s" % (fn, field, a[field].hex(), b[field].hex())
    assert fake.counters().get("rx_open", 0) == 0


def packets_payload(packet):
    return m17.parse_stream(packet)["payload"]


def test_one_transform_per_listener_codec(rig, fake):
    """One D-STAR talker, listeners in codecs 2 and 3 (and a D-STAR listener
    of the same codec): a single OPEN names outputs (2, 3), each listener gets
    its own one-hop transform of the same units, the same-codec listener gets
    the untouched frames. A double conversion would show as another K."""
    frames = prng("dstar", seed="multi")
    res = scenario(rig, "dstar", ["dmr", "m17", "dcs"], frames)
    for dst in ("dmr", "m17"):
        expect = crossmode.expected_listener("dstar", dst, frames)
        diff = first_diff(res.frames[dst], expect.frames)
        assert diff is None, "%s: %s" % (dst, diff)
    assert first_diff(res.frames["dcs"], frames) is None, "same-codec listener was transformed"
    verify_link(fake, "dstar", frames, ["dmr", "m17", "dcs"])


@pytest.mark.parametrize("src,dsts", [("dmr", ["dstar", "m17"]), ("m17", ["dstar", "dmr"]),
                                      ("dstar", ["dmr", "m17", "usrp"]), ("usrp", ["dstar", "dmr", "m17"])],
                         ids=lambda v: v if isinstance(v, str) else "+".join(v))
def test_one_transform_per_listener_codec_all_sources(rig, fake, src, dsts):
    """Each source codec with every other codec listening at once: one OPEN
    with all other codecs ascending, each listener its own K."""
    frames = prng(src, seed="multi2/%s" % src)
    res = scenario(rig, src, dsts, frames)
    for dst in dsts:
        expect = crossmode.expected_listener(src, dst, frames)
        assert_listener("%s-%s" % (src, dst), res.frames[dst], expect)
        if dst == "usrp":
            check_usrp_cross("%s-%s" % (src, dst), res.raw[dst])
    verify_link(fake, src, frames, dsts)


def test_pid_wraps_and_order_holds_over_300_units(rig, fake):
    """300 units cross the 8-bit pid at 256: requests carry pid = index mod 256
    in order, the listener gets 300 units in order (transcoding.md, Media)."""
    count = 300
    frames = prng("dstar", count, seed="long")
    res = scenario(rig, "dstar", ["dmr"], frames)
    expect = crossmode.expected_listener("dstar", "dmr", frames)
    assert first_diff(res.frames["dmr"], expect.frames) is None
    sessions, _ = fake.sessions()
    assert sessions and len(sessions[0].requests) == count + 1
    assert [r[1] for r in sessions[0].requests][254:258] == [254, 255, 0, 1]


# ---- XC1: the session opens on the first voice unit ------------------------------

@pytest.mark.parametrize("form", ["header", "header-end"])
def test_voiceless_stream_opens_no_session(rig, fake, form):
    """A D-STAR header with no voice (the 1.6 s silence timeout closes it) or a
    header followed by the last frame alone: no OPEN, no data request, no CS,
    and the DMR listener gets no converted voice (transcoding.md Controller
    transcoding, XC1: a header-only or end-only stream never sends
    CreateSession and produces no synthetic converted media)."""
    def talk(talker, kind):
        sid = clients.fresh_stream_id(kind)
        talker.client.send(talker.dest, talker.header(sid))
        talker.client.pump(0.3)
        if form == "header-end":
            talker.client.send(talker.dest, dextra.voice(sid, 0, bytes(9), True))
        talker.client.pump(2.5)                 # past the 1.6 s silence timeout
        return sid

    res = scenario(rig, "dstar", ["dmr"], [], talk=talk)
    c = fake.counters()
    got = {k: c.get(k, 0) for k in ("rx_open", "rx_data_request", "rx_cs")}
    assert got == {"rx_open": 0, "rx_data_request": 0, "rx_cs": 0}, "voiceless %s stream: %r" % (form, got)
    assert res.frames["dmr"] == [], "%d DMR voice frames for a voiceless stream" % len(res.frames["dmr"])


XC1_DST = "m17"             # codec 3: transcoded from D-STAR; one packet per two units
HEADER_GAP_S = 0.500        # header -> first voice: longer than the 300 ms OPEN, below the 1.6 s grant deadline
NO_GAP_S = 0.020            # header, then the first voice frame one frame later (the natural form)


def dstar_stream(kind, talker, sid, frames):
    """Header, voice frames (sync at superframe position 0), last frame, as
    the client library's talk() builds them for DExtra and DPlus."""
    packets = [talker.header(sid)]
    for index, frame in enumerate(frames):
        slow = dextra.SYNC_DATA if index % 21 == 0 else dextra.SLOW_DATA
        if kind == "dplus":
            packets.append(dplus.voice(sid, index, frame, slow))
        else:
            packets.append(dextra.voice(sid, index, frame, False, slow))
    if kind == "dplus":
        packets.append(dplus.last_voice(sid, len(frames)))
    else:
        packets.append(dextra.voice(sid, len(frames), bytes(9), True))
    return packets


def timed_run(rig, fake, src, frames, gap):
    """One stream: header, `gap` seconds, then the voice at 20 ms; every
    listener drained every 2 ms so receive times are arrival times. Returns
    (Result, probe): probe has the tester's send times (t_header, t_voice),
    the fake clock offset, the OPENs counted before the header and just before
    the first voice frame, and the fake log of this stream."""
    probe = {}
    peers = {}

    def on_linked(_talker, listeners):
        peers.update(listeners)

    def talk(talker, kind):
        sid = clients.fresh_stream_id(kind)
        packets = dstar_stream(kind, talker, sid, frames)
        drain = [talker] + list(peers.values())
        probe["offset"], probe["half_rtt"] = fake.clock_offset()
        stats = fake.ctl.stats()
        probe["seq0"], probe["opens_before"] = stats["last_seq"], stats["counters"].get("rx_open", 0)
        talker.client.send(talker.dest, packets[0])
        t_header = time.monotonic()
        if gap > 0.2:
            pump_all(drain, gap - 0.1)
            probe["opens_in_gap"] = fake.counters().get("rx_open", 0)
        stamps = paced_drain(talker.client, talker.dest, packets[1:], 0.020, drain, start=t_header + gap)
        pump_all(drain, 2.0)
        probe.update(t_header=t_header, t_voice=stamps[0])
        return sid

    res = scenario(rig, src, [XC1_DST], frames, talk=talk, on_linked=on_linked)
    fake.ctl.wait_for(lambda s: s["streams_live"] == 0, 4.0)
    probe["log"] = fake.log(since_seq=probe["seq0"])
    return res, probe


def first_voice_packet(want):
    """match() for the M17 packet of this stream that carries the first two
    transcoded voice units (packet n carries units 2n, 2n+1)."""
    first = b"".join(want[:2])

    def match(data):
        parsed = m17.parse_stream(data)
        return bool(parsed) and parsed["payload"] == first
    return match


def timing_of(res, probe, want):
    """crossmode.Timing of a timed_run plus the fake-side numbers (dict). The
    output delay uses the tester's own clock only (send stamp of the first
    voice frame, receive stamp of the M17 packet carrying the first transcoded
    units); the fake clock mapping is used for OPEN / STD / reply only."""
    to_local = lambda t: t - probe["offset"]                     # noqa: E731
    t_voice = probe["t_voice"]
    entries = probe["log"]
    opens = [e["t"] for e in entries if e["kind"] == "open" and e["dir"] == "rx"]
    stds = [e["t"] for e in entries if e["kind"] == "std" and e["dir"] == "tx"]
    replies = [e["t"] for e in entries if e["kind"] == "data_reply" and e["dir"] == "tx"]
    t_out = crossmode.first_arrival(res.times[XC1_DST], res.raw[XC1_DST], first_voice_packet(want))
    timing = crossmode.Timing(
        gap=t_voice - probe["t_header"],
        open_after_voice=to_local(opens[0]) - t_voice if opens else None,
        output_after_voice=None if t_out is None else t_out - t_voice)
    extra = {"std_after_voice": to_local(stds[0]) - t_voice if stds else None,
             "first_reply_after_voice": to_local(replies[0]) - t_voice if replies else None,
             "clock_error": probe["half_rtt"]}
    return timing, extra


def fmt(value):
    return "n/a" if value is None else "%.3f" % value


@pytest.mark.parametrize("open_ms", [0, 300], ids=lambda v: "open%d" % v)
@pytest.mark.parametrize("src", ["dstar", "dplus"])
def test_header_gap_costs_no_hidden_open(rig, fake, src, open_ms):
    """The same voice stream twice, D-STAR (DExtra) or DPlus to an M17
    listener: once with the first voice frame one frame after the header, once
    with a 500 ms voiceless header gap. Measured from the first voice frame
    sent: the OPEN at the fake, the first transcoded unit at the fake and at
    the listener. transcoding.md (XC1): CreateSession goes out in the turn of
    the first voice unit and the OPEN wait counts from it, so the OPEN no longer
    overlaps the header gap: the transcoded path starts later than with opening
    at grant by min(OPEN time, gap), i.e. the delay after the first voice frame
    includes the OPEN time and is the same with or without the gap. The fake
    answers OPEN after open_ms (open late:<ms>; 0 = the default std) to make
    that cost visible. Tolerances: dvtest.crossmode (OPEN_*_TOL, SAME_DELAY_TOL)."""
    if open_ms:
        fake.set(open="late:%d" % open_ms)
    frames = prng(src, seed="xc1/%s/%d" % (src, open_ms))
    want = crossmode.expected_listener(src, XC1_DST, frames).frames
    runs = {}
    for name, gap in (("no-gap", NO_GAP_S), ("header-gap", HEADER_GAP_S)):
        res, probe = timed_run(rig, fake, src, frames, gap)
        timing, extra = timing_of(res, probe, want)
        runs[name] = (res, probe, timing, extra)
        print("MEASURE xc1 %s open %d ms %s: header->voice %s s, OPEN %s s, STD %s s, first reply %s s, "
              "first %s output %s s after the first voice frame (clock error +-%s s)" % (
                  src, open_ms, name, fmt(timing.gap), fmt(timing.open_after_voice), fmt(extra["std_after_voice"]),
                  fmt(extra["first_reply_after_voice"]), XC1_DST, fmt(timing.output_after_voice),
                  fmt(extra["clock_error"])))
    fid = "xc1-%s-open%d" % (src, open_ms)
    gap_t, plain_t = runs["header-gap"][2], runs["no-gap"][2]
    fake.finding(fid + "-measure",
                 "deferred OPEN costs min(OPEN time, header gap) against opening at grant (transcoding.md, XC1)",
                 "first %s output after the first voice frame: %s s with a %s s header gap, %s s without; "
                 "OPEN after the first voice frame: %s s / %s s; fake OPEN delay %d ms" % (
                     XC1_DST, fmt(gap_t.output_after_voice), fmt(gap_t.gap), fmt(plain_t.output_after_voice),
                     fmt(gap_t.open_after_voice), fmt(plain_t.open_after_voice), open_ms))
    for name, (res, probe, _timing, _extra) in runs.items():
        fake.expect(first_diff(res.frames[XC1_DST], want) is None, "%s-%s-exact" % (fid, name),
                    "every unit transcoded once, in order", first_diff(res.frames[XC1_DST], want))
        sessions, _strays = crossmode.sessions(probe["log"])
        fake.expect(len(sessions) == 1, "%s-%s-one-session" % (fid, name), "one OPEN for the stream",
                    "%d OPENs" % len(sessions))
        if sessions:
            probs = crossmode.check_link(sessions[0], crossmode.CODEC[src], units_sent(src, frames),
                                         (crossmode.CODEC[XC1_DST],))
            fake.expect(not probs, "%s-%s-link" % (fid, name), "the data requests are the units in order",
                        "; ".join(probs))
    probe = runs["header-gap"][1]
    fake.expect(probe.get("opens_in_gap") == probe["opens_before"], fid + "-no-open-in-header-gap",
                "no OPEN while only the header was sent (XC1)",
                "%s OPEN(s) during the %.3f s gap" % (probe.get("opens_in_gap", 0) - probe["opens_before"], HEADER_GAP_S))
    for pid, documented, observed in crossmode.header_cost_problems(gap_t, plain_t, open_ms / 1000.0):
        fake.expect(False, "%s-%s" % (fid, pid), documented, observed)
