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
  POST /rec/clear      {"scenario": "x"}                    removes that scenario's recordings
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DVT_ASL_AGENT_PORT", "8099"))
PCAP = "/var/lib/dvt-lab/pcap"
REC = "/var/spool/asterisk/dvt-rec"
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
            ["tcpdump", "-i", "eth0", "-n", "-U", "--immediate-mode", "-s", "0", "-Z", "root", "-w", path, "udp"],
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
        m = re.fullmatch(r"/files/(pcap|rec)(?:/(.+))?", self.path)
        if not m:
            return self.reply(404, {"error": "not found"})
        root = PCAP if m.group(1) == "pcap" else REC
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
        if self.path == "/rec/clear":
            name = str(req.get("scenario", ""))
            if not NAME.match(name):
                return self.reply(400, {"error": "bad name"})
            subprocess.run(["rm", "-rf", "--", os.path.join(REC, name)], check=False)
            return self.reply(200, {"cleared": name})
        return self.reply(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
