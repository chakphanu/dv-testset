# SPDX-License-Identifier: MIT
"""YSF V/D mode 2 GPS record in the DCH of frames FN 6 and FN 7 (DP15-4a).

A radio that sends its position puts a 20 byte record into the 10 byte DCH of
two consecutive communications frames: FN 6 carries record bytes 0..9, FN 7
bytes 10..19, both with FT 7 and DT 2 (V/D mode 2). The record:

  byte 0       0x02 (start of text; no parser looks at it)
  bytes 1..2   22 62 (short GPS) or 47 64 (long GPS)
  byte 3       0x00
  byte 4       radio type (0x24 FT-1D, 0x28 FT-2D, 0x30 FT-3D, ...)
  bytes 5..13  position, Mic-E like decimal digits (below)
  bytes 14..   padding up to the end marker
  end marker   0x03 (here at byte 17), then one sum byte: the 8 bit wrapping sum
               of every record byte from byte 0 through the end marker, then pad

Position bytes (every byte of 5..10 has 0x30 or 0x50 in its high nibble):

  5, 6    latitude degrees, tens and units digit in the low nibble
  7, 8    latitude minutes, tens and units; the high nibble of byte 8 is the
          hemisphere (0x50 north, 0x30 south)
  9, 10   latitude hundredths of a minute, tens and units; the high nibble of
          byte 9 selects the longitude degree range (0x50: 0..9 and 100..179,
          0x30: 10..99); the high nibble of byte 10 is the hemisphere
          (0x30 east, 0x50 west)
  11      longitude degrees: with 0x50 in byte 9 degrees 0..9 are 0x76 + deg,
          100..109 are 0x6C + (deg - 100), 110..179 are 0x26 + (deg - 110);
          with 0x30 in byte 9 degrees 10..99 are 0x26 + (deg - 10)
  12      longitude minutes: 0..9 are 0x58 + min, 10..59 are 0x26 + (min - 10)
  13      longitude hundredths of a minute: 0x1C + value (0..99)

Sources READ for this layout (facts only, no code taken): the DV-Router parser
crates/lib/pkt/src/ysf.rs:25-123 (record checks, position decode) and the pinned
YSFClients GPS.cpp (.cache/ysf-research/src/YSFClients/YSFGateway/GPS.cpp:30-31
magic bytes, :48-117 frame and end marker handling, :150-244 position decode,
:248-282 radio names). VERIFIED against both; docs/dp15-cases.md lists the lines.
"""
from . import ysf

SHORT_GPS = b"\x22\x62"
LONG_GPS = b"\x47\x64"
RADIO_FT1D = 0x24
RADIO_FT2D = 0x28
RADIO_FT3D = 0x30
RADIO_NAMES = {0x20: "DR-2X", 0x24: "FT-1D", 0x25: "FTM-400D", 0x26: "DR-1X", 0x27: "FT-991A", 0x28: "FT-2D",
               0x29: "FTM-100D", 0x30: "FT-3D", 0x31: "FTM-300D", 0x33: "FT-5D"}
FN_FIRST, FN_SECOND = 6, 7
FT_GPS = 7
END_MARKER_AT = 17


def _digits(value):
    return divmod(int(value), 10)


def gps_record(lat, lon, radio=RADIO_FT1D, magic=SHORT_GPS):
    """The 20 byte record for a fix (degrees, south and west negative). The
    minutes are rounded to hundredths, the resolution of the record."""
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("latitude/longitude out of range")
    north, east = lat >= 0, lon >= 0
    lat_hund = round(abs(lat) * 60 * 100)          # hundredths of a minute
    lon_hund = round(abs(lon) * 60 * 100)
    lat_deg, rest = divmod(lat_hund, 6000)
    lat_min, lat_frac = divmod(rest, 100)
    lon_deg, rest = divmod(lon_hund, 6000)
    lon_min, lon_frac = divmod(rest, 100)
    if lat_deg > 89 or lon_deg > 179:
        raise ValueError("the record holds latitude 0..89 and longitude 0..179 degrees")
    rec = bytearray(20)
    rec[0] = 0x02
    rec[1:3] = magic
    rec[4] = radio
    td, ud = _digits(lat_deg)
    tm, um = _digits(lat_min)
    tf, uf = _digits(lat_frac)
    lon_range = 0x50 if (lon_deg < 10 or lon_deg >= 100) else 0x30
    rec[5] = 0x50 | td
    rec[6] = 0x50 | ud
    rec[7] = 0x50 | tm
    rec[8] = (0x50 if north else 0x30) | um
    rec[9] = lon_range | tf
    rec[10] = (0x30 if east else 0x50) | uf
    if lon_deg < 10:
        rec[11] = 0x76 + lon_deg
    elif lon_deg < 100:
        rec[11] = 0x26 + lon_deg - 10
    elif lon_deg < 110:
        rec[11] = 0x6C + lon_deg - 100
    else:
        rec[11] = 0x26 + lon_deg - 110
    rec[12] = 0x58 + lon_min if lon_min < 10 else 0x26 + lon_min - 10
    rec[13] = 0x1C + lon_frac
    rec[END_MARKER_AT] = 0x03
    rec[END_MARKER_AT + 1] = sum(rec[:END_MARKER_AT + 1]) & 0xFF
    return bytes(rec)


