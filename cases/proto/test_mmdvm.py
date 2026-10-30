# SPDX-License-Identifier: MIT
"""MMDVM Homebrew / DMR master conformance (docs/protocols/mmdvm.md,
testlist K1): one case per obligation P-DMR-NN, xlxd-ref and xlxd-sut
probed in parallel, DMR clients only.

Judged as in mmdvm.md: hb = a spec requirement, asserted on both sides;
dev = disputed between masters (or xlxd deviates from the spec): sut ==
ref under the xlxd profile, else the profile's "mmdvm" spec entry
{"P-DMR-NN": {observed key: value}} (skip "spec pending" without one);
diff = sut == ref (a convention the masters agree on). Every case prints
what both sides did.
"""
import threading
import time

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import dmr

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "L", "O"
TALKER, LISTENER = 1023007, 1023008             # N0CALL, N0TEST
EXTRA = (1023101, 1023102)                      # STR001, STR002
UNKNOWN_ID = 0                                  # in no dmrid.dat
# P-DMR-43 only (rig/config/dmrid.dat, unused elsewhere): talker, gateway
# login id, RF user id behind the gateway
GW_TALKER, GW_LOGIN, GW_RF = 1023205, 1023206, 1023207  # STR105, STR106, STR107
# 18 bursts, three superframes. xlxd's short-stream guard (B18,
# cases/test_short_stream_guard.py) strikes a callsign after three short
# streams (< 50 frames) in a row on a module, each closing within 2 s of
# the previous one, and once it has a strike any three in a row do (300 s);
# strike 3 refuses its logins. The empty join streams of each probe are
# short already, so a talk of FRAMES must not be.
FRAMES = 54
GAP = 0.060                                     # one burst per 60 ms
CACHE = 1.2                                     # xlxd refreshes its module client list once per second
TAIL = 0.8


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
    profile's mmdvm spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        for key in keys:
            assert got["sut"][key] == got["ref"][key], "%s %s: sut %r, ref %r" % (
                nn, key, got["sut"][key], got["ref"][key])
        return
    entry = (specs.rule(rig, "mmdvm") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no mmdvm %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def frames(n=FRAMES, seed="mmdvm"):
    return signals.prng_frames("dmr", n, seed=seed)


# ---------------------------------------------------------------- clients

def new(dmrid=TALKER, module=MOD):
    return dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=module)


def tagged(tag):
    return lambda data: (dmr.parse_control(data) or {}).get("tag") == tag


def anything(data):
    return True


def wait_for(client, pred, seconds=1.0):
    """First inbox packet matching pred, polled up to seconds, or None."""
    deadline = time.monotonic() + seconds
    while True:
        client.client.drain()
        for data in client.client.inbox:
            if pred(data):
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def ask(client, packet, pred=anything, seconds=1.0):
    """Send packet with an empty inbox; the first reply matching pred."""
    clear(client)
    client.client.send(client.dest, packet)
    return wait_for(client, pred, seconds)


def login(ip, client, join=True):
    """RPTL, then RPTK with a zero hash (no RPTC); join the client's
    module with an empty stream. Returns the salt."""
    client.dest = (ip, dmr.PORT)
    ack = ask(client, dmr.rptl(client.id), tagged("RPTACK"))
    assert ack, "%s: no RPTACK to RPTL from %d" % (ip, client.id)
    assert ask(client, dmr.rptk(client.id), tagged("RPTACK")), "%s: no RPTACK to RPTK from %d" % (ip, client.id)
    if join:
        client._empty_stream(client.tg, 0.05)
    return dmr.parse_control(ack)["tail"]


def ready(ip, *made, join=True):
    for client in made:
        login(ip, client, join)
    time.sleep(CACHE)
    clear(*made)
    return made


def clear(*made):
    for client in made:
        client.client.drain()
        client.client.inbox.clear()
        client.client.inbox_times.clear()


def settle(*made, seconds=TAIL):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for client in made:
            client.client.drain()
        time.sleep(0.01)


def done(*made):
    """TG 4000 unlink from every client that has a destination, then close."""
    for client in made:
        try:
            if client.dest:
                client._empty_stream(4000, 0.0)
        finally:
            client.client.close()


def pong(client):
    return ask(client, dmr.rptping(client.id), tagged("MSTPONG")) is not None


def control(data):
    return dmr.parse_control(data) is not None


def reply_kind(data):
    """Tag of a master reply ("MSTNAK", "RPTACK", ...), "none" for no reply."""
    return "none" if data is None else (dmr.parse_control(data) or {"tag": "other"})["tag"]


# ---------------------------------------------------------------- streams

