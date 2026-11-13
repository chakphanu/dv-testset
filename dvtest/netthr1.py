# SPDX-License-Identifier: MIT
"""Pure helpers of the NETTHR1a / NETTHR1b cases (docs/netthr1-cases.md; DV-Router fce97cd3 and c8f9b4ed). No docker, no sockets.

  NETTHR1a   node.fabric_max_sessions (4..=128, derived default), the session gauge {open, cap, peak}
             (hub metrics `dv_fabric_sessions{kind=open|cap|peak}`, launcher status `fabric.sessions`)
  NETTHR1b   web / admin max_file_streams (1..=64) and login_slots (1..=16) from configuration, one DNS resolver
             pool per edge process (two workers, 5 s cancel), per-process thread ceilings

The numbers below come from DV-Router's documents (deployment.md "Process schedulers", crates/README.md "Process thread
budgets", config.md "Fabric session admission"), not from the product code; each constant names its source.

  on(environ)                              the switch DVT_NETTHR1=on
  process_ceiling(comm, ...)               threads one process may have (None = no documented ceiling)
  profile_of(counts) / expected_total(...) the named process sets (router K6b fixture 251, hotspot 95, hotspot + link 101)
  derived_cap(doc)                         the default of node.fabric_max_sessions from a parsed config
  hostname_edges(doc)                      edge kinds with a host-name row (each gets the two resolver workers)
  sessions_from_samples / find_sessions    the gauge from hub metrics or from a status body
  analyse_sweeps(sweeps, ceilings)         violations, restarts and peaks of repeated proc-stat sweeps
  dns schedule helpers                     see dvtest/dnshole.py
"""
import re
from collections import Counter

SWITCH = "DVT_NETTHR1"

# ---------------------------------------------------------------- the numbers of the documents

FABRIC_MIN, FABRIC_MAX = 4, 128           # config.md "Fabric session admission": 4..=128
CODEC = "dv-codec-ambed"                  # the worker the rig's transcoder address spawns: in NO documented budget (tracked as NETTHR1c)
SESSION_HEADROOM = 8                      # the margin of the derived default
EDGE_KINDS, SERVICE_KINDS = 19, 9         # the registry the derived default counts (config.md)
STREAMS_RANGE = (1, 64)                   # config.md: web.max_file_streams / admin.max_file_streams
SLOTS_RANGE = (1, 16)                     # config.md: web.login_slots / admin.login_slots
ROUTER_HTTP = (16, 4)                     # (streams, login slots) of the router profile
HOTSPOT_HTTP = (2, 2)                     # the hotspot profile
BASE = 3                                  # main thread + shared async worker + TX worker
IO_BLOCKING = 1                           # Io class: edges, rooms, codec / data workers, ordinary services
RECORDER_BLOCKING = BASEMAP_BLOCKING = LAUNCHER_BLOCKING = 2
HTTP_WRITERS = 3                          # audit, accounts/users, journal/config: dedicated file writers
LAUNCHER_EXTRA = 1                        # config-reader
DNS_WORKERS = 2                           # edge-dns-0, edge-dns-1 of an edge with host-name rows
DNS_QUEUE, DNS_DEADLINE_S = 32, 5.0       # FIFO queue and the cancel of one running lookup
EDGE_PREFIX = "dv-edge-"
# edges that start the two resolver workers when they have host-name rows (deployment.md "Extra source")
DNS_EDGES = ("dv-edge-xlx", "dv-edge-nxdn", "dv-edge-p25", "dv-edge-m17", "dv-edge-link")
IO_PROCESSES = ("dv-room", "dv-data-worker", "dv-codec-ambed", "dv-svc-status", "dv-svc-callhome", "dv-svc-echo", "dv-edge-radio")
EXTRA_THREADS = {"dv-svc-iddb": 2, "dv-svc-idsync": 1}      # seed-dmr + seed-nxdn; the served-ID-sync loop


def on(environ=None):
    import os
    env = os.environ if environ is None else environ
    return str(env.get(SWITCH, "")).strip().lower() == "on"


def skip_reason():
    return "NETTHR1 cases need an image with DV-Router fce97cd3 + c8f9b4ed (wip-eeb44b4): set %s=on" % SWITCH


# ---------------------------------------------------------------- thread ceilings

def http_ceiling(login_slots, streams):
    """admin / web: three base threads, login_slots + 1 blockers, three writers, up to `streams` file-body threads
    (deployment.md: 7 + login_slots + max_file_streams; 27 for 4 / 16, 11 for 2 / 2)."""
    return BASE + login_slots + 1 + HTTP_WRITERS + streams


