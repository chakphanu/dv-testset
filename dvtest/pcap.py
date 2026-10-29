# SPDX-License-Identifier: MIT
"""Stdlib reader for classic pcap and pcapng captures: UDP datagrams only.

    stats = {}
    for d in udp_datagrams("x.pcapng", stats):
        d.frame, d.ts, d.src, d.sport, d.dst, d.dport, d.payload

frame is the 1-based packet number Wireshark shows; ts is seconds (float,
None for a pcapng simple packet). Classic pcap: both byte orders, us and ns
timestamps. pcapng: SHB/IDB/EPB/SPB (and the obsolete PB), both byte orders
per section, if_tsresol and if_tsoffset. Link types: Ethernet (1, VLAN
tags skipped), Linux SLL (113), SLL2 (276), raw IP (101, 228, 229), BSD
loopback (0). IPv4 and IPv6, UDP only. Everything else is counted in stats,
never dropped silently: packets, udp, non_ip, non_udp, fragments,
truncated, unsupported_link (by link type), bad_ip, bad_link.

write() and ethernet_udp() build synthetic captures for the unit tests.
"""
import struct
from collections import namedtuple

Datagram = namedtuple("Datagram", "frame ts src sport dst dport payload")

PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1e-6), b"\xa1\xb2\xc3\xd4": (">", 1e-6),
    b"\x4d\x3c\xb2\xa1": ("<", 1e-9), b"\xa1\xb2\x3c\x4d": (">", 1e-9),
}
SHB = 0x0A0D0D0A
BYTE_ORDER_MAGIC = 0x1A2B3C4D
ETH_IPV4, ETH_IPV6, ETH_VLAN = 0x0800, 0x86DD, (0x8100, 0x88A8, 0x9100)
IPV6_EXT = (0, 43, 60)


class CaptureError(ValueError):
    """The file is not a capture this reader understands, or is cut short."""


def _bump(stats, key, n=1):
    stats[key] = stats.get(key, 0) + n


def _ip_of(link, data, stats):
    """(ethertype-ish, ip bytes) for one link-layer frame, or None."""
    if link == 1:
        if len(data) < 14:
            return None
        kind = struct.unpack(">H", data[12:14])[0]
        offset = 14
        while kind in ETH_VLAN and len(data) >= offset + 4:
            kind = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            offset += 4
        return kind, data[offset:]
    if link == 113:
        return (struct.unpack(">H", data[14:16])[0], data[16:]) if len(data) >= 16 else None
    if link == 276:
        return (struct.unpack(">H", data[0:2])[0], data[20:]) if len(data) >= 20 else None
    if link in (101, 228, 229):
        if not data:
            return None
        return (ETH_IPV4 if data[0] >> 4 == 4 else ETH_IPV6 if data[0] >> 4 == 6 else 0), data
    if link == 0:
        if len(data) < 4:
            return None
        family = struct.unpack("<I", data[:4])[0]
        if family > 0xFFFF:
            family = struct.unpack(">I", data[:4])[0]
        return (ETH_IPV4 if family == 2 else ETH_IPV6 if family in (24, 28, 30) else 0), data[4:]
    _bump(stats, "unsupported_link")
    _bump(stats, "unsupported_link_%d" % link)
    return None


