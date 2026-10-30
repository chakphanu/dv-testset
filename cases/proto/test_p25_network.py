# SPDX-License-Identifier: MIT
"""P25 network (P25Reflector / P25Gateway, UDP 41000) conformance
(docs/protocols/p25.md, testlist K): one case per obligation P-P25-NN,
ref and sut probed in parallel.

Sections 1-3 and 6 (link, relay, malformed input, rooms) talk to the P25
server each side's clients link to: DVT_P25_REF (P25Reflector p25-ref, the
reference server) and DVT_P25_SUT (the sut's own P25 server; under the
xlxd profile, which has none, the p25-sut P25Reflector). A non-xlxd
profile runs them only with SPEC["p25"]["server"] = True ("edge not
ready" skip otherwise). Sections 4-5 (egress and ingress call structure,
peer mode) go through xlxd: a D-STAR client on DVT_P25_MODULE of
xlxd-ref / xlxd-sut and a P25 client on the P25Reflector peered with it
(DVT_P25_PEER_SUT, default DVT_P25_SUT); they need a transcoder that
serves IMBE.

Judged as in p25.md: hb = a spec requirement, asserted on both sides;
dev = disputed (or xlxd deviates): sut == ref under the xlxd profile,
else the profile's "p25" spec entry {"P-P25-NN": {observed key: value}}
(skip "spec pending" without one); diff = sut == ref. B1/B3 rows read
the profile's behaviour entries. Every case prints what both sides did.
"""
import os
import threading
import time

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import ambed, dextra, p25

pytestmark = [pytest.mark.proto, pytest.mark.rig, pytest.mark.p25]

FRAMES = 18                                     # one superframe
CALLS = ("N0CALL", "N0TEST", "N0P25A", "N0P25B")
IDS = (1023007, 1023008, 1023101, 1023102)
UNKNOWN_ID = 1023999                            # in no dmrid.dat
UNKNOWN_CALL = "N0UNK"                          # in no dmrid.dat
TAIL = 0.6
CALL_GAP = 2.0                                  # past every floor / stream timeout
SILENT_SAMPLES = (35, 65, 95, 125)              # s after the listener's last poll


# ---------------------------------------------------------------- setup

def imbe_served(addr, port):
    client = ambed.AmbedClient(addr, port)
    try:
        caps = ambed.parse_caps(client.rpc(ambed.CAPS_REQUEST) or b"")
        if caps is not None:
            return bool(caps.mask & (1 << ambed.IMBE)), "CAPS mask 0x%02x" % caps.mask
        reply = client.rpc(ambed.open_legacy("N0TEST", ambed.IMBE, ambed.AMBEPLUS, ambed.AMBE2PLUS,
                                             ambed.CODEC2_3200)) or b""
        std = ambed.parse_std(reply)
        if std is not None:
            client.close_stream(std.stream_id)
        return std is not None, "no CAPS, legacy open 4->(1,2,3)"
    finally:
        client.close()


@pytest.fixture(scope="module")
def ep(rig):
    """The P25 endpoints, talkgroup and module of the rig (profile p25).
    rig.p25: the P25 server each side's clients link to (DVT_P25_REF /
    DVT_P25_SUT); rig.p25_peer: the P25Reflector peered with each xlxd
    (DVT_P25_PEER_SUT, default DVT_P25_SUT, for a sut that serves P25
    itself and also peers with a reflector)."""
    if os.environ.get("DVT_P25") != "on":
        pytest.skip("no P25 endpoints: run.sh --profile p25 (DVT_P25=on)")
    rig.p25 = {"ref": rig.p25_ref, "sut": rig.p25_sut}
    rig.p25_peer = {"ref": rig.p25_ref, "sut": os.environ.get("DVT_P25_PEER_SUT") or rig.p25_sut}
    rig.p25_tg = int(os.environ.get("DVT_P25_TG", "31672"))
    rig.p25_module = os.environ.get("DVT_P25_MODULE", "G")
    print("P25 endpoints ref %s sut %s, TG %d, module %s"
          % (rig.p25_ref, rig.p25_sut, rig.p25_tg, rig.p25_module))
    return rig


