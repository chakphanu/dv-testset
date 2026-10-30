# SPDX-License-Identifier: MIT
"""B18 short-stream guard: a callsign that keys up in short bursts is
blocked for a while (xlxd-ref and xlxd-sut in parallel).

The ladder both profiles share: three short streams (under 50 frames)
from one callsign on a module, each opening within 2 s of the previous
close, earn a strike: transmissions of that callsign are refused for 3 s
(strike 1), 10 s (strike 2), 300 s (strike 3). While a callsign has
strikes, three more short streams earn the next one whatever the gaps.
Strikes decay 300 s after the last one. A refused over gets no reply and
nothing of it is relayed. The block is per callsign and spans protocols
(a DMR id or M17 callsign mapping to it is blocked too). On the third
strike new links of the callsign are refused; xlxd also disconnects the
client that closed the stream, DV-Router keeps existing sessions (the
profile difference).

xlxd (measured): the ladder and its 3 s / 10 s / 300 s blocks as above;
strikes count across protocols (stream history per module compares the
callsign only), but the block is enforced only where the protocol hands
the gatekeeper a module (DExtra, DPlus, DCS): DMR and M17 overs of a
blocked callsign are still relayed. Third strike: new links get the
protocol's refusal (DExtra NAK) and the talker is disconnected.
NXDN and P25 client links need the nxdn / p25 rig profiles and are not
probed here; their peer mode is not guarded (configured rows).

Under DVT_SUT_PROFILE=xlxd the sut must match xlxd-ref (block lengths
within 1 s); under another profile the dvtest/specs entry of each case
decides. Timing is measured by probing: a probe is a header and five
frames; if the listener hears them the probe goes on to a long stream
(60 frames, which ends any short-stream run), else it is ended at once.
A refused probe opens no stream, so it never counts as a short stream.

Each scenario has its own callsigns (rig/config/dmrid.dat STR110..STR119,
unused elsewhere) and module, because a strike-3 block lasts 300 s.
"""
import threading
import time

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.net import UdpClient
from dvtest.proto import dcs, dextra, dmr, dplus, m17, ysf

pytestmark = [pytest.mark.rig, pytest.mark.behaviour]

LADDER, OTHER, CROSS, DECAY, LISTEN = "STR110", "STR111", "STR112", "STR113", "STR119"
DMR_ID = {"STR110": 1023210, "STR112": 1023212}
MOD_LADDER, MOD_CROSS, MOD_DECAY = "W", "X", "Y"
MOD_YSF_JOIN = "V"                          # the YSF link joins here, away from the ladder module
SHORT = 10                                  # frames of a short stream (< 50)
LONG = 60                                   # frames of a long stream
GAP = 0.020
SHORT_GAP = 0.5                             # close to next open, well inside 2 s
CACHE = 1.2                                 # xlxd refreshes its module client lists once per second
TOLERANCE = 1.0                             # seconds, block lengths


def both(rig, func, *args):
    """{"ref": func(ref ip, *args), "sut": ...} run in parallel."""
    got = {}

    def side(name):
        try:
            got[name] = func(getattr(rig, "xlxd_" + name), *args)
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n,)) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    return got


_seq = [0]


def frames(n, kind="dstar"):
    _seq[0] += 1
    return signals.prng_frames(kind, n, seed="b18-%d" % _seq[0])


def dstar(ip, callsign, module):
    c = dextra.DstarClient("0.0.0.0", callsign=callsign, module=module)
    assert c.link(ip), "%s: DExtra link of %s failed" % (ip, callsign)
    return c


def drain(*made):
    for c in made:
        c.client.drain()


def clear(*made):
    for c in made:
        c.client.drain()
        c.client.inbox.clear()


def heard(listener, sent):
    """How many of the sent frames the listener's inbox holds."""
    sent = set(sent)
    got = 0
    for p in listener.client.inbox:
        v = dextra.parse_voice(p)
        if v and not v[2] and v[3] in sent:
            got += 1
    return got


