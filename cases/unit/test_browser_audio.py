# SPDX-License-Identifier: MIT
"""The audio judgement of the WebRTC browser cases (dvtest/browser.py): the Goertzel scan on synthetic
data, the same algorithm as the JavaScript that runs in the page (executed with node when it is on the
host), the fake microphone file, the launch options and the getStats verdicts."""
import json
import math
import random
import shutil
import struct
import subprocess
import wave
import io

import pytest

from dvtest import browser as b

pytestmark = pytest.mark.unit


def sine(freq, rate, seconds, amp=0.5, phase=0.0):
    return [amp * math.sin(2 * math.pi * freq * i / rate + phase) for i in range(int(rate * seconds))]


@pytest.mark.parametrize("rate", [8000, 48000])
@pytest.mark.parametrize("freq", [300, 700, 1235, 3000])
def test_scan_finds_the_tone(rate, freq):
    peak, amp, frac, level = b.scan_peak(sine(freq, rate, 0.25), rate, step=5.0)
    assert abs(peak - freq) <= 5.0
    assert amp == pytest.approx(0.5, rel=0.2)
    assert frac > 0.9
    assert level == pytest.approx(0.5 / math.sqrt(2), rel=0.01)


def test_amplitude_reads_the_sine_amplitude():
    x = sine(700, 8000, 1.0, amp=0.25)
    assert b.goertzel_amplitude(x, 8000, 700) == pytest.approx(0.25, rel=0.01)
    assert b.goertzel_amplitude(x, 8000, 1500) < 0.002


def test_two_tones_the_louder_wins():
    x = [a + 0.3 * c for a, c in zip(sine(700, 8000, 1.0), sine(1900, 8000, 1.0))]
    assert abs(b.scan_peak(x, 8000)[0] - 700) <= 10


def test_silence_and_noise_are_not_a_tone():
    assert b.scan_peak([0.0] * 8000, 8000) == (0.0, 0.0, 0.0, 0.0)
    assert b.scan_peak([], 8000) == (0.0, 0.0, 0.0, 0.0)
    rng = random.Random(7)
    noise = [rng.uniform(-0.3, 0.3) for _ in range(16000)]
    assert b.scan_peak(noise, 8000)[2] < 0.1          # most of the power is not in one bin


def test_tone_with_noise_still_found():
    rng = random.Random(3)
    x = [v + rng.uniform(-0.05, 0.05) for v in sine(700, 8000, 1.0)]
    peak, _a, frac, _l = b.scan_peak(x, 8000)
    assert abs(peak - 700) <= 10 and frac > 0.8


def test_wav_is_a_16_bit_mono_sine():
    data = b.tone_wav_bytes(700.0, 1.0, rate=48000)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 48000, 48000)
        pcm = w.readframes(w.getnframes())
    samples = [v / 32768.0 for v in struct.unpack("<%dh" % 48000, pcm)]
    assert abs(b.scan_peak(samples[:24000], 48000, step=2.0)[0] - 700) <= 2.0
    assert max(samples) < 0.51 and min(samples) > -0.51


def test_launch_options_per_engine():
    chromium = b.launch_options("chromium", "/media/x.wav")
    assert "--use-fake-device-for-media-stream" in chromium["args"]
    assert "--use-fake-ui-for-media-stream" in chromium["args"]
    assert "--use-file-for-fake-audio-capture=/media/x.wav" in chromium["args"]
    assert b.launch_options("firefox")["firefoxUserPrefs"]["media.navigator.streams.fake"] is True
    assert b.launch_options("webkit") == {}


def test_launch_options_url():
    assert b.with_launch_options("ws://h:3000/", {}) == "ws://h:3000/"
    url = b.with_launch_options("ws://h:3000/", {"args": ["--a=b c"]})
    assert url.startswith("ws://h:3000/?launch-options=")
    from urllib.parse import parse_qs, urlsplit
    assert json.loads(parse_qs(urlsplit(url).query)["launch-options"][0]) == {"args": ["--a=b c"]}
    assert "&launch-options=" in b.with_launch_options("ws://h:3000/?browser=x", {"a": 1})


def snap(t, packets, nbytes, lost=0):
    return {"t": t, "packetsReceived": packets, "bytesReceived": nbytes, "packetsLost": lost}


def test_inbound_verdict_good_20ms_pcmu():
    growth, problems = b.inbound_verdict(snap(0, 100, 16000), snap(2000, 200, 32000))
    assert problems == []
    assert growth["packets_per_s"] == 50.0 and growth["bytes_per_packet"] == 160.0 and growth["interval_ms"] == 20.0


