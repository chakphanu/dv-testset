# SPDX-License-Identifier: MIT
"""FLK10: hand-built WebRTC offers for the mid rule of dv-edge-webrtc, and the verdict logic of cases/test_web_rtc_mid.py. Pure logic, stdlib only.

Source (DV-Router, only `git show 98c88b2:<path>`): crates/edge/webrtc/src/peer.rs `check_offer_mids`, docs/design/web.md ("Built" bullet of the
transport), docs/design/status.md (WebRTC offer contract), crates/svc/web/src/rtc/rtc_core/offer/reply.rs (the reply mapping).

The rule, as the black box sees it. Every media section of the offer (at most two: one audio and an optional application section) needs
  * exactly one `a=mid:` line, so a section without one and a section with two are refused;
  * a value that is nonempty, at most MID_MAX (16) bytes long, and made of graphic ASCII only (0x21..0x7e: no space);
  * a value that differs from the mid of the previous section (checked after the stack's own normalisation of punctuation, which this module
    does not model: the cases use identical or plain alphanumeric tokens only).
A violation is the existing semantic offer refusal: the edge answers `denied`, the web service maps it to HTTP 403 problem code `forbidden`,
detail `WebRTC offer denied`, no Retry-After. The edge emits its `stack_error` peer diagnostic. It does NOT count as malformed signalling and it
does NOT move dv_edge_webrtc_offer_refused_total or dv_web_rtc_refused_total (both are capacity-only reason sets). A browser always sends its
mids, so the refusal is not a regression for real clients.
"""
import os

from . import refusals

SWITCH = "DVT_FLK10"
TRUE = ("on", "1", "true", "yes")

OFFER_PATH = "/api/v1/rtc/offer"
CLOSE_PATH = "/api/v1/rtc/close"

MID_MAX = 16
REFUSAL_STATUS = 403
REFUSAL_CODE = "forbidden"
REFUSAL_DETAIL = "WebRTC offer denied"

# a SHA-256 fingerprint of the right shape (the DTLS handshake never runs in these cases, the offer is only admitted or refused)
FINGERPRINT = "sha-256 " + ":".join(["AB"] * 32)
HOST = "192.0.2.2"                  # documentation address of the hand-written offer (RFC 5737)


def switch_on(environ=None):
    """True when DVT_FLK10=on: the image carries FLK10 (DV-Router 98c88b2, image wip-bd3ceb9), so the cases apply."""
    env = os.environ if environ is None else environ
    return str(env.get(SWITCH, "")).strip().lower() in TRUE


def skip_reason():
    return "%s=on not set: the mid cases are for an image with FLK10 (DV-Router 98c88b2, wip-bd3ceb9; docs/web-mid-cases.md)" % SWITCH


# ---------------------------------------------------------------- the offer

def build_offer(mids, ufrag="dvt0"):
    """A hand-written offer (RFC 8866 / 8829 shape, modelled on the edge's own hand-authored fixture): `mids` has one entry per media section
    (1 = audio only, 2 = audio then application), each None (no `a=mid:` line at all), a string (the value, possibly empty) or a tuple of
    strings (several `a=mid:` lines in that one section). The BUNDLE group names the nonempty values as given, so a duplicate stays visible
    in it. PCMU receive-only audio, ICE credentials, one documentation host candidate and the sha-256 fingerprint: with a sane mid the edge
    accepts it (the control cases)."""
    mids = list(mids)
    if not 1 <= len(mids) <= 2:
        raise ValueError("an offer has one or two media sections, got %d" % len(mids))
    lines = ["v=0", "o=- 1 1 IN IP4 %s" % HOST, "s=-", "t=0 0"]
    named = []
    for value in mids:
        for one in ((value,) if not isinstance(value, tuple) else value):
            if one:
                named.append(one)
    if named:
        lines.append("a=group:BUNDLE " + " ".join(named))
    for index, value in enumerate(mids):
        if index == 0:
            lines += ["m=audio 9 UDP/TLS/RTP/SAVPF 0", "c=IN IP4 %s" % HOST]
        else:
            lines += ["m=application 9 UDP/DTLS/SCTP webrtc-datachannel", "c=IN IP4 %s" % HOST]
        values = () if value is None else ((value,) if not isinstance(value, tuple) else value)
        lines += ["a=mid:%s" % one for one in values]
        if index == 0:
            lines += ["a=ice-ufrag:%s" % ufrag, "a=ice-pwd:abcdefghijklmnopqrstuvwxyz012345", "a=fingerprint:%s" % FINGERPRINT,
                      "a=setup:actpass", "a=recvonly", "a=rtcp-mux", "a=rtpmap:0 PCMU/8000",
                      "a=candidate:1 1 udp 2130706431 %s 40000 typ host" % HOST, "a=end-of-candidates"]
        else:
            lines += ["a=sctp-port:5000"]
    return "\r\n".join(lines) + "\r\n"


def mid_values(sdp):
    """[[value, ...], ...] : the `a=mid:` values of each media section of an SDP text, in order (a section without one gives [])."""
    out = []
    for line in sdp.splitlines():
        if line.startswith("m="):
            out.append([])
        elif line.startswith("a=mid:") and out:
            out[-1].append(line[len("a=mid:"):])
    return out


