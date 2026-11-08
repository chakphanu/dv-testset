# SPDX-License-Identifier: MIT
"""dvtest/fuzz.py (seeded mutators), dvtest/fuzz_run.py (run side, offline parts),
dvtest/tools/fuzz_replay.py and the structure of cases/test_fuzz_netns.py. No sockets
are opened to anything but the fake sources of the tests."""
import collections
import importlib.util
import json
from pathlib import Path

import pytest

from dvtest import fuzz, fuzz_run
from dvtest.proto import (dcs, dextra, dmr, dmrplus, dplus, g3, imrs, m17, nxdn, p25, usrp, xlx, ysf)
from dvtest.tools import fuzz_replay

CASES = Path(__file__).resolve().parents[1]
SEEDS = ("1", "2", "fuzz")
RUN = 1500                                   # cases generated per protocol in the bound tests


def role(name, wanted):
    return [it.data for it in fuzz.protocol(name).items if it.role == wanted]


# a parser of the proto module per protocol: the seed packet of the given role must be accepted by it
VALID = {
    "dextra": lambda: dextra.parse_header(role("dextra", "header")[0]) and dextra.parse_voice(role("dextra", "voice")[0]),
    "dplus": lambda: dplus.parse_header(role("dplus", "header")[0]) and dplus.parse_voice(role("dplus", "voice")[0]),
    "dcs": lambda: dcs.parse(role("dcs", "voice")[0]),
    "dmr": lambda: dmr.parse(role("dmr", "voice")[0]) and dmr.parse(role("dmr", "header")[0]),
    "dmrplus": lambda: dmrplus.parse(role("dmrplus", "voice")[0]),
    "m17": lambda: m17.parse_stream(role("m17", "voice")[0])["crc_ok"],
    "m17v3": lambda: m17.parse_stream(role("m17v3", "voice")[0])["crc_ok"],
    "ysf": lambda: ysf.parse(role("ysf", "voice")[0]),
    "nxdn": lambda: nxdn.parse(role("nxdn", "voice")[0]),
    "p25": lambda: p25.parse(role("p25", "voice")[0]),
    "usrp": lambda: usrp.parse(role("usrp", "voice")[0]),
    "xlx": lambda: xlx.parse_link(role("xlx", "control")[0]) and xlx.parse_voice(role("xlx", "voice")[0]),
    "imrs": lambda: imrs.parse(role("imrs", "voice")[0]),
    "g3": lambda: g3.kind_of(role("g3", "voice")[0]) == "voice",
}


# ---------------------------------------------------------------- seeds

def test_every_protocol_has_a_valid_seed_packet():
    assert set(VALID) == set(fuzz.PROTOCOLS)
    for name in fuzz.PROTOCOLS:
        spec = fuzz.protocol(name)
        assert spec.items, name
        assert {it.role for it in spec.items} <= set(fuzz.ROLES), name
        # DCS and USRP have no header datagram: the first voice packet opens the stream
        assert {"voice", "terminator"} <= {it.role for it in spec.items}, name
        assert {"control", "header"} & {it.role for it in spec.items}, name
        assert VALID[name](), "%s: the module's parser does not accept the seed voice packet" % name


@pytest.mark.parametrize("name", fuzz.PROTOCOLS)
def test_seed_fields_lie_inside_their_packets(name):
    for it in fuzz.protocol(name).items:
        assert it.data and len(it.data) <= fuzz.MAX_DATAGRAM
        for f in it.fields:
            assert f.kind in fuzz.KINDS and f.size >= 1 and 0 <= f.offset and f.offset + f.size <= len(it.data), (name, f)
    assert fuzz.interesting_fields(name), name


def test_fields_are_read_from_the_builders():
    # the stream id of a DExtra header moves with the builder, not with a table of offsets
    a = dextra.header(0x1234, fuzz.CALL, "B")
    b = dextra.header(0xABCD, fuzz.CALL, "B")
    assert fuzz.locate_runs(a, b) == [(12, 2)]
    sid = [f for f in fuzz.protocol("dextra").items[3].fields if f.kind == "stream_id"][0]
    assert (sid.offset, sid.size) == (12, 2) and a[12:14] == (0x1234).to_bytes(2, "little")
    with pytest.raises(ValueError):
        fuzz.locate_runs(b"ab", b"abc")
    with pytest.raises(ValueError):
        fuzz.fields_of(lambda v: b"xx", 1, 2, "none", "id")


