# SPDX-License-Identifier: MIT
"""AX.25 UI frames, TNC2 text and KISS framing (HWFREE1 item 3, docs/direwolf-cases.md).

Pure Python, stdlib only. This is OUR encoder and parser: the expected bytes of
the cases come from Dire Wolf (rig/Dockerfile.direwolf, dvtest.oracle_direwolf),
this module only has to agree with them (cases/unit/test_ax25.py checks the
committed vectors vectors/direwolf/*.json).

What is covered:

* address field: 6 callsign characters shifted left by one, SSID byte
  `C R R S S S S X` (command/response or has-been-repeated bit, two reserved
  bits, 4-bit SSID, address-extension bit), up to 8 digipeaters;
* UI frames only: control 0x03, PID 0xF0 (or another PID), information field;
* TNC2 monitor text `SRC>DST[,DIGI[*]...]:info` with the `<0xHH>` escape for
  bytes that are not printable (this is the notation Dire Wolf's gen_packets
  accepts and prints; the notation is a fact about its input, not a standard);
* the frame check sequence (CRC-16/X-25) and the bit stuffing, so the number of
  bits on the air for a 1200 baud AFSK frame can be computed;
* KISS framing: FEND / FESC / TFEND / TFESC, port (high nibble) and command
  (low nibble), a streaming decoder.

Conventions that were READ FROM Dire Wolf's output and not from a standard
(docs/direwolf-cases.md, "Facts read from the oracle"):

* the text encoder sets the command/response bit of BOTH the destination and
  the source SSID byte to 1 (0xE0 | ssid<<1 style bytes, reserved bits 11);
  `encode_address(..., cr=True)` is that default;
* a `*` after a digipeater sets the has-been-repeated bit on that digipeater
  and on every digipeater before it;
* the address extension bit is set on the last address only.
"""
import re

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

# KISS command nibbles
CMD_DATA = 0x0
CMD_TXDELAY = 0x1
CMD_P = 0x2
CMD_SLOTTIME = 0x3
CMD_TXTAIL = 0x4
CMD_FULLDUPLEX = 0x5
CMD_SETHARDWARE = 0x6
CMD_RETURN = 0xF            # the whole byte 0xFF

MAX_DIGIS = 8
CONTROL_UI = 0x03
PID_NO_L3 = 0xF0

_CALL_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-([0-9]{1,2}))?$")
_ESC_RE = re.compile(r"<0x([0-9A-Fa-f]{2})>")


# ------------------------------------------------------------------ addresses

def encode_address(call, ssid=0, *, cr=True, last=False, reserved=0b11):
    """7 address bytes. `cr` is the command/response bit (destination, source)
    or the has-been-repeated bit (digipeater), `last` the address extension bit."""
    if not 1 <= len(call) <= 6 or not re.fullmatch(r"[A-Z0-9]+", call):
        raise ValueError("callsign must be 1 to 6 upper case letters or digits: %r" % (call,))
    if not 0 <= ssid <= 15:
        raise ValueError("ssid must be 0..15")
    body = bytes((ord(ch) << 1) for ch in call.ljust(6))
    return body + bytes([(0x80 if cr else 0) | ((reserved & 3) << 5) | (ssid << 1) | (1 if last else 0)])


def split_call(text):
    """'N0CALL-7' -> ('N0CALL', 7). Raises ValueError for anything else."""
    m = _CALL_RE.match(text)
    if not m:
        raise ValueError("not a callsign: %r" % (text,))
    ssid = int(m.group(2)) if m.group(2) is not None else 0
    if ssid > 15:
        raise ValueError("ssid over 15: %r" % (text,))
    return m.group(1), ssid


def decode_address(seven):
    """7 bytes -> dict(call, ssid, bit7, reserved, last, text). text is
    CALL or CALL-SSID. Raises ValueError for shifted bytes with the low bit set."""
    if len(seven) != 7:
        raise ValueError("an address is 7 bytes")
    chars = []
    for b in seven[:6]:
        if b & 1:
            raise ValueError("callsign byte has the extension bit set: %02x" % b)
        chars.append(chr(b >> 1))
    call = "".join(chars).rstrip(" ")
    last = seven[6]
    ssid = (last >> 1) & 0x0F
    return {"call": call, "ssid": ssid, "bit7": bool(last & 0x80), "reserved": (last >> 5) & 3,
            "last": bool(last & 1), "text": call if ssid == 0 else "%s-%d" % (call, ssid)}


