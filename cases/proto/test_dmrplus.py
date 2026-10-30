# SPDX-License-Identifier: MIT
"""DMRplus dongle (UDP 8880) conformance (docs/protocols/dmrplus.md,
testlist K): one case per obligation P-DMP-NN, xlxd-ref and xlxd-sut
probed in parallel with DMR+ clients (dvtest/proto/dmrplus.py), MMDVM,
D-STAR and M17 clients as the other side.

Judged as in dmrplus.md: hb = a spec requirement; for the ETSI egress
rows (LC header, embedded LC, terminator) xlxd's lack is a known upstream
deviation: sut == ref under the xlxd profile, the spec value under
another one. dev = disputed: sut == ref under the xlxd profile, else the
profile's "dmrplus" spec entry {"P-DMP-NN": {observed key: value}} (skip
"spec pending"); diff = sut == ref. A non-xlxd profile runs the suite
only with SPEC["dmrplus"]["server"] = True ("edge not ready" skip).
Deliberately malformed traffic is bracketed by control-channel marks.
"""
import contextlib
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import ambed, dextra, dmr, dmrplus

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "H", "J"
TALKER, LISTENER = 1023007, 1023008             # N0CALL, N0TEST
EXTRA = (1023101, 1023102)                      # STR001, STR002
UNKNOWN_ID = 1023999                            # in no dmrid.dat
BLOCKED = "N0DMP"                               # dmrid.dat 1023230;N0DMP, DVT_XLXD_BLACKLIST=N0DMP (own run)
DMRID_DAT = Path(__file__).resolve().parents[2] / "rig" / "config" / "dmrid.dat"
UNKNOWN_CALL = "N0UNK"                          # D-STAR callsign with no DMR id
FRAMES = 36                                     # 12 bursts, two superframes
CACHE = 1.2                                     # xlxd refreshes its client lists once per second
TAIL = 1.0


# ---------------------------------------------------------------- setup and judging

@pytest.fixture(scope="module")
def dmp(rig):
    """The rig; under a non-xlxd profile only with SPEC["dmrplus"]["server"]."""
    if rig.sut_profile != specs.DEFAULT:
        ready = (specs.rule(rig, "dmrplus") or {}).get("server")
        if ready is not True:
            pytest.skip("DMR+ server edge not ready: SPEC['dmrplus']['server'] = %r in dvtest/specs/%s.py"
                        % (ready, rig.sut_profile))
    return rig