@pytest.fixture(scope="module")
def server(ep):
    """Sections 1-3 and 6: the sut's own P25 server (reference: the
    P25Reflector p25-ref). Under a non-xlxd profile it must declare
    SPEC["p25"]["server"] = True, else the cases skip "edge not ready"."""
    if ep.sut_profile != specs.DEFAULT:
        ready = (specs.rule(ep, "p25") or {}).get("server")
        if ready is not True:
            pytest.skip("P25 server edge not ready: SPEC['p25']['server'] = %r in dvtest/specs/%s.py"
                        % (ready, ep.sut_profile))
    return ep


@pytest.fixture(scope="module")
def chain(ep):
    """ep, with xlxd peered and a transcoder that serves IMBE on both sides
    (a D-STAR call reaches a P25 listener)."""
    for name in ("ambed_ref", "ambed_sut"):
        ok, how = imbe_served(getattr(ep, name), ep.ambed_port)
        if not ok:
            pytest.skip("%s does not serve IMBE (%s): xlxd <-> P25 needs IMBE transcoding" % (name, how))

    def ready(rig, side):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if dstar_to_p25(rig, side, dstar_frames(9, "probe"))["records"]:
                return True
            time.sleep(2)
        return False

    got = both(ep, ready, show=False)
    assert got["ref"] and got["sut"], "xlxd not peered with its P25 reflector after 60 s: %s" % got
    return ep


# ---------------------------------------------------------------- helpers

def both(rig, func, *args, show=True):
    """{"ref": func(rig, "ref", *args), "sut": ...} in parallel; printed
    unless show is False (the module fixtures print per case)."""
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
    if show:
        print("ref: %r\nsut: %r" % (got["ref"], got["sut"]))
    return got


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def judge_dev(rig, nn, got, *keys):
    """sut == ref under the xlxd profile, else the profile's p25 entry."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = specs.rule_mode(rig, "p25", nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no p25 %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def judge_behaviour(rig, case, got, keys, expect):
    """B rows: sut == ref under the xlxd profile, else expect(entry) gives
    {key: value} from the profile's behaviour entry for case."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = specs.rule(rig, case)
    if entry is None:
        pytest.skip("spec pending: no %s entry in dvtest/specs/%s.py" % (case, rig.sut_profile))
    for key, value in expect(entry).items():
        assert got["sut"][key] == value, "%s %s: sut %r, spec %r" % (case, key, got["sut"][key], value)


def new(rig, index=0, **kw):
    return p25.P25Client("0.0.0.0", callsign=kw.get("callsign", CALLS[index]),
                         src_id=kw.get("src_id", IDS[index]), tg=rig.p25_tg)


def linked(rig, side, *made, peer=False):
    """Link every client to the side's P25 server (peer=True: to the
    P25Reflector peered with the side's xlxd)."""
    ip = (rig.p25_peer if peer else rig.p25)[side]
    for client in made:
        assert client.link(ip), "%s: no poll echo from %s" % (side, ip)
    clear(*made)
    return made


def clear(*made):
    for client in made:
        client.client.drain()
        client.client.inbox.clear()
        client.client.inbox_times.clear()


