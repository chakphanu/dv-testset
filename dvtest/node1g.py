# SPDX-License-Identifier: MIT
"""NODE1g (DV-Router, image dv-router:wip-42d5473): a HOTSPOT node linked to a CENTRAL node over an emulated WAN.

What the product says (docs/design/link.md, answers-364-node1g.md): node link v1 over UDP (edges.link.port, default 31800), the hotspot dials out
from an ephemeral socket (an `[[edges.link.uplink]]` row), the central node answers the source it saw (an `[[edges.link.account]]` row); both send
a keepalive every 5 s and a session is dead after 30 s without a valid datagram; the credential is one pre-shared 32-byte key (64 hex digits, mode
0600) with HMAC-SHA256 proofs both ways and no expiry; after a lost session or a failed handshake the client retries after 0.5 s, doubling to 5 s
(+-20 percent); a NAT rebinding moves an ESTABLISHED session to the new source without a handshake; an over in flight when the link goes dark ends
by stream silence after 1.6 s (the floor is released then, the room folds the over end_missing).

This module is pure (stdlib only) except for the small clients it builds on (dvtest.nat control protocol, dvtest.admin): the switches, the hub
counters of the link, the refusal rate, the key files, the arrival-time evidence of a P25 listener, the plain-HTTP admin client of the two nodes and
the WAN router client. Cases: cases/test_node1g.py; unit tests: cases/unit/test_node1g.py; document: docs/node1g-cases.md.
"""
import http.client
import os
import re
import secrets
import tempfile

from dvtest import admin, metrics, nat
from dvtest.proto import p25

SWITCH = "DVT_NODE1G"
LINKOBS_SWITCH = "DVT_LINKOBS1"
SKIP_REASON = ("DVT_NODE1G=on is not set (run scripts/run.sh --profile twonode with DVT_NODE1G=on on an image with the node link: "
               "dv-router:wip-42d5473 or later; docs/node1g-cases.md)")
LINKOBS_REASON = ("DVT_LINKOBS1=on is not set (LINKOBS1, image wip-4732c43 and later: the hotspot's Reject counter, dv_edge_link_session_ends_total, "
                  "Bye{revoked}, datagrams bad_source, handshakes unknown_session)")

# the pair
ACCOUNT = "n0call-hs1"            # the account row of the central node (name), and the account the hotspot's uplink row names
UPLINK = "central"                # the name of the hotspot's uplink row
HOTSPOT_ROOM, CENTRAL_ROOM = "B", "C"
TG = 31672                        # P25 talk group of both nodes' server ports (UDP 41000)
LINK_PORT = 31800
KEY_NAME, ROTATED_NAME = "hs1.key", "rotated.key"      # in out/<run>/link-secrets/
UNMAPPED_BASE = 1030000           # radio ids of the relink probes: in no directory of the rig (rig/config/dmrid.dat ends at 1023230)

# the contract (answers-364-node1g.md, link.md): seconds
KEEPALIVE_S = 5.0
SESSION_DEAD_S = 30.0
RELINK_S = 10.0                   # "link up within 10 s of the blackout end"
RELINK_FIX_S = 12.0               # after a wrong key is put right: the attempt in progress ends at its 5 s deadline, then a backoff of up to 6 s, then the handshake (no number in the contract)
STREAM_SILENCE_S = 1.6            # an over without a Unit ends after this; the floor is released then
NAT_GAP_S = 5.0                   # media gap after a NAT rebinding: at most one keepalive period
NAT_GAP_SLACK_S = 0.2             # measurement slack of the arrival clock (the tester polls its sockets every 10 ms; netem off)
REFUSAL_RATE_MAX = 1.1            # Reject{auth} per second per hotspot: backoff 0.5 .. 5 s with Auth every 0.5 s for a 5 s attempt = about 1.0

# the impairments (docs/node1g-cases.md "Why these values"): per direction, the router shapes both of its interfaces
DELAY_ARGS = "delay 60ms 10ms"
LOSS_ARGS = "loss 3%"
DELAY_ONE_WAY_MS = 60.0
RTT_RANGE_MS = (100.0, 220.0)     # 2 x 60 ms +- jitter, plus the two nodes' own processing

HANDSHAKE_RESULTS = ("accepted", "welcomed", "auth", "version", "bad_proof", "auth_replay", "auth_capacity", "amplification")
MUST_NOT_MOVE_HANDSHAKE = ("auth_replay", "auth_capacity", "amplification", "bad_proof")
MUST_NOT_MOVE_DATAGRAM_RX = ("bad_tag", "unknown_session", "malformed")


