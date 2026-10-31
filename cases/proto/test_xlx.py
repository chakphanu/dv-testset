# SPDX-License-Identifier: MIT
"""XLX interlink conformance (docs/protocols/xlx.md): one case per
obligation P-XLX-NN. The tester is the XLX peer XLX996 (and BM3109) of
xlxd-ref and xlxd-sut (rig profile interlink lists it on both) and speaks
the interlink from UDP 10002 to both at once; ordinary clients on the
reflector and the rig peers XLX998 / XLX997 observe.

Judged as in xlx.md: diff = sut == ref; dev = sut == ref under the xlxd
profile, else the profile's "xlx" spec entry {"P-XLX-NN": {key: value}}
(skip "spec pending" without one); spec (Bn) = the DV-Router behaviour
spec. There is no hb row: the interlink has no published spec. Every case
prints what both sides did.
"""
import contextlib
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import dextra, xlx

pytestmark = [pytest.mark.proto, pytest.mark.rig, pytest.mark.interlink]

MOD, HOLD, UNSHARED = "D", "H", "H"      # D shared with XLX996; H only local
FRAMES = 40
CURRENT = (2, 6, 1)
SIDES = ("ref", "sut")


# ---------------------------------------------------------------- judging

def both(rig, func, *args):
    """{"ref": func("ref", ip, *args), "sut": ...} in parallel."""
    got = {}

    def side(name):
        try:
            got[name] = func(name, getattr(rig, "xlxd_" + name), *args)
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n,)) for n in SIDES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in SIDES:
        if isinstance(got[name], BaseException):
            raise AssertionError("%s: %r" % (name, got[name])) from got[name]
    print("ref:", got["ref"], "\nsut:", got["sut"])
    return got


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def judge_dev(rig, nn, got, *keys):
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "xlx") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no xlx %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


@contextlib.contextmanager
def marked(rig, tag):
    """Control channel "mark begin|end xlx-<tag>" around deliberately
    malformed tester traffic, so dvcheck reports it as a warning (no-op
    without the control channel)."""
    _mark(rig, "begin", "xlx-" + tag)
    try:
        yield
    finally:
        _mark(rig, "end", "xlx-" + tag)


def _mark(rig, phase, tag):
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "xlx-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------- rig

@pytest.fixture(scope="module")
def xrig(rig):
    if os.environ.get("DVT_INTERLINK") != "on":
        pytest.skip("no interlink rig: run.sh --profile interlink (tester row XLX996)")
    return rig


@pytest.fixture(scope="module")
def peer(xrig):
    p = xlx.XlxPeer("XLX996", "DE", CURRENT)
    yield p
    p.close()


def ip_of(rig, name):
    return getattr(rig, "xlxd_" + name)


def relink(peer, ip, **kw):
    """Unlink (if linked) and link again with the given identity."""
    if ip in peer.alive:
        peer.unlink(ip)
        time.sleep(0.5)
    got = peer.link(ip, **kw)
    assert got, "%s: no full link as %s" % (ip, kw.get("callsign", peer.callsign))
    return got


@pytest.fixture
def linked(xrig, peer):
    """XLX996, modules DE, version 2.6.1, linked to both reflectors."""
    for name in SIDES:
        ip = ip_of(xrig, name)
        if peer.callsign != "XLX996" or peer.version != CURRENT or peer.modules != "DE" or ip not in peer.alive:
            relink(peer, ip, callsign="XLX996", version=CURRENT, modules="DE")
    return peer


def dstar_frames(n=FRAMES, seed="xlx"):
    return signals.prng_frames("dstar", n, seed=seed)


def listeners(ip, module=MOD, kinds=("dstar", "dmr", "m17")):
    made = {}
    for kind in kinds:
        c = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=module) if kind == "dstar" \
            else clients.make(kind, module, 1)
        assert c.link(ip), "%s link to %s failed" % (kind, ip)
        made[kind] = c
    for c in made.values():
        c.client.drain()
        c.client.inbox.clear()
    return made


def collected(made, settle=1.0):
    out = {}
    for kind, c in made.items():
        out[kind] = c.collect(settle=settle)
        if kind == "dstar":
            sids = set()
            for p in c.client.inbox:
                parsed = dextra.parse_header(p) or dextra.parse_voice(p)
                if parsed:
                    sids.add(parsed["stream_id"] if isinstance(parsed, dict) else parsed[0])
            out["dstar_sids"] = sids
    clients.close(*made.values())
    return out


def ingress(peer, ip, frames, sid, module=MOD, kinds=("dstar", "dmr", "m17"), **kw):
    """Stream from the tester peer; what the reflector's clients got."""
    made = listeners(ip, module, kinds)
    try:
        peer.talk(ip, module, frames, sid, **kw)
    except Exception:
        clients.close(*made.values())
        raise
    return collected(made)


