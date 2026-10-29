# SPDX-License-Identifier: MIT
"""AMBEd transcoder protocol (xlxd <-> ambed, UDP 10100) and a fake server.

Control packets, all on the transcoder control port:
  AMBEDPING + callsign(8)                      17 B  xlxd -> ambed keepalive
  AMBEDPONG                                     9 B  reply
  AMBEDCAPS                                     9 B  capability request
  AMBEDCAPS + ver(1) + max_out(1) + BE32 mask  15 B  reply, mask bit n = codec n
  AMBEDOS + callsign(8) + in + out1            17 B  legacy open
  AMBEDOS + callsign(8) + in + out1 + out2     18 B  legacy open
  AMBEDOS + callsign(8) + in + out1..out3      19 B  legacy open
  AMBEDOS + callsign(8) + in + out1..out4 + BE32 transaction
                                               24 B  extended open
  AMBEDSTD + id(2) + port(2) + in + out0       14 B  stream descriptor
  ... + BE32 transaction                       18 B  reply to an extended open
  AMBEDBUSY                                     9 B  no stream
  AMBEDBUSY + BE32 transaction                 13 B  reply to an extended open
  AMBEDCS + id(2)                               9 B  close stream

id and port in STD/CS are host order on the ambed side (memcpy of a
uint16); every known deployment is little endian, so they are encoded
little endian here.

Data, on the stream port: request = codec_in + pid + payload; reply =
ordered (codec, payload) entries in open order with the pid after the
first codec byte: c0 pid p0 c1 p1 ...
"""
import socket
import struct
import threading
import time
from collections import namedtuple

PORT = 10100

NONE = 0
AMBEPLUS = 1
AMBE2PLUS = 2
CODEC2_3200 = 3
IMBE = 4
PCM = 5
CODECS = (AMBEPLUS, AMBE2PLUS, CODEC2_3200, IMBE, PCM)
# payload bytes per 20 ms frame; PCM is S16LE 8 kHz, 160 samples
WIDTH = {AMBEPLUS: 9, AMBE2PLUS: 9, CODEC2_3200: 8, IMBE: 11, PCM: 320}
MASK_ALL = 0x3E
MAX_OUTPUTS = 4

PING_TAG = b"AMBEDPING"
PONG = b"AMBEDPONG"
CAPS_REQUEST = b"AMBEDCAPS"
OPEN_TAG = b"AMBEDOS"
STD_TAG = b"AMBEDSTD"
BUSY_TAG = b"AMBEDBUSY"
CLOSE_TAG = b"AMBEDCS"

Caps = namedtuple("Caps", "version max_outputs mask")
Open = namedtuple("Open", "callsign codec_in outs transaction size")
Std = namedtuple("Std", "stream_id port codec_in codec_out transaction")


def mask_of(codecs):
    mask = 0
    for codec in codecs:
        mask |= 1 << codec
    return mask


def codecs_in(mask):
    return [c for c in CODECS if mask & (1 << c)]


def callsign8(text):
    if isinstance(text, str):
        text = text.encode()
    return (text + b" " * 8)[:8]


def ping(callsign):
    return PING_TAG + callsign8(callsign)


def parse_ping(data):
    """Callsign bytes of a 17 B ping, or None."""
    if len(data) == 17 and data.startswith(PING_TAG):
        return data[9:17]
    return None


def caps_reply(max_outputs=MAX_OUTPUTS, mask=MASK_ALL, version=1):
    return CAPS_REQUEST + bytes([version, max_outputs]) + struct.pack(">I", mask)


def parse_caps(data):
    if len(data) == 15 and data.startswith(CAPS_REQUEST):
        return Caps(data[9], data[10], struct.unpack(">I", data[11:15])[0])
    return None


def open_legacy(callsign, codec_in, *outs):
    """17, 18 or 19 B legacy open for one, two or three outputs."""
    if not 1 <= len(outs) <= 3:
        raise ValueError("legacy open takes 1..3 outputs")
    return OPEN_TAG + callsign8(callsign) + bytes([codec_in, *outs])


def open_ext(callsign, codec_in, outs, transaction):
    """24 B extended open; outs zero padded to 4."""
    outs = list(outs)
    if len(outs) > MAX_OUTPUTS:
        raise ValueError("extended open takes up to 4 outputs")
    outs += [NONE] * (MAX_OUTPUTS - len(outs))
    return (OPEN_TAG + callsign8(callsign) + bytes([codec_in, *outs])
            + struct.pack(">I", transaction & 0xFFFFFFFF))


def parse_open(data):
    """Open(...) for any 17/18/19/24 B open, else None. outs keeps the
    wire order with trailing NONE entries dropped; transaction is None
    for legacy forms."""
    if len(data) not in (17, 18, 19, 24) or not data.startswith(OPEN_TAG):
        return None
    if len(data) == 24:
        outs = list(data[16:20])
        transaction = struct.unpack(">I", data[20:24])[0]
    else:
        outs = list(data[16:])
        transaction = None
    while outs and outs[-1] == NONE:
        outs.pop()
    return Open(data[7:15], data[15], tuple(outs), transaction, len(data))