def _udp_of(kind, ip, stats):
    """(src, dst, udp bytes) or None; counts why not."""
    if kind == ETH_IPV4:
        if len(ip) < 20 or ip[0] >> 4 != 4:
            _bump(stats, "bad_ip")
            return None
        ihl = (ip[0] & 0x0F) * 4
        total = struct.unpack(">H", ip[2:4])[0]
        if ihl < 20 or total < ihl:
            _bump(stats, "bad_ip")
            return None
        if struct.unpack(">H", ip[6:8])[0] & 0x3FFF:
            _bump(stats, "fragments")
            return None
        if ip[9] != 17:
            _bump(stats, "non_udp")
            return None
        if len(ip) < total:
            _bump(stats, "truncated")
            return None
        return ".".join(map(str, ip[12:16])), ".".join(map(str, ip[16:20])), ip[ihl:total]
    if kind == ETH_IPV6:
        if len(ip) < 40:
            _bump(stats, "bad_ip")
            return None
        total = 40 + struct.unpack(">H", ip[4:6])[0]
        nxt, offset = ip[6], 40
        while nxt in IPV6_EXT and len(ip) >= offset + 8:
            nxt, offset = ip[offset], offset + 8 + ip[offset + 1] * 8
        if nxt == 44:
            _bump(stats, "fragments")
            return None
        if nxt != 17:
            _bump(stats, "non_udp")
            return None
        if len(ip) < total:
            _bump(stats, "truncated")
            return None
        return _ip6(ip[8:24]), _ip6(ip[24:40]), ip[offset:total]
    _bump(stats, "non_ip")
    return None


def _ip6(raw):
    return ":".join("%x" % w for w in struct.unpack(">8H", raw))


def _datagram(frame, ts, link, data, stats):
    _bump(stats, "packets")
    got = _ip_of(link, data, stats)
    if got is None:
        if link in (0, 1, 101, 113, 228, 229, 276):
            _bump(stats, "bad_link")
        return None
    got = _udp_of(got[0], got[1], stats)
    if got is None:
        return None
    src, dst, udp = got
    if len(udp) < 8:
        _bump(stats, "bad_ip")
        return None
    sport, dport, length = struct.unpack(">HHH", udp[:6])
    if length < 8 or length > len(udp):
        _bump(stats, "truncated")
        return None
    _bump(stats, "udp")
    return Datagram(frame, ts, src, sport, dst, dport, bytes(udp[8:length]))


def _classic(fh, head, stats):
    end, unit = PCAP_MAGIC[head[:4]]
    rest = fh.read(16)
    if len(rest) < 16:
        raise CaptureError("pcap header cut short")
    link = struct.unpack(end + "I", rest[12:16])[0] & 0xFFFF
    frame = 0
    while True:
        rec = fh.read(16)
        if not rec:
            return
        if len(rec) < 16:
            raise CaptureError("pcap record header cut short after frame %d" % frame)
        sec, frac, incl, _orig = struct.unpack(end + "IIII", rec)
        data = fh.read(incl)
        if len(data) < incl:
            raise CaptureError("pcap record cut short after frame %d" % frame)
        frame += 1
        d = _datagram(frame, sec + frac * unit, link, data, stats)
        if d:
            yield d


def _resolution(options, end):
    """(units per second, offset seconds) from IDB options."""
    unit, offset, pos = 1e-6, 0, 0
    while pos + 4 <= len(options):
        code, size = struct.unpack(end + "HH", options[pos:pos + 4])
        value = options[pos + 4:pos + 4 + size]
        if code == 0:
            break
        if code == 9 and size >= 1:
            unit = 2.0 ** -(value[0] & 0x7F) if value[0] & 0x80 else 10.0 ** -value[0]
        elif code == 14 and size >= 8:
            offset = struct.unpack(end + "q", value[:8])[0]
        pos += 4 + ((size + 3) & ~3)
    return unit, offset


