# SPDX-License-Identifier: MIT
"""USRP endpoint conformance (docs/protocols/usrp.md): one case per
obligation P-USR-NN. The tester is the USRP peer of module C of xlxd-sut
(rig/config/xlxd.usrp.in: xlxd .11:34003 <-> tester .30:32003); the rig's
AllStar node (app_rpt + chan_usrp) is the real peer of modules A and B.

xlxd-ref has no USRP, so nothing is differential. Judged as in usrp.md:
conv = an implementation convention the clients rely on, asserted on the
system under test for every profile; dev = disputed: recorded only under
the xlxd profile, else the profile's "usrp" spec entry {"P-USR-NN": {key:
value}} decides (skip "spec pending" without one); real = checked against
the real chan_usrp through the run.sh control channel ("asl ..."). B3, B6
and B9 items live in test_floor, test_backend_loss and test_allstar. Every
case prints what the system under test did.
"""
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import score, signals
from dvtest.net import pace
from dvtest.proto import ambed, dextra, usrp

pytestmark = [pytest.mark.proto, pytest.mark.rig, pytest.mark.allstar]

MOD = "C"                                       # USRP endpoint of the tester
XLXD_PORT, PEER_PORT = 34003, 32003
FOREIGN_PORT = 32099                            # same tester IP, another port
FRAMES = 50                                     # one second of voice
TONE = 1000
FLUSH = 25                                      # chan_usrp drops its queue above 25 frames


# ---------------------------------------------------------------- helpers

def peer_socket(port=PEER_PORT):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)
    return sock


def drain(sock, out):
    while True:
        try:
            out.append((time.monotonic(), sock.recv(2048)))
        except BlockingIOError:
            return out


def listen(sock, seconds, out=None):
    out = [] if out is None else out
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        drain(sock, out)
        time.sleep(0.002)
    return out


def tone_pcm(n=FRAMES, freq=TONE):
    return signals.frames(signals.tone(freq, n * 20))


def ambe_tone(rig, n=FRAMES, freq=TONE):
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [ambed.AMBEPLUS], tone_pcm(n, freq), port=rig.ambed_port)[1]


def dstar_listener(rig, index=1):
    client = clients.make("dstar", MOD, index)
    assert client.link(rig.xlxd_sut), "D-STAR client did not link module %s" % MOD
    client.client.drain()
    client.client.inbox.clear()
    return client


def dstar_streams(packets):
    """{stream id: {"header": dict or None, "frames": n, "last": bool}}."""
    out = {}
    for data in packets:
        head = dextra.parse_header(data)
        if head:
            out.setdefault(head["stream_id"], {"header": head, "frames": 0, "last": False})["header"] = head
            continue
        voice = dextra.parse_voice(data)
        if voice:
            entry = out.setdefault(voice[0], {"header": None, "frames": 0, "last": False})
            if voice[2]:
                entry["last"] = True
            else:
                entry["frames"] += 1
    return out


_NEXT_SEQ = [0]


def next_seqs(count):
    """The tester's own sequence numbers: one counter for the whole run,
    as chan_usrp keeps one per channel (so only deliberate cases show seq
    anomalies in the capture check)."""
    base = _NEXT_SEQ[0]
    _NEXT_SEQ[0] += count
    return list(range(base, base + count))


def usrp_over(sock, dest, pcm, seqs=None, keyup0_at=None, unkey=True):
    """Send one paced USRP over (352 B keyed frames) and the 32 B unkey;
    seqs overrides the sequence numbers (the run counter continues after
    the last one), keyup0_at sends that frame with keyup 0 (pyUC's unkey
    form, audio included)."""
    seqs = next_seqs(len(pcm) + 1) if seqs is None else seqs
    _NEXT_SEQ[0] = seqs[-1] + 1
    start = time.monotonic() + 0.05
    for index, frame in enumerate(pcm):
        pace(start, index)
        packet = usrp.voice(seqs[index], frame)
        if index == keyup0_at:
            packet = usrp.header(seqs[index], 0) + frame
        sock.sendto(packet, dest)
    if unkey:
        sock.sendto(usrp.unkey(seqs[len(pcm)]), dest)
    return time.monotonic()


