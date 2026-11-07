# SPDX-License-Identifier: MIT
"""One anonymous WebRTC Listen on a public DV-Router web service (docs/prod-checks.md, "Public web listen").

It does what the dashboard's Listen button does for a guest (web.md, "Public Listen and Talk panel"): a
receive-only audio transceiver, POST /api/v1/rtc/offer {sdp, room} without any login, apply the answer,
renew the circuit with POST /api/v1/rtc/alive (at once, then every second until six, then every 10 s), close it
with POST /api/v1/rtc/close at the end. The audio is PCMU, 20 ms per packet; every packet is decoded here and
measured. The lead starts it a few seconds before the echo over of dvtest.tools.prod_echo and reads the JSON
line it prints:

    {packets, bytes, rms, peak, silence_ratio, first_audio_s, connected, error, ...}

Certificate verification is always ON (the system roots, or only --ca-file for the lab CA). Never run it
anywhere but where the owner agreed; it is ONE listener for --seconds (at most 900), nothing else.

The WebRTC stack is aiortc, which lives only in the image dvt-webload:local (rig/Dockerfile.webload); the
module imports it lazily, so everything but run() works with the standard library alone (unit tests:
cases/unit/test_prod_listen.py). The signalling and the receive hooks are the ones of dvtest.tools.webload_gen.
"""
import argparse
import asyncio
import json
import math
import os
import re
import shlex
import sys
import time
import urllib.parse

from dvtest import limits, webload as wl
from dvtest.web import WebClient, WebError

OFFER_PATH = "/api/v1/rtc/offer"
ALIVE_PATH = "/api/v1/rtc/alive"
CLOSE_PATH = "/api/v1/rtc/close"
STATE_PATH = "/api/v1/public/state"
DEFAULT_SECONDS = 15.0
MAX_SECONDS = 900.0
CONNECT_TIMEOUT_S = 20.0
SILENCE_RMS = 30.0              # a 20 ms frame with an rms (16 bit units) under this is silence (about -60 dBFS)
FRAME_S = 0.020
MAX_PACKETS = 60000
USER_AGENT = "dvt-prod-listen/1"
IMAGE = "dvt-webload:local"
ULAW = [wl.ulaw_decode(code) for code in range(256)]


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- pure parts

def rtp_payload(pkt):
    """The payload of one decrypted RTP packet (version 2, payload type 0 = PCMU), or None: CSRC list,
    header extension and padding are skipped as RFC 3550 says."""
    pkt = bytes(pkt)
    if len(pkt) < 12 or pkt[0] >> 6 != 2 or (pkt[1] & 0x7F) != 0:
        return None
    b0 = pkt[0]
    start = 12 + 4 * (b0 & 0x0F)
    if b0 & 0x10:
        if len(pkt) < start + 4:
            return None
        start += 4 + 4 * ((pkt[start + 2] << 8) | pkt[start + 3])
    end = len(pkt) - (pkt[-1] if b0 & 0x20 and len(pkt) > start else 0)
    if end <= start:
        return None
    return pkt[start:end]


def frame_stats(payload):
    """(sum of squares, samples, peak) of a PCMU payload decoded to 16 bit."""
    total, peak = 0, 0
    for code in payload:
        s = ULAW[code]
        total += s * s
        peak = max(peak, abs(s))
    return total, len(payload), peak


