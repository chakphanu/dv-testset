# SPDX-License-Identifier: MIT
"""AMBEd transcoder wire conformance: PING, CAPS, legacy and extended
opens, transactions, data replies, PCM round trip, stream cap.

Implementation neutral: which codecs are expected to work comes from the
transcoder's own CAPS reply. Inputs are generated (tone bursts), never
shipped audio. Needs DVT_AMBED_ADDR (or TRANSCODER_IMAGE).
"""
import pytest

from dvtest.audio import score, signals
from dvtest.proto import ambed

pytestmark = pytest.mark.wire

CALLSIGN = "N0TEST"
FRAMES = 50


@pytest.fixture(scope="module")
def ctl(ambed_addr):
    client = ambed.AmbedClient(*ambed_addr)
    yield client
    client.close()


@pytest.fixture(scope="module")
def caps(ctl):
    reply = ctl.rpc(ambed.CAPS_REQUEST)
    assert reply is not None, "no reply to AMBEDCAPS (transcoder without the extension?)"
    parsed = ambed.parse_caps(reply)
    assert parsed is not None, "CAPS reply is not 15 bytes: %r" % reply
    return parsed


def served(caps, *codecs):
    return all(caps.mask & (1 << c) for c in codecs)


def need(caps, *codecs):
    if not served(caps, *codecs):
        pytest.skip("transcoder does not announce codecs %s (mask 0x%02x)" % (codecs, caps.mask))


def open_std(ctl, codec_in, outs, transaction):
    reply = ctl.rpc(ambed.open_ext(CALLSIGN, codec_in, outs, transaction))
    std = ambed.parse_std(reply or b"")
    assert std is not None and std.transaction is not None, \
        "extended open %d->%s: expected STD18, got %r" % (codec_in, list(outs), reply)
    return std


def stream(ctl, addr, codec_in, outs, frames, transaction):
    """Open, run frames, close. Returns per-frame {codec: payload}."""
    std = open_std(ctl, codec_in, outs, transaction)
    try:
        replies = ambed.run_stream(addr[0], std.port, codec_in, outs, frames)
    finally:
        ctl.close_stream(std.stream_id)
    size = ambed.reply_size(outs)
    assert len(replies) == len(frames), "got %d replies for %d frames" % (len(replies), len(frames))
    bad_len = [i for i, r in enumerate(replies) if len(r) != size]
    assert not bad_len, "replies not %d bytes: %s" % (size, bad_len[:5])
    parsed = []
    for index, reply in enumerate(replies):
        pid, entries = ambed.parse_data_reply(reply, outs)
        assert pid == index & 0xFF, "reply %d carries pid %d" % (index, pid)
        parsed.append(dict(entries))
    return parsed


def test_ping_pong(ctl):
    assert ctl.rpc(ambed.ping("XLX999")) == ambed.PONG


def test_caps_reply(caps):
    assert caps.version == 1
    assert 1 <= caps.max_outputs <= ambed.MAX_OUTPUTS
    assert caps.mask and not caps.mask & ~ambed.MASK_ALL, "mask 0x%08x" % caps.mask
    print("CAPS max_outputs=%d mask=0x%02x codecs=%s"
          % (caps.max_outputs, caps.mask, ambed.codecs_in(caps.mask)))


LEGACY_OPENS = {
    "19B_ambe_3out": (ambed.AMBEPLUS, ambed.AMBE2PLUS, ambed.CODEC2_3200, ambed.IMBE),
    "18B_ambe_2out": (ambed.AMBEPLUS, ambed.AMBE2PLUS, ambed.CODEC2_3200),
    "17B_codec2_1out": (ambed.CODEC2_3200, ambed.AMBEPLUS),
}


