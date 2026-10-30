# SPDX-License-Identifier: MIT
"""Yaesu IMRS conformance (docs/protocols/imrs.md, rows P-IMR-NN): the
tester plays a DR-2X repeater towards xlxd-ref and xlxd-sut in parallel.

IMRS has no port choice: every peer sends to UDP 21110, so the tester
binds 21110 once (dvtest.proto.imrs.Hub) and tells ref and sut apart by
source address. One tester address means one IMRS peer per callsign;
IMRS-to-IMRS cases need two addresses and are not run.

Order matters: the ping cases run before anything registers; the cases
that register more callsigns run near the end; the slow case idles 190 s
first (xlxd drops IMRS clients after 150-180 s).

Judged as in imrs.md: hb asserted on both sides; diff = sut == ref; dev
= sut == ref under the xlxd profile, else SPEC["imrs"]["P-IMR-NN"]; under
any other profile every case skips ("edge not ready") until
SPEC["imrs"]["server"] is True.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import ambed, dmr, imrs, nxdn

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "J", "K"                       # DG-ID 19 and 20
CALL = "N0CALL"
TALKER_DMR, LISTENER_DMR, EXTRA_DMR = 1023007, 1023008, 1023101
FRAMES = 30                                 # 6 IMRS packets, 10 DMR bursts
SETTLE = 1.5
RESULTS = {}


def record(value, key=None):
    print(value)
    case = os.environ.get("PYTEST_CURRENT_TEST", "case").split(" ")[0].split("::")[-1]
    RESULTS[case + ("/" + key if key else "")] = value
    out = os.environ.get("DVT_OUT")
    if out:
        with open(os.path.join(out, "imrs.json"), "w") as fh:
            json.dump(RESULTS, fh, indent=1, sort_keys=True, default=str)


def mark(rig, phase, tag):
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "imrs-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def both(ctx, func, *args, tag=None):
    """{"ref": func(xlxd ip, *args), "sut": ...} in parallel (in a dvcheck
    mark window when tag is given)."""
    got = {}

    def run(name):
        try:
            got[name] = func(getattr(ctx["rig"], "xlxd_" + name), *args)
        except Exception as exc:                    # reported below
            got[name] = exc

    if tag:
        mark(ctx["rig"], "begin", tag)
    try:
        threads = [threading.Thread(target=run, args=(n,)) for n in ("ref", "sut")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        if tag:
            mark(ctx["rig"], "end", tag)
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    record({n: {k: v for k, v in got[n].items() if not k.startswith("_")} for n in ("ref", "sut")})
    return got


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def judge_dev(rig, nn, got, *keys):
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = specs.rule_mode(rig, "imrs", nn) or {}
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    pending = [key for key in keys if key not in entry]
    if pending:
        pytest.skip("spec pending: no imrs %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def frames9(n=FRAMES, seed="imrs"):
    """Valid DMR-form AMBE+2 frames (the VCH carries only the 49 data bits)."""
    return [nxdn.to_dmr(nxdn.from_dmr(f)) for f in signals.prng_frames("dmr", n, seed=seed)]


_sid = [0x100]


def sid():
    _sid[0] += 1
    return _sid[0]


@pytest.fixture(scope="module")
def ctx(rig):
    if rig.sut_profile != specs.DEFAULT and not (specs.rule(rig, "imrs") or {}).get("server"):
        pytest.skip("edge not ready: SPEC['imrs']['server'] is not True in dvtest/specs/%s.py" % rig.sut_profile)
    try:
        hub = imrs.Hub()
    except OSError as exc:
        pytest.skip("cannot bind UDP %d on the tester: %s" % (imrs.PORT, exc))
    try:
        for name in ("ref", "sut"):
            ip = getattr(rig, "xlxd_" + name)
            deadline = time.monotonic() + 30
            while True:
                m = hub.mark()
                hub.send(ip, imrs.PING)
                time.sleep(0.5)
                if any(len(d) == imrs.INFO_SIZE for _, _, d in hub.since(m, ip)):
                    break
                if time.monotonic() > deadline:
                    pytest.fail("%s %s does not answer an IMRS ping on UDP %d" % (name, ip, imrs.PORT))
        yield {"rig": rig, "hub": hub}
    finally:
        hub.close()


# ---------------------------------------------------------------- helpers

def register(hub, ip, callsign=CALL):
    hub.send(ip, imrs.info(callsign))
    time.sleep(0.3)


def join(hub, ip, module=MOD, callsign=CALL):
    """Register, then an empty call with the room's DG-ID moves the peer
    there (xlxd re-links on the header's DG-ID)."""
    register(hub, ip, callsign)
    s = sid()
    dgid = imrs.module_dgid(module)
    hub.paced(ip, [imrs.header(s, callsign, callsign + " B", dgid), imrs.terminator(s, dgid, 1)], 0.1)
    time.sleep(1.8)


def dmr_client(ip, module=MOD, dmrid=LISTENER_DMR):
    client = dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=module)
    assert client.link(ip), "%s: DMR link failed" % ip
    client.client.inbox.clear()
    client.client.inbox_times.clear()
    return client