def drain_for(seconds, *made):
    """Drain without polling (P25Client.pump would poll)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for client in made:
            client.client.drain()
        time.sleep(0.01)


def done(*made):
    for client in made:
        try:
            if client.dest:
                client.unlink()
        finally:
            client.close()


def voice(client):
    """Parsed voice records and terminators in the inbox."""
    return [r for r in (p25.parse(d) for d in client.client.inbox) if r is not None]


def frames(n=FRAMES, seed="p25"):
    return signals.prng_frames("imbe", n, seed=seed)


def dstar_frames(n, seed="p25"):
    return signals.prng_frames("dstar", n, seed=seed)


def call(talker, imbe, *listeners, **shape):
    """talker sends one call (p25.call_records with shape), listeners drain."""
    records = p25.call_records(imbe, talker.src_id, talker.tg, **shape)
    talker.send(records, p25.GAP, *listeners)
    drain_for(TAIL, talker, *listeners)
    return records


def ask(client, packet, seconds=1.0):
    """Send packet with an empty inbox; the first reply or None."""
    clear(client)
    client.client.send(client.dest, packet)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        client.client.drain()
        if client.client.inbox:
            return client.client.inbox[0]
        time.sleep(0.01)
    return None


# ---------------------------------------------------------------- 1. link, keepalive, unlink

def test_p25_poll_echo(server):
    """P-P25-01: a poll is echoed byte for byte."""
    def run(rig, side):
        client = new(rig)
        try:
            client.dest = (rig.p25[side], p25.PORT)
            poll = p25.poll_packet(client.callsign)
            reply = ask(client, poll)
            return {"echo": reply == poll, "reply_len": len(reply) if reply else 0}
        finally:
            done(client)
    got = both(server, run)
    assert got["ref"]["echo"], "reference does not echo the poll"
    same(got, "echo", "reply_len")


def test_p25_no_server_polls(server):
    """P-P25-02: no unsolicited 0xF0/0xF1 to a linked, silent client (12 s)."""
    def run(rig, side):
        client = new(rig)
        try:
            linked(rig, side, client)
            drain_for(12.0, client)
            return {"unsolicited": sum(1 for d in client.client.inbox if p25.parse_poll(d))}
        finally:
            done(client)
    got = both(server, run)
    same(got, "unsolicited")


@pytest.mark.parametrize("size", [10, 12])
def test_p25_poll_wrong_size(server, size):
    """P-P25-03: a poll of another size: reply and link recorded."""
    def run(rig, side):
        client, talker = new(rig, 1), new(rig, 0)
        try:
            client.dest = (rig.p25[side], p25.PORT)
            reply = ask(client, p25.resized(p25.poll_packet(client.callsign), size - 11))
            linked(rig, side, talker)
            clear(client)
            call(talker, frames(), client)
            return {"reply": len(reply) if reply else 0, "linked": bool(voice(client))}
        finally:
            done(client, talker)
    got = both(server, run)
    judge_dev(server, "P-P25-03", got, "reply", "linked")


def test_p25_repoll_single_session(server):
    """P-P25-04: repeated polls keep one session; the call arrives once."""
    def run(rig, side):
        listener, talker = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener, talker)
            for _ in range(3):
                listener.keepalive()
            drain_for(0.3, listener)
            clear(listener)
            sent = call(talker, frames(), listener)
            return {"received": len(voice(listener)), "sent": len(sent)}
        finally:
            done(listener, talker)
    got = both(server, run)
    assert got["ref"]["received"] == got["ref"]["sent"]
    same(got, "received")


def test_p25_two_ports_two_sessions(server):
    """P-P25-05: two clients on one IP (two ports) are two sessions."""
    def run(rig, side):
        a, b, talker = new(rig, 1), new(rig, 2), new(rig, 0)
        try:
            linked(rig, side, a, b, talker)
            sent = call(talker, frames(), a, b)
            return {"a": len(voice(a)), "b": len(voice(b)), "sent": len(sent)}
        finally:
            done(a, b, talker)
    got = both(server, run)
    assert got["ref"]["a"] == got["ref"]["b"] == got["ref"]["sent"]
    same(got, "a", "b")


def test_p25_unlink(server):
    """P-P25-06: unlink -> no reply, no further voice."""
    def run(rig, side):
        listener, talker = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener, talker)
            reply = ask(listener, p25.unlink_packet(listener.callsign), 0.5)
            clear(listener)
            call(talker, frames(), listener)
            return {"reply": reply is not None, "heard": len(voice(listener))}
        finally:
            listener.dest = None
            done(listener, talker)
    got = both(server, run)
    same(got, "reply", "heard")


def test_p25_unlink_unknown(server):
    """P-P25-07: unlink from an address that never linked: ignored."""
    def run(rig, side):
        stranger, listener, talker = new(rig, 2), new(rig, 1), new(rig, 0)
        try:
            stranger.dest = (rig.p25[side], p25.PORT)
            reply = ask(stranger, p25.unlink_packet(stranger.callsign), 0.5)
            linked(rig, side, listener, talker)
            sent = call(talker, frames(), listener)
            return {"reply": reply is not None, "heard_ok": len(voice(listener)) == len(sent)}
        finally:
            stranger.dest = None
            done(stranger, listener, talker)
    got = both(server, run)
    same(got, "reply", "heard_ok")


@pytest.fixture(scope="module")
def silent(server):
    """P-P25-08/09: a listener polls once and stays silent; a talker calls
    at SILENT_SAMPLES s; heard per sample."""
    def run(rig, side):
        listener, talker = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener)
            start = time.monotonic()
            heard = []
            for mark in SILENT_SAMPLES:
                while time.monotonic() < start + mark:
                    listener.client.drain()
                    time.sleep(0.2)
                assert talker.link(rig.p25[side])
                clear(listener)
                call(talker, frames(9, "silent%d" % mark), listener)
                heard.append(bool(voice(listener)))
            return {"heard": heard, "dropped": not heard[-1]}
        finally:
            listener.dest = None
            done(listener, talker)
    return both(server, run)


@pytest.mark.slow
def test_p25_silent_client_dropped(server, silent):
    """P-P25-08: silent client dropped; when (35/65/95/125 s) recorded."""
    judge_dev(server, "P-P25-08", silent, "heard")


@pytest.mark.slow
def test_p25_dropped_client_no_voice(server, silent):
    """P-P25-09: once dropped, no voice."""
    same(silent, "dropped")


# ---------------------------------------------------------------- 2. relay and floor

def test_p25_relay_all_listeners(server):
    """P-P25-10: every other linked client gets the call (bytes recorded)."""
    def run(rig, side):
        a, b, talker = new(rig, 1), new(rig, 2), new(rig, 0)
        try:
            linked(rig, side, a, b, talker)
            sent = call(talker, frames(), a, b)
            return {"a": len(voice(a)), "b": len(voice(b)),
                    "verbatim": [d for d in a.client.inbox if p25.parse(d)] == sent}
        finally:
            done(a, b, talker)
    got = both(server, run)
    assert got["ref"]["a"] == got["ref"]["b"] == FRAMES + 1
    same(got, "a", "b")
    if server.sut_profile == specs.DEFAULT:          # a native server may rebuild the records
        same(got, "verbatim")


def test_p25_no_echo(server):
    """P-P25-11: the talker gets none of its call back."""
    def run(rig, side):
        listener, talker = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener, talker)
            call(talker, frames(), listener)
            return {"echo": len(voice(talker)), "heard": len(voice(listener))}
        finally:
            done(listener, talker)
    got = both(server, run)
    assert got["ref"]["echo"] == 0
    same(got, "echo", "heard")


def test_p25_unlinked_sender_dropped(server):
    """P-P25-12: records from an address that never polled are dropped."""
    def run(rig, side):
        listener, stranger = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener)
            stranger.dest = (rig.p25[side], p25.PORT)
            call(stranger, frames(), listener)
            return {"heard": len(voice(listener))}
        finally:
            stranger.dest = None
            done(listener, stranger)
    got = both(server, run)
    same(got, "heard")


def winners_of(listener, *seeds):
    got = set(r["imbe"] for r in voice(listener) if not r["terminator"])
    return [seed for seed, imbe in seeds if got & set(imbe)]


def test_p25_second_talker_dropped(server):
    """P-P25-13 (B1): a second talker during a call is not mixed in."""
    def run(rig, side):
        listener, a, b = new(rig, 1), new(rig, 0), new(rig, 2)
        try:
            linked(rig, side, listener, a, b)
            first, second = frames(36, "first"), frames(18, "second")
            thread = threading.Thread(target=call, args=(a, first))
            thread.start()
            time.sleep(0.2)
            call(b, second, listener)
            thread.join()
            drain_for(TAIL, listener)
            won = winners_of(listener, ("a", first), ("b", second))
            return {"winners": len(won), "first": won[:1] == ["a"]}
        finally:
            done(listener, a, b)
    got = both(server, run)
    judge_behaviour(server, "test_first_talker_wins", got, ("winners", "first"),
                    lambda entry: {"winners": entry["winners"]})


def test_p25_terminator_frees_floor(server):
    """P-P25-14: 0x80 is relayed and frees the floor at once."""
    def run(rig, side):
        listener, a, b = new(rig, 1), new(rig, 0), new(rig, 2)
        try:
            linked(rig, side, listener, a, b)
            first, second = frames(18, "a14"), frames(18, "b14")
            call(a, first, listener)
            got_a = voice(listener)
            clear(listener)
            time.sleep(0.1)
            call(b, second, listener)
            got_b = [r["imbe"] for r in voice(listener) if not r["terminator"]]
            return {"term_relayed": bool(got_a) and got_a[-1]["terminator"],
                    "second_whole": got_b == second}
        finally:
            done(listener, a, b)
    got = both(server, run)
    judge_dev(server, "P-P25-14", got, "term_relayed", "second_whole")


def test_p25_stream_timeout(server):
    """P-P25-15 (B3): a call without 0x80; a second talker 0.6 s and about
    3 s after its last record; terminator to listeners recorded."""
    def run(rig, side):
        listener, a, b = new(rig, 1), new(rig, 0), new(rig, 2)
        try:
            linked(rig, side, listener, a, b)
            call(a, frames(18, "a15"), listener, end=False)
            early = frames(9, "b15early")
            call(b, early, listener)
            drain_for(2.0, listener)
            late = frames(9, "b15late")
            call(b, late, listener)
            recs = voice(listener)
            terms = [i for i, r in enumerate(recs) if r["terminator"]]
            won = winners_of(listener, ("early", early), ("late", late))
            first_b = next((i for i, r in enumerate(recs) if not r["terminator"]
                            and (r["imbe"] in early or r["imbe"] in late)), len(recs))
            return {"terminator": any(i < first_b for i in terms), "early": "early" in won,
                    "late": "late" in won}
        finally:
            done(listener, a, b)
    got = both(server, run)
    judge_behaviour(server, "test_stream_timeout_frees_module", got, ("terminator", "early", "late"),
                    lambda entry: {"terminator": entry.get("terminator") == "all_egress", "late": True})


# ---------------------------------------------------------------- 3. malformed input

@pytest.mark.parametrize("delta", [1, -1], ids=["long", "short"])
def test_p25_record_wrong_size(server, delta):
    """P-P25-16: voice records one byte too long / short: relayed count."""
    def run(rig, side):
        listener, talker = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener, talker)
            records = [p25.resized(r, delta) for r in p25.call_records(frames(), talker.src_id, talker.tg,
                                                                         end=False)]
            talker.send(records, p25.GAP, listener)
            talker.client.send(talker.dest, p25.terminator())
            drain_for(TAIL, listener)
            return {"relayed": sum(1 for d in listener.client.inbox if d[:1] != b"\xf0" and d[:1] != b"\x80")}
        finally:
            done(listener, talker)
    got = both(server, run)
    judge_dev(server, "P-P25-16", got, "relayed")


def test_p25_unknown_type_dropped(server):
    """P-P25-17: unknown first byte (0x55, 17 B) from a linked client."""
    def run(rig, side):
        listener, talker = new(rig, 1), new(rig, 0)
        try:
            linked(rig, side, listener, talker)
            talker.send([b"\x55" + bytes(16)] * 5, p25.GAP, listener)
            drain_for(TAIL, listener)
            relayed = sum(1 for d in listener.client.inbox if d[:1] == b"\x55")
            talker.client.send(talker.dest, p25.terminator())   # release a floor it may have opened
            drain_for(TAIL, listener)
            return {"relayed": relayed}
        finally:
            done(listener, talker)
    got = both(server, run)
    judge_dev(server, "P-P25-17", got, "relayed")


@pytest.mark.parametrize("size", [3, 1400], ids=["short", "oversize"])
def test_p25_garbage_ignored(server, size):
    """P-P25-18: garbage gets no reply; the link stays (poll still echoed)."""
    def run(rig, side):
        client = new(rig)
        try:
            linked(rig, side, client)
            reply = ask(client, b"\x01" * size, 0.5)
            poll = p25.poll_packet(client.callsign)
            return {"reply": reply is not None, "echo": ask(client, poll) == poll}
        finally:
            done(client)
    got = both(server, run)
    same(got, "reply", "echo")


# ---------------------------------------------------------------- 4. egress (xlxd -> P25)

def dstar_to_p25(rig, side, ambe, callsign=CALLS[0], kind="dstar"):
    """A call of kind on the xlxd module, a P25 listener on the peered
    reflector: received records with arrival times."""
    xlxd_ip = getattr(rig, "xlxd_" + side)
    listener = new(rig, 1)
    talker = (dextra.DstarClient("0.0.0.0", callsign=callsign, module=rig.p25_module) if kind == "dstar"
              else clients.make(kind, rig.p25_module, 0))
    try:
        linked(rig, side, listener, peer=True)
        assert talker.link(xlxd_ip), "%s talker link to %s failed" % (kind, xlxd_ip)
        if ambe is not None:                    # keep what the link itself caused (DMR join)
            drain_for(0.5, listener)
            clear(listener)
            job = threading.Thread(target=clients.talk, args=(talker, kind, ambe, rig.reflector, 0.5))
            job.start()
            while job.is_alive():
                listener.pump(0.01)
            job.join()
        drain_for(1.0, listener)
        pairs = [(d, t) for d, t in zip(listener.client.inbox, listener.client.inbox_times) if p25.parse(d)]
        return {"records": [p25.parse(d) for d, _ in pairs], "raw": [d for d, _ in pairs],
                "times": [t for _, t in pairs]}
    finally:
        done(listener)
        clients.close(talker)


@pytest.fixture(scope="module")
def egress(chain):
    """Per side: a 40-frame D-STAR call (N0CALL), the same from an unknown
    callsign, a 5-frame call, and a DMR join (empty stream)."""
    def run(rig, side):
        out = {"call": dstar_to_p25(rig, side, dstar_frames(40, "egress"))}
        time.sleep(CALL_GAP)
        out["unknown"] = dstar_to_p25(rig, side, dstar_frames(40, "unknown"), callsign=UNKNOWN_CALL)
        time.sleep(CALL_GAP)
        out["short"] = dstar_to_p25(rig, side, dstar_frames(5, "short"))
        time.sleep(CALL_GAP)
        out["join"] = dstar_to_p25(rig, side, None, kind="dmr")
        return out
    return both(chain, run, show=False)


def egress_view(egress, func):
    return {side: func(egress[side]) for side in ("ref", "sut")}


def test_p25_egress_superframe(chain, egress):
    """P-P25-19: egress starts at 0x62 and cycles 0x62..0x73."""
    got = egress_view(egress, lambda e: {
        "first": e["call"]["records"][0]["type"] if e["call"]["records"] else None,
        "problems": p25.lifecycle([r["type"] for r in e["call"]["records"]], whole_ldu=False)})
    print(got)
    for side in ("ref", "sut"):
        assert got[side]["first"] == 0x62 and not got[side]["problems"], "%s: %s" % (side, got[side])


def test_p25_egress_record_layout(chain, egress):
    """P-P25-20: every egress datagram has its type's size."""
    got = egress_view(egress, lambda e: {
        "bad": sum(1 for d in e["call"]["raw"] if p25.parse(d) is None), "records": len(e["call"]["raw"])})
    print(got)
    assert got["ref"]["bad"] == 0
    same(got, "bad", "records")


