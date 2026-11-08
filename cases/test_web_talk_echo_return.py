# SPDX-License-Identifier: MIT
"""Web unit, Talk echo return: a signed-in user listens on the built-in ECHO module E, holds Talk for 3 s with the 700 Hz fake microphone
and releases; the echo replays the over 1 s after its end. Does the audio come back to THE SAME PAGE, and to a SECOND page that listens on
the module? Written for a product finding on dv-router:wip-97c669d (login, listen, "On air", the echo replays, "Ended: Button released."
appear, but the talker's page analyser saw nothing for 14 s, twice; the same harness heard the echo on wip-efd9eca): the case must say
whether the media stops at the edge or in the page. NOT RUN on a rig yet (docs/web-browser-cases.md, "Talk echo return", UNVERIFIED list).

  test_talk_echo_returns_to_the_page    one run per engine (Playwright Chromium, Firefox, Playwright WebKit; the fake microphone setup
                                        of cases/browser_fixtures.py exists for all three)

What is asserted. EVIDENCE (the preconditions, judged from the page text as the Talk cases do): both pages reach "Listening on module E";
the talk status becomes "On air" and, after the release, "Ended: Button released.". PRODUCT CHECK (the audio, in the page's own analyser):

  talker_hears_echo     the TALKER page: analyser rms above the floor (0.01) within 14 s after the release, and on Chromium the dominant
                        frequency is 700 Hz within 20 Hz (the judgement of dvtest.tools.prod_talk, deploy check g)
  listener_hears_live   a SECOND page (the user without may_tx) listening on E: analyser rms above the floor while Talk is held
  listener_hears_echo   the same page: the echo replay after the release (level, and on Chromium 700 Hz)

On Firefox the page's AudioContext stays suspended in the container (docs/web-browser-cases.md): the level is then judged from the decoder's
totalAudioEnergy of getStats, or from the packet count when the engine has none; Firefox's and WebKit's fake sources are not 700 Hz, so
their frequency is recorded and not asserted. The failure message names the stage that stopped (judge_window): no inbound-rtp bytes at all
(the media stops before the page), bytes but a silent decoder (page/decoder), bytes and an audible decoder but a silent analyser.

The recorder (cases/talk_echo_support.py ECHO_RECORDER_JS) samples every 500 ms in both pages from before the press to 14 s after the release
and keeps, for EVERY RTCPeerConnection the page creates (id, createdAt, closedAt, the three states, getStats inbound-rtp / outbound-rtp /
candidate pair bytes, audioLevel, totalAudioEnergy, and its own analyser tap on each pc's receiver). Whether the page swapped sessions at the
press or the release is in the file, and the request audit lists every /rtc/* request with its status and time. Everything is written to
out/<run>/web/talk-echo-<engine>.json in a finally block, also when a check fails or the flow breaks.

Needs --profile web,browser and rig/config/dv-router/echo.toml in DVT_SUT_CONFIG_EXTRA (module E is the echo). No USRP client is used.
"""
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import talk_echo_support as tes  # noqa: E402
import web_pages as pages  # noqa: E402
from browser_fixtures import BrowserPage, _rtc_end  # noqa: E402
from dvtest import browser as brlib  # noqa: E402
from dvtest import echo  # noqa: E402
from echo_support import EchoCfg  # noqa: E402
from test_web_webrtc import _digest  # noqa: E402
from webrtc_support import MIC_UNAVAILABLE, Rtc, on_air_or_end, press, public_state, release, wait_talk_text  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser, pytest.mark.webrtc, pytest.mark.slow]

POLL_S = 0.5


@pytest.fixture
def echo_on_e(target):
    """Module E is the built-in echo: DV-Router, native launch, and an [echo] table with E switched on in the rendered config (the
    file is read, never edited). Without the rendered file the defaults of echo.toml are assumed, as cases/test_echo.py does."""
    if target.sut_profile != "dvrouter":
        pytest.skip("the echo service is DV-Router's: DVT_SUT_PROFILE=dvrouter")
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("echo is configured in the TOML: DVT_SUT_LAUNCH=native with rig/config/dv-router/echo.toml")
    cfg = EchoCfg.locate(target)
    settings = cfg.settings if cfg else {"max_seconds": echo.DEFAULT_MAX_SECONDS, "delay_ms": echo.DEFAULT_DELAY_MS, "modules": {tes.ROOM: True}}
    if settings is None:
        pytest.skip("no [echo] table in the rendered config: add rig/config/dv-router/echo.toml to DVT_SUT_CONFIG_EXTRA")
    if not settings["modules"].get(tes.ROOM):
        pytest.skip("module %s is not switched on in [echo] modules (%s)" % (tes.ROOM, settings["modules"]))
    return settings


