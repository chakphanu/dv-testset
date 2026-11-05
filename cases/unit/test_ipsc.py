# SPDX-License-Identifier: MIT
"""IPSC packet encoders and decoders, the HMAC-SHA1 authentication, the
voice call generator and the simulated peer (dvtest/proto/ipsc.py).

Vectors: the control packets are built by hand from the field tables of
docs/ipsc-phase0.md (ids are placeholders). Two groups of values are
measurements published in the DMRlink documentation (documents/ and
DO_NOT_README.md of the repository pinned in the doc): the authentication
digest of one registration packet and the RS(12,9) parity of four captured
voice headers / terminators. They are data, listed once below."""
import socket
import threading

import pytest

from dvtest.audio import signals
from dvtest.proto import dmr, ipsc, nxdn

PEER = 1000001                      # placeholder repeater ids
MASTER = 1000000
SRC = 1234567
TG = 91

# DMRlink DO_NOT_README.md, "AUTHENTICATION": registration packet, key "12345"
DOC_AUTH_PACKET = bytes.fromhex("90000000016a000080dc04030400")
DOC_AUTH_DIGEST = bytes.fromhex("b0ec45f4c3f8fb0c0b1d")
# DMRlink documents/voice_burst_decoding.txt and voice_packets.txt: LC, parity, burst type
DOC_RS = (
    ("000000000002" "2f7cca", "92af70", ipsc.VOICE_HEAD),
    ("000000000002" "2f7cca", "9da07f", ipsc.VOICE_TERM),
    ("001020000c30" "2f9be5", "dad45a", ipsc.VOICE_HEAD),
    ("001020000c30" "2f9be5", "d5db55", ipsc.VOICE_TERM),
)


def frames49(count, seed=1):
    """count AMBE fields (19 bytes) from the deterministic DMR frames."""
    nine = ipsc.valid_dmr_frames(signals.prng_frames("dmr", 3 * count, seed=seed))
    return [ipsc.ambe_from_dmr(nine[i:i + 3]) for i in range(0, len(nine), 3)]


# ------------------------------------------------------------------ authentication
def test_auth_digest_vector():
    key = ipsc.auth_key("12345")
    assert key == bytes(17) + bytes.fromhex("012345") and len(key) == 20
    assert ipsc.digest(key, DOC_AUTH_PACKET) == DOC_AUTH_DIGEST
    signed = ipsc.sign(key, DOC_AUTH_PACKET)
    assert signed == DOC_AUTH_PACKET + DOC_AUTH_DIGEST
    assert ipsc.strip_auth(key, signed) == DOC_AUTH_PACKET


def test_auth_rejects():
    key = ipsc.auth_key("A1B2C3")
    signed = ipsc.sign(key, ipsc.peer_list_req(PEER))
    assert len(signed) == 5 + 10
    tampered = bytearray(signed)
    tampered[2] ^= 1
    assert ipsc.strip_auth(key, bytes(tampered)) is None
    assert ipsc.strip_auth(key, signed[:10]) is None             # not longer than the digest
    assert ipsc.strip_auth(ipsc.auth_key("A1B2C4"), signed) is None
    with pytest.raises(ipsc.AuthError):
        ipsc.decode(bytes(tampered), key)
    assert ipsc.decode(signed, key)["peer_id"] == PEER
    assert ipsc.sign(None, b"abc") == b"abc" and ipsc.sign(b"", b"abc") == b"abc"
    with pytest.raises(ValueError):
        ipsc.auth_key("0" * 41)
    with pytest.raises(ValueError):
        ipsc.auth_key("xyz")