def egress(peer, ip, frames, module=MOD, talking=None):
    """D-Star talker on the reflector; the stream the tester peer got:
    (talker sid, {sid: stream}) of DSVT packets after the talk began.
    talking (an Event) is set when the stream starts."""
    talker = clients.make("dstar", module, 0)
    try:
        assert talker.link(ip), "link to %s failed" % ip
        if talking is not None:
            talking.set()
        start = time.monotonic()
        sid = clients.talk(talker, "dstar", frames, tail=1.0)
    finally:
        clients.close(talker)
    return sid, peer.streams(ip, start)


def main_stream(streams, sid):
    return streams.get(sid) or {"headers": 0, "frames": [], "header": None}


def fresh():
    return clients.fresh_stream_id("dstar")


# ================================================================ 1. link

def link_reply(ip, packet, peer):
    data = peer.ask(ip, packet, lambda d: (xlx.parse_link(d) or {}).get("tag") == "A" or xlx.parse_short(d)
                    is not None and xlx.parse_short(d)["tag"] == "N", 1.5)
    if data is None:
        return {"reply": None}
    parsed = xlx.parse_link(data) or xlx.parse_short(data)
    return {"reply": parsed["tag"], "callsign": parsed["callsign"], "modules": parsed.get("modules"),
            "version": parsed.get("version")}


def test_xlx_connect_ack(xrig, peer):
    """P-XLX-01: L from a listed peer -> A with the shared modules."""
    got = both(xrig, lambda n, ip: link_reply(ip, xlx.connect("XLX996", "DE", CURRENT), peer))
    same(got, "reply", "modules", "callsign")
    assert got["ref"]["reply"] == "A" and got["ref"]["modules"] == "DE", got["ref"]


def test_xlx_ack_module_intersection(xrig, peer):
    """P-XLX-02 (dev): L offering D, E and H -> modules of the A."""
    got = both(xrig, lambda n, ip: link_reply(ip, xlx.connect("XLX996", "DEH", CURRENT), peer))
    judge_dev(xrig, "P-XLX-02", got, "reply", "modules")


def test_xlx_unlisted_nack(xrig, peer):
    """P-XLX-03: a callsign that is not listed gets N."""
    got = both(xrig, lambda n, ip: link_reply(ip, xlx.connect("XLX995", "DE", CURRENT), peer))
    same(got, "reply")
    assert got["ref"]["reply"] == "N", got["ref"]


def test_xlx_no_common_module_nack(xrig, peer):
    """P-XLX-04: no module in common -> N."""
    got = both(xrig, lambda n, ip: link_reply(ip, xlx.connect("XLX996", "JK", CURRENT), peer))
    same(got, "reply")
    assert got["ref"]["reply"] == "N", got["ref"]


def test_xlx_wrong_address(xrig, peer):
    """P-XLX-05 (dev): L with XLX998's callsign from the tester address
    (XLX998 is listed at another address). Only an L is sent, so no peer
    object is created even when it is acked."""
    with marked(xrig, "wrong-address"):
        got = both(xrig, lambda n, ip: link_reply(ip, xlx.connect("XLX998", "DE", CURRENT), peer))
    judge_dev(xrig, "P-XLX-05", got, "reply")


def test_xlx_half_link_no_traffic(xrig, peer):
    """P-XLX-06: our L acked, their L never answered -> no traffic."""
    def side(name, ip):
        if ip in peer.alive:
            peer.unlink(ip)
        peer.quiet(ip)
        time.sleep(1.0)
        reply = link_reply(ip, xlx.connect("XLX996", "DE", CURRENT), peer)
        sid, streams = egress(peer, ip, dstar_frames(20, "half"))
        return {"reply": reply["reply"], "frames": len(main_stream(streams, sid)["frames"])}
    got = both(xrig, side)
    same(got, "reply", "frames")
    assert got["ref"]["frames"] == 0, got["ref"]


