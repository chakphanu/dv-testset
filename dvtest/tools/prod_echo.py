# SPDX-License-Identifier: MIT
"""Production voice check of a live reflector through its built-in echo module (docs/prod-checks.md).

Per protocol: link ONE client (the callsign given on the command line) to the echo module, send ONE over
(default 3.0 s = 150 frames, deterministic PRNG codewords exactly as E1 of cases/test_echo.py), wait for
the echo to replay it, judge the replay (one replay, 0.8 to 2.5 s after the end of the over, frame count,
duration, byte identity), unlink and print one JSON result per protocol.

    python3 -m dvtest.tools.prod_echo --host HOST --callsign dextra=N0CALL-T --callsign ysf=N0CALL \\
        --callsign m17=N0CALL --dry-run
    python3 -m dvtest.tools.prod_echo --host HOST --callsign N0CALL --i-have-owner-approval

This tool keys a module of a LIVE reflector. Safety rails, all enforced before the first packet:
  * --i-have-owner-approval is required for everything that is not --dry-run;
  * the module is E; any other module needs --module X AND --allow-other-module;
  * --seconds is capped at 5.0 (and floored at 1.2: an over under 50 frames is never replayed);
  * the protocols run one after the other with a settle pause of at least 6 s between them;
  * a global timeout; the client is unlinked in a finally block;
  * --dry-run builds the packets without opening a socket and without resolving the host name.
No callsign, host, address or secret is built in: they are arguments. The DMR Homebrew password is read
from a file and never printed or written.

Stdlib only. Exit code 0: every selected protocol passed (or dry run); 1: a protocol failed; 2: usage error.
"""
import argparse
import datetime
import json
import os
import re
import socket
import sys
import time

from dvtest import clients, echo
from dvtest.proto import dcs, dextra, dmr, dplus, m17, ysf

MODULE = "E"
DEFAULT_SECONDS = 3.0
MAX_SECONDS = 5.0
MIN_SECONDS = 1.2               # 60 frames; the echo never replays an over under 50 frames (echo.MIN_FRAMES)
WINDOW = (0.8, 2.5)             # allowed start of the replay after the last packet of the over (E1: 0.8 to 2.0 + margin)
DURATION_BELOW, DURATION_ABOVE = 0.5, 0.6     # replay length against the over length (E1: 2.5 to 3.6 for 3 s)
EXTRA_FRAMES_MAX = 12           # frames after the sent ones (M17 repeats, DMR / YSF pad to whole bursts / packets)
SETTLE_S = 6.0                  # pause between two protocols
MIN_SETTLE_S = 6.0
GLOBAL_TIMEOUT_S = 300.0
LINK_SETTLE_S = 0.5
UNLINK_WAIT_S = 1.0
QUIET_AFTER_REPLAY_S = 1.5      # watch this long after the replay ended for a second one
GAP = 0.4                       # echo.split_overs gap

DEFAULT_PROTOCOLS = ("dextra", "dplus", "dcs", "ysf", "m17")
METRIC_NOTE = ("no codec decoder in dvtest/audio (decoding goes through the AMBEd transcoder or an external "
               "codec2 / mbelib): rms and correlation are null; the judge is frame count, duration, timing "
               "and byte identity")

SKIPPED = {
    "g3": "Icom G3 terminal mode: the reflector sends voice to UDP 40000 of the CLIENT address, so the client must be "
          "reachable from the internet (no NAT, an open port); a normal test host is not, and the G3 presence "
          "handshake has no unlink packet to undo it",
    "imrs": "Yaesu IMRS (UDP 21110) is a peer list: the reflector must have the tester's address in its own IMRS "
            "destination list (the owner edits the reflector), there is no link or unlink packet and no client role",
}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def hexshort(data, limit=48):
    data = bytes(data)
    text = data[:limit].hex()
    return text + ("... (%d bytes)" % len(data) if len(data) > limit else "")


_CALL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/ .-]*$")


def check_callsign(proto, value, limit):
    value = value.strip().upper()
    if not _CALL_RE.match(value):
        raise UsageError("callsign for %s has characters outside A-Z 0-9 / - . and space" % proto)
    if len(value) > limit:
        raise UsageError("callsign for %s is %d characters, the protocol field holds %d (nothing is truncated "
                         "silently)" % (proto, len(value), limit))
    return value


# ---------------------------------------------------------------- the protocol table

