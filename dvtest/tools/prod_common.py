# SPDX-License-Identifier: MIT
"""Pure helpers shared by the production checks prod_iax and prod_talk (docs/prod-checks.md): secret files with
a mode check, a redactor that keeps a secret out of every line, the dominant-frequency measure (stdlib Goertzel),
read-only GETs with certificate verification, and the reading of the public state. No tool here sends anything
by itself and no host, address, callsign or secret is built in.
"""
import datetime
import json
import math
import os
import re
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request

from dvtest import asl_lab
from dvtest.browser import goertzel_amplitude, hann

RATE = 8000
MAX_BODY = 8 * 1024 * 1024


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- time, text

def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")


def check_name(what, value):
    """An account or user name: 1 to 64 printable ASCII characters from A-Z a-z 0-9 . _ / -, no space."""
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise UsageError("%s must be 1 to 64 characters of A-Z a-z 0-9 . _ / - (no spaces)" % what)
    return value


class Redactor:
    """Replaces every registered secret by *** in a text. Values shorter than 4 characters are not registered
    (they would blank ordinary words)."""

    def __init__(self):
        self._values = set()

    def hide(self, *values):
        for v in values:
            if v and len(str(v)) >= 4:
                self._values.add(str(v))

    def scrub(self, text):
        text = str(text)
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, "***")
        return text


# ---------------------------------------------------------------- secrets

def check_secret_file(path):
    """Refuse a secret file that is not a regular file (a symlink is refused too) or that has any group or other
    permission bit (mode 0600 or stricter). Returns the mode. The content is not read here."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise UsageError("secret file %s: %s" % (path, exc.strerror or exc)) from exc
    if stat.S_ISLNK(st.st_mode):
        raise UsageError("secret file %s is a symbolic link (refused)" % path)
    if not stat.S_ISREG(st.st_mode):
        raise UsageError("secret file %s is not a regular file" % path)
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise UsageError("secret file %s has mode %04o: group or other can access it; chmod 600 it (looser modes are "
                         "refused)" % (path, mode))
    return mode


def read_secret(path, env_name, environ=None, what="secret"):
    """The secret from `path` (mode checked, one trailing newline removed) or, with no path, from the environment
    variable `env_name`. Returns (secret, source) where source is "file" or "env"; the value is never part of an
    error message."""
    environ = os.environ if environ is None else environ
    if path:
        check_secret_file(path)
        try:
            with open(path, "rb") as fh:
                raw = fh.read(4096)
        except OSError as exc:
            raise UsageError("secret file %s: %s" % (path, exc.strerror or exc)) from exc
        text = raw.decode("utf-8", "replace")
        if text.endswith("\n"):
            text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
        source = "file"
    else:
        text = environ.get(env_name, "")
        source = "env"
    if not text or "\n" in text:
        raise UsageError("no %s: give a file (mode 0600, one line) or set %s" % (what, env_name))
    return text, source


# ---------------------------------------------------------------- audio measures

_ULAW = [asl_lab.ulaw2lin(b) for b in range(256)]


def ulaw_samples(data):
    """G.711 mu-law bytes as a list of 16 bit integer samples."""
    return [_ULAW[b] for b in bytes(data)]


def rms_of(samples):
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def dominant_frequency(samples, rate=RATE, lo=100.0, hi=3400.0, coarse_s=0.25, fine_s=1.0, span=15.0):
    """(hz, amplitude, tone_fraction, rms) of the strongest sinusoid in `samples` (numbers), by two Goertzel scans
    over the middle of the signal (stdlib only): a coarse one on `coarse_s` seconds in 10 Hz steps from lo to hi
    (a Hann window of that length has a main lobe of 4/coarse_s Hz, wider than the step), then a fine one on
    `fine_s` seconds in 1 Hz steps within +-span Hz of the coarse peak. tone_fraction = (amplitude^2 / 2) / rms^2 of
    the fine window: about 1 for a pure tone, near 0 for noise. Silence or too little signal gives (0, 0, 0, rms)."""
    n = len(samples)
    if n < int(rate * 0.1):
        return (0.0, 0.0, 0.0, rms_of(samples))

    def middle(seconds):
        want = min(n, max(8, int(rate * seconds)))
        start = (n - want) // 2
        return samples[start:start + want]

    coarse = middle(coarse_s)
    level = rms_of(coarse)
    if level == 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    win = hann(len(coarse))
    best_f, best_a = lo, -1.0
    f = lo
    while f <= hi + 1e-9:
        a = goertzel_amplitude(coarse, rate, f, win)
        if a > best_a:
            best_f, best_a = f, a
        f += 10.0
    fine = middle(fine_s)
    fine_level = rms_of(fine)
    win = hann(len(fine))
    top_f, top_a = best_f, -1.0
    f = max(lo, best_f - span)
    while f <= min(hi, best_f + span) + 1e-9:
        a = goertzel_amplitude(fine, rate, f, win)
        if a > top_a:
            top_f, top_a = f, a
        f += 1.0
    frac = (top_a * top_a / 2.0) / (fine_level * fine_level) if fine_level else 0.0
    return (top_f, top_a, frac, fine_level)


# ---------------------------------------------------------------- read-only GETs

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_url(what, value, https_only=False):
    """A URL for a read-only GET: http or https, a host, no credentials. Returns the URL."""
    parts = urllib.parse.urlsplit(value)
    schemes = ("https",) if https_only else ("http", "https")
    if parts.scheme not in schemes or not parts.hostname or parts.username or parts.password:
        raise UsageError("%s must be %s://host[:port]/path without credentials" % (what, "https" if https_only else "http(s)"))
    return value


def url_label(url):
    """scheme://host[:port]/path of a URL without its query or fragment (a token may live there); None for None."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def http_get(url, ca_file=None, timeout=10.0, headers=None):
    """GET `url` (no redirect is followed; an https certificate and host name are always verified, against the
    system roots or only against `ca_file`). Returns (status, body bytes), at most 8 MiB. A transport error
    raises OSError."""
    check_url("url", url)
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, method="GET", headers=dict({"User-Agent": "dvt-prod/1", "Accept": "*/*"}, **(headers or {})))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BODY)
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc


