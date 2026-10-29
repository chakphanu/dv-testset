# SPDX-License-Identifier: MIT
from dvtest import clients
from dvtest.audio import signals


def test_wire_frames():
    dstar = signals.prng_frames("dstar", 5, seed=1)
    assert clients.wire_frames("dstar", dstar) == dstar
    assert clients.wire_frames("dmr", dstar) == dstar + [bytes(9)]
    c2 = signals.prng_frames("m17", 5, seed=1)
    # odd count pads the last pair, the EOT packet repeats it
    assert clients.wire_frames("m17", c2) == c2 + [c2[4]] * 3


def test_fresh_stream_ids():
    ids = {clients.fresh_stream_id("dstar") for _ in range(50)}
    assert len(ids) > 40 and all(0 < i < 1 << 16 for i in ids)
    assert all(0 < clients.fresh_stream_id("dmr") < 1 << 32 for _ in range(10))


def test_make_kinds():
    for kind in clients.KINDS:
        client = clients.make(kind, "D", 1)
        try:
            assert client.dest is None
        finally:
            clients.close(client)
