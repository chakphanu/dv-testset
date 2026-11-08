# SPDX-License-Identifier: MIT
"""The `firsts` evidence of the ICE1 cases (ICE1c): after `readdress on` the NAT router's packet sniffer records, per direction, the
first datagram of the SUT's media port and its kind, and the SUT's datagrams still sent to the old address per second. Frame and
payload parsers, the Firsts collector, the router words against a fake sniffer, and the AF_PACKET reader over a socket pair."""
import socket
import struct
import time

import pytest

from dvtest import nat
from dvtest.tools import nat_router

pytestmark = pytest.mark.unit

TOPO = nat.make_topology("172.31.0.0/24", "172.30.0.92", "172.31.0.2", "172.30.0.93", "172.31.0.91", "172.30.0.11",
                         out_if="eth0", in_if="eth1")
OLD_IP, NEW_IP, SUT_IP = "172.30.0.92", "172.30.0.93", "172.30.0.11"
COOKIE = 0x2112A442


# ---------------------------------------------------------------- frames

def stun(mtype, extra=b""):
    return struct.pack("!HHI12s", mtype, len(extra), COOKIE, b"t" * 12) + extra


def rtp(pt=0):
    return bytes([0x80, pt]) + b"\x00" * 10 + b"\x00" * 160


def eth_udp(src, dst, sport, dport, payload, vlan=False, frag=0, proto=17, ihl_words=5, cut=None):
    ip_len = ihl_words * 4 + 8 + len(payload)
    ip = struct.pack("!BBHHHBBH4s4s", 0x40 | ihl_words, 0, ip_len, 1, frag, 64, proto, 0,
                     bytes(int(x) for x in src.split(".")), bytes(int(x) for x in dst.split("."))) + b"\x00" * ((ihl_words - 5) * 4)
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0)
    eth = b"\x02" * 6 + b"\x04" * 6 + (struct.pack("!HH", 0x8100, 5) if vlan else b"") + struct.pack("!H", 0x0800)
    frame = eth + ip + udp + payload
    return frame if cut is None else frame[:cut]


@pytest.mark.parametrize("payload,kind", [
    (stun(0x0001), "stun-binding-request"), (stun(0x0101), "stun-binding-response"), (stun(0x0111), "stun-binding-error"),
    (stun(0x0011), "stun-binding-indication"), (stun(0x0003), "stun-other"), (stun(0x0113), "stun-other"),
    (rtp(0), "rtp"), (rtp(111), "rtp"), (rtp(200), "rtcp"), (rtp(201), "rtcp"), (rtp(223), "rtcp"),
    (b"\x16\xfe\xfd" + b"\x00" * 30, "dtls"), (b"\x14" + b"\x00" * 20, "dtls"), (b"\x00" * 30, "other"),
    (b"\x00\x01\x00\x00" + b"\x00" * 30, "other"), (b"", "other"), (b"\x80", "other"),
])
def test_classify_payload(payload, kind):
    assert nat.classify_payload(payload) == kind


def test_the_cookie_must_be_in_bytes_four_to_seven():
    wrong = struct.pack("!HHI12s", 0x0001, 0, 0x2112A443, b"t" * 12)
    assert nat.classify_payload(wrong) == "other"
    assert nat.classify_payload(stun(0x0001)[:19]) == "other"               # shorter than the 20 byte STUN header


def test_parse_frame_reads_addresses_ports_and_kind():
    dg = nat.parse_frame(eth_udp(NEW_IP, SUT_IP, 41000, 50000, stun(0x0001)))
    assert dg == {"src": NEW_IP, "dst": SUT_IP, "sport": 41000, "dport": 50000, "size": 20, "kind": "stun-binding-request"}
    assert nat.parse_frame(eth_udp(SUT_IP, NEW_IP, 50000, 41000, rtp()))["kind"] == "rtp"


def test_parse_frame_handles_vlan_ip_options_and_cut_frames():
    assert nat.parse_frame(eth_udp("10.0.0.1", "10.0.0.2", 1, 2, stun(0x0001), vlan=True))["kind"] == "stun-binding-request"
    assert nat.parse_frame(eth_udp("10.0.0.1", "10.0.0.2", 1, 2, stun(0x0001), ihl_words=8))["sport"] == 1
    # the sniffer keeps 128 bytes: a long RTP packet is cut, the kind survives
    long_rtp = eth_udp("10.0.0.1", "10.0.0.2", 1, 2, rtp(0) + b"\x00" * 1000, cut=nat_router.SNAP_BYTES)
    assert len(long_rtp) == 128 and nat.parse_frame(long_rtp)["kind"] == "rtp"