def dmr_rx(client):
    out = []
    for data in client.client.inbox:
        info = dmr.parse(data)
        if not info:
            continue
        bits = info["bitfield"]
        kind = {1: "header", 2: "terminator"}.get(bits & 0xF, "data") if (bits >> 4) & 3 == 2 else "voice"
        out.append((kind, info))
    return out


def dmr_voice(client):
    return [f for kind, info in dmr_rx(client) if kind == "voice" for f in dmr.extract(info["payload"])]


def close(*made):
    for client in made:
        try:
            if client.dest:
                client.unlink()
        finally:
            client.close()


def ingress(hub, ip, packets, module=MOD, listener_kind="dmr", wait=SETTLE):
    """IMRS packets into xlxd, heard by a listener on the module."""
    if listener_kind == "dmr":
        listener = dmr_client(ip, module)
    else:
        listener = clients.make(listener_kind, module, 1)
        assert listener.link(ip)
        listener.client.inbox.clear()
    # the listener's join (an empty call) also reaches the IMRS peer: let it pass
    time.sleep(0.8)
    listener.client.inbox.clear()
    m = hub.mark()
    try:
        hub.paced(ip, packets)
        listener.client.pump(wait)
        out = {"_echo": hub.packets(m, ip), "_listener": listener}
        if listener_kind == "dmr":
            out["_frames"], out["_rx"] = dmr_voice(listener), dmr_rx(listener)
        else:
            out["_frames"] = listener.collect(settle=0.2)
        return out
    finally:
        close(listener)


def egress(hub, ip, voice9, module=MOD, dmrid=TALKER_DMR, kind="dmr"):
    """A call from another mode on the module, as the IMRS peer receives it."""
    talker = dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=module) if kind == "dmr" else clients.make(kind, module, 0)
    try:
        assert talker.link(ip), "%s: %s talker link failed" % (ip, kind)
        time.sleep(0.3)
        m = hub.mark()
        clients.talk(talker, kind, voice9, tail=SETTLE)
        return hub.packets(m, ip)
    finally:
        close(talker)


def kinds(packets):
    return [p["kind"] for p in packets]


def voice_frames(packets):
    return [f for p in packets if p["kind"] == "voice" for f in p["frames"]]


# ---------------------------------------------------------------- 1-2. transport, ping, info (before any registration)

def ping_answer(hub, ip):
    m = hub.mark()
    hub.send(ip, imrs.PING)
    time.sleep(1.0)
    got = hub.since(m, ip)
    return {"answered": bool(got), "sizes": sorted(len(d) for _, _, d in got),
            "bytes": sum(len(d) for _, _, d in got), "_info": next((d for _, _, d in got if len(d) == imrs.INFO_SIZE), None)}


def test_imrs_ping_answer(ctx):
    """P-IMR-03: a ping gets an info packet and a ping back (measured size)."""
    got = both(ctx, lambda ip: ping_answer(ctx["hub"], ip))
    same(got, "sizes")
    assert got["ref"]["sizes"] == [imrs.PING_SIZE, imrs.INFO_SIZE], got["ref"]


def test_imrs_ping_from_stranger(ctx):
    """P-IMR-04: the tester has not sent info yet (not a peer): is the ping
    answered? (dev: xlxd answers anyone, 16 B in -> 76 B out)."""
    got = both(ctx, lambda ip: ping_answer(ctx["hub"], ip))
    judge_dev(ctx["rig"], "P-IMR-04", got, "answered")


