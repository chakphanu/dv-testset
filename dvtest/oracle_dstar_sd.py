# SPDX-License-Identifier: MIT
"""Two independent D-STAR slow data decoders as test oracles (HWFREE1 item 4,
docs/dstar-slowdata-cases.md).

Host side of rig/Dockerfile.dstarsd. MMDVMHost's CDStarSlowData and DStarGateway's text, D-PRS
and NMEA collectors (GPL-2.0-or-later) are linked unmodified into two small drivers
(rig/dstarsd/oracle-*.cpp) and run as processes in one throw-away image (--network none);
nothing of their code or text is in this repository, only the output formats and the facts we
read from them.

A corpus of overs is built from the specification (dvtest.dstar_sd): every case is a list of
(seq, wire data field) frames. The frames go to both drivers on stdin ("B name", "F seq hex6",
"E"); each driver prints what its decoder decided. Our own decoder (dvtest.dstar_sd.Decoder, both
sync policies) is compared with them, event by event, and the result goes to
vectors/dstar_sd/corpus.json.

The pure functions (corpus, input and output text, views, comparison) are unit tested without
Docker (cases/unit/test_oracle_dstar_sd.py); run_driver() needs the image.

    python3 -m dvtest.oracle_dstar_sd run --out vectors/dstar_sd/corpus.json
"""
import argparse
import json
import subprocess
import sys

from . import dstar_sd as sd
from . import limits
from .proto import dextra

IMAGE = "dvt-dstarsd:local"
RUN_TIMEOUT = 120
SCHEMA = "dvt-dstar-sd-corpus/3"

CALL = "N0CALL"
TEXT = "N0CALL: CQ DE N0CALL"             # 20 characters
TEXT_B = "N0TEST: HELLO WORLD."            # 20 characters
# D-PRS payload of the same length (81 characters + CR = 17 blocks) as the real-radio sample of
# [SLOW] (the sample itself holds a personal callsign and position and is not committed)
DPRS_PAYLOAD = "N0CALL>API282,DSTAR*:/211234h0000.00N/00000.00E-027/000/N0CALL at home"
# GGA / RMC / free-form line have the lengths of the real-radio samples of [SLOW] (71, 74, 31 bytes)
GGA = sd.nmea_sentence("GPGGA,211234.00,0000.00000,N,00000.0000,E,1,05,1.0,12.0,M,0.0,M,,")
RMC = sd.nmea_sentence("GPRMC,211234.00,A,0000.00000,N,00000.000000,E,000.0,000.0,061126,,,A")
GLL = sd.nmea_sentence("GPGLL,0000.00,N,00000.00,E,211234.00,A,A")
VTG = sd.nmea_sentence("GPVTG,27.0,T,,M,0.0,N,0.0,K,A")
GSA = sd.nmea_sentence("GPGSA,A,3,04,05,09,12,,,,,,,,,1.8,1.0,1.5")
GSV = sd.nmea_sentence("GPGSV,1,1,04,04,40,083,40,05,30,120,35,09,20,250,30,12,60,300,38")
FREEFORM = b"N0CALL  ,TEST MESSAGE        \r\n"
RADIO_HEADER = dextra.radio_header(my=CALL, rpt1="N0CALL B", rpt2="XLX999 B", ur="CQCQCQ")


def _sf(frames, drop=()):
    return [f for i, f in enumerate(frames) if i not in drop]


def _dprs_of_length(total):
    """A D-PRS sentence of `total` bytes from '$$CRC' to the CR (valid CRC): the comment is padded."""
    pad = total - len(sd.dprs_line(DPRS_PAYLOAD))
    return sd.dprs_line(DPRS_PAYLOAD + "x" * pad)


def _nmea_of_length(total):
    """A GGA sentence of `total` bytes including CR LF: the last field is padded."""
    base = len(sd.nmea_sentence("GPGGA,211234.00,0000.00,N,00000.00,E,1,05,1.0,12.0,M,0.0,M,,"))
    return sd.nmea_sentence("GPGGA,211234.00,0000.00,N,00000.00,E,1,05,1.0,12.0,M,0.0,M,," + "0" * (total - base))


def _with_mini(blocks, index, mini):
    """The block list with the mini-header byte of one block replaced (payload untouched)."""
    out = list(blocks)
    out[index] = bytes([mini]) + out[index][1:]
    return out


def _sized_blocks(data, sizes):
    """Simple data blocks whose valid byte counts follow `sizes` (cycled); a block with fewer than 5
    valid bytes carries the filler after them and says so in its length nibble (legal, [STD6] 6.1.3)."""
    out = []
    pos = 0
    i = 0
    while pos < len(data):
        n = min(sizes[i % len(sizes)], len(data) - pos)
        out.append(sd.block(sd.T_DATA | n, data[pos:pos + n]))
        pos += n
        i += 1
    return out


def _printable_header():
    """A radio header whose 41 bytes are ALL printable ASCII (flags 0x20 0x20 0x20, which no radio sends, and
    a suffix chosen so that both FCS bytes are printable too, the first suffix of the search order that
    works): the only kind of header a decoder that maps every byte outside 32..127 to '.' before the
    FCS check (DSDcc's slow data path) can accept."""
    import itertools
    for chars in itertools.product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=2):
        hdr = dextra.radio_header(my=CALL, rpt1="N0CALL B", rpt2="XLX999 B", ur="CQCQCQ", suffix="".join(chars),
                                  flags=b"\x20\x20\x20")
        if all(32 <= b <= 126 for b in hdr):
            return hdr
    raise ValueError("no printable header found")


def _render_blocks(blocks, first_slot=0, superframes=None):
    return sd.render(sd.superframes_to_halves(sd.place(blocks, first_slot, superframes)))


