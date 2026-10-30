# SPDX-License-Identifier: MIT
"""NXDN reflector server conformance (docs/protocols/nxdn.md section 4,
rows P-NXD-31..43): the obligations of a server that NXDNGateway and
DroidStar link to directly on UDP 41400. The tester plays several
gateways on one talkgroup.

Sides: "sut" = DVT_NXDN_SUT (the system under test's NXDN server
endpoint), "ref" = DVT_NXDN_REF (an NXDNReflector, the reference server).
xlxd has no NXDN server, so under the xlxd profile both sides are the rig's
NXDNReflectors. Judging: hb = spec or what every client needs, asserted on
both sides; conv = the convention the clients rely on, asserted on the sut
(ref recorded); dev = sut == ref under the xlxd profile, else the profile's
"nxdn" spec entry. A profile whose SPEC["nxdn"]["server"] is not True
skips every case ("edge not ready").
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import specs
from dvtest.audio import signals
from dvtest.net import UdpClient, pace
from dvtest.proto import nxdn

pytestmark = [pytest.mark.proto, pytest.mark.rig, pytest.mark.nxdn]

FRAMES = 40
IDS = (1001, 1002, 1003)
CALLS = ("N0CALL", "N0TEST", "N0SRV")
SETTLE = 1.5
RESULTS = {}


def record(value, key=None):
    print(value)
    case = os.environ.get("PYTEST_CURRENT_TEST", "case").split(" ")[0].split("::")[-1]
    RESULTS[case + ("/" + key if key else "")] = value
    out = os.environ.get("DVT_OUT")
    if out:
        with open(os.path.join(out, "nxdn_server.json"), "w") as fh:
            json.dump(RESULTS, fh, indent=1, sort_keys=True, default=str)


def mark(rig, phase, tag):
    """dvcheck mark window around deliberate probe traffic (no-op without
    the control channel)."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "nxdn-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


class Server:
    def __init__(self, name, addr, tg):
        self.name, self.addr, self.tg = name, addr, tg