# ------------------------------------------------------------------ mode, flags, version
def test_mode_flags_version():
    assert ipsc.mode_byte() == ipsc.MODE_DEFAULT == 0x6A
    assert ipsc.parse_mode(0x6A) == {"op": 1, "radio": 2, "ts1": 2, "ts2": 2}
    assert ipsc.mode_byte(radio=ipsc.RADIO_NONE, ts1=ipsc.SLOT_DISABLED, ts2=ipsc.SLOT_DISABLED) == 0x40
    assert ipsc.mode_byte(ts2=ipsc.SLOT_LOCAL) == 0x69
    assert ipsc.flags_word() == bytes.fromhex("00000004")
    assert ipsc.flags_word(master=True, auth=True) == bytes.fromhex("00000015")
    assert ipsc.flags_word(csbk=True, rpt_mon=True, con_app=True, xnl_con=True, data=True) == \
        bytes.fromhex("0000e08c")
    assert ipsc.flags_word(byte1=0x01, mnis=True) == bytes.fromhex("00810004")   # raw byte 1 OR the named bit
    parsed = ipsc.parse_flags(bytes.fromhex("000080dc"))        # the flags of the DMRlink example packet
    assert parsed["csbk"] and parsed["xnl_con"] and parsed["xnl_master"] and parsed["auth"]
    assert parsed["data"] and parsed["voice"] and not parsed["master"] and not parsed["rpt_mon"]
    with pytest.raises(ValueError):
        ipsc.flags_word(nonsense=True)
    assert ipsc.IPSC_VER == bytes.fromhex("04020401")
    assert ipsc.parse_version(ipsc.IPSC_VER) == {"main_type": 1, "main_version": 2, "compat_type": 1,
                                                 "compat_version": 1, "raw": ipsc.IPSC_VER}
    assert ipsc.parse_version(bytes.fromhex("04030400"))["main_version"] == 3
    assert ipsc.version_field(3, 5, 4, 7) == bytes.fromhex("0c051007")


# ------------------------------------------------------------------ link packets
def test_registration_layouts():
    peer = PEER.to_bytes(4, "big")                                # 000f4241
    assert peer.hex() == "000f4241"
    req = ipsc.master_reg_req(PEER)
    assert req.hex() == "90" "000f4241" "6a" "00000004" "04020401" and len(req) == 14
    reply = ipsc.master_reg_reply(MASTER, 3)
    assert reply.hex() == "91" "000f4240" "6a" "00000005" "0003" "04020401" and len(reply) == 16
    alive = ipsc.master_alive_req(PEER)
    assert alive.hex() == req.hex().replace("90", "96", 1)
    assert ipsc.master_alive_reply(MASTER).hex() == "97" "000f4240" "6a" "00000005" "04020401"
    assert ipsc.peer_list_req(PEER).hex() == "92000f4241"
    assert ipsc.peer_reg_req(PEER).hex() == "94" "000f4241" "04020401"
    assert ipsc.peer_reg_reply(PEER).hex() == "95" "000f4241" "04020401"
    assert ipsc.peer_alive_req(PEER).hex() == "98" "000f4241" "6a" "00000004"
    assert ipsc.peer_alive_reply(PEER).hex() == "99" "000f4241" "6a" "00000004"
    assert ipsc.de_reg_req(PEER).hex() == "9a000f4241"
    assert ipsc.de_reg_reply(MASTER).hex() == "9b000f4240"


def test_peer_list_layout():
    packet = ipsc.peer_list_reply(MASTER, [(PEER, "192.0.2.31", 50000, 0x6A), (PEER + 1, "192.0.2.32", 50001, 0x6A)])
    assert packet.hex() == ("93" "000f4240" "0016"
                            "000f4241" "c000021f" "c350" "6a"
                            "000f4242" "c0000220" "c351" "6a")
    decoded = ipsc.decode(packet)
    assert [p["radio_id"] for p in decoded["peers"]] == [PEER, PEER + 1]
    assert decoded["peers"][1] == {"radio_id": PEER + 1, "ip": "192.0.2.32", "port": 50001, "mode": 0x6A}
    empty = ipsc.decode(ipsc.peer_list_reply(MASTER, []))
    assert empty["peers"] == [] and empty["length"] == 7