def std(stream_id, port, codec_in, codec_out, transaction=None):
    out = STD_TAG + struct.pack("<HH", stream_id, port) + bytes([codec_in, codec_out])
    if transaction is not None:
        out += struct.pack(">I", transaction)
    return out


def parse_std(data):
    if len(data) in (14, 18) and data.startswith(STD_TAG):
        stream_id, port = struct.unpack("<HH", data[8:12])
        transaction = struct.unpack(">I", data[14:18])[0] if len(data) == 18 else None
        return Std(stream_id, port, data[12], data[13], transaction)
    return None


def busy(transaction=None):
    return BUSY_TAG + (b"" if transaction is None else struct.pack(">I", transaction))


def is_busy(data):
    return len(data) in (9, 13) and data.startswith(BUSY_TAG)


def busy_transaction(data):
    """Transaction of a 13 B BUSY, None for the 9 B legacy form."""
    return struct.unpack(">I", data[9:13])[0] if len(data) == 13 else None


def close(stream_id):
    return CLOSE_TAG + struct.pack("<H", stream_id)


def parse_close(data):
    if len(data) >= 9 and data.startswith(CLOSE_TAG):
        return struct.unpack("<H", data[7:9])[0]
    return None


def data_request(codec, pid, payload):
    if len(payload) != WIDTH[codec]:
        raise ValueError("codec %d payload must be %d bytes" % (codec, WIDTH[codec]))
    return bytes([codec, pid & 0xFF]) + payload


def parse_data_request(data):
    """(codec, pid, payload) or None when the size does not match."""
    if len(data) < 2 or data[0] not in WIDTH or len(data) != 2 + WIDTH[data[0]]:
        return None
    return data[0], data[1], data[2:]


def data_reply(pid, entries):
    """entries: [(codec, payload), ...] in open order."""
    out = bytearray()
    for index, (codec, payload) in enumerate(entries):
        if len(payload) != WIDTH[codec]:
            raise ValueError("codec %d payload must be %d bytes" % (codec, WIDTH[codec]))
        out.append(codec)
        if index == 0:
            out.append(pid & 0xFF)
        out += payload
    return bytes(out)


def reply_size(outs):
    return 1 + sum(1 + WIDTH[c] for c in outs)


def parse_data_reply(data, outs):
    """(pid, [(codec, payload), ...]) for a reply to a stream opened with
    outs; raises ValueError on size or codec order mismatch."""
    if len(data) != reply_size(outs):
        raise ValueError("reply is %d bytes, expected %d" % (len(data), reply_size(outs)))
    entries = []
    offset = 0
    pid = None
    for index, codec in enumerate(outs):
        if data[offset] != codec:
            raise ValueError("entry %d is codec %d, expected %d" % (index, data[offset], codec))
        offset += 1
        if index == 0:
            pid = data[offset]
            offset += 1
        entries.append((codec, data[offset:offset + WIDTH[codec]]))
        offset += WIDTH[codec]
    return pid, entries


def fake_payload(codec):
    """Fixed payload the FakeAmbed returns for a codec (0x10 + codec)."""
    return bytes([0x10 + codec]) * WIDTH[codec]


class AmbedClient:
    """Control-port client: one socket, request/reply helpers."""

    def __init__(self, addr, port=PORT, bind_ip="0.0.0.0", timeout=2.0):
        self.dest = (addr, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, 0))
        self.sock.settimeout(timeout)

    def send(self, data):
        self.sock.sendto(data, self.dest)

    def rpc(self, data):
        """Send and return the next reply, or None on timeout."""
        self.send(data)
        try:
            return self.sock.recvfrom(2048)[0]
        except socket.timeout:
            return None

    def close_stream(self, stream_id, settle=0.1):
        self.send(close(stream_id))
        time.sleep(settle)

    def close(self):
        self.sock.close()


