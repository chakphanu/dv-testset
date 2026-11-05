# SPDX-License-Identifier: MIT
"""E9: D-STAR echo return integrity (regression case for two defects the owner's voice test found in the
echo RETURN path for a DExtra sender of DV-Router's built-in echo on module E; docs/echo-cases.md).

  (A) the echo loses exactly one 20 ms frame in about 2 of 5 echoes (arrival gap 40 ms) while the
      forward copy to another DExtra listener is bit exact;
  (B) the seq byte of the echo stream is wrong (about 3 to 5 percent of the frames carry seq == n mod 21;
      the forward copy is 100 percent right).

One run = a DExtra sender S and a second DExtra listener L, both linked to module E; S sends a header,
RUN_FRAMES PRNG AMBE frames (20 ms from a monotonic clock) and the end packet; L records the forward copy
and then the echo, S records the echo that returns to it. Every datagram is stamped at recv
(cases/echo_support.py DstarTap). Each of the three streams (forward copy, echo to L, echo to S) is judged
with dvtest.echo.integrity: frames equal frames sent byte for byte, no arrival gap above 25 ms, seq byte =
position mod 21 with 0x40 on the last packet, the sync pattern 55 2d 16 on positions mod 21 == 0 only. The
forward copy is the control: if it fails, that is another defect (the live relay), and said so.

Runs: DVT_ECHO_INTEGRITY_RUNS (default 10), separate overs, at least 3 s between the end of the echo and the
next over. The numbers go to out/<run>/echo-integrity.json and one line is printed before the case asserts,
so a failing image still gives its numbers. Needs DVT_SUT_PROFILE=dvrouter, native launch and
rig/config/dv-router/echo.toml in DVT_SUT_CONFIG_EXTRA.
"""
import json
import os
import time
from pathlib import Path

import pytest

from dvtest import clients, echo
from echo_support import MODULE, DstarTap, EchoCfg, note, warm_up

pytestmark = pytest.mark.rig

RUN_FRAMES = 330                    # 6.6 s
QUIET_AFTER_ECHO = 3.0              # seconds between the end of the echo and the next over (the floor is busy during the replay)
ECHO_START_MIN = 0.3                # a stream that starts later than this after the end of the over is the echo
CASE = "E9"


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
    return target.xlxd_sut, delay


def runs_wanted():
    try:
        return max(1, int(os.environ.get("DVT_ECHO_INTEGRITY_RUNS", "10")))
    except ValueError:
        return 10


def echo_stream(streams, end):
    """The echo among the streams a client heard: the first that starts after the over (no stream before it
    counts: that is the forward copy), None when there is none."""
    return next((s for s in streams if s["start"] > end + ECHO_START_MIN), None)


def forward_stream(streams, end):
    return next((s for s in streams if s["start"] <= end), None)


def judge(frames, stream):
    """echo.integrity of a stream (None: nothing was heard)."""
    if stream is None:
        return echo.integrity(frames, [])
    voice = stream["voice"]
    return echo.integrity(frames, [d for _, d in voice], [t for t, _ in voice], stream["header"])


def echo_over(tap_listener, end, delay, limit):
    """Wait until L has heard an echo that has ended (its last packet is 0.5 s old), or `limit` seconds
    after `end`; returns the time of the last echo packet, or None."""
    deadline = end + limit
    while time.monotonic() < deadline:
        found = echo_stream(echo.dstar_streams(tap_listener.snapshot()), end)
        if found is not None and time.monotonic() - found["end"] > 0.5:
            return found["end"]
        time.sleep(0.1)
    found = echo_stream(echo.dstar_streams(tap_listener.snapshot()), end)
    return found["end"] if found else None


