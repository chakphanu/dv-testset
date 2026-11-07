# SPDX-License-Identifier: MIT
"""dvtest/tools/prod_listen.py: the parts that need no WebRTC stack (aiortc lives in the dvt-webload image):
RTP payload extraction, the audio measures, the options, the docker command line, the result document, and
that the dry run and the command printer touch no network. The live listen itself is NOT tested here."""
import json
import math
import socket
import ssl

import pytest

from dvtest import limits, webload as wl
from dvtest.tools import prod_listen as pl

URL = "https://web.example"


def ulaw_tone(freq, n=160, amp=8000):
    return bytes(wl.ulaw_encode(int(amp * math.sin(2 * math.pi * freq * i / 8000))) for i in range(n))


SILENT = bytes([wl.ulaw_encode(0)]) * 160


def rtp(payload, seq=1, pt=0, extra=b"", flags=0x80):
    return bytes([flags, pt, seq >> 8, seq & 255]) + bytes(8) + extra + payload


def opts(*extra):
    return pl.build_options(["--url", URL] + list(extra))


# ---------------------------------------------------------------- rtp and measures

def test_rtp_payload_plain_csrc_extension_padding():
    body = ulaw_tone(700)
    assert pl.rtp_payload(rtp(body)) == body
    assert pl.rtp_payload(rtp(body, flags=0x82, extra=bytes(8))) == body                       # two CSRC
    ext = bytes([0xBE, 0xDE, 0, 1]) + bytes(4)
    assert pl.rtp_payload(rtp(body, flags=0x90, extra=ext)) == body                            # one extension word
    assert pl.rtp_payload(rtp(body + bytes([0, 0, 3]), flags=0xA0)) == body                   # 3 bytes of padding
    assert pl.rtp_payload(rtp(body, pt=8)) is None                                             # not PCMU
    assert pl.rtp_payload(rtp(body, flags=0x40)) is None                                       # version 1
    assert pl.rtp_payload(b"\x80\x00") is None and pl.rtp_payload(rtp(b"")) is None


def test_analyse_tone_silence_and_gaps():
    tone, quiet = ulaw_tone(700), SILENT
    arrivals = [(1.0 + 0.02 * i, tone) for i in range(50)] + [(2.5 + 0.02 * i, quiet) for i in range(50)]
    st = pl.analyse(arrivals)
    assert st["packets"] == 100 and st["bytes"] == 16000
    assert st["silence_ratio"] == pytest.approx(0.5)
    assert st["first_audio_s"] == pytest.approx(1.0) and st["first_packet_s"] == pytest.approx(1.0)
    # rms of an 8000 amplitude sine is about 5657; half the packets are silent
    assert st["rms"] == pytest.approx(5657 / math.sqrt(2), rel=0.05)
    assert 7000 < st["peak"] < 8400
    assert st["max_gap_ms"] == pytest.approx(520, abs=2)            # 1.98 -> 2.5 s: the gap between the two blocks
    assert [row["s"] for row in st["timeline"]] == [1, 2, 3]
    assert st["timeline"][0]["packets"] + st["timeline"][1]["packets"] + st["timeline"][2]["packets"] == 100
    assert st["timeline"][0]["rms"] > 4000 and st["timeline"][2]["rms"] < 1


def test_analyse_nothing_and_only_silence():
    st = pl.analyse([])
    assert st["packets"] == 0 and st["silence_ratio"] == 1.0 and st["first_audio_s"] is None and st["rms"] == 0.0
    st = pl.analyse([(0.1 * i, SILENT) for i in range(10)])
    assert st["silence_ratio"] == 1.0 and st["first_audio_s"] is None and st["peak"] <= 8
    st = pl.analyse([(0.0, ulaw_tone(700, amp=100))], silence_rms=500.0)      # quiet but above zero: silence under a high threshold
    assert st["silence_ratio"] == 1.0
    assert pl.analyse([(0.0, ulaw_tone(700, amp=100))], silence_rms=10.0)["silence_ratio"] == 0.0


def test_result_document_contract_and_json():
    o = opts("--room", "E", "--seconds", "20")
    doc = pl.result_doc(o, pl.analyse([(0.5, ulaw_tone(700))]), connected=True, connect_s=1.2)
    assert list(doc)[:8] == ["packets", "bytes", "rms", "peak", "silence_ratio", "first_audio_s", "connected", "error"]
    assert doc["connected"] is True and doc["error"] is None and doc["packets"] == 1 and doc["seconds"] == 20.0
    json.dumps(doc)
    bad = pl.result_doc(o, error="boom")
    assert bad["connected"] is False and bad["packets"] == 0 and bad["silence_ratio"] == 1.0 and bad["error"] == "boom"