LINKREBIND_SWITCH = "DVT_LINKREBIND1"
LINKREBIND_GAP_S = 1.2            # LINKREBIND1: the central-to-hotspot gap after a NAT rebinding stays below this (a hotspot keepalive at most every 1 s while it receives)
LINKREBIND_SHARE = 0.97           # and every over of the trials is heard at least this much


def linkrebind_on(environ=None):
    """DVT_LINKREBIND1=on: the image has the LINKREBIND1 fix (the hotspot sends a keepalive at most every 1 s while it receives a stream); NODE1g-f / -g then
    assert the post-fix numbers as well. Default off: they assert the contract (gap 5 s, 90 percent heard) and fail on the defect."""
    return (environ if environ is not None else os.environ).get(LINKREBIND_SWITCH, "").strip().lower() in ("on", "1", "true", "yes")


LINKSEQ_SWITCH = "DVT_LINKSEQ1"
LINKSEQ_RESEQ_MS = 40.0           # LINKSEQ1: a unit reordered within about 40 ms is resequenced and played; later ones may stay late and be refused
LINKSEQ_LOSS_PPM_TOL = 5000.0     # LINKSEQ1: the row's loss_ppm is within this of the missing-unit share (0.5 percentage point; zero missing gives about 0)
FRAME_COUNTER_KEYS = ("late", "lost", "gap", "dropped")


def linkseq_on(environ=None):
    """DVT_LINKSEQ1=on: the image has LINKSEQ1 (a reordered unit within about 40 ms is resequenced and played, every missing frame is visible as late, lost or
    gap, the row's loss_ppm counts missing units); NODE1g-a then asserts it (default off: the current behaviour, recorded only)."""
    return (environ if environ is not None else os.environ).get(LINKSEQ_SWITCH, "").strip().lower() in ("on", "1", "true", "yes")


def unit_counters(moved):
    """The late / lost / gap / dropped unit counts a node's hub delta holds: the room's (`ounits.<room>.<result>`) and the edges'
    (`eunits.<proc>.<dir>.<result>`): {key: value} of the series that moved."""
    return {k: v for k, v in moved.items() if k.startswith(("ounits.", "eunits.")) and k.rsplit(".", 1)[1] in FRAME_COUNTER_KEYS and v}


def late_units(moved):
    """The late units of the link edge's ingress (`eunits.link.rx.late`), the number LINKSEQ1 wants at 0 for reorders inside 40 ms."""
    return moved.get("eunits.link.rx.late", 0.0)


def linkseq_problems(heard, sent_frames, mv, rows):
    """The LINKSEQ1 asserts of the delay-only case, as sentences (empty when they hold).
    heard: {"h2c": share, "c2h": share} of the over's frames the listener heard; sent_frames: frames per over; mv: {node: hub delta}; rows: {node: row or None}.
      a) every over heard 100 percent
      b) accounting: the frames missing (both directions) are at most the sum of the late, lost, gap and dropped unit counters of every node and edge
      c) the row's loss_ppm of the receiving node (central: h2c, hotspot: c2h) is within LINKSEQ_LOSS_PPM_TOL of the missing-unit share of that direction"""
    out = []
    missing = {}
    for who, share in heard.items():
        missing[who] = int(round((1.0 - share) * sent_frames))
        if missing[who]:
            out.append("LINKSEQ1 a: %s: the listener heard %.1f percent (%d of %d frames missing), wanted 100" % (who, 100 * share, missing[who], sent_frames))
    total = sum(sum(unit_counters(m).values()) for m in mv.values())
    if sum(missing.values()) > total:
        out.append("LINKSEQ1 b: %d frames missing at the listeners but the late / lost / gap / dropped counters of both nodes add up to %g (%s)" % (
            sum(missing.values()), total, {n: unit_counters(m) for n, m in mv.items()}))
    for who, node in (("h2c", "central"), ("c2h", "hotspot")):
        row = rows.get(node)
        if row is None or row.get("loss_ppm") is None or who not in missing:
            continue
        want = 1e6 * missing[who] / float(sent_frames)
        if abs(row["loss_ppm"] - want) > LINKSEQ_LOSS_PPM_TOL:
            out.append("LINKSEQ1 c: %s row loss_ppm %s, the missing-unit share of %s is %.0f ppm (%d of %d), wanted within %.0f" % (
                node, row["loss_ppm"], who, want, missing[who], sent_frames, LINKSEQ_LOSS_PPM_TOL))
    return out