def test_p25_egress_lcf(chain, egress):
    """P-P25-21: LCF 0x00 (group voice), MFID 0x00."""
    got = egress_view(egress, lambda e: p25.call_fields(e["call"]["records"]))
    print({s: (g["lcf"], g["mfid"], g["svc"]) for s, g in got.items()})
    for side in ("ref", "sut"):
        assert got[side]["lcf"] == {0} and got[side]["mfid"] == {0}, "%s: %s" % (side, got[side])


def test_p25_egress_dst_tg(chain, egress):
    """P-P25-22: 0x65 carries the room's TG."""
    got = egress_view(egress, lambda e: {"dst": sorted(p25.call_fields(e["call"]["records"])["dst"])})
    for side in ("ref", "sut"):
        got[side]["dst_is_tg"] = got[side]["dst"] == [chain.p25_tg]
    print(got)
    judge_dev(chain, "P-P25-22", got, "dst_is_tg")


@pytest.mark.parametrize("who", ["known", "unknown"])
def test_p25_egress_src_id(chain, egress, who):
    """P-P25-23: 0x66 = the talker's id (N0CALL 1023007); an unknown
    callsign's id recorded."""
    key = "call" if who == "known" else "unknown"
    got = egress_view(egress, lambda e: {"src": sorted(p25.call_fields(e[key]["records"])["src"])})
    print(got)
    if who == "known":
        for side in ("ref", "sut"):
            got[side]["src_is_talker"] = got[side]["src"] == [IDS[0]]
        judge_dev(chain, "P-P25-23", got, "src_is_talker")
    else:
        judge_dev(chain, "P-P25-23", got, "src")


