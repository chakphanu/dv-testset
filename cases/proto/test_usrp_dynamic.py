# SPDX-License-Identifier: MIT
"""USRP dynamic clients (docs/protocols/usrp.md section 5, P-USR-27..40):
DVSwitch-style clients that register themselves (REG:DVSWITCH / REG:OK)
from any address instead of a configured row.

Target: DVT_USRP_DYNAMIC_HUB=<ip:port> points the cases at that hub and
judges the dev items by dvtest/usrp_hub.SPEC (the fake hub the unit tests
start). Otherwise the system under test: under the xlxd profile everything
skips ("xlxd has no dynamic USRP"); under another profile the cases run
only when its "usrp" spec has "dynamic_server": True ("edge not ready"
until then), at xlxd-sut:<"dynamic_port", default 12345>, and the dev
items follow SPEC["usrp"]["P-USR-NN"]. conv items are asserted for every
target.

DVT_USRP_DYNAMIC_FAST=1 (the unit run against the local fake hub) shortens
overs and listening windows. DVT_USRP_IDLE_TIMEOUT_S overrides the idle
timeout the P-USR-31/32 cases wait out (a test server configured shorter);
a timeout above 600 s skips unless DVT_LONG_TIMEOUTS=1.
"""
import json
import os
import socket
import time

import pytest

from dvtest import specs, usrp_hub
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import usrp

pytestmark = [pytest.mark.proto, pytest.mark.rig]

FAST = os.environ.get("DVT_USRP_DYNAMIC_FAST") == "1"
FRAMES = 10 if FAST else 25
LISTEN = 0.15 if FAST else 0.8                  # listening window after an over
SETTLE = 0.03 if FAST else 0.2                  # after a module selection
GAP = 0.005 if FAST else 0.020                  # voice frame period (the local fake hub needs no real time)
REPLY_S = 1.0
LONG_TIMEOUT_S = 600


class Dyn:
    def __init__(self, addr, rules):
        self.addr = addr
        self.rules = rules

    def entry(self, nn):
        return self.rules.get(nn) or {}


@pytest.fixture(scope="module")
def dyn(target):
    hub = os.environ.get("DVT_USRP_DYNAMIC_HUB")
    if hub:
        host, port = hub.rsplit(":", 1)
        return Dyn((host, int(port)), usrp_hub.SPEC)
    if target.sut_profile == specs.DEFAULT:
        pytest.skip("xlxd has no dynamic USRP")
    rules = specs.rule(target, "usrp") or {}
    if not rules.get("dynamic_server"):
        pytest.skip("edge not ready: SPEC['usrp']['dynamic_server'] is not True in dvtest/specs/%s.py"
                    % target.sut_profile)
    if not target.is_given("xlxd_sut"):
        pytest.skip("no system under test address (DVT_XLXD_SUT)")
    return Dyn((target.xlxd_sut, int(rules.get("dynamic_port", 12345))), rules)


def judge_dev(dyn, nn, observed):
    """dev items: the target's rule for nn decides; skip while pending."""
    print("%s observed: %r" % (nn, observed))
    entry = dyn.entry(nn)
    pending = [key for key in observed if key not in entry]
    for key, value in observed.items():
        if key in entry:
            assert value == entry[key], "%s %s: sut %r, spec %r" % (nn, key, value, entry[key])
    if pending:
        pytest.skip("spec pending: no usrp %s %s entry" % (nn, pending))


def idle_timeout(dyn):
    """The idle timeout the P-USR-31/32 cases wait out."""
    env = os.environ.get("DVT_USRP_IDLE_TIMEOUT_S")
    timeout = float(env) if env else dyn.entry("P-USR-31").get("idle_timeout_s")
    if timeout is None:
        pytest.skip("spec pending: no usrp P-USR-31 idle_timeout_s")
    if timeout > LONG_TIMEOUT_S and os.environ.get("DVT_LONG_TIMEOUTS") != "1":
        pytest.skip("idle timeout %.0f s > %d s: set DVT_LONG_TIMEOUTS=1, or DVT_USRP_IDLE_TIMEOUT_S to a "
                    "server configured shorter" % (timeout, LONG_TIMEOUT_S))
    return timeout


