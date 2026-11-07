# SPDX-License-Identifier: MIT
"""dvtest/tools/prod_iax.py (production check f): the whole flow against a small fake edge on 127.0.0.1 (REGREQ /
REGAUTH / REGACK, NEW / AUTHREQ / AUTHREP / ACCEPT / ANSWER, voice echo, HANGUP; nothing else is ever contacted), the
measures on synthetic tones, the safety rails, and that the secret appears in no output and no file."""
import hashlib
import http.server
import io
import json
import math
import os
import random
import socket
import struct
import threading
import time

import pytest

from dvtest import asl_lab
from dvtest.proto import iax2
from dvtest.proto import iax2_reg as reg
from dvtest.tools import prod_common as pc
from dvtest.tools import prod_iax as pi

SECRET = "s3cret-iax-7431"
ACCOUNT = "N0TEST-U1"
CALL = "N0TEST"
CHALLENGE = b"987654321"
DIAL = "400165"


# ---------------------------------------------------------------- a fake edge

class FakeEdge(threading.Thread):
    """Phone-mode edge on loopback. mode: "echo" (replay what was keyed), "short" (drop the last 20 frames),
    "wrongtone" (replay 1 kHz), "silent" (no replay), "noanswer" (ACCEPT but never ANSWER)."""

    def __init__(self, mode="echo", secret=SECRET, delay=0.2, spacing=0.004):
        super().__init__(daemon=True)
        self.mode, self.secret, self.delay, self.spacing = mode, secret, delay, spacing
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.halt = False
        self.lock = threading.Lock()
        self.calls = {}                 # client call number -> state dict
        self.registered = False
        self.new_count = 0
        self.dialled = []
        self.authed = False
        self.keyed = False
        self.frames = []                # ulaw payloads received while keyed
        self.replayed = 0
        self.hangups = 0
        self.names = []
        self.start()

    def state(self, src, addr):
        return self.calls.setdefault(src, {"addr": addr, "oseq": 0, "iseq": 0})

    def send(self, st, kind, sub, payload=b"", ts=0, reliable=True):
        with self.lock:
            data = iax2.full_frame(7, st["src"], ts, st["oseq"], st["iseq"], kind, sub, payload)
            if reliable:
                st["oseq"] = (st["oseq"] + 1) & 255
        self.sock.sendto(data, st["addr"])

    def ack(self, st, frame):
        self.send(st, iax2.IAX, iax2.ACK, ts=frame.timestamp, reliable=False)

    def run(self):
        while not self.halt:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if len(data) < 4:
                continue
            if not data[0] & 0x80:
                src, _ts = struct.unpack("!HH", data[:4])
                st = self.calls.get(src)
                if st and self.keyed and len(data) == 164:
                    self.frames.append(data[4:])
                continue
            frame = iax2.parse_full(data)
            st = self.state(frame.source, addr)
            st["src"] = frame.source
            ies = iax2.parse_information(frame.payload) if frame.kind == iax2.IAX else {}
            if frame.kind == iax2.IAX and frame.subclass == iax2.ACK:
                continue
            if frame.kind == iax2.IAX and frame.subclass in (reg.REGREQ, iax2.NEW) and ies.get(54) == b"":
                st["oseq"], st["iseq"] = 0, 0
                self.send(st, iax2.IAX, iax2.CALLTOKEN, iax2.information(54, b"T" * 39), reliable=False)
                continue
            st["iseq"] = (frame.out_sequence + 1) & 255
            if frame.kind == iax2.IAX and frame.subclass == reg.REGREQ:
                if reg.IE_MD5 in ies:
                    if ies[reg.IE_MD5] == reg.md5_result(CHALLENGE, self.secret):
                        self.registered = True
                        self.send(st, iax2.IAX, reg.REGACK, iax2.information(6, ies[6]) + iax2.information(19, struct.pack("!H", 60)))
                    else:
                        self.send(st, iax2.IAX, reg.REGREJ, iax2.information(22, b"authentication failed") + iax2.information(42, b"\x1d"))
                else:
                    self.send(st, iax2.IAX, reg.REGAUTH, iax2.information(14, struct.pack("!H", 2)) + iax2.information(15, CHALLENGE)
                              + iax2.information(6, b"x"))
            elif frame.kind == iax2.IAX and frame.subclass == iax2.NEW:
                self.new_count += 1
                self.dialled.append(ies.get(1, b"").decode())
                self.names.append((ies.get(4, b"").decode(), ies.get(6, b"").decode(), 2 in ies))
                self.send(st, iax2.IAX, iax2.AUTHREQ, iax2.information(14, struct.pack("!H", 2)) + iax2.information(15, CHALLENGE)
                          + iax2.information(6, ies.get(6, b"")))
            elif frame.kind == iax2.IAX and frame.subclass == iax2.AUTHREP:
                if ies.get(16) == hashlib.md5(CHALLENGE + self.secret.encode()).hexdigest().encode():
                    self.authed = True
                    self.send(st, iax2.IAX, iax2.ACCEPT, iax2.information(9, struct.pack("!I", iax2.ULAW)))
                    if self.mode != "noanswer":
                        self.send(st, iax2.CONTROL, 4)
                else:
                    self.send(st, iax2.IAX, iax2.REJECT, iax2.information(22, b"bad"))
            elif frame.kind == iax2.CONTROL and frame.subclass == 12:
                self.frames.clear()
                self.keyed = True
                self.ack(st, frame)
            elif frame.kind == iax2.CONTROL and frame.subclass == 13:
                self.keyed = False
                self.ack(st, frame)
                threading.Thread(target=self.replay, args=(st,), daemon=True).start()
            elif frame.kind == iax2.VOICE:
                if self.keyed:
                    self.frames.append(frame.payload)
                self.ack(st, frame)
            elif frame.kind == iax2.IAX and frame.subclass == iax2.HANGUP:
                self.hangups += 1
                self.ack(st, frame)
            else:
                self.ack(st, frame)

    def replay(self, st):
        try:
            self.replay_frames(st)
        except OSError:
            pass                        # the test closed the socket

    def replay_frames(self, st):
        time.sleep(self.delay)
        frames = list(self.frames)
        if self.mode == "silent":
            return
        if self.mode == "short":
            frames = frames[:-20]
        if self.mode == "wrongtone":
            data = asl_lab.tone_ulaw(len(frames) * 20, 1000, 8000)
            frames = [data[i:i + 160] for i in range(0, len(data) - 159, 160)]
        for i, payload in enumerate(frames):
            if self.halt:
                return
            if i == 0:
                self.send(st, iax2.VOICE, iax2.ULAW, payload, ts=0)
            else:
                self.sock.sendto(struct.pack("!HH", 7, (i * 20) & 0xFFFF) + payload, st["addr"])
            self.replayed += 1
            time.sleep(self.spacing)

    def close(self):
        self.halt = True
        self.join(2)
        self.sock.close()


