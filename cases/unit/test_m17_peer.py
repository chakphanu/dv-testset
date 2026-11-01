# SPDX-License-Identifier: MIT
"""dvtest.m17_peer on 127.0.0.1 against a tiny in-test reflector (ephemeral
ports, timers scaled down)."""
import socket
import threading
import time

import pytest

from dvtest import m17_peer
from dvtest.proto import m17

pytestmark = pytest.mark.unit

SCALE = dict(conn_interval=0.2, ping_interval=0.1)


class Reflector:
    """mrefd-like: sends a 37 B CONN to the hub every conn_interval s until it
    gets an ACKN, answers a CONN with an ACKN, PINGs every ping_interval s
    once linked, never answers a PING."""

    def __init__(self, hub_port, designation="M17-REF", modules="M", connect=True, linked=False,
                 link_on_conn=False, conn_interval=0.15, ping_interval=0.1):
        self.hub = ("127.0.0.1", hub_port)
        self.designation, self.modules = designation, modules
        self.connect, self.linked, self.link_on_conn = connect, linked, link_on_conn
        self.conn_interval, self.ping_interval = conn_interval, ping_interval
        self.log = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.02)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        next_conn = next_ping = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if self.connect and not self.linked and now >= next_conn:
                self.sock.sendto(m17.peer_conn_packet(self.designation, self.modules), self.hub)
                next_conn = now + self.conn_interval
            if self.linked and now >= next_ping:
                self.sock.sendto(m17.peer_ping_packet(self.designation), self.hub)
                next_ping = now + self.ping_interval
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            self.log.append((time.monotonic(), data))
            kind = m17.classify(data)
            if kind == "CONN37":
                self.sock.sendto(m17.peer_ackn_packet(self.designation, self.modules), self.hub)
                self.linked = self.linked or self.link_on_conn
            elif kind == "ACKN37" and m17.parse_peer(data)["designation"].startswith("M17-"):
                self.linked = True
            elif kind == "DISC10":
                self.linked = False

    def kinds(self, kind):
        return [(t, d) for t, d in self.log if m17.classify(d) == kind]

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.sock.close()


@pytest.fixture
def made():
    items = []
    yield items
    for item in reversed(items):
        item.close()


def hub_for(made, **kw):
    hub = m17_peer.PeerHub("M17-DVT", "M", port=0, bind_ip="127.0.0.1", **kw)
    made.append(hub)
    return hub


def reflector_for(made, hub, **kw):
    refl = Reflector(hub.port, **kw)
    made.append(refl)
    return refl


def test_link_handshake_and_ping_cadence(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub)
    link = hub.peer("127.0.0.1", refl.port, **SCALE)
    took = link.link(timeout=5)
    assert took < 3 and link.linked and link.linked_at is not None
    assert link.remote_designation == "M17-REF" and link.remote_modules == "M"
    # the reflector's CONN got a 37 B ACKN with our designation and modules
    acks = refl.kinds("ACKN37")
    assert acks and acks[0][1] == m17.peer_ackn_packet("M17-DVT", "M")
    # PING every ping_interval, 10 B with our designation
    time.sleep(0.8)
    pings = refl.kinds("PING10")
    assert len(pings) >= 5 and all(d == m17.peer_ping_packet("M17-DVT") for _, d in pings)
    gaps = [b[0] - a[0] for a, b in zip(pings, pings[1:])]
    assert all(0.06 < gap < 0.25 for gap in gaps), gaps
    # the reflector's PINGs are never answered: we sent no PONG
    assert link.datagrams(kind="PING") and not link.sent(kind="PONG")
    assert [rx.kind for rx in link.sent()].count("PING10") == len(pings)


def test_ping_magic_pong_and_rate_are_ours(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub, ping_interval=0.02)           # 50 PING/s must not raise ours
    link = hub.peer("127.0.0.1", refl.port, ping_magic="PONG", **SCALE)
    link.link(timeout=5)
    time.sleep(0.5)
    assert len(refl.kinds("PONG10")) in range(3, 8) and not refl.kinds("PING10")


