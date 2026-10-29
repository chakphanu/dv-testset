# SPDX-License-Identifier: MIT
"""Bounded IAX2/SLIN test caller for an AllStar (app_rpt) node.

Wire layout follows RFC 5456. Radio controls use Asterisk's values 12/13.
The client converts S16LE test PCM to IAX SLIN network byte order.
It is a test endpoint, not a production media bridge.
"""
import hashlib
import secrets
import socket
import struct
import time
from dataclasses import dataclass

IAX, VOICE, CONTROL = 6, 2, 4
NEW, PING, PONG, ACK, HANGUP, REJECT, ACCEPT, AUTHREQ, AUTHREP = range(1, 10)
VNAK, CALLTOKEN, SLIN = 18, 40, 64


@dataclass(frozen=True)
class FullFrame:
    source: int
    destination: int
    timestamp: int
    out_sequence: int
    in_sequence: int
    kind: int
    subclass: int
    payload: bytes


def full_frame(source, destination, timestamp, out_sequence, in_sequence,
               kind, subclass, payload=b"", retransmit=False):
    if not 0 < source < 32768 or not 0 <= destination < 32768:
        raise ValueError("Invalid IAX call number")
    if subclass == -1:
        subclass = 255
    elif subclass > 127:
        if subclass & (subclass - 1):
            raise ValueError("Large subclasses must be powers of two")
        subclass = 0x80 | (subclass.bit_length() - 1)
    return struct.pack("!HHIBBBB", source | 0x8000,
        destination | (0x8000 if retransmit else 0), timestamp & 0xffffffff,
        out_sequence & 255, in_sequence & 255, kind, subclass) + payload


def parse_full(data):
    if len(data) < 12:
        raise ValueError("Truncated IAX full frame")
    source, destination, stamp, outgoing, incoming, kind, subclass = struct.unpack("!HHIBBBB", data[:12])
    if not source & 0x8000:
        raise ValueError("Not a full frame")
    if subclass == 255:
        subclass = -1
    elif subclass & 0x80:
        subclass = 1 << (subclass & 0x7f)
    return FullFrame(source & 0x7fff, destination & 0x7fff, stamp,
                     outgoing, incoming, kind, subclass, data[12:])


def information(kind, value):
    if len(value) > 255:
        raise ValueError("IAX information element too long")
    return bytes((kind, len(value))) + value


def parse_information(data):
    result = {}
    while data:
        if len(data) < 2 or len(data) < 2 + data[1]:
            raise ValueError("Truncated IAX information element")
        kind, length = data[:2]
        result[kind] = data[2:2 + length]
        data = data[2 + length:]
    return result


def swap_pcm(data):
    if len(data) != 320:
        raise ValueError("SLIN test frames must contain 160 samples")
    output = bytearray(data)
    output[0::2], output[1::2] = data[1::2], data[0::2]
    return bytes(output)


