# SPDX-License-Identifier: MIT
"""SvxReflector protocol (SvxLink, protocol 3.0 with the legacy 2.x UDP form):
framing, message codec and layout helpers. DRAFT, stdlib only, not used by
any case yet (docs/svx-cases.md).

Every layout below was read from the pinned SvxLink source (commit in
docs/svx-cases.md), not from a capture:

* TCP frame  = frame_len:u32 | msg_type:u16 | body. frame_len counts the
  type and the body, not its own four bytes (src/async/core/
  AsyncFramedTcpConnection.h, cpp onDataReceived).
* Integers are big endian. String = u16 length + bytes. Vector = u16 element
  count + elements (a byte vector is therefore u16 length + bytes). Set of
  u32 = u16 count + u32 values, ascending (src/async/core/AsyncMsg.h).
* Legacy UDP (protocol < 3, plaintext) = type:u16 | client_id:u16 | seq:u16 |
  body (ReflectorUdpMsgV2, ReflectorMsg.h).
* Protocol 3 UDP = aad | tag[8] | ciphertext, AES-128-GCM; the plaintext is
  type:u16 | body. This module only splits the datagram and builds the IV;
  it does not encrypt or decrypt (no GCM in the standard library).

Nothing here talks to a socket.
"""
import hashlib
import hmac
import struct
from collections import namedtuple

# --- TCP message types (ReflectorMsg.h) ---
HEARTBEAT = 1
PROTO_VER = 5
PROTO_VER_DOWNGRADE = 6
AUTH_CHALLENGE = 10
AUTH_RESPONSE = 11
AUTH_OK = 12
ERROR = 13
START_ENCRYPTION_REQUEST = 14
START_ENCRYPTION = 15
CLIENT_CSR_REQUEST = 16
CLIENT_CSR = 17
CLIENT_CERT = 18
CA_INFO = 19
CA_BUNDLE_REQUEST = 20
CA_BUNDLE = 21
SERVER_INFO = 100
NODE_LIST = 101
NODE_JOINED = 102
NODE_LEFT = 103
TALKER_START = 104
TALKER_STOP = 105
SELECT_TG = 106
TG_MONITOR = 107
REQUEST_QSY = 109
STATE_EVENT = 110
NODE_INFO = 111
SIGNAL_STRENGTH = 112
TX_STATUS = 113
START_UDP_ENCRYPTION = 114

# --- UDP message types ---
UDP_HEARTBEAT = 1
UDP_AUDIO = 101
UDP_FLUSH_SAMPLES = 102
UDP_ALL_SAMPLES_FLUSHED = 103
UDP_SIGNAL_STRENGTH = 104

NAMES = {
    HEARTBEAT: "Heartbeat", PROTO_VER: "ProtoVer",
    PROTO_VER_DOWNGRADE: "ProtoVerDowngrade", AUTH_CHALLENGE: "AuthChallenge",
    AUTH_RESPONSE: "AuthResponse", AUTH_OK: "AuthOk", ERROR: "Error",
    START_ENCRYPTION_REQUEST: "StartEncryptionRequest",
    START_ENCRYPTION: "StartEncryption", CLIENT_CSR_REQUEST: "ClientCsrRequest",
    CLIENT_CSR: "ClientCsr", CLIENT_CERT: "ClientCert", CA_INFO: "CAInfo",
    CA_BUNDLE_REQUEST: "CABundleRequest", CA_BUNDLE: "CABundle",
    SERVER_INFO: "ServerInfo", NODE_LIST: "NodeList", NODE_JOINED: "NodeJoined",
    NODE_LEFT: "NodeLeft", TALKER_START: "TalkerStart", TALKER_STOP: "TalkerStop",
    SELECT_TG: "SelectTG", TG_MONITOR: "TgMonitor", REQUEST_QSY: "RequestQsy",
    STATE_EVENT: "StateEvent", NODE_INFO: "NodeInfo",
    SIGNAL_STRENGTH: "SignalStrengthValues", TX_STATUS: "TxStatus",
    START_UDP_ENCRYPTION: "StartUdpEncryption",
}
UDP_NAMES = {
    UDP_HEARTBEAT: "Heartbeat", UDP_AUDIO: "Audio",
    UDP_FLUSH_SAMPLES: "FlushSamples",
    UDP_ALL_SAMPLES_FLUSHED: "AllSamplesFlushed",
    UDP_SIGNAL_STRENGTH: "SignalStrengthValues",
}

# Server receive limits per connection phase (ReflectorMsg.h:110-112); the
# limit applies to frame_len, i.e. type + body.
MAX_PREAUTH_FRAME = 64
MAX_SSL_SETUP_FRAME = 4096
MAX_POSTAUTH_FRAME = 32768