# ------------------------------------------------------------------ TNC2 text

def info_from_text(text):
    """Information field bytes from text: `<0xHH>` is one byte, any other
    character must be Latin-1 and is one byte."""
    out = bytearray()
    pos = 0
    for m in _ESC_RE.finditer(text):
        out += text[pos:m.start()].encode("latin-1")
        out.append(int(m.group(1), 16))
        pos = m.end()
    out += text[pos:].encode("latin-1")
    return bytes(out)


def info_to_text(info):
    """Inverse of info_from_text for the bytes 0x20..0x7E; every other byte is
    written as <0xHH>. Dire Wolf prints high bytes as Latin-1 characters
    instead: compare frame bytes, not this text."""
    return "".join(chr(b) if 0x20 <= b <= 0x7E else "<0x%02x>" % b for b in info)


def from_tnc2(text, *, cr_dest=True, cr_src=True, pid=PID_NO_L3, control=CONTROL_UI):
    """AX.25 frame bytes (no FCS, no flags) from `SRC>DST[,DIGI[*]...]:info`.
    More than 8 digipeaters raise ValueError (Dire Wolf cuts them silently)."""
    head, sep, info_text = text.partition(":")
    if not sep:
        raise ValueError("no ':' before the information field")
    src_text, gt, rest = head.partition(">")
    if not gt:
        raise ValueError("no '>' after the source")
    parts = rest.split(",")
    dest_text, digi_texts = parts[0], parts[1:]
    if len(digi_texts) > MAX_DIGIS:
        raise ValueError("more than %d digipeaters" % MAX_DIGIS)
    src, src_ssid = split_call(src_text)
    dest, dest_ssid = split_call(dest_text)
    digis = []
    for d in digi_texts:
        used = d.endswith("*")
        call, ssid = split_call(d[:-1] if used else d)
        digis.append((call, ssid, used))
    last_used = max((i for i, d in enumerate(digis) if d[2]), default=-1)
    out = bytearray(encode_address(dest, dest_ssid, cr=cr_dest, last=False))
    out += encode_address(src, src_ssid, cr=cr_src, last=not digis)
    for i, (call, ssid, _used) in enumerate(digis):
        out += encode_address(call, ssid, cr=i <= last_used, last=i == len(digis) - 1)
    out += bytes([control, pid]) + info_from_text(info_text)
    return bytes(out)


def parse_frame(frame):
    """Dict with dest, src, digis (list of address dicts), control, pid (None
    when the control field is not UI-like), info (bytes). Raises ValueError."""
    if len(frame) < 16:
        raise ValueError("frame shorter than two addresses and control + pid")
    addrs = []
    pos = 0
    while True:
        if pos + 7 > len(frame):
            raise ValueError("address field runs past the end of the frame")
        a = decode_address(frame[pos:pos + 7])
        addrs.append(a)
        pos += 7
        if a["last"]:
            break
        if len(addrs) > 2 + MAX_DIGIS:
            raise ValueError("more than %d digipeaters" % MAX_DIGIS)
    if len(addrs) < 2:
        raise ValueError("fewer than two addresses")
    if pos + 2 > len(frame):
        raise ValueError("no control and pid")
    return {"dest": addrs[0], "src": addrs[1], "digis": addrs[2:], "control": frame[pos],
            "pid": frame[pos + 1], "info": bytes(frame[pos + 2:])}


def to_tnc2(frame):
    """TNC2 text of a parsed frame; a digipeater with the repeated bit set gets
    a '*' only when it is the last such digipeater (the TNC2 convention)."""
    f = parse_frame(frame)
    last_used = max((i for i, d in enumerate(f["digis"]) if d["bit7"]), default=-1)
    digis = ["%s%s" % (d["text"], "*" if i == last_used else "") for i, d in enumerate(f["digis"])]
    head = "%s>%s" % (f["src"]["text"], ",".join([f["dest"]["text"]] + digis))
    return head + ":" + info_to_text(f["info"])


# ------------------------------------------------------------------ FCS and bit stuffing

