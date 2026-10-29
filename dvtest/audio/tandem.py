# SPDX-License-Identifier: MIT
"""Per-side tandem probe and verdict for Morse cells that cross a
transcoder (ref and sut each behind their own xlxd and transcoder).

probe(): a 12 wpm Morse ID ("PARIS nnnn", 800 Hz) is encoded once by
transcoder-ref (PCM -> codec_in), converted on each side's own transcoder
(codec_in -> codec_out, the conversion its xlxd asks for), decoded to PCM
by the decode callable and Morse-decoded: {"ref": exact, "sut": exact}.

judge(): every side whose probe passed must decode the case's call
exactly; while no side passes the probe the cell is informational
(xfail). Returns (verdict, record); verdict is "pass", "fail: ..." or
"xfail: ...", record is {"probe": {"ref", "sut"}, "ref": {"got", "exact"},
"sut": {"got", "exact"}}. Tandem paths (two MBE hops) default to 12 wpm:
each hop lengthens key-down, so at 20 wpm the 60 ms gaps close after the
second hop for every codec; single-hop cases keep morse's 20 wpm. The
case encodes its own call with encode() so probe and call use one speed.
Nothing here imports pytest; a case does:

    dec = tandem.decoder(rig)
    probe = tandem.probe(rig, ambed.IMBE, ambed.CODEC2_3200, dec)
    got = both(...)                                  # the rig call per side
    verdict, rec = tandem.judge(text, {n: dec(ambed.CODEC2_3200, v["frames"])
                                       for n, v in got.items()}, probe)
    save(label, {"sent": text, **rec})
    if verdict.startswith("xfail"):
        pytest.xfail("%s %s" % (label, verdict))
    assert verdict == "pass", "%s %s" % (label, verdict)
"""
import random
import shutil
import subprocess

from dvtest.audio import morse, signals
from dvtest.proto import ambed

WPM = 12                          # tandem default; single-hop cases use 20
FREQ = 800
SIDES = ("ref", "sut")
TRANSACTION = 0x544E0000          # "TN"; + 0x100 decode, 0x200 encode, 0x300 convert


def id_text():
    """Probe ID text, "PARIS nnnn"."""
    return "PARIS %04d" % random.SystemRandom().randrange(10000)


def decoder(rig, transaction=TRANSACTION, bind_ip="0.0.0.0", gap=0.02):
    """decode(codec, frames) -> PCM bytes: Codec2 3200 by c2dec when it is
    installed, everything else (and Codec2 without c2dec) by
    transcoder-ref."""
    def decode(codec, frames):
        if codec == ambed.CODEC2_3200 and shutil.which("c2dec"):
            return subprocess.run(["c2dec", "3200", "-", "-"], input=b"".join(frames),
                                  capture_output=True, check=True).stdout
        out = ambed.transcode(rig.ambed_ref, codec, [ambed.PCM], frames, port=rig.ambed_port,
                              transaction=transaction + 0x100 + codec, bind_ip=bind_ip, gap=gap)
        return b"".join(out[ambed.PCM])
    return decode


def encode(text, wpm=WPM, farnsworth=None):
    """The case's call as 320 B PCM frames at the tandem speed."""
    return signals.frames(morse.encode(text, wpm=wpm, freq=FREQ, farnsworth=farnsworth))


def probe(rig, codec_in, codec_out, decode, text=None, transaction=TRANSACTION,
          bind_ip="0.0.0.0", gap=0.02, wpm=WPM, farnsworth=None):
    """Does each side's own transcoder carry a Morse ID through codec_in ->
    codec_out? {"ref": bool, "sut": bool}. decode is called once per side,
    ref first. Raises RuntimeError when a transcoder refuses the stream."""
    text = text or id_text()
    pcm = encode(text, wpm, farnsworth)
    frames = ambed.transcode(rig.ambed_ref, ambed.PCM, [codec_in], pcm, port=rig.ambed_port,
                             transaction=transaction + 0x200 + codec_in,
                             bind_ip=bind_ip, gap=gap)[codec_in]
    out = {}
    for side in SIDES:
        conv = ambed.transcode(getattr(rig, "ambed_" + side), codec_in, [codec_out], frames,
                               port=rig.ambed_port, transaction=transaction + 0x300 + codec_in,
                               bind_ip=bind_ip, gap=gap)[codec_out]
        out[side] = morse.check(text, morse.decode(decode(codec_out, conv), wpm=wpm))["exact"]
    return out


def judge(text, decoded, probed, wpm=WPM):
    """(verdict, record) of the case's decoded output per side ({side: PCM
    bytes or already decoded text}) against text, gated by probed (the probe() result)."""
    checks = {}
    for side in SIDES:
        got = decoded[side]
        checks[side] = morse.check(text, got if isinstance(got, str) else morse.decode(got, wpm=wpm))
    record = {"probe": {s: bool(probed[s]) for s in SIDES},
              **{s: {"got": checks[s]["got"], "exact": checks[s]["exact"]} for s in SIDES}}
    bad = [s for s in SIDES if probed[s] and not checks[s]["exact"]]
    if bad:
        verdict = "fail: " + "; ".join("%s decoded %r, sent %r, although its transcoder passes the probe"
                                       % (s, checks[s]["got"], checks[s]["expected"]) for s in bad)
    elif not any(probed[s] for s in SIDES):
        verdict = "xfail: informational, no transcoder carries Morse through this tandem " \
                  "(probe ref %s sut %s); rig decode %s" % (
                      probed["ref"], probed["sut"], {s: checks[s]["got"] for s in SIDES})
    else:
        verdict = "pass"
    return verdict, record