def test_already_linked_reflector_is_recognised(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub, connect=False, linked=True)
    link = hub.peer("127.0.0.1", refl.port, auto_conn=False, **SCALE)
    assert link.link(timeout=5) < 1
    assert not link.sent(kind="CONN")


def test_auto_conn_to_passive_reflector(made):
    """The reflector never CONNs; our CONN alone links it (link_on_conn) and
    it then PINGs."""
    hub = hub_for(made)
    refl = reflector_for(made, hub, connect=False, link_on_conn=True)
    link = hub.peer("127.0.0.1", refl.port, **SCALE)
    assert not link.linked
    deadline = time.monotonic() + 3
    while not refl.kinds("CONN37") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert refl.kinds("CONN37")[0][1] == m17.peer_conn_packet("M17-DVT", "M")
    assert link.wait_for(kind="ACKN37", timeout=2)
    link.link(timeout=3)
    sent_conns = len(link.sent(kind="CONN"))
    time.sleep(0.5)
    assert len(link.sent(kind="CONN")) == sent_conns                 # no more CONN once linked


def test_conn_retry_interval_until_linked(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub, connect=False)
    refl.linked = False
    link = hub.peer("127.0.0.1", refl.port, conn_interval=0.1, ping_interval=0.1, silent=False)
    time.sleep(0.75)
    conns = refl.kinds("CONN37")
    assert 5 <= len(conns) <= 9
    link.auto_conn = False
    count = len(refl.kinds("CONN37"))
    time.sleep(0.4)
    assert len(refl.kinds("CONN37")) <= count + 1


def test_drop_sends_disc_and_relinks(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub)
    link = hub.peer("127.0.0.1", refl.port, **SCALE)
    link.link(timeout=5)
    refl.connect = False                    # its CONN retries would be answered and relink us
    link.drop()
    assert not link.linked
    deadline = time.monotonic() + 2
    while not refl.kinds("DISC10") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert refl.kinds("DISC10")[0][1] == m17.peer_disc_packet("M17-DVT")
    # stale PINGs in flight do not bring the link back; our timers are off
    pings_after = len(refl.kinds("PING10"))
    time.sleep(0.5)
    assert not link.linked and len(refl.kinds("PING10")) in (pings_after, pings_after + 1)
    # link() resumes: CONN again, ACKN, new PINGs
    refl.connect = True
    assert link.link(timeout=5) < 4 and link.linked


def test_silent_answers_nothing_and_timeout_report(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub)
    link = hub.peer("127.0.0.1", refl.port, silent=True, **SCALE)
    assert link.link(timeout=0.6, raise_on_timeout=False) is False
    assert link.datagrams(kind="CONN") and not link.sent()
    assert not refl.kinds("ACKN37") and not refl.kinds("PING10")
    with pytest.raises(m17_peer.PeerLinkTimeout) as err:
        link.link(timeout=0.3)
    text = str(err.value)
    assert "CONN37 x" in text and "we sent: nothing" in text and "M17-DVT" in text
    link.send(b"hello")                                              # send() still works when silent
    assert link.sent()[0].data == b"hello"
    link.silent = False
    link.link(timeout=5)


def test_two_remotes_on_one_socket(made):
    hub = hub_for(made)
    one = reflector_for(made, hub, designation="M17-REF")
    two = reflector_for(made, hub, designation="M17-SUT")
    link_one = hub.peer("127.0.0.1", one.port, **SCALE)
    link_two = hub.peer("127.0.0.1", two.port, **SCALE)
    assert link_one is not link_two and link_one.link(5) >= 0 and link_two.link(5) >= 0
    assert link_one.remote_designation == "M17-REF" and link_two.remote_designation == "M17-SUT"
    link_one.clear()
    link_two.clear()
    mark = link_one.mark()
    link_two.send(b"only-two")
    link_one.send(b"only-one")
    time.sleep(0.2)
    assert (b"only-two" in [d for _, d in two.log]) and (b"only-two" not in [d for _, d in one.log])
    assert (b"only-one" in [d for _, d in one.log]) and (b"only-one" not in [d for _, d in two.log])
    assert all(rx.t >= mark for rx in link_one.sent(since=mark))
    assert len(hub.links) == 2 and hub.error is None


