# SPDX-License-Identifier: MIT
"""Yaesu IMRS (Internet-linked Multi-site Repeater System) as xlxd speaks
it on UDP 21110 (docs/protocols/imrs.md). Yaesu publishes no wire
format; the layout below is the one xlxd uses towards DR-2X repeaters.

Every packet goes to the peer's IP at port 21110, whatever port it came
from. Sizes (bytes):

  ping        16   00 00 07 + 13 x 00 (answered with info + ping)
  info        60   00 2C 08 00 + 12 x 00, firmware u32 LE (16-19), MAC
                   (20-25), callsign (26-35, space padded), radio id text
                   (36-40), DG-ID bitmap u32 LE + 1 byte (41-45), 58-59
  header      91   prefix (19) + hex(frame id u16 BE, FICH 4) + 60 B
                   callsign data: dest, src, down, up (10 each), rem1-4 (5 each)
  voice      181   prefix + hex(frame id, FICH, DCH 10, five 13-byte V/D
                   type 2 VCH) = 100 ms
  terminator  31   prefix + hex(frame id, FICH)

Prefix: 00, size - 16, five tag bytes (00 00 00 00 07; the terminator
ends in 00), time u24 BE (frame id x 100), stream id u16 BE, 4 x 00, a
type character (I header, 2 voice, T terminator), "**".

FICH is the four raw field bytes of the Yaesu air interface
(dvtest.proto.ysf.fich_bytes); a VCH is the V/D type 2 voice channel
(ysf.vch_encode / vch_decode, 9-byte DMR-form AMBE+2 frames).
"""
import socket
import threading
import time

from . import ysf

PORT = 21110
PING_SIZE, INFO_SIZE, HEADER_SIZE, VOICE_SIZE, TERMINATOR_SIZE = 16, 60, 91, 181, 31
FRAMES_PER_PACKET = 5
PACKET_GAP = 0.100
PING = bytes((0x00, 0x00, 0x07)) + bytes(13)
INFO_TAG = bytes((0x00, 0x2C, 0x08, 0x00)) + bytes(12)
DG_ID_BASE = 10                         # xlxd: DG-ID 10 + n = module 'A' + n
KIND = {HEADER_SIZE: "header", VOICE_SIZE: "voice", TERMINATOR_SIZE: "terminator",
        PING_SIZE: "ping", INFO_SIZE: "info"}
TYPE_CHAR = {"header": b"I", "voice": b"2", "terminator": b"T"}


def module_dgid(module):
    return DG_ID_BASE + ord(module) - ord("A")


def is_ping(data):
    return data == PING


def info(callsign, firmware=0, mac=bytes(6), radio_id=b"00000", dgids=(), tail=b"\x00\x00"):
    """60-byte info (connect / pong) packet."""
    bitmap = 0
    for dgid in dgids:
        bitmap |= 1 << dgid
    out = bytearray(INFO_SIZE)
    out[0:16] = INFO_TAG
    out[16:20] = firmware.to_bytes(4, "little")
    out[20:26] = mac[:6].ljust(6, b"\x00")
    out[26:36] = callsign.encode("ascii")[:10].ljust(10, b" ")
    out[36:41] = radio_id[:5].ljust(5, b" ")
    out[41:46] = bitmap.to_bytes(5, "little")
    out[58:60] = tail[:2]
    return bytes(out)


def parse_info(data):
    """dict of a 60-byte info packet, or None."""
    if len(data) != INFO_SIZE or data[:16] != INFO_TAG:
        return None
    bitmap = int.from_bytes(data[41:46], "little")
    return {"firmware": int.from_bytes(data[16:20], "little"), "mac": data[20:26],
            "callsign": data[26:36].decode("ascii", "replace").strip(" \x00"),
            "radio_id": data[36:41], "dgids": sorted(i for i in range(40) if bitmap >> i & 1),
            "tail": data[58:60]}


def _prefix(size, kind, sid, fid):
    tag = bytes((0, 0, 0, 0, 0 if kind == "terminator" else 7))
    return (bytes((0, size - 16)) + tag + ((fid * 100) & 0xFFFFFF).to_bytes(3, "big")
            + (sid & 0xFFFF).to_bytes(2, "big") + bytes(4) + TYPE_CHAR[kind] + b"**")


def _hex(data):
    return data.hex().upper().encode("ascii")


def fich(fi, dgid, fn=0, ft=6, cm=ysf.CM_GROUP):
    return ysf.fich_bytes(fi, cs=2, cm=cm, fn=fn, ft=ft, mr=0, dt=ysf.DT_VD_MODE2, sq=dgid)


def _field(text, size):
    return text.encode("ascii")[:size].ljust(size, b" ") if isinstance(text, str) else text[:size].ljust(size, b" ")


