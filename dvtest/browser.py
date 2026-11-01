# SPDX-License-Identifier: MIT
"""Pure helpers of the browser cases (cases/test_web_browser.py, cases/browser_fixtures.py,
docs/web-browser-cases.md). Nothing here imports Playwright or touches the network, so it is
unit-tested (cases/unit/test_browser_helpers.py).

  * ENGINES / ENGINE_LABEL: the three Playwright engines and how a result is labelled. The
    WebKit of Playwright is the engine of Safari but it is NOT Safari (no iOS, no Safari
    privacy features, a Linux build): it is called "Playwright WebKit" everywhere.
  * XSS_PAYLOADS: hostile strings for every field the UI renders from stored data. Each one
    tries to set `window.__pwn` or to open a dialog if it ever becomes live markup or script.
  * PageLog: what a page did (console, page errors, requests, responses, dialogs, CSP
    violations) and `PageLog.problems()`, the list of things a normal page view must not do.
  * parse_csp / csp_problems, cookie_problems: header and cookie judgements.
  * artifact_dir / safe_name: where failure artifacts go ($DVT_OUT/web/browser/).
  * Stage 3b (WebRTC listen and talk): goertzel_amplitude / scan_peak (the frequency judgement),
    RTC_PROBE_JS (the same algorithm as plain JavaScript, installed in the page: it taps the received
    MediaStream and reads getStats), tone_wav_bytes (the fake microphone file), launch_options (how an
    engine is started with a fake microphone through `playwright run-server --unsafe`),
    summarize_stats / inbound_verdict (what getStats says about the audio).
  * TALK1: ctl_frames / renewal_verdict (the {"op":"ptt","state":"renewed","code":"ok"} acknowledgements
    of lease renewals the edge sends on the control DataChannel while an over is live, as the probe saw them).
"""
import json
import math
import os
import re
import struct
from dataclasses import dataclass, field
from urllib.parse import urlsplit

ENGINES = ("chromium", "firefox", "webkit")
ENGINE_LABEL = {"chromium": "Playwright Chromium", "firefox": "Playwright Firefox",
                "webkit": "Playwright WebKit"}

SESSION_COOKIE = "__Host-dv_web"           # web.md, "Hostnames and routing"


def label(engine):
    """'Playwright WebKit' for 'webkit' (never 'Safari')."""
    return ENGINE_LABEL[engine]


# ----------------------------------------------------------------- hostile strings

# printable ASCII only and short (a display name is at most 128 bytes, a message text 256), so the
# same strings can go into every field. Each reaches for `window.__pwn` or `alert` when it runs.
XSS_PAYLOADS = (
    "<script>window.__pwn=1;alert(1)</script>",
    "<img src=x onerror=\"window.__pwn=1;alert(1)\">",
    "\"><svg onload=window.__pwn=1;alert(1)>",
    "javascript:window.__pwn=1;alert(1)",
    "<a href=\"javascript:window.__pwn=1\">click</a>",
    "${window.__pwn=1}{{constructor.constructor('window.__pwn=1')()}}",
    "' onfocus='window.__pwn=1' autofocus x='",
    "`;window.__pwn=1;//</script><script>window.__pwn=1</script>",
)

# markup that must never exist as a live element after a payload was shown as text
LIVE_MARKUP_SELECTOR = ("img[src='x'], img[onerror], svg[onload], [onfocus], "
                        "a[href^='javascript:'], script:not([src])")


def printable_ascii(text):
    return all(0x20 <= ord(c) <= 0x7E for c in text)


# ------------------------------------------------------------------- page log

