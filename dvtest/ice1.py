# SPDX-License-Identifier: MIT
"""Pure helpers of the ICE1 case (cases/test_web_nat_ice1.py, docs/web-nat-cases.md "ICE1 address change"): the deadline
of the audio to come back after the client address changed, the analysis of the inbound packet counter of the page, the
restart seen in the SDP, the checks on what the NAT router saw, and readers of the operator client list. No I/O;
unit-tested in cases/unit/test_ice1.py."""
import re

# ICE1 (DV-Router wip-97c669d, e439f62) and ICE1c (the edge fix, open): after a pure NAT rebinding (the page cannot see it) the EDGE
# must follow the browser's new address on the SAME circuit and voice frames reach the listener again within these many seconds of
# the change: 8 s for Chromium and Firefox, 12 s for Playwright WebKit (DV-Router, 2026-11-08). On wip-97c669d all nine browser cases
# fail (about 14 s in Chromium and WebKit, no recovery within 20 s in Firefox); they stay as standing cases until ICE1c lands.
RESUME_BUDGET_S = {"chromium": 8.0, "firefox": 8.0, "webkit": 12.0}   # DV-Router ICE1c: the edge must follow a NAT rebinding within these
RESUME_BUDGET_DEFAULT_S = 12.0

STALL_MIN_S = 0.3         # no new packet for this long = the audio stopped (15 missed packets of 20 ms)
RESUME_PACKETS = 10       # this many new packets after the stall = the audio is back (0.2 s of voice)
STRAY_OLD_PAIR_MAX = 0    # datagrams the SUT may still send to the old address once the audio is back and settled


def resume_budget_s(engine):
    return RESUME_BUDGET_S.get(engine, RESUME_BUDGET_DEFAULT_S)


class InboundTotal:
    """Running total of the packets the page received, over `inbound-rtp` entries: an engine may start a new entry (a new
    id, counters from zero) or reset the counter when the stream restarts, so the total adds the growth of each entry."""

    def __init__(self):
        self.ident, self.last, self.total = None, 0, 0

    def update(self, ident, count):
        count = int(count or 0)
        if ident == self.ident and count >= self.last:
            self.total += count - self.last
        else:
            self.total += count
        self.ident, self.last = ident, count
        return self.total


def resume_analysis(samples, t_change, stall_min_s=STALL_MIN_S, resume_packets=RESUME_PACKETS):
    """How the inbound packet counter behaved after the address change at `t_change`. `samples` is [(time, total packets)]
    in time order (the times in any clock, the same as t_change; the sample at or just before t_change is the reference).

    stalled   the counter did not move for stall_min_s after the change (the audio stopped)
    resumed   the counter moved again by resume_packets (or it never stalled)
    resume_s  seconds from t_change to the sample that showed the audio back (0.0 when it never stalled), None if not
    gap_s     seconds from the last growth before the stall to the audio back; None when it never came back
    stall_start_s  seconds from t_change to the last growth before the stall
    inconclusive   the samples end less than 2 x stall_min_s after t_change and showed no stall: nothing can be said"""
    before = [s for s in samples if s[0] <= t_change]
    after = [s for s in samples if s[0] > t_change]
    out = {"stalled": False, "resumed": False, "resume_s": None, "gap_s": None, "stall_start_s": None, "inconclusive": False,
           "samples": len(after)}
    if not before and not after:
        out["inconclusive"] = True
        return out
    pts = [before[-1]] if before else []
    pts += after
    prev_n = pts[0][1]
    last_growth = t_change
    stall_at = stall_n = None
    for t, n in pts[1:]:
        if n > prev_n:
            if stall_at is not None and n - stall_n >= resume_packets:
                out.update(resumed=True, resume_s=round(t - t_change, 2), gap_s=round(t - stall_at, 2))
                return out
            if stall_at is None:
                last_growth = t
        elif stall_at is None and t - last_growth >= stall_min_s:
            stall_at, stall_n = last_growth, n
            out["stalled"] = True
            out["stall_start_s"] = round(last_growth - t_change, 2)
        prev_n = n
    if stall_at is None:
        if pts[-1][0] - t_change >= 2 * stall_min_s:
            out.update(resumed=True, resume_s=0.0, gap_s=0.0)          # the audio never stopped
        else:
            out["inconclusive"] = True
    return out


# ---------------------------------------------------------------------------------------- the restart in the SDP

def ice_ufrags(sdp):
    """The a=ice-ufrag values of an SDP (sorted, without duplicates). The passwords are never read."""
    return sorted(set(re.findall(r"^a=ice-ufrag:(\S+)", sdp or "", re.M)))


def restart_seen(before, after):
    """before / after: {'local': sdp, 'remote': sdp} of the page's peer (window.__dvtTap.sdp()). An ICE restart gives the
    description a new ufrag (RFC 8445, ICE restarts):{'local', 'remote', 'any'} (booleans)."""
    out = {}
    for side in ("local", "remote"):
        b, a = ice_ufrags((before or {}).get(side)), ice_ufrags((after or {}).get(side))
        out[side] = bool(a) and a != b
    out["any"] = out["local"] or out["remote"]
    return out


# ---------------------------------------------------------------------------------------- what the router saw

def mapping_problems(before, after, old_ip, new_ip):
    """before / after: the router's media_mappings() ([{'seen_ip', ...}]) taken before the change and after the audio came
    back. Before, the SUT must have been seeing the browser at old_ip only; after, at new_ip only (the old flow was deleted
    with the change, a flow to the SUT that still shows the old address is a leftover or the browser used both)."""
    out = []
    seen_before = sorted({r["seen_ip"] for r in before or []})
    seen_after = sorted({r["seen_ip"] for r in after or []})
    if not seen_before:
        out.append("before the change the router held no UDP flow from the browser to the SUT's media port")
    elif seen_before != [old_ip]:
        out.append("before the change the SUT saw the browser at %s, expected only %s" % (seen_before, old_ip))
    if not seen_after:
        out.append("after the change the router holds no UDP flow from the browser to the SUT's media port")
    elif seen_after != [new_ip]:
        out.append("after the change the SUT sees the browser at %s, expected only %s" % (seen_after, new_ip))
    return out