class Proto:
    name = ""
    label = ""
    port = 0
    kind = ""                   # dvtest.echo.KINDS-style frame kind: dstar, dmr, m17, ysf
    callsign_limit = 8
    link_timeout = 5.0
    payload_asserted = True     # byte identity is part of the verdict
    default = True              # in the default protocol list (see DEFAULT_PROTOCOLS)

    def make(self, callsign, module, opts):
        raise NotImplementedError

    def talk(self, client, frames, sid, opts):
        raise NotImplementedError

    def unlink(self, client):
        client.unlink()

    def events(self, packets, times):
        return echo.events(self.kind, packets, times)

    def plan(self, callsign, module, frames, sid, opts):
        """{"link": [packets known before the first reply], "over": [every packet of the over], "unlink": [...],
        "notes": [text]} built without any socket."""
        raise NotImplementedError

    def frames(self, n, seed):
        return echo.make_frames(self.kind, n, seed)


class Dextra(Proto):
    name, label, port, kind = "dextra", "DExtra", dextra.PORT, "dstar"
    link_timeout = 3.0              # DstarClient.link waits the whole time

    def make(self, callsign, module, opts):
        return dextra.DstarClient("0.0.0.0", callsign=callsign, module=module)

    def talk(self, client, frames, sid, opts):
        client.talk(frames, sid, reflector=opts.reflector, tail=0.0)

    def plan(self, callsign, module, frames, sid, opts):
        over = [dextra.header(sid, callsign, module, opts.reflector)]
        over += [dextra.voice(sid, i, f, False, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA)
                 for i, f in enumerate(frames)]
        over.append(dextra.voice(sid, len(frames), b"\x00" * 9, True))
        return {"link": [dextra.link_packet(callsign, module)], "over": over,
                "unlink": [dextra.unlink_packet(callsign, module)], "notes": []}


class Dplus(Dextra):
    name, label, port = "dplus", "DPlus", dplus.PORT
    link_timeout = 4.0              # the login OK comes after a random 0 to 2 s

    def make(self, callsign, module, opts):
        return dplus.DplusClient("0.0.0.0", callsign=callsign, module=module)

    def events(self, packets, times):
        out = []
        for data, t in zip(packets, times):
            data = bytes(data)
            head = dplus.parse_header(data)
            if head:
                out.append(echo.Ev(t, "header", head["stream_id"], head["my"].decode("ascii", "replace").strip(), []))
                continue
            voice = dplus.parse_voice(data)
            if voice:
                sid, _seq, last, frame = voice
                out.append(echo.Ev(t, "end" if last else "voice", sid, None, [] if last else [frame]))
        return out

    def plan(self, callsign, module, frames, sid, opts):
        over = [dplus.header(sid, callsign, module, opts.reflector)]
        over += [dplus.voice(sid, i, f, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA)
                 for i, f in enumerate(frames)]
        over.append(dplus.last_voice(sid, len(frames)))
        return {"link": [dplus.CONNECT, dplus.login_packet(callsign)], "over": over, "unlink": [dplus.DISCONNECT],
                "notes": ["after the login the client joins the module with an empty stream (header + last "
                          "voice, random stream id), as dvtest.proto.dplus does"]}


class Dcs(Proto):
    name, label, port, kind = "dcs", "DCS", dcs.PORT, "dstar"
    callsign_limit = 7
    link_timeout = 4.0              # the ACK comes after a random 0 to 2 s

    def make(self, callsign, module, opts):
        return dcs.DcsClient("0.0.0.0", callsign=callsign, module=module)

    def talk(self, client, frames, sid, opts):
        client.talk(frames, sid, reflector=opts.reflector, tail=0.0)

    def events(self, packets, times):
        out = []
        for data, t in zip(packets, times):
            info = dcs.parse(bytes(data))
            if info:
                out.append(echo.Ev(t, "end" if info["last"] else "voice", info["stream_id"],
                                   info["my"].decode("ascii", "replace").strip(), [] if info["last"] else [info["frame"]]))
        return out

    def plan(self, callsign, module, frames, sid, opts):
        cs = dcs._cs7(callsign)
        over = [dcs.voice(sid, i, f, False, i, cs, module, opts.reflector,
                          dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA) for i, f in enumerate(frames)]
        over.append(dcs.voice(sid, len(frames), b"\x00" * 9, True, len(frames), cs, module, opts.reflector))
        return {"link": [dcs.link_packet(callsign, module)], "over": over,
                "unlink": [dcs.unlink_packet(callsign, module)], "notes": []}