def launcher_fixed():
    """dv-router outside the Net scheduler: 3 + 2 blockers + config-reader = 6."""
    return BASE + LAUNCHER_BLOCKING + LAUNCHER_EXTRA


def launcher_ceiling(cap):
    """The launcher with the Net scheduler: 6 + (cap + 2)."""
    return launcher_fixed() + cap + 2


def process_ceiling(comm, web=ROUTER_HTTP, admin=ROUTER_HTTP, cap=63, dns_edges=()):
    """Threads the process called `comm` may have at one instant, or None when the documents give no ceiling.
    web / admin are (streams, login slots); cap the RUNNING fabric cap; dns_edges the edges with host-name rows."""
    if comm == "dv-router":
        return launcher_ceiling(cap)
    if comm == "dv-svc-web":
        return http_ceiling(web[1], web[0])
    if comm == "dv-svc-admin":
        return http_ceiling(admin[1], admin[0])
    if comm in ("dv-svc-recorder", "dv-svc-basemap"):
        return BASE + RECORDER_BLOCKING
    if comm == "dv-edge-allstar":
        return None                       # one DNS worker + one registration worker per public node: depends on the rows
    if comm in IO_PROCESSES or comm.startswith(EDGE_PREFIX) or comm in EXTRA_THREADS:
        n = BASE + IO_BLOCKING + EXTRA_THREADS.get(comm, 0)
        if comm in DNS_EDGES and comm in tuple(dns_edges):
            n += DNS_WORKERS
        return n
    return None


# named process sets: (counts of comm, headline total, http (streams, slots), derived cap)
ROUTER_K6B = Counter({"dv-room": 26, "dv-edge-usrp": 1, "dv-data-worker": 1, "dv-svc-status": 1, "dv-svc-recorder": 1,
                      "dv-svc-basemap": 1, "dv-svc-admin": 1, "dv-svc-web": 1, "dv-router": 1})
HOTSPOT = Counter({"dv-room": 1, "dv-edge-radio": 1, "dv-data-worker": 1, "dv-svc-iddb": 1, "dv-svc-status": 1,
                   "dv-svc-basemap": 1, "dv-svc-admin": 1, "dv-svc-web": 1, "dv-router": 1})
HOTSPOT_LINK = Counter(HOTSPOT) + Counter({"dv-edge-link": 1})
PROFILES = {
    "router-k6b": (ROUTER_K6B, 251, ROUTER_HTTP, 63),
    "hotspot": (HOTSPOT, 95, HOTSPOT_HTTP, 38),
    "hotspot-link": (HOTSPOT_LINK, 101, HOTSPOT_HTTP, 38),
}


def profile_of(counts):
    """The name of the documented process set that `counts` ({comm: copies}) equals, or None."""
    want = Counter({k: v for k, v in counts.items() if v})
    for name, (members, _total, _http, _cap) in PROFILES.items():
        if want == members:
            return name
    return None


def expected_total(counts, web=ROUTER_HTTP, admin=ROUTER_HTTP, cap=63, dns_edges=()):
    """(sum of the ceilings, [comm without a ceiling]) over a process set {comm: copies}."""
    total, unknown = 0, []
    for comm, copies in sorted(counts.items()):
        ceiling = process_ceiling(comm, web, admin, cap, dns_edges)
        if ceiling is None:
            unknown.append(comm)
        else:
            total += ceiling * copies
    return total, unknown


def headline(profile):
    """The total the DV-Router documents give for a named profile (251 / 95 / 101)."""
    return PROFILES[profile][1]


# ---------------------------------------------------------------- the derived cap, from a parsed config

def rooms_count(spec):
    """Number of rooms of node.rooms ("A-Z", "A-D", "B", "ABC", "A-C,X")."""
    letters = set()
    text = str(spec or "").replace(" ", "")
    i = 0
    while i < len(text):
        c = text[i]
        if c == ",":
            i += 1
        elif i + 2 < len(text) and text[i + 1] == "-" and text[i].isalpha() and text[i + 2].isalpha():
            letters.update(chr(x) for x in range(ord(text[i]), ord(text[i + 2]) + 1))
            i += 3
        else:
            if c.isalpha():
                letters.add(c)
            i += 1
    return len(letters)