class DynClient:
    """One DVSwitch-style client on its own ephemeral port."""

    def __init__(self, dyn, callsign="N0CALL"):
        self.dest = dyn.addr
        self.callsign = callsign
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)
        self.seq = 0
        self.inbox = []                              # (time, data, source)

    def send(self, build, *args):
        self.sock.sendto(build(self.seq, *args), self.dest)
        self.seq += 1

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            try:
                data, source = self.sock.recvfrom(2048)
                self.inbox.append((time.monotonic(), data, source))
                continue
            except BlockingIOError:
                pass
            if time.monotonic() >= deadline:
                return
            time.sleep(0.002)

    def texts(self):
        return [p for _, d, _ in self.inbox if (p := usrp.text_payload(d)) is not None]

    def wait_text(self, prefix, seconds=REPLY_S):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump(0.02)
            if any(t.startswith(prefix) for t in self.texts()):
                return True
        return False

    def register(self):
        self.send(usrp.text, usrp.REG)
        return self.wait_text(usrp.REG_OK)

    def join(self, module="D"):
        assert self.register(), "no REG:OK"
        self.send(usrp.text_set_info, self.callsign)
        self.send(usrp.dtmf, module)
        self.pump(SETTLE)

    def talk(self, frames=FRAMES, pcm=None):
        pcm = pcm or signals.frames(signals.tone(700, frames * 20))
        start = time.monotonic()
        for index, frame in enumerate(pcm):
            pace(start, index, GAP)
            self.send(usrp.voice, frame)
        self.send(usrp.unkey)

    def voice(self):
        return [d for _, d, _ in self.inbox if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0
                and usrp.parse(d).keyup == 1]

    def close(self, unregister=True):
        if unregister:
            self.send(usrp.text, usrp.REG_UNREG)
        self.sock.close()


@pytest.fixture
def make(dyn):
    made = []

    def factory(callsign="N0CALL"):
        made.append(DynClient(dyn, callsign))
        return made[-1]
    yield factory
    for client in made:
        try:
            client.close()
        except OSError:
            pass


def test_usrp_dyn_register(make, dyn):
    """P-USR-27 (conv): REG:DVSWITCH from a new address is answered
    REG:OK."""
    client = make()
    assert client.register(), "no REG:OK; texts %r" % client.texts()


def test_usrp_dyn_reply_to_source(make, dyn):
    """P-USR-28 (conv): two clients on ephemeral ports each get their
    REG:OK, and voice, on their own source port, from the server address."""
    a, b = make(), make("N0TEST")
    a.join()
    b.join()
    a.talk()
    b.pump(LISTEN)
    sources = {s for _, _, s in a.inbox + b.inbox}
    print("reply sources %s, b heard %d frames" % (sources, len(b.voice())))
    assert sources == {dyn.addr} or sources <= {dyn.addr, (socket.gethostbyname(dyn.addr[0]), dyn.addr[1])}
    assert len(b.voice()) >= FRAMES * 0.9


def test_usrp_dyn_unregistered_voice(make, dyn):
    """P-USR-29 (dev, key "unregistered_voice"): voice from an address that
    never registered; a registered listener on the default/selected module."""
    listener = make("N0TEST")
    listener.join()
    stranger = make()
    stranger.talk()
    listener.pump(LISTEN)
    stranger.pump(0.2)
    heard = len(listener.voice())
    print("listener heard %d frames, stranger got %d packets" % (heard, len(stranger.inbox)))
    judge_dev(dyn, "P-USR-29", {"unregistered_voice": "ignored" if heard == 0 else "admitted"})
    stranger.close(unregister=False)


def test_usrp_dyn_unregister(make, dyn):
    """P-USR-30 (conv): after REG:UNREG the client gets nothing more."""
    talker, leaver = make(), make("N0TEST")
    talker.join()
    leaver.join()
    leaver.send(usrp.text, usrp.REG_UNREG)
    leaver.pump(0.2)
    leaver.inbox.clear()
    talker.talk()
    leaver.pump(LISTEN)
    print("after REG:UNREG: %d packets" % len(leaver.inbox))
    assert not leaver.voice()
    leaver.close(unregister=False)


@pytest.mark.slow
def test_usrp_dyn_idle_timeout(make, dyn):
    """P-USR-31 (dev, keys "idle_timeout_s", "unreg_on_drop"): a silent
    registered client is dropped after the rule's timeout (REG:UNREG sent
    or not), gets no further voice, and can register again at once."""
    timeout = idle_timeout(dyn)
    talker, idle = make(), make("N0TEST")
    idle.join()
    idle.pump(timeout + (0.3 if FAST else 1.0))
    talker.join()
    talker.talk()
    idle.pump(LISTEN)
    dropped = not idle.voice()
    unreg = any(t.startswith(usrp.REG_UNREG) for t in idle.texts())
    again = idle.register()
    print("timeout %.1f s: dropped %s, REG:UNREG %s, re-register %s" % (timeout, dropped, unreg, again))
    assert dropped and again
    judge_dev(dyn, "P-USR-31", {"unreg_on_drop": unreg})