class Ysf(Proto):
    name, label, port, kind = "ysf", "YSF", ysf.PORT, "ysf"
    callsign_limit = 10
    payload_asserted = False
    link_timeout = 4.0

    def make(self, callsign, module, opts):
        return ysf.YsfClient("0.0.0.0", callsign=callsign, module=module)

    def talk(self, client, frames, sid, opts):
        client.talk(frames, None, tail=0.0)

    def plan(self, callsign, module, frames, sid, opts):
        cs = callsign.upper()
        over = ysf.stream_packets(frames, cs, ysf.module_dgid(module))
        return {"link": [ysf.poll_packet(cs)], "over": over,
                "unlink": list(ysf.wiresx_disc_packets(cs)) + [ysf.unlink_packet(cs)],
                "notes": ["the poll is repeated every 0.3 s until the reflector acknowledges; the client then joins "
                          "the module with an empty stream (header + terminator, DG-ID of the module); YSF has no "
                          "stream id (stream_id is ignored)"]}


class M17(Proto):
    name, label, port, kind = "m17", "M17", m17.PORT, "m17"
    callsign_limit = 9

    def make(self, callsign, module, opts):
        return m17.M17Client("0.0.0.0", callsign=callsign, module=module)

    def talk(self, client, frames, sid, opts):
        client.talk(frames, sid, tail=0.0)

    def plan(self, callsign, module, frames, sid, opts):
        return {"link": [m17.conn_packet(callsign, module)], "over": m17.voice_packets(frames, sid, callsign),
                "unlink": [m17.disc_packet(callsign)], "notes": []}


class DmrHbClient(dmr.DmrClient):
    """DMR Homebrew (MMDVM) repeater login with a real password: RPTL, RPTK = sha256(salt + password), RPTC,
    keepalive, then the join of the module's talkgroup. The password lives in this object only; repr hides it."""

    def __init__(self, bind_ip, dmrid, module, callsign, password):
        super().__init__(bind_ip, dmrid=dmrid, module=module)
        self.callsign = callsign
        self._password = password
        self._seen = 0

    def __repr__(self):
        return "DmrHbClient(id=%s, callsign=%s)" % (self.id, self.callsign)

    def _next(self, timeout):
        """The next unread master reply (RPTACK / MSTNAK / MSTCL), or None."""
        end = time.monotonic() + timeout
        while True:
            self.client.drain()
            while self._seen < len(self.client.inbox):
                data = self.client.inbox[self._seen]
                self._seen += 1
                if dmr.parse_control(data):
                    return bytes(data)
            if time.monotonic() >= end:
                return None
            time.sleep(0.01)

    def link(self, node_ip, timeout=4.0, port=dmr.PORT, join_gap=0.3):
        self.dest = (node_ip, port)
        self.client.send(self.dest, dmr.rptl(self.id))
        reply = self._next(timeout)
        if not reply or not reply.startswith(b"RPTACK") or len(reply) < 10:
            return False
        salt = reply[6:10]
        self.client.send(self.dest, dmr.rptk(self.id, dmr.auth_hash(salt, self._password)))
        reply = self._next(timeout)
        if not reply or not reply.startswith(b"RPTACK"):
            return False
        self.client.send(self.dest, dmr.rptc(self.id, callsign=self.callsign))
        reply = self._next(timeout)
        if not reply or not reply.startswith(b"RPTACK"):
            return False
        self.client.send(self.dest, dmr.rptping(self.id))
        self.client.pump(0.2)
        self.join(None, join_gap)
        return True

    def unlink(self):
        super().unlink()
        self.close_link()
        self.client.pump(0.2)


class DmrHb(Proto):
    name, label, port, kind = "dmrhb", "DMR Homebrew", dmr.PORT, "dmr"
    callsign_limit = 8
    default = False
    link_timeout = 4.0

    def make(self, callsign, module, opts):
        return DmrHbClient("0.0.0.0", opts.dmr_id, module, callsign, opts.dmr_password())

    def talk(self, client, frames, sid, opts):
        client.talk(frames, stream_id=sid, tail=0.0)

    def plan(self, callsign, module, frames, sid, opts):
        tg = dmr.module_tg(module)
        voice, seq = dmr.voice_packets(frames, opts.dmr_id, tg, sid)
        over = [dmr.packet(0, opts.dmr_id, tg, opts.dmr_id, 0xA1, sid, dmr.data_sync_payload())] + voice
        over.append(dmr.packet(seq, opts.dmr_id, tg, opts.dmr_id, 0xA2, sid, dmr.data_sync_payload()))
        return {"link": [dmr.rptl(opts.dmr_id)], "over": over,
                "unlink": [dmr.packet(0, opts.dmr_id, 4000, opts.dmr_id, 0xA1, sid, dmr.data_sync_payload()),
                           dmr.packet(1, opts.dmr_id, 4000, opts.dmr_id, 0xA2, sid, dmr.data_sync_payload()),
                           dmr.rptcl(opts.dmr_id, 13)],
                "notes": ["RPTK (sha256 of the master's salt + the password file), RPTC and the keepalive follow the "
                          "reply to RPTL; the join is an empty stream to TG %d (random stream id); the unlink "
                          "packets shown are an empty stream to TG 4000, then RPTCL" % tg]}


