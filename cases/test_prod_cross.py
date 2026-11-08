# SPDX-License-Identifier: MIT
"""Production cross-mode level check through module E of a LIVE reflector (docs/prod-checks.md, "Production cross-mode level check").

One M17 talker (Codec2 3200) with a D-STAR listener (DExtra), then the reverse, each with the 700 Hz tone (amp 8000, 3 s) and one
speech clip at an active level of -20 dBFS (3 s), measured as cases/test_transcoder_levels.py does: the talker's PCM is encoded by the
LOCAL transcoder (the rig's transcoder-ref, DVT_AMBED_REF) into the talker's codec, the production reflector transcodes, and the
listener's frames are decoded by the same local transcoder and scored. The rig's xlxd-sut is not used.

Skipped unless DVT_PROD_CROSS=1 with DVT_PROD_HOST, DVT_PROD_CALL_DSTAR (8 characters, a 'CALL T' form) and DVT_PROD_CALL_M17 set; none
has a default. DVT_PROD_CROSS_DRY=1 prints the plan and sends nothing. Module E only (a hard refusal otherwise). Module E is an ECHO
module: the listener hears the live cross-mode copy first and the echo copy about a second after the end; only the first stream is
judged, the others are counted (echo_overs). Rows go to <out>/prod-cross.jsonl; DVT_PROD_CROSS_REF=<transcoder-levels.jsonl of the same
tag> adds the PASS / FAIL against the rig unity rows. Nothing is run by collecting this file.
"""
import json
import socket
import threading
import time
from pathlib import Path

import pytest

import prod_cross_support as ps
from dvtest import clients, echo
from dvtest.audio import signals
from dvtest.proto import ambed, dextra, m17
from echo_support import Drainer

pytestmark = pytest.mark.rig

PUSH_GAP = 0.005                # seconds between the frames pushed into the local transcoder (faster than real time)
KEEPALIVE_S = 8.0


@pytest.fixture(scope="module")
def cfg():
    try:
        return ps.config_from_env()
    except ps.ConfigMissing as exc:
        pytest.skip(str(exc))
    except ps.ConfigRefused as exc:
        pytest.fail("refused: %s" % exc)


def encode(target, codec, pcm):
    return ambed.transcode(target.ambed_ref, ambed.PCM, [codec], signals.frames(pcm), port=target.ambed_port, gap=PUSH_GAP)[codec]


def decode(target, codec, frames):
    return b"".join(ambed.transcode(target.ambed_ref, codec, [ambed.PCM], frames, port=target.ambed_port, gap=PUSH_GAP)[ambed.PCM])


def make_client(kind, cfg):
    ps.require_module(cfg.module)
    if kind == "dstar":
        return dextra.DstarClient("0.0.0.0", callsign=cfg.call_dstar, module=ps.MODULE)
    return m17.M17Client("0.0.0.0", callsign=cfg.call_m17, module=ps.MODULE)


class KeepAlive(threading.Thread):
    """The protocol keepalive of the clients every few seconds (D-STAR); the main thread sleeps between overs."""

    def __init__(self, *made):
        super().__init__(daemon=True)
        self.made, self.halt = made, threading.Event()

    def run(self):
        while not self.halt.wait(KEEPALIVE_S):
            for c in self.made:
                try:
                    if hasattr(c, "keepalive"):
                        c.keepalive()
                except OSError:
                    pass

    def stop(self):
        self.halt.set()
        self.join(2.0)


def safe_close(*made):
    """Unlink every client that linked and close its socket; one failure does not skip the others."""
    for c in made:
        try:
            if c.dest:
                c.unlink()
        except Exception:                        # noqa: BLE001 - the close below still runs
            pass
        try:
            c.close()
        except Exception:                        # noqa: BLE001
            pass


def send_over(talker, kind, frames, reflector):
    sid = clients.fresh_stream_id(kind)
    if kind == "dstar":
        talker.talk(frames, sid, reflector=reflector, tail=0.0)
    else:
        talker.talk(frames, sid, tail=0.0)
    return sid


def sleep_until(moment, deadline):
    """Sleep until `moment` (monotonic) but never past the global deadline; True when the moment was reached."""
    while True:
        now = time.monotonic()
        if now >= moment:
            return True
        if now >= deadline:
            return False
        time.sleep(min(0.2, moment - now))


