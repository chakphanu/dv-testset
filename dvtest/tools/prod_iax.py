# SPDX-License-Identifier: MIT
"""Production check (f): an AllStar phone call to the echo module of a live DV-Router (docs/prod-checks.md).

Run on the HOST of the reflector against its own IAX2 port (127.0.0.1:4569) by the deploy session. One IAX2
client registers with the account secret (REGREQ, CALLTOKEN, REGAUTH, REGACK), dials the node of module E (NEW,
CALLTOKEN, AUTHREQ, AUTHREP, ACCEPT, ANSWER), keys (RADIO_KEY), sends 3.0 s of a 700 Hz tone as ulaw 8 kHz voice
frames of 20 ms, unkeys (RADIO_UNKEY), listens for the echo replay (about 1 s later) and hangs up. Asserted:

  regack            the registration ends with REGACK
  answer            the call is ACCEPTed and ANSWERed
  replay_frames     at least 140 of 150 frames come back after the unkey (scaled for other lengths)
  replay_frequency  the dominant frequency of the decoded ulaw is 700 Hz within 20 Hz
  replay_level      the replay is not silence (rms above 300 of 32768)
  hangup            the HANGUP was sent and the socket closed cleanly
  hub_metrics       (only with --hub-metrics URL) calls ok +1, voice frames rx and tx moved
  public_state      (only with --public-state-url URL and --callsign) the public state names the callsign on
                    air in the module while the call is up

    python3 -m dvtest.tools.prod_iax --account ACCOUNT --callsign N0CALL --secret-file PATH --dry-run
    python3 -m dvtest.tools.prod_iax --account ACCOUNT --callsign N0CALL --secret-file PATH --i-have-owner-approval

Safety rails, all enforced before the first packet:
  * --i-have-owner-approval is required for everything that is not --dry-run;
  * the module is E; any other module (or a --dial that is not the node of module E) needs --module X AND
    --allow-other-module; this tool never keys another module otherwise;
  * the host must be a loopback address unless --allow-non-loopback-host is given;
  * the secret comes from --secret-file (a regular file, mode 0600 or stricter, else refused) or from the
    environment variable DVT_PROD_IAX_SECRET; it is never printed, logged or written (every line and file of a
    run is scrubbed, and a unit test greps for it);
  * --seconds is 3.0 by default, capped at 5.0, floored at 1.2; a global timeout; the call is hung up in a
    finally block;
  * --dry-run opens no socket, resolves no name, reads no secret and writes no file.

No host, account, callsign, address or secret is built in. Stdlib only. Exit code 0: every check passed (or dry
run); 1: a check failed; 2: usage error.
"""
import argparse
import json
import math
import os
import re
import struct
import sys
import time

from dvtest import asl_lab, metrics
from dvtest.proto import iax2, iax2_reg
from dvtest.tools import prod_common as pc
from dvtest.tools.prod_common import UsageError

MODULE = "E"
IAX_PORT = 4569
ENV_SECRET = "DVT_PROD_IAX_SECRET"
TONE_HZ = 700
TONE_AMP = 8000
FREQ_TOL_HZ = 20.0
FRAME_MS = 20
DEFAULT_SECONDS = 3.0
MAX_SECONDS = 5.0
MIN_SECONDS = 1.2               # the echo never replays an over under 50 frames (1.0 s)
MIN_REPLAY_OF_150 = 140         # decision: >= 140 of 150 frames at 3.0 s; scaled for other lengths
LEVEL_FLOOR_RMS = 300.0         # 16 bit units; the 700 Hz test tone is about 5600
REPLAY_WAIT_S = 8.0             # the replay starts about 1 s after the unkey and lasts about as long as the over
REPLAY_QUIET_S = 0.6            # this long without a frame after the last one: the replay is over
GLOBAL_TIMEOUT_S = 120.0
METRICS_WAIT_S = 30.0           # the edge publishes its counters to the hub about every 10 s (the hub caches them)
REGISTER_TIMEOUT_S = 6.0
CALL_TIMEOUT_S = 8.0
REFRESH_S = 60
# One node number per module on the production host: A..H are consecutive from 400161 (A) and X is 400169
# (ASL1b facts, work-units). ASSUMPTION: the phone client dials the node number of the module, as the account's
# rooms list maps it (ASL1f); the form is a parameter, --dial.
NODE_BASE = 400161
NODE_X = 400169