def on(environ=None):
    """DVT_NODE1G=on: run.sh made the pair a hotspot behind a WAN router and its central node (--profile twonode)."""
    return (environ if environ is not None else os.environ).get(SWITCH, "").strip().lower() in ("on", "1", "true", "yes")


def linkobs_on(environ=None):
    """DVT_LINKOBS1=on: the image has LINKOBS1 (a future image)."""
    return (environ if environ is not None else os.environ).get(LINKOBS_SWITCH, "").strip().lower() in ("on", "1", "true", "yes")


# ---------------------------------------------------------------- hub counters

def counters(text):
    """{name: value} of what the cases judge, from one scrape of a node's hub (`metrics xlxd-sut` / `metrics dvr-peer`):

      hs.<result>            dv_edge_link_handshakes_total{result}
      sessions               dv_edge_link_sessions (gauge)
      rtt_ms                 dv_edge_link_rtt_ms (gauge, mean of the known samples, 0 when none)
      dgram.rx.<result>      dv_edge_link_datagrams_total{dir=rx,result}
      dgram.tx.<result>      the same for dir=tx
      se.<reason>            dv_edge_link_session_ends_total{reason} (LINKOBS1: timeout, bye, replaced, revoked)
      refused.<reason>       dv_edge_link_refused_total{reason}
      restarts.<child>       dv_launcher_child_restarts_total{child}
      overs.<room>.<result>  dv_room_overs_total{room,result}, room as the label gives it
      ounits.<room>.<result> dv_room_over_units_total{room,result} (late, lost, gap: the room's units of closed overs)
      eunits.<proc>.<dir>.<result>  dv_edge_over_units_total{proc,dir,result} (the edges' units of closed overs: rx late, dropped, gap; tx late, dropped, lost)
      onair.<room>           dv_status_room_onair{room} (gauge; the hub labels rooms in lower case)
      procs_down             number of dv_status_process_up series that are 0
    Every series that is not in the text is absent (a delta treats absent as 0)."""
    out = {}
    for s in metrics.parse(text):
        lab = s.labels
        if s.name == "dv_edge_link_handshakes_total" and "result" in lab:
            key = "hs.%s" % lab["result"]
        elif s.name == "dv_edge_link_sessions":
            key = "sessions"
        elif s.name == "dv_edge_link_rtt_ms":
            key = "rtt_ms"
        elif s.name == "dv_edge_link_datagrams_total" and "dir" in lab and "result" in lab:
            key = "dgram.%s.%s" % (lab["dir"], lab["result"])
        elif s.name == "dv_edge_link_session_ends_total" and "reason" in lab:
            key = "se.%s" % lab["reason"]
        elif s.name == "dv_edge_link_refused_total" and "reason" in lab:
            key = "refused.%s" % lab["reason"]
        elif s.name == "dv_launcher_child_restarts_total" and "child" in lab:
            key = "restarts.%s" % lab["child"]
        elif s.name == "dv_room_overs_total" and "room" in lab and "result" in lab:
            key = "overs.%s.%s" % (lab["room"].upper(), lab["result"])
        elif s.name == "dv_room_over_units_total" and "room" in lab and "result" in lab:
            key = "ounits.%s.%s" % (lab["room"].upper(), lab["result"])
        elif s.name == "dv_edge_over_units_total" and "dir" in lab and "result" in lab:
            key = "eunits.%s.%s.%s" % (lab.get("proc", "-"), lab["dir"], lab["result"])
        elif s.name == "dv_status_room_onair" and "room" in lab:
            key = "onair.%s" % lab["room"].upper()
        elif s.name == "dv_status_process_up":
            if s.value == 0:
                out["procs_down"] = out.get("procs_down", 0.0) + 1.0
            continue
        else:
            continue
        out[key] = out.get(key, 0.0) + s.value if not key.startswith(("sessions", "rtt_ms", "onair.")) else s.value
    return out


def delta(before, after):
    """{name: after - before} for the names whose value changed (a name missing on one side counts as 0)."""
    out = {}
    for key in set(before) | set(after):
        d = after.get(key, 0.0) - before.get(key, 0.0)
        if d:
            out[key] = d
    return out


