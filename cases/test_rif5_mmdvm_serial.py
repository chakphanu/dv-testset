# SPDX-License-Identifier: MIT
"""DV-Router RIF5: the `mmdvm-serial` radio adapter against a FAKE MMDVM modem played by the tester on a UDP byte port (DV-Router c017e491:
RIF5 S2 fd58a4e the adapter and the D-STAR framer, S3 43483ca DMR simplex, S4 c017e491 YSF). Case table, config shape, run command, time and
GAPs: docs/rif5-cases.md. Pure judgements: dvtest/rif5.py, the modem and the envelope: dvtest/mmdvm_serial.py, rig helpers: cases/rif5_support.py.

  RIF5-G1   (c) nothing that keys the modem is written during the startup guard (restart of dv-edge-radio through the admin API, an over in the
            guard, voice / EOT / CW ID / DMR control frames none, also not later)
  RIF5-O1   the open dialogue: GET_VERSION, SET_FREQ, SET_CONFIG (v2, 40 B), GET_STATUS, the bytes at the documented offsets
  RIF5-O2   GET_STATUS every 250 ms
  RIF5-O3   the port row in the public state and the admin status (state up, modes, firmware, modem)
  RIF5-TX   (a) room to modem, D-STAR / DMR / YSF: a protocol client keys the room, the modem gets the MMDVM frames of the mode
  RIF5-RX   (a) modem to room, D-STAR / DMR / YSF: the modem sends an over, a protocol client of the room hears it; nothing is sent back to RF
  RIF5-RXD  a damaged DMR header copy inside an open over refreshes the 300 ms native-over timeout (S4: "a damaged DMR frame refreshes the over timeout")
  RIF5-RXR  D-STAR RF headers the adapter refuses (RPT2 not station + G, RPT1 not station + module, bad CRC): not in the room, counters
  RIF5-LONG a 3 s over in each mode, record only (the 250 ms status poll limits what the host keeps in flight)
  RIF5-D3   a modem silent for 1.0 s (below the 2 s of the document) keeps the port Up
  RIF5-D2   (b) a short status does not refresh inactivity: Down after 2 s although bytes flow
  RIF5-D1   (b) a silent modem: the port goes Down after 2000 ms without a valid status; polls, backoff, six version tries, recovery
  RIF5-M1   the counters of the run: overs, modem results, port results

Gate: DVT_RIF5=on (rig/compose.yml passes it to the tester); every case skips with the reason otherwise. Needs DVT_SUT_PROFILE=dvrouter, native launch
and rig/config/dv-router/rif5-mmdvm-udp.toml in DVT_SUT_CONFIG_EXTRA. With --profile web (web.toml:admin.toml) the cases read the port state and G1 can
restart the edge; the rig control channel gives the metrics. A difference the documents do not settle is a FINDING line in out/<run>/rif5.txt, never an
assertion. Written offline against DV-Router c017e491, first rig run on dv-router:wip-7b0fca8 (docs/rif5-cases.md, "First rig run"); the unit tests cover the
encoders, the modem and the judges.
"""
import os
import time
import tomllib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from dvtest import echo, rif5, sut_config
from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_fw
from dvtest import mmdvm_serial as ms
from dvtest.control import RigControl
from rif5_support import (Member, admin_row, last_dialogue, note, public_row, quiesce, restart_radio_edge, settle_idle, wait_for, wait_unfenced,
                          write_timeline)

pytestmark = pytest.mark.rig

ROOT = Path(__file__).resolve().parents[1]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "rif5-mmdvm-udp.toml"
KINDS = ("dstar", "dmr", "ysf")
# What the flow control of radio.md 7.1 carries without a drop. D-STAR: 22 frames (0.44 s) cover frame 0 of two superframes; the first rig run (50
# frames, 1.0 s) lost words from word 23 on: the first status that refills the window comes up to 250 ms after PTT, the header costs 4 of the 10 frames
# in flight, and the host queue (16, oldest dropped) overflows at the 23rd word of a real-time over. One DMR superframe, three YSF frames.
TX_FRAMES = {"dstar": 22, "dmr": 18, "ysf": 15}
RX_SECONDS = 3.0
RXD_COPIES = 10                                      # damaged header copies of RIF5-RXD
LONG_FRAMES = 150                                    # 3 s
OPEN_WAIT_S = 150.0                                  # the edge retries a failed open with a backoff of up to 60 s
TALLY = Counter()                                    # what the cases did, for RIF5-M1


# ---------------------------------------------------------------- the rig and the modem

def read_config(target):
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_port(target):
    """The port row of the snippet with the rig's addresses, for a rendered config the tester cannot see."""
    net = ".".join(target.tester.split(".")[:3])
    text = sut_config.render("", net, own=int(target.xlxd_sut.split(".")[-1]), tester=int(target.tester.split(".")[-1]),
                             extras=[SNIPPET.read_text(encoding="utf-8")])
    return rif5.port_from_config(tomllib.loads(text))


@pytest.fixture(scope="module")
def ctx(rig, target):
    if not rif5.gate_on(os.environ):
        pytest.skip("DVT_RIF5 is not on: the RIF5 cases need DV-Router c017e491 or later and rig/config/dv-router/rif5-mmdvm-udp.toml (docs/rif5-cases.md)")
    if target.sut_profile != "dvrouter":
        pytest.skip("the mmdvm-serial adapter is DV-Router's: DVT_SUT_PROFILE=dvrouter")
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("the radio edge is configured in the TOML: DVT_SUT_LAUNCH=native with rig/config/dv-router/rif5-mmdvm-udp.toml")
    doc = read_config(target)
    port = rif5.port_from_config(doc)
    seen = bool(doc)
    if port is None:
        if seen:
            pytest.skip("the rendered config has no enabled [[edges.radio.port]] kind mmdvm-serial with a udp address: add "
                        "rig/config/dv-router/rif5-mmdvm-udp.toml to DVT_SUT_CONFIG_EXTRA")
        port = default_port(target)
    try:
        transport = ms.PinnedUdp(port.peer, port.bind)
    except OSError as exc:
        pytest.fail("cannot bind the modem side of the UDP byte port %s:%d: %s" % (port.peer[0], port.peer[1], exc))
    if rif5.fw_on(os.environ):
        profile, delay = rif5.fw_settings(os.environ)
        modem = mmdvm_fw.FwRif5Modem(transport, profile=profile, tx_delay_ms=delay).start()
        note(target, "setup", "RECORD", "DVT_RIF5FW=on: the fake modem is firmware-like (profile %s, TX delay %s, dvtest/mmdvm_fw.py)" % (
            profile, "from SET_CONFIG" if delay is None else "%d ms" % delay))
    else:
        modem = ms.Rif5Modem(transport).start()
    control = RigControl(Path(target.out) / "control") if os.environ.get("DVT_RIG_CONTROL") == "1" else None
    if not seen:
        note(target, "setup", "RECORD", "rendered config not visible in out/: the snippet's own values are assumed (%s)" % (port.modes,))
    try:
        if not modem.configured.wait(OPEN_WAIT_S):
            pytest.fail("no SET_CONFIG from the SUT's radio edge within %.0f s on %s:%d (frames seen: %d, datagrams from other addresses: %d): is "
                        "dv-edge-radio running (rif5-mmdvm-udp.toml in DVT_SUT_CONFIG_EXTRA, tx_ack exact), and does it send from %s:%d?" % (
                            OPEN_WAIT_S, port.peer[0], port.peer[1], len(modem.host_frames()), transport.foreign, port.bind[0], port.bind[1]))
        time.sleep(1.0)
        yield SimpleNamespace(target=target, port=port, modem=modem, ip=target.xlxd_sut, control=control, baseline=None, baseline_t=None, config_seen=seen)
    finally:
        modem.stop()