@pytest.fixture
def echo_rx_page(engine, engine_browsers, web_base_url, request, br_rx):
    """The second page: the user without may_tx, signed in, with the RTC probe (its own artifact name, so its HAR and trace never share a
    path with the talker's page)."""
    bp = BrowserPage(engine, engine_browsers(engine), web_base_url, request.node.name.replace("[", "-").rstrip("]") + "-listener")
    bp.install_rtc_probe()
    status = bp.api_login(br_rx.callsign, br_rx.password)
    if status != 200:
        bp.finish(True)
        pytest.fail("browser API login of %s: HTTP %s" % (br_rx.callsign, status))
    yield bp
    _rtc_end(bp, request)


class Run:
    """What one run keeps, whatever happens: phases (tester wall clock), the talk-status timeline, the public state, the audits."""

    def __init__(self, engine, tx, rx):
        self.engine, self.tx, self.rx = engine, tx, rx
        self.pages = {"talker": tx, "listener": rx}
        self.phases = {}
        self.timeline = []                  # (seconds since the "listening" phase, talker status, talker Listen line, listener Listen line) on change
        self.public = []                    # (seconds since the "listening" phase, over on E?, callsign)
        self.probe_press_ms = None          # the shared probe's clock at the press (its ctl frames are on that clock)
        self.audits = {"talker": tes.RtcAudit().attach(tx.page), "listener": tes.RtcAudit().attach(rx.page)}
        self.texts = {}
        self.dumps = {"talker": None, "listener": None}
        self.ctl = {}
        self.errors = []
        self._last = None

    def mark(self, name):
        self.phases[name] = time.time()
        for who, bp in self.pages.items():
            try:
                bp.page.evaluate("(n) => window.__dvtEcho.mark(n)", name)
            except Exception as exc:                     # noqa: BLE001 - evidence only
                self.errors.append("mark %s on %s: %s" % (name, who, type(exc).__name__))

    def since_start(self):
        return round(time.time() - self.phases["listening"], 2) if "listening" in self.phases else None

    def poll(self, rtc_tx, rtc_rx, anon, with_public):
        """One look at both pages (and the public state): recorded when something changed."""
        row = (rtc_tx.talk_status(), rtc_tx.status(), rtc_rx.status())
        if row != self._last:
            self._last = row
            self.timeline.append((self.since_start(),) + row)
        if with_public:
            try:
                got = tes.onair_summary(public_state(anon), tes.ROOM)
                self.public.append((self.since_start(), got["onair"], got["callsign"]))
            except Exception as exc:                     # noqa: BLE001
                self.public.append((self.since_start(), None, type(exc).__name__))

    def wait(self, seconds, rtc_tx, rtc_rx, anon, every=POLL_S, public_every=1.0):
        """Look at both pages every `every` s for `seconds`; Playwright's own wait keeps the page events flowing (the request audit)."""
        end = time.monotonic() + seconds
        next_public = time.monotonic()
        while time.monotonic() < end:
            with_public = time.monotonic() >= next_public
            if with_public:
                next_public = time.monotonic() + public_every
            self.poll(rtc_tx, rtc_rx, anon, with_public)
            self.tx.page.wait_for_timeout(int(1000 * min(every, max(0.0, end - time.monotonic()))))

    def collect(self):
        for who, bp in self.pages.items():
            try:
                self.dumps[who] = bp.page.evaluate("() => { window.__dvtEcho.mark('final'); window.__dvtEcho.stop(); return window.__dvtEcho.dump(); }")
            except Exception as exc:                     # noqa: BLE001
                self.errors.append("dump of %s: %s: %s" % (who, type(exc).__name__, str(exc).splitlines()[0][:100] if str(exc) else ""))
        try:
            sent = self.tx.page.evaluate("() => window.__dvtTap ? window.__dvtTap.sent : []")
            received = self.tx.page.evaluate("() => window.__dvtTap ? window.__dvtTap.msgs : []")
            since = self.probe_press_ms or 0.0
            self.ctl = {"note": "ctl DataChannel frames of the talker page reduced to op/state/code/reason; t_ms on the shared probe's "
                                "clock, relative to the press when it was taken",
                        "sent": _digest(sent, since), "received": _digest(received, since),
                        "relative_to_press": self.probe_press_ms is not None}
        except Exception as exc:                         # noqa: BLE001
            self.errors.append("ctl frames: %s" % type(exc).__name__)


def start_recorders(*bps):
    for bp in bps:
        got = bp.page.evaluate("(ms) => window.__dvtEcho ? window.__dvtEcho.start(ms) : null", tes.SAMPLE_MS)
        assert got is not None, "the recorder is not installed in the page"