def test_xlx_initiates_connect(xrig, peer):
    """P-XLX-07: an unlinked listed peer gets an L every 5 s, one per
    listed row. The tester address carries two rows (XLX996 with the
    interlink modules, BM3109 with the first one). An L names only the
    sending reflector, not the row, so the rows show up as the distinct
    module sets offered (l_module_sets: xlxd ["D", "DE"])."""
    def side(name, ip):
        if ip in peer.alive:
            peer.unlink(ip)
        peer.quiet(ip)
        start = time.monotonic()
        time.sleep(11.5)
        conns = [(t, xlx.parse_link(d)) for t, d in peer.packets(ip, start, lambda d: (xlx.parse_link(d) or {})
                                                                   .get("tag") == "L")]
        times = sorted({round(t, 1) for t, _c in conns})
        gaps = [round(b - a) for a, b in zip(times, times[1:]) if b - a > 1]
        return {"count_ok": len(times) >= 2, "callsign": {c["callsign"] for _t, c in conns},
                "l_module_sets": sorted({c["modules"] for _t, c in conns}), "gaps": sorted(set(gaps))}
    got = both(xrig, side)
    same(got, "count_ok", "callsign")
    assert got["ref"]["count_ok"], got["ref"]
    if xrig.sut_profile == specs.DEFAULT:
        same(got, "l_module_sets")
        return
    entry = (specs.rule(xrig, "xlx") or {}).get("P-XLX-07") or {}
    if "l_module_sets" in entry:
        assert got["sut"]["l_module_sets"] == entry["l_module_sets"], "P-XLX-07 l_module_sets: sut %r, spec %r" % (
            got["sut"]["l_module_sets"], entry["l_module_sets"])
    else:
        # until the profile states it: the XLX996 row's set must be offered
        assert "DE" in got["sut"]["l_module_sets"], "sut: no L with the modules DE: %s" % got["sut"]["l_module_sets"]


@pytest.mark.parametrize("version", [(1, 3, 9), (1, 4, 0), (2, 2, 0), (2, 6, 1)],
                         ids=["v1.3.9", "v1.4.0", "v2.2.0", "v2.6.1"])
def test_xlx_revision_by_version(xrig, peer, version):
    """P-XLX-08: egress frame size follows the version in our A."""
    def side(name, ip):
        relink(peer, ip, callsign="XLX996", version=version, modules="DE")
        sid, streams = egress(peer, ip, dstar_frames(20, "rev"))
        frames = main_stream(streams, sid)["frames"]
        return {"sizes": sorted({f["size"] for f in frames}), "frames": len(frames)}
    got = both(xrig, side)
    same(got, "sizes")
    assert got["ref"]["frames"] >= 18, got["ref"]
    print("expected by the README table:", xlx.FRAME_SIZE[xlx.revision_of(version)])


def test_xlx_bm_peer_class(xrig, peer):
    """P-XLX-09 (dev): linked as BM3109 (module D): egress frame form."""
    def side(name, ip):
        relink(peer, ip, callsign="BM3109", version=CURRENT, modules="D")
        try:
            sid, streams = egress(peer, ip, dstar_frames(20, "bm"))
            frames = main_stream(streams, sid)["frames"]
        finally:
            peer.unlink(ip, "BM3109")
            time.sleep(0.5)
        return {"sizes": sorted({f["size"] for f in frames}),
                "dstar_zero": bool(frames) and all(not any(f["ambe"]) for f in frames[:-1])}
    got = both(xrig, side)
    peer.callsign, peer.modules = "XLX996", "DE"
    judge_dev(xrig, "P-XLX-09", got, "sizes", "dstar_zero")


def test_xlx_relink_single(xrig, linked):
    """P-XLX-10: a repeated L and A while linked: one link, frames once."""
    def side(name, ip):
        linked.send(ip, xlx.connect("XLX996", "DE", CURRENT))
        linked.send(ip, xlx.ack("XLX996", "DE", CURRENT))
        time.sleep(1.0)
        sid, streams = egress(linked, ip, dstar_frames(20, "relink"))
        return {"frames": len(main_stream(streams, sid)["frames"])}
    got = both(xrig, side)
    same(got, "frames")
    assert got["ref"]["frames"] == 21, got["ref"]


# ================================================================ 2. keepalive / disconnect

def test_xlx_keepalive_form(xrig, linked):
    """P-XLX-11: 9 B keepalive with the reflector callsign, about 1/s."""
    def side(name, ip):
        start = time.monotonic()
        time.sleep(5.0)
        kas = linked.packets(ip, start, lambda d: xlx.parse_keepalive(d) is not None)
        return {"callsigns": sorted({xlx.parse_keepalive(d) for _t, d in kas}),
                "per_5s": len(kas), "rate_ok": 4 <= len(kas) <= 6}
    got = both(xrig, side)
    same(got, "callsigns", "rate_ok")


def test_xlx_disconnect(xrig, linked):
    """P-XLX-13: U removes the link; the reflector then sends L again."""
    def side(name, ip):
        linked.unlink(ip)
        start = time.monotonic()
        time.sleep(0.5)
        sid, streams = egress(linked, ip, dstar_frames(20, "unlink"))
        again = linked.wait(ip, lambda d: (xlx.parse_link(d) or {}).get("tag") == "L", 8.0, start)
        return {"frames": len(main_stream(streams, sid)["frames"]), "l_again": again is not None}
    got = both(xrig, side)
    same(got, "frames", "l_again")
    assert got["ref"]["frames"] == 0 and got["ref"]["l_again"], got["ref"]