def packets(c, sid, voice, start=0, end=True):
    out = [] if start else [dextra.header(sid, c.callsign, c.module)]
    for i, f in enumerate(voice, start):
        out.append(dextra.voice(sid, i, f, False, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA))
    if end:
        out.append(dextra.voice(sid, start + len(voice), bytes(9), True))
    return out


def short(c, *others):
    """One short stream; returns the time its last frame was sent."""
    sid = clients.fresh_stream_id("dstar")
    c.client.paced(c.dest, packets(c, sid, frames(SHORT)), GAP, *[o.client for o in others])
    return time.monotonic()


def shorts(c, *others, n=3):
    t = None
    for i in range(n):
        if i:
            time.sleep(SHORT_GAP)
        t = short(c, *others)
    time.sleep(0.3)                             # let the reflector close the last one
    drain(c, *others)
    return t


def talk_long(c, listener, *others):
    """A long stream; True if the listener heard all of it."""
    voice = frames(LONG)
    c.client.paced(c.dest, packets(c, clients.fresh_stream_id("dstar"), voice), GAP,
                   listener.client, *[o.client for o in others])
    time.sleep(0.4)
    drain(listener, *others)
    return heard(listener, voice) == len(voice)


def probe(c, listener):
    """Header + 5 frames; if the listener hears them go on to a long stream
    and return True, else end the over and return False."""
    sid = clients.fresh_stream_id("dstar")
    voice = frames(LONG)
    clear(listener)
    c.client.paced(c.dest, packets(c, sid, voice[:5], end=False), GAP, listener.client)
    time.sleep(0.15)
    drain(listener)
    if heard(listener, voice[:5]):
        c.client.paced(c.dest, packets(c, sid, voice[5:], start=5), GAP, listener.client)
        time.sleep(0.3)
        drain(listener)
        return True
    c.client.send(c.dest, dextra.voice(sid, 5, bytes(9), True))
    return False


def blocked_for(c, listener, t0, limit):
    """Seconds from t0 until a probe is heard (None past limit); probes
    about every 0.4 s."""
    while time.monotonic() - t0 < limit:
        start = time.monotonic()
        if probe(c, listener):
            return round(start - t0, 1)
        time.sleep(0.15)
    return None


def replies(c):
    """Non-keepalive packets the client got (no stream frames expected)."""
    return [p for p in c.client.inbox if not dextra.is_reflector_keepalive(p)]


# ---------------------------------------------------------------- the ladder

