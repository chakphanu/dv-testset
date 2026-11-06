# SPDX-License-Identifier: MIT
"""E9: echo return integrity of DV-Router's built-in echo on module E for five kinds of client / peer: DExtra,
DPlus, DCS, G3 and an XLX interlink peer (regression case for the defects the owner's voice test found in the
echo RETURN path; docs/echo-cases.md).

  (A) the echo loses exactly one 20 ms frame in about 2 of 5 echoes (arrival gap 40 ms, "no dropped unit
      in a catch-up tick") while the forward copy to another listener is bit exact;
  (B) the seq byte of the echo stream is wrong (about 95 percent of the frames on the images before the fix;
      the forward copy is 100 percent right);
  (C) the echo header: flags (3 bytes), RPT1, UR, MY and suffix must come back as the talker sent them and
      RPT2 must be the one the live (forward) copy carries, `XRF999 E` style (reflector name + room module).

One run of a kind = a sender S and a listener L, both on module E (S sends a header, RUN_FRAMES PRNG AMBE
frames, 20 ms apart from a monotonic clock, and the end packet); L records the forward copy and then the
echo, S records the echo that returns to it. Every datagram is stamped at recv (cases/echo_support.py Tap).
Each stream is judged with dvtest.echo.integrity (kind specific, see there): frames equal frames sent byte
for byte, no arrival gap above 25 ms (and the gaps of 35 ms or more counted apart), the D-STAR seq byte =
position mod 21 with 0x40 on the last packet, the sync pattern 55 2d 16 on positions mod 21 == 0 only, the
protocol's own counter. The forward copy is the control: if it fails, that is another defect (the live relay).

Kinds (pytest ids), sender -> listener, and the stream judged as:
  dextra  DExtra -> DExtra                  27 byte DSVT; forward, echo to L, echo to S
  dplus   DPlus -> DPlus                    29 / 32 byte DPlus voice; the same three streams
  dcs     DCS -> DCS                        100 byte packets; the same, plus the DCS frame counter (u24) == position
  g3      G3 terminal -> DExtra listener    S is the tester's G3 terminal (UDP 40000, DSVT in G2 framing); the
          forward copy is judged as the DExtra listener hears it; the echo to the terminal as G3
  xlx     DExtra -> XLX peer                L is the tester as XLX peer XLX996 (xlx-peer-tester.toml; the interlink
          wire: seq byte = xlxd's packet id, revision 3 frames also the DMR packet id / sub id, which must
          follow the forward copy's rule) or, with only xlx-peer.toml, a DExtra client on the peer
          reflector xlxd-peer-sut (what the peer re-encodes). Forward copy and echo to L are judged
Header rule (all kinds): echo flags / RPT1 / UR / MY / suffix == what S sent; echo RPT2 == the forward RPT2.

Runs: DVT_ECHO_INTEGRITY_RUNS (default 10), separate overs, at least 3 s between the end of the echo and the
next over. DVT_ECHO_INTEGRITY_KINDS: comma list of kinds (default all; a kind named there fails instead of
skipping when its client does not link). The numbers go to out/<run>/echo-integrity.json (one dict per kind,
merged over the tests of the run) and one line per kind is printed before the case asserts, so a failing
image still gives its numbers. Needs DVT_SUT_PROFILE=dvrouter, native launch and rig/config/dv-router/echo.toml
in DVT_SUT_CONFIG_EXTRA.
"""
import json
import os
import time
from pathlib import Path

import pytest

from dvtest import clients, echo
from dvtest.proto import xlx
from echo_support import MODULE, DcsTap, DplusTap, DstarTap, EchoCfg, G3Tap, LinkError, XlxListener, note, warm_up

pytestmark = pytest.mark.rig

RUN_FRAMES = 330                    # 6.6 s; DVT_ECHO_INTEGRITY_FRAMES overrides (a 9 s over is 450)
QUIET_AFTER_ECHO = 3.0              # seconds between the end of the echo and the next over (the floor is busy during the replay)
ECHO_START_MIN = 0.3                # a stream that starts later than this after the end of the over is the echo
MIN_STREAM_PACKETS = 10             # a shorter stream is a link join (DPlus), not an over
CASE = "E9"
ECHO_KEYS = ("echo_listener", "echo_sender")
XLX_SKIP = ("xlx kind: the SUT needs an XLX peer row on module %s and --profile interlink: add "
            "rig/config/dv-router/xlx-peer.toml (the rig's peer XLX998, seen through a client of that peer) or "
            "rig/config/dv-router/xlx-peer-tester.toml (the tester plays the peer XLX996, the interlink wire is "
            "judged) to DVT_SUT_CONFIG_EXTRA and run scripts/run.sh with --profile interlink" % MODULE)


