# SPDX-License-Identifier: MIT
"""D-STAR synthetic text over, as DV-Router renders it (docs/design/data.md 7.6)
and as a test client sees it on a DExtra socket.

Layout of the over: a header, 21 voice frames (seq 0..20, null AMBE) and a last
frame, 22 x 20 ms = 440 ms. The slow data field of each voice frame is 3 bytes:

  frame 0        sync 55 2D 16 (not scrambled)
  frames 1..8    the four text blocks as eight half blocks
  frames 9..16   the same eight half blocks again
  frames 17..20  filler 66 66 66 (on the wire 16 29 F5)

A block is 0x40 | i (i = 0..3) and 5 characters, i.e. 6 bytes sent as two 3-byte
halves; every field of frames 1..20 is XORed with 70 4F 93. The text is 20
characters, "source: body" padded with spaces.

  decode_text_over(groups)   text of the 21 slow data fields of an over
  encode_text_over(text)     the 21 fields for a 20 character text (the inverse;
                             the fixture N0CALL / CQ DE N0CALL of data.md 7.6)
  overs(datagrams)           DSVT datagrams grouped into overs by stream id
"""
from .proto import dextra

SYNC = dextra.SYNC_DATA                         # 55 2D 16
SCRAMBLE = dextra.SCRAMBLE                      # 70 4F 93
FILLER = dextra.NULL_SLOW                       # 16 29 F5 on the wire (66 66 66 scrambled)
OVER_FRAMES = 22                                # 21 voice frames + the last frame
TEXT_LEN = 20
BLOCKS = 4


def _xor(data3):
    return bytes(a ^ b for a, b in zip(bytes(data3), SCRAMBLE))


def display_text(text):
    """The 20 character display form: cut or padded with spaces."""
    return (text + " " * TEXT_LEN)[:TEXT_LEN]


def encode_text_over(text):
    """The 21 slow data fields (frames 0..20) of a text over for `text`
    (ASCII, cut or padded to 20 characters)."""
    raw = display_text(text).encode("ascii")
    blocks = b"".join(bytes([0x40 | i]) + raw[5 * i:5 * i + 5] for i in range(BLOCKS))
    halves = [_xor(blocks[i:i + 3]) for i in range(0, len(blocks), 3)]       # eight fields
    return [SYNC] + halves + halves + [FILLER] * 4


def decode_text_over(groups):
    """dict for the 21 slow data fields of frames 0..20 as received (frame 0
    plain, the rest scrambled), or None when they are not a text over:

      text        the 20 characters (not stripped)
      repeat_ok   frames 9..16 repeat frames 1..8
      filler_ok   frames 17..20 are the filler

    None when there are fewer than 21 fields, frame 0 is not the sync pattern,
    the block headers are not 0x40..0x43 in order or a character is not
    printable ASCII."""
    groups = [bytes(g) for g in groups]
    if len(groups) < 21 or any(len(g) != 3 for g in groups[:21]) or groups[0] != SYNC:
        return None
    first = b"".join(_xor(g) for g in groups[1:9])
    again = b"".join(_xor(g) for g in groups[9:17])
    text = bytearray()
    for i in range(BLOCKS):
        block = first[6 * i:6 * i + 6]
        if block[0] != 0x40 | i or not all(0x20 <= c < 0x7F for c in block[1:]):
            return None
        text += block[1:]
    return {"text": text.decode("ascii"), "repeat_ok": first == again,
            "filler_ok": all(g == FILLER for g in groups[17:21])}


def overs(datagrams):
    """Group DSVT datagrams (header 56 B, voice 27 B) by stream id, in order of
    first appearance: a list of dicts with stream_id, header (parse_header_full
    dict or None), voice (parse_voice_raw dicts of the frames before the last),
    last (the last-frame dict or None), frames (voice count + 1 when last) and
    slow (the slow data fields of the voice frames, in arrival order)."""
    out, by_id = [], {}
    for data in datagrams:
        header = dextra.parse_header_full(data)
        voice = None if header else dextra.parse_voice_raw(data)
        sid = (header or voice or {}).get("stream_id")
        if sid is None:
            continue
        if sid not in by_id:
            by_id[sid] = {"stream_id": sid, "header": None, "voice": [], "last": None, "frames": 0, "slow": []}
            out.append(by_id[sid])
        over = by_id[sid]
        if header:
            over["header"] = over["header"] or header
        elif voice["last"]:
            over["last"] = voice
            over["frames"] += 1
        else:
            over["voice"].append(voice)
            over["slow"].append(voice["slow"])
            over["frames"] += 1
    return out


def over_text(over):
    """decode_text_over of one overs() entry, or None (not a whole text over:
    wrong frame count or no last frame)."""
    if over["last"] is None or over["frames"] != OVER_FRAMES:
        return None
    seqs = [v["counter"] for v in over["voice"]]
    if seqs != list(range(21)):
        return None
    return decode_text_over(over["slow"])