def ladder_probe(ip):
    """Strike 1, 2 and 3 for LADDER on MOD_LADDER, with the per-callsign,
    cross-protocol and third-strike checks along the way."""
    out = {}
    listener = dstar(ip, LISTEN, MOD_LADDER)
    talker = dstar(ip, LADDER, MOD_LADDER)
    other = dstar(ip, OTHER, MOD_LADDER)
    # the same callsign over DMR (its id) and M17, linked before the
    # baseline: the DMR join is a short stream, the long baseline ends its run
    d = dmr.DmrClient("0.0.0.0", dmrid=DMR_ID[LADDER], module=MOD_LADDER)
    k = m17.M17Client("0.0.0.0", callsign=LADDER, module=MOD_LADDER)
    y = ysf.YsfClient("0.0.0.0", callsign=LADDER, module=MOD_YSF_JOIN)
    fresh = None
    try:
        out["dmr_linked"] = d.link(ip)
        out["m17_linked"] = k.link(ip)
        out["ysf_linked"] = y.link(ip)
        time.sleep(CACHE)
        out["baseline"] = talk_long(talker, listener, other, d, k)
        # strike 1 -> block about 3 s; probes get no reply
        t1 = shorts(talker, listener, other)
        clear(talker)
        out["block1_s"] = blocked_for(talker, listener, t1, 8.0)
        out["reply_while_blocked"] = len(replies(talker))
        # strike 2 -> block about 10 s
        t2 = shorts(talker, listener, other)
        # (c) another callsign talks freely during the block
        out["other_heard"] = talk_long(other, listener, talker, d, k)
        # (d) the same callsign on DMR and M17 is blocked too
        clear(listener)
        d.talk(frames(30, "dmr"), stream_id=clients.fresh_stream_id("dmr"), settle=0.2, tail=0.8)
        drain(listener)
        out["dmr_heard"] = any(dextra.parse_voice(p) for p in listener.client.inbox)
        clear(listener)
        k.talk(frames(30, "m17"), clients.fresh_stream_id("m17"), settle=0.2, tail=0.8)
        drain(listener)
        out["m17_heard"] = any(dextra.parse_voice(p) for p in listener.client.inbox)
        clients.close(d, k)
        d.dest = k.dest = None
        out["block2_s"] = blocked_for(talker, listener, t2, 16.0)
        # strike 3: links refused; xlxd also drops the talker
        y.client.inbox.clear()
        y.keepalive()                           # the YSF session polls on (30 s timeout)
        y.client.pump(0.5)
        out["ysf_poll_ack_before"] = any(ysf.is_poll_ack(p) for p in y.client.inbox)
        clear(talker)
        shorts(talker, listener, other)
        time.sleep(0.5)
        drain(talker)
        out["notice_to_talker"] = len(replies(talker))
        fresh = dextra.DstarClient("0.0.0.0", callsign=LADDER, module=MOD_LADDER)
        ok = fresh.link(ip)
        out["new_link"] = "accepted" if ok else (
            "refused" if any(b"NAK" in p for p in fresh.client.inbox) else "no reply")
        if not ok:
            fresh.dest = None
        clear(talker)
        voice = frames(LONG)
        other.client.paced(other.dest, packets(other, clients.fresh_stream_id("dstar"), voice), GAP,
                           talker.client, listener.client)
        time.sleep(0.5)
        drain(talker, listener)
        out["existing_kept"] = heard(talker, voice) == len(voice)
        out["third_blocks_tx"] = not probe(talker, listener)
        # new links of the callsign on every protocol, and the YSF session's poll
        out["new_link_by_protocol"] = new_links(ip, LADDER, DMR_ID[LADDER], MOD_LADDER)
        y.client.inbox.clear()
        y.keepalive()
        y.client.pump(1.0)
        out["ysf_existing_poll_ack"] = any(ysf.is_poll_ack(p) for p in y.client.inbox)
        return out
    finally:
        made = [listener, talker, other] + ([fresh] if fresh else [])
        if d.dest:
            made += [d, k]
        clients.close(*made)
        y.client.close()
        if not d.dest:
            for c in (d, k):
                try:
                    c.client.close()
                except OSError:
                    pass


def ask(dest, packets, pred, seconds):
    """Send packets from a fresh socket; the first reply matching pred
    (pred returns a label or None), or "none"."""
    c = UdpClient("0.0.0.0")
    try:
        for packet in packets:
            c.send(dest, packet)
            c.pump(0.2)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            c.drain()
            for data in c.inbox:
                label = pred(data)
                if label:
                    return label
            time.sleep(0.02)
        return "none"
    finally:
        c.close()