def test_xlx_foreign_callsign_unlink(xrig, linked):
    """P-XLX-14 (dev): U carrying another callsign from the peer's address."""
    def side(name, ip):
        linked.send(ip, xlx.disconnect("XLX995"))
        time.sleep(0.5)
        sid, streams = egress(linked, ip, dstar_frames(20, "foreign"))
        return {"linked_after": len(main_stream(streams, sid)["frames"]) > 0}
    with marked(xrig, "foreign-unlink"):
        got = both(xrig, side)
    judge_dev(xrig, "P-XLX-14", got, "linked_after")


# ================================================================ 3. frames (egress)

@pytest.fixture(scope="module")
def egress_run(xrig, peer):
    for name in SIDES:
        ip = ip_of(xrig, name)
        if peer.callsign != "XLX996" or peer.version != CURRENT or ip not in peer.alive:
            relink(peer, ip, callsign="XLX996", version=CURRENT, modules="DE")
    sent = dstar_frames(FRAMES, "egress")

    def side(name, ip):
        sid, streams = egress(peer, ip, sent)
        return {"sid": sid, "streams": streams}
    return sent, both(xrig, side)


def summary(sent, run):
    sid, streams = run["sid"], run["streams"]
    s = main_stream(streams, sid)
    frames = s["frames"]
    body = [f for f in frames if not f["last"]]
    h = s["header"] or {}
    return {
        "sids": sorted(streams), "sid_kept": sid in streams, "headers_ge1": s["headers"] >= 1,
        "rpt2": h.get("rpt2"), "rpt1": h.get("rpt1"), "my": h.get("my"), "ur": h.get("ur"),
        "sizes": sorted({f["size"] for f in frames}), "count": len(body),
        "seq_ok": [f["seq"] for f in body] == [i % 21 for i in range(len(body))],
        "dstar_exact": [f["ambe"] for f in body] == sent[:len(body)],
        "last": bool(frames) and frames[-1]["last"],
        "last_ambe": frames[-1]["ambe"].hex() if frames else None,
        "last_slow": frames[-1]["slow"].hex() if frames else None,
        "dmr_voiced": sum(1 for f in body if any(f.get("ambe2", b""))),
        "c2_voiced": sum(1 for f in body if any(f.get("codec2", b""))),
    }


def test_xlx_egress_header(xrig, egress_run):
    """P-XLX-15: 56 B header, RPT2 = own callsign + module, sid kept."""
    sent, runs = egress_run
    got = {n: summary(sent, runs[n]) for n in SIDES}
    print(got)
    same(got, "rpt2", "rpt1", "my", "ur", "headers_ge1", "sid_kept")
    assert got["ref"]["rpt2"] == b"XLX999 D" and got["ref"]["sid_kept"], got["ref"]


def test_xlx_egress_voice_layout(xrig, egress_run):
    """P-XLX-16: revision 3 frames (53 B), counter mod 21, D-Star exact."""
    sent, runs = egress_run
    got = {n: summary(sent, runs[n]) for n in SIDES}
    print(got)
    same(got, "sizes", "seq_ok", "dstar_exact", "count")
    assert got["ref"]["sizes"] == [53] and got["ref"]["dstar_exact"], got["ref"]


def test_xlx_egress_slots_transcoded(xrig, egress_run):
    """P-XLX-17 (crossmode): AMBE+2 and Codec2 slots carry audio."""
    sent, runs = egress_run
    got = {n: summary(sent, runs[n]) for n in SIDES}
    print(got)
    for n in SIDES:
        assert got[n]["dmr_voiced"] >= 0.8 * FRAMES and got[n]["c2_voiced"] >= 0.8 * FRAMES, (n, got[n])


def test_xlx_egress_last_frame(xrig, egress_run):
    """P-XLX-19: last frame flagged, D-Star slot = DExtra end pattern."""
    sent, runs = egress_run
    got = {n: summary(sent, runs[n]) for n in SIDES}
    print(got)
    same(got, "last", "last_ambe", "last_slow")
    assert got["ref"]["last"], got["ref"]


def test_xlx_stream_id_kept(xrig, linked):
    """P-XLX-24: the ingress sid reaches the D-Star clients unchanged."""
    def side(name, ip):
        sid = fresh()
        out = ingress(linked, ip, dstar_frames(20, "sid"), sid, kinds=("dstar",))
        return {"kept": out["dstar_sids"] == {sid}, "frames": len(out["dstar"])}
    got = both(xrig, side)
    same(got, "kept")
    assert got["ref"]["kept"], got["ref"]


# ================================================================ 3. frames (ingress)