PROTOCOLS = {p.name: p for p in (Dextra(), Dplus(), Dcs(), Ysf(), M17(), DmrHb())}


# ---------------------------------------------------------------- options

class Options:
    """Everything a run needs, after validation. Tests build one with build_options() and change timing fields."""

    def __init__(self):
        self.host = ""
        self.protocols = []
        self.module = MODULE
        self.callsigns = {}              # protocol -> checked callsign
        self.ports = {}                  # protocol -> port
        self.seconds = DEFAULT_SECONDS
        self.reflector = "XLX999"
        self.dry_run = False
        self.out = None
        self.settle = SETTLE_S
        self.timeout = GLOBAL_TIMEOUT_S
        self.window = WINDOW
        self.link_settle = LINK_SETTLE_S
        self.unlink_wait = UNLINK_WAIT_S
        self.quiet_after = QUIET_AFTER_REPLAY_S
        self.dmr_id = None
        self.dmr_pass_file = None
        self.stream_ids = {}             # protocol -> fixed stream id (tests); random otherwise

    @property
    def frames(self):
        return int(round(self.seconds / echo.FRAME_S))

    def dmr_password(self):
        with open(self.dmr_pass_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()


def parse_mapping(values, what, allowed=None):
    """[`VALUE`, `PROTO=VALUE`, ...] -> (default or None, {proto: value})."""
    default, mapping = None, {}
    for item in values or []:
        if "=" in item:
            proto, _, value = item.partition("=")
            proto = proto.strip().lower()
            if allowed is not None and proto not in allowed:
                raise UsageError("%s: unknown protocol %r (known: %s)" % (what, proto, ", ".join(sorted(allowed))))
            if not value.strip():
                raise UsageError("%s %s= has no value" % (what, proto))
            mapping[proto] = value.strip()
        else:
            if not item.strip():
                raise UsageError("empty %s" % what)
            default = item.strip()
    return default, mapping


def make_parser():
    ap = argparse.ArgumentParser(
        prog="python3 -m dvtest.tools.prod_echo",
        description="Production voice check of a live reflector through its echo module: link one client per "
                    "protocol, send one over, judge the replay, unlink. Owner approval required.")
    ap.add_argument("--host", required=True, help="reflector host name or address (never built in)")
    ap.add_argument("--protocol", action="append", metavar="NAME[,NAME]",
                    help="protocol to check, repeatable or comma separated (default %s; also dmrhb; g3 and imrs "
                         "are not supported, see docs/prod-checks.md)" % ",".join(DEFAULT_PROTOCOLS))
    ap.add_argument("--callsign", action="append", metavar="[PROTO=]VALUE",
                    help="callsign of the client; PROTO=VALUE sets it for one protocol (repeatable), a bare VALUE "
                         "is the default; nothing is built in")
    ap.add_argument("--port", action="append", metavar="PROTO=N", help="override the default port of a protocol")
    ap.add_argument("--module", default=MODULE, help="module to link (default E, the echo module)")
    ap.add_argument("--allow-other-module", action="store_true",
                    help="safety rail: needed for any module but E (a real talk on a real module)")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help="length of the over, %.1f to %.1f (default %.1f = %d frames)" % (
                        MIN_SECONDS, MAX_SECONDS, DEFAULT_SECONDS, int(DEFAULT_SECONDS / echo.FRAME_S)))
    ap.add_argument("--reflector", default="XLX999",
                    help="reflector name for RPT2 of the D-STAR headers (default XLX999; the reflector accepts any)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and the first and last packet of each step; opens no socket, resolves nothing")
    ap.add_argument("--i-have-owner-approval", action="store_true",
                    help="REQUIRED for every run that is not --dry-run: the owner of the reflector approved this check")
    ap.add_argument("--one-at-a-time", action="store_true",
                    help="accepted for explicitness: protocols always run one after the other")
    ap.add_argument("--settle", type=float, default=SETTLE_S,
                    help="seconds between two protocols (default %.0f, minimum %.0f)" % (SETTLE_S, MIN_SETTLE_S))
    ap.add_argument("--timeout", type=float, default=GLOBAL_TIMEOUT_S,
                    help="global timeout in seconds (default %.0f); protocols not started by then are skipped" % GLOBAL_TIMEOUT_S)
    ap.add_argument("--out", help="output directory (default out/prod-echo-<utc>/)")
    ap.add_argument("--dmr-id", type=int, help="DMR id of the test repeater (needed for dmrhb)")
    ap.add_argument("--dmr-pass-file", help="file holding the DMR Homebrew password (needed for dmrhb; never printed)")
    return ap


