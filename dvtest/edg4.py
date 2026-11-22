# SPDX-License-Identifier: MIT
"""What a DV-Router image with EDG3 / EDG4b / EDG4c lets a black-box case assert (DV-Router 5abe7510, merged in bc9af2b1; docs design/web.md 1147-1241 (EDG4, EDG4b, ICE1c), 1328-1333, 1367-1394
(EDG3), design/status.md 872-877 and 1027-1036; image dv-router:wip-f1f3777).

Switch: DVT_EDG4=on asserts (strict); off (the default) skips the cases with a reason.

  EDG3   one ordered reliable DataChannel `ctl` per peer; any other open is refused: an unknown label (`unknown_label`), a second `ctl` (`channel_limit`), an unordered / unreliable one
         (`invalid_config`), one during a pending close (`closing`). The FIRST refusal resets only that SCTP stream (the peer, `ctl` and media stay); the SECOND refusal over the peer's
         lifetime closes the whole association cleanly (log `reason=channel_limit`). Counters: dv_edge_webrtc_channel_refused_total{reason} (+1 per refusal),
         dv_edge_webrtc_channel_closed_total{reason} (+1 for a reset stream, 0 for the refusal that closes the association), dv_edge_webrtc_channel_limit_closes_total.
  EDG4b  a validated source (an integrity-checked Binding success for a transaction outside the 32-entry replay window) stays valid while fresh signed checks or replay-protected
         traffic arrive from that same source; 30 s without either expires it (the peer lives on when another validated source is fresh):
         dv_edge_webrtc_ice_source_expired_total (no labels, once per expired source); a non-STUN datagram from an expired / unknown source is counted
         dv_edge_webrtc_unrouted_total{reason=no_source|wrong_peer}.
  EDG4c  before the DTLS handshake completes, non-STUN input is accepted only from the SELECTED remote (selected by a signed USE-CANDIDATE check; a strictly higher priority nomination
         switches it): a plaintext fatal alert from another validated source is dropped, counted `unrouted{wrong_peer}`, and the peer survives; after the handshake any validated source
         may send media and control. The handshake switch itself (DTLS flight over the alternate pair) is not reproducible from outside: unit tests only.

Pure and stdlib only (cases/unit/test_edg4_lib.py).
"""
import os
import re
from collections import namedtuple

REFUSED = "dv_edge_webrtc_channel_refused_total"
CLOSED = "dv_edge_webrtc_channel_closed_total"
LIMIT_CLOSES = "dv_edge_webrtc_channel_limit_closes_total"
EXPIRED = "dv_edge_webrtc_ice_source_expired_total"
UNROUTED = "dv_edge_webrtc_unrouted_total"
LISTENERS = "dv_edge_webrtc_listeners"
CHANNEL_REASONS = ("unknown_label", "channel_limit", "invalid_config", "closing")
SOURCE_FRESH_S = 30.0
PUBLISH_S = 10.0                                     # the edge publishes its counters about every 10 s, the hub caches the record
SETTLE_S = 60.0


def strict_on(environ=None):
    """DVT_EDG4=on: the image has EDG3 / EDG4b / EDG4c."""
    return (environ if environ is not None else os.environ).get("DVT_EDG4", "").lower() in ("on", "1", "true", "yes")


Counters = namedtuple("Counters", "refused closed limit_closes expired unrouted listeners")


def total(samples, name, **labels):
    return sum(s.value for s in samples if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()))


def counters(samples):
    """The EDG counters of a hub read, each summed over processes; `refused` and `closed` are dicts by reason, `unrouted` a dict by reason."""
    return Counters(
        {r: total(samples, REFUSED, reason=r) for r in CHANNEL_REASONS},
        {r: total(samples, CLOSED, reason=r) for r in CHANNEL_REASONS},
        total(samples, LIMIT_CLOSES), total(samples, EXPIRED),
        {r: total(samples, UNROUTED, reason=r) for r in ("no_source", "wrong_peer")},
        total(samples, LISTENERS))


def families_present(samples):
    """True when the hub exports the EDG3 / EDG4b series at all (an image before 5abe7510 has none)."""
    return any(s.name in (REFUSED, CLOSED, LIMIT_CLOSES, EXPIRED) for s in samples)


def diff_map(after, before):
    return {k: after[k] - before[k] for k in after}


def moved(change):
    """The non-zero entries of a Counters delta (dict view), for printing."""
    out = {}
    for field, value in zip(Counters._fields, change):
        if isinstance(value, dict):
            nz = {k: v for k, v in value.items() if v}
            if nz:
                out[field] = nz
        elif value:
            out[field] = value
    return out


# ================================================================ SDP facts a tester needs for the signed checks

def ice_credentials(sdp):
    """(ufrag, pwd) of the first media section of an SDP text, or (None, None)."""
    ufrag = re.search(r"^a=ice-ufrag:(\S+)", sdp or "", re.M)
    pwd = re.search(r"^a=ice-pwd:(\S+)", sdp or "", re.M)
    return (ufrag.group(1) if ufrag else None, pwd.group(1) if pwd else None)


def host_candidates(sdp):
    """[(ip, port)] of the UDP host candidates of an SDP text, in order."""
    out = []
    for m in re.finditer(r"^a=candidate:\S+ \d+ udp \d+ (\S+) (\d+) typ host", sdp or "", re.M):
        out.append((m.group(1), int(m.group(2))))
    return out


def stun_username(edge_ufrag, client_ufrag):
    """The USERNAME of a request TO the edge: the receiver's ufrag first (RFC 8445 7.2.2)."""
    return "%s:%s" % (edge_ufrag, client_ufrag)


def expiry_window(t_last_fresh, now):
    """Seconds the source has been silent as of `now` and whether the 30 s rule says it expired."""
    silent = now - t_last_fresh
    return silent, silent >= SOURCE_FRESH_S


def judge_expiry(before, after, silent_s, margin_s=PUBLISH_S + 5.0):
    """Problems of an expiry observation: `before` / `after` are the `expired` totals around a silent validated source. The total must rise by exactly 1 once the source was silent for
    at least SOURCE_FRESH_S + margin; it must not rise before SOURCE_FRESH_S - 5 (the counter is published late, never early)."""
    rise = after - before
    problems = []
    if silent_s < SOURCE_FRESH_S - 5.0 and rise:
        problems.append("the expired counter rose by %g after only %.0f s of silence (rule: %.0f s)" % (rise, silent_s, SOURCE_FRESH_S))
    if silent_s >= SOURCE_FRESH_S + margin_s and rise != 1:
        problems.append("the expired counter rose by %g after %.0f s of silence, want exactly 1 (once per expired source)" % (rise, silent_s))
    return problems