def split_record(record):
    """(DCH of FN 6, DCH of FN 7): bytes 0..9 and 10..19."""
    if len(record) != 20:
        raise ValueError("a GPS record is 20 bytes")
    return bytes(record[:10]), bytes(record[10:])


def decode_record(record):
    """(lat, lon, radio name) of a record, or None when the magic, the end
    marker/sum or a position byte does not fit. An independent reading of the
    layout above (used by the unit tests to check gps_record)."""
    rec = bytes(record)
    if len(rec) != 20 or rec[1:3] not in (SHORT_GPS, LONG_GPS):
        return None
    end = rec.rfind(b"\x03", 14)
    if end < 14 or end + 1 >= len(rec) or sum(rec[:end + 1]) & 0xFF != rec[end + 1]:
        return None
    if any(b & 0xF0 not in (0x30, 0x50) for b in rec[5:11]):
        return None
    lat_deg = (rec[5] & 15) * 10 + (rec[6] & 15)
    lat_min = (rec[7] & 15) * 10 + (rec[8] & 15)
    lat_frac = (rec[9] & 15) * 10 + (rec[10] & 15)
    sel = rec[9] & 0xF0
    code = rec[11]
    if sel == 0x50:
        lon_deg = code - 0x76 if 0x76 <= code <= 0x7F else 100 + code - 0x6C if 0x6C <= code <= 0x75 else 110 + code - 0x26
    else:
        lon_deg = 10 + code - 0x26
    lon_min = rec[12] - 0x58 if rec[12] >= 0x58 else 10 + rec[12] - 0x26
    lon_frac = rec[13] - 0x1C
    lat = lat_deg + (lat_min + lat_frac / 100) / 60
    lon = lon_deg + (lon_min + lon_frac / 100) / 60
    if rec[8] & 0xF0 != 0x50:
        lat = -lat
    if rec[10] & 0xF0 != 0x30:
        lon = -lon
    return lat, lon, RADIO_NAMES.get(rec[4], "0x%02X" % rec[4])


def gps_stream_packets(frames, callsign, lat, lon, radio=RADIO_FT1D, dgid=0, gateway=None, dst=ysf.ALL,
                       pad=ysf.SILENCE, magic=SHORT_GPS):
    """Header, V/D mode 2 voice packets and terminator like ysf.stream_packets,
    but the packets with FN 6 and FN 7 carry the GPS record in their DCH and
    have FT 7. `frames` must make at least 8 packets (40 frames) so that FN 6
    and 7 occur (FN cycles 0..7 here)."""
    gateway = callsign if gateway is None else gateway
    frames = ysf.wire_frames(frames, pad)
    count = len(frames) // ysf.FRAMES_PER_PACKET
    if count < 8:
        raise ValueError("a GPS over needs 8 packets (40 frames) so that FN 6 and FN 7 are sent")
    first, second = split_record(gps_record(lat, lon, radio, magic))
    out = [ysf.packet(gateway, callsign, dst, 0, ysf.header_frame(callsign, dgid, downlink=gateway))]
    for index in range(count):
        fn = index % 8
        chunk = frames[index * ysf.FRAMES_PER_PACKET:(index + 1) * ysf.FRAMES_PER_PACKET]
        if fn == FN_FIRST:
            dch = first
        elif fn == FN_SECOND:
            dch = second
        else:
            dch = ysf.dch_text(fn, callsign, gateway)
        out.append(ysf.packet(gateway, callsign, dst, len(out),
                              ysf.voice_frame(fn, chunk, dch, dgid, ft=FT_GPS)))
    out.append(ysf.packet(gateway, callsign, dst, len(out),
                          ysf.terminator_frame(callsign, dgid, downlink=gateway), eot=True))
    return out