def build_options(argv):
    ap = make_parser()
    ns = ap.parse_args(argv)
    opts = Options()
    opts.host = ns.host.strip()
    if not opts.host:
        raise UsageError("--host is empty")
    names = []
    for item in ns.protocol or [",".join(DEFAULT_PROTOCOLS)]:
        names.extend(n.strip().lower() for n in item.split(",") if n.strip())
    for name in names:
        if name in SKIPPED:
            raise UsageError("protocol %s is not supported: %s" % (name, SKIPPED[name]))
        if name not in PROTOCOLS:
            raise UsageError("unknown protocol %r (known: %s; not supported: %s)" % (
                name, ", ".join(PROTOCOLS), ", ".join(SKIPPED)))
    opts.protocols = list(dict.fromkeys(names))
    if not opts.protocols:
        raise UsageError("no protocol selected")

    if not ns.dry_run and not ns.i_have_owner_approval:
        raise UsageError("this tool keys a module of a live reflector: pass --i-have-owner-approval once the owner of "
                         "the reflector approved the check (or --dry-run to see the plan without sending anything)")
    opts.dry_run = ns.dry_run

    opts.module = (ns.module or "").strip()
    if not re.fullmatch(r"[A-Z]", opts.module):
        raise UsageError("--module must be one capital letter")
    if opts.module != MODULE and not ns.allow_other_module:
        raise UsageError("module %s refused: this check only keys module %s (the echo module); another module needs "
                         "--module %s --allow-other-module" % (opts.module, MODULE, opts.module))

    if not (ns.seconds == ns.seconds) or ns.seconds > MAX_SECONDS:
        raise UsageError("--seconds %s is above the hard cap of %.1f" % (ns.seconds, MAX_SECONDS))
    if ns.seconds < MIN_SECONDS:
        raise UsageError("--seconds %s is below %.1f: the echo does not replay an over under %d frames" % (
            ns.seconds, MIN_SECONDS, echo.MIN_FRAMES))
    opts.seconds = ns.seconds

    if ns.settle < MIN_SETTLE_S:
        raise UsageError("--settle %s is below %.0f s" % (ns.settle, MIN_SETTLE_S))
    opts.settle = ns.settle
    if ns.timeout <= 0:
        raise UsageError("--timeout must be positive")
    opts.timeout = ns.timeout
    opts.reflector = (ns.reflector or "XLX999").strip().upper()[:7]

    default, mapping = parse_mapping(ns.callsign, "--callsign", PROTOCOLS)
    for name in opts.protocols:
        value = mapping.get(name, default)
        if value is None:
            raise UsageError("no callsign for %s: pass --callsign VALUE or --callsign %s=VALUE" % (name, name))
        opts.callsigns[name] = check_callsign(name, value, PROTOCOLS[name].callsign_limit)

    for name, value in parse_mapping(ns.port, "--port", PROTOCOLS)[1].items():
        try:
            port = int(value)
        except ValueError:
            raise UsageError("--port %s=%s is not a number" % (name, value)) from None
        if not 1 <= port <= 65535:
            raise UsageError("--port %s=%d is out of range" % (name, port))
        opts.ports[name] = port

    if "dmrhb" in opts.protocols:
        if ns.dmr_id is None or not ns.dmr_pass_file:
            raise UsageError("dmrhb needs --dmr-id and --dmr-pass-file (a test repeater id and a file with its password)")
        if not 1 <= ns.dmr_id < 1 << 32:
            raise UsageError("--dmr-id out of range")
        if not os.path.isfile(ns.dmr_pass_file):
            raise UsageError("--dmr-pass-file %s is not a file" % ns.dmr_pass_file)
        opts.dmr_id, opts.dmr_pass_file = ns.dmr_id, ns.dmr_pass_file
    opts.out = ns.out
    return opts


# ---------------------------------------------------------------- the verdict (pure)

def payload_report(sent, heard):
    """Where the sent frames sit in the heard ones: {"found", "offset", "extra", "missing", "first_diff"}. The
    sent frames must come contiguously and in order (offset 0 for D-STAR; the M17 repeat at the start gives 2)."""
    sent, heard = [bytes(f) for f in sent], [bytes(f) for f in heard]
    at = next((k for k in range(len(heard) - len(sent) + 1) if heard[k:k + len(sent)] == sent), None) if sent else None
    cmp = echo.compare_payload(sent, heard)
    return {"found": at is not None, "offset": at, "extra": len(heard) - len(sent) if at is not None else None,
            "missing": cmp.missing, "first_diff": None if at is not None else cmp.first_diff}


