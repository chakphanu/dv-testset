# SPDX-License-Identifier: MIT
"""NODE1g (DV-Router, image dv-router:wip-42d5473): a HOTSPOT node linked to a CENTRAL node over an emulated WAN (docs/node1g-cases.md).

The pair (scripts/run.sh --profile twonode with DVT_NODE1G=on): node 1 = the SUT xlxd-sut (XLX999) is the CENTRAL node, the server of the node link
(`[[edges.link.account]]` n0call-hs1, room C, talk right); node 2 = dvr-peer is the HOTSPOT (`node.profile = "hotspot"`, room B, one uplink row)
on the internal network behind the WAN router (.98), which carries every datagram of the link: a netem sidecar shapes both of its interfaces, its
control server blackholes the WAN and rebinds the hotspot's NAT mapping (a new source port, a new source address). Both nodes serve P25 (UDP 41000,
TG 31672: hotspot room B, central room C = the same room through the link) so the tester talks on one node and listens on the other without any
transcoder; the tester is on the hotspot's LAN as well. Evidence: media heard (P25 listeners with arrival times), the hubs' link counters
(`metrics xlxd-sut` / `metrics dvr-peer`, refreshed about every 10 s), the admin API of both nodes (`GET /api/v1/admin/status`, `link_edges`: the rows
with state, rtt_ms, loss_ppm, last_error), the WAN router's own counters.

  NODE1g-0  link up through the NAT: both rows connected, an over each way heard whole, room maps accepted
  NODE1g-a  delay: `delay 60ms 10ms` each way: link stays up, rtt_ms about 2 x 60, no handshake, overs heard whole and later by about the delay
  NODE1g-b  loss: `loss 3%` each way: link stays up (keepalives hold), no handshake, overs heard (at least 85 percent), loss_ppm recorded
  NODE1g-c  blackout 20 s: the session SURVIVES (no handshake), link up within 10 s of the end, overs resume both ways, floor free
  NODE1g-d  blackout 60 s: both sides expire (rows reconnecting / disconnected, last_error timeout), ONE new handshake each, link up within
            10 s of the end, overs resume both ways, floor free
  NODE1g-e1 an over in flight from the hotspot when the link goes dark (20 s blackout): the central floor is free about 1.6 s after the last Unit
            (a probe over 3.0 s after the cut is granted), the room folds the over end_missing, the session survives, overs resume
  NODE1g-e2 the same from the central node towards the hotspot
  NODE1g-e3 e1 with two early probes (1.0 s and 2.0 s after the cut): when was the floor freed (RECORD; the 3.0 s probe is asserted)
  NODE1g-e4 the same as e2
  NODE1g-f  NAT rebinding, NEW SOURCE PORT, mid-over (one 20 s over from the hotspot, three 8 s overs from the central node): no handshake, the
            central node moves the session to the new source (its row's port), media gap at most 5 s (the longest silence inside the over, a lost
            tail included; the first probe over after a second rebinding is heard within 5 s)
  NODE1g-g  NAT rebinding, NEW SOURCE ADDRESS (the router's alias address), the same assertions, plus the router's evidence of the first datagram
            of each side after the change
  NODE1g-h  rotated key: the central node has a new key, the hotspot the old one: Reject{auth} (central handshakes{auth} grows, never accepted),
            both rows refused_key, the refusal RATE at most 1.1 per second; the old key put back: link up within 10 s, overs resume
  NODE1g-j  ten fault cycles (blackouts that survive, rebindings, netem on and off): nothing stale afterwards (child pids, dv-edge-link threads and
            fds, restarts, sessions, floor)
  NODE1g-k  LINKOBS1 (DVT_LINKOBS1=on, image wip-4732c43): the new series exist, a datagram from another source is counted bad_source, a record of an
            unknown session unknown_session
  NODE1g-r  LINKOBS1 + DVT_SUT_CONFIG_RW=1: the hotspot's account row removed and the node reloaded: Bye{revoked}, session end `revoked`, last_error revoked, a backoff
            no faster than case h's limit; the row put back
  NODE1g-s  LINKOBS1 + DVT_SUT_CONFIG_RW=1: only the secret-file path changes: Bye{reload}, session end `bye` (not revoked), the link back within 10 s

The assertions are the contract of answers-364-node1g.md. A difference is a FINDING: a line `[FINDING]` in out/<run>/node1g.txt AND an assertion that
fails with it; nothing is loosened to pass. Observations that settle nothing are `[RECORD]` lines.
"""
import contextlib
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import admin as admin_mod, metrics, nat, node1g as n1, rfpos1, web  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import p25  # noqa: E402
from dvtest.webload import parse_procstat_out  # noqa: E402

import data_support as ds  # noqa: E402

pytestmark = pytest.mark.rig

LOG = "node1g.txt"
LINK_WAIT_S = 240.0               # both nodes up, the first handshake through the router
ADMIN_WAIT_S = 240.0              # the admin services answer a login
HUB_REFRESH_S = 12.0              # the hub refreshes its counters every 10 s: a read this long after the last event includes it
PROBE_BASE = n1.UNMAPPED_BASE     # radio ids of the relink probes: in no directory (unmapped, guard key = source id)
OVER_CENTRAL = (1023008, "N0TEST")      # talker ids of the rig directory
OVER_HOTSPOT = (1023007, "N0CALL")
READY_WAIT_S = 90.0
FRAME_S = 0.020


def note(n, case, tag, text):
    ds.write_note(n.target, case, tag, text, setting="twonode,hotspot-link,p25", log=LOG)


# ---------------------------------------------------------------- the rig

@pytest.fixture(scope="module")
def n1g(rig, target, rig_control):
    """The pair behind the WAN router, both admin APIs signed in, the link up; skips with the reason otherwise. Cleans the WAN up at the end."""
    if not n1.on():
        pytest.skip(n1.SKIP_REASON)
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_TWONODE") != "on":
        pytest.skip("the second DV-Router node is not part of this rig: run scripts/run.sh --profile twonode with DVT_NODE1G=on")
    ip_h, router = os.environ.get("DVT_N1G_HOTSPOT", ""), os.environ.get("DVT_N1G_ROUTER", "")
    if not ip_h or not router:
        pytest.skip("DVT_N1G_HOTSPOT / DVT_N1G_ROUTER (the hotspot and the WAN router) are not set")
    out = Path(target.out)
    key_path = out / "link-secrets" / n1.KEY_NAME
    if not key_path.is_file():
        pytest.skip("%s is not there (run.sh writes the per-run link key under DVT_NODE1G=on)" % key_path)
    wan = n1.Wan(router)
    try:
        wan.status()
    except (OSError, nat.NatError, ValueError) as exc:
        pytest.skip("the WAN router does not answer at %s:9100: %s" % (router, exc))
    n = types.SimpleNamespace(target=target, control=rig_control, wan=wan, ip_c=rig.xlxd_sut, ip_h=ip_h, out=out, key_path=key_path,
                              admin={"central": None, "hotspot": None}, why={}, t_event=time.monotonic(), key_changed=False, old_key=None, rw_restore=None)
    for node, ip, name in (("central", rig.xlxd_sut, "central-admin.env"), ("hotspot", ip_h, "hotspot-admin.env")):
        path = out / "node1g" / name
        if not path.is_file():
            n.why[node] = "no %s" % path
            continue
        user, password = admin_mod.read_accounts(path)["admin"]
        end, err = time.monotonic() + ADMIN_WAIT_S, "no try"
        while time.monotonic() < end:
            client = n1.PlainAdmin(ip)
            try:
                client.sign_in(user, password)
                n.admin[node] = client
                break
            except (OSError, web.WebError, ValueError) as exc:
                err = "%s: %s" % (type(exc).__name__, client.scrub(str(exc))[:160])
                time.sleep(3.0)
        if n.admin[node] is None:
            n.why[node] = err
    for node, client in n.admin.items():
        note(n, "rig", "RECORD" if client else "GAP", "admin API of the %s node %s" % (node, "signed in" if client else "NOT readable: %s" % n.why.get(node)))
    note(n, "rig", "RECORD", "central %s, hotspot %s behind the WAN router %s (link port %d); netem-wan idle, NAT port_restricted" % (
        n.ip_c, n.ip_h, router, n1.LINK_PORT))
    try:
        yield n
    finally:
        for step in (lambda: rig_control.netem("wan", "off"), lambda: wan.blackhole(False), lambda: wan.readdress(False), wan.reset):
            try:
                step()
            except Exception:                                  # noqa: BLE001 - cleanup of a rig that may be half down
                pass
        if n.key_changed and n.old_key:
            try:
                n1.write_key(n.key_path, n.old_key.value)
            except OSError:
                pass
        if n.rw_restore:                                       # a revocation case that did not finish: the account row back, one more reload
            try:
                (n.out / "xlxd-config" / "xlxd-sut" / "rw" / "dv-router.toml").write_text(n.rw_restore, encoding="utf-8")
                rig_control.hup()
            except Exception:                                  # noqa: BLE001
                pass


# ---------------------------------------------------------------- hubs, rows

def event(n):
    n.t_event = time.monotonic()


def hubs(n, quiet=True):
    """Both hubs' link counters. quiet: first wait until HUB_REFRESH_S after the last event (a refresh that includes it has happened)."""
    if quiet:
        time.sleep(max(0.0, n.t_event + HUB_REFRESH_S - time.monotonic()))
    return {"central": n1.counters(n.control.metrics("xlxd-sut")), "hotspot": n1.counters(n.control.metrics("dvr-peer"))}


def moved(before, after):
    return {node: n1.delta(before[node], after[node]) for node in before}