def test_decode_control_roundtrip():
    key = ipsc.auth_key("A1B2C3")
    cases = (
        (ipsc.master_reg_req(PEER), "MASTER_REG_REQ"), (ipsc.master_reg_reply(MASTER, 2), "MASTER_REG_REPLY"),
        (ipsc.master_alive_req(PEER), "MASTER_ALIVE_REQ"), (ipsc.master_alive_reply(MASTER), "MASTER_ALIVE_REPLY"),
        (ipsc.peer_list_req(PEER), "PEER_LIST_REQ"), (ipsc.peer_reg_req(PEER), "PEER_REG_REQ"),
        (ipsc.peer_reg_reply(PEER), "PEER_REG_REPLY"), (ipsc.peer_alive_req(PEER), "PEER_ALIVE_REQ"),
        (ipsc.peer_alive_reply(PEER), "PEER_ALIVE_REPLY"), (ipsc.de_reg_req(PEER), "DE_REG_REQ"),
        (ipsc.de_reg_reply(MASTER), "DE_REG_REPLY"),
    )
    for body, name in cases:
        for k in (None, key):
            packet = ipsc.decode(ipsc.sign(k, body), k)
            assert packet["name"] == name and packet["op"] == body[0]
            assert packet["peer_id"] in (PEER, MASTER)
    reg = ipsc.decode(ipsc.master_reg_req(PEER, flags=ipsc.flags_word(auth=True, data=True)))
    assert reg["flags"]["auth"] and reg["flags"]["data"] and reg["mode_fields"]["ts2"] == ipsc.SLOT_IPSC
    assert ipsc.decode(ipsc.master_reg_reply(MASTER, 5))["num_peers"] == 5
    other = ipsc.decode(bytes.fromhex("f0000f424100000000"))
    assert other["name"] == "OPCODE_F0" and other["peer_id"] == PEER
    assert ipsc.decode(bytes.fromhex("ee000f4241"))["name"] == "UNKNOWN_0xEE"


def test_decode_rejects_bad_lengths():
    for bad in (ipsc.master_reg_req(PEER)[:-1], ipsc.master_reg_req(PEER) + b"\x00",
                ipsc.peer_list_req(PEER) + b"\x00", ipsc.peer_reg_req(PEER)[:5], b"",
                ipsc.peer_list_reply(MASTER, [(PEER, "192.0.2.31", 1, 0x6A)])[:-1],
                bytes.fromhex("93000f4240" "000b") + bytes(10)):
        with pytest.raises(ipsc.IpscError):
            ipsc.decode(bad)


# ------------------------------------------------------------------ RS(12,9) and AMBE packing
@pytest.mark.parametrize("lc,parity,burst", DOC_RS)
def test_rs129_documented_captures(lc, parity, burst):
    assert ipsc.rs129(bytes.fromhex(lc), burst).hex() == parity


def test_rs129_properties():
    zero = ipsc.rs129(bytes(9), ipsc.VOICE_HEAD)
    assert zero == bytes((0x96,) * 3) and ipsc.rs129(bytes(9), ipsc.VOICE_TERM) == bytes((0x99,) * 3)
    with pytest.raises(ValueError):
        ipsc.rs129(bytes(8))


def test_ambe_packing_positions():
    one = bytes.fromhex("80000000000000")                        # only the first of 49 bits
    assert ipsc.pack_ambe([one, bytes(7), bytes(7)]).hex() == "80" + "00" * 18
    last = bytes.fromhex("00000000000001")                       # bit 55 is padding, not data
    assert ipsc.pack_ambe([last, bytes(7), bytes(7)]) == bytes(19)
    top = bytes.fromhex("80000000000000")
    field = ipsc.pack_ambe([bytes(7), top, bytes(7)])
    assert int.from_bytes(field, "big") == 1 << (152 - 1 - 50)    # frame b starts at bit 50
    field = ipsc.pack_ambe([bytes(7), bytes(7), top])
    assert int.from_bytes(field, "big") == 1 << (152 - 1 - 100)
    full = bytes.fromhex("ffffffffffff80")                        # 49 ones, 7 pad bits
    field = ipsc.pack_ambe([full] * 3)
    frames, bad = ipsc.unpack_ambe(field)
    assert frames == [full] * 3 and bad == (0, 0)                 # bits 49 and 99 stay clear
    assert field[-1] & 0x07 == 0
    flagged = bytearray(field)
    flagged[6] |= 0x40                                            # bit 49 = bad flag of frame b
    assert ipsc.unpack_ambe(bytes(flagged))[1] == (1, 0)
    with pytest.raises(ValueError):
        ipsc.pack_ambe([full, full])
    with pytest.raises(ValueError):
        ipsc.unpack_ambe(bytes(18))


