# SPDX-License-Identifier: MIT
"""IAX2 frames of a pcap / pcapng as hex, grouped by frame type (stdlib only).

This is the extractor behind the byte vectors DV-Router's IAX2 parser tests
consume (docs/asl-lab.md). It reads UDP datagrams with dvtest.pcap, treats
those to or from the IAX2 ports (default 4569) as IAX2, and writes one JSON
document:

    {"version": 1, "source": "a.pcap", "ports": [4569], "frames": 812,
     "summary": {"iax.NEW": 2, "voice.mini": 400, ...},
     "types":   {"iax.NEW": [{"frame": 12, "t": 0.0123, "src": "192.0.2.1:4569",
                              "dst": "192.0.2.2:4569", "form": "full", "scall": 3, "dcall": 0,
                              "retrans": false, "ts": 0, "oseq": 0, "iseq": 0,
                              "kind": 6, "subclass": 1, "ies": [...], "hex": "..."}, ...]},
     "ts16_wraps": [{"call": "...", "before": {...}, "after": {...}}],
     "dns": [{"frame": 3, "qr": 0, "rcode": 0, "questions": [{"name": "...", "type": "SRV"}], "answers": [...], "hex": "..."}]}

Frame types are "<kind>.<NAME>": iax.NEW, iax.AUTHREQ, control.RADIO_KEY,
text, voice.full, voice.mini, cng, meta (trunk / video mini) and so on.
Addresses in the output are the capture's own; pass --anon to rewrite them
to 192.0.2.x placeholders (first-seen order) when the file leaves out/.

    python3 -m dvtest.iax2_frames a.pcap -o a.json [--port 4569] [--limit 3] [--anon]

DNS datagrams (UDP port 53, --dns-port) are listed under "dns" with the question and the answer records
(A, SRV, TXT decoded), the shape ASL's node-name lookup uses.

--limit N keeps at most N frames per type in the output (the first N, plus
every ts16 wrap frame and its neighbours); the summary always counts all.
"""
import argparse
import json
import struct
import sys

from . import pcap

FRAME_KINDS = {1: "dtmf", 2: "voice", 3: "video", 4: "control", 5: "null", 6: "iax", 7: "text",
               8: "image", 9: "html", 10: "cng", 11: "modem", 12: "dtmf_begin"}
IAX_SUBCLASSES = {
    1: "NEW", 2: "PING", 3: "PONG", 4: "ACK", 5: "HANGUP", 6: "REJECT", 7: "ACCEPT", 8: "AUTHREQ",
    9: "AUTHREP", 10: "INVAL", 11: "LAGRQ", 12: "LAGRP", 13: "REGREQ", 14: "REGAUTH", 15: "REGACK",
    16: "REGREJ", 17: "REGREL", 18: "VNAK", 19: "DPREQ", 20: "DPREP", 21: "DIAL", 22: "TXREQ",
    23: "TXCNT", 24: "TXACC", 25: "TXREADY", 26: "TXREL", 27: "TXREJ", 28: "QUELCH", 29: "UNQUELCH",
    30: "POKE", 32: "MWI", 33: "UNSUPPORT", 34: "TRANSFER", 38: "FWDOWNL", 39: "RESERVED", 40: "CALLTOKEN",
}
CONTROL_SUBCLASSES = {
    1: "HANGUP", 2: "RING", 3: "RINGING", 4: "ANSWER", 5: "BUSY", 6: "TAKEOFFHOOK", 7: "OFFHOOK",
    8: "CONGESTION", 9: "FLASH", 10: "WINK", 11: "OPTION", 12: "RADIO_KEY", 13: "RADIO_UNKEY",
    14: "PROGRESS", 15: "PROCEEDING", 16: "HOLD", 17: "UNHOLD", 18: "VIDUPDATE", 19: "MCID",
    20: "SRCUPDATE", 21: "SRCCHANGE", 22: "T38", 23: "CC", 24: "INCOMPLETE", 25: "CONNECTED_LINE",
    26: "REDIRECTING", -1: "STOP_SOUND",
}
# Asterisk format bit values (the voice subclass of full voice frames, and IE 8 / 9)
FORMATS = {1: "g723", 2: "gsm", 4: "ulaw", 8: "alaw", 16: "g726", 32: "adpcm", 64: "slin", 128: "lpc10",
           256: "g729", 512: "speex", 1024: "ilbc", 2048: "g726aal2", 4096: "g722", 32768: "slin16"}