def test_unregistered_source_gets_its_own_link(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub)
    assert refl.hub[1] == hub.port
    deadline = time.monotonic() + 3
    while ("127.0.0.1", refl.port) not in hub.links and time.monotonic() < deadline:
        time.sleep(0.02)
    link = hub.links[("127.0.0.1", refl.port)]
    link.link(timeout=5)
    assert link.dest == ("127.0.0.1", refl.port)


def test_wait_for_datagrams_and_drain(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub)
    link = hub.peer("127.0.0.1", refl.port, **SCALE)
    link.link(timeout=5)
    stream = m17.with_trailer(m17.stream_packet(9, 0, bytes(16)), "M")
    mark = link.mark()
    refl.sock.sendto(stream, refl.hub)
    hit = link.wait_for(kind="STREAM", timeout=2, since=mark)
    assert hit is not None and hit.size == 55 and hit.data == stream and hit.kind == "STREAM55"
    assert link.wait_for(lambda rx: rx.size == 99, timeout=0.2, since=mark) is None
    assert link.datagrams(since=mark, kind="STREAM") == [hit]
    assert link.datagrams(kind=("STREAM", "PING")) and not link.datagrams(kind="M17P")
    assert link.wait_until(lambda lk: lk.linked, 0.5)
    link.drain(0.1)
    assert link.recv_log == [] or all(rx.t > mark for rx in link.recv_log)


def test_reset_restores_modes(made):
    hub = hub_for(made)
    link = hub.peer("127.0.0.1", 9, **SCALE)
    link.silent = True
    link.auto_ackn = False
    link.modules = "Z"
    link.reset()
    assert (link.silent, link.auto_ackn, link.modules) == (False, True, "M")
    with pytest.raises(TypeError):
        hub.peer("127.0.0.1", 10, no_such_mode=1)


def test_close_leaves_no_threads_and_sends_disc(made):
    before = {t.name for t in threading.enumerate()}
    hub = m17_peer.PeerHub("M17-DVU", "NO", port=0, bind_ip="127.0.0.1")
    refl = Reflector(hub.port, designation="M17-REF", modules="NO")
    link = hub.peer("127.0.0.1", refl.port, **SCALE)
    link.link(timeout=5)
    assert any(t.name.startswith("m17-peer-") for t in threading.enumerate())
    hub.close()
    hub.close()                                                      # idempotent
    deadline = time.monotonic() + 2
    while not refl.kinds("DISC10") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert refl.kinds("DISC10")[0][1] == m17.peer_disc_packet("M17-DVU")
    refl.close()
    assert {t.name for t in threading.enumerate()} - before == set()
    with pytest.raises(OSError):
        hub.sock.sendto(b"x", ("127.0.0.1", 9))
    # the port is free again
    again = m17_peer.PeerHub("M17-DVU", "NO", port=hub.port, bind_ip="127.0.0.1")
    again.close()


def test_bind_error_is_readable():
    hub = m17_peer.PeerHub("M17-DVT", "M", port=0, bind_ip="127.0.0.1")
    try:
        with pytest.raises(OSError, match="M17-DVT cannot bind"):
            m17_peer.PeerHub("M17-DVT", "M", port=hub.port, bind_ip="127.0.0.1")
    finally:
        hub.close()


def test_peer_set_link_report(made):
    hub_t = hub_for(made)
    hub_u = m17_peer.PeerHub("M17-DVU", "NO", port=0, bind_ip="127.0.0.1")
    made.append(hub_u)
    refl_t = reflector_for(made, hub_t)
    refl_u = Reflector(hub_u.port, modules="NO")
    made.append(refl_u)
    peers = m17_peer.PeerSet("ref", "127.0.0.1", hub_t.peer("127.0.0.1", refl_t.port, **SCALE),
                             hub_u.peer("127.0.0.1", refl_u.port, **SCALE))
    peers.link(timeout=5)
    assert all(link.linked for link in peers.links)
    peers.dvu.silent = True
    peers.dvu.drop()
    refl_u.connect = False
    with pytest.raises(m17_peer.PeerLinkTimeout) as err:
        peers.link(timeout=0.4)
    assert "M17-DVU" in str(err.value) and "ref 127.0.0.1" in str(err.value)
    peers.reset()
    assert peers.dvu.silent is False