def test_imrs_info_content(ctx):
    """P-IMR-05: callsign and DG-ID bitmap of the info packet (dev)."""
    def run(ip):
        info = imrs.parse_info(ping_answer(ctx["hub"], ip)["_info"] or b"")
        assert info, "%s: no info packet" % ip
        return {"callsign": info["callsign"], "dgids": info["dgids"], "tail": info["tail"].hex()}

    got = both(ctx, run)
    judge_dev(ctx["rig"], "P-IMR-05", got, "dgids", "callsign")


def test_imrs_port_21110(ctx):
    """P-IMR-01: a ping from another source port is answered at port 21110,
    from port 21110."""
    import socket

    def run(ip):
        side = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        side.bind(("0.0.0.0", 0))
        side.settimeout(0.05)
        m = ctx["hub"].mark()
        try:
            side.sendto(imrs.PING, (ip, imrs.PORT))
            to_source = 0
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                try:
                    side.recvfrom(2048)
                    to_source += 1
                except socket.timeout:
                    pass
            at_hub = ctx["hub"].since(m, ip)
            return {"to_source_port": to_source, "at_21110": len(at_hub),
                    "from_port": sorted({a[1] for _, a, _ in at_hub})}
        finally:
            side.close()

    got = both(ctx, run)
    for name in ("ref", "sut"):
        row = got[name]
        assert row["to_source_port"] == 0 and row["at_21110"] > 0 and row["from_port"] == [imrs.PORT], (name, row)


# ---------------------------------------------------------------- 3-5. with N0CALL registered on MOD

@pytest.fixture(scope="module")
def peer(ctx):
    for name in ("ref", "sut"):
        join(ctx["hub"], getattr(ctx["rig"], "xlxd_" + name))
    return ctx


@pytest.fixture(scope="module")
def eg(peer):
    """One DMR call of FRAMES frames on MOD, as the IMRS peer receives it."""
    sent = frames9()
    got = both(peer, lambda ip: {"_packets": egress(peer["hub"], ip, sent)})
    return {"sent": sent, "got": got}


@pytest.fixture(scope="module")
def ig(peer):
    """One IMRS call of FRAMES frames on MOD, heard by a DMR listener."""
    sent = frames9(seed="imrs-in")

    def run(ip):
        dgid = imrs.module_dgid(MOD)
        return ingress(peer["hub"], ip, imrs.stream_packets(sent, sid(), CALL, CALL + " B", dgid))

    return {"sent": sent, "got": both(peer, run)}


@pytest.mark.parametrize("what", ["mapped", "unmapped"])
def test_imrs_dgid_room(peer, what):
    """P-IMR-08: the header's DG-ID selects the room; mapped: heard there and
    the peer follows; unmapped (DG-ID 5): heard? still linked? (dev)."""
    hub = peer["hub"]

    def run(ip):
        try:
            dgid = imrs.module_dgid(OTHER) if what == "mapped" else 5
            room = OTHER if what == "mapped" else MOD
            heard = ingress(hub, ip, imrs.stream_packets(frames9(10, "dgid"), sid(), CALL, CALL + " B", dgid), room)
            follow = egress(hub, ip, frames9(6, "dgid-follow"), room)
            return {"heard": len(heard["_frames"]) > 0, "follows_or_linked": "voice" in kinds(follow)}
        finally:
            join(hub, ip)

    got = both(peer, run, tag="imrs-dgid-" + what)
    if what == "mapped":
        same(got, "heard", "follows_or_linked")
    else:
        judge_dev(peer["rig"], "P-IMR-08", got, "follows_or_linked")


def test_imrs_egress_dgid(peer, eg):
    """P-IMR-09: every egress packet carries the room's DG-ID, SQL 0."""
    got = {n: {"dgids": sorted({p["fich"]["sq"] for p in eg["got"][n]["_packets"]}),
               "sql": sorted({p["fich"]["sql"] for p in eg["got"][n]["_packets"]})} for n in ("ref", "sut")}
    record(got)
    same(got, "dgids", "sql")
    assert got["sut"]["dgids"] == [imrs.module_dgid(MOD)], got


def test_imrs_egress_room_only(peer):
    """P-IMR-10: a call on another room does not reach the peer."""
    got = both(peer, lambda ip: {"packets": len(egress(peer["hub"], ip, frames9(6, "other"), OTHER))})
    same(got, "packets")
    assert got["sut"]["packets"] == 0, got


