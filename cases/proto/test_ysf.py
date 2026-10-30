# SPDX-License-Identifier: MIT
"""YSF reflector conformance (docs/protocols/ysf.md): one case per
obligation P-YSF-NN, xlxd-ref and xlxd-sut probed in parallel.

Judged as in ysf.md: hb = frame structure from the Yaesu standard,
asserted on both sides; diff = sut == ref (a convention reflectors and
clients agree on); dev = disputed or xlxd deviates: sut == ref under the
xlxd profile, else the profile's "ysf" spec entry {"P-YSF-NN": {observed
key: value}} (skip "spec pending" without one). Every case prints what
both sides did.
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import dmr, nxdn
from dvtest.proto import ysf as Y

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "Y", "W"
TALKER = "N0CALL"                               # poll-only probes; also the DMR talker (1023007)
FRAMES = 65                                     # 13 voice packets: clear of xlxd's short-stream guard (< 50 frames)
GAP = Y.PACKET_GAP
CACHE = 1.2                                     # xlxd refreshes its module client list once per second
TAIL = 1.0


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
    profile's ysf spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "ysf") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no ysf %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def frames(n=FRAMES, seed="ysf"):
    """n 9-byte DMR-form AMBE+2 frames that survive the VCH round trip."""
    return [nxdn.to_dmr(nxdn.from_dmr(f)) for f in signals.prng_frames("dmr", n, seed=seed)]


def no_gain(frames_):
    return [nxdn.without_gain(nxdn.from_dmr(f)) for f in frames_]


def runs(sent, got):
    """How many times sent appears as a contiguous run in got (compared
    without the gain index): xlxd adds frames around a stream (a padded
    last packet), so whole-list equality is too strict."""
    sent, got = no_gain(sent), no_gain(got)
    count, offset = 0, 0
    while offset <= len(got) - len(sent):
        if got[offset:offset + len(sent)] == sent:
            count += 1
            offset += len(sent)
        else:
            offset += 1
    return count


# ---------------------------------------------------------------- clients

_calls = {}
_calls_lock = threading.Lock()


def call(ip):
    """A fresh test callsign STR001..STR120 (rig/config/dmrid.dat) per
    reflector: every transmitting client of a probe gets its own, so the
    joins (short streams) never trip xlxd's loop guard (three short
    streams of one MY callsign in a row)."""
    with _calls_lock:
        n = _calls.get(ip, 0)
        _calls[ip] = n + 1
    return "STR%03d" % (n % 120 + 1)


def new(ip, module=MOD, callsign=None):
    return Y.YsfClient("0.0.0.0", callsign=callsign or call(ip), module=module)


def mark(rig, phase, tag):
    """Control channel "mark begin|end <tag>": brackets deliberately
    malformed or probing ingress so dvcheck tells it from a fault (no-op
    without the control channel)."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "ysf-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def marked(rig, tag, func, *args):
    """both(...) inside a mark window."""
    mark(rig, "begin", tag)
    try:
        return both(rig, func, *args)
    finally:
        mark(rig, "end", tag)


def ready(ip, *made):
    """Link every client (poll, then the module's DG-ID join), wait for
    the reflector's client cache, clear the inboxes."""
    for c in made:
        assert c.link(ip), "%s: no poll reply for %s" % (ip, c.callsign)
    time.sleep(CACHE)
    clear(*made)
    return made


def clear(*made):
    for c in made:
        c.client.drain()
        c.client.inbox.clear()
        c.client.inbox_times.clear()


def settle(*made, seconds=TAIL):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for c in made:
            c.client.drain()
        time.sleep(0.01)


def done(*made):
    for c in made:
        c.client.close()


def send(c, packets, *listeners, wait=TAIL, gap=GAP):
    c.client.paced(c.dest, packets, gap, *[m.client for m in listeners])
    settle(c, *listeners, seconds=wait)


def talk(c, *listeners, voice=None, wait=TAIL, **kw):
    """A complete DN2 stream (header, voice, terminator); returns the frames."""
    voice = voice or frames()
    send(c, Y.stream_packets(voice, c.callsign, c.dgid, **kw), *listeners, wait=wait)
    return voice


def ysfd(c):
    """Parsed YSFD packets in the inbox."""
    return [p for p in (Y.parse(d) for d in c.client.inbox) if p]


def heard(c):
    out = []
    for p in ysfd(c):
        if p["kind"] == "voice":
            out.extend(p["frames"])
    return out


def replies(c, pred):
    return [d for d in c.client.inbox if pred(d)]


def raw_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    s.settimeout(0.05)
    return s


def recv_all(s, seconds):
    """[(data, addr)] received within seconds."""
    out = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            out.append(s.recvfrom(2048))
        except socket.timeout:
            pass
    return out