def analyse(arrivals, silence_rms=SILENCE_RMS):
    """Measurements of what arrived: `arrivals` = [(seconds since the receive window opened, PCMU payload)].
    Returns packets, bytes, rms and peak over all decoded samples (16 bit units), silence_ratio (share of the
    packets whose own rms is under silence_rms; 1.0 when nothing arrived), first_audio_s (arrival of the first
    packet above it, None if none), first_packet_s, max_gap_ms (largest gap between consecutive packets) and a
    timeline of one row per second."""
    arrivals = sorted(arrivals, key=lambda a: a[0])
    n_bytes, total, samples, peak, silent = 0, 0, 0, 0, 0
    first_audio = None
    timeline = {}
    gaps = 0.0
    prev = None
    for t, payload in arrivals:
        sq, n, pk = frame_stats(payload)
        n_bytes += len(payload)
        total += sq
        samples += n
        peak = max(peak, pk)
        loud = n > 0 and math.sqrt(sq / n) >= silence_rms
        if loud and first_audio is None:
            first_audio = t
        if not loud:
            silent += 1
        if prev is not None:
            gaps = max(gaps, (t - prev) * 1000.0)
        prev = t
        row = timeline.setdefault(int(max(t, 0.0)), [0, 0, 0])
        row[0] += 1
        row[1] += sq
        row[2] += n
    packets = len(arrivals)
    return {
        "packets": packets, "bytes": n_bytes,
        "rms": round(math.sqrt(total / samples), 1) if samples else 0.0, "peak": peak,
        "silence_ratio": round(silent / packets, 4) if packets else 1.0,
        "first_audio_s": None if first_audio is None else round(first_audio, 3),
        "first_packet_s": round(arrivals[0][0], 3) if arrivals else None,
        "max_gap_ms": round(gaps, 1),
        "timeline": [{"s": s, "packets": r[0], "rms": round(math.sqrt(r[1] / r[2]), 1) if r[2] else 0.0}
                     for s, r in sorted(timeline.items())]}


def result_doc(opts, stats=None, connected=False, error=None, **extra):
    """The JSON document: the contract keys first, then the context."""
    doc = {"packets": 0, "bytes": 0, "rms": 0.0, "peak": 0, "silence_ratio": 1.0, "first_audio_s": None,
           "connected": bool(connected), "error": error}
    if stats:
        doc.update({k: stats[k] for k in ("packets", "bytes", "rms", "peak", "silence_ratio", "first_audio_s")})
        doc.update({k: stats[k] for k in ("first_packet_s", "max_gap_ms", "timeline")})
    doc.update({"url": opts.url, "room": opts.room, "seconds": opts.seconds})
    doc.update(extra)
    return doc


def parse_state(body):
    """(guest_listen, rooms) out of the public state document: guest_listen is True / False / None (absent),
    rooms the room letters of info.rooms. Anything unexpected gives (None, [])."""
    info = body.get("info") if isinstance(body, dict) else None
    if not isinstance(info, dict):
        return None, []
    guest = info.get("guest_listen")
    rooms = [r.get("room") for r in info.get("rooms") or [] if isinstance(r, dict) and isinstance(r.get("room"), str)]
    return (guest if isinstance(guest, bool) else None), rooms