def judge(proto, frames, end, packets, times, window=WINDOW, linked=True):
    """The result fields that come from what was heard. `proto` is a Proto, `frames` the frames sent, `end` the
    monotonic time of the last packet of the over, `packets` / `times` everything the client received since the
    link. Pure: no I/O, no clock."""
    out = {"sent_frames": len(frames), "received_frames": 0, "duration_s": None, "replay_delay_s": None,
           "byte_identical": None, "rms": None, "correlation": None, "replays": 0, "replay_idents": [],
           "payload_offset": None, "extra_frames": None, "metric_note": METRIC_NOTE}
    fails = []
    if not linked:
        return out, ["link failed"]
    evs = proto.events(packets, times)
    overs = [o for o in echo.split_overs(evs, GAP) if o.n_voice > 0]
    replays = echo.after(overs, end + 0.05)
    out["replays"] = len(replays)
    if not replays:
        return out, ["no replay heard (watched %.1f s after the over)" % (
            max([t for t in times] + [end]) - end)]
    if len(replays) > 1:
        fails.append("%d replays heard, want one (starts %s s after the over)" % (
            len(replays), ", ".join("%.2f" % (o.start - end) for o in replays)))
    replay = replays[0]
    out["received_frames"] = len(replay.frames)
    out["replay_idents"] = [str(i) for i in replay.idents]
    delay = replay.start - end
    out["replay_delay_s"] = round(delay, 3)
    if not echo.in_window(delay, *window):
        fails.append("replay starts %.2f s after the over, want %.1f to %.1f" % (delay, window[0], window[1]))
    length = echo.duration(replay)
    out["duration_s"] = None if length is None else round(length, 3)
    over_s = echo.over_seconds(len(frames))
    if length is None or not (over_s - DURATION_BELOW <= length <= over_s + DURATION_ABOVE):
        fails.append("replay lasts %s s, want %.1f to %.1f" % (
            "?" if length is None else "%.2f" % length, over_s - DURATION_BELOW, over_s + DURATION_ABOVE))
    if not replay.ended:
        fails.append("the replay has no end of stream")
    rep = payload_report(frames, replay.frames)
    out["payload_offset"], out["extra_frames"] = rep["offset"], rep["extra"]
    out["byte_identical"] = rep["found"]
    if len(replay.frames) < len(frames):
        fails.append("%d of %d frames heard" % (len(replay.frames), len(frames)))
    elif rep["found"] and rep["extra"] > EXTRA_FRAMES_MAX:
        fails.append("%d frames after the sent ones, want at most %d" % (rep["extra"], EXTRA_FRAMES_MAX))
    elif not rep["found"] and len(replay.frames) > len(frames) + EXTRA_FRAMES_MAX:
        fails.append("%d frames heard for %d sent" % (len(replay.frames), len(frames)))
    if not rep["found"] and proto.payload_asserted:
        fails.append("payload differs from the sent frames (first difference at frame %s, heard %d, missing %d)" % (
            rep["first_diff"], len(replay.frames), rep["missing"]))
    if not rep["found"] and not proto.payload_asserted:
        out["metric_note"] += ("; %s payload not asserted: the egress re-encodes AMBE+2 frames (PRNG frames are not "
                               "valid codewords), see docs/echo-cases.md E1c" % proto.label)
    return out, fails


# ---------------------------------------------------------------- one protocol, live

def _watch(client, until, done=None):
    """Drain the client's socket (answering keepalives) until `until` (monotonic) or done() is true."""
    last_check = 0.0
    while True:
        client.client.drain()
        now = time.monotonic()
        if done is not None and now - last_check >= 0.05:
            last_check = now
            if done():
                return True
        if now >= until:
            return False
        time.sleep(0.004)


