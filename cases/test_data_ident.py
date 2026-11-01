# SPDX-License-Identifier: MIT
"""DV-Router MS3, the identity of data items (data.md 2.3, 7.6, 15.5): which
stream metadata and SMS the room controller admits, with [data] require_ident
true (the default) and false.

Only DV-Router with the data worker answers these (DVT_SUT_PROFILE=dvrouter,
DVT_SUT_LAUNCH=native, the config extra rig/config/dv-router/data-ident.toml and,
for the XLX cases, rig/config/dv-router/xlx-peer.toml with --profile interlink; see
docs/rig.md). Every case skips with the reason when one of them is missing. The
run's setting is DVT_REQUIRE_IDENT (true unless "false"), the same value the
config was rendered with.

Identity of an item (documented in the answer of the DV-Router project, MS3):

  direct client M17P SMS  ident 1 when the packet SRC matches the link callsign
                          (upper case, a trailing module letter and a -SSID
                          dropped, slashes kept: N0CALL matches N0CALL-7, not
                          N0CALL/P), else ident 0
  peer M17P SMS           ident 0 (peer circuit), whatever the SRC
  XLX peer slow data      ident 0; it becomes a `status` item, never a text item,
                          so no D-STAR text over: seen only in the counters
  require_ident = true    ident 0 is denied by the controller (reason 14,
                          "unidentified"); false: admitted at ident 0

Observation. The hub counters are read through the control channel action
`metrics xlxd-sut` (dvtest/control.py, scripts/lib/metrics.sh) and refresh every
10 s (the worker's `dv_pkt_items_total` may lag the controller's
`dv_room_data_total` by a second refresh), so a case takes a quiet baseline first
(no change for 12 s) and polls up to 25 s for the change. The text over is seen on
a DExtra client socket (dvtest/slowdata.py: 22 frames, sync at frame 0, four text
blocks at frames 1..8 and 9..16).

  a  test_direct_sms_becomes_a_dstar_text_over   M17P SMS (SRC = link callsign)
     from a direct M17 client reaches an idle-module DExtra listener as a text over
     "SRC: body"; counters: accepted, no unidentified. Both settings.
  b  test_direct_sms_src_variants                SRC that does not match: RECORD
     (delivered? counters?) against the documented rule; nothing asserted.
  c  test_peer_sms_needs_an_identity             SMS from the tester's fake M17 peer
     (SRC anything): true -> not delivered, room unidentified and text unidentified
     rise; false -> delivered, accepted rises.
  d  test_xlx_peer_slow_data_counters            a D-STAR talker with slow-data text
     (TX message blocks in every superframe) on the interlink peer reflector XLX998
     reaches the SUT over the XLX link: status item, counters only (true:
     unidentified, false: accepted), no text over; the listener on the SUT gets the
     voice and the slow data byte for byte.
  e  test_interlink_voice_unchanged              the voice cell of
     test_xlxd_interlink.py (both directions) is byte-identical in both settings.
  f  every case prints the deltas of all counters it reads, including
     dv_edge_data_total{result=unidentified}, which the DV-Router answer marks as
     unverified for worker-submitted items: recorded, never asserted.

Findings and records go to stdout and to out/<run>/data-ident.txt ("FINDING" =
observed differs from the documented rule, "RECORD" = nothing documented).
"""
import contextlib
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from m17_common import MOD, clear, done, new, ready  # noqa: E402
from dvtest import clients, metrics, slowdata  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import ambed, dextra, m17  # noqa: E402

import test_xlxd_interlink as interlink_cases  # noqa: E402  (its voice cell helpers)

pytestmark = pytest.mark.rig

REQUIRE_IDENT = os.environ.get("DVT_REQUIRE_IDENT", "true") != "false"
SETTING = "require_ident=%s" % ("true" if REQUIRE_IDENT else "false")
SMS = 0x05
LINK_CALLSIGN = "N0CALL"
FAMILIES = ("dv_room_data_total", "dv_pkt_items_total")
QUIET_S = 12.0                  # a baseline is quiet when nothing changed for one refresh and a bit
FAMILY_WAIT_S = 60.0            # the data families appear after the first publish of the controller and worker
POLL_S = 25.0                   # the change shows up within this after the event
OVER_WAIT_S = 15.0              # a text over starts after the floor was idle for idle_gap_ms
XLX_MODULE = "D"                # first of DVT_INTERLINK_MODULES
RESULTS = ("accepted", "unidentified")


