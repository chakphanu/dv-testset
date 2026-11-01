# SPDX-License-Identifier: MIT
"""dvtest.crossmode (expected listener frames, link checks) and
dvtest.fake_ctl against a FakeAmbedServer on 127.0.0.1."""
import pytest

from dvtest import crossmode as cm
from dvtest.audio import signals
from dvtest.fake_ambed import FakeAmbedServer
from dvtest.fake_ctl import FakeCtl, FakeCtlError
from dvtest.proto import ambed_ext as ext

from test_fake_ambed import HOST, Worker, wait_for

pytestmark = pytest.mark.unit

KEY = {"dstar": "dstar", "dcs": "dstar", "dplus": "dstar", "dmr": "dmr", "m17": "m17", "usrp": "pcm"}


def frames(kind, n=60):
    return signals.prng_frames(KEY[kind], n, seed="unit/" + kind)


# ---- expected counts: media-format.md "Expected counts" (FRAMES = 60) ---------

@pytest.mark.parametrize("src,dst,count", [
    ("dstar", "dmr", 60), ("dmr", "dstar", 60), ("dstar", "m17", 62),
    ("dmr", "m17", 62), ("m17", "dmr", 63), ("m17", "dstar", 61)])
def test_document_counts(src, dst, count):
    assert len(cm.expected_listener(src, dst, frames(src)).frames) == count


def test_source_units_per_protocol():
    f = frames("dstar", 6)
    assert cm.source_units("dstar", f) == (f, bytes(9))
    d = frames("dmr", 4)
    u = cm.source_units("dmr", d)
    assert u.voice == d + [bytes(9)] * 2 and u.end == bytes(9)       # whole bursts of three
    m = frames("m17", 5)
    u = cm.source_units("m17", m)
    # pairs (the odd last frame repeated), then the EOT: first unit voice, second the end unit
    assert u.voice == [m[0], m[1], m[2], m[3], m[4], m[4], m[4]] and u.end == m[4]
    p = frames("usrp", 3)
    assert cm.source_units("usrp", p) == (p, None)
    from dvtest.proto import dplus
    assert cm.source_units("dplus", f).end == dplus.END_PATTERN[:9] == cm.DPLUS_END_AMBE


def test_m17_source_even_frames_eot_repeats_the_last_pair():
    m = frames("m17", 4)
    u = cm.source_units("m17", m)
    assert u.voice == m + [m[2]] and u.end == m[3]


def test_dmr_listener_is_padded_with_raw_zero_not_transformed():
    f = frames("m17", 60)
    want = cm.expected_listener("m17", "dmr", f).frames
    voice = cm.transform_units(3, 2, cm.source_units("m17", f).voice)
    assert want == voice + [bytes(9)] * 2


def test_m17_listener_eot_holds_pending_unit_or_silence():
    f = frames("dmr", 60)
    got = cm.expected_listener("dmr", "m17", f).frames
    t = cm.transform_units(2, 3, f)
    assert got == t + [cm.M17_SILENCE, ext.expected_output(2, 3, bytes(9))]
    odd = [bytes(range(i, i + 9)) for i in range(5)]       # 5 voice + end
    got = cm.expected_listener("dstar", "m17", odd).frames
    t = cm.transform_units(1, 3, odd)
    assert got == t + [ext.expected_output(1, 3, bytes(9))] and len(got) == 6
    got = cm.expected_listener("usrp", "m17", frames("usrp", 4)).frames
    assert got[-2:] == [cm.M17_SILENCE, cm.M17_SILENCE]


def test_same_codec_is_not_transformed():
    f = frames("dstar", 10)
    got = cm.expected_listener("dstar", "dcs", f)
    assert got.frames == f and got.exact


def test_same_protocol_follows_the_client_library():
    f = frames("m17", 10)
    assert len(cm.expected_listener("m17", "m17", f).frames) == 12        # packets 1..5 (10 frames) + EOT (2)
    assert len(cm.expected_listener("m17", "m17", f, m17_fn0=True).frames) == 14
    d = frames("dmr", 4)
    assert cm.expected_listener("dmr", "dmr", d).frames == d + [bytes(9)] * 2


def test_usrp_listener_tail_is_open():
    e = cm.expected_listener("dstar", "usrp", frames("dstar", 5))
    assert not e.exact and e.prefix == 5 and len(e.frames) == 5


def test_every_unit_has_one_k_not_two():
    u = frames("dstar", 3)
    one = cm.expected_listener("dstar", "m17", u).frames[0]
    two = ext.expected_output(2, 3, ext.expected_output(1, 2, u[0]))
    assert one != two


