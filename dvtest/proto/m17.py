# SPDX-License-Identifier: MIT
"""M17 client in xlxd reflector-client mode (UDP 17000). Two 8-byte
Codec2 3200 frames per 54-byte voice packet."""
import time

from ..net import UdpClient

PORT = 17000
ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/."
FRAMETYPE_VOICE = 0x05
PACKET_SIZE = 54


def encode_callsign(text):
    """Base-40 callsign, 6 bytes big endian."""
    cs = (text or "").upper()[:9]
    cs = cs + " " * (9 - len(cs))
    value = 0
    for ch in reversed(cs):
        index = ALPHABET.find(ch)
        value = value * 40 + (index if index >= 0 else 0)
    return value.to_bytes(6, "big")


def decode_callsign(data):
    value = int.from_bytes(data, "big")
    out = []
    while value:
        out.append(ALPHABET[value % 40])
        value //= 40
    return "".join(out).rstrip()


def packet(stream_id, seq, src_callsign, payload16, eot=False):
    out = bytearray(b"M17 ")
    out += stream_id.to_bytes(2, "big")
    out += b"\xff" * 6
    out += encode_callsign(src_callsign)
    out.append(0)
    out.append(FRAMETYPE_VOICE)
    out += bytes(14)
    value = (seq & 0x7FFF) | (0x8000 if eot else 0)
    out += value.to_bytes(2, "big")
    out += payload16
    out += bytes(2)
    return bytes(out)


def parse(data):
    """dict of voice packet fields, or None."""
    if len(data) != PACKET_SIZE or not data.startswith(b"M17 "):
        return None
    seq = int.from_bytes(data[34:36], "big")
    return {"stream_id": int.from_bytes(data[4:6], "big"), "dst": data[6:12],
            "src": decode_callsign(data[12:18]), "frametype": data[19],
            "seq": seq & 0x7FFF, "eot": bool(seq & 0x8000),
            "frames": [data[36:44], data[44:52]]}


def conn_packet(callsign, module):
    return b"CONN" + encode_callsign(callsign) + module.encode()


def disc_packet(callsign):
    return b"DISC" + encode_callsign(callsign)


def pong_packet(callsign):
    return b"PONG" + encode_callsign(callsign)


def voice_packets(frames8, stream_id, callsign):
    """Seq 0 repeats the first frame, then pairs, then an EOT packet that
    repeats the last payload."""
    packets = [packet(stream_id, 0, callsign, (frames8[0] * 2) if frames8 else bytes(16))]
    for index in range(0, len(frames8), 2):
        pair = frames8[index:index + 2]
        payload = pair[0] + (pair[1] if len(pair) > 1 else pair[0])
        packets.append(packet(stream_id, 1 + index // 2, callsign, payload))
    packets.append(packet(stream_id, len(packets), callsign, packets[-1][36:52], eot=True))
    return packets


class M17Client:
    def _keepalive(self, data, _addr):
        # xlxd PINGs every ~10 s and drops a client that does not answer
        # PONG + callsign.
        if data.startswith(b"PING"):
            return pong_packet(self.callsign)
        return None

    def __init__(self, bind_ip, callsign="N0CALL", module="B"):
        self.client = UdpClient(bind_ip, responder=self._keepalive)
        self.callsign = callsign
        self.module = module
        self.dest = None

    def link(self, node_ip, timeout=5.0, port=PORT):
        self.dest = (node_ip, port)
        self.client.send(self.dest, conn_packet(self.callsign, self.module))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.drain()
            for data in self.client.inbox:
                if data.startswith(b"ACKN"):
                    return True
                if data.startswith(b"NACK"):
                    return False
            time.sleep(0.05)
        return False

    def unlink(self):
        self.client.send(self.dest, disc_packet(self.callsign))
        self.client.pump(0.4)

    def talk(self, frames8, stream_id, settle=0.3, tail=2.0, gap=0.040):
        """frames8: sequence of 8-byte Codec2 3200 frames (two per packet)."""
        packets = voice_packets(frames8, stream_id, self.callsign)
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        self.client.paced(self.dest, packets[1:], gap)
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Codec2 8-byte frames from forwarded voice packets. Every packet
        carries two frames, including seq 0 and the EOT packet. Pump first:
        frames sit in the kernel buffer until the socket is drained."""
        self.client.pump(settle)
        out = []
        for data in self.client.inbox:
            if len(data) == PACKET_SIZE and data.startswith(b"M17 "):
                out.append(data[36:44])
                out.append(data[44:52])
        return out

    def close(self):
        self.client.close()