def dmr_stream(client, voice):
    """Header, voice bursts and terminator of a DMR client to its module
    TG, sent back to back by the caller (no settle after the header, unlike
    DmrClient.talk)."""
    sid = clients.fresh_stream_id("dmr")
    head = dmr.packet(0, client.id, client.tg, client.id, 0xA1, sid, dmr.data_sync_payload())
    body, seq = dmr.voice_packets(voice, client.id, client.tg, sid)
    tail = dmr.packet(seq, client.id, client.tg, client.id, 0xA2, sid, dmr.data_sync_payload())
    return [head] + body + [tail]


# ---------------------------------------------------------------- 1 link

def poll_probe(ip):
    s = raw_socket()
    try:
        s.sendto(Y.poll_packet(TALKER), (ip, Y.PORT))
        first = recv_all(s, 1.0)
        out = {"first_reply": bool(first), "reply": first[0][0].decode("ascii", "replace") if first else None,
               "reply_len": len(first[0][0]) if first else 0,
               "from_port": first[0][1] == (ip, Y.PORT) if first else None,
               "latency_ms": None}
        if first:
            # latency of a poll on the linked address: time to the first reply
            s.sendto(Y.poll_packet(TALKER), (ip, Y.PORT))
            t0 = time.monotonic()
            s.settimeout(0.5)
            try:
                s.recvfrom(2048)
                out["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            except socket.timeout:
                pass
            s.settimeout(0.05)
        answered = 0
        for _ in range(3):
            s.sendto(Y.poll_packet(TALKER), (ip, Y.PORT))
            answered += len([d for d, _ in recv_all(s, 0.4) if d[:4] == b"YSFP"])
        out["every_poll"] = answered == 3
        return out
    finally:
        s.close()


def test_ysf_poll_reply(rig):
    """P-YSF-01 / 02 (diff): the first YSFP is answered at once with 14 B
    starting YSFP, every later poll too, from the port the client sent to."""
    got = both(rig, poll_probe)
    print("P-YSF-01/02 poll:", got)
    assert got["ref"]["first_reply"] and got["ref"]["reply_len"] == 14, got["ref"]
    same(got, "first_reply", "reply_len", "from_port", "every_poll")


def test_ysf_poll_reply_text(rig):
    """P-YSF-03 (dev): bytes 4-13 of the poll reply."""
    got = both(rig, poll_probe)
    print("P-YSF-03 reply text:", {k: v["reply"] for k, v in got.items()})
    judge_dev(rig, "P-YSF-03", got, "reply")


def callsign_probe(ip):
    out = {}
    base = call(ip)
    for name, callsign in (("suffix", base + "-1"), ("ten", base + "AB12")):
        s = raw_socket()
        try:
            s.sendto(b"YSFP" + callsign.encode().ljust(10)[:10], (ip, Y.PORT))
            out[name] = any(d[:4] == b"YSFP" for d, _ in recv_all(s, 0.8))
        finally:
            s.close()
    return out


def test_ysf_poll_callsign_forms(rig):
    """P-YSF-04 (dev): a suffixed and a 10-character callsign (one
    malformed link per run; '/' follows the same xlxd rule as '-')."""
    got = marked(rig, "ysf-callsign-forms", callsign_probe)
    print("P-YSF-04 callsign forms answered:", got)
    judge_dev(rig, "P-YSF-04", got, "suffix", "ten")


def length_probe(ip):
    out = {}
    for size in (15,):
        s = raw_socket()
        try:
            s.sendto(Y.poll_packet(call(ip)).ljust(size, b" ")[:size], (ip, Y.PORT))
            out["len%d" % size] = any(d[:4] == b"YSFP" for d, _ in recv_all(s, 0.8))
        finally:
            s.close()
    return out


def test_ysf_poll_length(rig):
    """P-YSF-05 (dev): a 15-byte poll (the second malformed link of the run)."""
    got = marked(rig, "ysf-poll-length", length_probe)
    print("P-YSF-05 poll lengths answered:", got)
    judge_dev(rig, "P-YSF-05", got, "len15")


def server_poll_probe(ip):
    s = raw_socket()
    try:
        s.sendto(Y.poll_packet(TALKER), (ip, Y.PORT))
        recv_all(s, 0.5)
        unsolicited = [d for d, _ in recv_all(s, 12.0) if d[:4] == b"YSFP"]
        return {"server_polls": bool(unsolicited)}
    finally:
        s.close()


def test_ysf_server_polls(rig):
    """P-YSF-06 (dev): does the reflector poll an idle client (12 s)?"""
    got = both(rig, server_poll_probe)
    print("P-YSF-06 reflector polls:", got)
    judge_dev(rig, "P-YSF-06", got, "server_polls")


def timeout_probe(ip):
    talker, polled, silent = new(ip), new(ip), new(ip)
    try:
        ready(ip, talker, polled, silent)
        out = {}
        start = time.monotonic()
        for mark in (35, 70):
            while time.monotonic() - start < mark:
                talker.keepalive()
                polled.keepalive()
                settle(talker, polled, silent, seconds=4.0)
            clear(talker, polled, silent)
            talk(talker, polled, silent)
            out["polled_%ds" % mark] = len(heard(polled)) > 0
            out["silent_%ds" % mark] = len(heard(silent)) > 0
        return out
    finally:
        done(talker, polled, silent)


@pytest.mark.slow
def test_ysf_timeout_drop(rig):
    """P-YSF-07 (dev) / P-YSF-37 (diff): a client that stops polling is
    dropped (heard at 35 s / 70 s?); one that keeps polling is kept and
    is not refreshed by the other's polls."""
    got = both(rig, timeout_probe)
    print("P-YSF-07/37 timeout:", got)
    for name, r in got.items():
        assert r["polled_35s"] and r["polled_70s"], "%s: a polling client lost the link: %s" % (name, r)
        assert not r["silent_70s"], "%s: a client silent for 70 s still gets voice" % name
    judge_dev(rig, "P-YSF-07", got, "silent_35s")


# ---------------------------------------------------------------- 2 unlink, status

def unlink_probe(ip):
    talker, leaver = new(ip), new(ip)
    try:
        ready(ip, talker, leaver)
        leaver.client.send(leaver.dest, Y.unlink_packet(leaver.callsign))
        settle(leaver, seconds=0.5)
        reply = [d for d in leaver.client.inbox if not d.startswith(b"YSFD")]
        clear(talker, leaver)
        talk(talker, leaver)
        return {"reply": bool(reply), "hears_after_unlink": len(heard(leaver)) > 0}
    finally:
        done(talker, leaver)


def test_ysf_unlink(rig):
    """P-YSF-08 (dev): YSFU removes the client (no further voice), no reply."""
    got = marked(rig, "ysf-unlink", unlink_probe)
    print("P-YSF-08 YSFU:", got)
    for name, r in got.items():
        assert not r["reply"], "%s: YSFU answered" % name
    judge_dev(rig, "P-YSF-08", got, "hears_after_unlink")


def status_query(ip):
    s = raw_socket()
    try:
        s.sendto(Y.status_request(), (ip, Y.PORT))
        got = [d for d, _ in recv_all(s, 1.0) if d.startswith(b"YSFS")]
        return got[0] if got else b""
    finally:
        s.close()


def status_probe(ip):
    """Status from an address that is not linked, right before and after
    one DMR client links (with two YSF clients already linked):
    dmr_counted = the count includes clients of other protocols."""
    a, b = new(ip), new(ip)
    dmr_client = clients.make("dmr", MOD, 1)
    try:
        ready(ip, a, b)
        before = Y.parse_status(status_query(ip)) or {}
        assert dmr_client.link(ip, timeout=0.5), "%s: DMR link failed" % ip
        reply = status_query(ip)
        info = Y.parse_status(reply) or {}
        layout = Y.status_layout(reply) if reply else {"size": 0, "id_digits": False, "count_digits": False}
        counted = None
        if info.get("count") is not None and before.get("count") is not None:
            counted = info["count"] > before["count"]
        return dict(layout, answered=bool(reply), count=info.get("count"), dmr_counted=counted,
                    name=info.get("name"), description=info.get("description"))
    finally:
        clients.close(dmr_client)
        done(a, b)


def test_ysf_status(rig):
    """P-YSF-09 (diff): YSFS from an address that is not linked gets the
    42 B status."""
    got = both(rig, status_probe)
    print("P-YSF-09 YSFS:", got)
    assert got["ref"]["answered"] and got["ref"]["size"] == 42, got["ref"]
    same(got, "answered", "size")


def test_ysf_status_fields(rig):
    """P-YSF-10 (dev): status field forms (id and count digits) and what
    the count counts (dmr_counted: one DMR client linked between two queries)."""
    got = both(rig, status_probe)
    print("P-YSF-10 status fields:", got)
    for name, r in got.items():
        assert r["id_digits"] and r["count_digits"], "%s: status field form %s" % (name, r)
    judge_dev(rig, "P-YSF-10", got, "dmr_counted")


# ---------------------------------------------------------------- 3 data path

def relay_probe(ip):
    talker, l1, l2, other = new(ip), new(ip), new(ip), new(ip, OTHER)
    try:
        ready(ip, talker, l1, l2, other)
        voice = talk(talker, l1, l2, other)
        out = {"echo": len(ysfd(talker))}
        for name, c in (("l1", l1), ("l2", l2)):
            got = ysfd(c)
            kinds = [p["kind"] for p in got]
            out[name + "_headers"] = kinds.count("header")
            out[name + "_terminators"] = kinds.count("terminator")
            out[name + "_frames_once"] = runs(voice, heard(c)) == 1
            out[name + "_extra_frames"] = len(heard(c)) - len(voice)
        out["other_module"] = len(ysfd(other))
        return out
    finally:
        done(talker, l1, l2, other)


def test_ysf_relay_once(rig):
    """P-YSF-11 / 24 (diff): a DN2 stream reaches every other client of
    the module once (frames compared without the gain index), nothing
    reaches another module."""
    got = both(rig, relay_probe)
    print("P-YSF-11/24 relay:", got)
    for name, r in got.items():
        assert r["l1_frames_once"] and r["l2_frames_once"], "%s: frames not relayed once: %s" % (name, r)
        assert r["other_module"] == 0, "%s: another module heard it" % name
    same(got, "l1_headers", "l1_terminators", "l2_headers", "l2_terminators")


def test_ysf_no_echo(rig):
    """P-YSF-12 (diff): the talker never gets its stream back."""
    got = both(rig, relay_probe)
    print("P-YSF-12 echo:", {k: v["echo"] for k, v in got.items()})
    for name, r in got.items():
        assert r["echo"] == 0, "%s: %d packets echoed" % (name, r["echo"])


def unlinked_probe(ip):
    listener = new(ip)
    stranger = new(ip)
    try:
        ready(ip, listener)
        stranger.dest = (ip, Y.PORT)
        send(stranger, Y.stream_packets(frames(), stranger.callsign, Y.module_dgid(MOD)), listener)
        return {"relayed": len(ysfd(listener))}
    finally:
        done(listener, stranger)


def test_ysf_unlinked_sender(rig):
    """P-YSF-13 (diff): YSFD from an address that never polled is not relayed."""
    got = marked(rig, "ysf-unlinked", unlinked_probe)
    print("P-YSF-13 unlinked sender:", got)
    for name, r in got.items():
        assert r["relayed"] == 0, "%s: %d packets relayed" % (name, r["relayed"])


def egress_checks(got):
    """Structure, status-byte and gateway checks over one egress stream."""
    kinds = [p["kind"] for p in got]
    counters = [p["counter"] for p in got]
    fichs = [p["fich"] for p in got]
    return {
        "packets": len(got),
        "sync_ok": all(p["sync_ok"] for p in got),
        "fich_ok": all(f is not None for f in fichs),
        "header_first": bool(kinds) and kinds[0] == "header",
        "terminator_last": bool(kinds) and kinds[-1] == "terminator",
        "counter_changes": all(a != b for a, b in zip(counters, counters[1:])),
        "counter_plus_one": all((b - a) & 0x7F == 1 for a, b in zip(counters, counters[1:])),
        "first_eot_clear": bool(got) and not got[0]["eot"],
        "last_eot_set": bool(got) and got[-1]["eot"],
        "eot_only_last": [p["eot"] for p in got[:-1]].count(True) == 0,
        "gateway_constant": len({p["gateway"] for p in got}) == 1,
        "gateway": got[0]["gateway"] if got else None,
        "src": sorted({p["src"] for p in got}),
        "dst": sorted({p["dst"] for p in got}),
        "dgid": sorted({f["sq"] for f in fichs if f}),
        "dt": sorted({f["dt"] for f in fichs if f}),
    }


def crossmode_probe(ip):
    """A DMR talker on MOD heard by a YSF listener: the reflector builds
    the YSF stream itself."""
    dmr_talker = clients.make("dmr", MOD, 0)
    listener = new(ip)
    try:
        assert dmr_talker.link(ip), "%s: DMR link failed" % ip
        ready(ip, listener)
        clients.talk(dmr_talker, "dmr", frames(), tail=0.5)
        settle(listener, seconds=2.0)
        return egress_checks(ysfd(listener))
    finally:
        clients.close(dmr_talker)
        done(listener)


def same_mode_probe(ip):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        talk(talker, listener)
        return egress_checks(ysfd(listener))
    finally:
        done(talker, listener)


def test_ysf_egress_structure(rig):
    """P-YSF-14 (hb): egress frames are 155 B with the frame sync and a
    decodable FICH, header first and terminator last (Yaesu standard 4.1,
    4.2.1), for a cross-mode (DMR) and a same-mode (YSF) talker."""
    for label, probe in (("dmr->ysf", crossmode_probe), ("ysf->ysf", same_mode_probe)):
        got = both(rig, probe)
        print("P-YSF-14 egress %s:" % label, got)
        for name, r in got.items():
            for key in ("sync_ok", "fich_ok", "header_first", "terminator_last"):
                assert r["packets"] and r[key], "%s %s: %s false: %s" % (name, label, key, r)


def test_ysf_egress_gateway_constant(rig):
    """P-YSF-15 (diff): the gateway field stays the same for a whole
    stream (MMDVMHost locks onto it)."""
    got = both(rig, crossmode_probe)
    print("P-YSF-15 gateway:", {k: (v["gateway_constant"], v["gateway"]) for k, v in got.items()})
    for name, r in got.items():
        assert r["gateway_constant"], "%s: gateway field changes in a stream" % name
    same(got, "gateway_constant")


def test_ysf_egress_counter(rig):
    """P-YSF-16 (dev): consecutive egress frames carry different counters
    (MMDVMHost drops a repeat) and the first frame has the end bit clear;
    for a cross-mode (DMR) and a same-mode (YSF) talker."""
    cross = both(rig, crossmode_probe)
    same_mode = both(rig, same_mode_probe)
    got = {k: {"cross_counter_changes": cross[k]["counter_changes"],
               "same_counter_changes": same_mode[k]["counter_changes"],
               "first_eot_clear": cross[k]["first_eot_clear"] and same_mode[k]["first_eot_clear"]}
           for k in ("ref", "sut")}
    print("P-YSF-16 counter:", got)
    for name, r in got.items():
        assert r["first_eot_clear"], "%s: first egress frame has the end bit" % name
    judge_dev(rig, "P-YSF-16", got, "cross_counter_changes", "same_counter_changes")


def test_ysf_egress_end_bit(rig):
    """P-YSF-17 (diff): the last egress frame has the end bit set, no frame
    before it does (cross-mode and same-mode)."""
    for label, probe in (("dmr->ysf", crossmode_probe), ("ysf->ysf", same_mode_probe)):
        got = both(rig, probe)
        print("P-YSF-17 end bit %s:" % label, {k: (v["last_eot_set"], v["eot_only_last"]) for k, v in got.items()})
        for name, r in got.items():
            assert r["last_eot_set"] and r["eot_only_last"], "%s %s: %s" % (name, label, r)


def kept_probe(ip):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        voice = talk(talker, listener, dst="N0DEST")
        got = ysfd(listener)
        exact = any(heard(listener)[i:i + len(voice)] == voice for i in range(len(heard(listener)) - len(voice) + 1))
        return {"voice_exact": exact,
                "voice_exact_wo_gain": runs(voice, heard(listener)) == 1,
                "src_kept": {p["src"] for p in got} == {talker.callsign},
                "dst_kept": {p["dst"] for p in got} == {"N0DEST"},
                "gateway_kept": {p["gateway"] for p in got} == {talker.callsign}}
    finally:
        done(talker, listener)


def test_ysf_same_mode_kept(rig):
    """P-YSF-18 (dev): YSF -> YSF keeps voice bits and callsigns (source,
    destination, gateway)."""
    got = both(rig, kept_probe)
    print("P-YSF-18 same mode:", got)
    for name, r in got.items():
        assert r["voice_exact_wo_gain"], "%s: voice changed beyond the gain index" % name
    judge_dev(rig, "P-YSF-18", got, "voice_exact", "src_kept", "dst_kept", "gateway_kept")


def test_ysf_crossmode_callsigns(rig):
    """P-YSF-19 (diff): a cross-mode stream carries the talker as source
    and ALL as destination."""
    got = both(rig, crossmode_probe)
    print("P-YSF-19 cross-mode callsigns:", {k: (v["src"], v["dst"]) for k, v in got.items()})
    for name, r in got.items():
        assert r["src"] == [TALKER] and r["dst"] == [Y.ALL], "%s: src %s dst %s" % (name, r["src"], r["dst"])
    same(got, "src", "dst")


# ---------------------------------------------------------------- 4 stream start / end

def end_marker_probe(ip, variant):
    """Talker ends its stream with only one marker (or none): how soon
    after its last packet does the listener's egress stream end (its
    terminator frame)? "prompt" < 0.5 s, "timeout" later, "none" never."""
    a, listener = new(ip), new(ip)
    try:
        ready(ip, a, listener)
        packets = Y.stream_packets(frames(), a.callsign, a.dgid)
        if variant == "end_bit_only":            # no terminator frame, end bit on the last voice
            packets = packets[:-2] + [Y.with_status(packets[-2], eot=True)]
        elif variant == "terminator_only":       # terminator frame without the end bit
            packets = packets[:-1] + [Y.with_status(packets[-1], eot=False)]
        elif variant == "none":
            packets = packets[:-1]
        stamps = a.client.paced(a.dest, packets, GAP, listener.client)
        settle(a, listener, seconds=3.0)
        ends = [t for d, t in zip(listener.client.inbox, listener.client.inbox_times)
                if (Y.parse(d) or {}).get("kind") == "terminator" or (Y.parse(d) or {}).get("eot")]
        if not ends:
            return {"end": "none", "end_s": None}
        delay = ends[0] - stamps[-1]
        return {"end": "prompt" if delay < 0.5 else "timeout", "end_s": round(delay, 2)}
    finally:
        done(a, listener)


def test_ysf_stream_end_markers(rig):
    """P-YSF-20 (dev): a stream ends on the end bit alone, on a terminator
    frame without the end bit, or only after the silence timeout: the
    egress stream end of a listener, measured from the talker's last packet."""
    got = {}
    for variant in ("end_bit_only", "terminator_only", "none"):
        r = marked(rig, "ysf-end-" + variant, end_marker_probe, variant)
        for side in ("ref", "sut"):
            got.setdefault(side, {})[variant] = r[side]["end"]
            got[side][variant + "_s"] = r[side]["end_s"]
    print("P-YSF-20 egress end after:", got)
    judge_dev(rig, "P-YSF-20", got, "end_bit_only", "terminator_only", "none")


def late_entry_probe(ip):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        packets = Y.stream_packets(frames(), talker.callsign, talker.dgid)[1:]
        send(talker, packets, listener)
        got = ysfd(listener)
        return {"carried": any(p["kind"] == "voice" for p in got),
                "header_synthesised": bool(got) and got[0]["kind"] == "header"}
    finally:
        done(talker, listener)


def test_ysf_late_entry(rig):
    """P-YSF-21 (dev): a stream without a header frame is carried."""
    got = marked(rig, "ysf-late-entry", late_entry_probe)
    print("P-YSF-21 late entry:", got)
    judge_dev(rig, "P-YSF-21", got, "carried", "header_synthesised")


def second_talker_probe(ip):
    """YSF talker A, then 0.25 s later DMR talker B on the same module (a
    second YSF client behind the tester's one IP would join A's stream
    on xlxd, which keys YSF streams by source IP)."""
    a, listener = new(ip), new(ip)
    b = clients.make("dmr", MOD, 1)
    try:
        assert b.link(ip), "%s: DMR link failed" % ip
        ready(ip, a, listener)
        voice_a, voice_b = frames(seed="a"), frames(66, seed="b")
        pa = Y.stream_packets(voice_a, a.callsign, a.dgid)
        t = threading.Thread(target=a.client.paced, args=(a.dest, pa, GAP, listener.client))
        t.start()
        time.sleep(0.25)
        b.client.paced(b.dest, dmr_stream(b, voice_b), 0.060)
        t.join()
        settle(a, listener)
        got = no_gain(heard(listener))
        a_set, b_set = set(no_gain(voice_a)), set(no_gain(voice_b))
        a_idx = [i for i, f in enumerate(got) if f in a_set]
        b_idx = [i for i, f in enumerate(got) if f in b_set]
        return {"mixed": bool(a_idx and b_idx) and b_idx[0] < a_idx[-1],
                "first_whole": runs(voice_a, heard(listener)) == 1,
                "second_after_first": len(b_idx)}
    finally:
        clients.close(b)
        done(a, listener)


def test_ysf_second_talker(rig):
    """P-YSF-22 (spec B1): a second talker is not mixed into a running stream."""
    got = marked(rig, "ysf-second-talker", second_talker_probe)
    print("P-YSF-22 second talker:", got)
    if rig.sut_profile == specs.DEFAULT:
        same(got, "mixed", "first_whole")
    else:
        assert not got["sut"]["mixed"], "sut mixed two talkers"


def wrap_probe(ip):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        voice = frames(5 * 135, seed="wrap")
        talk(talker, listener, voice=voice, wait=2.0)
        got = ysfd(listener)
        return {"headers": [p["kind"] for p in got].count("header"),
                "frames_once": runs(voice, heard(listener)) == 1}
    finally:
        done(talker, listener)


def test_ysf_counter_wrap(rig):
    """P-YSF-23 (diff): an over of 137 packets wraps the 7-bit counter and
    stays one stream."""
    got = both(rig, wrap_probe)
    print("P-YSF-23 counter wrap:", got)
    for name, r in got.items():
        assert r["frames_once"] and r["headers"] == 1, "%s: %s" % (name, r)


# ---------------------------------------------------------------- 5 data types, DG-ID

def dt_probe(ip, dt):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        packets = Y.dt_stream_packets(talker.callsign, dt, 6, sq=talker.dgid)
        send(talker, packets, listener)
        got = [p for p in ysfd(listener) if p["fich"] and p["fich"]["dt"] == dt]
        payload_same = [d[65:] for d in listener.client.inbox if d.startswith(b"YSFD")] == [p[65:] for p in packets]
        return {"relayed": len(got), "unchanged": payload_same}
    finally:
        done(talker, listener)


def test_ysf_dn1_vw_carried(rig):
    """P-YSF-25 (dev): V/D mode 1 and Voice FR streams between YSF clients."""
    got = {}
    for label, dt in (("dn1", Y.DT_VD_MODE1), ("vw", Y.DT_VOICE_FR_MODE)):
        r = marked(rig, "ysf-dt-" + label, dt_probe, dt)
        for side in ("ref", "sut"):
            got.setdefault(side, {})[label] = r[side]["relayed"] > 0
            got[side][label + "_unchanged"] = r[side]["unchanged"]
    print("P-YSF-25 DN1/VW carried:", got)
    judge_dev(rig, "P-YSF-25", got, "dn1", "vw")


def test_ysf_data_fr(rig):
    """P-YSF-26 (dev): Data FR frames that are not WiresX commands."""
    got = marked(rig, "ysf-data-fr", dt_probe, Y.DT_DATA_FR_MODE)
    got = {k: {"relayed": v["relayed"] > 0} for k, v in got.items()}
    print("P-YSF-26 data FR:", got)
    judge_dev(rig, "P-YSF-26", got, "relayed")


def bad_fich_probe(ip):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        packets = Y.stream_packets(frames(), talker.callsign, talker.dgid)
        bad = [packets[0]] + [Y.corrupt_fich(p) for p in packets[1:-1]] + [packets[-1]]
        send(talker, bad, listener)
        return {"relayed_voice": len(heard(listener)),
                "relayed_bad": sum(1 for p in ysfd(listener) if p["fich"] is None)}
    finally:
        done(talker, listener)


def test_ysf_bad_fich(rig):
    """P-YSF-27 (dev): packets whose FICH does not decode are not relayed."""
    got = marked(rig, "ysf-bad-fich", bad_fich_probe)
    print("P-YSF-27 bad FICH:", got)
    judge_dev(rig, "P-YSF-27", got, "relayed_bad")


def dgid_probe(ip):
    mover, home, away = new(ip), new(ip), new(ip, OTHER)
    try:
        ready(ip, mover, home, away)
        send(mover, Y.stream_packets(frames(), mover.callsign, Y.module_dgid(OTHER)), home, away)
        moved = len(heard(away)) > 0
        stayed = len(heard(home)) > 0
        clear(mover, home, away)
        time.sleep(CACHE)
        send(mover, Y.stream_packets(frames(), mover.callsign, 0), home, away)
        return {"moved": moved and not stayed, "permanent": len(heard(away)) > 0 and not heard(home)}
    finally:
        done(mover, home, away)


def test_ysf_dgid_module(rig):
    """P-YSF-28 (dev): a header with DG-ID 10 + n moves the client to
    module 'A' + n; is the move kept for a later DG-ID 0 stream?"""
    got = both(rig, dgid_probe)
    print("P-YSF-28 DG-ID:", got)
    judge_dev(rig, "P-YSF-28", got, "moved", "permanent")


def default_module_probe(ip):
    fresh = new(ip)
    talkers = {}
    try:
        fresh.dest = (ip, Y.PORT)
        s = fresh.client
        s.send(fresh.dest, Y.poll_packet(fresh.callsign))
        s.pump(0.5)
        time.sleep(CACHE)
        out = {}
        for module in ("A", "B"):
            t = talkers[module] = new(ip, module)
            ready(ip, t)
            clear(fresh)
            talk(t, fresh)
            out["hears_" + module] = len(heard(fresh)) > 0
        return out
    finally:
        done(fresh, *talkers.values())


def test_ysf_default_module(rig):
    """P-YSF-29 (dev): the module of a client that only polled."""
    got = both(rig, default_module_probe)
    print("P-YSF-29 default module:", got)
    judge_dev(rig, "P-YSF-29", got, "hears_A", "hears_B")


def test_ysf_egress_dgid(rig):
    """P-YSF-30 (dev): the DG-ID (FICH SQ) of egress frames."""
    got = both(rig, crossmode_probe)
    got = {k: {"dgid": v["dgid"]} for k, v in got.items()}
    print("P-YSF-30 egress DG-ID:", got)
    judge_dev(rig, "P-YSF-30", got, "dgid")


# ---------------------------------------------------------------- 6 WiresX

def wiresx_probe(ip, command):
    asker, listener = new(ip), new(ip)
    try:
        ready(ip, asker, listener)
        send(asker, Y.wiresx_packets(asker.callsign, command), listener, wait=3.0)
        return {"reply": Y.wiresx_reply_kind(ysfd(asker)),
                "relayed": sum(1 for p in ysfd(listener) if p["kind"] == "data")}
    finally:
        done(asker, listener)


def test_ysf_wiresx_dx(rig):
    """P-YSF-31 (dev): a WiresX DX_REQ is answered (and/or relayed)."""
    got = marked(rig, "ysf-wiresx-dx", wiresx_probe, Y.WIRESX_DX_REQ)
    print("P-YSF-31 DX_REQ:", got)
    judge_dev(rig, "P-YSF-31", got, "reply", "relayed")


def conn_disc_probe(ip):
    asker, home, target = new(ip), new(ip, MOD), new(ip, "B")
    other = new(ip)
    try:
        ready(ip, asker, home, target)
        send(asker, Y.wiresx_packets(asker.callsign, Y.wiresx_conn(2)), wait=3.0)
        conn_reply = Y.wiresx_reply_kind(ysfd(asker))
        time.sleep(CACHE)
        clear(asker, home, target)
        other.module = "B"
        ready(ip, other)
        talk(other, asker, home, target)
        conn_moved = len(heard(asker)) > 0
        send(asker, Y.wiresx_disc_packets(asker.callsign), wait=3.0)
        time.sleep(CACHE)
        clear(asker, other)
        talk(other, asker)
        return {"conn_reply": conn_reply, "conn_moved": conn_moved, "disc_hears": len(heard(asker)) > 0}
    finally:
        done(asker, home, target, other)


def test_ysf_wiresx_conn_disc(rig):
    """P-YSF-32 (dev): CONN_REQ room 2 selects module B; DISC_REQ leaves it."""
    got = marked(rig, "ysf-wiresx-conn", conn_disc_probe)
    print("P-YSF-32 CONN/DISC:", got)
    judge_dev(rig, "P-YSF-32", got, "conn_reply", "conn_moved", "disc_hears")


# ---------------------------------------------------------------- 7 other tags, malformed

def other_tags_probe(ip):
    c = new(ip)
    try:
        ready(ip, c)
        out = {}
        for name, data in (("ysfo", Y.options_packet(c.callsign, "20;21;")),
                           ("ysfi", Y.info_packet(c.callsign)),
                           ("ysfv", b"YSFV"), ("unknown", b"XXXX" + bytes(10))):
            clear(c)
            c.client.send(c.dest, data)
            settle(c, seconds=0.6)
            out[name] = len(c.client.inbox)
        clear(c)
        c.keepalive()
        settle(c, seconds=0.5)
        out["poll_after"] = any(Y.is_poll_ack(d) for d in c.client.inbox)
        return out
    finally:
        done(c)


def test_ysf_other_tags(rig):
    """P-YSF-33 (diff): YSFO, YSFI, YSFV and an unknown tag get no reply;
    polls are still answered."""
    got = marked(rig, "ysf-other-tags", other_tags_probe)
    print("P-YSF-33 other tags (replies):", got)
    for name, r in got.items():
        assert r["poll_after"], "%s: no poll reply after the other tags" % name
    same(got, "ysfo", "ysfi", "ysfv", "unknown")


def malformed_probe(ip):
    talker, listener = new(ip), new(ip)
    try:
        ready(ip, talker, listener)
        good = Y.stream_packets(frames(), talker.callsign, talker.dgid)
        bad_sync = bytearray(good[2])
        bad_sync[35] ^= 0xFF
        out = {}
        for name, packets in (("short_ysfd", [p[:154] for p in good]),
                              ("long_ysfd", [p + b"\x00" for p in good]),
                              ("bad_sync", good[:2] + [bytes(bad_sync)] + good[3:])):
            clear(talker, listener)
            send(talker, packets, listener, wait=2.0)
            out[name] = len(heard(listener)) if name != "bad_sync" else len(heard(listener)) < len(Y.wire_frames(frames()))
        clear(talker)
        talker.client.send(talker.dest, b"YSFU" + b"N0CALL")
        talker.keepalive()
        settle(talker, seconds=0.5)
        out["poll_after"] = any(Y.is_poll_ack(d) for d in talker.client.inbox)
        out["error_replies"] = sum(1 for d in talker.client.inbox if not Y.is_poll_ack(d) and not d.startswith(b"YSFD"))
        return out
    finally:
        done(talker, listener)


def test_ysf_malformed(rig):
    """P-YSF-34 (diff): 154 / 156 B YSFD and a short YSFU are not relayed
    or answered; service continues. A bad frame sync is recorded."""
    got = marked(rig, "ysf-malformed", malformed_probe)
    print("P-YSF-34 malformed:", got)
    for name, r in got.items():
        assert r["poll_after"] and r["error_replies"] == 0, "%s: %s" % (name, r)
        assert r["short_ysfd"] == 0 and r["long_ysfd"] == 0, "%s: malformed YSFD relayed: %s" % (name, r)
    same(got, "bad_sync")


# ---------------------------------------------------------------- 8 several clients

def two_ports_probe(ip):
    talker, p1 = new(ip), new(ip)
    p2 = new(ip, callsign=p1.callsign)
    try:
        ready(ip, talker, p1, p2)
        voice = talk(talker, p1, p2)
        return {"p1_once": runs(voice, heard(p1)) == 1, "p2_once": runs(voice, heard(p2)) == 1}
    finally:
        done(talker, p1, p2)


def test_ysf_two_ports(rig):
    """P-YSF-35 (diff): the same callsign from two ports of one IP is two
    clients, each gets the module's traffic once."""
    got = both(rig, two_ports_probe)
    print("P-YSF-35 two ports:", got)
    for name, r in got.items():
        assert r["p1_once"] and r["p2_once"], "%s: %s" % (name, r)


def two_calls_probe(ip):
    talker, shared = new(ip), new(ip)
    try:
        ready(ip, talker, shared)
        second = call(ip)                        # a second callsign on the same socket, joined to MOD
        shared.client.send(shared.dest, Y.poll_packet(second))
        join = Y.stream_packets([], second, Y.module_dgid(MOD))
        shared.client.paced(shared.dest, [join[0], join[-1]], 0.3)
        settle(shared, seconds=0.3)
        time.sleep(CACHE)
        clear(talker, shared)
        voice = talk(talker, shared)
        return {"copies": runs(voice, heard(shared))}
    finally:
        done(talker, shared)


def test_ysf_two_callsigns_one_address(rig):
    """P-YSF-36 (dev): two callsigns polling from one address: how many
    copies of a stream that address gets."""
    got = both(rig, two_calls_probe)
    print("P-YSF-36 two callsigns, one address:", got)
    judge_dev(rig, "P-YSF-36", got, "copies")
