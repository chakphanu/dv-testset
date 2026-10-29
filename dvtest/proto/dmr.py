# SPDX-License-Identifier: MIT
"""DMR over the MMDVM/Homebrew repeater protocol (UDP 62030) to xlxd.
The DMR id must exist in the reflector's dmrid.dat; TG 4001..4026 map to
modules A..Z, TG 4000 unlinks."""
import random

from ..net import UdpClient

PORT = 62030
DMR_ID = 1023007
PACKET_SIZE = 55
DATA_SYNC = (0xFF, 0x57, 0xD7, 0x5D, 0xF5)
_rng = random.SystemRandom()


def data_sync_payload():
    payload = bytearray(33)
    payload[13] = 0x0D
    payload[14:19] = bytes(DATA_SYNC)
    payload[19] = 0xD0
    return bytes(payload)


def voice_payload(frame0, frame1, frame2):
    """Three 9-byte AMBE frames around the 48-bit sync gap."""
    ambe = frame0 + frame1 + frame2
    payload = bytearray(33)
    payload[0:13] = ambe[0:13]
    payload[13] = ambe[13] & 0xF0
    payload[19] = ambe[13] & 0x0F
    payload[20:33] = ambe[14:27]
    return bytes(payload)


def extract(payload):
    """Inverse of voice_payload: the three 9-byte frames."""
    ambe = bytearray(27)
    ambe[0:14] = payload[0:14]
    ambe[13] = (payload[13] & 0xF0) | (payload[19] & 0x0F)
    ambe[14:27] = payload[20:33]
    return [bytes(ambe[0:9]), bytes(ambe[9:18]), bytes(ambe[18:27])]


def module_tg(module):
    return 4001 + (ord(module) - ord("A"))


def packet(seq, src, dst, repeater, bitfield, stream, payload):
    out = bytearray(PACKET_SIZE)
    out[0:4] = b"DMRD"
    out[4] = seq & 0xFF
    out[5:8] = src.to_bytes(3, "big")
    out[8:11] = dst.to_bytes(3, "big")
    out[11:15] = repeater.to_bytes(4, "big")
    out[15] = bitfield
    out[16:20] = int(stream).to_bytes(4, "little")
    out[20:53] = payload
    return bytes(out)


def parse(data):
    """dict of DMRD fields, or None."""
    if len(data) != PACKET_SIZE or not data.startswith(b"DMRD"):
        return None
    return {"seq": data[4], "src": int.from_bytes(data[5:8], "big"),
            "dst": int.from_bytes(data[8:11], "big"),
            "repeater": int.from_bytes(data[11:15], "big"), "bitfield": data[15],
            "stream": int.from_bytes(data[16:20], "little"), "payload": data[20:53]}


def is_voice(data):
    """Flags type bits 0 and 1 are voice bursts (type 1 = voice sync);
    type 2 is the data-sync header/terminator and carries no voice."""
    return len(data) == PACKET_SIZE and data.startswith(b"DMRD") and (data[15] & 0x30) != 0x20


def voice_packets(frames, src, dst, stream_id):
    """Voice bursts for frames (padded to a multiple of 3), seq from 1."""
    packets = []
    seq = 1
    index = 0
    while index < len(frames):
        trio = []
        for _ in range(3):
            trio.append(frames[index] if index < len(frames) else b"\x00" * 9)
            index += 1
        # bursts A..F (vseq 0..5) from the first packet; A carries voice sync
        vseq = (seq - 1) % 6
        kind = 1 if vseq == 0 else 0
        bitfield = 0x80 | (kind << 4) | vseq
        packets.append(packet(seq, src, dst, src, bitfield, stream_id, voice_payload(*trio)))
        seq = (seq + 1) & 0xFF
    return packets, seq


class DmrClient:
    def __init__(self, bind_ip, dmrid=DMR_ID, module="B"):
        self.client = UdpClient(bind_ip)
        self.id = dmrid
        self.tg = module_tg(module)
        self.dest = None

    def link(self, node_ip, timeout=1.0, port=PORT, join_gap=0.3):
        self.dest = (node_ip, port)
        ident = self.id.to_bytes(4, "big")
        self.client.send(self.dest, b"RPTL" + ident)
        self.client.pump(timeout)
        if not any(p.startswith(b"RPTACK") for p in self.client.inbox):
            return False
        self.client.send(self.dest, b"RPTK" + ident + b"\x00" * 32)
        self.client.pump(timeout)
        self.client.send(self.dest, b"RPTPING" + ident)
        self.client.pump(0.2)
        self.join(None, join_gap)
        return True

    def join(self, module=None, gap=0.3):
        """Join a module (default: the current one): a header, then a
        terminator, both to the module's talkgroup. The terminator follows
        well inside xlxd's 1.6 s stream timeout, so the join is a complete
        (empty) stream."""
        if module is not None:
            self.tg = module_tg(module)
        self._empty_stream(self.tg, gap)
        self.client.pump(1.0)

    def _empty_stream(self, tg, gap):
        # xlxd acts on link/unlink only in the header of a new stream id
        sid = _rng.randrange(1, 1 << 32)
        self.client.send(self.dest, packet(0, self.id, tg, self.id, 0xA1, sid, data_sync_payload()))
        self.client.pump(gap)
        self.client.send(self.dest, packet(1, self.id, tg, self.id, 0xA2, sid, data_sync_payload()))

    def keepalive(self):
        """Repeater keepalive; xlxd drops a silent DMR client after ~100 s."""
        self.client.send(self.dest, b"RPTPING" + self.id.to_bytes(4, "big"))

    def unlink(self):
        """TG 4000 unlinks: a header and a terminator on a new stream id."""
        self._empty_stream(4000, 0.3)
        self.client.pump(0.4)

    def talk(self, frames, stream_id=0x22222222, settle=1.2, tail=2.0):
        self.client.send(self.dest, packet(0, self.id, self.tg, self.id, 0xA1, stream_id,
                                           data_sync_payload()))
        self.client.pump(settle)
        packets, seq = voice_packets(frames, self.id, self.tg, stream_id)
        # send times of the voice bursts (header/terminator excluded)
        self.last_stamps = self.client.paced(self.dest, packets, 0.060)
        self.client.send(self.dest, packet(seq, self.id, self.tg, self.id, 0xA2, stream_id,
                                           data_sync_payload()))
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Voice frames from received DMRD voice bursts, in arrival order."""
        self.client.pump(settle)
        out = []
        for data in self.client.inbox:
            if is_voice(data):
                out.extend(extract(data[20:53]))
        return out

    def close(self):
        self.client.close()
