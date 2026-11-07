# SPDX-License-Identifier: MIT
"""dvtest/tools/prod_talk.py (production check g). Playwright is not part of the unit environment, so the browser
flow runs against a small scripted fake of the Playwright sync API (pages, locators, mouse, evaluate); the real
Chromium, the real page and the real WebRTC audio are NOT exercised here (docs/prod-checks.md, "Not covered
offline"). Tested for real: argument rules, safety rails, the file-mode check, the WAV of the fake microphone, the
audio verdict, the docker lines, the dry run, the recorder-row pick, the result JSON and that the password appears
in no output and no file."""
import io
import json
import os
import socket
import time
import wave

import pytest

from dvtest.tools import prod_common as pc
from dvtest.tools import prod_talk as pt

PASSWORD = "pw-check-user-5521"
USER = "N0CHECK"
URL = "https://web.example"
CA_FILE = os.path.abspath(__file__)          # any existing file will do for the option rules


@pytest.fixture
def pw_file(tmp_path):
    path = tmp_path / "talk.pw"
    path.write_text(PASSWORD + "\n")
    path.chmod(0o600)
    return str(path)


def parse(*extra, pw=None, environ=None, raw=False):
    """Options; --dry-run is added unless the case gives --i-have-owner-approval or --print-docker (or raw)."""
    extra = list(extra)
    if not raw and not {"--i-have-owner-approval", "--print-docker", "--dry-run"} & set(extra):
        extra.append("--dry-run")
    argv = ["--url", URL, "--user", USER] + (["--password-file", pw] if pw else []) + extra
    return pt.build_options(argv, environ={} if environ is None else environ)


def usage(*extra, pw=None, raw=False):
    with pytest.raises(pc.UsageError) as exc:
        parse(*extra, pw=pw, raw=raw)
    return str(exc.value)


# ---------------------------------------------------------------- a scripted fake of Playwright's sync API

class Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y):
        pass

    def down(self):
        self.page.talk = self.page.on_press

    def up(self):
        self.page.talk = pt.load_pages().TALK_RELEASED_TEXT
        self.page.released_at = time.monotonic()


class Loc:
    def __init__(self, page, key):
        self.page, self.key = page, key

    @property
    def first(self):
        return self

    def locator(self, css):
        return Loc(self.page, self.key + "|" + css)

    def count(self):
        if self.key == "talk-ptt":
            return 0 if self.page.no_talk else 1
        return 1

    def is_enabled(self):
        return True

    def wait_for(self, **kw):
        return None

    def click(self):
        if self.key == "listen-toggle":
            self.page.listen = "connected" if self.page.listen != "connected" else "closed"

    def select_option(self, value):
        self.page.room = value

    def bounding_box(self):
        return {"x": 10, "y": 10, "width": 100, "height": 30}

    def get_attribute(self, name):
        return self.page.listen if (self.key == "listen-connection" and name == "data-state") else None

    def inner_text(self):
        if self.key == "#talk-status":
            return self.page.talk
        if self.key.startswith("listen-connection|"):
            return "Listening on module %s" % self.page.room if self.page.listen == "connected" else "Idle"
        return ""