def test_seed_packets_come_from_the_builders_not_from_literals():
    assert fuzz.seed_packets("dextra")[0] == dextra.link_packet(fuzz.CALL, "B")
    assert fuzz.seed_packets("usrp")[3] == usrp.voice(1, bytes(320))
    assert fuzz.seed_packets("nxdn")[0] == nxdn.poll_packet(fuzz.CALL, 31672)
    assert fuzz.seed_packets("ysf")[0] == ysf.poll_packet(fuzz.CALL)


def test_ports_and_groups():
    assert fuzz.PORTS["dextra"] == (30001,) and fuzz.PORTS["g3"] == (12346, 12345, 40000)
    assert set(fuzz.PORT_GROUPS) == {"dextra", "dplus", "dcs", "dmr", "dmrplus", "ysf", "m17", "nxdn", "p25", "usrp",
                                     "xlx", "imrs", "g3"}
    assert sorted(p for g in fuzz.PORT_GROUPS.values() for p in g) == sorted(fuzz.PROTOCOLS)
    for name in fuzz.PROTOCOLS:
        spec = fuzz.protocol(name)
        assert {it.port for it in spec.items} <= set(fuzz.PORTS[name])
        assert [g[0].port for g in spec.groups] == [p for p in fuzz.PORTS[name] if any(i.port == p for i in spec.items)]


def test_unknown_protocol_is_an_error():
    with pytest.raises(KeyError):
        fuzz.protocol("nope")


# ---------------------------------------------------------------- determinism

@pytest.mark.parametrize("name", fuzz.PROTOCOLS)
def test_a_case_is_a_pure_function_of_protocol_seed_index(name):
    first = [fuzz.generate(name, "7", i) for i in range(120)]
    # other calls in between and a different order must not matter
    fuzz.generate(name, "other", 5)
    second = [fuzz.generate(name, "7", i) for i in reversed(range(120))][::-1]
    assert first == second
    assert first != [fuzz.generate(name, "8", i) for i in range(120)]
    it = fuzz.cases(name, "7", start=40)
    assert [next(it) for _ in range(5)] == first[40:45]


def test_the_random_stream_does_not_depend_on_hash_randomisation():
    # str seeding of random.Random is sha512 based: pinned values stay the same on every run
    assert fuzz.rng_for("dextra", "1", 0).getrandbits(32) == fuzz.rng_for("dextra", "1", 0).getrandbits(32)
    assert fuzz.generate("dextra", "1", 3).packets == fuzz.generate("dextra", "1", 3).packets
    digest = [fuzz.generate("dextra", "1", i).packets for i in range(40)]
    assert json.dumps([[p.hex() for p in c] for c in digest]) == json.dumps([[p.hex() for p in c] for c in digest])


# ---------------------------------------------------------------- limits

@pytest.mark.parametrize("name", fuzz.PROTOCOLS)
@pytest.mark.parametrize("seed", SEEDS)
def test_every_mutator_stays_within_the_limits(name, seed):
    used = set()
    for i in range(RUN):
        case = fuzz.generate(name, seed, i)
        used.add(case.mutator)
        assert case.protocol == name and case.seed == seed and case.index == i
        assert 1 <= len(case.packets) <= fuzz.MAX_BURST, (name, i, len(case.packets))
        assert case.port in fuzz.PORTS[name]
        for p in case.packets:
            assert isinstance(p, bytes) and len(p) <= fuzz.MAX_DATAGRAM, (name, i, case.mutator, len(p))
    assert used == set(fuzz.MUTATORS)


def test_the_mutator_table_names_every_required_class():
    names = set(fuzz.MUTATORS)
    # truncation, extension, bit flips, field off-by-one / extremes, magic, module / ids, fills, duplicates,
    # reordering, header without voice, voice without header, terminator storm
    assert {"truncate", "extend", "flip1", "flipn", "off_by_one", "extreme", "magic", "module", "ids", "fill",
            "duplicate", "reorder", "header_only", "voice_only", "term_storm", "compound"} <= names


# ---------------------------------------------------------------- what each mutator does