def test_parse_frame_skips_everything_that_is_not_a_first_udp_datagram():
    assert nat.parse_frame(b"") is None
    assert nat.parse_frame(eth_udp("10.0.0.1", "10.0.0.2", 1, 2, b"x" * 20, proto=6)) is None             # tcp
    assert nat.parse_frame(eth_udp("10.0.0.1", "10.0.0.2", 1, 2, b"x" * 20, frag=0x00B9)) is None        # not the first fragment
    assert nat.parse_frame(eth_udp("10.0.0.1", "10.0.0.2", 1, 2, b"x" * 20, cut=30)) is None             # cut inside the headers
    arp = b"\x02" * 12 + struct.pack("!H", 0x0806) + b"\x00" * 40
    assert nat.parse_frame(arp) is None
    assert nat.parse_frame(b"\x02" * 12 + struct.pack("!H", 0x0800) + b"\x60" + b"\x00" * 50) is None     # ip version 6 byte


# ---------------------------------------------------------------- the collector

def firsts_at(t0=100.0):
    return nat.Firsts(t0, OLD_IP, NEW_IP, SUT_IP, 50000)


def dg(src, dst, sport, dport, kind):
    return {"src": src, "dst": dst, "sport": sport, "dport": dport, "size": 0, "kind": kind}


def test_firsts_sorts_datagrams_into_the_four_directions():
    f = firsts_at()
    f.feed(100.4, dg(NEW_IP, SUT_IP, 41000, 50000, "stun-binding-request"))
    f.feed(100.5, dg(NEW_IP, SUT_IP, 41000, 50000, "stun-binding-request"))
    f.feed(100.9, dg(SUT_IP, NEW_IP, 50000, 41000, "stun-binding-response"))
    f.feed(101.2, dg(SUT_IP, NEW_IP, 50000, 41000, "rtp"))
    f.feed(100.05, dg(SUT_IP, OLD_IP, 50000, 41000, "rtp"))
    f.feed(100.07, dg(SUT_IP, OLD_IP, 50000, 41000, "rtp"))
    f.feed(102.5, dg(SUT_IP, OLD_IP, 50000, 41000, "rtcp"))
    f.feed(130.0, dg(SUT_IP, OLD_IP, 50000, 41000, "rtp"))                     # past the horizon
    f.feed(100.1, dg(OLD_IP, SUT_IP, 41000, 50000, "stun-binding-request"))
    f.feed(100.1, dg("172.30.0.94", SUT_IP, 3478, 50000, "other"))              # another address: not one of ours
    f.feed(100.1, dg(NEW_IP, "172.30.0.95", 41000, 3479, "other"))              # another destination
    f.feed(100.1, dg(NEW_IP, SUT_IP, 41000, 50001, "other"))                    # another port
    f.feed(99.9, dg(NEW_IP, SUT_IP, 41000, 50000, "rtp"))                       # before the change
    s = f.snapshot()["sides"]
    assert s["from_new"]["n"] == 2 and s["from_new"]["first_s"] == 0.4 and s["from_new"]["first_kind"] == "stun-binding-request"
    assert s["from_new"]["first_by_kind"] == {"stun-binding-request": 0.4} and s["from_new"]["kinds"] == {"stun-binding-request": 2}
    assert s["to_new"]["first_s"] == 0.9 and s["to_new"]["first_kind"] == "stun-binding-response"
    assert s["to_new"]["first_by_kind"] == {"stun-binding-response": 0.9, "rtp": 1.2}
    old = s["to_old"]
    assert old["n"] == 4 and old["first_s"] == 0.05 and old["last_s"] == 30.0 and old["after_horizon"] == 1
    assert old["per_s"][0] == 2 and old["per_s"][2] == 1 and sum(old["per_s"]) == 3 and len(old["per_s"]) == 20
    assert s["from_old"]["n"] == 1


def test_firsts_with_nothing_seen_and_a_stable_snapshot():
    f = firsts_at()
    snap = f.snapshot()
    assert snap["old"] == OLD_IP and snap["new"] == NEW_IP and snap["error"] is None and snap["horizon_s"] == 20
    assert all(v["n"] == 0 and v["first_s"] is None for v in snap["sides"].values())
    f.feed(100.5, dg(NEW_IP, SUT_IP, 1, 50000, "rtp"))
    assert snap["sides"]["from_new"]["n"] == 0                                   # a snapshot is a copy