def test_ambe_bad_flag_bits():
    value = (1 << (152 - 50)) | (1 << (152 - 100))
    assert ipsc.unpack_ambe(value.to_bytes(19, "big"))[1] == (1, 1)


def test_ambe_dmr_roundtrip():
    nine = ipsc.valid_dmr_frames(signals.prng_frames("dmr", 9, seed=3))
    assert all(nxdn.dmr_valid(f) for f in nine)
    for index in range(0, 9, 3):
        field = ipsc.ambe_from_dmr(nine[index:index + 3])
        assert len(field) == 19
        assert ipsc.ambe_to_dmr(field) == nine[index:index + 3]
    assert frames49(2) == frames49(2) and frames49(2) != frames49(2, seed=2)


# ------------------------------------------------------------------ voice packets
def test_header_layout_by_hand():
    ambe = frames49(1)
    packets = ipsc.call_packets(PEER, 7, SRC, TG, ambe, ts=2)
    head = packets[0]
    lc = bytes.fromhex("000000" "00005b" "12d687")
    assert head[:47].hex() == ("80" "000f4241" "07" "12d687" "00005b" "02" "000043e2" "20"
                               "80" "dd" "0000" "00000000" "00000000"
                               "01" "80" "000a" "80" "0a" "0060" + lc.hex())
    assert head[47:50] == ipsc.rs129(lc, ipsc.VOICE_HEAD)
    assert head[50:52].hex() == "0011" and head[52:54] == bytes(2) and len(head) == 54
    term = packets[-1]
    assert term[17] == 0x60 and term[19] == 0x5E and term[30] == 0x02 and term[50:52].hex() == "0012"
    assert term[47:50] == ipsc.rs129(lc, ipsc.VOICE_TERM) and len(term) == 54
    assert [p[19] for p in packets] == [0xDD, 0x5D, 0x5D, 0x5D, 0x5E]    # marker on the first header only
    for index, packet in enumerate(packets):
        assert int.from_bytes(packet[20:22], "big") == index
        assert int.from_bytes(packet[22:26], "big") == 480 * index
        assert packet[5] == 7 and packet[1:5] == PEER.to_bytes(4, "big")


def test_burst_layouts():
    packets = ipsc.call_packets(PEER, 1, SRC, TG, frames49(2), ts=2, headers=1)
    # twelve bursts: A..F twice
    bursts = ipsc.call_packets(PEER, 1, SRC, TG, frames49(6) * 2, ts=2, headers=1)[1:-1]
    assert len(bursts) == 12
    sizes = [52, 57, 57, 57, 66, 57]
    assert [len(p) for p in bursts] == sizes * 2
    assert {p[30] for p in bursts} == {0x8A}
    assert [p[31] for p in bursts[:6]] == [0x14, 0x19, 0x19, 0x19, 0x22, 0x19]
    assert [p[32] for p in bursts[:6]] == [0x40, 0x06, 0x06, 0x06, 0x16, 0x06]
    for packet in bursts:
        assert 32 + packet[31] == len(packet)                    # length to follow covers byte 32 on
    assert [p[56] for p in bursts[1:4]] == [0x12, 0x16, 0x16] and bursts[5][56] == 0x10
    assert bursts[5][52:56] == bytes(4)
    lc = dmr.full_lc(dmr.FLCO_GROUP, SRC, TG)
    assert bursts[4][56:65] == lc and bursts[4][65] == 0x14
    # the embedded LC fragments of B..E rebuild the LC
    fragments = [int.from_bytes(bursts[i][52:56], "big") for i in range(1, 5)]
    rebuilt, fec_ok, checksum_ok = dmr.decode_embedded_lc(fragments)
    assert rebuilt == lc and fec_ok and checksum_ok
    # the timeslot sits in the burst type and in the call info
    slot1 = ipsc.call_packets(PEER, 1, SRC, TG, frames49(1), ts=1)
    assert {p[30] for p in slot1[3:-1]} == {0x0A} and all(p[17] & 0x20 == 0 for p in slot1)
    assert slot1[0][31] == 0x00 and slot1[-1][17] == 0x40