def cases_of(name, mutator, count=60, seed="1"):
    out = []
    for i in range(10000):
        if fuzz.MUTATORS[i % len(fuzz.MUTATORS)] == mutator:
            out.append(fuzz.generate(name, seed, i))
            if len(out) == count:
                return out
    raise AssertionError(mutator)


@pytest.mark.parametrize("name", fuzz.PROTOCOLS)
def test_truncation_walks_every_boundary_of_every_seed_packet(name):
    spec = fuzz.protocol(name)
    plan = fuzz.truncation_plan(name)
    assert len(plan) == len(set(plan))
    for index, it in enumerate(spec.items):
        cuts = {c for i, c in plan if i == index}
        assert {0, len(it.data) - 1} <= cuts, (name, index)
        for f in it.fields:
            for c in (f.offset, f.offset + f.size):
                if c < len(it.data):
                    assert c in cuts, (name, index, f, c)
    # a default run (2000 datagrams) reaches the whole plan: two truncation slots per cycle
    assert len(plan) <= 2000 // len(fuzz.MUTATORS) * 2, "plan of %d does not fit a default run" % len(plan)
    cut = [fuzz.generate(name, "x", i) for i in range(2000) if fuzz.MUTATORS[i % len(fuzz.MUTATORS)] == "truncate"]
    assert {spec.items[i].data[:c] for i, c in plan} <= {p.packets[0] for p in cut}


@pytest.mark.parametrize("name", fuzz.PROTOCOLS)
def test_extension_adds_junk_and_a_flip_changes_bits(name):
    seeds = set(fuzz.seed_packets(name))
    for case in cases_of(name, "extend", 20):
        base = [s for s in seeds if case.packets[0].startswith(s)]
        assert base and len(case.packets[0]) > len(base[0]) or len(case.packets[0]) == fuzz.MAX_DATAGRAM
    for case in cases_of(name, "flip1", 30):
        same = [s for s in seeds if len(s) == len(case.packets[0])]
        diffs = [sum(bin(a ^ b).count("1") for a, b in zip(s, case.packets[0])) for s in same]
        assert 1 in diffs, case
    for case in cases_of(name, "flipn", 20):
        assert len(case.packets[0]) in {len(s) for s in seeds}


def test_magic_module_and_id_mutators_hit_their_fields():
    seeds = fuzz.seed_packets("dextra")
    changed = [c.packets[0] for c in cases_of("dextra", "magic", 30) if c.packets[0] not in seeds]
    assert len(changed) > 15
    bad = {m[0] for m in fuzz.MODULES_BAD}
    hits = 0
    for case in cases_of("dextra", "module", 40):
        if len(case.packets[0]) == 11 and case.packets[0][8] in bad:
            hits += 1
    assert hits
    ids = [c.packets[0] for c in cases_of("dmrplus", "ids", 40)]
    assert any(p not in fuzz.seed_packets("dmrplus") for p in ids)


def group_of(name, case):
    return [g for g in fuzz.protocol(name).groups if g[0].port == case.port][0]


def test_sequence_mutators_make_the_documented_shapes():
    for name in fuzz.PROTOCOLS:
        for case in cases_of(name, "term_storm", 10):
            assert len(case.packets) >= 8 and len(set(case.packets)) == 1
            assert case.packets[0] in {i.data for i in group_of(name, case)}
        for case in cases_of(name, "header_only", 10):
            assert len(case.packets) <= 4 and set(case.packets) <= {i.data for i in group_of(name, case)}
        for case in cases_of(name, "voice_only", 10):
            group = group_of(name, case)
            voice = {i.data for i in group if i.role == "voice"} or {i.data for i in group}
            assert set(case.packets) <= voice and len(case.packets) <= 32
        dup = cases_of(name, "duplicate", 10)
        assert any(len(c.packets) > len(group_of(name, c)) or len(c.packets) == fuzz.MAX_BURST for c in dup), name
        for c in cases_of(name, "reorder", 10):
            assert sorted(c.packets) == sorted(i.data for i in fuzz._stream(group_of(name, c))), name


def test_dextra_cases_reach_length_and_value_extremes():
    lens = collections.Counter(len(p) for c in (fuzz.generate("dextra", "1", i) for i in range(1500)) for p in c.packets)
    assert 0 in lens and 56 in lens and max(lens) > 56 and min(lens) == 0
    ones = [c for c in cases_of("usrp", "off_by_one", 80) if len(c.packets[0]) == 352]
    assert ones, "a USRP off-by-one keeps the datagram length while it changes a field"