def build_options(argv):
    ap = argparse.ArgumentParser(
        prog="python3 -m dvtest.tools.prod_listen",
        description="One anonymous WebRTC Listen on a public DV-Router web service; prints one JSON line.")
    ap.add_argument("--url", required=True, help="https://host[:port] of the web service (certificate verified)")
    ap.add_argument("--room", default="E", help="module to listen to (default E)")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help="how long to receive after the connection is up (default %g, at most %g)" % (DEFAULT_SECONDS, MAX_SECONDS))
    ap.add_argument("--ca-file", help="CA bundle to trust INSTEAD of the system roots (the lab CA; leave out for a public host)")
    ap.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT_S, help="seconds for ICE + DTLS (default %g)" % CONNECT_TIMEOUT_S)
    ap.add_argument("--silence-rms", type=float, default=SILENCE_RMS,
                    help="a packet with an rms below this counts as silence (default %g, 16 bit units)" % SILENCE_RMS)
    ap.add_argument("--json-out", help="also write the JSON document to this file")
    ap.add_argument("--dry-run", action="store_true", help="print what would be done; no network, no aiortc")
    ap.add_argument("--print-docker", action="store_true",
                    help="print the docker run command line for this check (not run) and exit")
    ns = ap.parse_args(argv)
    url = urllib.parse.urlsplit(ns.url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query:
        raise UsageError("--url must be https://host[:port] (https only, no credentials, no path)")
    if not re.fullmatch(r"[A-Z]", ns.room):
        raise UsageError("--room must be one capital letter")
    if not (ns.seconds == ns.seconds) or not 1.0 <= ns.seconds <= MAX_SECONDS:
        raise UsageError("--seconds must be between 1 and %g" % MAX_SECONDS)
    if ns.connect_timeout <= 0:
        raise UsageError("--connect-timeout must be positive")
    if ns.ca_file and not os.path.isfile(ns.ca_file):
        raise UsageError("--ca-file %s is not a file" % ns.ca_file)
    ns.url = "https://%s" % url.netloc
    return ns


def docker_command(opts, env=None):
    """The docker run command line (a list of words) that runs this check in the image dvt-webload:local:
    read-only root, all capabilities dropped, the limits of dvtest.limits.docker_run_args(), a tmpfs for /tmp, the
    repository's dvtest directory mounted read-only over the image's copy (the image was built before this tool
    existed), normal network access. Run from the root of the repository."""
    cmd = ["docker", "run", "--rm", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    cmd += limits.docker_run_args(env)
    cmd += ["--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--user", "1000:1000",
            "-v", "$PWD/dvtest:/app/dvtest:ro"]
    if opts.ca_file:
        ca = opts.ca_file if os.path.isabs(opts.ca_file) else "$PWD/" + opts.ca_file
        cmd += ["-v", "%s:/ca.pem:ro" % ca]
    cmd += ["--entrypoint", "python", IMAGE, "-m", "dvtest.tools.prod_listen",
            "--url", opts.url, "--room", opts.room, "--seconds", "%g" % opts.seconds]
    if opts.ca_file:
        cmd += ["--ca-file", "/ca.pem"]
    return cmd


def shell_line(words):
    """The words as one shell line; a word that starts with $PWD keeps the variable expandable."""
    return " ".join(('"%s"' % w) if w.startswith("$PWD") or "$PWD" in w else shlex.quote(w) for w in words)


# ---------------------------------------------------------------- signalling (blocking, run in a thread)

def make_client(opts):
    return WebClient(opts.url, ca_file=opts.ca_file, timeout=20.0, user_agent=USER_AGENT)


def preflight(client):
    """GET the public state: (guest_listen, rooms). Best effort: (None, []) on any failure."""
    try:
        resp = client.request("GET", STATE_PATH, pace=False)
        return parse_state(resp.json()) if resp.status == 200 else (None, [])
    except (WebError, ValueError):
        return None, []


# ---------------------------------------------------------------- the listener (needs aiortc)

async def listen(opts, client, state):
    """One guest circuit for opts.seconds; returns the result document. Needs aiortc (the dvt-webload image)."""
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpReceiver, RTCSessionDescription
    from dvtest.tools import webload_gen as gen

    gen.install_patches()
    loop = asyncio.get_running_loop()
    guest, rooms = state
    arrivals = []                   # (perf_counter_ns, payload)
    invalid = [0]
    sess_id = None
    pc = None
    connected_ns = None
    error = None
    alive = {}
    t_offer = None
    connect_s = None

    def on_rtp(t_ns, pkt):
        payload = rtp_payload(pkt)
        if payload is None:
            invalid[0] += 1
        elif len(arrivals) < MAX_PACKETS:
            arrivals.append((t_ns, payload))

    def post(path, body):
        try:
            return client.request("POST", path, json_body=body, pace=False)
        except WebError as exc:
            return exc

    try:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        up = asyncio.Event()

        @pc.on("connectionstatechange")
        async def _state():                                  # noqa: ANN202
            if pc.connectionState == "connected":
                up.set()
            elif pc.connectionState in ("failed", "closed") and not up.is_set():
                up.set()

        tr = pc.addTransceiver("audio", direction="recvonly")
        pcmu = [c for c in RTCRtpReceiver.getCapabilities("audio").codecs if c.name == "PCMU"]
        tr.setCodecPreferences(pcmu)
        tr.receiver.transport._dvt_sink = on_rtp
        await pc.setLocalDescription(await pc.createOffer())
        sdp = gen.relay_sdp(pc.localDescription.sdp)
        t_offer = time.monotonic()
        reply = await loop.run_in_executor(None, lambda: post(OFFER_PATH, {"sdp": sdp, "room": opts.room}))
        if isinstance(reply, WebError):
            error = "offer: %s" % str(reply)[:200]
        elif reply.status != 200:
            error = "offer HTTP %d %s" % (reply.status, reply.text()[:120].replace("\n", " "))
        else:
            body = reply.json()
            sess_id = body.get("sess_id")
            if not isinstance(sess_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", sess_id):
                sess_id, error = None, "the offer reply has no usable sess_id"
            else:
                try:
                    await asyncio.wait_for(pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="answer")),
                                           opts.connect_timeout)
                    await asyncio.wait_for(up.wait(), 2.0)
                except asyncio.TimeoutError:
                    error = "not connected %.0f s after the answer" % opts.connect_timeout
                if error is None and pc.connectionState != "connected":
                    error = "connection " + pc.connectionState
        if error is None:
            connected_ns = time.perf_counter_ns()
            connect_s = round(time.monotonic() - t_offer, 3)
            print("prod_listen: connected after %.2f s, receiving for %g s" % (connect_s, opts.seconds), file=sys.stderr, flush=True)
            t0 = loop.time()
            end = t0 + opts.seconds
            for off in wl.alive_schedule(first_seconds=6):
                when = t0 + off
                if when >= end:
                    break
                await asyncio.sleep(max(0.0, when - loop.time()))
                res = await loop.run_in_executor(None, lambda: post(ALIVE_PATH, {"sess_id": sess_id}))
                key = "error" if isinstance(res, WebError) else str(res.status)
                alive[key] = alive.get(key, 0) + 1
            await asyncio.sleep(max(0.0, end - loop.time()))
    except Exception as exc:                                 # noqa: BLE001 - reported in the document
        error = error or "%s: %s" % (type(exc).__name__, str(exc)[:160])
    finally:
        if sess_id:
            await loop.run_in_executor(None, lambda: post(CLOSE_PATH, {"sess_id": sess_id}))
        if pc is not None:
            try:
                await asyncio.wait_for(pc.close(), 10)
            except Exception:                                # noqa: BLE001
                pass
    base = connected_ns if connected_ns is not None else (arrivals[0][0] if arrivals else 0)
    stats = analyse([(max(0.0, (t - base) / 1e9), p) for t, p in arrivals], opts.silence_rms)
    return result_doc(opts, stats, connected=connected_ns is not None, error=error, connect_s=connect_s,
                      invalid_packets=invalid[0], alive=alive, guest_listen=guest, rooms_served=rooms,
                      silence_rms=opts.silence_rms)


def print_plan(opts):
    print("DRY RUN: no network, no aiortc")
    print("one anonymous listen on %s, room %s, %g s after the connection is up" % (opts.url, opts.room, opts.seconds))
    print("  verification: %s" % ("only the CA in %s" % opts.ca_file if opts.ca_file else "system roots (certificate and host name checked)"))
    print("  GET  %s   (guest_listen and rooms, best effort)" % STATE_PATH)
    print("  POST %s  {sdp, room}   no login, no cookie" % OFFER_PATH)
    print("  POST %s  {sess_id}     at once, every 1 s until 6, then every 10 s" % ALIVE_PATH)
    print("  POST %s  {sess_id}     at the end" % CLOSE_PATH)
    print("  docker: " + shell_line(docker_command(opts)))


def main(argv=None):
    try:
        opts = build_options(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        print("prod_listen: %s" % exc, file=sys.stderr)
        return 2
    if opts.print_docker:
        print(shell_line(docker_command(opts)))
        return 0
    if opts.dry_run:
        print_plan(opts)
        return 0
    try:
        client = make_client(opts)
        state = preflight(client)
        doc = asyncio.run(listen(opts, client, state))
    except ImportError as exc:
        doc = result_doc(opts, error="aiortc is not installed here (%s): run it in the dvt-webload image, see docs/prod-checks.md" % exc)
    except Exception as exc:                                 # noqa: BLE001
        doc = result_doc(opts, error="%s: %s" % (type(exc).__name__, str(exc)[:160]))
    line = json.dumps(doc)
    print(line, flush=True)
    if opts.json_out:
        with open(opts.json_out, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return 0 if doc["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
