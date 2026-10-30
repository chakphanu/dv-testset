# SPDX-License-Identifier: MIT
"""P25 client of a P25Reflector (UDP 41000), speaking the network side of
P25Gateway: the MMDVM P25 network records, one UDP datagram per record.

xlxd talks P25 only as a peer of a P25Reflector; a test client links to
the reflector the way a gateway does and reaches xlxd through it.

Link: the client sends poll 0xF0 + callsign (10 B, space padded) and the
reflector echoes it back; repeat at least every 5 s (the reflector drops
a repeater after 120 s of silence). Unlink: 0xF1 + callsign.

Voice: 18 records per superframe, LDU1 (0x62..0x6A) then LDU2
(0x6B..0x73), one 11-byte IMBE frame per record, one record per 20 ms;
terminator 0x80 (17 B) ends the call. There is no header record on this
link: the first LDU1 opens the call. Non-voice fields this client fills:
LCF/MFID/service options (0x64), destination TG (0x65), source id
(0x66), ALGID 0x80 = clear (0x70). Link-control and encryption-sync
parity, low-speed data and status bytes are sent as zero."""
import time

from ..net import UdpClient, pace

PORT = 41000
POLL = 0xF0
UNLINK = 0xF1
CALLSIGN_LENGTH = 10
IMBE_SIZE = 11
FRAMES_PER_LDU = 9
TERMINATOR = 0x80
TERMINATOR_SIZE = 17
POLL_PERIOD = 5.0
GAP = 0.020

LDU1 = tuple(range(0x62, 0x6B))
LDU2 = tuple(range(0x6B, 0x74))
VOICE_TYPES = LDU1 + LDU2
# position within an LDU (0..8) -> (record size, IMBE offset)
SLOT_LAYOUT = ((22, 10), (14, 1)) + ((17, 5),) * 6 + ((16, 4),)

# IMBE frame of silence on the MMDVM P25 network (pads a call to whole LDUs)
SILENCE = bytes((0x04, 0x0C, 0xFD, 0x7B, 0xFB, 0x7D, 0xF2, 0x7B, 0x3D, 0x9E, 0x44))

LCF_GROUP = 0x00
LCF_PROTECTED = 0x80                  # P bit of the LCF octet
ALGID_CLEAR = 0x80
MI_TYPES = (0x6D, 0x6E, 0x6F)         # message indicator, 3 bytes each
PARITY_TYPES = (0x67, 0x68, 0x69, 0x71, 0x72)   # LC / ES Reed-Solomon parity bytes 1..3
LSD_TYPES = (0x6A, 0x73)              # low speed data bytes 1..2


def callsign10(text):
    return (text or "").upper().encode("ascii")[:CALLSIGN_LENGTH].ljust(CALLSIGN_LENGTH, b" ")


def poll_packet(callsign):
    return bytes([POLL]) + callsign10(callsign)


def unlink_packet(callsign):
    return bytes([UNLINK]) + callsign10(callsign)


def parse_poll(data):
    """(kind, callsign) of an 11-byte poll or unlink, else None."""
    if len(data) == 1 + CALLSIGN_LENGTH and data[0] in (POLL, UNLINK):
        return ("poll" if data[0] == POLL else "unlink"), data[1:].decode("ascii", "replace").rstrip()
    return None


def layout(rtype):
    """(size, IMBE offset) of a voice record type, None for other bytes."""
    if rtype in LDU1:
        return SLOT_LAYOUT[rtype - LDU1[0]]
    if rtype in LDU2:
        return SLOT_LAYOUT[rtype - LDU2[0]]
    return None


def record_type(index):
    """Record type of the index-th IMBE frame of a call (from 0)."""
    step = index % (2 * FRAMES_PER_LDU)
    return LDU1[step] if step < FRAMES_PER_LDU else LDU2[step - FRAMES_PER_LDU]