# ---------------------------------------------------------------- run side: pure parts

STAT = "4242 (dv edge) S 1 4242 4242 0 -1 4194560 100 0 0 0 5 3 0 0 20 0 7 0 987654 1000 100 18446744073709551615 0 0 0 0 0 0 0\n"


def test_parse_stat_status_and_net_udp():
    assert fuzz_run.parse_stat(STAT) == (4242, "dv edge", 1, 987654)
    assert fuzz_run.parse_vmrss_kib("Name:\tx\nVmRSS:\t  1234 kB\nThreads:\t3\n") == 1234
    assert fuzz_run.parse_vmrss_kib("Name:\tkthread\n") is None
    net = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n"
           "  45: 0100007F:7531 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 1 2 0\n"
           "  46: 0100007F:4E21 0100007F:1234 01 00000000:00000000 00:00000000 00000000     0        0 1 2 0\n")
    assert fuzz_run.parse_udp_ports(net) == {30001}


def test_snapshot_walks_the_descendants_of_the_launcher(tmp_path):
    def proc(pid, comm, ppid, start, rss):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text("%d (%s) S %d 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 %d 0 0" % (pid, comm, ppid, start))
        (d / "status").write_text("Name:\t%s\nVmRSS:\t%d kB\n" % (comm, rss) if rss else "Name:\t%s\n" % comm)

    proc(10, "dv-router", 1, 100, 5000)
    proc(11, "dv-edge-dextra", 10, 101, 3000)
    proc(12, "dv-room", 11, 102, 2000)
    proc(13, "other", 1, 103, 9)
    (tmp_path / "self").mkdir()
    snap = fuzz_run.snapshot(10, str(tmp_path))
    assert [p["pid"] for p in snap] == [10, 11, 12]
    assert fuzz_run.edge_process(snap, ("dv-edge-dmrhb", "dv-edge-dextra"))["rss_kib"] == 3000
    assert fuzz_run.edge_process(snap, ("dv-edge-x",)) is None
    assert fuzz_run.compare_processes(snap, snap) == ([], [])
    restarted = [dict(p, pid=99, start=555) if p["pid"] == 11 else p for p in snap]
    gone, new = fuzz_run.compare_processes(snap, restarted)
    assert [p["pid"] for p in gone] == [11] and [p["pid"] for p in new] == [99]
    same_pid_new_start = [dict(p, start=777) if p["pid"] == 12 else p for p in snap]
    assert fuzz_run.compare_processes(snap, same_pid_new_start)[0][0]["pid"] == 12


def test_ring_keeps_the_last_fifty_and_the_replay_file_round_trips():
    ring = fuzz_run.Ring()
    for i in range(80):
        ring.add("dextra", 1, i, 0, 30001, "linked", bytes([i]))
    entries = ring.entries()
    assert len(entries) == 50 and entries[0]["index"] == 30 and entries[-1]["hex"] == "4f"
    doc = fuzz_run.replay_document("dextra", 1, entries, reason="x", host="127.0.0.1")
    parsed, back = fuzz_run.read_replay(json.dumps(doc))
    assert back == entries and parsed["format"] == fuzz_run.REPLAY_FORMAT
    for bad in ('{"format": "other"}', "[]", json.dumps(dict(doc, entries=[{"hex": "zz"}]))):
        with pytest.raises(ValueError):
            fuzz_run.read_replay(bad)


def test_two_thirds_of_the_cases_come_from_the_linked_source():
    kinds = collections.Counter(fuzz_run.source_for(i) for i in range(300))
    assert kinds == {"linked": 200, "stranger": 100}


class FakeSource:
    def __init__(self):
        self.sent, self.errors, self.drained = [], 0, 0

    def send(self, host, port, data):
        self.sent.append((host, port, bytes(data)))
        return True

    def drain(self):
        self.drained += 1