@pytest.fixture(scope="module")
def web(request):
    """An anonymous client of the public API (profile web), or None."""
    if os.environ.get("DVT_WEB") != "on":
        return None
    try:
        return request.getfixturevalue("web_client")()
    except pytest.skip.Exception:
        return None


@pytest.fixture(scope="module")
def admin_factory(request):
    """admin_session of cases/web_fixtures.py, or None without the web profile."""
    if os.environ.get("DVT_WEB") != "on":
        return None
    try:
        return request.getfixturevalue("admin_session")
    except pytest.skip.Exception:
        return None


def admin_for(factory, role):
    if factory is None:
        return None
    try:
        return factory(role)
    except pytest.skip.Exception:
        return None


def hub_text(ctx):
    if ctx.control is None:
        return None
    try:
        return ctx.control.metrics("xlxd-sut")
    except (RuntimeError, TimeoutError, OSError, ValueError):
        return None


def counters_now(ctx):
    return rif5.counters(hub_text(ctx), ctx.port.name)


def ready(ctx, web, case):
    """The port is Up (the host polls the modem), the RF gate is open (no fence, public state) and the modem is back in idle."""
    modem = ctx.modem
    polled = modem.wait_frame(lambda f: f.ftype == ms.GET_STATUS, since=time.monotonic() - 0.6, timeout=40.0)
    assert polled is not None, "%s: the host does not poll the modem (the port is not Up)" % case
    if web is not None:
        res = wait_unfenced(web, ctx.port.name)
        assert res.state in ("unfenced", "clear", "absent"), "%s: the TX fence did not end: %s" % (case, res)
        row = wait_for(lambda: (lambda r: r if r and r.get("state") == "up" else None)(public_row(web, ctx.port.name)), 30.0, 0.5)
        assert row is not None, "%s: the public state does not show the port up" % case
    else:
        d = last_dialogue(modem)
        left = d.config.mono + rif5.GUARD_S + 6.0 - time.monotonic() if d else 0
        if left > 0:
            time.sleep(left)
    if not settle_idle(modem, 30.0):
        note(ctx.target, case, "FINDING", "the host did not give the modem back to idle (SET_MODE 0) within 30 s of its last mode claim")
    if ctx.baseline is None and ctx.control is not None:
        ctx.baseline = counters_now(ctx)
        ctx.baseline_t = modem.mark()


def report(ctx, case, verdict):
    for line in verdict.findings:
        note(ctx.target, case, "FINDING", line)
    note(ctx.target, case, "RECORD", ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items())) or "no facts")


# ---------------------------------------------------------------- RIF5-G1: the startup guard

def test_g1_nothing_is_written_to_the_modem_during_the_startup_guard(ctx, web, admin_factory):
    """radio.md 7.1 "No frame transmits voice during open", 7.3 and 10.4 rule 4: after a start the radio edge holds TX for G = 13 s; in that time the
    modem gets GET_VERSION, SET_FREQ, SET_CONFIG and GET_STATUS and nothing that keys it. A room over inside the guard never reaches RF, also not
    after the guard (DV-Router's own test: crates/router/tests/real_children/radio/mmdvm.rs). The guard is re-armed by restarting dv-edge-radio."""
    case = "RIF5-G1"
    admin = admin_for(admin_factory, "admin")
    if admin is None or web is None:
        pytest.skip("the startup guard exists only after a start: needs --profile web with admin.toml to restart dv-edge-radio "
                    "(POST /api/v1/admin/children/dv-edge-radio/restart); GAP in docs/rif5-cases.md")
    ready(ctx, web, case)
    modem = ctx.modem
    t_restart = modem.mark()
    status = restart_radio_edge(admin)
    assert status == 200, "POST children/%s/restart answered HTTP %d, want 200" % (rif5.CHILD, status)
    ctx.baseline, ctx.baseline_t = None, None                     # a new process: its counters start at zero
    cfg = modem.wait_configured_after(t_restart, 90.0)
    assert cfg is not None, "the restarted edge did not open the modem again within 90 s"
    # the fence as the public state shows it, as early as it can be read
    fenced = wait_for(lambda: (lambda r: r if r and r.get("tx_state") == "fenced" else None)(public_row(web, ctx.port.name)), 20.0, 0.25)
    inside = fenced is not None
    note(ctx.target, case, "RECORD", "after the restart: opened %.1f s later; fence row %s" % (
        cfg.mono - t_restart, {k: fenced.get(k) for k in ("state", "tx_state", "fence", "guard_ms", "phys_busy")} if inside else "not seen fenced"))
    if inside:
        assert fenced.get("fence") == "startup_guard", "fence %r right after a start, the document says startup_guard (radio.md 10.4 rule 4)" % fenced.get("fence")
        guard = fenced.get("guard_ms")
        assert guard is None or (isinstance(guard, int) and not isinstance(guard, bool) and guard >= 0), "guard_ms %r" % (guard,)
    # a short D-STAR over from the room inside the guard
    frames = echo.make_frames("dstar", 30, "rif5-guard")
    talker = Member(ctx.ip, "dstar", rif5.ROOMS["dstar"], 1, listen=False)
    t_over = time.monotonic()
    try:
        talker.talk(frames, tail=0.5)
    finally:
        talker.close(settle=0.5)
    row_after = public_row(web, ctx.port.name) or {}
    over_inside = row_after.get("tx_state") == "fenced"
    res = wait_unfenced(web, ctx.port.name)
    t_end = time.monotonic()
    note(ctx.target, case, "RECORD", "over sent %.1f s after the restart (fence still up after it: %s); guard ended %.1f s after the restart (%s)" % (
        t_over - t_restart, over_inside, t_end - t_restart, res.state))
    time.sleep(4.0)                                               # the guarded over must not appear later either
    verdict = rif5.judge_guard(modem.host_frames(), t_restart, time.monotonic())
    report(ctx, case, verdict)
    assert not verdict.problems, "RF-producing frames inside the guard window: %s" % verdict.problems
    if not over_inside:
        note(ctx.target, case, "FINDING", "the fence was already over when the over ended: the over was not proven to lie inside the guard (restart-to-fence %.1f s)" % (
            t_over - t_restart))
    assert res.state in ("unfenced", "clear"), "the guard did not end: %s" % (res,)
    # 13 s after the first successful probe: the end of the fence is not earlier than about G after the start of the new process
    if inside and res.state == "unfenced":
        assert t_end - t_restart >= rif5.GUARD_S - 4.0, "the fence ended %.1f s after the restart, the document says G = 13 s" % (t_end - t_restart)


