# SPDX-License-Identifier: MIT
"""DPlus (REF, D-STAR) reflector conformance (docs/protocols/dplus.md,
testlist K): one case per obligation P-DPL-NN, xlxd-ref and xlxd-sut
probed in parallel, DPlus clients in the forms ircDDBGateway, QnetGateway
and DroidStar send.

Judged as in dplus.md: hb = a JARL radio-layer requirement, asserted on
both sides; dev = disputed, or xlxd deviates from what the clients need:
sut == ref under the xlxd profile, else the profile's "dplus" spec entry
{"P-DPL-NN": {observed key: value}} (skip "spec pending" without one);
diff = sut == ref (a convention the servers agree on and the clients rely
on). Every case prints what both sides did.

xlxd sends a DPlus client a module's traffic only after the client has
transmitted on it once, so listeners "join" with an empty stream (header +
end frame) unless a case is about exactly that (P-DPL-30).
"""
import threading
import time

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import UdpClient
from dvtest.proto import dmr, dplus

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD = "P"
NAME = "REF999"                                 # XLX999 as DPlus clients' host lists name it
TALKER, LISTENER = "N0CALL", "N0TEST"
SECOND, THIRD = "STR001", "STR002"              # extra test calls (also in rig dmrid.dat)
DMR_SECOND = 1023101                            # STR001
# xlxd blocks a callsign after three short (< 50 frames) streams in a row
# that each reopen within 2 s: every talk is longer, and every peer that
# transmits in one probe has its own callsign.
FRAMES = 63                                     # three superframes
GAP = 0.020
CACHE = 1.2                                     # xlxd refreshes its DPlus client list once per second
TAIL = 0.8
LOGIN_WAIT = 3.5                                # xlxd answers a new login after 0..2 s


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


