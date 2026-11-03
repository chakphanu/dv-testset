# SPDX-License-Identifier: MIT
"""AllStarLink test lab (profile asl-lab, docs/asl-lab.md): app_rpt <-> app_rpt and
app_rpt <-> an IAX2 client, with a pcap per scenario and the audio each far side
recorded. The point is the real IAX2 byte vectors DV-Router's future dv-edge-allstar
is built against; the assertions pin that the lab produced them.

    scripts/run.sh --profile asl-lab -m asl_lab                 # about 3 minutes
    DVT_ASL_LONG=1 scripts/run.sh --profile asl-lab -m asl_lab  # + the 75 s call that wraps ts16

Scenarios (all private nodes: A 199901, B 199902, E 299901; E stands in for the edge):

  link_dns        A dials E as the anonymous user "radio" (node lookup through the lab DNS zone,
                  call token), keys a 1 kHz tone, hangs up
  account_md5     A dials E as account "acct" with an md5 secret (no call token), then as "acct-ct"
                  (call token + AUTHREQ / AUTHREP)
  callee          B links A, then E (the edge's role, mode C) originates to B; E's tone reaches A
                  through B
  loop            A-B, E-A, then B-E: a loop through three nodes
  legacy_key      an IAX2 client (tester) calls E as account, legacy !NEWKEY!: RADIO_KEY / RADIO_UNKEY
                  both ways, PING / LAGRQ answered
  minimal_new     the tester plays the edge's address (node 299902 in the lab zone): the smallest NEW
                  the ASL1 design sends (radio, empty call token, no CALLING_NAME) is accepted and answered,
                  voice without RADIO_KEY is heard; the same NEW claiming node 199901 from the wrong
                  address is refused
  registration    A registers at the lab registrar (REGREQ, REGAUTH, REGACK), then with a wrong secret (REGREJ)
  dns_zone        the tester asks the lab zone for SRV, A, TXT and an unknown node
  long_call       (DVT_ASL_LONG=1) one call that lasts 75 s: mini-frame ts16 wraps at 65.536 s, natural
                  PING / PONG (21 s) and LAGRQ / LAGRP (10 s)
"""
import hashlib
import json
import os
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pytest

from dvtest import asl_lab, iax2_frames
from dvtest.proto import iax2

pytestmark = [pytest.mark.rig, pytest.mark.asl_lab]

TONE_FILE = "/var/lib/asterisk/sounds/dvt/tone1k"        # Asterisk path without extension
TONE_MS = 3000


@dataclass
class Ran:
    sc: object
    docs: dict = field(default_factory=dict)       # host key -> iax2_frames.extract() of its pcap

    def types(self, host):
        return self.docs[host]["summary"]

    def frames(self, host, kind):
        return self.docs[host]["types"].get(kind, [])

    def texts(self, host):
        return [f["text"].rstrip("\0") for f in self.frames(host, "text")]

    def rx(self, host, pattern):
        """PCM of the recording (rx side only) on `host` whose file name contains `pattern`."""
        files = self.rx_files(host)
        hits = [f for f in files if pattern in f.name]
        assert hits, "no *-rx.sln with %r on %s (have %s)" % (pattern, host, [f.name for f in files])
        return hits[-1].read_bytes()

    def rx_files(self, host):
        return sorted(f for f in self.sc.recs.get(host, []) if f.name.endswith("-rx.sln"))


@pytest.fixture(scope="module")
def lab():
    lab = asl_lab.from_env()
    if lab is None:
        pytest.skip("no AllStar lab: run scripts/run.sh --profile asl-lab (the tester gets DVT_ASL_LAB=on)")
    lab.wait_ready()
    lab.write_versions()
    sha, size = lab.put_tone(TONE_MS)
    lab.tone_sha256 = sha
    assert size == 8 * TONE_MS
    return lab