# ---------------------------------------------------------------- RIF5-O: the dialogue, the polls, the port row

def test_o1_open_dialogue(ctx):
    """radio.md 7.1: GET_VERSION, SET_FREQ (E0 11 04 00 + RX, TX, RF level, POCSAG Hz), SET_CONFIG (version 2 modem: 40 B, simplex flag, enables,
    levels, colour code, delay, oscillator 0x80), GET_STATUS; no keying byte in between."""
    case = "RIF5-O1"
    d = last_dialogue(ctx.modem)
    assert d is not None, "no complete open dialogue in the modem log"
    verdict = rif5.judge_open(d, ctx.port)
    report(ctx, case, verdict)
    assert not verdict.problems, "open dialogue: %s" % verdict.problems
    refused = [n for n in ctx.modem.naks if d.versions[0].mono <= n[0] <= d.config.mono + 1.0]
    assert not refused, "the modem had to NAK a frame of the dialogue: %s" % refused


def test_o2_status_poll_every_250_ms(ctx):
    """radio.md 7.1 "Polling": GET_STATUS every 250 ms."""
    d = last_dialogue(ctx.modem)
    assert d is not None
    since = d.config.mono
    wait_for(lambda: len([f for f in ctx.modem.host_frames(since, types=(ms.GET_STATUS,))]) >= 14, 15.0)
    now = time.monotonic()
    verdict = rif5.status_cadence(ctx.modem.host_frames(), since, now if now - since < 8 else since + 8)
    report(ctx, "RIF5-O2", verdict)
    assert not verdict.problems, "status polling: %s" % verdict.problems


def test_o3_port_row_in_the_status(ctx, web, admin_factory):
    """status.md "Radio port telemetry", radio.md 12.3: the public row (observer fields) and the admin row (firmware, modem) of an Up MMDVM port."""
    case = "RIF5-O3"
    if web is None:
        pytest.skip("needs --profile web (public state)")
    ready(ctx, web, case)
    row = public_row(web, ctx.port.name)
    assert row is not None, "the port %s is not in procs[].radio.ports[] of the public state" % ctx.port.name
    note(ctx.target, case, "RECORD", "public row: %s" % row)
    want = {"kind": "mmdvm-serial", "pattern": "mmdvm-host", "duplex": False, "state": "up", "band": "70cm", "rx_freq_hz": ctx.port.rx_freq,
            "tx_freq_hz": ctx.port.tx_freq}
    problems = []
    wrong = {k: (row.get(k), v) for k, v in want.items() if row.get(k) != v}
    if wrong:
        # radio.md 12.3 (RadioPortInfo, scope obs) and status.md "Radio port telemetry" list rx_freq_hz and tx_freq_hz among the observer fields
        problems.append("public row differs (got, wanted): %s; observer fields of the document missing from the row: %s" % (
            wrong, sorted(f for f in rif5.OBSERVER_FIELDS if f not in row)))
    if rif5.j_on(os.environ) and (set(row) != set(rif5.OBSERVER_FIELDS)):
        # RIF5j (design-276 2): the public port row has exactly the 19 observer fields, the two frequencies among them
        problems.append("public row keys differ from the 19 observer fields: missing %s, extra %s" % (
            sorted(set(rif5.OBSERVER_FIELDS) - set(row)), sorted(set(row) - set(rif5.OBSERVER_FIELDS))))
    if sorted(row.get("modes") or []) != ["dmr", "dstar", "ysf"]:
        problems.append("modes %r" % (row.get("modes"),))
    if row.get("tx") not in ("on", True):
        problems.append("tx %r for a port with tx = true" % (row.get("tx"),))
    for hidden in ("firmware", "modem", "down_reason", "physical"):
        if hidden in row:
            note(ctx.target, case, "FINDING", "the public (observer) row carries the admin-only field %r" % hidden)
    # the admin part runs whatever the public row said, so that one run reports both
    operator = admin_for(admin_factory, "operator")
    if operator is None:
        note(ctx.target, case, "RECORD", "no operator session: the admin fields were not read")
    else:
        admin = admin_row(operator, ctx.port.name)
        if admin is None:
            problems.append("the port is not in radio_edges[].radio.ports[] of GET /api/v1/admin/clients")
        else:
            note(ctx.target, case, "RECORD", "admin row: %s" % admin)
            if admin.get("firmware") != ctx.modem.text:
                problems.append("firmware %r, the modem said %r" % (admin.get("firmware"), ctx.modem.text))
            if (admin.get("modem") or {}).get("protocol") != 2:
                problems.append("modem.protocol %r, the modem speaks version 2" % ((admin.get("modem") or {}).get("protocol"),))
            if admin.get("down_reason") is not None or admin.get("physical") != ctx.port.physical:
                problems.append("down_reason %r, physical %r (wanted None and %r)" % (admin.get("down_reason"), admin.get("physical"), ctx.port.physical))
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-TX: room to modem

def rx_kwargs(kind, port):
    """What the modem's RF over carries: the identity of the radio, and the repeater / colour code fields the adapter checks."""
    if kind == "dstar":
        return {"my": rif5.STATION, "rpt1": rif5.STATION.ljust(7) + port.rpt_module, "rpt2": rif5.STATION.ljust(7) + "G"}
    if kind == "dmr":
        return {"src": 1023007, "dst": port.dmr_tg, "colour_code": port.color_code}
    return {"src": rif5.STATION}


