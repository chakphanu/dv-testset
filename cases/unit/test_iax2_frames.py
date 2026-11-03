# SPDX-License-Identifier: MIT
"""dvtest.iax2_frames: IAX2 frames of a capture as hex per type, from synthetic captures
built with dvtest.pcap.write (no network, no rig)."""
import hashlib
import json
import struct

import pytest

from dvtest import iax2_frames as fr
from dvtest import pcap
from dvtest.proto import iax2

A, B = "192.0.2.1", "192.0.2.2"
PORT = 4569


def ie(code, value):
    return bytes((code, len(value))) + value


def full(scall, dcall, ts, oseq, iseq, kind, sub, payload=b"", retrans=False):
    return iax2.full_frame(scall, dcall, ts, oseq, iseq, kind, sub, payload, retransmit=retrans)


def mini(scall, ts16, payload=b"\xff" * 160):
    return struct.pack("!HH", scall, ts16) + payload


def capture(tmp_path, datagrams, fmt="pcapng", name="x.pcap"):
    path = tmp_path / name
    packets = [(1000.0 + i * 0.02, pcap.ethernet_udp(src, sp, dst, dp, data))
               for i, (src, sp, dst, dp, data) in enumerate(datagrams)]
    pcap.write(str(path), packets, fmt=fmt)
    return path


NEW = full(5, 0, 0, 0, 0, 6, 1, ie(2, b"199901") + ie(6, b"radio") + ie(8, struct.pack("!I", 4)) + ie(54, b""))


def test_new_frame_is_decoded_with_its_hex(tmp_path):
    path = capture(tmp_path, [(A, PORT, B, PORT, NEW)])
    doc = fr.extract(path)
    (f,) = doc["types"]["iax.NEW"]
    assert f["hex"] == NEW.hex()
    assert (f["scall"], f["dcall"], f["oseq"], f["iseq"], f["retrans"]) == (5, 0, 0, 0, False)
    names = {i["name"]: i for i in f["ies"]}
    assert names["CALLING_NUMBER"]["text"] == "199901"
    assert names["USERNAME"]["text"] == "radio"
    assert names["CAPABILITY"]["formats"] == ["ulaw"]
    assert names["CALLTOKEN"]["len"] == 0
    assert doc["summary"] == {"iax.NEW": 1}


@pytest.mark.parametrize("sub,name", [(2, "PING"), (3, "PONG"), (4, "ACK"), (5, "HANGUP"), (7, "ACCEPT"), (8, "AUTHREQ"),
                                      (9, "AUTHREP"), (11, "LAGRQ"), (12, "LAGRP"), (13, "REGREQ"), (14, "REGAUTH"),
                                      (15, "REGACK"), (16, "REGREJ"), (40, "CALLTOKEN")])
def test_iax_subclass_names(tmp_path, sub, name):
    path = capture(tmp_path, [(A, PORT, B, PORT, full(5, 6, 20, 1, 1, 6, sub))])
    assert list(fr.extract(path)["types"]) == ["iax." + name]


def test_control_text_cng_and_full_voice(tmp_path):
    data = [
        (A, PORT, B, PORT, full(5, 6, 20, 1, 1, 4, 12)),                       # RADIO_KEY
        (A, PORT, B, PORT, full(5, 6, 40, 2, 1, 4, 13)),                       # RADIO_UNKEY
        (A, PORT, B, PORT, full(5, 6, 41, 3, 1, 4, 4)),                        # ANSWER
        (A, PORT, B, PORT, full(5, 6, 60, 4, 1, 7, 0, b"!NEWKEY1!\0")),
        (A, PORT, B, PORT, full(5, 6, 61, 5, 1, 10, 0)),
        (A, PORT, B, PORT, full(5, 6, 62, 6, 1, 2, 4, b"\xff" * 160)),         # voice, ulaw
    ]
    doc = fr.extract(capture(tmp_path, data))
    assert set(doc["types"]) == {"control.RADIO_KEY", "control.RADIO_UNKEY", "control.ANSWER", "text", "cng", "voice.full"}
    assert doc["types"]["text"][0]["text"] == "!NEWKEY1!\x00"
    assert doc["types"]["voice.full"][0]["codec"] == "ulaw"


def test_retransmit_bit_and_big_subclass(tmp_path):
    path = capture(tmp_path, [(A, PORT, B, PORT, full(5, 6, 20, 1, 1, 2, 64, b"\0" * 320, retrans=True))])
    (f,) = fr.extract(path)["types"]["voice.full"]
    assert f["retrans"] is True
    assert f["subclass"] == 64
    assert f["codec"] == "slin"


def test_mini_frames_and_ts16_wrap(tmp_path):
    ts = [0xFFC0, 0xFFE0, 0x0000, 0x0020]            # 20 ms steps over the 16-bit wrap
    data = [(A, PORT, B, PORT, mini(5, t)) for t in ts] + [(B, PORT, A, PORT, mini(6, 0x0100))]
    doc = fr.extract(capture(tmp_path, data))
    assert doc["summary"] == {"voice.mini": 5}
    assert len(doc["ts16_wraps"]) == 1
    wrap = doc["ts16_wraps"][0]
    assert (wrap["before"]["ts16"], wrap["after"]["ts16"]) == (0xFFE0, 0x0000)
    assert wrap["before"]["scall"] == wrap["after"]["scall"] == 5


