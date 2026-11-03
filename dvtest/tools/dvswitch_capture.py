# SPDX-License-Identifier: MIT
"""Run inside the tester (docker compose exec tester python -m dvtest.tools.dvswitch_capture <scenario> [--link-a]):
capture on the edge's side (asl-tap) into out/<run>/asl-lab/<scenario>/ until the file
$DVT_OUT/dvswitch-stop-<scenario> appears (or 300 s), optionally with lab node A linked to the edge so the
edge's relayed voice can be read; then print a one-line JSON verdict input (frame summary, bytes of ulaw the
edge sent to A). The verdict also carries `registration`: the REGREQ / CALLTOKEN / REGAUTH / REGACK steps in order
(iax2_frames.registration_steps_from_doc). Used by tools/dvswitch_radio_case.py and tools/dvswitch_phone_case.py (docs/asl-dvswitch.md)."""
import json
import os
import sys
import time

from dvtest import asl_lab, iax2_frames


def main(argv):
    name = argv[0]
    link_a = "--link-a" in argv
    lab = asl_lab.from_env()
    if lab is None or lab.hosts.get("edge") is None:
        print("no asl-edge lab", file=sys.stderr)
        return 2
    lab.wait_ready()
    a, edge = lab.node("a"), lab.node("edge")
    stop = os.path.join(os.environ["DVT_OUT"], "dvswitch-stop-" + name)
    if os.path.exists(stop):
        os.remove(stop)
    if link_a:
        a.disconnect_links()
        a.hangup_all()
        time.sleep(1)
    with lab.scenario(name, capture=["edge"], hosts=["a"]) as sc:
        if link_a:
            a.cli("rpt fun %s *3%s" % (a.node, edge.node))
            a.wait_link(edge.node)
        print("capturing", flush=True)
        t0 = time.monotonic()
        while not os.path.exists(stop) and time.monotonic() - t0 < 300:
            time.sleep(0.5)
        if link_a:
            a.disconnect_links()
    doc = iax2_frames.extract(sc.dir / "edge.pcap", ports=(lab.port,))
    raw = asl_lab.ulaw_from_pcap(sc.dir / "edge.pcap", edge.ip, a.ip, lab.port) if link_a else b""
    print(json.dumps({"scenario": name, "summary": doc["summary"], "edge_to_a_ulaw_bytes": len(raw), "dir": str(sc.dir),
                      "registration": iax2_frames.registration_steps_from_doc(doc)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