def emit(out_dir, row):
    print("[ROW] " + json.dumps(row, sort_keys=True))
    with open(Path(out_dir) / "prod-cross.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def test_prod_cross_levels(cfg, target, request):
    try:
        sigs = ps.source_signals(ps.ROOT)
    except ps.MissingClip as exc:
        pytest.skip(str(exc))
    plan = ps.make_plan(cfg, sigs)
    print(ps.format_plan(plan, dry=cfg.dry))
    if cfg.dry:
        return
    if not target.is_given("ambed_ref"):
        pytest.skip("no local transcoder: set DVT_AMBED_REF to the rig's transcoder-ref (the production tag) and DVT_AMBED_PORT")
    out_root = request.getfixturevalue("out_root")      # the run directory is made only for a real run
    ref_rows = None
    if cfg.ref:
        try:
            ref_rows = ps.pick_ref(ps.read_rows(cfg.ref), cfg.tag)
        except (OSError, ValueError, KeyError, ps.ConfigError) as exc:
            pytest.fail("DVT_PROD_CROSS_REF: %s" % exc)

    deadline = time.monotonic() + ps.GLOBAL_TIMEOUT_S
    # the local encoding happens before the first packet: nothing is linked while the transcoder works
    encoded = {(kind, name): encode(target, ps.CODEC[kind], pcm) for kind in ps.KINDS for name, pcm in sigs}
    for frames in encoded.values():
        ps.check_over_frames(len(frames))
    address = socket.gethostbyname(cfg.host)

    captures, problems = [], []
    last_end = None
    for talker_kind, listener_kind in ps.PAIRS:
        pair = ps.pair_label(talker_kind, listener_kind)
        talker = listener = keep = talk_drain = listen_drain = None
        try:
            talker, listener = make_client(talker_kind, cfg), make_client(listener_kind, cfg)
            ok = True
            for c, kind in ((talker, talker_kind), (listener, listener_kind)):
                if not c.link(address, port=ps.PORTS[kind]):
                    problems.append("%s: the %s client did not link" % (pair, kind))
                    ok = False
                    break
            if not ok:
                continue
            for c in (talker, listener):
                c.client.drain()
                c.client.inbox.clear()
                c.client.inbox_times.clear()
            listen_drain = Drainer(listener.client)
            listen_drain.start()
            talk_drain = Drainer(talker.client)
            talk_drain.start()
            keep = KeepAlive(talker, listener)
            keep.start()
            for name, pcm in sigs:
                frames = encoded[(talker_kind, name)]
                start_at = time.monotonic() + ps.gap_wait(last_end, time.monotonic())
                if time.monotonic() >= deadline or not sleep_until(start_at, deadline):
                    problems.append("%s %s: skipped, global timeout of %.0f s" % (pair, name, ps.GLOBAL_TIMEOUT_S))
                    break
                talk_drain.stop()                       # UdpClient.talk drains its own socket while it sends
                t0 = time.monotonic()
                send_over(talker, talker_kind, frames, cfg.reflector)
                t_end = last_end = time.monotonic()
                talk_drain = Drainer(talker.client)
                talk_drain.start()
                sleep_until(t_end + ps.WATCH_S, deadline)
                packets, times = list(listener.client.inbox), list(listener.client.inbox_times)
                captures.append({"pair": pair, "name": name, "pcm": pcm, "sent": len(frames), "listener": listener_kind,
                                 "events": echo.events(listener_kind, packets, times), "t0": t0, "t_end": t_end})
        finally:
            for t in (keep, talk_drain, listen_drain):
                if t is not None:
                    t.stop()
            safe_close(*[c for c in (talker, listener) if c is not None])
        if time.monotonic() >= deadline:
            break

    # all production traffic is over: decode and score
    rows = []
    for cap in captures:
        split = ps.live_and_echo(cap["events"], cap["t0"], cap["t_end"])
        out_pcm = decode(target, ps.CODEC[cap["listener"]], split["frames"]) if split["frames"] else b""
        row = ps.measure_row(cfg.tag, cap["pair"], cap["name"], cap["pcm"], cap["sent"], len(split["frames"]), out_pcm, split)
        emit(out_root, row)
        rows.append(row)
    results = ps.compare_all(rows, ref_rows)
    print(ps.report(results, ref_rows is not None))
    notes = ps.findings(results)
    for line in notes:
        print(line)
    expected = len(ps.PAIRS) * len(sigs)
    if len(rows) < expected:
        problems.append("%d of %d measurements taken" % (len(rows), expected))
    assert not problems and not notes, "; ".join(problems + notes)