def collateral(moved, allow_restarts=(), allow_rx=(), linkobs=False):
    """The things that must NOT move after a recovery (answers-364 section 5), as sentences: the handshake results auth_replay, auth_capacity,
    amplification and bad_proof, the datagram results bad_tag, unknown_session and malformed (except those named in `allow_rx`: a case that
    restarts the edge on purpose sees datagrams of the old session), any link refusal, any child restart except the children in
    `allow_restarts`, and a process of the hub's table that is down. `linkobs` (LINKOBS1 image): also `bad_source` datagrams (a reply from a changed
    central source: none in any fault of the cases) and `handshakes{unknown_session}` (the server's stateless hint after it lost a session) unless
    `unknown_session` is allowed."""
    out = []
    if linkobs:
        if moved.get("dgram.rx.bad_source"):
            out.append("datagram rx result bad_source moved by %g" % moved["dgram.rx.bad_source"])
        if "unknown_session" not in allow_rx and moved.get("hs.unknown_session"):
            out.append("handshake result unknown_session moved by %g" % moved["hs.unknown_session"])
    for name in MUST_NOT_MOVE_HANDSHAKE:
        if moved.get("hs.%s" % name):
            out.append("handshake result %s moved by %g" % (name, moved["hs.%s" % name]))
    for name in MUST_NOT_MOVE_DATAGRAM_RX:
        if name not in allow_rx and moved.get("dgram.rx.%s" % name):
            out.append("datagram rx result %s moved by %g" % (name, moved["dgram.rx.%s" % name]))
    for key, value in sorted(moved.items()):
        if key.startswith("refused.") and value:
            out.append("link refusal %s moved by %g" % (key.split(".", 1)[1], value))
        if key.startswith("restarts.") and value and key.split(".", 1)[1] not in allow_restarts:
            out.append("child %s restarted %g time(s)" % (key.split(".", 1)[1], value))
    return out


LINK_PROCESS_PREFIXES = ("hs.", "dgram.", "refused.", "se.", "sessions", "rtt_ms")      # the series of the dv-edge-link process: they restart with it


def rebase(before, after, restarted=LINK_PROCESS_PREFIXES):
    """Like `delta`, for a node whose dv-edge-link was restarted between the two reads (a key reload): the series of that process start again at
    zero, so for those names the later value IS the movement since the restart; every other series (the launcher's, the room controllers', the
    hub's) is a plain difference."""
    out = {}
    for key in set(before) | set(after):
        if key.startswith(restarted):
            d = after.get(key, 0.0)
        else:
            d = after.get(key, 0.0) - before.get(key, 0.0)
        if d:
            out[key] = d
    return out


SE_REASONS = ("timeout", "bye", "replaced", "revoked")


def ends_problems(moved, expect=None):
    """LINKOBS1: the session ends a node counted (`dv_edge_link_session_ends_total{reason}`, once per ended session on each side) against what the fault
    must give: `expect` {reason: n}, every other reason 0. Sentences, empty when they agree."""
    expect = expect or {}
    out = []
    for reason in SE_REASONS:
        got, want = int(moved.get("se.%s" % reason, 0)), int(expect.get(reason, 0))
        if got != want:
            out.append("session_ends{reason=%s} moved by %d, wanted %d" % (reason, got, want))
    return out


# ---------------------------------------------------------------- the central node's TOML (revocation cases)

_TABLE = "[[edges.link.account]]"


def _account_blocks(lines):
    """[(start, end, name)] of the `[[edges.link.account]]` blocks of a TOML text split in lines (end exclusive)."""
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip() == _TABLE:
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("["):
                j += 1
            name = None
            for ln in lines[i + 1:j]:
                key, sep, val = ln.partition("=")
                if sep and key.strip() == "name":
                    name = val.strip().strip('"')
            out.append((i, j, name))
            i = j
        else:
            i += 1
    return out


def remove_account(text, name):
    """The TOML text without the `[[edges.link.account]]` block called `name` (ValueError when there is none)."""
    lines = text.split("\n")
    for start, end, found in _account_blocks(lines):
        if found == name:
            return "\n".join(lines[:start] + lines[end:])
    raise ValueError("no account row called %s" % name)


def set_account_secret_file(text, name, path):
    """The TOML text with the `secret_file` of the account row `name` set to `path` (ValueError when there is no such row or key)."""
    lines = text.split("\n")
    for start, end, found in _account_blocks(lines):
        if found == name:
            for k in range(start + 1, end):
                if lines[k].partition("=")[0].strip() == "secret_file":
                    lines[k] = 'secret_file = "%s"' % path
                    return "\n".join(lines)
    raise ValueError("no secret_file of the account row %s" % name)


def link_active(counts):
    """True when a scrape shows one connected link session."""
    return counts.get("sessions", 0.0) >= 1.0