def test_imrs_egress_header(peer, eg):
    """P-IMR-11: the call starts with a 91 B header, FI 0, DT 2, src = the
    talker; the fixed fields recorded."""
    got = {}
    for n in ("ref", "sut"):
        pk = eg["got"][n]["_packets"]
        head = pk[0]
        got[n] = {"first": head["kind"], "fi": head["fich"]["fi"], "dt": head["fich"]["dt"], "cm": head["fich"]["cm"],
                  "src": head.get("src", b"").decode("ascii", "replace").strip(),
                  "dest": head.get("dest", b"").hex(), "up": head.get("up", b"").decode("ascii", "replace"),
                  "rem": head.get("rem", b"").hex(), "headers": kinds(pk).count("header")}
    record(got)
    for n, row in got.items():
        assert row["first"] == "header" and row["fi"] == 0 and row["dt"] == 2 and row["headers"] == 1, (n, row)
    same(got, "src", "dest", "up", "rem", "cm")


def test_imrs_voice_passthrough(peer, eg, ig):
    """P-IMR-12: V/D type 2, five frames per packet, FI 1; AMBE+2 frames
    unchanged both ways (DMR -> IMRS and IMRS -> DMR, no gain step)."""
    got = {}
    for n in ("ref", "sut"):
        pk = [p for p in eg["got"][n]["_packets"] if p["kind"] == "voice"]
        got[n] = {"voice_packets": len(pk), "fi": sorted({p["fich"]["fi"] for p in pk}),
                  "dt": sorted({p["fich"]["dt"] for p in pk}), "fn": sorted({p["fich"]["fn"] for p in pk}),
                  "out_exact": voice_frames(pk) == eg["sent"],
                  "in_exact": ig["got"][n]["_frames"][:FRAMES] == ig["sent"], "in_frames": len(ig["got"][n]["_frames"])}
    record(got)
    for n, row in got.items():
        assert row["voice_packets"] == FRAMES // 5 and row["fi"] == [1] and row["dt"] == [2], (n, row)
        assert row["out_exact"] and row["in_exact"], (n, row)
    same(got, "fn", "in_frames")


def test_imrs_egress_terminator(peer, eg):
    """P-IMR-13: the call ends with one 31 B terminator, FI 2."""
    for n in ("ref", "sut"):
        pk = eg["got"][n]["_packets"]
        assert pk[-1]["kind"] == "terminator" and pk[-1]["fich"]["fi"] == 2, (n, kinds(pk))
        assert kinds(pk).count("terminator") == 1, (n, kinds(pk))


def test_imrs_no_echo(peer, ig):
    """P-IMR-18: the talking peer gets nothing of its own call back (dev:
    xlxd sends the tail of the call back once the talker's terminator has
    released it)."""
    got = {n: {"echo": len(ig["got"][n]["_echo"]),
               "echo_kinds": kinds(ig["got"][n]["_echo"])} for n in ("ref", "sut")}
    record(got)
    judge_dev(peer["rig"], "P-IMR-18", got, "echo")


def test_imrs_empty_stream_not_relayed(peer):
    """P-IMR-24: an empty call from another mode (a DMR join: header +
    terminator) is not sent to the IMRS peer (dev)."""
    def run(ip):
        talker = dmr.DmrClient("0.0.0.0", dmrid=EXTRA_DMR, module=MOD)
        m = peer["hub"].mark()
        try:
            assert talker.link(ip)                  # the join is the empty call
            time.sleep(SETTLE)
            got = kinds(peer["hub"].packets(m, ip))
            return {"relayed": bool(got), "kinds": got}
        finally:
            close(talker)

    judge_dev(peer["rig"], "P-IMR-24", both(peer, run), "relayed")


def test_imrs_partial_group(peer):
    """P-IMR-14: 9 DMR frames: how many reach the IMRS peer (dev: xlxd
    sends only whole groups of five)."""
    got = both(peer, lambda ip: {"frames": len(voice_frames(egress(peer["hub"], ip, frames9(9, "partial"))))})
    judge_dev(peer["rig"], "P-IMR-14", got, "frames")


