# SPDX-License-Identifier: MIT
"""Seeded, deterministic mutators over VALID packets of every protocol a
DV-Router edge speaks (docs/fuzz-cases.md). Pure: no sockets, no clock, no
files. Every valid seed packet is built with dvtest/proto/* (never written
by hand), and the offsets of the "interesting" fields are read from those
builders: a field is found by building the same packet twice with two
different values and taking the bytes that differ (locate_runs), so a
change of a builder moves the field with it.

  spec = protocol("dextra")                  seed packets by role + fields
  case = generate("dextra", seed, index)     one mutation, a pure function of
                                             (protocol, seed, index)
  for case in cases("dextra", seed): ...     index 0, 1, 2, ...

A case is reproducible from (protocol, seed, index) alone: its random stream
is random.Random("<protocol>:<seed>:<index>") (string seeding does not depend
on PYTHONHASHSEED) and the choice of mutator is index % len(MUTATORS). The
truncation mutator walks a fixed plan (truncation_plan) so that, over a run,
every length boundary of every seed packet is cut.

Limits: a datagram never exceeds MAX_DATAGRAM bytes and a sequence mutator
never returns more than MAX_BURST datagrams.
"""
import random
from collections import namedtuple
from functools import lru_cache

from .proto import (dcs, dextra, dmr, dmrplus, dplus, g3, imrs, m17, m17v3, nxdn, p25,
                    usrp, xlx, ysf)

MAX_DATAGRAM = 1400         # bytes of one mutated datagram (a seed is at most 519)
MAX_BURST = 64              # datagrams of one sequence mutator (storm, duplicates)
JUNK_MAX = 256              # bytes appended by the extension mutator
CALL = "N0FUZZ"             # callsign of every seed packet (never the valid client's N0CALL / N0TEST)
DMR_ID = 1023009            # repeater / radio id of the DMR seeds
NXDN_ID = 1003
P25_ID = 1023009
FRAME9 = bytes(range(1, 10))
FRAME8 = bytes(range(1, 9))
FRAME7 = bytes(range(1, 8))
FRAME11 = bytes(range(1, 12))
SID = 0x1234                # stream id of the seeds
SID_B = 0xABCD              # second stream id used only to locate fields

Field = namedtuple("Field", "name kind offset size endian")
Item = namedtuple("Item", "role data fields port")
Case = namedtuple("Case", "protocol seed index mutator port packets")

ROLES = ("control", "header", "voice", "terminator")
KINDS = ("magic", "callsign", "module", "tg", "id", "stream_id", "seq", "flags", "type", "len", "count")
NUMERIC = ("tg", "id", "stream_id", "seq", "flags", "type", "len", "count")
LENGTHY = ("len", "count", "type")

MODULES_BAD = (b"@", b"[", b"\x00", b"\xff", b" ", b"a", b"0", b"\x7f")
EXTREMES = (0, 1, 2, 0x7F, 0x80, 0xFF)

# protocol name -> default UDP port(s) of the DV-Router edge, from the proto modules
PORTS = {"dextra": (dextra.PORT,), "dplus": (dplus.PORT,), "dcs": (dcs.PORT,), "dmr": (dmr.PORT,),
         "dmrplus": (dmrplus.PORT,), "ysf": (ysf.PORT,), "m17": (m17.PORT,), "m17v3": (m17.PORT,),
         "nxdn": (nxdn.PORT,), "p25": (p25.PORT,), "usrp": (34001,), "xlx": (xlx.PORT,),
         "imrs": (imrs.PORT,), "g3": (g3.PRESENCE_PORT, g3.ROUTE_PORT, g3.DV_PORT)}
# one scenario per port of DV-Router: the protocols sent to that port (m17v3 shares the M17 port)
PORT_GROUPS = {"dextra": ("dextra",), "dplus": ("dplus",), "dcs": ("dcs",), "dmr": ("dmr",),
               "dmrplus": ("dmrplus",), "ysf": ("ysf",), "m17": ("m17", "m17v3"), "nxdn": ("nxdn",),
               "p25": ("p25",), "usrp": ("usrp",), "xlx": ("xlx",), "imrs": ("imrs",), "g3": ("g3",)}
PROTOCOLS = tuple(PORTS)
# the module letter of the second variant (the fuzz client on another module than the valid clients on B);
# None: the protocol has no module to move (usrp is bound to B by xlxd.usrp; nxdn / p25 name a TG, not a module)
OTHER_MODULE = {"dextra": "C", "dplus": "C", "dcs": "C", "dmr": "C", "dmrplus": "C", "ysf": "C", "m17": "C",
                "xlx": "C", "imrs": "C", "g3": "C", "usrp": None, "nxdn": None, "p25": None}


# ---------------------------------------------------------------- locating fields in builders