def derived_cap(doc, argv_workers=0):
    """clamp(rooms + 19 edge kinds + 9 service kinds + data worker rows + transcoder worker rows + 8, 4, 128) of a parsed
    config (a dict from tomllib). The data worker term is the number of rows, and one (the default `pkt`) when there is none
    (config.md; the fabric_session_tests of DV-Router count 41 for A-D without rows). The explicit key wins. `argv_workers`: the
    transcoder address (third positional word of the rig launch) becomes one more worker row `ambed` that the file does not hold, so the running cap
    is one higher (DV-Router answer 8: hotspot 39, router 64); give 1 when the file has no [[transcoder.worker]] row and a dv-codec-ambed child runs."""
    node = doc.get("node", {}) or {}
    if isinstance(node.get("fabric_max_sessions"), int):
        return node["fabric_max_sessions"]
    data_rows = len((doc.get("data", {}) or {}).get("worker", []) or []) or 1
    tc_rows = len((doc.get("transcoder", {}) or {}).get("worker", []) or [])
    raw = rooms_count(node.get("rooms", "")) + EDGE_KINDS + SERVICE_KINDS + data_rows + tc_rows + argv_workers + SESSION_HEADROOM
    return max(FABRIC_MIN, min(FABRIC_MAX, raw))


_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def hostname_rows(doc):
    """{edge kind: [host names]} for the rows under edges.<kind>.<table> (peer, peers, uplink ...) whose `host` is not an IPv4
    literal (a trailing :port is cut). These edges start the two shared resolver workers."""
    out = {}
    for kind, section in (doc.get("edges", {}) or {}).items():
        if not isinstance(section, dict):
            continue
        for value in section.values():
            if not isinstance(value, list):
                continue
            for row in value:
                host = row.get("host") if isinstance(row, dict) else None
                if not isinstance(host, str) or not host:
                    continue
                bare = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) and host.count(":") == 1 else host
                if not _IPV4.match(bare):
                    out.setdefault(kind, []).append(bare)
    return out


ROW_EDGES = {"m17": "dv-edge-m17", "link": "dv-edge-link"}    # these start the two resolver workers for ANY configured peer / uplink row, numeric ones too


def hostname_edges(doc):
    """The process names that run the two resolver workers: an edge with a host-name row (xlx, nxdn, p25 ...), and m17 / link with any peer or
    uplink row (deployment.md "Extra source": "M17/link with configured peer/uplink rows"; the first rig run saw dv-edge-m17 with 6 threads on literal rows)."""
    names = {"dv-edge-" + kind for kind in hostname_rows(doc)}
    for kind, proc in ROW_EDGES.items():
        section = (doc.get("edges", {}) or {}).get(kind, {})
        if isinstance(section, dict) and any(isinstance(v, list) and v and isinstance(v[0], dict) for v in section.values()):
            names.add(proc)
    return tuple(sorted(names))


def http_settings(doc, hotspot=False):
    """((web streams, web slots), (admin streams, admin slots)) of a parsed config: the explicit keys, else the profile default
    (config.md: router 16 / 4, hotspot 2 / 2)."""
    profile_hot = hotspot or (doc.get("node", {}) or {}).get("profile") == "hotspot"
    d_streams, d_slots = HOTSPOT_HTTP if profile_hot else ROUTER_HTTP
    web, admin = doc.get("web", {}) or {}, doc.get("admin", {}) or {}
    return ((web.get("max_file_streams", d_streams), web.get("login_slots", d_slots)),
            (admin.get("max_file_streams", d_streams), admin.get("login_slots", d_slots)))


# ---------------------------------------------------------------- the gauge

GAUGE = "dv_fabric_sessions"
GAUGE_KINDS = ("open", "cap", "peak")


def sessions_from_samples(samples):
    """{kind: value} of the dv_fabric_sessions samples of a metrics.parse() list; kinds the document does not name are kept
    under their own label so a test can see them. Samples of another proc than dv-router are ignored when a proc label exists."""
    out = {}
    for s in samples:
        if s.name != GAUGE:
            continue
        proc = s.labels.get("proc")
        if proc not in (None, "dv-router"):
            continue
        v = s.value
        out[s.labels.get("kind", "")] = int(v) if isinstance(v, float) and v.is_integer() else v      # the hub text has 64.0
    return out


def gauge_problems(kinds):
    """Problems (list of text) of a {kind: value} gauge: exactly open / cap / peak, whole numbers, open <= peak <= cap."""
    problems = []
    if set(kinds) != set(GAUGE_KINDS):
        problems.append("kinds %s, want exactly %s" % (sorted(kinds), list(GAUGE_KINDS)))
        return problems
    for k, v in kinds.items():
        if v != int(v) or v < 0:
            problems.append("%s=%r is not a whole number" % (k, v))
    if not problems:
        if kinds["open"] > kinds["peak"]:
            problems.append("open %d is above peak %d" % (kinds["open"], kinds["peak"]))
        if kinds["peak"] > kinds["cap"]:
            problems.append("peak %d is above cap %d" % (kinds["peak"], kinds["cap"]))
    return problems


