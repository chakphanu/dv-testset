# SPDX-License-Identifier: MIT
"""DV-Router's AllStar edge (dv-edge-allstar) against the asl-lab (--profile asl-edge,
docs/asl-edge-results.md). The edge runs natively in xlxd-sut as node 1999, room L
(rig/config/dv-router/allstar-lab.toml), next to the lab's app_rpt nodes A (199901),
B (199902) and E (299901, left idle here; it is only the edge's own monitor-mode
outbound target, DVT_ASL_EDGE_OUT=1). A node called "radio" with no account; "acct"
is the edge's IAX2 client account (md5, talk, room L).

Run:
    DVT_ASL_EDGE_OUT=1 XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter \\
      DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<dv-router.toml template> \\
      scripts/run.sh --profile asl-edge -m asl_lab -- cases/test_asl_edge.py
    (+ DVT_ASL_LONG=1 for T1 and T2)

Registration cases (R1-R6, the edge's HTTPS registrar client) are not here: no
server URL override exists yet in this image (docs/asl-edge-results.md).
"""
import json
import os
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dvtest import asl_lab, iax2_frames
from dvtest.control import RigControl
from dvtest.proto import iax2, iax2_reg

pytestmark = [pytest.mark.rig, pytest.mark.asl_lab]

TONE_MS = 3000
REDIAL_WATCH_S = 90      # T8b: how long to wait for a redial after a plain HANGUP
DISC_WATCH_S = 70       # T8b: how long to watch for a (forbidden) redial after !!DISCONNECT!!


@pytest.fixture(scope="module")
def lab():
    lab = asl_lab.from_env()
    if lab is None or lab.hosts.get("edge") is None:
        pytest.skip("no AllStar edge: run scripts/run.sh --profile asl-edge (the tester gets DVT_ASL_EDGE=on)")
    lab.wait_ready()
    lab.write_versions()
    sha, size = lab.put_tone(TONE_MS, hosts=("a", "b", "e"))
    lab.tone_sha256 = sha
    assert size == 8 * TONE_MS
    return lab


@pytest.fixture(scope="module")
def edge_control(lab):
    """RigControl without the shared `rig` fixture (which needs xlxd-ref/DExtra, not started
    under --profile asl-edge). Same control dir, same request protocol."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1 is set by scripts/run.sh)")
    return RigControl(Path(lab.out) / "control")


@pytest.fixture(autouse=True)
def loadavg_per_case(request):
    """Host loadavg (1 min, /proc/loadavg is not namespaced) at the start and the end of every case, appended to
    out/<run>/asl-lab/loadavg.jsonl: timing-sensitive cases (T9, T11, the 10 minute hold) are judged with it."""
    def load():
        try:
            return float(open("/proc/loadavg").read().split()[0])
        except OSError:
            return None
    start, t0 = load(), time.time()
    yield
    path = Path(os.environ.get("DVT_OUT", "out")) / "asl-lab" / "loadavg.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({"case": request.node.name, "load_start": start, "load_end": load(),
                             "seconds": round(time.time() - t0, 1)}) + "\n")


def quiet(lab, keys=("a", "b")):
    lab.quiet(keys)


def tone_client(lab, username="acct", secret=None, calltoken=True, wait_newkey=False, timeout=10):
    """A tester IAX2 client of the edge's "acct" account (md5, ulaw)."""
    edge = lab.node("edge")
    c = iax2.Client(edge.ip, secret or asl_lab.ACCT_SECRET, username=username, port=lab.port, codec=iax2.ULAW)
    c.call(edge.node, caller="2999", name="N0TEST", timeout=timeout, empty_token=calltoken, wait_newkey=wait_newkey)
    return c


