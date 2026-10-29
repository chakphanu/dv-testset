# SPDX-License-Identifier: MIT
"""AMBEd transcoder wire conformance: PING, CAPS, legacy and extended
opens, transactions, data replies, PCM round trip, stream cap.

Implementation neutral: which codecs are expected to work comes from the
transcoder's own CAPS reply. Inputs are generated (tone bursts), never
shipped audio. Needs DVT_AMBED_ADDR (or TRANSCODER_IMAGE).
"""
import socket
import time

import pytest

from dvtest.audio import score, signals
from dvtest.net import pace
from dvtest.proto import ambed

pytestmark = pytest.mark.wire

CALLSIGN = "N0TEST"
# streams that break the protocol on purpose (A18-A20): dvcheck turns their
# findings into tagged warnings
FAULT_CALLSIGN = "N0FAULT"
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


def open_std(ctl, codec_in, outs, transaction, callsign=CALLSIGN):
    reply = ctl.rpc(ambed.open_ext(callsign, codec_in, outs, transaction))
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



def test_legacy_open_full_gets_busy9(ctl):
    """Legacy 19 B opens until the transcoder is full: the refusal is the
    9 B legacy BUSY (no transaction). Bounded at 200 opens."""
    opened = []
    refusal = None
    try:
        for _ in range(200):
            reply = ctl.rpc(ambed.open_legacy(CALLSIGN, ambed.AMBEPLUS, ambed.AMBE2PLUS,
                                              ambed.CODEC2_3200, ambed.IMBE)) or b""
            std = ambed.parse_std(reply)
            if std is None or len(reply) != 14:
                refusal = reply
                break
            opened.append(std)
    finally:
        for std in opened:
            ctl.close_stream(std.stream_id, settle=0)
    if refusal is None:
        pytest.skip("200 legacy opens all succeeded, no BUSY to check")
    print("legacy BUSY after %d streams" % len(opened))
    assert refusal == ambed.busy(), "refusal after %d streams: %r" % (len(opened), refusal)

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



def test_ext_open_same_tx_other_codecs(ctl, caps):
    """Only peer + transaction + codec_in + out1..4 together identify a
    retransmit: the same transaction with other codecs is a new open (new
    stream or BUSY13), an exact resend gets the identical STD18."""
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    tx = 0x5A5A0001
    first_req = ambed.open_ext(CALLSIGN, ambed.CODEC2_3200, [ambed.PCM], tx)
    other_req = ambed.open_ext(CALLSIGN, ambed.PCM, [ambed.CODEC2_3200], tx)
    first = ctl.rpc(first_req) or b""
    std1 = ambed.parse_std(first)
    opened = [std1] if std1 else []
    try:
        assert std1 is not None and std1.transaction == tx, "first open: %r" % first
        other = ctl.rpc(other_req) or b""
        std2 = ambed.parse_std(other)
        if std2 is not None:
            opened.append(std2)
            assert len(other) == 18 and std2.transaction == tx, other
            assert std2.stream_id != std1.stream_id, "same transaction, other codecs got the first stream"
            assert (std2.codec_in, std2.codec_out) == (ambed.PCM, ambed.CODEC2_3200), other
        else:
            assert other == ambed.busy(tx), "other codecs, same transaction: %r" % other
        assert ctl.rpc(first_req) == first, "exact resend must return the identical STD18"
        if std2 is not None:
            assert ctl.rpc(other_req) == other, "resend of the second open must return its own STD18"
    finally:
        for std in opened:
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


def test_imbe_follows_caps(ctl, caps, ambed_addr):
    """IMBE in the CAPS mask: an open mixing IMBE with another output gets
    STD18 and real IMBE payloads. Not in the mask: an extended open asking
    IMBE gets BUSY13, while a legacy 19 B open with IMBE as out3 still gets
    STD14 (that path never checked outputs; payloads not asserted)."""
    if served(caps, ambed.IMBE):
        need(caps, ambed.PCM, ambed.CODEC2_3200)
        parsed = stream(ctl, ambed_addr, ambed.PCM, [ambed.IMBE, ambed.CODEC2_3200], source_pcm(), 0x494D0001)
        imbe = [p[ambed.IMBE] for p in parsed]
        assert any(any(f) for f in imbe), "all IMBE payloads are zero"
        return
    tx = 0x494D0002
    reply = ctl.rpc(ambed.open_ext(CALLSIGN, ambed.CODEC2_3200, [ambed.PCM, ambed.IMBE], tx)) or b""
    std = ambed.parse_std(reply)
    if std is not None:
        ctl.close_stream(std.stream_id)
    assert reply == ambed.busy(tx), "IMBE not announced, extended open: %r" % reply
    need(caps, ambed.AMBEPLUS, ambed.AMBE2PLUS, ambed.CODEC2_3200)
    reply = ctl.rpc(ambed.open_legacy(CALLSIGN, ambed.AMBEPLUS, ambed.AMBE2PLUS,
                                      ambed.CODEC2_3200, ambed.IMBE)) or b""
    std = ambed.parse_std(reply)
    if std is not None:
        ctl.close_stream(std.stream_id)
    assert std is not None and len(reply) == 14, "legacy open with IMBE out3: %r" % reply

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



# ---------------------------------------------------------------- stream life

class DataPort:
    """One local socket on a stream port, kept across open/close so late
    replies are seen. ambed answers the opener address at the stream port."""

    def __init__(self, addr, port, local_port=None):
        self.dest = (addr, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port if local_port is None else local_port))
        self.sock.setblocking(False)

    def send(self, codec, pid, payload):
        self.sock.sendto(ambed.data_request(codec, pid, payload), self.dest)

    def recv_all(self, seconds):
        out = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                out.append(self.sock.recvfrom(4096)[0])
            except BlockingIOError:
                time.sleep(0.01)
        return out

    def close(self):
        self.sock.close()


