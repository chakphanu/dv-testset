# SPDX-License-Identifier: MIT
"""A tiny APRS-IS client (stdlib sockets) for the private aprsc of the rig (docs/dp15-cases.md, DP15-5a / 5b), and the
TNC2 line helpers the cases need. Only for the rig's own server: it logs in with the passcode COMPUTED from the login
callsign (dvtest/aprs_passcode.py), never against a public network.

APRS-IS text protocol as used here: after connect the server sends a banner comment line ("# ..."); the client answers
with one login line `user CALL pass N vers NAME VER [filter F]`; the server replies `# logresp CALL verified|unverified,
server ID`. Lines are CRLF terminated; lines starting with "#" are server comments (banner, logresp, keepalive every
20 s); everything else is a packet in TNC2 form `SRC>DST,PATH:information`.

  client = AprsisClient("172.30.0.47", login="N0TEST-7", filter="")
  client.connect()                                # raises AprsisError unless logged in (verified when `require_verified`)
  line = client.send_message("N0TEST-7", "XLX999B", "hello", msgid="12")
  hit = client.wait_packet(lambda p: p["info"].startswith(":N0TEST-7 :ack"), timeout=3.0)
  client.close()
"""
import re
import socket
import threading
import time

from dvtest import aprs_passcode

PORT = 14580
VERS = ("dvtest", "1.0")
LINE_MAX = 512                     # the server limit, CRLF included
ADDRESSEE_WIDTH = 9
TOCALL = "APDVT1"                  # destination of the tester's own packets
Q_CONSTRUCT = re.compile(r"^q[A-Z]{2}$")


class AprsisError(Exception):
    pass


def pad_addressee(call):
    """The 9 character message addressee field (the call padded with spaces)."""
    if not 1 <= len(call) <= ADDRESSEE_WIDTH:
        raise ValueError("addressee %r is not 1..9 characters" % call)
    return call.ljust(ADDRESSEE_WIDTH)


def message_info(addressee, text, msgid=None):
    """The information field of an APRS message: `:ADDRESSEE:text{id` (the id is optional, 1..5 characters)."""
    if "\r" in text or "\n" in text:
        raise ValueError("a message text has no line breaks")
    if msgid is not None and not 1 <= len(str(msgid)) <= 5:
        raise ValueError("a message id is 1..5 characters")
    return ":%s:%s%s" % (pad_addressee(addressee), text, "" if msgid is None else "{%s" % msgid)


def message_line(src, addressee, text, msgid=None, dest=TOCALL, path="TCPIP*"):
    """A TNC2 message line `SRC>DEST,PATH::ADDRESSEE:text{id` without CRLF (no q construct: a client never writes one)."""
    return "%s>%s,%s:%s" % (src, dest, path, message_info(addressee, text, msgid))


def parse_line(line):
    """{src, dest, path (list), q (the q construct and what follows it, list), before_q (path elements before it), info,
    raw} of a TNC2 line, or None when it is not one."""
    head, sep, info = line.partition(":")
    src, gt, rest = head.partition(">")
    if not sep or not gt or not src:
        return None
    parts = rest.split(",")
    dest, path = parts[0], parts[1:]
    q_at = next((i for i, item in enumerate(path) if Q_CONSTRUCT.match(item)), None)
    return {"src": src, "dest": dest, "path": path, "q": [] if q_at is None else path[q_at:],
            "before_q": path if q_at is None else path[:q_at], "info": info, "raw": line}


def parse_message(info):
    """(addressee, text, msgid or None) of a message information field, or None. The addressee is returned without its
    padding; an ack/rej text is `ack<id>` / `rej<id>` and is returned as the text."""
    if len(info) < 11 or info[0] != ":" or info[10] != ":":
        return None
    text, brace, msgid = info[11:].partition("{")
    return info[1:10].rstrip(), text, (msgid if brace else None)


def ack_id(info):
    """The id acked by an `ack<id>` message information field, else None."""
    parsed = parse_message(info)
    if parsed and parsed[1].startswith("ack") and len(parsed[1]) > 3:
        return parsed[1][3:]
    return None


def login_line(login, passcode, filter="", vers=VERS):
    """The login line without CRLF."""
    out = "user %s pass %s vers %s %s" % (login, passcode, vers[0], vers[1])
    return out + (" filter %s" % filter if filter else "")


