# SPDX-License-Identifier: MIT
"""Cross-mode traffic of DV-Router checked byte for byte against the fake
extended AMBEd (DVT_TRANSCODER_FAKE=1 scripts/run.sh, docs/ambed-fake.md).

The fake's transform is deterministic (out[i] = in[i mod w] ^ K(src,dst) ^ i),
so the codec units a listener receives are predictable from the units the
talker sent: every payload byte, the count, the order, no duplicate and exactly
one transform per listener codec (a wrong hop count or direction shows as a
different K). The expectation comes from DV-Router's written design
(media-format.md "Unit granularity", "End unit", "Cross-mode egress",
"Expected counts"; transcoding.md "Controller transcoding", "Media") through
dvtest.crossmode; nothing is derived from observed behaviour. Where the
documents are silent the case records a FINDING instead of asserting.

Profile dvrouter only; the fixtures skip with the reason otherwise.
"""
import pytest

from dvtest import clients, crossmode, specs
from dvtest.proto import dcs, dextra, m17

from crossmode_fake import dstar_voice, first_diff, prng, scenario, units_sent

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
    join): the sut transcodes that stream's single end unit in a session of its
    own; it is checked and recorded as a FINDING, the talk is the next session."""
    outs = tuple(sorted({crossmode.CODEC[d] for d in dsts} - {crossmode.CODEC[src]}))
    joins = 1 if src == "dplus" else 0
    sessions, strays = fake.sessions(expect_cs=count_sessions + joins)
    assert len(sessions) == count_sessions + joins, "%d OPENs, expected %d; strays %d; sessions %s" % (
        len(sessions), count_sessions + joins, len(strays),
        [(x.open[3], x.open[1], x.open[2], len(x.requests), len(x.replies), x.cs, x.busy) for x in sessions])
    if joins:
        join = sessions[0]
        problems = crossmode.check_link(join, crossmode.CODEC[src], [crossmode.DPLUS_END_AMBE], outs)
        assert not problems, "join stream: " + "; ".join(problems)
        fake.finding("dplus-join-stream", "a header-only stream has no voice unit (media-format.md, End unit)",
                     "the DPlus link header opened a transcode session of its own with the end unit alone")
        sessions = sessions[1:]
    problems = crossmode.check_link(sessions[0], crossmode.CODEC[src], units_sent(src, frames), outs)
    assert not problems, "; ".join(problems)
    assert len(sessions[0].cs) == 1, "CS for the stream sent %d times" % len(sessions[0].cs)
    assert not strays, "%d datagrams the fake dropped or could not place" % len(strays)
    return sessions[0]


def assert_listener(fake, label, got, expect):
    """Exact comparison; a listener whose tail the documents leave open gets
    a prefix comparison and a FINDING for what follows."""
    if expect.exact:
        diff = first_diff(got, expect.frames)
        assert diff is None, "%s: %s" % (label, diff)
        return
    diff = first_diff(got[:expect.prefix], expect.frames[:expect.prefix])
    assert diff is None, "%s: %s" % (label, diff)
    print("NOTE %s: %d frames, %d documented (%s)" % (label, len(got), expect.prefix, expect.note))
    if len(got) != expect.prefix:
        fake.finding("%s-tail" % label, "%s: %d frames documented (%s)" % (label, expect.prefix, expect.note),
                     "%d frames, extra %s" % (len(got), [bytes(f).hex()[:24] for f in got[expect.prefix:]]))


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
    res = scenario(rig, src, [dst], frames)
    expect = crossmode.expected_listener(src, dst, frames)
    assert_listener(fake, cell_id(cell), res.frames[dst], expect)
    verify_link(fake, src, frames, [dst])
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
        assert_listener(fake, "%s-%s" % (src, dst), res.frames[dst], expect)
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
