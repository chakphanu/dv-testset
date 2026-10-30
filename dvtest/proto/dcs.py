# SPDX-License-Identifier: MIT
"""D-STAR over DCS (UDP 30051): link, keepalive, 100-byte voice packets
that each carry the D-STAR header, and a client that talks and collects
9-byte AMBE frames.

| Packet | Size | Layout |
|--------|------|--------|
| link | 519 B | callsign(7) + own module + own module + reflector module + 0x00 + text, zero padded |
| link ACK / NAK | 14 B | callsign(7) + ' ' + own module + reflector module + `ACK\\0` / `NAK\\0`, after 0..2 s |
| unlink | 11 B | callsign(7) + own module + own module + ' ' + 0x00; xlxd answers with a NAK |
| keepalive (client) | 17 B | callsign(7) + own module + 0x00 + reflector(7) + reflector module |
| keepalive (xlxd) | 8 + 22 B | reflector callsign (8, `DCS999`); reflector(7) + module + ' ' + callsign(7) + module + module + 0A 00 20 20 |
| voice | 100 B | `0001`, header without CRC (39: flags 3, RPT2, RPT1, UR, MY, suffix 4), stream id u16 LE, seq (mod 21, 0x40 = last), AMBE(9), slow data(3), packet counter u24 LE, 0x01, 38 B text area |

xlxd takes the module from the link, not from RPT2, and opens the stream
on the first voice packet. The text area of xlxd's own packets carries
0x21 at byte 63 and a 20 character text at 64..83.
"""
from . import dextra
from ..net import UdpClient

PORT = 30051
LINK_SIZE = 519
ACK_SIZE = 14
PACKET_SIZE = 100
TEXT = b"<table><tr><td>dvtest</td></tr></table>"


def _cs7(callsign):
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return (callsign.rstrip(b" ") + b" " * 7)[:7]


def link_packet(callsign, module, own_module=None):
    own = (own_module or module).encode()
    out = _cs7(callsign) + own + own + module.encode() + b"\x00" + TEXT
    return out + bytes(LINK_SIZE - len(out))


def unlink_packet(callsign, own_module):
    own = own_module.encode()
    return _cs7(callsign) + own + own + b" \x00"


def keepalive_packet(callsign, own_module, reflector="XLX999", module="B"):
    return _cs7(callsign) + own_module.encode() + b"\x00" + _cs7(reflector) + module.encode()


def parse_ack(data):
    """"ACK" or "NAK" of a 14 B link reply, else None."""
    if len(data) != ACK_SIZE or data[13:14] != b"\x00" or data[10:13] not in (b"ACK", b"NAK"):
        return None
    return data[10:13].decode()


def is_reflector_keepalive(data):
    """The 22 B per-client keepalive xlxd sends every 5 s."""
    return len(data) == 22 and data[18:22] == b"\x0a\x00\x20\x20"


def voice(stream_id, seq, frame, last, counter, callsign, module, reflector="XLX999",
          slow=dextra.SLOW_DATA):
    """100 B voice packet. RPT2 = reflector + module, RPT1 = callsign(7) +
    module, UR = CQCQCQ, MY = callsign, as dextra.header builds them."""
    body = dextra.header(stream_id, callsign, module, reflector)[15:15 + 39]
    out = bytearray(b"0001" + body)
    out += stream_id.to_bytes(2, "little")
    out.append((seq % 21) | (0x40 if last else 0))
    out += frame + slow
    out += (counter & 0xFFFFFF).to_bytes(3, "little")
    out.append(0x01)
    out += bytes(PACKET_SIZE - len(out))
    return bytes(out)


def parse(data):
    """dict of voice packet fields, or None."""
    if len(data) < PACKET_SIZE or not data.startswith(b"0001"):
        return None
    head = data[4:43]
    return {"rpt2": head[3:11], "rpt1": head[11:19], "ur": head[19:27], "my": head[27:35],
            "stream_id": int.from_bytes(data[43:45], "little"), "seq": data[45] & 0x1F,
            "last": bool(data[45] & 0x40), "frame": data[46:55], "slow": data[55:58],
            "counter": int.from_bytes(data[58:61], "little")}


# ------------------------------------------------- conformance builders
# Client forms as real gateways send them (docs/protocols/dcs.md); the
# functions above are the tester's own compact forms and stay as they are.

