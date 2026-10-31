# SPDX-License-Identifier: MIT
"""Pure decisions of cases/test_edge_relink.py (unit tested)."""

def restore_action(relayed, reachable, waited):
    """What the finally block of a case does about the SUT:
    none     the edge relays and the SUT answers: nothing to do
    wait     the SUT answers but the edge does not relay yet: wait (bounded) first
    restart  the SUT does not answer (container not running), or it still
             does not relay after the wait: restart it through the control channel"""
    if reachable and relayed:
        return "none"
    if reachable and not waited:
        return "wait"
    return "restart"


def sut_state_text(reachable, last_error=None):
    """What the tester can say about the SUT container: it can only probe the
    DExtra port; the exit code of the container is not visible from here
    (compose-ps.txt and logs/xlxd-sut.log of the run have it)."""
    if reachable:
        text = "SUT container answers a DExtra link (running)"
    else:
        text = ("SUT container does NOT answer a DExtra link (not running or exited, e.g. e.g. exit code 138; "
                "exit code not visible from the tester: see compose-ps.txt and logs/xlxd-sut.log of the run)")
    return text + (("; last error: %s" % last_error) if last_error else "")