def parse_logresp(line):
    """{login, verified, server} of `# logresp CALL verified, server ID`, else None."""
    m = re.match(r"^# logresp (\S+) (verified|unverified)(?:, server (\S+))?", line)
    return None if not m else {"login": m.group(1), "verified": m.group(2) == "verified", "server": m.group(3)}


class AprsisClient:
    """One login on an APRS-IS server. Lines are read in a thread: `comments` and `packets` are lists of
    (monotonic time, line); `wait_packet` polls the packets after a mark."""

    def __init__(self, host, port=PORT, login="N0TEST-7", passcode=None, filter="", vers=VERS, timeout=5.0):
        self.host, self.port, self.login, self.filter, self.vers, self.timeout = host, port, login, filter, vers, timeout
        self.passcode = aprs_passcode.aprs_passcode(login) if passcode is None else passcode
        self.sock = None
        self.banner = None
        self.logresp = None
        self.sent = []                          # (monotonic time, line) of every packet line written
        self.comments = []
        self.packets = []
        self.error = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    # -- connection

    def connect(self, require_verified=True):
        """Connect, read the banner, log in and wait for the logresp; the logresp dict. AprsisError when the server does
        not answer, or when `require_verified` and the login is not verified."""
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self._wait(lambda: self.comments, self.timeout, "no banner from %s:%d" % (self.host, self.port))
        self.banner = self.comments[0][1]
        self.sock.sendall((login_line(self.login, self.passcode, self.filter, self.vers) + "\r\n").encode("ascii"))
        self._wait(lambda: any(parse_logresp(line) for _t, line in self.comments), self.timeout, "no logresp for %s" % self.login)
        self.logresp = next(parse_logresp(line) for _t, line in self.comments if parse_logresp(line))
        if require_verified and not self.logresp["verified"]:
            raise AprsisError("login %s is unverified (the passcode is not the one of the callsign)" % self.login)
        return self.logresp

    def _wait(self, cond, timeout, reason):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if cond():
                    return
            if self.error is not None:
                raise AprsisError("%s: %s" % (reason, self.error))
            time.sleep(0.02)
        raise AprsisError(reason)

    def _read(self):
        buf = b""
        try:
            while not self.stop.is_set():
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    self.error = "connection closed by the server"
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.rstrip(b"\r").decode("utf-8", "replace")
                    with self.lock:
                        (self.comments if line.startswith("#") else self.packets).append((time.monotonic(), line))
        except OSError as exc:
            if not self.stop.is_set():
                self.error = str(exc)

    def close(self):
        self.stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(1.0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- writing

    def send_line(self, line):
        """Write one packet line (CRLF added). Returns (monotonic time, line)."""
        data = line.encode("ascii") + b"\r\n"
        if len(data) > LINE_MAX:
            raise ValueError("a line is at most %d bytes with CRLF" % LINE_MAX)
        self.sock.sendall(data)
        stamp = (time.monotonic(), line)
        self.sent.append(stamp)
        return stamp

    def send_message(self, src, addressee, text, msgid=None, **kw):
        """Send a message line (see message_line); returns the line text."""
        return self.send_line(message_line(src, addressee, text, msgid, **kw))[1]

    # -- reading

    def mark(self):
        """A position in `packets` for wait_packet(since=...)."""
        with self.lock:
            return len(self.packets)

    def snapshot(self):
        """[(time, line)] of the packets read so far."""
        with self.lock:
            return list(self.packets)

    def wait_packet(self, pred, timeout, since=0, poll=0.02):
        """(time, parsed, line) of the first packet after position `since` whose parse_line dict satisfies pred, else None."""
        end = time.monotonic() + timeout
        while True:
            for at, line in self.snapshot()[since:]:
                parsed = parse_line(line)
                if parsed is not None and pred(parsed):
                    return at, parsed, line
            if time.monotonic() >= end or self.error is not None:
                return None
            time.sleep(poll)

    def matching(self, pred, since=0):
        """[(time, parsed)] of every packet after `since` that satisfies pred."""
        out = []
        for at, line in self.snapshot()[since:]:
            parsed = parse_line(line)
            if parsed is not None and pred(parsed):
                out.append((at, parsed))
        return out