CHALLENGE_LEN = 20      # MsgAuthChallenge::LENGTH
DIGEST_LEN = 20         # MsgAuthResponse::DIGEST_LEN, HMAC-SHA1
AES_KEY_LEN = 16        # UdpCipher: AES-128-GCM
TAG_LEN = 8             # UdpCipher::TAGLEN
IV_LEN = 12             # UdpCipher::IVLEN
IV_RAND_LEN = 6         # IVLEN - sizeof(counter) - sizeof(client id)
AAD_LEN = 4             # UdpCipher::AADLEN
INITIAL_AAD_LEN = 6     # counter 0 + client id


class SvxError(ValueError):
    """Malformed input (short body, trailing bytes, oversize frame)."""


# --- primitive packers ---

def p_u16(v):
    return struct.pack(">H", v)


def p_u32(v):
    return struct.pack(">I", v)


def p_str(s):
    b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
    if len(b) > 0xFFFF:
        raise SvxError("string longer than 65535 bytes")
    return p_u16(len(b)) + b


def p_bytes(b):
    b = bytes(b)
    if len(b) > 0xFFFF:
        raise SvxError("byte vector longer than 65535 bytes")
    return p_u16(len(b)) + b


def p_vec_str(items):
    items = list(items)
    if len(items) > 0xFFFF:
        raise SvxError("vector longer than 65535 elements")
    return p_u16(len(items)) + b"".join(p_str(s) for s in items)


def p_set32(values):
    vals = sorted(set(values))
    return p_u16(len(vals)) + b"".join(p_u32(v) for v in vals)


class Reader:
    """Cursor over a body; every read is bounds checked."""

    def __init__(self, data):
        self.data = bytes(data)
        self.pos = 0

    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise SvxError("body too short")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack(">H", self.take(2))[0]

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def string(self):
        return self.take(self.u16()).decode("utf-8", "replace")

    def blob(self):
        return self.take(self.u16())

    def vec_str(self):
        return [self.string() for _ in range(self.u16())]

    def set32(self):
        return [self.u32() for _ in range(self.u16())]

    def rest(self):
        out = self.data[self.pos:]
        self.pos = len(self.data)
        return out

    def done(self):
        return self.pos == len(self.data)


# --- TCP framing ---

def tcp_frame(msg_type, body=b""):
    """frame_len | msg_type | body."""
    return p_u32(2 + len(body)) + p_u16(msg_type) + bytes(body)


class FrameSplitter:
    """Reassembles the TCP byte stream into (msg_type, body) pairs. Handles
    fragmented and coalesced input. A frame_len above max_frame raises
    SvxError at the length word, before any body byte is buffered (the real
    server closes the connection without an Error message,
    AsyncFramedTcpConnection.cpp onDataReceived). A frame_len below 2 cannot
    hold a type and raises as well (the real server would pass the short
    frame on and answer Error "Protocol error"; for a decoder it is
    malformed input)."""

    def __init__(self, max_frame=MAX_POSTAUTH_FRAME):
        self.max_frame = max_frame
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data
        out = []
        while len(self._buf) >= 4:
            n = struct.unpack(">I", self._buf[:4])[0]
            if n > self.max_frame:
                raise SvxError("frame_len %d above limit %d" % (n, self.max_frame))
            if n < 2:
                raise SvxError("frame_len %d cannot hold a message type" % n)
            if len(self._buf) < 4 + n:
                break
            payload = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            out.append((struct.unpack(">H", payload[:2])[0], payload[2:]))
        return out

    @property
    def pending(self):
        return len(self._buf)


# --- authentication ---