def stream(client, voice, sid, tg=None, seq0=0, src=None, head=True, end=True, sync=dmr.BS_DATA):
    """Header, voice bursts A-F (three frames each), terminator; the
    repeater id is the client's login id."""
    tg = tg or client.tg
    src = src or client.id
    out = []
    if head:
        out.append(dmr.packet(seq0, src, tg, client.id, 0xA1, sid, dmr.data_sync_payload(sync)))
    for index in range(0, len(voice), 3):
        trio = (list(voice[index:index + 3]) + [bytes(9)] * 2)[:3]
        vseq = (index // 3) % 6
        bits = dmr.bitfield(2, False, dmr.VOICE_SYNC if vseq == 0 else dmr.VOICE, vseq)
        out.append(dmr.packet(seq0 + len(out), src, tg, client.id, bits, sid, dmr.voice_payload(*trio)))
    if end:
        out.append(dmr.packet(seq0 + len(out), src, tg, client.id, 0xA2, sid, dmr.data_sync_payload()))
    return out


def retag(packets, clear_bits=0, add=0):
    """The packets with flags byte 15 changed."""
    out = []
    for data in packets:
        data = bytearray(data)
        data[15] = (data[15] & ~clear_bits & 0xFF) | add
        out.append(bytes(data))
    return out


def send(client, packets, *listeners, wait=TAIL):
    client.client.paced(client.dest, packets, GAP, *[m.client for m in listeners])
    settle(client, *listeners, seconds=wait)


def talk(client, *listeners, voice=None, wait=TAIL, **kw):
    """A complete stream of voice (default FRAMES frames); returns (voice, sid)."""
    voice = voice or frames()
    sid = clients.fresh_stream_id("dmr")
    send(client, stream(client, voice, sid, **kw), *listeners, wait=wait)
    return voice, sid


def received(client):
    return [d for d in map(dmr.parse, client.client.inbox) if d]


def kind(d):
    f = dmr.parse_bitfield(d["bitfield"])
    if f["frame_type"] == dmr.DATA:
        return {dmr.HEADER: "header", dmr.TERMINATOR: "terminator"}.get(f["vseq"], "data")
    return "voice"


def heard(client):
    """Voice frames of the inbox, in arrival order."""
    out = []
    for d in received(client):
        if kind(d) == "voice":
            out.extend(dmr.extract(d["payload"]))
    return out


def by_stream(client):
    """[[parsed DMRD of one egress stream id], ...] in order of arrival."""
    out = {}
    for d in received(client):
        out.setdefault(d["stream"], []).append(d)
    return list(out.values())


# ---------------------------------------------------------------- 1 login

def login_probe(ip):
    c = new()
    c.dest = (ip, dmr.PORT)
    try:
        ack = ask(c, dmr.rptl(c.id), tagged("RPTACK"))
        again = ask(c, dmr.rptl(c.id), tagged("RPTACK"))
        tail = dmr.parse_control(ack)["tail"] if ack else b""
        return {"ack": ack is not None, "salt_len": len(tail), "salt": tail.hex(),
                "same_salt_next_login": bool(again) and dmr.parse_control(again)["tail"] == tail}
    finally:
        c.dest = None
        done(c)


def test_mmdvm_login_ack_salt(rig):
    """P-DMR-01 (hb): RPTL from a known id -> RPTACK + 4-byte salt."""
    got = both(rig, login_probe)
    print("P-DMR-01 login:", got)
    for name, r in got.items():
        assert r["ack"] and r["salt_len"] == 4, "%s: RPTL answer %s" % (name, r)


def unknown_probe(ip):
    c = new(UNKNOWN_ID)
    c.dest = (ip, dmr.PORT)
    try:
        return {"reply": reply_kind(ask(c, dmr.rptl(c.id)))}
    finally:
        c.dest = None
        done(c)


def test_mmdvm_login_unknown_id(rig):
    """P-DMR-03 (dev): RPTL from an id the master does not know never
    logs in (no RPTACK); silence (xlxd) or MSTNAK is the dev part."""
    got = both(rig, unknown_probe)
    print("P-DMR-03 unknown id:", got)
    for name, r in got.items():
        assert r["reply"] != "RPTACK", "%s: unknown id got RPTACK" % name
    judge_dev(rig, "P-DMR-03", got, "reply")


def auth_probe(ip):
    c = new()
    c.dest = (ip, dmr.PORT)
    try:
        salt = ask(c, dmr.rptl(c.id), tagged("RPTACK"))
        salt = dmr.parse_control(salt)["tail"] if salt else b""
        zero = ask(c, dmr.rptk(c.id), lambda d: tagged("RPTACK")(d) or tagged("MSTNAK")(d))
        out = {"zero_hash": (dmr.parse_control(zero) or {}).get("tag", "none")}
        ask(c, dmr.rptl(c.id), tagged("RPTACK"))
        wrong = ask(c, dmr.rptk(c.id, dmr.auth_hash(salt, "not-the-password")),
                    lambda d: tagged("RPTACK")(d) or tagged("MSTNAK")(d))
        out["wrong_hash"] = (dmr.parse_control(wrong) or {}).get("tag", "none")
        return out
    finally:
        done(c)


def test_mmdvm_auth_zero_hash(rig):
    """P-DMR-04 (dev): RPTK completes the login. Lenient auth (zero hash
    acked) is the default expectation for every profile; a profile spec
    {"auth": "strict"} turns it round. The wrong-hash probe is recorded."""
    got = both(rig, auth_probe)
    print("P-DMR-04 auth:", got)
    entry = (specs.rule(rig, "mmdvm") or {}).get("P-DMR-04") or {"auth": "lenient"}
    assert got["ref"]["zero_hash"] == "RPTACK", "ref: zero hash %s" % got["ref"]["zero_hash"]
    want = "RPTACK" if entry["auth"] == "lenient" else "MSTNAK"
    assert got["sut"]["zero_hash"] == want, "sut: zero hash %s, %s auth wants %s" % (
        got["sut"]["zero_hash"], entry["auth"], want)
    if rig.sut_profile == specs.DEFAULT:
        same(got, "zero_hash")


def auth_first_probe(ip):
    c = new()
    c.dest = (ip, dmr.PORT)
    try:
        reply = reply_kind(ask(c, dmr.rptk(c.id), control))
        return {"reply": reply, "pong": pong(c)}
    finally:
        c.dest = None
        done(c)


def test_mmdvm_auth_without_login(rig):
    """P-DMR-05 (dev): RPTK without RPTL -> reply kind ("none", "MSTNAK",
    "RPTACK"), then an RPTPING -> MSTPONG or not (after a NAK there is no
    session). xlxd acks and logs the client in."""
    got = both(rig, auth_first_probe)
    print("P-DMR-05 RPTK first:", got)
    judge_dev(rig, "P-DMR-05", got, "reply", "pong")


def config_probe(ip):
    """RPTL, RPTK, RPTC (302 B), RPTO; the tail of each RPTACK after the
    RPTL one (whose tail is the salt): "id" (the 4-byte id, big endian), "bare",
    "none" (no RPTACK) or its hex."""
    c = new()
    c.dest = (ip, dmr.PORT)
    try:
        assert ask(c, dmr.rptl(c.id), tagged("RPTACK")), "%s: no RPTACK to RPTL" % ip
        tails = {}
        for name, packet in (("rptk", dmr.rptk(c.id)), ("rptc", dmr.rptc(c.id)),
                             ("rpto", dmr.rpto(c.id, "TS2=%d;" % c.tg))):
            ack = ask(c, packet, tagged("RPTACK"))
            tail = dmr.parse_control(ack)["tail"] if ack else None
            tails[name] = "none" if tail is None else "id" if tail == c.id.to_bytes(4, "big") else tail.hex() or "bare"
        return {"ack": tails["rptc"] != "none", "tails": tails,
                "ack_id": all(t == "id" for t in tails.values())}
    finally:
        done(c)


def test_mmdvm_config_ack(rig):
    """P-DMR-06 (hb + dev): RPTC of 302 B -> RPTACK (hb); whether every
    RPTACK of the login flow except the RPTL one (salt) carries the 4-byte
    id, big endian, after the tag (RPTK, RPTC and RPTO acks) is dev (xlxd:
    bare 6-byte RPTACK)."""
    got = both(rig, config_probe)
    print("P-DMR-06 RPTC:", got)
    for name, r in got.items():
        assert r["ack"], "%s: no RPTACK to RPTC" % name
    judge_dev(rig, "P-DMR-06", got, "ack_id")


def config_size_probe(ip):
    c = new()
    try:
        login(ip, c, join=False)
        data = dmr.rptc(c.id)
        out = {"ack_301": ask(c, data[:-1]) is not None, "ack_303": ask(c, data + b" ") is not None}
        out["pong"] = pong(c)
        return out
    finally:
        done(c)


def test_mmdvm_config_wrong_size(rig):
    """P-DMR-07 (dev): RPTC of 301 or 303 B; xlxd and URFd want exactly
    302 B, the HBlink family does not check the size."""
    got = both(rig, config_size_probe)
    print("P-DMR-07 RPTC size:", got)
    judge_dev(rig, "P-DMR-07", got, "ack_301", "ack_303", "pong")


def no_config_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice, _ = talk(talker, listener)
        return {"routed": heard(listener) == voice}
    finally:
        done(talker, listener)


def test_mmdvm_no_config_still_routed(rig):
    """P-DMR-08 (dev): voice from clients that never sent RPTC; xlxd and
    URFd route it, the HBlink family drops DMRD until RPTC."""
    got = both(rig, no_config_probe)
    print("P-DMR-08 no RPTC:", got)
    judge_dev(rig, "P-DMR-08", got, "routed")


def options_probe(ip):
    c = new()
    try:
        login(ip, c)
        ack = ask(c, dmr.rpto(c.id, "TS2=%d;" % c.tg), tagged("RPTACK"))
        return {"ack": ack is not None}
    finally:
        done(c)


def test_mmdvm_options_ack(rig):
    """P-DMR-09 (diff): RPTO -> RPTACK."""
    got = both(rig, options_probe)
    print("P-DMR-09 RPTO:", got)
    same(got, "ack")


def relogin_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        login(ip, listener, join=False)
        clear(listener)
        listener.client.send(listener.dest, dmr.rptping(listener.id))
        settle(listener, seconds=0.5)
        pongs = sum(1 for p in listener.client.inbox if tagged("MSTPONG")(p))
        time.sleep(CACHE)
        clear(listener)
        voice, _ = talk(talker, listener)
        got = heard(listener)
        return {"pongs": pongs, "frames": len(got), "once": got == voice}
    finally:
        done(talker, listener)


def test_mmdvm_relogin_single_session(rig):
    """P-DMR-10 (diff): RPTL/RPTK again from the same id and address: one
    MSTPONG per RPTPING (one session) and the module's traffic once."""
    got = both(rig, relogin_probe)
    print("P-DMR-10 re-login:", got)
    same(got, "pongs", "once")


# ---------------------------------------------------------------- 2 keepalive and close

def ping_probe(ip):
    c = new()
    try:
        login(ip, c, join=False)
        reply = ask(c, dmr.rptping(c.id), tagged("MSTPONG"))
        tail = dmr.parse_control(reply)["tail"] if reply else b""
        order = {c.id.to_bytes(4, "big"): "be", c.id.to_bytes(4, "little"): "le"}.get(tail, "other")
        return {"pong": reply is not None, "order": order if reply else None, "tail": tail.hex()}
    finally:
        done(c)


def test_mmdvm_ping_pong(rig):
    """P-DMR-11 (dev): RPTPING -> MSTPONG + id (MUST); the id byte order
    is the dev part (xlxd: host order, little endian)."""
    got = both(rig, ping_probe)
    print("P-DMR-11 ping:", got)
    for name, r in got.items():
        assert r["pong"], "%s: no MSTPONG" % name
    judge_dev(rig, "P-DMR-11", got, "order")


def ping_stranger_probe(ip):
    c = new(LISTENER)
    c.dest = (ip, dmr.PORT)
    try:
        return {"reply": reply_kind(ask(c, dmr.rptping(c.id), control))}
    finally:
        c.dest = None
        done(c)


def test_mmdvm_ping_not_logged_in(rig):
    """P-DMR-12 (dev): RPTPING from an address that is not logged in never
    gets MSTPONG; the reply kind ("none" on xlxd, MSTNAK per HB15) is dev."""
    got = both(rig, ping_stranger_probe)
    print("P-DMR-12 ping, not logged in:", got)
    for name, r in got.items():
        assert r["reply"] != "MSTPONG", "%s: MSTPONG to a client that is not logged in" % name
    judge_dev(rig, "P-DMR-12", got, "reply")


SILENCE_S = 125


def silence_scenario(ip):
    """keeper and talker ping every 10 s, silent never does; wait for the
    master's MSTCL to silent (up to SILENCE_S), then the talker talks."""
    keeper, silent, talker = new(LISTENER), new(EXTRA[0]), new(TALKER)
    try:
        ready(ip, keeper, silent, talker)
        start = time.monotonic()
        last_ping = mstcl_at = None
        while time.monotonic() - start < SILENCE_S:
            now = time.monotonic()
            if last_ping is None or now - last_ping >= 10:
                for c in (keeper, talker):
                    c.client.send(c.dest, dmr.rptping(c.id))
                last_ping = now
            settle(keeper, silent, talker, seconds=0.5)
            if mstcl_at is None and any(tagged("MSTCL")(p) for p in silent.client.inbox):
                mstcl_at = time.monotonic() - start
            if mstcl_at is not None and time.monotonic() - start > mstcl_at + 2 * CACHE:
                break
        keeper_mstcl = any(tagged("MSTCL")(p) for p in keeper.client.inbox)
        clear(keeper, silent, talker)
        voice, _ = talk(talker, keeper, silent)
        out = {"mstcl_s": None if mstcl_at is None else round(mstcl_at, 1),
               "silent_heard": len(heard(silent)), "keeper_heard": heard(keeper) == voice,
               "keeper_mstcl": keeper_mstcl, "keeper_pong": pong(keeper)}
        # the dropped client's next packet: an RPTPING, then (no reply) a
        # lone terminator DMRD, which opens no stream on the module
        reply = ask(silent, dmr.rptping(silent.id), control)
        if reply is None:
            end = dmr.packet(0, silent.id, silent.tg, silent.id, 0xA2, clients.fresh_stream_id("dmr"),
                             dmr.data_sync_payload())
            reply = ask(silent, end, control)
        out["next_after_drop"] = reply_kind(reply)
        return out
    finally:
        done(keeper, silent, talker)


@pytest.fixture(scope="module")
def silence(rig):
    got = both(rig, silence_scenario)
    print("silent client scenario:", got)
    return got


@pytest.mark.slow
def test_mmdvm_silent_client_dropped(rig, silence):
    """P-DMR-13 (dev): what a client silent for the server timeout gets
    (xlxd: MSTCL after 100 s; other masters send MSTNAK, nothing or an
    empty datagram; the timeout value is informative), and the reply kind
    to its next packet after the drop (RPTPING, else a DMRD)."""
    got = {n: {"mstcl": r["mstcl_s"] is not None, "next_after_drop": r["next_after_drop"]}
           for n, r in silence.items()}
    print("P-DMR-13 MSTCL after s:", {n: r["mstcl_s"] for n, r in silence.items()}, got)
    judge_dev(rig, "P-DMR-13", got, "mstcl", "next_after_drop")


@pytest.mark.slow
def test_mmdvm_dropped_client_no_voice(rig, silence):
    """P-DMR-14 (diff): the dropped client gets no further voice."""
    print("P-DMR-14 frames to the dropped client:", {n: r["silent_heard"] for n, r in silence.items()})
    assert (silence["sut"]["silent_heard"] > 0) == (silence["ref"]["silent_heard"] > 0), silence


@pytest.mark.slow
def test_mmdvm_keepalive_isolated(rig, silence):
    """P-DMR-40 (diff): the pinging client keeps its session while another
    client is dropped."""
    print("P-DMR-40 keeper:", {n: {k: r[k] for k in ("keeper_heard", "keeper_mstcl", "keeper_pong")}
                               for n, r in silence.items()})
    same(silence, "keeper_heard", "keeper_mstcl", "keeper_pong")


def rptcl_probe(ip, size):
    closer, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, closer, talker)
        closer.client.send(closer.dest, dmr.rptcl(closer.id, size))
        time.sleep(1.5)                     # past xlxd's 1 s client cache
        clear(closer, talker)
        talk(talker, closer)
        return {"heard": len(heard(closer)) > 0, "pong": pong(closer)}
    finally:
        done(closer, talker)