UNIT66_DOC = ("unit66_hex: one string per superframe unit (a new unit starts when the frame number does not increase): "
              "3 byte presence bitmap + 21 x 3 raw slow data bytes as received (frame 0 included, others scrambled; "
              "an absent frame is 3 zero bytes). Bitmap (DV-Router dstar.rs:132-136, VERIFIED): little endian, "
              "bitmap = b0 | b1 << 8 | b2 << 16, LSB first, bit i = frame i (frame 0 = mask 0x01 of byte 0, frame 8 = "
              "bit 0 of byte 1, frame 20 = mask 0x10 of byte 2); the top 3 bits of byte 2 are spare and must be zero. "
              "A complete unit starts ffff1f 552d16.")

MODEL_LIMITS = ("The DV-Router model (dvtest.dstar_sd.DvrUnitDecoder) follows crates/lib/pkt/src/dstar.rs line by line. It does "
                "NOT model what happens after the line level checks: the field parsing of a valid $GPRMC / $GPGGA "
                "(position, time, date), the 29 byte station ID line (dstar_nmea.rs:107-148), the D-PRS source binding "
                "and position fields (dstar_dprs.rs:34-147), the blank-text rule of the status item. A valid RMC / GGA "
                "gives no item of its own in DV-Router; `gps` lists the lines that passed the line level checks "
                "(dprs = valid CRC, nmea = valid RMC / GGA checksum) and only their presence and frame number are "
                "meaningful. `outcome` is per unit: parsed, or reject:<Reject name> (dstar.rs:116-121, 208-212: a line "
                "error is reported only when the unit produced no text item).")

RULES = {
    "unit-size": "dstar.rs:123-130 (length != 66: state reset; < 66 TooShort, > 66 Size)",
    "unit-spare": "dstar.rs:131-135 (input[2] & 0xe0 != 0: state reset, Invalid)",
    "unit-bitmap": "dstar.rs:136 (bitmap = b0 | b1 << 8 | b2 << 16, bit i = frame i)",
    "unit-sync": "dstar.rs:137-141 (bit 0 set and frame 0 != 55 2d 16: state reset, Invalid)",
    "pair-hole": "dstar.rs:144-151 (pair bits (3 << frame) both set, else the GPS line is reset and the block skipped)",
    "unscramble": "dstar.rs:152-156 (XOR 70 4f 93 repeating over the 6 bytes)",
    "text": "dstar.rs:157-180 (0x40..0x43, 5 bytes & 0x7f, state.text persists across units, one text item per unit)",
    "gps-bytes": "dstar.rs:181-204 (0x30..0x35, n = low nibble, 0 means 5; CR or LF finish, other bytes push)",
    "opaque": "dstar.rs:205 (every other header byte is ignored, the GPS line is NOT reset)",
    "push-limits": "dstar.rs:49-69 with 15, 57-66 (NMEA 82, D-PRS 300 bytes; overflow: Size, mode discard)",
    "finish": "dstar.rs:70-113 (discard to the next CR or LF; D-PRS needs CR else Invalid and a slot for it; NMEA len 0: nothing)",
    "error-report": "dstar.rs:194-197, 208-212 (a line error resets the line; reported only when no item)",
    "nmea-crc": "dstar_nmea.rs:21-34 (RMC / GGA: no '*', suffix != 2 digits or XOR mismatch: Crc), 107-113 (other sentences ignored), position.rs:167-178 (upper case hex only)",
    "dprs-crc": "dstar_dprs.rs:15-33 (prefix / comma: Invalid; hex or CRC mismatch, CRC covers the CR: Crc)",
}