# ---------------------------------------------------------------- findings

def note(target, case, tag, text):
    """Print and append "[tag] case: text" (RECORD, FINDING or OK) to the run's
    data-ident.txt."""
    line = "[%s] %s (%s): %s" % (tag, case, SETTING, text)
    print(line)
    try:
        with open(Path(target.out) / "data-ident.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- hub counters

def snapshot(samples):
    """The counters the cases read, by flat name: room.<result> (controller,
    dv_room_data_total), pkt.<kind>.<result> (worker, dv_pkt_items_total),
    edge.<result> (dv_edge_data_total), each also as .all (any result)."""
    snap = {"room.all": metrics.total(samples, "dv_room_data_total"),
            "pkt.all": metrics.total(samples, "dv_pkt_items_total"),
            "edge.all": metrics.total(samples, "dv_edge_data_total")}
    for result in RESULTS:
        snap["room." + result] = metrics.total(samples, "dv_room_data_total", result=result)
        snap["edge." + result] = metrics.total(samples, "dv_edge_data_total", result=result)
        for kind in ("text", "status"):
            snap["pkt.%s.%s" % (kind, result)] = metrics.total(
                samples, "dv_pkt_items_total", kind=kind, result=result)
    for kind in ("text", "status"):
        snap["pkt.%s.all" % kind] = metrics.total(samples, "dv_pkt_items_total", kind=kind)
    return snap


def delta(after, before):
    return {key: after[key] - before[key] for key in after}


def moved(delta_map):
    """The non-zero entries, for printing."""
    return {key: int(value) for key, value in sorted(delta_map.items()) if value}


class Hub:
    """The hub metrics of xlxd-sut through the control channel."""

    def __init__(self, control):
        self.control = control

    def text(self):
        return self.control.metrics("xlxd-sut")

    def snap(self):
        return snapshot(metrics.parse(self.text()))

    def settle(self, quiet=QUIET_S, limit=90.0, poll=2.0):
        """A snapshot after the counters did not change for `quiet` seconds (a
        refresh of an earlier case has landed)."""
        snap, changed, end = self.snap(), time.monotonic(), time.monotonic() + limit
        while time.monotonic() - changed < quiet and time.monotonic() < end:
            time.sleep(poll)
            current = self.snap()
            if current != snap:
                snap, changed = current, time.monotonic()
        return snap

    def wait_for(self, before, pred, timeout=POLL_S, poll=2.0):
        """(snapshot, True) once pred(snapshot, before) holds, else (last
        snapshot, False) after `timeout` seconds."""
        end = time.monotonic() + timeout
        while True:
            current = self.snap()
            if pred(current, before):
                return current, True
            if time.monotonic() >= end:
                return current, False
            time.sleep(poll)


def anything_counted(now, before):
    """Both the controller's and the worker's counter moved. They refresh on
    their own 10 s clocks, so the second one may show up one refresh after the first."""
    return now["room.all"] > before["room.all"] and now["pkt.all"] > before["pkt.all"]


def anything_counted_any(now, before):
    return now["room.all"] > before["room.all"] or now["pkt.all"] > before["pkt.all"]


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics of the DV-Router SUT with the data worker on; skips with the
    reason otherwise. Writes the data counter lines into data-ident.txt at the end."""
    if target.sut_profile != "dvrouter":
        pytest.skip("DV-Router only: DVT_SUT_PROFILE=dvrouter (profile %s)" % target.sut_profile)
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_LAUNCH=native with the data-ident.toml config extra")
    helper = Hub(rig_control)
    # the controller and the worker publish their counters 10 s after they start (and then
    # every 10 s), so the families may show up only after the rig is up for a while
    end = time.monotonic() + FAMILY_WAIT_S
    while True:
        try:
            text = helper.text()
        except RuntimeError as exc:
            pytest.skip("the hub metrics are not reachable through the control channel (%s)" % exc)
        if set(FAMILIES) & metrics.families(text) or time.monotonic() >= end:
            break
        time.sleep(3.0)
    if not set(FAMILIES) & metrics.families(text):
        pytest.skip("[data] is not enabled: no %s family in the hub metrics after %.0f s; run with "
                    "DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml" % (" or ".join(FAMILIES), FAMILY_WAIT_S))
    yield helper
    try:
        for line in helper.text().splitlines():
            if line.startswith(("dv_room_data_total", "dv_pkt_items_total", "dv_edge_data_total")) \
                    and line.rsplit(" ", 1)[-1] not in ("0", "0.0"):
                note(target, "hub", "RECORD", "end of run: " + line)
    except Exception:                                    # the SUT may be gone; not a case failure
        pass


# ---------------------------------------------------------------- clients

@contextlib.contextmanager
def dstar_listener(ip, module=MOD):
    """A DExtra client linked to the module, with an empty inbox."""
    listener = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=module)
    try:
        assert listener.link(ip), "DExtra link to %s module %s failed" % (ip, module)
        time.sleep(1.2)                         # the module's client list refreshes once per second
        listener.client.drain()
        listener.client.inbox.clear()
        listener.client.inbox_times.clear()
        yield listener
    finally:
        clients.close(listener)


@contextlib.contextmanager
def m17_client(ip, callsign=LINK_CALLSIGN, module=MOD):
    """A linked M17 client (CONN, not LSTN) with an empty inbox."""
    c = new(callsign, module)
    try:
        ready(ip, c)
        yield c
    finally:
        done(c)


def nonce_body(tag="DVT"):
    return "%s %04d" % (tag, int(time.time() * 10) % 10000)


def sms_packet(body, src):
    """M17P packet-mode SMS: LSF with DST @ALL and the legacy TYPE, specifier
    0x05, UTF-8 body, NUL, payload CRC (dvtest/proto/m17.py)."""
    return m17.m17p_packet(SMS, body.encode("ascii") + b"\0", src=src)


def wait_for_overs(listener, seconds, want=None):
    """Drain the listener for up to `seconds` (stop early when want(overs));
    the overs received so far (dvtest.slowdata.overs)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        listener.client.drain()
        if want is not None and want(slowdata.overs(listener.client.inbox)):
            time.sleep(0.3)
            listener.client.drain()
            break
        time.sleep(0.02)
    return slowdata.overs(listener.client.inbox)


def texts_of(overs):
    """[(stream id, decoded dict)] of the whole text overs among `overs`."""
    out = []
    for over in overs:
        got = slowdata.over_text(over)
        if got:
            out.append((over["stream_id"], got))
    return out


def has_text_with(body):
    return lambda overs: any(got["text"].rstrip().endswith(body) for _sid, got in texts_of(overs))


def describe(over):
    header = over["header"] or {}
    return {"stream_id": over["stream_id"], "frames": over["frames"],
            "my": bytes(header.get("my", b"")).decode("ascii", "replace"),
            "ur": bytes(header.get("ur", b"")).decode("ascii", "replace"),
            "suffix": bytes(header.get("suffix", b"")).decode("ascii", "replace"),
            "seqs": [v["counter"] for v in over["voice"]][:3] + ["..."]}


# ---------------------------------------------------------------- a: direct SMS

def test_direct_sms_becomes_a_dstar_text_over(rig, target, hub):
    """(a) SRC = the link callsign: ident 1, admitted under both settings, shown to
    an idle-module D-STAR client as a 22 frame text over "N0CALL: <body>"."""
    body = nonce_body()
    want = "%s: %s" % (LINK_CALLSIGN, body)
    before = hub.settle()
    with dstar_listener(rig.xlxd_sut) as listener, m17_client(rig.xlxd_sut) as talker:
        talker.client.send(talker.dest, sms_packet(body, LINK_CALLSIGN))
        overs = wait_for_overs(listener, OVER_WAIT_S, has_text_with(body))
    after, seen = hub.wait_for(before, anything_counted)
    change = delta(after, before)
    note(target, "a", "RECORD", "counter deltas %s; overs %s" % (moved(change), [describe(o) for o in overs]))
    texts = texts_of(overs)
    assert texts, "no whole text over reached the D-STAR listener in %.0f s: %s" % (
        OVER_WAIT_S, [describe(o) for o in overs])
    sid, got = texts[0]
    assert got["text"].rstrip() == want, "text over says %r, want %r" % (got["text"], want)
    assert got["repeat_ok"] and got["filler_ok"], got
    over = [o for o in overs if o["stream_id"] == sid][0]
    assert over["header"]["my"].rstrip() == b"XLX999" and over["header"]["suffix"] == b"M17 ", describe(over)
    assert seen, "no counter moved in %.0f s" % POLL_S
    assert change["room.accepted"] >= 1, "controller did not count the SMS as accepted: %s" % moved(change)
    assert change["room.unidentified"] == 0 and change["pkt.text.unidentified"] == 0, moved(change)


# ---------------------------------------------------------------- b: SRC variants

SRC_VARIANTS = [
    pytest.param("N0TEST", False, id="other-callsign"),
    pytest.param("N0CALL/P", False, id="slash-kept"),
    pytest.param("N0CALL-7", True, id="ssid-stripped"),
]


@pytest.mark.parametrize("src,matches", SRC_VARIANTS)
def test_direct_sms_src_variants(rig, target, hub, src, matches):
    """(b) RECORD: a direct client whose M17P SRC differs from its link callsign
    N0CALL. Documented: a match (SSID stripped) is ident 1, anything else ident 0,
    denied under require_ident=true and admitted at ident 0 under false. Prints
    delivered / counters / any datagram the edge answers with; nothing asserted."""
    case = "b[%s]" % src
    body = nonce_body("SRC")
    documented_delivered = matches or not REQUIRE_IDENT
    before = hub.settle()
    with dstar_listener(rig.xlxd_sut) as listener, m17_client(rig.xlxd_sut) as talker:
        clear(talker)
        talker.client.send(talker.dest, sms_packet(body, src))
        overs = wait_for_overs(listener, OVER_WAIT_S, has_text_with(body))
        talker.client.drain()
        answers = [d[:4] for d in talker.client.inbox if not d.startswith(b"PING")]
    after, seen = hub.wait_for(before, anything_counted)
    change = delta(after, before)
    delivered = [got["text"].rstrip() for _sid, got in texts_of(overs) if got["text"].rstrip().endswith(body)]
    observed = "delivered=%s text=%s counters=%s edge_answers=%s" % (
        bool(delivered), delivered[:1], moved(change), answers)
    if bool(delivered) != documented_delivered:
        note(target, case, "FINDING", "documented delivered=%s, observed: %s" % (documented_delivered, observed))
    else:
        note(target, case, "RECORD", "as documented (delivered=%s): %s" % (documented_delivered, observed))
    if not seen:
        note(target, case, "FINDING", "no counter moved in %.0f s (edge dropped the SMS before the worker?)" % POLL_S)


# ---------------------------------------------------------------- c: SMS from a peer

def test_peer_sms_needs_an_identity(rig, target, hub, m17_peers):
    """(c) An M17P SMS relayed by a peer reflector has no identity: with
    require_ident=true the controller denies it (not delivered, unidentified
    counters rise), with false it is admitted and shown as a text over."""
    if "sut" not in m17_peers:
        pytest.skip("no M17 peering on the sut")
    peers = m17_peers["sut"]
    src = "N0TEST"
    body = nonce_body("PEER")
    before = hub.settle()
    peers.dvt.clear()
    with dstar_listener(rig.xlxd_sut) as listener:
        peers.dvt.send(m17.with_trailer(sms_packet(body, src), MOD))
        overs = wait_for_overs(listener, OVER_WAIT_S, has_text_with(body))
    after, seen = hub.wait_for(before, anything_counted)
    change = delta(after, before)
    delivered = [got["text"].rstrip() for _sid, got in texts_of(overs) if got["text"].rstrip().endswith(body)]
    note(target, "c", "RECORD", "delivered=%s %s counters=%s dv_edge_data_total{unidentified} delta=%d (unverified "
         "in the docs)" % (bool(delivered), delivered[:1], moved(change), change["edge.unidentified"]))
    assert seen, "no counter moved in %.0f s" % POLL_S
    if REQUIRE_IDENT:
        assert not delivered, "peer SMS was delivered under require_ident=true: %s" % delivered
        assert change["room.unidentified"] >= 1, "controller counted no unidentified item: %s" % moved(change)
        assert change["pkt.text.unidentified"] >= 1, "worker counted no unidentified text: %s" % moved(change)
        assert change["room.accepted"] == 0 and change["pkt.text.accepted"] == 0, moved(change)
    else:
        assert delivered, "peer SMS was not delivered under require_ident=false (counters %s, overs %s)" % (
            moved(change), [describe(o) for o in overs])
        assert change["room.accepted"] >= 1 and change["pkt.text.accepted"] >= 1, moved(change)
        assert change["room.unidentified"] == 0 and change["pkt.text.unidentified"] == 0, moved(change)
        text = delivered[0]
        if text != "%s: %s" % (src, body):
            note(target, "c", "RECORD", "display text %r (SRC %s, body %s)" % (text, src, body))


# ---------------------------------------------------------------- d, e: interlink peer

@pytest.fixture(scope="module")
def interlink(rig, hub):
    """The XLX998 peer of xlxd-sut (profile interlink); the sut has the peer row
    (rig/config/dv-router/xlx-peer.toml) when a stream from the peer reaches a
    listener on the sut: probed for up to 60 s."""
    if os.environ.get("DVT_INTERLINK") != "on" or not rig.is_given("xlxd_peer_sut"):
        pytest.skip("no interlink peer: run.sh --profile interlink (DVT_INTERLINK=on)")
    frames = signals.prng_frames(1, 30, seed="data-ident-probe")
    deadline = time.monotonic() + 60
    while True:
        got = peer_talk(rig, frames, None)
        if len(got["received"]) >= len(frames) * 0.8:
            break
        if time.monotonic() > deadline:
            pytest.fail("xlxd-peer-sut -> xlxd-sut carries no stream on module %s (%d of %d frames): does the config "
                        "have the XLX peer row, DVT_SUT_CONFIG_EXTRA=...:rig/config/dv-router/xlx-peer.toml?"
                        % (XLX_MODULE, len(got["received"]), len(frames)))
        time.sleep(5)
    return XLX_MODULE


def peer_talk(rig, frames, text, tail=1.5):
    """A D-STAR talker on the interlink peer (xlxd-peer-sut, module D) sends
    `frames` (9 byte AMBE each) with `text` as TX message slow data in every
    superframe (frame 0 sync, frames 1..20 the four blocks of dextra.slow_text;
    text None: zero slow data), a D-STAR listener on xlxd-sut module D collects.
    Returns sent (the voice packets), received (the listener's [(ambe, slow)]),
    overs (dvtest.slowdata.overs of the listener)."""
    talker = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=XLX_MODULE)
    listener = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=XLX_MODULE)
    try:
        assert listener.link(rig.xlxd_sut), "listener link to the sut failed"
        assert talker.link(rig.xlxd_peer_sut), "talker link to the interlink peer failed"
        time.sleep(1.2)
        for c in (talker, listener):
            c.client.drain()
            c.client.inbox.clear()
        groups = dextra.slow_text(text) if text else None
        sid = clients.fresh_stream_id("dstar")
        packets = [dextra.voice(sid, i, frame, False,
                                dextra.SYNC_DATA if i % 21 == 0 else (groups[i % 21 - 1] if groups else dextra.SLOW_DATA))
                   for i, frame in enumerate(frames)]
        last = dextra.voice(sid, len(frames), bytes(9), True)
        talker.client.send(talker.dest, talker.header(sid, "XLX998"))
        talker.client.pump(0.3)
        talker.client.paced(talker.dest, packets + [last], 0.020, listener.client)
        end = time.monotonic() + tail
        while time.monotonic() < end:
            listener.client.drain()
            talker.client.drain()
            time.sleep(0.01)
        overs = slowdata.overs(listener.client.inbox)
        received = [(v["ambe"], v["slow"]) for o in overs for v in o["voice"]]
        return {"sid": sid, "sent": [(dextra.parse_voice_raw(p)["ambe"], dextra.parse_voice_raw(p)["slow"])
                                     for p in packets], "received": received, "overs": overs}
    finally:
        clients.close(talker, listener)