def one_run(ip, delay, number):
    """One over; returns {"callsign", "forward", "echo_listener", "echo_sender"} of integrity() results."""
    frames = echo.make_frames("dstar", RUN_FRAMES, "e9-%d" % number)
    sender = DstarTap(ip, MODULE, 0)
    listener = DstarTap(ip, MODULE, 1)
    try:
        sid = clients.fresh_stream_id("dstar")
        end = sender.send_over(frames, sid)
        echo_end = echo_over(listener, end, delay, delay + echo.over_seconds(RUN_FRAMES) + 4.0)
        time.sleep(0.6)                          # the sender's copy of the echo arrives with the listener's
        heard_l = echo.dstar_streams(listener.snapshot())
        heard_s = echo.dstar_streams(sender.snapshot())
    finally:
        sender.close()
        listener.close()
    return {"callsign": sender.callsign, "echo_end": echo_end, "end": end,
            "forward": judge(frames, forward_stream(heard_l, end)),
            "echo_listener": judge(frames, echo_stream(heard_l, end)),
            "echo_sender": judge(frames, echo_stream(heard_s, end))}


def line(result):
    return "; ".join("%s lost %d gaps>25ms %d (max %.1f) seq_ok %.3f sync_err %d" % (
        key, r["lost"], len(r["gaps_over_25ms"]), r["max_gap_ms"], r["seq_ok_share"],
        len(r["sync_missing"]) + len(r["sync_extra"])) for key, r in
        ((k, result[k]) for k in ("forward", "echo_listener", "echo_sender")))


def test_e9_dstar_echo_return_integrity(e9_rig, target):
    ip, delay = e9_rig
    n_runs = runs_wanted()
    results, headers = [], []
    for number in range(n_runs):
        result = one_run(ip, delay, number)
        results.append(result)
        headers.append({k: result[k]["header"] for k in ("forward", "echo_listener", "echo_sender")})
        note(target, CASE, "RECORD", "run %d/%d: %s" % (number + 1, n_runs, line(result)))
        note(target, CASE, "RECORD", "run %d headers (RPT1 / RPT2 / MY / UR): %s" % (number + 1, json.dumps(headers[-1], sort_keys=True)))
        # the next over starts QUIET_AFTER_ECHO after the end of the echo (the floor is busy while it plays)
        quiet_from = result["echo_end"] if result["echo_end"] is not None else time.monotonic()
        if number + 1 < n_runs:
            time.sleep(max(0.0, quiet_from + QUIET_AFTER_ECHO - time.monotonic()))

    summaries = {key: echo.summarize(key, [r[key] for r in results]) for key in ("forward", "echo_listener", "echo_sender")}
    summary = {"image": os.environ.get("XLXD_SUT_IMAGE", ""), "runs": n_runs, "frames_per_run": RUN_FRAMES,
               "gap_limit_ms": echo.GAP_MAX_MS, **summaries,
               "headers": headers,
               "per_run": [{k: {f: v for f, v in r[k].items() if f != "sync_frames"}
                            for k in ("forward", "echo_listener", "echo_sender")} for r in results]}
    try:
        with open(Path(target.out) / "echo-integrity.json", "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        note(target, CASE, "RECORD", "echo-integrity.json not written: %s" % exc)
    print("echo-integrity image=%s runs=%d %s" % (summary["image"] or "-", n_runs, " | ".join(
        "%s: lost_echoes %d lost %d wrong_seq %s max_gap %.1f gaps %d sync_err %d" % (
            k, s["echoes_with_lost_frame"], s["lost_frames_total"], s["share_of_frames_with_wrong_seq"], s["max_gap_ms"],
            s["gaps_over_25ms"], s["sync_errors"]) for k, s in summaries.items())))

    failures = echo.integrity_failures(summaries)
    callsign = results[0]["callsign"]
    for key in ("echo_listener", "echo_sender"):
        for number, hdr in enumerate(h[key] for h in headers):
            if hdr is None:
                failures.append("%s run %d: no header before the echo" % (key, number + 1))
            elif hdr["my"] != callsign:
                failures.append("%s run %d: echo header MY is %r, the sender is %r" % (key, number + 1, hdr["my"], callsign))
    fwd = summaries["forward"]
    if fwd["lost_frames_total"] or fwd["gaps_over_25ms"] or fwd["share_of_frames_with_wrong_seq"]:
        note(target, CASE, "FINDING", "the forward copy (control) is not clean: another defect than A / B")
    if failures:
        pytest.fail("D-STAR echo return integrity:\n  " + "\n  ".join(failures))
