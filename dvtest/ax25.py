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


# ------------------------------------------------------------------ H bit and path handling (DIGI1, docs/digi1-cases.md)
#
# TNC2 text cannot say "hop 2 is used but hop 1 is not" (a `*` means "this hop and every hop before it"), and the DIGI1 path vectors
# need exactly that (G10: non-contiguous H bits) and need the two reserved bits of a hop (G12: clear). So the cases build frames hop by
# hop: a hop is a call text, or (text, h) or (text, h, reserved). The functions below only touch the 7 bytes of one hop; they are the
# frame form of what a fill-in digipeater does (AX.25 v2.2 section 3.12) and are checked against hand-built bytes in
# cases/unit/test_ax25_digi.py.

def _hop_spec(item):
    """(call, ssid, h, reserved) of one hop spec: 'CALL-S', ('CALL-S', h) or ('CALL-S', h, reserved)."""
    if isinstance(item, str):
        text, h, reserved = item, False, 0b11
    elif len(item) == 2:
        text, h, reserved = item[0], bool(item[1]), 0b11
    else:
        text, h, reserved = item[0], bool(item[1]), item[2]
    call, ssid = split_call(text)
    return call, ssid, h, reserved


def build_ui(dest, src, digis=(), info=b"", *, cr_dest=True, cr_src=True, pid=PID_NO_L3, control=CONTROL_UI, src_reserved=0b11,
             dest_reserved=0b11):
    """AX.25 UI frame bytes (no FCS) with an explicit H bit and reserved bits per digipeater (see the section note). `dest` and `src` are
    'CALL-S' texts, `digis` a sequence of hop specs, `info` bytes or str (Latin-1). More than 8 hops raise ValueError. The address
    extension bit is set on the last address only. `cr_dest` / `cr_src` are the command/response bits (Dire Wolf sets both; a
    hand-built v2 command frame has the destination set and the source clear)."""
    hops = [_hop_spec(d) for d in digis]
    if len(hops) > MAX_DIGIS:
        raise ValueError("more than %d digipeaters" % MAX_DIGIS)
    dcall, dssid = split_call(dest)
    scall, sssid = split_call(src)
    out = bytearray(encode_address(dcall, dssid, cr=cr_dest, last=False, reserved=dest_reserved))
    out += encode_address(scall, sssid, cr=cr_src, last=not hops, reserved=src_reserved)
    for i, (call, ssid, h, reserved) in enumerate(hops):
        out += encode_address(call, ssid, cr=h, last=i == len(hops) - 1, reserved=reserved)
    if isinstance(info, str):
        info = info.encode("latin-1")
    out += bytes([control, pid]) + bytes(info)
    return bytes(out)


def hops(frame):
    """[(text, h, reserved)] of the digipeaters of a frame, in path order (h = has-been-repeated bit)."""
    return [(d["text"], d["bit7"], d["reserved"]) for d in parse_frame(frame)["digis"]]


def path_text(frame):
    """The path with a `*` after EVERY hop whose H bit is set (not the TNC2 'last used only' convention), e.g. `SRC>DST,A*,B`; the
    information field is not part of it. Use it in assertion messages: it shows non-contiguous H bits."""
    f = parse_frame(frame)
    return "%s>%s" % (f["src"]["text"], ",".join([f["dest"]["text"]] + ["%s%s" % (d["text"], "*" if d["bit7"] else "") for d in f["digis"]]))


def first_unused_hop(frame):
    """Index of the first digipeater whose H bit is clear, or None (no hops, or all used)."""
    for i, d in enumerate(parse_frame(frame)["digis"]):
        if not d["bit7"]:
            return i
    return None


def replace_hop(frame, index, text):
    """The frame with digipeater `index` replaced by `text`, H bit set and both reserved bits set, the address extension bit of that hop
    kept: the one rewrite a fill-in digipeater makes (DIGI1, data.md 8.3). Every other byte is unchanged. IndexError when the frame
    has no such hop."""
    f = parse_frame(frame)
    if not 0 <= index < len(f["digis"]):
        raise IndexError("the frame has %d digipeaters, no hop %d" % (len(f["digis"]), index))
    call, ssid = split_call(text)
    offset = 14 + 7 * index
    seven = encode_address(call, ssid, cr=True, last=bool(frame[offset + 6] & 1))
    return bytes(frame[:offset]) + seven + bytes(frame[offset + 7:])