def assert_tone(pcm, label, min_ms=1500):
    loud = asl_lab.loud_part(pcm)
    assert len(loud) >= min_ms * 16, "%s: only %d ms of audio above the floor" % (label, len(loud) // 16)
    rms, ratio = asl_lab.tone_energy(loud)
    assert rms > 1000, "%s: rms %.0f" % (label, rms)
    assert ratio > 0.9, "%s: %.2f of the power at 1 kHz" % (label, ratio)


def edge_out_pcm(sc, lab, dst_key):
    """PCM of the voice the edge sent toward lab host `dst_key`, read from the tap's pcap."""
    edge, dst = lab.node("edge"), lab.node(dst_key)
    raw = asl_lab.ulaw_from_pcap(sc.dir / "edge.pcap", edge.ip, dst.ip, lab.port)
    return asl_lab.ulaw_to_pcm(raw)


def play_tone(talker):
    talker.cli("rpt playback %s /var/lib/asterisk/sounds/dvt/tone1k" % talker.node)


def hear_tone(talker, c, tries=6, gap=45.0, floor=300):
    """Play the 3 s tone from `talker` until the listening client `c` hears it; returns (attempts, pcm).

    FINDING (docs/asl-edge-results.md "overs cut after grant"): the edge intermittently grants an over and
    ends it at once (RADIO_KEY then RADIO_UNKEY to the listeners within 0.1 ms, no voice forwarded), then keeps
    doing so until about 3 minutes of idle (floor-control.md: the 181.6 s stale-stream expiry). Cases whose
    subject is something else (a 10 minute hold, TOT, loss) therefore first look for a working over: a probe
    every `gap` seconds, at most `tries` times, the attempt count recorded. The cases that are about the
    cut itself (test_back_to_back_overs_are_not_cut) do not use this."""
    for attempt in range(1, tries + 1):
        c.media.clear()
        play_tone(talker)
        c.pump(TONE_MS / 1000 + 1.5)
        pcm = asl_lab.ulaw_to_pcm(b"".join(c.media))
        if len(asl_lab.loud_part(pcm, floor=floor)) >= 1000 * 16:
            return attempt, pcm
        c.pump(gap)
    raise AssertionError("no over reached the listener in %d probes %.0f s apart (the edge cuts overs, see "
                         "docs/asl-edge-results.md)" % (tries, gap))


# ------------------------------------------------------------------ T5: identity / wrong address

def test_t5_wrong_source_ip_is_refused_before_answer(lab):
    """A caller claiming a statically-known node number (199901, host <net>.42) but sending from a
    different address: ACCEPT then HANGUP, never ANSWER (allstar.md, "Identity ... Validated refusal
    supplies ACCEPT followed by HANGUP, never ANSWER")."""
    quiet(lab)
    edge = lab.node("edge")
    bad = iax2.Client(edge.ip, "", username="radio", port=lab.port, codec=iax2.ULAW)
    try:
        elements = b"".join(iax2.information(code, value) for code, value in (
            (11, struct.pack("!H", 2)), (1, str(edge.node).encode()), (2, b"199901"), (6, b"radio"),
            (9, struct.pack("!I", iax2.ULAW)), (56, b"\0" + struct.pack("!Q", iax2.ULAW)),
            (8, struct.pack("!I", iax2.ULAW)), (55, b"\0" + struct.pack("!Q", iax2.ULAW))))
        try:
            bad.call(edge.node, elements=elements, newkey=b"!NEWKEY1!", empty_token=True, wait_newkey=False, timeout=4)
            refused = False
        except (TimeoutError, ConnectionError):
            refused = True
        assert refused or 4 not in bad.controls, "no ANSWER for a claimed node from the wrong source address"
        assert bad.closed or bad.accepted, "the edge answered the NEW (ACCEPT) before refusing"
    finally:
        bad.close()


def test_t5_right_source_ip_is_accepted(lab):
    """The same claim from the real address of 199901 (asl-a) is accepted and answered, for contrast."""
    quiet(lab)
    a, edge = lab.node("a"), lab.node("edge")
    a.cli("rpt fun %s *3%s" % (a.node, edge.node))
    try:
        a.wait_link(edge.node, timeout=15)
    finally:
        a.disconnect_links()


# ------------------------------------------------------------------ T10: IAX2 client account

def test_t10_client_account_no_registration_needed(lab):
    """The edge's "acct" client account: MD5 AUTHREQ/AUTHREP, ACCEPT, ANSWER, no REGREQ, keyed with
    RADIO_KEY/RADIO_UNKEY (not NEWKEY1: it is a client account, not a node account)."""
    quiet(lab)
    with lab.scenario("t10-client", capture=["edge"]) as sc:
        c = tone_client(lab, calltoken=True)
        try:
            c.pump(0.3)
            assert 4 in c.controls, "no ANSWER"
            c.key()
            tone = asl_lab.tone_ulaw(1500)
            for i in range(0, len(tone) - 159, 160):
                c.voice(tone[i:i + 160])
                c.pump(0.02)
            c.unkey()
            c.pump(0.5)
        finally:
            c.close()
        time.sleep(0.5)
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    summary = doc["summary"]
    assert summary.get("iax.AUTHREQ") == 1 and summary.get("iax.AUTHREP") == 1
    assert "iax.REGREQ" not in summary, "no registration for a plain client account"
    ies = {i["name"]: i for i in doc["types"]["iax.AUTHREQ"][0]["ies"]}
    assert ies["AUTHMETHODS"]["int"] & 2, "md5 offered"
    assert len(doc["types"]["control.RADIO_KEY"]) >= 1


# ------------------------------------------------------------------ T3: hub, L list exchange

def test_t3_hub_l_lists_split_horizon(lab):
    """A and B both link to the edge; the edge's own monitor outbound to E (DVT_ASL_EDGE_OUT=1) is
    already up. The L list the edge sends each peer is built per destination (that peer's own branch
    excluded) with the right mode letter (T transceive, R monitor)."""
    quiet(lab)
    a, b = lab.node("a"), lab.node("b")
    with lab.scenario("t3-hub", capture=["edge"]) as sc:
        a.cli("rpt fun %s *3%s" % (a.node, lab.node("edge").node))
        a.wait_link(lab.node("edge").node)
        b.cli("rpt fun %s *3%s" % (b.node, lab.node("edge").node))
        b.wait_link(lab.node("edge").node)
        time.sleep(2.0)
        a.disconnect_links()
        b.disconnect_links()
        time.sleep(1.5)
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    edge, a_ip, b_ip, e_ip = lab.node("edge").ip, a.ip, b.ip, lab.node("e").ip
    all_texts = [f for f in doc["types"].get("text", []) if f["src"].split(":")[0] == edge
                 and f.get("text", "").rstrip("\0").startswith("L ")]

    def last_mentioning(dst_ip, needles):
        hits = [f["text"].rstrip("\0") for f in all_texts if f["dst"].split(":")[0] == dst_ip
                and all(n in f["text"] for n in needles)]
        assert hits, "no L list to %s mentions all of %s: %s" % (dst_ip, needles, [t["text"] for t in all_texts])
        return hits[-1]

    assert "199901" not in last_mentioning(a_ip, ["T199902", "R299901"])
    assert "199902" not in last_mentioning(b_ip, ["T199901", "R299901"])
    assert "299901" not in last_mentioning(e_ip, ["T199901", "T199902"])


# ------------------------------------------------------------------ T4: loop A + B + edge

def test_t4_loop_a_b_edge_no_loop_forms(lab):
    """A and B both link to the edge, and (in the same instant) A also dials B directly: a triangle.
    In this lab, A's own node_lookup for B goes through the lab's DNS zone (slower than the edge's L
    list reaching A), so by the time A's caller-side pre-check runs it already knows B through the edge
    and refuses locally (REMALREADY, rpt/rpt_link.c) before any packet to B is sent: the loop is
    prevented before it forms, not detected-and-torn-down afterwards. Only two of the three links survive."""
    quiet(lab)
    a, b, edge = lab.node("a"), lab.node("b"), lab.node("edge")
    with lab.scenario("t4-loop", capture=["edge"]) as sc:
        with ThreadPoolExecutor(3) as pool:
            jobs = [pool.submit(a.cli, "rpt fun %s *3%s" % (a.node, edge.node)),
                    pool.submit(b.cli, "rpt fun %s *3%s" % (b.node, edge.node)),
                    pool.submit(a.cli, "rpt fun %s *3%s" % (a.node, b.node))]
            for job in jobs:
                job.result()
        time.sleep(4.0)
        a_vars, b_vars = a.cli("rpt show variables %s" % a.node), b.cli("rpt show variables %s" % b.node)
        a.disconnect_links()
        b.disconnect_links()
        time.sleep(1.5)
    assert "RPT_NUMLINKS=1" in a_vars and "RPT_LINKS=1,T1999" in a_vars, a_vars
    assert "RPT_NUMLINKS=1" in b_vars and "RPT_LINKS=1,T1999" in b_vars, b_vars
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    texts = [f["text"].rstrip("\0") for f in doc["types"].get("text", []) if f["src"].split(":")[0] == a.ip]
    assert any("REMALREADY" in t for t in texts), "A's own pre-check did not refuse the direct dial: %s" % texts
    # no stray HANGUP from the edge mid-session (it never had to intervene: the loop never reached the wire)
    hangups = [f for f in doc["types"].get("iax.HANGUP", []) if f["t"] < 3.5]
    assert not hangups, "unexpected mid-session HANGUP: %s" % hangups


def test_t4b_loop_through_the_edge_is_broken_by_the_edge(lab):
    """The edge in the path of a real loop. Needs a rig WITHOUT the edge's outbound rows (DVT_ASL_EDGE_OUT unset):
    with them, B and E are already linked to the edge and app_rpt's caller-side check refuses any dial that
    would close a loop (test_t4_loop_a_b_edge_no_loop_forms). Here A-E and B-E are linked first (a chain
    A - E - B), then A and B dial the edge in the same instant: neither knows yet that the edge is reachable
    through the other, so both links come up and a loop A - edge - B - E - A exists. The edge learns it from
    the nodes' L lists (design rule 3) and must drop the youngest branch (DISCONNECT, HANGUP, 60 s hold-down).
    Recorded: which link dropped and after how long, and whether any audio is forwarded twice afterwards."""
    if os.environ.get("DVT_ASL_EDGE_OUT") == "1":
        pytest.skip("needs a rig without the edge's outbound rows (DVT_ASL_EDGE_OUT unset)")
    a, b, e, edge = lab.node("a"), lab.node("b"), lab.node("e"), lab.node("edge")
    lab.quiet(("a", "b", "e"))
    with lab.scenario("t4b-loop", capture=["edge"]) as sc:
        a.cli("rpt fun %s *3%s" % (a.node, e.node))
        a.wait_link(e.node, timeout=15)
        b.cli("rpt fun %s *3%s" % (b.node, e.node))
        b.wait_link(e.node, timeout=15)
        time.sleep(3.0)                          # the chain's L lists settle
        with ThreadPoolExecutor(2) as pool:
            for job in [pool.submit(a.cli, "rpt fun %s *3%s" % (a.node, edge.node)),
                        pool.submit(b.cli, "rpt fun %s *3%s" % (b.node, edge.node))]:
                job.result()
        time.sleep(8.0)                          # L lists cross (150-249 ms kick, then 30 s cadence)
        links = {k: lab.node(k).cli("rpt show variables %s" % lab.node(k).node) for k in ("a", "b")}
        sc.note("links_a", links["a"])
        sc.note("links_b", links["b"])
        c = tone_client(lab)
        try:
            c.pump(0.5)
            c.media.clear()
            play_tone(a)
            c.pump(TONE_MS / 1000 + 1.5)
            heard_ms = len(asl_lab.loud_part(asl_lab.ulaw_to_pcm(b"".join(c.media)), floor=300)) // 16
        finally:
            c.close()
        sc.note("client_heard_ms", heard_ms)
        lab.quiet(("a", "b", "e"))
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    drops = [(f["t"], f["dst"].split(":")[0]) for f in doc["types"].get("iax.HANGUP", [])
             if f["src"].split(":")[0] == edge.ip and f["t"] < 12]
    # the notes were taken after the scenario closed: write them into its scenario.json as well
    meta_path = sc.dir / "scenario.json"
    meta = json.loads(meta_path.read_text())
    meta["notes"]["edge_hangups_before_t12"] = drops
    voice_from = {k: sum(1 for f in doc["types"].get("voice.mini", []) if f["src"].split(":")[0] == lab.node(k).ip)
                  for k in ("a", "b")}
    meta["notes"]["voice_into_edge"] = voice_from
    meta_path.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    # the loop must not survive: the edge hung up on one of the two before the audio probe
    assert drops, "the edge never dropped a branch of the loop A - edge - B - E - A: %s %s" % (links["a"], links["b"])
    assert not (voice_from["a"] > 50 and voice_from["b"] > 50), "A's over reached the edge from both A and B: %s" % voice_from


# ------------------------------------------------------------------ T6 / T7: outbound dial, redial after
# our restart; T8: redial after the ASL3 peer's restart

def _wait_established(host, peer_node, timeout, direction=None):
    """Like Host.wait_link, but also checks DIRECTION ("IN" or "OUT") when given; returns the matching line."""
    deadline = time.monotonic() + timeout
    out = ""
    while time.monotonic() < deadline:
        out = host.cli("rpt lstats %s" % host.node)
        for line in out.splitlines():
            parts = line.split()
            if parts[:1] == [str(peer_node)] and "ESTABLISHED" in line and (direction is None or direction in parts):
                return line
        time.sleep(1)
    raise AssertionError("%s: no ESTABLISHED (%s) link to %s after %.0f s:\n%s"
                         % (host.name, direction or "either direction", peer_node, timeout, out))


def _restart_edge(edge_control, settle=3.0):
    """kill-proc USR1 on dv-edge-allstar, with a settle margin: the launcher backs off 500 ms, then
    1000 ms, ... between consecutive restarts of that child (observed in out/<run>/logs/xlxd-sut.log),
    so a kill-proc that lands in that gap gets "error 3" (no such process) even though the edge is fine
    a moment later. Tests that restart more than once must not do it back to back without this margin."""
    edge_control.kill_proc("xlxd-sut", "dv-edge-allstar", "USR1", timeout=30)
    time.sleep(settle)


def test_t6_outbound_rows_reach_the_lab_nodes(lab, edge_control):
    """The edge's own configured outbound rows (allstar-lab-out.toml, DVT_ASL_EDGE_OUT=1): a permanent
    transceive row to B (199902) and a monitor row to E (299901). Restart the edge first (quiet reload,
    SIGUSR1) so this does not depend on what an earlier case left connected or disconnected."""
    b, e = lab.node("b"), lab.node("e")
    _restart_edge(edge_control)
    line_b = _wait_established(b, lab.node("edge").node, timeout=30, direction="IN")
    line_e = _wait_established(e, lab.node("edge").node, timeout=30, direction="IN")
    assert line_b and line_e  # monitor mode itself is checked by test_t6_monitor_is_listen_only


def test_t6_monitor_is_listen_only(lab, edge_control):
    """The monitor row to E (299901): a talker in the room (A through its own link, heard by the acct client)
    must produce no voice on the edge's call to E (deployment.md: "Monitor outbound rows are receive-only
    from our perspective ... no room audio or KEY is sent to that node"). The edge is restarted INSIDE the
    scenario window so E's recording of the call lands in this scenario's directory."""
    a, e, edge = lab.node("a"), lab.node("e"), lab.node("edge")
    with lab.scenario("t6-monitor", capture=["edge", "e"]) as sc:
        _restart_edge(edge_control)
        _wait_established(e, edge.node, timeout=30, direction="IN")
        a.disconnect_links()
        a.hangup_all()
        a.cli("rpt fun %s *3%s" % (a.node, edge.node))
        a.wait_link(edge.node, timeout=15)
        c = tone_client(lab)
        try:
            c.pump(0.5)
            attempts, _pcm = hear_tone(a, c)
            sc.note("probe_attempts", attempts)
        finally:
            c.close()
        time.sleep(0.5)
    to_e = asl_lab.ulaw_from_pcap(sc.dir / "edge.pcap", edge.ip, e.ip, lab.port)
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    keyed = [f for f in doc["types"].get("control.RADIO_KEY", []) if f["dst"].split(":")[0] == e.ip]
    assert not to_e and not keyed, "voice %d bytes and %d RADIO_KEY sent to the monitor-mode row" % (len(to_e), len(keyed))


def test_t7_edge_restart_recovers_permanent_link(lab, edge_control):
    """A links to the edge; a quiet reload restart of the edge (SIGUSR1, HANGUP without DISCONNECT per
    allstar.md "restart_stop") does not stop A's own redial policy (A received no DISCONNECT), so the
    link comes back without any action from the test."""
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    a.cli("rpt fun %s *3%s" % (a.node, edge.node))
    a.wait_link(edge.node, timeout=15)
    _restart_edge(edge_control)
    line = _wait_established(a, edge.node, timeout=25)
    assert line, "A's link to the edge did not recover after the edge's quiet restart"


def test_t8_lab_node_restart_recovers_edge_outbound(lab, edge_control):
    """B's own container restarts (a hard loss: no DISCONNECT is sent, unlike A's or B's own `rpt fun
    *6`). The edge's permanent outbound row to B is not told to stop, so its normal redial policy
    should reconnect once B is reachable again. No extra edge restart here: B's permanent row from
    test_t6_outbound_rows_reach_the_lab_nodes is still up (nothing in between touches it), and adding
    one would race the launcher's own restart backoff (see _restart_edge)."""
    b, edge = lab.node("b"), lab.node("edge")
    _wait_established(b, edge.node, timeout=30, direction="IN")
    edge_control("restart", "asl-b", timeout=60)
    b.ready(timeout=60)
    line = _wait_established(b, edge.node, timeout=90, direction="IN")
    assert line, "the edge did not redial B after B's container restarted"


def _new_from_edge_to(sc, lab, host_key, since=0.0):
    """Times (s from the capture start) of NEW frames the edge sent to lab host `host_key`."""
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    edge_ip, dst_ip = lab.node("edge").ip, lab.node(host_key).ip
    return [f["t"] for f in doc["types"].get("iax.NEW", [])
            if f["src"].split(":")[0] == edge_ip and f["dst"].split(":")[0] == dst_ip and f["t"] >= since]


def test_t8b_edge_redials_after_hangup_but_never_after_disconnect(lab, edge_control):
    """Design 3.5: the edge's permanent outbound row to B must redial after a loss WITHOUT !!DISCONNECT!!
    (a plain HANGUP) but must NEVER redial after a received !!DISCONNECT!!, permanent row or not.
    Step 1: B drops the edge's call with a plain channel hangup (no DISCONNECT text): the edge should dial
    again. Step 2: B disconnects with `rpt fun *6` (DISCONNECT text, then HANGUP): no NEW from the edge for
    DISC_WATCH_S seconds. The edge is restarted at the end so later cases find the row enabled."""
    b, edge = lab.node("b"), lab.node("edge")
    with lab.scenario("t8b-disconnect", capture=["edge"]) as sc:
        _restart_edge(edge_control)
        _wait_established(b, edge.node, timeout=30, direction="IN")
        t_step1 = time.monotonic()
        # plain hangup of the edge's channel on B, no DISCONNECT text first
        for ln in b.cli("core show channels concise").splitlines():
            if ln.startswith("IAX2/"):
                b.cli("channel request hangup " + ln.split("!")[0])

        def linked():
            return any(ln.split()[:1] == [str(edge.node)] and "ESTABLISHED" in ln
                       for ln in b.cli("rpt lstats %s" % b.node).splitlines())

        # app_rpt keeps an inbound link entry for 10 s after a lost call (DISC_TIME): wait until it is gone,
        # then for the edge's redial
        gone_deadline = time.monotonic() + 20
        while linked() and time.monotonic() < gone_deadline:
            time.sleep(0.5)
        sc.note("link_gone_seconds", round(time.monotonic() - t_step1, 1))
        deadline = time.monotonic() + REDIAL_WATCH_S
        redialed_after = None
        while time.monotonic() < deadline:
            if linked():
                redialed_after = time.monotonic() - t_step1
                break
            time.sleep(1)
        sc.note("step1_redial_seconds", None if redialed_after is None else round(redialed_after, 1))
        if redialed_after is not None:
            time.sleep(3)
            t_step2 = time.monotonic()
            b.disconnect_links()
            time.sleep(DISC_WATCH_S)
            sc.note("step2_watch_seconds", round(time.monotonic() - t_step2, 1))
        _restart_edge(edge_control)
        _wait_established(b, edge.node, timeout=30, direction="IN")
    news = _new_from_edge_to(sc, lab, "b")
    assert redialed_after is not None, "step 1: no redial within %d s after a plain HANGUP (NEW frames to B: %s)" % (
        REDIAL_WATCH_S, news)
    # NEW frames to B: the initial dial of the restart, the redial of step 1, then only the one after the
    # final restart; none between the DISCONNECT of step 2 and that restart
    disc = [f["t"] for f in iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))["types"].get("text", [])
            if f["src"].split(":")[0] == b.ip and f.get("text", "").startswith("!!DISCONNECT!!")]
    assert disc, "B never sent !!DISCONNECT!!"
    redial_after_disconnect = [t for t in news if disc[0] < t < disc[0] + DISC_WATCH_S]
    assert not redial_after_disconnect, "the edge redialled %s s after a received !!DISCONNECT!!" % (
        [round(t - disc[0], 1) for t in redial_after_disconnect])