def test_firsts_feed_frame_parses_and_ignores_garbage():
    f = firsts_at()
    f.feed_frame(100.3, eth_udp(NEW_IP, SUT_IP, 41000, 50000, stun(0x0001)))
    f.feed_frame(100.3, b"garbage")
    assert f.snapshot()["sides"]["from_new"]["first_by_kind"] == {"stun-binding-request": 0.3}


# ---------------------------------------------------------------- the router words

class Recorder:
    """A runner that accepts everything (and fails the commands `fail` says)."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, argv):
        self.calls.append(argv)
        if self.fail and self.fail(argv):
            return 1, "iptables: boom"
        if argv[:2] == ["conntrack", "-D"]:
            return 0, "conntrack v1.4.7: 0 flow entries have been deleted.\n"
        if argv[3:5] == ["-C", "DVT_IN"] or argv[3:4] == ["-C"]:
            return 1, ""
        return 0, ""


class FakeSniffer:
    made = []

    def __init__(self, ifname, on_frame, duration_s):
        self.ifname, self.on_frame, self.duration_s = ifname, on_frame, duration_s
        self.started = self.stopped = False
        FakeSniffer.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def sniffing_router(mode="port_restricted"):
    FakeSniffer.made = []
    clock = Clock()
    r = nat.Router(TOPO, Recorder(), clock=clock, sniffer=FakeSniffer)
    r.handle({"op": "nat_mode", "mode": mode})
    return r, clock


def test_parse_request_knows_firsts_and_refuses_arguments():
    assert nat.parse_request('{"op":"firsts"}')["op"] == "firsts" and "firsts" in nat.OPS
    with pytest.raises(nat.NatError):
        nat.parse_request('{"op":"firsts","state":"on"}')


def test_readdress_starts_the_sniffer_and_firsts_reads_it():
    r, clock = sniffing_router()
    assert r.handle({"op": "firsts"}) == {"ok": True, "firsts": None, "readdressed": False}
    ans = r.handle({"op": "readdress", "state": "on"})
    assert ans["sniffer"] == "ok" and ans["t0"] == 100.0
    sniff = FakeSniffer.made[0]
    assert sniff.started and sniff.ifname == "eth0" and sniff.duration_s == nat.Firsts.HORIZON_S + 2.0
    clock.now = 100.4
    sniff.on_frame(eth_udp(NEW_IP, SUT_IP, 41000, 50000, stun(0x0001)))
    clock.now = 100.9
    sniff.on_frame(eth_udp(SUT_IP, NEW_IP, 50000, 41000, rtp()))
    clock.now = 101.0
    sniff.on_frame(eth_udp(SUT_IP, OLD_IP, 50000, 41000, rtp()))
    got = r.handle({"op": "firsts"})
    assert got["ok"] and got["readdressed"] is True
    sides = got["firsts"]["sides"]
    assert sides["from_new"]["first_s"] == 0.4 and sides["to_new"]["first_kind"] == "rtp" and sides["to_old"]["per_s"][1] == 1


def test_the_sniffer_runs_before_the_rules_change():
    calls = []

    class Order(FakeSniffer):
        def start(self):
            calls.append("sniffer")

    def run(argv):
        calls.append(" ".join(argv[:5]))
        return (1, "") if argv[3:4] == ["-C"] else (0, "")

    r = nat.Router(TOPO, run, sniffer=Order)
    r.handle({"op": "nat_mode", "mode": "port_restricted"})
    calls.clear()
    r.handle({"op": "readdress", "state": "on"})
    assert calls[0] == "sniffer"
    assert calls.index("sniffer") < next(i for i, c in enumerate(calls) if "SNAT" in c or "-A DVT_MODE_POST" in c)


def test_off_stops_the_sniffer_and_the_snapshot_stays_until_the_next_move():
    r, clock = sniffing_router("symmetric")
    r.handle({"op": "readdress", "state": "on"})
    sniff = FakeSniffer.made[0]
    clock.now = 100.5
    sniff.on_frame(eth_udp(NEW_IP, SUT_IP, 41000, 50000, stun(0x0001)))
    r.handle({"op": "readdress", "state": "off"})
    assert sniff.stopped
    got = r.handle({"op": "firsts"})
    assert got["readdressed"] is False and got["firsts"]["sides"]["from_new"]["first_s"] == 0.5
    clock.now = 200.0
    r.handle({"op": "readdress", "state": "on"})
    assert r.handle({"op": "firsts"})["firsts"]["sides"]["from_new"]["n"] == 0 and len(FakeSniffer.made) == 2


def test_full_cone_names_the_alias_as_the_old_address():
    r, _clock = sniffing_router("full_cone")
    r.handle({"op": "readdress", "state": "on"})
    snap = r.handle({"op": "firsts"})["firsts"]
    assert (snap["old"], snap["new"]) == (NEW_IP, OLD_IP)


def test_a_sniffer_that_cannot_start_does_not_stop_the_move():
    def broken(ifname, on_frame, duration_s):
        raise PermissionError("Operation not permitted")
    r = nat.Router(TOPO, Recorder(), sniffer=broken)
    r.handle({"op": "nat_mode", "mode": "port_restricted"})
    ans = r.handle({"op": "readdress", "state": "on"})
    assert ans["ok"] and ans["readdressed"] is True and ans["sniffer"].startswith("PermissionError")
    assert r.handle({"op": "firsts"})["firsts"]["error"].startswith("PermissionError")
    assert r.handle({"op": "readdress", "state": "off"})["ok"]


def test_reset_and_a_new_mode_stop_the_sniffer():
    r, _clock = sniffing_router()
    r.handle({"op": "readdress", "state": "on"})
    r.handle({"op": "reset"})
    assert FakeSniffer.made[0].stopped
    r.handle({"op": "readdress", "state": "on"})
    r.handle({"op": "nat_mode", "mode": "symmetric"})
    assert FakeSniffer.made[1].stopped


def test_a_failing_move_stops_the_sniffer():
    FakeSniffer.made = []
    rec = Recorder(fail=lambda argv: argv[:4] == ["iptables", "-t", "nat", "-A"] and "SNAT" in argv)
    r = nat.Router(TOPO, rec, sniffer=FakeSniffer)
    r.handle({"op": "nat_mode", "mode": "port_restricted"})
    assert r.handle({"op": "readdress", "state": "on"})["ok"] is False
    assert FakeSniffer.made[0].stopped and not r.readdressed


def test_client_firsts_returns_the_snapshot(monkeypatch):
    monkeypatch.setattr(nat, "call", lambda addr, request, timeout=10.0: {"ok": True, "firsts": {"old": "a"}, "readdressed": True})
    client = nat.NatClient(("127.0.0.1", 9100), ("127.0.0.1", 3001), "127.0.0.3", "127.0.0.4")
    assert client.firsts() == {"old": "a"}


# ---------------------------------------------------------------- the AF_PACKET reader (a socket pair stands in)

def test_packet_sniffer_hands_frames_to_the_callback_and_stops():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    got = []
    sniff = nat_router.PacketSniffer("eth0", got.append, 5.0, open_socket=lambda ifname: a)
    sniff.start()
    frame = eth_udp(NEW_IP, SUT_IP, 41000, 50000, stun(0x0001))
    b.send(frame)
    b.send(frame + b"\x00" * 400)
    end = time.monotonic() + 3
    while len(got) < 2 and time.monotonic() < end:
        time.sleep(0.02)
    thread = sniff.thread
    sniff.stop()
    b.close()
    assert got[0] == frame and len(got[1]) == nat_router.SNAP_BYTES            # cut to the snap length
    assert sniff.sock is None and not thread.is_alive()


def test_packet_sniffer_survives_a_failing_callback_and_reports_a_missing_capability():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    seen = []

    def on_frame(frame):
        seen.append(frame)
        if len(seen) == 1:
            raise ValueError("bad frame")

    sniff = nat_router.PacketSniffer("eth0", on_frame, 5.0, open_socket=lambda ifname: a)
    sniff.start()
    b.send(b"one")
    b.send(b"two")
    end = time.monotonic() + 3
    while len(seen) < 2 and time.monotonic() < end:
        time.sleep(0.02)
    sniff.stop()
    b.close()
    assert seen == [b"one", b"two"]

    def denied(ifname):
        raise PermissionError("Operation not permitted")
    with pytest.raises(PermissionError):
        nat_router.PacketSniffer("eth0", seen.append, 1.0, open_socket=denied).start()


class FakePacketSocket:
    def __init__(self, calls, args):
        self.calls = calls
        calls.append(("socket",) + args)

    def setsockopt(self, *a):
        self.calls.append(("setsockopt",) + a)

    def bind(self, addr):
        self.calls.append(("bind", addr))

    def close(self):
        self.calls.append(("close",))


class FakeSocketModule:
    """Just enough of the socket module for PacketSniffer._open_packet_socket; the real constants, a recording socket."""
    AF_PACKET, SOCK_RAW, SOL_SOCKET, SO_RCVBUF = socket.AF_PACKET, socket.SOCK_RAW, socket.SOL_SOCKET, socket.SO_RCVBUF
    htons = staticmethod(socket.htons)

    def __init__(self):
        self.calls = []

    def socket(self, *args):
        return FakePacketSocket(self.calls, args)


def test_the_packet_socket_listens_for_every_protocol_so_that_outgoing_frames_arrive(monkeypatch):
    """ICE1c, first run of `firsts`: a packet socket bound to ETH_P_IP got the SUT's datagrams (incoming) and none of the browser's,
    which leave the router's outside interface after SNAT (outgoing). The kernel gives sent frames only to ETH_P_ALL sockets."""
    fake = FakeSocketModule()
    monkeypatch.setattr(nat_router, "socket", fake)
    nat_router.PacketSniffer._open_packet_socket("eth0")
    assert nat_router.ETH_P_ALL == 0x0003 and not hasattr(nat_router, "ETH_P_IP")
    assert fake.calls[0] == ("socket", socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    assert ("bind", ("eth0", 0x0003)) in fake.calls


def test_a_bind_failure_closes_the_packet_socket(monkeypatch):
    fake = FakeSocketModule()

    def failing_bind(self, addr):
        raise OSError("no such device")
    monkeypatch.setattr(FakePacketSocket, "bind", failing_bind)
    monkeypatch.setattr(nat_router, "socket", fake)
    with pytest.raises(OSError):
        nat_router.PacketSniffer._open_packet_socket("nope0")
    assert ("close",) in fake.calls


@pytest.mark.parametrize("mode,old,new,sport", [("port_restricted", OLD_IP, NEW_IP, 41000), ("symmetric", OLD_IP, NEW_IP, 28506),
                                                ("full_cone", NEW_IP, OLD_IP, 41000)])
def test_the_browsers_datagrams_leaving_the_router_after_nat_are_the_new_mapping(mode, old, new, sport):
    """The outgoing direction: browser -> SUT as it is on the router's outside interface after SNAT (source = the new outside
    address, in the symmetric NAT with a port the browser never used), a STUN binding request first and RTCP later. These
    are the frames the packet socket delivers as PACKET_OUTGOING; the figures DV-Router asked for come from them."""
    from dvtest import ice1
    r, clock = sniffing_router(mode)
    r.handle({"op": "readdress", "state": "on"})
    sniff = FakeSniffer.made[0]
    clock.now = 100.31
    sniff.on_frame(eth_udp(new, SUT_IP, sport, 50000, stun(0x0001)))
    clock.now = 100.51
    sniff.on_frame(eth_udp(new, SUT_IP, sport, 50000, stun(0x0001)))
    clock.now = 100.91
    sniff.on_frame(eth_udp(SUT_IP, new, 50000, sport, stun(0x0101)))
    clock.now = 101.4
    sniff.on_frame(eth_udp(new, SUT_IP, sport, 50000, bytes([0x80, 201]) + b"\x00" * 30))
    ev = ice1.firsts_evidence(r.handle({"op": "firsts"})["firsts"])
    assert ev["first_stun_new_s"] == 0.31 and ev["first_udp_new_s"] == 0.31 and ev["first_udp_new_kind"] == "stun-binding-request"
    assert ev["first_sut_to_new_s"] == 0.91 and ev["first_sut_stun_response_new_s"] == 0.91
    assert ev["browser_to_sut_new_kinds"] == {"stun-binding-request": 2, "rtcp": 1}
    assert ev["browser_from_old_total"] == 0 and ev["stale_to_old_total"] == 0


def test_the_sniffer_stops_by_itself_after_its_duration():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    sniff = nat_router.PacketSniffer("eth0", lambda frame: None, 0.3, open_socket=lambda ifname: a)
    sniff.start()
    sniff.thread.join(3)
    assert not sniff.thread.is_alive()
    sniff.stop()
    b.close()