COUNTERS = {
    "setup_ok": ("dv_edge_allstar_setup_total", {"kind": "client", "result": "ok"}),
    "voice_rx": ("dv_edge_allstar_frames_total", {"dir": "rx", "kind": "voice"}),
    "voice_tx": ("dv_edge_allstar_frames_total", {"dir": "tx", "kind": "voice"}),
}


# ---------------------------------------------------------------- pure parts

def node_for_module(module):
    """The node number the phone client dials for a module letter: A..H = 400161..400168, X = 400169."""
    if module == "X":
        return str(NODE_X)
    if len(module) == 1 and "A" <= module <= "H":
        return str(NODE_BASE + ord(module) - ord("A"))
    raise UsageError("no documented node number for module %s: give --dial" % module)


def min_replay_frames(sent):
    """140 of 150, scaled to the number of frames sent."""
    return int(math.ceil(sent * MIN_REPLAY_OF_150 / 150.0))


def tone_frames(seconds, freq=TONE_HZ, amp=TONE_AMP):
    """The over: `seconds` of a pure sine as ulaw 8 kHz frames of 160 bytes (20 ms)."""
    data = asl_lab.tone_ulaw(int(round(seconds * 1000)), freq, amp)
    return [data[i:i + 160] for i in range(0, len(data) - 159, 160)]


def new_elements(dial, account, caller_name):
    """The information elements of NEW: VERSION 2, CALLED_NUMBER (the dial string), CALLING_NAME, USERNAME, FORMAT
    and CAPABILITY ulaw. No CALLING_NUMBER, as a phone client sends (docs/asl-dvswitch.md)."""
    return b"".join([
        iax2.information(11, struct.pack("!H", 2)), iax2.information(1, dial.encode()),
        iax2.information(4, caller_name.encode()), iax2.information(6, account.encode()),
        iax2.information(9, struct.pack("!I", iax2.ULAW)), iax2.information(8, struct.pack("!I", iax2.ULAW))])


def judge_replay(replay, sent, tone_hz=TONE_HZ, tol=FREQ_TOL_HZ):
    """Judge the frames (160 byte ulaw payloads) that arrived after the unkey: returns
    {"frames", "min_frames", "frames_ok", "hz", "freq_ok", "fraction", "rms", "level_ok"}."""
    need = min_replay_frames(sent)
    samples = pc.ulaw_samples(b"".join(replay))
    hz, _amp, frac, rms = pc.dominant_frequency(samples)
    return {"frames": len(replay), "min_frames": need, "frames_ok": len(replay) >= need,
            "hz": round(hz, 1), "freq_ok": len(replay) >= 1 and abs(hz - tone_hz) <= tol and rms > 0,
            "fraction": round(frac, 3), "rms": round(rms, 1), "level_ok": rms > LEVEL_FLOOR_RMS}


def read_counters(text):
    """{"setup_ok", "voice_rx", "voice_tx", "series"} out of Prometheus text; series counts the AllStar samples."""
    samples = metrics.parse(text)
    out = {key: metrics.total(samples, name, **labels) for key, (name, labels) in COUNTERS.items()}
    out["series"] = sum(1 for s in samples if s.name.startswith("dv_edge_allstar_"))
    return out


def judge_counters(before, after):
    """(passed, deltas) for the metric deltas: calls ok +1 at least, voice frames rx and tx moved."""
    delta = {k: after[k] - before[k] for k in COUNTERS}
    ok = delta["setup_ok"] >= 1 and delta["voice_rx"] > 0 and delta["voice_tx"] > 0
    return ok, delta


class Check:
    def __init__(self, name, passed, detail, **extra):
        self.name, self.passed, self.detail, self.extra = name, passed, detail, extra

    def doc(self):
        out = {"tool": "prod_iax", "check": self.name, "pass": self.passed, "detail": self.detail}
        out.update(self.extra)
        return out


