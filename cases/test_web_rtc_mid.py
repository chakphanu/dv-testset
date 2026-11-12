# SPDX-License-Identifier: MIT
"""FLK10 (DV-Router 98c88b2, image wip-bd3ceb9): dv-edge-webrtc refuses a WebRTC offer whose media sections do not each carry exactly one
usable `a=mid`. WRITTEN OFFLINE; NOT RUN. docs/web-mid-cases.md has the rule, the run command and the audit of the rig's own SDP.

The offers are hand-written (dvtest/sdp_offer.py) and posted by an HTTPS client (dvtest/web.py) to POST /api/v1/rtc/offer as a logged-in user,
the route a browser uses; no browser is needed. The rule (peer.rs `check_offer_mids`): per media section exactly one `a=mid:` line, nonempty,
at most 16 bytes, graphic ASCII only, different from the previous section's. A violation is the existing semantic refusal: HTTP 403, problem
code `forbidden`, detail `WebRTC offer denied`, no Retry-After; it is neither malformed signalling nor a capacity refusal.

  0  precondition (fixture)                       a well-formed hand-written offer (mid "audio") is admitted (200 with an answer and a sess_id):
                                                  otherwise every refusal below could be the room or the offer text, not the mid
  a  test_refused_offer[no_mid]                   an audio section without `a=mid`                              403 `forbidden`
  b  test_refused_offer[duplicate_mid]            audio and application both `a=mid:audio`                      403 `forbidden`
  c  test_refused_offer[empty_mid]                `a=mid:` with no value                                        403 `forbidden`
  d  test_refused_offer[over_long_mid]            a 17 byte mid (one over the limit)                            403 `forbidden`
     test_refused_offer[second_section_without_mid | two_mids_in_one_section | mid_with_a_space]   variations of the same rule
  e  test_mid_at_the_limit_is_not_refused         a 16 byte mid is admitted: it must NOT get the SDP refusal    200
  f  test_mid_refusals_move_no_refusal_counter    the refused offers above move no reason of dv_edge_webrtc_offer_refused_total nor of
                                                  dv_web_rtc_refused_total (both are capacity-only reason sets; the image says so)

Switch: every case skips naming DVT_FLK10=on without it (passed to the tester by rig/compose.yml). Needs `--profile web` and a USRP dynamic hub
(DVT_USRP_DYNAMIC=1) so that the Listen room V is served: an unserved room is also 403 `forbidden`, which is why the precondition exists.
The status and code are asserted; a different detail text is a web.finding(), not a failure. The counters are published about every 10 s and cached
by the hub: dvtest.refusals.settled waits for two equal reads.
"""
import time

import pytest

from dvtest import refusals, sdp_offer as so, web

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.skipif(not so.switch_on(), reason=so.skip_reason())]

ROOM = "V"                      # a served module of the USRP dynamic hub (the WEBSID1 / WEBSTALL1 cases use it too)
CALLSIGN = "N0MID"
OFFER_GAP_S = 0.7               # the per-address join gate is 1/s with a burst of 5 (cases/test_web_rtc_refusals.py): offers spaced like this fit


def note(text):
    print("[RECORD] FLK10: %s" % text)


def post_offer(client, sdp):
    """POST /api/v1/rtc/offer {sdp, room}; the facts of the answer (dvtest.sdp_offer.summarize). Spaced from the next offer."""
    resp = client.request("POST", so.OFFER_PATH, json_body={"sdp": sdp, "room": ROOM}, pace=False)
    time.sleep(OFFER_GAP_S)
    return so.summarize(resp.status, resp.headers, resp.body)


def close_circuit(client, answer):
    """End an admitted circuit (the lease would end it anyway); the cap is eight per login."""
    sess = (answer or {}).get("sess_id")
    if not sess:
        return
    try:
        client.request("POST", so.CLOSE_PATH, json_body={"sess_id": sess}, pace=False)
    except web.WebError:
        pass


