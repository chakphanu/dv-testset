# SPDX-License-Identifier: MIT
"""Tester-side fake M17 reflector peer (M17 Part II, reflector to reflector;
docs/protocols/m17.md stage 2).

A reflector under test (DV-Router, or mrefd in the rig) is configured with
interlink peers that live at the tester, e.g. M17-DVT at <tester>:17011 and
M17-DVU at <tester>:17012. It connects OUT to those ports, so the tester must
hold the port, answer the handshake, keep the link alive and see everything
the reflector sends:

  PeerHub    one UDP socket bound on one tester port, one receive thread, the
             designation and module list the tester presents. It serves every
             remote reflector that talks to the port (mrefd and the sut may
             both point at the same port): datagrams are demultiplexed by
             source ip:port into one PeerLink per remote.
  PeerLink   (alias FakePeer) the conversation with one remote reflector:
             recv_log / sent_log of (monotonic time, size, bytes), the
             handshake and keepalive modes, link(), drop(), send().
  PeerSet    the DVT and DVU links of one judged side (sut or ref).

Handshake (research T7 A.1). The spec is symmetric: both ends send a 37 B
CONN and answer with a 37 B ACKN. mrefd links a peer only when its own CONN is
answered with a valid ACKN; a reflector answers an inbound CONN with ACKN. So
the link does all of: ACKN every acceptable reflector CONN (auto_ackn), send
its own CONN to the remote (auto_conn, default address:17000, every
conn_interval s until the link is up), PING every ping_interval s once the
handshake is done (auto_ping) and NEVER answer a PING. silent makes the link
answer and send nothing at all (the log still fills; send() still works).

Link-up (link()). A link counts as up when the handshake step happened (we
answered the reflector's CONN, or it answered ours with an ACKN) AND the
reflector then sent a PING / PONG or relayed a stream / M17P datagram to us,
the first such datagram being the evidence that it holds the link. Without a
handshake step a PING / relay still counts when the link was never dropped, so
a reflector that is already linked is recognised from its first PING. It stops
counting as up after alive_timeout s without any PING / PONG / relay.

No busy loops: the receive thread blocks in recvfrom with a short socket
timeout (poll) and runs the timers (CONN retry, PING cadence) between
datagrams. close() sends DISC to every contacted remote, stops and joins the
thread and closes the socket.
"""
import re
import socket
import threading
import time
from collections import namedtuple

from .proto import m17

DESIGNATION_DVT, DESIGNATION_DVU = "M17-DVT", "M17-DVU"
DEFAULT_PORTS = {"dvt": 17011, "dvu": 17012}
DEFAULT_MODULES = {"dvt": "M", "dvu": "MN"}
PING_INTERVAL = 3.0
CONN_INTERVAL = 10.0
ALIVE_TIMEOUT = 10.0        # three PINGs of the reflector missed
LINK_TIMEOUT = 40.0


class Rx(namedtuple("Rx", "t size data")):
    """One logged datagram: monotonic time, size, bytes; kind is the
    classify() name (CONN37, ACKN37, PING10, STREAM55, M17P, ...)."""
    __slots__ = ()

    @property
    def kind(self):
        return m17.classify(self.data)


class PeerLinkTimeout(TimeoutError):
    """link() did not see the link come up; str() says what each side sent."""


def peer_env(env):
    """{"dvt": {"port", "modules"}, "dvu": {...}} from DVT_M17_PEER_{DVT,DVU}_
    {PORT,MODULES} (rig/compose.yml defaults when unset)."""
    out = {}
    for key in ("dvt", "dvu"):
        name = "DVT_M17_PEER_%s_" % key.upper()
        out[key] = {"port": int(env.get(name + "PORT") or DEFAULT_PORTS[key]),
                    "modules": env.get(name + "MODULES") or DEFAULT_MODULES[key]}
    return out


def peer_binds(sides, env, tester):
    """{side: {"dvt": ip, "dvu": ip}}: the local address each hub of a judged
    side binds ("sut" / "ref", as cases/proto/conftest.py names them).

    mrefd keys its interlink peers by IP only (the port is ignored), so its
    two peers need two tester addresses: the "ref" side binds M17-DVT to
    DVT_M17_PEER_DVT_HOST (default: the tester address) and M17-DVU to
    DVT_M17_PEER_DVU_HOST (default: any address when unset). The "sut" side
    keeps the tester address for both (the sut keys peers by ip:port); it
    binds any address (0.0.0.0) unless "ref" is judged too, because a wildcard
    socket on a port would block the ref hub on a specific address there.
    Sides that share an (ip, port) share one hub."""
    wild = "0.0.0.0"
    dvt_host = env.get("DVT_M17_PEER_DVT_HOST") or tester or wild
    out = {}
    if "sut" in sides:
        own = (tester or wild) if "ref" in sides else wild
        out["sut"] = {"dvt": own, "dvu": own}
    if "ref" in sides:
        out["ref"] = {"dvt": dvt_host, "dvu": env.get("DVT_M17_PEER_DVU_HOST") or wild}
    return out