def build_options(argv, environ=None):
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(
        prog="python3 -m dvtest.tools.prod_iax", allow_abbrev=False,
        description="Production check (f): register, dial module E, send a 3 s 700 Hz tone, judge the echo replay.")
    ap.add_argument("--account", required=True, help="IAX2 account name (USERNAME of REGREQ and NEW); an argument, never built in")
    ap.add_argument("--callsign", help="CALLING_NAME of the call and the callsign expected in the public state (default: the account name)")
    ap.add_argument("--secret-file", help="file with the account secret, one line, mode 0600 (else %s)" % ENV_SECRET)
    ap.add_argument("--host", default="127.0.0.1", help="the IAX2 host (default 127.0.0.1; anything else needs --allow-non-loopback-host)")
    ap.add_argument("--port", type=int, default=IAX_PORT, help="IAX2 port (default %d)" % IAX_PORT)
    ap.add_argument("--module", default=MODULE, help="module to call (default E; any other needs --allow-other-module)")
    ap.add_argument("--dial", help="CALLED_NUMBER: the node number of the module (default: derived, A..H = %d..%d)" % (NODE_BASE, NODE_BASE + 7))
    ap.add_argument("--allow-other-module", action="store_true", help="allow a module other than E (and a --dial that is not its node)")
    ap.add_argument("--allow-non-loopback-host", action="store_true", help="allow --host to be a non-loopback address")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help="length of the tone (default %g, at most %g, at least %g)" % (DEFAULT_SECONDS, MAX_SECONDS, MIN_SECONDS))
    ap.add_argument("--replay-wait", type=float, default=REPLAY_WAIT_S, help="seconds to wait for the replay after the unkey (default %g)" % REPLAY_WAIT_S)
    ap.add_argument("--timeout", type=float, default=GLOBAL_TIMEOUT_S, help="global timeout in seconds (default %g)" % GLOBAL_TIMEOUT_S)
    ap.add_argument("--hub-metrics", help="URL of the hub metrics text (read-only GET before and after); optional")
    ap.add_argument("--metrics-wait", type=float, default=METRICS_WAIT_S, help="seconds to wait for the counters to move (default %g)" % METRICS_WAIT_S)
    ap.add_argument("--public-state-url", help="URL of GET /api/v1/public/state (read-only, polled during the call); optional, needs --callsign")
    ap.add_argument("--ca-file", help="CA bundle for https URLs INSTEAD of the system roots (a lab CA; verification is never off)")
    ap.add_argument("--out", help="directory for results.json and summary.txt (default out/prod-iax-<utc>/)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no socket, no name resolution, no secret read, no file")
    ap.add_argument("--i-have-owner-approval", action="store_true", help="required for a real run: the owner approved this check")
    ns = ap.parse_args(argv)
    ns.environ = environ
    ns.account = pc.check_name("--account", ns.account)
    if ns.callsign is not None:
        ns.callsign = pc.check_name("--callsign", ns.callsign)
    if not re.fullmatch(r"[A-Z]", ns.module):
        raise UsageError("--module must be one capital letter")
    if ns.module != MODULE and not ns.allow_other_module:
        raise UsageError("module %s is not the echo module E; another module needs --allow-other-module as well "
                         "(this tool does not key another module otherwise)" % ns.module)
    if ns.dial is None:
        ns.dial = node_for_module(ns.module)
    elif not re.fullmatch(r"[0-9A-Za-z*#]{1,32}", ns.dial):
        raise UsageError("--dial must be 1 to 32 characters of 0-9 A-Z a-z * #")
    elif ns.dial != node_for_module(MODULE) and not ns.allow_other_module:
        raise UsageError("--dial %s is not the node of module E (%s); another dial string needs --allow-other-module"
                         % (ns.dial, node_for_module(MODULE)))
    if not 1 <= ns.port <= 65535:
        raise UsageError("--port must be 1 to 65535")
    if not (ns.seconds == ns.seconds) or not MIN_SECONDS <= ns.seconds <= MAX_SECONDS:
        raise UsageError("--seconds must be between %g and %g" % (MIN_SECONDS, MAX_SECONDS))
    ns.frames = int(round(ns.seconds * 1000 / FRAME_MS))
    if ns.replay_wait < 4.0 or ns.timeout < 20.0 or ns.metrics_wait < 0:
        raise UsageError("--replay-wait must be at least 4, --timeout at least 20 and --metrics-wait not negative")
    if not ns.allow_non_loopback_host and not is_loopback(ns.host):
        raise UsageError("--host %s is not a loopback address: this check runs on the reflector host against 127.0.0.1; "
                         "--allow-non-loopback-host overrides" % ns.host)
    if ns.hub_metrics:
        pc.check_url("--hub-metrics", ns.hub_metrics)
    if ns.public_state_url:
        pc.check_url("--public-state-url", ns.public_state_url)
        if not ns.callsign:
            raise UsageError("--public-state-url needs --callsign (the callsign the public state must name)")
    if ns.ca_file and not os.path.isfile(ns.ca_file):
        raise UsageError("--ca-file %s is not a file" % ns.ca_file)
    if ns.secret_file:
        pc.check_secret_file(ns.secret_file)                 # mode check also in a dry run; the content is not read
    if not ns.dry_run and not ns.i_have_owner_approval:
        raise UsageError("this check calls the echo module of a live reflector: add --i-have-owner-approval once the owner "
                         "approved it (--dry-run shows the plan and sends nothing)")
    return ns