@pytest.mark.parametrize("name", list(LEGACY_OPENS))
def test_legacy_open_gets_legacy_reply(ctl, name):
    """A legacy open never gets an extended (18/13 B) reply."""
    codec_in, *outs = LEGACY_OPENS[name]
    reply = ctl.rpc(ambed.open_legacy(CALLSIGN, codec_in, *outs))
    assert reply is not None, "no reply"
    std = ambed.parse_std(reply)
    if std is not None:
        ctl.close_stream(std.stream_id)
        assert len(reply) == 14 and std.codec_in == codec_in, reply
    else:
        assert reply == ambed.busy(), reply


@pytest.mark.parametrize("name", list(LEGACY_OPENS))
def test_legacy_open_matches_reference(ctl, ambed_ref_addr, name):
    """Same reply kind (STD14 or BUSY9) as the reference transcoder."""
    codec_in, *outs = LEGACY_OPENS[name]
    request = ambed.open_legacy(CALLSIGN, codec_in, *outs)
    ref = ambed.AmbedClient(*ambed_ref_addr)
    try:
        replies = []
        for client in (ctl, ref):
            reply = client.rpc(request) or b""
            std = ambed.parse_std(reply)
            if std is not None:
                client.close_stream(std.stream_id)
            replies.append((len(reply), reply[:8]))
    finally:
        ref.close()
    assert replies[0] == replies[1], replies


def test_ext_open_unserved_codec_busy(ctl, caps):
    """An extended open with a codec outside the CAPS mask gets BUSY13."""
    missing = [c for c in ambed.CODECS if not served(caps, c)]
    if not missing:
        pytest.skip("transcoder announces every codec")
    for index, codec in enumerate(missing):
        other = next(c for c in (ambed.PCM, ambed.CODEC2_3200, ambed.AMBE2PLUS) if c != codec)
        for codec_in, outs in ((codec, [other]), (other, [codec])):
            tx = 0x11223344 + 2 * index + (codec_in == other)
            reply = ctl.rpc(ambed.open_ext(CALLSIGN, codec_in, outs, tx))
            std = ambed.parse_std(reply or b"")
            if std is not None:
                ctl.close_stream(std.stream_id)
            assert reply == ambed.busy(tx), "%d->%s: %r" % (codec_in, outs, reply)


INVALID_OPENS = {
    "dup_outputs": (ambed.CODEC2_3200, [ambed.PCM, ambed.PCM], CALLSIGN),
    "gap": (ambed.CODEC2_3200, [ambed.PCM, 0, ambed.IMBE], CALLSIGN),
    "out_equals_in": (ambed.CODEC2_3200, [ambed.CODEC2_3200], CALLSIGN),
    "no_outputs": (ambed.CODEC2_3200, [], CALLSIGN),
    "bad_codec": (ambed.CODEC2_3200, [6], CALLSIGN),
    "bad_callsign": (ambed.CODEC2_3200, [ambed.PCM], b"\0" * 8),
}


@pytest.mark.parametrize("name", list(INVALID_OPENS))
def test_ext_open_invalid_busy(ctl, name):
    """An extended open always gets a reply: invalid ones BUSY13 + tx."""
    codec_in, outs, callsign = INVALID_OPENS[name]
    tx = 0x7000 + list(INVALID_OPENS).index(name)
    reply = ctl.rpc(ambed.open_ext(callsign, codec_in, outs, tx))
    std = ambed.parse_std(reply or b"")
    if std is not None:
        ctl.close_stream(std.stream_id)
    assert reply == ambed.busy(tx), reply


def test_ext_open_std18_and_retransmit(ctl, caps):
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    tx = 0xA1B2C3D4
    request = ambed.open_ext(CALLSIGN, ambed.CODEC2_3200, [ambed.PCM], tx)
    first = ctl.rpc(request)
    std = ambed.parse_std(first or b"")
    try:
        assert std is not None and len(first) == 18, first
        assert (std.codec_in, std.codec_out, std.transaction) == (ambed.CODEC2_3200, ambed.PCM, tx)
        assert std.port, "stream port 0"
        assert ctl.rpc(request) == first, "retransmitted open must get the same STD18"
    finally:
        if std is not None:
            ctl.close_stream(std.stream_id)