@pytest.mark.interlink
def test_xlx_peer_slow_data_counters(rig, target, hub, interlink):
    """(d) Slow data of a stream relayed by an XLX peer has no identity and is a
    `status` item. How it is produced: a D-STAR talker on the peer reflector
    (xlxd-peer-sut, XLX998) sends 63 voice frames (3 superframes) whose slow data
    carries the 20 character TX message in every superframe; xlxd relays it over
    the interlink to the sut's XLX edge (class 1, ident none). Asserted (documented):
    the listener on the sut gets the voice AND the slow data unchanged (native
    D-STAR path, data.md 15.5) and no text over; counters: require_ident=true ->
    pkt status unidentified and controller unidentified rise, nothing accepted;
    false -> status accepted rises; never a `text` item. A counter that does not
    move is a FINDING (the slow data may not reach the worker)."""
    frames = signals.prng_frames(1, 63, seed="data-ident-slow")
    text = "DVT SLOW DATA %04d" % (int(time.time()) % 10000)
    before = hub.settle()
    got = peer_talk(rig, frames, text)
    after, seen = hub.wait_for(
        before, lambda now, was: now["pkt.status.all"] > was["pkt.status.all"] and now["room.all"] > was["room.all"])
    if not seen:                                         # one of the two may be all there is: judged below
        seen = anything_counted_any(after, before)
    change = delta(after, before)
    note(target, "d", "RECORD", "sent %d frames, listener got %d; counters %s; dv_edge_data_total{unidentified} "
         "delta=%d" % (len(frames), len(got["received"]), moved(change), change["edge.unidentified"]))
    assert [a for a, _s in got["received"]] == [a for a, _s in got["sent"]], "voice differs across the interlink"
    slow_differs = [i for i, (r, s) in enumerate(zip(got["received"], got["sent"])) if r[1] != s[1]]
    if slow_differs:
        note(target, "d", "FINDING", "slow data differs from the talker's at frames %s (documented: byte-identical "
             "native D-STAR slow data, data.md 15.5); first: sent %s got %s" % (
                 slow_differs[:6], got["sent"][slow_differs[0]][1].hex(), got["received"][slow_differs[0]][1].hex()))
    assert not slow_differs, "slow data changed at frames %s" % slow_differs[:6]
    assert len(got["overs"]) == 1, "listener saw %d streams, want the talker's only (a status makes no text over): %s" % (
        len(got["overs"]), [describe(o) for o in got["overs"]])
    if not seen:
        note(target, "d", "FINDING", "no counter moved in %.0f s: the slow data of an XLX peer stream did not reach the "
             "worker as a status item (documented: status unidentified/accepted)" % POLL_S)
        pytest.fail("no data counter moved after the peer's slow data")
    assert change["pkt.text.all"] == 0, "a `text` item was counted for XLX slow data: %s" % moved(change)
    if REQUIRE_IDENT:
        assert change["pkt.status.unidentified"] >= 1 and change["pkt.status.accepted"] == 0, moved(change)
        assert change["room.unidentified"] >= 1 and change["room.accepted"] == 0, moved(change)
    else:
        assert change["pkt.status.accepted"] >= 1 and change["pkt.status.unidentified"] == 0, moved(change)
        assert change["room.accepted"] >= 1 and change["room.unidentified"] == 0, moved(change)