IE_NAMES = {
    1: "CALLED_NUMBER", 2: "CALLING_NUMBER", 3: "CALLING_ANI", 4: "CALLING_NAME", 5: "CALLED_CONTEXT",
    6: "USERNAME", 7: "PASSWORD", 8: "CAPABILITY", 9: "FORMAT", 10: "LANGUAGE", 11: "VERSION",
    12: "ADSICPE", 13: "DNID", 14: "AUTHMETHODS", 15: "CHALLENGE", 16: "MD5_RESULT", 17: "RSA_RESULT",
    18: "APPARENT_ADDR", 19: "REFRESH", 20: "DPSTATUS", 21: "CALLNO", 22: "CAUSE", 23: "IAX_UNKNOWN",
    24: "MSGCOUNT", 25: "AUTOANSWER", 26: "MUSICONHOLD", 27: "TRANSFERID", 28: "RDNIS", 29: "PROVISIONING",
    30: "AESPROVISIONING", 31: "DATETIME", 32: "DEVICETYPE", 33: "SERVICEIDENT", 34: "FIRMWAREVER",
    35: "FWBLOCKDESC", 36: "FWBLOCKDATA", 37: "PROVVER", 38: "CALLINGPRES", 39: "CALLINGTON",
    40: "CALLINGTNS", 41: "SAMPLINGRATE", 42: "CAUSECODE", 43: "ENCRYPTION", 44: "ENCKEY",
    45: "CODEC_PREFS", 46: "RR_JITTER", 47: "RR_LOSS", 48: "RR_PKTS", 49: "RR_DELAY", 50: "RR_DROPPED",
    51: "RR_OOO", 52: "VARIABLE", 53: "OSPTOKEN", 54: "CALLTOKEN", 55: "CAPABILITY2", 56: "FORMAT2",
    57: "CALLINGANI2",
    255: "DATAFORMAT",
}
# IEs whose value is text; the rest are shown as hex (and as a number when 1, 2 or 4 bytes)
TEXT_IES = {1, 2, 3, 4, 5, 6, 7, 10, 13, 15, 16, 17, 22, 26, 28, 52}
ADDR_IES = {18}
FORMAT_IES = {8, 9}
VERSION = 1


class FrameError(ValueError):
    pass


def format_names(mask):
    names = [n for bit, n in sorted(FORMATS.items()) if mask & bit]
    rest = mask & ~sum(FORMATS)
    return names + (["0x%x" % rest] if rest else [])


def parse_ies(data):
    """List of information elements; raises FrameError on a truncated one."""
    out = []
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data) or pos + 2 + data[pos + 1] > len(data):
            raise FrameError("truncated information element at %d" % pos)
        code, length = data[pos], data[pos + 1]
        value = data[pos + 2:pos + 2 + length]
        entry = {"ie": code, "name": IE_NAMES.get(code, "IE_%d" % code), "len": length, "hex": value.hex()}
        if code in TEXT_IES:
            entry["text"] = value.decode("utf-8", "replace")
        elif code in FORMAT_IES and length == 4:
            entry["formats"] = format_names(struct.unpack("!I", value)[0])
        elif code in ADDR_IES and length == 16:
            fam, port, ip = struct.unpack("!HH4s", value[:8])
            entry["addr"] = "%s:%d" % (".".join(str(b) for b in ip), port)
        elif length in (1, 2, 4):
            entry["int"] = int.from_bytes(value, "big")
        out.append(entry)
        pos += 2 + length
    return out


def subclass_value(raw):
    """The subclass byte of a full frame: 0x80|n means 1<<n (a power of two), 255 is -1."""
    if raw == 255:
        return -1
    if raw & 0x80:
        return 1 << (raw & 0x7f)
    return raw