@pytest.fixture(scope="module")
def e9_rig(rig, target):
    if target.sut_profile != "dvrouter":
        pytest.skip("the echo service is DV-Router's: DVT_SUT_PROFILE=dvrouter")
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("echo is configured in the TOML: DVT_SUT_LAUNCH=native with rig/config/dv-router/echo.toml")
    cfg = EchoCfg.locate(target)
    settings = cfg.settings if cfg else {"max_seconds": echo.DEFAULT_MAX_SECONDS, "delay_ms": echo.DEFAULT_DELAY_MS,
                                         "modules": {MODULE: True}}
    if settings is None:
        pytest.skip("no [echo] table in the rendered config: add rig/config/dv-router/echo.toml to DVT_SUT_CONFIG_EXTRA")
    if not settings["modules"].get(MODULE):
        pytest.skip("module %s is not switched on in [echo] modules (%s)" % (MODULE, settings["modules"]))
    delay = settings["delay_ms"] / 1000.0
    if not warm_up(target.xlxd_sut, delay):
        pytest.fail("module %s does not replay a 60 frame over within 60 s of the start: is dv-svc-echo running "
                    "([echo] modules = { E = true } in the rendered config)?" % MODULE)
    return target.xlxd_sut, delay, (cfg.original if cfg else None)


def env_int(name, default, low=1):
    try:
        return max(low, int(os.environ.get(name) or default))
    except ValueError:
        return default


def runs_wanted():
    return env_int("DVT_ECHO_INTEGRITY_RUNS", 10)


def selection():
    """(kinds wanted, named explicitly by DVT_ECHO_INTEGRITY_KINDS)."""
    text = os.environ.get("DVT_ECHO_INTEGRITY_KINDS") or ""
    kinds, unknown = echo.parse_kinds(text)
    if unknown:
        pytest.fail("DVT_ECHO_INTEGRITY_KINDS names %s; the kinds are %s" % (", ".join(unknown), ", ".join(echo.KINDS)))
    return kinds, bool(text.strip())


# ---------------------------------------------------------------- the clients of a kind

def pair(make_sender, make_listener):
    """A sender and a listener; the sender is closed again when the listener cannot be made."""
    sender = make_sender()
    try:
        return sender, make_listener()
    except BaseException:
        sender.close()
        raise


class KindRig:
    """The clients of one kind. new_run() makes a fresh sender and listener for one over (the XLX peer of the
    xlx kind lives for the whole test); close() ends the test. `judge_sender`: the sender's own echo return
    is a stream of the kind (not so for xlx, whose sender is a DExtra client: that is the dextra kind)."""

    def __init__(self, kind, ip, new_run, close=lambda: None, judge_sender=True, note=""):
        self.kind, self.ip, self.new_run, self.close = kind, ip, new_run, close
        self.judge_sender, self.note = judge_sender, note


def xlx_version():
    text = os.environ.get("DVT_ECHO_INTEGRITY_XLX_VERSION") or "2.6.1"
    try:
        return tuple(int(x) for x in text.split("."))[:3]
    except ValueError:
        pytest.fail("DVT_ECHO_INTEGRITY_XLX_VERSION=%r: three numbers like 2.6.1" % text)


def open_xlx(ip, target, cfg_text):
    """The xlx kind: the peer row of the rendered config decides (echo.xlx_mode); skips with the way to get one."""
    try:
        rows = echo.xlx_peer_rows(cfg_text) if cfg_text else []
    except ValueError:
        rows = []
    mode, row = echo.xlx_mode(rows, target.tester, MODULE)
    if mode is None and cfg_text is None and os.environ.get("DVT_INTERLINK") == "on" and target.is_given("xlxd_peer_sut"):
        mode = "reflector"                       # the rendered config is not visible: assume xlx-peer.toml
    if mode is None:
        pytest.skip(XLX_SKIP)
    if mode == "tester":
        peer = xlx.XlxPeer(row["callsign"], row["modules"], xlx_version())
        if not peer.link(ip, seconds=20.0):
            peer.close()
            raise LinkError("no full XLX link to %s as %s (does the SUT's [[edges.xlx.peer]] row name the tester?)" % (ip, row["callsign"]))
        return KindRig("xlx", ip, lambda: pair(lambda: DstarTap(ip, MODULE, 0), lambda: XlxListener(peer, ip)), peer.close, judge_sender=False,
                       note="tester is the XLX peer %s, revision %d" % (row["callsign"], xlx.revision_of(xlx_version())))
    if os.environ.get("DVT_INTERLINK") != "on" or not target.is_given("xlxd_peer_sut"):
        pytest.skip(XLX_SKIP)
    far = target.xlxd_peer_sut
    return KindRig("xlx", ip, lambda: pair(lambda: DstarTap(ip, MODULE, 0), lambda: DstarTap(far, MODULE, 1)), judge_sender=False,
                   note="DExtra listener on the peer reflector %s (row %s): what xlxd re-encodes, not the interlink wire" % (far, row["callsign"]))