def is_loopback(host):
    import ipaddress
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# ---------------------------------------------------------------- plan (dry run)

def hexshort(data, limit=40):
    data = bytes(data)
    return data[:limit].hex() + ("... (%d bytes)" % len(data) if len(data) > limit else "")


def make_plan(opts):
    """The plan as data: the frames the tool would send (hex, with a placeholder call number 1) and the checks."""
    frames = tone_frames(opts.seconds)
    reg1 = iax2.full_frame(1, 0, 0, 0, 0, iax2.IAX, iax2_reg.REGREQ, iax2_reg.request_elements(opts.account, REFRESH_S, token=b""))
    new = iax2.full_frame(1, 0, 0, 0, 0, iax2.IAX, iax2.NEW,
                          new_elements(opts.dial, opts.account, opts.callsign or opts.account) + iax2.information(54, b""))
    voice0 = iax2.full_frame(1, 0, 0, 2, 0, iax2.VOICE, iax2.ULAW, frames[0])
    plan = {
        "host": opts.host, "port": opts.port, "account": opts.account, "module": opts.module, "dial": opts.dial,
        "caller_name": opts.callsign or opts.account, "tone_hz": TONE_HZ, "seconds": opts.seconds, "frames": len(frames),
        "frame_bytes": 160, "first_register_frame": hexshort(reg1), "first_new_frame": hexshort(new),
        "first_voice_frame": hexshort(voice0), "secret_source": "file" if opts.secret_file else "env " + ENV_SECRET,
        "checks": ["regack", "answer", "replay_frames >= %d of %d" % (min_replay_frames(len(frames)), len(frames)),
                   "replay_frequency %d Hz +- %g Hz" % (TONE_HZ, FREQ_TOL_HZ), "replay_level rms > %g" % LEVEL_FLOOR_RMS, "hangup"],
        "hub_metrics": pc.url_label(opts.hub_metrics), "public_state_url": pc.url_label(opts.public_state_url)}
    if opts.hub_metrics:
        plan["checks"].append("hub_metrics (calls ok +1, voice rx and tx moved)")
    if opts.public_state_url:
        plan["checks"].append("public_state names %s on module %s" % (opts.callsign, opts.module))
    return plan


def print_plan(opts, plan, out=None):
    out = out or sys.stdout
    w = lambda s="": print(s, file=out)                         # noqa: E731
    w("DRY RUN: nothing is sent, no socket is opened, no name is resolved, the secret is not read")
    w("IAX2 %s:%d  account %s  module %s  dial %s  caller name %s" % (
        plan["host"], plan["port"], plan["account"], plan["module"], plan["dial"], plan["caller_name"]))
    w("secret: %s (mode checked, never printed)" % plan["secret_source"])
    w("steps: REGREQ (CALLTOKEN, REGAUTH, MD5) -> REGACK; NEW (CALLTOKEN, AUTHREQ, AUTHREP) -> ACCEPT, ANSWER; RADIO_KEY;")
    w("       %d ulaw frames of 20 ms (%g s of %d Hz); RADIO_UNKEY; listen up to %g s for the replay; HANGUP" % (
        plan["frames"], plan["seconds"], TONE_HZ, opts.replay_wait))
    w("  first REGREQ  %s" % plan["first_register_frame"])
    w("  first NEW     %s" % plan["first_new_frame"])
    w("  first voice   %s" % plan["first_voice_frame"])
    w("checks: " + "; ".join(plan["checks"]))
    w("optional reads: hub metrics %s; public state %s" % (plan["hub_metrics"] or "not given (skipped)", plan["public_state_url"] or "not given (skipped)"))
    w("To run for real: drop --dry-run and add --i-have-owner-approval.")