def test_peer_env_defaults_and_overrides():
    assert m17_peer.peer_env({}) == {"dvt": {"port": 17011, "modules": "M"}, "dvu": {"port": 17012, "modules": "MN"}}
    got = m17_peer.peer_env({"DVT_M17_PEER_DVT_PORT": "20001", "DVT_M17_PEER_DVU_MODULES": "MN"})
    assert got == {"dvt": {"port": 20001, "modules": "M"}, "dvu": {"port": 17012, "modules": "MN"}}


def test_designation_mismatch_listed_and_pong_visible(made):
    hub = hub_for(made)
    refl = reflector_for(made, hub, designation="M17-REF")
    link = hub.peer("127.0.0.1", refl.port, expect_designation="M17-DVR", **SCALE)
    link.link(timeout=5)                                             # a mismatch does not stop the handshake
    assert link.designation_mismatches()
    link.reset()
    assert link.expect_designation == "M17-DVR"                      # a constructor mode survives reset()
    # set the expectation BEFORE clearing: a datagram of the fake reflector still in flight would
    # otherwise be counted against the old expectation after clear() (load-dependent flake)
    link.expect_designation = "M17-REF"
    link.clear()
    refl.sock.sendto(m17.peer_pong_packet("M17-REF"), refl.hub)
    assert link.wait_for(kind="PONG", timeout=2) and not link.sent(kind="PONG")
    time.sleep(0.2)
    assert link.designation_mismatches() == []


def test_peer_binds_ref_needs_its_own_dvu_address():
    """mrefd keys peers by IP: the ref side binds DVT on the tester address
    and DVU on DVT_M17_PEER_DVU_HOST; the sut side keeps 0.0.0.0, or the
    tester address when ref is judged too (a wildcard would block the ref
    hubs on that port)."""
    env = {"DVT_M17_PEER_DVT_HOST": "172.30.0.30", "DVT_M17_PEER_DVU_HOST": "172.30.0.32"}
    both = m17_peer.peer_binds({"sut": 1, "ref": 1}, env, "172.30.0.30")
    assert both == {"sut": {"dvt": "172.30.0.30", "dvu": "172.30.0.30"},
                    "ref": {"dvt": "172.30.0.30", "dvu": "172.30.0.32"}}
    assert m17_peer.peer_binds({"sut": 1}, env, "172.30.0.30") == {"sut": {"dvt": "0.0.0.0", "dvu": "0.0.0.0"}}
    # nothing set: ref DVT follows the tester address, ref DVU any address
    assert m17_peer.peer_binds({"ref": 1}, {}, "172.30.0.30") == {"ref": {"dvt": "172.30.0.30", "dvu": "0.0.0.0"}}
    assert m17_peer.peer_binds({}, env, "172.30.0.30") == {}


def test_hub_pool_binds_per_ip_and_waits_for_a_late_address():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "m17_peer_fixtures_by_path", Path(__file__).resolve().parents[1] / "m17_peer_fixtures.py")
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    config = {"dvt": {"port": 0, "modules": "M"}, "dvu": {"port": 0, "modules": "NO"}}
    pool = conf.HubPool(config, wait=0.6)
    try:
        a, b = pool.get("dvu", "127.0.0.1"), pool.get("dvu", "127.0.0.2")
        assert a is not b and pool.get("dvu", "127.0.0.1") is a
        assert a.sock.getsockname()[0] == "127.0.0.1" and b.sock.getsockname()[0] == "127.0.0.2"
        start = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match="cannot hold"):
            pool.get("dvt", "192.0.2.77")                  # not an address of this host
        assert time.monotonic() - start >= 0.5              # it waited for the address first
    finally:
        pool.close()