def locate_runs(a, b):
    """[(offset, size)] of the runs of bytes that differ between two packets
    of the same length."""
    if len(a) != len(b):
        raise ValueError("builder output changes size with the value (%d vs %d)" % (len(a), len(b)))
    runs = []
    for i in range(len(a)):
        if a[i] != b[i]:
            if runs and runs[-1][0] + runs[-1][1] == i:
                runs[-1][1] += 1
            else:
                runs.append([i, 1])
    return [(o, s) for o, s in runs]


def fields_of(build, value_a, value_b, name, kind, endian="big", limit=3):
    """Fields read from a builder: build(value) must differ between value_a
    and value_b only where the parameter lives."""
    runs = locate_runs(build(value_a), build(value_b))
    if not runs:
        raise ValueError("field %s: the two builds are equal" % name)
    return [Field(name if i == 0 else "%s%d" % (name, i + 1), kind, o, s, endian)
            for i, (o, s) in enumerate(runs[:limit])]


def item(role, data, *groups, tag=0, port=None):
    """Item of a seed: role, bytes, the fields (lists from fields_of), an optional
    leading tag of `tag` bytes (magic) and the UDP port when not the protocol's first."""
    fields = []
    if tag:
        fields.append(Field("tag", "magic", 0, tag, "big"))
    for group in groups:
        fields.extend(group)
    for f in fields:
        if f.offset < 0 or f.offset + f.size > len(data):
            raise ValueError("field %s outside the packet" % (f,))
    return Item(role, bytes(data), tuple(fields), port)


def _sid(build, name="stream_id", endian="little"):
    return fields_of(build, SID, SID_B, name, "stream_id", endian)


def _call8(build, name="callsign"):
    return fields_of(build, "AAAAAAAA", "BBBBBBBB", name, "callsign")


def _module(build, name="module"):
    return fields_of(build, "B", "Q", name, "module")


# ---------------------------------------------------------------- seeds per protocol

def _dextra(m="B"):
    h = lambda s, mm=m: dextra.header(s, CALL, mm)
    v = lambda s, last=False: dextra.voice(s, 1, FRAME9, last)
    return [
        item("control", dextra.link_packet(CALL, m), _call8(lambda c: dextra.link_packet(c, m)),
             _module(lambda mm: dextra.link_packet(CALL, mm))),
        item("control", dextra.unlink_packet(CALL, m), _call8(lambda c: dextra.unlink_packet(c, m))),
        item("control", dextra.keepalive_packet(CALL), _call8(dextra.keepalive_packet)),
        item("header", h(SID), _sid(h), _module(lambda mm: h(SID, mm)), tag=4),
        item("voice", v(SID), _sid(v), fields_of(lambda n: dextra.voice(SID, n, FRAME9, False), 1, 2, "seq", "seq"),
             tag=4),
        item("terminator", v(SID, True), _sid(lambda s: v(s, True)), tag=4),
    ]


def _shift(fields, by):
    """Fields located in a packet without its 2-byte DPlus length / type prefix."""
    return [f._replace(offset=f.offset + by) for f in fields]


def _dplus(m="B"):
    h = lambda s, mm=m: dplus.header(s, CALL, mm)
    v = lambda s: dplus.voice(s, 1, FRAME9)
    t = lambda s: dplus.last_voice(s, 3)
    return [
        item("control", dplus.CONNECT, tag=2),
        item("control", dplus.login_packet(CALL), fields_of(dplus.login_packet, "AAAAAAAA", "BBBBBBBB",
                                                           "callsign", "callsign"), tag=4),
        item("control", dplus.DISCONNECT, tag=2),
        item("control", dplus.KEEPALIVE, tag=2),
        item("header", h(SID), _shift(_sid(lambda s: h(s)[2:]), 2),
             _shift(_module(lambda mm: h(SID, mm)[2:]), 2), tag=2),
        item("voice", v(SID), _shift(_sid(lambda s: v(s)[2:]), 2), tag=2),
        item("terminator", t(SID), _shift(_sid(lambda s: t(s)[2:]), 2), tag=2),
    ]


def _dcs(m="B"):
    vo = lambda s, last=False: dcs.voice(s, 1, FRAME9, last, 5, CALL, m)
    return [
        item("control", dcs.link_packet(CALL, m), fields_of(lambda c: dcs.link_packet(c, m), "AAAAAAA",
                                                            "BBBBBBB", "callsign", "callsign"),
             _module(lambda mm: dcs.link_packet(CALL, mm))),
        item("control", dcs.unlink_packet(CALL, m), _module(lambda mm: dcs.unlink_packet(CALL, mm))),
        item("control", dcs.keepalive_packet(CALL, m), _module(lambda mm: dcs.keepalive_packet(CALL, mm, module=mm))),
        item("voice", vo(SID), _sid(vo), fields_of(lambda c: dcs.voice(SID, 1, FRAME9, False, c, CALL, m),
                                                   5, 0x010203, "counter", "count", "little"), tag=4),
        item("terminator", vo(SID, True), _sid(lambda s: vo(s, True)), tag=4),
    ]