def fcs(data):
    """CRC-16/X-25 (init 0xFFFF, reflected polynomial 0x8408, final xor 0xFFFF)
    as an int; check value of b"123456789" is 0x906E."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def with_fcs(frame):
    """The frame followed by its FCS, low byte first (the order on the air)."""
    c = fcs(frame)
    return bytes(frame) + bytes([c & 0xFF, c >> 8])


def stuffed_zeros(data):
    """Number of zero bits inserted after five consecutive ones when `data` is
    sent least significant bit first (HDLC transparency)."""
    ones = 0
    inserted = 0
    for b in data:
        for i in range(8):
            if (b >> i) & 1:
                ones += 1
                if ones == 5:
                    inserted += 1
                    ones = 0
            else:
                ones = 0
    return inserted


def air_bits(frame, *, preamble_flags=0, postamble_flags=0):
    """Bits of one frame on the air at any baud rate: opening flag, stuffed
    frame + FCS, closing flag, plus extra flag octets before and after."""
    body = with_fcs(frame)
    return 8 * (preamble_flags + postamble_flags) + 8 + 8 * len(body) + stuffed_zeros(body) + 8


def air_ms(bits, baud=1200):
    """Milliseconds for `bits` at `baud` (exact Fraction)."""
    from fractions import Fraction
    return Fraction(bits * 1000, baud)


# ------------------------------------------------------------------ KISS

def kiss_escape(data):
    out = bytearray()
    for b in data:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    return bytes(out)


def kiss_encode(port, cmd, data=b""):
    """FEND, (port << 4 | cmd), escaped data, FEND."""
    if not 0 <= port <= 15 or not 0 <= cmd <= 15:
        raise ValueError("port and command are nibbles")
    return bytes([FEND, (port << 4) | cmd]) + kiss_escape(data) + bytes([FEND])


class KissDecoder:
    """Streaming decoder of the original KISS definition: FEND ends a frame
    (an empty frame is skipped), FESC TFEND is FEND, FESC TFESC is FESC. Any
    other byte after FESC is kept as it is and counted in `bad_escapes`.
    feed() returns the completed frames as (port, cmd_nibble, data, raw_first_byte);
    `cmd_nibble` is the low nibble of the type byte (the Return command 0xFF
    is port 15 command 15)."""

    def __init__(self):
        self._buf = bytearray()
        self._esc = False
        self.bad_escapes = 0

    def feed(self, data):
        frames = []
        for b in data:
            if self._esc:
                self._esc = False
                if b == TFEND:
                    self._buf.append(FEND)
                elif b == TFESC:
                    self._buf.append(FESC)
                else:
                    self.bad_escapes += 1
                    self._buf.append(b)
                continue
            if b == FEND:
                if self._buf:
                    t = self._buf[0]
                    frames.append((t >> 4, t & 0x0F, bytes(self._buf[1:]), t))
                self._buf = bytearray()
            elif b == FESC:
                self._esc = True
            else:
                self._buf.append(b)
        return frames

    def pending(self):
        """Bytes of an unfinished frame (without escapes resolved)."""
        return bytes(self._buf)


def kiss_decode(stream):
    """All complete frames of a byte string (see KissDecoder)."""
    return KissDecoder().feed(stream)


# ------------------------------------------------------------------ DV-Router model (statements of its lead)
#
# Everything below models what the DV-Router lead STATED about its KISS / AX.25 adapter
# (docs/direwolf-cases.md, "Documented differences"). Nothing here was run against DV-Router:
# it is a checkable restatement, so a test can show where Dire Wolf's vectors agree or differ.

DVR_MIN_FRAME = 16            # two addresses + control + PID, UI frames only
DVR_MAX_FRAME = 1100          # also the KISS data limit
DW_MIN_FRAME = 15             # Dire Wolf: 14 refused, 15 accepted (vectors/direwolf/kiss_tx.json)
DW_MAX_FRAME = 2123           # Dire Wolf, ax25_pad.h
DVR_PARAMS = ((CMD_TXDELAY, 0x1E), (CMD_P, 0x3F), (CMD_SLOTTIME, 0x0A), (CMD_TXTAIL, 0x03), (CMD_FULLDUPLEX, 0x00))


def mask_cr_bits(frame):
    """The frame with bit 7 (command/response) of the destination and the source SSID byte
    cleared: the form in which a Dire Wolf frame (e0 / ee) and a DV-Router frame (e0 / 72)
    compare equal. A digipeater byte's bit 7 is the has-been-repeated flag and is NOT masked."""
    if len(frame) < 14:
        raise ValueError("frame shorter than two addresses")
    out = bytearray(frame)
    out[6] &= 0x7F
    out[13] &= 0x7F
    return bytes(out)