def test_private_call_and_ids():
    packets = ipsc.call_packets(PEER, 2, SRC, 7654321, frames49(1), private=True)
    assert {p[0] for p in packets} == {0x81}
    assert packets[0][38] == 0x03                                 # FLCO unit-to-unit
    decoded = ipsc.decode(packets[0])
    assert decoded["name"] == "PVT_VOICE" and decoded["dst"] == 7654321 and decoded["src"] == SRC


def test_decode_voice_fields():
    ambe = frames49(6)
    packets = ipsc.call_packets(PEER, 9, SRC, TG, ambe, ts=2)
    decoded = [ipsc.decode(p) for p in packets]
    head = decoded[0]
    assert head["kind"] == "HEAD" and head["ts"] == 2 and not head["end"] and head["rs_ok"]
    assert head["rtp"]["marker"] == 1 and head["rtp"]["pt"] == 0x5D and head["rtp"]["version_byte"] == 0x80
    assert head["lc"] == dmr.full_lc(dmr.FLCO_GROUP, SRC, TG) and head["data_bits"] == 96 and head["words"] == 10
    assert head["call_ctrl"] == 0x43E2 and head["call_type"] == 2 and head["rssi_present"]
    assert [d["kind"] for d in decoded[:3]] == ["HEAD"] * 3
    assert [d["kind"] for d in decoded[3:9]] == ["A", "BCD", "BCD", "BCD", "E", "F"]
    assert decoded[3]["sync"] and decoded[7]["lc_present"] and not decoded[4]["sync"]
    assert decoded[3]["ambe"] == ambe[0] and decoded[7]["lc"] == dmr.full_lc(dmr.FLCO_GROUP, SRC, TG)
    assert decoded[-1]["kind"] == "TERM" and decoded[-1]["end"] and decoded[-1]["rs_ok"]
    assert "ts=2" in ipsc.describe(decoded[3]) and "TERM" in ipsc.describe(decoded[-1])


def test_decode_voice_rejects():
    packet = ipsc.call_packets(PEER, 1, SRC, TG, frames49(1))[3]
    with pytest.raises(ipsc.IpscError):
        ipsc.decode(packet[:-1])                                  # length byte no longer matches
    with pytest.raises(ipsc.IpscError):
        ipsc.decode(packet[:20])
    with pytest.raises(ipsc.IpscError):
        ipsc.decode(ipsc.call_packets(PEER, 1, SRC, TG, frames49(1))[0][:40])
    bad = bytearray(ipsc.call_packets(PEER, 1, SRC, TG, frames49(1))[0])
    bad[47] ^= 0xFF
    assert ipsc.decode(bytes(bad))["rs_ok"] is False


def test_short_header_follows_length_to_follow():
    """A header whose length-to-follow is 8 words (50 bytes, no slot type and no RSSI) is accepted: the
    bridge reads such a packet too (variant seen in its own test input, hardware origin unverified)."""
    head = bytearray(ipsc.call_packets(PEER, 1, SRC, TG, frames49(1))[0][:50])
    head[32:34] = (8).to_bytes(2, "big")
    decoded = ipsc.decode(bytes(head))
    assert decoded["kind"] == "HEAD" and decoded["rssi_raw"] is None and decoded["tag"] == b"" and decoded["rs_ok"]
    head[32:34] = (10).to_bytes(2, "big")
    with pytest.raises(ipsc.IpscError):
        ipsc.decode(bytes(head))


def test_sync_undetect_placeholder():
    packet = ipsc.voice_packet(PEER, 1, SRC, TG, bytes((0x93, 0x00, 0x00, 0x00)), rtp_pt=0x5D)
    decoded = ipsc.decode(packet)
    assert len(packet) == 34 and decoded["kind"] == "SYNC_UNDETECT" and decoded["slot2_bit"]


def test_voice_with_auth():
    key = ipsc.auth_key("A1B2C3")
    packets = [ipsc.sign(key, p) for p in ipsc.call_packets(PEER, 1, SRC, TG, frames49(1))]
    assert all(len(signed) == len(plain) + 10 for signed, plain in
               zip(packets, ipsc.call_packets(PEER, 1, SRC, TG, frames49(1))))
    assert ipsc.decode(packets[0], key)["kind"] == "HEAD"
    with pytest.raises(ipsc.AuthError):
        ipsc.decode(packets[0], ipsc.auth_key("A1B2C4"))