def _dmr(m="B"):
    tg = dmr.module_tg(m)
    pk = lambda bits, sid=0x22222222, src=DMR_ID, dst=tg, payload=None: dmr.packet(
        1, src, dst, DMR_ID, bits, sid, payload or dmr.data_sync_payload())
    vp = lambda dst=tg, sid=0x22222222: dmr.voice_packets([FRAME9] * 3, DMR_ID, dst, sid)[0][0]
    return [
        item("control", dmr.rptl(DMR_ID), fields_of(dmr.rptl, 0x01020304, 0x05060708, "repeater", "id"), tag=4),
        item("control", dmr.rptk(DMR_ID), fields_of(dmr.rptk, 0x01020304, 0x05060708, "repeater", "id"), tag=4),
        item("control", dmr.rptc(DMR_ID, CALL), fields_of(dmr.rptc, 0x01020304, 0x05060708, "repeater", "id"),
             tag=4),
        item("control", dmr.rptping(DMR_ID), fields_of(dmr.rptping, 0x01020304, 0x05060708, "repeater", "id"),
             tag=7),
        item("control", dmr.rptcl(DMR_ID, 13), fields_of(lambda r: dmr.rptcl(r, 13), 0x01020304, 0x05060708,
                                                         "repeater", "id"), tag=5),
        item("control", dmr.rpto(DMR_ID, "TS2=%d" % tg), tag=4),
        item("header", pk(0xA1), fields_of(lambda s: pk(0xA1, sid=s), 0x01020304, 0x05060708, "stream_id", "stream_id",
                                           "little"),
             fields_of(lambda d: pk(0xA1, dst=d), 0x010203, 0x040506, "dst", "tg"),
             fields_of(lambda s: pk(0xA1, src=s), 0x010203, 0x040506, "src", "id"),
             fields_of(lambda b: pk(b), 0xA1, 0xA2, "bitfield", "flags"), tag=4),
        item("voice", vp(), fields_of(lambda s: vp(sid=s), 0x01020304, 0x05060708, "stream_id", "stream_id",
                                      "little"),
             fields_of(lambda d: vp(dst=d), 0x010203, 0x040506, "dst", "tg"), tag=4),
        item("terminator", pk(0xA2), tag=4),
    ]


def _dmrplus(m="B"):
    tg = dmrplus.reflector_of(m)
    st = dmrplus.stream([FRAME9] * 3, 1023009, tg)
    return [
        item("control", dmrplus.connect_packet(DMR_ID, tg),
             fields_of(lambda d: dmrplus.connect_packet(d, tg), 11111111, 22222222, "dmrid", "id"),
             fields_of(lambda r: dmrplus.connect_packet(DMR_ID, r), 1234, 5678, "reflector", "tg")),
        item("control", dmrplus.disconnect_packet(DMR_ID, m),
             fields_of(lambda d: dmrplus.disconnect_packet(d, m), 11111111, 22222222, "dmrid", "id"),
             _module(lambda mm: dmrplus.disconnect_packet(DMR_ID, mm))),
        item("header", st[0], fields_of(lambda d: dmrplus.stream([FRAME9] * 3, 1023009, d)[0], 0x010203, 0x040506, "dst", "tg",
                                        "little"),
             fields_of(lambda d: dmrplus.stream([FRAME9] * 3, d, tg)[0], 0x010203, 0x040506, "src", "id", "little")),
        item("voice", st[1], fields_of(lambda d: dmrplus.stream([FRAME9] * 3, 1023009, d)[1], 0x010203, 0x040506, "dst", "tg",
                                       "little")),
        item("terminator", st[-1], fields_of(lambda d: dmrplus.stream([FRAME9] * 3, 1023009, d)[-1], 0x010203,
                                             0x040506, "dst", "tg", "little")),
    ]


def _m17(m="B"):
    pk = lambda sid, seq=1, last=False, src=CALL: m17.stream_packet(sid, seq, bytes(range(16)), src=src, last=last)
    conn = lambda c, mm=m: m17.conn_packet(c, mm)
    return [
        item("control", conn(CALL), fields_of(conn, "AAAAAA", "BBBBBB", "callsign", "callsign", limit=1),
             _module(lambda mm: conn(CALL, mm)), tag=4),
        item("control", m17.lstn_packet(CALL, m), _module(lambda mm: m17.lstn_packet(CALL, mm)), tag=4),
        item("control", m17.disc_packet(CALL), tag=4),
        item("control", m17.pong_packet(CALL), tag=4),
        item("header", pk(SID, 0), fields_of(lambda s: pk(s, 0), 0x1234, 0xABCD, "stream_id", "stream_id", limit=1),
             fields_of(lambda s: pk(SID, 0, src=s), "AAAAAA", "BBBBBB", "src", "callsign", limit=1),
             fields_of(lambda n: pk(SID, n), 0x0101, 0x0202, "fn", "seq", limit=1), tag=4),
        item("voice", pk(SID, 1), fields_of(lambda s: pk(s, 1), 0x1234, 0xABCD, "stream_id", "stream_id", limit=1),
             fields_of(lambda n: pk(SID, n), 0x0101, 0x0202, "fn", "seq", limit=1), tag=4),
        item("terminator", pk(SID, 2, True), fields_of(lambda n: pk(SID, n, True), 0x0101, 0x0202, "fn", "seq", limit=1), tag=4),
    ]


