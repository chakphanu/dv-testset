#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Plain-HTTP stand-in for register.allstarlink.org (asl-lab, docs/asl-lab.md), stdlib only, rig network only.

DV-Router's edge registers a public node by POSTing JSON to a configurable URL
([edges.allstar.register] server = "http://<host>:<port>/", insecure = true):

    {"port": <edge IAX2 UDP port>, "data": {"nodes": {"<node>": {"node": "<node>", "passwd": "<password>", "remote": 0}}}}

one node per request, Content-Type: application/json, client timeout 15 s. The reply is HTTP 200 JSON with
"ipaddr" (the address the registrar saw), "port", "refresh" and "data"; success means the text of "data" holds
"successfully registered". Which answer a request gets is chosen by its node number (VARIANTS, or the env
DVT_ASL_REG_VARIANTS "node=variant,..."):

    ok         success, ipaddr = the sender, port = the request's port, refresh 180
    port       success, but port = request port + 1 (the edge must report a mismatch)
    http500    HTTP 500 with a short text body
    notext     HTTP 200 JSON whose data has no "successfully registered"
    slow       the same answer as ok after SLOW_S (default 20 s: past the client's 15 s timeout)
    oversize   HTTP 200 JSON larger than 4 KiB (the client caps the reply at 4 KiB)
    (any other node: ok)

Every request is appended in full to the log file as one JSON line (time, peer, path, headers, raw body, parsed
body, variant, answer): the password is the lab fixture, nothing here is a credential. GET / answers "OK".
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("DVT_ASL_REG_LOG", "/var/lib/dvt-lab/register/requests.jsonl")
PORT = int(os.environ.get("DVT_ASL_HTTP_REG_PORT", "8082"))
SLOW_S = float(os.environ.get("DVT_ASL_REG_SLOW_S", "20"))
DEFAULT_VARIANTS = "1991=ok,1992=port,1993=http500,1994=notext,1995=slow,1996=oversize"
_lock = threading.Lock()


def variants():
    out = {}
    for item in os.environ.get("DVT_ASL_REG_VARIANTS", DEFAULT_VARIANTS).split(","):
        node, _, name = item.strip().partition("=")
        if node and name:
            out[node] = name
    return out


def answer(variant, peer_ip, body):
    """(status, bytes) of the reply for one parsed request body."""
    node = next(iter(body.get("data", {}).get("nodes", {})), "")
    port = int(body.get("port", 0))
    ok = {"ipaddr": peer_ip, "port": port, "refresh": 180, "data": "node %s successfully registered" % node}
    if variant == "port":
        ok["port"] = port + 1
    if variant == "http500":
        return 500, b"internal error\n"
    if variant == "notext":
        return 200, json.dumps({"ipaddr": peer_ip, "port": port, "refresh": 180, "data": "node %s pending" % node}).encode()
    if variant == "oversize":
        ok["data"] = "node %s successfully registered %s" % (node, "x" * 6000)
    return 200, json.dumps(ok).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "dvt-asl-registrar"

    def log_message(self, fmt, *args):
        print("registrar: " + fmt % args, flush=True)

    def _send(self, status, data, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, b"OK", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 1 << 20))
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        record = {"t": time.time(), "peer": self.client_address[0], "path": self.path,
                  "headers": {k: v for k, v in self.headers.items()}, "raw": raw.decode("utf-8", "replace"), "body": body}
        if not isinstance(body, dict) or not isinstance(body.get("data", {}).get("nodes"), dict):
            record["variant"], record["status"] = "bad-request", 400
            self._log(record)
            return self._send(400, b'{"error":"bad request"}')
        node = next(iter(body["data"]["nodes"]), "")
        variant = variants().get(node, "ok")
        record["variant"] = variant
        status, data = answer(variant, self.client_address[0], body)
        record["status"], record["reply_bytes"] = status, len(data)
        self._log(record)                      # logged before a slow reply is held back
        if variant == "slow":
            time.sleep(SLOW_S)
        try:
            self._send(status, data, "text/plain" if status == 500 else "application/json")
        except OSError:
            pass                               # the client gave up (slow variant): expected

    def _log(self, record):
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with _lock, open(LOG, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


def make_server(port=PORT, host="0.0.0.0"):
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    srv = make_server()
    print("registrar: listening on %d, variants %s" % (PORT, variants()), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