def find_sessions(obj):
    """The first dict {open, cap, peak} stored under a key `sessions` anywhere in a JSON body (the admin status body, the status
    feed, the hub state: the nesting differs, summary.fabric.sessions / data.fabric.sessions / launcher.fabric.sessions); None."""
    if isinstance(obj, dict):
        got = obj.get("sessions")
        if isinstance(got, dict) and {"open", "cap", "peak"} <= set(got):
            return {k: got[k] for k in GAUGE_KINDS}
        for value in obj.values():
            found = find_sessions(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_sessions(value)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------- reload text edits (the rendered config of the run)

def data_worker_rows(n, prefix="nt", first=None):
    """TOML text of n extra [[data.worker]] rows (ids nt01 ...). Explicit rows REPLACE the implicit default row `pkt` (the first rig run: 1 -> 2 workers for two
    rows), so `first="pkt"` puts the default back as an explicit row when the file has none."""
    head = "[[data.worker]]\nid = '%s'\n" % first if first else ""
    return head + "".join("[[data.worker]]\nid = '%s%02d'\n" % (prefix, i + 1) for i in range(n))


def add_rows(text, rows):
    """`text` with the TOML rows appended (arrays of tables may continue at the end of the file)."""
    return text.rstrip("\n") + "\n" + rows


def m17_rows(rows):
    """TOML text for [[edges.m17.peers]] rows: rows = [(callsign, host, port)], module M."""
    return "".join("[[edges.m17.peers]]\ncallsign = '%s'\nhost = '%s'\nport = %d\nmodules = 'M'\n" % r for r in rows)


# ---------------------------------------------------------------- repeated proc-stat sweeps

def analyse_sweeps(sweeps, ceilings, total_limit=None):
    """sweeps: list of lists of dicts {comm, pid, start, threads}; ceilings {comm: int or None}.
    Returns {violations, restarts, peak_total, peak_by_comm, unknown}: a violation is "comm pid=N has T threads, ceiling C"; a
    restart is a (comm, pid) of the previous sweep that is gone, one that is new, or one whose start time changed; peak_total is
    the largest sum of one sweep (the reads of a sweep are not simultaneous to the millisecond: a lower bound of the true peak)."""
    violations, restarts, unknown = [], [], set()
    peak_by_comm, peak_total = {}, 0
    prev = None
    for n, sweep in enumerate(sweeps):
        total = 0
        cur = {}
        for row in sweep:
            total += row["threads"]
            comm = row["comm"]
            cur[(comm, row["pid"])] = row["start"]
            peak_by_comm[comm] = max(peak_by_comm.get(comm, 0), row["threads"])
            ceiling = ceilings.get(comm)
            if ceiling is None:
                unknown.add(comm)
            elif row["threads"] > ceiling:
                violations.append("sweep %d: %s pid=%d has %d threads, ceiling %d" % (n, comm, row["pid"], row["threads"], ceiling))
        if prev is not None:
            for key in sorted(set(prev) - set(cur)):
                restarts.append("sweep %d: %s pid=%d is gone" % (n, key[0], key[1]))
            for key in sorted(set(cur) - set(prev)):
                restarts.append("sweep %d: %s pid=%d is new" % (n, key[0], key[1]))
            for key in sorted(set(cur) & set(prev)):
                if cur[key] != prev[key]:
                    restarts.append("sweep %d: %s pid=%d changed start time" % (n, key[0], key[1]))
        prev = cur
        peak_total = max(peak_total, total)
        if total_limit is not None and total > total_limit:
            violations.append("sweep %d: %d threads in total, limit %d" % (n, total, total_limit))
    return {"violations": violations, "restarts": restarts, "peak_total": peak_total,
            "peak_by_comm": peak_by_comm, "unknown": sorted(unknown)}


def counts_of(sweep):
    """{comm: copies} of one sweep."""
    return dict(Counter(r["comm"] for r in sweep))


# ---------------------------------------------------------------- login timing

def hash_estimate_ms(login_ms, plain_ms):
    """One password hash as the difference between a login and a request that does no hash, in milliseconds (medians in)."""
    return max(0.0, login_ms - plain_ms)


def median(values):
    s = sorted(values)
    if not s:
        raise ValueError("no values")
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def concurrency_needed(hash_ms, wait_ms=500.0, slots=1):
    """Concurrent logins that make the last one wait longer than the 0.5 s admission wait, for `slots` password slots and the
    hash time: request k (1 based) is served in round ceil(k / slots) and waits (round - 1) hash times, so the first one that
    waits longer than `wait_ms` is k = slots * (floor(wait / hash) + 1) + 1."""
    if hash_ms <= 0:
        return 10 ** 6
    return slots * (int(wait_ms // hash_ms) + 1) + 1