def dstar_over(rig, frames, last=True):
    """A D-STAR talker on module C sends frames (paced); returns its
    stream id. last=False leaves out the last-frame packet."""
    talker = clients.make("dstar", MOD, 0)
    try:
        assert talker.link(rig.xlxd_sut)
        sid = clients.fresh_stream_id("dstar")
        talker.client.send(talker.dest, talker.header(sid))
        start = time.monotonic() + 0.1
        for index, frame in enumerate(frames):
            pace(start, index)
            slow = dextra.SYNC_DATA if index % 21 == 0 else dextra.SLOW_DATA
            talker.client.send(talker.dest, dextra.voice(sid, index, frame, False, slow=slow))
        if last:
            talker.client.send(talker.dest, talker.voice(sid, len(frames), bytes(9), True))
    finally:
        clients.close(talker)
    return sid


def during(sock, func, after=1.0):
    """Run func() in a thread while recording what the peer socket gets
    (timestamped as it arrives); keep listening after seconds more."""
    got, errors = [], []

    def run():
        try:
            func()
        except Exception as exc:                    # re-raised below
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    while thread.is_alive():
        drain(sock, got)
        time.sleep(0.001)
    listen(sock, after, got)
    if errors:
        raise errors[0]
    return got


def mark(rig, phase, tag):
    """Control channel "mark begin|end <tag>": brackets deliberately
    malformed ingress so dvcheck can tell it from a fault (no-op without
    the control channel)."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "usrp-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def keyed(packets):
    return [(t, p) for t, p in packets if len(p) == usrp.VOICE_SIZE and usrp.parse(p) and usrp.parse(p).keyup == 1]


def unkeys(packets):
    return [(t, p) for t, p in packets if len(p) == usrp.HEADER_SIZE and usrp.parse(p)]


def judge_dev(rig, nn, observed):
    """dev items: recorded under the xlxd profile (xlxd-ref has no USRP),
    else the profile's usrp spec entry for nn decides."""
    print("%s observed: %r" % (nn, observed))
    if rig.sut_profile == specs.DEFAULT:
        return
    entry = (specs.rule(rig, "usrp") or {}).get(nn) or {}
    pending = [key for key in observed if key not in entry]
    for key, value in observed.items():
        if key in entry:
            assert value == entry[key], "%s %s: sut %r, spec %r" % (nn, key, value, entry[key])
    if pending:
        pytest.skip("spec pending: no usrp %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def asl(rig, command, timeout=30.0):
    """Control channel "asl <command>": returns the command's output."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1 is set by scripts/run.sh)")
    if not rig.is_given("allstar"):
        pytest.skip("no AllStar node (run.sh --profile allstar)")
    directory = Path(rig.out) / "control"
    directory.mkdir(parents=True, exist_ok=True)
    name = "usrp-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("asl %s\n" % command)
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    ack = directory / (name + ".ack")
    deadline = time.monotonic() + timeout
    while not ack.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("no ack for asl %s" % command)
        time.sleep(0.1)
    reply = ack.read_text().strip()
    out = directory / (name + ".out")
    text = out.read_text() if out.exists() else ""
    if reply != "ok":
        raise RuntimeError("asl %s: %s %s" % (command, reply, text))
    return text


def usrp_counters(text):
    """{xlxd port: (read, write)} from chan_usrp's "usrp show"."""
    out = {}
    for m in re.finditer(r"Channel [0-9.]+:(\d+):\d+: .*?Read (\d+), Write (\d+)", text):
        out[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
    return out


# ---------------------------------------------------------------- egress (P-USR-01..06)

@pytest.fixture(scope="module")
def egress(rig_usrp):
    """Two D-STAR overs (1 kHz tone, with last frame) on module C, as the
    USRP peer receives them: [[(t, packet), ...] per over]."""
    frames = ambe_tone(rig_usrp)
    sock = peer_socket()
    overs = []
    try:
        for _ in range(2):
            overs.append(during(sock, lambda: dstar_over(rig_usrp, frames)))
    finally:
        sock.close()
    return overs


def test_usrp_egress_voice_frame(egress):
    """P-USR-01 (conv): voice is 352 B, "USRP", type 0, keyup 1."""
    voice = [p for over in egress for t, p in over if len(p) != usrp.HEADER_SIZE]
    print("egress packets per over: %s" % [len(o) for o in egress])
    assert len(voice) >= FRAMES * 2 * 0.8, "only %d voice frames for two overs" % len(voice)
    bad = [p[:32].hex() for p in voice if not (len(p) == usrp.VOICE_SIZE and p[:4] == b"USRP"
                                               and p[12:16] == b"\0\0\0\1" and p[20:24] == bytes(4))]
    assert not bad, "%d voice packets not 352 B / type 0 / keyup 1, first %s" % (len(bad), bad[0])


def test_usrp_egress_fields_zero(egress):
    """P-USR-02 (conv): memory, talkgroup, type, mpxid, reserved zero."""
    bad = [p[:32].hex() for over in egress for t, p in over
           if p[8:12] != bytes(4) or p[16:32] != bytes(16)]
    assert not bad, "%d packets with non-zero spare fields, first %s" % (len(bad), bad[0])


def test_usrp_pcm_byte_order(egress):
    """P-USR-03 (conv): PCM is S16LE: a 1 kHz tone in, 1 kHz out; the
    byte-swapped reading is not the tone."""
    pcm = b"".join(p[usrp.HEADER_SIZE:] for over in egress for t, p in keyed(over))
    swapped = b"".join(pcm[i + 1:i + 2] + pcm[i:i + 1] for i in range(0, len(pcm) - 1, 2))
    le, be = score.tone_ratio(pcm, TONE), score.tone_ratio(swapped, TONE)
    print("1 kHz share: read LE %.2f, read BE %.2f, peak %d" % (le, be, score.peak(pcm)))
    assert le > 0.8 and be < le - 0.3, "the egress PCM is not a little-endian 1 kHz tone"


def test_usrp_egress_seq(egress, rig_usrp):
    """P-USR-04: seq BE, +1 per packet within an over (conv); across
    overs continuous or reset (dev, key "seq")."""
    seqs = [[usrp.parse(p).seq for t, p in over] for over in egress]
    for over in seqs:
        steps = {b - a for a, b in zip(over, over[1:])}
        assert steps <= {1}, "seq steps %s within an over" % sorted(steps)
    mode = "continuous" if seqs[1][0] == seqs[0][-1] + 1 else ("reset" if seqs[1][0] == 0 else "other")
    print("over 1 seq %d..%d, over 2 seq %d..%d" % (seqs[0][0], seqs[0][-1], seqs[1][0], seqs[1][-1]))
    judge_dev(rig_usrp, "P-USR-04", {"seq": mode})


def test_usrp_egress_pacing(egress):
    """P-USR-05 (conv): voice paced ~20 ms; never more than 25 frames
    ahead of a 20 ms play-out (chan_usrp flushes its queue above 25)."""
    worst_queue, gaps = 0, []
    for over in egress:
        times = [t for t, p in keyed(over)]
        gaps += [b - a for a, b in zip(times, times[1:])]
        for index, t in enumerate(times):
            played = int((t - times[0]) / 0.020) + 1
            worst_queue = max(worst_queue, index + 1 - played)
    gaps.sort()
    median = gaps[len(gaps) // 2]
    print("gap median %.1f ms, max %.1f ms, min %.1f ms; worst play-out queue %d frames" % (
        median * 1000, gaps[-1] * 1000, gaps[0] * 1000, worst_queue))
    assert 0.015 <= median <= 0.025, "median frame gap %.1f ms" % (median * 1000)
    assert worst_queue <= FLUSH, "%d frames ahead of play-out (chan_usrp flushes above %d)" % (worst_queue, FLUSH)


def test_usrp_single_unkey(egress):
    """P-USR-06 (conv): each over ends with exactly one 32 B unkey (keyup
    0, type 0) after the last voice frame."""
    for number, over in enumerate(egress, 1):
        ends = unkeys(over)
        assert len(ends) == 1, "over %d: %d unkey frames" % (number, len(ends))
        assert over[-1][1] == ends[0][1], "over %d: packets after the unkey" % number
        assert usrp.parse(ends[0][1]).keyup == 0 and ends[0][1][20:24] == bytes(4)


# ---------------------------------------------------------------- egress, other cases

def test_usrp_unkey_after_lost_source_end(rig_usrp):
    """P-USR-07 (conv; timing in B3): a D-STAR over without its last frame
    still ends with an unkey to the peer."""
    frames = ambe_tone(rig_usrp)
    sock = peer_socket()
    try:
        got = during(sock, lambda: dstar_over(rig_usrp, frames, last=False), after=2.5)
    finally:
        sock.close()
    voice, ends = keyed(got), unkeys(got)
    print("%d keyed frames, %d unkeys, unkey %.0f ms after the last voice" % (
        len(voice), len(ends), (ends[0][0] - voice[-1][0]) * 1000 if voice and ends else -1))
    assert len(voice) >= FRAMES * 0.8 and len(ends) == 1


def test_usrp_idle_silent(rig_usrp):
    """P-USR-08 (conv): nothing is sent to an idle peer (20 s), and never a
    352 B frame that is not voice."""
    sock = peer_socket()
    try:
        got = listen(sock, 20.0)
    finally:
        sock.close()
    print("idle: %d packets in 20 s" % len(got))
    assert not got, "%d packets to an idle peer, first %s" % (len(got), got[0][1][:32].hex())


def test_usrp_egress_set_info(rig_usrp):
    """P-USR-09 (dev, key "set_info_egress"): a TEXT SET_INFO before the
    voice of an over."""
    frames = ambe_tone(rig_usrp, 25)
    sock = peer_socket()
    try:
        got = during(sock, lambda: dstar_over(rig_usrp, frames))
    finally:
        sock.close()
    texts = [p for t, p in got if usrp.type_le(p) == usrp.TYPE_TEXT]
    info = [usrp.parse_set_info(p[usrp.HEADER_SIZE:]) for p in texts]
    print("TEXT frames %d, SET_INFO %s" % (len(texts), info))
    assert not [p for t, p in got if len(p) == usrp.VOICE_SIZE and usrp.type_le(p) not in (0, usrp.TYPE_TEXT)], \
        "352 B frames of another type"
    judge_dev(rig_usrp, "P-USR-09", {"set_info_egress": any(info)})


# ---------------------------------------------------------------- ingress (P-USR-10..18)

def ingress(rig, send, listen_s=1.2, port=PEER_PORT):
    """D-STAR listener on module C while send(sock, dest) runs; returns
    (streams, inbox packets, packets the peer got back)."""
    listener = dstar_listener(rig)
    sock = peer_socket(port)
    back = []
    try:
        send(sock, (rig.xlxd_sut, XLXD_PORT))
        listener.client.pump(listen_s)
        drain(sock, back)
        packets = list(listener.client.inbox)
    finally:
        sock.close()
        clients.close(listener)
    return dstar_streams(packets), packets, back


def test_usrp_ingress_opens_stream(rig_usrp):
    """P-USR-10 (conv): keyed voice opens a stream; the module's D-STAR
    listener hears it transcoded."""
    streams, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm()))
    print("streams: %s" % {k: (v["frames"], v["last"]) for k, v in streams.items()})
    assert len(streams) == 1 and next(iter(streams.values()))["frames"] >= FRAMES * 0.9


def test_usrp_ingress_unkey_ends_stream(rig_usrp):
    """P-USR-11 (conv): the 32 B unkey ends the stream at once: the
    listener's last-frame packet comes within 0.5 s of the unkey."""
    listener = dstar_listener(rig_usrp)
    sock = peer_socket()
    try:
        sent = usrp_over(sock, (rig_usrp.xlxd_sut, XLXD_PORT), tone_pcm())
        end = None
        while end is None and time.monotonic() < sent + 2.0:
            listener.client.pump(0.005)
            if any((v := dextra.parse_voice(p)) and v[2] for p in listener.client.inbox):
                end = time.monotonic()
    finally:
        sock.close()
        clients.close(listener)
    assert end is not None, "no end of stream after the unkey"
    print("end of stream %.0f ms after the unkey" % ((end - sent) * 1000))
    assert end - sent <= 0.5


def test_usrp_ingress_keyup0_voice(rig_usrp):
    """P-USR-12 (dev, key "keyup0_voice"): frame 25 of 50 sent as a 352 B
    keyup-0 frame (pyUC's unkey). Two streams = taken as unkey; one stream
    = dropped (49 frames) or played (50)."""
    mark(rig_usrp, "begin", "usrp-keyup0")
    streams, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm(), keyup0_at=25))
    mark(rig_usrp, "end", "usrp-keyup0")
    counts = sorted(v["frames"] for v in streams.values())
    mode = "unkey" if len(streams) >= 2 else ("played" if counts and counts[0] >= FRAMES else "dropped")
    print("streams %d, frames %s" % (len(streams), counts))
    judge_dev(rig_usrp, "P-USR-12", {"keyup0_voice": mode})