class Client:
    def __init__(self, host, secret, username="rig", port=4569, bind="0.0.0.0"):
        self.peer = (socket.gethostbyname(host), port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((bind, 0))
        self.secret, self.username = secret.encode(), username.encode()
        self.local = secrets.randbelow(32767) + 1
        self.remote = 0
        self.out_sequence = self.in_sequence = 0
        self.started = time.monotonic()
        self.pending = {}
        self.accepted = self.answered = self.closed = False
        self.media = []
        self.controls = []
        self.voice_started = False
        self.voice_timestamp = 0
        self.link_ready = False
        self.radio_keyed = False
        self.radio_due = float("inf")
        self.new_elements = b""
        self.retransmissions = 0

    def stamp(self):
        return int((time.monotonic() - self.started) * 1000) & 0xffffffff

    def send(self, kind, subclass, payload=b"", stamp=None, sequence=None, reliable=True):
        stamp = self.stamp() if stamp is None else stamp
        sequence = self.out_sequence if sequence is None else sequence
        data = full_frame(self.local, self.remote, stamp, sequence,
                          self.in_sequence, kind, subclass, payload)
        self.socket.sendto(data, self.peer)
        if reliable:
            self.pending[sequence] = [data, time.monotonic(), 0]
            self.out_sequence = (self.out_sequence + 1) & 255

    def acknowledge(self, frame):
        self.send(IAX, ACK, stamp=frame.timestamp, sequence=frame.in_sequence, reliable=False)

    def retry(self):
        now = time.monotonic()
        if self.link_ready and now >= self.radio_due:
            self.send(CONTROL, 12 if self.radio_keyed else 13)
            self.radio_due = now + 2
        for seq, entry in list(self.pending.items()):
            data, sent, attempts = entry
            if now - sent < min(0.2 * 2**attempts, 0.8):
                continue
            if attempts >= 5:
                raise TimeoutError("IAX reliable frame was not acknowledged")
            packet = bytearray(data)
            packet[2] |= 0x80
            packet[9] = self.in_sequence
            self.socket.sendto(packet, self.peer)
            self.pending[seq] = [data, now, attempts + 1]
            self.retransmissions += 1

    def handle(self, data, peer):
        if peer != self.peer or len(data) < 4:
            return
        call = struct.unpack("!H", data[:2])[0]
        if not call & 0x8000:
            if self.accepted and call == self.remote and len(data) == 324:
                self.media.append(swap_pcm(data[4:]))
            return
        frame = parse_full(data)
        if frame.destination != self.local or (self.remote and frame.source != self.remote):
            return
        if frame.kind == IAX and frame.subclass == CALLTOKEN:
            token = parse_information(frame.payload).get(54)
            if token is None:
                raise ValueError("CALLTOKEN response has no token")
            self.pending.clear()
            self.out_sequence = self.in_sequence = 0
            self.send(IAX, NEW, self.new_elements + information(54, token), stamp=0)
            return
        if not frame.source:
            return
        if not self.remote:
            self.remote = frame.source
        for seq in list(self.pending):
            if 0 < ((frame.in_sequence - seq) & 255) < 128:
                del self.pending[seq]
        special = frame.kind == IAX and frame.subclass in (ACK, VNAK)
        if not special:
            delta = (frame.out_sequence - self.in_sequence) & 255
            if delta:
                if delta >= 128:
                    self.acknowledge(frame)
                else:
                    self.send(IAX, VNAK, stamp=frame.timestamp, reliable=False)
                return
            self.in_sequence = (self.in_sequence + 1) & 255
            self.acknowledge(frame)
        if frame.kind == IAX:
            if frame.subclass == AUTHREQ:
                elements = parse_information(frame.payload)
                if not struct.unpack("!H", elements.get(14, b"\0\0"))[0] & 2 or 15 not in elements:
                    raise ValueError("Rig caller requires MD5 challenge authentication")
                result = hashlib.md5(elements[15] + self.secret).hexdigest().encode()
                self.send(IAX, AUTHREP, information(6, self.username) + information(16, result))
            elif frame.subclass == ACCEPT:
                elements = parse_information(frame.payload)
                if elements.get(9) != struct.pack("!I", SLIN):
                    raise ValueError("Peer did not accept SLIN")
                self.accepted = True
            elif frame.subclass in (REJECT, HANGUP):
                self.closed = True
                cause = parse_information(frame.payload).get(22, b"Peer ended call").decode(errors="replace")
                if not self.accepted:
                    raise ConnectionError(cause)
            elif frame.subclass in (PING, 11):
                self.send(IAX, PONG if frame.subclass == PING else 12, stamp=frame.timestamp)
            elif frame.subclass == VNAK:
                for seq, entry in self.pending.items():
                    if ((seq - frame.in_sequence) & 255) < 128:
                        entry[1] = 0
        elif frame.kind == CONTROL:
            self.controls.append(frame.subclass)
            if frame.subclass == 4:
                self.answered = True
            elif frame.subclass == 1:
                self.closed = True
        elif frame.kind == 7:
            if frame.payload.rstrip(b"\0") == b"!NEWKEY!":
                self.link_ready = True
                self.radio_due = time.monotonic() + 2
        elif frame.kind == VOICE:
            if frame.subclass != SLIN:
                raise ValueError("Unexpected IAX voice codec")
            self.media.append(swap_pcm(frame.payload))

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.closed:
            self.retry()
            self.socket.settimeout(min(0.02, max(0.001, deadline - time.monotonic())))
            try:
                data, peer = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            self.handle(data, peer)

    def call(self, node, caller="1999", name="N0IAX", timeout=5):
        self.new_elements = b"".join([
            information(1, str(node).encode()), information(2, caller.encode()),
            information(4, name.encode()), information(6, self.username),
            information(8, struct.pack("!I", SLIN)), information(9, struct.pack("!I", SLIN)),
            information(11, struct.pack("!H", 2))])
        self.send(IAX, NEW, self.new_elements, stamp=0)
        deadline = time.monotonic() + timeout
        requested_link = False
        while time.monotonic() < deadline and not self.closed:
            self.pump(0.05)
            if self.accepted and self.answered:
                if not requested_link:
                    self.send(7, 0, b"!NEWKEY!\0")
                    requested_link = True
                if self.link_ready:
                    return
        raise TimeoutError("IAX call did not reach ACCEPT/ANSWER/NEWKEY")

    def key(self):
        self.voice_started = False
        self.radio_keyed = True
        self.send(CONTROL, 12)
        self.radio_due = time.monotonic() + 2

    def unkey(self):
        self.radio_keyed = False
        self.send(CONTROL, 13)
        self.radio_due = time.monotonic() + 2

    def voice(self, pcm):
        network = swap_pcm(pcm)
        if not self.voice_started:
            self.voice_timestamp = self.stamp()
            self.send(VOICE, SLIN, network, stamp=self.voice_timestamp)
            self.voice_started = True
        else:
            self.voice_timestamp = (self.voice_timestamp + 20) & 0xffffffff
            self.socket.sendto(struct.pack("!HH", self.local, self.voice_timestamp & 0xffff) + network, self.peer)

    def close(self):
        try:
            if not self.closed and self.remote:
                self.unkey()
                self.send(IAX, HANGUP, information(22, b"Rig test complete"))
                self.pump(0.2)
        finally:
            self.socket.close()