class Page:
    def __init__(self, ctx):
        self.ctx = ctx
        self.talk, self.on_press = "Ready", "On air"
        self.listen, self.room = "idle", None
        self.no_talk = False
        self.login_status = 200
        self.released_at = None
        self.tone_hz, self.tone_rms = 700.0, 0.17
        self.rows = []
        self.logins, self.gotos, self.logged_out = [], [], False
        self.mouse = Mouse(self)

    def on(self, event, fn):
        pass

    def goto(self, url, **kw):
        self.gotos.append(url)

    def get_by_test_id(self, tid):
        return Loc(self, tid)

    def locator(self, css):
        return Loc(self, css)

    def screenshot(self, path=None, **kw):
        open(path, "wb").write(b"png")

    def evaluate(self, js, arg=None):
        if js == pt.LOGIN_JS:
            self.logins.append(arg)
            return self.login_status
        if js == pt.LOGOUT_JS:
            self.logged_out = True
            return 200
        if js == pt.TAP_START_JS:
            return {"rate": 48000, "state": "running"}
        if js == pt.TAP_CLEAR_JS:
            return None
        if js == pt.TAP_ANALYSE_JS:
            return self.analyse()
        if js == pt.GET_JSON_JS:
            return {"status": 200, "body": {"items": self.rows}}
        raise AssertionError("unexpected page.evaluate: %s" % js[:60])

    def analyse(self):
        quiet = {"n": 48000, "rate": 48000, "rms": 0.0, "peak_hz": 0, "peak_amp": 0, "fraction": 0}
        if self.released_at is None:
            return dict(quiet, rms=0.002)                      # the live over is not relayed back to the sender
        age = time.monotonic() - self.released_at
        if 0.3 <= age <= 1.8:
            return {"n": 48000, "rate": 48000, "rms": self.tone_rms, "peak_hz": self.tone_hz, "peak_amp": 0.24, "fraction": 0.8}
        return quiet


class Context:
    def __init__(self, browser, kwargs):
        self.browser, self.kwargs, self.closed, self.scripts = browser, kwargs, False, []
        self.page = None

    def set_default_timeout(self, ms):
        pass

    set_default_navigation_timeout = set_default_timeout

    def add_init_script(self, script):
        self.scripts.append(script)

    def new_page(self):
        self.page = Page(self)
        self.browser.fake.configure(self.page)
        return self.page

    def close(self):
        self.closed = True


class Browser:
    version = "999.0"

    def __init__(self, fake):
        self.fake, self.closed = fake, False

    def new_context(self, **kw):
        self.fake.context = Context(self, kw)
        return self.fake.context

    def close(self):
        self.closed = True

    def is_connected(self):
        return True


class Chromium:
    def __init__(self, fake):
        self.fake = fake

    def connect(self, url, timeout=None):
        self.fake.connected_url = url
        return Browser(self.fake)

    def launch(self, **kw):
        self.fake.launch_kwargs = kw
        return Browser(self.fake)


class FakePlaywright:
    """Callable like sync_playwright: .start() gives the driver with .chromium and .stop()."""

    def __init__(self, **page_attrs):
        self.page_attrs = page_attrs
        self.connected_url = self.launch_kwargs = self.context = None
        self.stopped = False
        self.chromium = Chromium(self)

    def configure(self, page):
        for k, v in self.page_attrs.items():
            setattr(page, k, v)

    def __call__(self):
        return self

    def start(self):
        return self

    def stop(self):
        self.stopped = True


def execute(opts, fake):
    out, err = io.StringIO(), io.StringIO()
    doc, code = pt.execute(opts, out=out, err=err, sync_playwright=fake)
    return doc, code, out.getvalue(), err.getvalue()


def checks_of(doc):
    return {c["check"]: c for c in doc["checks"]}


def texts(path):
    blob = ""
    for root, _d, files in os.walk(path):
        for name in files:
            blob += open(os.path.join(root, name), "rb").read().decode("utf-8", "replace")
    return blob


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(pt, "ON_AIR_WAIT_S", 3.0)
    monkeypatch.setattr(pt, "RELEASE_WAIT_S", 3.0)


def real_opts(tmp_path, pw_file, *extra):
    return parse("--hold", "1.5", "--replay-wait", "5", "--out", str(tmp_path / "out"), "--i-have-owner-approval", *extra, pw=pw_file)


# ---------------------------------------------------------------- the flow against the fake

