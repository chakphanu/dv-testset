# SPDX-License-Identifier: MIT
"""xlxd restarted with clients connected (D5).

Each reflector (xlxd-ref, then xlxd-sut) goes through the same sequence
on module D, so the outcome is differential:

  1. D-STAR talker, D-STAR listener and DMR listener linked; a stream works
  2. the talker opens a stream (header + voice, no last frame)
  3. control channel: restart xlxd-<setup>; wait until it ACKs a DExtra link
  4. old links, no relink: the talker sends a new stream
  5. everyone relinks; the talker continues the stream of step 2 (voice
     without a header)
  6. 2 s quiet window
  7. a fresh stream

A restarted xlxd keeps no client list: clients that do not link again get
nothing (step 4, same on the reference; real clients relink after their
keepalive timeout). After the relink the module must work again (step 7:
D-STAR exact, DMR transcoded, sut == ref) and nothing from before the
restart may come out (steps 5 and 6: no stale stream).
Results: $DVT_OUT/restart.json.
"""
import json
import time
from pathlib import Path

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.proto import ambed, dextra

pytestmark = pytest.mark.rig

MODULE = "D"
FRAMES = 60
UP_TIMEOUT = 45


def voice_count(client):
    client.client.drain()
    return sum(1 for p in client.client.inbox if dextra.parse_voice(p))


def stream_count(client):
    """Distinct D-STAR streams received: xlxd repeats a header, so count
    stream ids of headers and voice packets."""
    ids = set()
    for p in client.client.inbox:
        parsed = dextra.parse_header(p) or dextra.parse_voice(p)
        if parsed:
            ids.add(parsed["stream_id"] if isinstance(parsed, dict) else parsed[0])
    return len(ids)


def clear(*cs):
    for c in cs:
        c.client.drain()
        c.client.inbox.clear()


def wait_up(ip, seconds=UP_TIMEOUT):
    """Seconds until xlxd at ip ACKs a fresh DExtra link."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        probe = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module="A")
        try:
            if probe.link(ip, timeout=1.0):
                probe.unlink()
                return time.monotonic() - start
        except OSError:
            pass
        finally:
            probe.close()
        time.sleep(0.5)
    return None


def link_all(ip, *cs):
    for c in cs:
        assert c.link(ip), "link to %s failed" % ip
    clear(*cs)


def sequence(ip, setup, rig_control, frames):
    talker = clients.make("dstar", MODULE, 0)
    listener = clients.make("dstar", MODULE, 1)
    dmr = clients.make("dmr", MODULE, 1)
    out = {}
    try:
        link_all(ip, listener, dmr, talker)
        clients.talk(talker, "dstar", frames)
        out["before"] = len(listener.collect(settle=0.5))
        clear(listener, dmr, talker)

        # 2: open stream across the restart
        sid = clients.fresh_stream_id("dstar")
        talker.client.send(talker.dest, dextra.header(sid, "N0CALL", MODULE))
        talker.client.paced(talker.dest, [dextra.voice(sid, i, frames[i], False) for i in range(25)], 0.020)

        # 3
        rig_control("restart", "xlxd-%s" % setup)
        out["up_seconds"] = wait_up(ip)
        assert out["up_seconds"] is not None, "xlxd-%s does not ACK a link %d s after restart" % (setup, UP_TIMEOUT)

        # 4: old links
        clear(listener, dmr, talker)
        clients.talk(talker, "dstar", frames)
        out["old_dstar"] = len(listener.collect(settle=0.5))
        out["old_dmr"] = len(dmr.collect(settle=0.5))

        # 5: relink, continue the stream of step 2 without a header
        link_all(ip, listener, dmr, talker)
        talker.client.paced(talker.dest, [dextra.voice(sid, i, frames[i], i == FRAMES - 1)
                                          for i in range(25, FRAMES)], 0.020)
        talker.client.pump(1.0)
        out["stale_dstar"] = voice_count(listener)
        out["stale_dmr"] = len(dmr.collect(settle=0.5))

        # 6
        clear(listener, dmr)
        time.sleep(2.0)
        out["quiet_dstar"] = voice_count(listener)
        out["quiet_dmr"] = len(dmr.collect(settle=0.1))

        # 7: fresh stream; the transcoder link may need a few seconds
        deadline = time.monotonic() + 30
        attempts = 0
        while True:
            attempts += 1
            clear(listener, dmr, talker)
            clients.talk(talker, "dstar", frames)
            got_dstar = listener.collect(settle=0.5)
            got_dmr = dmr.collect(settle=0.5)
            voiced = [f for f in got_dmr if any(f)]
            if len(voiced) >= FRAMES * 0.8 or time.monotonic() > deadline:
                break
            time.sleep(3)
        out.update(fresh_attempts=attempts, fresh_streams=stream_count(listener),
                   fresh_dstar_exact=got_dstar == frames, fresh_dstar=len(got_dstar),
                   fresh_dmr=[f.hex() for f in got_dmr], fresh_dmr_voiced=len(voiced),
                   talker_back=voice_count(talker))
        return out
    finally:
        clients.close(talker, listener, dmr)


@pytest.fixture(scope="module")
def frames(rig):
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [ambed.AMBEPLUS], pcm,
                           port=rig.ambed_port)[ambed.AMBEPLUS]


@pytest.fixture(scope="module")
def results(rig, rig_control, frames):
    out = {"ref": sequence(rig.xlxd_ref, "ref", rig_control, frames),
           "sut": sequence(rig.xlxd_sut, "sut", rig_control, frames)}
    (Path(rig.out) / "restart.json").write_text(json.dumps(out, indent=1))
    return out


SETUPS = ["ref", "sut"]


@pytest.mark.parametrize("setup", SETUPS)
def test_restart_stream_before(results, setup):
    assert results[setup]["before"] == FRAMES, "%s before the restart: %d frames" % (setup, results[setup]["before"])


def test_restart_old_links_same_as_reference(results):
    """Clients that do not relink: documented behaviour, sut == ref."""
    sut, ref = results["sut"], results["ref"]
    keys = ("old_dstar", "old_dmr")
    assert {k: sut[k] for k in keys} == {k: ref[k] for k in keys}, (
        "without relink: sut %s, ref %s" % ({k: sut[k] for k in keys}, {k: ref[k] for k in keys}))


@pytest.mark.parametrize("setup", SETUPS)
def test_restart_no_stale_stream(results, setup):
    r = results[setup]
    stale = {k: r[k] for k in ("stale_dstar", "stale_dmr", "quiet_dstar", "quiet_dmr")}
    assert not any(stale.values()), "%s: frames from before the restart: %s" % (setup, stale)


@pytest.mark.parametrize("setup", SETUPS)
def test_restart_module_works_after_relink(results, setup):
    r = results[setup]
    assert r["fresh_dstar_exact"], "%s: D-STAR after relink %d frames, content differs" % (setup, r["fresh_dstar"])
    assert r["fresh_streams"] == 1, "%s: %d streams for one talk" % (setup, r["fresh_streams"])
    assert r["talker_back"] == 0, "%s: %d voice packets back to the talker" % (setup, r["talker_back"])
    assert r["fresh_dmr_voiced"] >= FRAMES * 0.8, "%s: DMR after relink voiced %d (attempts %d)" % (
        setup, r["fresh_dmr_voiced"], r["fresh_attempts"])


def test_restart_dmr_sut_equals_ref(results):
    sut, ref = results["sut"]["fresh_dmr"], results["ref"]["fresh_dmr"]
    assert sut == ref, "DMR after restart: sut %d frames, ref %d, content differs" % (len(sut), len(ref))