def test_p25_egress_terminator_ldu_boundary(chain, egress):
    """P-P25-24: one 0x80 at the end, after a completed LDU (40 frames in)."""
    def view(e):
        types = [r["type"] for r in e["call"]["records"]]
        return {"terminated": types[-1:] == [p25.TERMINATOR] and types.count(p25.TERMINATOR) == 1,
                "whole_ldu": not p25.lifecycle(types), "records": len(types)}
    got = egress_view(egress, view)
    print(got)
    judge_dev(chain, "P-P25-24", got, "terminated", "whole_ldu", "records")


def test_p25_no_lone_terminator(chain, egress):
    """P-P25-25: an empty stream (DMR join) sends no 0x80."""
    got = egress_view(egress, lambda e: {"lone": sum(1 for r in e["join"]["records"] if r["terminator"]),
                                         "voice": sum(1 for r in e["join"]["records"] if not r["terminator"])})
    print(got)
    judge_dev(chain, "P-P25-25", got, "lone", "voice")


def test_p25_short_call_padded(chain, egress):
    """P-P25-26: a 5-frame call: does it reach 0x73 (MMDVMHost plays it)?"""
    got = egress_view(egress, lambda e: {
        "has_0x73": any(r["type"] == 0x73 for r in e["short"]["records"]),
        "records": len(e["short"]["records"])})
    print(got)
    judge_dev(chain, "P-P25-26", got, "has_0x73", "records")