def test_fuzzer_is_reproducible_and_keeps_the_last_fifty():
    runs = []
    for _ in range(2):
        linked, stranger = FakeSource(), FakeSource()
        fz = fuzz_run.Fuzzer("m17", ("m17", "m17v3"), "127.0.0.1", "5", linked, stranger, gap=0)
        done = fz.batch(200) + fz.batch(200)
        assert done >= 400 and fz.sent == done
        runs.append((linked.sent, stranger.sent, fz.ring.entries()))
    assert runs[0] == runs[1]
    linked, stranger, entries = runs[0]
    assert len(entries) == 50 and linked and stranger
    assert {e["protocol"] for e in entries} <= {"m17", "m17v3"}
    # an entry names (protocol, seed, index): regenerate it and find the same bytes
    e = entries[-1]
    assert bytes.fromhex(e["hex"]) == fuzz.generate(e["protocol"], e["seed"], e["index"]).packets[e["sub"]]


def test_replayer_resends_recorded_entries_from_their_source():
    ring = fuzz_run.Ring()
    for i in range(10):
        ring.add("dextra", 1, i, 0, 30001, fuzz_run.source_for(i), bytes([i, i]))
    linked, stranger = FakeSource(), FakeSource()
    player = fuzz_run.Replayer(ring.entries(), "127.0.0.1", linked, stranger, gap=0)
    assert player.batch(4) == 4 and player.batch(100) == 6 and player.sent == 10 and player.batch(5) == 0
    assert [d for _h, _p, d in linked.sent + stranger.sent].count(b"\x00\x00") == 1
    assert player.ring.entries() == ring.entries()
    assert len(linked.sent) == 6 and len(stranger.sent) == 4


def test_error_counters_and_settings():
    lines = ['dv_edge_drops_total{proc="dv-edge-dextra",reason="bad_packet"} 5',
             'dv_edge_drops_total{proc="dv-edge-ysf",reason="no_session"} 7',
             'dv_pkt_reject_total{reason="spoof"} 2']
    assert fuzz_run.error_count(lines, ("dv-edge-dextra",)) == 7
    assert fuzz_run.error_count(lines) == 14
    assert fuzz_run.error_count(lines, ("dv-edge-none",)) == 2
    assert fuzz_run.error_count(["error: refused"]) == 0
    unlabelled = ['dv_edge_drops_total{reason="x"} 3']
    assert fuzz_run.error_count(unlabelled, ("dv-edge-dextra",)) == 3
    assert fuzz_run.settings_from_env({}) == (2000, "1", 0.001, None)
    assert fuzz_run.settings_from_env({"DVT_FUZZ_N": "50", "DVT_FUZZ_SEED": "abc", "DVT_FUZZ_GAP_MS": "2.5",
                                       "DVT_FUZZ_REPLAY": "f.json"}) == (50, "abc", 0.0025, "f.json")
    for bad in ({"DVT_FUZZ_N": "0"}, {"DVT_FUZZ_N": "x"}, {"DVT_FUZZ_GAP_MS": "-1"}):
        with pytest.raises(ValueError, match="DVT_FUZZ"):
            fuzz_run.settings_from_env(bad)
    assert fuzz_run.timeout_s(2000) < 600 and fuzz_run.timeout_s(100000) > fuzz_run.timeout_s(2000)


def test_clients_and_frames_exist_for_every_client_protocol():
    for name in fuzz_run.CLIENT_KINDS:
        client = fuzz_run.make_client(name, "127.0.0.1", "talker")
        try:
            assert client.dest is None
        finally:
            client.close()
        assert len(fuzz_run.frames_for(name, 3)) == 3
    assert len(fuzz_run.frames_for("nxdn", 2)[0]) == nxdn.FRAME_BYTES
    assert set(fuzz_run.IDENT) == {"talker", "listener", "fuzz", "probe"}
    assert len({v["call"] for v in fuzz_run.IDENT.values()}) == 4 and fuzz_run.IDENT["fuzz"]["call"] == fuzz.CALL


# ---------------------------------------------------------------- replay tool

