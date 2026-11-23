# SPDX-License-Identifier: MIT
"""GUARDID2 (DV-Router 9a8d1cb4, image dv-router:wip-3f142cd): the guard source identity crosses the router link.

What the product says (docs/design/floor-control.md "Guard penalties", link.md "Appendix A", media-format.md): an unmapped P25 talker gets the shared
placeholder callsign P31672 (`P%05u` of the talkgroup) and the RTS flag RTS_SOURCE_ID; the guard then keys on the decimal SOURCE ID, so two
unmapped talkers behind the one placeholder never strike each other. A node link relays the flag as bit 0 of the `carried` u16 of its StreamStart
record (the same bit as in the fabric StreamStart; no profile has id 0, so the bit is outside every profile mask) with NO bytes added; the
receiving link edge then states RTS_SOURCE_ID, so node 2's guard keys on the same source id as node 1's. A mapped callsign has the flag clear and
keeps its callsign key. Radio FM ports, USRP rows and AllStar links share one guard identity by design (floor-control.md): not tested.

This module is pure (stdlib only): the wire judge over the events of `dvtest.rfpos1.LinkRelay`, the heard-share of a P25 listener, and the
hub counter deltas. Cases: cases/test_guardid2.py; unit tests: cases/unit/test_guardid2_lib.py; document: docs/guardid2-cases.md.
"""
import os

from dvtest import metrics

SWITCH = "DVT_GUARDID2"
ROOM = "G"                         # the room both nodes map (the node link pair of rfpos1-link-a / -b.toml) and the P25 server module
TG = 31672                         # edges.p25 server_tg of rig/config/dv-router/guardid2-p25.toml
SHARED = "P31672"                  # the placeholder callsign every unmapped P25 talker of TG 31672 shares
FLAG = 0x0001                      # bit 0 of StreamStart `carried`
FIXED_STREAMSTART = 62             # type 1 + session 4 + counter 8 + body 33 (up to raw_len) + tag 16: a datagram is this plus raw_len

# the rig's id directory (rig/config/dmrid.dat): mapped ids, and ids that are in no directory
MAPPED = {"M": (1023007, "N0CALL"), "M2": (1023008, "N0TEST")}
UNMAPPED = {"X": 1023961, "Y": 1023962}


def on(environ=None):
    """DVT_GUARDID2=on: the image has GUARDID2 and run.sh links the pair by a node link and serves P25 on both nodes (guardid2-p25.toml)."""
    return (environ if environ is not None else os.environ).get(SWITCH, "").strip().lower() in ("on", "1", "true", "yes")


# ---------------------------------------------------------------- the link wire

def flag_of(carried):
    """True when bit 0 (the GUARDID2 source-id flag) of a StreamStart `carried` word is set."""
    return bool(carried & FLAG)


def companions_of(carried):
    """The companion-profile mask of a `carried` word: bit 0 is the flag, not a profile."""
    return carried & ~FLAG & 0xFFFF


def starts(events, radio_ids=None, since=0):
    """The client-to-server StreamStart records (kind 19) of the relay log, one per link stream id (the first of each), as dicts
    {stream_id, profile, carried, flag, companions, callsign, radio_id, raw_len, size}. `radio_ids` (a collection) keeps only those talkers."""
    out, seen = [], set()
    for ev in events[since:]:
        if ev.get("dir") != "c2s" or ev.get("kind") != 19 or "radio_id" not in ev:
            continue
        if radio_ids is not None and ev["radio_id"] not in radio_ids:
            continue
        if ev["stream_id"] in seen:
            continue
        seen.add(ev["stream_id"])
        out.append({"stream_id": ev["stream_id"], "profile": ev["profile"], "carried": ev["carried"], "flag": flag_of(ev["carried"]),
                    "companions": companions_of(ev["carried"]), "callsign": ev["callsign"], "radio_id": ev["radio_id"],
                    "raw_len": ev["raw_len"], "size": ev["size"]})
    return out


def refused(events, since=0):
    """The server-to-client Refused records (kind 22): [{stream_id, reason}]. A refused link stream is a denial by node 2's room."""
    return [{"stream_id": ev["stream_id"], "reason": ev["reason"]} for ev in events[since:]
            if ev.get("dir") == "s2c" and ev.get("kind") == 22]


def wire_problems(found, radio_id, callsign, flag, want_streams):
    """What the StreamStart records of one talker must show on the link: exactly `want_streams` link streams, each with the flag `flag`, the
    shown callsign `callsign`, the talker's own radio id, and a datagram of FIXED_STREAMSTART + raw_len bytes (no bytes added by the flag).
    `found` is starts(...) already narrowed to the talker. A list of sentences, empty when it holds."""
    out = []
    if len(found) != want_streams:
        out.append("radio id %d: %d link streams seen, wanted %d" % (radio_id, len(found), want_streams))
    for rec in found:
        if rec["flag"] != flag:
            out.append("radio id %d stream %d: StreamStart carried 0x%04x, source-id flag (bit 0) %s, wanted %s" % (
                radio_id, rec["stream_id"], rec["carried"], "set" if rec["flag"] else "clear", "set" if flag else "clear"))
        if rec["callsign"] != callsign:
            out.append("radio id %d stream %d: callsign %r, wanted %r" % (radio_id, rec["stream_id"], rec["callsign"], callsign))
        if rec["size"] != FIXED_STREAMSTART + rec["raw_len"]:
            out.append("radio id %d stream %d: StreamStart datagram of %d bytes with raw_len %d, a record without extra bytes is %d" % (
                radio_id, rec["stream_id"], rec["size"], rec["raw_len"], FIXED_STREAMSTART + rec["raw_len"]))
    return out


