#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Control agent of one asl-lab container (stdlib only, rig network only).

The tester has no docker socket, so this small HTTP server is the lab's hands:
it runs allow-listed Asterisk CLI commands, starts and stops one tcpdump per
scenario, takes the tone file, writes the registration lines and hands the
pcap and recording files back. JSON in, JSON out; files are raw bytes.

  GET  /health
  GET  /versions       package versions of this image (dpkg-query lines written at build time)
  POST /cli            {"cmd": "rpt fun 199901 *3199902"}   -> {"rc", "out"}
  POST /scenario       {"name": "link-basic"}               sets the recording directory
  POST /capture/start  {"name": "link-basic"}               tcpdump -> pcap/<name>.pcap
  POST /capture/stop   {"name": "link-basic"}               -> {"bytes"}
  PUT  /media/<name>   raw bytes -> sounds/dvt/<name>       (<name> [A-Za-z0-9_.-]+, <= 4 MiB)
  POST /register       {"lines": ["register => ..."]}       writes dvt-register.conf, reloads chan_iax2
  GET  /files/pcap     list;  GET /files/pcap/<name>        bytes
  GET  /files/rec      list (recursive); GET /files/rec/<scenario>/<file>
  GET  /files/reg      the HTTP registrar stand-in's log (requests.jsonl), asl-reg only
  POST /rec/clear      {"scenario": "x"}                    removes that scenario's recordings
  POST /ext/register   {"on": true|false}                   role node-ext: move the staged registration line (written at start from
                                                            the mounted password file, never read here) into rpt_http_registrations.conf
                                                            (on) or an empty list (off) and reload res_rpt_http_registrations
  POST /resolve        {"name": "400169.nodes.allstarlink.org", "type": "A|SRV|TXT"} -> {"rcode", "answers": [{"type","ttl","text"}]}
                                                            (role node-ext only: node names of the real zone, through the container's resolver,
                                                            the path app_rpt's own lookups take; the docker resolver is not on eth0, so a capture
                                                            does not show them)
Role node-ext (DVT_ASL_EXT=1, profile asl-ext): tcpdump runs on the interface of the default route with the filter
DVT_ASL_CAP_FILTER (the real network leaves through the second, non-internal network), and "core stop gracefully" is allowed.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DVT_ASL_AGENT_PORT", "8099"))
PCAP = "/var/lib/dvt-lab/pcap"
REC = "/var/spool/asterisk/dvt-rec"
REG = "/var/lib/dvt-lab/register"          # requests.jsonl of the HTTP registrar stand-in (rig/asl-lab-registrar.py)
MEDIA = "/var/lib/asterisk/sounds/dvt"
REGISTER = "/etc/asterisk/dvt-register.conf"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# CLI commands the cases may run (prefix match on the lower-cased command)
CLI_ALLOW = (
    "rpt fun ", "rpt cmd ", "rpt playback ", "rpt localplay ", "rpt status ", "rpt lstats ",
    "rpt nodes ", "rpt stats ", "rpt show ", "rpt reload", "rpt record ",
    "iax2 show ", "iax2 set debug", "iax2 reload", "core show ", "module reload chan_iax2",
    "module show like ", "dialplan set global ", "dialplan show ", "channel originate ",
    "channel request hangup ", "database show", "mixmonitor ", "logger rotate",
)
EXT = os.environ.get("DVT_ASL_EXT") == "1"
if EXT:
    CLI_ALLOW += ("core stop gracefully", "module reload res_rpt_http_registrations")
CAP_FILTER = os.environ.get("DVT_ASL_CAP_FILTER", "udp")
NODE_NAME = re.compile(r"^(?:_iax\._udp\.)?[0-9]{4,6}\.nodes\.allstarlink\.org$")
DNS_TYPES = {"A": 1, "TXT": 16, "SRV": 33}
_captures = {}
_lock = threading.Lock()