def test_happy_path_judges_the_replay_and_closes_everything(tmp_path, pw_file):
    fake = FakePlaywright()
    doc, code, out, err = execute(real_opts(tmp_path, pw_file, "--ws-endpoint", "ws://dvt-prod-browser:3000"), fake)
    c = checks_of(doc)
    assert code == 0 and doc["pass"] is True, doc["checks"]
    assert [x["check"] for x in doc["checks"]] == ["signin", "listen", "talk_button", "on_air", "released", "replay_level", "replay_frequency"]
    assert c["released"]["detail"] == "talk status 'Ended: Button released.' after the release"
    assert c["replay_level"]["rms"] == 0.17 and c["replay_frequency"]["hz"] == 700.0
    assert set(doc["skipped"]) == {"public_state", "recorder"}
    page = fake.context.page
    assert page.logins == [{"callsign": USER, "password": PASSWORD}]            # the password goes into the page and nowhere else
    assert page.room == "E" and page.logged_out and page.listen == "closed"
    assert page.gotos[0] == URL + "/login.html" and page.gotos[1] == URL + "/"
    assert fake.context.closed and fake.stopped
    assert fake.connected_url.startswith("ws://dvt-prod-browser:3000/?launch-options=")
    assert "use-fake-device-for-media-stream" in fake.connected_url and "use-file-for-fake-audio-capture%3D%2Fmedia%2Ffake-mic.wav" in fake.connected_url
    assert fake.context.kwargs["ignore_https_errors"] is False and fake.context.kwargs["permissions"] == ["microphone"]
    assert fake.context.scripts == [pt.brlib.RTC_PROBE_JS]
    assert not os.path.exists(tmp_path / "out" / "media" / "fake-mic.wav")      # the generated audio is removed again
    assert json.loads(out.splitlines()[-1])["passed"] == 7
    assert "result: 7 passed, 0 failed, 2 skipped" in err


def test_password_is_in_no_output_and_no_file(tmp_path, pw_file):
    fake = FakePlaywright(login_status=401)
    doc, code, out, err = execute(real_opts(tmp_path, pw_file), fake)
    assert code == 1
    assert PASSWORD not in out + err + texts(tmp_path / "out") + json.dumps(doc)
    fake = FakePlaywright()
    doc, code, out, err = execute(real_opts(tmp_path, pw_file), fake)
    assert code == 0 and PASSWORD not in out + err + texts(tmp_path / "out") + json.dumps(doc)
    # an exception text that carries the password is scrubbed
    class Boom(FakePlaywright):
        def __init__(self):
            super().__init__()
            self.chromium.launch = lambda **kw: (_ for _ in ()).throw(RuntimeError("cannot start %s now" % PASSWORD))
    doc, code, out, err = execute(real_opts(tmp_path, pw_file), Boom())
    assert code == 1 and checks_of(doc)["error"]["detail"].count("***") == 1
    assert PASSWORD not in out + err + texts(tmp_path / "out") + json.dumps(doc)


def test_local_chromium_gets_the_microphone_file_path(tmp_path, pw_file):
    fake = FakePlaywright()
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), fake)
    assert code == 0 and fake.connected_url is None
    args = fake.launch_kwargs["args"]
    assert fake.launch_kwargs["headless"] is True
    assert "--use-fake-ui-for-media-stream" in args and "--use-fake-device-for-media-stream" in args
    assert "--use-file-for-fake-audio-capture=" + str((tmp_path / "out" / "media" / "fake-mic.wav").resolve()) in args


def test_wrong_frequency_and_silence_fail(tmp_path, pw_file):
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), FakePlaywright(tone_hz=1000.0))
    c = checks_of(doc)
    assert code == 1 and c["replay_level"]["pass"] is True and c["replay_frequency"]["pass"] is False and c["replay_frequency"]["hz"] == 1000.0
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), FakePlaywright(tone_rms=0.003))
    c = checks_of(doc)
    assert code == 1 and c["replay_level"]["pass"] is False and c["replay_frequency"]["pass"] is False
    assert (tmp_path / "out" / "failure.png").exists()                                      # a failed run keeps one screenshot