@pytest.fixture(scope="module")
def mid_client(web_user):
    return web_user(CALLSIGN).client


@pytest.fixture(scope="module")
def baseline(mid_client):
    """Precondition: the plain hand-written offer is admitted, so a refusal below is about the mid and nothing else."""
    name, sdp = so.control_offers()[0]
    assert name == "plain"
    assert not so.mid_problems(sdp), "the plain control offer breaks the mid rule itself: %s" % so.mid_problems(sdp)
    answer = post_offer(mid_client, sdp)
    close_circuit(mid_client, answer)
    note("plain offer (mid 'audio'): status %s, code %s, detail %r" % (answer["status"], answer["code"], answer["detail"]))
    if answer["status"] != 200 or not answer["sess_id"]:
        pytest.fail("the well-formed hand-written offer was not admitted (HTTP %s, code %s, detail %r): the room %s is not served "
                    "(DVT_USRP_DYNAMIC=1?) or the offer text is not accepted, so no refusal below would be about the mid"
                    % (answer["status"], answer["code"], answer["detail"], ROOM))
    return answer


@pytest.mark.parametrize("name,sdp", so.refused_offers(), ids=[n for n, _ in so.refused_offers()])
def test_refused_offer(name, sdp, mid_client, baseline):
    problems = so.mid_problems(sdp)
    assert problems, "%s: the offer does not break the mid rule (a bug in the case): %s" % (name, sdp)
    answer = post_offer(mid_client, sdp)
    close_circuit(mid_client, answer)                        # only if it was wrongly admitted
    note("%s (%s): status %s, code %s, retry-after %s, detail %r" % (name, "; ".join(problems), answer["status"], answer["code"],
                                                                    answer["retry_after"], answer["detail"]))
    bad = so.refusal_problems(name, answer)
    assert not bad, "; ".join(bad)
    if not so.detail_matches(answer):
        web.finding("FLK10 %s: the refusal says %r, DV-Router 98c88b2 documents %r (web.md transport, status.md WebRTC offer contract)"
                    % (name, answer["detail"], so.REFUSAL_DETAIL))


def test_mid_at_the_limit_is_not_refused(mid_client, baseline):
    name, sdp = so.control_offers()[1]
    assert name == "at_limit" and len(so.mid_values(sdp)[0][0]) == so.MID_MAX
    assert not so.mid_problems(sdp)
    answer = post_offer(mid_client, sdp)
    close_circuit(mid_client, answer)
    note("mid of %d bytes: status %s, code %s, detail %r" % (so.MID_MAX, answer["status"], answer["code"], answer["detail"]))
    assert not so.is_sdp_refusal(answer), "a mid of exactly %d bytes got the SDP refusal (HTTP %s %s, %r)" % (
        so.MID_MAX, answer["status"], answer["code"], answer["detail"])
    assert answer["status"] == 200 and answer["sess_id"], "a mid of exactly %d bytes was not admitted: HTTP %s, code %s, detail %r" % (
        so.MID_MAX, answer["status"], answer["code"], answer["detail"])


def test_mid_refusals_move_no_refusal_counter(mid_client, baseline, rig_control):
    before, _ = refusals.settled(lambda: refusals.read(rig_control))
    shape = refusals.shape_problems(before)
    assert not shape, "before the offers: %s" % "; ".join(shape)
    for name, sdp in so.refused_offers():
        answer = post_offer(mid_client, sdp)
        close_circuit(mid_client, answer)
        bad = so.refusal_problems(name, answer)
        assert not bad, "; ".join(bad)
    after, ok = refusals.settled(lambda: refusals.read(rig_control))
    rec = refusals.record(before, after)
    note("counters after %d refused offers: %s (settled=%s)" % (len(so.refused_offers()), refusals.line(rec), ok))
    bad = so.counter_problems(before, after)
    assert not bad, "; ".join(bad)