@pytest.mark.parametrize("size", [13, 9])
def test_mmdvm_rptcl_close(rig, size):
    """P-DMR-15 (dev): after RPTCL the client gets no further traffic.
    xlxd ignores both forms (sut == ref); another profile follows its
    test_dmr_rptcl_closes_session entry ({"rptcl": "close"}, B15)."""
    got = both(rig, rptcl_probe, size)
    print("P-DMR-15 RPTCL %d B:" % size, got)
    if rig.sut_profile == specs.DEFAULT:
        same(got, "heard")
        return
    spec = specs.rule(rig, "test_dmr_rptcl_closes_session")
    if spec is None:
        pytest.skip("spec pending: no test_dmr_rptcl_closes_session entry for %s" % rig.sut_profile)
    if spec.get("rptcl") == "close":
        assert not got["sut"]["heard"], "sut sent voice after RPTCL %d B" % size


def rptintr_probe(ip):
    c = new()
    try:
        login(ip, c, join=False)
        reply = ask(c, b"RPTINTR" + ("%08X" % c.id).encode() + b"00", seconds=0.5)
        return {"reply": reply is not None, "pong": pong(c)}
    finally:
        done(c)


def test_mmdvm_rptintr_ignored(rig):
    """P-DMR-16 (diff): RPTINTR (17 B) gets no answer, the login stays."""
    got = both(rig, rptintr_probe)
    print("P-DMR-16 RPTINTR:", got)
    same(got, "reply", "pong")