@dataclass
class PageLog:
    """Everything a page did while a case drove it. The fixture fills the lists from
    Playwright events; `problems()` judges them."""
    origin: str = ""
    console: list = field(default_factory=list)       # {type, text, url}
    errors: list = field(default_factory=list)        # uncaught page exceptions (strings)
    requests: list = field(default_factory=list)      # {method, url, path, status, ctype, resource}
    failed: list = field(default_factory=list)        # {method, path, why}
    dialogs: list = field(default_factory=list)       # {type, message}
    csp: list = field(default_factory=list)           # securitypolicyviolation events
    reports: list = field(default_factory=list)      # window.__dvtReport(kind, data) calls

    def same_origin(self, url):
        return bool(self.origin) and url.startswith(self.origin + "/") or url == self.origin

    def paths(self, prefix="", method=None):
        """Request paths (same origin, no query) that start with `prefix`."""
        out = []
        for r in self.requests:
            if method and r["method"] != method:
                continue
            if r["path"].startswith(prefix):
                out.append(r["path"])
        return out

    def statuses(self, path):
        return [r["status"] for r in self.requests if r["path"] == path and r["status"] is not None]

    def pwned(self):
        return [r for r in self.reports if r[0] == "xss"]

    def problems(self, expected=()):
        """What a normal view of a page must not do, as a list of strings (empty = clean).
        `expected` is a collection of (path, status) answers the page legitimately gets (for
        example the signed-out dashboard asks /api/v1/auth/session and gets 401); the console
        line a browser writes for such an answer is tolerated too, and only that line."""
        expected = set(expected)
        out = []
        for c in self.console:
            if c["type"] not in ("error", "assert"):
                continue
            if self._is_expected_network_line(c, expected):
                continue
            out.append("console %s: %s" % (c["type"], c["text"][:200]))
        for e in self.errors:
            out.append("page error: %s" % e[:200])
        for r in self.requests:
            st = r["status"]
            if st is None or not self.same_origin(r["url"]):
                continue
            if st >= 400 and (r["path"], st) not in expected:
                out.append("%s %s -> HTTP %d" % (r["method"], r["path"], st))
        for f in self.failed:
            out.append("request failed: %s %s (%s)" % (f["method"], f["path"], f["why"]))
        for d in self.dialogs:
            out.append("dialog opened: %s %r" % (d["type"], d["message"][:80]))
        for v in self.csp:
            out.append("CSP violation: %s blocked %s" % (v.get("directive"), (v.get("blocked") or "")[:80]))
        out.extend("script of a payload ran: %s" % (r[1],) for r in self.pwned())
        return out

    def _is_expected_network_line(self, c, expected):
        """'Failed to load resource: the server responded with a status of 401 ()' for a
        (path, status) the case expects."""
        m = re.match(r"Failed to load resource: the server responded with a status of (\d{3})", c["text"])
        if not m or not c.get("url"):
            return False
        path = urlsplit(c["url"]).path
        return (path, int(m.group(1))) in expected


def record_request(log, method, url, resource="", status=None, ctype=""):
    """Append a request record with the path split off (the query is kept in `url` only)."""
    log.requests.append({"method": method, "url": url, "path": urlsplit(url).path, "status": status,
                         "ctype": ctype, "resource": resource})
    return log.requests[-1]


# ---------------------------------------------------------------------- CSP

def parse_csp(header):
    """{directive: [sources]} of a Content-Security-Policy header value (lower-case names)."""
    out = {}
    for part in (header or "").split(";"):
        words = part.split()
        if words:
            out.setdefault(words[0].lower(), words[1:])
    return out


def csp_problems(header):
    """Weaknesses of a CSP for a page that renders stored data: empty list = restrictive.
    Judged: a default or script source of 'self' only, no unsafe-inline / unsafe-eval / data:
    / wildcard scripts, framing denied, no <base> or form hijack."""
    if not header:
        return ["no Content-Security-Policy header"]
    csp = parse_csp(header)
    out = []
    script = csp.get("script-src") or csp.get("default-src")
    if script is None:
        out.append("neither script-src nor default-src")
    else:
        for bad in ("'unsafe-inline'", "'unsafe-eval'", "*", "data:", "http:", "https:"):
            if bad in script:
                out.append("script source %s allowed" % bad)
    if "default-src" in csp and csp["default-src"] != ["'self'"]:
        out.append("default-src is %s, not 'self'" % " ".join(csp["default-src"]))
    if csp.get("frame-ancestors") != ["'none'"]:
        out.append("frame-ancestors is %s, not 'none'" % (" ".join(csp.get("frame-ancestors", [])) or "missing"))
    return out


# ------------------------------------------------------------------ cookies