def both(servers, func, *args):
    got = {}

    def run(srv):
        try:
            got[srv.name] = func(srv, *args)
        except Exception as exc:                    # reported below
            got[srv.name] = exc

    threads = [threading.Thread(target=run, args=(s,)) for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    record({n: {k: v for k, v in got[n].items() if not k.startswith("_")} for n in ("ref", "sut")})
    return got


def conv(got, nn, **expected):
    """conv rows: the sut must show the clients' convention; ref recorded."""
    for key, value in expected.items():
        assert got["sut"][key] == value, "%s %s: sut %r, convention %r (ref %r)" % (
            nn, key, got["sut"][key], value, got["ref"][key])


def judge_dev(rig, nn, got, *keys):
    if rig.sut_profile == specs.DEFAULT:
        for key in keys:
            assert got["sut"][key] == got["ref"][key], "%s %s: sut %r, ref %r" % (
                nn, key, got["sut"][key], got["ref"][key])
        return
    entry = (specs.rule(rig, "nxdn") or {}).get(nn) or {}
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    pending = [key for key in keys if key not in entry]
    if pending:
        pytest.skip("spec pending: no nxdn %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def frames49(n=FRAMES, seed="nxdn-server"):
    return [nxdn.from_dmr(f) for f in signals.prng_frames("dmr", n, seed=seed)]


def gw(srv, index, tg=None):
    client = nxdn.NxdnClient("0.0.0.0", tg or srv.tg, nxdn_id=IDS[index], callsign=CALLS[index])
    assert client.link(srv.addr), "%s: %s gets no poll reply" % (srv.name, CALLS[index])
    client.client.inbox.clear()
    return client


def nxdnd(client):
    return [p for p in client.packets()]


def send(talker, packets, *listeners, gap=nxdn.PACKET_GAP, wait=SETTLE):
    talker.client.paced(talker.dest, packets, gap, *[c.client for c in listeners])
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for c in (talker,) + listeners:
            c.client.drain()
        time.sleep(0.01)


def close(*made):
    for client in made:
        try:
            if client.dest:
                client.unlink()
        finally:
            client.close()


@pytest.fixture(scope="module")
def servers(rig):
    if os.environ.get("DVT_NXDN") != "on":
        pytest.skip("no NXDN server: run.sh --profile nxdn (DVT_NXDN=on)")
    if rig.sut_profile != specs.DEFAULT and not (specs.rule(rig, "nxdn") or {}).get("server"):
        pytest.skip("edge not ready: SPEC['nxdn']['server'] is not True in dvtest/specs/%s.py" % rig.sut_profile)
    tg = int(os.environ["DVT_NXDN_TG"])
    return [Server("ref", os.environ["DVT_NXDN_REF"], tg), Server("sut", os.environ["DVT_NXDN_SUT"], tg)]


def test_nxdn_server_poll_echo(servers):
    """P-NXD-31: a poll for the server's TG gets the identical 17 bytes back."""
    def run(srv):
        c = UdpClient("0.0.0.0")
        try:
            poll = nxdn.poll_packet("N0CALL", srv.tg)
            c.send((srv.addr, nxdn.PORT), poll)
            c.pump(1.0)
            replies = [d for d in c.inbox]
            return {"echo": poll in replies, "replies": len(replies)}
        finally:
            c.close()

    got = both(servers, run)
    for name in ("ref", "sut"):
        assert got[name]["echo"] and got[name]["replies"] == 1, (name, got[name])


def test_nxdn_server_poll_other_tg(servers, rig):
    """P-NXD-32: a poll for a TG the server does not carry (dev: REF stays
    silent, URFd accepts any TG)."""
    def run(srv):
        c = UdpClient("0.0.0.0")
        try:
            poll = nxdn.poll_packet("N0CALL", srv.tg + 1)
            c.send((srv.addr, nxdn.PORT), poll)
            c.pump(1.0)
            return {"echo": poll in c.inbox}
        finally:
            c.close()

    judge_dev(rig, "P-NXD-32", both(servers, run), "echo")


def test_nxdn_server_relay(servers):
    """P-NXD-33: a linked gateway's call reaches every other gateway on the
    TG: header first, all frames unchanged, trailer last, group, dst = TG;
    nothing goes back to the talker (P-NXD-36)."""
    sent = frames49()

    def run(srv):
        a, b, c = gw(srv, 0), gw(srv, 1), gw(srv, 2)
        try:
            send(a, nxdn.stream_packets(sent, IDS[0], srv.tg), b, c)
            out = {}
            for name, client in (("b", b), ("c", c)):
                pk = nxdnd(client)
                frames = [f for p in pk if p["kind"] == "voice" for f in nxdn.voice_frames(p["payload"])]
                out[name] = {"frames_exact": frames == sent, "first": pk[0]["kind"] if pk else None,
                             "last": pk[-1]["kind"] if pk else None,
                             "dst_tg": all(p["dst"] == srv.tg and p["group"] for p in pk), "src": sorted({p["src"] for p in pk})}
            return {"b": out["b"], "c": out["c"], "echo": len(nxdnd(a))}
        finally:
            close(a, b, c)

    got = both(servers, run)
    for name in ("ref", "sut"):
        for who in ("b", "c"):
            row = got[name][who]
            assert row["frames_exact"] and row["first"] == "header" and row["last"] == "trailer", (name, who, row)
            assert row["dst_tg"] and row["src"] == [IDS[0]], (name, who, row)
        assert got[name]["echo"] == 0, (name, got[name])


def test_nxdn_server_unlink(servers):
    """P-NXD-34: NXDNU removes the gateway at once: no reply, no further
    traffic to it."""
    def run(srv):
        a, b = gw(srv, 0), gw(srv, 1)
        try:
            b.client.inbox.clear()
            b.client.send(b.dest, nxdn.unlink_packet(CALLS[1], srv.tg))
            b.client.pump(0.5)
            reply = len(b.client.inbox)
            b.client.inbox.clear()
            send(a, nxdn.stream_packets(frames49(8, "unlink"), IDS[0], srv.tg), b)
            return {"reply": reply, "after_unlink": len(nxdnd(b))}
        finally:
            close(a)
            b.close()

    got = both(servers, run)
    conv(got, "P-NXD-34", reply=0, after_unlink=0)


def test_nxdn_server_unknown_source(servers, rig):
    """P-NXD-35: NXDND from an address that never polled is not relayed."""
    def run(srv):
        b = gw(srv, 1)
        stranger = UdpClient("0.0.0.0")
        try:
            packets = nxdn.stream_packets(frames49(8, "stranger"), IDS[2], srv.tg)
            start = time.monotonic()
            for index, data in enumerate(packets):
                pace(start, index, nxdn.PACKET_GAP)
                stranger.send((srv.addr, nxdn.PORT), data)
            b.client.pump(SETTLE)
            return {"relayed": len(nxdnd(b))}
        finally:
            stranger.close()
            close(b)

    mark(rig, "begin", "nxdn-unknown-source")
    try:
        got = both(servers, run)
    finally:
        mark(rig, "end", "nxdn-unknown-source")
    for name in ("ref", "sut"):
        assert got[name]["relayed"] == 0, (name, got[name])


@pytest.mark.parametrize("what", ["private", "other_tg"])
def test_nxdn_server_private_or_other_tg(servers, rig, what):
    """P-NXD-37: an individual call, or a group call to another TG, from a
    linked gateway is not relayed (REF; URFd does not check)."""
    def run(srv):
        a, b = gw(srv, 0), gw(srv, 1)
        try:
            if what == "private":
                packets = nxdn.stream_packets(frames49(8, what), IDS[0], srv.tg, group=False)
            else:
                packets = nxdn.stream_packets(frames49(8, what), IDS[0], srv.tg + 1)
            send(a, packets, b)
            return {"relayed": len(nxdnd(b)) > 0}
        finally:
            close(a, b)

    mark(rig, "begin", "nxdn-" + what)
    try:
        got = both(servers, run)
    finally:
        mark(rig, "end", "nxdn-" + what)
    conv(got, "P-NXD-37", relayed=False)


def test_nxdn_server_two_talkers(servers, rig):
    """P-NXD-38: two gateways talk at once: one winner, no mixing (spec B1;
    REF relays both)."""
    a_frames, b_frames = frames49(seed="two-a"), frames49(seed="two-b")

    def run(srv):
        a, b, c = gw(srv, 0), gw(srv, 1), gw(srv, 2)
        try:
            pa = nxdn.stream_packets(a_frames, IDS[0], srv.tg)
            pb = nxdn.stream_packets(b_frames, IDS[1], srv.tg)
            start = time.monotonic()
            for tick in range(len(pa) + 4):
                pace(start, tick, nxdn.PACKET_GAP)
                if tick < len(pa):
                    a.client.send(a.dest, pa[tick])
                if 4 <= tick < 4 + len(pb):
                    b.client.send(b.dest, pb[tick - 4])
                c.client.drain()
            c.client.pump(SETTLE)
            srcs = [p["src"] for p in nxdnd(c) if p["kind"] == "voice"]
            return {"from_a": srcs.count(IDS[0]), "from_b": srcs.count(IDS[1]),
                    "mixed": IDS[0] in srcs and IDS[1] in srcs}
        finally:
            close(a, b, c)

    mark(rig, "begin", "nxdn-two-talkers")
    try:
        got = both(servers, run)
    finally:
        mark(rig, "end", "nxdn-two-talkers")
    judge_dev(rig, "P-NXD-38", got, "mixed")


def test_nxdn_server_lost_trailer(servers, rig):
    """P-NXD-39: a call without a trailer: do listeners get a trailer, and
    is the next call relayed (spec B3)?"""
    def run(srv):
        a, b, c = gw(srv, 0), gw(srv, 1), gw(srv, 2)
        try:
            send(a, nxdn.stream_packets(frames49(12, "lost"), IDS[0], srv.tg, trailer=False), c, wait=2.5)
            trailer = any(p["kind"] == "trailer" for p in nxdnd(c))
            c.client.inbox.clear()
            send(b, nxdn.stream_packets(frames49(12, "next"), IDS[1], srv.tg), c)
            return {"trailer": trailer, "next_relayed": any(p["kind"] == "voice" for p in nxdnd(c))}
        finally:
            close(a, b, c)

    mark(rig, "begin", "nxdn-lost-trailer")
    try:
        got = both(servers, run)
    finally:
        mark(rig, "end", "nxdn-lost-trailer")
    for name in ("ref", "sut"):
        assert got[name]["next_relayed"], (name, got[name])
    judge_dev(rig, "P-NXD-39", got, "trailer")


def test_nxdn_server_garbage_ignored(servers, rig):
    """P-NXD-40: non-NXDN, short, long and unknown-tag packets from a linked
    gateway get no reply; the gateway stays linked."""
    def run(srv):
        a, b = gw(srv, 0), gw(srv, 1)
        try:
            for junk in (b"XXXX" + bytes(6), b"NXDND" + bytes(37), b"NXDND" + bytes(39), b"NXDNQ" + bytes(12)):
                a.client.send(a.dest, junk)
            a.client.pump(0.8)
            replies = len(a.client.inbox)
            send(a, nxdn.stream_packets(frames49(8, "after-junk"), IDS[0], srv.tg), b)
            return {"replies": replies, "still_relayed": any(p["kind"] == "voice" for p in nxdnd(b))}
        finally:
            close(a, b)

    mark(rig, "begin", "nxdn-garbage")
    try:
        got = both(servers, run)
    finally:
        mark(rig, "end", "nxdn-garbage")
    conv(got, "P-NXD-40", replies=0, still_relayed=True)


def test_nxdn_server_no_unsolicited(servers):
    """P-NXD-41: the server sends no polls of its own and does not relay one
    gateway's polls to another."""
    def run(srv):
        a, b = gw(srv, 0), gw(srv, 1)
        try:
            end = time.monotonic() + 11
            while time.monotonic() < end:
                b.keepalive()
                a.client.pump(1.0)
            return {"to_a": len(a.client.inbox)}
        finally:
            close(a, b)

    got = both(servers, run)
    conv(got, "P-NXD-41", to_a=0)


def test_nxdn_server_data_frames(servers, rig):
    """P-NXD-42: data frames (flags 0x03) from a gateway: relayed to the
    other gateways unchanged or dropped (dev, recorded)."""
    def run(srv):
        a, b = gw(srv, 0), gw(srv, 1)
        try:
            send(a, [nxdn.packet(IDS[0], srv.tg, nxdn.data_payload(bytes([i]) * 8)) for i in range(6)], b)
            return {"relayed": sum(p["kind"] == "data" for p in nxdnd(b))}
        finally:
            close(a, b)

    mark(rig, "begin", "nxdn-data")
    try:
        got = both(servers, run)
    finally:
        mark(rig, "end", "nxdn-data")
    judge_dev(rig, "P-NXD-42", got, "relayed")


@pytest.mark.slow
def test_nxdn_server_client_expiry(servers, rig):
    """P-NXD-43: a gateway that stops polling still gets traffic 35 s
    later? 130 s later? (REF expires after 120 s, URFd after 30 s)."""
    def run(srv):
        a, b = gw(srv, 0), gw(srv, 1)
        try:
            start = time.monotonic()
            out = {}
            for label, at in (("after_35s", 35), ("after_130s", 130)):
                while time.monotonic() - start < at:
                    b.keepalive()
                    a.client.pump(min(5.0, max(0.0, at - (time.monotonic() - start))))
                a.client.inbox.clear()
                send(b, nxdn.stream_packets(frames49(8, label), IDS[1], srv.tg), a)
                out[label] = any(p["kind"] == "voice" for p in nxdnd(a))
            return out
        finally:
            close(a, b)

    judge_dev(rig, "P-NXD-43", both(servers, run), "after_35s", "after_130s")