def get_row(n, node):
    """The link row of `node` from its admin API, or None (no admin, no row, a failed call)."""
    client = n.admin.get(node)
    if client is None:
        return None
    name = "account:%s" % n1.ACCOUNT if node == "central" else "uplink:%s" % n1.UPLINK
    try:
        status = client.get(client.path("/status"), expect=200).json()
    except (OSError, web.WebError, ValueError):
        return None
    return n1.link_row(status, name)


def end_cleared(n, node, who, budget_s=15.0):
    """LINKFIX2: once the link is up again the row shows no session end any more (the next successful handshake clears `last_session_end`): wait for a connected
    row with none; [] when it comes, else the sentence with the value still shown."""
    row, waited = wait_row(n, node, lambda r: connected(r) and n1.row_end_reason(r) is None, budget_s)
    if waited is not None or row is None:
        return []
    return ["%s: the %s row still shows last_session_end %s after the relink (wanted null: the next successful handshake clears it)" % (who, node, n1.row_end_reason(row))]


def wait_row(n, node, ok, budget_s, step_s=1.0):
    """Poll the row of `node` until `ok(row)`; (row, seconds waited) or (last row, None) on timeout."""
    t0 = time.monotonic()
    row = None
    while time.monotonic() - t0 < budget_s:
        row = get_row(n, node)
        if row is not None and ok(row):
            return row, time.monotonic() - t0
        time.sleep(step_s)
    return row, None


def connected(row):
    return row.get("state") == "connected"


def both_connected(n, budget_s):
    """Both rows connected within budget_s (without admin: True, the media probes decide). (rows, ok)."""
    rows = {}
    for node in ("central", "hotspot"):
        if n.admin[node] is None:
            rows[node] = None
            continue
        rows[node], waited = wait_row(n, node, connected, budget_s)
        if waited is None:
            return rows, False
    return rows, True


# ---------------------------------------------------------------- the LAN clients

def client(callsign, src_id):
    return p25.P25Client("0.0.0.0", callsign=callsign, src_id=src_id, tg=n1.TG)


def link_to(c, ip, budget_s=READY_WAIT_S):
    end = time.monotonic() + budget_s
    while time.monotonic() < end:
        if c.link(ip, timeout=3.0):
            return True
    return False


def tick(clients):
    """Poll when due (a P25 link needs a poll at least every 30 s; the tester polls every 5 s) and drain."""
    for c in clients:
        if c.dest and time.monotonic() - c.last_poll >= p25.POLL_PERIOD:
            c.keepalive()
        c.client.drain()