def cookie_problems(cookie, name=SESSION_COOKIE):
    """Problems of a cookie as Playwright reports it (dict with name, secure, httpOnly,
    sameSite, path, domain): the documented session cookie is __Host-dv_web with Path=/,
    Secure, HttpOnly, SameSite=Strict and no Domain attribute (a host-only cookie)."""
    out = []
    if cookie.get("name") != name:
        out.append("name is %r, documented %r" % (cookie.get("name"), name))
    if not cookie.get("secure"):
        out.append("not Secure")
    if not cookie.get("httpOnly"):
        out.append("not HttpOnly (a script can read it)")
    if str(cookie.get("sameSite", "")).lower() != "strict":
        out.append("SameSite is %r, documented Strict" % cookie.get("sameSite"))
    if cookie.get("path") != "/":
        out.append("Path is %r, documented /" % cookie.get("path"))
    if cookie.get("domain", "").startswith("."):
        out.append("Domain attribute set (%r): not host-only" % cookie.get("domain"))
    return out


# ---------------------------------------------------------------- artifacts

def safe_name(text):
    """A file name part: letters, digits, dot, dash, underscore."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:120] or "case"


def artifact_dir(out_dir, engine, test_name):
    """$DVT_OUT/web/browser/<engine>-<test>/ (never committed: out/ is ignored)."""
    return os.path.join(out_dir, "web", "browser", "%s-%s" % (engine, safe_name(test_name)))


# ============================================================ stage 3b: audio in the page

# Frequency judgement. The page taps the MediaStream it receives (a ScriptProcessorNode on an
# AudioContext: an AudioWorklet module would have to come from a blob: URL, which the page's CSP
# default-src 'self' forbids) and keeps the last seconds of float samples. Both the page (JavaScript,
# RTC_PROBE_JS) and the cases (Python, here) use the SAME algorithm: a Hann window and the Goertzel
# filter on a frequency grid; the amplitude of a bin is 2*sqrt(P)/sum(window), so a sine of amplitude A
# reads A. The unit tests run both on the same synthetic data (cases/unit/test_browser_audio.py).

def hann(n):
    """Hann window of n points (periodic form: w[i] = 0.5 - 0.5 cos(2 pi i / n))."""
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / n) for i in range(n)]


def goertzel_amplitude(samples, rate, freq, window=None):
    """Amplitude of the sinusoid at `freq` Hz in `samples` (floats), Hann-windowed by default."""
    n = len(samples)
    if n == 0:
        return 0.0
    win = hann(n) if window is None else window
    w = 2.0 * math.pi * freq / rate
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for i in range(n):
        s0 = samples[i] * win[i] + coeff * s1 - s2
        s2 = s1
        s1 = s0
    power = max(s1 * s1 + s2 * s2 - coeff * s1 * s2, 0.0)
    return 2.0 * math.sqrt(power) / sum(win)


def rms(samples):
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def scan_peak(samples, rate, lo=200.0, hi=3600.0, step=10.0):
    """(peak_hz, peak_amplitude, tone_fraction, rms): the strongest bin of a Goertzel scan from lo to hi
    in `step` Hz; tone_fraction = (peak_amplitude^2 / 2) / rms^2, about 1 for a pure tone and near 0 for
    noise. Silence (rms 0) gives (0, 0, 0, 0)."""
    n = len(samples)
    level = rms(samples)
    if n == 0 or level == 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    win = hann(n)
    best_f, best_a = lo, -1.0
    f = lo
    while f <= hi + 1e-9:
        a = goertzel_amplitude(samples, rate, f, win)
        if a > best_a:
            best_f, best_a = f, a
        f += step
    return (best_f, best_a, (best_a * best_a / 2.0) / (level * level), level)


def tone_wav_bytes(freq=700.0, seconds=30.0, rate=48000, amp=0.5):
    """A mono 16-bit PCM WAV of one sine, for the fake microphone of Chromium
    (--use-file-for-fake-audio-capture). Generated at run time, never committed."""
    n = int(rate * seconds)
    pcm = struct.pack("<%dh" % n, *[int(round(32767 * amp * math.sin(2.0 * math.pi * freq * i / rate))) for i in range(n)])
    head = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return head + b"data" + struct.pack("<I", len(pcm)) + pcm


# Engine start options for a fake microphone, sent as the `launch-options` query of the run-server URL
# (the browser service runs with --unsafe, rig/compose.yml). Chromium plays the given file in a loop;
# Firefox has only its built-in fake source (a beep of about 1 kHz); Playwright WebKit has no flag here
# (permissions are granted per context, whether it offers a fake device is recorded by the cases).
CHROMIUM_FAKE_ARGS = ("--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required")
FIREFOX_FAKE_PREFS = {"media.navigator.streams.fake": True, "media.navigator.permission.disabled": True,
                      "media.autoplay.default": 0, "media.autoplay.blocking_policy": 0,
                      "media.autoplay.block-webaudio": False,
                      # no sound card in the container: a null audio context keeps media clocks running (no OnMediaSinkAudioError)
                      "media.cubeb.force_null_context": True}
MIC_FILE_IN_BROWSER = "/media/fake-mic.wav"      # out/<run>/media mounted read-only at /media


def launch_options(engine, mic_path=MIC_FILE_IN_BROWSER):
    """The launch options dict for one engine ({} for WebKit)."""
    if engine == "chromium":
        return {"args": list(CHROMIUM_FAKE_ARGS) + ["--use-file-for-fake-audio-capture=" + mic_path]}
    if engine == "firefox":
        return {"firefoxUserPrefs": dict(FIREFOX_FAKE_PREFS)}
    return {}


def with_launch_options(ws_url, options):
    """ws://host:3000/ + {"args": [...]} -> ws://host:3000/?launch-options=<json> (unchanged when empty)."""
    if not options:
        return ws_url
    from urllib.parse import quote
    sep = "&" if "?" in ws_url else "?"
    return ws_url + sep + "launch-options=" + quote(json.dumps(options, separators=(",", ":")), safe="")