def judge_tx(kind, log, sent, ident, port, lossy=False):
    """The judge of the mode. `lossy`: a long over, where the host queue may drop words (a FINDING, radio.md 7.1 flow control)."""
    if kind == "dstar":
        return rif5.judge_tx_dstar(log, sent, ident, rif5.STATION, port.rpt_module, lossy=lossy)
    if kind == "dmr":
        return rif5.judge_tx_dmr(log, sent, ident, port.dmr_tg, port.color_code, lossy=lossy)
    return rif5.judge_tx_ysf(log, sent, ident, port.ysf_lo_dev)


def tx_over(ctx, web, kind, n, seed, case):
    """One over of `n` frames from the protocol client N0TEST into the mode's room; returns (log since the start, frames sent, expected words, ident)."""
    ready(ctx, web, case)
    frames = echo.make_frames(kind, n, "%s-%s" % (seed, kind))
    talker = Member(ctx.ip, kind, rif5.ROOMS[kind], 1, listen=False)
    since = ctx.modem.mark()
    try:
        t_talk = time.monotonic()
        talker.talk(frames, tail=0.5)
        ctx.last_talk = (t_talk, time.monotonic() - 0.5, len(frames))       # the words are spread over the talk without its 0.5 s tail (an estimate)
        ident = talker.ident()
    finally:
        talker.close(settle=0.5)
    log = quiesce(ctx.modem, since, ms.TX_TYPES[kind], quiet=1.5, timeout=30.0)
    assert log is not None, "%s: no %s frames reached the modem within 30 s of the end of the over (frames seen: %s)" % (
        case, kind, sorted({ms.name_of(f.ftype) for f in ctx.modem.host_frames(since)}))
    return log, since, echo.expected_frames(kind, frames), ident


def check_naks(ctx, case, since):
    naks = [n for n in ctx.modem.naks if n[0] >= since]
    full = [n for n in naks if n[2] == ms.NAK_FULL]
    other = [n for n in naks if n[2] != ms.NAK_FULL]
    if full:
        note(ctx.target, case, "FINDING", "the host wrote %d frame(s) the fake modem's buffer model had no room for (NAK 5 %s); a fill model, not a firmware" % (
            len(full), [ms.name_of(n[1]) for n in full][:4]))
    assert not other, "%s: the modem had to refuse frames of the host: %s (NAK 2 = mode or enable, 4 = value, 1 = unknown type)" % (
        case, [(ms.name_of(n[1]), n[2]) for n in other])


def timeline(ctx, case, since, verdict):
    """Write out/<run>/rif5/timeline-<case>.jsonl for the flow-control analysis (docs/rif5-cases.md, "The timeline") and name it in the notes."""
    sent = getattr(ctx, "last_talk", None)
    summary = {"case": case, "facts": verdict.facts, "problems": verdict.problems, "findings": verdict.findings,
               "frames_total": "not read in the case: hub counters are in control/8-N.out (cumulative, 10 s old at most)"}
    path = write_timeline(ctx.target, case, ctx.modem, since, sent, summary)
    note(ctx.target, case, "RECORD", "timeline: %s" % (path if path is None else "rif5/" + path.name))


@pytest.mark.parametrize("kind", KINDS)
def test_tx_room_to_modem(kind, ctx, web):
    """(a) A protocol client keys the room of the mode; the modem receives SET_MODE for the mode, then the native frames: D-STAR header + data + EOT,
    DMR eight headers + A to F + three terminators, YSF header + V/D mode 2 frames + terminator, the talker's identity in the header, the voice words
    byte for byte (radio.md 7.1, 7.2, 7.3, 10.3). Sizes are what the 250 ms poll lets the host keep in flight (RIF5-LONG records the rest)."""
    case = "RIF5-TX-%s" % kind
    log, since, sent, ident = tx_over(ctx, web, kind, TX_FRAMES[kind], "rif5-tx", case)
    verdict = judge_tx(kind, log, sent, ident, ctx.port)
    report(ctx, case, verdict)
    timeline(ctx, case, since, verdict)
    TALLY["tx"] += 1
    TALLY["tx_native_frames"] += len([f for f in log if f.ftype in ms.TX_TYPES[kind]])
    check_naks(ctx, case, since)
    assert not verdict.problems, "%s: %s" % (case, verdict.problems)


# ---------------------------------------------------------------- RIF5-RX: modem to room

@pytest.mark.parametrize("kind", KINDS)
def test_rx_modem_to_room(kind, ctx, web):
    """(a) The fake modem sends an RF over (valid header / LC / FICH, valid voice words); a protocol client linked to the room of the mode hears one
    over: the radio's identity, the voice words in order, the end. The RF ingress circuit is not echoed back to RF (radio.md 4.4, 7.2)."""
    case = "RIF5-RX-%s" % kind
    ready(ctx, web, case)
    listener = Member(ctx.ip, kind, rif5.ROOMS[kind], 1, listen=True)
    try:
        since = ctx.modem.mark()
        ku = ctx.modem.keyup(kind, seconds=RX_SECONDS, seed="rif5-rx", **rx_kwargs(kind, ctx.port))
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        got = wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
        wanted = {"dstar": rif5.STATION, "dmr": 1023007, "ysf": rif5.STATION}[kind]
    finally:
        listener.close(settle=0.5)
    note(ctx.target, case, "RECORD", "overs heard: %s" % [(round(o.start - overs[0].start, 2), round(o.end - o.start, 2), o.idents, len(o.frames), o.ended) for o in overs])
    assert got and len(overs) == 1, "%s: the client heard %d overs, wanted one" % (case, len(overs))
    over = overs[0]
    assert over.idents == [wanted], "%s: the talker is %s, the modem's RF source is %s" % (case, over.idents, wanted)
    if kind == "ysf":
        # a YSF over is not bit exact through the room: ingress +1.0 dB and egress -0.4 dB step only the gain index b2 (decisions D1, RIF5 D3)
        words = rif5.compare_ysf_words(ku.voice, over.frames)
        for line in words.findings:
            note(ctx.target, case, "FINDING", line)
        note(ctx.target, case, "RECORD", "voice words sent %d, heard %d, extra %d, b2 steps %s" % (
            len(ku.voice), len(over.frames), words.facts["extra_words"], words.facts["b2_steps"]))
        assert not words.problems, "%s: %s" % (case, words.problems)
    else:
        compare = echo.compare_payload(ku.voice, over.frames)
        note(ctx.target, case, "RECORD", "voice words sent %d, heard %d, first difference %s, extra %d, missing %d" % (
            len(ku.voice), len(over.frames), compare.first_diff, len(compare.extra), compare.missing))
        if compare.extra:
            note(ctx.target, case, "FINDING", "%d voice words after the %d the modem sent (a pad or an end marker of the egress)" % (len(compare.extra), len(ku.voice)))
        assert compare.prefix_ok and not compare.missing, "%s: the words heard differ from the words the modem sent (first difference at %s, %d missing)" % (
            case, compare.first_diff, compare.missing)
    assert over.ended, "%s: the over did not end on the client side" % case
    time.sleep(1.5)
    echoed = [ms.name_of(f.ftype) for f in ctx.modem.host_frames(since, types=rif5.RF_TYPES)]
    assert not echoed, "%s: the RF ingress circuit was echoed back to the modem: %s" % (case, echoed[:6])
    TALLY["rx"] += 1


