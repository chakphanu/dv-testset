# SPDX-License-Identifier: MIT
"""Round trips and boundaries for the NXDN layer 3 data call and Kenwood GPS
generator (dvtest/proto/nxdn_l3.py). Offline; no decoder is run here."""
import pytest

from dvtest.proto import nxdn, nxdn_l3 as l3
from dvtest.proto.nxdn_l3 import DSDFME, GATEWAY, LON_BOTH, GpsFix

DEG_TOL = 1.0 / 600000 + 1e-9        # half of 1e-4 minute, in degrees


def table_crc32(data):
    """Independent CRC-32/MPEG-2: table driven, not the bit loop of the lib."""
    table = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = ((value << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if value & 0x80000000 else (value << 1) & 0xFFFFFFFF
        table.append(value)
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[(crc >> 24) ^ byte]
    return crc


# ------------------------------------------------------------------ CRC and NMEA

def test_crc32_published_check_value():
    # published by the CRC RevEng catalogue: CRC-32/MPEG-2 (poly 04C11DB7,
    # init FFFFFFFF, no reflection, xorout 0) of "123456789" is 0376E6E7.
    # That is the variant TS 1-A 9.3 describes (ones preset, MSB first).
    assert l3.message_crc32(b"123456789") == 0x0376E6E7
    assert l3.message_crc32(b"") == 0xFFFFFFFF


def test_crc32_matches_table_implementation():
    for size in (1, 4, 20, 38, 316):
        data = bytes((index * 37 + size) & 0xFF for index in range(size))
        assert l3.message_crc32(data) == table_crc32(data)


def test_nmea_checksum_published_example():
    # published by NMEA 0183 documentation examples (widely quoted GGA sample)
    body = "GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,"
    assert l3.nmea_checksum(body) == "47"


def test_nmea_rmc_and_icom_form():
    fix = GpsFix(13.7563, 100.5018, 90.0, 55.0)
    text = l3.nmea_rmc(fix)
    body, star, check = text[1:].partition("*")
    assert star == "*" and check == l3.nmea_checksum(body)
    assert body.startswith("GPRMC,123400,A,1345.3780,N,10030.1080,E,55.0,90.0,061126")
    data = l3.icom_user_data(fix)
    assert data[0] == 0x06 and data[1:2] == b"$" and data.endswith(b"\r\n")
    south = l3.nmea_rmc(GpsFix(-33.8688, -151.2093))
    assert ",S," in south and ",W," in south


# ------------------------------------------------------------------ degrees / minutes

@pytest.mark.parametrize("value,expected", [
    (13.7563, (False, 1345, 3780)),
    (100.5018, (False, 10030, 1080)),
    (-100.5018, (True, 10030, 1080)),
    (0.0, (False, 0, 0)),
    (180.0, (False, 18000, 0)),
    (-0.0001, (True, 0, 60)),               # 0.0001 deg = 0.006 min
    (0.0000125, (False, 0, 8)),             # 7.5e-4 min -> 7.5 units -> half up -> 8
    (0.0000124, (False, 0, 7)),
    (0.99999999, (False, 100, 0)),          # rounding carries into the degree
    (12.99999999, (False, 1300, 0)),        # ... and through the minute
    (89.9999, (False, 8959, 9940)),
])
def test_degmin_boundaries(value, expected):
    assert l3.to_degmin(value) == expected


def test_degmin_range_errors():
    for bad in (180.1, -181.0, float("nan")):
        with pytest.raises(ValueError):
            l3.to_degmin(bad)
    with pytest.raises(ValueError):
        l3.kenwood_full(GpsFix(90.5, 0.0))      # latitude over 90 degrees
    with pytest.raises(ValueError):
        l3.kenwood_full(GpsFix(0.0, 180.1))


def test_degmin_round_trip_grid():
    for tenth_minutes in range(0, 18000 * 600, 7919):       # sparse sweep of 1e-4 minute units
        degrees = tenth_minutes / 600000.0
        neg, dm, frac = l3.to_degmin(degrees)
        assert abs(l3.from_degmin(neg, dm, frac) - degrees) <= DEG_TOL
        assert 0 <= frac <= 9999 and dm % 100 < 60


def test_course_boundaries():
    assert l3.course_tenths(0) == 0
    assert l3.course_tenths(359) == 3590
    assert l3.course_tenths(359.9) == 3599
    assert l3.course_tenths(359.94) == 3599
    assert l3.course_tenths(359.95) == 0        # rounds to 360.0, wraps
    assert l3.course_tenths(360.0) == 0
    for bad in (-0.1, 360.1):
        with pytest.raises(ValueError):
            l3.course_tenths(bad)


# ------------------------------------------------------------------ Kenwood FULL / SHORT

def assert_fix_close(got, want, speed=True):
    assert abs(got.latitude - want.latitude) <= DEG_TOL
    assert abs(got.longitude - want.longitude) <= DEG_TOL
    assert got.course == pytest.approx(l3.course_tenths(want.course) / 10.0)
    if speed:
        assert got.speed == pytest.approx(want.speed, abs=0.05)
    assert (got.year, got.month, got.day, got.hour, got.minute) == (
        want.year, want.month, want.day, want.hour, want.minute)
    assert got.elevation == want.elevation


@pytest.mark.parametrize("layout", [GATEWAY, DSDFME, LON_BOTH], ids=["gateway", "dsdfme", "lon-both"])
@pytest.mark.parametrize("name,fix", l3.KNOWN_POSITIONS, ids=[n for n, _ in l3.KNOWN_POSITIONS])
def test_full_round_trip_table(name, fix, layout):
    data = l3.kenwood_full(fix, layout)
    assert len(data) == l3.FULL_BYTES and data[0] == 0 and data[1:3] == b"\0\0"
    assert data[36:38] == bytes(l3.FULL_MARKER)
    assert_fix_close(l3.parse_kenwood(data, layout), fix)


def test_base_position_fields():
    data = l3.kenwood_full(GpsFix(l3.BASE_LAT, l3.BASE_LON, 90.0, 55.0), GATEWAY)
    # latitude 13 deg 45.3780 min north: ddmm 1345 = 0x0541, fraction 3780
    assert data[11:13] == (1345).to_bytes(2, "little") and data[9] == (3780 & 0x7F) << 1
    assert data[10] == 3780 >> 7
    # longitude 100 deg 30.1080 min east: ddmm 10030 = 0x272E, fraction 1080
    assert data[15:17] == (10030).to_bytes(2, "little") and data[13] == (1080 & 0x7F) << 1
    assert data[14] == 1080 >> 7
    # course 90.0 -> 900 tenths, speed 55 whole + 0 tenths (gateway packing)
    assert data[23] == 900 >> 4 and data[24] == 900 & 15
    assert data[25] == 0x00 and data[26] == 55


def test_hemispheres_and_negative_longitude():
    fix = GpsFix(-33.8688, -74.006, 10.0, 5.0)
    data = l3.kenwood_full(fix)
    assert data[9] & 1 == 1 and data[13] & 1 == 1       # south, west
    got = l3.parse_kenwood(data)
    assert got.latitude < 0 and got.longitude < 0
    assert_fix_close(got, fix)
    east = l3.parse_kenwood(l3.kenwood_full(GpsFix(1.0, 2.0)))
    assert east.latitude > 0 and east.longitude > 0


def test_longitude_fraction_rounding_in_message():
    # 100.0000125 deg is 7.5 units of 1e-4 minute above 100 deg: rounds up to 8
    value = 100.0000125
    assert l3.to_degmin(value) == (False, 10000, 8)
    got = l3.parse_kenwood(l3.kenwood_full(GpsFix(0.0, value)))
    assert abs(got.longitude - l3.from_degmin(False, 10000, 8)) < 1e-12
    assert l3.to_degmin(100.0000124) == (False, 10000, 7)
    # the highest fraction, 0.9999 min: stays below the next minute
    top = l3.parse_kenwood(l3.kenwood_full(GpsFix(0.0, 100 + 30.9999 / 60)))
    assert abs(top.longitude - (100 + 30.9999 / 60)) <= DEG_TOL


def test_course_359_and_wrap_in_both_packings():
    for layout in (GATEWAY, DSDFME):
        assert l3.parse_kenwood(l3.kenwood_full(GpsFix(1.0, 1.0, 359.9), layout), layout).course == 359.9
        assert l3.parse_kenwood(l3.kenwood_full(GpsFix(1.0, 1.0, 359.97), layout), layout).course == 0.0


def test_speed_limits_and_sentinels():
    top = l3.kenwood_full(GpsFix(1.0, 1.0, speed=1022.9))
    assert l3.parse_kenwood(top).speed == pytest.approx(1022.9)
    with pytest.raises(ValueError):
        l3.kenwood_full(GpsFix(1.0, 1.0, speed=1023.0))
    with pytest.raises(ValueError):
        l3.kenwood_full(GpsFix(1.0, 1.0, speed=-1.0))
    with pytest.raises(ValueError):
        l3.kenwood_full(GpsFix(1.0, 1.0, speed=1638.3), DSDFME)      # 16383 is kept as "no value"
    assert l3.parse_kenwood(l3.kenwood_full(GpsFix(1.0, 1.0, speed=1638.2), DSDFME), DSDFME).speed == \
        pytest.approx(1638.2)
    absent = l3.parse_kenwood_fields(l3.kenwood_full(GpsFix(1.0, 1.0, speed=None)))
    assert "speed" not in absent["present"]


def test_the_two_readings_disagree_on_exactly_three_fields():
    """A frame written the gateway way, read the dsd-fme way (and back): the
    position degrees/minutes and hemispheres agree, the longitude fraction,
    the course and the speed do not. Values worked out by hand."""
    fix = GpsFix(l3.BASE_LAT, l3.BASE_LON, 90.0, 55.0)
    data = l3.kenwood_full(fix, GATEWAY)
    seen = l3.parse_kenwood_fields(data, DSDFME)
    assert seen["raw"]["lat_degmin"] == 1345 and seen["raw"]["lat_fraction"] == 3780
    assert seen["raw"]["lon_degmin"] == 10030 and seen["raw"]["lon_fraction"] == 0
    assert seen["fix"].course == 108.0          # tenths 900 = 0x384: 0x04 0x38 swapped
    assert seen["fix"].speed == 1408.0          # 0x37 0x00 read as 14080 tenths
    back = l3.parse_kenwood(l3.kenwood_full(fix, DSDFME), GATEWAY)
    assert back.course != fix.course and back.speed != fix.speed
    # LON_BOTH removes the longitude difference only
    both = l3.kenwood_full(fix, LON_BOTH)
    assert l3.parse_kenwood_fields(both, GATEWAY)["raw"]["lon_fraction"] == 1080
    assert l3.parse_kenwood_fields(both, DSDFME)["raw"]["lon_fraction"] == 1080


def test_short_and_no_fix_and_errors():
    fix = GpsFix(-13.7563, 100.5018)
    data = l3.kenwood_short(fix)
    assert len(data) == l3.SHORT_BYTES and data[0] == 1
    got = l3.parse_kenwood_fields(data)
    assert got["kind"] == "short" and got["present"] == {"latitude", "longitude"}
    assert abs(got["fix"].latitude - fix.latitude) <= DEG_TOL
    for kind in ("full", "short"):
        assert l3.parse_kenwood_fields(l3.kenwood_no_fix(kind))["fix"] is None
    with pytest.raises(ValueError):
        l3.parse_kenwood(b"")
    with pytest.raises(ValueError):
        l3.parse_kenwood(bytes([0x02]) + bytes(14))         # VERY_SHORT is not supported
    with pytest.raises(ValueError):
        l3.parse_kenwood(l3.kenwood_full(GpsFix(1.0, 1.0))[:37])


def test_elevation_signed_and_time_fields():
    fix = GpsFix(1.0, 1.0, elevation=-12, year=2099, month=12, day=31, hour=23, minute=59)
    got = l3.parse_kenwood(l3.kenwood_full(fix))
    assert (got.elevation, got.year, got.month, got.day, got.hour, got.minute) == (-12, 2099, 12, 31, 23, 59)
    for bad in (dict(hour=24), dict(minute=60), dict(month=13), dict(day=0), dict(year=1999)):
        with pytest.raises(ValueError):
            l3.kenwood_full(GpsFix(1.0, 1.0, **bad))


# ------------------------------------------------------------------ container

def test_packet_information_fields():
    info = l3.packet_information(3, 18)
    assert info == bytes([0x02, 0x94, 0x00])
    assert l3.parse_packet_information(info) == {
        "confirmed": False, "retry": False, "blocks": 3, "pad": 18, "start": True,
        "circular": False, "fragment_count": 0}
    odd = l3.packet_information(16, 0, confirmed=True, retry=True, start=False, circular=True,
                                fragment_count=0x1FF)
    assert odd == bytes([0xAF, 0x03, 0xFF])
    assert l3.parse_packet_information(odd)["fragment_count"] == 0x1FF
    for bad in ((0, 0), (17, 0), (1, 21)):
        with pytest.raises(ValueError):
            l3.packet_information(*bad)


def test_header_and_block_octets():
    header = l3.dcall_header(1001, 31672, l3.packet_information(3, 18))
    assert len(header) == 22
    assert header[:11] == bytes([0x09, 0x00, 0x20, 0x03, 0xE9, 0x7B, 0xB8, 0x00, 0x02, 0x94, 0x00])
    assert header[11:] == bytes(11)
    block = l3.dcall_block(2, 2, bytes(range(20)))
    assert block[:2] == bytes([0x0B, 0x22]) and block[2:] == bytes(range(20))
    with pytest.raises(ValueError):
        l3.dcall_block(0, 0, b"short")


def test_published_block_numbering_ts1b_figure_5_4_1():
    # published by the NXDN Forum, TS 1-B v1.3 5.4 Figure 5.4-1: a first
    # transmission of four user blocks has Block Count 3, Packet Frame Number
    # 3,2,1,0 and Block Number 3,2,1,0; a selective retry of blocks 3 and 1
    # has the retry flag, Block Count 1, Packet Frame Number 1,0, Block Number 3,1.
    user = bytes(range(76))                 # 76 + 4 CRC = 80 = four full blocks
    first = l3.data_call_octets(1001, 31672, user)
    assert first[0][8] == 0x03 and first[0][9] >> 3 == 0
    assert [o[1] for o in first[1:5]] == [0x33, 0x22, 0x11, 0x00]
    retry = l3.data_call_octets(1001, 31672, user, retry_blocks=[3, 1])
    assert retry[0][8] == 0x20 | 0x01
    assert [o[1] for o in retry[1:3]] == [0x13, 0x01]
    assert retry[1][2:] == first[1][2:] and retry[2][2:] == first[3][2:]


def test_udch_frame_and_flags():
    octets = l3.dcall_header(1001, 31672, l3.packet_information(1, 0))
    frame = l3.udch_frame(octets, ran=5)
    assert len(frame) == nxdn.PAYLOAD_SIZE and frame[0] == 0x90 and frame[1] == 5
    assert frame[2:24] == octets and frame[24:] == bytes(9)
    assert l3.data_flags(frame) == 0x03 | 0x04
    block = l3.udch_frame(l3.dcall_block(0, 0, bytes(20)))
    assert l3.data_flags(block) == 0x03
    assert l3.data_flags(l3.udch_frame(l3.tx_release(1, 2))) == 0x03 | 0x08
    assert l3.data_flags(block, group=False) == 0x02
    # the four LICH values the reflector client tests for header/trailer
    for lich in l3.LICH_DATA_FAMILY:
        assert l3.data_flags(l3.udch_frame(octets, lich=lich)) == 0x07
    assert l3.data_flags(l3.udch_frame(octets, lich=0x94)) == 0x03       # not in the family
    assert l3.LICH_DATA == 0x90


@pytest.mark.parametrize("size,blocks,pad", [(16, 1, 0), (17, 2, 19), (36, 2, 0), (38, 3, 18), (316, 16, 0)])
def test_blocks_and_pad(size, blocks, pad):
    chunks, got_pad = l3.split_blocks(l3.with_crc(bytes(size)))
    assert (len(chunks), got_pad) == (blocks, pad)
    assert (blocks * 20) - pad == size + 4
    call = l3.parse_call(l3.data_call_packets(1001, 31672, bytes(size)))
    assert call.problems == [] and call.crc_ok and call.user == bytes(size)


def test_user_data_over_limit():
    assert l3.MAX_USER_BYTES == 316
    with pytest.raises(ValueError):
        l3.with_crc(bytes(317))


def test_full_gps_call_on_the_wire():
    fix = GpsFix(l3.BASE_LAT, l3.BASE_LON, 180.0, 120.0)
    user = l3.kenwood_full(fix)
    packets = l3.data_call_packets(1001, 31672, user)
    assert [len(p) for p in packets] == [43] * 5
    assert [p[9] for p in packets] == [0x07, 0x03, 0x03, 0x03, 0x0B]
    assert all(p[:5] == b"NXDND" and p[5:9] == bytes.fromhex("03e97bb8") for p in packets)
    assert [nxdn.parse(p)["kind"] for p in packets] == ["data"] * 5
    assert [p[10 + 3] for p in packets[1:4]] == [0x22, 0x11, 0x00]         # countdown, both nibbles
    call = l3.parse_call(packets)
    assert call.problems == [] and call.crc_ok and call.user == user
    assert (call.src, call.dst, call.info["blocks"], call.info["pad"]) == (1001, 31672, 3, 18)
    assert_fix_close(l3.parse_kenwood(call.user), fix)


def test_individual_call_flags_and_type():
    packets = l3.data_call_packets(1001, 1002, l3.kenwood_short(GpsFix(1.0, 1.0)), group=False)
    assert packets[0][9] == 0x06 and packets[1][9] == 0x02 and packets[-1][9] == 0x0A
    assert l3.parse_call(packets).info["call_type"] == nxdn.CALL_INDIVIDUAL


def test_negative_calls_are_reported():
    user = l3.kenwood_full(GpsFix(1.0, 1.0))
    good = l3.data_call_packets(1001, 31672, user)
    # lost middle block: the gateway would shift every later byte by 20
    lost = l3.parse_call(good[:2] + good[3:])
    assert any("block count" in p for p in lost.problems) and "CRC mismatch" in lost.problems
    assert any("count down" in p for p in lost.problems)
    # flipped CRC bit
    bad = l3.parse_call(l3.data_call_packets(1001, 31672, user, corrupt_crc=True))
    assert not bad.crc_ok and bad.problems == ["CRC mismatch"]
    # no trailer, no header
    assert "no TX_REL" in l3.parse_call(good[:-1]).problems
    assert l3.parse_call(good[1:]).problems[0] == "no header"
    # selective retry, fragmented, encrypted
    retry = l3.parse_call(l3.data_call_packets(1001, 31672, user, retry_blocks=[2, 0]))
    assert "selective retry" in retry.problems
    frag = l3.parse_call(l3.data_call_packets(1001, 31672, user, header_opts={"circular": True}))
    assert "fragmented" in frag.problems
    octets = l3.data_call_octets(1001, 31672, user)
    octets[0] = l3.dcall_header(1001, 31672, octets[0][8:11], cipher=1, key=3)
    enc = l3.parse_call([l3.udch_frame(o) for o in octets])
    assert "encrypted" in enc.problems
    # a second header and an unknown message type
    assert "second header" in l3.parse_call(good[:1] + good).problems
    assert any("unexpected" in p for p in l3.parse_call(good + [l3.udch_frame(bytes([0x3F]) + bytes(21))]).problems)


def test_retry_block_range():
    with pytest.raises(ValueError):
        l3.data_call_octets(1, 2, bytes(38), retry_blocks=[3])
    with pytest.raises(ValueError):
        l3.data_call_octets(1, 2, bytes(38), retry_blocks=[])


def test_parse_call_accepts_frames_and_packets():
    packets = l3.data_call_packets(7, 8, b"hello")
    frames = [p[10:] for p in packets]
    assert l3.parse_call(frames).user == b"hello" == l3.parse_call(packets).user