def corpus():
    """[(name, note, frames)] where frames = [(seq, wire3)]. Names are stable: they are the keys of the
    committed vectors."""
    tb = sd.text_blocks(TEXT)
    tb_b = sd.text_blocks(TEXT_B)
    hb = sd.header_blocks(RADIO_HEADER)
    bad_header = bytearray(RADIO_HEADER)
    bad_header[12] ^= 0x01                  # one bit of RPT1, FCS now wrong
    hb_bad = sd.header_blocks(bytes(bad_header))
    dprs = sd.dprs_line(DPRS_PAYLOAD)
    dprs_bad = bytearray(dprs)
    dprs_bad[20] ^= 0x01                    # one bit inside the string: CRC wrong
    gga_b = sd.simple_data_blocks(GGA.encode())
    rmc_b = sd.simple_data_blocks(RMC.encode())
    gga_bad = sd.simple_data_blocks(GGA.encode().replace(b"1.0,12.0", b"1.0,13.0"))
    free_b = sd.simple_data_blocks(FREEFORM)
    dprs_b = sd.simple_data_blocks(dprs)
    F = None
    cases = []

    def add(name, note, frames):
        cases.append((name, note, frames))

    # ---- message (text)
    add("text-aligned", "4 text blocks in slots 0..3, filler after: the layout of [STD6] 6.3 (message from frame 2)", _render_blocks(tb))
    add("text-dvrouter-twice", "the 4 blocks twice, then filler: what DV-Router renders (dvtest.slowdata.encode_text_over)", _render_blocks(tb + tb))
    add("text-slot1", "message from data frame 4 ([STD6] 6.3: 'can start from an even position')", _render_blocks(tb, 1))
    add("text-slot3", "message from data frame 8", _render_blocks(tb, 3))
    add("text-slot6", "last position that still fits one superframe (slots 6..9)", _render_blocks(tb, 6))
    add("text-slot8-split", "blocks 0,1 in slots 8,9; blocks 2,3 in slots 0,1 of the next superframe (cross-superframe)", _render_blocks(tb, 8))
    add("text-slot9-split", "block 0 in slot 9; blocks 1..3 in the next superframe", _render_blocks(tb, 9))
    add("text-straddle", "blocks start in an EVEN frame: every block straddles the sync of its superframe (out of spec)",
        sd.render(sd.halves(tb), start=2))
    add("text-missing-block-late", "sf1: blocks 0,1,3; sf2: block 2 (a late segment completes the message)",
        sd.render(sd.superframes_to_halves([[tb[0], tb[1], F, tb[3]] + [F] * 6, [tb[2]] + [F] * 9])))
    add("text-missing-block-never", "sf1: blocks 0,1,3; sf2: filler (message never completes)",
        sd.render(sd.superframes_to_halves([[tb[0], tb[1], F, tb[3]] + [F] * 6, [F] * 10])))
    add("text-repeat-per-superframe", "the 4 blocks in each of 2 superframes (a radio repeats the message)",
        sd.render(sd.superframes_to_halves([tb + [F] * 6, tb + [F] * 6])))
    add("text-two-messages-split", "A complete in sf1; B blocks 0,1 in sf2 and 2,3 in sf3",
        sd.render(sd.superframes_to_halves([tb + [F] * 6, tb_b[:2] + [F] * 8, tb_b[2:] + [F] * 8])))
    add("text-stale-mix", "sf1: A0 A1 A2; sf2: B3: no message id in the blocks, so old slots mix with new ones",
        sd.render(sd.superframes_to_halves([tb[:3] + [F] * 7, tb_b[3:] + [F] * 9])))
    hi = [blk[:1] + bytes(c | 0x80 for c in blk[1:]) for blk in tb]
    add("text-high-bit", "text bytes with bit 7 set (a bit error): both decoders strip it", _render_blocks(hi))
    add("text-lost-frame", "frame 4 of sf1 never arrives (text in sf1 and sf2): the pair phase is wrong until the next sync",
        _sf(sd.render(sd.superframes_to_halves([tb + [F] * 6, tb + [F] * 6])), drop=(4,)))
    add("text-sync-missing", "text in sf2; the sync frame of sf2 (frame 21) never arrives",
        _sf(sd.render(sd.superframes_to_halves([[F] * 10, tb + [F] * 6])), drop=(21,)))

    # ---- radio header resend
    add("header-aligned", "9 header blocks + filler in one superframe ([STD6] 6.4)", _render_blocks(hb))
    add("header-after-squelch", "code squelch, then the 9 header blocks ([STD6] 6.4: 'or after the code squelch')",
        _render_blocks([sd.squelch_block(19)] + hb))
    add("header-split", "header blocks 0..4 in slots 5..9, blocks 5..8 in the next superframe", _render_blocks(hb, 5))
    add("header-twice", "the full header in each of 2 superframes", sd.render(sd.superframes_to_halves([hb + [F], hb + [F]])))
    add("header-printable-bytes", "9 header blocks of a header whose 41 bytes are all printable ASCII (flags 20 20 20, printable FCS)",
        _render_blocks(sd.header_blocks(_printable_header())))
    add("header-bad-fcs", "9 header blocks of a header whose FCS does not match", _render_blocks(hb_bad))

    # ---- GPS: D-PRS ($$CRC) and NMEA
    add("dprs-aligned", "D-PRS line, 17 blocks over 2 superframes, from slot 0", _render_blocks(dprs_b))
    add("dprs-slot3", "the same from slot 3 (spills over the sync)", _render_blocks(dprs_b, 3))
    add("dprs-bad-crc", "one bit of the string flipped after the CRC was computed", _render_blocks(sd.simple_data_blocks(bytes(dprs_bad))))
    add("dprs-text-interleave", "Text + APRS pattern T A T A T A T A A A (from [SLOW])",
        sd.render(sd.superframes_to_halves(sd.interleave(sd.LAYOUTS["text-aprs"], {"T": ("cycle", tb), "A": ("once", dprs_b)}))))
    add("dprs-crc-without-cr", "the CRC computed over the string WITHOUT the final CR (the oracle must refuse it)",
        _render_blocks(sd.simple_data_blocks(sd.dprs_line(DPRS_PAYLOAD, crc_covers_cr=False))))
    add("dprs-two-lines", "two D-PRS lines one after the other (collector state after a valid line)",
        _render_blocks(dprs_b + sd.simple_data_blocks(sd.dprs_line(DPRS_PAYLOAD.replace("home", "work")))))
    add("dprs-then-text", "D-PRS line (17 blocks, 2 superframes) then a message in the third superframe (DSDcc evaluates a GPS line only when a text or header block starts a superframe)",
        sd.render(sd.superframes_to_halves(sd.place(dprs_b, 0, 2) + [tb + [F] * 6])))
    add("dprs-len-299", "D-PRS sentence of exactly 299 bytes ('$$CRC' to the CR), valid CRC",
        _render_blocks(sd.simple_data_blocks(_dprs_of_length(299))))
    add("dprs-len-300", "D-PRS sentence of exactly 300 bytes ('$$CRC' to the CR), valid CRC",
        _render_blocks(sd.simple_data_blocks(_dprs_of_length(300))))
    add("dprs-len-301", "the same with 301 bytes (one over the DV-Router GPS-A buffer)",
        _render_blocks(sd.simple_data_blocks(_dprs_of_length(301))))
    add("nmea-gga", "$GPGGA with its checksum", _render_blocks(gga_b))
    add("nmea-rmc", "$GPRMC with its checksum", _render_blocks(rmc_b))
    add("nmea-bad-checksum", "$GPGGA with one field changed after the checksum was computed", _render_blocks(gga_bad))
    add("nmea-gga-rmc-free", "GGA, RMC and the free-form line one after the other ([SLOW]: three sets)",
        _render_blocks(gga_b + rmc_b + free_b))
    add("nmea-six-types", "GGA, GLL, VTG, RMC, GSA, GSV one after the other (the six DStarGateway collects)",
        _render_blocks(sum((sd.simple_data_blocks(x.encode()) for x in (GGA, GLL, VTG, RMC, GSA, GSV)), [])))
    add("nmea-len-82", "NMEA sentence of 82 bytes including CR LF (the NMEA 0183 maximum), valid checksum",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(82).encode())))
    add("nmea-len-83", "the same with 83 bytes", _render_blocks(sd.simple_data_blocks(_nmea_of_length(83).encode())))
    add("nmea-len-80", "NMEA sentence of 80 bytes including CR LF (line body 78), valid checksum: inside every reading of the DV-Router limit",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(80).encode())))
    add("nmea-len-81", "the same with 81 bytes including CR LF (line body 79)", _render_blocks(sd.simple_data_blocks(_nmea_of_length(81).encode())))
    add("nmea-len-84", "the same with 84 bytes including CR LF (line body 82, one over the 83 of reading A)",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(84).encode())))
    add("nmea-len-85", "the same with 85 bytes including CR LF (line body 83, over the limit in every reading)",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(85).encode())))
    add("nmea-text-interleave", "Text + GPS pattern T G T G T G T G G G (from [SLOW]) carrying the GGA",
        sd.render(sd.superframes_to_halves(sd.interleave(sd.LAYOUTS["text-gps"], {"T": ("cycle", tb), "G": ("once", gga_b)}))))

    # ---- length nibble of the 0x3x blocks (named difference DIFF-NIBBLE: DV-Router reads it,
    # DStarGateway does not), block 3 of the GGA sentence is a full block of 5 characters
    add("nmea-nibble-0x36", "GGA with the length nibble of block 3 set to 6 (0x36, full block): DV-Router drops 0x36..0x3F",
        _render_blocks(_with_mini(gga_b, 3, 0x36)))
    add("nmea-nibble-0x3f", "GGA with the length nibble of block 3 set to 15 (0x3F, full block)",
        _render_blocks(_with_mini(gga_b, 3, 0x3F)))
    add("nmea-nibble-0x30", "GGA with the length nibble of block 3 set to 0 (0x30, full block): DV-Router reads n = 0 as 5",
        _render_blocks(_with_mini(gga_b, 3, 0x30)))
    # the two characters a low nibble cuts off must differ: the NMEA checksum is an XOR, equal pairs cancel
    low = next(i for i in range(2, len(gga_b) - 1) if gga_b[i][4] != gga_b[i][5])
    add("nmea-nibble-low", "GGA with block %d marked 0x33 although it holds 5 characters (nibble disagrees with the content): 3 are used" % low,
        _render_blocks(_with_mini(gga_b, low, 0x33)))
    add("nmea-nibble-short-block", "GGA in blocks of 5,5,5,3,5,... (a short block of 3 characters + 2 filler in the middle, legal): the nibble is needed",
        _render_blocks(_sized_blocks(GGA.encode(), [5, 5, 5, 3, 5])))

    # ---- fixed frame pairs of the DV-Router unit: a frame of a pair missing (holes in the presence bitmap)
    add("pair-hole-text-once", "message in sf1 with frame 6 (second of pair 5,6) never received, sf2 filler: the block is skipped, no text",
        _sf(sd.render(sd.superframes_to_halves([tb + [F] * 6, [F] * 10])), drop=(6,)))
    add("pair-hole-text-twice", "the 4 blocks twice in sf1, frame 4 missing (pair 3,4, block 1 of the first copy): the second copy completes it",
        _sf(sd.render(sd.superframes_to_halves([tb + tb + [F] * 2, [F] * 10])), drop=(4,)))
    add("pair-hole-nmea-then-ok", "GGA then RMC (30 blocks), frame 6 missing in sf1: the GGA line is lost, the RMC after it still decodes",
        _sf(_render_blocks(gga_b + rmc_b), drop=(6,)))

    # ---- squelch, fast data, filler
    add("squelch-only", "code squelch 19 in slot 0", _render_blocks([sd.squelch_block(19)]))
    add("squelch-mismatch", "0xC2 with two different code bytes", _render_blocks([sd.block(0xC2, b"\x19\x18")]))
    payload = bytes((7 * i + 1) & 0xFF for i in range(208))
    fast = sd.fast_data_superframe(payload)
    add("fast-data-superframe", "28 + 9 x 20 bytes: only the data fields (mini-headers 0x9C, then 0x94 x 9)",
        [(seq, data) for seq, _v, data in fast])
    add("fast-length-max", "mini-header 0x9C (28 bytes), the largest valid length", _render_blocks([sd.block(0x9C, b"abcde")]))
    add("fast-length-invalid", "mini-header 0x9D: above the 28 bytes a block can hold", _render_blocks([sd.block(0x9D, b"abcde")]))
    add("filler-only", "20 filler data fields (66 66 66 on the wire 16 29 F5)", sd.render([], start=1, frames=21))

    # ---- stage 1c, the DV-Router source contract (dstar.rs): line terminators and checksum rejects
    rmc_bad = RMC[:RMC.index("*") + 1] + ("00" if not RMC.endswith("00\r\n") else "FF") + "\r\n"
    gga_nostar = GGA[:GGA.index("*")] + "\r\n"
    add("nmea-reject-bad-checksum", "RMC whose two checksum digits are wrong (the body is intact): DV-Router counts a Crc reject, DStarGateway is silent",
        _render_blocks(sd.simple_data_blocks(rmc_bad.encode())))
    add("nmea-reject-no-star", "GGA without '*' and checksum digits: DV-Router counts a Crc reject (no '*' is a Crc), DStarGateway is silent",
        _render_blocks(sd.simple_data_blocks(gga_nostar.encode())))
    add("nmea-term-lf-only", "GGA ending in LF alone: DV-Router ends a line at CR or LF",
        _render_blocks(sd.simple_data_blocks(GGA.rstrip("\r\n").encode() + b"\n")))
    add("nmea-term-cr-only", "GGA ending in CR alone: DV-Router ends the line at the CR, DStarGateway waits for an LF that never comes",
        _render_blocks(sd.simple_data_blocks(GGA.rstrip("\r\n").encode() + b"\r")))
    add("dprs-term-lf", "D-PRS line (CRC over the string) ended by an LF instead of a CR: DV-Router needs the CR (Invalid)",
        _render_blocks(sd.simple_data_blocks(sd.dprs_line(DPRS_PAYLOAD, cr=False) + b"\n")))
    return cases