def test_p25_egress_clear_es(chain, egress):
    """P-P25-27: ALGID 0x80, KID 0, MI 0."""
    got = egress_view(egress, lambda e: p25.call_fields(e["call"]["records"]))
    print({s: (g["algid"], g["kid"], g["mi"]) for s, g in got.items()})
    for side in ("ref", "sut"):
        g = got[side]
        assert g["algid"] == {0x80} and g["kid"] == {0} and g["mi"] == {bytes(3)}, "%s: %s" % (side, g)


def test_p25_egress_parity_recorded(chain, egress):
    """P-P25-28: LC/ES parity bytes and LSD, recorded."""
    def view(e):
        fields = p25.call_fields(e["call"]["records"])
        return {"parity": {"%02x" % t: sorted(v.hex() for v in s) for t, s in sorted(fields["parity"].items())},
                "lsd": sorted(v.hex() for v in fields["lsd"])}
    got = egress_view(egress, view)
    print(got)
    judge_dev(chain, "P-P25-28", got, "parity", "lsd")


def test_p25_egress_pacing(chain, egress):
    """P-P25-29: no long bursts (most records within any 60 ms window)."""
    def view(e):
        times = e["call"]["times"]
        burst = max((sum(1 for u in times if t <= u < t + 0.060) for t in times), default=0)
        return {"burst": burst, "bursty": burst >= 6}
    got = egress_view(egress, view)
    print(got)
    same(got, "bursty")