@pytest.fixture(scope="module")
def rev3_in(xrig, peer):
    for name in SIDES:
        ip = ip_of(xrig, name)
        if peer.callsign != "XLX996" or peer.version != CURRENT or ip not in peer.alive:
            relink(peer, ip, callsign="XLX996", version=CURRENT, modules="DE")
    a = dstar_frames(FRAMES, "in-a")
    b = signals.prng_frames("dmr", FRAMES, seed="in-b")
    c = signals.prng_frames("m17", FRAMES, seed="in-c")

    def side(name, ip):
        return ingress(peer, ip, a, fresh(), ambe2=b, codec2=c)
    return (a, b, c), both(xrig, side)


def matches(got, sent):
    """Fraction of received frames found at the same index in sent."""
    hits = sum(1 for i, f in enumerate(got) if i < len(sent) and f == sent[i])
    return hits / len(sent)


def test_xlx_ingress_rev3_slots(xrig, rev3_in):
    """P-XLX-20: revision 3 slots go to DMR / M17 clients as sent."""
    (a, b, c), runs = rev3_in
    got = {n: {"dstar_exact": runs[n]["dstar"] == a, "dmr_match": round(matches(runs[n]["dmr"], b), 2),
               "m17_in_sent": round(sum(1 for f in runs[n]["m17"] if f in c) / len(c), 2)} for n in SIDES}
    print(got)
    same(got, "dstar_exact", "dmr_match")
    # xlxd-ref loses some M17 frames at the edges: the sut may do better
    assert got["sut"]["m17_in_sent"] >= got["ref"]["m17_in_sent"], got
    assert got["ref"]["dstar_exact"] and got["ref"]["dmr_match"] >= 0.8, got["ref"]


def test_xlx_cross_family(xrig, rev3_in):
    """P-XLX-29: XLX ingress reaches the D-Star, DMR and M17 clients."""
    (a, _b, _c), runs = rev3_in
    got = {n: {k: len(runs[n][k]) >= 0.8 * FRAMES for k in ("dstar", "dmr", "m17")} for n in SIDES}
    print(got)
    same(got, "dstar", "dmr", "m17")
    assert all(got["ref"].values()), got["ref"]


@pytest.fixture(scope="module")
def tone_dstar(xrig):
    from dvtest.proto import ambed
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    return ambed.transcode(xrig.ambed_ref, ambed.PCM, [ambed.AMBEPLUS], pcm,
                           port=xrig.ambed_port)[ambed.AMBEPLUS]


def test_xlx_ingress_rev1_transcoded(xrig, peer, tone_dstar):
    """P-XLX-21 (crossmode): a revision 1 peer's 27 B frames are
    transcoded for the DMR clients."""
    def side(name, ip):
        relink(peer, ip, callsign="XLX996", version=(1, 4, 0), modules="DE")
        out = ingress(peer, ip, tone_dstar, fresh(), revision=1, kinds=("dmr",))
        return {"dmr": out["dmr"], "voiced": sum(1 for f in out["dmr"] if any(f))}
    got = both(xrig, side)
    assert got["sut"]["dmr"] == got["ref"]["dmr"], "DMR from a revision 1 peer: sut %d frames, ref %d" % (
        len(got["sut"]["dmr"]), len(got["ref"]["dmr"]))
    assert got["ref"]["voiced"] >= 0.8 * FRAMES, got["ref"]["voiced"]


def test_xlx_ingress_size_mismatch(xrig, peer):
    """P-XLX-22 (dev): 53 B frames from a peer linked as revision 1, and
    27 B frames from a peer linked as revision 3: what DMR clients get."""
    a = dstar_frames(FRAMES, "mm-a")
    b = signals.prng_frames("dmr", FRAMES, seed="mm-b")

    def side(name, ip):
        relink(peer, ip, callsign="XLX996", version=(1, 4, 0), modules="DE")
        big = ingress(peer, ip, a, fresh(), revision=3, ambe2=b, kinds=("dmr",))["dmr"]
        relink(peer, ip, callsign="XLX996", version=CURRENT, modules="DE")
        small = ingress(peer, ip, a, fresh(), revision=1, kinds=("dmr",))["dmr"]
        return {"rev1_53_dmr_is_sent": matches(big, b) >= 0.8, "rev1_53_frames": len(big) >= 0.8 * FRAMES,
                "rev3_27_dmr_zero": bool(small) and not any(any(f) for f in small),
                "rev3_27_frames": len(small) >= 0.8 * FRAMES}
    with marked(xrig, "size-mismatch"):
        got = both(xrig, side)
    judge_dev(xrig, "P-XLX-22", got, "rev1_53_dmr_is_sent", "rev1_53_frames", "rev3_27_dmr_zero", "rev3_27_frames")


GARBAGE = {
    "short5": b"XLX99",
    "link_no_nul": b"L" + b"XLX996  " + bytes(CURRENT) + b"DE" + b"x" * 25,
    "keepalive_badcs": b"\x01\x02\x03\x04\x05\x06\x07\x08\x00",
    "dsvt_60": b"DSVT\x20\x00\x00\x00\x20\x00\x01\x02" + bytes(48),
    "unknown_tag": b"Z" + b"XLX996  " + b"\x00",
}