def test_talk_echo_returns_to_the_page(engine, rtc_tx_page, echo_rx_page, echo_on_e, web_client):
    tx, rx = rtc_tx_page, echo_rx_page
    for bp in (tx, rx):
        bp.context.add_init_script(tes.ECHO_RECORDER_JS)         # after the shared probe, before the first dashboard navigation
    run = Run(engine, tx, rx)
    rtc_tx, rtc_rx = Rtc(tx), Rtc(rx)
    anon = web_client()
    failures = []
    analysis = {}
    try:
        rtc_tx.open()
        rtc_rx.open()
        assert pages.talk_button(tx.page).inner_text().strip() == "Hold Talk", "the talker page shows no Hold Talk button"
        assert pages.talk_button(rx.page).count() == 0, "the page of the user without may_tx offers Talk"
        start_recorders(tx, rx)
        for who, rtc in (("listener", rtc_rx), ("talker", rtc_tx)):
            got = rtc.connect(tes.ROOM)
            assert got["status"] == "Listening on module %s" % tes.ROOM, "%s page: %s" % (who, got)
        assert pages.talk_button(tx.page).is_enabled(), "Hold Talk is not enabled after Listen"
        run.mark("listening")
        run.wait(tes.PRE_S, rtc_tx, rtc_rx, anon)

        # -- the hold
        run.mark("press")
        run.probe_press_ms = rtc_tx.clock()
        press(rtc_tx)
        text, took = wait_talk_text(rtc_tx, on_air_or_end, 30.0)
        run.mark("on_air")
        run.texts["after_press"] = {"status": text, "seconds": round(took, 2) if took else None}
        if text.startswith(MIC_UNAVAILABLE):
            release(rtc_tx)
            pytest.skip("%s: no fake microphone (talk status %r)" % (brlib.label(engine), text))
        if text != pages.TALK_ON_AIR_TEXT:
            release(rtc_tx)
            failures.append("evidence: the talk status after the press is %r, not %r (Listen line %r)" % (text, pages.TALK_ON_AIR_TEXT, rtc_tx.status()))
        else:
            run.wait(tes.HOLD_S, rtc_tx, rtc_rx, anon, every=0.25, public_every=0.5)
            run.mark("release")
            release(rtc_tx)
            ended, took_end = wait_talk_text(rtc_tx, lambda s: s != pages.TALK_ON_AIR_TEXT, 8.0)
            run.mark("ended")
            run.texts["after_release"] = {"status": ended, "seconds": round(took_end, 2) if took_end else None}
            if ended != pages.TALK_RELEASED_TEXT:
                failures.append("evidence: after the release the talk status is %r, not %r" % (ended, pages.TALK_RELEASED_TEXT))
            elapsed = time.time() - run.phases["release"]
            run.wait(max(0.0, tes.TAIL_S - elapsed), rtc_tx, rtc_rx, anon)
            run.mark("tail_end")
    finally:
        try:
            run.collect()
            if "release" in run.phases:
                analysis = tes.analyse_run(run.dumps, engine)
                failures += tes.failure_lines(analysis["checks"], analysis["diagnosis"])
            tables = {who: tes.requests_table(a.rows, run.phases) for who, a in run.audits.items()}
            doc = {
                "meta": {"engine": brlib.label(engine), "room": tes.ROOM, "hold_s": tes.HOLD_S, "pre_s": tes.PRE_S, "tail_s": tes.TAIL_S,
                         "sample_ms": tes.SAMPLE_MS, "rms_floor": tes.RMS_FLOOR, "expect_hz": tes.EXPECT_HZ.get(engine),
                         "image": os.environ.get("XLXD_SUT_IMAGE", ""), "echo": echo_on_e,
                         "note": "checks of kind product are the audio assertions, kind evidence the rest; times of a recorder are ms on its "
                                 "own page clock, phases are the tester's wall clock (seconds)"},
                "phases": run.phases, "texts": run.texts, "timeline": run.timeline, "public_state_room_E": run.public,
                "checks": [{k: c[k] for k in ("name", "kind", "ok", "stage", "detail")} for c in analysis.get("checks", [])],
                "verdicts": {c["name"]: c["verdict"] for c in analysis.get("checks", []) if c.get("verdict")},
                "diagnosis": analysis.get("diagnosis", ""), "failures": list(failures), "errors": run.errors,
                "talker": {"analysis": analysis.get("talker"), "requests": tables["talker"], "requests_summary": tes.requests_summary(tables["talker"]),
                           "recorder": run.dumps["talker"], "ctl": run.ctl,
                           "series": tes.series(run.dumps["talker"]) if run.dumps["talker"] else []},
                "listener": {"analysis": analysis.get("listener"), "requests": tables["listener"],
                             "requests_summary": tes.requests_summary(tables["listener"]), "recorder": run.dumps["listener"],
                             "series": tes.series(run.dumps["listener"]) if run.dumps["listener"] else []}}
            tes.write_evidence(tes.evidence_path(os.environ.get("DVT_OUT", "out"), engine), doc)
        except Exception as exc:                         # noqa: BLE001 - the evidence must never mask the case's own outcome
            print("talk-echo: evidence not written: %s: %s" % (type(exc).__name__, exc))

    if analysis.get("diagnosis"):
        print("[RECORD] talk-echo %s: %s" % (brlib.label(engine), analysis["diagnosis"]))
    if failures:
        pytest.fail("\n".join(failures))