# ---------------------------------------------------------------- 5. ingress (P25 -> xlxd)

def p25_to_dstar(rig, side, talker_kw, imbe, **shape):
    """A P25 call on the reflector, a D-STAR listener on the xlxd module:
    frames and header MY callsigns received."""
    xlxd_ip = getattr(rig, "xlxd_" + side)
    listener = dextra.DstarClient("0.0.0.0", callsign=CALLS[1], module=rig.p25_module)
    talker = new(rig, 0, **talker_kw)
    try:
        assert listener.link(xlxd_ip), "D-STAR listener link to %s failed" % xlxd_ip
        linked(rig, side, talker, peer=True)
        listener.client.drain()
        listener.client.inbox.clear()
        records = shape.pop("records", None) or p25.call_records(imbe, talker.src_id, talker.tg, **shape)
        talker.send(records, p25.GAP)
        got = listener.collect(settle=1.5)
        heads = [dextra.parse_header(d) for d in listener.client.inbox]
        return {"received": len(got), "my": sorted({h["my"].decode().strip() for h in heads if h})}
    finally:
        done(talker)
        listener.unlink()
        listener.close()


@pytest.fixture(scope="module")
def ingress(chain):
    def run(rig, side):
        imbe = frames(36, "ingress")
        out = {"normal": p25_to_dstar(rig, side, {}, imbe)}
        time.sleep(CALL_GAP)
        out["late"] = p25_to_dstar(rig, side, {}, imbe[:34], start=2, padded=False)
        time.sleep(CALL_GAP)
        out["other_tg"] = p25_to_dstar(rig, side, {}, imbe, records=p25.call_records(imbe, IDS[0], 99))
        time.sleep(CALL_GAP)
        out["unknown"] = p25_to_dstar(rig, side, {"src_id": UNKNOWN_ID}, imbe)
        time.sleep(CALL_GAP)
        full = p25.call_records(imbe, IDS[0], rig.p25_tg)
        out["loss"] = p25_to_dstar(rig, side, {}, imbe, records=[r for i, r in enumerate(full) if i not in (5, 20)])
        time.sleep(CALL_GAP)
        out["encrypted"] = p25_to_dstar(rig, side, {}, imbe, lcf=p25.LCF_PROTECTED, algid=0x81, kid=1,
                                        mi=bytes(range(1, 10)))
        return out
    return both(chain, run, show=False)


