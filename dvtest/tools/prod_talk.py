# SPDX-License-Identifier: MIT
"""Production check (g): a browser Hold Talk on the echo module of a live DV-Router web service (docs/prod-checks.md).

Headless Chromium (Playwright) with a fake microphone that plays a 700 Hz tone signs in as a check user, presses
Listen on module E, holds Talk for 3 s, releases, and must hear the echo replay in the page's own audio analyser
(the RTC probe of dvtest.browser, the algorithm prod_listen / the browser cases use). Asserted:

  signin             the sign-in answers 200 (no authenticator step: the check user is an ordinary user)
  listen             the Listen card reaches data-state connected and says "Listening on module E"
  talk_button        the Talk button is offered (the user has may_tx on the module)
  on_air             the talk status becomes "On air" (a microphone problem or a denial is named)
  released           after the release the talk status is "Ended: Button released."
  replay_level       a 1 s window of the received audio after the release has an rms above the floor
  replay_frequency   its dominant frequency is 700 Hz within 20 Hz
  public_state       (only with --public-state-url) the public state names the account callsign on air in the
                     module while the button is held
  recorder           (only with --recorder-url) one row of the user's recordings for this over has protocol webrtc

    python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file PATH --dry-run
    python3 -m dvtest.tools.prod_talk --url https://WEB-HOST --user USER --password-file PATH --i-have-owner-approval \\
        --ws-endpoint ws://dvt-prod-browser:3000          # a `playwright run-server` (docs/prod-checks.md)

The Playwright Python client is imported lazily: without --ws-endpoint a local Chromium is launched (needs
`playwright install chromium`), with it the tool connects to the browser image's run-server. The WAV of the fake
microphone is generated at run time into <out>/media (stdlib wave module, never committed) and removed at the end.

Safety rails, all enforced before the first packet:
  * --i-have-owner-approval is required for everything that is not --dry-run or --print-docker;
  * the module is E; any other module needs --room X AND --allow-other-module (the tool never keys another module);
  * the password comes from --password-file (a regular file, mode 0600 or stricter) or DVT_PROD_TALK_PASSWORD and
    is never printed, logged or written (every line and file is scrubbed; no trace, HAR or video is recorded);
  * the certificate and host name of the web service are always verified; --ca-file (the lab CA) is the only
    change, and then the browser (which cannot take a CA from Playwright) ignores certificate errors while this
    tool checks the same host with the CA first;
  * --hold is 3.0 by default, capped at 5.0, floored at 1.5; a global timeout; the page leaves the circuit
    (Disconnect, sign out, context closed) in a finally block;
  * the browser endpoint must be loopback or a single-label container name unless --allow-remote-browser;
  * --dry-run opens no connection and writes no file.

No host, user, callsign or secret is built in. Exit code 0: every check passed (or dry run); 1: a check failed; 2: usage.
"""
import argparse
import json
import os
import re
import shlex
import struct
import sys
import time
import urllib.parse
import wave
from pathlib import Path

from dvtest import browser as brlib
from dvtest import limits
from dvtest.tools import prod_common as pc
from dvtest.tools.prod_common import UsageError

ROOT = Path(__file__).resolve().parents[2]
MODULE = "E"
ENV_PASSWORD = "DVT_PROD_TALK_PASSWORD"
TONE_HZ = 700.0
TONE_TOL_HZ = 20.0
MIN_TONE_FRACTION = 0.1         # getUserMedia leaves echo cancellation and AGC on: a tone is judged loosely (cases/test_web_webrtc.py)
RMS_FLOOR = 0.01                # float samples (-1..1) of the page's analyser; the 700 Hz test tone reads about 0.17
DEFAULT_HOLD_S = 3.0
MAX_HOLD_S = 5.0
MIN_HOLD_S = 1.5
REPLAY_WAIT_S = 9.0
GLOBAL_TIMEOUT_S = 150.0
ON_AIR_WAIT_S = 30.0
RELEASE_WAIT_S = 8.0
RECORDER_WAIT_S = 30.0
MIC_FILE = "fake-mic.wav"
MIC_SECONDS = 10.0
MIC_RATE = 48000
BROWSER_IMAGE = "dvt-browser:local"
TESTER_IMAGE = "dvt-tester:local"
BROWSER_NAME = "dvt-prod-browser"
NETWORK = "dvt-prod"
BROWSER_PORT = 3000
MIC_UNAVAILABLE = "Denied: Microphone access is unavailable"