def run_protocol(proto, opts, address, deadline, emit=lambda msg: None):
    """Link, one over, wait for the replay, judge, unlink. Returns the result dict. The client is unlinked and
    closed whatever happens."""
    callsign = opts.callsigns[proto.name]
    port = opts.ports.get(proto.name, proto.port)
    started = time.monotonic()
    result = {"protocol": proto.name, "host": opts.host, "port": port, "module": opts.module,
              "callsign_used": callsign, "started_utc": utc_stamp()}
    frames = proto.frames(opts.frames, "prod-echo-" + proto.name)
    sid = opts.stream_ids.get(proto.name) or clients.fresh_stream_id(proto.kind)
    result["stream_id"] = sid if proto.name != "ysf" else None
    client = None
    linked = False
    end = None
    err = None
    packets, times = [], []
    try:
        client = proto.make(callsign, opts.module, opts)
        emit("%s: linking to module %s" % (proto.name, opts.module))
        left = max(1.0, min(proto.link_timeout, deadline - time.monotonic()))
        linked = bool(client.link(address, timeout=left, port=port))
        if linked:
            emit("%s: linked; settling %.1f s" % (proto.name, opts.link_settle))
            _watch(client, time.monotonic() + opts.link_settle)
            client.client.inbox.clear()
            client.client.inbox_times.clear()
            if time.monotonic() >= deadline:
                err = "global timeout before the over"
            else:
                emit("%s: sending %d frames (%.1f s)" % (proto.name, len(frames), opts.seconds))
                proto.talk(client, frames, sid, opts)
                end = time.monotonic()
                over_s = echo.over_seconds(len(frames))
                hard = min(deadline, end + opts.window[1] + over_s + 3.0)
                give_up = end + opts.window[1] + 1.0

                def finished():
                    packets, times = list(client.client.inbox), list(client.client.inbox_times)
                    overs = echo.after([o for o in echo.split_overs(proto.events(packets, times), GAP) if o.n_voice > 0],
                                       end + 0.05)
                    now = time.monotonic()
                    if not overs:
                        return now >= give_up
                    return overs[-1].ended and now - overs[-1].end >= opts.quiet_after

                _watch(client, hard, finished)
    except Exception as exc:                        # noqa: BLE001 - reported in the result, the unlink below still runs
        err = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    finally:
        unlinked = False
        if client is not None:
            try:
                if getattr(client, "dest", None):
                    emit("%s: unlinking" % proto.name)
                    proto.unlink(client)
                    _watch(client, time.monotonic() + opts.unlink_wait)
                    unlinked = True
            except Exception as exc:                # noqa: BLE001
                err = err or "unlink: %s: %s" % (type(exc).__name__, str(exc)[:160])
            packets, times = list(client.client.inbox), list(client.client.inbox_times)
            try:
                client.client.close()
            except Exception:                       # noqa: BLE001
                pass
    if end is None:
        judged = {"sent_frames": len(frames), "received_frames": 0, "duration_s": None, "replay_delay_s": None,
                  "byte_identical": None, "rms": None, "correlation": None, "replays": 0, "replay_idents": [],
                  "payload_offset": None, "extra_frames": None, "metric_note": METRIC_NOTE}
        fails = [err or ("link failed (no acknowledgement within %.1f s)" % proto.link_timeout)]
    else:
        judged, fails = judge(proto, frames, end, packets, times, opts.window, linked=True)
        if err:
            fails.append(err)
    result.update(judged)
    result["linked"] = linked
    result["unlinked"] = unlinked
    if not unlinked:
        fails.append("unlink not sent")
    result["pass"] = not fails
    result["reason"] = "ok" if not fails else "; ".join(fails)
    result["elapsed_s"] = round(time.monotonic() - started, 1)
    return result


# ---------------------------------------------------------------- plan (dry run)

def make_plan(opts):
    """The plan as data: for every protocol the packets it would send (hex) and the checks that follow. No socket."""
    plan = []
    for name in opts.protocols:
        proto = PROTOCOLS[name]
        callsign = opts.callsigns[name]
        frames = proto.frames(opts.frames, "prod-echo-" + name)
        sid = opts.stream_ids.get(name) or clients.fresh_stream_id(proto.kind)
        data = proto.plan(callsign, opts.module, frames, sid, opts)
        over = data["over"]
        plan.append({
            "protocol": name, "host": opts.host, "port": opts.ports.get(name, proto.port), "module": opts.module,
            "callsign_used": callsign, "frames": len(frames), "over_seconds": opts.seconds,
            "stream_id": sid if name != "ysf" else None,
            "link_packets_before_reply": [hexshort(p) for p in data["link"]],
            "over_packets": len(over), "over_first": hexshort(over[0]), "over_last": hexshort(over[-1]),
            "unlink_packets": [hexshort(p) for p in data["unlink"]], "notes": data["notes"],
            "expect": "one replay %.1f to %.1f s after the last packet of the over, %s frames in %.1f to %.1f s%s" % (
                opts.window[0], opts.window[1], len(frames), opts.seconds - DURATION_BELOW, opts.seconds + DURATION_ABOVE,
                "" if proto.payload_asserted else "; payload recorded, not asserted")})
    return plan