def dvrouter_startup_params(port=0):
    """The KISS frames DV-Router sends per channel on every (re)connect, in this order:
    TXDELAY 0x1E, P 0x3F, SlotTime 0x0A, TXtail 0x03, FullDuplex 0x00; never SetHardware or
    Return. For port 0 this is C0 01 1E C0 C0 02 3F C0 C0 03 0A C0 C0 04 03 C0 C0 05 00 C0."""
    return b"".join(kiss_encode(port, cmd, bytes([val])) for cmd, val in DVR_PARAMS)


def dvrouter_accepts_frame(frame):
    """True when the lead's rule accepts an AX.25 frame: 16..1100 bytes and a UI frame
    (control 0x03, PID 0xF0). The control / PID position is after the address field
    (extension bit), so digipeaters are allowed."""
    if not DVR_MIN_FRAME <= len(frame) <= DVR_MAX_FRAME:
        return False
    pos = 0
    while True:
        if pos + 7 > len(frame):
            return False
        last = frame[pos + 6] & 1
        pos += 7
        if last:
            break
    return pos + 2 <= len(frame) and frame[pos] == CONTROL_UI and frame[pos + 1] == PID_NO_L3


def dw_accepts_len(length):
    """Dire Wolf's frame length window (ax25_pad.h): 15..2123 bytes."""
    return DW_MIN_FRAME <= length <= DW_MAX_FRAME


class DvrouterKiss:
    """The lead's description of DV-Router's KISS receive decoder, as a streaming model:

    * bytes before the first FEND are dropped (`dropped_noise`);
    * FESC TFEND is FEND and FESC TFESC is FESC; FESC followed by any other byte drops BOTH
      bytes, keeps the frame and counts it (`bad_escapes`);
    * an unescaped FEND inside a frame closes it; FEND FEND emits nothing;
    * the command nibble of the type byte: 0 is data; 1 to 6 and the byte 0xFF are ignored
      (`ignored_commands`); 7 to 14 are NOT SPECIFIED by the lead (treated as ignored and
      counted in `unspecified_commands`);
    * a data frame over `max_data` bytes is discarded up to the next FEND (`overlong`). Whether
      the limit counts the type byte or the unescaped length is not stated: this model counts
      the unescaped bytes after the type byte (UNVERIFIED).

    feed() returns the data frames as (port, bytes); the AX.25 acceptance rule
    (dvrouter_accepts_frame) is a second step, applied by the caller."""

    def __init__(self, max_data=DVR_MAX_FRAME):
        self.max_data = max_data
        self._buf = None                  # None: before the first FEND
        self._esc = False
        self._discard = False
        self.dropped_noise = 0
        self.bad_escapes = 0
        self.ignored_commands = 0
        self.unspecified_commands = 0
        self.overlong = 0

    def feed(self, data):
        frames = []
        for b in data:
            if self._buf is None:
                if b == FEND:
                    self._buf = bytearray()
                else:
                    self.dropped_noise += 1
                continue
            if self._discard:
                if b == FEND:
                    self._discard = False
                    self._esc = False
                    self._buf = bytearray()
                continue
            if self._esc:
                self._esc = False
                if b == TFEND:
                    self._append(FEND)
                elif b == TFESC:
                    self._append(FESC)
                elif b == FEND:
                    self.bad_escapes += 1
                    self._close(frames)
                else:
                    self.bad_escapes += 1          # both bytes dropped, the frame goes on
                continue
            if b == FEND:
                self._close(frames)
            elif b == FESC:
                self._esc = True
            else:
                self._append(b)
        return frames

    def _append(self, b):
        self._buf.append(b)
        if len(self._buf) - 1 > self.max_data:
            self.overlong += 1
            self._buf = bytearray()
            self._discard = True

    def _close(self, frames):
        buf, self._buf = self._buf, bytearray()
        if not buf:
            return
        t = buf[0]
        cmd = t & 0x0F
        if t == 0xFF or 1 <= cmd <= 6:
            self.ignored_commands += 1
        elif cmd == 0:
            frames.append((t >> 4, bytes(buf[1:])))
        else:
            self.unspecified_commands += 1
            self.ignored_commands += 1