# ---------------------------------------------------------------- the refusal rate

def refusal_rate(samples):
    """(rate per second, window seconds, events) of a counter that grows in bursts, from [(t, value)] reads taken while it grows. The hub
    refreshes its counters every 10 s, so a read shows the value of the last refresh: the instants at which the value GROWS from one read to the
    next are the refreshes (known to within one read interval). The rate is the growth between the first and the last growth instant over the
    time between them; the events before the first refresh and after the last one are not counted. A read that is LOWER than the one before is a
    counter that restarted with its process (an edge restarted by a reload): that step is not a growth and is skipped. None when fewer than two
    growths were seen."""
    changes = []
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        if v1 > v0:
            changes.append((t1, v1 - v0))
    if len(changes) < 2:
        return None
    window = changes[-1][0] - changes[0][0]
    if window <= 0:
        return None
    events = sum(step for _, step in changes[1:])          # the first growth only marks where the window starts
    return events / window, window, events


# ---------------------------------------------------------------- key files

KEY_RE = re.compile(r"^[0-9a-fA-F]{64}\n?$")


def new_key_hex(rng=None):
    """A fresh 32-byte key as 64 lower-case hex digits."""
    return (rng or secrets).token_hex(32)


def valid_key_text(text):
    """True for exactly 64 hex digits and an optional trailing newline (link.md "Keys and accounts")."""
    return bool(KEY_RE.match(text))


def write_key(path, key_hex):
    """Replace `path` with a regular file of mode 0600 that holds `key_hex` and a newline, by writing a sibling file and renaming it over the old
    one (the documented rotation, link.md: the launcher then sees one inode change, never a half-written key). Nothing is returned or printed."""
    if not valid_key_text(key_hex):
        raise ValueError("not a 64 hex digit key")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".key-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(key_hex.strip() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_key(path):
    """The key text of `path` (kept in memory by a case to put it back; never printed)."""
    with open(path, encoding="ascii") as fh:
        text = fh.read()
    if not valid_key_text(text):
        raise ValueError("%s does not hold a 64 hex digit key" % os.path.basename(path))
    return text.strip()


# ---------------------------------------------------------------- the admin API of a node (plain HTTP)

class PlainAdmin(admin.AdminClient):
    """The admin API of one node over plain HTTP on its own address (rig/config/dv-router/node1g-hotspot.toml and node1g-central.toml: `[admin]
    bind = <OWN_IP>:8080`, `host` the same, `allow_from` the tester, totp on). dv-svc-admin has no TLS; its Host mount demands the effective host
    to equal `host` and an Origin, when sent, to be https://<host>, which the base client does. The cookies are handled by the base client by name
    (the Secure attribute is not enforced here)."""

    def __init__(self, ip, port=8080, **kw):
        super().__init__("https://%s:%d" % (ip, port), None, mount="host", **kw)
        self._plain = (ip, port)

    def _connect(self):
        return http.client.HTTPConnection(self._plain[0], self._plain[1], timeout=self.timeout)

    def sign_in(self, user, password):
        """Log in, enrolling the authenticator at the first login (the rig's users have no TOTP secret yet). Returns the identity."""
        self.hide(password)
        ident = self.login(user, password)
        if ident.get("totp") == "enrol":
            self.enrol_totp()
            ident = self.session()
        return ident


def link_rows(status):
    """[{row}] of every link session of an admin `GET /api/v1/admin/status` body (`link_edges: [{edge, age_ms, sessions: [...]}]`)."""
    out = []
    for edge in (status or {}).get("link_edges") or []:
        for row in edge.get("sessions") or []:
            out.append(row)
    return out


def link_row(status, name):
    """The row called `name` (`account:<account>` on the central node, `uplink:<name>` on the hotspot), or None."""
    for row in link_rows(status):
        if row.get("name") == name:
            return row
    return None


def row_summary(row):
    """One line about a row for the notes: state, last_error, rtt, loss, peer."""
    if row is None:
        return "no row"
    return "state=%s last_error=%s rtt_ms=%s loss_ppm=%s peer=%s:%s" % (
        row.get("state"), row.get("last_error"), row.get("rtt_ms"), row.get("loss_ppm"), row.get("ip"), row.get("port"))


# ---------------------------------------------------------------- the WAN router