def test_rx_dmr_damaged_copies_refresh_the_over_timeout(ctx, web):
    """radio.md 7.3 (S3/S4): "A header or terminator copy that fails its checks inside an open over is ignored ... and refreshes the native-over timeout:
    the firmware still tracks the carrier and sends LOST2 itself", and 7.1: an RF over ends 300 ms without a native frame. The modem plays a DMR over
    of ten voice bursts and then ten header copies with a wrong colour code, 100 ms apart, and no terminator. If the copies refresh the timeout the
    over stays open until the last copy and ends 300 ms after it (about 1.3 s after the last valid burst); if they did not, it would end 300 ms after
    the last valid burst.

    First rig run (docs/rif5-cases.md, "First rig run", RXD): the over ended 0.20 s after the last valid burst, the radio edge's own SILENCE of radio.md 4.4
    (200 ms without a unit; a damaged copy delivers none). PRODUCT finding: the assertion stays."""
    case = "RIF5-RXD"
    ready(ctx, web, case)
    kw = rx_kwargs("dmr", ctx.port)
    frames = rf.canonical_frames("dmr", echo.make_frames("dmr", 30, "rif5-rxd"))
    air, voice, count = ctx.modem.build_keyup("dmr", frames=frames, **kw)
    valid = air[:-1]                                              # header and the ten voice bursts, the real terminator dropped
    bad = rif5.dmr_damaged_copy(kw["src"], kw["dst"], kw["colour_code"])
    air = valid + [(rf.DMR_DATA2, bad, 100.0)] * RXD_COPIES
    listener = Member(ctx.ip, "dmr", rif5.ROOMS["dmr"], 1, listen=True)
    try:
        ku = ctx.modem.play_air("dmr", air, voice, count)
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        t_valid = ku.sent[len(valid) - 1][0]
        t_last = ku.sent[-1][0]
        got = wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    assert got and len(overs) == 1, "%s: the client heard %d overs, wanted one" % (case, len(overs))
    over = overs[0]
    # the listener's clock is the same monotonic clock as the modem's (one process); the end packet is the last event of the over
    after_valid, after_last = over.end - t_valid, over.end - t_last
    note(ctx.target, case, "RECORD", "last valid burst to the end of the over %.2f s, last damaged copy to the end %.2f s (refresh: about %.1f s after the last copy)" % (
        after_valid, after_last, rif5.NATIVE_OVER_TIMEOUT_S))
    compare = echo.compare_payload(ku.voice, over.frames)
    assert compare.prefix_ok and not compare.missing, "%s: the voice words heard differ from the words sent" % case
    # radio.md 4.4 gives the radio edge SILENCE = 200 ms (ten units): the core ends an ingress stream that delivers no UNIT for 200 ms, and a damaged
    # copy delivers none. An end at about 0.20 s after the last valid burst is that timer, not the 300 ms native-over timeout of the hub.
    note(ctx.target, case, "RECORD", "end %.3f s after the last valid burst; the document's radio edge SILENCE is %.1f s, the hub's native-over timeout %.1f s" % (
        after_valid, rif5.EDGE_SILENCE_S, rif5.NATIVE_OVER_TIMEOUT_S))
    assert after_valid >= (t_last - t_valid) - 0.05, "%s: the over ended %.2f s after the last valid burst, before the damaged copies stopped (%.2f s): " \
        "the damaged copies did not refresh the native-over timeout of radio.md 7.3 (the room over ends with the radio edge SILENCE of radio.md 4.4, %.1f s " \
        "without a unit)" % (case, after_valid, t_last - t_valid, rif5.EDGE_SILENCE_S)
    assert after_last <= 3.0, "%s: the over ended %.2f s after the last copy, the timeout is 0.3 s" % (case, after_last)
    if rif5.j_on(os.environ):
        # RIF5j (design-276 3.3): a frame the modem delivered inside the open over that the framer rejects becomes codec silence for its airtime (3 units
        # per DMR burst), the room over ends with the hub's 300 ms native-over timeout after the last copy (the radio edge SILENCE is 400 ms, a backstop)
        problems = []
        silence = compare.extra
        if not rif5.RXD_J_END_S[0] <= after_last <= rif5.RXD_J_END_S[1]:
            problems.append("the over ended %.2f s after the last damaged copy, wanted %.2f to %.2f s (300 ms native-over timeout)" % ((after_last,) + rif5.RXD_J_END_S))
        if len(silence) != 3 * RXD_COPIES:
            problems.append("%d words after the 30 sent, wanted %d (3 silence units for each of the %d damaged copies)" % (len(silence), 3 * RXD_COPIES, RXD_COPIES))
        elif len(set(silence)) != 1:
            problems.append("the %d extra words are not one silence word: %d different" % (len(silence), len(set(silence))))
        note(ctx.target, case, "RECORD", "RIF5j: end %.3f s after the last copy; %d extra words, distinct %d, the first %s; the rig DMR pad word is %s" % (
            after_last, len(silence), len(set(silence)), silence[0].hex() if silence else None, rf.PAD_WORD["dmr"].hex()))
        assert not problems, "%s: %s" % (case, problems)
    TALLY["rx"] += 1