def run_cli(cmd):
    low = cmd.lower().strip()
    if not low.startswith(CLI_ALLOW):
        return 126, "refused: %r is not an allowed CLI command" % cmd
    try:
        proc = subprocess.run(["asterisk", "-rx", cmd], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return proc.returncode, proc.stdout + proc.stderr


def dns_name(data, pos, depth=0):
    labels = []
    while True:
        n = data[pos]
        if n == 0:
            return ".".join(labels), pos + 1
        if n & 0xC0 == 0xC0:
            if depth > 8:
                raise ValueError("dns pointer loop")
            tail, _ = dns_name(data, ((n & 0x3F) << 8) | data[pos + 1], depth + 1)
            return ".".join(labels + [tail]) if tail else ".".join(labels), pos + 2
        labels.append(data[pos + 1:pos + 1 + n].decode("ascii", "replace"))
        pos += 1 + n


def dns_query(name, qtype):
    """One UDP query to the first nameserver of /etc/resolv.conf: {"rcode", "answers": [{"type", "ttl", "text"}]}."""
    import struct
    ns = None
    with open("/etc/resolv.conf") as fh:
        for line in fh:
            f = line.split()
            if len(f) >= 2 and f[0] == "nameserver" and ":" not in f[1]:
                ns = f[1]
                break
    if ns is None:
        return {"rcode": -1, "answers": [], "error": "no nameserver"}
    ident = int.from_bytes(os.urandom(2), "big")
    q = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(x)]) + x.encode() for x in name.split(".")) + b"\0" + struct.pack("!HH", DNS_TYPES[qtype], 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    data = None
    try:
        for _ in range(3):
            sock.sendto(q, (ns, 53))
            try:
                data, _src = sock.recvfrom(4096)
                if data[:2] == q[:2]:
                    break
                data = None
            except socket.timeout:
                continue
    finally:
        sock.close()
    if data is None or len(data) < 12:
        return {"rcode": -1, "answers": [], "error": "no answer"}
    _id, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    pos = 12
    for _ in range(qd):
        _n, pos = dns_name(data, pos)
        pos += 4
    answers = []
    for _ in range(an):
        _n, pos = dns_name(data, pos)
        rtype, _cls, ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        rd = data[pos:pos + rdlen]
        if rtype == 1 and rdlen == 4:
            answers.append({"type": "A", "ttl": ttl, "text": ".".join(str(b) for b in rd)})
        elif rtype == 33 and rdlen > 6:
            prio, weight, port = struct.unpack("!HHH", rd[:6])
            target, _ = dns_name(data, pos + 6)
            answers.append({"type": "SRV", "ttl": ttl, "text": "%d %d %d %s" % (prio, weight, port, target)})
        elif rtype == 16:
            parts, i = [], 0
            while i < rdlen:
                parts.append(rd[i + 1:i + 1 + rd[i]].decode("ascii", "replace"))
                i += 1 + rd[i]
            answers.append({"type": "TXT", "ttl": ttl, "text": " ".join(parts)})
        pos += rdlen
    return {"rcode": flags & 0xF, "answers": answers}


def capture_iface():
    """eth0, or (role node-ext) the interface of the default route, found in /proc/net/route."""
    if not EXT:
        return "eth0"
    try:
        with open("/proc/net/route") as fh:
            for line in fh.read().splitlines()[1:]:
                f = line.split()
                if len(f) > 7 and f[1] == "00000000" and f[7] == "00000000":
                    return f[0]
    except OSError:
        pass
    return "eth0"


def start_capture(name):
    if not NAME.match(name):
        return 400, {"error": "bad name"}
    with _lock:
        if name in _captures:
            return 409, {"error": "capture %s already running" % name}
        os.makedirs(PCAP, exist_ok=True)
        path = os.path.join(PCAP, name + ".pcap")
        log = open(path + ".log", "wb")
        proc = subprocess.Popen(
            ["tcpdump", "-i", capture_iface(), "-n", "-U", "--immediate-mode", "-s", "0", "-Z", "root", "-w", path]
            + CAP_FILTER.split(),
            stdout=log, stderr=log)
        _captures[name] = proc
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        # tcpdump writes the pcap header before the socket is attached: wait for its "listening" line
        if os.path.exists(path + ".log") and b"listening on" in open(path + ".log", "rb").read():
            return 200, {"started": name}
        time.sleep(0.05)
    return 500, {"error": "tcpdump did not start"}


def stop_capture(name):
    with _lock:
        proc = _captures.pop(name, None)
    if proc is None:
        return 404, {"error": "no capture %s" % name}
    time.sleep(0.3)         # packets still in the kernel buffer
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return 200, {"bytes": os.path.getsize(os.path.join(PCAP, name + ".pcap"))}


def list_files(root):
    found = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".log") and root == PCAP:
                continue
            full = os.path.join(base, f)
            found.append({"path": os.path.relpath(full, root), "bytes": os.path.getsize(full)})
    return sorted(found, key=lambda e: e["path"])


def safe_join(root, rel):
    full = os.path.normpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