def _pcapng(fh, head, stats):
    end = None
    ifaces = []          # [(link, snaplen, unit, offset)]
    frame = 0
    block = head
    while True:
        if len(block) < 8:
            if block:
                raise CaptureError("pcapng block header cut short after frame %d" % frame)
            return
        if block[:4] == b"\x0a\x0d\x0d\x0a":
            magic = fh.read(4)
            if magic == b"\x4d\x3c\x2b\x1a":
                end = "<"
            elif magic == b"\x1a\x2b\x3c\x4d":
                end = ">"
            else:
                raise CaptureError("pcapng section with a bad byte-order magic")
            size = struct.unpack(end + "I", block[4:8])[0]
            if size < 28 or size % 4:
                raise CaptureError("pcapng section header with a bad length")
            body = magic + fh.read(size - 16)
            ifaces = []
        else:
            if end is None:
                raise CaptureError("pcapng block before a section header")
            size = struct.unpack(end + "I", block[4:8])[0]
            if size < 12 or size % 4:
                raise CaptureError("pcapng block with a bad length after frame %d" % frame)
            body = fh.read(size - 12)
        if len(body) < size - 12:
            raise CaptureError("pcapng block cut short after frame %d" % frame)
        fh.read(4)
        kind = struct.unpack(end + "I", block[:4])[0]
        if kind == 1:
            link, _res, snap = struct.unpack(end + "HHI", body[:8])
            ifaces.append((link, snap) + _resolution(body[8:], end))
        elif kind in (2, 3, 6):
            frame += 1
            if kind == 6:
                iface, hi, lo, incl = struct.unpack(end + "IIII", body[:16])
                data = body[20:20 + incl]
            elif kind == 2:
                iface, _drops, hi, lo, incl = struct.unpack(end + "HHIII", body[:16])
                data = body[20:20 + incl]
            else:
                iface, hi, lo = 0, None, None
                orig = struct.unpack(end + "I", body[:4])[0]
                snap = ifaces[0][1] if ifaces else 0
                data = body[4:4 + (min(orig, snap) if snap else orig)]
            if iface >= len(ifaces):
                raise CaptureError("frame %d names an undeclared interface" % frame)
            link, _snap, unit, offset = ifaces[iface]
            ts = None if hi is None else ((hi << 32) | lo) * unit + offset
            d = _datagram(frame, ts, link, data, stats)
            if d:
                yield d
        block = fh.read(8)


def udp_datagrams(path, stats=None):
    """Yield Datagram for every complete, unfragmented UDP datagram in a
    pcap or pcapng file. stats (dict) collects counters."""
    stats = {} if stats is None else stats
    with open(path, "rb") as fh:
        head = fh.read(8)
        if head[:4] in PCAP_MAGIC:
            yield from _classic(fh, head, stats)
        elif head[:4] == b"\x0a\x0d\x0d\x0a":
            yield from _pcapng(fh, head, stats)
        else:
            raise CaptureError("%s is neither pcap nor pcapng" % path)



def ethernet_udp(src, sport, dst, dport, payload):
    """Ethernet + IPv4 + UDP frame (checksums zero) for synthetic captures."""
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0,
                     bytes(map(int, src.split("."))), bytes(map(int, dst.split("."))))
    return b"\x02\x00\x00\x00\x00\x02\x02\x00\x00\x00\x00\x01\x08\x00" + ip + udp


def write(path, packets, fmt="pcapng", end="<", link=1):
    """Write a capture of packets [(ts, frame bytes)]: fmt "pcapng" (one
    section, one interface, nanosecond EPBs) or "pcap" (microseconds)."""
    with open(path, "wb") as fh:
        if fmt == "pcap":
            fh.write(struct.pack(end + "IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, link))
            for ts, data in packets:
                sec = int(ts)
                fh.write(struct.pack(end + "IIII", sec, int(round((ts - sec) * 1e6)), len(data), len(data)) + data)
            return
        fh.write(struct.pack(end + "IIIHHqI", SHB, 28, BYTE_ORDER_MAGIC, 1, 0, -1, 28))
        opt = struct.pack(end + "HHB3xHH", 9, 1, 9, 0, 0)
        fh.write(struct.pack(end + "IIHHI", 1, 20 + len(opt), link, 0, 0) + opt + struct.pack(end + "I", 20 + len(opt)))
        for ts, data in packets:
            pad = b"\x00" * (-len(data) % 4)
            size = 32 + len(data) + len(pad)
            ns = int(round(ts * 1e9))
            fh.write(struct.pack(end + "IIIIIII", 6, size, 0, ns >> 32, ns & 0xFFFFFFFF, len(data), len(data))
                     + data + pad + struct.pack(end + "I", size))