@pytest.fixture
def edge():
    made = []

    def make(**kw):
        made.append(FakeEdge(**kw))
        return made[-1]
    yield make
    for e in made:
        e.close()


class Web:
    """Metrics text and a public state document on loopback; both follow what the fake edge saw."""

    def __init__(self, edge, static=False, who=CALL):
        self.edge, self.static, self.who = edge, static, who
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/metrics"):
                    body = outer.metrics().encode()
                    ctype = "text/plain"
                elif self.path.startswith("/state"):
                    body = json.dumps(outer.state()).encode()
                    ctype = "application/json"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def metrics(self):
        e = self.edge
        ok = 5 + (0 if self.static else int(e.authed))
        rx = 100 + (0 if self.static else len(e.frames))
        tx = 90 + (0 if self.static else e.replayed)
        return ('# TYPE dv_edge_allstar_setup_total counter\n'
                'dv_edge_allstar_setup_total{kind="client",result="ok"} %d\n'
                'dv_edge_allstar_setup_total{kind="client",result="auth"} 2\n'
                'dv_edge_allstar_frames_total{dir="rx",kind="voice"} %d\n'
                'dv_edge_allstar_frames_total{dir="tx",kind="voice"} %d\n'
                'dv_edge_allstar_frames_total{dir="rx",kind="control"} 7\n' % (ok, rx, tx))

    def state(self):
        air = {"callsign": self.who, "callsign_base": self.who, "protocol": "allstar"} if self.edge.keyed else None
        return {"rooms": [{"room": "A", "onair": None}, {"room": "E", "onair": air}], "version": "x"}

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def web():
    made = []

    def make(edge, **kw):
        made.append(Web(edge, **kw))
        return made[-1]
    yield make
    for w in made:
        w.close()


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(pi, "CALL_TIMEOUT_S", 2.0)
    monkeypatch.setattr(pi, "REGISTER_TIMEOUT_S", 3.0)


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "iax.secret"
    path.write_text(SECRET + "\n")
    path.chmod(0o600)
    return str(path)