def both(rig, func, *args):
    """{"ref": func(rig, "ref", *args), "sut": ...} in parallel, printed."""
    got = {}

    def side(name):
        try:
            got[name] = func(rig, name, *args)
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n,)) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    print("ref: %r\nsut: %r" % (got["ref"], got["sut"]))
    return got


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def judge_dev(rig, nn, got, *keys):
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = specs.rule_mode(rig, "dmrplus", nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no dmrplus %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def judge_spec(rig, nn, got, expected):
    """ETSI MUST rows xlxd does not meet by design: sut == ref under the
    xlxd profile (xlxd recorded), the spec values under another profile."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *expected)
        return
    for key, value in expected.items():
        assert got["sut"][key] == value, "%s %s: sut %r, ETSI %r" % (nn, key, got["sut"][key], value)


def judge_behaviour(rig, case, got, keys, expect):
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = specs.rule(rig, case)
    if entry is None:
        pytest.skip("spec pending: no %s entry in dvtest/specs/%s.py" % (case, rig.sut_profile))
    for key, value in expect(entry).items():
        assert got["sut"][key] == value, "%s %s: sut %r, spec %r" % (case, key, got["sut"][key], value)


@contextlib.contextmanager
def marked(rig, tag):
    """Control channel "mark begin|end <tag>" around deliberately
    malformed tester traffic (no-op without the control channel)."""
    _mark(rig, "begin", tag)
    try:
        yield
    finally:
        _mark(rig, "end", tag)


def _mark(rig, phase, tag):
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    directory.mkdir(parents=True, exist_ok=True)
    name = "dmp-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------- clients

def ip_of(rig, side):
    return getattr(rig, "xlxd_" + side)


def dongle(dmrid=TALKER, module=MOD):
    return dmrplus.DmrPlusClient("0.0.0.0", dmrid=dmrid, module=module)


def mmdvm(dmrid=LISTENER, module=MOD):
    return dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=module)


def linked(rig, side, *made):
    """Link every client (DMR+ connect / MMDVM login + join), wait for
    the server's client cache, clear the inboxes."""
    ip = ip_of(rig, side)
    for client in made:
        if isinstance(client, dmrplus.DmrPlusClient):
            assert dmrplus.ACK in client.link(ip), "%s: no ACK to a DMR+ connect from %d" % (side, client.id)
        else:
            assert client.link(ip), "%s: %s link failed" % (side, type(client).__name__)
    hold(CACHE, *made)
    clear(*made)
    return made


def clear(*made):
    for client in made:
        client.client.drain()
        client.client.inbox.clear()
        client.client.inbox_times.clear()


def hold(seconds, *made):
    """Drain for seconds; DMR+ clients keep their keepalive going."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for client in made:
            if isinstance(client, dmrplus.DmrPlusClient):
                client.pump(0.0)
            else:
                client.client.drain()
        time.sleep(0.01)


def done(*made):
    for client in made:
        try:
            if client.dest:
                if isinstance(client, dmrplus.DmrPlusClient):
                    client.disconnect()
                else:
                    clients.close(client)
                    continue
        finally:
            try:
                client.close()
            except OSError:
                pass


def frames(n=FRAMES, seed="dmrplus"):
    return signals.prng_frames("dmr", n, seed=seed)


def heard_mmdvm(listener):
    return [f for d in listener.client.inbox if dmr.is_voice(d) for f in dmr.extract(d[20:53])]


def dongle_talk(talker, listeners, voice=None, **shape):
    """talker sends one DMR+ stream; listeners drain (DMR+ ones keep alive)."""
    voice = voice or frames()
    packets = shape.pop("packets", None) or dmrplus.stream(voice, talker.id, talker.reflector, **shape)
    talker.send(packets, dmrplus.GAP, *listeners)
    hold(TAIL, talker, *listeners)
    return voice


def mmdvm_talk(talker, listeners, voice=None, settle=1.2):
    """talker (MMDVM) talks in a thread while listeners drain / keep alive."""
    voice = voice or frames()
    job = threading.Thread(target=talker.talk, args=(voice,),
                           kwargs={"stream_id": clients.fresh_stream_id("dmr"), "settle": settle, "tail": 0.5})
    job.start()
    while job.is_alive():
        hold(0.05, *listeners)
    job.join()
    hold(TAIL, *listeners)
    return voice


def ask(client, packet, seconds=1.0):
    """Send packet with an empty inbox; replies within seconds."""
    clear(client)
    client.client.send(client.dest, packet)
    client.client.pump(seconds)
    return list(client.client.inbox)


# ---------------------------------------------------------------- 1. connect, keepalive, disconnect

def test_dmp_connect_ack(dmp):
    """P-DMP-01: connect -> ACK; the client hears its module."""
    def run(rig, side):
        d, talker = dongle(LISTENER), mmdvm(TALKER)
        try:
            replies = d.link(ip_of(rig, side))
            linked(rig, side, talker)
            hold(CACHE, d)
            clear(d)
            mmdvm_talk(talker, [d])
            return {"replies": replies, "heard": len(dmrplus.frames_of(d.voice())) > 0}
        finally:
            done(d, talker)
    got = both(dmp, run)
    assert got["ref"]["replies"] == [dmrplus.ACK] and got["ref"]["heard"]
    same(got, "replies", "heard")


def test_dmp_connect_unknown_id(dmp):
    """P-DMP-02: an id missing from the database gets no reply."""
    def run(rig, side):
        d = dongle(UNKNOWN_ID)
        try:
            with marked(rig, "dmp-unknown-id-%s" % side):
                return {"replies": d.link(ip_of(rig, side), timeout=1.5)}
        finally:
            d.dest = None
            done(d)
    got = both(dmp, run)
    same(got, "replies")


def blocked_id():
    """(id, callsign) of a blacklisted callsign with a dmrid.dat row:
    N0DMP when listed, else the first other one; None without any."""
    listed = os.environ.get("DVT_XLXD_BLACKLIST", "").split()
    rows = {}
    for line in DMRID_DAT.read_text().splitlines():
        parts = line.split(";")
        if len(parts) >= 2 and parts[0].isdigit():
            rows[parts[1].strip()] = int(parts[0])
    for call in sorted(listed, key=lambda c: c != BLOCKED):
        if call in rows:
            return rows[call], call
    return None


def test_dmp_connect_refused(dmp):
    """P-DMP-03: a refused (blacklisted) id gets NAK."""
    found = blocked_id()
    if found is None:
        pytest.skip("needs DVT_XLXD_BLACKLIST=%s (a separate run) and the dmrid.dat row 1023230;%s"
                    % (BLOCKED, BLOCKED))

    def run(rig, side):
        d = dongle(found[0])
        try:
            with marked(rig, "dmp-refused-%s" % side):
                return {"replies": d.link(ip_of(rig, side)), "callsign": found[1]}
        finally:
            d.dest = None
            done(d)
    got = both(dmp, run)
    assert got["ref"]["replies"] == [dmrplus.NAK]
    same(got, "replies")


@pytest.mark.parametrize("reflector", [4000, 4500])
def test_dmp_connect_no_module(dmp, reflector):
    """P-DMP-04: a reflector number that maps to no module."""
    def run(rig, side):
        d, talker = dongle(LISTENER), mmdvm(TALKER)
        try:
            d.dest = (ip_of(rig, side), dmrplus.PORT)
            with marked(rig, "dmp-no-module-%s" % side):
                replies = ask(d, dmrplus.connect_packet(d.id, reflector), 1.0)
            linked(rig, side, talker)
            clear(d)
            mmdvm_talk(talker, [])
            d.client.pump(0.2)
            return {"replies": [r for r in replies if r in (dmrplus.ACK, dmrplus.NAK)],
                    "heard": len(dmrplus.frames_of(d.voice())) > 0}
        finally:
            done(d, talker)
    got = both(dmp, run)
    judge_dev(dmp, "P-DMP-04", got, "replies", "heard")


def test_dmp_keepalive_ack(dmp):
    """P-DMP-05: every keepalive is ACKed; the server sends nothing else."""
    def run(rig, side):
        d = dongle(LISTENER)
        try:
            linked(rig, side, d)
            acks = 0
            for _ in range(3):
                acks += ask(d, dmrplus.connect_packet(d.id, d.reflector), 1.0).count(dmrplus.ACK)
            clear(d)
            d.client.pump(3.0)                          # silent: no keepalive from the client
            return {"acks": acks, "unsolicited": len(d.client.inbox)}
        finally:
            done(d)
    got = both(dmp, run)
    assert got["ref"]["acks"] == 3
    same(got, "acks", "unsolicited")


def test_dmp_keepalive_other_module(dmp):
    """P-DMP-06: a linked client sends a keepalive naming another module."""
    def run(rig, side):
        d = dongle(LISTENER)
        on_old, on_new = mmdvm(TALKER, MOD), mmdvm(EXTRA[0], OTHER)
        try:
            linked(rig, side, d, on_old, on_new)
            d.reflector = dmrplus.reflector_of(OTHER)       # every later keepalive names J
            d.keepalive()
            hold(CACHE, d)
            clear(d)
            mmdvm_talk(on_new, [d], frames(seed="new"))
            heard_new = len(dmrplus.frames_of(d.voice())) > 0
            clear(d)
            mmdvm_talk(on_old, [d], frames(seed="old"))
            return {"hears_new": heard_new, "hears_old": len(dmrplus.frames_of(d.voice())) > 0}
        finally:
            d.reflector = dmrplus.reflector_of(MOD)
            done(d, on_old, on_new)
    got = both(dmp, run)
    judge_dev(dmp, "P-DMP-06", got, "hears_new", "hears_old")


@pytest.fixture(scope="module")
def silent(dmp):
    """P-DMP-07/08: connect once, then no keepalive; an MMDVM talker calls
    6 s and 13 s after the connect."""
    def run(rig, side):
        d, talker = dongle(LISTENER), mmdvm(TALKER)
        try:
            d.link(ip_of(rig, side))
            start = time.monotonic()
            linked(rig, side, talker)
            clear(d)
            heard, other = [], 0
            for mark in (6.0, 13.0):
                while time.monotonic() < start + mark:
                    d.client.drain()
                    time.sleep(0.05)
                other += sum(1 for x in d.client.inbox if dmrplus.parse(x) is None)
                clear(d)
                mmdvm_talk(talker, [])
                d.client.drain()
                heard.append(len(dmrplus.frames_of(d.voice())) > 0)
            return {"heard": heard, "dropped": not heard[-1], "sent_on_drop": other}
        finally:
            d.dest = None
            done(d, talker)
    return both(dmp, run)


def test_dmp_silent_client_dropped(dmp, silent):
    """P-DMP-07: dropped about 10 s after the last keepalive, nothing sent."""
    same(silent, "heard", "sent_on_drop")


def test_dmp_dropped_client_no_voice(dmp, silent):
    """P-DMP-08: once dropped, no voice."""
    same(silent, "dropped")


def test_dmp_disconnect(dmp):
    """P-DMP-09: disconnect unlinks, no reply."""
    def run(rig, side):
        d, talker = dongle(LISTENER), mmdvm(TALKER)
        try:
            linked(rig, side, d, talker)
            replies = ask(d, dmrplus.disconnect_packet(d.id, MOD), 0.5)
            hold(CACHE)
            clear(d)
            mmdvm_talk(talker, [])
            d.client.drain()
            return {"replies": replies, "heard": len(dmrplus.frames_of(d.voice())) > 0}
        finally:
            d.dest = None
            done(d, talker)
    got = both(dmp, run)
    same(got, "replies", "heard")


def test_dmp_disconnect_unknown(dmp):
    """P-DMP-10: disconnect from an address that never connected."""
    def run(rig, side):
        stranger, d, talker = dongle(EXTRA[1]), dongle(LISTENER), mmdvm(TALKER)
        try:
            stranger.dest = (ip_of(rig, side), dmrplus.PORT)
            replies = ask(stranger, dmrplus.disconnect_packet(stranger.id, MOD), 0.5)
            linked(rig, side, d, talker)
            mmdvm_talk(talker, [d])
            return {"replies": replies, "heard": len(dmrplus.frames_of(d.voice())) > 0}
        finally:
            stranger.dest = None
            done(stranger, d, talker)
    got = both(dmp, run)
    same(got, "replies", "heard")


# ---------------------------------------------------------------- 2. ingress (DMR+ -> MMDVM)

def to_mmdvm(rig, side, voice=None, tag=None, listen=MOD, extra=None, **shape):
    """A DMR+ talker on MOD sends one stream; MMDVM listeners on listen
    (and extra) collect frames. Returns {"frames": ..., "extra": ...,
    "headers": count of header bursts at the main listener}."""
    talker, listener = dongle(TALKER), mmdvm(LISTENER, listen)
    other = mmdvm(EXTRA[0], extra) if extra else None
    made = [c for c in (talker, listener, other) if c]
    try:
        linked(rig, side, *made)
        context = marked(rig, "%s-%s" % (tag, side)) if tag else contextlib.nullcontext()
        with context:
            sent = dongle_talk(talker, [listener] + ([other] if other else []), voice, **shape)
        out = {"sent": sent, "frames": heard_mmdvm(listener),
               "headers": sum(1 for d in listener.client.inbox if dmr.parse(d) and dmr.parse(d)["bitfield"] & 0x3F == 0x21),
               "raw": list(listener.client.inbox), "talker_inbox": talker.voice()}
        if other:
            out["extra"] = heard_mmdvm(other)
        return out
    finally:
        done(*made)


def test_dmp_header_opens_stream(dmp):
    """P-DMP-11: header + 36 frames + terminator reach an MMDVM listener."""
    got = both(dmp, lambda rig, side: {"frames": len(to_mmdvm(rig, side)["frames"])})
    for side in ("ref", "sut"):
        assert got[side]["frames"] >= FRAMES * 0.8, "%s: %d frames" % (side, got[side]["frames"])


@pytest.mark.parametrize("what,shape", [("slot1", {"slot": 0x11}), ("cc2", {"colour_code": 2}),
                                        ("private", {"call": 0})], ids=["slot1", "cc2", "private"])
def test_dmp_ingress_filtered(dmp, what, shape):
    """P-DMP-12 (slot 1), P-DMP-13 (colour code 2), P-DMP-14 (private
    call): not relayed."""
    got = both(dmp, lambda rig, side: {"frames": len(to_mmdvm(rig, side, tag="dmp-" + what, **shape)["frames"])})
    same(got, "frames")


def test_dmp_header_other_module(dmp):
    """P-DMP-15: a client linked to H sends a header to 4010 (J)."""
    def run(rig, side):
        talker, on_h, on_j = dongle(TALKER), mmdvm(LISTENER, MOD), mmdvm(EXTRA[0], OTHER)
        try:
            linked(rig, side, talker, on_h, on_j)
            voice = frames(seed="other")
            talker.send(dmrplus.stream(voice, talker.id, dmrplus.reflector_of(OTHER)), dmrplus.GAP, on_h, on_j)
            hold(TAIL, talker, on_h, on_j)
            got = {"on_linked": len(heard_mmdvm(on_h)) > 0, "on_header_tg": len(heard_mmdvm(on_j)) > 0}
            return got
        finally:
            done(talker, on_h, on_j)
    got = both(dmp, run)
    judge_dev(dmp, "P-DMP-15", got, "on_linked", "on_header_tg")


def test_dmp_unknown_source(dmp):
    """P-DMP-16: header and voice with source id 1023999 (not in the database)."""
    def run(rig, side):
        voice = frames(seed="unknown")
        res = to_mmdvm(rig, side, voice, tag="dmp-unknown-src",
                       packets=dmrplus.stream(voice, UNKNOWN_ID, dmrplus.reflector_of(MOD)))
        return {"relayed": len(res["frames"]) > 0}
    got = both(dmp, run)
    judge_dev(dmp, "P-DMP-16", got, "relayed")


def test_dmp_frames_exact_to_mmdvm(dmp):
    """P-DMP-17 / 33: AMBE+2 frames DMR+ -> MMDVM; burst written as the
    server writes it (byte 32 at wire 59) and as it reads it (at 58)."""
    def run(rig, side):
        out = {}
        for layout in ("pair34", "pair33"):
            voice = frames(seed=layout)
            res = to_mmdvm(rig, side, voice, layout=layout)
            got = res["frames"][:len(voice)]
            out[layout] = got == voice
            if layout == "pair34":
                out["c_last_lost"] = sum(1 for i, f in enumerate(got) if i % 3 == 2 and f[8] != voice[i][8])
            time.sleep(1.7)
        return {"exact": out["pair34"], "exact_pair33": out["pair33"], "c_last_lost": out["c_last_lost"]}
    got = both(dmp, run)
    judge_dev(dmp, "P-DMP-17", got, "exact", "exact_pair33", "c_last_lost")


def test_dmp_no_header_not_relayed(dmp):
    """P-DMP-18: voice bursts without a header."""
    got = both(dmp, lambda rig, side: {"frames": len(to_mmdvm(rig, side, tag="dmp-no-header", head=False)["frames"])})
    judge_dev(dmp, "P-DMP-18", got, "frames")


def test_dmp_terminator_frees_module(dmp):
    """P-DMP-19: after a DMR+ terminator an MMDVM talker 0.3 s later is
    heard; the listener got a terminator burst."""
    def run(rig, side):
        talker, second, listener = dongle(TALKER), mmdvm(EXTRA[0]), mmdvm(LISTENER)
        try:
            linked(rig, side, talker, second, listener)
            talker.send(dmrplus.stream(frames(seed="first"), talker.id, talker.reflector), dmrplus.GAP, listener)
            hold(0.3, talker, listener)
            first = list(listener.client.inbox)
            clear(listener)
            voice = frames(seed="second")
            mmdvm_talk(second, [talker, listener], voice, settle=0.05)
            got = heard_mmdvm(listener)
            terms = sum(1 for d in first if dmr.parse(d) and dmr.parse(d)["bitfield"] & 0x3F == 0x22)
            return {"second_heard": len(got) >= FRAMES * 0.8, "terminator_within_0.3s": terms > 0}
        finally:
            done(talker, second, listener)
    got = both(dmp, run)
    judge_dev(dmp, "P-DMP-19", got, "second_heard", "terminator_within_0.3s")


def test_dmp_repeated_header(dmp):
    """P-DMP-20: a second header in the middle of the stream."""
    def run(rig, side):
        voice = frames(seed="rehead")
        pkts = dmrplus.stream(voice, TALKER, dmrplus.reflector_of(MOD))
        pkts = pkts[:7] + [pkts[0]] + pkts[7:]
        res = to_mmdvm(rig, side, voice, packets=pkts)
        return {"frames": len(res["frames"]), "headers": res["headers"]}
    got = both(dmp, run)
    same(got, "frames", "headers")


def test_dmp_no_echo(dmp):
    """P-DMP-21: the DMR+ talker gets none of its stream back; another
    dongle on the module hears it."""
    def run(rig, side):
        talker, other = dongle(TALKER), dongle(LISTENER)
        try:
            linked(rig, side, talker, other)
            dongle_talk(talker, [other])
            return {"echo": len(talker.voice()), "other": len(dmrplus.frames_of(other.voice())) > 0}
        finally:
            done(talker, other)
    got = both(dmp, run)
    assert got["ref"]["echo"] == 0
    same(got, "echo", "other")


# ---------------------------------------------------------------- 3. egress (MMDVM / D-STAR -> DMR+)

def from_talker(rig, side, kind="dmr", callsign="N0CALL", voice=None):
    """A talker of kind on MOD, a DMR+ listener: its parsed packets with
    arrival times, plus the MMDVM header seen by an MMDVM listener (to
    validate the LC decoder on xlxd's MMDVM egress)."""
    listener, check = dongle(LISTENER), mmdvm(EXTRA[1])
    if kind == "dmr":
        talker = mmdvm(TALKER)
    else:
        talker = dextra.DstarClient("0.0.0.0", callsign=callsign, module=MOD)
    try:
        linked(rig, side, listener, check)
        assert talker.link(ip_of(rig, side)), "%s talker link failed" % kind
        hold(CACHE, listener, check)
        clear(listener, check)
        if kind == "dmr":
            mmdvm_talk(talker, [listener, check], voice)
        else:
            job = threading.Thread(target=clients.talk, args=(talker, "dstar", voice or signals.prng_frames(
                "dstar", FRAMES, seed="dstar"), rig.reflector, 0.5))
            job.start()
            while job.is_alive():
                hold(0.05, listener, check)
            job.join()
            hold(TAIL, listener, check)
        pairs = [(dmrplus.parse(d), t) for d, t in zip(listener.client.inbox, listener.client.inbox_times)]
        mmdvm_head = next((dmr.parse(d) for d in check.client.inbox
                           if dmr.parse(d) and dmr.parse(d)["bitfield"] & 0x3F == 0x21), None)
        return {"packets": [p for p, _ in pairs if p], "times": [t for p, t in pairs if p],
                "mmdvm_header": mmdvm_head["payload"] if mmdvm_head else None}
    finally:
        done(listener, check)
        clients.close(talker)


@pytest.fixture(scope="module")
def egress(dmp):
    def run(rig, side):
        out = {"known": from_talker(rig, side, voice=frames(seed="egress"))}
        time.sleep(1.7)
        out["unknown"] = from_talker(rig, side, kind="dstar", callsign=UNKNOWN_CALL)
        return out
    got = {}

    def side(name):
        got[name] = run(dmp, name)
    threads = [threading.Thread(target=side, args=(n,)) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert name in got, "%s: egress run failed" % name
    return got


def view(egress, func):
    got = {side: func(egress[side]) for side in ("ref", "sut")}
    print(got)
    return got


@pytest.mark.parametrize("who", ["known", "unknown"])
def test_dmp_egress_header_fields(dmp, egress, who):
    """P-DMP-22: TS2, CC 1, group, destination 4001 + module, source id."""
    def fields(e):
        heads = [p for p in e[who]["packets"] if p["type"] == dmrplus.HEADER]
        if not heads:
            return {"header": False}
        h = heads[0]
        return {"header": True, "slot": h["slot"], "cc": h["cc"], "call": h["call"], "dst": h["dst"], "src": h["src"]}
    got = view(egress, fields)
    assert got["ref"]["header"]
    for side in ("ref", "sut"):
        got[side]["src_" + who] = got[side].get("src")
    # unknown talker: the sut's header (if any) must match ref's fields;
    # whether it sends one is DVT_NO_RADIO_ID's (P-DMP-22 src_unknown,
    # None = no header) under a non-xlxd profile
    if who == "known" or dmp.sut_profile == specs.DEFAULT or got["sut"]["header"]:
        same(got, "header", "slot", "cc", "call", "dst")
    judge_dev(dmp, "P-DMP-22", got, "src_" + who)


def lc_view(e):
    heads = [p for p in e["known"]["packets"] if p["type"] == dmrplus.HEADER]
    out = {"lc_ok": False}
    if heads:
        lc = dmrplus.lc_header(heads[0]["burst"])
        out = {"lc_ok": lc["sync"] in (dmrplus.BS_DATA, dmrplus.MS_DATA) and lc["data_type"] == 1
               and lc["lc"]["flco"] == 0 and lc["lc"]["src"] == TALKER,
               "sync": lc["sync"].hex(), "data_type": lc["data_type"], "lc": lc["lc"]}
    mh = e["known"]["mmdvm_header"]
    if mh:
        ref_lc = dmrplus.lc_header(mh)
        out["decoder_on_mmdvm_header"] = ref_lc["data_type"] == 1 and ref_lc["lc"]["src"] == TALKER
    return out


def test_dmp_egress_lc_header(dmp, egress):
    """P-DMP-23 (ETSI 7.1.1): the header burst is a voice LC header."""
    got = view(egress, lc_view)
    judge_spec(dmp, "P-DMP-23", got, {"lc_ok": True})


def embedded_view(e):
    voice = [p for p in e["known"]["packets"] if p["type"] == dmrplus.VOICE]
    if len(voice) < 6:
        return {"sync_ok": False, "emb_ok": False, "lc_ok": False, "bursts": len(voice)}
    a = voice[0]
    sf = dmr.superframe_lc([p["burst"] for p in voice[1:5]])
    emb = [dmr.parse_emb(dmr.embedded(p["burst"])[0]) for p in voice[1:6]]
    return {"sync_ok": dmrplus.centre(a["burst"]) in (dmrplus.BS_VOICE, dmrplus.MS_VOICE),
            "emb_ok": all(x["valid"] and x["cc"] == 1 for x in emb),
            "lc_ok": bool(sf["checksum_ok"] and sf["fec_ok"] and sf["lc"] and sf["lc"]["flco"] == 0
                          and sf["lc"]["src"] == TALKER),           # an all-zero LC also checks
            "lc": sf["lc"],
            "bursts": len(voice)}


def test_dmp_egress_embedded_lc(dmp, egress):
    """P-DMP-24 (ETSI 7.1.3): A voice sync, B-F EMB (CC 1) + embedded LC."""
    got = view(egress, embedded_view)
    judge_spec(dmp, "P-DMP-24", got, {"sync_ok": True, "emb_ok": True, "lc_ok": True})


def test_dmp_egress_terminator(dmp, egress):
    """P-DMP-25 (ETSI 7.1.2): the stream ends with a terminator with LC."""
    def term(e):
        pkts = e["known"]["packets"]
        last = pkts[-1] if pkts else None
        ok = bool(last and last["type"] == dmrplus.TERMINATOR)
        lc = dmrplus.lc_header(last["burst"]) if ok else None
        return {"terminator": ok, "lc_ok": bool(lc and lc["data_type"] == 2 and lc["lc"]["src"] == TALKER)}
    got = view(egress, term)
    judge_spec(dmp, "P-DMP-25", got, {"terminator": True, "lc_ok": True})


def test_dmp_egress_sequence(dmp, egress):
    """P-DMP-26: one packet per burst, sequence counting from the header."""
    def seq(e):
        pkts, times = e["known"]["packets"], e["known"]["times"]
        values = [p["seq"] for p in pkts]
        steps = {(b - a) % 256 for a, b in zip(values, values[1:])}
        burst = max((sum(1 for u in times if t <= u < t + 0.040) for t in times), default=0)
        return {"steps": sorted(steps), "first_voice": values[1] if len(values) > 1 else None, "bursty": burst > 2}
    got = view(egress, seq)
    same(got, "steps", "first_voice", "bursty")


def test_dmp_frames_exact_from_mmdvm(dmp, egress):
    """P-DMP-27 / 33: MMDVM -> DMR+ frames unchanged (pair34 layout)."""
    sent = frames(seed="egress")
    got = view(egress, lambda e: {"exact": dmrplus.frames_of(e["known"]["packets"])[:FRAMES] == sent,
                                  "frames": len(dmrplus.frames_of(e["known"]["packets"]))})
    assert got["ref"]["exact"]
    same(got, "exact", "frames")


# ---------------------------------------------------------------- 4. malformed input

@pytest.mark.parametrize("size", [3, 30, 73, 1400], ids=["short", "30", "73", "oversize"])
def test_dmp_garbage_ignored(dmp, size):
    """P-DMP-28: other sizes get no reply; keepalives are still ACKed."""
    def run(rig, side):
        d = dongle(LISTENER)
        try:
            linked(rig, side, d)
            with marked(rig, "dmp-garbage-%d-%s" % (size, side)):
                replies = ask(d, b"\x31" * size, 0.5)
            ack = dmrplus.ACK in ask(d, dmrplus.connect_packet(d.id, d.reflector), 1.0)
            return {"replies": len(replies), "ack": ack}
        finally:
            done(d)
    got = both(dmp, run)
    same(got, "replies", "ack")


def test_dmp_unknown_type(dmp):
    """P-DMP-29: a 72-byte packet of type 9 opens nothing."""
    def run(rig, side):
        voice = frames(seed="type9")
        pkts = [dmrplus.packet(9, i, b"\x55" * 33, TALKER, 4008, 0x77) for i in range(5)]
        pkts += dmrplus.stream(voice, TALKER, 4008, head=False)
        return {"frames": len(to_mmdvm(rig, side, voice, tag="dmp-type9", packets=pkts)["frames"])}
    got = both(dmp, run)
    same(got, "frames")


def test_dmp_unlinked_sender(dmp):
    """P-DMP-30: a stream from an address that never connected."""
    def run(rig, side):
        stranger, listener = dongle(EXTRA[1]), mmdvm(LISTENER)
        try:
            linked(rig, side, listener)
            stranger.dest = (ip_of(rig, side), dmrplus.PORT)
            with marked(rig, "dmp-unlinked-%s" % side):
                packets = dmrplus.stream(frames(seed="stranger"), stranger.id, 4008)
                start = time.monotonic()
                for index, data in enumerate(packets):
                    pace(start, index, dmrplus.GAP)
                    stranger.client.send(stranger.dest, data)
                    listener.client.drain()
                hold(TAIL, listener)
            return {"frames": len(heard_mmdvm(listener))}
        finally:
            stranger.dest = None
            done(stranger, listener)
    got = both(dmp, run)
    same(got, "frames")


# ---------------------------------------------------------------- 5. several clients, other modes

@pytest.mark.parametrize("ids", [EXTRA, (LISTENER, LISTENER)], ids=["two_ports", "same_id"])
def test_dmp_two_clients(dmp, ids):
    """P-DMP-31 (two ids, one IP) / P-DMP-32 (one id, two ports): both hear
    the module once."""
    def run(rig, side):
        a, b, talker = dongle(ids[0]), dongle(ids[1]), mmdvm(TALKER)
        try:
            linked(rig, side, a, b, talker)
            mmdvm_talk(talker, [a, b])
            return {"a": len(dmrplus.frames_of(a.voice())), "b": len(dmrplus.frames_of(b.voice()))}
        finally:
            done(a, b, talker)
    got = both(dmp, run)
    assert got["ref"]["a"] == got["ref"]["b"] > 0
    same(got, "a", "b")


@pytest.mark.parametrize("kind", ["dstar", "m17"])
def test_dmp_cross_mode(dmp, kind):
    """P-DMP-34: DMR+ -> D-STAR / M17 through the transcoder; sut == ref."""
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    voice = ambed.transcode(dmp.ambed_ref, ambed.PCM, [ambed.AMBE2PLUS], pcm, port=dmp.ambed_port,
                            transaction=0x444D5000)[ambed.AMBE2PLUS]

    def run(rig, side):
        talker, listener = dongle(TALKER), clients.make(kind, MOD, 1)
        try:
            linked(rig, side, talker)
            assert listener.link(ip_of(rig, side))
            hold(CACHE, talker)
            listener.client.drain()
            listener.client.inbox.clear()
            dongle_talk(talker, [], voice)
            got = listener.collect(settle=0.5)
            return {"received": len(got), "sha256": hashlib.sha256(b"".join(got)).hexdigest()}
        finally:
            done(talker)
            clients.close(listener)
    got = both(dmp, run)
    assert got["ref"]["received"] >= FRAMES * 0.8
    same(got, "received", "sha256")


def test_dmp_busy_module_no_mix(dmp):
    """P-DMP-35 (B1): a DMR+ header on a module busy with an MMDVM stream."""
    def run(rig, side):
        first, second, listener = mmdvm(TALKER), dongle(EXTRA[0]), mmdvm(LISTENER)
        try:
            linked(rig, side, first, second, listener)
            a, b = frames(120, seed="busy-a"), frames(18, seed="busy-b")
            job = threading.Thread(target=first.talk, args=(a,),
                                   kwargs={"stream_id": clients.fresh_stream_id("dmr"), "settle": 0.2, "tail": 0.3})
            job.start()
            time.sleep(1.0)
            second.send(dmrplus.stream(b, second.id, second.reflector), dmrplus.GAP, listener)
            job.join()
            hold(TAIL, listener)
            heard = heard_mmdvm(listener)
            got = set(heard)
            winners = [name for name, voice in (("first", a), ("second", b)) if got & set(voice)]
            at_a = [i for i, f in enumerate(heard) if f in set(a)]
            at_b = [i for i, f in enumerate(heard) if f in set(b)]
            mixed = bool(at_a and at_b and at_a[0] < at_b[0] < at_a[-1])
            return {"winners": len(winners), "first": winners[:1] == ["first"], "mixed": mixed,
                    "a_frames": len(at_a), "b_frames": len(at_b)}
        finally:
            done(first, second, listener)
    got = both(dmp, run)
    judge_behaviour(dmp, "test_first_talker_wins", got, ("winners", "first", "mixed"),
                    lambda entry: {"winners": entry["winners"], "mixed": False})