@pytest.mark.slow
def test_usrp_dyn_ping_keeps(make, dyn):
    """P-USR-32 (conv): PINGs keep a client registered past the idle
    timeout (1.3 x) and get no reply."""
    timeout = idle_timeout(dyn)
    talker, keeper = make(), make("N0TEST")
    keeper.join()
    keeper.inbox.clear()
    end = time.monotonic() + timeout * 1.3
    while time.monotonic() < end:
        keeper.send(usrp.ping)
        keeper.pump(timeout / 4)
    replies = list(keeper.inbox)
    talker.join()
    talker.talk()
    keeper.pump(LISTEN)
    print("PING replies %d, heard %d frames after %.1f s" % (len(replies), len(keeper.voice()), timeout * 1.3))
    assert not replies and len(keeper.voice()) >= FRAMES * 0.9


@pytest.mark.parametrize("form", ["letter", "tg"])
def test_usrp_dyn_select_module(make, dyn, form):
    """P-USR-33 (dev, key "dial_letter" / "dial_tg"): a DTMF dial string
    picks the module (E or 4005); a client on another module hears nothing."""
    dial = "E" if form == "letter" else "4005"
    talker, same, other = make(), make("N0TEST"), make("N0TEST")
    talker.join(dial)
    same.join(dial)
    other.join("F")
    talker.talk()
    same.pump(LISTEN)
    other.pump(0.1)
    works = len(same.voice()) >= FRAMES * 0.9 and not other.voice()
    print("%s: same module %d frames, other module %d, confirms %r" % (
        dial, len(same.voice()), len(other.voice()), [t for t in talker.texts() if t.startswith(b"INFO:MSG")]))
    judge_dev(dyn, "P-USR-33", {"dial_" + form: works})


def test_usrp_dyn_default_module(make, dyn):
    """P-USR-34 (dev, key "default_module"): two registered clients that
    never picked a module: does one hear the other ("shared") or not
    ("none")?"""
    a, b = make(), make("N0TEST")
    assert a.register() and b.register()
    a.send(usrp.text_set_info, a.callsign)          # allowed to talk, just no module
    b.send(usrp.text_set_info, b.callsign)
    a.talk()
    b.pump(LISTEN)
    judge_dev(dyn, "P-USR-34", {"default_module": "shared" if b.voice() else "none"})


def test_usrp_dyn_set_info_in(make, dyn):
    """P-USR-35 (conv): the talker's own SET_INFO names it: the listener's
    SET_INFO for the call carries that callsign."""
    talker, listener = make("N0TEST"), make("N0CALL")
    talker.join()
    listener.join()
    talker.talk()
    listener.pump(LISTEN)
    infos = [usrp.parse_set_info(d[usrp.HEADER_SIZE:]) for _, d, _ in listener.inbox
             if usrp.type_le(d) == usrp.TYPE_TEXT]
    calls = [i["callsign"] for i in infos if i]
    print("SET_INFO callsigns at the listener: %r" % calls)
    assert calls and calls[0] == "N0TEST"


def test_usrp_dyn_set_info_out(make, dyn):
    """P-USR-36 (conv): each call to a dynamic client starts with a TEXT
    SET_INFO before its first voice frame (two calls)."""
    talker, listener = make(), make("N0TEST")
    talker.join()
    listener.join()
    for _ in range(2):
        talker.talk()
        listener.pump(LISTEN + (0.05 if FAST else 0.2))
    order = ["info" if usrp.type_le(d) == usrp.TYPE_TEXT and usrp.parse_set_info(d[usrp.HEADER_SIZE:])
             else "voice" if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0
             else "unkey" if len(d) == usrp.HEADER_SIZE else "other"
             for _, d, _ in listener.inbox if not usrp.text_payload(d) or usrp.parse_set_info(d[usrp.HEADER_SIZE:])]
    calls, current = [], None
    for item in order:
        if item == "info":
            current = []
            calls.append(current)
        elif item == "voice":
            assert current is not None, "voice before any SET_INFO: %s" % order[:5]
            current.append(item)
    print("calls with SET_INFO first: %s" % [len(c) for c in calls])
    assert len(calls) == 2 and all(len(c) >= FRAMES * 0.9 for c in calls)


def test_usrp_dyn_info(make, dyn):
    """P-USR-37 (dev, key "info_reply"): "INFO:" is answered with JSON
    ("json"), something else ("text") or nothing ("none")."""
    client = make()
    client.join()
    client.inbox.clear()
    client.send(usrp.text, usrp.INFO)
    got = client.wait_text(usrp.INFO + b"{") or client.wait_text(usrp.INFO, 0.2)
    reply = next((t for t in client.texts() if t.startswith(usrp.INFO)), None)
    mode = "none"
    if reply is not None:
        try:
            json.loads(reply[len(usrp.INFO):])
            mode = "json"
        except ValueError:
            mode = "text"
    print("INFO reply %r (%s)" % (reply, got))
    judge_dev(dyn, "P-USR-37", {"info_reply": mode})