def test_inbound_verdict_problems():
    _g, p = b.inbound_verdict(snap(0, 100, 16000), snap(2000, 100, 16000))
    assert p and "no packets" in p[0]
    _g, p = b.inbound_verdict(snap(0, 100, 16000), snap(2000, 150, 24000))       # 25 packets/s
    assert any("packets/s" in x for x in p)
    _g, p = b.inbound_verdict(snap(0, 0, 0), snap(2000, 100, 16000, lost=20))
    assert any("packetsLost" in x for x in p)


def test_summarize_stats():
    entries = [
        {"id": "I1", "type": "inbound-rtp", "kind": "audio", "codecId": "C1", "packetsReceived": 5},
        {"id": "C1", "type": "codec", "mimeType": "audio/PCMU"},
        {"id": "O1", "type": "outbound-rtp", "mediaType": "audio", "codecId": "C2"},
        {"id": "C2", "type": "codec", "mimeType": "audio/PCMA"},
        {"id": "P1", "type": "candidate-pair", "state": "succeeded", "nominated": True},
    ]
    s = b.summarize_stats(entries)
    assert s["inbound"]["packetsReceived"] == 5 and s["codec_in"] == "audio/PCMU"
    assert s["codec_out"] == "audio/PCMA" and s["pair"]["id"] == "P1"
    assert b.summarize_stats([]) == {"inbound": None, "outbound": None, "codec_in": None, "codec_out": None, "pair": None}


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed (the page JavaScript is checked against the Python reference with it)")
@pytest.mark.parametrize("rate,freq", [(8000, 700), (48000, 700), (44100, 1234)])
def test_page_javascript_agrees_with_the_python_reference(rate, freq):
    rng = random.Random(freq)
    x = [v + rng.uniform(-0.02, 0.02) for v in sine(freq, rate, 0.25)]
    py = b.scan_peak(x, rate, 200.0, 3600.0, 10.0)
    script = (
        "globalThis.window = globalThis; globalThis.performance = {now: () => 0};\n"
        "globalThis.RTCPeerConnection = class {};\n"
        "globalThis.document = {addEventListener() {}};\n" + b.RTC_PROBE_JS +
        "\nconst x = JSON.parse(process.argv[1]);\n"
        "console.log(JSON.stringify(window.__dvtTap.analyseSamples(x, %d, 200, 3600, 10)));\n" % rate)
    script = script.replace("process.argv[1]", "require('fs').readFileSync(0, 'utf8')")
    out = subprocess.run([NODE, "-e", script], input=json.dumps(x), capture_output=True, text=True, timeout=60, check=True)
    js = json.loads(out.stdout)
    assert js["peak_hz"] == py[0]
    assert js["peak_amp"] == pytest.approx(py[1], rel=1e-3)
    assert js["fraction"] == pytest.approx(py[2], rel=1e-3)
    assert js["rms"] == pytest.approx(py[3], rel=1e-3)


def test_no_non_ascii_in_the_probe():
    assert b.RTC_PROBE_JS.isascii()


def test_probe_records_sent_and_received_channel_frames():
    js = b.RTC_PROBE_JS
    assert "tap.sent.push" in js and "tap.msgs.push" in js
    assert "pc.addEventListener('datachannel'" in js and "watch(e.channel)" in js


def _ack(t, frame=None, label="ctl"):
    return {"t": t, "pc": 0, "label": label, "data": json.dumps(frame or b.RENEWED)}


def test_renewal_verdict_counts_and_spaces_the_acks():
    msgs = [_ack(0.0, {"op": "selected", "room": "Y"}), _ack(100.0, {"op": "ptt", "state": "granted", "code": "ok"})]
    msgs += [_ack(1000.0 * i + 30.0) for i in range(1, 10)]                       # one per lease, 1 s apart
    msgs.append(_ack(5000.0, label="other"))                                     # another channel: ignored
    msgs.append({"t": 5100.0, "label": "ctl", "data": "not json"})
    v = b.renewal_verdict(msgs)
    assert v["acks"] == 9 and v["problems"] == [] and v["min_interval_ms"] == 1000.0
    assert v["other_ptt"] == ["granted/ok"]
    assert b.renewal_verdict(msgs, since_ms=2500.0, until_ms=6100.0)["acks"] == 4