def open_kind(kind, ip, target, cfg_text):
    taps = {"dextra": DstarTap, "dplus": DplusTap, "dcs": DcsTap}
    if kind in taps:
        return KindRig(kind, ip, lambda: pair(lambda: taps[kind](ip, MODULE, 0), lambda: taps[kind](ip, MODULE, 1)))
    if kind == "g3":
        return KindRig(kind, ip, lambda: pair(lambda: G3Tap(ip, MODULE, 0), lambda: DstarTap(ip, MODULE, 1)),
                       note="forward copy as a DExtra listener hears it, echo to the G3 terminal as G3")
    return open_xlx(ip, target, cfg_text)


# ---------------------------------------------------------------- one over

def real_streams(tap):
    """The overs a tap heard (streams of at least MIN_STREAM_PACKETS voice packets), in time order."""
    return [s for s in echo.dstar_streams(tap.snapshot(), kind=tap.kind) if len(s["voice"]) >= MIN_STREAM_PACKETS]


def echo_stream(streams, end):
    """The echo among the streams a client heard: the first that starts after the over (no stream before it
    counts: that is the forward copy), None when there is none."""
    return next((s for s in streams if s["start"] > end + ECHO_START_MIN), None)


def forward_stream(streams, end):
    return next((s for s in streams if s["start"] <= end), None)


def judge(frames, stream, kind):
    """echo.integrity of a stream (None: nothing was heard)."""
    if stream is None:
        return echo.integrity(frames, [], kind=kind)
    voice = stream["voice"]
    return echo.integrity(frames, [d for _, d in voice], [t for t, _ in voice], stream["header"], kind=kind)


def echo_over(listener, end, limit):
    """Wait until the listener has heard an echo that has ended (its last packet is 0.5 s old), or `limit`
    seconds after `end`; returns the time of the last echo packet, or None."""
    deadline = end + limit
    while time.monotonic() < deadline:
        found = echo_stream(real_streams(listener), end)
        if found is not None and time.monotonic() - found["end"] > 0.5:
            return found["end"]
        time.sleep(0.1)
    found = echo_stream(real_streams(listener), end)
    return found["end"] if found else None


def one_run(kind_rig, delay, number):
    """One over; returns {"callsign", "sent_header", "forward", "echo_listener"[, "echo_sender"]} of integrity()
    results (and the times of the end of the over and of the echo)."""
    n_frames = env_int("DVT_ECHO_INTEGRITY_FRAMES", RUN_FRAMES, 25)
    jitter = float(env_int("DVT_ECHO_INTEGRITY_JITTER_MS", 0, 0))
    stall = float(env_int("DVT_ECHO_INTEGRITY_STALL_PCT", 0, 0))
    frames = echo.make_frames("dstar", n_frames, "e9-%s-%d" % (kind_rig.kind, number))
    sender, listener = kind_rig.new_run()
    try:
        sid = clients.fresh_stream_id("dstar")
        end = sender.send_over(frames, sid, jitter_ms=jitter, seed=number + 1, stall_pct=stall)
        echo_end = echo_over(listener, end, delay + echo.over_seconds(n_frames) + 4.0)
        time.sleep(0.6)                          # the sender's copy of the echo arrives with the listener's
        heard_l = real_streams(listener)
        heard_s = real_streams(sender) if kind_rig.judge_sender else []
    finally:
        sender.close()
        listener.close()
    result = {"callsign": sender.callsign, "echo_end": echo_end, "end": end,
              "sent_header": echo.header_record(sender.sent_header),
              "forward": judge(frames, forward_stream(heard_l, end), listener.kind),
              "echo_listener": judge(frames, echo_stream(heard_l, end), listener.kind)}
    if kind_rig.judge_sender:
        result["echo_sender"] = judge(frames, echo_stream(heard_s, end), sender.kind)
    for key in ECHO_KEYS:
        if key in result and result[key]["aux_name"] == "dmr_packet_id":
            result[key]["aux_vs_forward"] = echo.aux_vs_forward(result["forward"], result[key])
    return result


def stream_keys(result):
    return ("forward",) + tuple(k for k in ECHO_KEYS if k in result)


def line(result):
    return "; ".join("%s lost %d gaps>25ms %d >=35ms %d (max %.1f) seq_ok %.3f sync_err %d aux_err %d" % (
        key, r["lost"], len(r["gaps_over_25ms"]), len(r["gaps_ge_35ms"]), r["max_gap_ms"], r["seq_ok_share"],
        len(r["sync_missing"]) + len(r["sync_extra"]), r["aux_bad_count"]) for key, r in
        ((k, result[k]) for k in stream_keys(result)))