def _m17v3(m="B"):
    voice_type = m17v3.v3_type(m17v3.PAYLOAD_VOICE3200)
    text_meta = m17v3.v3_text_blocks("N0FUZZ")[0]
    gnss = m17v3.gnss_meta(lat=10.0, lon=20.0)
    payloads = m17v3.payload_stream("fuzz", 3)

    def mk(fn, last=False, type_=voice_type, meta=bytes(14), sid=SID):
        return m17v3.frames_to_packets([(fn, last, payloads[0])], sid, src=CALL, type_=type_, meta=meta)[0]

    return [
        item("header", mk(0), fields_of(lambda t: mk(0, type_=t), voice_type, m17v3.v3_type(
            m17v3.PAYLOAD_DATA, meta=m17v3.META_TEXT), "type", "type", limit=1),
             fields_of(lambda s: mk(0, sid=s), 0x1234, 0xABCD, "stream_id", "stream_id", limit=1), tag=4),
        item("header", mk(0, type_=m17v3.v3_type(m17v3.PAYLOAD_VOICE3200, meta=m17v3.META_TEXT), meta=text_meta),
             fields_of(lambda m: mk(0, meta=m), bytes(14), bytes(range(1, 15)), "meta", "flags", limit=1), tag=4),
        item("header", mk(0, type_=m17v3.v3_type(m17v3.PAYLOAD_VOICE3200, meta=m17v3.META_GNSS), meta=gnss),
             fields_of(lambda m: mk(0, meta=m), bytes(14), bytes(range(1, 15)), "meta", "flags", limit=1), tag=4),
        item("voice", mk(1), fields_of(lambda n: mk(n), 0x0101, 0x0202, "fn", "seq", limit=1), tag=4),
        item("voice", mk(m17v3.SIG_FN_FIRST), fields_of(lambda n: mk(n), 0x0101, 0x0202, "fn", "seq", limit=1), tag=4),
        item("terminator", mk(2, True), tag=4),
    ]


def _ysf(m="B"):
    dg = ysf.module_dgid(m)
    st = ysf.stream_packets([ysf.SILENCE] * 5, CALL, dg)
    sp = lambda c, i=1: ysf.stream_packets([ysf.SILENCE] * 5, c, dg)[i]
    return [
        item("control", ysf.poll_packet(CALL), fields_of(ysf.poll_packet, "AAAAAAAAAA", "BBBBBBBBBB", "callsign",
                                                         "callsign"), tag=4),
        item("control", ysf.unlink_packet(CALL), tag=4),
        item("control", ysf.status_request(), tag=4),
        item("header", st[0], fields_of(lambda c: sp(c, 0), "AAAAAAAAAA", "BBBBBBBBBB", "gateway", "callsign"),
             tag=4),
        item("voice", st[1], fields_of(lambda c: sp(c), "AAAAAAAAAA", "BBBBBBBBBB", "gateway", "callsign"), tag=4),
        item("terminator", st[-1], fields_of(lambda c: sp(c, -1), "AAAAAAAAAA", "BBBBBBBBBB", "gateway",
                                             "callsign"), tag=4),
    ]


def _nxdn(m="B"):
    st = nxdn.stream_packets([FRAME7] * 4, NXDN_ID, 31672)
    sp = lambda src, i=0: nxdn.stream_packets([FRAME7] * 4, src, 31672)[i]
    dp = lambda dst, i=0: nxdn.stream_packets([FRAME7] * 4, NXDN_ID, dst)[i]
    ids = lambda i: (fields_of(lambda s: sp(s, i), 1001, 2002, "src", "id"),
                     fields_of(lambda d: dp(d, i), 0x1234, 0xABCD, "dst", "tg"))
    return [
        item("control", nxdn.poll_packet(CALL, 31672), fields_of(lambda c: nxdn.poll_packet(c, 31672), "AAAAAAAAAA",
                                                                 "BBBBBBBBBB", "callsign", "callsign"),
             fields_of(lambda t: nxdn.poll_packet(CALL, t), 0x1234, 0xABCD, "tg", "tg"), tag=5),
        item("control", nxdn.unlink_packet(CALL, 31672), tag=5),
        item("header", st[0], *ids(0), tag=5),
        item("voice", st[1], *ids(1), tag=5),
        item("terminator", st[-1], *ids(-1), tag=5),
    ]