# ------------------------------------------------------------------ T9: lossy, jittery egress

def test_t9_lossy_jittery_link_still_establishes_and_carries_audio(lab, edge_control):
    """80 ms jitter and 5 percent loss on the edge's egress (netem, profile asl-edge's netem-xlxd-sut
    sidecar): A still links and a tone still arrives recognisably at the acct client, allowing for the
    configured loss (a relaxed tone-ratio floor; assert_tone's 0.9 assumes a clean link). Retransmissions
    stay bounded and no over is split into several (one RADIO_UNKEY to the client for the probe)."""
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    edge_control.netem("edge", "delay 80ms 20ms distribution normal loss 5%")
    try:
        with lab.scenario("t9-netem", capture=["edge"]) as sc:
            a.cli("rpt fun %s *3%s" % (a.node, edge.node))
            a.wait_link(edge.node, timeout=20)
            c = tone_client(lab, calltoken=True, timeout=20)
            try:
                c.pump(0.5)
                attempts, pcm = hear_tone(a, c)
                sc.note("probe_attempts", attempts)
            finally:
                c.close()
            time.sleep(0.5)
        loud = asl_lab.loud_part(pcm, floor=300)
        rms, ratio = asl_lab.tone_energy(loud)
        assert rms > 500 and ratio > 0.5, "tone 1 kHz: rms %.0f ratio %.2f" % (rms, ratio)
        doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
        retrans = sum(1 for kind in doc["types"].values() for f in kind if f.get("retrans"))
        assert retrans < 100, "%d retransmitted frames in a few seconds" % retrans
    finally:
        edge_control.netem("edge", "off")