def test_usrp_dyn_two_clients(make, dyn):
    """P-USR-38 (conv): three clients on one module; the talker's over
    reaches both others once each and never comes back to it; a second
    talker keying during the over is not relayed."""
    talker, b, c = make(), make("N0TEST"), make("N0TEST")
    for client in (talker, b, c):
        client.join()
    pcm = signals.frames(signals.tone(700, FRAMES * 20))
    start = time.monotonic()
    for index, frame in enumerate(pcm):
        pace(start, index, GAP)
        talker.send(usrp.voice, frame)
        if index >= 5:
            b.send(usrp.voice, frame)              # second talker keys during the over
    talker.send(usrp.unkey)
    b.send(usrp.unkey)
    for client in (talker, b, c):
        client.pump(LISTEN)
    print("talker got %d, b %d, c %d frames" % (len(talker.voice()), len(b.voice()), len(c.voice())))
    assert not talker.voice(), "echo to the talker"
    assert FRAMES * 0.9 <= len(b.voice()) <= FRAMES and FRAMES * 0.9 <= len(c.voice()) <= FRAMES


def test_usrp_dyn_slin(make, dyn):
    """P-USR-39 (conv): audio to a dynamic client is type 0 SLIN, 352 B,
    keyup 1, ending with a 32 B unkey."""
    talker, listener = make(), make("N0TEST")
    talker.join()
    listener.join()
    talker.talk()
    listener.pump(LISTEN)
    audio = [d for _, d, _ in listener.inbox if usrp.text_payload(d) is None]
    bad = [d[:32].hex() for d in audio if not (usrp.type_le(d) == 0 and len(d) in (usrp.VOICE_SIZE, usrp.HEADER_SIZE))]
    print("audio packets %d, other %d" % (len(audio), len(bad)))
    assert not bad and len(listener.voice()) >= FRAMES * 0.9 and len(audio[-1]) == usrp.HEADER_SIZE


def test_usrp_dyn_table_full(make, dyn):
    """P-USR-40 (dev, keys "max_clients", "full_reply"): registering one
    client more than the rule's max_clients is refused (REG:UNREG or
    silence)."""
    limit = dyn.entry("P-USR-40").get("max_clients")
    if limit is None:
        pytest.skip("spec pending: no usrp P-USR-40 max_clients")
    if limit > 64:
        pytest.skip("max_clients %d too large to fill from one tester" % limit)
    clients = [make() for _ in range(limit)]
    accepted = sum(client.register() for client in clients)
    extra = make()
    extra.send(usrp.text, usrp.REG)
    deadline = time.monotonic() + REPLY_S
    while time.monotonic() < deadline and not {usrp.REG_OK, usrp.REG_UNREG} & set(extra.texts()):
        extra.pump(0.02)
    texts = extra.texts()
    reply = "REG:OK" if usrp.REG_OK in texts else ("REG:UNREG" if usrp.REG_UNREG in texts else "none")
    print("accepted %d of %d, extra got %s" % (accepted, limit, reply))
    assert accepted == limit
    judge_dev(dyn, "P-USR-40", {"max_clients": limit if reply != "REG:OK" else limit + 1, "full_reply": reply})


@pytest.mark.parametrize("case", ["none", "invalid", "denied"])
def test_usrp_dyn_listen_only_without_callsign(make, dyn, case):
    """P-USR-41 (conv): a client without a valid SET_INFO callsign (none
    sent, an invalid one, or one on the deny list) hears its module but its
    own voice is never relayed. "denied" needs the rule's denied_callsign
    (N0DENY) and, on the rig, that callsign in DVT_XLXD_BLACKLIST."""
    if case == "denied":
        denied = dyn.entry("P-USR-41").get("denied_callsign")
        if not denied:
            pytest.skip("spec pending: no usrp P-USR-41 denied_callsign (a callsign on the server's deny list)")
        if not os.environ.get("DVT_USRP_DYNAMIC_HUB") and denied not in os.environ.get("DVT_XLXD_BLACKLIST", "").split():
            pytest.skip("run with DVT_XLXD_BLACKLIST=%s so the system under test denies it" % denied)
    mute, listener, talker = make(), make("N0TEST"), make("N0CALL")
    assert mute.register(), "no REG:OK"
    if case == "invalid":
        mute.send(usrp.text_set_info, "!!")
    elif case == "denied":
        mute.send(usrp.text_set_info, denied)
    mute.send(usrp.dtmf, "D")
    mute.pump(SETTLE)
    listener.join()
    talker.join()
    mute.talk()
    listener.pump(LISTEN)
    blocked = len(listener.voice())
    talker.talk()
    mute.pump(LISTEN)
    heard = len(mute.voice())
    print("%s: its over relayed %d frames; it heard %d of %d" % (case, blocked, heard, FRAMES))
    assert blocked == 0, "voice of a client without a valid callsign was relayed"
    assert heard >= FRAMES * 0.9, "a listen-only client did not hear its module"