class Handler(BaseHTTPRequestHandler):
    server_version = "dvt-asl-lab"

    def log_message(self, fmt, *args):
        print("agent: " + fmt % args, flush=True)

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit=4 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n)

    def json_body(self):
        try:
            data = json.loads(self.body(1 << 16) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        if self.path == "/versions":
            text = ""
            for name in ("/etc/asterisk/dvt-versions.txt", "/etc/asterisk/dvt-lab-versions.txt"):
                with open(name) as fh:
                    text += fh.read()
            return self.reply(200, text.encode(), "text/plain")
        m = re.fullmatch(r"/files/(pcap|rec|reg)(?:/(.+))?", self.path)
        if not m:
            return self.reply(404, {"error": "not found"})
        root = {"pcap": PCAP, "rec": REC, "reg": REG}[m.group(1)]
        if m.group(2) is None:
            return self.reply(200, list_files(root))
        full = safe_join(root, m.group(2))
        if full is None or not os.path.isfile(full):
            return self.reply(404, {"error": "no such file"})
        with open(full, "rb") as fh:
            return self.reply(200, fh.read(), "application/octet-stream")

    def do_PUT(self):
        m = re.fullmatch(r"/media/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})", self.path)
        if not m:
            return self.reply(404, {"error": "not found"})
        try:
            data = self.body()
        except ValueError as err:
            return self.reply(413, {"error": str(err)})
        os.makedirs(MEDIA, exist_ok=True)
        with open(os.path.join(MEDIA, m.group(1)), "wb") as fh:
            fh.write(data)
        os.chmod(os.path.join(MEDIA, m.group(1)), 0o644)
        return self.reply(200, {"bytes": len(data)})

    def do_POST(self):
        req = self.json_body()
        if req is None:
            return self.reply(400, {"error": "bad json"})
        if self.path == "/cli":
            rc, out = run_cli(str(req.get("cmd", "")))
            return self.reply(200, {"rc": rc, "out": out})
        if self.path == "/scenario":
            name = str(req.get("name", ""))
            if not NAME.match(name):
                return self.reply(400, {"error": "bad name"})
            os.makedirs(os.path.join(REC, name), exist_ok=True)
            os.system("chown -R asterisk:asterisk %s" % REC)
            rc, out = run_cli("dialplan set global DVT_SCEN %s" % name)
            return self.reply(200, {"rc": rc, "out": out})
        if self.path == "/capture/start":
            code, body = start_capture(str(req.get("name", "")))
            return self.reply(code, body)
        if self.path == "/capture/stop":
            code, body = stop_capture(str(req.get("name", "")))
            return self.reply(code, body)
        if self.path == "/register":
            lines = req.get("lines", [])
            if not isinstance(lines, list) or any(
                    not re.fullmatch(r"register => [A-Za-z0-9_.-]+:[A-Za-z0-9_.-]*@[0-9.]+(:[0-9]+)?", str(x)) for x in lines):
                return self.reply(400, {"error": "register lines only"})
            with open(REGISTER, "w") as fh:
                fh.write("".join(str(x) + "\n" for x in lines))
            os.system("chown asterisk:asterisk %s; chmod 640 %s" % (REGISTER, REGISTER))
            rc, out = run_cli("module reload chan_iax2.so")
            return self.reply(200, {"rc": rc, "out": out})
        if self.path == "/ext/register":
            if not EXT:
                return self.reply(400, {"error": "role node-ext only"})
            live = "/etc/asterisk/rpt_http_registrations.conf"
            staged = "/etc/asterisk/dvt-ext-registration.staged"
            tmp = live + ".new"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                if req.get("on"):
                    with open(staged, "rb") as src:
                        out.write(src.read())
                else:
                    out.write(b"[general]\nregister_interval = 180\n\n[registrations]\n")
            shutil.chown(tmp, "asterisk", "asterisk")
            os.replace(tmp, live)
            rc, out_text = run_cli("module reload res_rpt_http_registrations.so")
            return self.reply(200, {"rc": rc, "out": out_text, "on": bool(req.get("on"))})
        if self.path == "/resolve":
            name = str(req.get("name", ""))
            qtype = str(req.get("type", "A"))
            if not EXT or not NODE_NAME.match(name) or qtype not in DNS_TYPES:
                return self.reply(400, {"error": "node names of the real zone, types A SRV TXT (role node-ext)"})
            try:
                reply = dns_query(name, qtype)
            except (OSError, ValueError, IndexError) as err:
                reply = {"rcode": -1, "answers": [], "error": err.__class__.__name__}
            return self.reply(200, reply)
        if self.path == "/rec/clear":
            name = str(req.get("scenario", ""))
            if not NAME.match(name):
                return self.reply(400, {"error": "bad name"})
            subprocess.run(["rm", "-rf", "--", os.path.join(REC, name)], check=False)
            return self.reply(200, {"cleared": name})
        return self.reply(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