# ------------------------------------------------------------------ T11: bounded NEW/POKE abuse

def _fake_new(scall, called="1999", username="radio"):
    ies = b"".join(iax2.information(code, value) for code, value in (
        (11, struct.pack("!H", 2)), (1, called.encode()), (2, b"6999"), (6, username.encode()),
        (9, struct.pack("!I", iax2.ULAW)), (8, struct.pack("!I", iax2.ULAW))))
    return iax2.full_frame(scall, 0, 0, 0, 0, iax2.IAX, iax2.NEW, ies)


def _fake_poke(scall):
    return iax2.full_frame(scall, 0, 0, 0, 0, iax2.IAX, 30, b"")  # POKE = 30 (dvtest.proto.iax2 has no name for it)


def test_t11_new_poke_abuse_is_bounded_and_does_not_block_real_links(lab, edge_control):
    """A bounded burst of NEW and POKE datagrams with distinct fake call numbers from the tester's one
    rig address (no real source-IP spoofing is attempted here: the per-IP limits allstar.md documents,
    64 calls / 16 pending / 4 per IP / 2 NEW per s burst 5, are exactly what one source address should
    trigger). Checks: the edge process stays up and its cpu over the burst is bounded (proc-stat), and a
    real node can still link during and after it."""
    edge = lab.node("edge")
    before = edge_control.proc_stat("xlxd-sut", "dv-edge-allstar")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    t0 = time.monotonic()
    try:
        for scall in range(1, 61):
            sock.sendto(_fake_new(scall), (edge.ip, lab.port))
            if scall % 3 == 0:
                sock.sendto(_fake_poke(scall), (edge.ip, lab.port))
            time.sleep(0.02)
        # the edge still answers a real node's link while the burst's replies/backoff settle
        a = lab.node("a")
        a.disconnect_links()
        a.hangup_all()
        a.cli("rpt fun %s *3%s" % (a.node, edge.node))
        a.wait_link(edge.node, timeout=20)
    finally:
        sock.close()
    elapsed = time.monotonic() - t0
    after = edge_control.proc_stat("xlxd-sut", "dv-edge-allstar")

    def ticks_clk(text):
        kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        return int(kv["utime"]) + int(kv["stime"]), int(kv["clk_tck"]), int(kv["pid"])

    t_before, clk, pid_before = ticks_clk(before)
    t_after, _clk2, pid_after = ticks_clk(after)
    assert pid_before == pid_after, "the edge process restarted during the flood (it should not have)"
    cpu_s = (t_after - t_before) / clk
    assert cpu_s < elapsed * 1.5, "cpu time %.2fs over %.2fs wall (not bounded)" % (cpu_s, elapsed)