def test_microphone_denied_login_refused_and_no_talk_button(tmp_path, pw_file):
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), FakePlaywright(on_press="Denied: Microphone access is unavailable. Press Talk to try again."))
    c = checks_of(doc)
    assert code == 1 and c["on_air"]["pass"] is False and "fake microphone" in c["on_air"]["detail"] and "replay_level" not in c
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), FakePlaywright(login_status=401))
    c = checks_of(doc)
    assert code == 1 and list(c) == ["signin", "error"] or list(c) == ["signin"]
    assert c["signin"]["pass"] is False and "401" in c["signin"]["detail"]
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), FakePlaywright(no_talk=True))
    c = checks_of(doc)
    assert code == 1 and c["talk_button"]["pass"] is False and "may_tx" in c["talk_button"]["detail"] and "on_air" not in c


def test_denied_floor_is_a_failed_on_air_check(tmp_path, pw_file):
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file), FakePlaywright(on_press="Denied: The floor is busy."))
    c = checks_of(doc)
    assert code == 1 and "The floor is busy" in c["on_air"]["detail"]


def test_public_state_is_read_while_the_button_is_held(tmp_path, pw_file, monkeypatch):
    seen = []

    def fake_get(url, ca_file=None, timeout=5.0):
        seen.append(url)
        page = fake.context.page if fake.context else None
        on = page is not None and page.talk == "On air"
        return 200, {"rooms": [{"room": "E", "onair": {"callsign": USER, "protocol": "webrtc"} if on else None}]}
    monkeypatch.setattr(pc, "get_json", fake_get)
    fake = FakePlaywright()
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file, "--public-state-url", URL + "/api/v1/public/state"), fake)
    c = checks_of(doc)
    assert code == 0 and c["public_state"]["pass"] is True and "named " + USER in c["public_state"]["detail"] and "webrtc" in c["public_state"]["detail"]
    assert set(seen) == {URL + "/api/v1/public/state"} and "public_state" not in doc["skipped"]
    # nobody on air in the state: the check fails
    monkeypatch.setattr(pc, "get_json", lambda *a, **k: (200, {"rooms": [{"room": "E", "onair": None}]}))
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file, "--public-state-url", URL + "/api/v1/public/state"), FakePlaywright())
    assert code == 1 and checks_of(doc)["public_state"]["pass"] is False


def test_recorder_row_protocol_must_be_webrtc(tmp_path, pw_file, monkeypatch):
    monkeypatch.setattr(pt, "RECORDER_WAIT_S", 3.0)
    now = int(time.time() * 1000) + 60000          # later than any press of this test
    good = [{"callsign": USER, "protocol": "webrtc", "reason": "terminator", "start_ms": now, "duration_ms": 1500, "room": "E"}]
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file, "--recorder-url", URL + "/api/v1/user/recordings"), FakePlaywright(rows=good))
    c = checks_of(doc)
    assert code == 0 and c["recorder"]["pass"] is True and c["recorder"]["protocol"] == "webrtc" and "recorder" not in doc["skipped"]
    bad = [dict(good[0], protocol="usrp")]
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file, "--recorder-url", URL + "/api/v1/user/recordings"), FakePlaywright(rows=bad))
    assert code == 1 and checks_of(doc)["recorder"]["pass"] is False and "usrp" in checks_of(doc)["recorder"]["detail"]
    doc, code, _o, _e = execute(real_opts(tmp_path, pw_file, "--recorder-url", URL + "/api/v1/user/recordings"), FakePlaywright(rows=[]))
    assert code == 1 and "no recorder row" in checks_of(doc)["recorder"]["detail"]


def test_playwright_missing_is_reported_not_a_crash(tmp_path, pw_file):
    try:
        import playwright  # noqa: F401
        pytest.skip("Playwright is installed here: the import-error path cannot be taken")
    except ImportError:
        pass
    out, err = io.StringIO(), io.StringIO()
    doc, code = pt.execute(real_opts(tmp_path, pw_file), out=out, err=err)
    c = checks_of(doc)
    assert code == 1 and "not importable" in c["error"]["detail"] and PASSWORD not in out.getvalue() + err.getvalue()


# ---------------------------------------------------------------- pure parts

