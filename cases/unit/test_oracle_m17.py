# SPDX-License-Identifier: MIT
"""dvtest.oracle_m17: input files and the parsing of m17-fme's output lines
(format strings read in its source; no m17-fme is run here)."""
from dvtest import ecdsa_p256 as ec
from dvtest import m17v3_vectors as vec
from dvtest import oracle_m17 as o
from dvtest.proto import m17, m17v3 as v3

SAMPLE_VALID = (
    "\n(2026-11-06 10:00:00) M17 IP Stream: 4D01; FN: 0000;"
    "\n(2026-11-06 10:00:00) M17 IP Stream: 4D01; FN: 7FFC;"
    "\n(2026-11-06 10:00:00) M17 IP Stream: 4D01; FN: 7FFF; EOT; Signature Valid;\n")


def test_ipf_lines_and_key_file():
    s = v3.signed_stream(vec.secret("a"), v3.payload_stream("s", 2))
    text = o.ipf_lines(s["packets"])
    lines = text.splitlines()
    assert len(lines) == 6 and all(line.startswith("054,") and len(line) == 4 + 108 for line in lines)
    assert bytes.fromhex(lines[0][4:]) == s["packets"][0] and lines[0][4:] == lines[0][4:].upper()
    pub = ec.public_key(vec.secret("a"))
    key = o.pubkey_text(pub)
    assert len(key) == 128 and bytes.fromhex(key) == pub
    try:
        o.pubkey_text(pub[:63])
    except ValueError:
        pass
    else:
        raise AssertionError("short key accepted")


def test_legacy_view_relabels_and_keeps_signature():
    s = v3.signed_stream(vec.secret("a"), v3.payload_stream("s", 3))
    legacy = o.legacy_view(s["packets"], 0x0885)
    assert all(m17.parse_stream(p)["type"] == 0x0885 and m17.parse_stream(p)["crc_ok"] for p in legacy)
    assert v3.verify_stream(legacy, ec.public_key(vec.secret("a")))["verdict"] == "valid"
    short = s["packets"][0][:53]
    assert o.legacy_view([short], 0x0885) == [short]


def test_parse_valid_and_invalid():
    ev = o.parse_fme_output(SAMPLE_VALID)
    assert [e["kind"] for e in ev] == ["stream", "stream", "stream", "signature"]
    assert ev[2] == {"kind": "stream", "sid": 0x4D01, "fn": 0x7FFF, "eot": True}
    summary = o.summarize(ev)
    assert summary["packets"] == 3 and summary["signatures"] == [True] and summary["eot_seen"]
    assert summary["last_fn"] == 0x7FFF
    assert o.fme_verdict(summary) == "valid"
    bad = o.summarize(o.parse_fme_output(SAMPLE_VALID.replace("Valid", "Invalid")))
    assert o.fme_verdict(bad) == "invalid_signature"


def test_parse_colours_and_other_markers():
    text = "\x1b[32m\n(d t) M17 IP Stream: 0001; FN: 0000;\x1b[0m IP CRC ERR\n No Public Key Loaded;"
    summary = o.summarize(o.parse_fme_output(text))
    assert summary["crc_errors"] == 1 and summary["no_key"] and o.fme_verdict(summary) == "no_key"
    rejected = o.summarize(o.parse_fme_output("\n(d t) M17 IP Stream: 0001; FN: 0000; Unknown LSF TYPE;"))
    assert o.fme_verdict(rejected) == "rejected_type"
    assert o.fme_verdict(o.summarize(o.parse_fme_output("nothing"))) == "no_input"
    no_sig = o.summarize(o.parse_fme_output("\n(d t) M17 IP Stream: 0001; FN: 0003;"))
    assert o.fme_verdict(no_sig) == "no_verdict"


def test_compare_rules():
    valid = {"id": "v", "expect": {"verdict": "valid", "also_acceptable": []}}
    ok = o.summarize(o.parse_fme_output(SAMPLE_VALID))
    assert o.compare(valid, ok)["agrees"] is True
    bad = o.summarize(o.parse_fme_output(SAMPLE_VALID.replace("Valid", "Invalid")))
    assert o.compare(valid, bad)["agrees"] is False
    swapped = {"id": "r", "expect": {"verdict": "invalid_signature", "also_acceptable": ["valid"]}}
    assert o.compare(swapped, ok)["agrees"] is True
    inc = {"id": "i", "expect": {"verdict": "incomplete", "also_acceptable": []}}
    assert o.compare(inc, ok)["agrees"] is False
    assert o.compare({"id": "m", "expect": {"verdict": "malformed"}}, ok)["agrees"] is None
    assert o.compare({"id": "u", "expect": {"verdict": "unsigned"}}, ok)["agrees"] is None


def test_docker_argv_is_offline_and_capped():
    argv = o.docker_argv("dvt-m17fme:local", "/work", "stream.ipf", "pub.txt", env={})
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--memory" in argv and "--pids-limit" in argv
    assert argv[-3:] == ["verify", "/data/stream.ipf", "/data/pub.txt"] and "/work:/data:ro" in argv


def test_verify_entry_with_a_fake_runner(tmp_path):
    import json
    import os
    doc = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                      "vectors", "m17v3", "signed_streams.json")))
    entry = next(e for e in doc["entries"] if e["id"] == "valid")
    seen = {}

    def fake(argv):
        seen["argv"] = argv
        return {"argv": argv, "returncode": 0, "timed_out": False, "stdout": SAMPLE_VALID}

    res = o.verify_entry(entry, ec.public_key(vec.secret("a")), "img", str(tmp_path), run=fake)
    assert res["agrees"] is True and res["fme"] == "valid"
    first = (tmp_path / "stream.ipf").read_text().splitlines()[0]
    assert m17.parse_stream(bytes.fromhex(first[4:]))["type"] == 0x0885      # legacy label
    assert len((tmp_path / "pub.txt").read_text()) == 128
    big = next(e for e in doc["entries"] if e["id"] == "boundary-max-frames")
    assert o.verify_entry(big, b"", "img", str(tmp_path), run=fake) is None
    short_key = next(e for e in doc["entries"] if e["id"] == "bad-key-63-bytes")
    assert o.verify_entry(short_key, bytes.fromhex(short_key["verify_public_xy"]), "img", str(tmp_path), run=fake) is None
