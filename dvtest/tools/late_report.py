# SPDX-License-Identifier: MIT
"""Report on the late packets of a W8 run (THREADS1b evidence): are the stalls periodic, and which edge thread was
busy at the time. Inputs: out/<run>/web/load.json (cases.w8_200.late_bins: {epoch ms bin: [late packets, largest
deviation ms]}) and the file of dvtest.tools.thread_sample.

    python3 -m dvtest.tools.late_report --late out/<run>/web/load.json --threads threads.txt [--min-packets 20]
"""

import argparse
import json
from collections import defaultdict


def load_bins(path, case="w8_200"):
    data = json.load(open(path))
    cases = data.get("cases", data)
    return {int(k): v for k, v in (cases[case].get("late_bins") or {}).items()}


def clusters(bins, min_packets, gap_ms=50):
    """Neighbouring bins within gap_ms joined: [(start ms, end ms, packets, max dev ms)] of those with >= min_packets."""
    out = []
    cur = None
    for key in sorted(bins):
        n, dev = bins[key]
        if cur is not None and key - cur[1] <= gap_ms:
            cur[1] = key
            cur[2] += n
            cur[3] = max(cur[3], dev)
        else:
            if cur is not None:
                out.append(cur)
            cur = [key, key, n, dev]
    if cur is not None:
        out.append(cur)
    return [tuple(c) for c in out if c[2] >= min_packets]


def intervals(items):
    starts = [c[0] for c in items]
    return [b - a for a, b in zip(starts, starts[1:])]


def thread_busy(path):
    """{tid: (comm, [(epoch ms, ticks)])} from a thread_sample file."""
    series = defaultdict(list)
    names = {}
    for line in open(path):
        parts = line.split()
        if len(parts) < 6:
            continue
        ms, tid, comm, ut, st = int(parts[0]), int(parts[1]), parts[2], int(parts[3]), int(parts[4])
        names[tid] = comm
        series[tid].append((ms, ut + st))
    return {tid: (names[tid], pts) for tid, pts in series.items()}


def busy_around(series, start_ms, end_ms, margin_ms=100):
    """Ticks used by each thread between start-margin and end+margin (10 ms per tick)."""
    out = {}
    for tid, (comm, pts) in series.items():
        inside = [t for ms, t in pts if start_ms - margin_ms <= ms <= end_ms + margin_ms]
        if len(inside) >= 2:
            out[tid] = (comm, inside[-1] - inside[0])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--late", required=True)
    ap.add_argument("--threads")
    ap.add_argument("--min-packets", type=int, default=20)
    args = ap.parse_args(argv)
    bins = load_bins(args.late)
    items = clusters(bins, args.min_packets)
    total = sum(n for n, _ in bins.values())
    print("late packets %d in %d bins, %d clusters with >= %d packets" % (total, len(bins), len(items), args.min_packets))
    gaps = intervals(items)
    if gaps:
        gaps_sorted = sorted(gaps)
        print("cluster spacing ms: min %d median %d max %d (n=%d)" % (gaps_sorted[0], gaps_sorted[len(gaps) // 2],
                                                                     gaps_sorted[-1], len(gaps)))
        near = sum(1 for g in gaps if abs(g - 1000) <= 60)
        print("spacings within 60 ms of 1000 ms: %d of %d" % (near, len(gaps)))
    for start, end, n, dev in items[:40]:
        line = "t=%d len=%d ms late=%d maxdev=%.1f" % (start, end - start + 10, n, dev)
        if args.threads:
            busy = busy_around(thread_busy(args.threads), start, end)
            top = sorted(busy.items(), key=lambda kv: -kv[1][1])[:3]
            line += " busy " + ", ".join("%s/%d=%d" % (c, t, ticks) for t, (c, ticks) in top)
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
