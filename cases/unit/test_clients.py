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


def test_wire_frames_m17_fn0_by_profile():
    from dvtest import specs, target
    c2 = signals.prng_frames("m17", 6, seed=4)
    xlxd = target.load(env={})
    dvrouter = target.load(env={"DVT_SUT_PROFILE": "dvrouter"})
    assert not specs.m17_fn0_relayed(xlxd) and specs.m17_fn0_relayed(dvrouter)
    plain = clients.wire_frames("m17", c2, m17_fn0=specs.m17_fn0_relayed(xlxd))
    with_fn0 = clients.wire_frames("m17", c2, m17_fn0=specs.m17_fn0_relayed(dvrouter))
    assert plain == c2 + [c2[4], c2[5]]
    assert with_fn0 == [c2[0], c2[0]] + plain
    # other kinds ignore the flag
    assert clients.wire_frames("dstar", c2, m17_fn0=True) == c2


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


def test_extra_kinds_make_and_codec():
    for kind in clients.EXTRA_KINDS:
        assert clients.CODEC[kind] in (1, 2)
        client = clients.make(kind, "D", 0)
        try:
            assert client.dest is None
        finally:
            clients.close(client)
    frames = signals.prng_frames("dstar", 4, seed=2)
    assert clients.wire_frames("dplus", frames) == frames == clients.wire_frames("dcs", frames)


def test_talk_records_send_stamps():
    import socket
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    try:
        for kind, count in (("dstar", 5), ("dmr", 6), ("m17", 6)):
            client = clients.make(kind, "D", 0)
            client.dest = sink.getsockname()
            try:
                clients.talk(client, kind, signals.prng_frames(kind, count, seed=3), tail=0)
                voice = {"dstar": count + 1, "dmr": count // 3, "m17": count // 2 + 1}[kind]
                assert len(client.last_stamps) == voice
                assert client.last_stamps == sorted(client.last_stamps)
            finally:
                client.dest = None
                client.close()
    finally:
        sink.close()