def same_size(found_a, found_b):
    """The StreamStart size minus raw_len of two groups must be one constant: the flag adds no bytes. A sentence, or None when it holds."""
    sizes = {r["size"] - r["raw_len"] for r in list(found_a) + list(found_b)}
    if len(sizes) > 1:
        return "StreamStart fixed size differs between flagged and unflagged records: %s" % sorted(sizes)
    return None


# ---------------------------------------------------------------- what a P25 listener heard

def heard(imbe_frames, sent):
    """How many of `sent` (a list of distinct 11-byte frames) a listener's received frames contain, each sent frame counted once."""
    got = set(bytes(f) for f in imbe_frames)
    return sum(1 for f in set(bytes(s) for s in sent) if f in got)


def share(count, total):
    return 0.0 if not total else count / float(total)


def listener_problems(name, got, total, want_whole, who):
    """`got` of `total` frames of the talker `who` reached the listener `name`. want_whole: at least 80 percent must have; else none may.
    A list of sentences, empty when it holds."""
    if want_whole and share(got, total) < 0.8:
        return ["%s heard %d of %d frames of %s (%.0f percent), wanted at least 80" % (name, got, total, who, 100 * share(got, total))]
    if not want_whole and got:
        return ["%s heard %d frames of %s, wanted none (the over was refused)" % (name, got, who)]
    return []


# ---------------------------------------------------------------- hub counters

RTS_FAMILY = "dv_room_rts_total"
DROPS_FAMILY = "dv_edge_drops_total"
GUARD_REASONS = ("deny_guard_block", "guard_blocked")


def counters(text):
    """{name: value} of the counters the cases judge, from a hub scrape: dv_room_rts_total by result (all rooms summed; room G is the only
    one used), dv_edge_drops_total by (proc, reason) for the guard reasons plus every nonzero reason of the proc `p25` and `link`."""
    samples = metrics.parse(text)
    out = {}
    for s in samples:
        if s.name == RTS_FAMILY and "result" in s.labels:
            key = "rts.%s" % s.labels["result"]
            out[key] = out.get(key, 0.0) + s.value
        elif s.name == DROPS_FAMILY and "reason" in s.labels:
            proc = s.labels.get("proc", "")
            if s.labels["reason"] in GUARD_REASONS or (proc in ("p25", "link") and s.value):
                key = "drops.%s.%s" % (proc, s.labels["reason"])
                out[key] = out.get(key, 0.0) + s.value
    return out


def delta(before, after):
    """{name: after - before} of the names that moved (a name missing before is 0)."""
    out = {}
    for name in set(before) | set(after):
        d = after.get(name, 0.0) - before.get(name, 0.0)
        if d:
            out[name] = d
    return out


def guard_denials(moved):
    """Sum of the guard denials in a delta: dv_edge_drops_total reasons deny_guard_block and guard_blocked of every proc."""
    return sum(v for k, v in moved.items() if k.startswith("drops.") and k.rsplit(".", 1)[-1] in GUARD_REASONS)


def counter_problems(node, moved, grants_at_least, denials_at_least=0, denials_exactly=None):
    """Judge the counter delta `moved` of one node: dv_room_rts_total{result=grant} rose by at least `grants_at_least`; the guard denials
    (dv_edge_drops_total deny_guard_block / guard_blocked of any proc) rose by at least `denials_at_least`, or by exactly `denials_exactly`
    when that is not None (node 2 must show none for the unstruck source). A list of sentences, empty when it holds."""
    out = []
    grants = moved.get("rts.grant", 0.0)
    if grants < grants_at_least:
        out.append("%s: dv_room_rts_total grant rose by %g, wanted at least %d" % (node, grants, grants_at_least))
    denied = guard_denials(moved)
    if denials_exactly is not None and denied != denials_exactly:
        out.append("%s: guard denials (deny_guard_block / guard_blocked) rose by %g, wanted %d" % (node, denied, denials_exactly))
    if denials_exactly is None and denied < denials_at_least:
        out.append("%s: guard denials rose by %g, wanted at least %d" % (node, denied, denials_at_least))
    if denials_exactly == 0 and moved.get("rts.deny", 0.0):
        out.append("%s: dv_room_rts_total deny rose by %g, wanted 0" % (node, moved["rts.deny"]))
    return out