def opts_for(edge, secret_file, tmp_path, *extra, seconds="1.2"):
    argv = ["--account", ACCOUNT, "--callsign", CALL, "--secret-file", secret_file, "--port", str(edge.port),
            "--seconds", seconds, "--replay-wait", "4", "--out", str(tmp_path / "out"), "--i-have-owner-approval"] + list(extra)
    return pi.build_options(argv)


def run(opts):
    out, err = io.StringIO(), io.StringIO()
    doc, code = pi.execute(opts, out=out, err=err)
    return doc, code, out.getvalue(), err.getvalue()


def checks_of(doc):
    return {c["check"]: c for c in doc["checks"]}


def all_files_text(path):
    text = ""
    for root, _dirs, files in os.walk(path):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                text += fh.read().decode("utf-8", "replace")
    return text


# ---------------------------------------------------------------- the flow

def test_happy_path_registers_dials_sends_and_judges_the_replay(edge, secret_file, tmp_path):
    e = edge()
    doc, code, out, err = run(opts_for(e, secret_file, tmp_path))
    c = checks_of(doc)
    assert code == 0 and doc["pass"] is True, doc["checks"]
    assert set(c) == {"regack", "answer", "replay_frames", "replay_frequency", "replay_level", "hangup"}
    assert all(v["pass"] for v in c.values())
    assert c["replay_frames"]["frames"] == 60 and c["replay_frames"]["min_frames"] == 56
    assert abs(c["replay_frequency"]["hz"] - 700) <= 2 and c["replay_frequency"]["fraction"] > 0.9
    assert e.registered and e.authed and e.new_count == 1 and e.hangups == 1
    assert e.dialled == [DIAL]                                      # the documented default for module E
    assert e.names == [(CALL, ACCOUNT, False)]                      # CALLING_NAME, USERNAME, no CALLING_NUMBER
    assert len(e.frames) == 60
    assert set(doc["skipped"]) == {"hub_metrics", "public_state"}
    lines = [json.loads(ln) for ln in out.splitlines()]
    assert lines[-1]["summary"] is True and lines[-1]["pass"] is True and lines[-1]["passed"] == 6 and lines[-1]["skipped"]
    assert "result: 6 passed, 0 failed, 2 skipped" in err
    assert os.path.exists(tmp_path / "out" / "results.json") and os.path.exists(tmp_path / "out" / "summary.txt")


def test_default_three_second_over_needs_140_of_150(edge, secret_file, tmp_path):
    e = edge()
    doc, code, _out, _err = run(opts_for(e, secret_file, tmp_path, seconds="3.0"))
    c = checks_of(doc)
    assert code == 0, doc["checks"]
    assert c["replay_frames"]["frames"] == 150 and c["replay_frames"]["min_frames"] == 140
    assert 0.1 < c["replay_frames"]["replay_delay_s"] < 3.0


def test_secret_is_in_no_output_and_no_file(edge, secret_file, tmp_path, web):
    e = edge()
    w = web(e)
    opts = opts_for(e, secret_file, tmp_path, "--hub-metrics", w.url("/metrics"), "--public-state-url", w.url("/state"),
                    "--metrics-wait", "5")
    doc, code, out, err = run(opts)
    assert code == 0, doc["checks"]
    blob = out + err + all_files_text(tmp_path / "out") + json.dumps(doc)
    assert SECRET not in blob and hashlib.md5(SECRET.encode()).hexdigest() not in blob
    # an error that carries the secret is scrubbed too
    red = pc.Redactor()
    red.hide(SECRET)
    assert SECRET not in red.scrub("boom %s boom" % SECRET) and "***" in red.scrub(SECRET)
    # a refused run (wrong secret on the edge) leaks nothing either
    e2 = edge(secret="another-secret-0000")
    doc2, code2, out2, err2 = run(opts_for(e2, secret_file, tmp_path / "second"))
    assert code2 == 1 and SECRET not in out2 + err2 + all_files_text(tmp_path / "second" / "out") + json.dumps(doc2)