def _summary(log):
    counts = {}
    for rx in log:
        counts[rx.kind] = counts.get(rx.kind, 0) + 1
    return ", ".join("%s x%d" % item for item in sorted(counts.items())) or "nothing"


class PeerLink:
    """The conversation with one remote reflector (ip, port) through a hub.
    Modes are plain attributes, changeable at any time:

      auto_ackn   answer an acceptable 37 B CONN with a 37 B ACKN (our
                  designation, ackn_modules or modules)
      auto_conn   send our own 37 B CONN to dest every conn_interval s while
                  seeking the link (before link-up, after drop() only once
                  link() is called again)
      auto_ping   send ping_magic ("PING" or "PONG") every ping_interval s once
                  the handshake is done; a PING of the remote is never answered
      silent      send nothing by itself and answer nothing
      accept      callable(parsed CONN dict) -> bool; default: a NUL
                  terminated list of upper case letters (the designation is
                  not checked: the reflector under test may use any)
      ackn_modules / ackn_designation  override what the ACKN carries
      expect_designation  the designation the remote reflector must use in
                  its CONN / ACKN / PING / PONG / DISC (the sut's setting, or
                  mrefd's M17-REF); a mismatch does not stop the handshake but
                  is listed by designation_mismatches(). A PONG from the
                  remote is an ordinary logged datagram (recv_log kind
                  PONG10), so a stray one is visible with datagrams(kind="PONG").
    """

    MODES = ("auto_ackn", "auto_conn", "auto_ping", "silent", "conn_interval", "ping_interval", "ping_magic",
             "modules", "accept", "ackn_modules", "ackn_designation", "alive_timeout", "dest",
             "expect_designation")

    def __init__(self, hub, remote, dest=None, label="", **modes):
        self.hub = hub
        self.remote = remote
        self.label = label or "%s:%d" % remote
        self._defaults = dict(auto_ackn=True, auto_conn=True, auto_ping=True, silent=False,
                              conn_interval=CONN_INTERVAL, ping_interval=PING_INTERVAL, ping_magic="PING",
                              modules=hub.modules, accept=None, ackn_modules=None, ackn_designation=None,
                              alive_timeout=ALIVE_TIMEOUT, dest=dest or remote,
                              expect_designation=None)
        self._defaults.update({k: v for k, v in hub.link_defaults.items() if k in self.MODES})
        self._defaults.update(modes)
        unknown = set(modes) - set(self.MODES)
        if unknown:
            raise TypeError("unknown PeerLink modes %s" % sorted(unknown))
        for key, value in self._defaults.items():
            setattr(self, key, value)
        self.recv_log = []
        self.sent_log = []
        self.remote_designation = None
        self.remote_modules = None
        self._reset_state(need_handshake=False)

    # -------- state

    def _reset_state(self, need_handshake):
        self._need_handshake = need_handshake
        self._handshake = False
        self._last_alive = None         # last PING / PONG / relay from the remote
        self._seeking = True
        self._dropped = False
        self._next_conn = None
        self._next_ping = None
        self.linked_at = None

    @property
    def linked(self):
        if self._last_alive is None or time.monotonic() - self._last_alive > self.alive_timeout:
            return False
        return self._handshake or not self._need_handshake

    def reset(self):
        """Back to the constructor modes and an empty log; the link state
        (up or down) is kept."""
        for key, value in self._defaults.items():
            setattr(self, key, value)
        self.clear()

    def clear(self):
        with self.hub.cv:
            self.recv_log.clear()
            self.sent_log.clear()

    def mark(self):
        """A time stamp for since= arguments."""
        return time.monotonic()

    # -------- sending

    def send(self, data):
        """data from the hub's bound port to the remote reflector."""
        self.hub.send_to(self, bytes(data))

    def send_conn(self, modules=None, designation=None):
        self.send(m17.peer_conn_packet(designation or self.hub.designation, self.modules if modules is None else modules))

    def send_ackn(self, modules=None):
        self.send(m17.peer_ackn_packet(self.ackn_designation or self.hub.designation,
                                       modules or self.ackn_modules or self.modules))

    def send_ping(self, magic=None):
        data = m17.peer_ping_packet(self.hub.designation)
        self.send((magic or self.ping_magic).encode() + data[4:])

    def send_disc(self):
        self.send(m17.peer_disc_packet(self.hub.designation))

    def drop(self):
        """Send DISC (10 B) and leave the link: auto_conn and auto_ping stay
        off until link() is called again; the reflector's own CONN retries
        are still answered unless auto_ackn is off or the link is silent.
        Old PINGs in flight do not count as link-up afterwards."""
        self.send_disc()
        with self.hub.cv:
            self._reset_state(need_handshake=True)
            self._seeking = False
            self._dropped = True

    # -------- waiting

    def datagrams(self, since=None, kind=None, until=None):
        """Received datagrams (Rx) at or after since (monotonic time), of a
        kind (classify() name or prefix: "STREAM", "PING"; a tuple of
        them; a callable), oldest first."""
        with self.hub.cv:
            log = list(self.recv_log)
        return [rx for rx in log if (since is None or rx.t >= since) and (until is None or rx.t < until)
                and m17.kind_matches(rx.kind, kind)]

    def designation_mismatches(self, since=None):
        """Received 37 B / 10 B control datagrams (Rx) whose designation is
        not expect_designation; empty without an expectation."""
        if not self.expect_designation:
            return []
        out = []
        for rx in self.datagrams(since=since):
            info = m17.parse_peer(rx.data)
            if info and info["designation"] != self.expect_designation:
                out.append(rx)
        return out

    def sent(self, since=None, kind=None):
        with self.hub.cv:
            log = list(self.sent_log)
        return [rx for rx in log if (since is None or rx.t >= since) and m17.kind_matches(rx.kind, kind)]

    def wait_for(self, predicate=None, timeout=1.0, since=None, kind=None):
        """The first received datagram (Rx) at or after since that matches
        kind and predicate(rx), waiting up to timeout s; None on timeout."""
        deadline = time.monotonic() + timeout
        seen = 0
        with self.hub.cv:
            while True:
                log = self.recv_log
                for rx in log[seen:]:
                    if (since is None or rx.t >= since) and m17.kind_matches(rx.kind, kind) \
                            and (predicate is None or predicate(rx)):
                        return rx
                seen = len(log)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.hub.cv.wait(remaining)

    def wait_until(self, condition, timeout=1.0):
        """True once condition(link) holds, waiting up to timeout s."""
        deadline = time.monotonic() + timeout
        with self.hub.cv:
            while not condition(self):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.hub.cv.wait(min(remaining, 0.25))     # linked also changes with time
            return True

    def drain(self, seconds=1.0):
        """Let late datagrams arrive for seconds, then empty the logs."""
        time.sleep(seconds)
        self.clear()

    def link(self, timeout=LINK_TIMEOUT, raise_on_timeout=True):
        """Wait until the link is up (class docstring); the seconds it took
        (0.0 when it already was). Resumes seeking after drop(). On timeout
        PeerLinkTimeout with report(), or False."""
        started = time.monotonic()
        with self.hub.cv:
            self._seeking = True
            self._dropped = False
        if self.wait_until(lambda link: link.linked, timeout):
            return time.monotonic() - started
        if raise_on_timeout:
            raise PeerLinkTimeout("%s: no link in %.0f s; %s" % (self.label, timeout, self.report()))
        return False

    def report(self):
        """What both sides sent so far, for failure messages."""
        now = time.monotonic()
        last = self.recv_log[-1] if self.recv_log else None
        return ("%s as %s %s; reflector sent: %s%s; we sent: %s; state: handshake=%s alive=%s"
                % (self.label, self.hub.designation, self.modules, _summary(self.recv_log),
                   " (last %s %.1f s ago)" % (last.kind, now - last.t) if last else "",
                   _summary(self.sent_log), self._handshake,
                   "never" if self._last_alive is None else "%.1f s ago" % (now - self._last_alive)))

    # -------- called by the hub thread

    def _acceptable(self, info):
        if self.accept is not None:
            return bool(self.accept(info))
        return bool(info["nul_ok"] and re.fullmatch(r"[A-Z]+", info["modules"]))

    def _on_datagram(self, rx):
        kind = rx.kind
        info = m17.parse_peer(rx.data)
        if info and kind in ("CONN37", "ACKN37"):
            self.remote_designation, self.remote_modules = info["designation"], info["modules"]
        if kind == "ACKN37":
            # a late ACKN to the CONN we sent before drop() must not revive the link
            self._handshake = self._handshake or not self._dropped
        elif kind == "DISC10":
            self._reset_state(need_handshake=True)
            self._next_conn = rx.t + self.conn_interval
        elif kind in ("PING10", "PONG10") or kind.startswith("STREAM") or kind == "M17P":
            if self._handshake or not self._need_handshake:
                if self._last_alive is None or time.monotonic() - self._last_alive > self.alive_timeout:
                    self.linked_at = rx.t
                self._last_alive = rx.t
        if self.silent:
            return
        if kind == "CONN37" and self.auto_ackn and info and self._acceptable(info):
            self._handshake = True
            self.send_ackn()
        # a PING / PONG is never answered

    def _tick(self, now):
        if self.silent:
            return
        if (self._handshake or self._last_alive is not None) and self.auto_ping:
            if self._next_ping is None:
                self._next_ping = now + self.ping_interval
            elif now >= self._next_ping:
                self.send_ping()
                self._next_ping += self.ping_interval
                if self._next_ping <= now:
                    self._next_ping = now + self.ping_interval
        if self._seeking and self.auto_conn and not self.linked:
            if self._next_conn is None or now >= self._next_conn:
                self.send_conn()
                self._next_conn = now + self.conn_interval


