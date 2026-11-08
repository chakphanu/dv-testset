# SPDX-License-Identifier: MIT
"""Run side of the fuzz harness (docs/fuzz-cases.md): what cases/test_fuzz_netns.py
does inside its network namespace and dvtest/tools/fuzz_replay.py does against a
host. dvtest/fuzz.py makes the packets; this module sends them, keeps the last
50 for a replay file, reads /proc, and holds the valid clients that must stay
served while the fuzz runs.

Pure helpers (no sockets, tested offline in cases/unit/test_fuzz.py):
  parse_stat, parse_vmrss_kib, parse_udp_ports, snapshot, compare_processes,
  Ring, replay_document, read_replay, source_for
Socket helpers: Source, make_client, drive, heard, link_probe, Fuzzer.
"""
import collections
import json
import os
import socket
import time

from . import fuzz
from .audio import signals
from .net import UdpClient, pace
from .proto import dcs, dextra, dmr, dmrplus, dplus, g3, m17, nxdn, p25, xlx, ysf

REPLAY_FORMAT = "dv-fuzz-replay-1"
LAST = 50                       # packets kept for a replay file
NXDN_TG = 31672
P25_TG = 31672

# the four roles, each with its own identity (the fuzz identity is never the valid one)
IDENT = {"talker": dict(call="N0CALL", dmrid=1023007, nxdn=1001),
         "listener": dict(call="N0TEST", dmrid=1023008, nxdn=1002),
         "fuzz": dict(call="N0FUZZ", dmrid=1023009, nxdn=1003),
         "probe": dict(call="N0PROB", dmrid=1023010, nxdn=1004)}
FIXED_PORT = {"g3": g3.DV_PORT, "xlx": xlx.PORT, "imrs": 21110}   # the source port of the "linked" fuzz source
OPTIONAL = ("nxdn", "p25", "xlx", "imrs", "g3")                    # no served check when the warm-up is not served
CLIENT_KINDS = ("dextra", "dplus", "dcs", "dmr", "dmrplus", "m17", "ysf", "nxdn", "p25")


# ---------------------------------------------------------------- pure helpers

def parse_stat(text):
    """(pid, comm, ppid, starttime) of the text of /proc/<pid>/stat (the comm may hold spaces)."""
    left, right = text.index("("), text.rindex(")")
    rest = text[right + 2:].split()
    return int(text[:left]), text[left + 1:right], int(rest[1]), int(rest[19])


def parse_vmrss_kib(text):
    """VmRSS in KiB of the text of /proc/<pid>/status, None when the line is missing (kernel threads)."""
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


def parse_udp_ports(text):
    """Set of the local UDP ports that are bound (state 07, not connected) in the text of /proc/net/udp."""
    ports = set()
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 3 and cols[3] == "07":
            ports.add(int(cols[1].rsplit(":", 1)[1], 16))
    return ports


def snapshot(root_pid, proc="/proc"):
    """[dict(pid, ppid, comm, start, rss_kib)] of root_pid and all its descendants, sorted by pid."""
    stats, rss = {}, {}
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        try:
            with open("%s/%s/stat" % (proc, entry)) as handle:
                pid, comm, ppid, start = parse_stat(handle.read())
            stats[pid] = (comm, ppid, start)
        except (OSError, ValueError, IndexError):
            continue
    keep, changed = {root_pid}, True
    while changed:
        changed = False
        for pid, (_comm, ppid, _start) in stats.items():
            if pid not in keep and ppid in keep:
                keep.add(pid)
                changed = True
    out = []
    for pid in sorted(keep & set(stats)):
        try:
            with open("%s/%d/status" % (proc, pid)) as handle:
                rss[pid] = parse_vmrss_kib(handle.read())
        except OSError:
            rss[pid] = None
        comm, ppid, start = stats[pid]
        out.append(dict(pid=pid, ppid=ppid, comm=comm, start=start, rss_kib=rss[pid]))
    return out


def compare_processes(before, after):
    """(gone, new): processes of `before` that no longer run with the same pid AND
    start time (a restart is a gone + a new), and processes that were not there."""
    have = {(p["pid"], p["start"]) for p in after}
    had = {(p["pid"], p["start"]) for p in before}
    gone = [p for p in before if (p["pid"], p["start"]) not in have]
    new = [p for p in after if (p["pid"], p["start"]) not in had]
    return gone, new