def header(sid, my, rpt1, dgid, fid=0, dest="*****", down="", rem=""):
    """91-byte header: FICH FI 0, CM 1 as xlxd sends; callsign data plain."""
    body = (_field(dest, 10) + _field(my, 10) + _field(down, 10) + _field(rpt1, 10) + _field(rem, 20))
    return (_prefix(HEADER_SIZE, "header", sid, fid) + _hex(fid.to_bytes(2, "big") + fich(0, dgid, cm=1))
            + body)


def voice(sid, frames9, dgid, fid, fn=0, dch=b"**********"):
    """181-byte voice packet: five DMR-form frames as V/D type 2 VCH."""
    if len(frames9) != FRAMES_PER_PACKET:
        raise ValueError("an IMRS voice packet carries 5 frames")
    body = fid.to_bytes(2, "big") + fich(1, dgid, fn=fn) + dch[:10].ljust(10, b" ")
    body += b"".join(ysf.vch_encode(f) for f in frames9)
    return _prefix(VOICE_SIZE, "voice", sid, fid) + _hex(body)


def terminator(sid, dgid, fid):
    return _prefix(TERMINATOR_SIZE, "terminator", sid, fid) + _hex(fid.to_bytes(2, "big") + fich(2, dgid, fn=1))


def stream_packets(frames9, sid, my, rpt1, dgid, head=True, end=True, fid0=1):
    """Header, voice packets (frames padded with silence to a multiple of
    five), terminator."""
    frames9 = list(frames9) + [ysf.SILENCE] * (-len(frames9) % FRAMES_PER_PACKET)
    out = [header(sid, my, rpt1, dgid)] if head else []
    for index in range(0, len(frames9), FRAMES_PER_PACKET):
        out.append(voice(sid, frames9[index:index + 5], dgid, fid0 + index // 5))
    if end:
        out.append(terminator(sid, dgid, fid0 + len(frames9) // 5))
    return out


def parse(data):
    """dict of a header / voice / terminator / ping / info packet, or None."""
    kind = KIND.get(len(data))
    if kind == "ping":
        return {"kind": "ping"} if is_ping(data) else None
    if kind == "info":
        got = parse_info(data)
        return dict(got, kind="info") if got else None
    if kind is None or data[0] != 0 or data[1] != len(data) - 16:
        return None
    try:
        raw = bytes.fromhex(data[19:31].decode("ascii"))
    except ValueError:
        return None
    out = {"kind": kind, "tag": data[2:7], "time": int.from_bytes(data[7:10], "big"),
           "sid": int.from_bytes(data[10:12], "big"), "type": data[16:17], "fid": int.from_bytes(raw[:2], "big"),
           "fich": ysf.parse_fich_bytes(raw[2:6])}
    if kind == "header":
        cs = data[31:91]
        out.update({"dest": cs[0:10], "src": cs[10:20], "down": cs[20:30], "up": cs[30:40], "rem": cs[40:60]})
    elif kind == "voice":
        body = bytes.fromhex(data[19:181].decode("ascii"))
        out["dch"] = body[6:16]
        out["frames"] = [ysf.vch_decode(body[16 + 13 * k:29 + 13 * k]) for k in range(FRAMES_PER_PACKET)]
    return out


class Hub:
    """The tester's single IMRS socket on UDP 21110 (every IMRS peer sends
    there), drained by a background thread; packets are kept per source IP
    with their arrival times. Pings get no answer unless answer_pings."""

    def __init__(self, bind_ip="0.0.0.0", port=PORT, answer_pings=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(0.05)
        self.answer_pings = answer_pings
        self.rx = []                         # (monotonic, (ip, port), bytes)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            with self.lock:
                self.rx.append((time.monotonic(), addr, data))

    def send(self, ip, data, port=PORT):
        self.sock.sendto(data, (ip, port))

    def mark(self):
        with self.lock:
            return len(self.rx)

    def since(self, mark, ip=None):
        """(time, addr, data) received from ip since mark."""
        with self.lock:
            return [r for r in self.rx[mark:] if ip is None or r[1][0] == ip]

    def packets(self, mark, ip):
        """Parsed stream packets (header / voice / terminator) from ip since mark."""
        out = []
        for stamp, _, data in self.since(mark, ip):
            info = parse(data)
            if info and info["kind"] in ("header", "voice", "terminator"):
                out.append(dict(info, at=stamp))
        return out

    def paced(self, ip, packets, gap=PACKET_GAP):
        start = time.monotonic()
        for index, data in enumerate(packets):
            delay = start + index * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.send(ip, data)

    def close(self):
        self.stop.set()
        self.thread.join(1.0)
        self.sock.close()