class Wan:
    """The tester's client of the WAN router's control server (dvtest/nat.py protocol, `<net>.98:9100`). The hotspot is the NAT'd host: its UDP to
    the central node's link port crosses the router, so `blackhole` is a blackout of the WAN in both directions, `nat_mode symmetric` plus the flush
    it does is a NAT rebinding to a NEW SOURCE PORT, and `readdress` moves the hotspot's UDP to another outside ADDRESS."""

    def __init__(self, ip, port=9100):
        self.addr = (ip, port)

    def ctl(self, op, **kw):
        ans = nat.call(self.addr, dict(op=op, **kw))
        if not ans.get("ok"):
            raise nat.NatError("wan router: %s" % ans.get("error"))
        return ans

    def blackhole(self, on):
        return self.ctl("blackhole", state="on" if on else "off")

    def mode(self, mode):
        return self.ctl("nat_mode", mode=mode)

    def readdress(self, on):
        return self.ctl("readdress", state="on" if on else "off")

    def mappings(self):
        """[{'browser_port', 'seen_ip', 'seen_port'}]: the hotspot's UDP flows to the central node's link port as the central node sees them."""
        return self.ctl("mappings")["mappings"]

    def firsts(self):
        return self.ctl("firsts")["firsts"]

    def status(self):
        return self.ctl("status")

    def reset(self):
        return self.ctl("reset")


# ---------------------------------------------------------------- what a P25 listener heard

def voice_arrivals(client):
    """[(monotonic arrival time, imbe bytes)] of the voice records (no terminators) in a P25Client's inbox, in arrival order."""
    out = []
    for data, t in zip(client.client.inbox, client.client.inbox_times):
        rec = p25.parse(data)
        if rec is not None and not rec["terminator"]:
            out.append((t, bytes(rec["imbe"])))
    return out


def heard_share(arrivals, sent, since=0.0):
    """Share (0..1) of the distinct frames in `sent` that arrived at or after `since` (monotonic seconds)."""
    want = set(bytes(f) for f in sent)
    if not want:
        return 0.0
    got = set(imbe for t, imbe in arrivals if t >= since)
    return len(want & got) / float(len(want))


def first_heard(arrivals, sent, since=0.0):
    """The arrival time of the first frame of `sent` that arrived at or after `since`, or None."""
    want = set(bytes(f) for f in sent)
    for t, imbe in arrivals:
        if t >= since and imbe in want:
            return t
    return None


def max_gap(times):
    """(largest gap between neighbouring arrival times, the time it starts) of a list of times; (0.0, None) for fewer than two."""
    ts = sorted(times)
    best, at = 0.0, None
    for a, b in zip(ts, ts[1:]):
        if b - a > best:
            best, at = b - a, a
    return best, at


def stream_gap(arrivals, sent, start, end):
    """The largest silence between the frames of ONE over that arrived (frames of `sent` only) within [start, end] (monotonic seconds): what a
    listener perceives as a media gap. The over's own first and last arrival bound the window, so the silence before the first frame (setup) and
    after the last one (end of the over) are not gaps. (0.0, None) when fewer than two of its frames arrived."""
    want = set(bytes(f) for f in sent)
    return max_gap([t for t, imbe in arrivals if start <= t <= end and imbe in want])


def missing_run(arrivals, sent, since=0.0, frame_s=0.020):
    """(seconds, index of the first missing frame) of the longest run of consecutive frames of `sent` (in sending order, one frame per `frame_s`)
    that never arrived at or after `since`, the tail of an over that stopped arriving included: the silence a listener perceives inside ONE over
    (or from its first frame to its end). (0.0, None) when every frame arrived."""
    heard = set(imbe for t, imbe in arrivals if t >= since)
    best, best_at, run, run_at = 0, None, 0, None
    for i, frame in enumerate(sent):
        if bytes(frame) in heard:
            run = 0
            continue
        if run == 0:
            run_at = i
        run += 1
        if run > best:
            best, best_at = run, run_at
    return best * frame_s, best_at


class KeyBox:
    """A key held in memory whose repr and str never show it (pytest prints the namespace of a failing test, and a case must never print a key)."""

    def __init__(self, key_hex):
        self._key = key_hex

    @property
    def value(self):
        return self._key

    def __repr__(self):
        return "<key hidden>"

    __str__ = __repr__


def floor_probe_ok(share, floor=0.8):
    """A floor probe over was granted when the listener on the same node heard at least `floor` of it (the over of a busy floor is refused whole)."""
    return share >= floor


def unmapped_id(base, n):
    """A radio id for probe number n that is in no directory (the nodes' P25 ports serve it under the placeholder callsign P31672 and key the guard
    on the source id), so a train of short probe overs never earns a strike."""
    return base + (n % 900)