def classify(payload):
    """Parse one IAX2 datagram. Returns (type name, fields dict)."""
    if len(payload) < 4:
        raise FrameError("datagram of %d bytes" % len(payload))
    word0 = struct.unpack("!H", payload[:2])[0]
    if word0 == 0:
        # meta frame: trunk (meta command 1) or video mini; payload layout not interpreted here
        return "meta", {"form": "meta", "meta_cmd": payload[2] if len(payload) > 2 else None}
    if not word0 & 0x8000:
        scall, ts16 = struct.unpack("!HH", payload[:4])
        return "voice.mini", {"form": "mini", "scall": scall, "ts16": ts16, "payload_len": len(payload) - 4}
    if len(payload) < 12:
        raise FrameError("full frame of %d bytes" % len(payload))
    scall, dword, ts, oseq, iseq, kind, raw = struct.unpack("!HHIBBBB", payload[:12])
    sub = subclass_value(raw)
    fields = {"form": "full", "scall": scall & 0x7fff, "dcall": dword & 0x7fff, "retrans": bool(dword & 0x8000),
              "ts": ts, "oseq": oseq, "iseq": iseq, "kind": kind, "subclass": sub, "payload_len": len(payload) - 12}
    body = payload[12:]
    if kind == 6:
        name = IAX_SUBCLASSES.get(sub, "SUBCLASS_%d" % sub)
        try:
            fields["ies"] = parse_ies(body)
        except FrameError as err:
            fields["ies_error"] = str(err)
        return "iax." + name, fields
    if kind == 4:
        return "control." + CONTROL_SUBCLASSES.get(sub, "SUBCLASS_%d" % sub), fields
    if kind == 2:
        fields["codec"] = "+".join(format_names(sub))
        return "voice.full", fields
    if kind == 7:
        fields["text"] = body.decode("utf-8", "replace")
        return "text", fields
    if kind == 10:
        return "cng", fields
    return FRAME_KINDS.get(kind, "kind_%d" % kind), fields