def test_dstar_cross_slow_and_seq():
    assert cm.dstar_cross_slow(23)[0] == cm.DSTAR_SYNC and cm.dstar_cross_slow(23)[21] == cm.DSTAR_SYNC
    assert cm.dstar_cross_slow(3)[1] == cm.DSTAR_FILLER
    assert cm.dstar_cross_seq(23)[20:] == [20, 0, 1]


# ---- sessions / check_link on a real fake log -------------------------------------

@pytest.fixture
def server():
    with FakeAmbedServer(HOST, port=0, control_port=0) as srv:
        yield srv


@pytest.fixture
def worker(server):
    w = Worker(server)
    yield w
    w.close()


def drive(worker, server, units, src=1, outs=(2, 3), mutate=None):
    st = worker.stream(src, list(outs))
    for index, unit in enumerate(units):
        st.sock.sendto(ext.data_request(src, index, unit), st.dest)
        assert st.reply() is not None
    st.close()
    assert wait_for(lambda: server.stats()["counters"].get("cs_known", 0) == 1)
    return st


def test_sessions_and_check_link_pass_for_a_clean_run(worker, server):
    units = frames("dstar", 5)
    drive(worker, server, units)
    entries = FakeCtl(HOST, server.control_port).log()
    sessions, strays = cm.sessions(entries)
    assert len(sessions) == 1 and not strays
    s = sessions[0]
    assert s.std is not None and len(s.requests) == 5 and len(s.replies) == 5 and len(s.cs) == 1
    assert cm.check_link(s, 1, units, (2, 3)) == []


def test_check_link_names_each_kind_of_deviation(worker, server):
    units = frames("dstar", 5)
    drive(worker, server, units)
    s = cm.sessions(FakeCtl(HOST, server.control_port).log())[0][0]
    assert any("outputs" in p for p in cm.check_link(s, 1, units, (2,)))
    assert any("input codec" in p for p in cm.check_link(s, 2, units, (2, 3)))
    assert any("data requests" in p for p in cm.check_link(s, 1, units[:4], (2, 3)))
    swapped = units[:1] + [units[2], units[1]] + units[3:]
    assert any("request 1" in p for p in cm.check_link(s, 1, swapped, (2, 3)))


def test_sessions_drop_invalid_opens_and_dedupe_retransmission(worker, server):
    worker.rpc(ext.open_request("N0TEST", 1, [3, 2], 9), 0.1)          # invalid order
    tx, first = worker.open(1, [2], 0x31)
    worker.open(1, [2], 0x31)                                         # retransmission
    sessions, strays = cm.sessions(FakeCtl(HOST, server.control_port).log())
    assert len(sessions) == 1 and len(strays) == 1 and strays[0]["note"] == "invalid"


def test_sessions_busy_and_dropped_data(worker, server):
    server.set_modes(open="busy")
    tx, reply = worker.open(1, [2], 5)
    assert ext.parse_busy(reply) == tx
    sessions, _ = cm.sessions(FakeCtl(HOST, server.control_port).log())
    assert len(sessions) == 1 and sessions[0].busy == tx and sessions[0].std is None
    server.set_modes(open="std")
    st = worker.stream(1, [2], 6)
    st.sock.sendto(b"\x02\x00" + bytes(9), st.dest)                   # wrong input codec: dropped
    assert st.reply(0.15) is None
    sessions, strays = cm.sessions(FakeCtl(HOST, server.control_port).log())
    assert len(sessions) == 2 and any(e["note"] == "dropped" for e in strays)


# ---- FakeCtl ---------------------------------------------------------------------

def test_fake_ctl_ops_and_errors(worker, server):
    ctl = FakeCtl(HOST, server.control_port, timeout=1.0, tries=1)
    assert ctl.stats()["streams_live"] == 0
    assert ctl.set(open="busy")["modes"]["open"] == "busy"
    with pytest.raises(FakeCtlError):
        ctl.set(open="nonsense")
    assert ctl.reset()["modes"]["open"] == "std"
    worker.stream(1, [2])
    assert ctl.counters()["open_new"] == 1 and ctl.last_seq() > 0
    assert any(e["kind"] == "std" for e in ctl.log(kinds=["std"]))
    assert ctl.wait_for(lambda s: s["streams_live"] == 1, 0.5)["streams_live"] == 1
    assert ctl.wait_for(lambda s: s["streams_live"] == 9, 0.3)["streams_live"] == 1


def test_fake_ctl_no_answer_raises():
    ctl = FakeCtl("127.0.0.1", 9, timeout=0.1, tries=2)
    with pytest.raises(FakeCtlError):
        ctl.stats()


def test_fake_ctl_follows_more(worker, server):
    server.max_log = 100000
    for i in range(400):
        server._note("rx", "unknown", bytes(100), 1)                  # long log: needs several datagrams
    ctl = FakeCtl(HOST, server.control_port)
    assert len(ctl.log()) == 400
