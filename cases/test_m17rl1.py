# SPDX-License-Identifier: MIT
"""M17RL1 (DV-Router 307525dc, in bc9af2b1, image dv-router:wip-f1f3777): the M17 reflector interlink answers every valid CONN with an ACKN, sends our
own CONN at most once per peer row per 3 s (timer and answers together), and DEFERS a suppressed answer to the end of the interval instead of dropping it.
Document: docs/design/m17.md "Peer handshake"; docs/m17rl1-cases.md. Pure judgements: dvtest/m17rl1.py. Switch DVT_M17RL1=on. NOT RUN on a rig: written
against the sources of bc9af2b1.

The tester plays the peer M17-DVT the way mrefd behaves on the wire (dvtest/m17_peer.py: auto_conn off, a CONN sent by hand when "mrefd starts", ACKN to
every CONN of the row, no answer to a PING): mrefd links on a received ACKN and sends its own CONN only from a 10 s link check while the peer is not linked
(m17.md). Real mrefd (DVT_REF_KIND=mrefd, side "ref") is calibration only: M17RL1-g records what it does, nothing on that side is asserted.

  M17RL1-a  every CONN gets an ACKN: linked row (ACKN alone, no CONN back) and unlinked row (ACKN for each of a stream of CONN)
  M17RL1-b  our CONN at most once per row per 3 s: a flood of valid CONN (4 per second for 14 s) at an unlinked row that is never answered
  M17RL1-c  a suppressed answer is deferred: a CONN 0.3 s after our CONN gets an ACKN at once, no CONN before +3 s, and the CONN at +3 s (not at the 6 s backoff)
  M17RL1-d  the three start orders of an mrefd peer relink within about 3 s:
            A  we start while it runs (the row retries 3 s after the link loss and the peer answers it)
            B  it starts while we run unlinked: B1 our last CONN < 3 s old (deferred CONN), B2 our last CONN >= 3 s old (CONN at once)
            C  both start within the same 3 s (the peer CONN lands next to our own first retry)
  M17RL1-e  a peer restart relinks within about 3 s: with its DISC first (we are unlinked, CONN at once or within 3 s) and without (we still think it linked:
            ACKN alone, the link holds)
  M17RL1-g  (side ref, record only) what mrefd does with the same silence and the same CONN
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import m17_peer, m17rl1  # noqa: E402

import data_support as ds  # noqa: E402

pytestmark = [pytest.mark.rig]

LOG = "m17rl1.txt"
FLOOD_S = 14.0
FLOOD_PERIOD_S = 0.25


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target):
    if not m17rl1.strict_on():
        pytest.skip("DVT_M17RL1=on is not set (the image under test has M17RL1, DV-Router 307525dc: bc9af2b1 / wip-f1f3777 or later)")


@pytest.fixture
def link(m17_peers):
    """The M17-DVT PeerLink of the sut side in the mrefd manner (no CONN of its own), linked at the start and linked again at the end."""
    if "sut" not in m17_peers or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs the sut side with M17 peering: DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native, the peers M17-DVT / M17-DVU of the template, --profile interlink")
    peer = m17_peers["sut"].dvt
    yield peer
    peer.reset()
    peer.auto_conn = False
    peer.send_conn()                          # an mrefd that is up again: a row in a long backoff answers it at once (M17RL1) instead of at 30 s
    peer.link(m17_peer.LINK_TIMEOUT)          # the next case starts linked (PeerLinkTimeout names what both sides sent)
    peer.clear()


def mrefd_manner(peer):
    """Back to the constructor modes, with no CONN of its own (an mrefd sends CONN from its 10 s check, here by hand), linked, logs empty."""
    peer.reset()
    peer.auto_conn = False
    peer.link(m17_peer.LINK_TIMEOUT)
    peer.clear()


def unlink(peer, silent=True):
    """DISC to the row (it is unlinked from now on, first retry 3 s later); `silent`: the peer answers nothing, so the row stays unlinked. The monotonic time of the DISC."""
    peer.silent = silent
    t = time.monotonic()
    peer.drop()
    peer.silent = silent
    return t


def row_conns(peer, since):
    return m17rl1.times(peer.datagrams(since=since, kind="CONN37"))


def row_ackns(peer, since):
    return m17rl1.times(peer.datagrams(since=since, kind="ACKN37"))


def wait_row_conn(peer, since, timeout):
    rx = peer.wait_for(kind="CONN37", since=since, timeout=timeout)
    assert rx is not None, "the row sent no CONN within %.0f s of %s: %s" % (timeout, "the DISC" if since else "the start", peer.report())
    return rx.t


def sleep_until(t):
    delay = t - time.monotonic()
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------- a

def test_m17rl1_a_every_conn_gets_an_ackn(rig, target, link):
    case = "M17RL1-a"
    mrefd_manner(link)
    # linked: the ACKN alone
    t0 = time.monotonic()
    link.send_conn()
    ackn = link.wait_for(kind="ACKN37", since=t0, timeout=2.0)
    time.sleep(2.5)
    conns = row_conns(link, t0)
    note(target, case, "RECORD", "linked row: ACKN %s after the CONN, row CONN in the next 2.5 s: %d" % ("%.3f s" % (ackn.t - t0) if ackn else "NONE", len(conns)))
    assert ackn is not None, "%s: a linked row did not answer a valid CONN with an ACKN: %s" % (case, link.report())
    assert ackn.size == 37, "%s: the ACKN is %d bytes, not 37" % (case, ackn.size)
    assert conns == [], "%s: a linked row answered a CONN with a CONN of its own (%d): an existing link gets the ACKN alone" % (case, len(conns))
    assert link.linked, "%s: the link did not hold after a CONN of the peer" % case
    # unlinked: an ACKN for each CONN (10 CONN, 0.5 s apart; the row's own CONN are not answered)
    t1 = unlink(link)
    sent = 0
    for _ in range(10):
        link.send_conn()
        sent += 1
        time.sleep(0.5)
    time.sleep(1.0)
    ackns = row_ackns(link, t1)
    note(target, case, "RECORD", "unlinked row: %d CONN sent, %d ACKN, %d row CONN" % (sent, len(ackns), len(row_conns(link, t1))))
    problems = m17rl1.ack_problems(sent, ackns)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


# ---------------------------------------------------------------- b

def test_m17rl1_b_our_conn_goes_at_most_once_per_three_seconds(rig, target, link):
    case = "M17RL1-b"
    mrefd_manner(link)
    t0 = unlink(link)
    sent, end = 0, time.monotonic() + FLOOD_S
    while time.monotonic() < end:
        link.send_conn()
        sent += 1
        time.sleep(FLOOD_PERIOD_S)
    time.sleep(1.0)
    conns, ackns = row_conns(link, t0), row_ackns(link, t0)
    gaps = ["%.2f" % (b - a) for a, b in zip(conns, conns[1:])]
    note(target, case, "RECORD", "%d valid CONN in %.0f s -> %d ACKN, %d row CONN, gaps %s" % (sent, FLOOD_S, len(ackns), len(conns), gaps))
    problems = m17rl1.ack_problems(sent, ackns)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
    assert len(conns) >= 2, "%s: the unlinked row sent %d CONN in %.0f s: it neither answers nor retries" % (case, len(conns), FLOOD_S)
    problems = m17rl1.spacing_problems(conns)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
    limit = m17rl1.max_conn_in(FLOOD_S + 1.0)
    assert len(conns) <= limit, "%s: %d CONN in %.0f s, at most %d fit at one per 3 s" % (case, len(conns), FLOOD_S + 1.0, limit)


# ---------------------------------------------------------------- c

def test_m17rl1_c_a_suppressed_answer_is_deferred_not_dropped(rig, target, link):
    case = "M17RL1-c"
    mrefd_manner(link)
    t0 = unlink(link)
    first = wait_row_conn(link, t0, 8.0)                    # the retry 3 s after the link loss; nobody answers it (silent)
    sleep_until(first + 0.3)
    peer_conn = time.monotonic()
    link.send_conn()
    time.sleep(m17rl1.CONN_INTERVAL_S + 1.2)
    after = [t for t in row_conns(link, t0) if t > first + 0.01]
    ackn_after = m17rl1.first_at_or_after(row_ackns(link, peer_conn), peer_conn)
    note(target, case, "RECORD", "first row CONN %.2f s after the DISC; peer CONN at +0.3 s; ACKN after %s; next row CONN at +%s s of the first" % (
        first - t0, "%.3f s" % (ackn_after - peer_conn) if ackn_after else "NONE", ", +".join("%.2f" % (t - first) for t in after) or "NONE"))
    problems = m17rl1.deferral_problems(first, peer_conn, after, ackn_after)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


# ---------------------------------------------------------------- d

def relinked(peer, peer_conn, wait_s):
    """Seconds from the peer CONN to the full handshake (the row's ACKN and a row CONN that the peer answered), waiting up to wait_s; None when it did not happen."""
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        got = m17rl1.relink_time(peer_conn, row_conns(peer, peer_conn), row_ackns(peer, peer_conn))
        if got is not None:
            return got
        time.sleep(0.05)
    return m17rl1.relink_time(peer_conn, row_conns(peer, peer_conn), row_ackns(peer, peer_conn))


def test_m17rl1_d_a_we_start_while_the_peer_runs(rig, target, link):
    """Order A: the row loses the link and retries 3 s later; the peer (running, answering every CONN) links us at that CONN."""
    case = "M17RL1-d-A"
    mrefd_manner(link)
    t0 = unlink(link, silent=False)
    first = wait_row_conn(link, t0, 8.0)
    handshake = first - t0
    note(target, case, "RECORD", "DISC -> first row CONN %.2f s (answered with ACKN by the peer)" % handshake)
    assert handshake <= m17rl1.RELINK_BOUND_S + 0.7, "%s: the row's first CONN left %.2f s after the link loss (the first retry is 3 s)" % (case, handshake)
    took = link.link(m17_peer.LINK_TIMEOUT)
    note(target, case, "RECORD", "link up (a PING of the row seen) %.2f s after the first CONN was answered" % took)


def test_m17rl1_d_b1_the_peer_starts_inside_the_interval(rig, target, link):
    """Order B1: the peer starts while the row is unlinked and its last CONN is < 3 s old: ACKN at once, the CONN deferred to +3 s, linked within 3 s of the peer CONN."""
    case = "M17RL1-d-B1"
    mrefd_manner(link)
    t0 = unlink(link)
    first = wait_row_conn(link, t0, 8.0)
    sleep_until(first + 1.0)
    link.silent = False                                        # "mrefd is up now": it answers the row's CONN
    peer_conn = time.monotonic()
    link.send_conn()
    took = relinked(link, peer_conn, m17rl1.RELINK_BOUND_S + 1.0)
    after = [t for t in row_conns(link, t0) if t > first + 0.01]
    note(target, case, "RECORD", "peer CONN %.2f s after the row's CONN; relinked after %s s; row CONN at +%s s of the first" % (
        peer_conn - first, "%.2f" % took if took is not None else "NEVER", ", +".join("%.2f" % (t - first) for t in after) or "NONE"))
    assert took is not None, "%s: no handshake within %.1f s of the peer CONN: %s" % (case, m17rl1.RELINK_BOUND_S + 1.0, link.report())
    problems = m17rl1.deferral_problems(first, peer_conn, after, m17rl1.first_at_or_after(row_ackns(link, peer_conn), peer_conn))
    assert not problems, "%s: %s" % (case, "; ".join(problems))
    assert took <= m17rl1.RELINK_BOUND_S, "%s: relinked %.2f s after the peer CONN (about 3 s allowed, %.1f s with the tick)" % (case, took, m17rl1.RELINK_BOUND_S)
    link.link(m17_peer.LINK_TIMEOUT)


def test_m17rl1_d_b2_the_peer_starts_after_the_interval(rig, target, link):
    """Order B2: the row's last CONN is >= 3 s old (its next retry is 6 s after it): the peer CONN is answered with our CONN at once."""
    case = "M17RL1-d-B2"
    mrefd_manner(link)
    t0 = unlink(link)
    first = wait_row_conn(link, t0, 8.0)
    sleep_until(first + 3.6)                                   # the row's next retry is at first + 6 s
    link.silent = False
    peer_conn = time.monotonic()
    link.send_conn()
    took = relinked(link, peer_conn, 3.0)
    after = row_conns(link, peer_conn)
    note(target, case, "RECORD", "peer CONN %.2f s after the row's last CONN; row CONN %s s after it; relinked after %s s" % (
        peer_conn - first, ", ".join("%.2f" % (t - peer_conn) for t in after) or "NONE", "%.2f" % took if took is not None else "NEVER"))
    problems = m17rl1.immediate_problems(peer_conn, after)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
    assert took is not None and took <= m17rl1.IMMEDIATE_S, "%s: relinked after %s s, an answer to a CONN >= 3 s after the last one is immediate" % (case, took)
    link.link(m17_peer.LINK_TIMEOUT)


def test_m17rl1_d_c_both_start_within_the_same_three_seconds(rig, target, link):
    """Order C: the peer CONN lands next to the row's own first retry (2.8 s after the link loss): the link comes up within the interval and the row never sends two CONN closer than 3 s."""
    case = "M17RL1-d-C"
    mrefd_manner(link)
    t0 = unlink(link, silent=False)
    sleep_until(t0 + 2.8)
    peer_conn = time.monotonic()
    link.send_conn()
    took = relinked(link, peer_conn, m17rl1.RELINK_BOUND_S + 1.0)
    conns = row_conns(link, t0)
    note(target, case, "RECORD", "peer CONN at +2.8 s; row CONN at +%s s of the DISC; relinked after %s s" % (", +".join("%.2f" % (t - t0) for t in conns) or "NONE", "%.2f" % took if took is not None else "NEVER"))
    assert took is not None, "%s: no handshake after the peer CONN: %s" % (case, link.report())
    assert took <= m17rl1.RELINK_BOUND_S, "%s: relinked %.2f s after the peer CONN" % (case, took)
    problems = m17rl1.spacing_problems(conns)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
    link.link(m17_peer.LINK_TIMEOUT)


# ---------------------------------------------------------------- e

def test_m17rl1_e_a_peer_restart_with_a_disc_relinks_within_three_seconds(rig, target, link):
    case = "M17RL1-e-disc"
    mrefd_manner(link)
    last = row_conns(link, None)
    t0 = unlink(link, silent=False)                            # the peer says DISC when it stops, answers everything when it is back
    time.sleep(0.6)
    peer_conn = time.monotonic()
    link.send_conn()                                           # its first link check after the restart
    took = relinked(link, peer_conn, m17rl1.RELINK_BOUND_S + 1.0)
    note(target, case, "RECORD", "DISC at 0, peer CONN at +%.2f s; relinked after %s s (row CONN seen in the whole case %d)" % (
        peer_conn - t0, "%.2f" % took if took is not None else "NEVER", len(row_conns(link, None)) - len(last)))
    assert took is not None, "%s: no handshake within %.1f s of the restarted peer's CONN: %s" % (case, m17rl1.RELINK_BOUND_S + 1.0, link.report())
    assert took <= m17rl1.RELINK_BOUND_S, "%s: relinked %.2f s after the peer CONN (about 3 s allowed)" % (case, took)
    link.link(m17_peer.LINK_TIMEOUT)


def test_m17rl1_e_a_peer_restart_without_a_disc_keeps_the_link(rig, target, link):
    case = "M17RL1-e-crash"
    mrefd_manner(link)
    t0 = time.monotonic()
    link.send_conn()                                           # the restarted peer knows nothing of the link: its first CONN reaches a row that is still linked
    ackn = link.wait_for(kind="ACKN37", since=t0, timeout=2.0)
    time.sleep(m17rl1.RELINK_BOUND_S + 0.5)
    conns = row_conns(link, t0)
    note(target, case, "RECORD", "ACKN after %s; row CONN %d; link still up %s" % ("%.3f s" % (ackn.t - t0) if ackn else "NONE", len(conns), link.linked))
    assert ackn is not None, "%s: a linked row did not answer the CONN of a restarted peer with an ACKN" % case
    assert conns == [], "%s: a linked row answered with a CONN of its own: %s" % (case, conns)
    assert link.linked, "%s: the link did not hold: %s" % (case, link.report())


# ---------------------------------------------------------------- g

def test_m17rl1_g_what_mrefd_does_with_the_same_silence(rig, target, m17_peers):
    """Calibration (side ref = real mrefd, DVT_REF_KIND=mrefd): record, never assert. mrefd sends CONN from its 10 s link check while we are silent, answers our CONN
    with an ACKN alone and links on a received ACKN."""
    case = "M17RL1-g"
    if "ref" not in m17_peers:
        pytest.skip("the reference is not mrefd (DVT_REF_KIND=mrefd --profile mrefd)")
    peer = m17_peers["ref"].dvt
    peer.reset()
    peer.auto_conn = False
    t0 = time.monotonic()
    peer.silent = True
    peer.drop()
    peer.silent = True
    time.sleep(25.0)
    conns = row_conns(peer, t0)
    peer.send_conn()
    t1 = time.monotonic()
    time.sleep(3.0)
    note(target, case, "RECORD", "mrefd, silent peer for 25 s after a DISC: its CONN at +%s s; our CONN answered with %s ACKN and %d CONN within 3 s" % (
        ", +".join("%.2f" % (t - t0) for t in conns) or "NONE", len(row_ackns(peer, t1)), len(row_conns(peer, t1))))
    peer.reset()
    peer.link(m17_peer.LINK_TIMEOUT)