# ---------------------------------------------------------------- 3 malformed

GARBAGE = {
    "unknown": b"XXXX" + TALKER.to_bytes(4, "big") + bytes(8),
    "short3": b"RPT",
    "rptl7": dmr.rptl(TALKER)[:7],
    "oversize": b"DMRD" + bytes(1396),
}


def garbage_probe(ip, name):
    c = new()
    try:
        login(ip, c, join=False)
        reply = ask(c, GARBAGE[name], seconds=0.5)
        return {"reply": reply is not None, "pong": pong(c)}
    finally:
        done(c)


@pytest.mark.parametrize("name", list(GARBAGE))
def test_mmdvm_garbage_ignored(rig, name):
    """P-DMR-17 (diff): unknown, short and oversize packets get no answer
    and the master keeps serving (the client still gets MSTPONG)."""
    got = both(rig, garbage_probe, name)
    print("P-DMR-17 %s:" % name, got)
    same(got, "reply", "pong")


def wrong_size_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        packets = stream(talker, frames(), clients.fresh_stream_id("dmr"))
        packets[1:-1] = [p[:54] if i % 2 else p + b"\x00" for i, p in enumerate(packets[1:-1])]
        send(talker, packets, listener)
        return {"voice": len(heard(listener)), "dmrd": len(received(listener))}
    finally:
        done(talker, listener)


def test_mmdvm_dmrd_wrong_size(rig):
    """P-DMR-18 (dev): voice bursts of 54 and 56 B (xlxd and URFd want
    exactly 55 B; HBlink4 >= 55, FreeDMR >= 53, HBlink3 any size)."""
    got = both(rig, wrong_size_probe)
    print("P-DMR-18 DMRD 54/56 B:", got)
    judge_dev(rig, "P-DMR-18", {n: {"relayed": r["voice"] > 0} for n, r in got.items()}, "relayed")


STRANGER_BURST = 20                             # DMRD: a header + 19 voice bursts
STRANGER_GAP = 0.090                            # 20 packets in 1.8 s
STRANGER_SETTLE = 2.0


def per_window_max(times, window=10.0):
    """Most of times (seconds, sorted) inside any window."""
    return max((sum(1 for u in times[i:] if u - t < window) for i, t in enumerate(times)), default=0)