def test_xlx_garbage_ignored(xrig, linked):
    """P-XLX-23: malformed packets get no reply and the link stays."""
    def side(name, ip):
        start = time.monotonic()
        for data in GARBAGE.values():
            linked.send(ip, data)
        time.sleep(0.5)
        # an "L" is the server's own periodic link request to an unlinked
        # row (every ~5 s, P-XLX-07), not a reply to the garbage: only A / N / U count
        replies = linked.packets(ip, start, lambda d: (xlx.parse_link(d) or xlx.parse_short(d) or {}).get("tag") in
                                 ("A", "N", "U"))
        out = ingress(linked, ip, dstar_frames(20, "garbage"), fresh(), kinds=("dstar",))
        return {"replies": len(replies), "still_linked": len(out["dstar"]) == 20}
    with marked(xrig, "garbage"):
        got = both(xrig, side)
    same(got, "replies", "still_linked")
    assert got["ref"]["still_linked"], got["ref"]


# ================================================================ 4. streams and routing

def test_xlx_sid_zero(xrig, linked):
    """P-XLX-25: stream id 0 opens nothing."""
    with marked(xrig, "sid-zero"):
        got = both(xrig, lambda n, ip: {"frames": len(ingress(linked, ip, dstar_frames(20, "z"), 0,
                                                              kinds=("dstar",))["dstar"])})
    same(got, "frames")


def test_xlx_sid_open_elsewhere(xrig, linked):
    """P-XLX-26 (spec B8c): our stream whose sid is open on module H."""
    def side(name, ip):
        out = {}
        for case in ("control", "collision"):
            sid = fresh()
            holder = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=HOLD)
            assert holder.link(ip)
            hold = threading.Thread(target=holder.talk, args=([bytes(9)] * 200, sid), kwargs={"tail": 0.3})
            hold.start()
            try:
                time.sleep(0.8)
                theirs = sid if case == "collision" else (sid ^ 0x5A5A) or 1
                out[case] = len(ingress(linked, ip, dstar_frames(20, case), theirs, kinds=("dstar",))["dstar"])
            finally:
                hold.join()
                clients.close(holder)
        return out
    got = both(xrig, side)
    for n in SIDES:
        assert got[n]["control"] == 20, (n, got[n])
    spec = specs.rule(xrig, "test_admission_remote_sid_open_elsewhere")
    if spec is None:
        same(got, "collision")
        assert got["ref"]["collision"] == 0, got["ref"]
    elif spec["remote_sid_open_elsewhere"] == "deny":
        assert got["sut"]["collision"] == 0, got["sut"]


def test_xlx_module_mapping(xrig, linked):
    """P-XLX-27: a header for module H (not shared) opens nothing; a
    stream on H is not sent to us; D maps to D."""
    def side(name, ip):
        h_in = len(ingress(linked, ip, dstar_frames(20, "h"), fresh(), module=UNSHARED, kinds=("dstar",))["dstar"])
        sid, streams = egress(linked, ip, dstar_frames(20, "h-out"), module=UNSHARED)
        d_in = ingress(linked, ip, dstar_frames(20, "d"), fresh(), kinds=("dstar",))
        return {"unshared_in": h_in, "unshared_out": len(main_stream(streams, sid)["frames"]),
                "shared_in": len(d_in["dstar"])}
    got = both(xrig, side)
    same(got, "unshared_in", "unshared_out", "shared_in")
    assert got["ref"] == {"unshared_in": 0, "unshared_out": 0, "shared_in": 20}, got["ref"]


def test_xlx_no_transit(xrig, linked):
    """P-XLX-28 (B11): our stream is not sent to XLX998 / XLX997 nor back
    to us; XLX998's stream is not sent to us. Seen on the wire."""
    peers = {"sut": (xrig.xlxd_peer_sut, xrig.xlxd_peer2_sut), "ref": (xrig.xlxd_peer_ref, xrig.xlxd_peer2_ref)}

    def side(name, ip):
        far = [dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=MOD) for _ in peers[name]]
        for c, pip in zip(far, peers[name]):
            assert c.link(pip), "link to %s failed" % pip
            c.client.drain()
            c.client.inbox.clear()
        start = time.monotonic()
        sid = fresh()
        own = ingress(linked, ip, dstar_frames(20, "transit"), sid, kinds=("dstar",))
        back = len(main_stream(linked.streams(ip, start), sid)["frames"])
        onward = [len(c.collect(settle=0.5)) for c in far]
        clients.close(*far)
        # XLX998 talks: the reflector's own client hears it, we must not
        talker = clients.make("dstar", MOD, 0)
        assert talker.link(peers[name][0])
        start = time.monotonic()
        made = listeners(ip, kinds=("dstar",))
        try:
            sid2 = clients.talk(talker, "dstar", dstar_frames(20, "transit2"), reflector="XLX998", tail=1.0)
        finally:
            clients.close(talker)
        local = len(collected(made)["dstar"])
        to_us = len(main_stream(linked.streams(ip, start), sid2)["frames"])
        return {"own_clients": len(own["dstar"]), "back_to_us": back, "to_xlx998_xlx997": onward,
                "xlx998_local": local, "xlx998_to_us": to_us}
    got = both(xrig, side)
    same(got, "back_to_us", "to_xlx998_xlx997", "xlx998_to_us")
    for n in SIDES:
        assert got[n]["own_clients"] == 20 and got[n]["xlx998_local"] == 20, (n, got[n])
        assert got[n]["back_to_us"] == 0 and got[n]["to_xlx998_xlx997"] == [0, 0] and got[n]["xlx998_to_us"] == 0, \
            (n, got[n])