def hmac_sha1(key, data):
    """HMAC-SHA1; key is the clear text password (bytes or str)."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, bytes(data), hashlib.sha1).digest()


def auth_digest(password, challenge):
    """The digest of MsgAuthResponse: HMAC-SHA1(key=password,
    data=the 20 challenge bytes) (ReflectorMsg.h calcHMAC)."""
    if len(challenge) != CHALLENGE_LEN:
        raise SvxError("challenge must be %d bytes" % CHALLENGE_LEN)
    return hmac_sha1(password, challenge)


# --- TCP builders (what a client or a fake server sends) ---

def proto_ver(major=3, minor=0):
    return tcp_frame(PROTO_VER, p_u16(major) + p_u16(minor))


def proto_ver_downgrade(major=3, minor=0):
    return tcp_frame(PROTO_VER_DOWNGRADE, p_u16(major) + p_u16(minor))


def heartbeat():
    return tcp_frame(HEARTBEAT)


def auth_challenge(challenge):
    if len(challenge) != CHALLENGE_LEN:
        raise SvxError("challenge must be %d bytes" % CHALLENGE_LEN)
    return tcp_frame(AUTH_CHALLENGE, p_bytes(challenge))


def auth_response(callsign, password, challenge):
    return tcp_frame(AUTH_RESPONSE,
                     p_str(callsign) + p_bytes(auth_digest(password, challenge)))


def auth_ok():
    return tcp_frame(AUTH_OK)


def error(text):
    return tcp_frame(ERROR, p_str(text))


def server_info(client_id, nodes, codecs):
    return tcp_frame(SERVER_INFO, p_u16(0) + p_u16(client_id)
                     + p_vec_str(nodes) + p_vec_str(codecs))


def node_joined(callsign):
    return tcp_frame(NODE_JOINED, p_str(callsign))


def node_left(callsign):
    return tcp_frame(NODE_LEFT, p_str(callsign))


def talker_start(tg, callsign):
    return tcp_frame(TALKER_START, p_u32(tg) + p_str(callsign))


def talker_stop(tg, callsign):
    return tcp_frame(TALKER_STOP, p_u32(tg) + p_str(callsign))


def select_tg(tg):
    return tcp_frame(SELECT_TG, p_u32(tg))


def tg_monitor(tgs):
    return tcp_frame(TG_MONITOR, p_set32(tgs))


def node_info_v3(iv_rand, key, json_text):
    return tcp_frame(NODE_INFO, p_bytes(iv_rand) + p_bytes(key) + p_str(json_text))


def node_info_v2(json_text):
    return tcp_frame(NODE_INFO, p_str(json_text))


def start_udp_encryption():
    return tcp_frame(START_UDP_ENCRYPTION)


# --- TCP decoder ---

Message = namedtuple("Message", "type name fields")


def _decode_rx_list(r):
    out = []
    for _ in range(r.u16()):
        ident, siglev, flags = r.u8(), r.u8(), r.u8()
        out.append({"id": chr(ident), "siglev": siglev,
                    "enabled": bool(flags & 1), "sql_open": bool(flags & 2),
                    "active": bool(flags & 4)})
    return out


def _decode_tx_list(r):
    out = []
    for _ in range(r.u16()):
        ident, flags = r.u8(), r.u8()
        out.append({"id": chr(ident), "transmit": bool(flags & 1)})
    return out


def decode_tcp(msg_type, body, client_proto=(3, 0)):
    """Fields of a TCP body as a dict. NodeInfo (111) depends on the protocol
    version (v3: iv_random, key, json; v2: json only), pass the version of
    the connection. Raises SvxError on short or trailing bytes. Unknown types
    return fields {"raw": body}, because the real peers ignore them
    (ReflectorClient.cpp onFrameReceived default branch)."""
    r = Reader(body)
    t = msg_type
    if t in (HEARTBEAT, AUTH_OK, START_ENCRYPTION_REQUEST, START_ENCRYPTION,
             CLIENT_CSR_REQUEST, CA_BUNDLE_REQUEST, START_UDP_ENCRYPTION):
        f = {}
    elif t in (PROTO_VER, PROTO_VER_DOWNGRADE):
        f = {"major": r.u16(), "minor": r.u16()}
    elif t == AUTH_CHALLENGE:
        f = {"challenge": r.blob()}
    elif t == AUTH_RESPONSE:
        f = {"callsign": r.string(), "digest": r.blob()}
    elif t == ERROR:
        f = {"text": r.string()}
    elif t in (CLIENT_CSR, CLIENT_CERT):
        f = {"pem": r.string()}
    elif t == CA_INFO:
        f = {"pem_size": r.u16(), "sha256": r.blob()}
    elif t == CA_BUNDLE:
        f = {"ca_pem": r.string(), "signature": r.blob(),
             "signing_cert_pem": r.string()}
    elif t == SERVER_INFO:
        f = {"reserved": r.u16(), "client_id": r.u16(),
             "nodes": r.vec_str(), "codecs": r.vec_str()}
    elif t == NODE_LIST:
        f = {"nodes": r.vec_str()}
    elif t in (NODE_JOINED, NODE_LEFT):
        f = {"callsign": r.string()}
    elif t in (TALKER_START, TALKER_STOP):
        if tuple(client_proto) < (2, 0):
            f = {"callsign": r.string()}
        else:
            f = {"tg": r.u32(), "callsign": r.string()}
    elif t in (SELECT_TG, REQUEST_QSY):
        f = {"tg": r.u32()}
    elif t == TG_MONITOR:
        f = {"tgs": r.set32()}
    elif t == STATE_EVENT:
        f = {"source": r.string(), "name": r.string(), "message": r.string()}
    elif t == NODE_INFO:
        if tuple(client_proto) >= (3, 0):
            f = {"iv_random": r.blob(), "key": r.blob(), "json": r.string()}
        else:
            f = {"json": r.string()}
    elif t == SIGNAL_STRENGTH:
        f = {"rx": _decode_rx_list(r)}
    elif t == TX_STATUS:
        f = {"tx": _decode_tx_list(r)}
    else:
        return Message(t, NAMES.get(t, "Unknown"), {"raw": bytes(body)})
    if not r.done():
        raise SvxError("%d trailing bytes in %s" % (len(body) - r.pos,
                                                    NAMES.get(t, t)))
    return Message(t, NAMES[t], f)


# --- UDP ---

def udp_v2(msg_type, client_id, seq, body=b""):
    """Legacy plaintext datagram: type | client_id | seq | body."""
    return p_u16(msg_type) + p_u16(client_id) + p_u16(seq & 0xFFFF) + bytes(body)


def udp_audio_body(payload):
    return p_bytes(payload)


UdpV2 = namedtuple("UdpV2", "type name client_id seq body")


def decode_udp_v2(data):
    if len(data) < 6:
        raise SvxError("legacy UDP datagram shorter than its 6 byte header")
    t, cid, seq = struct.unpack(">HHH", data[:6])
    return UdpV2(t, UDP_NAMES.get(t, "Unknown"), cid, seq, data[6:])


def decode_udp_body(msg_type, body):
    """Body of a UDP message (the same for v2 and for the decrypted v3
    plaintext after its 2 byte type)."""
    r = Reader(body)
    if msg_type in (UDP_HEARTBEAT, UDP_FLUSH_SAMPLES, UDP_ALL_SAMPLES_FLUSHED):
        f = {}
    elif msg_type == UDP_AUDIO:
        f = {"audio": r.blob()}
    elif msg_type == UDP_SIGNAL_STRENGTH:
        f = {"rx": _decode_rx_list(r)}
    else:
        return {"raw": bytes(body)}
    if not r.done():
        raise SvxError("trailing bytes in UDP %s" % UDP_NAMES.get(msg_type, msg_type))
    return f


def signal_strength_body(entries):
    """entries: (id char, siglev 0..255, flags) with flags bit0 enabled,
    bit1 sql_open, bit2 active."""
    out = p_u16(len(entries))
    for ident, siglev, flags in entries:
        out += bytes([ord(ident), siglev & 0xFF, flags & 0xFF])
    return out


UdpV3 = namedtuple("UdpV3", "counter client_id aad tag ciphertext")


def split_udp_v3(data, initial=False):
    """Splits a protocol 3 datagram into AAD, tag and ciphertext. The first
    datagram of a session (counter 0, the UDP registration) has a 6 byte AAD:
    counter | client_id; all others have a 4 byte AAD. 'initial' must be given
    by the caller; it is also implied when the counter reads 0 and the datagram
    is long enough, which is how the real server decides
    (Reflector.cpp udpCipherDataReceived)."""
    if len(data) < AAD_LEN:
        raise SvxError("datagram shorter than the 4 byte AAD")
    counter = struct.unpack(">I", data[:4])[0]
    if counter == 0 or initial:
        if len(data) < INITIAL_AAD_LEN + TAG_LEN:
            raise SvxError("registration datagram too short")
        cid = struct.unpack(">H", data[4:6])[0]
        aad_len = INITIAL_AAD_LEN
    else:
        cid = None
        aad_len = AAD_LEN
    if len(data) < aad_len + TAG_LEN:
        raise SvxError("datagram shorter than AAD + 8 byte tag")
    return UdpV3(counter, cid, data[:aad_len], data[aad_len:aad_len + TAG_LEN],
                 data[aad_len + TAG_LEN:])


def v3_iv(iv_random, direction_id, counter):
    """12 byte GCM IV = iv_random[6] | id:u16 | counter:u32. The id is the
    allocated client id for client to server datagrams and 0 for server to
    client datagrams (ReflectorClient.cpp udpCipherIV, ReflectorLogic.cpp
    sendUdpMsg)."""
    if len(iv_random) != IV_RAND_LEN:
        raise SvxError("iv_random must be %d bytes" % IV_RAND_LEN)
    return bytes(iv_random) + p_u16(direction_id) + p_u32(counter)


def v3_plaintext(msg_type, body=b""):
    """The data the GCM encrypts: type:u16 | body (no client id, no seq)."""
    return p_u16(msg_type) + bytes(body)