def source_pcm():
    pcm = signals.bursts(700, 4, on_ms=200, off_ms=200)
    return signals.frames(pcm)[:FRAMES]


@pytest.fixture(scope="module")
def codec2_frames(ctl, caps, ambed_addr):
    """PCM -> Codec2 of the tone bursts; feeds the Codec2 input cases."""
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    parsed = stream(ctl, ambed_addr, ambed.PCM, [ambed.CODEC2_3200], source_pcm(), 0x0BADF00D)
    return [p[ambed.CODEC2_3200] for p in parsed]


def test_pcm_to_codec2(codec2_frames):
    assert len(codec2_frames) == FRAMES
    assert len(set(codec2_frames)) > 1, "all Codec2 frames identical"


def test_codec2_to_pcm_nonzero(ctl, ambed_addr, codec2_frames):
    parsed = stream(ctl, ambed_addr, ambed.CODEC2_3200, [ambed.PCM], codec2_frames, 0xA1B2C3D5)
    nonzero = sum(1 for p in parsed if score.peak(p[ambed.PCM]) > 0)
    assert nonzero >= FRAMES * 0.4, "nonzero PCM frames %d/%d" % (nonzero, FRAMES)


def test_codec2_multi_output_round_trip(ctl, caps, ambed_addr, codec2_frames):
    """Codec2 -> [every other served codec ..., PCM]: entries in open order,
    and the PCM keeps the tone-burst envelope of the source."""
    others = [c for c in ambed.codecs_in(caps.mask) if c not in (ambed.CODEC2_3200, ambed.PCM)]
    outs = others[:caps.max_outputs - 1] + [ambed.PCM]
    if len(outs) < 2:
        pytest.skip("needs two outputs for Codec2 input (mask 0x%02x)" % caps.mask)
    parsed = stream(ctl, ambed_addr, ambed.CODEC2_3200, outs, codec2_frames, 0x0BADF00E)
    pcm = b"".join(p[ambed.PCM] for p in parsed)
    corr, lag = score.envelope_correlation(b"".join(source_pcm()), pcm)
    print("outs %s envelope correlation %.3f at lag %d frames" % (outs, corr, lag))
    assert corr > 0.6, "round trip envelope correlation %.3f" % corr


def test_pcm_two_outputs_keep_order(ctl, caps, ambed_addr):
    """PCM -> two outputs in descending codec order: replies keep the open
    order, not ascending order."""
    need(caps, ambed.PCM)
    outs = [c for c in ambed.codecs_in(caps.mask) if c != ambed.PCM][::-1][:2]
    if len(outs) < 2 or caps.max_outputs < 2:
        pytest.skip("needs two non-PCM outputs (mask 0x%02x, max %d)" % (caps.mask, caps.max_outputs))
    stream(ctl, ambed_addr, ambed.PCM, outs, source_pcm(), 0x0BADF00F)


def test_stream_cap(ctl, caps, target):
    """DVT_AMBED_STREAM_CAP opens succeed, the next one gets BUSY13."""
    cap = target.ambed_stream_cap
    if not cap:
        pytest.skip("set DVT_AMBED_STREAM_CAP=<n> to check the stream limit")
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    replies = []
    try:
        for index in range(cap + 1):
            replies.append(ctl.rpc(ambed.open_ext(CALLSIGN, ambed.CODEC2_3200, [ambed.PCM],
                                                  0x5000 + index)) or b"")
    finally:
        for reply in replies:
            std = ambed.parse_std(reply)
            if std is not None:
                ctl.close_stream(std.stream_id)
    assert all(len(r) == 18 for r in replies[:cap]), [len(r) for r in replies]
    assert replies[cap] == ambed.busy(0x5000 + cap), replies[cap]