def judge_dev(rig, nn, got, *keys):
    """dev items: sut == ref on keys under the xlxd profile, else the
    profile's dplus spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "dplus") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no dplus %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def frames(n=FRAMES, seed="dplus"):
    return signals.prng_frames("dstar", n, seed=seed)


# ---------------------------------------------------------------- clients

class Peer:
    """One DPlus socket: logs every packet with its source address and
    time; answers the server keepalive while answer is True."""

    def __init__(self, ip, callsign=TALKER, module=MOD, answer=True):
        self.client = UdpClient("0.0.0.0", responder=self._rx)
        self.dest = (ip, dplus.PORT)
        self.callsign, self.module, self.answer = callsign, module, answer
        self.log = []
        self.linked = False

    def _rx(self, data, addr):
        self.log.append((time.monotonic(), data, addr))
        if self.answer and data == dplus.KEEPALIVE:
            return dplus.KEEPALIVE
        return None

    def send(self, packet):
        self.client.send(self.dest, packet)

    def clear(self):
        self.client.drain()
        self.client.inbox.clear()
        self.client.inbox_times.clear()
        self.log.clear()

    def wait(self, pred, seconds):
        """(time, data, addr) of the first logged packet matching pred, or None."""
        deadline = time.monotonic() + seconds
        while True:
            self.client.drain()
            for entry in self.log:
                if pred(entry[1]):
                    return entry
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def login(self, packet=None, connect=True):
        """Connect (unless connect is False), then the login; returns
        {"reply": text or "none", "t": seconds from login to reply}."""
        if connect:
            self.clear()
            self.send(dplus.CONNECT)
            self.wait(lambda d: d == dplus.CONNECT, 1.0)
        self.clear()
        start = time.monotonic()
        self.send(packet or dplus.login_irc(self.callsign))
        hit = self.wait(lambda d: dplus.parse(d)["kind"] == "reply", LOGIN_WAIT)
        if not hit:
            return {"reply": "none", "t": None}
        text = dplus.parse(hit[1])["text"]
        self.linked = self.linked or text == "OKRW"
        return {"reply": text, "t": round(hit[0] - start, 2)}

    def radio(self, rpt1=None, rpt2=None, my=None, crc=True):
        cs7 = (self.callsign + " " * 7)[:7]
        return dplus.radio_header(rpt2 or "%s %s" % (NAME, self.module), rpt1 or cs7 + self.module,
                                  my or self.callsign, crc=crc)

    def join(self):
        """Empty stream on the module (header + end frame)."""
        sid = clients.fresh_stream_id("dstar")
        self.send(dplus.header_packet(sid, self.radio()))
        time.sleep(0.1)
        self.send(dplus.end_frame(sid, 0))

    def link(self, join=True):
        got = self.login()
        assert got["reply"] == "OKRW", "%s: login of %s answered %s" % (self.dest[0], self.callsign, got)
        if join:
            self.join()
        return got

    def close(self):
        try:
            if self.linked:
                self.send(dplus.DISCONNECT)
        finally:
            self.client.close()

    def parsed(self):
        return [p for p in (dplus.parse(d) for d in self.client.inbox) if p["kind"] != "keepalive"]

    def heard(self):
        return [p["frame"] for p in self.parsed() if p["kind"] == "voice"]


def ready(*peers, join=True):
    for peer in peers:
        peer.link(join)
    time.sleep(CACHE)
    for peer in peers:
        peer.clear()


def done(*peers):
    for peer in peers:
        peer.close()


def settle(*peers, seconds=TAIL):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for peer in peers:
            peer.client.drain()
        time.sleep(0.01)


def stream(peer, voice, sid, heads=1, head_every=False, end="irc", radio=None):
    """Header (heads times), voice with the sync on seq 0 (and the header
    again before every superframe if head_every), then an end frame in
    the ircDDBGateway ("irc"), DroidStar ("droid") or xlxd ("xlxd") form,
    or none."""
    head = dplus.header_packet(sid, radio or peer.radio())
    out = [head] * heads
    for index, frame in enumerate(voice):
        if head_every and index and index % 21 == 0:
            out.append(head)
        out.append(dplus.voice(sid, index, frame, dplus.SYNC if index % 21 == 0 else dextra_slow()))
    n = len(voice)
    if end == "irc":
        out.append(dplus.end_frame(sid, n))
    elif end == "droid":
        out.append(dplus.end_frame(sid, n, ambe=bytes(9)))
    elif end == "xlxd":
        out.append(dplus.last_voice(sid, n))
    return out


def dextra_slow():
    return b"\x16\x29\xf5"                          # slow-data filler


def send(peer, packets, *listeners, wait=TAIL):
    peer.client.paced(peer.dest, packets, GAP, *[m.client for m in listeners])
    settle(peer, *listeners, seconds=wait)


def talk(peer, *listeners, voice=None, wait=TAIL, **kw):
    voice = voice or frames()
    sid = clients.fresh_stream_id("dstar")
    send(peer, stream(peer, voice, sid, **kw), *listeners, wait=wait)
    return voice, sid


def dmr_talk(ip, listener, voice):
    talker = dmr.DmrClient("0.0.0.0", dmrid=DMR_SECOND, module=MOD)
    try:
        assert talker.link(ip), "%s: DMR talker link failed" % ip
        time.sleep(CACHE)
        listener.clear()
        clients.talk(talker, "dmr", voice, tail=1.5)
        settle(listener, seconds=0.5)
    finally:
        clients.close(talker)


# ---------------------------------------------------------------- 1 connect and login

def connect_probe(ip):
    p = Peer(ip)
    try:
        p.send(dplus.CONNECT)
        hit = p.wait(lambda d: True, 1.0)
        return {"reply": hit[1].hex() if hit else "none", "src_port": hit[2][1] if hit else None}
    finally:
        done(p)


def test_dplus_connect_echo(rig):
    """P-DPL-01 (diff): 05 00 18 00 01 is echoed byte for byte, no state needed."""
    got = both(rig, connect_probe)
    print("P-DPL-01 connect:", got)
    assert got["ref"]["reply"] == dplus.CONNECT.hex(), "ref: %s" % got["ref"]
    same(got, "reply")


def test_dplus_reply_source_port(rig):
    """P-DPL-02 (diff): replies come from UDP 20001 to the sender's own
    (ephemeral) port; QnetGateway accepts nothing else."""
    got = both(rig, connect_probe)
    print("P-DPL-02 source port:", got)
    assert got["ref"]["src_port"] == dplus.PORT, "ref: %s" % got["ref"]
    same(got, "src_port")


def login_forms_probe(ip):
    out = {}
    for style, packet in (("irc", dplus.login_irc(TALKER)), ("qnet", dplus.login_qnet(TALKER)),
                          ("droid", dplus.login_droid(TALKER, 314159))):
        p = Peer(ip)
        try:
            got = p.login(packet)
            out[style], out[style + "_t"] = got["reply"], got["t"]
        finally:
            done(p)
    return out


def test_dplus_login_okrw(rig):
    """P-DPL-03, P-DPL-07 (diff): the ircDDBGateway (DV019999), QnetGateway
    and DroidStar (HSnnnnnn) logins of an unregistered test callsign get
    OKRW."""
    got = both(rig, login_forms_probe)
    print("P-DPL-03 login forms:", got)
    for style in ("irc", "qnet", "droid"):
        assert got["ref"][style] == "OKRW", "ref: %s login answered %s" % (style, got["ref"][style])
    same(got, "irc", "qnet", "droid")


def test_dplus_login_reply_time(rig):
    """P-DPL-04 (diff, bound): every login is answered within 3 s (no
    client retries a login; ircDDBGateway fails the link at 30 s)."""
    got = both(rig, login_forms_probe)
    print("P-DPL-04 login reply time:", got)
    for name, r in got.items():
        for style in ("irc", "qnet", "droid"):
            assert r[style + "_t"] is not None and r[style + "_t"] <= 3.0, "%s: %s reply after %s s" % (
                name, style, r[style + "_t"])


def reply_set_probe(ip):
    p = Peer(ip)
    try:
        p.link(join=False)
        settle(p, seconds=5.0)
        texts = sorted({dplus.parse(d)["text"] for _, d, _ in p.log if dplus.parse(d)["kind"] == "reply"})
        return {"later_replies": texts}
    finally:
        done(p)


def test_dplus_login_reply_set(rig):
    """P-DPL-05 (diff): nothing but OKRW / BUSY ever comes as a login reply
    (ircDDBGateway drops the link on any other 8-byte reply, even later)."""
    got = both(rig, reply_set_probe)
    print("P-DPL-05 reply set:", got)
    for name, r in got.items():
        assert set(r["later_replies"]) <= {"OKRW", "BUSY"}, "%s: %s" % (name, r)
    same(got, "later_replies")


def invalid_login_probe(ip):
    bad, good = Peer(ip), Peer(ip)
    try:
        # one malformed login only: xlxd ignores the whole IP after three
        reply = bad.login(dplus.login_irc("1234567"))["reply"]
        bad.linked = False
        after = good.login()["reply"]
        return {"reply": reply, "valid_login_after": after}
    finally:
        done(bad, good)


def test_dplus_login_invalid_callsign(rig):
    """P-DPL-06 (dev): a login with a malformed callsign never links; the
    reply (BUSY, silence) is the dev part. One try only, so the tester's IP
    is never banned; a valid login right after must still work."""
    got = both(rig, invalid_login_probe)
    print("P-DPL-06 invalid callsign:", got)
    for name, r in got.items():
        assert r["reply"] != "OKRW", "%s: malformed callsign linked" % name
        assert r["valid_login_after"] == "OKRW", "%s: valid login refused after one bad try" % name
    judge_dev(rig, "P-DPL-06", got, "reply")


def no_connect_probe(ip):
    p = Peer(ip)
    try:
        return {"reply": p.login(connect=False)["reply"]}
    finally:
        done(p)


def test_dplus_login_without_connect(rig):
    """P-DPL-08 (dev): a login without the connect first."""
    got = both(rig, no_connect_probe)
    print("P-DPL-08 login without connect:", got)
    judge_dev(rig, "P-DPL-08", got, "reply")


def relogin_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        again = listener.login()
        listener.clear()
        voice, _ = talk(talker, listener)
        heard = listener.heard()
        return {"again": again["reply"], "again_t": again["t"], "exact": heard == voice,
                "received": len(heard)}
    finally:
        done(talker, listener)


def test_dplus_relogin_one_session(rig):
    """P-DPL-09 (diff): connect + login again from a linked IP:port: OKRW,
    one session, every frame arrives once."""
    got = both(rig, relogin_probe)
    print("P-DPL-09 re-login:", got)
    assert got["ref"]["again"] == "OKRW" and got["ref"]["exact"], "ref: %s" % got["ref"]
    same(got, "again", "exact")


# ---------------------------------------------------------------- 2 keepalive and close

def keepalive_probe(ip):
    p = Peer(ip, answer=False)
    try:
        start = time.monotonic()
        p.link(join=False)
        settle(p, seconds=4.0)                      # phase A: the client sends nothing
        quiet = [t for t, d, _ in p.log if d == dplus.KEEPALIVE]
        mark = time.monotonic()
        for _ in range(16):                         # phase B: a client keepalive every 0.25 s
            p.send(dplus.KEEPALIVE)
            settle(p, seconds=0.25)
        busy = [t for t, d, _ in p.log if d == dplus.KEEPALIVE and t >= mark]
        times = sorted(quiet + busy)
        gaps = [b - a for a, b in zip(times, times[1:])]
        return {"first": round(times[0] - start, 1) if times else None,
                "max_gap": round(max(gaps), 1) if gaps else None,
                "quiet_count": len([t for t in quiet if t >= start]), "busy_count": len(busy),
                "period_ok": bool(times) and times[0] - start <= 6.0 and bool(gaps) and max(gaps) <= 2.0,
                "one_for_one": len(busy) >= 12}
    finally:
        done(p)


def test_dplus_server_keepalive(rig):
    """P-DPL-10, P-DPL-11 (diff): 03 60 00 to a linked client, the first
    within 6 s, then at most 2 s apart; client keepalives are not answered
    one for one."""
    got = both(rig, keepalive_probe)
    print("P-DPL-10/11 keepalive:", got)
    for name, r in got.items():
        assert r["period_ok"], "%s: %s" % (name, r)
    same(got, "period_ok", "one_for_one")


def timeout_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER, answer=False)
    try:
        start = time.monotonic()
        listener.link()
        talker.link(join=False)
        hit = listener.wait(lambda d: d == dplus.DISCONNECT, 45.0)
        dropped_after = round(hit[0] - start) if hit else None
        listener.clear()
        talk(talker, listener)
        heard_after = len(listener.heard())
        return {"dropped": hit is not None or heard_after == 0, "disconnect_sent": hit is not None,
                "timeout_s": None if dropped_after is None else 5 * round(dropped_after / 5),
                "dropped_after": dropped_after, "heard_after": heard_after}
    finally:
        listener.linked = False
        done(talker, listener)


def test_dplus_timeout_drop(rig):
    """P-DPL-12 (dev): a client that never sends a keepalive is dropped;
    after that it hears nothing. Timeout (5 s buckets) and the disconnect
    notice are the dev part."""
    got = both(rig, timeout_probe)
    print("P-DPL-12 timeout:", got)
    for name, r in got.items():
        if r["dropped"]:
            assert r["heard_after"] == 0, "%s: heard %d frames after the drop" % (name, r["heard_after"])
    judge_dev(rig, "P-DPL-12", got, "dropped", "disconnect_sent", "timeout_s")


def kept_alive_probe(ip):
    talker, listener = Peer(ip, answer=False), Peer(ip, LISTENER)
    try:
        ready(listener)
        talker.link(join=False)
        voice = frames(1800, "p13")                # 36 s, past xlxd's 30 s keepalive timeout
        talk(talker, listener, voice=voice)
        cut = [t for t, d, _ in talker.log if d == dplus.DISCONNECT]
        heard = listener.heard()
        return {"disconnected": bool(cut), "heard_all": heard == voice, "received": len(heard)}
    finally:
        done(talker, listener)


def test_dplus_talker_kept_alive(rig):
    """P-DPL-13 (diff): a client that talks for 36 s without one keepalive
    is not dropped mid-stream."""
    got = both(rig, kept_alive_probe)
    print("P-DPL-13 talker kept alive:", got)
    assert not got["ref"]["disconnected"] and got["ref"]["heard_all"], "ref: %s" % got["ref"]
    same(got, "disconnected", "heard_all")


def disconnect_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        listener.send(dplus.DISCONNECT)
        echo = listener.wait(lambda d: d == dplus.DISCONNECT, 1.0) is not None
        listener.linked = False
        time.sleep(CACHE)
        listener.clear()
        talk(talker, listener)
        return {"echo": echo, "heard_after": len(listener.heard())}
    finally:
        done(talker, listener)


def test_dplus_disconnect(rig):
    """P-DPL-14 (diff): 05 00 18 00 00 from a linked client is echoed and
    ends the session (no further voice)."""
    got = both(rig, disconnect_probe)
    print("P-DPL-14 disconnect:", got)
    assert got["ref"]["echo"] and got["ref"]["heard_after"] == 0, "ref: %s" % got["ref"]
    same(got, "echo", "heard_after")


def disconnect_unknown_probe(ip):
    p = Peer(ip)
    try:
        p.send(dplus.DISCONNECT)
        return {"echo": p.wait(lambda d: d == dplus.DISCONNECT, 1.0) is not None}
    finally:
        done(p)


def test_dplus_disconnect_unknown(rig):
    """P-DPL-15 (dev): a disconnect from an IP:port that is not linked
    (ircDDBGateway resends it every 60 s until it is echoed)."""
    got = both(rig, disconnect_unknown_probe)
    print("P-DPL-15 disconnect unknown:", got)
    judge_dev(rig, "P-DPL-15", got, "echo")


# ---------------------------------------------------------------- 3 header and voice

def module_probe(ip):
    listener = Peer(ip, LISTENER)
    other, space = Peer(ip), Peer(ip, SECOND)
    try:
        ready(listener)
        other.link(join=False)
        space.link(join=False)
        out = {}
        for key, peer, rpt1 in (("rpt1_other_heard", other, "N0CALL B"), ("rpt1_space_heard", space, "STR001  ")):
            listener.clear()
            voice, _ = talk(peer, listener, radio=peer.radio(rpt1=rpt1), wait=1.2)
            out[key] = listener.heard() == voice
        return out
    finally:
        done(listener, other, space)


def test_dplus_module_from_rpt2(rig):
    """P-DPL-16 (dev): the stream goes to the RPT2 module. With RPT1 = the
    hotspot's own module (B) it must arrive (every server); with a
    non-letter RPT1 module xlxd drops the header (the dev part)."""
    got = both(rig, module_probe)
    print("P-DPL-16 module:", got)
    for name, r in got.items():
        assert r["rpt1_other_heard"], "%s: RPT1 module B, RPT2 module %s not relayed" % (name, MOD)
    judge_dev(rig, "P-DPL-16", got, "rpt1_space_heard")


def crc_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        voice, _ = talk(talker, listener, radio=talker.radio(crc=0x0000))
        return {"heard_bad_crc": listener.heard() == voice}
    finally:
        done(talker, listener)


def test_dplus_header_crc_ignored(rig):
    """P-DPL-17 (diff): a header with a wrong CRC is relayed (no client
    trusts the DPlus CRC)."""
    got = both(rig, crc_probe)
    print("P-DPL-17 bad CRC:", got)
    assert got["ref"]["heard_bad_crc"], "ref: %s" % got["ref"]
    same(got, "heard_bad_crc")


def first(peer, kind):
    return next((p for p in peer.parsed() if p["kind"] == kind), None)


def egress_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    out = {}
    try:
        ready(listener, talker)
        talk(talker, listener)
        dmr_heard = None
        for source in ("dplus", "dmr"):
            if source == "dmr":
                dmr_talk(ip, listener, signals.prng_frames("dmr", 63, seed="p18"))
            head = first(listener, "header")
            out[source + "_crc_ok"] = bool(head and head["crc_ok"])
            for key in ("rpt2", "rpt1", "ur", "flags"):
                value = head[key] if head else None
                out["%s_%s" % (source, key)] = value if value is None else (
                    value.hex() if key == "flags" else value.decode("latin-1"))
            end = first(listener, "end")
            out[source + "_end"] = end and {"byte0": end["byte0"], "byte8": end["byte8"],
                                            "tail": end["tail"].hex()}
            if source == "dmr":
                dmr_heard = len(listener.heard())
            listener.clear()
        out["dmr_heard"] = dmr_heard
        return out
    finally:
        done(talker, listener)


def test_dplus_egress_header(rig):
    """P-DPL-18 (hb): egress headers (from DPlus and from DMR) carry a
    correct CRC-CCITT. P-DPL-19, P-DPL-20 (diff): RPT2 = REF999 + module,
    flags; RPT1 and UR recorded."""
    got = both(rig, egress_probe)
    print("P-DPL-18..20 egress header:", got)
    for name, r in got.items():
        for source in ("dplus", "dmr"):
            assert r[source + "_crc_ok"], "%s: %s header CRC wrong or missing" % (name, source)
    assert got["ref"]["dplus_rpt2"] == "%s %s" % (NAME, MOD), "ref: RPT2 %r" % got["ref"]["dplus_rpt2"]
    same(got, "dplus_rpt2", "dmr_rpt2", "dplus_flags", "dmr_flags", "dplus_ur", "dmr_ur")


def passthrough_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        voice, sid = talk(talker, listener)
        got = listener.parsed()
        ids = {p["stream_id"] for p in got if p["kind"] in ("header", "voice", "end")}
        return {"exact": listener.heard() == voice, "one_id": len(ids) == 1, "id_nonzero": 0 not in ids,
                "kept_id": ids == {sid}, "echo": len(talker.heard())}
    finally:
        done(talker, listener)


def test_dplus_passthrough(rig):
    """P-DPL-21 (diff): voice relayed with AMBE and slow data unchanged.
    P-DPL-27 (hb): one nonzero stream id across header, voice and end.
    P-DPL-28 (diff): the talker hears nothing of its own stream."""
    got = both(rig, passthrough_probe)
    print("P-DPL-21/27/28 passthrough:", got)
    for name, r in got.items():
        assert r["one_id"] and r["id_nonzero"], "%s: %s" % (name, r)
    assert got["ref"]["exact"] and got["ref"]["echo"] == 0, "ref: %s" % got["ref"]
    same(got, "exact", "kept_id", "echo")


def sync_probe(ip):
    listener = Peer(ip, LISTENER)
    try:
        ready(listener)
        dmr_talk(ip, listener, signals.prng_frames("dmr", 63, seed="p22"))
        voice = [p for p in listener.parsed() if p["kind"] == "voice"]
        seq0 = [p for p in voice if p["seq"] == 0]
        return {"voice": len(voice), "seq0": len(seq0), "all_sync": bool(seq0) and all(
            p["slow"] == dplus.SYNC for p in seq0), "first_seq": voice[0]["seq"] if voice else None}
    finally:
        done(listener)


def test_dplus_egress_sync(rig):
    """P-DPL-22 (hb): every egress seq 0 frame of a stream from another mode
    (DMR) carries the sync 55 2D 16."""
    got = both(rig, sync_probe)
    print("P-DPL-22 sync:", got)
    for name, r in got.items():
        assert r["all_sync"], "%s: %s" % (name, r)
    same(got, "first_seq")


def end_forms_probe(ip):
    out = {}
    talker, second, listener = Peer(ip), Peer(ip, SECOND), Peer(ip, LISTENER)
    try:
        ready(listener, talker, second)
        for form in ("irc", "droid", "xlxd"):
            talk(talker, listener, voice=frames(63, "p23" + form), end=form, wait=0.5)
            out[form + "_end_relayed"] = first(listener, "end") is not None
            listener.clear()
            voice, _ = talk(second, listener, voice=frames(63, "p23b" + form), wait=0.8)
            out[form + "_next_relayed"] = listener.heard() == voice
            listener.clear()
        return out
    finally:
        done(talker, second, listener)


def test_dplus_end_forms(rig):
    """P-DPL-23 (diff): the ircDDBGateway (null AMBE + JARL pattern),
    DroidStar (zero AMBE + pattern) and xlxd end frames close the stream
    at once: the end is relayed and a second talker 0.5 s later is heard."""
    got = both(rig, end_forms_probe)
    print("P-DPL-23 end forms:", got)
    keys = [f + s for f in ("irc", "droid", "xlxd") for s in ("_end_relayed", "_next_relayed")]
    for key in keys:
        assert got["ref"][key], "ref: %s" % got["ref"]
    same(got, *keys)


def egress_end_probe(ip):
    r = egress_probe(ip)
    out = {}
    for source in ("dplus", "dmr"):
        end = r[source + "_end"] or {}
        tail = bytes.fromhex(end.get("tail", ""))
        out[source + "_byte8"] = end.get("byte8")
        out[source + "_pattern_at_26"] = tail[9:15] == dplus.JARL_END
        out[source + "_end_seen"] = bool(end)
    out["pattern_at_26"] = out["dplus_pattern_at_26"] and out["dmr_pattern_at_26"]
    out["byte8"] = out["dplus_byte8"]
    return out


def test_dplus_egress_end(rig):
    """P-DPL-24 (dev): the egress end frame is 32 B with the 0x40 bit (all
    clients); the JARL end pattern at bytes 26-31 and byte 8 are the dev
    part."""
    got = both(rig, egress_end_probe)
    print("P-DPL-24 egress end:", got)
    for name, r in got.items():
        assert r["dplus_end_seen"] and r["dmr_end_seen"], "%s: no end frame relayed: %s" % (name, r)
    judge_dev(rig, "P-DPL-24", got, "pattern_at_26", "byte8")


def repeat_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        voice, _ = talk(talker, listener, voice=frames(84, "p25"))
        before, at = 0, []
        for p in listener.parsed():
            if p["kind"] == "voice":
                before += 1
            elif p["kind"] == "header":
                at.append(before)
        return {"header_after_frames": at, "exact": listener.heard() == voice}
    finally:
        done(talker, listener)


def test_dplus_late_listener(rig):
    """P-DPL-25 (diff): the egress header is repeated once per superframe,
    so a listener that missed the start (ircDDBGateway, DroidStar start
    only on a header) can pick the stream up."""
    got = both(rig, repeat_probe)
    print("P-DPL-25 header repetition:", got)
    assert len(got["ref"]["header_after_frames"]) >= 4, "ref: %s" % got["ref"]
    same(got, "header_after_frames")


def repeated_headers_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        voice, _ = talk(talker, listener, voice=frames(63, "p26"), heads=5, head_every=True)
        got = listener.parsed()
        return {"exact": listener.heard() == voice,
                "streams": len({p["stream_id"] for p in got if p["kind"] == "voice"})}
    finally:
        done(talker, listener)


def test_dplus_repeated_headers(rig):
    """P-DPL-26 (diff): five headers at the start (ircDDBGateway) and one
    per superframe (DroidStar) give one stream, every frame once."""
    got = both(rig, repeated_headers_probe)
    print("P-DPL-26 repeated headers:", got)
    assert got["ref"]["exact"] and got["ref"]["streams"] == 1, "ref: %s" % got["ref"]
    same(got, "exact", "streams")


def lost_end_probe(ip):
    a, b, listener = Peer(ip), Peer(ip, SECOND), Peer(ip, LISTENER)
    try:
        ready(listener, a, b)
        talk(a, listener, voice=frames(63, "p29a"), end=None, wait=2.2)  # past xlxd's 1.6 s
        a_end = first(listener, "end") is not None
        listener.clear()
        voice, _ = talk(b, listener, voice=frames(63, "p29b"))
        return {"a_end": a_end, "b_exact": listener.heard() == voice}
    finally:
        done(a, b, listener)


def test_dplus_lost_end(rig):
    """P-DPL-29 (spec, B3): a stream without its end frame is closed after
    the silence timeout and the module is free; whether listeners get an
    end frame: sut == ref under xlxd, the spec's "terminator" otherwise."""
    got = both(rig, lost_end_probe)
    print("P-DPL-29 lost end:", got)
    for name, r in got.items():
        assert r["b_exact"], "%s: module not free 2.2 s after a stream lost its end" % name
    spec = specs.rule(rig, "test_stream_timeout_frees_module")
    if rig.sut_profile == specs.DEFAULT:
        same(got, "a_end")
    elif spec is None:
        pytest.skip("spec pending: test_stream_timeout_frees_module")
    elif spec.get("terminator") == "all_egress":
        assert got["sut"]["a_end"], "sut: no end frame after the silence timeout"