def new_links(ip, callsign, dmr_id, module):
    """The reply each protocol gives a new link of callsign."""
    out = {}
    out["dextra"] = ask((ip, dextra.PORT), [dextra.link_packet(callsign, module)],
                        lambda d: "NAK" if b"NAK" in d else ("ACK" if b"ACK" in d else None), 3.0)
    out["dplus"] = ask((ip, dplus.PORT), [dplus.CONNECT, dplus.login_irc(callsign)],
                       lambda d: dplus.parse(d).get("text"), 3.5)
    out["dcs"] = ask((ip, dcs.PORT), [dcs.link_packet(callsign, module)], dcs.parse_ack, 3.0)

    def dmr_reply(data):
        tag = (dmr.parse_control(data) or {}).get("tag")
        return tag if tag in ("MSTNAK",) else None
    c = UdpClient("0.0.0.0")
    try:
        dest = (ip, dmr.PORT)
        c.send(dest, dmr.rptl(dmr_id))
        c.pump(1.0)
        tags = [(dmr.parse_control(p) or {}).get("tag") for p in c.inbox]
        if "RPTACK" in tags:
            c.inbox.clear()
            c.send(dest, dmr.rptk(dmr_id))
            c.pump(1.0)
            tags = [(dmr.parse_control(p) or {}).get("tag") for p in c.inbox]
            out["dmr"] = "MSTNAK" if "MSTNAK" in tags else ("RPTACK" if "RPTACK" in tags else "none at RPTK")
        else:
            out["dmr"] = "MSTNAK" if "MSTNAK" in tags else "none at RPTL"
    finally:
        c.close()
    out["m17"] = ask((ip, m17.PORT), [m17.conn_packet(callsign, module)],
                     lambda d: d[:4].decode("latin-1") if d[:4] in (b"ACKN", b"NACK") else None, 3.0)
    out["ysf"] = ask((ip, ysf.PORT), [ysf.poll_packet(callsign)],
                     lambda d: "ack" if ysf.is_poll_ack(d) else None, 2.5)
    return out


@pytest.fixture(scope="module")
def ladder(rig):
    got = both(rig, ladder_probe)
    print("B18 ladder:", got)
    for name, r in got.items():
        assert r["baseline"], "%s: the ladder callsign is not heard before any strike: %s" % (name, r)
    return got


def close_to(a, b):
    return a is not None and b is not None and abs(a - b) <= TOLERANCE


def judge_seconds(got, spec, key, spec_key):
    if spec is None:
        assert close_to(got["sut"][key], got["ref"][key]), "%s: sut %s, ref %s" % (
            key, got["sut"][key], got["ref"][key])
    else:
        assert close_to(got["sut"][key], spec[spec_key]), "%s: sut %s, spec %s" % (
            key, got["sut"][key], spec[spec_key])


def judge_same(got, spec, *keys):
    for key in keys:
        want = got["ref"][key] if spec is None else spec[key]
        assert got["sut"][key] == want, "%s: sut %r, %s %r" % (
            key, got["sut"][key], "ref" if spec is None else "spec", want)


def test_short_stream_guard_first_block(ladder, behaviour_spec):
    """B18 (a): three short streams -> the callsign's next over is refused
    for about 3 s, silently (no reply to the talker)."""
    judge_seconds(ladder, behaviour_spec, "block1_s", "block_s")
    judge_same(ladder, behaviour_spec, "reply_while_blocked")


def test_short_stream_guard_second_block(ladder, behaviour_spec):
    """B18 (b): three more short streams while the strike is active ->
    blocked for about 10 s."""
    judge_seconds(ladder, behaviour_spec, "block2_s", "block_s")


def test_short_stream_guard_per_callsign(ladder, behaviour_spec):
    """B18 (c): another callsign on the same module talks freely while the
    first one is blocked."""
    judge_same(ladder, behaviour_spec, "other_heard")
    assert ladder["ref"]["other_heard"] or behaviour_spec is not None, "ref: %s" % ladder["ref"]


def test_short_stream_guard_cross_protocol_block(ladder, behaviour_spec):
    """B18 (d): while blocked, the same callsign over DMR (its id) and M17
    is refused too."""
    for name, r in ladder.items():
        assert r["dmr_linked"] and r["m17_linked"], "%s: DMR/M17 link failed: %s" % (name, r)
    judge_same(ladder, behaviour_spec, "dmr_heard", "m17_heard")


def test_short_stream_guard_third_strike(ladder, behaviour_spec):
    """B18 (e): third strike: new links of the callsign are refused and its
    over is refused; existing sessions: xlxd drops the talker, DV-Router
    keeps it (it still hears the module)."""
    judge_same(ladder, behaviour_spec, "new_link", "existing_kept", "third_blocks_tx")


