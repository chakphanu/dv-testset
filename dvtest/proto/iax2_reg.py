# SPDX-License-Identifier: MIT
"""IAX2 registration client (REGREQ / REGAUTH / REGACK / REGREJ) for the phone-mode registrar of DV-Router's AllStar edge
(ASL1g, wip-c165c93 and later; docs/asl-dvswitch.md): what DVSwitch Mobile's "Phone mode IAX2 connection" sends, as a tester
endpoint. Wire layout RFC 5456, the same frames as Asterisk's chan_iax2 registration:

    REGREQ (USERNAME, REFRESH, empty CALLTOKEN)      ->
                                                      <- CALLTOKEN (the token)
    REGREQ (USERNAME, REFRESH, CALLTOKEN = token)    ->
                                                      <- REGAUTH (AUTHMETHODS md5, CHALLENGE, USERNAME)
    REGREQ (USERNAME, REFRESH, MD5_RESULT)           ->
                                                      <- REGACK  (or REGREJ: CAUSE, CAUSECODE)

MD5_RESULT is hex(md5(challenge + secret)). A REGREQ without a CALLTOKEN IE is dropped silently by the edge (no reply), which
`register(calltoken=False)` shows as a timeout. Stdlib only; one call number per attempt.
"""
import hashlib
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field

from dvtest.proto import iax2

REGREQ, REGAUTH, REGACK, REGREJ = 13, 14, 15, 16
IE_USERNAME, IE_AUTHMETHODS, IE_CHALLENGE, IE_MD5, IE_REFRESH, IE_CAUSE, IE_CAUSECODE, IE_CALLTOKEN = 6, 14, 15, 16, 19, 22, 42, 54


@dataclass
class RegResult:
    outcome: str                                  # "REGACK", "REGREJ" or "timeout"
    steps: list = field(default_factory=list)     # what happened in order: REGREQ, CALLTOKEN, REGREQ:token, REGAUTH, REGREQ:md5, ...
    cause: str = ""                               # CAUSE text of a REGREJ
    causecode: int = 0                            # CAUSECODE of a REGREJ
    refresh: int = 0                              # REFRESH of the REGACK
    seconds: float = 0.0                          # time to the final answer


def md5_result(challenge, secret):
    """The MD5_RESULT value: hex(md5(challenge + secret)), ASCII bytes."""
    if isinstance(challenge, str):
        challenge = challenge.encode()
    if isinstance(secret, str):
        secret = secret.encode()
    return hashlib.md5(challenge + secret).hexdigest().encode()


def request_elements(username, refresh=60, token=None, md5=None):
    """The IEs of a REGREQ: USERNAME, REFRESH and a CALLTOKEN IE (empty = ask for a token) or an MD5_RESULT."""
    out = iax2.information(IE_USERNAME, username.encode()) + iax2.information(IE_REFRESH, struct.pack("!H", refresh))
    if md5 is not None:
        out += iax2.information(IE_MD5, md5)
    elif token is not None:
        out += iax2.information(IE_CALLTOKEN, token)
    return out


def register(host, username, secret, port=4569, refresh=60, calltoken=True, timeout=5.0, bind="0.0.0.0"):
    """One registration attempt. Returns a RegResult; never raises for a refusal or silence (outcome "REGREJ" / "timeout")."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind, 0))
    peer = (socket.gethostbyname(host), port)
    local = secrets.randbelow(32767) + 1
    state = {"remote": 0, "oseq": 0, "iseq": 0}
    started = time.monotonic()
    result = RegResult("timeout")

    def send(subclass, payload, stamp=None, reliable=True, kind=iax2.IAX):
        stamp = int((time.monotonic() - started) * 1000) if stamp is None else stamp
        frame = iax2.full_frame(local, state["remote"], stamp, state["oseq"], state["iseq"], kind, subclass, payload)
        sock.sendto(frame, peer)
        if reliable:
            state["oseq"] = (state["oseq"] + 1) & 255

    def ack(frame):
        data = iax2.full_frame(local, state["remote"], frame.timestamp, frame.in_sequence, state["iseq"], iax2.IAX, iax2.ACK)
        sock.sendto(data, peer)

    try:
        first = request_elements(username, refresh, token=b"") if calltoken else request_elements(username, refresh)
        send(REGREQ, first, stamp=0)
        result.steps.append("REGREQ")
        end = started + timeout
        while time.monotonic() < end:
            sock.settimeout(max(0.01, min(0.2, end - time.monotonic())))
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if addr != peer or len(data) < 12 or not struct.unpack("!H", data[:2])[0] & 0x8000:
                continue
            frame = iax2.parse_full(data)
            if frame.destination != local or frame.kind != iax2.IAX:
                continue
            ies = iax2.parse_information(frame.payload)
            if frame.subclass == iax2.CALLTOKEN:
                token = ies.get(IE_CALLTOKEN)
                if token is None:
                    raise ValueError("CALLTOKEN reply without a token")
                result.steps.append("CALLTOKEN")
                state.update(oseq=0, iseq=0, remote=0)                 # a new call: the sequence restarts, as for NEW
                send(REGREQ, request_elements(username, refresh, token=token), stamp=0)
                result.steps.append("REGREQ:token")
                continue
            if frame.subclass in (iax2.ACK, iax2.VNAK) or not frame.source:
                continue
            state["remote"] = state["remote"] or frame.source
            state["iseq"] = (frame.out_sequence + 1) & 255
            ack(frame)
            if frame.subclass == REGAUTH:
                challenge = ies.get(IE_CHALLENGE)
                if challenge is None or not struct.unpack("!H", ies.get(IE_AUTHMETHODS, b"\0\0"))[0] & 2:
                    raise ValueError("REGAUTH without an MD5 challenge")
                result.steps.append("REGAUTH")
                send(REGREQ, request_elements(username, refresh, md5=md5_result(challenge, secret)))
                result.steps.append("REGREQ:md5")
            elif frame.subclass == REGACK:
                result.steps.append("REGACK")
                result.outcome = "REGACK"
                result.refresh = struct.unpack("!H", ies[IE_REFRESH])[0] if len(ies.get(IE_REFRESH, b"")) == 2 else 0
                break
            elif frame.subclass == REGREJ:
                result.steps.append("REGREJ")
                result.outcome = "REGREJ"
                result.cause = ies.get(IE_CAUSE, b"").decode("utf-8", "replace")
                result.causecode = ies.get(IE_CAUSECODE, b"\0")[-1] if ies.get(IE_CAUSECODE) else 0
                break
    finally:
        result.seconds = round(time.monotonic() - started, 3)
        sock.close()
    return result
