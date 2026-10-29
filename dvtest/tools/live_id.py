# SPDX-License-Identifier: MIT
"""Send a Morse identification to a reflector module over DExtra (D-STAR).

The text (default "VVV DE {callsign} {module} {nonce}", fresh 4-digit
nonce per run) is keyed as a tone, encoded PCM -> AMBE+ by an AMBEd
transcoder and sent as one D-STAR transmission with a fresh random stream
id: link, talk, unlink. A listener decoding the same text proves the
transmission arrived complete on that module. --dry-run encodes (and, with
a transcoder, decodes the AMBE+ back locally) without linking or sending.

  python3 -m dvtest.tools.live_id --reflector-ip IP --reflector-name XLX999 \\
      --module E --callsign <yours> [--transcoder IP[:PORT] | IMAGE] [--dry-run]

--transcoder defaults to $TRANSCODER_IMAGE. An image is started with
docker (container dvt-live-<pid>, extra -e from DVT_TRANSCODER_ENV and
arguments from DVT_TRANSCODER_ARGS) and stopped at the end. The callsign
is never defaulted or stored.
"""
import argparse
import hashlib
import ipaddress
import os
import random
import re
import subprocess
import sys
import time

from ..audio import morse, signals
from ..proto import ambed, dextra

DEFAULT_TEXT = "VVV DE {callsign} {module} {nonce}"
_rng = random.SystemRandom()


def log(text):
    print("live-id: " + text, flush=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="live-id", description=__doc__.split("\n\n")[0])
    p.add_argument("--reflector-ip", help="reflector address (required unless --dry-run)")
    p.add_argument("--reflector-name", help="reflector callsign, e.g. XLX999 (required unless --dry-run)")
    p.add_argument("--module", required=True, help="module letter A-Z")
    p.add_argument("--callsign", required=True, help="your callsign (3-7 letters/digits)")
    p.add_argument("--text", default=DEFAULT_TEXT,
                   help="text to key; {callsign} {module} {nonce} are filled in (default %(default)r)")
    p.add_argument("--transcoder", default=os.environ.get("TRANSCODER_IMAGE"),
                   help="AMBEd transcoder ip[:port] or docker image (default $TRANSCODER_IMAGE)")
    p.add_argument("--wpm", type=int, default=20)
    p.add_argument("--freq", type=int, default=800, help="tone Hz")
    p.add_argument("--port", type=int, default=dextra.PORT, help="DExtra port")
    p.add_argument("--dry-run", action="store_true", help="encode and print, link and send nothing")
    args = p.parse_args(argv)
    args.callsign, args.module = args.callsign.upper(), args.module.upper()
    if not re.fullmatch(r"[A-Z0-9]{3,7}", args.callsign):
        p.error("--callsign must be 3-7 letters/digits")
    if not re.fullmatch(r"[A-Z]", args.module):
        p.error("--module must be one letter A-Z")
    if not args.dry_run:
        if not (args.reflector_ip and args.reflector_name):
            p.error("--reflector-ip and --reflector-name are required unless --dry-run")
        if not args.transcoder:
            p.error("--transcoder (or $TRANSCODER_IMAGE) is required unless --dry-run")
    return args


def _address(value):
    """(ip, port) when value is ip[:port], else None (an image name)."""
    host, _, port = value.partition(":")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host, int(port or ambed.PORT)


def _pong(addr, port, seconds):
    client = ambed.AmbedClient(addr, port, timeout=0.5)
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if client.rpc(ambed.ping("N0TEST")) == ambed.PONG:
                return True
        return False
    finally:
        client.close()


class Transcoder:
    """An AMBEd transcoder given by address, or a container of an image
    that lives for the with-block."""

    def __init__(self, spec):
        self.spec, self.name, self.addr = spec, None, _address(spec)

    def __enter__(self):
        if self.addr is None:
            self.name = "dvt-live-%d" % os.getpid()
            cmd = ["docker", "run", "-d", "--rm", "--name", self.name]
            for item in os.environ.get("DVT_TRANSCODER_ENV", "").split():
                cmd += ["-e", item]
            cmd += [self.spec] + os.environ.get("DVT_TRANSCODER_ARGS", "").split()
            run = subprocess.run(cmd, capture_output=True, text=True)
            if run.returncode != 0:
                raise RuntimeError("docker run %s failed: %s" % (self.spec, run.stderr.strip()))
            ip = subprocess.run(["docker", "inspect", "-f",
                                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", self.name],
                                capture_output=True, text=True).stdout.strip()
            self.addr = (ip, ambed.PORT)
            log("transcoder container %s (%s) at %s:%d" % (self.name, self.spec, *self.addr))
        if not _pong(*self.addr, 15):
            self.__exit__(None, None, None)
            raise RuntimeError("transcoder %s:%d does not answer AMBEDPING" % self.addr)
        return self

    def __exit__(self, *_exc):
        if self.name:
            subprocess.run(["docker", "stop", self.name], capture_output=True)
            self.name = None

    def transcode(self, codec_in, codec_out, frames):
        tx = _rng.randrange(1, 1 << 32)
        return ambed.transcode(self.addr[0], codec_in, [codec_out], frames, port=self.addr[1],
                               transaction=tx)[codec_out]


def run(args):
    """Encode and (unless dry run) send; returns what was sent."""
    nonce = "%04d" % _rng.randrange(10000)
    text = args.text.format(callsign=args.callsign, module=args.module, nonce=nonce)
    pcm = morse.encode(text, wpm=args.wpm, freq=args.freq)
    frames = signals.frames(pcm)
    sent = {"nonce": nonce, "text": text, "frames": len(frames), "stream_id": None}
    log("nonce %s" % nonce)
    log("text %r (%d wpm, %d Hz, %d frames, %.1f s)"
        % (text, args.wpm, args.freq, len(frames), len(frames) * 0.02))
    if not args.transcoder:
        log("PCM sha256 %s; no transcoder, dry run ends here" % hashlib.sha256(pcm).hexdigest())
        return sent
    with Transcoder(args.transcoder) as tc:
        ambe = tc.transcode(ambed.PCM, ambed.AMBEPLUS, frames)
        log("AMBE+ %d frames sha256 %s" % (len(ambe), signals.digest(ambe)))
        if args.dry_run:
            back = morse.decode(b"".join(tc.transcode(ambed.AMBEPLUS, ambed.PCM, ambe)))
            result = morse.check(text, back)
            log("loopback decode %r %s" % (back, "exact" if result["exact"] else "DIFF %s" % result["diff"]))
            sent["loopback"] = back
            log("dry run: nothing linked or sent")
            return sent
    client = dextra.DstarClient("0.0.0.0", callsign=args.callsign, module=args.module)
    try:
        if not client.link(args.reflector_ip, port=args.port):
            raise RuntimeError("no DExtra ACK from %s module %s" % (args.reflector_name, args.module))
        log("linked %s module %s as %s" % (args.reflector_name, args.module, args.callsign))
        sent["stream_id"] = _rng.randrange(1, 1 << 16)
        client.talk(ambe, sent["stream_id"], reflector=args.reflector_name)
        log("sent stream 0x%04x, %d frames" % (sent["stream_id"], len(ambe)))
    finally:
        if client.dest:
            client.unlink()
        client.close()
    log("unlinked")
    return sent


def main(argv=None):
    args = parse_args(argv)
    try:
        run(args)
    except (RuntimeError, OSError, ValueError) as exc:
        log("error: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