# ------------------------------------------------------------------ T1 / T2: the two long holds
# (DVT_ASL_LONG=1; everything else above is seconds, these are the only minutes-long cases)

HOLD_S = 600       # T1: "hold 10 min"
OVER_S = 190       # T2: "3 min over" (long enough to cross the room's 180 s TOT)


def _long():
    if os.environ.get("DVT_ASL_LONG") != "1":
        pytest.skip("long case: set DVT_ASL_LONG=1")


def test_t1_ten_minute_hold_audio_both_ways(lab):
    """A links to the edge and the link is held 10 minutes (PING/LAGRQ keep it alive on their own; the
    client pump just answers them); A -> edge -> client audio is checked at the start and again at the end,
    so a slow idle-timeout or leak that shows only after minutes is caught. The other direction
    (client -> edge -> A) is test_t1b_client_to_node_audio. The scenario's pcap is the whole hold: only
    keepalives and link lists, small."""
    _long()
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    with lab.scenario("t1-hold", capture=["edge"]) as sc:
        a.cli("rpt fun %s *3%s" % (a.node, edge.node))
        a.wait_link(edge.node, timeout=15)
        c = tone_client(lab, calltoken=True, timeout=15)
        try:
            c.pump(0.5)
            first, pcm = hear_tone(a, c)
            assert_tone(pcm, "A -> edge -> client at the start of the hold")
            sc.note("probe_attempts_start", first)
            deadline = time.monotonic() + HOLD_S
            while time.monotonic() < deadline:
                c.pump(min(5.0, deadline - time.monotonic()))
                line = next((ln for ln in a.cli("rpt lstats %s" % a.node).splitlines()
                             if ln.split()[:1] == [str(edge.node)]), "")
                assert "ESTABLISHED" in line, "the link dropped during the hold: %r" % line
            last, pcm = hear_tone(a, c)
            assert_tone(pcm, "A -> edge -> client at the end of the 10 minute hold")
            sc.note("probe_attempts_end", last)
        finally:
            c.close()
        time.sleep(0.5)
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    assert doc["summary"].get("iax.PING", 0) >= 20 and doc["summary"].get("iax.PONG", 0) >= 20, \
        "the edge answers A's PING (and sends its own) through the hold: %s" % doc["summary"]