def test_parse_state():
    body = {"info": {"guest_listen": True, "rooms": [{"room": "E", "name": None}, {"room": "A", "name": "x"}]}}
    assert pl.parse_state(body) == (True, ["E", "A"])
    assert pl.parse_state({"info": {"guest_listen": False}}) == (False, [])
    assert pl.parse_state({"info": {}}) == (None, [])
    assert pl.parse_state([1]) == (None, []) and pl.parse_state({"info": "x"}) == (None, [])


# ---------------------------------------------------------------- options, docker line, safety

def usage(*argv):
    with pytest.raises(pl.UsageError) as exc:
        pl.build_options(list(argv))
    return str(exc.value)


def test_options():
    o = opts()
    assert (o.url, o.room, o.seconds, o.ca_file) == (URL, "E", pl.DEFAULT_SECONDS, None)
    assert pl.build_options(["--url", "https://web.example:8443/"]).url == "https://web.example:8443"
    assert "https only" in usage("--url", "http://web.example")
    assert "https only" in usage("--url", "https://u:p" + chr(64) + "web.example")
    assert "https only" in usage("--url", "https://web.example/login")
    assert "one capital" in usage("--url", URL, "--room", "e")
    assert "between 1 and" in usage("--url", URL, "--seconds", "901")
    assert "between 1 and" in usage("--url", URL, "--seconds", "nan")
    assert "not a file" in usage("--url", URL, "--ca-file", "/nonexistent-ca")


def test_certificate_verification_is_always_on():
    client = pl.make_client(opts())
    assert client._ctx.verify_mode == ssl.CERT_REQUIRED and client._ctx.check_hostname is True
    assert not [a for a in pl.build_options.__code__.co_consts if isinstance(a, str) and "insecure" in a.lower()]
    cafile = ssl.get_default_verify_paths().cafile
    if cafile:
        client = pl.make_client(opts("--ca-file", cafile))
        assert client._ctx.verify_mode == ssl.CERT_REQUIRED and client._ctx.check_hostname is True


def test_docker_command_is_contained():
    env = {"DVT_CTR_MEM": "512m", "DVT_CTR_CPUS": "1", "DVT_CTR_PIDS": "128"}
    words = pl.docker_command(opts("--seconds", "40"), env)
    line = " ".join(words)
    assert words[:3] == ["docker", "run", "--rm"]
    for need in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--memory 512m", "--memory-swap 512m",
                 "--cpus 1", "--pids-limit 128", "--tmpfs /tmp:rw,noexec,nosuid,size=16m", "-v $PWD/dvtest:/app/dvtest:ro",
                 "--entrypoint python dvt-webload:local -m dvtest.tools.prod_listen", "--url " + URL, "--room E", "--seconds 40"):
        assert need in line, need
    assert " ".join(limits.docker_run_args(env)) in line
    assert "--network" not in line and "--privileged" not in line and "--ca-file" not in line
    assert "--ca-file /ca.pem" in " ".join(pl.docker_command(opts("--ca-file", __file__), env))
    assert '"$PWD/dvtest:/app/dvtest:ro"' in pl.shell_line(words)


def test_dry_run_and_print_docker_touch_no_network(monkeypatch, capsys):
    def boom(name):
        def inner(*a, **k):
            raise AssertionError("network touched: " + name)
        return inner
    for name in ("socket", "create_connection", "gethostbyname", "getaddrinfo"):
        monkeypatch.setattr(socket, name, boom(name))
    assert pl.main(["--url", URL, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "POST /api/v1/rtc/offer" in out and "system roots" in out and "docker run" in out
    assert pl.main(["--url", URL, "--print-docker"]) == 0
    assert capsys.readouterr().out.startswith("docker run --rm --read-only --cap-drop ALL")
    assert pl.main(["--url", "http://web.example"]) == 2
    assert "https only" in capsys.readouterr().err


def test_main_prints_one_json_line_and_exit_code(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(pl, "make_client", lambda o: object())
    monkeypatch.setattr(pl, "preflight", lambda c: (True, ["E"]))
    seen = {}

    async def fake_listen(o, client, state):
        seen["state"] = state
        return pl.result_doc(o, pl.analyse([(0.2, ulaw_tone(700))]), connected=True, guest_listen=state[0])

    monkeypatch.setattr(pl, "listen", fake_listen)
    path = tmp_path / "r.json"
    assert pl.main(["--url", URL, "--json-out", str(path)]) == 0
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc["connected"] and doc["packets"] == 1 and doc["guest_listen"] is True and seen["state"] == (True, ["E"])
    assert json.loads(path.read_text()) == doc

    async def failing(o, client, state):
        raise ImportError("No module named 'aiortc'")

    monkeypatch.setattr(pl, "listen", failing)
    assert pl.main(["--url", URL]) == 1
    doc = json.loads(capsys.readouterr().out.strip())
    assert doc["connected"] is False and "aiortc is not installed" in doc["error"] and doc["packets"] == 0