VARIANTS = {"local_only": ({"rpt2": rif5.STATION.ljust(7) + "C"}, "local_only", "radio.md 7.3: a wrong RPT2 is local_only and never enters the room"),
            "bad_rpt1": ({"rpt1": rif5.STATION.ljust(7) + "C"}, "bad_header", "radio.md 7.3: a bad RPT1 is bad_header"),
            "bad_crc": ({}, "bad_header", "radio.md 7.3: a bad CRC is bad_header")}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_rx_dstar_header_refused(variant, ctx, web):
    """RF headers the D-STAR framer refuses: nothing reaches the room and the counter of the reason moves (frames_total{dir=rx,result=local_only |
    bad_header}, radio.md 12.3; the metrics come every 10 s, so they are read for up to 30 s)."""
    case = "RIF5-RXR-%s" % variant
    override, result, source = VARIANTS[variant]
    ready(ctx, web, case)
    before = counters_now(ctx)
    kw = dict(rx_kwargs("dstar", ctx.port), **override)
    frames = rf.canonical_frames("dstar", echo.make_frames("dstar", 40, "rif5-refuse"))
    air = rf.dstar_keyup(frames, **kw)
    if variant == "bad_crc":
        ftype, payload, delay = air[0]
        air[0] = (ftype, payload[:-1] + bytes((payload[-1] ^ 0x01,)), delay)
    listener = Member(ctx.ip, "dstar", rif5.ROOMS["dstar"], 1, listen=True)
    try:
        ku = ctx.modem.play_air("dstar", air, voice=frames, count=len(frames))
        assert ku.wait(10)
        time.sleep(2.0)
        heard = listener.overs()
    finally:
        listener.close(settle=0.5)
    assert not heard, "%s: a refused RF header reached the room (%s)" % (case, source)
    if ctx.control is None:
        note(ctx.target, case, "RECORD", "no control channel: the counter %s was not read" % result)
        return
    moved = wait_for(lambda: (rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result=result) or 0) >= 1, 30.0, 4.0)
    assert moved, "%s: dv_edge_radio_frames_total{dir=rx,result=%s} did not move within 30 s (%s)" % (case, result, source)


# ---------------------------------------------------------------- RIF5-LONG: record only

@pytest.mark.parametrize("kind", KINDS)
def test_long_over_is_recorded(kind, ctx, web):
    """A 3 s over from the room in each mode. radio.md 7.1 "Flow control": the host keeps at most 10 D-STAR, 3 DMR and 2 YSF frames in flight, refilled
    from the status every 250 ms, and its queue holds 16 per mode (the oldest are dropped). Whether that sustains a real-time over is not settled by the
    document: the words written against the words sent are a RECORD and a FINDING when words are missing; the case asserts only that the over has a
    start and an end and that no corrupted word was written."""
    case = "RIF5-LONG-%s" % kind
    log, since, sent, ident = tx_over(ctx, web, kind, LONG_FRAMES, "rif5-long", case)
    verdict = judge_tx(kind, log, sent, ident, ctx.port, lossy=True)
    timeline(ctx, case, since, verdict)
    TALLY["long"] += 1
    native = [f for f in log if f.ftype in ms.TX_TYPES[kind]]
    span = native[-1].mono - native[0].mono if native else 0.0
    note(ctx.target, case, "RECORD", "native frames %d over %.2f s; %s" % (len(native), span, ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items()))))
    check_naks(ctx, case, since)
    for problem in verdict.problems + verdict.findings:
        note(ctx.target, case, "FINDING", problem)
    TALLY["tx_native_frames"] += len(native)
    assert native, "%s: no native frame was written" % case
    assert verdict.facts.get("voice_written", 0) > 0 or kind == "dmr" and verdict.facts.get("voice_bursts", 0) > 0, "%s: no voice written" % case


# ---------------------------------------------------------------- RIF5-J: the image with the RIF5j fix (DVT_RIF5J=on)

def test_j_long_overs_are_lossless(ctx, web):
    """design-276 1.8, "Rig (dv-testset), what RIF5 should assert after the fix". A 3 s over from the room in each mode (RIF5-LONG's, but strict): all 150
    voice words written in order and byte-exact (YSF but for the b2 gain steps), the D-STAR sync at position 0 mod 21, the DMR burst A at 0 mod 6, no NAK
    from the fake, GET_STATUS every 50 ms while keyed (median 0.065 s at most), no `underrun` event of the firmware-like fake (DVT_RIF5FW=on; without
    it the fill model cannot run dry and that check is a RECORD), and over the three overs the hub counters: `frames_total{dir=tx,result=dropped}`
    grew by 0, `modem_total{result=tx_underrun}` exists and grew by 0. Timelines: out/<run>/rif5/timeline-J-LONG-<mode>.jsonl."""
    if not rif5.j_on(os.environ):
        pytest.skip("DVT_RIF5J is not on: this case is for an image with DV-Router RIF5j (design-276; docs/rif5-cases.md \"Cases for the fix image\")")
    case = "RIF5-J-LONG"
    problems = []
    firmware = hasattr(ctx.modem, "fw")
    before = None
    if ctx.control is not None:
        ready(ctx, web, case)
        time.sleep(11.0)                                          # the hub metrics are up to 10 s old: a baseline that holds everything before the overs
        before = counters_now(ctx)
    natives = 0
    for kind in KINDS:
        sub = "%s-%s" % (case, kind)
        log, since, sent, ident = tx_over(ctx, web, kind, LONG_FRAMES, "rif5-jlong", sub)
        verdict = judge_tx(kind, log, sent, ident, ctx.port)                   # strict: a dropped word is a problem
        native = [f for f in log if f.ftype in ms.TX_TYPES[kind]]
        natives += len(native)
        keyed = rif5.status_cadence_keyed(log, native[0].mono, native[-1].mono) if native else rif5.verdict(["no native frame was written"])
        naks = [(ms.name_of(n[1]), n[2]) for n in ctx.modem.naks if n[0] >= since]
        fw = ctx.modem.fw_events(since, names=("underrun", "nak")) if firmware else None
        timeline(ctx, sub, since, verdict)
        span = native[-1].mono - native[0].mono if native else 0.0
        note(ctx.target, sub, "RECORD", "native frames %d over %.2f s; %s; status while keyed %s; fake underrun events %s" % (
            len(native), span, ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items())), keyed.facts,
            "not modelled (DVT_RIF5FW is off)" if fw is None else len(fw)))
        for line in verdict.findings:
            note(ctx.target, sub, "FINDING", line)
        problems += ["%s: %s" % (kind, p) for p in verdict.problems + keyed.problems]
        if naks:
            problems.append("%s: the modem had to NAK frames of the host: %s" % (kind, naks[:4]))
        if fw:
            problems.append("%s: the firmware-like fake logged %s" % (kind, [(round(t - since, 3), e, d.get("dry_s")) for t, e, d in fw[:4]]))
    if before is None:
        note(ctx.target, case, "RECORD", "no control channel: the counters of dropped frames and tx_underrun were not read")
    else:
        time.sleep(12.0)
        after = counters_now(ctx)
        if not rif5.has_series(after, "dv_edge_radio_modem_total", result="tx_underrun"):
            problems.append("dv_edge_radio_modem_total{result=tx_underrun} is not exported (the fixed result label set of the fix)")
        for label, family, labels in (("dropped", "dv_edge_radio_frames_total", {"dir": "tx", "result": "dropped"}),
                                      ("tx_underrun", "dv_edge_radio_modem_total", {"result": "tx_underrun"})):
            grown = rif5.delta(before, after, family, **labels)
            note(ctx.target, case, "RECORD", "%s grew by %s over the three overs" % (label, grown))
            if grown:
                problems.append("%s grew by %s over the three overs of 150 words" % (label, grown))
        ok = rif5.delta(before, after, "dv_edge_radio_frames_total", dir="tx", result="ok")
        note(ctx.target, case, "RECORD", "frames_total{dir=tx,result=ok} grew by %s, the modem logged %d native frames" % (ok, natives))
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-D: a modem that goes silent