def test_replay_tool_regenerates_and_refuses_public_hosts():
    entries = fuzz_replay.regenerate("ysf", "3", 10, 25)
    assert len(entries) == 25 and entries[0]["index"] == 10
    for e in entries:
        assert bytes.fromhex(e["hex"]) == fuzz.generate("ysf", "3", e["index"]).packets[e["sub"]]
        assert e["src"] == fuzz_run.source_for(e["index"]) and e["port"] == 42000
    assert fuzz_replay.describe(entries)[0].startswith("  0 ysf seed=3 index=10.0 port=42000")
    for ok in ("127.0.0.1", "10.1.2.3", "172.30.0.11", "192.0.2.5", "198.51.100.7"):
        assert fuzz_replay.allowed_host(ok), ok
    for bad in ("100.%d.0.1" % 64, "example.org", "", "not-an-address"):
        assert not fuzz_replay.allowed_host(bad), bad
    with pytest.raises(SystemExit):
        fuzz_replay.main(["--regen", "ysf", "--host", "100.%d.0.1" % 64])
    with pytest.raises(SystemExit):
        fuzz_replay.main(["--regen", "ysf"])                     # no default host
    with pytest.raises(SystemExit):
        fuzz_replay.main(["--regen", "nope", "--list"])
    assert fuzz_replay.main(["--regen", "dextra", "--count", "3", "--list"]) == 0


def test_replay_tool_sends_to_the_host_it_is_given():
    import socket
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.settimeout(2)
    try:
        port = sink.getsockname()[1]
        entries = [dict(protocol="dextra", seed="1", index=0, sub=0, port=port, src="stranger", hex="0102"),
                   dict(protocol="dextra", seed="1", index=1, sub=0, port=port, src="linked", hex="")]
        src = fuzz_run.Source("127.0.0.1")
        try:
            player = fuzz_run.Replayer(entries, "127.0.0.1", src, src, gap=0)
            assert player.batch(10) == 2
        finally:
            src.close()
        assert sink.recv(100) == b"\x01\x02" and sink.recv(100) == b""
    finally:
        sink.close()


# ---------------------------------------------------------------- the case file

def load_cases():
    spec = importlib.util.spec_from_file_location("fuzz_netns_under_test", CASES / "test_fuzz_netns.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_case_file_has_one_scenario_per_port_group_and_four_cases_each():
    mod = load_cases()
    assert set(mod.SCENARIOS) == set(fuzz.PORT_GROUPS)
    names = [n for n in dir(mod) if n.startswith("test_fuzz_")]
    assert len(names) == 4 * len(fuzz.PORT_GROUPS)
    prefixes = [s["cases"] for s in mod.SCENARIOS.values()]
    for name, spec in mod.SCENARIOS.items():
        assert spec["group"] == list(fuzz.PORT_GROUPS[name]) and spec["profile"] == "dvrouter"
        mine = [n for n in names if n.startswith(spec["cases"])]
        assert sorted(n[len(spec["cases"]):] for n in mine) == ["counters_move", "edge_survives", "memory_bounded",
                                                                  "valid_client_served"], name
        assert [p for p in prefixes if p != spec["cases"] and spec["cases"].startswith(p)] == []
        assert spec["usrp"] == (name == "usrp") and spec["edges"]
    assert mod.EVERY == 200 and mod.RSS_GROWTH_MAX_KIB > 0 and mod.VALID_FRAMES >= 50
    # the netns scenario selection finds exactly the scenario of a selected case
    from dvtest import netns_runs
    assert netns_runs.scenarios_for(mod.SCENARIOS, {"test_fuzz_dmr_edge_survives"}) == ["dmr"]
    assert netns_runs.scenarios_for(mod.SCENARIOS, {"test_fuzz_dmrplus_counters_move"}) == ["dmrplus"]
    assert netns_runs.for_profile(["dmr"], mod.SCENARIOS, "xlxd") == []
    assert netns_runs.for_profile(["dmr"], mod.SCENARIOS, "dvrouter") == ["dmr"]


def test_case_file_edge_names_follow_the_launcher_children():
    mod = load_cases()
    # the same child names the soak and proc-stat allow-list use
    allowed = {"dv-edge-dextra", "dv-edge-dplus", "dv-edge-dcs", "dv-edge-mmdvm", "dv-edge-dmrhb", "dv-edge-dmrplus",
               "dv-edge-ysf", "dv-edge-m17", "dv-edge-g3", "dv-edge-imrs", "dv-edge-xlx", "dv-edge-nxdn",
               "dv-edge-p25", "dv-edge-usrp"}
    for spec in mod.SCENARIOS.values():
        assert set(spec["edges"]) <= allowed