def test_xlx_stream_timeout(xrig, linked):
    """P-XLX-30: our stream without a last frame frees the module after
    the silence timeout: a local talker 0.5 s later vs 2.2 s later."""
    def side(name, ip):
        out = {}
        for label, wait in (("after_0.5s", 0.5), ("after_2.2s", 2.2)):
            made = listeners(ip, kinds=("dstar",))
            talker = clients.make("dstar", MOD, 0)
            try:
                assert talker.link(ip)
                linked.talk(ip, MOD, dstar_frames(20, "open"), fresh(), last=False)
                time.sleep(wait)
                talker.client.drain()
                made["dstar"].client.inbox.clear()
                sid = clients.talk(talker, "dstar", dstar_frames(20, "next"), tail=1.0)
            finally:
                clients.close(talker)
            got = collected(made)
            out[label] = sid in got["dstar_sids"]
            time.sleep(2.0)
        return out
    with marked(xrig, "no-last-frame"):
        got = both(xrig, side)
    same(got, "after_0.5s", "after_2.2s")
    assert got["ref"]["after_2.2s"], got["ref"]


def test_xlx_repeated_header(xrig, linked):
    """P-XLX-31: a header sent 5 times opens one stream; frames once."""
    def side(name, ip):
        out = ingress(linked, ip, dstar_frames(20, "rep"), fresh(), header_repeat=5, kinds=("dstar",))
        return {"streams": len(out["dstar_sids"]), "frames": len(out["dstar"])}
    got = both(xrig, side)
    same(got, "streams", "frames")
    assert got["ref"] == {"streams": 1, "frames": 20}, got["ref"]


def test_xlx_headerless_voice(xrig, linked):
    """P-XLX-32 (dev): voice frames of a sid that had no header."""
    with marked(xrig, "headerless"):
        got = both(xrig, lambda n, ip: {"relayed": len(ingress(linked, ip, dstar_frames(20, "nohdr"), fresh(),
                                                               with_header=False, kinds=("dstar",))["dstar"])})
    judge_dev(xrig, "P-XLX-32", got, "relayed")


def test_xlx_cut_stream_no_closing_frame(xrig, linked):
    """P-XLX-34 (dev; D8): a stream from XLX998 (its D-Star client stops
    without a last frame) is cut 0.5 s later by a local header on D. We
    (XLX996) never got that stream (no transit), so we must get no
    packet of it either, the closing last frame included."""
    peers = {"sut": xrig.xlxd_peer_sut, "ref": xrig.xlxd_peer_ref}

    def side(name, ip):
        far = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=MOD)
        local = clients.make("dstar", MOD, 1)
        try:
            assert far.link(peers[name]), "link to %s failed" % peers[name]
            assert local.link(ip), "link to %s failed" % ip
            start = time.monotonic()
            sid = fresh()
            far.client.send(far.dest, dextra.header(sid, "N0CALL", MOD, reflector="XLX998"))
            far.client.paced(far.dest, [dextra.voice(sid, i, f, False) for i, f in
                                        enumerate(dstar_frames(20, "cut"))], 0.020)
            time.sleep(0.5)
            clients.talk(local, "dstar", dstar_frames(20, "cutter"), tail=1.5)
        finally:
            clients.close(far, local)
        ours = [d for _t, d in linked.packets(ip, start) if d.startswith(b"DSVT") and
                int.from_bytes(d[12:14], "little") == sid]
        return {"closing_frame_to_other_peers": len(ours),
                "kinds": sorted({"header" if len(d) == 56 else "last" if (xlx.parse_voice(d) or {}).get("last")
                                 else "voice" for d in ours})}
    with marked(xrig, "cut-stream"):
        got = both(xrig, side)
    judge_dev(xrig, "P-XLX-34", got, "closing_frame_to_other_peers")