@pytest.mark.parametrize("case", ["gap", "start", "reorder"])
def test_usrp_ingress_seq_ignored(rig_usrp, case):
    """P-USR-14 (conv): seq gaps, a non-zero start and two swapped frames
    lose no voice and keep one stream."""
    seqs = next_seqs(FRAMES + 1)
    mark(rig_usrp, "begin", "usrp-seq-" + case)
    if case == "gap":
        seqs = seqs[:20] + [s + 5 for s in seqs[20:]]
    elif case == "start":
        seqs = [s + 100000 for s in seqs]
    else:
        seqs[20], seqs[21] = seqs[21], seqs[20]
    streams, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm(), seqs=seqs))
    mark(rig_usrp, "end", "usrp-seq-" + case)
    counts = [v["frames"] for v in streams.values()]
    print("%s: streams %d, frames %s" % (case, len(streams), counts))
    assert len(streams) == 1 and counts[0] >= FRAMES * 0.9


def test_usrp_ingress_foreign_source(rig_usrp):
    """P-USR-15 (conv): an over from the tester's IP but another port is
    ignored; the configured port still works afterwards."""
    mark(rig_usrp, "begin", "usrp-foreign")
    foreign, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm()), port=FOREIGN_PORT)
    mark(rig_usrp, "end", "usrp-foreign")
    good, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm(25)))
    print("foreign port: %d streams; configured port: %d streams" % (len(foreign), len(good)))
    assert not foreign and len(good) == 1