def write_json(target, kind, data):
    """echo-integrity.json holds one dict per kind: read what the run has so far, replace this kind, write back."""
    path = Path(target.out) / "echo-integrity.json"
    try:
        try:
            whole = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(whole, dict):
                whole = {}
        except (OSError, ValueError):
            whole = {}
        whole[kind] = data
        path.write_text(json.dumps(whole, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        note(target, CASE, "RECORD", "echo-integrity.json not written: %s" % exc)


# ---------------------------------------------------------------- the case

@pytest.mark.parametrize("kind", echo.KINDS)
def test_e9_dstar_echo_return_integrity(e9_rig, target, kind):
    ip, delay, cfg_text = e9_rig
    wanted, explicit = selection()
    if kind not in wanted:
        pytest.skip("kind %s is not in DVT_ECHO_INTEGRITY_KINDS (%s)" % (kind, ",".join(wanted)))
    try:
        kind_rig = open_kind(kind, ip, target, cfg_text)
        n_runs = runs_wanted()
        results = []
        try:
            for number in range(n_runs):
                result = one_run(kind_rig, delay, number)
                results.append(result)
                note(target, CASE, "RECORD", "%s run %d/%d: %s" % (kind, number + 1, n_runs, line(result)))
                heads = {"sent": result["sent_header"], **{k: result[k]["header"] for k in stream_keys(result)}}
                note(target, CASE, "RECORD", "%s run %d headers (flags / RPT1 / RPT2 / UR / MY / suffix): %s" % (
                    kind, number + 1, json.dumps(heads, sort_keys=True)))
                # the next over starts QUIET_AFTER_ECHO after the end of the echo (the floor is busy while it plays)
                quiet_from = result["echo_end"] if result["echo_end"] is not None else time.monotonic()
                if number + 1 < n_runs:
                    time.sleep(max(0.0, quiet_from + QUIET_AFTER_ECHO - time.monotonic()))
        finally:
            kind_rig.close()
    except LinkError as exc:
        if explicit or kind == "dextra":
            pytest.fail("kind %s: %s" % (kind, exc))
        pytest.skip("kind %s: %s (the rig profile has no such edge? name the kind in DVT_ECHO_INTEGRITY_KINDS to make this a failure)" % (kind, exc))

    keys = stream_keys(results[0])
    summaries = {key: echo.summarize(key, [r[key] for r in results]) for key in keys}
    headers = [{"sent": r["sent_header"], **{k: r[k]["header"] for k in keys}} for r in results]
    header_msgs = []
    for number, hdr in enumerate(headers):
        for key in keys[1:]:
            header_msgs += ["%s run %d: %s" % (key, number + 1, m) for m in echo.header_failures(hdr["sent"], hdr["forward"], hdr[key])]
    n_frames = env_int("DVT_ECHO_INTEGRITY_FRAMES", RUN_FRAMES, 25)
    summary = {"kind": kind, "note": kind_rig.note,
               "image": os.environ.get("DVT_ECHO_IMAGE_TAG") or os.environ.get("XLXD_SUT_IMAGE", ""), "runs": n_runs,
               "frames_per_run": n_frames, "jitter_ms": env_int("DVT_ECHO_INTEGRITY_JITTER_MS", 0, 0),
               "stall_pct": env_int("DVT_ECHO_INTEGRITY_STALL_PCT", 0, 0),
               "gap_limit_ms": echo.GAP_MAX_MS, "catchup_gap_ms": echo.GAP_CATCHUP_MS, **summaries,
               "header_failures": header_msgs, "headers": headers,
               "per_run": [{k: {f: v for f, v in r[k].items() if f not in ("sync_frames", "aux_seq")} for k in keys}
                           for r in results]}
    write_json(target, kind, summary)
    print("echo-integrity kind=%s image=%s runs=%d %s | header_failures %d" % (kind, summary["image"] or "-", n_runs, " | ".join(
        "%s: lost_echoes %d lost %d wrong_seq %s max_gap %.1f gaps>25 %d gaps>=35 %d sync_err %d aux_err %d" % (
            k, s["echoes_with_lost_frame"], s["lost_frames_total"], s["share_of_frames_with_wrong_seq"], s["max_gap_ms"],
            s["gaps_over_25ms"], s["gaps_ge_35ms"], s["sync_errors"], s["aux_errors"] + s["aux_vs_forward_errors"])
        for k, s in summaries.items()), len(header_msgs)))

    failures = echo.integrity_failures(summaries) + header_msgs
    fwd = summaries["forward"]
    if fwd["lost_frames_total"] or fwd["gaps_over_25ms"] or fwd["share_of_frames_with_wrong_seq"] or fwd["aux_errors"]:
        note(target, CASE, "FINDING", "%s: the forward copy (control) is not clean: another defect than A / B / C" % kind)
    if failures:
        pytest.fail("%s echo return integrity:\n  " % kind + "\n  ".join(failures))