def finish(lab, sc, hosts, limit=3):
    """Parse every pcap of the scenario; write <host>.frames.json (first `limit` frames per type) beside it."""
    ran = Ran(sc)
    for key in hosts:
        path = sc.dir / ("%s.pcap" % key)
        assert path.exists() and path.stat().st_size > 24, "no pcap for %s in %s" % (key, sc.dir)
        ran.docs[key] = iax2_frames.extract(path, ports=(lab.port,))
        (sc.dir / ("%s.frames.json" % key)).write_text(
            json.dumps(iax2_frames.extract(path, ports=(lab.port,), limit=limit), indent=1) + "\n")
    return ran


def quiet(lab, keys=("a", "b", "e")):
    lab.quiet(keys)


def assert_tone(pcm, label, min_ms=1500):
    """The recording holds at least min_ms of loud audio and it is the 1 kHz tone."""
    loud = asl_lab.loud_part(pcm)
    assert len(loud) >= min_ms * 16, "%s: only %d ms of audio above the floor" % (label, len(loud) // 16)
    rms, ratio = asl_lab.tone_energy(loud)
    assert rms > 1000, "%s: rms %.0f" % (label, rms)
    assert ratio > 0.9, "%s: %.2f of the power at 1 kHz" % (label, ratio)


# ------------------------------------------------------------------ scenarios

@pytest.fixture(scope="module")
def link_dns(lab):
    a, e = lab.node("a"), lab.node("e")
    quiet(lab)
    with lab.scenario("link-dns", capture=["a", "e", "reg"]) as sc:
        sc.note("tone_sha256", lab.tone_sha256)
        a.cli("rpt fun %s *3%s" % (a.node, e.node))
        a.wait_link(e.node)
        e.wait_link(a.node)
        time.sleep(1.0)
        a.cli("rpt playback %s %s" % (a.node, TONE_FILE))
        time.sleep(TONE_MS / 1000 + 1.5)
        a.cli("rpt fun %s *1%s" % (a.node, e.node))
        time.sleep(1.5)
    return finish(lab, sc, ["a", "e", "reg"])


@pytest.fixture(scope="module")
def account_md5(lab):
    a, e = lab.node("a"), lab.node("e")
    quiet(lab)
    with lab.scenario("account-md5", capture=["a", "e"]) as sc:
        sc.note("tone_sha256", lab.tone_sha256)
        for user in ("acct", "acct-ct"):
            a.cli("channel originate IAX2/%s:%s@%s:%d/%s application Rpt %s,X"
                  % (user, asl_lab.ACCT_SECRET, e.ip, lab.port, e.node, a.node))
            a.wait_calls(1)
            e.wait_calls(1)
            time.sleep(1.0)
            a.cli("rpt playback %s %s" % (a.node, TONE_FILE))
            time.sleep(TONE_MS / 1000 + 1.5)
            quiet(lab, ("a", "e"))
    return finish(lab, sc, ["a", "e"])


@pytest.fixture(scope="module")
def callee(lab):
    a, b, e = lab.node("a"), lab.node("b"), lab.node("e")
    quiet(lab)
    with lab.scenario("callee", capture=["a", "b", "e"]) as sc:
        sc.note("tone_sha256", lab.tone_sha256)
        b.cli("rpt fun %s *3%s" % (b.node, a.node))               # B has its own link list first
        b.wait_link(a.node)
        e.cli("rpt fun %s *3%s" % (e.node, b.node))               # the edge originates (mode C)
        e.wait_link(b.node)
        b.wait_link(e.node)
        time.sleep(1.5)                                           # L messages settle
        e.cli("rpt playback %s %s" % (e.node, TONE_FILE))
        time.sleep(TONE_MS / 1000 + 2.0)
        sc.note("b_links", b.cli("rpt show variables %s" % b.node))
        quiet(lab)
        time.sleep(1.0)
    return finish(lab, sc, ["a", "b", "e"])


@pytest.fixture(scope="module")
def loop(lab):
    a, b, e = lab.node("a"), lab.node("b"), lab.node("e")
    quiet(lab)
    with lab.scenario("loop", capture=["a", "b", "e"]) as sc:
        # app_rpt refuses a link when the target is already in the caller's tree, and learns a tree from the
        # peers' L messages 150 ms or more after a link comes up: three dials issued together close the
        # triangle A -> B -> E -> A before any L message has been exchanged
        with ThreadPoolExecutor(3) as pool:
            jobs = [pool.submit(x.cli, "rpt fun %s *3%s" % (x.node, y.node)) for x, y in ((a, b), (b, e), (e, a))]
            for job in jobs:
                job.result()
        time.sleep(3.0)
        for host in (a, b, e):
            sc.note("variables_" + host.name, host.cli("rpt show variables %s" % host.node))
        quiet(lab)
        time.sleep(1.0)
    return finish(lab, sc, ["a", "b", "e"])


@pytest.fixture(scope="module")
def legacy_key(lab):
    e = lab.node("e")
    quiet(lab)
    tone = asl_lab.tone_ulaw(TONE_MS)
    with lab.scenario("legacy-key", capture=["e"]) as sc:
        sc.note("tone_sha256", lab.tone_sha256)
        client = iax2.Client(e.ip, asl_lab.ACCT_SECRET, username=asl_lab.ACCT_USER, port=lab.port, codec=iax2.ULAW)
        try:
            client.call(e.node, caller="1999", name="N0TEST", timeout=10)
            client.pump(1.0)
            client.key()
            for i in range(0, len(tone) - 159, 160):
                client.voice(tone[i:i + 160])
                client.pump(0.02)
            client.unkey()
            client.pump(0.5)
            client.media.clear()
            e.cli("rpt playback %s %s" % (e.node, TONE_FILE))
            client.pump(TONE_MS / 1000 + 1.5)
            client.ping()
            client.lagrq()
            client.pump(0.6)
            sc.note("client_controls", client.controls)
        finally:
            client.close()
        time.sleep(1.0)
    ran = finish(lab, sc, ["e"])
    ran.client_media = b"".join(client.media)
    ran.client_controls = list(client.controls)
    return ran


def minimal_new_elements(called, calling, codec=iax2.ULAW):
    """The smallest NEW the ASL1 design note sends: VERSION, CALLED_NUMBER, CALLING_NUMBER, USERNAME radio,
    FORMAT, FORMAT2, CAPABILITY, CAPABILITY2 (the call token IE is added by Client.call)."""
    mask = struct.pack("!I", codec)
    mask2 = b"\0" + struct.pack("!Q", codec)
    return b"".join(iax2.information(code, value) for code, value in (
        (11, struct.pack("!H", 2)), (1, called.encode()), (2, calling.encode()), (6, b"radio"),
        (9, mask), (56, mask2), (8, mask), (55, mask2)))


@pytest.fixture(scope="module")
def minimal_new(lab):
    e = lab.node("e")
    quiet(lab)
    tone = asl_lab.tone_ulaw(1500)
    out = {}
    with lab.scenario("minimal-new", capture=["e"]) as sc:
        sc.note("tone_sha256", hashlib.sha256(tone).hexdigest())
        good = iax2.Client(e.ip, "", username="radio", port=lab.port, codec=iax2.ULAW)
        try:
            good.call(e.node, timeout=10, elements=minimal_new_elements(e.node, lab.tester_node),
                      newkey=b"!NEWKEY1!", empty_token=True)
            good.pump(0.5)
            # voice only, no RADIO_KEY: voice implies key. Silence frames before and after the tone, as an
            # Asterisk node sends them: E's forced jitter buffer holds back (and at hangup drops) a stream
            # that starts with the tone, so a bare 1.5 s tone is not recorded at all
            frames = [b"\xff" * 160] * 50 + [tone[i:i + 160] for i in range(0, len(tone) - 159, 160)] + [b"\xff" * 160] * 40
            for n, frame in enumerate(frames):
                good.voice(frame)
                good.pump(0.02)
                if n == 100:
                    sc.note("variables_during_voice", e.cli("rpt show variables %s" % e.node))
            good.pump(0.3)
            out["good_controls"] = list(good.controls)
        finally:
            good.close()
        time.sleep(1.0)
        bad = iax2.Client(e.ip, "", username="radio", port=lab.port, codec=iax2.ULAW)
        try:
            try:
                bad.call(e.node, timeout=4, elements=minimal_new_elements(e.node, lab.node("a").node),
                         newkey=b"!NEWKEY1!", empty_token=True)
                out["bad"] = "answered"
            except (TimeoutError, ConnectionError) as err:
                out["bad"] = "refused: %s" % err
            out["bad_controls"] = list(bad.controls)
            out["bad_closed"] = bad.closed
        finally:
            bad.close()
        time.sleep(1.0)
        sc.note("bad_claim", out["bad"])
    ran = finish(lab, sc, ["e"])
    ran.out = out
    return ran


@pytest.fixture(scope="module")
def registration(lab):
    a, reg = lab.node("a"), lab.node("reg")
    quiet(lab)
    line = "register => %s:%s@%s:%d"
    with lab.scenario("registration", capture=["a", "reg"], hosts=["a", "reg"]) as sc:
        a.register_lines([line % (a.node, asl_lab.REG_SECRET, reg.ip, lab.port)])
        deadline = time.monotonic() + 10
        while "Registered" not in a.cli("iax2 show registry") and time.monotonic() < deadline:
            time.sleep(0.25)
        sc.note("registry_ok", a.cli("iax2 show registry"))
        a.register_lines([])
        time.sleep(0.5)
        a.register_lines([line % (a.node, "wrong-secret-not-real", reg.ip, lab.port)])
        deadline = time.monotonic() + 10
        while "Rejected" not in a.cli("iax2 show registry") and time.monotonic() < deadline:
            time.sleep(0.25)
        sc.note("registry_rejected", a.cli("iax2 show registry"))
        a.register_lines([])
        time.sleep(0.5)
    return finish(lab, sc, ["a", "reg"])


def dns_query(server, name, qtype, ident=0x4455):
    """One UDP DNS question; returns (rcode, [(type, rdata bytes)])."""
    qname = b"".join(bytes((len(p),)) + p.encode() for p in name.split(".")) + b"\0"
    msg = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.sendto(msg, (server, 53))
        data, _ = sock.recvfrom(4096)
    parsed = iax2_frames.parse_dns(data)
    return parsed["rcode"], parsed["answers"]


@pytest.fixture(scope="module")
def dns_zone(lab):
    reg, e = lab.node("reg"), lab.node("e")
    answers = {}
    with lab.scenario("dns-zone", capture=["reg"], hosts=["reg"]) as sc:
        base = "%s.%s" % (e.node, lab.domain)
        answers["srv"] = dns_query(reg.ip, "_iax._udp." + base, 33)
        answers["a"] = dns_query(reg.ip, base, 1)
        answers["txt"] = dns_query(reg.ip, base, 16)
        answers["missing"] = dns_query(reg.ip, "_iax._udp.399999.%s" % lab.domain, 33)
        answers["register"] = dns_query(reg.ip, "register.%s" % lab.domain, 1)
    ran = finish(lab, sc, ["reg"])
    ran.answers = answers
    return ran


@pytest.fixture(scope="module")
def long_call(lab):
    if os.environ.get("DVT_ASL_LONG") != "1":
        pytest.skip("75 s scenario: set DVT_ASL_LONG=1")
    a, e = lab.node("a"), lab.node("e")
    quiet(lab)
    data = asl_lab.tone_ulaw(70000)
    a.put_media("tone70.ulaw", data)
    with lab.scenario("long-call", capture=["a", "e"]) as sc:
        sc.note("tone70_sha256", hashlib.sha256(data).hexdigest())
        a.cli("rpt fun %s *3%s" % (a.node, e.node))
        a.wait_link(e.node)
        time.sleep(1.0)
        a.cli("rpt playback %s /var/lib/asterisk/sounds/dvt/tone70" % a.node)
        time.sleep(73.0)
        a.cli("rpt fun %s *1%s" % (a.node, e.node))
        time.sleep(1.5)
    return finish(lab, sc, ["a", "e"], limit=3)


# ------------------------------------------------------------------ cases

def test_link_dns_handshake(link_dns):
    types = link_dns.types("a")
    assert types["iax.NEW"] >= 2, "NEW without and with a call token"
    assert types["iax.CALLTOKEN"] == 1
    assert types["iax.ACCEPT"] == 1
    assert types["control.ANSWER"] == 1
    assert types["iax.HANGUP"] >= 1
    assert types["iax.ACK"] > 3
    first, second = link_dns.frames("a", "iax.NEW")[:2]
    assert {i["name"]: i for i in first["ies"]}["CALLTOKEN"]["len"] == 0
    assert {i["name"]: i for i in second["ies"]}["CALLTOKEN"]["len"] > 0
    names = {i["name"]: i for i in second["ies"]}
    assert names["USERNAME"]["text"] == "radio" and names["CALLING_NUMBER"]["text"] == "199901"
    assert names["CALLED_NUMBER"]["text"] == "299901"


def test_link_dns_text_frames(link_dns):
    texts = link_dns.texts("a")
    assert "!NEWKEY1!" in texts
    assert "!!DISCONNECT!!" in texts
    assert any(t.startswith("T 199901 CONNECTED") for t in texts)
    assert any(t.startswith("L ") for t in texts)
    assert "!NEWKEY1!" in link_dns.texts("e")


def test_link_dns_voice_and_control(link_dns):
    types = link_dns.types("a")
    assert types["voice.full"] >= 1 and types["voice.mini"] > 100
    assert types.get("control.RADIO_UNKEY", 0) >= 1
    assert link_dns.frames("a", "voice.full")[0]["codec"] == "ulaw"
    assert all(f["payload_len"] == 160 for f in link_dns.frames("a", "voice.mini"))


def test_link_dns_lookup_goes_through_the_lab_zone(link_dns, lab):
    asked = {q["name"]: m for m in link_dns.docs["a"]["dns"] for q in m["questions"] if m["qr"] == 1 and m["answers"]}
    base = "299901." + lab.domain
    assert "_iax._udp." + base in asked and base in asked
    srv = asked["_iax._udp." + base]["answers"][0]
    assert srv["text"].startswith("0 0 %d " % lab.port)
    assert asked[base]["answers"][0]["text"] == lab.node("e").ip


def test_link_dns_tone_recorded_at_far_side(link_dns, lab):
    assert_tone(link_dns.rx("e", "from-199901"), "tone at E")
    meta = json.loads((link_dns.sc.dir / "scenario.json").read_text())
    assert meta["notes"]["tone_sha256"] == hashlib.sha256(asl_lab.tone_ulaw(TONE_MS)).hexdigest()
    assert meta["pcaps"]["a"] > 1000


def test_account_md5_auth(account_md5):
    types = account_md5.types("a")
    assert types["iax.AUTHREQ"] == 2 and types["iax.AUTHREP"] == 2
    assert types["iax.ACCEPT"] == 2
    req = account_md5.frames("a", "iax.AUTHREQ")[0]
    ies = {i["name"]: i for i in req["ies"]}
    assert ies["AUTHMETHODS"]["int"] & 2, "md5 offered"
    assert len(ies["CHALLENGE"]["text"]) > 0
    rep = account_md5.frames("a", "iax.AUTHREP")[0]
    assert len({i["name"]: i for i in rep["ies"]}["MD5_RESULT"]["text"]) == 32


def test_call_token_is_offered_even_when_not_required(account_md5, legacy_key):
    # Asterisk's own client opens with an empty CALLTOKEN IE and the server answers with a token for
    # "acct" (requirecalltoken=no) as well as for "acct-ct"; only a client that sends no such IE
    # (the tester's, legacy_key) gets in without the exchange
    news = account_md5.frames("a", "iax.NEW")
    users = [{i["name"]: i for i in f["ies"]}["USERNAME"]["text"] for f in news]
    assert users == ["acct", "acct", "acct-ct", "acct-ct"]
    assert account_md5.types("a")["iax.CALLTOKEN"] == 2
    assert "iax.CALLTOKEN" not in legacy_key.types("e")
    assert legacy_key.types("e")["iax.NEW"] == 1


def test_account_md5_tone_arrives(account_md5):
    files = account_md5.rx_files("e")
    assert len(files) == 2
    for f in files:
        assert_tone(f.read_bytes(), f.name)


def test_callee_mode_c(callee):
    types = callee.types("e")
    assert types["iax.NEW"] >= 1 and types["control.ANSWER"] == 1
    assert "!NEWKEY1!" in callee.texts("b")
    assert any(t == "L T199901" or t.startswith("L T199901") for t in callee.texts("e")), \
        "B's link list (A) reaches the edge: %s" % callee.texts("e")


def test_callee_tone_reaches_b_and_a(callee):
    assert_tone(callee.rx("b", "from-299901"), "tone at B")
    assert_tone(callee.rx("a", "from-199902"), "tone at A through B")


def test_loop_through_three_nodes(loop):
    for host in ("a", "b", "e"):
        assert "RPT_NUMALINKS=2" in loop.sc.notes["variables_asl-" + host], "two direct links each"
    # the loop shows as a node listed twice in the tree a node learned from the L messages
    trees = [line.split("=", 1)[1].split(",")[1:] for host in ("a", "b", "e")
             for line in loop.sc.notes["variables_asl-" + host].splitlines() if line.strip().startswith("RPT_LINKS=")]
    assert any(len(tree) != len(set(tree)) for tree in trees), trees
    assert any(t.startswith("L ") and "T" in t for t in loop.texts("e"))
    assert loop.types("e")["iax.NEW"] >= 2


def test_legacy_key_unkey_both_ways(legacy_key, lab):
    types = legacy_key.types("e")
    assert types["iax.AUTHREQ"] == 1 and types["iax.AUTHREP"] == 1
    keyed = legacy_key.frames("e", "control.RADIO_KEY") + legacy_key.frames("e", "control.RADIO_UNKEY")
    from_e = [f for f in keyed if f["src"].split(":")[0] == lab.node("e").ip]
    from_client = [f for f in keyed if f["src"].split(":")[0] != lab.node("e").ip]
    assert from_e and from_client
    assert 12 in legacy_key.client_controls and 13 in legacy_key.client_controls
    assert "!NEWKEY!" in legacy_key.texts("e")


def test_legacy_client_hears_e(legacy_key):
    media = asl_lab.ulaw_to_pcm(legacy_key.client_media)
    assert_tone(media, "tone at the IAX2 client", min_ms=1000)
    assert_tone(legacy_key.rx("e", "acct"), "tone at E from the client", min_ms=1500)


def test_ping_pong_lagrq_lagrp_answered(legacy_key):
    types = legacy_key.types("e")
    assert types["iax.PING"] >= 1 and types["iax.PONG"] >= 1
    assert types["iax.LAGRQ"] >= 1 and types["iax.LAGRP"] >= 1


def test_minimal_new_is_accepted_and_answered(minimal_new, lab):
    news = minimal_new.frames("e", "iax.NEW")
    first = [{i["name"]: i for i in f["ies"]} for f in news][0]
    assert list(first) == ["VERSION", "CALLED_NUMBER", "CALLING_NUMBER", "USERNAME", "FORMAT", "FORMAT2",
                           "CAPABILITY", "CAPABILITY2", "CALLTOKEN"]
    assert first["CALLTOKEN"]["len"] == 0
    assert 4 in minimal_new.out["good_controls"], "ANSWER"
    assert "!NEWKEY1!" in minimal_new.texts("e")
    assert any(t.startswith("L") for t in minimal_new.texts("e"))
    assert minimal_new.frames("e", "voice.full")[0]["codec"] == "ulaw"


def test_minimal_new_voice_without_key_is_heard(minimal_new):
    variables = minimal_new.sc.notes["variables_during_voice"]
    assert "299902TK" in variables and "RPT_TXKEYED=1" in variables, variables
    assert not [f for f in minimal_new.frames("e", "control.RADIO_KEY")], "no RADIO_KEY on a new-mode link"
    assert_tone(minimal_new.rx("e", "from-299902"), "tone at E from the tester", min_ms=1000)


def test_minimal_new_wrong_address_is_refused(minimal_new):
    assert minimal_new.out["bad"].startswith("refused"), minimal_new.out
    assert 4 not in minimal_new.out["bad_controls"], "no ANSWER"
    assert minimal_new.types("e")["iax.HANGUP"] >= 2
    rx = minimal_new.rx("e", "from-199901")
    assert len(asl_lab.loud_part(rx)) == 0


def test_registration_ok_and_rejected(registration):
    types = registration.types("reg")
    assert types["iax.REGREQ"] >= 4
    assert types["iax.REGAUTH"] >= 2 and types["iax.REGACK"] == 1 and types["iax.REGREJ"] == 1
    ack = {i["name"]: i for i in registration.frames("reg", "iax.REGACK")[0]["ies"]}
    assert ack["REFRESH"]["int"] == 180 and ack["USERNAME"]["text"] == "199901"
    assert ack["APPARENT_ADDR"]["addr"].endswith(":4569") or ack["APPARENT_ADDR"]["addr"]
    rej = {i["name"]: i for i in registration.frames("reg", "iax.REGREJ")[0]["ies"]}
    assert rej["CAUSE"]["text"] == "Registration Refused"
    assert "Registered" in registration.sc.notes["registry_ok"]
    assert "Rejected" in registration.sc.notes["registry_rejected"]
    md5s = [{i["name"]: i for i in f["ies"]}.get("MD5_RESULT") for f in registration.frames("reg", "iax.REGREQ")]
    assert len([m for m in md5s if m]) >= 2


def test_dns_zone_shape(dns_zone, lab):
    rc, srv = dns_zone.answers["srv"]
    assert rc == 0 and srv[0]["text"] == "0 0 %d 299901.%s" % (lab.port, lab.domain)
    rc, a = dns_zone.answers["a"]
    assert rc == 0 and a[0]["text"] == lab.node("e").ip and a[0]["ttl"] <= 60
    rc, txt = dns_zone.answers["txt"]
    assert rc == 0 and txt[0]["text"] == '"NN=299901" "IP=%s" "PT=%d"' % (lab.node("e").ip, lab.port)
    assert dns_zone.answers["missing"][0] == 3, "an unknown node is NXDOMAIN"
    assert dns_zone.answers["register"][1][0]["text"] == lab.node("reg").ip
    assert len(dns_zone.docs["reg"]["dns"]) == 10


def test_long_call_timestamp_wrap_ping_lagrq(long_call):
    wraps = long_call.docs["a"]["ts16_wraps"]
    assert wraps, "no mini-frame ts16 wrap in a 75 s call"
    w = wraps[0]
    assert w["before"]["ts16"] > 0xF000 and w["after"]["ts16"] < 0x1000
    types = long_call.types("a")
    assert types["iax.PING"] >= 2 and types["iax.PONG"] >= 2
    assert types["iax.LAGRQ"] >= 5 and types["iax.LAGRP"] >= 5
    assert_tone(long_call.rx("e", "from-199901"), "tone at E", min_ms=60000)