def ingress_view(ingress, name):
    return {side: dict(ingress[side][name]) for side in ("ref", "sut")}


def test_p25_ingress_no_frame_loss(chain, ingress):
    """P-P25-30: 36 frames (two superframes, no header record) all arrive."""
    got = ingress_view(ingress, "normal")
    print(got)
    assert got["ref"]["received"] > 0, "ref: no frames"
    judge_dev(chain, "P-P25-30", got, "received")


def test_p25_ingress_late_start(chain, ingress):
    """P-P25-31: a call starting at 0x64 (34 frames)."""
    got = ingress_view(ingress, "late")
    print(got)
    judge_dev(chain, "P-P25-31", got, "received")


def test_p25_ingress_other_tg(chain, ingress):
    """P-P25-32: 0x65 = TG 99 still reaches the linked room."""
    got = ingress_view(ingress, "other_tg")
    print(got)
    same(got, "received")


def test_p25_ingress_unknown_src(chain, ingress):
    """P-P25-33: source id in no id database: callsign shown cross-mode."""
    got = ingress_view(ingress, "unknown")
    print(got)
    judge_dev(chain, "P-P25-33", got, "my", "received")


def test_p25_ingress_record_loss(chain, ingress):
    """P-P25-34: records 5 and 20 lost: the call goes on."""
    got = ingress_view(ingress, "loss")
    print(got)
    assert got["ref"]["received"] > 0
    same(got, "received")


def test_p25_ingress_encrypted(chain, ingress):
    """P-P25-35: ALGID 0x81 + protected LCF: transcoded or not."""
    got = ingress_view(ingress, "encrypted")
    for side in ("ref", "sut"):
        got[side]["transcoded"] = got[side]["received"] > 0
    print(got)
    judge_dev(chain, "P-P25-35", got, "transcoded")


# ---------------------------------------------------------------- 6. rooms

def test_p25_room_per_port(server):
    """P-P25-36: the room is the port; poll callsign and 0x65 do not pick it."""
    def run(rig, side):
        a, b = new(rig, 1), new(rig, 3)
        talker = new(rig, 0)
        try:
            linked(rig, side, a, b, talker)
            records = p25.call_records(frames(), talker.src_id, 99)
            talker.send(records, p25.GAP, a, b)
            drain_for(TAIL, a, b)
            return {"a": len(voice(a)), "b": len(voice(b))}
        finally:
            done(a, b, talker)
    got = both(server, run)
    assert got["ref"]["a"] == got["ref"]["b"] == FRAMES + 1
    same(got, "a", "b")