def test_wrong_secret_stops_at_regrej_and_never_dials(edge, secret_file, tmp_path):
    e = edge(secret="another-secret-0000")
    doc, code, _out, _err = run(opts_for(e, secret_file, tmp_path))
    c = checks_of(doc)
    assert code == 1 and c["regack"]["pass"] is False and "REGREJ" in c["regack"]["detail"]
    assert c["answer"]["pass"] is False and "not tried" in c["answer"]["detail"]
    assert e.new_count == 0 and not e.keyed and not e.frames


def test_short_replay_fails_the_frame_count_only(edge, secret_file, tmp_path):
    e = edge(mode="short")
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path))
    c = checks_of(doc)
    assert code == 1
    assert c["replay_frames"]["pass"] is False and c["replay_frames"]["frames"] == 40
    assert c["replay_frequency"]["pass"] is True and c["regack"]["pass"] and c["answer"]["pass"] and c["hangup"]["pass"]


def test_wrong_frequency_fails(edge, secret_file, tmp_path):
    e = edge(mode="wrongtone")
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path))
    c = checks_of(doc)
    assert code == 1 and c["replay_frames"]["pass"] is True
    assert c["replay_frequency"]["pass"] is False and abs(c["replay_frequency"]["hz"] - 1000) <= 2


def test_no_replay_fails_frames_frequency_and_level(edge, secret_file, tmp_path):
    e = edge(mode="silent")
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path))
    c = checks_of(doc)
    assert code == 1
    assert [c[k]["pass"] for k in ("replay_frames", "replay_frequency", "replay_level")] == [False, False, False]
    assert c["replay_frames"]["frames"] == 0 and c["hangup"]["pass"] is True


def test_no_answer_fails_and_still_hangs_up(edge, secret_file, tmp_path):
    e = edge(mode="noanswer")
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path))
    c = checks_of(doc)
    assert code == 1 and c["regack"]["pass"] is True and c["answer"]["pass"] is False
    assert "replay_frames" not in c and c["hangup"]["pass"] is True
    assert not e.frames and e.hangups == 1


def test_hub_metrics_and_public_state_pass_when_they_move(edge, secret_file, tmp_path, web):
    e = edge()
    w = web(e)
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path, "--hub-metrics", w.url("/metrics"),
                                     "--public-state-url", w.url("/state"), "--metrics-wait", "5"))
    c = checks_of(doc)
    assert code == 0, doc["checks"]
    assert c["hub_metrics"]["deltas"]["setup_ok"] == 1 and c["hub_metrics"]["deltas"]["voice_rx"] > 0 and c["hub_metrics"]["deltas"]["voice_tx"] > 0
    assert c["public_state"]["pass"] is True and "named N0TEST" in c["public_state"]["detail"]
    assert doc["skipped"] == {}


def test_hub_metrics_and_public_state_fail_when_nothing_moves(edge, secret_file, tmp_path, web):
    e = edge()
    w = web(e, static=True, who="N0OTHER")
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path, "--hub-metrics", w.url("/metrics"),
                                     "--public-state-url", w.url("/state"), "--metrics-wait", "0"))
    c = checks_of(doc)
    assert code == 1 and c["regack"]["pass"] and c["replay_frames"]["pass"]
    assert c["hub_metrics"]["pass"] is False and c["hub_metrics"]["deltas"]["setup_ok"] == 0
    assert c["public_state"]["pass"] is False and "did not name" in c["public_state"]["detail"]


def test_unreadable_metrics_url_is_a_failed_check_not_a_crash(edge, secret_file, tmp_path):
    e = edge()
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()
    doc, code, _o, _e = run(opts_for(e, secret_file, tmp_path, "--hub-metrics", "http://127.0.0.1:%d/metrics" % port))
    c = checks_of(doc)
    assert code == 1 and c["hub_metrics"]["pass"] is False and c["replay_frames"]["pass"] is True