FakePeer = PeerLink


class PeerHub:
    """One UDP socket bound on a tester port, the designation and module list
    the tester presents on it, and one PeerLink per remote reflector.

    peer(host, port=17000, **modes) registers the link to a reflector whose
    datagrams come from host:port (a reflector sends from its own listener,
    17000) and whose CONN auto_conn sends to the same address; a datagram
    from an unregistered source creates a link on its own (modes from the
    hub's link_defaults) so several reflectors can use the port at once.
    link_defaults: PeerLink modes for every link of the hub."""

    def __init__(self, designation, modules, port=0, bind_ip="0.0.0.0", remote_port=m17.PORT, poll=0.02,
                 name=None, **link_defaults):
        self.designation = designation
        self.modules = modules
        self.remote_port = remote_port
        self.poll = poll
        self.link_defaults = link_defaults
        self.name = name or designation
        self.cv = threading.Condition()
        self.links = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((bind_ip, port))
        except OSError as exc:
            self.sock.close()
            raise OSError("%s cannot bind %s:%d: %s" % (designation, bind_ip, port, exc)) from exc
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.sock.settimeout(poll)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._closed = False
        self.error = None
        self._thread = threading.Thread(target=self._run, name="m17-peer-%s-%d" % (self.name, self.port), daemon=True)
        self._thread.start()

    def peer(self, host, port=None, **modes):
        """The (possibly new) link to the reflector at host:port (default
        remote_port). modes override the hub's link_defaults."""
        addr = (host, port or self.remote_port)
        with self.cv:
            link = self.links.get(addr)
            if link is None:
                link = self.links[addr] = PeerLink(self, addr, **modes)
            else:
                for key, value in modes.items():
                    setattr(link, key, value)
            return link

    def send_to(self, link, data):
        try:
            self.sock.sendto(data, link.dest)
        except OSError:
            return
        with self.cv:
            link.sent_log.append(Rx(time.monotonic(), len(data), data))

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                data = None
            except OSError:
                if self._stop.is_set():
                    break
                continue
            now = time.monotonic()
            try:
                if data is not None:
                    with self.cv:
                        link = self.links.get(addr)
                        if link is None:
                            link = self.links[addr] = PeerLink(self, addr)
                        rx = Rx(now, len(data), data)
                        link.recv_log.append(rx)
                        link._on_datagram(rx)
                        self.cv.notify_all()
                with self.cv:
                    links = list(self.links.values())
                for link in links:
                    link._tick(now)
            except Exception as exc:           # keep serving; the case sees the missing reaction
                self.error = exc

    def close(self):
        """DISC (10 B) to every remote that was contacted, stop and join the
        thread, close the socket. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        with self.cv:
            links = list(self.links.values())
        for link in links:
            if link.recv_log or link.sent_log:
                try:
                    self.sock.sendto(m17.peer_disc_packet(self.designation), link.dest)
                except OSError:
                    pass
        self._stop.set()
        self._thread.join(2.0)
        self.sock.close()
        with self.cv:
            self.cv.notify_all()


class PeerSet:
    """The DVT and DVU links of one judged side: side ("sut" or "ref"), the
    reflector address (ip) and client port, and the two PeerLinks."""

    def __init__(self, side, address, dvt, dvu, port=m17.PORT):
        self.side, self.address, self.port = side, address, port
        self.dvt, self.dvu = dvt, dvu

    @property
    def links(self):
        return (self.dvt, self.dvu)

    def link(self, timeout=LINK_TIMEOUT):
        """Wait for every link (one common deadline); PeerLinkTimeout names
        the links that did not come up and what both sides sent."""
        deadline = time.monotonic() + timeout
        down = []
        for link in self.links:
            if link.link(max(0.0, deadline - time.monotonic()), raise_on_timeout=False) is False:
                down.append(link)
        if down:
            raise PeerLinkTimeout("%s %s: %s not linked after %.0f s: %s" % (
                self.side, self.address, ", ".join(link.label for link in down), timeout,
                " | ".join(link.report() for link in self.links)))

    def reset(self):
        for link in self.links:
            link.reset()

    def clear(self):
        for link in self.links:
            link.clear()

    def drain(self, seconds=1.0):
        time.sleep(seconds)
        self.clear()

    def report(self):
        return " | ".join(link.report() for link in self.links)