def run_stream(addr, port, codec_in, outs, frames, gap=0.02, wait=3.0, bind_ip="0.0.0.0"):
    """Send frames on an open stream and collect the replies. ambed sends
    replies to the client address at the stream port number, so the data
    socket binds that same port locally. Replies are drained after every
    send so long streams do not overflow the socket buffer. Returns the
    raw replies of this stream's shape, at most one per frame."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    sock.bind((bind_ip, port))
    sock.setblocking(False)
    replies = []
    size = reply_size(outs)

    def drain():
        # a late reply of an earlier stream on a reused port has another
        # shape (or arrives once all replies are in) and is dropped
        while True:
            try:
                data = sock.recvfrom(4096)[0]
            except BlockingIOError:
                return
            if len(data) == size and data[0] == outs[0] and len(replies) < len(frames):
                replies.append(data)

    try:
        for pid, frame in enumerate(frames):
            sock.sendto(data_request(codec_in, pid, frame), (addr, port))
            drain()
            time.sleep(gap)
        deadline = time.monotonic() + wait
        while len(replies) < len(frames) and time.monotonic() < deadline:
            drain()
            time.sleep(0.01)
    finally:
        sock.close()
    return replies

class FakeAmbed:
    """Minimal AMBEd server for reflector tests.

    mode "extended" answers CAPS (max_outputs, mask) and both open forms;
    mode "legacy" never answers CAPS and ignores 24 B opens, like a
    transcoder that predates the extension. Opens get a data port from
    data_port_base upward; each data request is answered with fake_payload()
    per output codec. open_delay delays the STD reply (open still pending
    while the reflector keeps sending). Everything received is logged.
    """

    def __init__(self, bind_ip, port=PORT, mode="extended", max_outputs=MAX_OUTPUTS,
                 mask=MASK_ALL, open_delay=0.0, data_port_base=41000):
        if mode not in ("extended", "legacy"):
            raise ValueError("mode is extended or legacy")
        self.bind_ip = bind_ip
        self.mode = mode
        self.max_outputs = max_outputs
        self.mask = mask
        self.open_delay = open_delay
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.ctrl = []          # [(t, bytes)] every control packet received
        self.events = []        # [(t, text)]
        self.opens = []         # [(t, Open, data_port)]
        self.closes = []        # [(t, stream_id)]
        self.data_in = {}       # port -> [(t, bytes)]
        self.connected = threading.Event()
        self.stopping = threading.Event()
        self.next_port = data_port_base
        self.next_id = 1
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(0.2)
        self.threads = [threading.Thread(target=self._control, daemon=True)]
        self.threads[0].start()

    def now(self):
        return time.monotonic() - self.t0

    def event(self, text):
        with self.lock:
            self.events.append((self.now(), text))

    def caps_requests(self):
        with self.lock:
            return sum(1 for _t, b in self.ctrl if b == CAPS_REQUEST)

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.sock.close()

    def _serve_data(self, sock, port, outs):
        with self.lock:
            self.data_in[port] = []
        while not self.stopping.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self.data_in[port].append((self.now(), data))
            if len(data) < 2:
                continue
            reply = data_reply(data[1], [(c, fake_payload(c)) for c in outs])
            sock.sendto(reply, addr)
        sock.close()

    def _open(self, request, addr):
        if request.size == 24 and self.mode == "legacy":
            self.event("ignored extended open")
            return
        port, stream_id = self.next_port, self.next_id
        self.next_port += 1
        self.next_id += 1
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.bind_ip, port))
        sock.settimeout(0.2)
        thread = threading.Thread(target=self._serve_data, args=(sock, port, request.outs), daemon=True)
        self.threads.append(thread)
        thread.start()
        with self.lock:
            self.opens.append((self.now(), request, port))
        if self.open_delay:
            time.sleep(self.open_delay)
        out0 = request.outs[0] if request.outs else NONE
        self.event("OPEN len=%d in=%d outs=%s tr=%s -> port %d" % (
            request.size, request.codec_in, list(request.outs), request.transaction, port))
        self.sock.sendto(std(stream_id, port, request.codec_in, out0, request.transaction), addr)

    def _control(self):
        while not self.stopping.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self.ctrl.append((self.now(), data))
            if parse_ping(data) is not None:
                self.connected.set()
                self.sock.sendto(PONG, addr)
            elif data == CAPS_REQUEST:
                self.event("CAPS request")
                if self.mode == "extended":
                    self.sock.sendto(caps_reply(self.max_outputs, self.mask), addr)
            elif parse_open(data) is not None:
                self._open(parse_open(data), addr)
            elif parse_close(data) is not None:
                stream_id = parse_close(data)
                with self.lock:
                    self.closes.append((self.now(), stream_id))
                self.event("CLOSE id=%d" % stream_id)


def transcode(addr, codec_in, outs, frames, port=PORT, transaction=0x44565400, callsign="N0TEST",
              bind_ip="0.0.0.0"):
    """Run frames through one extended stream of a transcoder; returns
    {codec: [payload per frame]}. Raises RuntimeError when the open is
    refused or replies are missing."""
    client = AmbedClient(addr, port, bind_ip=bind_ip)
    try:
        std = parse_std(client.rpc(open_ext(callsign, codec_in, outs, transaction)) or b"")
        if std is None:
            raise RuntimeError("transcoder %s refused %d -> %s" % (addr, codec_in, list(outs)))
        try:
            replies = run_stream(addr, std.port, codec_in, outs, frames, bind_ip=bind_ip)
        finally:
            client.close_stream(std.stream_id)
    finally:
        client.close()
    if len(replies) != len(frames):
        raise RuntimeError("transcoder %s returned %d of %d frames" % (addr, len(replies), len(frames)))
    out = {c: [] for c in outs}
    for reply in replies:
        for codec, payload in parse_data_reply(reply, outs)[1]:
            out[codec].append(payload)
    return out
