# SPDX-License-Identifier: MIT
"""DP19 (image 4ac79f26 and later): an APRS status report whose `>` is followed by a four- or six-character Maidenhead locator and a valid symbol-table/code pair
(APRS101 chapter 16, printed pp.81-82) is a POSITION for the privacy policy: dropped for a private base callsign at the controller (denied/13), not gated to APRS-IS by
the RF transit path. Look-alikes (a leading space, a missing or invalid symbol pair, an invalid locator, text that does not start with a space) and the plain status stay
ordinary statuses. Specification: DV-Router data.md 13.4 "Built (DP19)", vectors dvtest/proto/aprs.py LOCATOR_VALID / LOCATOR_LOOKALIKES. The packet monitor half
(worker rows, header-only edge RX, KISS client TX) is OBSPKT1-g in cases/test_obspkt1_monitor.py. Run shapes and commands: docs/dp6c-cases.md. NOT RUN ON A RIG yet.

  DP19-a   RF locator statuses of a private base (position_optout N0OPT, or positions = false with any base): the valid forms are dropped (the optout counter of kind status
           rises), the look-alikes and a plain status are public statuses; a public base's valid locator status is a public status
  DP19-b   RF transit to the private aprsc (dp7-rf-igate.toml): the private base's valid locator statuses are not gated, its look-alikes and a plain status are, with the
           information field unchanged; a public base's locator status is gated
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import dp6c, web  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

import dp6c_support as sp  # noqa: E402
import position_support as ps  # noqa: E402
from test_dp15_positions import aprsc, aprsc_login, hub, rf, shape, web_ctx  # noqa: E402,F401

pytestmark = pytest.mark.rig

CASE = "DP19"
PUBLIC_BASE = "N0LOP"                    # not private in any shape
TRANSIT_WAIT_S = 45.0
TRANSIT_ABSENT_S = 15.0


def private_base(shape):
    """The base callsign whose statuses are private in this run shape (position_optout N0OPT, or positions = false), else a skip."""
    if shape.positions and dp6c.OPT_BASE in shape.optout:
        return dp6c.OPT_BASE
    if not shape.positions:
        return dp6c.LOC_BASE
    pytest.skip("neither position_optout %s nor positions = false is configured: add rig/config/dv-router/dp7-position-optout.toml or dp7-positions-off.toml" % dp6c.OPT_BASE)


def word_of(info):
    """The distinguishing last word of a status information field (it stays in the status text)."""
    return info.lstrip(">").rsplit(" ", 1)[-1]


def send_status(rf, call, info, spend=True):
    sp.send_rf(rf, call, info, kinds=("status",), spend=spend)


def room_history(web_ctx):
    return web.room_messages(web_ctx.reader, dp6c.ROOM)


def test_dp19_a_locator_status_is_dropped_for_a_private_base(rig, target, hub, web_ctx, shape, rf):
    """data.md 13.4 (DP19): the original information bytes are classified before sanitization. The private base's valid locator statuses (four and six characters, lower
    case subsquare, backslash table, with and without text) are denied/13; each look-alike is an ordinary status; a plain status is unchanged; a PUBLIC base's valid locator
    status is a public status. Statuses are 1 per 10 s per base callsign (paced)."""
    case = CASE + "-a"
    base = private_base(shape)
    valid, lookalikes = dict(aprs.LOCATOR_VALID), dict(aprs.LOCATOR_LOOKALIKES)
    assert all(aprs.is_locator_status(v) for v in valid.values()) and not any(aprs.is_locator_status(v) for v in lookalikes.values())
    counters = sp.OptoutCounters(hub, ("status",))
    with sp.feeds(web_ctx) as fd:
        for n, (name, info) in enumerate(sorted(valid.items())):
            send_status(rf, "%s-%d" % (base, 3 + n), info)
        items, room, reached = counters.wait(minimum=len(valid))
        sp.note(target, case, "RECORD", "optout counters for kind status: %s, dv_room_data_total +%s (%d valid locator statuses sent)" % (items, room, len(valid)))
        assert reached, "the valid locator statuses of the private base %s were not all denied/13: %s / %s" % (base, items, room)
        # the look-alikes and the plain status are ordinary statuses of the same base
        controls = dict(lookalikes, plain=">On the air plainword")
        missing = []
        for n, (name, info) in enumerate(sorted(controls.items())):
            send_status(rf, "%s-%d" % (base, 3 + n), info)
            if sp.wait_status(fd.pub, fd.t0, base, word_of(info)) is None:
                missing.append(name)
        sp.note(target, case, "RECORD", "look-alikes and the plain status that never reached the public SSE as statuses: %s" % (missing or "none"))
        assert not missing, "ordinary statuses were treated as locator statuses (dropped): %s" % missing
        # no valid locator status of the private base is public, on any surface
        sse = [r for r in sp.sse_status(fd.pub, fd.t0, base) if aprs.is_locator_status(">" + str(r.get("body", "")))]
        history = [r for r in sp.status_rows(room_history(web_ctx), base) if aprs.is_locator_status(">" + str(r.get("body", "")))]
        assert not sse and not history, "a valid locator status of the private base is public: %s %s" % (sse[:1], history[:1])
        if shape.positions:
            send_status(rf, PUBLIC_BASE + "-3", aprs.LOCATOR_VALID["six"])
            public = sp.wait_status(fd.pub, fd.t0, PUBLIC_BASE, "locator6")
            sp.note(target, case, "RECORD", "a valid locator status of the public base %s: %s" % (PUBLIC_BASE, "public status" if public else "NOT public"))
            assert public is not None, "a valid locator status of a PUBLIC base is not a public status"


def test_dp19_b_locator_status_is_not_gated_to_aprsis_for_a_private_base(rig, target, hub, web_ctx, shape, rf, aprsc):
    """data.md 9.3 and 13.4: the worker applies the same spatial classifier to whole-packet RF transit (the path of dp7-rf-igate.toml): a locator-bearing status keeps its
    transit routing type and falls under the position check. Seen at the private aprsc: the private base's valid locator statuses are absent, its look-alikes and a plain status
    arrive with the information field unchanged (the control proves the path), and a public base's locator status is gated."""
    case = CASE + "-b"
    if not shape.igate:
        pytest.skip("no transit path edge:ax25/rf-a -> edge:aprsis: add rig/config/dv-router/dp15-aprsis.toml and dp7-rf-igate.toml")
    base = private_base(shape)
    gated = {"plain": ">On the air gated", "lookalike": aprs.LOCATOR_LOOKALIKES["leading_space"], "public_locator": aprs.LOCATOR_VALID["six"]}
    with aprsc_login(aprsc) as tester:
        mark = tester.mark()
        sp.send_rf(rf, base + "-5", gated["plain"], kinds=("status",))
        sp.send_rf(rf, base + "-6", gated["lookalike"], kinds=("status",))
        if shape.positions:
            sp.send_rf(rf, PUBLIC_BASE + "-3", gated["public_locator"], kinds=("status",))
        for name, info in sorted(gated.items()):
            if name == "public_locator" and not shape.positions:
                continue
            base_name = PUBLIC_BASE if name == "public_locator" else base
            hit = tester.wait_packet(lambda p, b=base_name, i=info: ps.same_base(p["src"], b) and i in p["info"], TRANSIT_WAIT_S, since=mark)
            sp.note(target, case, "RECORD", "%s at the aprsc: %s" % (name, None if hit is None else hit[2]))
            assert hit is not None, "the %s status of %s never reached the private aprsc through the transit path (the control proves nothing)" % (name, base_name)
        mark2 = tester.mark()
        for n, (name, info) in enumerate(sorted(aprs.LOCATOR_VALID.items())):
            sp.send_rf(rf, "%s-%d" % (base, 7 + n % 5), info, kinds=("status",), spend=False)
        time.sleep(TRANSIT_ABSENT_S)
        lines = [p["raw"] for _t, p in tester.matching(lambda p: ps.same_base(p["src"], base) and aprs.is_locator_status(p["info"]), mark2)]
    sp.note(target, case, "RECORD", "valid locator statuses of the private base %s at the aprsc: %s" % (base, lines))
    assert not lines, "a valid locator status of a private base was gated to APRS-IS: %s" % lines