# ---------------------------------------------------------------- 4 who hears what

def listen_probe(ip, join):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        listener.link(join=join)
        talker.link(join=False)
        time.sleep(CACHE)
        listener.clear()
        voice, _ = talk(talker, listener)
        heard = listener.heard()
        return {"heard": heard == voice, "received": len(heard)}
    finally:
        done(talker, listener)


def test_dplus_listen_before_transmit(rig):
    """P-DPL-30 (dev): a client that has linked but never transmitted hears
    the module it will talk on; every client filters by RPT2, so a server
    may send it every module. xlxd sends nothing until it has transmitted."""
    got = both(rig, listen_probe, False)
    print("P-DPL-30 listen before transmit:", got)
    judge_dev(rig, "P-DPL-30", got, "heard")


def test_dplus_listen_after_transmit(rig):
    """P-DPL-31 (diff): after one transmission on the module the client
    hears it."""
    got = both(rig, listen_probe, True)
    print("P-DPL-31 listen after transmit:", got)
    assert got["ref"]["heard"], "ref: %s" % got["ref"]
    same(got, "heard")


def two_ports_probe(ip):
    talker, a, b = Peer(ip), Peer(ip, LISTENER), Peer(ip, LISTENER)
    try:
        ready(a, b, talker)
        voice, _ = talk(talker, a, b)
        return {"a": a.heard() == voice, "b": b.heard() == voice}
    finally:
        done(talker, a, b)