REFLECTOR = "DCS999"
POLL_SIZE = 22
NOTICE_SIZE = 19
# last-packet payloads: (AMBE 9, slow data 3)
END_JARL = (b"\x55\x55\x55\x55\xc8\x7a\x00\x00\x00", b"\x55\x55\x55")    # IG
END_XLXD = (b"\x55\xc8\x7a\x00\x00\x00\x00\x00\x00", b"\x25\x1a\xc6")    # xlxd
HTML = (b"<table border=\"0\" width=\"95%\"><tr><td width=\"4%\"><img border=\"0\" "
        b"src=hotspot.jpg></td><td width=\"96%\"><font size=\"2\"><b>HOTSPOT</b> dvtest"
        b"</font></td></tr></table>")


def _cs8(callsign):
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return (callsign + b" " * 8)[:8]


def gateway_link(callsign, own_module, module, reflector=REFLECTOR, text=HTML):
    """519 B link as ircDDBGateway / DStarGateway / QnetGateway send it:
    callsign (8), own module, reflector module, 0x00, reflector name (8),
    HTML text, zero padded."""
    out = (_cs8(callsign) + own_module.encode() + module.encode() + b"\x00"
           + _cs8(reflector) + text)
    return out + bytes(LINK_SIZE - len(out))


def droidstar_link(callsign, module):
    """519 B link as DroidStar sends it: callsign (8), module twice, 0x0B,
    nothing else (bytes 11-518 left as allocated: zero here)."""
    out = _cs8(callsign) + module.encode() * 2 + b"\x0b"
    return out + bytes(LINK_SIZE - len(out))


def raw_link(head10, tail=b"\x00", body=b"", size=LINK_SIZE):
    """A link of any first 10 bytes, byte 10 and size (invalid forms)."""
    out = bytes(head10) + bytes(tail) + bytes(body)
    return (out + bytes(size))[:size]


def unlink19(callsign, own_module, reflector=REFLECTOR):
    """19 B unlink (ircDDBGateway, QnetGateway): callsign (8), own module,
    space, 0x00, reflector name (8)."""
    return _cs8(callsign) + own_module.encode() + b" \x00" + _cs8(reflector)


def unlink11(callsign, own_module):
    """11 B unlink (DroidStar): callsign (8), own module, space, 0x00."""
    return _cs8(callsign) + own_module.encode() + b" \x00"


def keepalive17(callsign, own_module, reflector=REFLECTOR, module=None, form="gateway"):
    """17 B client keepalive. gateway: callsign (7) + own module + 0x00 +
    reflector (7) + its module; droidstar: callsign (7) + module + 0x00 +
    reflector name as is + 0x00 + module (17 B for a 6-character name)."""
    module = module or own_module
    head = _cs7(callsign) + own_module.encode() + b"\x00"
    if form == "droidstar":
        name = reflector.encode() if isinstance(reflector, str) else reflector
        return head + name + b"\x00" + module.encode()
    return head + _cs7(reflector) + module.encode()


def parse_reply(data):
    """A packet from the reflector: {"kind": "ack" | "nak" | "poll" |
    "ref9" | "notice" | "voice" | "other", ...fields}."""
    size = len(data)
    if size == ACK_SIZE and data[10:13] in (b"ACK", b"NAK") and data[13:14] == b"\x00":
        return {"kind": data[10:13].decode().lower(), "callsign": data[:7], "byte7": data[7:8],
                "own_module": data[8:9], "module": data[9:10]}
    if size == POLL_SIZE:
        return {"kind": "poll", "reflector": data[:7], "module": data[7:8], "byte8": data[8:9],
                "callsign": data[9:16], "byte16": data[16:17], "own_module": data[17:18],
                "tail": data[18:22]}
    if size == 9 and data[8] == 0:
        return {"kind": "ref9", "reflector": data[:8]}
    if size == NOTICE_SIZE:
        return {"kind": "notice", "raw": data}
    if size >= PACKET_SIZE and data.startswith(b"0001"):
        return dict(parse_raw(data), kind="voice")
    return {"kind": "other", "size": size}