def test_t1b_client_to_node_audio(lab):
    """The other half of "audio both ways": the acct client keys (RADIO_KEY) and sends 3 s of the tone, the
    edge relays it out on A's link: the voice the edge sent A (read from the still-running capture) is the
    tone. Retried (45 s apart, 6 times) because of the over-cut finding."""
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    tone = asl_lab.tone_ulaw(TONE_MS)
    with lab.scenario("t1b-client-to-node", capture=["edge"]) as sc:
        a.cli("rpt fun %s *3%s" % (a.node, edge.node))
        a.wait_link(edge.node, timeout=15)
        c = tone_client(lab, calltoken=True, timeout=15)
        got = 0
        try:
            c.pump(0.5)
            for attempt in range(1, 7):
                c.key()
                for i in range(0, len(tone) - 159, 160):
                    c.voice(tone[i:i + 160])
                    c.pump(0.02)
                c.unkey()
                c.pump(1.0)
                # the capture is still running (immediate mode): look at what the edge sent A so far
                live = sc.dir / "live.pcap"
                live.write_bytes(edge.fetch("pcap", "t1b-client-to-node.pcap"))
                heard = asl_lab.loud_part(asl_lab.ulaw_to_pcm(
                    asl_lab.ulaw_from_pcap(live, edge.ip, a.ip, lab.port)), floor=300)
                if len(heard) >= 1000 * 16:
                    got = attempt
                    break
                c.pump(45.0)
            sc.note("attempts", attempt)
        finally:
            c.close()
        time.sleep(0.5)
    assert got, "A never saw the client's over (6 attempts)"
    assert_tone(edge_out_pcm(sc, lab, "a"), "client -> edge -> A", min_ms=1000)