def test_imrs_terminator_no_audio(peer):
    """P-IMR-15: 15 IMRS frames + terminator (a whole number of DMR bursts):
    frames and all-zero frames at the DMR listener (dev: xlxd injects one
    zero frame per terminator, which also forces DMR padding)."""
    def run(ip):
        got = ingress(peer["hub"], ip, imrs.stream_packets(frames9(15, "term"), sid(), CALL, CALL + " B",
                                                            imrs.module_dgid(MOD)))
        return {"frames": len(got["_frames"]), "zero_frames": sum(f == bytes(9) for f in got["_frames"])}

    judge_dev(peer["rig"], "P-IMR-15", both(peer, run), "frames", "zero_frames")


def test_imrs_stream_id(peer):
    """P-IMR-17: two IMRS calls in a row from the peer: same DMR stream id
    at the listener? (dev: xlxd derives it from the peer address)."""
    def run(ip):
        listener = dmr_client(ip)
        try:
            for seed in ("sid1", "sid2"):
                imrs_packets = imrs.stream_packets(frames9(10, seed), sid(), CALL, CALL + " B", imrs.module_dgid(MOD))
                peer["hub"].paced(ip, imrs_packets)
                listener.client.pump(2.0)
            sids = [info["stream"] for kind, info in dmr_rx(listener) if kind == "header"]
            return {"headers": len(sids), "same_sid": len(sids) == 2 and sids[0] == sids[1]}
        finally:
            close(listener)

    judge_dev(peer["rig"], "P-IMR-17", both(peer, run), "same_sid")


def test_imrs_callsign_case(peer):
    """P-IMR-20: a header with a lower-case source callsign: routed? (dev).
    Heard by a D-STAR listener, so a radio-id lookup policy for DMR egress
    (DVT_NO_RADIO_ID) cannot hide the case handling."""
    def run(ip):
        got = ingress(peer["hub"], ip, imrs.stream_packets(frames9(10, "case"), sid(), "n0call", "n0call B",
                                                            imrs.module_dgid(MOD)), listener_kind="dstar")
        return {"heard": len(got["_frames"]) > 0}

    judge_dev(peer["rig"], "P-IMR-20", both(peer, run, tag="imrs-lowercase"), "heard")


def test_imrs_lost_terminator(peer):
    """P-IMR-21: an IMRS call without a terminator: the DMR listener gets a
    terminator, the room is free afterwards (spec B3)."""
    def run(ip):
        got = ingress(peer["hub"], ip, imrs.stream_packets(frames9(10, "lost"), sid(), CALL, CALL + " B",
                                                            imrs.module_dgid(MOD), end=False), wait=2.5)
        after = egress(peer["hub"], ip, frames9(6, "after-lost"))
        return {"terminator": any(k == "terminator" for k, _ in got["_rx"]), "freed": "voice" in kinds(after)}

    judge_dev(peer["rig"], "P-IMR-21", both(peer, run, tag="imrs-lost-terminator"), "terminator", "freed")


def test_imrs_two_talkers(peer):
    """P-IMR-22: an IMRS call and a DMR call on the room at once: one
    winner, no mixing at a second DMR listener (spec B1)."""
    a, b = frames9(30, "two-a"), frames9(30, "two-b")

    def run(ip):
        listener = dmr_client(ip)
        talker = dmr.DmrClient("0.0.0.0", dmrid=EXTRA_DMR, module=MOD)
        try:
            assert talker.link(ip)
            done = threading.Event()

            def dmr_side():
                time.sleep(0.4)
                talker.talk(b, stream_id=clients.fresh_stream_id("dmr"), settle=0.1, tail=0.2)
                done.set()

            threading.Thread(target=dmr_side, daemon=True).start()
            peer["hub"].paced(ip, imrs.stream_packets(a, sid(), CALL, CALL + " B", imrs.module_dgid(MOD)))
            done.wait(10)
            listener.client.pump(SETTLE)
            heard = dmr_voice(listener)
            from_a, from_b = sum(f in set(a) for f in heard), sum(f in set(b) for f in heard)
            return {"from_imrs": from_a, "from_dmr": from_b, "mixed": from_a > 0 and from_b > 0}
        finally:
            close(talker, listener)

    judge_dev(peer["rig"], "P-IMR-22", both(peer, run, tag="imrs-two-talkers"), "mixed")