def mid_problems(sdp):
    """What the FLK10 rule would refuse in an SDP text: [] when every media section has exactly one nonempty, graphic ASCII mid of at most
    MID_MAX bytes that differs from the previous section's. The tester-side mirror of check_offer_mids, used to check that rig SDP built or
    rewritten by this repository keeps its mids; it does not model the stack's punctuation normalisation."""
    problems = []
    previous = None
    sections = mid_values(sdp)
    if not sections:
        return ["no media section"]
    for number, values in enumerate(sections):
        if len(values) != 1:
            problems.append("section %d has %d a=mid lines" % (number, len(values)))
            previous = None
            continue
        value = values[0]
        if not value:
            problems.append("section %d has an empty mid" % number)
        elif len(value.encode("ascii", "replace")) > MID_MAX:
            problems.append("section %d mid is %d bytes (limit %d)" % (number, len(value), MID_MAX))
        elif any(not 0x21 <= ord(ch) <= 0x7e for ch in value):
            problems.append("section %d mid has a byte that is not graphic ASCII" % number)
        if previous is not None and value == previous:
            problems.append("section %d repeats the mid of the previous section" % number)
        previous = value
    return problems


# ---------------------------------------------------------------- the offers of the cases

def at_limit_mid():
    return "a" * MID_MAX


def over_long_mid():
    return "a" * (MID_MAX + 1)


def refused_offers():
    """[(name, sdp)] : the offers the FLK10 image must refuse. The four named by the unit first, then variations of the same rule."""
    return [
        ("no_mid", build_offer([None], ufrag="dvn0")),
        ("duplicate_mid", build_offer(["audio", "audio"], ufrag="dvd0")),
        ("empty_mid", build_offer([""], ufrag="dve0")),
        ("over_long_mid", build_offer([over_long_mid()], ufrag="dvl0")),
        ("second_section_without_mid", build_offer(["audio", None], ufrag="dvs0")),
        ("two_mids_in_one_section", build_offer([("audio", "other")], ufrag="dvt1")),
        ("mid_with_a_space", build_offer(["au dio"], ufrag="dvp0")),
    ]


def control_offers():
    """[(name, sdp)] : offers that are well formed. 'plain' is the precondition (the hand-written offer itself is accepted), 'at_limit' is the
    control of the unit: a mid of exactly MID_MAX bytes must not get the SDP refusal."""
    return [
        ("plain", build_offer(["audio"], ufrag="dvc0")),
        ("at_limit", build_offer([at_limit_mid()], ufrag="dvc1")),
    ]


# ---------------------------------------------------------------- the answer

def summarize(status, headers, body):
    """The facts of one POST /api/v1/rtc/offer answer: {"status", "code", "detail", "retry_after", "sess_id"}. `headers` has lower case names,
    `body` is bytes or text; anything that is not a JSON object leaves code, detail and sess_id None."""
    import json
    parsed = None
    try:
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    obj = parsed if isinstance(parsed, dict) else {}
    detail = obj.get("detail") if isinstance(obj.get("detail"), str) else None
    sess = obj.get("sess_id") if isinstance(obj.get("sess_id"), str) else None
    return {"status": status, "code": refusals.problem_code(obj), "detail": detail[:120] if detail else None,
            "retry_after": (headers or {}).get("retry-after"), "sess_id": sess}


def refusal_problems(what, answer):
    """The contract of the SDP refusal (status 403, code `forbidden`, no Retry-After: it is not a capacity refusal). The detail is judged
    separately (detail_matches) and only recorded as a finding."""
    out = []
    if answer.get("status") != REFUSAL_STATUS:
        out.append("%s answered HTTP %s, want %d" % (what, answer.get("status"), REFUSAL_STATUS))
    if answer.get("code") != REFUSAL_CODE:
        out.append("%s problem code %r, want %r" % (what, answer.get("code"), REFUSAL_CODE))
    if answer.get("retry_after") not in (None, ""):
        out.append("%s carries Retry-After %r: a refusal of the offer text is not a capacity refusal" % (what, answer.get("retry_after")))
    return out


def detail_matches(answer):
    return answer.get("detail") == REFUSAL_DETAIL


def is_sdp_refusal(answer):
    """True for the answer a refused mid gets (403 `forbidden`); the control offers must never get it."""
    return answer.get("status") == REFUSAL_STATUS and answer.get("code") == REFUSAL_CODE


def counter_problems(before, after):
    """[] when no reason of either counter family moved between two snapshots (dvtest.refusals.snapshot / read); else a line per moved reason or
    per missing family. Both families are capacity-only reason sets, and the mid refusal is none of their reasons."""
    out = []
    shape = refusals.shape_problems(before)
    if shape:
        return ["before: " + "; ".join(shape)]
    delta = refusals.delta(before, after)
    for fam, family in (("edge", refusals.EDGE_FAMILY), ("web", refusals.WEB_FAMILY)):
        if delta.get(fam) is None:
            out.append("%s is not exposed after the offers" % family)
            continue
        moved = refusals.moved(delta[fam])
        if moved:
            out.append("%s moved %s" % (family, ", ".join("%s %+g" % (r, v) for r, v in sorted(moved.items()))))
    return out