# ------------------------------------------------------------------ simulated peer (no socket)
MASTER_ADDR = ("192.0.2.30", 50000)
OTHER = {"id": 1000002, "addr": ("192.0.2.32", 50000)}
THIRD = {"id": 1000003, "addr": ("192.0.2.33", 50000)}


def listing(*peers):
    return ipsc.peer_list_reply(MASTER, [(p["id"], p["addr"][0], p["addr"][1], 0x6A) for p in peers])


def names(pairs):
    return [ipsc.decode(body)["name"] for _, body in pairs]


def test_peer_session_flow():
    peer = ipsc.PeerSession(PEER, MASTER_ADDR)
    out = peer.register()
    assert out == [(MASTER_ADDR, ipsc.master_reg_req(PEER))] and peer.state == "REGISTERING"
    assert peer.keepalive() == []                                 # nothing before the registration is answered
    out = peer.handle(ipsc.master_reg_reply(MASTER, 2), MASTER_ADDR)
    assert out == [(MASTER_ADDR, ipsc.peer_list_req(PEER))] and peer.state == "REGISTERED"
    assert peer.master_info["num_peers"] == 2
    assert names(peer.keepalive()) == ["MASTER_ALIVE_REQ"]
    assert peer.handle(listing({"id": PEER, "addr": ("192.0.2.31", 50000)}, OTHER, THIRD), MASTER_ADDR) == []
    assert peer.state == "ACTIVE" and sorted(peer.mesh) == [OTHER["id"], THIRD["id"]]
    tick = peer.keepalive()
    assert [(a, ipsc.decode(b)["name"]) for a, b in tick] == [
        (MASTER_ADDR, "MASTER_ALIVE_REQ"), (OTHER["addr"], "PEER_REG_REQ"), (THIRD["addr"], "PEER_REG_REQ")]
    assert peer.handle(ipsc.peer_reg_reply(OTHER["id"]), OTHER["addr"]) == []
    tick = peer.keepalive()
    assert [(a, ipsc.decode(b)["name"]) for a, b in tick][1:] == [
        (OTHER["addr"], "PEER_ALIVE_REQ"), (THIRD["addr"], "PEER_REG_REQ")]
    assert peer.mesh[OTHER["id"]]["outstanding"] == 1
    peer.handle(ipsc.peer_alive_reply(OTHER["id"]), OTHER["addr"])
    assert peer.mesh[OTHER["id"]]["outstanding"] == 0
    # requests from the others are answered to their source address
    out = peer.handle(ipsc.peer_reg_req(THIRD["id"]), THIRD["addr"])
    assert out == [(THIRD["addr"], ipsc.peer_reg_reply(PEER))]
    out = peer.handle(ipsc.peer_alive_req(OTHER["id"]), OTHER["addr"])
    assert out == [(OTHER["addr"], ipsc.peer_alive_reply(PEER))]
    peer.handle(ipsc.master_alive_reply(MASTER), MASTER_ADDR)
    assert peer.missed == 0
    # a shorter list drops the peer that is gone
    peer.handle(listing(OTHER), MASTER_ADDR)
    assert sorted(peer.mesh) == [OTHER["id"]]
    assert names(peer.de_register()) == ["DE_REG_REQ"] and peer.state == "IDLE"


def test_peer_session_without_peer_list_request():
    peer = ipsc.PeerSession(PEER, MASTER_ADDR, ask_peer_list=False)
    peer.register()
    assert peer.handle(ipsc.master_reg_reply(MASTER, 0), MASTER_ADDR) == []