def test_dplus_two_ports_one_ip(rig):
    """P-DPL-32 (diff): two links with the same callsign from one IP on two
    ports (one gateway's port pool, or NAT) are two clients; both hear."""
    got = both(rig, two_ports_probe)
    print("P-DPL-32 two ports:", got)
    assert got["ref"]["a"] and got["ref"]["b"], "ref: %s" % got["ref"]
    same(got, "a", "b")


# ---------------------------------------------------------------- 5 malformed

JUNK = {"tag": b"XXXX", "one": b"\x03", "keepalive_short": b"\x03\x60", "oversize": bytes(1400),
        "qnet_status": b"\x04\xc0\x03\x00", "header_57": None, "voice_28": None}


def malformed_probe(ip):
    p = Peer(ip)
    try:
        p.link(join=False)
        sid = clients.fresh_stream_id("dstar")
        junk = dict(JUNK, header_57=dplus.header_packet(sid, p.radio())[:57],
                    voice_28=dplus.voice(sid, 1, bytes(9))[:28])
        replies = {}
        for name, packet in junk.items():
            p.clear()
            p.send(packet)
            settle(p, seconds=0.5)
            other = [d.hex() for _, d, _ in p.log if d != dplus.KEEPALIVE]
            if other:
                replies[name] = other
        p.clear()
        p.send(dplus.CONNECT)
        alive = p.wait(lambda d: d == dplus.CONNECT, 1.0) is not None
        return {"replies": replies, "alive": alive}
    finally:
        done(p)