def test_renewal_verdict_flags_a_flood_and_odd_frames():
    close = [_ack(0.0), _ack(460.0), _ack(800.0)]                                # 460 passes (tolerance), 340 does not
    v = b.renewal_verdict(close)
    assert v["acks"] == 3 and len(v["problems"]) == 1 and "340.0" in v["problems"][0]
    odd = b.renewal_verdict([_ack(0.0, {"op": "ptt", "state": "renewed", "code": "ok", "x": 1})])
    assert odd["acks"] == 0 and odd["odd_acks"] == 1 and odd["problems"]
    assert b.ctl_frames([{"t": 1, "label": "ctl", "data": "[1]"}]) == []         # not an object


def test_usrp_check_leaves_webrtc_media_alone_in_the_web_profile(monkeypatch):
    from types import SimpleNamespace
    from dvtest.check import usrp
    stun = SimpleNamespace(payload=b"\x00\x01\x00\x00", sport=50332, dport=50000)
    hdr = SimpleNamespace(payload=b"USRP" + bytes(40), sport=50332, dport=50000)
    chk = usrp.Checker(report=None, endpoints=[(("172.30.0.30", 50000), ("172.30.0.11", 50000))])
    monkeypatch.delenv("DVT_WEB", raising=False)
    assert chk.claims(stun) and chk.claims(hdr)            # the rig's own USRP port: as before
    monkeypatch.setenv("DVT_WEB", "on")
    assert not chk.claims(stun) and chk.claims(hdr)


# ---- NAT stage: selected candidate pair, arrival deviations ----

def test_selected_pair_prefers_the_transport_choice():
    entries = [
        {"id": "T", "type": "transport", "selectedCandidatePairId": "P2"},
        {"id": "P1", "type": "candidate-pair", "state": "succeeded", "nominated": True, "localCandidateId": "L1", "remoteCandidateId": "R1"},
        {"id": "P2", "type": "candidate-pair", "state": "succeeded", "nominated": True, "localCandidateId": "L2", "remoteCandidateId": "R1"},
        {"id": "L1", "type": "local-candidate", "candidateType": "host", "protocol": "udp"},
        {"id": "L2", "type": "local-candidate", "candidateType": "relay", "protocol": "udp", "relayProtocol": "udp"},
        {"id": "R1", "type": "remote-candidate", "candidateType": "host"},
    ]
    got = b.selected_pair(entries)
    assert got["local"] == "relay" and got["remote"] == "host" and got["via"] == "transport" and got["relay_protocol"] == "udp"
    # no transport entry: the nominated pair
    got = b.selected_pair([e for e in entries if e["type"] != "transport"])
    assert got["via"] == "nominated" and got["local"] == "host"
    assert b.selected_pair([]) is None
    assert b.selected_pair([{"id": "P", "type": "candidate-pair", "state": "failed"}]) is None


def _samples(times_ms, with_ts=True, base=1000.0, start_rtp=0):
    return [(base + t + 0.3, (start_rtp + 160 * i) % (1 << 32), 5000.0 + t if with_ts else None) for i, t in enumerate(times_ms)]


def test_arrival_deviations_of_a_perfect_and_a_jittery_stream():
    perfect = [i * 20.0 for i in range(200)]
    got = b.arrival_deviations(_samples(perfect))
    assert got["max"] == 0.0 and got["p99"] == 0.0 and got["n"] == 199 and got["source"] == "engine receive time"
    jit = list(perfect)
    jit[100] += 35.0                           # one late packet: two intervals deviate by 35 and 15 (the next one is early)
    got = b.arrival_deviations(_samples(jit))
    assert got["max"] == 35.0 and got["p50"] == 0.0
    # without engine receive times the sampler's time is used
    assert b.arrival_deviations(_samples(perfect, with_ts=False))["source"] == "sample time"


def test_arrival_deviations_skip_lost_packets_and_survive_the_rtp_wrap():
    times = [i * 20.0 for i in range(60)]
    samples = _samples(times, start_rtp=(1 << 32) - 160 * 30)        # the RTP timestamp wraps in the middle
    assert b.arrival_deviations(samples)["n"] == 59
    # packet 30 never came: the pair around the hole is not an interval of one packet
    samples = samples[:30] + samples[31:]
    got = b.arrival_deviations(samples)
    assert got["n"] == 57 and got["max"] == 0.0
    assert b.arrival_deviations(samples[:10]) is None
    assert b.arrival_deviations([]) is None