def idle(clients, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        tick(clients)
        time.sleep(0.02)


def over(talker, frames, everyone=()):
    """One over of `frames`; the others are drained meanwhile. (start, end) in monotonic seconds."""
    records = p25.call_records(frames, talker.src_id, talker.tg)
    t0 = time.monotonic()
    talker.send(records, p25.GAP, *[c for c in everyone if c is not talker])
    return t0, time.monotonic()


def frames_of(count, seed):
    return signals.prng_frames("imbe", count, seed="node1g/%s" % seed)


@contextlib.contextmanager
def lan(n):
    """Listeners on both nodes (lc central, lh hotspot) and mapped talkers (hc on the hotspot as N0CALL, cc on the central as N0TEST), all linked.
    Every case gets fresh clients, so its inboxes hold its own traffic only."""
    ns = types.SimpleNamespace(lc=client("N0LSTA", 1023991), lh=client("N0LSTB", 1023992),
                               hc=client(OVER_HOTSPOT[1], OVER_HOTSPOT[0]), cc=client(OVER_CENTRAL[1], OVER_CENTRAL[0]))
    ns.all = (ns.lc, ns.lh, ns.hc, ns.cc)
    try:
        for c, ip in ((ns.lc, n.ip_c), (ns.cc, n.ip_c), (ns.lh, n.ip_h), (ns.hc, n.ip_h)):
            assert link_to(c, ip), "P25 client %s: no poll echo from %s within %.0f s (is the P25 port of that node up?)" % (c.callsign, ip, READY_WAIT_S)
        idle(ns.all, 1.0)
        yield ns
    finally:
        for c in ns.all:
            try:
                if c.dest:
                    c.unlink()
            except OSError:
                pass
            finally:
                c.close()


def probe_until_heard(ns, talker, listener, budget_s, tag, k0=0):
    """Short unmapped probe overs (9 frames, a new radio id each, so no guard strike) from `talker` every 0.4 s until `listener` hears one:
    (arrival time of the first heard frame or None, probes sent). The arrival time is an UPPER bound of the moment the link carried media again."""
    end = time.monotonic() + budget_s
    k = k0
    while time.monotonic() < end:
        sent = frames_of(9, "%s/%d" % (tag, k))
        talker.src_id = n1.unmapped_id(PROBE_BASE, k)
        over(talker, sent, ns.all)
        idle(ns.all, 0.4)
        t = n1.first_heard(n1.voice_arrivals(listener), sent)
        k += 1
        if t is not None:
            return t, k - k0
    return None, k - k0


def resume_both_ways(ns, t_ref, budget_s, tag):
    """Overs resume: a probe from the hotspot heard at the central node within `budget_s` of `t_ref`, then one from the central node heard on the
    hotspot, then a whole over each way. (seconds to the first heard h2c probe, problems)."""
    problems = []
    t_h2c, sent = probe_until_heard(ns, ns.hc, ns.lc, budget_s, tag + "-h2c")
    rel = None if t_h2c is None else t_h2c - t_ref
    if t_h2c is None:
        problems.append("no hotspot-to-central probe over was heard within %.0f s of the end (%d probes)" % (budget_s, sent))
        return rel, problems
    if rel > budget_s:
        problems.append("first hotspot-to-central probe heard %.1f s after the end, wanted within %.0f s" % (rel, budget_s))
    t_c2h, sent = probe_until_heard(ns, ns.cc, ns.lh, n1.RELINK_S, tag + "-c2h")
    if t_c2h is None:
        problems.append("no central-to-hotspot probe over was heard within %.0f s after the first one came the other way (%d probes)" % (n1.RELINK_S, sent))
    ns.hc.src_id, ns.cc.src_id = OVER_HOTSPOT[0], OVER_CENTRAL[0]
    for who, talker, listener, name in (("h2c", ns.hc, ns.lc, "hotspot to central"), ("c2h", ns.cc, ns.lh, "central to hotspot")):
        sent = frames_of(54, "%s/%s-whole" % (tag, who))
        t0, _ = over(talker, sent, ns.all)
        idle(ns.all, 1.5)
        share = n1.heard_share(n1.voice_arrivals(listener), sent, since=t0)
        if share < 0.9:
            problems.append("the whole over %s was heard %.0f percent (wanted at least 90)" % (name, 100 * share))
    return rel, problems


def floor_free(n, before_after, rooms):
    """Problems for the rooms that still show an over on the air in the later hub read."""
    problems = []
    for node, room in rooms:
        if before_after[node].get("onair.%s" % room, 0.0) != 0:
            problems.append("%s hub: room %s is still on the air (dv_status_room_onair %g)" % (node, room, before_after[node]["onair.%s" % room]))
    return problems


def finish(n, case, problems, extra=None):
    for p in problems:
        note(n, case, "FINDING", p)
    if extra:
        note(n, case, "RECORD", extra)
    assert not problems, "; ".join(problems)


def counters_problems(moved_by_node, handshakes, allow_restarts=(), allow_rx=(), ends=None):
    """`handshakes` = (welcomed on the hotspot, accepted on the central): exact deltas expected; plus what must not move on either node. With
    DVT_LINKOBS1=on (LINKOBS1 image) also the session ends of each node (`ends`: {node: {reason: n}}, every other reason 0: a fault the session survives ends
    none), and `bad_source` / `unknown_session` must not move."""
    problems = []
    want_w, want_a = handshakes
    got_w, got_a = int(moved_by_node["hotspot"].get("hs.welcomed", 0)), int(moved_by_node["central"].get("hs.accepted", 0))
    if got_w != want_w:
        problems.append("hotspot handshakes{welcomed} moved by %d, wanted %d" % (got_w, want_w))
    if got_a != want_a:
        problems.append("central handshakes{accepted} moved by %d, wanted %d" % (got_a, want_a))
    for node in ("central", "hotspot"):
        problems += ["%s: %s" % (node, p) for p in n1.collateral(moved_by_node[node], allow_restarts, allow_rx, linkobs=n1.linkobs_on())]
        if n1.linkobs_on():
            problems += ["%s: %s" % (node, p) for p in n1.ends_problems(moved_by_node[node], (ends or {}).get(node))]
    return problems


# ---------------------------------------------------------------- NODE1g-0

def test_node1g_0_link_is_up_through_the_nat(n1g):
    """The link comes up through the NAT router: both rows connected with the rooms mapped (B here = C there), an over each way heard whole, the
    central node sees the hotspot at the router's address, both hubs count one session."""
    n = n1g
    case = "0"
    rows, ok = both_connected(n, LINK_WAIT_S)
    problems = []
    if not ok:
        problems.append("the rows are not both connected within %.0f s: %s / %s" % (LINK_WAIT_S, n1.row_summary(rows.get("central")), n1.row_summary(rows.get("hotspot"))))
    else:
        for node, row in rows.items():
            if row is None:
                continue
            rooms = row.get("rooms") or []
            if not any(r.get("local") in (n1.HOTSPOT_ROOM, n1.CENTRAL_ROOM) for r in rooms):
                problems.append("%s row lists no accepted room map: %s" % (node, rooms))
        peer = (rows.get("central") or {}).get("ip")
        if peer is not None and peer != os.environ.get("DVT_N1G_ROUTER"):
            problems.append("the central node sees the hotspot at %s, wanted the WAN router %s" % (peer, os.environ.get("DVT_N1G_ROUTER")))
    with lan(n) as ns:
        t_ref = time.monotonic()
        rel, more = resume_both_ways(ns, t_ref, 20.0, "n0")
        problems += more
    event(n)
    after = hubs(n)
    for node in ("central", "hotspot"):
        if after[node].get("sessions", 0.0) < 1.0:
            problems.append("%s hub shows %g link session(s), wanted 1" % (node, after[node].get("sessions", 0.0)))
    note(n, case, "RECORD", "central row: %s; hotspot row: %s; WAN router mappings %s" % (
        n1.row_summary(rows.get("central")), n1.row_summary(rows.get("hotspot")), n.wan.mappings()))
    note(n, case, "RECORD", "hub counters central %s; hotspot %s" % (
        {k: v for k, v in sorted(after["central"].items()) if k.startswith(("hs.", "sessions", "rtt"))},
        {k: v for k, v in sorted(after["hotspot"].items()) if k.startswith(("hs.", "sessions", "rtt"))}))
    finish(n, case, problems)


# ---------------------------------------------------------------- NODE1g-a / b: delay and loss

def window(n, **rec):
    """Append one observation window to out/<run>/node1g/windows.jsonl (epoch seconds of the host clock, which dumpcap shares): what
    `python3 -m dvtest.node1g_capture out/<run>` reads together with the captures of a run with --capture."""
    with open(n.out / "node1g" / "windows.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


IMPAIRED_FRAMES = 500             # frames of the 10 s over each way in the delay and loss cases


def impaired_run(n, case, args, long_s, ns_seed):
    """One impairment: set it on the router, let the link run `long_s` seconds, one 10 s over each way; returns what the judge needs."""
    before = hubs(n)
    t_epoch_on = time.time()
    n.control.netem("wan", args)
    event(n)
    t_on = time.monotonic()
    with lan(n) as ns:
        idle(ns.all, 12.0)                                    # the first keepalive echo gives the first RTT sample (5-10 s)
        res = {}
        for who, talker, listener in (("h2c", ns.hc, ns.lc), ("c2h", ns.cc, ns.lh)):
            sent = frames_of(IMPAIRED_FRAMES, "%s/%s" % (ns_seed, who))
            t0, t1 = over(talker, sent, ns.all)
            idle(ns.all, 2.0)
            arr = n1.voice_arrivals(listener)
            first = n1.first_heard(arr, sent, since=t0)
            res[who] = {"share": n1.heard_share(arr, sent, since=t0), "first_ms": None if first is None else round((first - t0) * 1000.0)}
        idle(ns.all, max(0.0, long_s - (time.monotonic() - t_on)))
    rows = {node: get_row(n, node) for node in ("central", "hotspot")}
    n.last_window = {"t_start": t_epoch_on, "t_stop": time.time(), "args": args}          # the case writes the window once it has the hub deltas
    n.control.netem("wan", "off")
    event(n)
    after = hubs(n)
    return before, after, res, rows


def test_node1g_a_delay(n1g):
    """`delay 60ms 10ms` on each interface of the router (one way 60 ms, jitter 10 ms, below the 20 ms frame spacing so nothing reorders): the link
    stays up with no handshake, rtt_ms of the rows is about 2 x 60, an over each way is heard whole and its first frame arrives 40 to 200 ms
    after the first frame was sent (the LAN path alone is a few ms). RECORD: per node the late / lost / gap units of the room and of the link edge
    and the rows' loss_ppm, and (host step, run with --capture) how many datagrams and frames the jitter reordered."""
    n = n1g
    case = "a"
    before, after, res, rows = impaired_run(n, case, n1.DELAY_ARGS, 25.0, "a")
    mv = moved(before, after)
    problems = counters_problems(mv, (0, 0))
    for who, r in res.items():
        if r["share"] < 0.95:
            problems.append("%s: the over was heard %.0f percent (wanted at least 95 with delay only)" % (who, 100 * r["share"]))
        if r["first_ms"] is None or not 40 <= r["first_ms"] <= 200:
            problems.append("%s: first frame %s ms after sending, wanted 40..200 (one way 60 ms)" % (who, r["first_ms"]))
    for node, row in rows.items():
        if row is None:
            continue
        if not connected(row):
            problems.append("%s row is not connected under delay: %s" % (node, n1.row_summary(row)))
        rtt = row.get("rtt_ms")
        lo, hi = n1.RTT_RANGE_MS
        if rtt is None or not lo <= rtt <= hi:
            problems.append("%s row rtt_ms %s, wanted %g..%g (2 x %g ms)" % (node, rtt, lo, hi, n1.DELAY_ONE_WAY_MS))
    note(n, case, "RECORD", "delay only: the rows' loss_ppm (a counter-gap estimate: a reordered datagram counts as a gap and is never subtracted) and the overs folded `lossy` "
         "tell whether the 10 ms jitter reordered datagrams (dgram.rx.replay moved: %s)" % {k: v.get("dgram.rx.replay", 0) for k, v in mv.items()})
    note(n, case, "RECORD", "heard %s; rows central %s / hotspot %s; moved %s" % (res, n1.row_summary(rows["central"]), n1.row_summary(rows["hotspot"]), mv))
    for node in ("central", "hotspot"):
        units = {k: v for k, v in sorted(mv[node].items()) if k.startswith(("ounits.", "eunits.")) or ".lossy" in k}
        note(n, case, "RECORD", "%s node: late / lost / gap units of the room and of the link edge, lossy overs: %s; row loss_ppm %s" % (
            node, units or "none moved", (rows[node] or {}).get("loss_ppm")))
    # the window for the capture judge (python3 -m dvtest.node1g_capture): the late units counted by each node's link edge, what the listeners missed
    window(n, case=case, kind="delay", linkseq1=n1.linkseq_on(), late_units={node: n1.late_units(m) for node, m in mv.items()},
           missing={who: int(round((1.0 - r["share"]) * IMPAIRED_FRAMES)) for who, r in res.items()}, **n.last_window)
    if n1.linkseq_on():
        # LINKSEQ1 (a future image; default off keeps the behaviour above): a) every over heard 100 percent, b) every missing frame visible in a late / lost / gap
        # counter of some node or edge, c) the row's loss_ppm counts missing units. The lateness of each reorder (late 0 inside 40 ms) is a host step
        # on the capture: python3 -m dvtest.node1g_capture out/<run>
        problems += n1.linkseq_problems({who: r["share"] for who, r in res.items()}, IMPAIRED_FRAMES, mv, rows)
    finish(n, case, problems)


def test_node1g_b_loss(n1g):
    """`loss 3%` on each interface (3 percent each way, the order of a poor WiFi or LTE uplink): six keepalives in a row would have to be lost to end
    a session (about 1e-9), so the link stays up with no handshake; units have no repair (link.md), so an over is heard at 85 percent or more."""
    n = n1g
    case = "b"
    before, after, res, rows = impaired_run(n, case, n1.LOSS_ARGS, 60.0, "b")
    mv = moved(before, after)
    problems = counters_problems(mv, (0, 0))
    for who, r in res.items():
        if r["share"] < 0.85:
            problems.append("%s: the over was heard %.0f percent (wanted at least 85 with 3 percent loss)" % (who, 100 * r["share"]))
    for node, row in rows.items():
        if row is not None and not connected(row):
            problems.append("%s row is not connected under loss: %s" % (node, n1.row_summary(row)))
    note(n, case, "RECORD", "heard %s; loss_ppm central %s / hotspot %s; rows %s / %s" % (
        res, (rows["central"] or {}).get("loss_ppm"), (rows["hotspot"] or {}).get("loss_ppm"), n1.row_summary(rows["central"]), n1.row_summary(rows["hotspot"])))
    note(n, case, "RECORD", "moved %s" % mv)
    finish(n, case, problems)


# ---------------------------------------------------------------- NODE1g-c / d: blackouts

def blackout(n, ns, seconds):
    """Blackhole the WAN for `seconds` (the tester keeps its LAN clients alive meanwhile); returns the monotonic times (cut, end)."""
    t_cut = time.monotonic()
    n.wan.blackhole(True)
    event(n)
    idle(ns.all, max(0.0, seconds - (time.monotonic() - t_cut)))
    n.wan.blackhole(False)
    t_end = time.monotonic()
    event(n)
    return t_cut, t_end


def test_node1g_c_blackout_20s_survives(n1g):
    """A 20 s blackout of the WAN with the link idle: last traffic was at most 5 s before it, so the session does not reach the 30 s timeout; no
    handshake on either side, the link carries overs within 10 s of the end, the floor is free and nothing is stale."""
    n = n1g
    case = "c"
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    problems = []
    with lan(n) as ns:
        t_cut, t_end = blackout(n, ns, 20.0)
        rel, more = resume_both_ways(ns, t_end, n1.RELINK_S, "c")
        problems += more
        for node in ("central", "hotspot"):
            mid_row = get_row(n, node)
            if mid_row is not None and not connected(mid_row):
                problems.append("%s row is %s after the blackout (last_error %s)" % (node, mid_row.get("state"), mid_row.get("last_error")))
    event(n)
    after = hubs(n)
    problems += counters_problems(moved(before, after), (0, 0))
    problems += floor_free(n, after, (("central", "C"), ("hotspot", "B")))
    for node in ("central", "hotspot"):
        if after[node].get("sessions", 0.0) < 1.0:
            problems.append("%s hub shows %g link session(s) after the blackout, wanted 1" % (node, after[node].get("sessions", 0.0)))
    note(n, case, "RECORD", "blackout %.1f s; first probe heard %s s after the end; moved %s" % (t_end - t_cut, None if rel is None else round(rel, 2), moved(before, after)))
    finish(n, case, problems)


def test_node1g_d_blackout_60s_relinks(n1g):
    """A 60 s blackout: both sides expire the session (30 s without a datagram): the rows leave `connected` (hotspot reconnecting, central
    disconnected, last_error timeout), the hubs count no session; after the end ONE new handshake each side, link up within 10 s, overs resume
    both ways, the floor is free."""
    n = n1g
    case = "d"
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    problems = []
    seen_mid = {}
    with lan(n) as ns:
        t_cut = time.monotonic()
        n.wan.blackhole(True)
        event(n)
        idle(ns.all, 42.0)                                    # 30 s timeout + the 5 s keepalive phase + margin
        for node in ("central", "hotspot"):
            seen_mid[node] = get_row(n, node)
        hub_mid = hubs(n, quiet=False)
        idle(ns.all, max(0.0, 60.0 - (time.monotonic() - t_cut)))
        n.wan.blackhole(False)
        t_end = time.monotonic()
        event(n)
        rel, more = resume_both_ways(ns, t_end, n1.RELINK_S, "d")
        problems += more
        rows_after = {node: wait_row(n, node, connected, 15.0)[0] for node in ("central", "hotspot")}
    event(n)
    after = hubs(n)
    want_state = {"central": "disconnected", "hotspot": "reconnecting"}
    for node, row in seen_mid.items():
        if row is None:
            continue
        if n1.linkfix2_on() and n1.row_end_reason(row) != "timeout":
            problems.append("LINKFIX2: %s row last_session_end is %s during the blackout (wanted timeout)" % (node, n1.row_end_reason(row)))
        if row.get("state") != want_state[node]:
            problems.append("%s row during the blackout is %s (wanted %s): %s" % (node, row.get("state"), want_state[node], n1.row_summary(row)))
        # LINKFIX2 moved the session end into last_session_end: the central node (a server, it makes no attempts) has no attempt error any more, the hotspot's own
        # attempts still time out (wip-ae11b8f, 20261124T100404Z: central None, hotspot timeout; contract section 5 had timeout on both)
        allowed = (None, "timeout") if (n1.linkfix2_on() and node == "central") else ("timeout",)
        if row.get("last_error") not in allowed:
            problems.append("%s row last_error during the blackout is %s (wanted %s)" % (node, row.get("last_error"), " or ".join(str(a) for a in allowed)))
    for node in ("central", "hotspot"):
        if hub_mid[node].get("sessions", 1.0) != 0.0:
            problems.append("%s hub shows %g link session(s) 42 s into the blackout (the session must be dead: 30 s without a datagram)" % (node, hub_mid[node].get("sessions", 0.0)))
    problems += counters_problems(moved(before, after), (1, 1), ends={"central": {"timeout": 1}, "hotspot": {"timeout": 1}})
    problems += floor_free(n, after, (("central", "C"), ("hotspot", "B")))
    for node, row in rows_after.items():
        if row is not None and row.get("last_error") not in (None, ""):
            problems.append("%s row keeps last_error %s after the link came back" % (node, row.get("last_error")))
    if n1.linkfix2_on():
        for node in ("central", "hotspot"):
            problems += end_cleared(n, node, "d")
            problems += n1.end_vs_hub_problems("d " + node, n1.row_end_reason(seen_mid.get(node)), n1.delta(before[node], hub_mid[node]))
    note(n, case, "RECORD", "mid-blackout rows: central %s / hotspot %s; first probe heard %s s after the end; moved %s" % (
        n1.row_summary(seen_mid.get("central")), n1.row_summary(seen_mid.get("hotspot")), None if rel is None else round(rel, 2), moved(before, after)))
    finish(n, case, problems)


# ---------------------------------------------------------------- NODE1g-e: an over in flight

def in_flight(n, direction, early_probes=False):
    """One over of 600 frames (12 s) from one node while the WAN goes dark 3 s into it for 20 s. A talker of the RECEIVING node sends a 54 frame
    probe over 3.0 s after the cut: granted (heard at its node's listener) means the floor of the room was free by then. With `early_probes` two
    more probes (9 frames, unmapped radio ids) go 1.0 s and 2.0 s after the cut and tell WHEN the floor became free; they are a second talker
    competing for the floor, so those runs are separate cases (the way the in-flight over is folded is judged only without them). Returns the evidence."""
    h2c = direction == "h2c"
    before = hubs(n)
    with lan(n) as ns:
        talker = ns.hc if h2c else ns.cc
        prober = ns.cc if h2c else ns.hc
        recv, local = (ns.lc, ns.lh) if h2c else (ns.lh, ns.lc)     # listener of the receiving node, listener of the sending node
        sent = frames_of(600, "e-%s/long" % direction)
        probe = frames_of(54, "e-%s/probe" % direction)
        records = p25.call_records(sent, talker.src_id, talker.tg)
        box = {}
        thread = threading.Thread(target=lambda: (box.update(start=time.monotonic()), talker.send(records, p25.GAP), box.update(end=time.monotonic())), daemon=True)
        t0 = time.monotonic()
        thread.start()
        others = [c for c in ns.all if c is not talker]
        while time.monotonic() - t0 < 3.0:
            tick(others)
            time.sleep(0.01)
        t_cut = time.monotonic()
        n.wan.blackhole(True)
        event(n)
        # two early probes (9 frames, unmapped radio ids: no guard strike) narrow down WHEN the floor became free: RECORD only
        early, prober_id = [], prober.src_id
        for at, kk in ((1.0, 0), (2.0, 1)) if early_probes else ():
            while time.monotonic() - t_cut < at:
                tick(others)
                time.sleep(0.01)
            ef = frames_of(9, "e-%s/early%d" % (direction, kk))
            prober.src_id = n1.unmapped_id(PROBE_BASE, 500 + kk)
            te, _ = over(prober, ef, others)
            early.append((at, ef, te))
        prober.src_id = prober_id
        while time.monotonic() - t_cut < 3.0:
            tick(others)
            time.sleep(0.01)
        t_probe, t_probe_end = over(prober, probe, others)
        idle(others, 0.8)
        while thread.is_alive():
            tick(others)
            time.sleep(0.02)
        idle(ns.all, max(0.0, 20.0 - (time.monotonic() - t_cut)))
        n.wan.blackhole(False)
        t_end = time.monotonic()
        event(n)
        arr_recv = n1.voice_arrivals(recv)
        arr_local = n1.voice_arrivals(local)
        sent_set = set(bytes(f) for f in sent)
        times = [t for t, imbe in arr_recv if imbe in sent_set]
        last_unit = max(times) if times else None
        probe_share = n1.heard_share(arr_recv, probe, since=t_probe)
        local_share = n1.heard_share(arr_local, sent, since=t0)
        early_heard = {at: round(n1.heard_share(arr_recv, ef, since=te), 2) for at, ef, te in early}
        rel, more = resume_both_ways(ns, t_end, n1.RELINK_S, "e-%s" % direction)
    event(n)
    after = hubs(n)
    return types.SimpleNamespace(before=before, after=after, probe_share=probe_share, last_unit=last_unit, t_cut=t_cut, t_probe=t_probe,
                                 local_share=local_share, rel=rel, more=more, sent_share=n1.heard_share(arr_recv, sent, since=t0),
                                 early_heard=early_heard)


def judge_in_flight(n, case, ev, room_node, room, fold=True):
    """`fold`: assert that the room folds the over in flight as end_missing (contract section 4); the runs with early probes only record it."""
    problems = list(ev.more)
    release = None if ev.last_unit is None else ev.last_unit - ev.t_cut
    if not n1.floor_probe_ok(ev.probe_share):
        problems.append("the probe over %.1f s after the cut was heard %.0f percent on the receiving node: its floor was still held (wanted free about %.1f s after the last "
                        "Unit, probe at 3.0 s)" % (ev.t_probe - ev.t_cut, 100 * ev.probe_share, n1.STREAM_SILENCE_S))
    mv = moved(ev.before, ev.after)
    problems += counters_problems(mv, (0, 0))
    problems += floor_free(n, ev.after, (("central", "C"), ("hotspot", "B")))
    got = mv[room_node].get("overs.%s.end_missing" % room, 0.0)
    if fold and got < 1:
        problems.append("%s hub: dv_room_overs_total{room=%s,result=end_missing} moved by %g, wanted at least 1 (the over in flight ends by stream silence)" % (room_node, room, got))
    note(n, case, "RECORD", "last Unit heard %s s after the cut; early probes (seconds after the cut: share heard) %s; probe sent %.1f s after the cut, heard %.0f percent; "
         "the over was heard %.0f percent on the receiving node and %.0f percent on its own node; first probe after the end heard %s s after it; moved %s" % (
             None if release is None else round(release, 2), ev.early_heard, ev.t_probe - ev.t_cut, 100 * ev.probe_share, 100 * ev.sent_share, 100 * ev.local_share,
             None if ev.rel is None else round(ev.rel, 2), mv))
    finish(n, case, problems)


def test_node1g_e1_over_in_flight_hotspot_to_central(n1g):
    """An over from the hotspot is on the air when the WAN goes dark for 20 s: the central node's stream ends by silence, its room C floor is free
    about 1.6 s after the last Unit (a probe over of its own talker 3.0 s after the cut is granted, not 30 s later), the room folds the over
    end_missing, the session survives (no handshake), overs resume both ways after the end."""
    judge_in_flight(n1g, "e1", in_flight(n1g, "h2c"), "central", "C")


def test_node1g_e2_over_in_flight_central_to_hotspot(n1g):
    """The same the other way: the over of a central talker is on the air towards the hotspot; the hotspot's stream ends by silence, a local
    talker of the hotspot is granted 3.0 s after the cut, the room B folds the over end_missing, the session survives."""
    judge_in_flight(n1g, "e2", in_flight(n1g, "c2h"), "hotspot", "B")


FOLD_ENV = "DVT_NODE1G_FOLD"


def fold_iteration(n, case, direction):
    """One over of 12 s from one node, the WAN dark from 3 s for 20 s, and NOTHING else on the link until the hubs have counted it: the receiving node's room
    folds exactly this over, so its hub delta says how (end_missing / complete / lossy). The epoch times go to windows.jsonl for the capture judge."""
    h2c = direction == "h2c"
    node, room = ("central", "C") if h2c else ("hotspot", "B")
    before = hubs(n)
    with lan(n) as ns:
        talker = ns.hc if h2c else ns.cc
        records = p25.call_records(frames_of(600, "%s/long" % case), talker.src_id, talker.tg)
        thread = threading.Thread(target=lambda: talker.send(records, p25.GAP), daemon=True)
        others = [c for c in ns.all if c is not talker]
        t_start, t0 = time.time(), time.monotonic()
        thread.start()
        while time.monotonic() - t0 < 3.0:
            tick(others)
            time.sleep(0.01)
        t_cut, m_cut = time.time(), time.monotonic()
        n.wan.blackhole(True)
        event(n)
        while thread.is_alive():
            tick(others)
            time.sleep(0.02)
        idle(ns.all, max(0.0, 20.0 - (time.monotonic() - m_cut)))
        n.wan.blackhole(False)
        t_end = time.time()
        event(n)
    after = hubs(n)
    mv = moved(before, after)
    fold = {k: {kk: vv for kk, vv in v.items() if kk.startswith("overs.")} for k, v in mv.items()}
    window(n, case=case, kind="fold", dir=direction, t_start=t_start, t_cut=t_cut, t_end=t_end, fold=fold)
    return node, room, fold[node], mv


def test_node1g_m_fold_repeat(n1g):
    """Opt-in investigation (DVT_NODE1G_FOLD=<n>: n isolated in-flight overs per direction; run with --capture): an over whose End and later units were lost in a
    blackout must fold end_missing on the receiving node's room (dv-router's rule: complete only when the End arrived and no unit was lost). The
    case asserts that by construction (the End of a 12 s over is sent 9 s into the blackout); `python3 -m dvtest.node1g_capture out/<run>` then checks the
    same rule against the captured link datagrams and names the stream of a defect."""
    try:
        count = int(os.environ.get(FOLD_ENV, "0"))
    except ValueError:
        count = 0
    if count < 1:
        pytest.skip("%s=<n> is not set (an opt-in investigation of the fold of an over cut by a blackout; docs/node1g-cases.md)" % FOLD_ENV)
    n = n1g
    problems, seen = [], []
    for i in range(count):
        for direction in ("h2c", "c2h"):
            node, room, fold, mv = fold_iteration(n, "m%d" % (i + 1), direction)
            got = {k.split(".", 2)[2]: v for k, v in fold.items() if v}
            seen.append("%s %s: %s" % ("m%d" % (i + 1), direction, got))
            if got != {"end_missing": 1.0}:
                problems.append("m%d %s: the over folded %s on the %s node (room %s), wanted exactly one end_missing (its End and later units were lost)" % (
                    i + 1, direction, got or "nothing", node, room))
    note(n, "m", "RECORD", "folds of the isolated overs cut by a blackout: %s" % "; ".join(seen))
    finish(n, "m", problems)


def test_node1g_e3_floor_release_window_hotspot_to_central(n1g):
    """As e1 with two early probes of the central node's own talker (9 frames, 1.0 s and 2.0 s after the cut): which of them is granted tells when the
    floor was freed (the contract: 1.6 s after the last Unit). The 3.0 s probe is asserted as in e1; how the room folds the over is only recorded
    (a second talker was competing for the floor)."""
    judge_in_flight(n1g, "e3", in_flight(n1g, "h2c", early_probes=True), "central", "C", fold=False)


def test_node1g_e4_floor_release_window_central_to_hotspot(n1g):
    """As e2 with the two early probes of a hotspot talker."""
    judge_in_flight(n1g, "e4", in_flight(n1g, "c2h", early_probes=True), "hotspot", "B", fold=False)


# ---------------------------------------------------------------- NODE1g-f / g: NAT rebinding

def rebind_trial(n, case, kind, who, over_s, rebind_at):
    """One trial: an over from one node (`who`: h2c = hotspot talker, c2h = central talker) of `over_s` seconds; `rebind_at` seconds into it the NAT
    mapping of the hotspot changes: `kind` port (NAT type symmetric + the flush: a new random source port) or address (readdress: the router's alias
    address). Afterwards the way back (a second rebinding) with a train of probe overs started AT it, in the same direction."""
    map_before = n.wan.mappings()
    with lan(n) as ns:
        talker, listener = (ns.hc, ns.lc) if who == "h2c" else (ns.cc, ns.lh)
        sent = frames_of(int(over_s / FRAME_S), "%s-%s-%d" % (case, who, int(time.monotonic()) % 1000))
        records = p25.call_records(sent, talker.src_id, talker.tg)
        thread = threading.Thread(target=lambda: talker.send(records, p25.GAP), daemon=True)
        others = [c for c in ns.all if c is not talker]
        t0 = time.monotonic()
        thread.start()
        while time.monotonic() - t0 < rebind_at:
            tick(others)
            time.sleep(0.01)
        t_rebind = time.monotonic()
        if kind == "port":
            n.wan.mode("symmetric")
        else:
            n.wan.readdress(True)
        event(n)
        while thread.is_alive():
            tick(others)
            time.sleep(0.02)
        idle(ns.all, 2.0)
        arr = n1.voice_arrivals(listener)
        # the silence a listener perceives INSIDE the over, its lost tail included (the frames that never arrived are the gap)
        gap, gap_idx = n1.missing_run(arr, sent, since=t0)
        gap_at = None if gap_idx is None else t0 + gap_idx * FRAME_S
        share = n1.heard_share(arr, sent, since=t0)
        map_after = n.wan.mappings()
        firsts = n.wan.firsts() if kind == "address" else None
        row_c = None
        if n.admin["central"] is not None:
            want = sorted((m["seen_ip"], m["seen_port"]) for m in map_after)
            # the central row moves to the new source when its snapshot is next published; wait for the row to show the translated address AND port
            row_c, waited = wait_row(n, "central", lambda r: connected(r) and (not want or (r.get("ip"), r.get("port")) == want[-1]), 12.0)
        idle(ns.all, 1.0)
        # the way back (a second rebinding) with a probe train started AT it, in the direction under test: how soon does media flow again?
        t_back = time.monotonic()
        if kind == "port":
            n.wan.mode("port_restricted")
        else:
            n.wan.readdress(False)
        event(n)
        t_heard, probes = probe_until_heard(ns, talker, listener, 9.0, "%s-%s-back" % (case, who))
        resume_s = None if t_heard is None else t_heard - t_back
    idle([], 3.0)
    return types.SimpleNamespace(who=who, map_before=map_before, map_after=map_after, gap=gap, gap_after_rebind=None if gap_at is None else gap_at - t_rebind,
                                 share=share, firsts=firsts, row_c=row_c, resume_s=resume_s, probes=probes, over_s=over_s, rebind_at=rebind_at)


# one 20 s over from the hotspot, then three 8 s overs from the central node (the gap of the central-to-hotspot direction is the time until the
# hotspot's next datagram, which depends on the phase of its 5 s keepalive: one trial would be luck either way)
REBIND_TRIALS = (("h2c", 20.0, 8.0), ("c2h", 8.0, 3.0), ("c2h", 8.0, 3.0), ("c2h", 8.0, 3.0))


def rebind_scenario(n, case, kind):
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    trials = [rebind_trial(n, case, kind, who, over_s, at) for who, over_s, at in REBIND_TRIALS]
    event(n)
    after = hubs(n)
    return before, after, trials


def judge_rebind(n, case, scenario, kind):
    before, after, trials = scenario
    problems = []
    limit = n1.NAT_GAP_S + n1.NAT_GAP_SLACK_S
    mv = moved(before, after)
    problems += counters_problems(mv, (0, 0))
    cut = [t for t in trials if t.who == "c2h" and t.share < 0.9]
    for i, ev in enumerate(trials):
        name = "%s#%d" % (ev.who, i)
        if ev.gap > limit:
            problems.append("%s: media gap %.2f s inside the over that was on the air at the rebinding (the longest run of its frames that never arrived, its lost tail "
                            "included), wanted at most %.1f s (one keepalive period); %.0f percent of the over was heard" % (name, ev.gap, n1.NAT_GAP_S, 100 * ev.share))
        elif ev.share < 0.9:
            problems.append("%s: the over was CUT by the rebinding: %.0f percent of it was heard (wanted at least 90: only the frames of the gap may be lost); the rest was never "
                            "delivered although the session moved (the receiving link edge ends a stream after 1.6 s without a Unit, and later Units of that stream are "
                            "refused)" % (name, 100 * ev.share))
        if ev.firsts:
            sides = ev.firsts.get("sides", {})
            for side in ("from_new", "to_new"):
                first = (sides.get(side) or {}).get("first_s")
                if first is None or first > limit:
                    problems.append("%s: router evidence: the first datagram %s came %s s after the readdress (wanted at most %.1f s: the hotspot's next keepalive "
                                    "tells the central node where to send)" % (name, side, first, n1.NAT_GAP_S))
        if ev.resume_s is None or ev.resume_s > limit:
            problems.append("%s: after the second rebinding the first probe over was heard %s s later (%d probes), wanted at most %.1f s" % (
                name, None if ev.resume_s is None else round(ev.resume_s, 2), ev.probes, n1.NAT_GAP_S))
        if n1.linkrebind_on():
            # LINKREBIND1 (the hotspot sends a keepalive at most every 1 s while it receives): the post-fix numbers, asserted on top of the contract ones
            if ev.who == "c2h" and ev.gap >= n1.LINKREBIND_GAP_S:
                problems.append("%s: LINKREBIND1: central-to-hotspot gap %.2f s, wanted below %.1f s" % (name, ev.gap, n1.LINKREBIND_GAP_S))
            if ev.share < n1.LINKREBIND_SHARE:
                problems.append("%s: LINKREBIND1: the over was heard %.1f percent, wanted at least %.0f (no cut over in any trial)" % (
                    name, 100 * ev.share, 100 * n1.LINKREBIND_SHARE))
        seen_b = sorted((m["seen_ip"], m["seen_port"]) for m in ev.map_before)
        seen_a = sorted((m["seen_ip"], m["seen_port"]) for m in ev.map_after)
        if not seen_a or seen_a == seen_b:
            problems.append("%s: the central node sees the hotspot at %s before and %s after: the rebinding did not happen" % (name, seen_b, seen_a))
        elif kind == "address" and all(ip == seen_b[0][0] for ip, _ in seen_a):
            problems.append("%s: the source ADDRESS did not change (%s -> %s)" % (name, seen_b, seen_a))
        elif kind == "port" and all(port == seen_b[0][1] for _, port in seen_a):
            problems.append("%s: the source PORT did not change (%s -> %s)" % (name, seen_b, seen_a))
        row = ev.row_c
        if row is not None and seen_a:
            want_ip, want_port = seen_a[-1]
            if (row.get("ip"), row.get("port")) != (want_ip, want_port):
                problems.append("%s: the central row shows the peer at %s:%s, the router translates it to %s:%s (the session was not moved)" % (
                    name, row.get("ip"), row.get("port"), want_ip, want_port))
        note(n, case, "RECORD", "%s (over %.0f s, rebinding at %.0f s): longest silence inside the over %.2f s (starting %s s after the rebinding), heard %.0f percent; first probe %s s "
             "after the second rebinding (%d probes); the central node saw %s then %s; central row %s" % (
                 name, ev.over_s, ev.rebind_at, ev.gap, None if ev.gap_after_rebind is None else round(ev.gap_after_rebind, 2), 100 * ev.share,
                 None if ev.resume_s is None else round(ev.resume_s, 2), ev.probes, seen_b, seen_a, n1.row_summary(ev.row_c)))
        if ev.firsts:
            sides = ev.firsts.get("sides", {})
            note(n, case, "RECORD", "%s router evidence after the readdress (seconds to the first datagram of each side): %s" % (
                name, {k: {"first_s": v.get("first_s"), "n": v.get("n")} for k, v in sides.items()}))
    note(n, case, "RECORD", "central-to-hotspot overs cut by the rebinding: %d of %d; moved %s" % (len(cut), len([t for t in trials if t.who == "c2h"]), mv))
    finish(n, case, problems)


def test_node1g_f_nat_rebinding_new_source_port(n1g):
    """The hotspot's NAT mapping changes to a NEW SOURCE PORT in the middle of a 20 s over (NAT type symmetric plus a flush of the translations),
    in each direction: it is not a new peer: no new handshake on either side, the central node moves the session to the new source (its row shows
    the new port), the media gap is at most 5 s (the next keepalive of the hotspot tells the central node where to send)."""
    judge_rebind(n1g, "f", rebind_scenario(n1g, "f", "port"), "port")


def test_node1g_g_nat_rebinding_new_source_address(n1g):
    """The same with a NEW SOURCE ADDRESS (the router moves the hotspot's UDP to its alias address): no handshake, the session moves, gap at most 5 s."""
    judge_rebind(n1g, "g", rebind_scenario(n1g, "g", "address"), "address")


# ---------------------------------------------------------------- NODE1g-h: a rotated key

class RowSampler:
    """The link row of one node read every 0.25 s for `seconds` on a thread (a label such as last_error revoked lasts well under a second): `finish()`
    joins and returns [(state, last_error)] in time order. Used only around a reload, before the main thread reads rows again."""

    def __init__(self, n, node, seconds):
        self.n, self.node, self.seconds, self.rows, self.full = n, node, seconds, [], []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        end = time.monotonic() + self.seconds
        while time.monotonic() < end:
            row = get_row(self.n, self.node)
            if row is not None:
                self.rows.append((row.get("state"), row.get("last_error")))
                self.full.append((row.get("state"), row.get("last_error"), n1.row_end_reason(row)))
            time.sleep(0.25)

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        self.thread.join(self.seconds + 5.0)
        return list(self.rows)


def test_node1g_h_rotated_key(n1g):
    """The central node gets a NEW key (the file replaced at the same path by rename, then SIGHUP through the control channel: link.md rotation), the
    hotspot keeps the old one. The edge restarts and sends Bye{reload}; every attempt of the hotspot ends in Reject{auth}: the central node's
    handshakes{auth} grows but its handshakes{accepted} and sessions do not, both rows carry last_error refused_key (the hotspot's row is only a hint),
    the RATE of the refusals stays at most 1.1 per second (backoff 0.5 s doubling to 5 s, +-20 percent, an attempt every 10 s with Auth every 0.5 s).
    The old key put back (the same way): the link is up within 10 s, overs resume both ways, one handshake each side."""
    n = n1g
    case = "h"
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    problems = []
    old = n1.KeyBox(n1.read_key(n.key_path))                         # a KeyBox never shows its key (pytest prints the locals of a failing test)
    n.old_key = old
    new = n1.KeyBox(n1.new_key_hex())
    n1.write_key(n.key_path.with_name(n1.ROTATED_NAME), new.value)    # beside the live key, so the run's secret scan covers it
    n1.write_key(n.key_path, new.value)
    n.key_changed = True
    t_rot = time.time()
    # the hotspot's row sampled every 0.25 s around the reload: a rekey is a revocation (LINKOBS1: Bye{revoked}, last_error revoked until the first refused attempt)
    sampler = RowSampler(n, "hotspot", 10.0).start()
    n.control.hup()
    event(n)
    first_labels = sampler.finish()
    note(n, case, "RECORD", "hotspot row right after the reload (state, last_error): %s" % first_labels[:40])
    if n1.linkfix2_on():
        # LINKFIX2: the row keeps the end reason apart from the attempt error: revoked while it retries, last_error the refusal
        problems += n1.end_reason_problems(sampler.full, "h")
    elif n1.linkobs_on() and "revoked" not in [e for _, e in first_labels]:
        problems.append("LINKOBS1: the hotspot row never showed last_error revoked after the rekey (labels seen %s)" % sorted(set(str(e) for _, e in first_labels)))
    window = 120.0
    t_start = time.monotonic()
    samples, rows_seen = [], {"central": [], "hotspot": []}
    ever_connected = {"central": False, "hotspot": False}
    with lan(n) as ns:
        k = 0
        while time.monotonic() - t_start < window:
            c = n1.counters(n.control.metrics("xlxd-sut"))
            samples.append((time.monotonic(), c.get("hs.auth", 0.0)))
            if k % 3 == 0:                                    # the rows are read every third round: the counter read is the one that sets the resolution
                for node in ("central", "hotspot"):
                    row = get_row(n, node)
                    if row is not None:
                        rows_seen[node].append((round(time.monotonic() - t_start, 1), row.get("state"), row.get("last_error")))
                        ever_connected[node] = ever_connected[node] or connected(row)
            k += 1
            tick(ns.all)
        mid = hubs(n, quiet=False)
        # probes over the dead link: nothing may cross
        t_dead, sent = probe_until_heard(ns, ns.hc, ns.lc, 3.0, "h-dead")
        if t_dead is not None:
            problems.append("an over crossed the link while the keys differ")
    rate = n1.refusal_rate(samples)
    note(n, case, "RECORD", "rotated at +0 s; refusal samples %d, rate %s; rows central %s; hotspot %s" % (
        len(samples), None if rate is None else "%.3f per s over %.0f s (%g refusals)" % rate, rows_seen["central"][::8], rows_seen["hotspot"][::8]))
    if rate is None:
        problems.append("handshakes{auth} on the central node did not grow while the keys differ (no Reject{auth} counted; samples %d)" % len(samples))
    elif rate[0] > n1.REFUSAL_RATE_MAX:
        problems.append("Reject{auth} rate %.3f per second over %.0f s, wanted at most %.1f (about %g refusals)" % (rate[0], rate[1], n1.REFUSAL_RATE_MAX, rate[2]))
    # the central dv-edge-link was restarted by the reload: its series start again at zero (rebase), the hotspot's edge was not
    mv_mid = {"central": n1.rebase(before["central"], mid["central"]), "hotspot": n1.delta(before["hotspot"], mid["hotspot"])}
    if mv_mid["central"].get("hs.accepted", 0):
        problems.append("the central node accepted a session with the wrong key (handshakes{accepted} +%g)" % mv_mid["central"]["hs.accepted"])
    for node in ("central", "hotspot"):
        if mid[node].get("sessions", 1.0) != 0.0:
            problems.append("%s hub shows %g link session(s) while the keys differ" % (node, mid[node].get("sessions", 0.0)))
        if ever_connected[node] and any(s == "connected" for _, s, _ in rows_seen[node][3:]):
            problems.append("the %s row says connected while the keys differ" % node)
        errs = [e for _, _, e in rows_seen[node]]
        if n.admin[node] is not None and "refused_key" not in errs:
            problems.append("the %s row never showed last_error refused_key (labels seen %s)" % (node, sorted(set(str(e) for e in errs))))
    # the hotspot's refusal-time rows: connected is never seen after the first sample
    # fix: the old key back
    n1.write_key(n.key_path, old.value)
    n.key_changed = False
    with lan(n) as ns:
        t_fix = time.monotonic()
        n.control.hup()
        event(n)
        rel, more = resume_both_ways(ns, t_fix, n1.RELINK_FIX_S, "h-fix")
        problems += more
        rows_after = {node: wait_row(n, node, connected, 15.0)[0] for node in ("central", "hotspot")}
    event(n)
    after = hubs(n)
    mv = {"central": n1.rebase(before["central"], after["central"]), "hotspot": n1.delta(before["hotspot"], after["hotspot"])}
    # the central edge was restarted twice on purpose (its series count from the last restart): datagrams of the old session may reach the new
    # one (unknown_session), nothing else may move
    problems += counters_problems(mv, (1, 1), allow_restarts=("dv-edge-link",), allow_rx=("unknown_session",), ends={"hotspot": {"revoked": 1}})
    if n1.linkobs_on():
        hints = mv_mid["hotspot"].get("hs.auth", 0)
        if not 1 <= hints <= window / 5.0 + 1:
            problems.append("LINKOBS1: the hotspot counted %g Reject{auth} hints in %.0f s (once per attempt: wanted 1..%d, an attempt lasts 5 s)" % (hints, window, window / 5 + 1))
    for node in ("central", "hotspot"):
        if mv[node].get("dgram.rx.unknown_session"):
            note(n, case, "RECORD", "%s: dgram rx unknown_session +%g (datagrams of the session the restarted edge no longer knows)" % (node, mv[node]["dgram.rx.unknown_session"]))
    problems += floor_free(n, after, (("central", "C"), ("hotspot", "B")))
    for node, row in rows_after.items():
        if row is not None and row.get("last_error") not in (None, ""):
            problems.append("%s row keeps last_error %s after the key was fixed" % (node, row.get("last_error")))
    if n1.linkfix2_on():
        problems += end_cleared(n, "hotspot", "h")
    note(n, case, "RECORD", "key fixed: first probe heard %s s after the fix; handshakes moved central %s / hotspot %s" % (
        None if rel is None else round(rel, 2), {k: v for k, v in mv["central"].items() if k.startswith("hs.")}, {k: v for k, v in mv["hotspot"].items() if k.startswith("hs.")}))
    finish(n, case, problems)


# ---------------------------------------------------------------- NODE1g-j: ten fault cycles

def edge_state(n):
    """(pids of every process of the central node, threads and fds of its dv-edge-link) from the read-only proc-stat."""
    procs = n.control.proc_list("xlxd-sut")
    sample = parse_procstat_out(n.control.proc_stat("xlxd-sut", "dv-edge-link"))
    return sorted(procs), sample.threads, sample.fds


def test_node1g_j_ten_fault_cycles_leave_nothing_stale(n1g):
    """Ten faults one after the other (four blackouts of 8 s, three NAT rebindings of the port, two of the address, one delay of 3 s on and off):
    the link is carrying overs after each, and at the end the central node's processes are the same pids, dv-edge-link has the same thread and
    fd count, no child restarted, one link session on each hub, the floors are free and the replay / unknown-session counters did not move."""
    n = n1g
    case = "j"
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    pids0, threads0, fds0 = edge_state(n)
    problems = []
    plan = ["blackout"] * 4 + ["port"] * 3 + ["address"] * 2 + ["delay"]
    with lan(n) as ns:
        for i, kind in enumerate(plan):
            t_ref = time.monotonic()
            if kind == "blackout":
                n.wan.blackhole(True)
                idle(ns.all, 8.0)
                n.wan.blackhole(False)
            elif kind == "port":
                n.wan.mode("symmetric")
                idle(ns.all, 4.0)
                n.wan.mode("port_restricted")
            elif kind == "address":
                n.wan.readdress(True)
                idle(ns.all, 4.0)
                n.wan.readdress(False)
            else:
                n.control.netem("wan", n1.DELAY_ARGS)
                idle(ns.all, 3.0)
                n.control.netem("wan", "off")
            event(n)
            t_ref = time.monotonic()
            idle(ns.all, 6.0)                                   # the next keepalive after a rebinding is at most 5 s away
            rel, more = resume_both_ways(ns, t_ref, 8.0, "j%d" % i)
            problems += ["cycle %d (%s): %s" % (i, kind, p) for p in more]
    event(n)
    after = hubs(n)
    pids1, threads1, fds1 = edge_state(n)
    if pids1 != pids0:
        problems.append("the central node's processes changed: %s -> %s" % (pids0, pids1))
    if threads1 != threads0:
        problems.append("dv-edge-link threads %d -> %d across ten cycles" % (threads0, threads1))
    if fds1 != fds0:
        problems.append("dv-edge-link open fds %d -> %d across ten cycles" % (fds0, fds1))
    mv = moved(before, after)
    problems += counters_problems(mv, (0, 0))
    problems += floor_free(n, after, (("central", "C"), ("hotspot", "B")))
    for node in ("central", "hotspot"):
        if after[node].get("sessions", 0.0) != 1.0:
            problems.append("%s hub shows %g link sessions, wanted 1" % (node, after[node].get("sessions", 0.0)))
        for key in ("dgram.rx.replay",):
            if mv[node].get(key):
                note(n, case, "RECORD", "%s %s moved by %g (a reordered datagram of a new source: allowed)" % (node, key, mv[node][key]))
    note(n, case, "RECORD", "plan %s; central pids %d -> %d, dv-edge-link threads %d -> %d, fds %d -> %d; moved %s" % (
        plan, len(pids0), len(pids1), threads0, threads1, fds0, fds1, mv))
    finish(n, case, problems)


# ---------------------------------------------------------------- NODE1g-k, -r, -s: LINKOBS1 (image wip-4732c43, DV-Router 06fc9c55)

def rw_config(n):
    """The central node's rendered config of this run when it is replaceable (DVT_SUT_CONFIG_RW=1), else a skip with the reason."""
    path = n.out / "xlxd-config" / "xlxd-sut" / "rw" / "dv-router.toml"
    if not path.is_file():
        pytest.skip("the central node's config is not replaceable: run with DVT_SUT_CONFIG_RW=1 (the case edits the run's own rendered copy and puts it back)")
    return path


def inject(ip, port, payloads):
    """One datagram per payload from a throw-away socket of the tester (its address on the network of `ip`): a source the receiving socket does not expect."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for data in payloads:
            s.sendto(data, (ip, port))
            time.sleep(0.05)


def test_node1g_k_linkobs1_counters(n1g):
    """LINKOBS1 counters, behind DVT_LINKOBS1=on (an older image skips with the reason): every new series exists on both hubs
    (`dv_edge_link_session_ends_total` for timeout, bye, replaced and revoked, `handshakes{result=unknown_session}`, `datagrams{dir=rx,result=bad_source}`),
    a datagram from a source that is not the central node reaches the hotspot's uplink socket 3 times and is counted `bad_source` exactly 3 times with the link
    unchanged, and 3 records for a session id the central node does not know are counted `datagrams{unknown_session}` 3 times and answered with at least one
    stateless `Reject{unknown_session}` (`handshakes{unknown_session}`). The session ends by reason are asserted in d (timeout), h and r (revoked) and s (bye);
    the Reject counter of the hotspot in h and r."""
    n = n1g
    if not n1.linkobs_on():
        pytest.skip(n1.LINKOBS_REASON)
    case = "k"
    problems = []
    rows, ok = both_connected(n, 30.0)
    first = {"central": n1.counters(n.control.metrics("xlxd-sut")), "hotspot": n1.counters(n.control.metrics("dvr-peer"))}
    wanted = ["se.%s" % r for r in n1.SE_REASONS] + ["hs.unknown_session", "dgram.rx.bad_source"]
    for node, c in first.items():
        absent = [k for k in wanted if k not in c]
        if absent:
            problems.append("%s hub: the LINKOBS1 series %s are not exposed (every series exists at zero)" % (node, absent))
    before = hubs(n)
    port = n.wan.mappings()[0]["browser_port"]
    inject(n.ip_h, port, [bytes([40 + i]) * 64 for i in range(3)])
    session = bytes([7, 7, 7, 7])
    inject(n.ip_c, n1.LINK_PORT, [rfpos1.build_record(23, int.from_bytes(session, "little") + i, 1, b"") for i in range(3)])
    event(n)
    after = hubs(n)
    mv = moved(before, after)
    if mv["hotspot"].get("dgram.rx.bad_source") != 3:
        problems.append("hotspot: datagrams{rx,bad_source} moved by %g after 3 datagrams from another source, wanted exactly 3" % mv["hotspot"].get("dgram.rx.bad_source", 0))
    if mv["central"].get("dgram.rx.unknown_session") != 3:
        problems.append("central: datagrams{rx,unknown_session} moved by %g after 3 records of an unknown session, wanted exactly 3" % mv["central"].get("dgram.rx.unknown_session", 0))
    if mv["central"].get("hs.unknown_session", 0) < 1:
        problems.append("central: handshakes{unknown_session} (the stateless Reject hint) did not move")
    for node in ("central", "hotspot"):
        if after[node].get("sessions") != 1.0:
            problems.append("%s hub shows %s link sessions after the injections, wanted 1" % (node, after[node].get("sessions")))
        problems += ["%s: %s" % (node, p) for p in n1.ends_problems(mv[node])]
    for node in ("central", "hotspot"):
        row = get_row(n, node)
        if row is not None and not connected(row):
            problems.append("%s row is %s after the injections" % (node, row.get("state")))
    note(n, case, "RECORD", "moved %s" % {node: {k: v for k, v in m.items() if k.startswith(("dgram.", "hs.", "se."))} for node, m in mv.items()})
    finish(n, case, problems)


def test_node1g_r_revoked_account(n1g):
    """Behind DVT_LINKOBS1=on and DVT_SUT_CONFIG_RW=1: the hotspot's account row is removed from the central node's config and the node reloads (`hup xlxd-sut`;
    a second account row keeps dv-edge-link running). The central node sends Bye{revoked}: the hotspot ends its session with reason `revoked`
    (`dv_edge_link_session_ends_total{reason=revoked}` +1, nothing else), its row shows last_error revoked, it never connects again, and it backs off: the central
    node's Reject{auth} rate stays at most 1.1 per second (the limit of case h) and the hotspot counts at most one Reject hint per 5 s attempt. The row put back
    (and a reload): the link is up within 10 s, one handshake each side, overs resume both ways."""
    n = n1g
    if not n1.linkobs_on():
        pytest.skip(n1.LINKOBS_REASON)
    path = rw_config(n)
    case = "r"
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    problems = []
    original = path.read_text(encoding="utf-8")
    n.rw_restore = original
    path.write_text(n1.remove_account(original, n1.ACCOUNT), encoding="utf-8")
    sampler = RowSampler(n, "hotspot", 10.0).start()
    n.control.hup()
    event(n)
    labels = sampler.finish()
    note(n, case, "RECORD", "hotspot row right after the reload (state, last_error): %s" % labels[:40])
    if n1.linkfix2_on():
        problems += n1.end_reason_problems(sampler.full, "r")
    elif "revoked" not in [e for _, e in labels]:
        problems.append("the hotspot row never showed last_error revoked (labels seen %s)" % sorted(set(str(e) for _, e in labels)))
    window, t_start, samples, seen, ends_seen = 60.0, time.monotonic(), [], [], []
    with lan(n) as ns:
        k = 0
        while time.monotonic() - t_start < window:
            samples.append((time.monotonic(), n1.counters(n.control.metrics("xlxd-sut")).get("hs.auth", 0.0)))
            if k % 4 == 0:
                row = get_row(n, "hotspot")
                if row is not None:
                    seen.append((round(time.monotonic() - t_start, 1), row.get("state"), row.get("last_error")))
                    ends_seen.append((row.get("state"), row.get("last_error"), n1.row_end_reason(row)))
            k += 1
            tick(ns.all)
        mid = hubs(n, quiet=False)
        t_dead, sent = probe_until_heard(ns, ns.hc, ns.lc, 3.0, "r-dead")
        if t_dead is not None:
            problems.append("an over crossed the link after the account was revoked")
    if n1.linkfix2_on():
        # until the next successful handshake the row still says why the last session ended, while it retries and is refused (every 4th round of the window)
        problems += n1.end_reason_problems(ends_seen, "r window")
    rate = n1.refusal_rate(samples)
    note(n, case, "RECORD", "revoked at +0 s; central Reject{auth} rate %s; hotspot rows %s" % (
        None if rate is None else "%.3f per s over %.0f s (%g)" % rate, seen[::2]))
    if rate is not None and rate[0] > n1.REFUSAL_RATE_MAX:
        problems.append("after the revocation the central node counted Reject{auth} at %.3f per second, wanted at most %.1f" % (rate[0], n1.REFUSAL_RATE_MAX))
    mv_mid = {"central": n1.rebase(before["central"], mid["central"]), "hotspot": n1.delta(before["hotspot"], mid["hotspot"])}
    problems += ["hotspot: %s" % p for p in n1.ends_problems(mv_mid["hotspot"], {"revoked": 1})]
    if n1.linkfix2_on():                                  # the admin status view against the hub's counters
        problems += n1.end_vs_hub_problems("r", next((e for _, _, e in reversed(ends_seen) if e), None), mv_mid["hotspot"])
    hints = mv_mid["hotspot"].get("hs.auth", 0)
    if not 1 <= hints <= window / 5.0 + 1:
        problems.append("the hotspot counted %g Reject{auth} hints in %.0f s (once per attempt: wanted 1..%d)" % (hints, window, window / 5 + 1))
    if mv_mid["central"].get("hs.accepted", 0):
        problems.append("the central node accepted a session of the revoked account")
    for node in ("central", "hotspot"):
        if mid[node].get("sessions", 1.0) != 0.0:
            problems.append("%s hub shows %g link session(s) after the revocation" % (node, mid[node].get("sessions", 0.0)))
    if any(s == "connected" for _, s, _ in seen):
        problems.append("the hotspot row says connected while its account is revoked")
    # the row back, reload, the link comes up again
    path.write_text(original, encoding="utf-8")
    n.rw_restore = None
    with lan(n) as ns:
        t_fix = time.monotonic()
        n.control.hup()
        event(n)
        rel, more = resume_both_ways(ns, t_fix, n1.RELINK_FIX_S, "r-fix")
        problems += more
    event(n)
    after = hubs(n)
    mv = {"central": n1.rebase(before["central"], after["central"]), "hotspot": n1.delta(before["hotspot"], after["hotspot"])}
    problems += counters_problems(mv, (1, 1), allow_restarts=("dv-edge-link",), allow_rx=("unknown_session",), ends={"hotspot": {"revoked": 1}})
    problems += floor_free(n, after, (("central", "C"), ("hotspot", "B")))
    if n1.linkfix2_on():
        problems += end_cleared(n, "hotspot", "r")
    note(n, case, "RECORD", "row put back: first probe heard %s s after the reload; hotspot moved %s" % (
        None if rel is None else round(rel, 2), {k: v for k, v in mv["hotspot"].items() if k.startswith(("hs.", "se."))}))
    finish(n, case, problems)


def test_node1g_s_reload_with_the_same_key_is_a_bye(n1g):
    """Behind DVT_LINKOBS1=on and DVT_SUT_CONFIG_RW=1: only the secret-file PATH of the hotspot's account row changes (the same key in another file): an ordinary
    reload, Bye{reload}, no revocation. The hotspot counts one session end `bye` (not `revoked`), reconnects within one backoff (10 s) with the same key and
    overs resume; the same again for the way back."""
    n = n1g
    if not n1.linkobs_on():
        pytest.skip(n1.LINKOBS_REASON)
    path = rw_config(n)
    case = "s"
    rows, ok = both_connected(n, 30.0)
    before = hubs(n)
    problems = []
    original = path.read_text(encoding="utf-8")
    other = n.key_path.with_name("hs1b.key")
    n1.write_key(other, n1.read_key(n.key_path))                        # the same key in a second file (scanned for by run.sh like the others)
    n.rw_restore = original
    rels = []
    for step, text in (("to hs1b.key", n1.set_account_secret_file(original, n1.ACCOUNT, "/etc/dv-router/link-secrets/hs1b.key")), ("back", original)):
        path.write_text(text, encoding="utf-8")
        with lan(n) as ns:                                      # the clients are linked BEFORE the reload: the probes start with it
            t0 = time.monotonic()
            sampler = RowSampler(n, "hotspot", 8.0).start()
            n.control.hup()
            event(n)
            rel, more = resume_both_ways(ns, t0, n1.RELINK_S, "s-%s" % step.split()[0])
            problems += ["%s: %s" % (step, p) for p in more]
            labels = sampler.finish()
        if n1.linkfix2_on():
            # `bye` can only be on the row between the Bye and the end of the relink (0.5 s): any sample in between must show it, and when no 0.25 s sample lands there it is
            # recorded, not failed; after the relink the next handshake has cleared it
            between = [e for _, _, e in sampler.full if e is not None]
            if between and set(between) != {"bye"}:
                problems.append("LINKFIX2: %s: the hotspot row showed last_session_end %s after Bye{reload} (wanted bye)" % (step, sorted(set(between))))
            if not between:
                note(n, case, "RECORD", "%s: no 0.25 s sample landed between the Bye and the relink (0.5 s): last_session_end bye was not seen" % step)
            problems += end_cleared(n, "hotspot", "s " + step)
        note(n, case, "RECORD", "%s: hotspot row after the reload (state, last_error): %s" % (step, labels[:30]))
        if "revoked" in [e for _, e in labels]:
            problems.append("%s: the hotspot row shows revoked although the key did not change" % step)
        rels.append(rel)
    n.rw_restore = None
    event(n)
    after = hubs(n)
    mv = {"central": n1.rebase(before["central"], after["central"]), "hotspot": n1.delta(before["hotspot"], after["hotspot"])}
    problems += counters_problems(mv, (2, 1), allow_restarts=("dv-edge-link",), allow_rx=("unknown_session",), ends={"hotspot": {"bye": 2}})
    problems += floor_free(n, after, (("central", "C"), ("hotspot", "B")))
    note(n, case, "RECORD", "two reloads with the same key: first probe heard %s s after each; hotspot moved %s" % (
        [None if r is None else round(r, 2) for r in rels], {k: v for k, v in mv["hotspot"].items() if k.startswith(("hs.", "se."))}))
    finish(n, case, problems)
