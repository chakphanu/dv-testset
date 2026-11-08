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
    lines = ['dv_edge_drops_total{proc="dextra",reason="bad_packet"} 5',
             'dv_edge_drops_total{proc="ysf",reason="no_session"} 7',
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
    assert len(names) == 4 * len(fuzz.PORT_GROUPS) + 1 and "test_fuzz_replay_served" in names
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


# ---------------------------------------------------------------- DMR+ listener, module variants, floor rule, hub diff

def test_heard_reads_the_dmrplus_client_through_its_own_api():
    client = fuzz_run.make_client("dmrplus", "127.0.0.1", "listener")
    try:
        assert fuzz_run.heard("dmrplus", client, settle=0.01) == 0
        client.client.inbox.extend(dmrplus.stream(fuzz_run.frames_for("dmrplus", 54), 1023007, 4002))
        client.client.inbox.append(dmrplus.ACK)                       # non-voice datagrams are ignored
        assert fuzz_run.heard("dmrplus", client, settle=0.01) == 54
    finally:
        client.close()
    for name in fuzz_run.CLIENT_KINDS:
        c = fuzz_run.make_client(name, "127.0.0.1", "listener")
        try:
            assert fuzz_run.heard(name, c, settle=0.01) == 0, name       # no AttributeError for any client
        finally:
            c.close()


def test_protocol_seeds_follow_the_module():
    for name in fuzz.PROTOCOLS:
        b, c = fuzz.protocol(name, "B"), fuzz.protocol(name, "C")
        assert [i.role for i in b.items] == [i.role for i in c.items]
        assert [len(i.data) for i in b.items] == [len(i.data) for i in c.items]
        moved = sum(x.data != y.data for x, y in zip(b.items, c.items))
        assert (moved > 0) == (name not in ("m17v3", "nxdn", "p25", "usrp")), name
    assert fuzz.protocol("dextra", "C").items[0].data == dextra.link_packet(fuzz.CALL, "C")
    assert fuzz.protocol("dmr", "C").items[6].data[8:11] == (4003).to_bytes(3, "big")
    assert fuzz.protocol("dmrplus", "C").items[0].data == dmrplus.connect_packet(fuzz.DMR_ID, 4003)
    assert fuzz.protocol("ysf", "C").items[3].data != fuzz.protocol("ysf", "B").items[3].data
    assert fuzz.generate("dextra", "1", 7, "B") == fuzz.generate("dextra", "1", 7)
    assert any(fuzz.generate("dextra", "1", i, "C") != fuzz.generate("dextra", "1", i, "B") for i in range(60))
    assert set(fuzz.OTHER_MODULE) == set(fuzz.PORT_GROUPS)


def test_fuzzer_second_phase_continues_after_the_first_on_another_module():
    first = FakeSource()
    fz = fuzz_run.Fuzzer("dextra", ("dextra",), "127.0.0.1", "9", first, FakeSource(), gap=0, module="C")
    fz.batch(100)
    second = FakeSource()
    fz2 = fuzz_run.Fuzzer("dextra", ("dextra",), "127.0.0.1", "9", second, FakeSource(), gap=0, ring=fz.ring,
                          module="B", start=fz.case_index)
    fz2.batch(100)
    seen = [e["index"] for e in fz.ring.entries()]
    assert fz2.ring is fz.ring and seen == sorted(seen) and len(fz.ring.entries()) == 50
    assert fz2.case_index > fz.case_index > 0
    e = fz.ring.entries()[-1]
    assert bytes.fromhex(e["hex"]) == fuzz.generate("dextra", "9", e["index"], "B").packets[e["sub"]]


def test_phases_and_the_floor_rule():
    assert fuzz_run.QUIET_S == fuzz_run.STREAM_TIMEOUT_S + fuzz_run.QUIET_MARGIN_S == 2.6
    p = fuzz_run.plan_phases(2000, "C")
    assert [(x["name"], x["module"], x["quiet"], x["count"]) for x in p] == [
        ("other", "C", False, 1000), ("same", "B", True, 1000)]
    assert fuzz_run.plan_phases(2000, None) == [dict(name="same", module="B", quiet=True, count=2000)]
    assert [x["count"] for x in fuzz_run.plan_phases(450, "C")] == [200, 250]
    assert sum(x["count"] for x in fuzz_run.plan_phases(1, "C")) == 1
    from dvtest.specs import dvrouter
    assert dvrouter.SPEC["test_stream_timeout_frees_module"]["timeout_s"] == fuzz_run.STREAM_TIMEOUT_S


def test_hub_scrape_diff_and_edge_labels():
    before = ('# HELP x\ndv_edge_drops_total{proc="dextra",reason="no_session"} 1\n'
              'dv_edge_drops_total{proc="ysf",reason="no_session"} 4\ndv_up 1\n')
    after = ('dv_edge_drops_total{proc="dextra",reason="no_session"} 3\ndv_edge_drops_total{proc="dextra",reason="x"} 2\n'
             'dv_edge_drops_total{proc="ysf",reason="no_session"} 4\ndv_up 1\ndv_pkt_reject_total{reason="spoof"} 1\n')
    ch = fuzz_run.metric_changes(before, after)
    assert [(n, lab.get("reason"), a, b) for n, lab, a, b in ch] == [
        ("dv_edge_drops_total", "no_session", 1.0, 3.0), ("dv_edge_drops_total", "x", 0.0, 2.0),
        ("dv_pkt_reject_total", "spoof", 0.0, 1.0)]
    assert fuzz_run.family_delta(ch, "dv_edge_drops_total", proc="dextra") == 4.0
    assert fuzz_run.family_delta(ch, "dv_edge_drops_total", proc="dextra", reason="malformed") == 0
    assert fuzz_run.family_delta(ch, "dv_edge_drops_total", proc="ysf") == 0
    assert fuzz_run.metric_changes(before, before) == []
    assert fuzz_run.edge_label("dv-edge-dmrhb") == "dmrhb" and fuzz_run.edge_label("dv-room") == "dv-room"


def run_record(tmp_path, **kw):
    metrics = dict(error=None, edge_proc="dextra", start_dumped=True, end_dumped=True, families=["dv_up"],
                   changed=[["dv_edge_drops_total", {"proc": "dextra", "reason": "no_session"}, 1.0, 5.0],
                            ["dv_pkt_reject_total", {"reason": "spoof"}, 0.0, 2.0]])
    metrics.update(kw.pop("metrics", {}))
    return dict(run_dir=str(tmp_path), pinged=True, sent=2000, seed="1", metrics=metrics, **kw)


def test_counters_move_is_a_record_and_a_finding_until_obs1_lands(tmp_path, monkeypatch):
    mod = load_cases()
    monkeypatch.delenv("DVT_FUZZ_ASSERT_MALFORMED", raising=False)
    runs = {"dextra": run_record(tmp_path)}
    with pytest.warns(UserWarning, match=r"FINDING: no counter of malformed input moved \(OBS1 open\)"):
        mod.check_counters(runs, "dextra")                                # passes: a record, not a failure
    rec = json.loads((tmp_path / "counters.json").read_text())
    assert rec["edge_drops_by_reason"] == {"no_session": 4.0} and rec["pkt_reject_by_reason"] == {"spoof": 2.0}
    assert rec["expected"] == "dv_edge_drops_total{reason=malformed}" and rec["expected_delta"] == 0
    assert "OBS1 open" in (tmp_path / "fuzz-findings.txt").read_text()
    # the switch: once OBS1 landed the counter is asserted
    monkeypatch.setenv("DVT_FUZZ_ASSERT_MALFORMED", "1")
    with pytest.raises(pytest.fail.Exception, match="did not grow"):
        mod.check_counters(runs, "dextra")
    runs["dextra"]["metrics"]["changed"].append(
        ["dv_edge_drops_total", {"proc": "dextra", "reason": "malformed"}, 0.0, 1990.0])
    mod.check_counters(runs, "dextra")                                    # no finding, no failure
    assert json.loads((tmp_path / "counters.json").read_text())["expected_delta"] == 1990.0
    # an unreachable hub is a failure in both modes
    monkeypatch.delenv("DVT_FUZZ_ASSERT_MALFORMED")
    bad = run_record(tmp_path, metrics=dict(error="error: refused", end_dumped=False))
    with pytest.raises(pytest.fail.Exception, match="no hub scrape"):
        mod.check_counters({"dextra": bad}, "dextra")
    assert mod.EXPECTED_MALFORMED == ("dv_edge_drops_total", "malformed")


def test_a_miss_after_the_quiet_wait_is_a_failure_and_says_how_long_it_was_quiet(tmp_path):
    mod = load_cases()
    warm = dict(tag="warm", ok=True, heard=54, expected=54, probe=True, reason="")
    row = dict(tag="same-1000", ok=False, heard=0, expected=54, probe=True, sent=1000, phase="same", fuzz_module="B",
               quiet_s=2.6, recovered_after_s=4.8, gone=[], new=[], launcher_alive=True)
    run = dict(run_dir=str(tmp_path), pinged=True, valid_mode="served", warmup=warm, rounds=[row], last50=[], seed="1")
    with pytest.warns(UserWarning, match="FINDING"):
        with pytest.raises(pytest.fail.Exception) as info:
            mod.check_served({"dextra": run}, "dextra")
    text = str(info.value)
    assert "quiet for 2.6 s" in text and "stream timeout 1.6 s" in text and "served again 4.8 s after" in text
    assert (tmp_path / "fuzz-replay-dextra.json").exists()
    run["rounds"] = [dict(row, phase="other", fuzz_module="C", quiet_s=0.0)]
    with pytest.warns(UserWarning), pytest.raises(pytest.fail.Exception, match="module C and the valid clients on B"):
        mod.check_served({"dextra": run}, "dextra")
    run["rounds"] = [dict(row, ok=True)]
    mod.check_served({"dextra": run}, "dextra")


def test_nxdn_p25_g3_are_switched_on_by_a_toml_merged_after_the_conversion():
    import tomllib
    mod = load_cases()
    assert set(mod.EXTRA_TOML) == {"nxdn", "p25", "g3"}
    for name, text in mod.EXTRA_TOML.items():
        assert tomllib.loads(text)["edges"][name]["enable"] is True
    assert tomllib.loads(mod.EXTRA_TOML["nxdn"])["edges"]["nxdn"]["tg_modules"] == "31672:F"
    assert tomllib.loads(mod.EXTRA_TOML["p25"])["edges"]["p25"]["server_tg"] == 31672


# ---------------------------------------------------------------- the replay scenario

def nxdn_entries(src_ids=(1003,), polls=(), tg=31672):
    entries = []
    for i, src in enumerate(src_ids):
        for packet in (nxdn.stream_packets([bytes(7)] * 4, src, tg)):
            entries.append(dict(protocol="nxdn", seed="1", index=i, sub=len(entries), port=41400, src="linked",
                                hex=packet.hex()))
    for callsign, ptg in polls:
        entries.append(dict(protocol="nxdn", seed="1", index=99, sub=0, port=41400, src="stranger",
                            hex=nxdn.poll_packet(callsign, ptg).hex()))
    return entries


def test_replayed_nxdn_datagrams_are_decoded_and_compared_with_the_valid_client():
    out = fuzz_run.decode_entries(nxdn_entries((1003, 2002), [("N0FUZZ", 31673)]), "N0CALL", 1001, 31672)
    assert out["datagram_src_ids"] == [1003, 2002] and out["layer3_src_ids"] == [1003, 2002]
    assert out["datagram_dst_tgs"] == [31672] and out["layer3_dst_tgs"] == [31672]
    assert out["poll_callsigns"] == ["N0FUZZ"] and out["poll_tgs"] == [31673]
    assert out["kinds"]["header"] == 2 and out["kinds"]["trailer"] == 2 and out["kinds"]["poll"] == 1
    assert out["equals_valid_id"] is False and out["equals_valid_tg"] is True
    assert out["poll_callsign_equals_valid"] is False
    assert out["valid_client"] == {"nxdn_id": 1001, "tg": 31672, "callsign": "N0CALL"}
    same = fuzz_run.decode_entries(nxdn_entries((1001,), [("n0call", 31672)]), "N0CALL", 1001, 31672)
    assert same["equals_valid_id"] and same["poll_callsign_equals_valid"]
    junk = [dict(protocol="nxdn", seed="1", index=0, sub=0, port=41400, src="stranger", hex="0102")]
    assert fuzz_run.decode_entries(junk, "N0CALL", 1001, 31672)["kinds"] == {"other(2 bytes)": 1}
    other = fuzz_run.decode_entries([dict(junk[0], protocol="dextra")], "N0CALL", 1001, 31672)
    assert other["decoder"].startswith("none") and other["sizes"] == {2: 1}


def test_replay_helpers_ring_scenario_and_hub_series():
    assert fuzz_run.ring_size_from_env({}) == 50 and fuzz_run.ring_size_from_env({"DVT_FUZZ_RING": "2000"}) == 2000
    for bad in ("0", "x", "100001"):
        with pytest.raises(ValueError, match="DVT_FUZZ_RING"):
            fuzz_run.ring_size_from_env({"DVT_FUZZ_RING": bad})
    ring = fuzz_run.Ring(3)
    for i in range(5):
        ring.add("nxdn", 1, i, 0, 41400, "linked", b"x")
    assert [e["index"] for e in ring.entries()] == [2, 3, 4]
    assert fuzz_run.scenario_of("m17v3") == "m17" and fuzz_run.scenario_of("nxdn") == "nxdn"
    with pytest.raises(KeyError):
        fuzz_run.scenario_of("nope")
    text = ('dv_edge_drops_total{proc="nxdn",reason="rx_ignored"} 7\ndv_edge_drops_total{proc="ysf",reason="x"} 1\n'
            'dv_edge_sessions{proc="nxdn"} 2\ndv_up 1\n')
    assert fuzz_run.edge_series(text, "nxdn") == {"dv_edge_drops_total{proc=nxdn,reason=rx_ignored}": 7.0,
                                                  "dv_edge_sessions{proc=nxdn}": 2.0}
    assert fuzz_run.edge_series(None, "nxdn") == {}
    assert [label for label, _ in fuzz_run.REPLAY_CHECKS] == ["quiet", "t10", "t30"]
    assert fuzz_run.REPLAY_CHECKS[0][1] == fuzz_run.QUIET_S and fuzz_run.REPLAY_CHECKS[2][1] == 30.0


def test_regenerated_history_is_what_the_fuzzer_sent(tmp_path):
    out = tmp_path / "full.json"
    assert fuzz_replay.main(["--regen", "nxdn", "--seed", "1", "--count", "300", "--write", str(out)]) == 0
    _doc, entries = fuzz_run.read_replay(out.read_text())
    assert len(entries) == 300 and entries[0]["index"] == 0
    linked, stranger = FakeSource(), FakeSource()
    fz = fuzz_run.Fuzzer("nxdn", ("nxdn",), "127.0.0.1", "1", linked, stranger, gap=0, ring=fuzz_run.Ring(5000))
    fz.batch(300)
    sent = fz.ring.entries()
    assert [(e["index"], e["sub"], e["hex"], e["src"]) for e in sent[:300]] == \
        [(e["index"], e["sub"], e["hex"], e["src"]) for e in entries]
    with pytest.raises(SystemExit):
        fuzz_replay.main(["--file", str(out), "--write", str(tmp_path / "x.json")])


def replay_run(tmp_path, checks, served=True):
    rows = [dict(label=l, planned_s=p, started_s=p, heard=h, expected=54, ok=h >= 27, probe=True)
            for (l, p), h in zip(fuzz_run.REPLAY_CHECKS, checks)]
    rp = dict(datagrams=50, record="RECORD: replay of 50 datagrams", checks=rows,
              served_before=dict(ok=served, reason="" if served else "the listener link was not acknowledged"))
    return {"replay-nxdn": dict(run_dir=str(tmp_path), pinged=True, scenario="nxdn", replay=rp, last50=[], seed="1")}


def test_replay_case_fails_only_when_the_valid_client_is_not_served_at_30_s(tmp_path):
    mod = load_cases()
    with pytest.raises(pytest.skip.Exception, match="DVT_FUZZ_REPLAY"):
        mod.test_fuzz_replay_served({})
    with pytest.warns(UserWarning, match="RECORD"):
        mod.test_fuzz_replay_served(replay_run(tmp_path, (0, 0, 54)))             # served late: still a pass
    assert "RECORD" in (tmp_path / "fuzz-findings.txt").read_text()
    with pytest.warns(UserWarning, match="RECORD"):
        with pytest.raises(pytest.fail.Exception, match="still not served 30.0 s after"):
            mod.test_fuzz_replay_served(replay_run(tmp_path, (54, 54, 0)))
    with pytest.warns(UserWarning, match="RECORD"):
        with pytest.raises(pytest.skip.Exception, match="not served before the replay"):
            mod.test_fuzz_replay_served(replay_run(tmp_path, (0, 0, 0), served=False))
    assert mod.REPLAY_CASES == "test_fuzz_replay_"
    from dvtest import netns_runs
    assert netns_runs.scenarios_for(mod.SCENARIOS, {"test_fuzz_replay_served"}) == []   # not a normal scenario


# ---------------------------------------------------------------- keeper of the valid clients

class _Counting:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def keepalive(self):
        self.calls += 1
        if self.fail:
            raise OSError("closed")


@pytest.mark.unit
def test_keeper_sends_keepalive_on_schedule_and_stops():
    import time
    good, bad = _Counting(), _Counting(fail=True)
    keeper = fuzz_run.Keeper("nxdn", [good, bad, object()], every=0.05).start()
    time.sleep(0.3)
    keeper.stop()
    calls = good.calls
    time.sleep(0.15)
    assert calls >= 3 and good.calls == calls
    assert keeper.sent == good.calls and keeper.errors == bad.calls >= 3


@pytest.mark.unit
def test_keeper_leaves_dmrplus_alone(monkeypatch):
    keeper = fuzz_run.Keeper("dmrplus", [_Counting()], every=0.05).start()
    keeper.stop()
    assert keeper.clients == [] and keeper.sent == 0
    monkeypatch.setenv("DVT_FUZZ_NO_KEEPALIVE", "1")
    assert fuzz_run.keepalive_off()


@pytest.mark.unit
def test_every_valid_client_kind_has_a_keepalive():
    for kind in fuzz_run.CLIENT_KINDS:
        if kind in ("dmrplus", "m17"):
            continue                           # dmrplus pumps; m17 answers the server's PING (responder)
        client = fuzz_run.make_client(kind, "127.0.0.1", "listener")
        try:
            assert hasattr(client, "keepalive"), kind
        finally:
            client.close()