def stranger_probe(ip):
    """A burst of 20 DMRD within 2 s (header + voice, module TG) from an
    address that never logged in, a logged-in listener on the module."""
    stranger, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener)
        stranger.dest = (ip, dmr.PORT)
        voice = frames(3 * (STRANGER_BURST - 1), "p19")
        packets = stream(stranger, voice, clients.fresh_stream_id("dmr"), end=False)
        assert len(packets) == STRANGER_BURST
        stranger.client.paced(stranger.dest, packets, STRANGER_GAP, listener.client)
        settle(stranger, listener, seconds=STRANGER_SETTLE)
        naks = [t for d, t in zip(stranger.client.inbox, stranger.client.inbox_times) if tagged("MSTNAK")(d)]
        return {"dmrd": len(received(listener)), "relayed": len(received(listener)) > 0,
                "nak": bool(naks), "nak_count": len(naks), "nak_per_10s_max": per_window_max(naks)}
    finally:
        stranger.dest = None
        done(stranger, listener)


def test_mmdvm_dmrd_not_logged_in(rig):
    """P-DMR-19 (hb + dev): DMRD from an address that is not logged in is
    not relayed (hb: nothing reaches the module's listener); whether it
    gets MSTNAK, and at most how many per 10 s for a 20-DMRD burst, is dev
    (xlxd: silence; HB15 dead link: MSTNAK)."""
    got = both(rig, stranger_probe)
    print("P-DMR-19 DMRD, not logged in:", got)
    for name, r in got.items():
        assert r["dmrd"] == 0, "%s relayed %d packets of a stranger" % (name, r["dmrd"])
    judge_dev(rig, "P-DMR-19", got, "relayed", "nak", "nak_per_10s_max")


# ---------------------------------------------------------------- 4 DMRD layout

def flagged_probe(ip, clear_bits, add, group_header=False):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        packets = retag(stream(talker, frames(), clients.fresh_stream_id("dmr")), clear_bits, add)
        if group_header:
            packets[0] = retag(packets[:1], 0x40)[0]
        send(talker, packets, listener)
        return {"voice": len(heard(listener)), "dmrd": len(received(listener))}
    finally:
        done(talker, listener)


def test_mmdvm_slot1_ignored(rig):
    """P-DMR-20 (diff): header, voice and terminator on slot 1."""
    got = both(rig, flagged_probe, 0x80, 0)
    print("P-DMR-20 slot 1:", got)
    assert (got["sut"]["dmrd"] > 0) == (got["ref"]["dmrd"] > 0), got


def test_mmdvm_private_call_not_relayed(rig):
    """P-DMR-21 (diff): private-call voice after a group header, and a
    whole private-call stream."""
    got = {"voice": both(rig, flagged_probe, 0, 0x40, True), "stream": both(rig, flagged_probe, 0, 0x40)}
    print("P-DMR-21 private call:", got)
    for part in ("voice", "stream"):
        assert (got[part]["sut"]["voice"] > 0) == (got[part]["ref"]["voice"] > 0), got


def sync_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    out = {}
    try:
        ready(ip, listener, talker)
        for name, sync in (("ms_data", dmr.MS_DATA), ("bs_voice", dmr.BS_VOICE)):
            voice = frames(seed="p22" + name)
            send(talker, stream(talker, voice, clients.fresh_stream_id("dmr"), sync=sync), listener)
            out[name] = len(heard(listener))
            out[name + "_exact"] = heard(listener) == voice
            clear(listener, talker)
        return out
    finally:
        done(talker, listener)


def test_mmdvm_header_sync_required(rig):
    """P-DMR-22 (hb + dev): a header with MS data sync opens the stream
    (hb); whether the voice after a header with the voice sync pattern is
    relayed is dev (xlxd: no; URFd opens on the voice, HBlink does not
    check the sync)."""
    got = both(rig, sync_probe)
    print("P-DMR-22 header sync:", got)
    for name, r in got.items():
        assert r["ms_data_exact"], "%s: stream after an MS data sync header: %d frames" % (name, r["ms_data"])
    judge_dev(rig, "P-DMR-22", {n: {"voice_sync_header_relayed": r["bs_voice"] > 0} for n, r in got.items()},
              "voice_sync_header_relayed")


def exact_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice, _ = talk(talker, listener, voice=frames(60, "p23"))
        got = heard(listener)
        return {"frames": len(got), "exact": got == voice}
    finally:
        done(talker, listener)


def test_mmdvm_superframe_relayed_exact(rig):
    """P-DMR-23 (diff): bursts A-F of three AMBE+2 frames are relayed with
    every frame unchanged."""
    got = both(rig, exact_probe)
    print("P-DMR-23 frames:", got)
    same(got, "exact")


def terminator_probe(ip):
    a, b, listener = new(TALKER), new(EXTRA[0]), new(LISTENER)
    try:
        ready(ip, listener, a, b)
        talk(a, listener, b, wait=0.1)
        voice_b, _ = talk(b, listener, a, voice=frames(seed="p24b"))
        streams = by_stream(listener)
        return {"streams": len(streams), "a_terminated": bool(streams) and kind(streams[0][-1]) == "terminator",
                "b_exact": len(streams) > 1 and [f for d in streams[1] if kind(d) == "voice"
                                                  for f in dmr.extract(d["payload"])] == voice_b}
    finally:
        done(a, b, listener)


def test_mmdvm_terminator_ends_stream(rig):
    """P-DMR-24 (hb): the terminator ends the stream: listeners get it and a
    second talker 100 ms later is relayed in full."""
    got = both(rig, terminator_probe)
    print("P-DMR-24 terminator:", got)
    for name, r in got.items():
        assert r["a_terminated"] and r["b_exact"], "%s: %s" % (name, r)


def egress_probe(ip, src=None):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        _, sid = talk(talker, listener, src=src)
        streams = by_stream(listener)
        packets = streams[0] if streams else []
        return {"sid": sid, "streams": len(streams), "packets": packets}
    finally:
        done(talker, listener)


def structure_ok(packets):
    """Header, voice A-F (A as voice sync) in order, terminator; slot 2,
    group call, one stream id."""
    if len(packets) < 3 or kind(packets[0]) != "header" or kind(packets[-1]) != "terminator":
        return False
    for index, d in enumerate(packets[1:-1]):
        f = dmr.parse_bitfield(d["bitfield"])
        if f["vseq"] != index % 6 or f["frame_type"] != (dmr.VOICE_SYNC if index % 6 == 0 else dmr.VOICE):
            return False
    return all(d["bitfield"] & 0xC0 == 0x80 for d in packets) and len({d["stream"] for d in packets}) == 1