# ------------------------------------------------------------------ driver text

def driver_input(cases):
    """The stdin text for both drivers: one B / F... / E group per case."""
    lines = []
    for name, _note, frames in cases:
        lines.append("B %s" % name)
        for seq, wire in frames:
            lines.append("F %d %s" % (seq, wire.hex()))
        lines.append("E")
    return "\n".join(lines) + "\n"


def parse_driver_output(text):
    """{case: [event]} from a driver's stdout. Event: {"kind", "seq", "hex"}; lines that do not start
    with 'D' or 'R ' (log text) are ignored. Events before the first 'D' line are dropped."""
    out = {}
    cur = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "D":
            if parts[1] != "end":
                cur = parts[1]
                out[cur] = []
            continue
        if len(parts) == 4 and parts[0] == "R" and cur is not None:
            out[cur].append({"kind": parts[1], "seq": int(parts[2]), "hex": parts[3]})
    return out


def docker_argv(entrypoint, image=IMAGE, env=None):
    return (["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=16m"]
            + limits.docker_run_args(env) + ["--entrypoint", entrypoint, image])


def run_driver(which, cases, image=IMAGE):
    """Run `which` ('mmdvm' or 'dgw') on the corpus; returns {case: [event]}."""
    argv = docker_argv("/usr/local/bin/dstarsd-%s" % which, image)
    proc = subprocess.run(argv, input=driver_input(cases), capture_output=True, text=True,
                          timeout=RUN_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError("dstarsd-%s exited %d: %s" % (which, proc.returncode, proc.stderr[-400:]))
    return parse_driver_output(proc.stdout)


# ------------------------------------------------------------------ our side, in the oracle's terms

def nmea_id(sentence):
    """'GPGGA' of '$GPGGA,...'."""
    return sentence[1:6]


def dgw_nmea_line(sentence, call="N0CALL"):
    """The APRS line DStarGateway's CNMEASentenceCollector builds from a valid sentence (read from its
    source, confirmed by the oracle run): the checksum is cut and recomputed as two LOWER case
    hex digits, the CR LF is gone, a time stamp 000000.00 would be replaced by the clock (not used
    in the corpus), the prefix is "<CALL>-5>GPS30,DSTAR*:"."""
    text = sentence.rstrip("\r\n")
    body = text[1:].rpartition("*")[0]
    cut = "$" + body
    return "%s-5>GPS30,DSTAR*:%s*%02x" % (call, cut, sd.nmea_checksum(body))


def mmdvm_clean_header(raw41):
    """The header MMDVMHost's CDStarSlowData::getHeader() hands out, or None: it first masks the flags byte
    with 0x68, zeroes flag bytes 2 and 3, clears bit 7 of bytes 3..38 (read from the pinned source, confirmed
    by the case header-printable-bytes) and only then checks the FCS, so a header whose flag bytes are not
    the cleaned ones fails."""
    hdr = bytearray(raw41)
    hdr[0] &= 0x68
    hdr[1] = hdr[2] = 0
    for i in range(3, 39):
        hdr[i] &= 0x7F
    if sd.crc16_x25(bytes(hdr[:39])).to_bytes(2, "little") != bytes(hdr[39:41]):
        return None
    return bytes(hdr)


def ours_mmdvm_view(frames):
    """What MMDVMHost's decoder should print, according to dvtest.dstar_sd.Decoder("reset"):
    list of {"kind", "seq", "hex"} in the order of the driver output (block, header, text per frame)."""
    dec = sd.Decoder("reset")
    out = []
    for seq, wire in frames:
        before = len(dec.blocks)
        events = dec.feed(seq, wire)
        if len(dec.blocks) > before:
            out.append({"kind": "block", "seq": seq, "hex": "%02x" % dec.blocks[-1][1]})
        for ev in events:
            if ev["kind"] == "header":
                clean = mmdvm_clean_header(ev["bytes"])
                if clean is not None:
                    out.append({"kind": "header", "seq": seq, "hex": clean.hex()})
        for ev in events:
            if ev["kind"] == "text":
                out.append({"kind": "text", "seq": seq, "hex": ev["text"].encode("ascii").hex()})
    return _mmdvm_order(out)


def _mmdvm_order(events):
    """The driver prints block, then header, then text for one frame: ours is already in that order."""
    return events


def ours_dgw_view(frames):
    """What DStarGateway's collectors should print, according to dvtest.dstar_sd.Decoder("accumulate",
    nibble="ignore"): text, gpsa (valid CRC only), nmea:<ID> (valid checksum only). The collectors
    add 5 bytes per 0x3x block and do not read the length nibble."""
    dec = sd.Decoder("accumulate", max_dprs=10 ** 6, max_nmea=10 ** 6, nibble="ignore")
    out = []
    for seq, wire in frames:
        for ev in dec.feed(seq, wire):
            if ev["kind"] == "text":
                out.append({"kind": "text", "seq": seq, "hex": ev["text"].encode("ascii").hex()})
            elif ev["kind"] == "dprs" and ev["crc_ok"]:
                out.append({"kind": "gpsa", "seq": seq, "hex": ev["line"].hex()})
            elif ev["kind"] == "nmea" and ev["checksum_ok"]:
                out.append({"kind": "nmea:" + nmea_id(ev["sentence"]), "seq": seq,
                            "hex": dgw_nmea_line(ev["sentence"]).encode("ascii").hex()})
    return out


def _model_events(events):
    """Events of dvtest.dstar_sd.DvrUnitDecoder in the oracles' terms: text, gpsa (valid CRC), nmea:<ID>
    (valid RMC / GGA checksum). Header, squelch and fast data are opaque (no events)."""
    out = []
    for ev in events:
        seq = ev["seq"]
        if ev["kind"] == "text":
            out.append({"kind": "text", "seq": seq, "hex": ev["text"].encode("ascii").hex()})
        elif ev["kind"] == "dprs" and ev["crc_ok"]:
            out.append({"kind": "gpsa", "seq": seq, "hex": ev["line"].hex()})
        elif ev["kind"] == "nmea" and ev["checksum_ok"]:
            out.append({"kind": "nmea:" + nmea_id(ev["sentence"]), "seq": seq,
                        "hex": dgw_nmea_line(ev["sentence"]).encode("ascii").hex()})
    return out


def dvrouter_results(frames, dp5f=False):
    """[{"events", "outcome", "reject"}] per unit of an over from the DV-Router unit model."""
    return sd.DvrUnitDecoder(dp5f=dp5f).outcomes(frames)


def ours_dvrouter_view(frames, dp5f=False):
    """What the DV-Router unit model (dvtest.dstar_sd.DvrUnitDecoder, a reading of dstar.rs) says, in the
    oracles' event terms: text, gpsa, nmea:<ID>. dp5f=True is the rule set the DV-Router lead plans for DP5f."""
    out = []
    for res in dvrouter_results(frames, dp5f):
        out += _model_events(res["events"])
    return out


def _norm(events):
    return [(e["kind"], e["seq"], e["hex"]) for e in events]


# Basis of every expectation of this revision: the model of dstar.rs, VERIFIED by reading the lines
# (RULES). The oracles are cross-checks (cases/unit/test_dstar_sd_dvrouter.py), not the source of a value.
BASIS = "dvrouter-source"
BASIS_DP5F = "lead-statement-unverified"
DP5F_RULE = ("DP5f as stated by the DV-Router lead (NOT in the code read): NMEA 0183 line limit, 82 including CR LF = 80 content "
             "bytes; text segments cleared at the start of every unit (MMDVMHost's reset at a sync)")

# Named, documented differences between DV-Router's expectation and DStarGateway's result (the
# lenient alternative). They are differences by design, not failures.
NAMED_DIFFERENCES = {
    "DIFF-NIBBLE": "DV-Router reads the length nibble of 0x3x blocks (0x30..0x35, 0 means 5, 0x36..0x3F ignored, the "
                   "sentence then fails its checksum: Crc), DStarGateway ignores it and adds 5 bytes per block",
    "DIFF-GPS-LIMIT": "DV-Router stores at most 300 bytes of a D-PRS line including the CR and 82 bytes of an NMEA line "
                      "without its terminator (a longer line: Size); DStarGateway has no limit",
    "DIFF-PAIR-HOLE": "DV-Router pairs fixed frames (1,2)..(19,20) of a unit, a hole skips one block; the oracles pair "
                      "the frames they receive in turn, so a lost frame shifts their pairing until the next sync",
    "DIFF-ONE-TEXT-PER-UNIT": "DV-Router hands out at most one text item per unit (dstar.rs:173-175), the oracles one per "
                              "complete set of four blocks",
    "DIFF-NMEA-DELIM": "DV-Router ends an NMEA line at the first CR or LF (dstar.rs:186-187), DStarGateway at the LF: the frame "
                       "number differs when the CR is the last byte of a block",
    "DIFF-NMEA-TYPES": "DV-Router checks and uses only $GPRMC and $GPGGA, every other sentence is ignored (dstar_nmea.rs:107-113); "
                       "DStarGateway collects six types",
    "DIFF-CRC-REJECT": "a bad checksum / CRC is a Reject::Crc of the unit in DV-Router (counted as a worker parse reject, reason "
                       "crc); DStarGateway is silent",
    "DIFF-TERMINATOR": "DV-Router accepts CR or LF as the end of an NMEA line and needs the CR for D-PRS (LF: Invalid); "
                       "DStarGateway ends NMEA at LF and D-PRS at CR",
}
CASE_DIFFERENCES = {
    "text-dvrouter-twice": "DIFF-ONE-TEXT-PER-UNIT",
    "nmea-nibble-0x36": "DIFF-NIBBLE", "nmea-nibble-0x3f": "DIFF-NIBBLE", "nmea-nibble-low": "DIFF-NIBBLE",
    "nmea-nibble-short-block": "DIFF-NIBBLE",
    "dprs-len-301": "DIFF-GPS-LIMIT", "nmea-len-85": "DIFF-GPS-LIMIT",
    "pair-hole-text-twice": "DIFF-PAIR-HOLE", "text-lost-frame": "DIFF-PAIR-HOLE",
    "dprs-bad-crc": "DIFF-CRC-REJECT", "dprs-crc-without-cr": "DIFF-CRC-REJECT", "nmea-bad-checksum": "DIFF-CRC-REJECT",
    "nmea-reject-bad-checksum": "DIFF-CRC-REJECT", "nmea-reject-no-star": "DIFF-CRC-REJECT",
    "nmea-gga": "DIFF-NMEA-DELIM", "nmea-gga-rmc-free": "DIFF-NMEA-DELIM", "nmea-len-81": "DIFF-NMEA-DELIM",
    "nmea-text-interleave": "DIFF-NMEA-DELIM", "nmea-nibble-0x30": "DIFF-NMEA-DELIM",
    "nmea-six-types": "DIFF-NMEA-TYPES",
    "nmea-term-cr-only": "DIFF-TERMINATOR", "dprs-term-lf": "DIFF-TERMINATOR",
}
# Cases whose model result rests on something the model does not follow to the end (see MODEL_LIMITS)
UNMODELLED = {
    "nmea-gga-rmc-free": "the 29 byte free-form line takes the station ID line path (dstar_nmea.rs:107-148): Invalid unless 29 "
                         "bytes, Source / Spoof from the stream's callsign, Crc without '*'; not modelled, so the unit may "
                         "really be a reject",
}


def split_text_gps(events):
    """([text events], [gps events: gpsa and nmea:*]) of a normalised event list."""
    return [e for e in events if e[0] == "text"], [e for e in events if e[0] != "text" and e[0] != "block" and e[0] != "header"]


def _expectation(frames, dp5f):
    """{"text", "gps", "outcome"} of the unit model: two lists of [kind, seq, hex] in time order (the frame number
    restarts every superframe, so the two lists are not merged) and one outcome string per unit."""
    results = dvrouter_results(frames, dp5f)
    events = []
    for res in results:
        events += _model_events(res["events"])
    text, gps = split_text_gps(_norm(events))
    return {"text": text, "gps": gps, "outcome": [r["outcome"] for r in results]}


def expected_dvrouter(name, mmdvm_events, dgw_events, model_events, frames=None):
    """DV-Router's CURRENT behaviour for one case, as {"basis", "text", "gps", "outcome"} (+ "unmodelled"): the
    unit model of dstar.rs (basis "dvrouter-source"; every rule VERIFIED by reading, RULES). With `frames` the
    outcome per unit is included; without, only text and gps of `model_events`. The two oracle arguments are
    kept for the callers: the oracles are cross-checks in the tests, not the source of a value."""
    if frames is not None:
        out = dict(basis=BASIS, **_expectation(frames, False))
    else:
        text, gps = split_text_gps(_norm(model_events))
        out = {"basis": BASIS, "text": text, "gps": gps}
    if name in UNMODELLED:
        out["unmodelled"] = UNMODELLED[name]
    return out


def expected_dvrouter_after_dp5f(name, frames):
    """The expectation under the DP5f rules the DV-Router lead plans (DP5F_RULE), or None when it equals the
    current one. basis "lead-statement-unverified": the code read does not implement it yet."""
    cur, new = _expectation(frames, False), _expectation(frames, True)
    if cur == new:
        return None
    return dict(basis=BASIS_DP5F, rule=DP5F_RULE, **new)


def compare(name, frames, mmdvm_events, dgw_events):
    """One vector record: frames, what each oracle printed, our views and whether they agree. The two
    oracles are compared with ours for the kinds each one implements: MMDVMHost text / header /
    block types; DStarGateway text / gpsa / nmea."""
    ours_m = ours_mmdvm_view(frames)
    ours_d = ours_dgw_view(frames)
    rec = {
        "case": name,
        "frames": ["%d %s" % (seq, wire.hex()) for seq, wire in frames],
        "mmdvm": _norm(mmdvm_events),
        "dgw": _norm(dgw_events),
        "ours_reset": _norm(ours_m),
        "ours_accumulate": _norm(ours_d),
    }
    rec["mmdvm_match"] = rec["mmdvm"] == rec["ours_reset"]
    rec["dgw_match"] = rec["dgw"] == rec["ours_accumulate"]
    # the two oracles side by side on the one kind both decode
    texts_m = [e for e in rec["mmdvm"] if e[0] == "text"]
    texts_d = [e for e in rec["dgw"] if e[0] == "text"]
    rec["text_agree"] = texts_m == texts_d
    # DV-Router: the decision of DP5f is MMDVMHost's reset at every sync; the unit model is the generator's
    # reading of the lead's statements and must reproduce the expectation
    model = ours_dvrouter_view(frames)
    rec["unit66_hex"] = [sd.unit66(u).hex() for u in sd.split_units(frames)]
    rec["expected_dvrouter"] = expected_dvrouter(name, mmdvm_events, dgw_events, model, frames)
    after = expected_dvrouter_after_dp5f(name, frames)
    if after is not None:
        rec["expected_dvrouter_after_dp5f"] = after
    rec["lenient_alternative"] = rec["dgw"]
    rec["ours_dvrouter"] = _norm(model)
    exp = rec["expected_dvrouter"]
    rec["dvrouter_match"] = split_text_gps(rec["ours_dvrouter"]) == (exp["text"], exp["gps"])
    # DStarGateway never rejects a unit: the outcome counts as a difference too
    rec["lenient_differs"] = (split_text_gps(rec["lenient_alternative"]) != (exp["text"], exp["gps"])
                              or any(o != "parsed" for o in exp["outcome"]))
    if name in CASE_DIFFERENCES:
        rec["named_difference"] = CASE_DIFFERENCES[name]
    return rec


def pins(path=None):
    """The DSTARSD_* settings of .env.example (never hard-coded here)."""
    import os
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")
    out = {}
    with open(path) as fh:
        for line in fh:
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("DSTARSD_"):
                out[key] = value
    return out


# ------------------------------------------------------------------ unit level rejects (dstar.rs:122-141)

def _unit_of(blocks, first_slot=0):
    """The raw 66 byte unit of one superframe holding `blocks` from `first_slot` (the sync frame included)."""
    units = sd.split_units(_render_blocks(blocks, first_slot, 1))
    assert len(units) == 1
    return sd.unit66(units[0])


def _hexes(*units):
    return [bytes(u).hex() for u in units]


def unit_vectors():
    """The unit level vectors of the DV-Router decoder, expected outcomes stated by hand from the rules of
    dstar.rs (RULES), NOT taken from the model (the tests compare the two): bad spare bits, a wrong frame 0
    pattern, 65 / 67 byte units, the old MSB first bitmap, and sequences that show the state reset of a reject.
    Each: name, note, rule, unit_hex (a list: units fed in order, any length), expected_dvrouter {outcome, text,
    gps}. Reject names are the Rust ones (Reject::TooShort, Size, Invalid)."""
    tb = sd.text_blocks(TEXT)
    ok = _unit_of(tb)                                       # a valid unit holding the whole message
    part_a = _unit_of(tb[:3])                               # blocks 0..2: no text yet
    part_b = _unit_of([None, None, None, tb[3]])            # block 3 in slot 3 (frames 7, 8)
    with_spare = lambda mask: bytes([ok[0], ok[1], ok[2] | mask]) + ok[3:]
    wrong_sync = lambda three: ok[:3] + three + ok[6:]
    text_ev = [["text", 8, _text_hex()]]
    one = lambda out, text=None: {"outcome": out, "text": text or [], "gps": []}
    vectors = [
        ("unit-valid", "the complete unit of a message (ffff1f 552d16 ...)", "unit-bitmap", _hexes(ok), one(["parsed"], text_ev)),
        ("unit-65-bytes", "one byte short", "unit-size", _hexes(ok[:-1]), one(["reject:TooShort"])),
        ("unit-67-bytes", "one byte too long", "unit-size", _hexes(ok + b"\x00"), one(["reject:Size"])),
        ("unit-empty", "no bytes at all", "unit-size", _hexes(b""), one(["reject:TooShort"])),
        ("unit-132-bytes", "two units glued together", "unit-size", _hexes(ok + ok), one(["reject:Size"])),
        ("unit-spare-bit-5", "byte 2 bit 5 (0x20) set", "unit-spare", _hexes(with_spare(0x20)), one(["reject:Invalid"])),
        ("unit-spare-bit-6", "byte 2 bit 6 (0x40) set", "unit-spare", _hexes(with_spare(0x40)), one(["reject:Invalid"])),
        ("unit-spare-bit-7", "byte 2 bit 7 (0x80) set", "unit-spare", _hexes(with_spare(0x80)), one(["reject:Invalid"])),
        ("unit-old-msb-first-bitmap", "the retired MSB first layout of a complete unit (fffff8): byte 2 has spare bits set",
         "unit-spare", _hexes(bytes.fromhex("fffff8") + ok[3:]), one(["reject:Invalid"])),
        ("unit-frame0-wrong-last-byte", "bit 0 set, frame 0 = 55 2d 17", "unit-sync", _hexes(wrong_sync(b"\x55\x2d\x17")), one(["reject:Invalid"])),
        ("unit-frame0-zero", "bit 0 set, frame 0 = 00 00 00 (an absent frame marked present)", "unit-sync",
         _hexes(wrong_sync(b"\x00\x00\x00")), one(["reject:Invalid"])),
        ("unit-frame0-scrambled", "bit 0 set, frame 0 = the sync XORed with the scrambler", "unit-sync",
         _hexes(wrong_sync(sd.xor3(sd.SYNC))), one(["reject:Invalid"])),
        ("unit-frame0-absent-garbage", "bit 0 CLEAR, frame 0 bytes are garbage: not checked, the unit decodes", "unit-sync",
         _hexes(bytes([ok[0] & 0xFE]) + ok[1:3] + b"\xff\xff\xff" + ok[6:]), one(["parsed"], text_ev)),
        ("unit-bitmap-empty", "bitmap 000000: no frame present, nothing to decode", "unit-bitmap",
         _hexes(bytes(66)), one(["parsed"])),
        ("unit-frame20-only", "bitmap 000010: only frame 20 (bit 4 of byte 2) present, no pair complete", "unit-bitmap",
         _hexes(bytes.fromhex("000010") + bytes(60) + b"\xaa\xbb\xcc"), one(["parsed"])),
        ("seq-reset-after-invalid", "blocks 0..2, a unit with a spare bit set, block 3: the Invalid unit resets the text "
         "segments, so block 3 alone makes no text", "unit-spare", _hexes(part_a, with_spare(0x20), part_b),
         one(["parsed", "reject:Invalid", "parsed"])),
        ("seq-reset-after-too-short", "blocks 0..2, a 65 byte unit, block 3: the TooShort unit resets the text segments",
         "unit-size", _hexes(part_a, ok[:-1], part_b), one(["parsed", "reject:TooShort", "parsed"])),
        ("seq-reset-after-wrong-sync", "blocks 0..2, a unit with a wrong frame 0, block 3", "unit-sync",
         _hexes(part_a, wrong_sync(b"\x55\x2d\x17"), part_b), one(["parsed", "reject:Invalid", "parsed"])),
        ("seq-no-reset-without-reject", "the same without the bad unit in between: the segments persist across units "
         "today, the text completes at frame 8 of the second unit (DP5f: it would not)", "text", _hexes(part_a, part_b),
         one(["parsed", "parsed"], text_ev)),
    ]
    out = []
    for name, note, rule, units, exp in vectors:
        rec = {"name": name, "note": note, "rule": RULES[rule], "unit_hex": units,
               "expected_dvrouter": dict(exp, basis=BASIS)}
        if name == "seq-no-reset-without-reject":
            rec["expected_dvrouter_after_dp5f"] = {"basis": BASIS_DP5F, "rule": DP5F_RULE, "outcome": ["parsed", "parsed"],
                                                   "text": [], "gps": []}
        out.append(rec)
    return out


def _text_hex():
    return TEXT.encode("ascii").hex()


def unit_vector_model_outcomes(rec, dp5f=False):
    """What the unit model says about one unit vector: [outcome] and the text events (for the tests)."""
    dec = sd.DvrUnitDecoder(dp5f=dp5f)
    outs, text = [], []
    for h in rec["unit_hex"]:
        res = dec.feed_raw(bytes.fromhex(h))
        outs.append(res["outcome"])
        text += [["text", e["seq"], e["text"].encode("ascii").hex()] for e in res["events"] if e["kind"] == "text"]
    return outs, text


def build_records(image=IMAGE):
    cases = corpus()
    mm = run_driver("mmdvm", cases, image)
    dg = run_driver("dgw", cases, image)
    records = []
    for name, note, frames in cases:
        rec = compare(name, frames, mm.get(name, []), dg.get(name, []))
        rec["note"] = note
        records.append(rec)
    return records


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["run", "input"])
    ap.add_argument("--out", default="vectors/dstar_sd/corpus.json")
    args = ap.parse_args(argv)
    if args.action == "input":
        sys.stdout.write(driver_input(corpus()))
        return 0
    records = build_records()
    doc = {"schema": SCHEMA, "image": IMAGE, "pins": pins(), "named_differences": NAMED_DIFFERENCES,
           "unit66": UNIT66_DOC, "dvrouter_source": {"file": "crates/lib/pkt/src/dstar.rs", "rules": RULES,
                                                     "model_limits": MODEL_LIMITS, "dp5f": DP5F_RULE},
           "cases": records, "unit_vectors": unit_vectors()}
    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    bad = [r["case"] for r in records if not (r["mmdvm_match"] and r["dgw_match"] and r["dvrouter_match"])]
    print("cases %d, differing from our decoder: %s" % (len(records), ", ".join(bad) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
