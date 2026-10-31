# SPDX-License-Identifier: MIT
"""Pure helpers of the M17 stage 2 relay cases (cases/proto/test_m17_relay.py,
docs/protocols/m17.md rows P-M17-61..73): what a stream datagram that left a
reflector has to look like, compared byte for byte with what the tester sent.

A stream datagram to a client is the 54 B body; to a configured reflector peer
it is the 55 B form: the body plus one trailer byte, the module letter of the
originating module ([M17-2] "Stream and packet mode data passed between
reflectors"). The CRC (bytes 52..53) covers bytes 0..51 only, the trailer is
outside it. All functions return lists of problem strings (empty = fine), so a
failing case says which datagram differs in which field.
"""
from .proto import m17

BODY = m17.PACKET_SIZE                      # 54
FIELDS = ((0, 4, "magic"), (4, 6, "stream_id"), (6, 12, "dst"), (12, 18, "src"), (18, 20, "type"),
          (20, 34, "meta"), (34, 36, "fn"), (36, 52, "payload"), (52, 54, "crc"))


def stream_id(datagram):
    return int.from_bytes(datagram[4:6], "big")


def is_stream(datagram):
    return datagram[:4] == b"M17 " and len(datagram) in (BODY, BODY + 1)


def group_by_stream(datagrams):
    """{stream id: [datagram, ...]} in arrival order, stream datagrams (54 B
    and 55 B) only."""
    out = {}
    for data in datagrams:
        if is_stream(data):
            out.setdefault(stream_id(data), []).append(bytes(data))
    return out


def crc_ok(body):
    return len(body) == BODY and int.from_bytes(body[52:54], "big") == m17.crc16(body[:52])


def with_dst(body, dst):
    """body with its destination replaced (dst as m17.address() takes it) and
    the CRC recomputed over bytes 0..51: what a reflector that rewrites a
    reflector designation to broadcast must send."""
    out = bytearray(body)
    out[6:12] = m17.address(dst)
    out[52:54] = m17.crc16(bytes(out[:52])).to_bytes(2, "big")
    return bytes(out)


def with_crc(body, value):
    """body with the CRC field forced to value."""
    return bytes(body[:52]) + int(value).to_bytes(2, "big")


def diff_fields(want, got):
    """Names of the stream fields in which two 54 B bodies differ."""
    return [name for lo, hi, name in FIELDS if want[lo:hi] != got[lo:hi]]


def fn_of(body):
    value = int.from_bytes(body[34:36], "big")
    return value & 0x7FFF, bool(value & 0x8000)


def trailer_text(byte):
    return chr(byte) if 0x20 <= byte < 0x7F else "0x%02x" % byte


def egress_problems(want, got, letter=None, trailer=True):
    """Datagrams (bytes) a receiver got for one stream against the bodies
    that must have reached it, in order. letter None: a client, 54 B each;
    a module letter: a reflector peer, 55 B each ending in that letter.
    trailer False: compare only the first 54 bytes (size and trailer not
    judged)."""
    problems = []
    if len(got) != len(want):
        problems.append("%d datagrams, want %d" % (len(got), len(want)))
    size = BODY if letter is None else BODY + 1
    for index, (body, data) in enumerate(zip(want, got)):
        if trailer and len(data) != size:
            problems.append("packet %d: %d B, want %d B" % (index, len(data), size))
        if len(data) < BODY:
            continue
        if trailer and letter is not None and len(data) == size and data[BODY:] != letter.encode():
            problems.append("packet %d: trailer %s, want %s" % (index, trailer_text(data[BODY]), letter))
        if data[:BODY] != body:
            problems.append("packet %d: differs in %s" % (index, ",".join(diff_fields(body, data[:BODY]))))
        elif not crc_ok(data[:BODY]):
            problems.append("packet %d: bad CRC" % index)
    return problems[:8]


def fn_problems(datagrams):
    """FN of the received stream datagrams counts up by one and only the
    last has bit 15."""
    problems = []
    fns = [fn_of(data[:BODY]) for data in datagrams]
    for index, ((fn, last), (prev, _)) in enumerate(zip(fns[1:], fns)):
        if fn != (prev + 1) & 0x7FFF:
            problems.append("packet %d: FN %d after %d" % (index + 1, fn, prev))
    flags = [last for _, last in fns]
    if flags and (flags[-1] is not True or flags.count(True) != 1):
        problems.append("last-frame bit on packets %s" % [i for i, f in enumerate(flags) if f])
    return problems[:8]


def closing_problems(last_data, data, letter=None):
    """The synthesised last packet that follows a stream which timed out
    without bit 15 (decisions Q7): same stream id, FN = (last FN + 1) with
    bit 15, valid CRC, 54 B (client) or 55 B with the module letter (peer).
    last_data: the last packet the stream carried (any bytes of 54+)."""
    problems = []
    size = BODY if letter is None else BODY + 1
    if len(data) != size:
        return ["%d B, want %d B" % (len(data), size)]
    if letter is not None and data[BODY:] != letter.encode():
        problems.append("trailer %s, want %s" % (trailer_text(data[BODY]), letter))
    body = data[:BODY]
    if body[:4] != b"M17 ":
        problems.append("magic %r" % body[:4])
    if body[4:6] != last_data[4:6]:
        problems.append("stream id %d, want %d" % (stream_id(body), stream_id(last_data)))
    fn, last = fn_of(body)
    want_fn = (fn_of(last_data[:BODY])[0] + 1) & 0x7FFF
    if not last or fn != want_fn:
        problems.append("FN %d last=%s, want %d with bit 15" % (fn, last, want_fn))
    if not crc_ok(body):
        problems.append("bad CRC")
    return problems


def m17p_with_low_byte(want, data=b"hi\0", protocol=5, **fields):
    """An M17P packet (valid LSF and payload CRCs) whose payload CRC low byte
    (the last byte of the datagram, the one a reflector takes for the trailer
    when a peer left it out, decisions Q9) satisfies want: an int (that
    byte), True (an upper case letter) or False (not 0x41..0x5A). The user
    data gets a 2 byte counter until the CRC fits."""
    for index in range(4096):
        packet = m17.m17p_packet(protocol, data + (index.to_bytes(2, "big") if index else b""), **fields)
        low = packet[-1]
        if (low == want) if isinstance(want, int) and not isinstance(want, bool) \
                else (0x41 <= low <= 0x5A) == bool(want):
            return packet
    raise ValueError("no payload CRC with the wanted low byte")