def test_short_stream_guard_third_strike_protocols(ladder, behaviour_spec):
    """B18 (e): the third-strike link refusal on every client protocol:
    the protocol's refusal where it has one (DExtra NAK, DPlus BUSY, DCS
    NAK, MMDVM MSTNAK, M17 NACK), no poll reply on YSF; a YSF session
    linked before the strike keeps getting poll replies (DV-Router)."""
    for name, r in ladder.items():
        assert r["ysf_linked"] and r["ysf_poll_ack_before"], "%s: YSF session not up before strike 3: %s" % (name, r)
    judge_same(ladder, behaviour_spec, "new_link_by_protocol", "ysf_existing_poll_ack")


# ---------------------------------------------------------------- cross-protocol strikes

def cross_strikes_probe(ip):
    """Short streams of one callsign over DExtra, DMR and M17 in turn earn
    a strike like three DExtra ones would."""
    listener = dstar(ip, LISTEN, MOD_CROSS)
    talker = dstar(ip, CROSS, MOD_CROSS)
    d = dmr.DmrClient("0.0.0.0", dmrid=DMR_ID[CROSS], module=MOD_CROSS)
    k = m17.M17Client("0.0.0.0", callsign=CROSS, module=MOD_CROSS)
    try:
        assert d.link(ip) and k.link(ip), "%s: DMR/M17 link failed" % ip
        time.sleep(CACHE)
        baseline = talk_long(talker, listener, d, k)    # ends the DMR join's short run
        short(talker, listener)
        time.sleep(SHORT_GAP)
        d.talk(frames(6, "dmr"), stream_id=clients.fresh_stream_id("dmr"), settle=0.1, tail=0.1)
        time.sleep(SHORT_GAP)
        k.talk(frames(6, "m17"), clients.fresh_stream_id("m17"), settle=0.1, tail=0.1)
        t = time.monotonic()
        time.sleep(0.3)
        block = blocked_for(talker, listener, t, 8.0)
        return {"baseline": baseline, "block_s": block, "struck": block is not None and block >= 1.5}
    finally:
        clients.close(listener, talker, d, k)


def test_short_stream_guard_cross_protocol_strikes(rig, behaviour_spec):
    """B18 (d): a DExtra, a DMR and an M17 short stream of one callsign in
    a row count as three: the next DExtra over is refused for a while."""
    got = both(rig, cross_strikes_probe)
    print("B18 cross-protocol strikes:", got)
    for name, r in got.items():
        assert r["baseline"], "%s: %s" % (name, r)
    judge_same(got, behaviour_spec, "struck")


# ---------------------------------------------------------------- decay

DECAY_WAIT = 310.0


def decay_probe(ip):
    listener = dstar(ip, LISTEN, MOD_DECAY)
    talker = dstar(ip, DECAY, MOD_DECAY)
    try:
        time.sleep(CACHE)
        baseline = talk_long(talker, listener)
        t = shorts(talker, listener)
        first = blocked_for(talker, listener, t, 8.0)
        deadline = time.monotonic() + DECAY_WAIT
        while time.monotonic() < deadline:          # answer keepalives on both links
            talker.client.pump(0.5)
            listener.client.drain()
        # both links still up after the wait (a long stream: no strike either way)
        baseline2 = talk_long(talker, listener)
        t = shorts(talker, listener)
        after = blocked_for(talker, listener, t, 16.0)
        return {"baseline": baseline and baseline2, "first_block_s": first, "block_after_decay_s": after}
    finally:
        clients.close(listener, talker)


@pytest.mark.slow
def test_short_stream_guard_decay(rig, behaviour_spec):
    """B18 (f): 300 s after the last strike the strikes are gone: the next
    run of short streams earns strike 1 again (about 3 s, not 10 s)."""
    got = both(rig, decay_probe)
    print("B18 decay:", got)
    for name, r in got.items():
        assert r["baseline"], "%s: %s" % (name, r)
    judge_seconds(got, behaviour_spec, "block_after_decay_s", "block_after_decay_s")