# ================================================================ slow / faults (last)

@pytest.mark.slow
def test_xlx_silent_peer_dropped(xrig, linked):
    """P-XLX-12: no keepalive for 30 s -> U, link removed, L resumes."""
    def side(name, ip):
        linked.quiet(ip)
        start = time.monotonic()
        u = linked.wait(ip, lambda d: (xlx.parse_short(d) or {}).get("tag") == "U", 45.0, start)
        if u is None:
            return {"u": False}
        again = linked.wait(ip, lambda d: (xlx.parse_link(d) or {}).get("tag") == "L", 8.0, u[0])
        sid, streams = egress(linked, ip, dstar_frames(20, "silent"))
        return {"u": True, "after_s": round(u[0] - start), "u_in_30_36": 29 <= u[0] - start <= 36,
                "l_again": again is not None, "frames": len(main_stream(streams, sid)["frames"])}
    got = both(xrig, side)
    same(got, "u", "u_in_30_36", "l_again", "frames")
    assert got["ref"]["u"] and got["ref"]["frames"] == 0, got["ref"]


def form_of(frames):
    """How missing transcoder output reached the peer."""
    body = [f for f in frames if not f["last"]]
    if not body:
        return "dropped"
    if {f["size"] for f in body} == {27}:
        return "rev1"
    if not any(any(f.get("ambe2", b"")) or any(f.get("codec2", b"")) for f in body):
        return "zero_slot"
    return "content"


@pytest.fixture(scope="module")
def transcoder_fault(xrig, rig_control, peer):
    """Phase "mid": a 4 s stream; both transcoders stop after 1 s (a slot
    goes missing inside a transcoded stream). Phase "whole": a stream
    while they are down. Transcoders are started again afterwards."""
    for name in SIDES:
        ip = ip_of(xrig, name)
        if peer.callsign != "XLX996" or peer.version != CURRENT or ip not in peer.alive:
            relink(peer, ip, callsign="XLX996", version=CURRENT, modules="DE")
    out = {n: {} for n in SIDES}
    long = dstar_frames(200, "mid")
    sent = dstar_frames(FRAMES, "whole")
    try:
        talks = {}
        talking = {n: threading.Event() for n in SIDES}

        def mid(name):
            talks[name] = egress(peer, ip_of(xrig, name), long, talking=talking[name])

        threads = [threading.Thread(target=mid, args=(n,)) for n in SIDES]
        for t in threads:
            t.start()
        for n in SIDES:
            assert talking[n].wait(10), "%s: talker did not start" % n
        time.sleep(1.2)
        rig_control("stop", "transcoder-sut")
        rig_control("stop", "transcoder-ref")
        stopped = time.monotonic()
        for t in threads:
            t.join()
        for name in SIDES:
            sid, _streams = talks[name]
            frames = [(t, xlx.parse_voice(d)) for t, d in peer.packets(ip_of(xrig, name), 0.0)
                      if xlx.parse_voice(d) and xlx.parse_voice(d)["stream_id"] == sid]
            before = [f for t, f in frames if t < stopped - 0.3]
            after = [f for t, f in frames if t > stopped + 0.5]
            out[name]["mid"] = {"egress": form_of(after), "before": form_of(before), "after_frames": len(after),
                                "frames": len(frames)}
        time.sleep(1.0)
        for name in SIDES:
            sid, streams = egress(peer, ip_of(xrig, name), sent)
            s = summary(sent, {"sid": sid, "streams": streams})
            out[name]["whole"] = {"egress": form_of(main_stream(streams, sid)["frames"]), "sizes": s["sizes"],
                                  "dstar_exact": s["dstar_exact"], "count": s["count"]}
    finally:
        rig_control("start", "transcoder-sut")
        rig_control("start", "transcoder-ref")
    print(out)
    return out


def test_xlx_egress_slot_missing_mid_stream(xrig, transcoder_fault):
    """P-XLX-18 (dev; B6b): transcoders lost inside a transcoded stream:
    the frames go on, the missing slots are zero."""
    got = {n: transcoder_fault[n]["mid"] for n in SIDES}
    for n in SIDES:
        assert got[n]["before"] == "content" and got[n]["after_frames"] >= 50, (n, got[n])
    judge_dev(xrig, "P-XLX-18", got, "egress")


def test_xlx_egress_transcoder_down(xrig, transcoder_fault):
    """P-XLX-33 (dev; B6b): no transcoder for the whole stream: frame
    size and slot contents sent to the peer."""
    got = {n: transcoder_fault[n]["whole"] for n in SIDES}
    for n in SIDES:
        assert got[n]["dstar_exact"] and got[n]["count"] == FRAMES, (n, got[n])
    judge_dev(xrig, "P-XLX-33", got, "egress")