NON_VOICE = {
    "text": lambda seq: usrp.frame_le(seq, usrp.TYPE_TEXT, b"REG:DVSWITCH"),
    "set_info": lambda seq: usrp.text_set_info(seq, "N0CALL"),
    "dtmf": lambda seq: usrp.frame_le(seq, usrp.TYPE_DTMF, b"*123", pad=True),
    "ping": lambda seq: usrp.ping(seq, pad=True),
    "tlv": lambda seq: usrp.frame_le(seq, usrp.TYPE_TLV, usrp.tlv(usrp.TLV_BEGIN_TX, b""), pad=True),
    "adpcm": lambda seq: usrp.header_le(seq, 1, usrp.TYPE_VOICE_ADPCM) + bytes(84),
    "ulaw": lambda seq: usrp.header_le(seq, 1, usrp.TYPE_VOICE_ULAW) + bytes(160),
    "short": lambda seq: usrp.voice(seq, bytes(320))[:200],
    "long": lambda seq: usrp.voice(seq, bytes(320)) + bytes(48),
    "eye": lambda seq: b"XSRP" + usrp.voice(seq, bytes(320))[4:],
}


@pytest.mark.parametrize("kind", sorted(NON_VOICE))
def test_usrp_ingress_non_voice_ignored(rig_usrp, kind):
    """P-USR-16 (conv): 25 paced non-voice or malformed packets open no
    stream; a normal over right after works."""
    def send(sock, dest):
        start = time.monotonic()
        for index, seq in enumerate(next_seqs(25)):
            pace(start, index)
            sock.sendto(NON_VOICE[kind](seq), dest)
    mark(rig_usrp, "begin", "usrp-non-voice-" + kind)
    junk, _, back = ingress(rig_usrp, send, listen_s=0.6)
    mark(rig_usrp, "end", "usrp-non-voice-" + kind)
    good, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm(25)))
    print("%s: streams %d, replies %d; normal over after: %d streams" % (kind, len(junk), len(back), len(good)))
    assert not junk, "%s opened %d streams" % (kind, len(junk))
    assert len(good) == 1, "a normal over after %s opened %d streams" % (kind, len(good))