def record(rtype, imbe, src=0, dst=0, lcf=LCF_GROUP, mfid=0, algid=ALGID_CLEAR, kid=0, mi=bytes(9)):
    """One voice record. LDU1 0x64 carries LCF/MFID, 0x65 the destination,
    0x66 the source; LDU2 0x6D..0x6F carry the 72-bit message indicator
    (three bytes each), 0x70 ALGID and KID. Parity, low speed data and
    status bytes stay zero."""
    if len(imbe) != IMBE_SIZE:
        raise ValueError("IMBE frame must be %d bytes" % IMBE_SIZE)
    size, offset = layout(rtype)
    out = bytearray(size)
    out[0] = rtype
    out[offset:offset + IMBE_SIZE] = imbe
    if rtype == 0x64:
        out[1] = lcf                      # service options (3) zero
        out[2] = mfid
    elif rtype == 0x65:
        out[1:4] = dst.to_bytes(3, "big")
    elif rtype == 0x66:
        out[1:4] = src.to_bytes(3, "big")
    elif rtype in MI_TYPES:
        part = MI_TYPES.index(rtype) * 3
        out[1:4] = mi[part:part + 3]
    elif rtype == 0x70:
        out[1] = algid
        out[2:4] = kid.to_bytes(2, "big")
    return bytes(out)


def terminator():
    out = bytearray(TERMINATOR_SIZE)
    out[0] = TERMINATOR
    return bytes(out)


def pad(frames):
    """Frames padded with SILENCE to whole LDUs (a radio sends whole LDUs)."""
    frames = list(frames)
    return frames + [SILENCE] * (-len(frames) % FRAMES_PER_LDU)


def voice_records(frames, src, dst):
    """Records of a call: frames padded to whole LDUs, then the terminator."""
    return call_records(frames, src, dst)


def call_records(frames, src, dst, start=0, padded=True, end=True, **fields):
    """Records of a call with control over the shape: start = index of the
    first record in the superframe (0 = 0x62), padded = complete the last
    LDU with SILENCE, end = append the terminator; fields go to record()
    (lcf, mfid, algid, kid, mi)."""
    frames = pad(frames) if padded else list(frames)
    out = [record(record_type(start + i), f, src=src, dst=dst, **fields) for i, f in enumerate(frames)]
    if end:
        out.append(terminator())
    return out


def resized(data, delta):
    """data with delta bytes appended (zeros) or cut from the end."""
    return bytes(data) + bytes(delta) if delta >= 0 else bytes(data[:delta])


def parse(data):
    """dict of a voice record or terminator, None for anything else."""
    if not data:
        return None
    rtype = data[0]
    if rtype == TERMINATOR and len(data) == TERMINATOR_SIZE:
        return {"type": rtype, "terminator": True}
    shape = layout(rtype)
    if shape is None or len(data) != shape[0]:
        return None
    offset = shape[1]
    out = {"type": rtype, "terminator": False, "ldu": 1 if rtype in LDU1 else 2,
           "imbe": bytes(data[offset:offset + IMBE_SIZE])}
    if rtype == 0x64:
        out["lcf"] = data[1]
    elif rtype == 0x65:
        out["dst"] = int.from_bytes(data[1:4], "big")
    elif rtype == 0x66:
        out["src"] = int.from_bytes(data[1:4], "big")
    elif rtype in MI_TYPES:
        out["mi"] = bytes(data[1:4])
    elif rtype == 0x70:
        out["algid"] = data[1]
        out["kid"] = int.from_bytes(data[2:4], "big")
    elif rtype in PARITY_TYPES:
        out["parity"] = bytes(data[1:4])
    elif rtype in LSD_TYPES:
        out["lsd"] = bytes(data[1:3])
    if rtype == 0x64:
        out["mfid"] = data[2]
        out["svc"] = data[3]
    return out