def test_mmdvm_egress_structure(rig):
    """P-DMR-25 (hb + diff): egress structure is a MUST on both sides; the
    egress destination TG (xlxd and URFd: always 9, which DMRGateway's XLX
    mode requires) is sut == ref."""
    raw = both(rig, egress_probe)
    got = {n: {"structure": structure_ok(r["packets"]), "count": len(r["packets"]),
               "dst": sorted({d["dst"] for d in r["packets"]})} for n, r in raw.items()}
    print("P-DMR-25 egress:", got)
    for name, r in got.items():
        assert r["structure"], "%s: egress structure broken (%d packets)" % (name, r["count"])
    same(got, "dst")


def test_mmdvm_egress_sequence(rig):
    """P-DMR-26 (diff): the egress sequence byte counts up per stream."""
    raw = both(rig, egress_probe)
    got = {}
    for name, r in raw.items():
        seqs = [d["seq"] for d in r["packets"]]
        got[name] = {"first": seqs[0] if seqs else None,
                     "consecutive": bool(seqs) and all((b - a) % 256 == 1 for a, b in zip(seqs, seqs[1:]))}
    print("P-DMR-26 egress seq:", got)
    same(got, "first", "consecutive")


def test_mmdvm_egress_ids(rig):
    """P-DMR-27 (diff + dev): egress source id (sut == ref) and repeater id
    (dev: "source", "login" or "other") for a radio id (STR002) that
    differs from the talker's login (repeater) id."""
    raw = both(rig, egress_probe, EXTRA[1])
    got = {n: {"src": sorted({d["src"] for d in r["packets"]}),
               "repeater": sorted({d["repeater"] for d in r["packets"]})} for n, r in raw.items()}
    for r in got.values():
        r["repeater_is"] = {(EXTRA[1],): "source", (TALKER,): "login"}.get(tuple(r["repeater"]), "other")
    print("P-DMR-27 egress ids (talker %d, radio %d):" % (TALKER, EXTRA[1]), got)
    same(got, "src")
    judge_dev(rig, "P-DMR-27", got, "repeater_is")


def fold(sid):
    return (sid ^ (sid >> 16)) & 0xFFFF or 1


def test_mmdvm_egress_stream_id(rig):
    """P-DMR-28 (dev): egress stream id. xlxd profile: sut == ref; another
    profile that lists "dmr" in its sid_kept entry must keep the id (B10)."""
    raw = both(rig, egress_probe)
    got = {}
    for name, r in raw.items():
        seen = sorted({d["stream"] for d in r["packets"]})
        got[name] = {"kept": seen == [r["sid"]], "folded": seen == [fold(r["sid"])], "seen": seen}
    print("P-DMR-28 egress sid:", got)
    if rig.sut_profile == specs.DEFAULT:
        same(got, "kept")
        return
    kept = specs.rule(rig, "sid_kept")
    if kept is None:
        pytest.skip("spec pending: no sid_kept entry in dvtest/specs/%s.py" % rig.sut_profile)
    if "dmr" in kept:
        assert got["sut"]["kept"], "sut egress sid %s, ingress %d" % (got["sut"]["seen"], raw["sut"]["sid"])


# ---------------------------------------------------------------- 5 talkgroups

def tg_link_probe(ip, module):
    joiner, talker = new(LISTENER, module), new(TALKER, module)
    try:
        ready(ip, talker)
        login(ip, joiner, join=False)
        joiner._empty_stream(dmr.module_tg(module), 0.05)
        time.sleep(CACHE)
        clear(joiner, talker)
        voice, _ = talk(talker, joiner)
        return {"linked": heard(joiner) == voice}
    finally:
        done(joiner, talker)


@pytest.mark.parametrize("module", ["A", "Z"])
def test_mmdvm_tg_links_module(rig, module):
    """P-DMR-29 (diff): a header to TG 4001 / 4026 links module A / Z."""
    got = both(rig, tg_link_probe, module)
    print("P-DMR-29 TG %d:" % dmr.module_tg(module), got)
    same(got, "linked")


def unlink_probe(ip):
    leaver, control, talker = new(LISTENER), new(EXTRA[0]), new(TALKER)
    try:
        ready(ip, leaver, control, talker)
        leaver._empty_stream(4000, 0.05)
        time.sleep(1.5)                     # past xlxd's 1 s client cache
        clear(leaver, control, talker)
        talk(talker, leaver, control)
        return {"leaver_heard": len(heard(leaver)) > 0, "control_heard": len(heard(control)) > 0}
    finally:
        done(leaver, control, talker)


def test_mmdvm_tg4000_unlinks(rig):
    """P-DMR-30 (diff): a header to TG 4000 unlinks; the client gets no
    further voice (a client still linked does)."""
    got = both(rig, unlink_probe)
    print("P-DMR-30 TG 4000:", got)
    for name, r in got.items():
        assert r["control_heard"], "%s: linked control listener heard nothing" % name
    same(got, "leaver_heard")


def other_tg_probe(ip):
    talker, own, other = new(TALKER), new(LISTENER), new(EXTRA[0], OTHER)
    try:
        ready(ip, own, other, talker)
        talk(talker, own, other, tg=dmr.module_tg(OTHER))
        return {"own": len(heard(own)) > 0, "other": len(heard(other)) > 0}
    finally:
        done(talker, own, other)


def test_mmdvm_other_tg_stays_on_module(rig):
    """P-DMR-31 (diff): a client linked to L talking to O's TG."""
    got = both(rig, other_tg_probe)
    print("P-DMR-31 other TG:", got)
    same(got, "own", "other")


def tg9_probe(ip, linked):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener)
        ready(ip, talker, join=linked)
        talk(talker, listener, tg=9)
        return {"heard": len(heard(listener)) > 0}
    finally:
        done(talker, listener)


@pytest.mark.parametrize("state", ["linked", "unlinked"])
def test_mmdvm_tg9_routing(rig, state):
    """P-DMR-32 (diff): TG 9 goes to the linked module; from an unlinked
    client it goes nowhere."""
    got = both(rig, tg9_probe, state == "linked")
    print("P-DMR-32 TG 9 %s:" % state, got)
    same(got, "heard")