def test_usrp_ping_no_reply(rig_usrp):
    """P-USR-17 (conv): a PING (unpadded, pyUC style, and padded, as
    Analog_Bridge sends it) gets no reply."""
    sock = peer_socket()
    mark(rig_usrp, "begin", "usrp-ping")
    try:
        first, second = next_seqs(2)
        sock.sendto(usrp.ping(first), (rig_usrp.xlxd_sut, XLXD_PORT))
        sock.sendto(usrp.ping(second, pad=True), (rig_usrp.xlxd_sut, XLXD_PORT))
        got = listen(sock, 1.5)
    finally:
        sock.close()
        mark(rig_usrp, "end", "usrp-ping")
    assert not got, "%d replies to PING" % len(got)


def test_usrp_ingress_set_info_callsign(rig_usrp):
    """P-USR-18 (dev, key "set_info_talker"): a TEXT SET_INFO naming
    N0CALL before the over; the D-STAR header's MY callsign is N0CALL
    ("use") or the endpoint's own N0TEST ("ignore")."""
    def send(sock, dest):
        sock.sendto(usrp.text_set_info(next_seqs(1)[0], "N0CALL", src_id=1023007), dest)
        time.sleep(0.02)
        usrp_over(sock, dest, tone_pcm(25))
    mark(rig_usrp, "begin", "usrp-set-info")
    streams, _, _ = ingress(rig_usrp, send)
    mark(rig_usrp, "end", "usrp-set-info")
    heads = [v["header"] for v in streams.values() if v["header"]]
    assert heads, "no stream"
    my = heads[0]["my"].decode(errors="replace").strip()
    print("D-STAR MY callsign: %r" % my)
    judge_dev(rig_usrp, "P-USR-18", {"set_info_talker": "use" if my.startswith("N0CALL") else "ignore"})