def call_fields(records):
    """Summary of parsed records (parse() dicts) of one or more calls: the
    distinct values of every control field, the MI in record order and the
    parity bytes per type."""
    out = {"lcf": set(), "mfid": set(), "svc": set(), "dst": set(), "src": set(), "algid": set(),
           "kid": set(), "lsd": set(), "mi": set(), "parity": {}}
    for rec in records:
        for key in ("lcf", "mfid", "svc", "dst", "src", "algid", "kid", "lsd", "mi"):
            if key in rec:
                out[key].add(rec[key])
        if "parity" in rec:
            out["parity"].setdefault(rec["type"], set()).add(rec["parity"])
    return out


def is_voice(data):
    return bool(data) and data[0] in VOICE_TYPES and parse(data) is not None


def lifecycle(types, whole_ldu=True):
    """Check a received record-type sequence: one or more calls, each
    LDU1 0x62..0x6A then LDU2 0x6B..0x73 in order, closed by 0x80 (after
    a whole LDU when whole_ldu). Returns a list of problems (empty =
    clean)."""
    problems = []
    step = None                     # next expected index in the superframe
    for pos, rtype in enumerate(types):
        if rtype == TERMINATOR:
            if step is None:
                problems.append("terminator %d without a call" % pos)
            elif whole_ldu and step % FRAMES_PER_LDU:
                problems.append("terminator %d inside an LDU" % pos)
            step = None
            continue
        if step is None:
            if rtype != LDU1[0]:
                problems.append("call opens with 0x%02x at %d, not 0x62" % (rtype, pos))
            step = 0
        expected = record_type(step)
        if rtype != expected:
            problems.append("record %d is 0x%02x, expected 0x%02x" % (pos, rtype, expected))
            step = VOICE_TYPES.index(rtype) if rtype in VOICE_TYPES else step
        step = (step + 1) % (2 * FRAMES_PER_LDU)
    if step is not None:
        problems.append("call not terminated")
    return problems


class P25Client:
    """One gateway-like client on one P25Reflector (one talkgroup)."""

    def __init__(self, bind_ip, callsign="N0CALL", src_id=1023007, tg=31672):
        self.client = UdpClient(bind_ip)
        self.callsign = callsign
        self.src_id = src_id
        self.tg = tg
        self.dest = None
        self.last_poll = 0.0

    def keepalive(self):
        self.client.send(self.dest, poll_packet(self.callsign))
        self.last_poll = time.monotonic()

    def pump(self, seconds):
        """Drain for seconds, polling every POLL_PERIOD."""
        deadline = time.monotonic() + seconds
        while True:
            if self.dest and time.monotonic() - self.last_poll >= POLL_PERIOD:
                self.keepalive()
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self.client.pump(min(left, 0.5))

    def link(self, node_ip, timeout=3.0, port=PORT):
        """Poll until the reflector echoes our poll."""
        self.dest = (node_ip, port)
        want = poll_packet(self.callsign)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.keepalive()
            self.client.pump(0.3)
            if want in self.client.inbox:
                return True
        return False

    def unlink(self):
        self.client.send(self.dest, unlink_packet(self.callsign))
        self.client.pump(0.2)

    def talk(self, frames, settle=0.0, tail=2.0, gap=GAP):
        """One call: frames (11-byte IMBE) padded to whole LDUs, one record
        per gap, then the terminator."""
        if time.monotonic() - self.last_poll >= POLL_PERIOD:
            self.keepalive()
        self.pump(settle)
        self.send(voice_records(frames, self.src_id, self.tg), gap)
        self.pump(tail)

    def send(self, records, gap=GAP, *others):
        """Send datagrams one per gap (monotonic schedule), draining this
        client and the other P25Clients after each one."""
        start = time.monotonic()
        for index, data in enumerate(records):
            pace(start, index, gap)
            self.client.send(self.dest, data)
            self.client.drain()
            for other in others:
                other.client.drain()

    def records(self):
        """Parsed voice records and terminators received, in order."""
        return [r for r in (parse(d) for d in self.client.inbox) if r is not None]

    def collect(self, settle=1.5):
        """IMBE frames from received voice records, in arrival order."""
        self.pump(settle)
        return [r["imbe"] for r in self.records() if not r["terminator"]]

    def close(self):
        self.client.close()