# What getStats says (a list of dicts: [...report.values()]).
def summarize_stats(entries):
    """{'inbound': {...}, 'outbound': {...}, 'codec_in': mime, 'codec_out': mime, 'pair': {...}} of the
    audio parts of an RTCStatsReport; absent parts are None."""
    by_id = {e.get("id"): e for e in entries}
    out = {"inbound": None, "outbound": None, "codec_in": None, "codec_out": None, "pair": None}
    for e in entries:
        kind = e.get("kind") or e.get("mediaType")
        if e.get("type") == "inbound-rtp" and kind == "audio":
            out["inbound"] = e
            out["codec_in"] = (by_id.get(e.get("codecId")) or {}).get("mimeType")
        elif e.get("type") == "outbound-rtp" and kind == "audio":
            out["outbound"] = e
            out["codec_out"] = (by_id.get(e.get("codecId")) or {}).get("mimeType")
        elif e.get("type") == "candidate-pair" and (e.get("nominated") or e.get("state") == "succeeded"):
            if out["pair"] is None or e.get("nominated"):
                out["pair"] = e
    return out


def selected_pair(entries):
    """The candidate pair in use of a getStats list: {'local': type, 'remote': type, 'protocol': udp|tcp,
    'relay_protocol': ..., 'state': ..., 'via': 'transport'|'nominated'} or None. Prefers the pair the
    transport names (selectedCandidatePairId), else a nominated succeeded pair, else a succeeded one."""
    by_id = {e.get("id"): e for e in entries}
    pair = None
    via = None
    for e in entries:
        if e.get("type") == "transport" and by_id.get(e.get("selectedCandidatePairId")):
            pair, via = by_id[e["selectedCandidatePairId"]], "transport"
            break
    if pair is None:
        cands = [e for e in entries if e.get("type") == "candidate-pair" and e.get("state") == "succeeded"]
        nominated = [e for e in cands if e.get("nominated") or e.get("selected")]
        pair = (nominated or cands or [None])[0]
        via = "nominated" if nominated else "succeeded"
    if pair is None:
        return None
    local = by_id.get(pair.get("localCandidateId")) or {}
    remote = by_id.get(pair.get("remoteCandidateId")) or {}
    return {"local": local.get("candidateType"), "remote": remote.get("candidateType"), "protocol": local.get("protocol"),
            "relay_protocol": local.get("relayProtocol"), "state": pair.get("state"), "via": via}