# ---------------------------------------------------------------- routing (P-USR-20..22)

@pytest.mark.parametrize("kind", ["dstar", "dmr", "m17"])
def test_usrp_egress_from_digital(rig_usrp, kind):
    """P-USR-20 (conv): an over from a D-STAR, DMR or M17 client on the
    module reaches the USRP peer as keyed voice, ending with one unkey."""
    talker = clients.make(kind, MOD, 0)
    sock = peer_socket()
    try:
        assert talker.link(rig_usrp.xlxd_sut)
        time.sleep(1.2)                         # DMR/M17 module lists refresh once per second
        drain(sock, [])
        clients.talk(talker, kind, signals.prng_frames(kind, FRAMES, seed="usr"), tail=0.2)
        got = listen(sock, 1.0)
    finally:
        sock.close()
        clients.close(talker)
    print("%s: %d keyed, %d unkeys" % (kind, len(keyed(got)), len(unkeys(got))))
    assert len(keyed(got)) >= FRAMES * 0.8 and len(unkeys(got)) == 1


def test_usrp_endpoint_permanent(rig_usrp):
    """P-USR-22 (conv): after 10 s of silence and a peer "restart" (seq
    back to 0, as after an Asterisk restart) an over works at once."""
    first, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm(10)))
    time.sleep(10)
    mark(rig_usrp, "begin", "usrp-seq-restart")
    second, _, _ = ingress(rig_usrp, lambda s, d: usrp_over(s, d, tone_pcm(), seqs=list(range(FRAMES + 1))))
    mark(rig_usrp, "end", "usrp-seq-restart")
    counts = [v["frames"] for v in second.values()]
    print("before: %d streams; after idle + seq restart: %d streams, frames %s" % (len(first), len(second), counts))
    assert len(second) == 1 and counts[0] >= FRAMES * 0.9