@pytest.fixture(scope="module")
def encoded(peer):
    rig = peer["rig"]
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    out = ambed.transcode(rig.ambed_ref, ambed.PCM, [clients.CODEC[k] for k in clients.KINDS], pcm,
                          port=rig.ambed_port)
    return {k: out[clients.CODEC[k]] for k in clients.KINDS}


@pytest.mark.parametrize("kind", ["dstar", "m17"])
def test_imrs_cross_mode(peer, encoded, kind):
    """P-IMR-23: IMRS -> D-STAR / M17 and back through the transcoder: sut
    == ref (frame counts and bytes)."""
    import hashlib

    def run(ip):
        into = ingress(peer["hub"], ip, imrs.stream_packets(encoded["dmr"], sid(), CALL, CALL + " B",
                                                             imrs.module_dgid(MOD)), listener_kind=kind)
        back = egress(peer["hub"], ip, encoded[kind], kind=kind)
        out_frames = voice_frames(back)
        return {"to_" + kind: len(into["_frames"]), "to_sha": hashlib.sha256(b"".join(into["_frames"])).hexdigest()[:16],
                "from_" + kind: len(out_frames), "from_sha": hashlib.sha256(b"".join(out_frames)).hexdigest()[:16]}

    got = both(peer, run)
    assert got["ref"]["to_" + kind] >= FRAMES * 0.8, got
    same(got, "to_" + kind, "to_sha", "from_" + kind, "from_sha")


# ---------------------------------------------------------------- more callsigns from the tester address (late)

@pytest.mark.parametrize("size", [7, 8])
def test_imrs_register(peer, size):
    """P-IMR-06: an info packet with a 7- or 8-character callsign registers
    a peer (default room B) that then hears room B (dev for 8)."""
    callsign = {7: "N0TEST1", 8: "N0TEST12"}[size]

    def run(ip):
        register(peer["hub"], ip, callsign)
        heard = egress(peer["hub"], ip, frames9(6, "reg"), "B")
        heads = [p for p in heard if p["kind"] == "header"]
        return {"registered": bool(heads), "copies": len(heads)}

    got = both(peer, run)
    if size == 7:
        same(got, "registered")
    else:
        judge_dev(peer["rig"], "P-IMR-06", got, "registered")


def test_imrs_two_callsigns_one_ip(peer):
    """P-IMR-02: two callsigns from one address on one room: how many copies
    of a call reach that address? (dev: xlxd keeps one client per
    callsign and address)."""
    def run(ip):
        join(peer["hub"], ip, OTHER, "N0TEST")
        join(peer["hub"], ip, OTHER, "N0TEST2")
        heard = egress(peer["hub"], ip, frames9(6, "copies"), OTHER)
        return {"copies": kinds(heard).count("header")}

    judge_dev(peer["rig"], "P-IMR-02", both(peer, run), "copies")


# ---------------------------------------------------------------- slow: timeout, unregistered header

@pytest.mark.slow
def test_imrs_peer_timeout(peer):
    """P-IMR-07 and P-IMR-19: after 190 s with no traffic the peers are
    dropped: a header from the now unregistered address is not routed, and
    a call no longer reaches it. Then, registered again: does a call 100 s
    later (egress) keep it alive at 200 s without info packets (dev:
    xlxd refreshes every IMRS client on every egress packet)?"""
    hub = peer["hub"]

    def run(ip):
        time.sleep(190)
        unreg = ingress(hub, ip, imrs.stream_packets(frames9(10, "unreg"), sid(), CALL, CALL + " B",
                                                      imrs.module_dgid(MOD)))
        dropped = "voice" not in kinds(egress(hub, ip, frames9(6, "idle"), "B"))
        join(hub, ip)
        start = time.monotonic()
        time.sleep(100)
        egress(hub, ip, frames9(6, "refresh"))
        time.sleep(max(0.0, 200 - (time.monotonic() - start)))
        kept = "voice" in kinds(egress(hub, ip, frames9(6, "kept")))
        join(hub, ip)
        return {"header_unregistered_routed": len(unreg["_frames"]) > 0, "dropped_after_idle": dropped,
                "kept_by_egress": kept}

    got = both(peer, run, tag="imrs-timeout")
    same(got, "header_unregistered_routed")
    judge_dev(peer["rig"], "P-IMR-07", got, "dropped_after_idle", "kept_by_egress")