LOGIN_JS = """async (body) => {
  const r = await fetch('/api/v1/auth/session', {method: 'POST', credentials: 'same-origin',
    headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  return r.status;
}"""
LOGOUT_JS = """async () => {
  const s = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});
  if (s.status !== 200) return s.status;
  const csrf = (await s.json()).csrf;
  const r = await fetch('/api/v1/auth/session', {method: 'DELETE', credentials: 'same-origin',
    headers: {'content-type': 'application/json', 'X-DV-CSRF': csrf}, body: '{}'});
  return r.status;
}"""
GET_JSON_JS = """async (path) => {
  const r = await fetch(path, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return {status: r.status, body: body};
}"""
TAP_START_JS = "window.__dvtTap ? window.__dvtTap.start() : {error: 'no probe'}"
TAP_CLEAR_JS = "window.__dvtTap && window.__dvtTap.clear()"
TAP_ANALYSE_JS = "([s, a, b, c]) => window.__dvtTap.analyse(s, a, b, c)"


# ---------------------------------------------------------------- pure parts

def load_pages():
    """cases/web_pages.py, the one module that knows the selectors (not duplicated here)."""
    cases = str(ROOT / "cases")
    if not (ROOT / "cases" / "web_pages.py").is_file():
        raise ImportError("cases/web_pages.py not found under %s: run from a checkout of the repository (mount it at /dvt "
                          "in the tester image)" % ROOT)
    if cases not in sys.path:
        sys.path.insert(0, cases)
    import web_pages
    return web_pages


def write_mic_wav(path, freq=TONE_HZ, seconds=MIC_SECONDS, rate=MIC_RATE, amp=0.5):
    """The fake microphone: a mono 16 bit WAV of one sine, written with the wave module. Returns the path."""
    import math
    n = int(rate * seconds)
    frames = struct.pack("<%dh" % n, *[int(round(32767 * amp * math.sin(2.0 * math.pi * freq * i / rate))) for i in range(n)])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return str(path)


def judge_audio(windows, floor=RMS_FLOOR, tone=TONE_HZ, tol=TONE_TOL_HZ, min_fraction=MIN_TONE_FRACTION):
    """Judge the analyser windows read after the release ([{t, rms, peak_hz, fraction}], float samples): the best
    window above the floor (highest tone fraction) gives the frequency verdict. Returns {level_ok, freq_ok, best_rms,
    peak_hz, fraction, loud_windows, windows, first_heard_s}."""
    loud = [w for w in windows if w.get("rms", 0.0) >= floor]
    top = max((w.get("rms", 0.0) for w in windows), default=0.0)
    if not loud:
        return {"level_ok": False, "freq_ok": False, "best_rms": round(top, 4), "peak_hz": None, "fraction": None,
                "loud_windows": 0, "windows": len(windows), "first_heard_s": None}
    best = max(loud, key=lambda w: w.get("fraction", 0.0))
    return {"level_ok": True, "freq_ok": abs(best["peak_hz"] - tone) <= tol and best["fraction"] >= min_fraction,
            "best_rms": round(best["rms"], 4), "peak_hz": best["peak_hz"], "fraction": round(best["fraction"], 3),
            "loud_windows": len(loud), "windows": len(windows), "first_heard_s": round(min(w["t"] for w in loud), 2)}


def pick_recorder_row(rows, callsign, since_ms):
    """The recorder rows of this over: callsign matches and the over started no earlier than since_ms."""
    return [r for r in rows if isinstance(r, dict) and pc.same_callsign(r.get("callsign"), callsign)
            and isinstance(r.get("start_ms"), int) and r["start_ms"] >= since_ms]


def browser_endpoint_ok(ws, allow_remote=False):
    """(ok, why): ws:// or wss:// with a host; loopback or a single-label name (a container on a docker network) is
    accepted without --allow-remote-browser."""
    parts = urllib.parse.urlsplit(ws)
    if parts.scheme not in ("ws", "wss") or not parts.hostname or parts.username or parts.password:
        return False, "--ws-endpoint must be ws://host:port (or wss://), without credentials"
    host = parts.hostname
    if allow_remote or host == "localhost" or ("." not in host and ":" not in host and not host.isdigit()):
        return True, ""
    import ipaddress
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True, ""
    except ValueError:
        pass
    return False, "the browser endpoint %s is neither loopback nor a container name on a local docker network; " \
                  "--allow-remote-browser overrides (a run-server started with --unsafe must not be reachable from outside)" % host


def context_options(opts):
    """Playwright new_context() arguments. The certificate is verified (ignore_https_errors False) unless --ca-file
    names the lab CA: the browser cannot be given a CA by Playwright, so this tool verifies the host with that CA
    first (preflight_certificate) and the browser then ignores certificate errors."""
    return {"locale": "en-US", "timezone_id": "UTC", "viewport": {"width": 1280, "height": 800}, "service_workers": "block",
            "permissions": ["microphone"], "ignore_https_errors": bool(opts.ca_file)}