DNS_TYPES = {1: "A", 2: "NS", 5: "CNAME", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV"}


def _dns_name(data, pos, depth=0):
    """(name, position after it) of a possibly compressed DNS name."""
    labels = []
    end = None
    while True:
        if pos >= len(data) or depth > 20:
            raise FrameError("bad DNS name")
        n = data[pos]
        if n == 0:
            pos += 1
            break
        if n & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise FrameError("bad DNS pointer")
            if end is None:
                end = pos + 2
            pos = ((n & 0x3F) << 8) | data[pos + 1]
            depth += 1
            continue
        labels.append(data[pos + 1:pos + 1 + n].decode("ascii", "replace"))
        pos += 1 + n
    return ".".join(labels), (end if end is not None else pos)


def parse_dns(data):
    """Header, questions and answers of a DNS message; raises FrameError when it is cut short."""
    if len(data) < 12:
        raise FrameError("short DNS message")
    ident, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    pos = 12
    questions = []
    for _ in range(qd):
        name, pos = _dns_name(data, pos)
        if pos + 4 > len(data):
            raise FrameError("short DNS question")
        qtype, _qclass = struct.unpack("!HH", data[pos:pos + 4])
        pos += 4
        questions.append({"name": name, "type": DNS_TYPES.get(qtype, str(qtype))})
    answers = []
    for _ in range(an):
        name, pos = _dns_name(data, pos)
        if pos + 10 > len(data):
            raise FrameError("short DNS record")
        rtype, _rclass, ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        rdata = data[pos:pos + rdlen]
        pos += rdlen
        rec = {"name": name, "type": DNS_TYPES.get(rtype, str(rtype)), "ttl": ttl, "rdata": rdata.hex()}
        if rtype == 1 and rdlen == 4:
            rec["text"] = ".".join(str(b) for b in rdata)
        elif rtype == 33 and rdlen > 6:
            prio, weight, port = struct.unpack("!HHH", rdata[:6])
            target, _ = _dns_name(data, pos - rdlen + 6)
            rec["text"] = "%d %d %d %s" % (prio, weight, port, target)
        elif rtype == 16:
            strings, i = [], 0
            while i < rdlen:
                n = rdata[i]
                strings.append(rdata[i + 1:i + 1 + n].decode("ascii", "replace"))
                i += 1 + n
            rec["text"] = " ".join('"%s"' % x for x in strings)
        answers.append(rec)
    return {"id": ident, "qr": flags >> 15, "rcode": flags & 0xF, "questions": questions, "answers": answers}


def _wrap_pairs(minis):
    """Per call (src, dst, scall) the mini frames in capture order where ts16 went backwards by more than half the range."""
    last = {}
    wraps = []
    for item in minis:
        key = (item["src"], item["dst"], item["scall"])
        prev = last.get(key)
        if prev is not None and prev["ts16"] - item["ts16"] > 0x8000:
            wraps.append({"call": "%s>%s scall=%d" % key, "before": prev, "after": item})
        last[key] = item
    return wraps


def extract(path, ports=(4569,), limit=0, anon=False, dns_port=53):
    """Frames of the capture as the JSON-ready dict described in the module docstring."""
    stats = {}
    frames = []
    skipped = {"non_iax_port": 0, "bad": 0}
    dns = []
    for dg in pcap.udp_datagrams(path, stats):
        if dg.sport == dns_port or dg.dport == dns_port:
            try:
                msg = parse_dns(dg.payload)
            except FrameError:
                skipped["bad"] += 1
                continue
            msg.update(frame=dg.frame, t=None if dg.ts is None else round(dg.ts, 6),
                       src="%s:%d" % (dg.src, dg.sport), dst="%s:%d" % (dg.dst, dg.dport), hex=dg.payload.hex())
            dns.append(msg)
            continue
        if dg.sport not in ports and dg.dport not in ports:
            skipped["non_iax_port"] += 1
            continue
        try:
            name, fields = classify(dg.payload)
        except FrameError:
            skipped["bad"] += 1
            continue
        item = {"frame": dg.frame, "t": None if dg.ts is None else round(dg.ts, 6),
                "src": "%s:%d" % (dg.src, dg.sport), "dst": "%s:%d" % (dg.dst, dg.dport), "type": name}
        item.update(fields)
        item["hex"] = dg.payload.hex()
        frames.append(item)
    stamped = [f for f in frames + dns if f["t"] is not None]
    if stamped:
        t0 = min(f["t"] for f in stamped)
        for f in stamped:
            f["t"] = round(f["t"] - t0, 6)
    wraps = _wrap_pairs([f for f in frames if f["type"] == "voice.mini"])
    if anon:
        _anonymise(frames, wraps, dns)
    summary = {}
    types = {}
    keep_frames = set()
    for w in wraps:
        keep_frames.update((w["before"]["frame"], w["after"]["frame"]))
    for f in frames:
        summary[f["type"]] = summary.get(f["type"], 0) + 1
        bucket = types.setdefault(f["type"], [])
        if not limit or len(bucket) < limit or f["frame"] in keep_frames:
            bucket.append(f)
    return {"version": VERSION, "source": str(path).rsplit("/", 1)[-1], "ports": list(ports), "frames": len(frames),
            "skipped": skipped, "capture_stats": stats, "summary": dict(sorted(summary.items())),
            "types": dict(sorted(types.items())), "ts16_wraps": wraps, "dns": dns}


def _anonymise(frames, wraps, dns=()):
    """Replace every address by 192.0.2.<n> in first-seen order (ports kept)."""
    seen = {}

    def swap(ep):
        host, port = ep.rsplit(":", 1)
        if host not in seen:
            seen[host] = "192.0.2.%d" % (len(seen) + 1)
        return "%s:%s" % (seen[host], port)

    for f in frames:
        f["src"], f["dst"] = swap(f["src"]), swap(f["dst"])
    for f in dns:
        f["src"], f["dst"] = swap(f["src"]), swap(f["dst"])
    for w in wraps:
        for side in ("before", "after"):
            w[side]["src"], w[side]["dst"] = swap(w[side]["src"]), swap(w[side]["dst"])
        w["call"] = "%s>%s scall=%d" % (w["before"]["src"], w["before"]["dst"], w["before"]["scall"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="IAX2 frames of a pcap as hex per frame type (JSON)")
    ap.add_argument("pcap")
    ap.add_argument("-o", "--output", help="JSON file (default: stdout)")
    ap.add_argument("--port", type=int, action="append", help="IAX2 UDP port (repeatable, default 4569)")
    ap.add_argument("--limit", type=int, default=0, help="keep at most N frames per type (summary counts all)")
    ap.add_argument("--dns-port", type=int, default=53, help="UDP port whose datagrams are listed as DNS (default 53)")
    ap.add_argument("--anon", action="store_true", help="rewrite addresses to 192.0.2.x")
    args = ap.parse_args(argv)
    doc = extract(args.pcap, tuple(args.port or (4569,)), args.limit, args.anon, args.dns_port)
    text = json.dumps(doc, indent=1, sort_keys=False) + "\n"
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