def _p25(m="B"):
    recs = p25.voice_records([FRAME11] * 9, P25_ID, 31672)
    return [
        item("control", p25.poll_packet(CALL), fields_of(p25.poll_packet, "AAAAAAAAAA", "BBBBBBBBBB", "callsign",
                                                        "callsign"), tag=1),
        item("control", p25.unlink_packet(CALL), tag=1),
        item("header", recs[0], [Field("type", "type", 0, 1, "big")]),
        item("voice", recs[2], [Field("type", "type", 0, 1, "big")]),
        item("voice", recs[3], [Field("type", "type", 0, 1, "big")]),
        item("voice", p25.record(0x66, FRAME11, src=1), fields_of(lambda s: p25.record(0x66, FRAME11, src=s), 0x010203,
                                                                  0x040506, "src", "id"), [Field("type", "type", 0, 1, "big")]),
        item("terminator", recs[-1], [Field("type", "type", 0, 1, "big")]),
    ]


def _usrp(m="B"):
    pcm = bytes(320)
    seq = lambda n, kind=0: usrp.header(n, 1, kind=kind) + pcm
    return [
        item("control", usrp.text_set_info(0, CALL, tg=4002), fields_of(lambda n: usrp.text_set_info(n, CALL, tg=4002),
                                                                     0x01020304, 0x05060708, "seq", "seq", "big", limit=1), tag=4),
        item("control", usrp.ping(0, pad=True), tag=4),
        item("control", usrp.frame_le(0, usrp.TYPE_TLV, usrp.tlv(usrp.TLV_REMOTE_CMD, b"tgs=4002")),
             fields_of(lambda v: usrp.frame_le(0, usrp.TYPE_TLV, usrp.tlv(usrp.TLV_REMOTE_CMD, v)), b"tgs=1234",
                       b"tgs=5678", "tlv_value", "tg"),
             [Field("tlv_len", "len", usrp.HEADER_SIZE + 1, 1, "big")], tag=4),
        item("voice", usrp.voice(1, pcm), fields_of(lambda n: usrp.voice(n, pcm), 0x01020304, 0x05060708, "seq", "seq", "big", limit=1),
             fields_of(lambda n: usrp.header(1, 1, talkgroup=n) + pcm, 0x01020304, 0x05060708, "talkgroup", "tg", "big"),
             fields_of(lambda k: usrp.header(1, k) + pcm, 0x01020304, 0x05060708, "keyup", "flags", "big"), tag=4),
        item("voice", seq(1, usrp.TYPE_VOICE_ULAW), fields_of(lambda t: usrp.header(1, 1, kind=t) + pcm, 0x01020304, 0x05060708,
                                                              "type", "type", "big"), tag=4),
        item("terminator", usrp.unkey(2), fields_of(usrp.unkey, 0x01020304, 0x05060708, "seq", "seq", "big", limit=1), tag=4),
    ]


def _xlx(m="B"):
    xm = "D" if m == "B" else "E"       # the modules the XLX peer rows of the rig name are D and E
    return [
        item("control", xlx.connect("XLX996", "DE"), fields_of(lambda c: xlx.connect(c, "DE"), "AAAAAAAA", "BBBBBBBB",
                                                                "callsign", "callsign"),
             fields_of(lambda v: xlx.connect("XLX996", "DE", v), (2, 6, 1), (3, 7, 4), "version", "count"),
             fields_of(lambda mm: xlx.connect("XLX996", mm), "DE", "QR", "modules", "module")),
        item("control", xlx.ack("XLX996", "DE")),
        item("control", xlx.nack("XLX996"), tag=1),
        item("control", xlx.disconnect("XLX996"), tag=1),
        item("control", xlx.keepalive("XLX996")),
        item("header", xlx.header(SID, CALL, xm), _sid(lambda s: xlx.header(s, CALL, xm)),
             _module(lambda mm: xlx.header(SID, CALL, mm)), tag=4),
        item("voice", xlx.voice(SID, 1, FRAME9), _sid(lambda s: xlx.voice(s, 1, FRAME9)), tag=4),
        item("voice", xlx.voice(SID, 1, FRAME9, revision=1), _sid(lambda s: xlx.voice(s, 1, FRAME9, revision=1)),
             tag=4),
        item("voice", xlx.voice(SID, 1, FRAME9, revision=2), _sid(lambda s: xlx.voice(s, 1, FRAME9, revision=2)),
             tag=4),
        item("terminator", xlx.voice(SID, 2, FRAME9, last=True), _sid(lambda s: xlx.voice(s, 2, FRAME9, last=True)),
             tag=4),
    ]


def _imrs(m="B"):
    dg = imrs.module_dgid(m)
    st = imrs.stream_packets([ysf.SILENCE] * 5, 0x1234, CALL, CALL, dg)
    sp = lambda s, i: imrs.stream_packets([ysf.SILENCE] * 5, s, CALL, CALL, dg)[i]
    return [
        item("control", imrs.PING),
        item("control", imrs.info(CALL, dgids=(dg,)), fields_of(lambda c: imrs.info(c), "AAAAAAAAAA", "BBBBBBBBBB",
                                                                "callsign", "callsign"),
             fields_of(lambda d: imrs.info(CALL, dgids=(d,)), 11, 12, "dgid_bitmap", "flags")),
        item("header", st[0], _sid(lambda s: sp(s, 0)), tag=4),
        item("voice", st[1], _sid(lambda s: sp(s, 1)), tag=4),
        item("terminator", st[-1], _sid(lambda s: sp(s, -1)), tag=4),
    ]