def join_probe(ip):
    joiner, listener, talker = new(EXTRA[0]), new(LISTENER), new(TALKER)
    try:
        ready(ip, listener, talker)
        login(ip, joiner, join=False)
        clear(listener)
        joiner._empty_stream(joiner.tg, 0.05)
        time.sleep(CACHE)
        join_seen = {"voice": len(heard(listener)), "dmrd": len(received(listener))}
        clear(joiner, listener, talker)
        voice, _ = talk(talker, joiner, listener)
        return {"joined": heard(joiner) == voice, "join_voice": join_seen["voice"],
                "join_dmrd": join_seen["dmrd"]}
    finally:
        done(joiner, listener, talker)


def test_mmdvm_join_by_empty_stream(rig):
    """P-DMR-33 (diff): header + terminator is enough to join; the join
    stream carries no voice to the module's listeners."""
    got = both(rig, join_probe)
    print("P-DMR-33 join:", got)
    same(got, "joined", "join_voice")


def gateway_burst(client, tg, src):
    """DMRGateway's link / unlink form, back to back on a new stream id."""
    for data in dmr.gateway_link(src, tg, client.id, clients.fresh_stream_id("dmr")):
        client.client.send(client.dest, data)


def gateway_link_probe(ip, rf_src):
    """The joiner (unlinked) links to MOD with the private-call burst, a
    talker on MOD talks; the joiner unlinks with the private TG 4000
    burst, the talker talks again (a linked control must hear it). src =
    the gateway's login id (startup / relink form) or an RF user's id
    (user-control form). The talker and the gateway have their own ids
    (GW_*), so the strike state of TALKER / LISTENER never reaches them."""
    joiner, control, talker = new(GW_LOGIN), new(EXTRA[0]), new(GW_TALKER)
    src = GW_RF if rf_src else GW_LOGIN
    try:
        ready(ip, control, talker)
        login(ip, joiner, join=False)
        clear(control, talker)
        gateway_burst(joiner, dmr.module_tg(MOD), src)
        time.sleep(CACHE)
        link_dmrd = len(received(control)) + len(received(talker))
        clear(joiner, control, talker)
        voice, _ = talk(talker, joiner, control)
        linked = heard(joiner) == voice
        gateway_burst(joiner, 4000, src)
        time.sleep(1.5)                     # past xlxd's 1 s client cache
        clear(joiner, control, talker)
        talk(talker, joiner, control, voice=frames(seed="p43u"))
        return {"linked": linked, "link_dmrd": link_dmrd, "after_unlink": len(heard(joiner)),
                "control_heard": len(heard(control)) > 0}
    finally:
        done(joiner, control, talker)


@pytest.mark.parametrize("src", ["login_id", "rf_id"])
def test_mmdvm_gateway_private_link(rig, src):
    """P-DMR-43 (diff): DMRGateway's XLX link / unlink form (3 private-call
    headers + 2 terminators, one stream id, no voice) to TG 4001+ links
    the module, to TG 4000 unlinks; nothing of the burst reaches the
    module's listeners."""
    got = both(rig, gateway_link_probe, src == "rf_id")
    print("P-DMR-43 gateway private link (%s):" % src, got)
    for name, r in got.items():
        assert r["control_heard"], "%s: linked control listener heard nothing" % name
    same(got, "linked", "after_unlink", "link_dmrd")


# ---------------------------------------------------------------- 6 stream lifetime

def lost_terminator_probe(ip):
    a, b, listener = new(TALKER), new(EXTRA[0]), new(LISTENER)
    try:
        ready(ip, listener, a, b)
        send(a, stream(a, frames(seed="p34a"), clients.fresh_stream_id("dmr"), end=False), listener, b)
        settle(listener, a, b, seconds=2.2)         # past xlxd's 1.6 s silence timeout
        a_terminator = any(kind(d) == "terminator" for d in received(listener))
        clear(listener, a, b)
        voice_b, _ = talk(b, listener, a, voice=frames(seed="p34b"))
        return {"a_terminator": a_terminator, "b_exact": heard(listener) == voice_b}
    finally:
        done(a, b, listener)


def test_mmdvm_lost_terminator_timeout(rig, behaviour_spec):
    """P-DMR-34 (spec, B3): a stream without a terminator ends after the
    silence timeout and the module is free (both sides). Whether listeners
    get a terminator: sut == ref under the xlxd profile, the spec's
    "terminator" otherwise."""
    got = both(rig, lost_terminator_probe)
    print("P-DMR-34 lost terminator:", got)
    for name, r in got.items():
        assert r["b_exact"], "%s: module not free 2.2 s after a stream lost its terminator" % name
    if behaviour_spec is None:
        same(got, "a_terminator")
    elif behaviour_spec.get("terminator") == "all_egress":
        assert got["sut"]["a_terminator"], "sut: no terminator after the silence timeout"


def busy_probe(ip):
    a, b, listener = new(TALKER), new(EXTRA[0]), new(LISTENER)
    try:
        ready(ip, listener, a, b)
        voice_a, voice_b = frames(60, "p35a"), frames(27, "p35b")
        packets_a = stream(a, voice_a, clients.fresh_stream_id("dmr"))
        packets_b = stream(b, voice_b, clients.fresh_stream_id("dmr"))
        start = time.monotonic() + 0.05
        for tick, packet in enumerate(packets_a):
            pace(start, tick, GAP)
            a.client.send(a.dest, packet)
            if 5 <= tick < 5 + len(packets_b):
                b.client.send(b.dest, packets_b[tick - 5])
            listener.client.drain()
        settle(listener, a, b, seconds=1.5)
        got = heard(listener)
    finally:
        done(a, b, listener)
    set_a, set_b = set(voice_a), set(voice_b)
    at = [i for i, f in enumerate(got) if f in set_a]
    return {"a_exact": [got[i] for i in at] == voice_a,
            "mixed": bool(at) and any(f in set_b for f in got[at[0]:at[-1]]),
            "b_total": sum(f in set_b for f in got)}


def test_mmdvm_busy_module_no_mix(rig, behaviour_spec):
    """P-DMR-35 (spec, B1/B2): a second header on a busy module: the first
    stream arrives whole and nothing of the second is mixed into it. A
    profile with late_entry "deny" must relay none of the second."""
    got = both(rig, busy_probe)
    print("P-DMR-35 busy module:", got)
    for name, r in got.items():
        assert r["a_exact"] and not r["mixed"], "%s: %s" % (name, r)
    if (behaviour_spec or {}).get("late_entry") == "deny":
        assert got["sut"]["b_total"] == 0, "sut relayed %d frames of the late talker" % got["sut"]["b_total"]