# ---------------------------------------------------------------- the call

class TimedClient(iax2.Client):
    """iax2.Client that notes when each voice payload arrived."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.media_times = []

    def handle(self, data, peer):
        super().handle(data, peer)
        now = time.monotonic()
        while len(self.media_times) < len(self.media):
            self.media_times.append(now)


def fetch_counters(opts, redact):
    status, body = pc.http_get(opts.hub_metrics, opts.ca_file, timeout=10.0)
    if status != 200:
        raise OSError("hub metrics HTTP %d" % status)
    return read_counters(body.decode("utf-8", "replace"))


def run_call(opts, secret, redact, deadline, say):
    """Register, call, send the over, collect the replay, hang up. Returns (checks, facts)."""
    checks, facts = [], {}
    bind = "127.0.0.1" if is_loopback(opts.host) else "0.0.0.0"

    # -- hub metrics before
    before = None
    metrics_on = bool(opts.hub_metrics)
    if metrics_on:
        try:
            before = fetch_counters(opts, redact)
            facts["counters_before"] = before
            if not before["series"]:
                say("prod_iax: note: the metrics text has no dv_edge_allstar_ series")
        except OSError as exc:
            checks.append(Check("hub_metrics", False, "cannot read %s before the call: %s" % ("the metrics URL", redact.scrub(exc))))
            metrics_on = False

    # -- registration
    say("prod_iax: registering %s at %s:%d" % (opts.account, opts.host, opts.port))
    try:
        reg = iax2_reg.register(opts.host, opts.account, secret, port=opts.port, refresh=REFRESH_S,
                                timeout=min(REGISTER_TIMEOUT_S, max(1.0, deadline - time.monotonic())), bind=bind)
        detail = "%s after %s (%s)" % (reg.outcome, "%.2f s" % reg.seconds, " > ".join(reg.steps))
        if reg.outcome == "REGREJ":
            detail += "; cause %r code %d" % (reg.cause, reg.causecode)
        checks.append(Check("regack", reg.outcome == "REGACK", redact.scrub(detail), steps=reg.steps, seconds=reg.seconds, refresh=reg.refresh))
        facts["registration"] = reg.outcome
    except Exception as exc:                        # noqa: BLE001 - reported in the check
        checks.append(Check("regack", False, redact.scrub("%s: %s" % (type(exc).__name__, str(exc)[:160]))))
        return checks, facts
    if reg.outcome != "REGACK":
        checks.append(Check("answer", False, "not tried: the registration did not end in REGACK"))
        return checks, facts

    # -- the call
    client = None
    watcher = None
    sent = 0
    replay, replay_times = [], []
    key_time = unkey_time = None
    try:
        client = TimedClient(opts.host, secret, username=opts.account, port=opts.port, bind=bind, codec=iax2.ULAW)
        say("prod_iax: dialling %s (module %s)" % (opts.dial, opts.module))
        t_call = time.monotonic()
        try:
            client.call(opts.dial, timeout=min(CALL_TIMEOUT_S, max(1.0, deadline - time.monotonic())),
                        elements=new_elements(opts.dial, opts.account, opts.callsign or opts.account), empty_token=True,
                        wait_newkey=False)
            checks.append(Check("answer", True, "ACCEPT and ANSWER after %.2f s" % (time.monotonic() - t_call),
                                seconds=round(time.monotonic() - t_call, 3)))
        except Exception as exc:                    # noqa: BLE001
            checks.append(Check("answer", False, redact.scrub("%s: %s (accepted=%s answered=%s)" % (
                type(exc).__name__, str(exc)[:120], client.accepted, client.answered))))
            return checks, facts
        frames = tone_frames(opts.seconds)
        client.media.clear()
        client.media_times.clear()
        if opts.public_state_url:
            watcher = pc.StateWatcher(opts.public_state_url, opts.module, opts.callsign, opts.ca_file).start()
        say("prod_iax: keying, %d frames (%g s of %d Hz)" % (len(frames), opts.seconds, TONE_HZ))
        client.key()
        key_time = time.monotonic()
        for i, frame in enumerate(frames):
            client.pump(max(0.0, key_time + i * FRAME_MS / 1000.0 - time.monotonic()))
            client.voice(frame)
            sent += 1
        client.pump(max(0.0, key_time + len(frames) * FRAME_MS / 1000.0 - time.monotonic()))
        client.unkey()
        unkey_time = time.monotonic()
        mark = len(client.media)
        say("prod_iax: unkeyed, waiting for the replay")
        end = unkey_time + opts.replay_wait
        last = None
        while time.monotonic() < min(end, deadline):
            client.pump(0.05)
            if len(client.media) > mark:
                last = client.media_times[-1]
                if len(client.media) - mark >= sent and time.monotonic() - last >= 0.3:
                    break
                if time.monotonic() - last >= REPLAY_QUIET_S and len(client.media) - mark >= 1:
                    break
            if client.closed:
                break
        replay = list(client.media[mark:])
        replay_times = list(client.media_times[mark:])
        facts["live_relay_frames"] = mark
        facts["controls_from_edge"] = list(client.controls)
        facts["retransmissions"] = client.retransmissions
    except Exception as exc:                        # noqa: BLE001
        checks.append(Check("call_error", False, redact.scrub("%s: %s" % (type(exc).__name__, str(exc)[:160]))))
    finally:
        hung_up = False
        err = ""
        if client is not None:
            try:
                say("prod_iax: hanging up")
                client.close()
                hung_up = True
            except Exception as exc:                # noqa: BLE001
                err = redact.scrub("%s: %s" % (type(exc).__name__, str(exc)[:100]))
                try:
                    client.socket.close()
                except OSError:
                    pass
        if watcher is not None:
            facts["public_state"] = watcher.stop()
        if client is not None:
            checks.append(Check("hangup", hung_up, "HANGUP sent, socket closed" if hung_up else "hang up failed: " + err))

    if sent:
        verdict = judge_replay(replay, sent)
        delay = round(replay_times[0] - unkey_time, 2) if replay_times and unkey_time else None
        span = round(replay_times[-1] - replay_times[0], 2) if len(replay_times) > 1 else None
        facts["replay"] = dict(verdict, delay_s=delay, span_s=span, sent=sent)
        checks.append(Check("replay_frames", verdict["frames_ok"], "%d of %d frames back (need %d), first %s s after the unkey, over %s s" % (
            verdict["frames"], sent, verdict["min_frames"], delay, span), frames=verdict["frames"], sent=sent,
            min_frames=verdict["min_frames"], replay_delay_s=delay, replay_span_s=span))
        checks.append(Check("replay_frequency", verdict["freq_ok"], "dominant %.1f Hz (want %d +- %g), tone fraction %.2f" % (
            verdict["hz"], TONE_HZ, FREQ_TOL_HZ, verdict["fraction"]), hz=verdict["hz"], fraction=verdict["fraction"]))
        checks.append(Check("replay_level", verdict["level_ok"], "rms %.0f (floor %g)" % (verdict["rms"], LEVEL_FLOOR_RMS), rms=verdict["rms"]))
    else:
        for name in ("replay_frames", "replay_frequency", "replay_level"):
            if not any(c.name == name for c in checks):
                checks.append(Check(name, False, "no over was sent"))

    # -- optional reads
    if metrics_on and before is not None:
        ok, delta, after = False, {}, before
        stop = time.monotonic() + opts.metrics_wait
        while True:
            try:
                after = fetch_counters(opts, redact)
                ok, delta = judge_counters(before, after)
            except OSError as exc:
                delta = {"error": redact.scrub(exc)}
            if ok or time.monotonic() >= stop:
                break
            time.sleep(2.0)
        facts["counters_after"] = after
        checks.append(Check("hub_metrics", ok, "deltas %s" % json.dumps(delta, sort_keys=True), deltas=delta))
    if opts.public_state_url:
        ps = facts.get("public_state") or {"polls": 0, "named": False}
        detail = "%s on module %s in %d polls" % (("named " + opts.callsign) if ps["named"] else "did not name " + opts.callsign, opts.module, ps["polls"])
        if ps.get("errors"):
            detail += "; errors %s" % ps["errors"][:2]
        if ps.get("hit"):
            detail += "; protocol %s" % ps["hit"].get("protocol")
        checks.append(Check("public_state", bool(ps["named"]), detail, polls=ps["polls"]))
    return checks, facts


# ---------------------------------------------------------------- the run

def summary_lines(checks, skipped):
    rows = ["%-17s %-5s %s" % (c.name, "PASS" if c.passed else "FAIL", c.detail) for c in checks]
    for name, why in skipped:
        rows.append("%-17s skip  %s" % (name, why))
    passed = sum(1 for c in checks if c.passed)
    rows.append("result: %d passed, %d failed, %d skipped" % (passed, len(checks) - passed, len(skipped)))
    return rows


def execute(opts, out=None, err=None):
    """Run the check; returns (document, exit code). The secret never leaves this function except into the IAX2
    digest computation."""
    out, err = out or sys.stdout, err or sys.stderr
    redact = pc.Redactor()
    say = lambda msg: print(redact.scrub(msg), file=err, flush=True)         # noqa: E731
    secret, source = pc.read_secret(opts.secret_file, ENV_SECRET, opts.environ, "IAX2 account secret")
    redact.hide(secret)
    out_dir = opts.out or os.path.join("out", "prod-iax-" + pc.utc_stamp())
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()
    say("prod_iax: host %s:%d, account %s, module %s, dial %s, secret from %s, out %s" % (
        opts.host, opts.port, opts.account, opts.module, opts.dial, source, out_dir))
    checks, facts = [], {}
    try:
        checks, facts = run_call(opts, secret, redact, started + opts.timeout, say)
    except KeyboardInterrupt:
        say("prod_iax: interrupted; the call was hung up")
        checks.append(Check("interrupted", False, "Ctrl-C"))
    skipped = []
    if not opts.hub_metrics:
        skipped.append(("hub_metrics", "no --hub-metrics URL given: the edge counters were not read"))
    if not opts.public_state_url:
        skipped.append(("public_state", "no --public-state-url given: the public state was not read"))
    for c in checks:
        print(redact.scrub(json.dumps(c.doc())), file=out, flush=True)
    passed = bool(checks) and all(c.passed for c in checks)
    document = {"tool": "dvtest.tools.prod_iax", "started_utc": pc.utc_stamp(), "host": opts.host, "port": opts.port,
                "account": opts.account, "module": opts.module, "dial": opts.dial, "seconds": opts.seconds,
                "checks": [c.doc() for c in checks], "skipped": dict(skipped), "facts": facts, "pass": passed,
                "elapsed_s": round(time.monotonic() - started, 1)}
    final = {"tool": "prod_iax", "summary": True, "pass": passed, "passed": sum(1 for c in checks if c.passed),
             "failed": sum(1 for c in checks if not c.passed), "skipped": [n for n, _w in skipped]}
    print(json.dumps(final), file=out, flush=True)
    text = redact.scrub(json.dumps(document, indent=1))
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    lines = summary_lines(checks, skipped)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(redact.scrub("\n".join(lines)) + "\n")
    say("")
    for line in lines:
        say(line)
    say("prod_iax: wrote %s (results.json, summary.txt); no audio is stored" % out_dir)
    return document, 0 if passed else 1


def main(argv=None):
    try:
        opts = build_options(sys.argv[1:] if argv is None else argv)
        if opts.dry_run:
            plan = make_plan(opts)
            print_plan(opts, plan)
            print(json.dumps({"tool": "prod_iax", "dry_run": True, "plan": plan}))
            return 0
        return execute(opts)[1]
    except UsageError as exc:
        print("prod_iax: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