def edge_process(procs, names):
    """First process whose comm is one of names (in the order of names), else None."""
    for name in names:
        for p in procs:
            if p["comm"] == name:
                return p
    return None


def source_for(index):
    """Which source sends the case `index`: the session of the fuzz client ("linked", two
    cases in three) or a socket that never linked ("stranger")."""
    return "linked" if index % 3 else "stranger"


class Ring:
    """The last `size` datagrams sent, as replay entries."""

    def __init__(self, size=LAST):
        self.items = collections.deque(maxlen=size)

    def add(self, protocol, seed, index, sub, port, src, data):
        self.items.append(dict(protocol=protocol, seed=str(seed), index=index, sub=sub, port=port, src=src,
                               hex=bytes(data).hex()))

    def entries(self):
        return list(self.items)


def replay_document(scenario, seed, entries, reason="", host=None):
    """The replay file: every entry resent in order reproduces the last packets of a run."""
    return {"format": REPLAY_FORMAT, "scenario": scenario, "seed": str(seed), "reason": reason, "host": host,
            "entries": list(entries)}


def read_replay(text):
    """Entries of a replay file (a JSON text), validated; ValueError on anything else."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("format") != REPLAY_FORMAT:
        raise ValueError("not a %s file" % REPLAY_FORMAT)
    out = []
    for e in doc.get("entries", []):
        try:
            out.append(dict(e, hex=bytes.fromhex(e["hex"]).hex(), port=int(e["port"]), index=int(e["index"]),
                            sub=int(e["sub"]), protocol=str(e["protocol"]), seed=str(e["seed"]),
                            src=str(e.get("src", "stranger"))))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("bad replay entry %r: %s" % (e, exc)) from exc
    return doc, out


# ---------------------------------------------------------------- sockets

class Source:
    """A UDP socket bound to ip:port that sends fuzz datagrams and throws the replies away."""

    def __init__(self, ip, port=0, sock=None):
        """sock: an existing socket to send from (the USRP peer's), left as it is but made non-blocking."""
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind((ip, port))
        self.sock = sock
        self.sock.setblocking(False)
        self.errors = 0

    def send(self, host, port, data):
        try:
            self.sock.sendto(data, (host, port))
            return True
        except OSError:
            self.errors += 1                    # refused by an earlier ICMP, buffer full
            return False

    def drain(self):
        while True:
            try:
                self.sock.recvfrom(2048)
            except OSError:
                return

    def close(self):
        self.sock.close()


class ClientSource:
    """A linked protocol client used as a fuzz source: the client's own socket, so a mutated
    packet arrives from a live session. Its replies are drained (keepalive responder runs)."""

    def __init__(self, client):
        self.client = client
        self.errors = 0

    def send(self, host, port, data):
        try:
            self.client.client.sock.sendto(data, (host, port))
            return True
        except OSError:
            self.errors += 1
            return False

    def drain(self):
        self.client.client.drain()

    def close(self):
        self.client.close()


def make_client(protocol, bind_ip, role, module="B", nxdn_tg=NXDN_TG, p25_tg=P25_TG):
    """The proto module's own client for a protocol, as `role` (IDENT)."""
    who = IDENT[role]
    if protocol == "dextra":
        return dextra.DstarClient(bind_ip, callsign=who["call"], module=module)
    if protocol == "dplus":
        return dplus.DplusClient(bind_ip, callsign=who["call"], module=module)
    if protocol == "dcs":
        return dcs.DcsClient(bind_ip, callsign=who["call"], module=module)
    if protocol == "dmr":
        return dmr.DmrClient(bind_ip, dmrid=who["dmrid"], module=module)
    if protocol == "dmrplus":
        return dmrplus.DmrPlusClient(bind_ip, dmrid=who["dmrid"], module=module)
    if protocol == "m17":
        return m17.M17Client(bind_ip, callsign=who["call"], module=module)
    if protocol == "ysf":
        return ysf.YsfClient(bind_ip, callsign=who["call"], module=module)
    if protocol == "nxdn":
        return nxdn.NxdnClient(bind_ip, nxdn_tg, nxdn_id=who["nxdn"], callsign=who["call"])
    if protocol == "p25":
        return p25.P25Client(bind_ip, callsign=who["call"], src_id=who["dmrid"], tg=p25_tg)
    raise KeyError("no client for %r" % protocol)


def link(protocol, client, host):
    """Link a client from make_client; True when the server acknowledged."""
    got = client.link(host)
    if protocol == "dmrplus":
        return dmrplus.ACK in got
    return bool(got)


def frames_for(protocol, count, seed="fuzz-valid"):
    """count pseudo-random frames of the width the protocol's client sends."""
    if protocol == "m17":
        return signals.prng_frames("m17", count, seed=seed)
    if protocol == "nxdn":
        raw = signals.prng_bytes(seed + "/nxdn", nxdn.FRAME_BYTES * count)
        return [raw[i * nxdn.FRAME_BYTES:(i + 1) * nxdn.FRAME_BYTES] for i in range(count)]
    if protocol == "p25":
        return signals.prng_frames("imbe", count, seed=seed)
    return signals.prng_frames("dstar", count, seed=seed)


def drive(protocol, client, frames, stream_id):
    """One short over from `client`, settle and tail kept small (the harness budget)."""
    if protocol in ("dextra", "dplus", "dcs"):
        client.talk(frames, stream_id, settle=0.2, tail=0.3)
    elif protocol == "dmr":
        client.talk(frames, stream_id=stream_id, settle=0.4, tail=0.3)
    elif protocol == "m17":
        client.talk(frames, stream_id, settle=0.2, tail=0.3)
    elif protocol == "ysf":
        client.talk(frames, settle=0.3, tail=0.3)
    elif protocol == "dmrplus":
        client.talk(frames, tail=0.3)
    elif protocol == "nxdn":
        client.talk(frames, settle=0.3, tail=0.3)
    elif protocol == "p25":
        client.talk(frames, tail=0.3)
    else:
        raise KeyError(protocol)


def heard(protocol, listener, settle=0.5):
    """Number of voice frames the listener received. Every client has collect() except the DMR+ dongle
    (DmrPlusClient): it pumps (keepalives on schedule) and its voice() packets are read with frames_of."""
    if protocol == "dmrplus":
        listener.pump(settle)
        return len(dmrplus.frames_of(listener.voice()))
    return len(listener.collect(settle))


# ---------------------------------------------------------------- link probes (a fresh client, link only)

def _wait(client, pred, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        client.drain()
        for data in client.inbox:
            if pred(data):
                return data
        time.sleep(0.02)
    return None


def link_probe(protocol, host, bind_ip, wait=3.0):
    """True when a fresh client of the "probe" role gets its link acknowledged, False when not, None
    when the protocol has no link to probe (imrs, usrp). The state it creates is undone."""
    who = IDENT["probe"]
    if protocol in ("dplus", "dcs", "m17", "dmrplus", "dextra"):
        client = make_client(protocol, bind_ip, "probe")
        try:
            if protocol == "dplus":
                ok = bool(client.link(host, timeout=wait, join=False))
            elif protocol == "dmrplus":
                ok = dmrplus.ACK in client.link(host, timeout=wait)
            else:
                ok = bool(client.link(host, timeout=wait))
            if ok and protocol in ("dextra", "dplus", "dcs", "m17"):
                client.unlink()
            elif ok:
                client.disconnect()
            return ok
        finally:
            client.close()
    if protocol == "imrs" or protocol == "usrp":
        return None
    udp = UdpClient(bind_ip)
    dest = (host, fuzz.PORTS[protocol][0])
    try:
        if protocol == "dmr":
            udp.send(dest, dmr.rptl(who["dmrid"]))
            ok = _wait(udp, lambda d: d.startswith(b"RPTACK"), wait) is not None
            udp.send(dest, dmr.rptcl(who["dmrid"], 13))
            return ok
        if protocol == "ysf":
            udp.send(dest, ysf.poll_packet(who["call"]))
            return _wait(udp, ysf.is_poll_ack, wait) is not None
        if protocol == "nxdn":
            poll = nxdn.poll_packet(who["call"], NXDN_TG)
            udp.send(dest, poll)
            return _wait(udp, lambda d: d == poll, wait) is not None
        if protocol == "p25":
            poll = p25.poll_packet(who["call"])
            udp.send(dest, poll)
            return _wait(udp, lambda d: d == poll, wait) is not None
        if protocol == "xlx":
            udp.send(dest, xlx.connect("XLX995", "DE"))
            got = _wait(udp, lambda d: xlx.parse_link(d) is not None or xlx.parse_short(d) is not None, wait)
            udp.send(dest, xlx.disconnect("XLX995"))
            return got is not None
        if protocol == "g3":
            dest = (host, g3.PRESENCE_PORT)
            udp.send(dest, g3.presence_packet("N0PROB T"))
            return _wait(udp, lambda d: g3.parse_presence_reply(d) is not None, wait) is not None
        raise KeyError(protocol)
    finally:
        udp.close()


# ---------------------------------------------------------------- the fuzzer

class Fuzzer:
    """Sends the cases of one port group in order: at most N datagrams, 1 ms apart by default,
    from two sources (source_for), keeping the last 50 in a Ring. The caller runs the valid
    clients between batches (batch() returns after `count` datagrams)."""

    def __init__(self, scenario, group, host, seed, linked, stranger, gap=0.001, ring=None, module="B", start=0):
        """module: the module letter the seed packets name (fuzz.protocol); start: first case index of the
        run (a second phase continues after the first, so no case is sent twice)."""
        self.scenario, self.group, self.host, self.seed = scenario, tuple(group), host, str(seed)
        self.module = module
        self.sources = {"linked": linked, "stranger": stranger}
        self.gap = gap
        self.ring = ring or Ring()
        self.sent = 0
        self.case_index = start               # next case of the run (across the group)
        self.errors = 0
        self._start = time.monotonic()

    def next_case(self):
        """The protocol, its own case index and the Case for the run's next case."""
        protocol = self.group[self.case_index % len(self.group)]
        own = self.case_index // len(self.group)
        self.case_index += 1
        return protocol, own, fuzz.generate(protocol, self.seed, own, self.module)

    def batch(self, count):
        """Send at least `count` datagrams (whole cases); returns how many were sent."""
        done = 0
        while done < count:
            protocol, own, case = self.next_case()
            src = source_for(own)
            for sub, data in enumerate(case.packets):
                pace(self._start, self.sent, self.gap)
                ok = self.sources[src].send(self.host, case.port, data)
                self.errors += 0 if ok else 1
                self.ring.add(protocol, self.seed, own, sub, case.port, src, data)
                self.sent += 1
                done += 1
                if self.sent % 50 == 0:
                    for s in self.sources.values():
                        s.drain()
        return done


class Replayer:
    """The same interface as Fuzzer (batch, sent, ring, errors) over recorded entries: they are
    resent in order, each from the source it was recorded with ("linked" / "stranger")."""

    def __init__(self, entries, host, linked, stranger, gap=0.001, ring=None):
        self.entries, self.host = list(entries), host
        self.sources = {"linked": linked, "stranger": stranger}
        self.gap = gap
        self.ring = ring or Ring()
        self.sent = 0
        self.errors = 0
        self._start = time.monotonic()

    @property
    def total(self):
        return len(self.entries)

    def batch(self, count):
        done = 0
        while done < count and self.sent < len(self.entries):
            e = self.entries[self.sent]
            pace(self._start, self.sent, self.gap)
            data = bytes.fromhex(e["hex"])
            ok = self.sources.get(e["src"], self.sources["stranger"]).send(self.host, e["port"], data)
            self.errors += 0 if ok else 1
            self.ring.add(e["protocol"], e["seed"], e["index"], e["sub"], e["port"], e["src"], data)
            self.sent += 1
            done += 1
            if self.sent % 50 == 0:
                for src in self.sources.values():
                    src.drain()
        return done


# ---------------------------------------------------------------- hub counters and settings

METRIC_PREFIXES = ("dv_edge_drops_total", "dv_pkt_reject_total", "dv_edge_queue_drops_total", "dv_hub_dropped_total")


def edge_label(comm):
    """The proc label of dv_edge_drops_total for a child process name: "dv-edge-dextra" is proc="dextra"."""
    return comm[len("dv-edge-"):] if comm.startswith("dv-edge-") else comm


def sample_map(text):
    """{(name, ((label, value), ...)): value} of the samples of a hub scrape (the text of /metrics)."""
    from . import metrics
    return {(s.name, tuple(sorted(s.labels.items()))): s.value for s in metrics.parse(text)}


def metric_changes(before, after):
    """[(name, labels dict, before, after)] of every series whose value differs between two scrape
    texts (a series that is new counts from 0), sorted by name and labels."""
    old, new = sample_map(before), sample_map(after)
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key, 0.0), new.get(key, 0.0)
        if a != b and not (a != a and b != b):
            out.append((key[0], dict(key[1]), a, b))
    return out


def family_delta(changes, name, **labels):
    """Sum of (after - before) over the changed series of one family whose labels contain `labels`."""
    return sum(b - a for n, lab, a, b in changes if n == name and all(lab.get(k) == v for k, v in labels.items()))


def error_count(lines, edges=()):
    """dv_edge_drops_total (of the processes named in edges, as child names or proc labels, else of all
    when no sample carries a proc label) plus dv_pkt_reject_total, from the text lines of a hub scrape."""
    from . import metrics
    labels = {edge_label(e) for e in edges}
    samples = metrics.parse("\n".join(lines))
    drops = [s for s in samples if s.name == "dv_edge_drops_total"]
    if labels and any("proc" in s.labels for s in drops):
        drops = [s for s in drops if s.labels.get("proc") in labels]
    return sum(s.value for s in drops) + metrics.total(samples, "dv_pkt_reject_total")


# ---------------------------------------------------------------- floor rule and phases

STREAM_TIMEOUT_S = 1.6          # silence timeout of a stream (dvtest/specs/dvrouter.py B3: "timeout_s": 1.6)
QUIET_MARGIN_S = 1.0            # added before a valid round on a module the fuzz client also used
QUIET_S = STREAM_TIMEOUT_S + QUIET_MARGIN_S


def plan_phases(n, other_module, every=200):
    """The phases of a run of n datagrams: [{name, module, quiet, count}]. With a module to move to
    (fuzz.OTHER_MODULE) the first half fuzzes THAT module (the valid clients stay on B: a floor hold of the
    fuzz client cannot explain a miss, no wait), the second half fuzzes B and waits QUIET_S of quiet before
    each valid round. Without one the whole run is "same" with the wait."""
    if other_module is None or n < 2:
        return [dict(name="same", module="B", quiet=True, count=n)]
    first = (n // 2 // every) * every or n // 2
    return [dict(name="other", module=other_module, quiet=False, count=first),
            dict(name="same", module="B", quiet=True, count=n - first)]


def settings_from_env(environ=None):
    """(n, seed, gap_s, replay_path) from DVT_FUZZ_N (default 2000), DVT_FUZZ_SEED (default "1"),
    DVT_FUZZ_GAP_MS (default 1) and DVT_FUZZ_REPLAY (default none); ValueError on a bad number."""
    env = os.environ if environ is None else environ

    def number(name, default, minimum, cast):
        text = env.get(name, "").strip()
        if not text:
            return default
        try:
            value = cast(text)
        except ValueError:
            value = None
        if value is None or value < minimum:
            raise ValueError("%s must be a number >= %s, got %r" % (name, minimum, text))
        return value

    n = number("DVT_FUZZ_N", 2000, 1, int)
    gap = number("DVT_FUZZ_GAP_MS", 1.0, 0.0, float) / 1000.0
    seed = env.get("DVT_FUZZ_SEED", "").strip() or "1"
    return n, seed, gap, env.get("DVT_FUZZ_REPLAY", "").strip() or None


def timeout_s(n, every=200):
    """Seconds one scenario may take: start-up, N datagrams, one valid round (about 3 s) per `every`."""
    return int(120 + n * 0.005 + (n // every + 2) * 3.5)