def _g3(m="B"):
    dest = "XLX999 " + m
    pres, rt = g3.PRESENCE_PORT, g3.ROUTE_PORT
    st = g3.stream(SID, [FRAME9] * 2, CALL, dest)
    return [
        item("control", g3.presence_packet("N0FUZZ T"), fields_of(g3.presence_packet, "AAAAAAAA", "BBBBBBBB",
                                                                  "terminal", "callsign"), port=pres),
        item("control", g3.presence_packet("N0FUZZ T", header=bytes([0xFF] * 8)), port=pres),
        item("control", g3.route_packet(dest), fields_of(g3.route_packet, "AAAAAAAA", "BBBBBBBB", "destination",
                                                               "callsign"), port=rt),
        item("control", g3.route_packet(dest, repeater=False), port=rt),
        item("control", g3.hpch_packet("N0FUZZ"), tag=4, port=g3.DV_PORT),
        item("header", st[0], _sid(lambda s: g3.stream(s, [FRAME9] * 2, CALL, dest)[0]), tag=4,
             port=g3.DV_PORT),
        item("voice", st[1], _sid(lambda s: g3.stream(s, [FRAME9] * 2, CALL, dest)[1]), tag=4, port=g3.DV_PORT),
        item("terminator", st[-1], _sid(lambda s: g3.stream(s, [FRAME9] * 2, CALL, dest)[-1]), tag=4,
             port=g3.DV_PORT),
    ]


BUILDERS = {"dextra": _dextra, "dplus": _dplus, "dcs": _dcs, "dmr": _dmr, "dmrplus": _dmrplus, "m17": _m17,
            "m17v3": _m17v3, "ysf": _ysf, "nxdn": _nxdn, "p25": _p25, "usrp": _usrp, "xlx": _xlx, "imrs": _imrs,
            "g3": _g3}

Spec = namedtuple("Spec", "protocol ports items groups")


@lru_cache(maxsize=None)
def protocol(name, module="B"):
    """Spec of one protocol: its ports, the seed Items (valid packets with roles and
    fields) and the items grouped by port (a group is what one case draws from).
    module: the module letter the seed packets name (default B; the fuzz variant on
    another module uses OTHER_MODULE)."""
    if name not in BUILDERS:
        raise KeyError("unknown protocol %r (known: %s)" % (name, ", ".join(PROTOCOLS)))
    ports = PORTS[name]
    items = tuple(it._replace(port=it.port or ports[0]) for it in BUILDERS[name](module))
    groups = tuple(tuple(it for it in items if it.port == port) for port in ports)
    return Spec(name, ports, items, tuple(g for g in groups if g))


def interesting_fields(name):
    """[dict(role, index, name, kind, offset, size, endian, port)] of every field of
    every seed packet of a protocol."""
    out = []
    for index, it in enumerate(protocol(name).items):
        for f in it.fields:
            out.append(dict(role=it.role, item=index, name=f.name, kind=f.kind, offset=f.offset,
                            size=f.size, endian=f.endian, port=it.port, packet_size=len(it.data)))
    return out


def seed_packets(name):
    """[bytes] of the valid seed packets, in seed order."""
    return [it.data for it in protocol(name).items]


# ---------------------------------------------------------------- byte helpers

def _get(data, f):
    return int.from_bytes(data[f.offset:f.offset + f.size], f.endian)


def _put(data, f, value):
    out = bytearray(data)
    out[f.offset:f.offset + f.size] = (value & ((1 << (8 * f.size)) - 1)).to_bytes(f.size, f.endian)
    return bytes(out)


def _clip(data):
    return bytes(data[:MAX_DATAGRAM])


def _max(f):
    return (1 << (8 * f.size)) - 1


# ---------------------------------------------------------------- truncation plan