def test_peer_session_auth_and_errors():
    key = ipsc.auth_key("A1B2C3")
    peer = ipsc.PeerSession(PEER, MASTER_ADDR, key=key)
    (addr, wire), = peer.register()
    assert addr == MASTER_ADDR and ipsc.strip_auth(key, wire) == ipsc.master_reg_req(
        PEER, flags=ipsc.flags_word(auth=True))
    assert ipsc.decode(wire, key)["flags"]["auth"]
    assert peer.handle(ipsc.master_reg_reply(MASTER, 0), MASTER_ADDR) == []      # unsigned: dropped
    assert peer.state == "REGISTERING" and len(peer.errors) == 1
    assert peer.handle(b"\x90\x00", MASTER_ADDR) == [] and len(peer.errors) == 2
    out = peer.handle(ipsc.sign(key, ipsc.master_reg_reply(MASTER, 0)), MASTER_ADDR)
    assert peer.state == "REGISTERED" and ipsc.strip_auth(key, out[0][1]) == ipsc.peer_list_req(PEER)
    assert all(direction in ("tx", "rx") for direction, _, _ in peer.trace)
    assert peer.trace[0][2].startswith("MASTER_REG_REQ id=1000001")


def test_peer_session_voice_and_calls():
    peer = ipsc.PeerSession(PEER, MASTER_ADDR)
    peer.register()
    peer.handle(ipsc.master_reg_reply(MASTER, 1), MASTER_ADDR)
    peer.handle(listing(OTHER), MASTER_ADDR)
    peer.handle(ipsc.peer_reg_reply(OTHER["id"]), OTHER["addr"])
    peer.handle(ipsc.call_packets(OTHER["id"], 3, SRC, TG, frames49(1))[0], OTHER["addr"])
    peer.handle(bytes.fromhex("9e000f424200"), MASTER_ADDR)
    assert len(peer.received) == 1 and peer.received[0]["peer_id"] == OTHER["id"]
    assert [p["name"] for p in peer.unhandled] == ["UNKNOWN_9E"]
    call = peer.call(SRC, TG, frames49(1))
    assert len(call) == 3 + 1 + 1                                 # three headers, one burst, one terminator
    assert [a for a, _ in call[0]] == [MASTER_ADDR, OTHER["addr"]]
    assert ipsc.decode(call[0][0][1])["call_seq"] == 1 and ipsc.decode(peer.call(SRC, TG, [])[0][0][1])["call_seq"] == 2


# ------------------------------------------------------------------ simulated peer on loopback
def test_ipsc_peer_over_loopback():
    master = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    master.bind(("127.0.0.1", 0))
    master.settimeout(0.05)
    maddr = master.getsockname()
    stop = threading.Event()
    seen = []

    def serve():
        while not stop.is_set():
            try:
                data, addr = master.recvfrom(2048)
            except socket.timeout:
                continue
            packet = ipsc.decode(data)
            seen.append(packet)
            if packet["op"] == ipsc.MASTER_REG_REQ:
                master.sendto(ipsc.master_reg_reply(MASTER, 1), addr)
            elif packet["op"] == ipsc.PEER_LIST_REQ:
                master.sendto(ipsc.peer_list_reply(MASTER, [(PEER, addr[0], addr[1], 0x6A)]), addr)
            elif packet["op"] == ipsc.MASTER_ALIVE_REQ:
                master.sendto(ipsc.master_alive_reply(MASTER), addr)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    peer = ipsc.IpscPeer("127.0.0.1", maddr, PEER, interval=0.2)
    try:
        assert peer.connect(timeout=3.0)
        assert peer.session.master_info["peer_id"] == MASTER
        peer.step(0.5)                                            # at least one keep-alive
        peer.send_call(SRC, TG, frames49(1), gap=0.005)
        peer.step(0.2)
        peer.close()
        for _ in range(100):                                      # let the master thread see DE_REG_REQ
            if seen and seen[-1]["name"] == "DE_REG_REQ":
                break
            threading.Event().wait(0.01)
    finally:
        stop.set()
        thread.join(2)
        master.close()
    voice = [p for p in seen if p["op"] == ipsc.GROUP_VOICE]
    assert [p["kind"] for p in voice] == ["HEAD"] * 3 + ["A", "TERM"]
    assert [p["rtp"]["seq"] for p in voice] == [0, 1, 2, 3, 4]
    assert any(p["name"] == "MASTER_ALIVE_REQ" for p in seen) and seen[-1]["name"] == "DE_REG_REQ"