def arrival_deviations(samples, interval_ms=20.0, rtp_step=160):
    """Per-packet arrival timing of the received stream from RTC_PROBE_JS tap.arrivals samples [(t_ms,
    rtpTimestamp, receive_time_ms or None), ...]: one entry per new RTP timestamp seen. Two entries whose RTP
    timestamps differ by exactly one packet (`rtp_step`, 160 for 20 ms of PCMU at 8 kHz; the 32 bit counter
    wraps) are consecutive packets, and the deviation is |receive time difference - interval_ms|; the engine's own
    receive time is used when every entry has one, else the time the sampler noticed the packet. Returns
    {'n', 'p50', 'p99', 'max', 'resolution_ms', 'source'} (ms) or None when fewer than 20 pairs were seen."""
    if not samples:
        return None
    use_ts = all(s[2] is not None for s in samples)
    devs = []
    for a, b in zip(samples, samples[1:]):
        if (b[1] - a[1]) % (1 << 32) != rtp_step:
            continue
        step = (b[2] - a[2]) if use_ts else (b[0] - a[0])
        devs.append(abs(step - interval_ms))
    if len(devs) < 20:
        return None
    devs.sort()
    return {"n": len(devs), "p50": round(devs[len(devs) // 2], 2), "p99": round(devs[min(len(devs) - 1, int(len(devs) * 0.99))], 2),
            "max": round(devs[-1], 2), "source": "engine receive time" if use_ts else "sample time"}


def inbound_verdict(first, second, interval_ms=20.0, tolerance=0.15):
    """Compare two inbound-rtp snapshots ({'t': ms, 'packetsReceived', 'bytesReceived', 'packetsLost',
    'jitter'} each) taken while the sender talks. Returns (growth dict, problems list): packets and bytes
    grew, loss is under 2 %, the packet rate matches `interval_ms` within `tolerance`, payload per packet
    is constant (bytesReceived / packetsReceived)."""
    problems = []
    dt = (second["t"] - first["t"]) / 1000.0
    dp = second["packetsReceived"] - first["packetsReceived"]
    db = second["bytesReceived"] - first["bytesReceived"]
    dl = max(second.get("packetsLost", 0) - first.get("packetsLost", 0), 0)
    growth = {"seconds": round(dt, 3), "packets": dp, "bytes": db, "lost": dl,
              "packets_per_s": round(dp / dt, 1) if dt > 0 else 0.0,
              "bytes_per_packet": round(db / dp, 1) if dp > 0 else 0.0,
              "interval_ms": round(1000.0 * dt / dp, 2) if dp > 0 else 0.0}
    if dt <= 0 or dp <= 0:
        problems.append("no packets received between the snapshots")
        return growth, problems
    if db <= 0:
        problems.append("bytesReceived did not grow")
    if dl > 0.02 * (dp + dl):
        problems.append("packetsLost grew by %d of %d" % (dl, dp + dl))
    want = 1000.0 / interval_ms
    if abs(growth["packets_per_s"] - want) > tolerance * want:
        problems.append("%.1f packets/s, a %g ms packet interval is %.0f/s" % (growth["packets_per_s"], interval_ms, want))
    return growth, problems


# TALK1 (DV-Router web.md, "PTT"): while an over is live and controller-granted, the edge answers each lease
# renewal that advances the lease with {"op":"ptt","state":"renewed","code":"ok"} on the ctl DataChannel, at most
# one per 500 ms per peer; the page sends a lease every 1000 ms while the button is held and resets its 4 s
# watchdog on each ack. The probe keeps every received frame as {t: performance.now() ms, label, data}.
RENEWED = {"op": "ptt", "state": "renewed", "code": "ok"}
ACK_MIN_INTERVAL_MS = 500.0
ACK_INTERVAL_TOLERANCE_MS = 50.0          # receive-side timer and event-loop jitter of the page: >= 450 ms passes


def ctl_frames(msgs, label="ctl"):
    """[(t_ms, frame dict)] of the JSON frames received on the DataChannel `label` (tap.msgs of RTC_PROBE_JS);
    frames that are not a JSON object are skipped."""
    out = []
    for m in msgs or ():
        if m.get("label") != label:
            continue
        try:
            frame = json.loads(m.get("data") or "")
        except ValueError:
            continue
        if isinstance(frame, dict):
            out.append((float(m.get("t", 0.0)), frame))
    return out


def renewal_verdict(msgs, since_ms=None, until_ms=None, min_interval_ms=ACK_MIN_INTERVAL_MS,
                    tolerance_ms=ACK_INTERVAL_TOLERANCE_MS):
    """What the ctl channel said about lease renewals in [since_ms, until_ms] (page clock). Returns
    {'acks': n, 'intervals_ms': [...], 'min_interval_ms': x or None, 'other_ptt': [state/code, ...],
    'odd_acks': [frames with state 'renewed' that are not exactly RENEWED], 'problems': [...]}. A problem is
    an ack frame that is not exactly {"op":"ptt","state":"renewed","code":"ok"} or two acks closer than
    min_interval_ms - tolerance_ms. Counting enough acks is the caller's judgement."""
    acks, odd, other = [], [], []
    for t, f in ctl_frames(msgs):
        if (since_ms is not None and t < since_ms) or (until_ms is not None and t > until_ms):
            continue
        if f.get("op") != "ptt":
            continue
        if f.get("state") == "renewed":
            (acks if f == RENEWED else odd).append(t)
        else:
            other.append("%s/%s" % (f.get("state"), f.get("code")))
    intervals = [round(b - a, 1) for a, b in zip(acks, acks[1:])]
    problems = []
    if odd:
        problems.append("%d 'renewed' frame(s) not equal to %s" % (len(odd), json.dumps(RENEWED, sort_keys=True)))
    close = [i for i in intervals if i < min_interval_ms - tolerance_ms]
    if close:
        problems.append("acks %s ms apart, documented at most one per %g ms (tolerance %g ms)"
                        % (close[:4], min_interval_ms, tolerance_ms))
    return {"acks": len(acks), "intervals_ms": intervals, "min_interval_ms": min(intervals) if intervals else None,
            "other_ptt": other, "odd_acks": len(odd), "problems": problems}


# The page side. Installed with context.add_init_script before the first navigation. It wraps
# RTCPeerConnection so the page's own peer can be reached (window.__dvtTap), records connection
# state changes with a timestamp (performance.now()), taps the received audio and answers
# getStats. The Goertzel code below is the algorithm of goertzel_amplitude / scan_peak above.
RTC_PROBE_JS = r"""
(() => {
  if (window.__dvtTap) return;
  const Orig = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  const tap = { pcs: [], channels: [], msgs: [], sent: [], events: [], ctx: null, buf: [], have: 0, rate: 0, proc: null, el: null, sources: 0 };
  window.__dvtTap = tap;
  const now = () => performance.now();
  document.addEventListener('click', (e) => tap.events.push({ t: now(), what: 'click',
    target: (e.target && (e.target.id || e.target.textContent || '')).toString().slice(0, 30) }), true);
  document.addEventListener('pointerdown', (e) => tap.events.push({ t: now(), what: 'pointerdown',
    target: (e.target && (e.target.id || '')).toString().slice(0, 30) }), true);
  if (!Orig) { tap.missing = true; return; }
  function Wrapped(...args) {
    const pc = new Orig(...args);
    const n = tap.pcs.length;
    tap.pcs.push(pc);
    // the TURN credential is never recorded: urls only (docs/web-nat-cases.md, "no credential in the page")
    const urlsOf = (cfg) => JSON.stringify(((cfg && cfg.iceServers) || []).map((s) => s.urls));
    tap.events.push({ t: now(), pc: n, what: 'new', config: urlsOf(args[0]) });
    const setConfig = pc.setConfiguration.bind(pc);
    pc.setConfiguration = (cfg) => { tap.events.push({ t: now(), pc: n, what: 'setConfiguration', config: urlsOf(cfg) }); return setConfig(cfg); };
    const setRemote = pc.setRemoteDescription.bind(pc);
    pc.setRemoteDescription = (d) => { tap.events.push({ t: now(), pc: n, what: 'setRemoteDescription', type: d && d.type }); return setRemote(d); };
    for (const ev of ['connectionstatechange', 'iceconnectionstatechange', 'signalingstatechange', 'icegatheringstatechange']) {
      pc.addEventListener(ev, () => tap.events.push({ t: now(), pc: n, what: ev,
        state: ev === 'connectionstatechange' ? pc.connectionState : ev === 'iceconnectionstatechange' ? pc.iceConnectionState
             : ev === 'signalingstatechange' ? pc.signalingState : pc.iceGatheringState }));
    }
    // every frame received (tap.msgs) and sent (tap.sent: the page's own ptt / lease / select frames) on a
    // data channel, with the page clock; frames are short JSON (the page drops anything over 128 bytes)
    const watch = (ch) => {
      tap.channels.push(ch);
      ch.addEventListener('open', () => tap.events.push({ t: now(), pc: n, what: 'channel-open', label: ch.label }));
      ch.addEventListener('close', () => tap.events.push({ t: now(), pc: n, what: 'channel-close', label: ch.label }));
      ch.addEventListener('message', (ev) => tap.msgs.push({ t: now(), pc: n, label: ch.label, data: String(ev.data).slice(0, 200) }));
      const send = ch.send.bind(ch);
      ch.send = (d) => { tap.sent.push({ t: now(), pc: n, label: ch.label, data: String(d).slice(0, 200) }); return send(d); };
      return ch;
    };
    const makeChannel = pc.createDataChannel.bind(pc);
    pc.createDataChannel = (...a) => watch(makeChannel(...a));
    pc.addEventListener('track', (e) => tap.events.push({ t: now(), pc: n, what: 'track', kind: e.track.kind }));
    pc.addEventListener('datachannel', (e) => { tap.events.push({ t: now(), pc: n, what: 'datachannel', label: e.channel.label }); watch(e.channel); });
    return pc;
  }
  Wrapped.prototype = Orig.prototype;
  Object.setPrototypeOf(Wrapped, Orig);
  window.RTCPeerConnection = Wrapped;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Wrapped;

  tap.live = () => tap.pcs.filter((p) => p.connectionState !== 'closed');
  tap.last = () => tap.pcs[tap.pcs.length - 1] || null;
  tap.state = () => { const p = tap.last(); return p ? { connection: p.connectionState, ice: p.iceConnectionState, signaling: p.signalingState } : null; };
  tap.sdp = () => { const p = tap.last(); return p ? { local: p.localDescription && p.localDescription.sdp, remote: p.remoteDescription && p.remoteDescription.sdp } : null; };
  tap.receivers = () => { const p = tap.last(); return p ? p.getReceivers().map((r) => ({ kind: r.track && r.track.kind, state: r.track && r.track.readyState, muted: r.track && r.track.muted })) : []; };
  tap.senders = () => { const p = tap.last(); return p ? p.getSenders().map((r) => ({ kind: r.track && r.track.kind, state: r.track && r.track.readyState })) : []; };
  tap.stats = async () => {
    const p = tap.last();
    if (!p) return [];
    const rep = await p.getStats();
    const out = [];
    rep.forEach((v) => out.push(JSON.parse(JSON.stringify(v))));
    return out;
  };

  // the received audio: a ScriptProcessorNode keeps the last 10 s of samples (mono, context rate)
  tap.start = async () => {
    if (tap.proc) return { rate: tap.rate, reused: true };
    const p = tap.last();
    const track = p && p.getReceivers().map((r) => r.track).find((t) => t && t.kind === 'audio');
    if (!track) return { error: 'no audio receiver' };
    const stream = new MediaStream([track]);
    const AC = window.AudioContext || window.webkitAudioContext;
    tap.ctx = new AC();
    try { await Promise.race([tap.ctx.resume(), new Promise((r) => setTimeout(r, 1500))]); } catch (e) { /* needs a gesture */ }
    tap.rate = tap.ctx.sampleRate;
    // a remote stream only flows into Web Audio in Chromium when a media element has it too
    tap.el = document.createElement('audio');
    tap.el.muted = true;
    tap.el.srcObject = stream;
    // not awaited: play() of a stream without data can stay pending (Firefox)
    try { tap.el.play().catch((e) => { tap.playError = String(e); }); } catch (e) { tap.playError = String(e); }
    const src = tap.ctx.createMediaStreamSource(stream);
    tap.proc = tap.ctx.createScriptProcessor(4096, 1, 1);
    tap.proc.onaudioprocess = (e) => {
      const x = e.inputBuffer.getChannelData(0);
      tap.buf.push(new Float32Array(x));
      tap.have += x.length;
      while (tap.have - tap.buf[0].length >= tap.rate * 10) { tap.have -= tap.buf[0].length; tap.buf.shift(); }
    };
    src.connect(tap.proc);
    tap.proc.connect(tap.ctx.destination);
    return { rate: tap.rate, state: tap.ctx.state };
  };
  // arrival times of the received audio packets: the receiver's synchronization source (RTCRtpReceiver.
  // getSynchronizationSources: the RTP timestamp of the newest packet and its receive time) polled in a
  // MessageChannel loop (no timer clamping) for `ms`. getStats is no use here: Chromium serves it from a cache
  // of about 50 ms. One entry [performance.now(), rtpTimestamp, receive time or null] per new rtpTimestamp.
  tap.arrivals = (ms) => new Promise((resolve) => {
    const out = [];
    const end = now() + ms;
    let last = null;
    const mc = new MessageChannel();
    mc.port1.onmessage = () => {
      const t = now();
      const p = tap.last();
      let seen = false;
      if (p && p.connectionState !== 'closed') {
        for (const r of p.getReceivers()) {
          if (!r.track || r.track.kind !== 'audio' || !r.getSynchronizationSources) continue;
          seen = true;
          const s = r.getSynchronizationSources();
          if (s.length && s[0].rtpTimestamp !== last) {
            last = s[0].rtpTimestamp;
            out.push([t, s[0].rtpTimestamp, s[0].timestamp == null ? null : s[0].timestamp]);
          }
        }
      }
      if (t >= end) resolve(out);
      else if (seen) mc.port2.postMessage(0);
      else setTimeout(() => mc.port2.postMessage(0), 5);
    };
    mc.port2.postMessage(0);
  });
  // a raw frame on the page's own control channel (the page itself only sends well-formed ones)
  tap.sendControl = (text) => {
    const ctl = tap.channels.filter((c) => c.label === 'ctl');
    const ch = ctl[ctl.length - 1] || tap.channels[tap.channels.length - 1];
    if (!ch || ch.readyState !== 'open') return false;
    ch.send(text);
    return true;
  };
  tap.clear = () => { tap.buf = []; tap.have = 0; };
  tap.window = (seconds) => {
    const want = Math.min(Math.floor(seconds * tap.rate), tap.have);
    const out = new Float32Array(want);
    let at = want;
    for (let i = tap.buf.length - 1; i >= 0 && at > 0; i--) {
      const b = tap.buf[i];
      const take = Math.min(b.length, at);
      out.set(b.subarray(b.length - take), at - take);
      at -= take;
    }
    return out;
  };
  const goertzel = (x, win, wsum, rate, f) => {
    const w = 2 * Math.PI * f / rate, c = 2 * Math.cos(w);
    let s1 = 0, s2 = 0;
    for (let i = 0; i < x.length; i++) { const s0 = x[i] * win[i] + c * s1 - s2; s2 = s1; s1 = s0; }
    return 2 * Math.sqrt(Math.max(s1 * s1 + s2 * s2 - c * s1 * s2, 0)) / wsum;
  };
  tap.analyse = (seconds, lo, hi, step) => {
    const x = tap.window(seconds);
    const n = x.length;
    if (!n) return { n: 0, rate: tap.rate, rms: 0, peak_hz: 0, peak_amp: 0, fraction: 0 };
    let e = 0;
    for (let i = 0; i < n; i++) e += x[i] * x[i];
    const level = Math.sqrt(e / n);
    if (level === 0) return { n, rate: tap.rate, rms: 0, peak_hz: 0, peak_amp: 0, fraction: 0 };
    const win = new Float64Array(n);
    let wsum = 0;
    for (let i = 0; i < n; i++) { win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n); wsum += win[i]; }
    let bf = lo, ba = -1;
    for (let f = lo; f <= hi + 1e-9; f += step) {
      const a = goertzel(x, win, wsum, tap.rate, f);
      if (a > ba) { ba = a; bf = f; }
    }
    return { n, rate: tap.rate, rms: level, peak_hz: bf, peak_amp: ba, fraction: (ba * ba / 2) / (level * level) };
  };
  // for the unit tests of the algorithm in node: analyse a given array instead of the tap buffer
  tap.analyseSamples = (arr, rate, lo, hi, step) => {
    tap.rate = rate; tap.buf = [Float32Array.from(arr)]; tap.have = arr.length;
    return tap.analyse(arr.length / rate, lo, hi, step);
  };
})();
"""