def wait_port_state(ctx, web, state, timeout):
    """(seconds, row) until the public row shows `state`, polled every 0.25 s; (None, last row) on timeout."""
    start = time.monotonic()
    row = None
    while time.monotonic() - start < timeout:
        row = public_row(web, ctx.port.name)
        if row and row.get("state") == state:
            return time.monotonic() - start, row
        time.sleep(0.25)
    return None, row


def recover(ctx, web, case, timeout=120.0):
    """After a fault: the modem answers again, wait for the port Up and the fence open."""
    ctx.modem.mute = False
    ctx.modem.status_mode = "valid"
    if web is not None:
        took, row = wait_port_state(ctx, web, "up", timeout)
        assert took is not None, "%s: the port did not come back Up within %.0f s of the modem answering again (row %s)" % (case, timeout, row)
        return took
    d = wait_for(lambda: (lambda x: x if x and x.statuses else None)(last_dialogue(ctx.modem)), timeout, 0.5)
    assert d is not None, "%s: no new dialogue within %.0f s" % (case, timeout)
    return None


def test_d3_a_gap_below_the_timeout_keeps_the_port_up(ctx, web):
    """radio.md 7.1: 2000 ms without a valid status after Up gives Down. A modem silent for 1.0 s answers the next poll: no Down, no new dialogue."""
    case = "RIF5-D3"
    ready(ctx, web, case)
    modem = ctx.modem
    before = len(rif5.dialogues(modem.host_frames()))
    t_mute = modem.mark()
    modem.mute = True
    time.sleep(1.0)
    modem.mute = False
    time.sleep(6.0)
    versions = modem.host_frames(t_mute, types=(ms.GET_VERSION,))
    assert not versions, "%s: a gap of 1.0 s (below the 2 s of the document) started a new dialogue: %d GET_VERSION frames" % (case, len(versions))
    assert len(rif5.dialogues(modem.host_frames())) == before
    if web is not None:
        row = public_row(web, ctx.port.name)
        assert row and row.get("state") == "up", "%s: the port is %s after a 1.0 s gap" % (case, row and row.get("state"))


def test_d2_a_short_status_does_not_refresh_inactivity(ctx, web):
    """radio.md 7.1: "Short status never refreshes inactivity". The modem answers every poll with a 10 byte status (bytes flow, no valid status); the
    port still goes Down after 2 s."""
    case = "RIF5-D2"
    ready(ctx, web, case)
    modem = ctx.modem
    before = counters_now(ctx)
    t_switch = modem.mark()
    modem.status_mode = "short"
    try:
        if web is not None:
            took, row = wait_port_state(ctx, web, "down", 20.0)
            note(ctx.target, case, "RECORD", "the public state shows Down %s s after the first short status; row %s" % (
                None if took is None else round(took, 2), row))
            assert took is not None, "%s: the port is not Down 20 s after the modem began to answer with short statuses" % case
        got = modem.wait_frame(lambda f: f.ftype == ms.GET_VERSION, since=t_switch, timeout=40.0)
        assert got is not None, "%s: the host did not reopen the link" % case
        verdict = rif5.judge_silence(modem.host_frames(), t_switch)
        report(ctx, case, verdict)
        assert not verdict.problems, "%s: %s" % (case, verdict.problems)
    finally:
        recover(ctx, web, case)
    TALLY["down"] += 1
    if ctx.control is not None and before is not None:
        wait_for(lambda: (rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result="short") or 0) >= 1, 30.0, 4.0)
        short = rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result="short")
        note(ctx.target, case, "RECORD", "frames_total{dir=rx,result=short} grew by %s" % short)
        if not short:
            note(ctx.target, case, "FINDING", "frames_total{dir=rx,result=short} did not count the short statuses (radio.md 12.3 lists `short` among the rx results)")


def test_d1_a_silent_modem_takes_the_port_down(ctx, web, admin_factory):
    """(b) The mechanism is the status poll, not a timeout of bytes: the host polls GET_STATUS every 250 ms; a valid status refreshes inactivity;
    2000 ms without one after Up gives Down with reason `inactive` (radio.md 7.1), the TX gate closes, the link is closed and reopened after a backoff
    (2 s, doubling to 60 s) with the whole dialogue: settle 2 s, GET_VERSION up to six times 1.8 s apart. Seen from outside: the polls stop about 2 s
    after the last answer, the public state shows `down`, the admin row `down_reason` inactive, the counters dv_edge_radio_modem_total{result=inactive}
    and dv_edge_radio_port_total{result=down} move. The modem answers again after the six tries and the port comes back Up."""
    case = "RIF5-D1"
    ready(ctx, web, case)
    modem = ctx.modem
    operator = admin_for(admin_factory, "operator")
    before = counters_now(ctx)
    t_mute = modem.mark()
    modem.mute = True
    reason = None
    try:
        if web is not None:
            took, row = wait_port_state(ctx, web, "down", 25.0)
            note(ctx.target, case, "RECORD", "public state Down %s s after the modem went silent; row %s" % (None if took is None else round(took, 2), row))
            assert took is not None, "%s: the port is not Down 25 s after the modem went silent" % case
            assert row.get("tx_state") == "rx-only", "tx_state %r of a Down port, the document says rx-only (radio.md 4.3: any Down -> RxOnly until the next Up)" % row.get("tx_state")
            if operator is not None:
                admin = wait_for(lambda: (lambda r: r if r and r.get("state") == "down" else None)(admin_row(operator, ctx.port.name)), 10.0, 0.5)
                if admin is not None:
                    reason = admin.get("down_reason")
                    note(ctx.target, case, "RECORD", "admin row: down_reason %r, errors %r, reconnects %r" % (reason, admin.get("errors"), admin.get("reconnects")))
        first = modem.wait_frame(lambda f: f.ftype == ms.GET_VERSION, since=t_mute, timeout=40.0)
        assert first is not None, "%s: no GET_VERSION within 40 s of the modem going silent: the host did not reopen the link" % case
        time.sleep(12.0)                                        # the six tries are 1.8 s apart: 9 s after the first, then the failure
        verdict = rif5.judge_silence(modem.host_frames(), t_mute, tries=True)
        report(ctx, case, verdict)
        note(ctx.target, case, "RECORD", "dialogue starts after the silence (seconds since the previous frame, GET_VERSION tries): %s" % (
            rif5.reopen_delays(modem.host_frames(), t_mute),))
    finally:
        took = recover(ctx, web, case)
    note(ctx.target, case, "RECORD", "the port was Up again %s s after the modem answered" % (None if took is None else round(took, 1)))
    assert not verdict.problems, "%s: %s" % (case, verdict.problems)
    if reason is not None:
        assert reason == "inactive", "%s: down_reason %r right after the silence, the document says inactive (radio.md 12.3)" % (case, reason)
    TALLY["down"] += 1
    TALLY["down_version"] += 1 if verdict.facts.get("version_tries") == rif5.VERSION_TRIES else 0
    if ctx.control is not None and before is not None:
        wait_for(lambda: (rif5.delta(before, counters_now(ctx), "dv_edge_radio_modem_total", result="inactive") or 0) >= 1, 30.0, 4.0)
        after = counters_now(ctx)
        grown = {r: rif5.delta(before, after, "dv_edge_radio_modem_total", result=r) for r in ("inactive", "version_fail", "reopen", "open")}
        grown.update({"port_" + r: rif5.delta(before, after, "dv_edge_radio_port_total", result=r) for r in ("down", "up")})
        note(ctx.target, case, "RECORD", "counter growth: %s" % grown)
        assert (grown["inactive"] or 0) >= 1, "%s: dv_edge_radio_modem_total{result=inactive} did not move" % case
        assert (grown["port_down"] or 0) >= 1, "%s: dv_edge_radio_port_total{result=down} did not move" % case


