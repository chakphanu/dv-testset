# SPDX-License-Identifier: MIT
"""A small DVSwitch-style USRP hub for dynamic clients: the behaviour that
docs/protocols/usrp.md section 5 recommends, so cases/proto/test_usrp_dynamic.py
can be proven end to end without a real server (DVT_USRP_DYNAMIC_HUB).

- TEXT "REG:DVSWITCH" registers the sender (keyed by source ip:port) and is
  answered "REG:OK"; a full table is answered "REG:UNREG"; "REG:UNREG"
  removes the client. Replies always go to the source ip:port.
- PING and any other packet refresh the client; no reply. A client silent
  for idle_timeout_s is dropped with "REG:UNREG".
- A DTMF dial string (module letter A..Z or 4001..4026) or a REMOTE_CMD
  "tgs=..." picks the module, confirmed with "INFO:MSG:module <X>". No
  module until one is picked. "INFO:" is answered with a small JSON.
- A client's SET_INFO callsign names it as talker. A client without a
  valid, not denied SET_INFO callsign may listen but never transmit.
  Voice (type 0, 352 B, keyup 1) from an allowed client on a module goes to
  every other client of the module: a padded SET_INFO first, then SLIN
  frames, then a 32 B unkey. One talker per module; unregistered senders
  are ignored.

The idle timeout is DVT_FAKE_HUB_IDLE_S (default 5 s); the unit test runs
the hub at 1 s.
"""
import json
import os
import re
import select
import socket
import threading
import time

from .proto import usrp

# well-formed, so it exercises the deny list (the rig puts it in xlxd.blacklist)
DENIED = "N0DENY"
CALLSIGN = re.compile(r"^[A-Z0-9]{3,8}(-[A-Z0-9]{1,2})?$")


def spec(idle=None):
    """The hub's behaviour as a "usrp" spec entry (the cases judge by it)."""
    idle = float(os.environ.get("DVT_FAKE_HUB_IDLE_S", "5")) if idle is None else idle
    return {
        "dynamic_server": True,
        "P-USR-29": {"unregistered_voice": "ignored"},
        "P-USR-31": {"idle_timeout_s": idle, "unreg_on_drop": True},
        "P-USR-33": {"dial_letter": True, "dial_tg": True},
        "P-USR-34": {"default_module": "none"},
        "P-USR-37": {"info_reply": "json"},
        "P-USR-40": {"max_clients": 6, "full_reply": "REG:UNREG"},
        "P-USR-41": {"denied_callsign": DENIED},
    }


SPEC = spec()

FLOOR_HANG = 0.5                                # a talker silent this long loses the floor


class Client:
    def __init__(self, addr):
        self.addr = addr
        self.module = None
        self.callsign = None                        # set by a valid SET_INFO
        self.seen = time.monotonic()
        self.seq = 0


class FakeHub:
    def __init__(self, bind=("127.0.0.1", 0), spec=SPEC):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind)
        self.addr = self.sock.getsockname()
        self.timeout = spec["P-USR-31"]["idle_timeout_s"]
        self.max_clients = spec["P-USR-40"]["max_clients"]
        self.denied = {spec["P-USR-41"]["denied_callsign"]}
        self.clients = {}
        self.floor = {}                             # module -> [talker addr, last voice time]
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def close(self):
        self.running = False
        self.thread.join(timeout=2)
        self.sock.close()

    def _send(self, client, packet_for_seq):
        self.sock.sendto(packet_for_seq(client.seq), client.addr)
        client.seq += 1

    def _text(self, client, payload, pad=False):
        self._send(client, lambda seq: usrp.text(seq, payload, pad=pad))

    def _end_floor(self, module):
        talker, _ = self.floor.pop(module)
        for client in self.clients.values():
            if client.module == module and client.addr != talker:
                self._send(client, usrp.unkey)

    def _run(self):
        while self.running:
            ready, _, _ = select.select([self.sock], [], [], 0.02)
            if ready:
                data, addr = self.sock.recvfrom(2048)
                self._packet(data, addr)
            now = time.monotonic()
            for module, (talker, last) in list(self.floor.items()):
                if now - last > FLOOR_HANG:
                    self._end_floor(module)
            for addr, client in list(self.clients.items()):
                if now - client.seen > self.timeout:
                    self._text(client, usrp.REG_UNREG)
                    del self.clients[addr]

    def _packet(self, data, addr):
        if len(data) < usrp.HEADER_SIZE or not data.startswith(b"USRP"):
            return
        client = self.clients.get(addr)
        text = usrp.text_payload(data)
        if text == usrp.REG:
            if client is None and len(self.clients) >= self.max_clients:
                self.sock.sendto(usrp.text(0, usrp.REG_UNREG), addr)
                return
            client = self.clients.setdefault(addr, Client(addr))
            client.seen = time.monotonic()
            self._text(client, usrp.REG_OK)
            return
        if client is None:
            return                                  # unregistered: ignored
        client.seen = time.monotonic()
        kind = usrp.type_le(data)
        if text == usrp.REG_UNREG:
            del self.clients[addr]
        elif text == usrp.INFO:
            body = json.dumps({"call": client.callsign, "module": client.module, "reflector": "XLX999"}).encode()
            self._text(client, usrp.INFO + body)
        elif text is not None:
            info = usrp.parse_set_info(data[usrp.HEADER_SIZE:])
            if info is not None:
                call = info["callsign"].strip().upper()
                client.callsign = call if CALLSIGN.match(call) and call not in self.denied else None
        elif kind == usrp.TYPE_DTMF:
            self._select(client, data[usrp.HEADER_SIZE:].split(b"\0", 1)[0].decode(errors="replace"))
        elif kind == usrp.TYPE_TLV:
            item = usrp.parse_tlv(data[usrp.HEADER_SIZE:])
            if item and item[0] == usrp.TLV_REMOTE_CMD and item[1].startswith(b"tgs="):
                self._select(client, item[1][4:].split(b",")[0].decode(errors="replace"))
        elif kind == usrp.TYPE_VOICE:
            self._voice(client, data)

    def _select(self, client, dial):
        dial = dial.strip().upper()
        module = None
        if len(dial) == 1 and "A" <= dial <= "Z":
            module = dial
        elif dial.isdigit() and 4001 <= int(dial) <= 4026:
            module = chr(ord("A") + int(dial) - 4001)
        if module:
            client.module = module
            self._text(client, b"INFO:MSG:module " + module.encode())

    def _voice(self, client, data):
        module = client.module
        if module is None or client.callsign is None:
            return                                  # no module, or listen-only (no valid callsign)
        frame = usrp.parse(data)
        floor = self.floor.get(module)
        if floor and floor[0] != client.addr:
            return                                  # one talker per module
        if frame.keyup == 0 or len(data) == usrp.HEADER_SIZE:
            if floor:
                self._end_floor(module)
            return
        if len(data) != usrp.VOICE_SIZE:
            return
        listeners = [c for c in self.clients.values() if c.module == module and c.addr != client.addr]
        if floor is None:
            for listener in listeners:
                self._send(listener, lambda seq: usrp.text_set_info(seq, client.callsign))
        self.floor[module] = [client.addr, time.monotonic()]
        pcm = data[usrp.HEADER_SIZE:]
        for listener in listeners:
            self._send(listener, lambda seq: usrp.voice(seq, pcm))