def test_limit_keeps_wrap_frames_but_counts_all(tmp_path):
    ts = list(range(0xFF00, 0x10000, 0x20)) + [0, 0x20, 0x40]
    data = [(A, PORT, B, PORT, mini(5, t)) for t in ts]
    doc = fr.extract(capture(tmp_path, data), limit=2)
    assert doc["summary"]["voice.mini"] == len(ts)
    kept = [f["ts16"] for f in doc["types"]["voice.mini"]]
    assert kept[:2] == [0xFF00, 0xFF20]
    assert 0xFFE0 in kept and 0 in kept            # the frames around the wrap survive the limit


def test_other_ports_and_garbage_are_counted_not_decoded(tmp_path):
    data = [(A, 9999, B, 9998, NEW), (A, PORT, B, PORT, b"\x80\x05"), (A, PORT, B, PORT, NEW)]
    doc = fr.extract(capture(tmp_path, data))
    assert doc["skipped"] == {"non_iax_port": 1, "bad": 1}
    assert doc["summary"] == {"iax.NEW": 1}
    assert fr.extract(capture(tmp_path, data, name="y.pcap"), ports=(9999,))["summary"] == {"iax.NEW": 1}


def test_meta_frame_and_truncated_ie(tmp_path):
    bad_ie = full(5, 6, 20, 1, 1, 6, 6, b"\x06\x09ab")           # IE says 9 bytes, 2 present
    data = [(A, PORT, B, PORT, b"\x00\x00\x01\x00" + b"\0" * 8), (A, PORT, B, PORT, bad_ie)]
    doc = fr.extract(capture(tmp_path, data))
    assert doc["summary"] == {"iax.REJECT": 1, "meta": 1}
    assert "ies_error" in doc["types"]["iax.REJECT"][0]


def test_classic_pcap_and_timestamps(tmp_path):
    path = capture(tmp_path, [(A, PORT, B, PORT, NEW), (B, PORT, A, PORT, full(6, 5, 1, 0, 1, 6, 7))], fmt="pcap")
    doc = fr.extract(path)
    assert sorted(f["t"] for t in doc["types"].values() for f in t) == [0.0, 0.02]


def test_anon_rewrites_addresses_in_first_seen_order(tmp_path):
    path = capture(tmp_path, [("10.9.8.7", PORT, "10.9.8.6", PORT, NEW), ("10.9.8.6", PORT, "10.9.8.7", PORT, mini(5, 1))])
    doc = fr.extract(path, anon=True)
    assert doc["types"]["iax.NEW"][0]["src"] == "192.0.2.1:4569"
    assert doc["types"]["iax.NEW"][0]["dst"] == "192.0.2.2:4569"
    assert doc["types"]["voice.mini"][0]["src"] == "192.0.2.2:4569"
    assert "10.9.8" not in json.dumps({k: v for k, v in doc.items() if k != "capture_stats"}).replace(NEW.hex(), "")


def dns_message(name, qtype, answers=(), rcode=0, qr=1):
    def enc(n):
        return b"".join(bytes((len(p),)) + p.encode() for p in n.split(".")) + b"\0"
    msg = struct.pack("!HHHHHH", 0x1234, (qr << 15) | 0x0100 | rcode, 1, len(answers), 0, 0) + enc(name) + struct.pack("!HH", qtype, 1)
    for rtype, rdata in answers:
        msg += b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 30, len(rdata)) + rdata
    return msg


def test_dns_srv_a_txt_answers(tmp_path):
    name = "_iax._udp.199901.nodes.asl.test"
    srv = struct.pack("!HHH", 0, 0, 4569) + b"\x06199901\x05nodes\x03asl\x04test\x00"
    txt = b"\x09NN=199901" + b"\x0eIP=192.0.2.9"
    data = [(A, 40000, B, 53, dns_message(name, 33, qr=0)),
            (B, 53, A, 40000, dns_message(name, 33, [(33, srv)])),
            (B, 53, A, 40001, dns_message("199901.nodes.asl.test", 1, [(1, bytes((192, 0, 2, 9)))])),
            (B, 53, A, 40002, dns_message("199901.nodes.asl.test", 16, [(16, txt)])),
            (B, 53, A, 40003, dns_message("nope.nodes.asl.test", 33, rcode=3))]
    doc = fr.extract(capture(tmp_path, data))
    assert doc["summary"] == {}
    dns = doc["dns"]
    assert dns[0]["qr"] == 0 and dns[0]["questions"] == [{"name": name, "type": "SRV"}]
    assert dns[1]["answers"][0]["text"] == "0 0 4569 199901.nodes.asl.test"
    assert dns[2]["answers"][0]["text"] == "192.0.2.9"
    assert dns[3]["answers"][0]["text"] == '"NN=199901" "IP=192.0.2.9"'
    assert dns[4]["rcode"] == 3


def test_main_writes_json_file(tmp_path):
    path = capture(tmp_path, [(A, PORT, B, PORT, NEW)])
    out = tmp_path / "o.json"
    assert fr.main([str(path), "-o", str(out), "--limit", "1", "--anon"]) == 0
    doc = json.loads(out.read_text())
    assert doc["version"] == 1 and doc["source"] == "x.pcap" and doc["frames"] == 1


def test_tone_is_deterministic_ulaw():
    from dvtest import asl_lab
    data = asl_lab.tone_ulaw(100)
    assert len(data) == 800
    assert hashlib.sha256(asl_lab.tone_ulaw(100)).hexdigest() == hashlib.sha256(data).hexdigest()
    assert asl_lab.lin2ulaw(0) == 0xff and asl_lab.lin2ulaw(-1) == 0x7f
    assert asl_lab.lin2ulaw(32767) == 0x80 and asl_lab.lin2ulaw(-32768) == 0x00