def boundaries(it):
    """Lengths at which a seed packet is cut: the first few bytes, both ends,
    the middle, and the start and end of every field."""
    size = len(it.data)
    cuts = {0, 1, 2, 3, 4, 5, size // 2, size - 2, size - 1}
    for f in it.fields:
        cuts.update((f.offset, f.offset + 1, f.offset + f.size - 1, f.offset + f.size))
    return sorted(c for c in cuts if 0 <= c < size)


@lru_cache(maxsize=None)
def truncation_plan(name, module="B"):
    """[(item index, cut length)]: every boundary of every seed packet, the
    k-th boundary of each packet before the (k+1)-th of any (so a short run
    still touches every packet)."""
    spec = protocol(name, module)
    per = [[(i, c) for c in boundaries(it)] for i, it in enumerate(spec.items)]
    plan = []
    for rank in range(max(len(p) for p in per)):
        plan.extend(p[rank] for p in per if rank < len(p))
    return tuple(plan)


# ---------------------------------------------------------------- packet mutators
# signature: (rng, spec, group, it) -> bytes ; `it` is the Item drawn from the group

def m_extend(rng, spec, group, it):
    junk_len = rng.randint(1, JUNK_MAX)
    style = rng.randrange(4)
    if style == 0:
        junk = bytes(rng.getrandbits(8) for _ in range(junk_len))
    elif style == 1:
        junk = bytes(junk_len)
    elif style == 2:
        junk = b"\xff" * junk_len
    else:
        tail = it.data[-16:] or b"\x00"
        junk = (tail * (junk_len // len(tail) + 1))[:junk_len]
    return _clip(it.data + junk)


def m_flip1(rng, spec, group, it):
    out = bytearray(it.data)
    pos = rng.randrange(len(out))
    out[pos] ^= 1 << rng.randrange(8)
    return bytes(out)


def m_flipn(rng, spec, group, it):
    out = bytearray(it.data)
    for _ in range(rng.randint(2, 16)):
        out[rng.randrange(len(out))] ^= 1 << rng.randrange(8)
    return bytes(out)


def _fit(it, kinds=None):
    """Fields of an item that still lie inside its (possibly already mutated) data."""
    return [f for f in it.fields if f.offset + f.size <= len(it.data) and (kinds is None or f.kind in kinds)]


def _numeric(it, kinds=NUMERIC):
    return _fit(it, kinds)


def m_off_by_one(rng, spec, group, it):
    """Length / count fields +-1, 0 and max; the datagram length itself +-1."""
    lengthy = _numeric(it, LENGTHY) or _numeric(it)
    if not lengthy or rng.random() < 0.35:
        if rng.random() < 0.5:
            return it.data[:-1]
        return _clip(it.data + bytes([rng.getrandbits(8)]))
    f = rng.choice(lengthy)
    cur = _get(it.data, f)
    return _put(it.data, f, rng.choice((cur + 1, cur - 1, 0, _max(f), _max(f) - 1)))


def m_extreme(rng, spec, group, it):
    nums = _numeric(it)
    if not nums:
        return m_flip1(rng, spec, group, it)
    out = it.data
    for f in rng.sample(nums, min(len(nums), rng.randint(1, 3))):
        out = _put(out, f, rng.choice(EXTREMES + (_max(f), _max(f) >> 1, rng.getrandbits(8 * f.size))))
    return out


def m_magic(rng, spec, group, it):
    magic = _fit(it, ("magic",))
    if not magic or not it.data:
        out = bytearray(it.data or b"\x00")
        out[0] ^= 0xFF
        return bytes(out)
    f = magic[0]
    others = [o.data[f.offset:f.offset + f.size] for o in spec.items if len(o.data) >= f.offset + f.size]
    style = rng.randrange(5)
    if style == 0:
        value = rng.choice(others)                       # the tag of another packet of the protocol
    elif style == 1:
        value = bytes(rng.getrandbits(8) for _ in range(f.size))
    elif style == 2:
        value = bytes(f.size)
    elif style == 3:
        value = b"\xff" * f.size
    else:
        value = bytes(b ^ 0x20 for b in it.data[f.offset:f.offset + f.size])     # case flip
    out = bytearray(it.data)
    out[f.offset:f.offset + f.size] = value
    return bytes(out)


def m_module(rng, spec, group, it):
    mods = _fit(it, ("module",))
    if not mods:
        calls = _fit(it, ("callsign",))
        if not calls:
            return m_flip1(rng, spec, group, it)
        f = rng.choice(calls)                            # a callsign field: bytes outside the alphabet
        out = bytearray(it.data)
        out[f.offset + rng.randrange(f.size)] = rng.choice((0x00, 0xFF, 0x80, 0x0A, 0x7F, 0x22))
        return bytes(out)
    out = bytearray(it.data)
    for f in rng.sample(mods, rng.randint(1, len(mods))):
        value = rng.choice(MODULES_BAD)
        out[f.offset:f.offset + f.size] = (value * f.size)[:f.size]
    return bytes(out)


def m_ids(rng, spec, group, it):
    """Out-of-range TG, radio id, stream id and sequence values."""
    ids = _numeric(it, ("tg", "id", "stream_id", "seq"))
    if not ids:
        return m_extreme(rng, spec, group, it)
    f = rng.choice(ids)
    cur = _get(it.data, f)
    if f.kind == "tg":
        pool = (0, 1, 4000, 4027, 4096, 9999, 0xFFFF, _max(f), cur ^ 1, cur + 26)
    elif f.kind == "stream_id":
        pool = (0, 1, _max(f), cur ^ 0xFFFF, cur + 1)
    else:
        pool = (0, 1, 0x7FFF, 0x8000, _max(f), cur + 1, cur - 1)
    return _put(it.data, f, rng.choice(pool))


def m_fill(rng, spec, group, it):
    size = len(it.data)
    byte = rng.choice((0x00, 0xFF))
    style = rng.randrange(4)
    if style == 0:
        return bytes([byte]) * size
    keep = 0
    for f in _fit(it):
        if f.kind == "magic":
            keep = max(keep, f.offset + f.size)
    if style == 1:
        return it.data[:keep] + bytes([byte]) * (size - keep)
    start = rng.randrange(size)
    end = min(size, start + rng.randint(1, max(1, size // 2)))
    out = bytearray(it.data)
    out[start:end] = bytes([byte]) * (end - start)
    if style == 2:
        return bytes(out)
    return bytes(out[:max(1, start)]) + bytes([byte]) * rng.randint(0, 32)


PACKET_MUTATORS = (m_extend, m_flip1, m_flipn, m_off_by_one, m_extreme, m_magic, m_module, m_ids, m_fill)


def m_compound(rng, spec, group, it):
    """Two or three packet mutators one after the other on one packet."""
    data = it.data
    for _ in range(rng.randint(2, 3)):
        step = rng.choice(PACKET_MUTATORS)
        if not data:
            break                                           # nothing left to mutate (a cut to zero bytes)
        data = step(rng, spec, group, it._replace(data=data))
    return _clip(data)


# ---------------------------------------------------------------- sequence mutators
# signature: (rng, spec, group) -> [bytes]

def _stream(group):
    """header + voice + terminator items of a group, seed order; the whole group when it has none."""
    seq = [it for it in group if it.role in ("header", "voice", "terminator")]
    return seq or list(group)


def _of(group, role):
    return [it for it in group if it.role == role]


def s_duplicate(rng, spec, group):
    stream = _stream(group)
    out = []
    for it in stream:
        out.append(it.data)
        if rng.random() < 0.5:
            out.extend([it.data] * rng.randint(1, 8))
    if rng.random() < 0.3:
        out = out + out
    return [_clip(p) for p in out[:MAX_BURST]]


def s_reorder(rng, spec, group):
    out = [it.data for it in _stream(group)]
    style = rng.randrange(4)
    if style == 0:
        out.reverse()
    elif style == 1:
        rng.shuffle(out)
    elif style == 2:
        out = out[-1:] + out[:-1]                         # terminator first
    else:
        out = out[1:] + out[:1]                           # header last
    return [_clip(p) for p in out[:MAX_BURST]]


def s_header_only(rng, spec, group):
    heads = _of(group, "header") or _of(group, "control") or list(group)
    it = rng.choice(heads)
    return [it.data] * rng.randint(1, 4)


def s_voice_only(rng, spec, group):
    voice = _of(group, "voice") or list(group)
    out = [voice[i % len(voice)].data for i in range(rng.randint(1, 32))]
    return out[:MAX_BURST]


def s_term_storm(rng, spec, group):
    terms = _of(group, "terminator") or [group[-1]]
    it = rng.choice(terms)
    return [it.data] * rng.randint(8, MAX_BURST)


SEQUENCE_MUTATORS = {"duplicate": s_duplicate, "reorder": s_reorder, "header_only": s_header_only,
                     "voice_only": s_voice_only, "term_storm": s_term_storm}

# the table indexed by index % len(MUTATORS); "truncate" twice so a run cuts every boundary
MUTATORS = ("truncate", "extend", "flip1", "flipn", "off_by_one", "extreme", "magic", "module", "ids",
            "fill", "truncate", "duplicate", "reorder", "header_only", "voice_only", "term_storm", "compound")
_PACKET = {"extend": m_extend, "flip1": m_flip1, "flipn": m_flipn, "off_by_one": m_off_by_one,
           "extreme": m_extreme, "magic": m_magic, "module": m_module, "ids": m_ids, "fill": m_fill,
           "compound": m_compound}
_TRUNC_SLOTS = tuple(i for i, n in enumerate(MUTATORS) if n == "truncate")


def rng_for(name, seed, index):
    return random.Random("%s:%s:%d" % (name, seed, index))


def generate(name, seed, index, module="B"):
    """The index-th case of (protocol, seed): a pure function of its arguments."""
    spec = protocol(name, module)
    rng = rng_for(name, seed, index)
    slot = index % len(MUTATORS)
    mutator = MUTATORS[slot]
    if mutator == "truncate":
        plan = truncation_plan(name, module)
        ordinal = (index // len(MUTATORS)) * len(_TRUNC_SLOTS) + _TRUNC_SLOTS.index(slot)
        item_index, cut = plan[ordinal % len(plan)]
        it = spec.items[item_index]
        return Case(name, seed, index, mutator, it.port, [it.data[:cut]])
    group = spec.groups[rng.randrange(len(spec.groups))]
    port = group[0].port
    if mutator in SEQUENCE_MUTATORS:
        return Case(name, seed, index, mutator, port, SEQUENCE_MUTATORS[mutator](rng, spec, group))
    it = group[rng.randrange(len(group))]
    return Case(name, seed, index, mutator, port, [_clip(_PACKET[mutator](rng, spec, group, it))])


def cases(name, seed, start=0, module="B"):
    """Endless iterator of generate(name, seed, i, module) for i = start, start+1, ..."""
    index = start
    while True:
        yield generate(name, seed, index, module)
        index += 1