def headless_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        send(talker, stream(talker, frames(), clients.fresh_stream_id("dmr"), head=False), listener)
        voiced = [[kind(d) for d in s] for s in by_stream(listener) if any(kind(d) == "voice" for d in s)]
        kinds = voiced[0] if voiced else []
        relayed = len(heard(listener)) > 0
        # a voice LC header (data sync, data type 1) before the first voice burst
        header_first = (kinds[0] == "header") if relayed else None
        return {"voice": len(heard(listener)), "relayed": relayed, "header_first": header_first,
                "first_kinds": kinds[:3]}
    finally:
        done(talker, listener)


def test_mmdvm_voice_without_header(rig):
    """P-DMR-36 (dev): voice bursts whose stream id had no header (xlxd:
    not relayed; URFd and HBlink open the stream on late entry); when
    relayed, whether the listener gets a voice LC header before the first
    voice burst (a non-xlxd profile must send it, B16)."""
    got = both(rig, headless_probe)
    print("P-DMR-36 no header:", got)
    if rig.sut_profile == specs.DEFAULT:
        judge_dev(rig, "P-DMR-36", got, "relayed", "header_first")
        return
    entry = (specs.rule(rig, "mmdvm") or {}).get("P-DMR-36") or {}
    if got["sut"]["relayed"]:
        assert got["sut"]["header_first"], "sut: late entry relayed without a header first: %s" % (
            got["sut"]["first_kinds"],)
    judge_dev(rig, "P-DMR-36", got, *(["relayed"] + (["header_first"] if "header_first" in entry else [])))


def echo_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice, _ = talk(talker, listener)
        return {"echo": len(received(talker)), "listener_exact": heard(listener) == voice}
    finally:
        done(talker, listener)


def test_mmdvm_no_self_echo(rig):
    """P-DMR-37 (diff): the talker never gets its own stream back."""
    got = both(rig, echo_probe)
    print("P-DMR-37 self echo:", got)
    for name, r in got.items():
        assert r["listener_exact"], "%s: listener did not get the stream" % name
    assert (got["sut"]["echo"] > 0) == (got["ref"]["echo"] > 0), got


DATA_TYPES = {"csbk": dmr.CSBK, "data_header": dmr.DATA_HEADER, "rate12": dmr.RATE12, "rate34": dmr.RATE34}


def data_probe(ip, data_type):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        sid = clients.fresh_stream_id("dmr")
        flags = dmr.bitfield(2, False, dmr.DATA, data_type)
        packets = [dmr.packet(i, talker.id, talker.tg, talker.id, flags, sid, dmr.data_sync_payload())
                   for i in range(4)]
        packets += stream(talker, frames(), sid, seq0=4, head=False)
        send(talker, packets, listener)
        return {"dmrd": len(received(listener)), "voice": len(heard(listener))}
    finally:
        done(talker, listener)


@pytest.mark.parametrize("name", list(DATA_TYPES))
def test_mmdvm_data_burst_not_voice(rig, name):
    """P-DMR-41 (dev): four data bursts of one data type, then voice under
    the same stream id (xlxd: nothing relayed; URFd opens on the voice,
    HBlink3 and FreeDMR forward data bursts)."""
    got = both(rig, data_probe, DATA_TYPES[name])
    print("P-DMR-41 %s:" % name, got)
    judge_dev(rig, "P-DMR-41", {n: {"relayed": r["dmrd"] > 0} for n, r in got.items()}, "relayed")


def wrap_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice, _ = talk(talker, listener, voice=frames(120, "p42"), seq0=230)
        streams = by_stream(listener)
        return {"exact": heard(listener) == voice, "streams": len(streams),
                "headers": sum(kind(d) == "header" for d in received(listener))}
    finally:
        done(talker, listener)


def test_mmdvm_sequence_wrap(rig):
    """P-DMR-42 (hb): an over whose sequence byte wraps 255 -> 0 (header at
    230, 40 bursts) arrives whole as one stream."""
    got = both(rig, wrap_probe)
    print("P-DMR-42 seq wrap:", got)
    for name, r in got.items():
        assert r["exact"] and r["streams"] == 1 and r["headers"] == 1, "%s: %s" % (name, r)


# ---------------------------------------------------------------- 7 several clients

def two_listeners_probe(ip, second_id):
    talker, first, second = new(TALKER), new(LISTENER), new(second_id)
    try:
        ready(ip, first, second, talker)
        voice, _ = talk(talker, first, second)
        return {"first_once": heard(first) == voice, "second_once": heard(second) == voice,
                "frames": [len(heard(first)), len(heard(second))]}
    finally:
        done(talker, first, second)


def test_mmdvm_two_ids_one_ip(rig):
    """P-DMR-38 (diff): two ids from one IP are two sessions, each gets the
    module's traffic once."""
    got = both(rig, two_listeners_probe, EXTRA[0])
    print("P-DMR-38 two ids:", got)
    same(got, "first_once", "second_once")


def test_mmdvm_same_id_two_ports(rig):
    """P-DMR-39 (dev): one id from two ports (xlxd and URFd: two sessions,
    traffic once each; the HBlink family keeps one session per id)."""
    got = both(rig, two_listeners_probe, LISTENER)
    print("P-DMR-39 same id, two ports:", got)
    judge_dev(rig, "P-DMR-39", got, "first_once", "second_once")


# ---------------------------------------------------------------- restart (last: it restarts both masters)

def salt_of(ip):
    c = new()
    c.dest = (ip, dmr.PORT)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ack = ask(c, dmr.rptl(c.id), tagged("RPTACK"))
            if ack:
                return dmr.parse_control(ack)["tail"].hex()
        return None
    finally:
        c.dest = None
        done(c)


def test_mmdvm_salt_changes_on_restart(rig, rig_control):
    """P-DMR-02 (diff): the RPTACK salt differs after a master restart."""
    before = both(rig, salt_of)
    for side in ("ref", "sut"):
        rig_control("restart", "xlxd-" + side)
    after = both(rig, salt_of)
    time.sleep(5)                           # let the masters reach their transcoders again
    got = {n: {"before": before[n], "after": after[n], "changed": before[n] != after[n]} for n in before}
    print("P-DMR-02 salt:", got)
    for name, r in got.items():
        assert r["after"], "%s: no RPTACK within 60 s of the restart" % name
    same(got, "changed")