def browser_launch_options(opts, mic_in_browser):
    """The Chromium launch options (the fake microphone flags of dvtest.browser.launch_options) and, for a remote
    run-server, the ws URL carrying them."""
    options = brlib.launch_options("chromium", mic_in_browser)
    ws = opts.ws_endpoint
    if ws and urllib.parse.urlsplit(ws).path == "":
        ws += "/"                                  # ws://host:3000/ as the rig's cases write it
    return options, (brlib.with_launch_options(ws, options) if ws else None)


def docker_commands(opts, env=None, out_dir=None):
    """The docker run lines of the two ways to run this check with the browser image, as lists of words (not run):
    'browser' (the `playwright run-server` service on a private docker network, the fake-microphone directory
    mounted read-only) and 'tool' (the Playwright client in the tester image). Limits come from
    dvtest.limits.docker_run_args (the browser needs more than the default: 3g, pids 1024, like rig/compose.yml)."""
    env = dict(os.environ if env is None else env)
    browser_env = dict(env)
    browser_env.setdefault("DVT_CTR_MEM", "3g")
    browser_env.setdefault("DVT_CTR_PIDS", "1024")
    out = out_dir or "$OUT"
    browser = ["docker", "run", "-d", "--rm", "--name", BROWSER_NAME, "--init", "--network", NETWORK, "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    browser += limits.docker_run_args(browser_env)
    browser += ["--shm-size", "1g", "--tmpfs", "/tmp:rw,size=512m", "--tmpfs", "/var/tmp:rw,size=256m",
                "-v", "%s/media:/media:ro" % out, BROWSER_IMAGE,
                "--host", "0.0.0.0", "--port", str(BROWSER_PORT), "--unsafe"]
    tool = ["docker", "run", "--rm", "--network", NETWORK, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges"]
    tool += limits.docker_run_args(env)
    tool += ["--tmpfs", "/tmp:rw,size=64m", "-e", "HOME=/tmp", "--user", "$(id -u):$(id -g)",
             "-v", "$PWD:/dvt:ro", "-v", "%s:/out" % out]
    pw = os.path.normpath(opts.password_file) if opts.password_file else "PASSWORD-FILE"
    tool += ["-v", "%s:/run/pw:ro" % (pw if os.path.isabs(pw) else "$PWD/" + pw), "-w", "/dvt", TESTER_IMAGE,
             "python3", "-m", "dvtest.tools.prod_talk", "--url", opts.url, "--user", opts.user, "--password-file", "/run/pw",
             "--ws-endpoint", "ws://%s:%d" % (BROWSER_NAME, BROWSER_PORT), "--out", "/out", "--room", opts.room,
             "--hold", "%g" % opts.hold]
    if opts.callsign:
        tool += ["--callsign", opts.callsign]
    if opts.public_state_url:
        tool += ["--public-state-url", opts.public_state_url]
    if opts.recorder_url:
        tool += ["--recorder-url", opts.recorder_url]
    tool += ["--i-have-owner-approval"]
    return {"browser": browser, "tool": tool}


def shell_line(words):
    """The words as one shell line; a word with $PWD, $OUT or $(id ...) stays expandable."""
    return " ".join(('"%s"' % w) if re.search(r"\$(PWD|OUT|\(id)", w) else shlex.quote(w) for w in words)


def docker_script(opts, env=None):
    """The whole sequence as shell lines (printed by --print-docker; never run by this tool)."""
    cmds = docker_commands(opts, env)
    return [
        "# one run directory; the fake microphone goes into $OUT/media (generated by the tool, never committed)",
        'OUT="$PWD/out/prod-talk-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$OUT/media"',
        "docker network create %s" % NETWORK,
        "# the browser service: published to NOBODY (no -p), reachable only from the %s network" % NETWORK,
        shell_line(cmds["browser"]),
        "# the check (the Playwright client of the tester image); stop the browser afterwards",
        shell_line(cmds["tool"]),
        "docker stop %s; docker network rm %s" % (BROWSER_NAME, NETWORK)]


def build_options(argv, environ=None):
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(
        prog="python3 -m dvtest.tools.prod_talk", allow_abbrev=False,
        description="Production check (g): browser Hold Talk on module E with a fake 700 Hz microphone, judge the echo replay.")
    ap.add_argument("--url", required=True, help="https://host[:port] of the web service (certificate verified)")
    ap.add_argument("--user", required=True, help="login callsign of the check user (an argument, never built in)")
    ap.add_argument("--password-file", help="file with the password, one line, mode 0600 (else %s)" % ENV_PASSWORD)
    ap.add_argument("--callsign", help="callsign the public state and the recorder must name (default: --user)")
    ap.add_argument("--room", default=MODULE, help="module (default E; any other needs --allow-other-module)")
    ap.add_argument("--allow-other-module", action="store_true", help="allow a module other than E")
    ap.add_argument("--hold", type=float, default=DEFAULT_HOLD_S,
                    help="seconds to hold Talk (default %g, at most %g, at least %g)" % (DEFAULT_HOLD_S, MAX_HOLD_S, MIN_HOLD_S))
    ap.add_argument("--ws-endpoint", help="ws://HOST:PORT of a `playwright run-server --unsafe` (the browser image); "
                                          "without it a local Chromium is launched")
    ap.add_argument("--allow-remote-browser", action="store_true", help="allow a --ws-endpoint that is not loopback or a container name")
    ap.add_argument("--mic-path", default=brlib.MIC_FILE_IN_BROWSER,
                    help="where the browser sees <out>/media/%s with --ws-endpoint (default %s)" % (MIC_FILE, brlib.MIC_FILE_IN_BROWSER))
    ap.add_argument("--ca-file", help="CA bundle INSTEAD of the system roots (the lab CA only; verification is never off for a public host)")
    ap.add_argument("--public-state-url", help="URL of GET /api/v1/public/state (read-only, polled while the button is held); optional")
    ap.add_argument("--recorder-url", help="URL of GET /api/v1/user/recordings on the same origin (read from the page); optional")
    ap.add_argument("--rms-floor", type=float, default=RMS_FLOOR, help="rms floor of the page's analyser, float samples (default %g)" % RMS_FLOOR)
    ap.add_argument("--replay-wait", type=float, default=REPLAY_WAIT_S, help="seconds to listen for the replay after the release (default %g)" % REPLAY_WAIT_S)
    ap.add_argument("--timeout", type=float, default=GLOBAL_TIMEOUT_S, help="global timeout in seconds (default %g)" % GLOBAL_TIMEOUT_S)
    ap.add_argument("--out", help="directory for results.json, summary.txt and media/ (default out/prod-talk-<utc>/)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no connection, no file")
    ap.add_argument("--print-docker", action="store_true", help="print the docker run lines for the browser image (not run) and exit")
    ap.add_argument("--i-have-owner-approval", action="store_true", help="required for a real run: the owner approved this check")
    ns = ap.parse_args(argv)
    ns.environ = environ
    ns.user = pc.check_name("--user", ns.user)
    if ns.callsign is not None:
        ns.callsign = pc.check_name("--callsign", ns.callsign)
    parts = urllib.parse.urlsplit(ns.url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") or parts.query:
        raise UsageError("--url must be https://host[:port] (https only, no credentials, no path)")
    ns.url = "https://%s" % parts.netloc
    if not re.fullmatch(r"[A-Z]", ns.room):
        raise UsageError("--room must be one capital letter")
    if ns.room != MODULE and not ns.allow_other_module:
        raise UsageError("module %s is not the echo module E; another module needs --allow-other-module as well "
                         "(this tool does not key another module otherwise)" % ns.room)
    if not (ns.hold == ns.hold) or not MIN_HOLD_S <= ns.hold <= MAX_HOLD_S:
        raise UsageError("--hold must be between %g and %g" % (MIN_HOLD_S, MAX_HOLD_S))
    if ns.replay_wait < 5.0 or ns.timeout < 40.0 or not 0 < ns.rms_floor < 1:
        raise UsageError("--replay-wait must be at least 5, --timeout at least 40 and --rms-floor between 0 and 1")
    if ns.ws_endpoint:
        ok, why = browser_endpoint_ok(ns.ws_endpoint, ns.allow_remote_browser)
        if not ok:
            raise UsageError(why)
    if ns.ca_file and not os.path.isfile(ns.ca_file):
        raise UsageError("--ca-file %s is not a file" % ns.ca_file)
    for flag in ("public_state_url", "recorder_url"):
        if getattr(ns, flag):
            pc.check_url("--" + flag.replace("_", "-"), getattr(ns, flag), https_only=True)
    if ns.recorder_url and urllib.parse.urlsplit(ns.recorder_url).netloc != parts.netloc:
        raise UsageError("--recorder-url must be on the same origin as --url (it is read from the signed-in page)")
    if ns.password_file and not ns.print_docker:
        pc.check_secret_file(ns.password_file)               # the mode is checked also in a dry run; the content is not read
    if not ns.dry_run and not ns.print_docker and not ns.i_have_owner_approval:
        raise UsageError("this check keys the echo module of a live reflector: add --i-have-owner-approval once the owner "
                         "approved it (--dry-run shows the plan and sends nothing)")
    return ns


class Check:
    def __init__(self, name, passed, detail, **extra):
        self.name, self.passed, self.detail, self.extra = name, passed, detail, extra

    def doc(self):
        out = {"tool": "prod_talk", "check": self.name, "pass": self.passed, "detail": self.detail}
        out.update(self.extra)
        return out


def summary_lines(checks, skipped):
    rows = ["%-17s %-5s %s" % (c.name, "PASS" if c.passed else "FAIL", c.detail) for c in checks]
    for name, why in skipped:
        rows.append("%-17s skip  %s" % (name, why))
    passed = sum(1 for c in checks if c.passed)
    rows.append("result: %d passed, %d failed, %d skipped" % (passed, len(checks) - passed, len(skipped)))
    return rows


# ---------------------------------------------------------------- plan (dry run)

def make_plan(opts):
    options, ws = browser_launch_options(opts, opts.mic_path if opts.ws_endpoint else "<out>/media/" + MIC_FILE)
    return {
        "url": opts.url, "user": opts.user, "room": opts.room, "hold_s": opts.hold, "tone_hz": TONE_HZ,
        "callsign_expected": opts.callsign or opts.user, "password_source": "file" if opts.password_file else "env " + ENV_PASSWORD,
        "browser": ("connect " + pc.url_label(opts.ws_endpoint)) if opts.ws_endpoint else "local Chromium (playwright install chromium)",
        "chromium_args": options["args"], "certificate": "only the CA in %s (browser ignores certificate errors after the tool verified the host)" % opts.ca_file
        if opts.ca_file else "verified (system roots and host name)",
        "steps": ["generate %s (%g s of %g Hz, %d Hz mono 16 bit) into <out>/media" % (MIC_FILE, MIC_SECONDS, TONE_HZ, MIC_RATE),
                  "open the page, POST /api/v1/auth/session from the page (user %s)" % opts.user,
                  "#/ : choose module %s, Listen, wait for data-state connected" % opts.room,
                  "start the audio tap, press Hold Talk, wait for 'On air', hold %g s" % opts.hold,
                  "release, expect 'Ended: Button released.', clear the tap, read 1 s windows for %g s" % opts.replay_wait,
                  "Disconnect, sign out, close the browser context"],
        "checks": ["signin", "listen", "talk_button", "on_air", "released", "replay_level rms >= %g" % opts.rms_floor,
                   "replay_frequency %g Hz +- %g Hz" % (TONE_HZ, TONE_TOL_HZ)] +
                  (["public_state names %s on module %s" % (opts.callsign or opts.user, opts.room)] if opts.public_state_url else []) +
                  (["recorder row protocol webrtc"] if opts.recorder_url else []),
        "public_state_url": pc.url_label(opts.public_state_url), "recorder_url": pc.url_label(opts.recorder_url)}


def print_plan(opts, plan, out=None):
    out = out or sys.stdout
    w = lambda s="": print(s, file=out)                         # noqa: E731
    w("DRY RUN: no connection is opened, nothing is written, the password is not read")
    w("web %s  user %s  module %s  hold %g s  expected callsign %s" % (plan["url"], plan["user"], plan["room"], plan["hold_s"], plan["callsign_expected"]))
    w("browser: %s" % plan["browser"])
    w("certificate: %s" % plan["certificate"])
    w("password: %s (mode checked, never printed)" % plan["password_source"])
    w("chromium args: %s" % " ".join(plan["chromium_args"]))
    for i, step in enumerate(plan["steps"], 1):
        w("  %d. %s" % (i, step))
    w("checks: " + "; ".join(plan["checks"]))
    w("optional reads: public state %s; recorder %s" % (plan["public_state_url"] or "not given (skipped)", plan["recorder_url"] or "not given (skipped)"))
    w("To run for real: drop --dry-run and add --i-have-owner-approval (docker lines: --print-docker).")


# ---------------------------------------------------------------- the browser flow (needs Playwright)

def preflight_certificate(opts):
    """With --ca-file: one GET of the site with that CA (verified), so the browser's ignore_https_errors is not the
    only thing that looked at the certificate."""
    status, _body = pc.http_get(opts.url + "/", opts.ca_file, timeout=10.0)
    return status


class Session:
    """The page side of one check: small helpers over web_pages (the selectors) and the RTC probe."""

    def __init__(self, page, pages, opts, redact):
        self.page, self.pages, self.opts, self.redact = page, pages, opts, redact

    def listen_state(self):
        return self.pages.listen_state(self.page)

    def listen_text(self):
        return self.pages.listen_status(self.page).inner_text().strip()

    def talk_text(self):
        return self.pages.talk_status(self.page).inner_text().strip()

    def wait_listening(self, timeout_s=25.0):
        """Wait for data-state connected and the status line 'Listening on module X'. Returns (ok, detail)."""
        end = time.monotonic() + timeout_s
        state = text = None
        while time.monotonic() < end:
            state = self.listen_state()
            text = self.listen_text() if state else None
            if state == "connected" and text == "Listening on module %s" % self.opts.room:
                return True, text
            if state == "failed":
                return False, "data-state failed: %s" % text
            time.sleep(0.2)
        return False, "data-state %s, status %r after %.0f s" % (state, text, timeout_s)

    def wait_talk(self, pred, timeout_s):
        end = time.monotonic() + timeout_s
        text = ""
        while time.monotonic() < end:
            text = self.talk_text()
            if pred(text):
                return text
            time.sleep(0.1)
        return text

    def press(self):
        box = self.pages.talk_button(self.page).bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        self.page.mouse.down()

    def release(self):
        self.page.mouse.up()

    def window(self, seconds=1.0):
        return self.page.evaluate(TAP_ANALYSE_JS, [seconds, 200.0, 3600.0, 10.0])


def collect_replay(sess, wait_s, floor, deadline, sleep=time.sleep, clock=time.monotonic):
    """Read 1 s analyser windows after the release for up to wait_s; stop 1 s after the sound ended again (two
    consecutive windows under the floor after at least two above it). Returns [{t, rms, peak_hz, fraction}]."""
    t0 = clock()
    windows = []
    loud_seen = quiet_after = 0
    while clock() - t0 < wait_s and clock() < deadline:
        w = sess.window(1.0)
        rec = {"t": round(clock() - t0, 2), "rms": float(w.get("rms", 0.0)), "peak_hz": float(w.get("peak_hz", 0.0)),
               "fraction": float(w.get("fraction", 0.0))}
        windows.append(rec)
        if rec["rms"] >= floor:
            loud_seen += 1
            quiet_after = 0
        elif loud_seen >= 2:
            quiet_after += 1
            if quiet_after >= 2:
                break
        sleep(0.4)
    return windows


def run_browser(opts, password, redact, deadline, say, mic_local, sync_playwright=None):
    """The whole browser flow; returns (checks, facts). `sync_playwright` is injectable for tests."""
    pages = load_pages()
    if sync_playwright is None:
        from playwright.sync_api import sync_playwright
    checks, facts = [], {"console_errors": []}
    options, ws_url = browser_launch_options(opts, opts.mic_path if opts.ws_endpoint else mic_local)
    watcher = None
    pw = sync_playwright().start()
    browser = context = page = None
    signed_in = False
    sess = None
    t_press_ms = None
    try:
        if opts.ca_file:
            facts["preflight_status"] = preflight_certificate(opts)
        say("prod_talk: %s" % ("connecting to " + pc.url_label(opts.ws_endpoint) if opts.ws_endpoint else "launching a local Chromium"))
        if opts.ws_endpoint:
            browser = pw.chromium.connect(ws_url, timeout=60000)
        else:
            browser = pw.chromium.launch(headless=True, **options)
        facts["browser"] = "Chromium %s" % getattr(browser, "version", "?")
        context = browser.new_context(**context_options(opts))
        context.set_default_timeout(20000)
        context.set_default_navigation_timeout(30000)
        context.add_init_script(brlib.RTC_PROBE_JS)
        page = context.new_page()
        page.on("console", lambda m: facts["console_errors"].append(redact.scrub(m.text)[:160]) if m.type == "error" and len(facts["console_errors"]) < 10 else None)
        page.on("pageerror", lambda e: facts["console_errors"].append(redact.scrub("pageerror: %s" % e)[:160]) if len(facts["console_errors"]) < 10 else None)
        sess = Session(page, pages, opts, redact)

        # -- sign in, from inside the page (the session is bound to the browser's User-Agent)
        page.goto(opts.url + pages.LOGIN)
        status = page.evaluate(LOGIN_JS, {"callsign": opts.user, "password": password})
        signed_in = status == 200
        checks.append(Check("signin", signed_in, "HTTP %s" % status if signed_in else
                            "HTTP %s from POST /api/v1/auth/session (the check user must be an ordinary user without an authenticator step)" % status))
        if not signed_in:
            return checks, facts

        # -- listen
        page.goto(opts.url + pages.dashboard_url("/"))
        pages.listen_panel(page).wait_for(state="visible", timeout=30000)
        pages.listen_room_select(page).select_option(opts.room)
        pages.listen_button(page).click()
        ok, detail = sess.wait_listening()
        checks.append(Check("listen", ok, detail))
        if not ok:
            return checks, facts
        talk = pages.talk_button(page)
        has_talk = talk.count() > 0
        checks.append(Check("talk_button", has_talk and talk.is_enabled(), "Hold Talk offered and enabled" if has_talk else
                            "no Talk button: the user needs may_tx on module %s" % opts.room))
        if not has_talk:
            return checks, facts
        tap = page.evaluate(TAP_START_JS) or {}
        facts["audio_tap"] = {k: tap.get(k) for k in ("rate", "state", "error")}
        if tap.get("error"):
            checks.append(Check("replay_level", False, "audio tap unavailable: %s" % tap["error"]))
            return checks, facts

        # -- talk
        if opts.public_state_url:
            watcher = pc.StateWatcher(opts.public_state_url, opts.room, opts.callsign or opts.user, opts.ca_file).start()
        t_press_ms = int(time.time() * 1000)
        sess.press()
        text = sess.wait_talk(lambda s: s == pages.TALK_ON_AIR_TEXT or s.startswith(("Denied", "Ended")), ON_AIR_WAIT_S)
        on_air = text == pages.TALK_ON_AIR_TEXT
        hint = ""
        if text.startswith(MIC_UNAVAILABLE):
            hint = " (the fake microphone is not available: start Chromium with the flags of dvtest.browser.launch_options)"
        checks.append(Check("on_air", on_air, ("On air" if on_air else "talk status %r%s" % (text, hint))))
        if not on_air:
            sess.release()
            return checks, facts
        t_air = time.monotonic()
        while time.monotonic() - t_air < opts.hold:
            time.sleep(0.25)
        facts["heard_during_hold"] = {k: v for k, v in (sess.window(1.0) or {}).items() if k in ("rms", "peak_hz", "fraction")}
        sess.release()
        t_release = time.monotonic()
        facts["on_air_s"] = round(t_release - t_air, 2)
        ended = sess.wait_talk(lambda s: s != pages.TALK_ON_AIR_TEXT, RELEASE_WAIT_S)
        checks.append(Check("released", ended == pages.TALK_RELEASED_TEXT, "talk status %r after the release" % ended))
        page.evaluate(TAP_CLEAR_JS)
        if watcher is not None:
            facts["public_state"] = watcher.stop()
            watcher = None

        # -- the replay
        windows = collect_replay(sess, opts.replay_wait, opts.rms_floor, deadline)
        verdict = judge_audio(windows, opts.rms_floor)
        facts["replay"] = dict(verdict, rate=facts["audio_tap"].get("rate"))
        checks.append(Check("replay_level", verdict["level_ok"], "best window rms %.4f (floor %g), heard %s s after the release, %d loud of %d windows" % (
            verdict["best_rms"], opts.rms_floor, verdict["first_heard_s"], verdict["loud_windows"], verdict["windows"]),
            rms=verdict["best_rms"], first_heard_s=verdict["first_heard_s"]))
        checks.append(Check("replay_frequency", verdict["freq_ok"], "dominant %s Hz (want %g +- %g), tone fraction %s" % (
            verdict["peak_hz"], TONE_HZ, TONE_TOL_HZ, verdict["fraction"]), hz=verdict["peak_hz"], fraction=verdict["fraction"]))

        # -- the recorder (optional, read from the signed-in page)
        if opts.recorder_url:
            checks.append(recorder_check(sess, opts, t_press_ms, deadline, redact))
    except Exception as exc:                        # noqa: BLE001 - reported as a failed check
        checks.append(Check("error", False, redact.scrub("%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:200] if str(exc) else ""))))
    finally:
        if watcher is not None:
            facts["public_state"] = watcher.stop()
        failed = any(not c.passed for c in checks)
        if page is not None and failed:
            try:
                shot = Path(opts.out_dir) / "failure.png"
                page.screenshot(path=str(shot))
                facts["screenshot"] = shot.name
            except Exception:                       # noqa: BLE001
                pass
        for step in (lambda: sess and signed_in and pages.listen_state(page) in ("connected", "connecting") and pages.listen_button(page).click(),
                     lambda: signed_in and page.evaluate(LOGOUT_JS),
                     lambda: context and context.close(), lambda: browser and browser.close(), lambda: pw.stop()):
            try:
                step()
            except Exception:                       # noqa: BLE001 - cleanup is best effort
                pass
    if opts.public_state_url:
        ps = facts.get("public_state") or {"polls": 0, "named": False}
        detail = "%s on module %s in %d polls" % ("named " + (opts.callsign or opts.user) if ps["named"] else "did not name " + (opts.callsign or opts.user),
                                                  opts.room, ps["polls"])
        if ps.get("hit"):
            detail += "; protocol %s" % ps["hit"].get("protocol")
        if ps.get("errors"):
            detail += "; errors %s" % ps["errors"][:2]
        checks.append(Check("public_state", bool(ps["named"]), detail, polls=ps["polls"]))
    return checks, facts


def recorder_check(sess, opts, since_ms, deadline, redact):
    """Poll the user's recordings (the page's own session, 2 requests per second allowed) until the row of this
    over is listed: protocol webrtc. The row is found by callsign and start time."""
    parts = urllib.parse.urlsplit(opts.recorder_url)
    who = opts.callsign or opts.user
    day = 86400000
    query = urllib.parse.urlencode({"room": opts.room, "callsign": who, "date_from_ms": since_ms - day, "date_to_ms": since_ms + day, "limit": 100})
    path = "%s?%s" % (parts.path, query)
    end = min(time.monotonic() + RECORDER_WAIT_S, deadline)
    last = "no answer"
    while time.monotonic() < end:
        try:
            res = sess.page.evaluate(GET_JSON_JS, path)
        except Exception as exc:                    # noqa: BLE001
            last = redact.scrub("%s" % exc)[:80]
            res = None
        if res:
            if res.get("status") != 200:
                last = "HTTP %s" % res.get("status")
            else:
                rows = pick_recorder_row((res.get("body") or {}).get("items") or [], who, since_ms - 2000)
                if rows:
                    row = rows[-1]
                    return Check("recorder", row.get("protocol") == "webrtc", "row protocol %r, reason %r, %s ms" % (
                        row.get("protocol"), row.get("reason"), row.get("duration_ms")), protocol=row.get("protocol"), rows=len(rows))
                last = "no row for %s since the press" % who
        time.sleep(2.0)
    return Check("recorder", False, "no recorder row for this over within %.0f s (%s); is recording enabled?" % (RECORDER_WAIT_S, last))


# ---------------------------------------------------------------- the run

def execute(opts, out=None, err=None, sync_playwright=None):
    """Run the check; returns (document, exit code). The password is read here and goes only into the page."""
    out, err = out or sys.stdout, err or sys.stderr
    redact = pc.Redactor()
    say = lambda msg: print(redact.scrub(msg), file=err, flush=True)         # noqa: E731
    password, source = pc.read_secret(opts.password_file, ENV_PASSWORD, opts.environ, "password of the check user")
    redact.hide(password)
    out_dir = Path(opts.out or os.path.join("out", "prod-talk-" + pc.utc_stamp()))
    out_dir.mkdir(parents=True, exist_ok=True)
    opts.out_dir = str(out_dir)
    mic = write_mic_wav((out_dir / "media" / MIC_FILE).resolve())
    started = time.monotonic()
    say("prod_talk: %s as %s, module %s, hold %g s, password from %s, out %s" % (opts.url, opts.user, opts.room, opts.hold, source, out_dir))
    checks, facts = [], {}
    try:
        checks, facts = run_browser(opts, password, redact, started + opts.timeout, say, mic, sync_playwright)
    except ImportError as exc:
        checks.append(Check("error", False, redact.scrub("Playwright is not importable here (%s): use --ws-endpoint from the tester image or "
                                                         "pip install playwright; see docs/prod-checks.md" % exc)))
    except KeyboardInterrupt:
        say("prod_talk: interrupted")
        checks.append(Check("interrupted", False, "Ctrl-C"))
    finally:
        try:
            os.unlink(mic)
        except OSError:
            pass
    skipped = []
    if not opts.public_state_url:
        skipped.append(("public_state", "no --public-state-url given: the public state was not read"))
    if not opts.recorder_url:
        skipped.append(("recorder", "no --recorder-url given: the recorder row was not read"))
    for c in checks:
        print(redact.scrub(json.dumps(c.doc())), file=out, flush=True)
    passed = bool(checks) and all(c.passed for c in checks)
    document = {"tool": "dvtest.tools.prod_talk", "started_utc": pc.utc_stamp(), "url": opts.url, "user": opts.user,
                "room": opts.room, "hold_s": opts.hold, "checks": [c.doc() for c in checks], "skipped": dict(skipped),
                "facts": facts, "pass": passed, "elapsed_s": round(time.monotonic() - started, 1)}
    final = {"tool": "prod_talk", "summary": True, "pass": passed, "passed": sum(1 for c in checks if c.passed),
             "failed": sum(1 for c in checks if not c.passed), "skipped": [n for n, _w in skipped]}
    print(json.dumps(final), file=out, flush=True)
    with open(out_dir / "results.json", "w", encoding="utf-8") as fh:
        fh.write(redact.scrub(json.dumps(document, indent=1)) + "\n")
    lines = summary_lines(checks, skipped)
    with open(out_dir / "summary.txt", "w", encoding="utf-8") as fh:
        fh.write(redact.scrub("\n".join(lines)) + "\n")
    say("")
    for line in lines:
        say(line)
    say("prod_talk: wrote %s (results.json, summary.txt); the microphone file was removed" % out_dir)
    return document, 0 if passed else 1


def main(argv=None):
    try:
        opts = build_options(sys.argv[1:] if argv is None else argv)
        if opts.print_docker:
            print("\n".join(docker_script(opts)))
            return 0
        if opts.dry_run:
            plan = make_plan(opts)
            print_plan(opts, plan)
            print(json.dumps({"tool": "prod_talk", "dry_run": True, "plan": plan}))
            return 0
        return execute(opts)[1]
    except UsageError as exc:
        print("prod_talk: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