def test_judge_audio():
    loud = [{"t": 0.4, "rms": 0.17, "peak_hz": 700.0, "fraction": 0.8}, {"t": 0.8, "rms": 0.17, "peak_hz": 1500.0, "fraction": 0.2}]
    got = pt.judge_audio(loud)
    assert got["level_ok"] and got["freq_ok"] and got["peak_hz"] == 700.0 and got["first_heard_s"] == 0.4 and got["loud_windows"] == 2
    assert pt.judge_audio([dict(loud[0], peak_hz=720.0)])["freq_ok"] is True
    assert pt.judge_audio([dict(loud[0], peak_hz=721.0)])["freq_ok"] is False
    assert pt.judge_audio([dict(loud[0], peak_hz=679.0)])["freq_ok"] is False
    assert pt.judge_audio([dict(loud[0], fraction=0.05)])["freq_ok"] is False                 # a flat spectrum is not a tone
    quiet = pt.judge_audio([{"t": 1, "rms": 0.005, "peak_hz": 700.0, "fraction": 0.9}])
    assert quiet["level_ok"] is False and quiet["freq_ok"] is False and quiet["best_rms"] == 0.005
    assert pt.judge_audio([])["level_ok"] is False
    assert pt.judge_audio([{"t": 1, "rms": 0.015, "peak_hz": 700.0, "fraction": 0.9}], floor=0.02)["level_ok"] is False