def diff_offsets(a, b):
    """Byte offsets at which two frames of equal length differ; ValueError for different lengths."""
    if len(a) != len(b):
        raise ValueError("frames of %d and %d bytes" % (len(a), len(b)))
    return [i for i in range(len(a)) if a[i] != b[i]]


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
    """The DV-Router lead's description of its KISS receive decoder (crates/edge/ax25/src/kiss.rs,
    read by the lead; lines 36-80 are the push() routine), as a streaming model:

    * bytes before the first FEND are dropped (`dropped_noise`);
    * EVERY FEND closes the current frame and opens the next one (shared-FEND convention): the bytes
      after an unescaped FEND are the type byte of a new frame. Dire Wolf instead calls them noise
      until another FEND (documented difference, case `truncated-then-frame`). A FEND also wins over
      a pending FESC (no bad escape is counted, the frame is closed);
    * FESC TFEND is FEND and FESC TFESC is FESC; FESC followed by any other byte drops BOTH
      bytes, keeps the frame and counts it (`bad_escapes`);
    * FEND FEND emits nothing (an empty buffer);
    * the command nibble of the type byte: 0 is data; EVERY other value 1..15 (1..6 parameters, 7..14
      undefined, 15 = the byte 0xFF Return) is ignored silently (`ignored_commands`, of which
      `commands_7_to_14`; `unspecified_commands` is the same counter under its earlier name);
    * a data frame whose port nibble is not a configured channel is dropped silently
      (`dropped_channel`; crates/edge/ax25/src/adapter/tcp.rs about lines 373-379, 417-419);
    * the length limit counts the UNESCAPED bytes after the type byte: the buffer holds the type byte
      plus at most `max_data` (1100) data bytes, checked before each push as `len(buffer) > max_data`;
      escape pairs are resolved first and are not counted. The 1101st data byte starts a discard up
      to the next FEND, and that FEND reports the frame as too long (`overlong`, counted at the FEND
      like the edge's frames_bad).

    feed() returns the data frames of configured channels as (port, bytes); the AX.25 acceptance rule
    (dvrouter_accepts_frame) is a second step, applied by the caller. `channels=None` keeps every
    port (no channel filter)."""

    def __init__(self, max_data=DVR_MAX_FRAME, channels=(0,)):
        self.max_data = max_data
        self.channels = None if channels is None else frozenset(channels)
        self._buf = None                  # None: before the first FEND
        self._esc = False
        self._discard = False
        self.dropped_noise = 0
        self.bad_escapes = 0
        self.ignored_commands = 0
        self.commands_7_to_14 = 0
        self.dropped_channel = 0
        self.overlong = 0

    @property
    def unspecified_commands(self):
        return self.commands_7_to_14

    def feed(self, data):
        frames = []
        for b in data:
            if b == FEND:
                if self._buf is not None:
                    self._close(frames)
                self._buf = bytearray()
                self._esc = False
                self._discard = False
                continue
            if self._buf is None:
                self.dropped_noise += 1
                continue
            if self._discard:
                continue
            if self._esc:
                self._esc = False
                if b == TFEND:
                    self._append(FEND)
                elif b == TFESC:
                    self._append(FESC)
                else:
                    self.bad_escapes += 1          # both bytes dropped, the frame goes on
                continue
            if b == FESC:
                self._esc = True
            else:
                self._append(b)
        return frames

    def _append(self, b):
        if len(self._buf) > self.max_data:        # type byte + max_data bytes are already held
            self._discard = True
            self._buf = bytearray()
            return
        self._buf.append(b)

    def _close(self, frames):
        if self._discard:
            self.overlong += 1
            return
        buf = self._buf
        if not buf:
            return
        t = buf[0]
        cmd = t & 0x0F
        if cmd != 0:
            self.ignored_commands += 1
            if 7 <= cmd <= 14:
                self.commands_7_to_14 += 1
        elif self.channels is not None and (t >> 4) not in self.channels:
            self.dropped_channel += 1
        else:
            frames.append((t >> 4, bytes(buf[1:])))