def print_plan(opts, plan, out=None):
    out = out or sys.stdout
    w = lambda s="": print(s, file=out)                         # noqa: E731
    w("DRY RUN: nothing is sent, no socket is opened, the host name is not resolved")
    w("host %s  module %s  over %.1f s = %d frames  reflector name in RPT2 %s" % (
        opts.host, opts.module, opts.seconds, opts.frames, opts.reflector))
    w("order: %s, one at a time, %.0f s settle between protocols, global timeout %.0f s" % (
        ", ".join(opts.protocols), opts.settle, opts.timeout))
    for item in plan:
        w()
        w("[%s] port %d  callsign %s" % (item["protocol"], item["port"], item["callsign_used"]))
        w("  link (first packets, before any reply): %s" % "; ".join(item["link_packets_before_reply"]))
        w("  over: %d packets  first %s" % (item["over_packets"], item["over_first"]))
        w("                    last  %s" % item["over_last"])
        w("  unlink: %s" % "; ".join(item["unlink_packets"]))
        w("  expect: %s" % item["expect"])
        for note in item["notes"]:
            w("  note: %s" % note)
    skipped = [n for n in PROTOCOLS if n not in opts.protocols]
    w()
    w("not selected: %s" % (", ".join(skipped) or "-"))
    for name, why in SKIPPED.items():
        w("not supported: %s: %s" % (name, why))
    w("To run for real: drop --dry-run and add --i-have-owner-approval.")


# ---------------------------------------------------------------- the run

def summary_lines(results, skipped):
    rows = ["%-7s %-5s %6s %6s %8s %8s %-9s %s" % ("proto", "pass", "sent", "heard", "dur_s", "delay_s", "identical", "reason")]
    for r in results:
        rows.append("%-7s %-5s %6s %6s %8s %8s %-9s %s" % (
            r["protocol"], "PASS" if r["pass"] else "FAIL", r.get("sent_frames"), r.get("received_frames"),
            r.get("duration_s"), r.get("replay_delay_s"), r.get("byte_identical"), r["reason"]))
    for name, why in skipped:
        rows.append("%-7s skipped: %s" % (name, why))
    rows.append("rms / correlation: not measured (%s)" % METRIC_NOTE.split(":")[0])
    return rows


def execute(opts, out=None, err=None):
    """Run the selected protocols one after the other; returns (results, exit code)."""
    out, err = out or sys.stdout, err or sys.stderr
    say = lambda msg: print(msg, file=err, flush=True)          # noqa: E731
    start = time.monotonic()
    deadline = start + opts.timeout
    out_dir = opts.out or os.path.join("out", "prod-echo-" + utc_stamp())
    os.makedirs(out_dir, exist_ok=True)
    try:
        address = socket.gethostbyname(opts.host)
    except OSError as exc:
        raise UsageError("cannot resolve %s: %s" % (opts.host, exc)) from exc
    say("prod_echo: host %s, protocols %s, module %s, out %s" % (opts.host, ",".join(opts.protocols), opts.module, out_dir))
    results = []
    try:
        for i, name in enumerate(opts.protocols):
            proto = PROTOCOLS[name]
            if time.monotonic() >= deadline:
                res = {"protocol": name, "host": opts.host, "port": opts.ports.get(name, proto.port),
                       "module": opts.module, "callsign_used": opts.callsigns[name], "sent_frames": opts.frames,
                       "received_frames": 0, "duration_s": None, "replay_delay_s": None, "byte_identical": None,
                       "rms": None, "correlation": None, "pass": False,
                       "reason": "skipped: global timeout of %.0f s reached" % opts.timeout}
            else:
                if i:
                    say("prod_echo: settling %.0f s" % opts.settle)
                    time.sleep(opts.settle)
                res = run_protocol(proto, opts, address, deadline, emit=say)
            results.append(res)
            print(json.dumps(res), file=out, flush=True)
            with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as fh:
                json.dump(res, fh, indent=1)
    except KeyboardInterrupt:
        say("prod_echo: interrupted; the running protocol was unlinked")
    document = {"tool": "dvtest.tools.prod_echo", "started_utc": utc_stamp(), "host": opts.host, "address": address,
                "module": opts.module, "seconds": opts.seconds, "frames": opts.frames, "results": results,
                "not_supported": SKIPPED, "metric_note": METRIC_NOTE}
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=1)
    lines = summary_lines(results, list(SKIPPED.items()))
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    say("")
    for line in lines:
        say(line)
    say("prod_echo: wrote %s (results.json, summary.txt, one JSON per protocol)" % out_dir)
    ok = bool(results) and len(results) == len(opts.protocols) and all(r["pass"] for r in results)
    return results, 0 if ok else 1


def main(argv=None):
    try:
        opts = build_options(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        print("prod_echo: %s" % exc, file=sys.stderr)
        return 2
    if opts.dry_run:
        try:
            plan = make_plan(opts)
        except UsageError as exc:
            print("prod_echo: %s" % exc, file=sys.stderr)
            return 2
        print_plan(opts, plan)
        print(json.dumps({"dry_run": True, "plan": plan}))
        return 0
    try:
        return execute(opts)[1]
    except UsageError as exc:
        print("prod_echo: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