def test_collect_replay_stops_after_the_sound_ended():
    class S:
        def __init__(self, script):
            self.script, self.i = script, 0

        def window(self, seconds):
            w = self.script[min(self.i, len(self.script) - 1)]
            self.i += 1
            return {"rms": w, "peak_hz": 700.0, "fraction": 0.8}
    clock = {"t": 0.0}
    sess = S([0.0, 0.2, 0.2, 0.2, 0.0, 0.0, 0.2, 0.2])
    wins = pt.collect_replay(sess, 9.0, 0.01, 1e9, sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    assert [w["rms"] for w in wins] == [0.0, 0.2, 0.2, 0.2, 0.0, 0.0]
    wins = pt.collect_replay(S([0.0]), 2.0, 0.01, 1e9, sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    assert 4 <= len(wins) <= 6


def test_pick_recorder_row():
    rows = [{"callsign": "N0CHECK", "protocol": "webrtc", "start_ms": 1000}, {"callsign": "N0CHECK", "protocol": "usrp", "start_ms": 100},
            {"callsign": "N0OTHER", "protocol": "webrtc", "start_ms": 2000}, "x", {"callsign": None, "start_ms": 5000}]
    assert pt.pick_recorder_row(rows, "n0check", 500) == [rows[0]]
    assert pt.pick_recorder_row(rows, "N0CHECK", 5000) == []


def test_mic_wav_is_a_mono_16_bit_700_hz_sine(tmp_path):
    path = pt.write_mic_wav(tmp_path / "m" / "fake-mic.wav", seconds=1.0)
    with wave.open(path, "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 48000, 48000)
        import struct
        samples = struct.unpack("<%dh" % wav.getnframes(), wav.readframes(wav.getnframes()))
    hz, _a, frac, rms = pc.dominant_frequency(list(samples), rate=48000, lo=300.0, hi=1500.0)
    assert abs(hz - 700) <= 1.5 and frac > 0.95 and 10000 < rms < 13000
    assert not [p for p in tmp_path.rglob("*") if p.suffix == ".wav" and "fake-mic" not in p.name]


def test_wav_is_never_committed():
    root = pt.ROOT
    ignore = (root / ".gitignore").read_text()
    assert "*.wav" in ignore or "out/" in ignore


def test_context_and_launch_options(pw_file):
    o = parse("--ca-file", CA_FILE, pw=pw_file)
    assert pt.context_options(o)["ignore_https_errors"] is True
    assert pt.context_options(parse(pw=pw_file))["ignore_https_errors"] is False
    options, ws = pt.browser_launch_options(parse("--ws-endpoint", "ws://127.0.0.1:3000", pw=pw_file), "/media/fake-mic.wav")
    assert "--use-file-for-fake-audio-capture=/media/fake-mic.wav" in options["args"] and ws.startswith("ws://127.0.0.1:3000/?launch-options=")
    assert pt.browser_launch_options(parse(pw=pw_file), "/x.wav")[1] is None


# ---------------------------------------------------------------- safety rails

def test_approval_module_and_hold_rules(pw_file):
    assert "--i-have-owner-approval" in usage(pw=pw_file, raw=True)
    assert parse("--dry-run", pw=pw_file).dry_run and parse("--print-docker", pw=pw_file).print_docker
    assert pt.main(["--url", URL, "--user", USER, "--password-file", pw_file]) == 2
    assert "echo module E" in usage("--room", "A", "--i-have-owner-approval", pw=pw_file)
    assert parse("--room", "A", "--allow-other-module", "--i-have-owner-approval", pw=pw_file).room == "A"
    assert "one capital" in usage("--room", "e", "--i-have-owner-approval", pw=pw_file)
    for bad in ("1.4", "5.1", "nan", "0"):
        assert "between 1.5 and 5" in usage("--hold", bad, "--i-have-owner-approval", pw=pw_file)
    assert parse("--hold", "5", "--i-have-owner-approval", pw=pw_file).hold == 5.0
    assert parse("--i-have-owner-approval", pw=pw_file).hold == 3.0


def test_url_and_endpoint_rules(pw_file):
    ok = ["--i-have-owner-approval"]
    for bad in ("http://web.example", "https://u:p" + chr(64) + "web.example", "https://web.example/login", "https://web.example/?a=1", "web.example"):
        with pytest.raises(pc.UsageError):
            pt.build_options(["--url", bad, "--user", USER, "--password-file", pw_file] + ok, environ={})
    assert pt.build_options(["--url", "https://web.example:8443/", "--user", USER] + ok, environ={}).url == "https://web.example:8443"
    for good in ("ws://127.0.0.1:3000", "ws://localhost:3000", "ws://dvt-prod-browser:3000", "wss://[::1]:3000"):
        assert parse("--ws-endpoint", good, *ok, pw=pw_file).ws_endpoint == good
    for bad in ("ws://browser.example:3000", "ws://192.0.2.7:3000"):
        assert "allow-remote-browser" in usage("--ws-endpoint", bad, *ok, pw=pw_file)
        assert parse("--ws-endpoint", bad, "--allow-remote-browser", *ok, pw=pw_file).ws_endpoint == bad
    assert "ws://" in usage("--ws-endpoint", "http://127.0.0.1:3000", *ok, pw=pw_file)
    assert "same origin" in usage("--recorder-url", "https://other.example/api/v1/user/recordings", *ok, pw=pw_file)
    assert "https" in usage("--public-state-url", "http://web.example/api/v1/public/state", *ok, pw=pw_file)
    assert parse("--recorder-url", URL + "/api/v1/user/recordings", "--public-state-url", URL + "/api/v1/public/state", *ok, pw=pw_file).recorder_url
    assert "not a file" in usage("--ca-file", "/nonexistent-ca", *ok, pw=pw_file)


def test_certificate_verification_is_never_switched_off():
    import inspect
    src = inspect.getsource(pt) + inspect.getsource(pc)
    assert "CERT_NONE" not in src and "check_hostname = False" not in src and "--ignore-certificate-errors" not in src
    assert pt.context_options(parse())["ignore_https_errors"] is False


def test_password_file_mode_and_environment(tmp_path):
    f = tmp_path / "p"
    f.write_text(PASSWORD)
    for mode in (0o644, 0o640, 0o604, 0o666):
        f.chmod(mode)
        assert "mode" in usage("--i-have-owner-approval", pw=str(f)) and PASSWORD not in usage("--i-have-owner-approval", pw=str(f))
    f.chmod(0o600)
    assert parse("--i-have-owner-approval", pw=str(f)).password_file == str(f)
    link = tmp_path / "l"
    link.symlink_to(f)
    assert "symbolic link" in usage("--i-have-owner-approval", pw=str(link))
    assert pc.read_secret(None, pt.ENV_PASSWORD, {pt.ENV_PASSWORD: PASSWORD}) == (PASSWORD, "env")
    with pytest.raises(SystemExit):                          # there is no --password option, and no abbreviation of --password-file
        pt.build_options(["--url", URL, "--user", USER, "--password", PASSWORD, "--i-have-owner-approval"], environ={})


def test_dry_run_connects_to_nothing_and_writes_nothing(pw_file, tmp_path, monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("network used in a dry run")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(pc, "read_secret", boom)
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    code = pt.main(["--url", URL, "--user", USER, "--password-file", pw_file, "--dry-run", "--ws-endpoint", "ws://dvt-prod-browser:3000",
                    "--public-state-url", URL + "/api/v1/public/state?token=abc"])
    cap = capsys.readouterr()
    assert code == 0 and PASSWORD not in cap.out + cap.err and "token" not in cap.out
    plan = json.loads(cap.out.splitlines()[-1])["plan"]
    assert plan["room"] == "E" and plan["hold_s"] == 3.0 and plan["certificate"].startswith("verified")
    assert "--use-file-for-fake-audio-capture=/media/fake-mic.wav" in plan["chromium_args"]
    assert "replay_frequency 700 Hz +- 20 Hz" in plan["checks"] and any(x.startswith("public_state") for x in plan["checks"])
    assert "DRY RUN" in cap.out and sorted(p.name for p in tmp_path.iterdir()) == before


def test_docker_lines_are_contained_and_not_run(pw_file, monkeypatch, capsys):
    env = {}
    o = parse("--public-state-url", URL + "/api/v1/public/state", "--recorder-url", URL + "/api/v1/user/recordings", pw=pw_file)
    cmds = pt.docker_commands(o, env)
    browser, tool = " ".join(cmds["browser"]), " ".join(cmds["tool"])
    for need in ("docker run -d --rm --name dvt-prod-browser --init --network dvt-prod --read-only", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--memory 3g --memory-swap 3g --cpus 2 --pids-limit 1024", "--shm-size 1g",
                 "-v $OUT/media:/media:ro dvt-browser:local --host 0.0.0.0 --port 3000 --unsafe"):
        assert need in browser, need
    assert "-p " not in browser and "--publish" not in browser and "--privileged" not in browser       # published to nobody
    for need in ("--read-only", "--cap-drop ALL", "--memory 1g --memory-swap 1g --cpus 2 --pids-limit 512", "-v $PWD:/dvt:ro",
                 "dvt-tester:local python3 -m dvtest.tools.prod_talk", "--ws-endpoint ws://dvt-prod-browser:3000", "--password-file /run/pw",
                 "--i-have-owner-approval", "--public-state-url", "--recorder-url"):
        assert need in tool, need
    assert PASSWORD not in tool and "--network host" not in tool and "--privileged" not in tool
    tuned = pt.docker_commands(o, {"DVT_CTR_MEM": "2g", "DVT_CTR_CPUS": "1", "DVT_CTR_PIDS": "256"})
    assert "--memory 2g --memory-swap 2g --cpus 1 --pids-limit 256" in " ".join(tuned["browser"])
    assert "--memory 2g" in " ".join(tuned["tool"])
    # --print-docker prints the script and runs nothing
    monkeypatch.setattr(os, "system", lambda *a: (_ for _ in ()).throw(AssertionError("ran a command")))
    assert pt.main(["--url", URL, "--user", USER, "--password-file", pw_file, "--print-docker"]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith("#") and "docker network create dvt-prod" in text and 'mkdir -p "$OUT/media"' in text
    assert "docker stop dvt-prod-browser" in text and '"$PWD:/dvt:ro"' in text