def test_back_to_back_overs_are_not_cut(lab):
    """Three overs from A, 1 s apart, the client listening throughout: each must reach the client."""
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    a.cli("rpt fun %s *3%s" % (a.node, edge.node))
    a.wait_link(edge.node, timeout=15)
    c = tone_client(lab, calltoken=True, timeout=15)
    heard = []
    try:
        c.pump(0.5)
        for _ in range(3):
            c.media.clear()
            play_tone(a)
            c.pump(TONE_MS / 1000 + 1.0)
            heard.append(len(asl_lab.loud_part(asl_lab.ulaw_to_pcm(b"".join(c.media)), floor=300)) // 16)
    finally:
        c.close()
    assert all(ms >= 1000 for ms in heard), "ms of tone heard in overs 1..3: %s" % heard


def test_t2_three_minute_over_tot(lab):
    """A plays one 190 s tone (a single continuous over): the acct client, listening the whole time,
    hears it for a while, then the room's 180 s TOT (floor-control.md, "Maximum continuous TX (TOT):
    180 s, then revoke") makes later media of that over rejected: the transport "quarantines the rest
    of a denied/revoked over" (allstar.md), so audio reaching the client stops well before the 190 s
    tone finishes, even though A keeps sending the whole time. A working over is looked for first (probe
    helper); the room is not usable for a further over for minutes afterwards (the finding), so a
    "TOT is per over, not a ban" follow-up is left to a later run."""
    _long()
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    tone = asl_lab.tone_ulaw(OVER_S * 1000)
    a.put_media("tone190.ulaw", tone)
    with lab.scenario("t2-tot", capture=["edge"]) as sc:
        a.cli("rpt fun %s *3%s" % (a.node, edge.node))
        a.wait_link(edge.node, timeout=15)
        c = tone_client(lab, calltoken=True, timeout=15)
        try:
            c.pump(0.5)
            sc.note("probe_attempts", hear_tone(a, c)[0])
            c.media.clear()
            a.cli("rpt playback %s /var/lib/asterisk/sounds/dvt/tone190" % a.node)
            t0 = time.monotonic()
            c.pump(OVER_S + 3.0)
            pcm = asl_lab.ulaw_to_pcm(b"".join(c.media))
            loud = asl_lab.loud_part(pcm)
            received_s = len(loud) / 16000
            sc.note("received_s", round(received_s, 1))
            sc.note("over_seconds", round(time.monotonic() - t0, 1))
            assert 120 <= received_s <= 183, (
                "expected the room's 180 s TOT to cut the client's audio well before the 190 s over ended, "
                "got %.0f s of audio" % received_s)
        finally:
            c.close()
        time.sleep(0.5)


def test_restart_then_over_five_times(lab, edge_control):
    """Five quiet restarts of the edge (SIGUSR1), each followed by one over A -> edge -> client that must be
    heard at the first attempt (no probe retry): the c199 image cut the first over after some restarts."""
    a, edge = lab.node("a"), lab.node("edge")
    a.disconnect_links()
    a.hangup_all()
    a.cli("rpt fun %s *3%s" % (a.node, edge.node))
    a.wait_link(edge.node, timeout=15)
    heard = []
    for _ in range(5):
        _restart_edge(edge_control)
        _wait_established(a, edge.node, timeout=30)
        c = tone_client(lab, calltoken=True, timeout=15)
        try:
            c.pump(0.5)
            c.media.clear()
            play_tone(a)
            c.pump(TONE_MS / 1000 + 1.5)
            heard.append(len(asl_lab.loud_part(asl_lab.ulaw_to_pcm(b"".join(c.media)), floor=300)) // 16)
        finally:
            c.close()
    assert all(ms >= 1000 for ms in heard), "ms of tone heard after restart 1..5: %s" % heard


# ------------------------------------------------------------------ R1-R6: HTTP registration (DVT_ASL_EDGE_REG=1)
# The stand-in registrar (rig/asl-lab-registrar.py on asl-reg:8082, plain HTTP) answers six public nodes one
# way each. Needs an image with [edges.allstar.register] server / insecure (dv-router:asl-c199b and later) and
# a rig started with DVT_ASL_EDGE_REG=1; run these cases on their own (-k "r1 or r2 ...") on a fresh rig, since
# their timing checks look at the request cadence since the edge started.

REG_NODES = {"1991": "ok", "1992": "port", "1993": "http500", "1994": "notext", "1995": "slow", "1996": "oversize"}


@pytest.fixture(scope="module")
def registrations(lab, edge_control):
    if os.environ.get("DVT_ASL_EDGE_REG") != "1":
        pytest.skip("registration cases: start the rig with DVT_ASL_EDGE_REG=1 (an image with register.server/insecure)")
    reg = lab.node("reg")
    deadline = time.monotonic() + 240
    rows = []
    while time.monotonic() < deadline:
        raw = reg.fetch("reg", "requests.jsonl").decode() if any(
            f["path"] == "requests.jsonl" for f in reg.files("reg")) else ""
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        per = {n: [r for r in rows if n in (r.get("body") or {}).get("data", {}).get("nodes", {})] for n in REG_NODES}
        if all(len(v) >= 2 for v in per.values()) and rows and rows[-1]["t"] - rows[0]["t"] >= 75:
            break
        time.sleep(5)
    sdir = lab.out / "asl-lab" / "register"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "requests.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    metrics = edge_control.metrics("xlxd-sut") if os.environ.get("DVT_RIG_CONTROL") == "1" else ""
    (sdir / "metrics.txt").write_text("\n".join(ln for ln in metrics.splitlines() if "allstar" in ln) + "\n")
    return {"rows": rows, "per": {n: [r for r in rows if n in (r.get("body") or {}).get("data", {}).get("nodes", {})]
                                  for n in REG_NODES},
            "metrics": metrics}


def _gaps(rows):
    return [round(b["t"] - a["t"], 1) for a, b in zip(rows, rows[1:])]


def _counter(metrics, result):
    for line in metrics.splitlines():
        if line.startswith("dv_edge_allstar_register_total") and 'result="%s"' % result in line:
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _shape_ok(row, node, lab):
    body = row["body"]
    assert row["path"] == "/" and row["headers"].get("Content-Type", "").startswith("application/json"), row["headers"]
    want = {"port": lab.port, "data": {"nodes": {node: {"node": node, "passwd": "test-secret-not-real", "remote": 0}}}}
    assert body == want, "request body %r (one node per request, port = the edge's IAX2 port)" % row["raw"]


def test_r1_success_registers_and_refreshes(lab, registrations):
    rows = registrations["per"]["1991"]
    assert len(rows) >= 2
    _shape_ok(rows[0], "1991", lab)
    assert 50 <= _gaps(rows)[0] <= 90, "refresh cadence %s (max(60, min(interval, refresh)) = 60 s)" % _gaps(rows)


def test_r2_other_port_is_a_mismatch(lab, registrations):
    rows = registrations["per"]["1992"]
    _shape_ok(rows[0], "1992", lab)
    assert _counter(registrations["metrics"], "mismatch") >= 1, "no mismatch counted: %s" % registrations["metrics"][:400]


def test_r3_http_500_is_retried_with_backoff(lab, registrations):
    rows = registrations["per"]["1993"]
    _shape_ok(rows[0], "1993", lab)
    assert 20 <= _gaps(rows)[0] <= 45, "first retry %s (30 s +/-20 percent expected)" % _gaps(rows)


def test_r4_success_without_the_text_is_a_failure(lab, registrations):
    rows = registrations["per"]["1994"]
    _shape_ok(rows[0], "1994", lab)
    assert 20 <= _gaps(rows)[0] <= 45, "retry %s" % _gaps(rows)


def test_r5_slow_reply_times_out_on_the_client(lab, registrations):
    rows = registrations["per"]["1995"]
    _shape_ok(rows[0], "1995", lab)
    # the reply comes after 20 s; the client gives up at 15 s (the core at 20 s), then backs off ~30 s
    assert 40 <= _gaps(rows)[0] <= 75, "retry after the timeout %s" % _gaps(rows)


def test_r6_oversize_reply_is_refused(lab, registrations):
    rows = registrations["per"]["1996"]
    _shape_ok(rows[0], "1996", lab)
    assert rows[0]["reply_bytes"] > 4096
    assert 20 <= _gaps(rows)[0] <= 45, "retry after the oversize reply %s" % _gaps(rows)


def test_r7_metrics_count_every_outcome(registrations):
    m = registrations["metrics"]
    assert _counter(m, "ok") >= 1 and _counter(m, "mismatch") >= 1
    assert _counter(m, "http") + _counter(m, "parse") + _counter(m, "tls") >= 4, m[:600]


# ------------------------------------------------------------------ R8-R12: phone-mode IAX2 registration (ASL1g, wip-c165c93)
# DVSwitch Mobile in "Phone mode IAX2 connection" registers to the edge before it calls. wip-7efdd33 / wip-71ab207 answer a REGREQ
# with REGREJ; wip-c165c93 and later are a registrar for the kind=client accounts ("acct": md5, REFRESH clamped to
# refresh_min_s / refresh_max_s = 60 / 300). On by default (wip-c165c93); DVT_ASL_PHONE_REG=0 skips them for an older image (docs/rig.md "Image generations").
# Contract (dv-router review-118 / ASL1g): an empty CALLTOKEN on a REGREQ is answered with the 41 byte CALLTOKEN; a REGREQ with no
# CALLTOKEN IE is dropped silently; the REGAUTH has the same fields for a known and an unknown user; a wrong proof is REGREJ with
# CAUSE "authentication failed" and CAUSECODE 29; a registration grants no call privilege (a call still goes NEW/AUTHREQ/AUTHREP).

PHONE_REG_STEPS = list(iax2_frames.PHONE_REGISTRATION)


@pytest.fixture(scope="module")
def phone_reg(lab):
    if os.environ.get("DVT_ASL_PHONE_REG") == "0":
        pytest.skip("phone-mode registration cases switched off (DVT_ASL_PHONE_REG=0: an image before ASL1g, wip-71ab207 and older)")
    return lab


def _client_reg_counters(lab):
    """(sum of dv_edge_allstar_client_reg_total, dv_edge_allstar_client_registrations) from the hub metrics, or None without a
    control channel."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return None
    text = RigControl(Path(lab.out) / "control").metrics("xlxd-sut")
    total = gauge = 0.0
    for line in text.splitlines():
        if line.startswith("dv_edge_allstar_client_reg_total"):
            total += float(line.rsplit(" ", 1)[1])
        elif line.startswith("dv_edge_allstar_client_registrations"):
            gauge = float(line.rsplit(" ", 1)[1])
    return total, gauge


def _register(lab, scenario, username, secret, **kw):
    """One registration attempt against the edge inside a capture; (result, capture doc)."""
    quiet(lab)
    edge = lab.node("edge")
    with lab.scenario(scenario, capture=["edge"]) as sc:
        result = iax2_reg.register(edge.ip, username, secret, port=lab.port, **kw)
        time.sleep(0.5)
    return result, iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))


def test_r8_phone_registration_md5_regack(lab, phone_reg):
    """REGREQ -> CALLTOKEN -> REGREQ with the token -> REGAUTH (md5) -> REGREQ with MD5_RESULT -> REGACK, REFRESH inside the
    edge's clamp 60..300, one registered account afterwards."""
    before = _client_reg_counters(lab)
    result, doc = _register(lab, "r8-phone-reg", "acct", asl_lab.ACCT_SECRET, refresh=60)
    assert result.outcome == "REGACK", "registration of the client account: %s" % result.steps
    assert result.steps == PHONE_REG_STEPS, result.steps
    assert 60 <= result.refresh <= 300, "REGACK REFRESH %s (clamped to refresh_min_s..refresh_max_s = 60..300)" % result.refresh
    steps = iax2_frames.registration_steps_from_doc(doc)
    ok, why = iax2_frames.judge_phone_registration(steps)
    assert ok, "on the wire: %s" % why
    authreq = doc["summary"].get("iax.REGAUTH", 0)
    assert authreq >= 1 and doc["summary"].get("iax.REGACK", 0) >= 1 and "iax.REGREJ" not in doc["summary"], doc["summary"]
    ies = {i["name"]: i for i in doc["types"]["iax.REGAUTH"][0]["ies"]}
    assert ies["AUTHMETHODS"]["int"] & 2, "md5 offered by the REGAUTH"
    if before is not None:
        # the edge publishes its metrics to the hub about every 10 s (the hub caches the snapshot): wait for the counters
        end, after = time.monotonic() + 45, before
        while time.monotonic() < end:
            after = _client_reg_counters(lab)
            if after[0] > before[0] and after[1] >= 1:
                break
            time.sleep(3)
        assert after[0] > before[0], "dv_edge_allstar_client_reg_total did not count the registration (%s -> %s)" % (before, after)
        assert after[1] >= 1, "dv_edge_allstar_client_registrations is %s after a REGACK" % after[1]


def test_r9_wrong_secret_is_regrej(lab, phone_reg):
    """A wrong MD5 proof: REGREJ (CAUSE authentication failed, CAUSECODE 29), no REGACK, quickly (the edge refuses within 100 ms)."""
    result, doc = _register(lab, "r9-phone-wrong-secret", "acct", "wrong-secret-not-real")
    assert result.outcome == "REGREJ", "a wrong secret: %s" % result.steps
    assert result.steps == ["REGREQ", "CALLTOKEN", "REGREQ:token", "REGAUTH", "REGREQ:md5", "REGREJ"], result.steps
    assert result.cause == "authentication failed" and result.causecode == 29, (result.cause, result.causecode)
    assert "iax.REGACK" not in doc["summary"]
    assert result.seconds < 3.0, "REGREJ took %.2f s (deadline 100 ms after the proof)" % result.seconds


def test_r10_unknown_user_is_indistinguishable_from_a_wrong_secret(lab, phone_reg):
    """REGAUTH has the same fields for a known and an unknown user and the REGREJ is the same (no account enumeration)."""
    known, doc_known = _register(lab, "r10-known", "acct", "wrong-secret-not-real")
    unknown, doc_unknown = _register(lab, "r10-unknown", "nobody-here", "wrong-secret-not-real")
    assert unknown.outcome == "REGREJ" and unknown.steps == known.steps, (known.steps, unknown.steps)
    assert (unknown.cause, unknown.causecode) == (known.cause, known.causecode)

    def shape(doc):
        # the USERNAME echo differs in length with the name typed; every other IE must have the same name and length
        return sorted((i["name"], i["len"]) for i in doc["types"]["iax.REGAUTH"][0]["ies"] if i["name"] != "USERNAME")
    assert shape(doc_known) == shape(doc_unknown), "REGAUTH fields differ between a known and an unknown user"


def test_r11_regreq_without_calltoken_is_dropped(lab, phone_reg):
    """A REGREQ with no CALLTOKEN IE at all gets no reply (amplification rule: nothing is sent to an unvalidated address)."""
    result, doc = _register(lab, "r11-no-token", "acct", asl_lab.ACCT_SECRET, calltoken=False, timeout=2.0)
    assert result.outcome == "timeout" and result.steps == ["REGREQ"], result.steps
    answers = [t for t in ("iax.CALLTOKEN", "iax.REGAUTH", "iax.REGACK", "iax.REGREJ") if t in doc["summary"]]
    assert not answers, "the edge answered a REGREQ without CALLTOKEN: %s" % answers


def test_r12_node_account_cannot_register_as_a_phone(lab, phone_reg):
    """Registration is for kind=client accounts: the node account number 1999 (public node, password) is refused like any
    wrong proof (dv-router review-118: node accounts are refused)."""
    result, _doc = _register(lab, "r12-node-account", "1999", asl_lab.ACCT_SECRET)
    assert result.outcome == "REGREJ", "a node account registered as a phone: %s" % result.steps