def test_dplus_malformed_ignored(rig):
    """P-DPL-34 (diff): unknown, short and oversize packets (also a
    QnetGateway status request) get no reply; the server keeps serving."""
    got = both(rig, malformed_probe)
    print("P-DPL-34 malformed:", got)
    assert got["ref"]["replies"] == {} and got["ref"]["alive"], "ref: %s" % got["ref"]
    same(got, "replies", "alive")


def no_login_probe(ip):
    stranger, listener = Peer(ip, THIRD), Peer(ip, LISTENER)
    try:
        ready(listener)
        talk(stranger, listener)
        return {"heard": len(listener.heard())}
    finally:
        done(stranger, listener)


def test_dplus_dv_without_login(rig):
    """P-DPL-35 (diff): voice from an IP:port that never logged in is not
    relayed."""
    got = both(rig, no_login_probe)
    print("P-DPL-35 DV without login:", got)
    assert got["ref"]["heard"] == 0, "ref: %s" % got["ref"]
    same(got, "heard")


def bad_my_probe(ip):
    talker, listener = Peer(ip), Peer(ip, LISTENER)
    try:
        ready(listener, talker)
        voice, _ = talk(talker, listener, radio=talker.radio(my="N0-CALL"))
        return {"heard": listener.heard() == voice}
    finally:
        done(talker, listener)


def test_dplus_header_bad_my(rig):
    """P-DPL-36 (dev): a header whose MY callsign is malformed."""
    got = both(rig, bad_my_probe)
    print("P-DPL-36 bad MY:", got)
    judge_dev(rig, "P-DPL-36", got, "heard")
