# SPDX-License-Identifier: MIT
"""dvtest.audio.tandem against two FakeAmbed transcoders on loopback
(127.0.0.3 ref, 127.0.0.4 sut, client on 127.0.0.2). FakeAmbed answers
fixed payloads, so the decoder is injected: it returns the keyed ID for a
"good" side and silence for a "bad" one (probe decodes ref, then sut)."""
import socket
import types

import pytest

from dvtest.audio import morse, tandem
from dvtest.proto import ambed

TEXT = "T 42"                      # short ID keeps the loopback streams fast
SILENCE = bytes(16000)
FAST = {"bind_ip": "127.0.0.2", "gap": 0.001}


def _free_run(n=48, ips=("127.0.0.2", "127.0.0.3", "127.0.0.4")):
    """First port of n consecutive ports free on every ip (FakeAmbed hands
    out data ports upward from a base)."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.3", 0))
            base = s.getsockname()[1]
        if base + n > 65535:
            continue
        socks = []
        try:
            for port in range(base, base + n):
                for ip in ips:
                    socks.append(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                    socks[-1].bind((ip, port))
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError("no free port run")


@pytest.fixture(scope="module")
def rig():
    base = _free_run()
    fakes = {side: ambed.FakeAmbed(ip, port=base, data_port_base=base + 1 + i * 23)
             for i, (side, ip) in enumerate((("ref", "127.0.0.3"), ("sut", "127.0.0.4")))}
    try:
        yield types.SimpleNamespace(ambed_ref="127.0.0.3", ambed_sut="127.0.0.4", ambed_port=base,
                                    fakes=fakes)
    finally:
        for fake in fakes.values():
            fake.stop()


def run_probe(rig, ref_ok, sut_ok, codec_in=ambed.IMBE, codec_out=ambed.CODEC2_3200):
    """probe() with a decoder that is good or bad per side (ref call first)."""
    calls = []
    outputs = iter([ref_ok, sut_ok])

    def decode(codec, frames):
        calls.append((codec, frames))
        return morse.encode(TEXT, wpm=tandem.WPM) if next(outputs) else SILENCE
    result = tandem.probe(rig, codec_in, codec_out, decode, text=TEXT, **FAST)
    assert [c for c, _ in calls] == [codec_out, codec_out]
    assert all(f and set(f) == {ambed.fake_payload(codec_out)} for _, f in calls)
    return result


def test_probe_uses_each_sides_own_transcoder(rig):
    before = {s: len(f.opens) for s, f in rig.fakes.items()}
    assert run_probe(rig, True, False) == {"ref": True, "sut": False}
    ref = [o for _, o, _ in rig.fakes["ref"].opens[before["ref"]:]]
    sut = [o for _, o, _ in rig.fakes["sut"].opens[before["sut"]:]]
    assert [(o.codec_in, o.outs, o.transaction) for o in ref] == [
        (ambed.PCM, (ambed.IMBE,), tandem.TRANSACTION + 0x200 + ambed.IMBE),
        (ambed.IMBE, (ambed.CODEC2_3200,), tandem.TRANSACTION + 0x300 + ambed.IMBE)]
    assert [(o.codec_in, o.outs, o.callsign) for o in sut] == [
        (ambed.IMBE, (ambed.CODEC2_3200,), b"N0TEST  ")]


def test_both_probe_both_exact_pass(rig):
    probed = run_probe(rig, True, True)
    good = morse.encode(TEXT, wpm=tandem.WPM)
    verdict, record = tandem.judge(TEXT, {"ref": good, "sut": good}, probed)
    assert verdict == "pass"
    assert record == {"probe": {"ref": True, "sut": True},
                      "ref": {"got": TEXT, "exact": True}, "sut": {"got": TEXT, "exact": True}}


def test_failed_probe_side_not_required(rig):
    probed = run_probe(rig, True, False)
    verdict, record = tandem.judge(TEXT, {"ref": morse.encode(TEXT), "sut": SILENCE}, probed)
    assert verdict == "pass"
    assert record["sut"] == {"got": "", "exact": False} and record["probe"]["sut"] is False


def test_probe_passing_side_must_decode_exactly(rig):
    probed = run_probe(rig, True, True)
    verdict, record = tandem.judge(TEXT, {"ref": morse.encode(TEXT), "sut": "T 4"}, probed)
    assert verdict.startswith("fail: sut decoded 'T 4'") and "ref" not in verdict.split(":", 1)[1]
    assert record["ref"]["exact"] and not record["sut"]["exact"]


def test_no_probe_passes_is_xfail(rig):
    probed = run_probe(rig, False, False)
    verdict, record = tandem.judge(TEXT, {"ref": SILENCE, "sut": "T"}, probed)
    assert verdict.startswith("xfail: ") and "probe ref False sut False" in verdict
    assert record["probe"] == {"ref": False, "sut": False} and record["sut"]["got"] == "T"


def test_default_decoder_via_reference_transcoder(rig, monkeypatch):
    monkeypatch.setattr(tandem.shutil, "which", lambda name: None)
    decode = tandem.decoder(rig, **FAST)
    pcm = decode(ambed.CODEC2_3200, [ambed.fake_payload(ambed.CODEC2_3200)] * 3)
    assert pcm == ambed.fake_payload(ambed.PCM) * 3
    assert rig.fakes["ref"].opens[-1][1].transaction == tandem.TRANSACTION + 0x100 + ambed.CODEC2_3200