def voice_packet(stream_id, seq_byte, frame=bytes(9), slow=dextra.SLOW_DATA, *, callsign="N0CALL",
                 module="B", reflector=REFLECTOR, rpt2=None, rpt1=None, ur=b"CQCQCQ  ", my=None,
                 suffix=b"    ", flags=b"\x00\x00\x00", counter=0, tail=None, sid_bytes=None):
    """100 B voice with every field settable: raw sequence byte (0x40 =
    last), header fields, counter, the area from byte 61 (default 0x01 +
    zeros) and the raw stream id bytes (default low byte first)."""
    rpt2 = rpt2 if rpt2 is not None else _cs7(reflector) + module.encode()
    rpt1 = rpt1 if rpt1 is not None else _cs7(callsign) + module.encode()
    my = my if my is not None else _cs8(callsign)
    sid = sid_bytes if sid_bytes is not None else (stream_id & 0xFFFF).to_bytes(2, "little")
    out = (b"0001" + bytes(flags) + _field(rpt2) + _field(rpt1) + _field(ur) + _field(my)
           + (bytes(suffix) + b"    ")[:4] + bytes(sid) + bytes([seq_byte & 0xFF]) + bytes(frame)
           + bytes(slow) + (counter & 0xFFFFFF).to_bytes(3, "little"))
    out += tail if tail is not None else b"\x01"
    return (out + bytes(PACKET_SIZE))[:max(PACKET_SIZE, len(out))]


def _field(value):
    value = value.encode() if isinstance(value, str) else bytes(value)
    return (value + b" " * 8)[:8]


def parse_raw(data):
    """Every field of a voice packet (100 B or longer), raw."""
    return {"flags": data[4:7], "rpt2": data[7:15], "rpt1": data[15:23], "ur": data[23:31],
            "my": data[31:39], "suffix": data[39:43], "sid": data[43:45], "seq_byte": data[45],
            "seq": data[45] & 0x3F, "last": bool(data[45] & 0x40), "frame": data[46:55],
            "slow": data[55:58], "counter": int.from_bytes(data[58:61], "little"),
            "tail": data[61:100], "size": len(data)}


def stream_packets(stream_id, frames, *, start=0, last=True, end=None, **fields):
    """Voice packets of one over: sequence from start (mod 21), sync slow
    data at sequence 0, counter from 0, then a last packet (payload end =
    (AMBE, slow) or the default zero AMBE + filler)."""
    out = []
    for index, frame in enumerate(frames):
        seq = (start + index) % 21
        out.append(voice_packet(stream_id, seq, frame, dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA,
                                counter=index, **fields))
    if last:
        ambe, slow = end or (bytes(9), dextra.SLOW_DATA)
        seq = (start + len(frames)) % 21
        out.append(voice_packet(stream_id, seq | 0x40, ambe, slow, counter=len(frames), **fields))
    return out


class DcsClient:
    def _keepalive(self, data, _addr):
        # xlxd drops a DCS client it has not heard from for 60 s; answer
        # its per-client keepalive with ours.
        if is_reflector_keepalive(data):
            return keepalive_packet(self.callsign, self.module, module=self.module)
        return None

    def __init__(self, bind_ip, callsign="N0CALL", module="B"):
        self.client = UdpClient(bind_ip, responder=self._keepalive)
        self.callsign = _cs7(callsign)
        self.module = module
        self.dest = None

    def link(self, node_ip, timeout=3.0, port=PORT):
        """Link to the module; the ACK comes after a random 0..2 s delay."""
        self.dest = (node_ip, port)
        self.client.send(self.dest, link_packet(self.callsign, self.module))
        steps = int(timeout / 0.05) + 1
        for _ in range(steps):
            self.client.drain()
            for packet in self.client.inbox:
                reply = parse_ack(packet)
                if reply:
                    return reply == "ACK"
            self.client.pump(0.05)
        return False

    def keepalive(self):
        self.client.send(self.dest, keepalive_packet(self.callsign, self.module, module=self.module))

    def unlink(self):
        self.client.send(self.dest, unlink_packet(self.callsign, self.module))
        self.client.pump(0.4)

    def packets(self, frames, stream_id, reflector="XLX999"):
        out = []
        for index, frame in enumerate(frames):
            out.append(voice(stream_id, index, frame, False, index, self.callsign, self.module,
                             reflector, dextra.SYNC_DATA if index % 21 == 0 else dextra.SLOW_DATA))
        out.append(voice(stream_id, len(frames), b"\x00" * 9, True, len(frames), self.callsign,
                         self.module, reflector))
        return out

    def talk(self, frames, stream_id, reflector="XLX999", settle=0.3, tail=2.0):
        """Stream 9-byte AMBE frames: the first packet opens the stream,
        the rest follow every 20 ms; the receiver drains."""
        packets = self.packets(frames, stream_id, reflector)
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        self.client.paced(self.dest, packets[1:], 0.020)
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Voice frames received on this socket, in arrival order (the
        last packet is excluded)."""
        self.client.pump(settle)
        out = []
        for packet in self.client.inbox:
            parsed = parse(packet)
            if parsed and not parsed["last"]:
                out.append(parsed["frame"])
        return out

    def close(self):
        self.client.close()