def get_json(url, ca_file=None, timeout=10.0):
    """(status, parsed JSON or None)."""
    status, body = http_get(url, ca_file, timeout, {"Accept": "application/json"})
    try:
        return status, json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return status, None


# ---------------------------------------------------------------- the public state

def callsign_base(text):
    """'N0CALL-T/P' -> 'N0CALL' (the part before the first '-' or '/' or space), upper case; '' for None."""
    return re.split(r"[-/ ]", (text or "").strip().upper(), maxsplit=1)[0]


def same_callsign(a, b):
    """Two callsigns match when they are equal (case-insensitive) or have the same base callsign and do not both
    carry a different suffix ('N0CALL' matches 'N0CALL-T'; 'N0CALL-A' does not match 'N0CALL-B')."""
    a, b = (a or "").strip().upper(), (b or "").strip().upper()
    if not a or not b:
        return False
    if a == b:
        return True
    if callsign_base(a) != callsign_base(b):
        return False
    return not (a != callsign_base(a) and b != callsign_base(b))


def onair(state, room):
    """(callsign, protocol) of the over on air in `room` according to the public state document
    (rooms[].onair, openapi-web.yaml RoomState / OnAir), or (None, None) when the room is idle or absent."""
    rooms = state.get("rooms") if isinstance(state, dict) else None
    for item in rooms if isinstance(rooms, list) else []:
        if isinstance(item, dict) and item.get("room") == room:
            air = item.get("onair")
            if isinstance(air, dict):
                call = air.get("callsign_full") or air.get("callsign") or air.get("callsign_base")
                return (call if isinstance(call, str) else None), air.get("protocol")
    return None, None


def names_callsign(state, room, callsign):
    """True when the public state shows `callsign` (compared by base callsign, case-insensitive) on air in `room`."""
    seen, _proto = onair(state, room)
    return same_callsign(seen, callsign)


class StateWatcher:
    """Polls the public state URL in a thread while a call or an over is running and keeps the first document that
    names `callsign` on air in `room`. Read-only GETs, one every `every` seconds."""

    def __init__(self, url, room, callsign, ca_file=None, every=0.5, timeout=5.0, getter=None):
        import threading
        self.url, self.room, self.callsign, self.ca_file = url, room, callsign, ca_file
        self.every, self.timeout, self.getter = every, timeout, getter or (lambda *a: get_json(*a))
        self.polls = 0
        self.errors = []
        self.hit = None                     # {"callsign", "protocol", "poll"} of the first match
        self.last_onair = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                status, doc = self.getter(self.url, self.ca_file, self.timeout)
                self.polls += 1
                if status != 200 or doc is None:
                    self.errors.append("HTTP %s" % status)
                else:
                    seen, proto = onair(doc, self.room)
                    self.last_onair = {"callsign": seen, "protocol": proto}
                    if self.hit is None and same_callsign(seen, self.callsign):
                        self.hit = {"callsign": seen, "protocol": proto, "poll": self.polls}
            except Exception as exc:        # noqa: BLE001 - reported in the result
                self.errors.append("%s: %s" % (type(exc).__name__, str(exc)[:80]))
            self._stop.wait(self.every)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=self.timeout + 1.0)
        return {"polls": self.polls, "named": self.hit is not None, "hit": self.hit, "last_onair": self.last_onair,
                "errors": self.errors[:5]}