# ---------------------------------------------------------------- real chan_usrp (P-USR-21, 25, 26)

def test_usrp_endpoints_isolated(rig_usrp):
    """P-USR-21 (conv, real): a D-STAR over on module C reaches the tester
    once per frame and none of it reaches the AllStar channels of A/B."""
    before = usrp_counters(asl(rig_usrp, "usrp-show"))
    frames = ambe_tone(rig_usrp)
    sock = peer_socket()
    try:
        got = during(sock, lambda: dstar_over(rig_usrp, frames))
    finally:
        sock.close()
    after = usrp_counters(asl(rig_usrp, "usrp-show"))
    seqs = [usrp.parse(p).seq for t, p in keyed(got)]
    print("tester: %d keyed (%d distinct seq); chan_usrp before %s after %s" % (
        len(seqs), len(set(seqs)), before, after))
    assert len(seqs) >= FRAMES * 0.8 and len(seqs) == len(set(seqs))
    assert before and all(after[port][0] == before[port][0] for port in before), "module C reached A/B"


def test_usrp_real_asl_receives(rig_usrp):
    """P-USR-25 (real): a D-STAR over on module A (node 1901) raises
    chan_usrp's read counter of 34001 by about the frames sent."""
    before = usrp_counters(asl(rig_usrp, "usrp-show"))
    talker = clients.make("dstar", "A", 0)
    try:
        assert talker.link(rig_usrp.xlxd_sut)
        clients.talk(talker, "dstar", ambe_tone(rig_usrp), tail=1.0)
    finally:
        clients.close(talker)
    after = usrp_counters(asl(rig_usrp, "usrp-show"))
    read = after[34001][0] - before[34001][0]
    print("chan_usrp 34001 read +%d for %d frames sent (34002 +%d)" % (
        read, FRAMES, after[34002][0] - before[34002][0]))
    assert FRAMES * 0.9 <= read <= FRAMES + 2


def test_usrp_real_asl_sends(rig_usrp):
    """P-USR-26 (real): app_rpt plays a file on node 1901: a stream opens
    on module A and ends (last frame), and chan_usrp's write counter rises."""
    listener = clients.make("dstar", "A", 1)
    try:
        assert listener.link(rig_usrp.xlxd_sut)
        listener.client.drain()
        listener.client.inbox.clear()
        before = usrp_counters(asl(rig_usrp, "usrp-show"))
        asl(rig_usrp, "play-1901")
        listener.client.pump(1.0)
        after = usrp_counters(asl(rig_usrp, "usrp-show"))
        streams = dstar_streams(listener.client.inbox)
    finally:
        clients.close(listener)
    written = after[34001][1] - before[34001][1]
    print("chan_usrp 34001 wrote +%d; streams %s" % (written, {k: (v["frames"], v["last"]) for k, v in streams.items()}))
    assert written >= 20 and len(streams) == 1
    entry = next(iter(streams.values()))
    assert entry["frames"] >= written * 0.8 and entry["last"]