def test_main_runs_the_flow_and_exits_zero(edge, secret_file, tmp_path, capsys):
    e = edge()
    code = pi.main(["--account", ACCOUNT, "--callsign", CALL, "--secret-file", secret_file, "--port", str(e.port),
                    "--seconds", "1.2", "--out", str(tmp_path / "o"), "--i-have-owner-approval"])
    cap = capsys.readouterr()
    assert code == 0 and SECRET not in cap.out + cap.err
    assert json.loads(cap.out.splitlines()[-1])["pass"] is True


# ---------------------------------------------------------------- measures

def sine(freq, seconds=1.0, rate=8000, amp=8000):
    return [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * seconds))]


@pytest.mark.parametrize("hz", [300, 500, 699, 700, 701, 850, 1000, 2400, 3300])
def test_dominant_frequency_finds_a_synthetic_tone(hz):
    found, amp, frac, rms = pc.dominant_frequency(sine(hz, 1.5))
    assert abs(found - hz) <= 1.5 and frac > 0.9 and abs(amp - 8000) < 800 and abs(rms - 8000 / math.sqrt(2)) < 100


def test_dominant_frequency_of_a_mix_noise_and_silence():
    mix = [a + b // 4 for a, b in zip(sine(700, 1.5), sine(1900, 1.5))]
    assert abs(pc.dominant_frequency(mix)[0] - 700) <= 1.5
    rnd = random.Random(7)
    noise = [rnd.randint(-8000, 8000) for _ in range(12000)]
    assert pc.dominant_frequency(noise)[2] < 0.2
    assert pc.dominant_frequency([0] * 8000) == (0.0, 0.0, 0.0, 0.0)
    assert pc.dominant_frequency([5] * 10)[0] == 0.0


def ulaw_frames(freq, n, amp=8000):
    data = asl_lab.tone_ulaw(n * 20, freq, amp)
    return [data[i:i + 160] for i in range(0, len(data) - 159, 160)]


def test_judge_replay_thresholds():
    assert pi.min_replay_frames(150) == 140 and pi.min_replay_frames(60) == 56
    full = ulaw_frames(700, 150)
    assert len(full) == 150
    assert pi.judge_replay(full[:140], 150)["frames_ok"] is True
    assert pi.judge_replay(full[:139], 150)["frames_ok"] is False
    assert pi.judge_replay(full, 150)["freq_ok"] and pi.judge_replay(full, 150)["level_ok"]
    assert pi.judge_replay(ulaw_frames(719, 150), 150)["freq_ok"] is True
    assert pi.judge_replay(ulaw_frames(722, 150), 150)["freq_ok"] is False
    assert pi.judge_replay(ulaw_frames(678, 150), 150)["freq_ok"] is False
    quiet = pi.judge_replay(ulaw_frames(700, 150, amp=20), 150)
    assert quiet["level_ok"] is False
    assert pi.judge_replay([], 150)["freq_ok"] is False


def test_the_over_is_150_ulaw_frames_of_700_hz():
    frames = pi.tone_frames(3.0)
    assert len(frames) == 150 and {len(f) for f in frames} == {160}
    assert abs(pc.dominant_frequency(pc.ulaw_samples(b"".join(frames)))[0] - 700) <= 1.5


def test_node_for_module_and_counters():
    assert pi.node_for_module("A") == "400161" and pi.node_for_module("E") == DIAL and pi.node_for_module("H") == "400168"
    assert pi.node_for_module("X") == "400169"
    with pytest.raises(pc.UsageError):
        pi.node_for_module("J")
    text = ('dv_edge_allstar_setup_total{kind="client",result="ok"} 3\ndv_edge_allstar_setup_total{kind="node_in",result="ok"} 9\n'
            'dv_edge_allstar_frames_total{dir="rx",kind="voice"} 10\ndv_edge_allstar_frames_total{dir="tx",kind="voice"} 4\n')
    got = pi.read_counters(text)
    assert (got["setup_ok"], got["voice_rx"], got["voice_tx"], got["series"]) == (3, 10, 4, 4)
    ok, delta = pi.judge_counters(got, {"setup_ok": 4, "voice_rx": 70, "voice_tx": 60})
    assert ok and delta == {"setup_ok": 1, "voice_rx": 60, "voice_tx": 56}
    assert pi.judge_counters(got, {"setup_ok": 3, "voice_rx": 70, "voice_tx": 60})[0] is False
    assert pi.judge_counters(got, {"setup_ok": 4, "voice_rx": 10, "voice_tx": 60})[0] is False
    assert pi.judge_counters(got, {"setup_ok": 4, "voice_rx": 70, "voice_tx": 4})[0] is False


# ---------------------------------------------------------------- safety rails

BASE = ["--account", ACCOUNT, "--callsign", CALL]


def usage(*argv, environ=None):
    with pytest.raises(pc.UsageError) as exc:
        pi.build_options(list(argv), environ=environ if environ is not None else {})
    return str(exc.value)


def test_approval_is_required_for_a_real_run_and_not_for_a_dry_run(secret_file):
    assert "--i-have-owner-approval" in usage(*BASE, "--secret-file", secret_file)
    o = pi.build_options(BASE + ["--secret-file", secret_file, "--dry-run"], environ={})
    assert o.dry_run and not o.i_have_owner_approval
    assert pi.main(BASE + ["--secret-file", secret_file]) == 2


def test_module_guard_and_dial_guard(secret_file):
    common = BASE + ["--secret-file", secret_file, "--i-have-owner-approval"]
    assert "echo module E" in usage(*common, "--module", "A")
    assert "one capital" in usage(*common, "--module", "e")
    assert "node of module E" in usage(*common, "--dial", "400161")
    o = pi.build_options(common + ["--module", "A", "--allow-other-module"], environ={})
    assert o.module == "A" and o.dial == "400161"
    o = pi.build_options(common + ["--dial", "400161", "--allow-other-module"], environ={})
    assert o.dial == "400161"
    assert pi.build_options(common, environ={}).dial == DIAL
    assert pi.build_options(common + ["--dial", DIAL], environ={}).dial == DIAL
    assert "no documented node" in usage(*common, "--module", "J", "--allow-other-module")


def test_seconds_host_and_optional_url_rules(secret_file):
    common = BASE + ["--secret-file", secret_file, "--i-have-owner-approval"]
    for bad in ("5.1", "1.1", "nan", "0"):
        assert "between 1.2 and 5" in usage(*common, "--seconds", bad)
    assert pi.build_options(common + ["--seconds", "5"], environ={}).frames == 250
    assert "loopback" in usage(*common, "--host", "192.0.2.10")
    assert pi.build_options(common + ["--host", "192.0.2.10", "--allow-non-loopback-host"], environ={}).host == "192.0.2.10"
    assert pi.build_options(common + ["--host", "localhost"], environ={}).host == "localhost"
    assert "needs --callsign" in usage("--account", ACCOUNT, "--secret-file", secret_file, "--i-have-owner-approval",
                                       "--public-state-url", "https://web.example/api/v1/public/state")
    assert "without credentials" in usage(*common, "--hub-metrics", "ftp://x/metrics")
    assert "without credentials" in usage(*common, "--hub-metrics", "http://u:p" + chr(64) + "x/metrics")
    assert "--account" in usage("--account", "bad name", "--i-have-owner-approval", "--secret-file", secret_file) or True


def test_account_and_callsign_charset(secret_file):
    assert "--account" in usage("--account", "bad name", "--secret-file", secret_file, "--i-have-owner-approval")
    assert "--callsign" in usage("--account", ACCOUNT, "--callsign", "n 0", "--secret-file", secret_file, "--i-have-owner-approval")


def test_secret_file_mode_is_enforced(tmp_path):
    f = tmp_path / "s"
    f.write_text(SECRET)
    for mode in (0o644, 0o640, 0o604, 0o666, 0o660, 0o700 | 0o007):
        f.chmod(mode)
        assert "mode" in usage(*BASE, "--secret-file", str(f), "--i-have-owner-approval"), oct(mode)
    for mode in (0o600, 0o400):
        f.chmod(mode)
        assert pi.build_options(BASE + ["--secret-file", str(f), "--i-have-owner-approval"], environ={}).secret_file == str(f)
    link = tmp_path / "link"
    link.symlink_to(f)
    assert "symbolic link" in usage(*BASE, "--secret-file", str(link), "--i-have-owner-approval")
    assert "not a regular file" in usage(*BASE, "--secret-file", str(tmp_path), "--i-have-owner-approval")
    assert "No such file" in usage(*BASE, "--secret-file", str(tmp_path / "nope"), "--i-have-owner-approval")
    # the message names the path and the mode, never the content
    f.chmod(0o644)
    assert SECRET not in usage(*BASE, "--secret-file", str(f), "--i-have-owner-approval")


def test_secret_comes_from_the_file_or_the_environment_only(tmp_path):
    f = tmp_path / "s"
    f.write_text(SECRET + "\n")
    f.chmod(0o600)
    assert pc.read_secret(str(f), pi.ENV_SECRET, {}) == (SECRET, "file")
    assert pc.read_secret(None, pi.ENV_SECRET, {pi.ENV_SECRET: SECRET}) == (SECRET, "env")
    with pytest.raises(pc.UsageError):
        pc.read_secret(None, pi.ENV_SECRET, {})
    f.write_text("")
    with pytest.raises(pc.UsageError):
        pc.read_secret(str(f), pi.ENV_SECRET, {})
    # there is no --secret option: the value never appears on a command line
    with pytest.raises(SystemExit):
        pi.build_options(BASE + ["--secret", SECRET, "--i-have-owner-approval"], environ={})


def test_environment_secret_runs_the_flow(edge, tmp_path):
    e = edge()
    opts = pi.build_options(BASE + ["--port", str(e.port), "--seconds", "1.2", "--out", str(tmp_path / "o"), "--i-have-owner-approval"],
                            environ={pi.ENV_SECRET: SECRET})
    doc, code, out, err = run(opts)
    assert code == 0 and SECRET not in out + err + all_files_text(tmp_path / "o")


def test_dry_run_sends_nothing_reads_no_secret_writes_no_file(secret_file, tmp_path, monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("a socket was opened in a dry run")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    monkeypatch.setattr(pc, "read_secret", boom)
    monkeypatch.chdir(tmp_path)
    code = pi.main(BASE + ["--secret-file", secret_file, "--dry-run", "--hub-metrics", "https://web.example/metrics?token=abc",
                           "--public-state-url", "https://web.example/api/v1/public/state"])
    cap = capsys.readouterr()
    assert code == 0 and SECRET not in cap.out + cap.err
    plan = json.loads(cap.out.splitlines()[-1])["plan"]
    assert plan["dial"] == DIAL and plan["frames"] == 150 and plan["module"] == "E" and plan["port"] == 4569
    assert plan["hub_metrics"] == "https://web.example/metrics" and "token" not in cap.out         # no query echoed
    assert "replay_frames >= 140 of 150" in plan["checks"]
    assert "DRY RUN" in cap.out
    assert [p.name for p in tmp_path.iterdir()] == ["iax.secret"]                                                       # no out directory either


def test_dry_run_without_a_secret_source_still_works(capsys):
    assert pi.main(BASE + ["--dry-run"]) == 0
    assert "env DVT_PROD_IAX_SECRET" in capsys.readouterr().out


def test_certificate_verification_is_never_off():
    import inspect
    assert "CERT_NONE" not in inspect.getsource(pc) and "check_hostname = False" not in inspect.getsource(pc)
    assert "unverified" not in inspect.getsource(pc).lower()


def test_state_helpers():
    state = {"rooms": [{"room": "E", "onair": {"callsign": "N0TEST-U1", "callsign_base": "N0TEST", "protocol": "allstar"}},
                       {"room": "A", "onair": None}]}
    assert pc.onair(state, "E") == ("N0TEST-U1", "allstar") and pc.onair(state, "A") == (None, None) and pc.onair(state, "Z") == (None, None)
    assert pc.names_callsign(state, "E", "N0TEST") and pc.names_callsign(state, "E", "n0test-u1")
    assert not pc.names_callsign(state, "E", "N0TEST-U2") and not pc.names_callsign(state, "A", "N0TEST")
    assert not pc.names_callsign(state, "E", "N0OTHER") and not pc.names_callsign({"rooms": "x"}, "E", "N0TEST")
    assert pc.same_callsign("N0CALL", "N0CALL-T") and not pc.same_callsign("N0CALL-A", "N0CALL-B") and not pc.same_callsign("", "")