# ---------------------------------------------------------------- RIF5-M1

def test_m1_counters_account_for_the_overs(ctx, web):
    """dv_edge_radio_over_total{dir=rx|tx,result=started|ended}, dv_edge_radio_frames_total{dir=tx,result=ok}, dv_edge_radio_ptt_total{result=key|unkey}
    against what the cases did since the counters were last at zero (the edge restart of RIF5-G1) or since their first over. The hub counts every 10 s."""
    case = "RIF5-M1"
    if ctx.control is None:
        pytest.skip("needs the rig control channel (DVT_RIG_CONTROL=1, set by scripts/run.sh) for the hub metrics")
    if ctx.baseline is None:
        pytest.skip("no baseline of the counters was taken (no over ran)")
    time.sleep(12.0)
    after = counters_now(ctx)
    assert after is not None, "the hub metrics have no dv_edge_radio_* family of port %s" % ctx.port.name
    grown = {}
    for key, labels in (("rx_started", ("dv_edge_radio_over_total", {"dir": "rx", "result": "started"})),
                        ("rx_ended", ("dv_edge_radio_over_total", {"dir": "rx", "result": "ended"})),
                        ("tx_started", ("dv_edge_radio_over_total", {"dir": "tx", "result": "started"})),
                        ("tx_ended", ("dv_edge_radio_over_total", {"dir": "tx", "result": "ended"})),
                        ("ptt_key", ("dv_edge_radio_ptt_total", {"result": "key"})),
                        ("ptt_unkey", ("dv_edge_radio_ptt_total", {"result": "unkey"})),
                        ("frames_tx_ok", ("dv_edge_radio_frames_total", {"dir": "tx", "result": "ok"})),
                        ("frames_tx_dropped", ("dv_edge_radio_frames_total", {"dir": "tx", "result": "dropped"})),
                        ("frames_rx_ok", ("dv_edge_radio_frames_total", {"dir": "rx", "result": "ok"}))):
        grown[key] = rif5.delta(ctx.baseline, after, labels[0], **labels[1])
    note(ctx.target, case, "RECORD", "tally %s; counter growth %s" % (dict(TALLY), grown))
    tx_overs, rx_overs = TALLY["tx"] + TALLY["long"], TALLY["rx"]
    if not (tx_overs or rx_overs):
        pytest.skip("no accepted over was made in this run")
    problems = []
    if grown["rx_started"] < rx_overs:
        problems.append("rx started grew by %s, %d RF overs were accepted" % (grown["rx_started"], rx_overs))
    if grown["rx_ended"] < rx_overs:
        problems.append("rx ended grew by %s, %d RF overs were accepted" % (grown["rx_ended"], rx_overs))
    if grown["tx_started"] < tx_overs:
        problems.append("tx started grew by %s, %d room overs were sent" % (grown["tx_started"], tx_overs))
    if grown["ptt_key"] < tx_overs or grown["ptt_unkey"] < tx_overs:
        problems.append("ptt key / unkey grew by %s / %s, %d room overs were sent" % (grown["ptt_key"], grown["ptt_unkey"], tx_overs))
    if not grown["frames_rx_ok"]:
        problems.append("frames_total{dir=rx,result=ok} did not move although %d RF overs were accepted" % rx_overs)
    # Every frame the fake logged since the counters' baseline (the restart of G1), not only those of the overs the cases tallied: the document says
    # ok is byte-link acceptance, so the comparison is with all frames the host wrote to the byte link in that time. Native frames first; then all
    # types, so that a difference can be read against the SET_MODE and other control frames of the run.
    since = ctx.baseline_t
    written = ctx.modem.host_frames(since) if since is not None else []
    native_types = tuple(sorted({t for types in ms.TX_TYPES.values() for t in types}))
    native = [f for f in written if f.ftype in native_types]
    by_type = Counter(ms.name_of(f.ftype) for f in written if f.ftype not in (ms.GET_STATUS, ms.GET_VERSION))
    note(ctx.target, case, "RECORD", "frames the host wrote since the baseline (status polls left out): native %d, by type %s; tallied native frames of the "
                                    "room overs %d" % (len(native), dict(by_type), TALLY["tx_native_frames"]))
    if since is not None and grown["frames_tx_ok"] != len(native):
        note(ctx.target, case, "FINDING", "frames_total{dir=tx,result=ok} grew by %s, the modem logged %d native frames since the baseline (all types %s; the "
                                          "document: ok is byte-link acceptance)" % (grown["frames_tx_ok"], len(native), dict(by_type)))
    if TALLY["long"]:
        note(ctx.target, case, "RECORD", "frames_total{dir=tx,result=dropped} grew by %s over the whole run (the 3 s overs of RIF5-LONG are the likely source)" %
             grown["frames_tx_dropped"])
    assert not problems, "%s: %s" % (case, problems)