@pytest.fixture(scope="module")
def voice_frames(rig):
    """The tone bursts of test_xlxd_interlink.py, encoded once by transcoder-ref."""
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:interlink_cases.FRAMES]
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [ambed.AMBEPLUS], pcm, port=rig.ambed_port)[ambed.AMBEPLUS]


@pytest.mark.interlink
@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_interlink_voice_unchanged(rig, target, hub, interlink, voice_frames, direction):
    """(e) The voice cell of test_xlxd_interlink.py (run_direction) on the sut
    pair: D-STAR frames cross the interlink byte for byte, once, with no echo, in
    both settings. forward: talker on the sut, listener on the peer; reverse:
    talker on the peer (XLX998), listener on the sut."""
    sut, peer = rig.xlxd_sut, rig.xlxd_peer_sut
    before = hub.settle()
    if direction == "forward":
        got = interlink_cases.run_direction(sut, peer, interlink, voice_frames)
    else:
        got = interlink_cases.run_direction(peer, sut, interlink, voice_frames, reflector=interlink_cases.PEER_CALLSIGN)
    after = hub.snap()
    note(target, "e[%s]" % direction, "RECORD", "far frames %d/%d, streams far %d near %d, talker back %d; counters "
         "during the cell %s" % (len(got["far_dstar"]), len(voice_frames), got["far_streams"], got["near_streams"],
                                 got["talker_back"], moved(delta(after, before))))
    assert got["far_dstar"] == voice_frames, "%s: %d frames, content differs" % (direction, len(got["far_dstar"]))
    assert got["far_streams"] == 1, "%s: %d streams across the interlink" % (direction, got["far_streams"])
    assert got["talker_back"] == 0, "%s: %d voice packets back to the talker" % (direction, got["talker_back"])
    assert got["near_streams"] == 1 and got["near_dstar"] == voice_frames, "%s: own-reflector listener differs" % direction
