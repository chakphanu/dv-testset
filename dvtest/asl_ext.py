# SPDX-License-Identifier: MIT
"""Helpers of the external dial test (profile asl-ext, cases/test_asl_ext.py): pure functions that read the
CLI text of res_rpt_http_registrations and the frames JSON of dvtest.iax2_frames. Nothing here talks to a network.

Public IP addresses show up in what these functions return (the registry's view of the node, the DNS answers
for the dialled nodes); the callers keep them in out/<run>/asl-ext/ and never in a committed file.
"""
import re

IPPORT = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")


def parse_registrations(text):
    """Rows of `rpt show registrations`: host, user, perceived ("ip:port" or None), refresh (int or None), state.

    The table is `Host Username Perceived Refresh State`; an unregistered row shows `<Unregistered>`."""
    rows = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or f[0] == "Host" or not f[1].isdigit():
            continue
        perceived = f[2] if IPPORT.match(f[2]) else None
        refresh = int(f[3]) if f[3].isdigit() else None
        state = " ".join(f[4:]) if refresh is not None else " ".join(f[3:])
        rows.append({"host": f[0], "user": f[1], "perceived": perceived, "refresh": refresh, "state": state})
    return rows


def registered(rows, node):
    """The row of `node` when its state is exactly Registered, else None."""
    for row in rows:
        if row["user"] == str(node) and row["state"].strip().lower() == "registered":
            return row
    return None


def lstats_state(text, peer):
    """State word of `peer` in `rpt lstats` ("ESTABLISHED", "CONNECTING", ...) or None when it is not listed."""
    for line in text.splitlines():
        f = line.split()
        if f[:1] == [str(peer)]:
            return f[-1]
    return None


def host_of(endpoint):
    return endpoint.rsplit(":", 1)[0]


def dns_for(doc, node):
    """What the capture's DNS messages say about `node`: the SRV and A answers seen (public addresses: out/ only)."""
    out = {"srv": [], "a": [], "rcodes": []}
    for msg in doc.get("dns", []):
        if not msg.get("qr"):
            continue
        names = [q["name"] for q in msg["questions"]]
        if not any(("%s." % node) in n or n.startswith("%s." % node) for n in names):
            continue
        out["rcodes"].append({"names": names, "rcode": msg["rcode"], "frame": msg["frame"], "t": msg["t"]})
        for ans in msg["answers"]:
            if ans["type"] == "SRV":
                out["srv"].append(ans.get("text"))
            elif ans["type"] == "A":
                out["a"].append({"name": ans["name"], "ip": ans.get("text")})
    return out


def calls(doc):
    """Own calls of a capture: [{"own": ip, "peer": ip, "scall": n, "called": node, "peer_scall": n or None,
    "accepted": bool, "answered": bool, "hangup_by": "own"|"peer"|None}] in order of the NEW frames. The own
    side is the sender of the NEW."""
    types = doc["types"]
    out = []
    seen = set()
    for f in types.get("iax.NEW", []):
        key = (f["src"], f["scall"])
        if key in seen:                  # the NEW repeated with the call token
            continue
        seen.add(key)
        called = next((ie.get("text") for ie in f.get("ies", []) if ie["name"] == "CALLED_NUMBER"), None)
        out.append({"own": host_of(f["src"]), "peer": host_of(f["dst"]), "scall": f["scall"], "called": called,
                    "peer_scall": None, "accepted": False, "answered": False, "hangup_by": None,
                    "new_frame": f["frame"], "t": f["t"]})
    for c in out:
        for f in types.get("iax.ACCEPT", []):
            if host_of(f["src"]) == c["peer"] and f["dcall"] == c["scall"]:
                c["accepted"] = True
                c["peer_scall"] = f["scall"]
        for f in types.get("control.ANSWER", []):
            if host_of(f["src"]) == c["peer"] and f["dcall"] == c["scall"]:
                c["answered"] = True
        for f in types.get("iax.HANGUP", []):
            if f["dcall"] == c["scall"] and host_of(f["src"]) == c["peer"]:
                c["hangup_by"] = "peer"
            elif f["scall"] == c["scall"] and host_of(f["src"]) == c["own"] and c["hangup_by"] is None:
                c["hangup_by"] = "own"
    return out


def text_exchange(doc, call):
    """TEXT frames of one call, both ways, in order: [{"t", "dir", "text"}] (NUL shown as \\0)."""
    out = []
    for f in doc["types"].get("text", []):
        ends = {(host_of(f["src"]), f["scall"]), (host_of(f["dst"]), f["dcall"])}
        if (call["own"], call["scall"]) in ends and call["peer_scall"] is not None:
            out.append({"t": f["t"], "dir": "out" if host_of(f["src"]) == call["own"] else "in",
                        "text": f.get("text", "").replace("\x00", "\\0")})
    return sorted(out, key=lambda e: e["t"])


def rx_audio(doc, call):
    """The mu-law bytes the peer sent on one call (full voice frames of codec ulaw, then mini frames), capture order.

    Frames carry the sender's call number; full frames hold their 12 byte header, mini frames 4. The result is
    what goes to out/ as rx-<node>.ulaw (never committed)."""
    if call.get("peer_scall") is None:
        return b""
    frames = []
    for kind in ("voice.full", "voice.mini"):
        for f in doc["types"].get(kind, []):
            if host_of(f["src"]) == call["peer"] and f["scall"] == call["peer_scall"]:
                if kind == "voice.full" and f.get("codec") != "ulaw":
                    continue
                frames.append((f["frame"], bytes.fromhex(f["hex"])[12 if kind == "voice.full" else 4:]))
    return b"".join(p for _n, p in sorted(frames))