def send_paced(port, codec, frames, first_pid=0):
    start = time.monotonic()
    for index, frame in enumerate(frames):
        pace(start, index)
        port.send(codec, first_pid + index, frame)


def test_close_stops_replies(ctl, caps, ambed_addr):
    """A18: after AMBEDCS the stream answers no more data."""
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    std = open_std(ctl, ambed.PCM, [ambed.CODEC2_3200], 0x41180001, callsign=FAULT_CALLSIGN)
    port = DataPort(ambed_addr[0], std.port)
    try:
        send_paced(port, ambed.PCM, source_pcm()[:10])
        before = port.recv_all(1.0)
        ctl.close_stream(std.stream_id, settle=0.3)
        port.recv_all(0.2)
        send_paced(port, ambed.PCM, source_pcm()[10:20], first_pid=10)
        after = port.recv_all(1.5)
    finally:
        port.close()
    assert len(before) == 10, "open stream answered %d of 10 frames" % len(before)
    assert not after, "%d replies after AMBEDCS" % len(after)


IDLE_TIMEOUT_S = 3.0        # reference ambed stream activity timeout


def test_idle_stream_times_out(ctl, caps, ambed_addr):
    """A19: a stream that gets no data is closed by the transcoder after
    its idle timeout (reference 3 s): data sent later gets no reply. The
    timeout is not advertised; the reference value plus 2 s is used."""
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    std = open_std(ctl, ambed.PCM, [ambed.CODEC2_3200], 0x41190001, callsign=FAULT_CALLSIGN)
    port = DataPort(ambed_addr[0], std.port)
    try:
        time.sleep(IDLE_TIMEOUT_S + 2.0)
        send_paced(port, ambed.PCM, source_pcm()[:5])
        late = port.recv_all(1.5)
    finally:
        port.close()
        ctl.close_stream(std.stream_id)
    assert not late, "idle stream still answered %d of 5 frames %.0f s after the open" % (
        len(late), IDLE_TIMEOUT_S + 2.0)
    # the slot is usable again: a fresh open succeeds
    again = open_std(ctl, ambed.PCM, [ambed.CODEC2_3200], 0x41190002)
    ctl.close_stream(again.stream_id)


@pytest.mark.xfail(reason="reference ambed takes stream data from any source: cstream.cpp reads the "
                          "sender address and never compares it with the opener (PR #272 tree)",
                   strict=False)
def test_data_from_other_port_ignored(ctl, caps, ambed_addr):
    """A20: data for a stream from another source port of the opener's
    address is ignored (no reply to either socket). A different source
    IP is not tested: the tester has one address. Expected to fail on
    transcoders that follow the reference (xfail, reported, not hidden)."""
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    std = open_std(ctl, ambed.PCM, [ambed.CODEC2_3200], 0x41200001, callsign=FAULT_CALLSIGN)
    owner = DataPort(ambed_addr[0], std.port)
    stranger = DataPort(ambed_addr[0], std.port, local_port=0)
    try:
        send_paced(stranger, ambed.PCM, source_pcm()[:10], first_pid=100)
        to_owner = owner.recv_all(1.0)
        to_stranger = stranger.recv_all(0.2)
        send_paced(owner, ambed.PCM, source_pcm()[:5])
        own = owner.recv_all(1.0)
    finally:
        owner.close()
        stranger.close()
        ctl.close_stream(std.stream_id)
    assert not to_stranger, "%d replies to the foreign source port" % len(to_stranger)
    assert not to_owner, "data from a foreign port was transcoded (%d replies, pids %s)" % (
        len(to_owner), sorted({r[1] for r in to_owner}))
    assert len(own) == 5, "the opener still gets answers (%d of 5)" % len(own)


CONCURRENT = 8


def test_concurrent_streams_do_not_mix(ctl, caps, ambed_addr):
    """A21: eight streams, each fed its own tone, interleaved frame by
    frame: every stream returns exactly what the same input gives alone."""
    need(caps, ambed.CODEC2_3200, ambed.PCM)
    inputs = [signals.frames(signals.tone(400 + 150 * k, 600)) for k in range(CONCURRENT)]
    solo = [stream(ctl, ambed_addr, ambed.PCM, [ambed.CODEC2_3200], frames, 0x41210100 + k)
            for k, frames in enumerate(inputs)]
    stds = [open_std(ctl, ambed.PCM, [ambed.CODEC2_3200], 0x41210200 + k) for k in range(CONCURRENT)]
    ports = [DataPort(ambed_addr[0], std.port) for std in stds]
    got = [[] for _ in range(CONCURRENT)]
    try:
        start = time.monotonic()
        for index in range(len(inputs[0])):
            pace(start, index)
            for k, port in enumerate(ports):
                port.send(ambed.PCM, index, inputs[k][index])
            for k, port in enumerate(ports):
                got[k] += port.recv_all(0)
        for k, port in enumerate(ports):
            got[k] += port.recv_all(1.0 if k == 0 else 0.1)
    finally:
        for port in ports:
            port.close()
        for std in stds:
            ctl.close_stream(std.stream_id)
    for k in range(CONCURRENT):
        payloads = [dict(ambed.parse_data_reply(r, [ambed.CODEC2_3200])[1])[ambed.CODEC2_3200]
                    for r in got[k]]
        expected = [p[ambed.CODEC2_3200] for p in solo[k]]
        assert payloads == expected, "stream %d (%d Hz): %d replies, differs from its solo run" % (
            k, 400 + 150 * k, len(payloads))
