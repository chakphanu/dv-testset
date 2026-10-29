# SPDX-License-Identifier: MIT
"""dvtest.pcap: classic pcap and pcapng in both byte orders, timestamp
resolutions, link types, IPv6, and the counted skips (fragments, non-UDP)."""
import struct

import pytest

from dvtest import pcap

SRC, DST = "192.0.2.1", "192.0.2.2"


def ipv4_udp(payload, sport=1000, dport=2000, frag=0x4000, proto=17):
    udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
    return struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, frag, 64, proto, 0,
                       bytes([192, 0, 2, 1]), bytes([192, 0, 2, 2])) + udp


def ipv6_udp(payload, ext=False):
    udp = struct.pack(">HHHH", 1000, 2000, 8 + len(payload), 0) + payload
    body = (bytes([17, 0]) + bytes(6) + udp) if ext else udp
    src, dst = bytes(15) + b"\x01", bytes(15) + b"\x02"
    return struct.pack(">IHBB", 6 << 28, len(body), 0 if ext else 17, 64) + src + dst + body


def ether(ip, kind=0x0800, vlan=False):
    tag = struct.pack(">HH", 0x8100, 7) if vlan else b""
    return b"\x02" * 6 + b"\x04" * 6 + tag + struct.pack(">H", kind) + ip


def sll(ip, kind=0x0800):
    return struct.pack(">HHH8sH", 0, 1, 6, b"\x02" * 6, kind) + ip


def sll2(ip, kind=0x0800):
    return struct.pack(">HHIHBB8s", kind, 0, 2, 1, 0, 6, b"\x02" * 6) + ip


def block(end, kind, body):
    body += b"\x00" * (-len(body) % 4)
    return struct.pack(end + "II", kind, 12 + len(body)) + body + struct.pack(end + "I", 12 + len(body))


def idb(end, link, tsresol=None, offset=None, snap=0):
    opts = b""
    if tsresol is not None:
        opts += struct.pack(end + "HHB3x", 9, 1, tsresol)
    if offset is not None:
        opts += struct.pack(end + "HHq", 14, 8, offset)
    if opts:
        opts += struct.pack(end + "HH", 0, 0)
    return block(end, 1, struct.pack(end + "HHI", link, 0, snap) + opts)


def shb(end):
    return block(end, pcap.SHB, struct.pack(end + "IHHq", pcap.BYTE_ORDER_MAGIC, 1, 0, -1))


def epb(end, iface, ticks, data):
    return block(end, 6, struct.pack(end + "IIIII", iface, ticks >> 32, ticks & 0xFFFFFFFF,
                                     len(data), len(data)) + data)


def read(path):
    stats = {}
    return list(pcap.udp_datagrams(path, stats)), stats


@pytest.mark.parametrize("end", ["<", ">"])
def test_pcapng_links_resolutions_and_spb(tmp_path, end):
    data = (shb(end) + idb(end, 1) + idb(end, 113, tsresol=9) + idb(end, 276, tsresol=0x80 | 10, offset=100)
            + idb(end, 101, tsresol=3)
            + epb(end, 0, 1_500_000, ether(ipv4_udp(b"eth"), vlan=True))
            + epb(end, 1, 2_000_000_000, sll(ipv4_udp(b"sll")))
            + epb(end, 2, 3 * 1024, sll2(ipv4_udp(b"sll2")))
            + epb(end, 3, 4000, ipv6_udp(b"raw6", ext=True))
            + block(end, 3, struct.pack(end + "I", 14 + 28 + 3) + ether(ipv4_udp(b"spb")))
            + block(end, 5, b"stats block is skipped"))
    path = tmp_path / "x.pcapng"
    path.write_bytes(data)
    got, stats = read(path)
    assert [(d.frame, d.payload) for d in got] == [(1, b"eth"), (2, b"sll"), (3, b"sll2"), (4, b"raw6"), (5, b"spb")]
    assert [d.ts for d in got[:4]] == [pytest.approx(1.5), pytest.approx(2.0), pytest.approx(103.0),
                                      pytest.approx(4.0)]
    assert got[4].ts is None and (got[0].src, got[0].sport, got[0].dst, got[0].dport) == (SRC, 1000, DST, 2000)
    assert got[3].src == "0:0:0:0:0:0:0:1"
    assert stats == {"packets": 5, "udp": 5}


@pytest.mark.parametrize("end,magic,unit", [("<", 0xA1B2C3D4, 1e-6), (">", 0xA1B2C3D4, 1e-6),
                                            ("<", 0xA1B23C4D, 1e-9), (">", 0xA1B23C4D, 1e-9)])
def test_classic_pcap_and_counted_skips(tmp_path, end, magic, unit):
    frames = [ether(ipv4_udp(b"one")),
              ether(ipv4_udp(b"frag", frag=0x2000)),            # MF set
              ether(ipv4_udp(b"tcp", proto=6)),
              ether(b"\x00" * 28, kind=0x0806),                 # ARP
              ether(ipv4_udp(b"two")) + b"\x00" * 6,            # Ethernet padding
              ether(ipv4_udp(b"cut"))[:-2]]                     # snapped short
    data = struct.pack(end + "IHHiIII", magic, 2, 4, 0, 0, 65535, 1)
    for i, f in enumerate(frames):
        data += struct.pack(end + "IIII", 10 + i, 500, len(f), len(f)) + f
    path = tmp_path / "x.pcap"
    path.write_bytes(data)
    got, stats = read(path)
    assert [(d.frame, d.payload) for d in got] == [(1, b"one"), (5, b"two")]
    assert got[0].ts == pytest.approx(10 + 500 * unit)
    assert stats == {"packets": 6, "udp": 2, "fragments": 1, "non_udp": 1, "non_ip": 1, "truncated": 1}


def test_writer_round_trip_and_errors(tmp_path):
    rows = [(5.25 + i, pcap.ethernet_udp(SRC, 1000 + i, DST, 2000, bytes([i]) * i)) for i in range(4)]
    for fmt in ("pcap", "pcapng"):
        for end in "<>":
            path = tmp_path / ("w." + fmt)
            pcap.write(path, rows, fmt, end)
            got, _ = read(path)
            assert [(d.frame, d.sport, d.payload) for d in got] == [(i + 1, 1000 + i, bytes([i]) * i) for i in range(4)]
            assert got[3].ts == pytest.approx(8.25)
    bad = tmp_path / "bad.pcapng"
    bad.write_bytes(b"\x00" * 32)
    with pytest.raises(pcap.CaptureError):
        read(bad)
    cut = tmp_path / "cut.pcapng"
    pcap.write(cut, rows)
    cut.write_bytes(cut.read_bytes()[:-10])
    with pytest.raises(pcap.CaptureError):
        read(cut)