def firsts_evidence(snap):
    """The router sniffer's snapshot (nat.Firsts.snapshot(), the answer of the `firsts` word) reduced to the figures DV-Router
    asked for after the first ICE1 run (ICE1c). All times are seconds after the router started the address change.

      first_udp_new_s        first datagram of any kind browser -> SUT from the NEW mapping (None = none seen)
      first_stun_new_s       first STUN binding request browser -> SUT from the new mapping
      first_sut_to_new_s     first datagram SUT -> browser sent to the new mapping: the edge following the browser
      stale_to_old_per_s     SUT -> browser datagrams still sent to the OLD mapping, per second, for 20 s
    plus the kinds of the firsts, the first STUN response and RTP to the new mapping, the totals and the last stale send
    (when the edge stopped sending to the old mapping). `error` is set when the router had no sniffer (the figures are None)."""
    if not snap:
        return {"error": "no snapshot", "first_udp_new_s": None, "first_stun_new_s": None, "first_sut_to_new_s": None,
                "stale_to_old_per_s": None}
    sides = snap.get("sides") or {}
    new_in, new_out, old_out, old_in = (sides.get(k) or {} for k in ("from_new", "to_new", "to_old", "from_old"))
    return {"error": snap.get("error"),
            "first_udp_new_s": new_in.get("first_s"), "first_udp_new_kind": new_in.get("first_kind"),
            "first_stun_new_s": (new_in.get("first_by_kind") or {}).get("stun-binding-request"),
            "first_sut_to_new_s": new_out.get("first_s"), "first_sut_to_new_kind": new_out.get("first_kind"),
            "first_sut_stun_response_new_s": (new_out.get("first_by_kind") or {}).get("stun-binding-response"),
            "first_sut_rtp_new_s": (new_out.get("first_by_kind") or {}).get("rtp"),
            "stale_to_old_per_s": old_out.get("per_s"), "stale_to_old_total": old_out.get("n"),
            "stale_to_old_last_s": old_out.get("last_s"), "browser_from_old_total": old_in.get("n"),
            "browser_to_sut_new_kinds": new_in.get("kinds"), "sut_to_new_kinds": new_out.get("kinds")}


def stray_problems(first, second, window_s, maximum=STRAY_OLD_PAIR_MAX):
    """first / second: the router's count of datagrams from the SUT's media port to the old (unmapped) address, read at the
    start and the end of a window in which the audio is back and the talker is still on air."""
    grew = second - first
    if grew > maximum:
        return ["the SUT sent %d datagrams to the old address in the %.1f s after the audio was back (allowed %d): the old "
                "candidate pair is still in use" % (grew, window_s, maximum)]
    return []


# ---------------------------------------------------------------------------------------- the page's candidates

def local_candidates(entries):
    """[{'type', 'addr', 'protocol'}] of the local-candidate entries of a getStats list (addr 'ip:port', '?' where the engine
    reports none). The address of a prflx candidate is the one the far end saw the browser at."""
    out = []
    for e in entries or []:
        if e.get("type") != "local-candidate":
            continue
        out.append({"type": e.get("candidateType"), "protocol": e.get("protocol"),
                    "addr": "%s:%s" % (e.get("address", e.get("ip", "?")), e.get("port", "?"))})
    return sorted(out, key=lambda c: (str(c["type"]), c["addr"]))


# ---------------------------------------------------------------------------------------- the operator client list

ADDRESS_WORDS = {"addr", "address", "remote", "ip", "endpoint", "peer", "host"}


def webrtc_rows(items):
    """The rows of GET /api/v1/operator/clients that belong to a WebRTC edge (protocol or edge names it)."""
    return [r for r in items or [] if isinstance(r, dict) and "webrtc" in (str(r.get("protocol", "")) + str(r.get("edge", ""))).lower()]


def address_like(row):
    """{key: value} of the fields of a client row whose name looks like an address (UNVERIFIED: the API names no such field
    in the documents this suite reads; the ops page has an 'Address' column)."""
    out = {}
    for key, value in (row or {}).items():
        words = set(re.split(r"[^a-z0-9]+", str(key).lower()))
        if words & ADDRESS_WORDS and isinstance(value, (str, int)) and not isinstance(value, bool):
            out[key] = str(value)
    return out


def match_row(rows, circuit, callsign):
    """(row, how): the row of the page's circuit: 'circuit' when a row's circuit equals the id the page holds, 'callsign'
    when exactly one row names the listener, 'newest' as the last resort (the largest since_ms), (None, None) without rows."""
    for row in rows:
        if circuit and row.get("circuit") == circuit:
            return row, "circuit"
    named = [r for r in rows if callsign and str(r.get("callsign", "")).upper() == callsign.upper()]
    if len(named) == 1:
        return named[0], "callsign"
    if rows:
        return max(rows, key=lambda r: r.get("since_ms", 0) if isinstance(r.get("since_ms", 0), (int, float)) else 0), "newest"
    return None, None


def new_circuits(before_rows, after_rows):
    """Circuit ids in the after list that were not in the before list (rows with no 'circuit' field are ignored)."""
    seen = {r.get("circuit") for r in before_rows if r.get("circuit") is not None}
    return sorted(str(r["circuit"]) for r in after_rows if r.get("circuit") is not None and r["circuit"] not in seen)
