# SPDX-License-Identifier: MIT
"""Resend the datagrams of a fuzz failure (docs/fuzz-cases.md, "Replay a failure").

  python3 -m dvtest.tools.fuzz_replay --file fuzz-replay-dextra.json --host 127.0.0.1 [--gap-ms 1]
  python3 -m dvtest.tools.fuzz_replay --file FILE --list            (only print the entries)
  python3 -m dvtest.tools.fuzz_replay --regen dextra --seed 1 --from 1180 --count 50 --host 127.0.0.1

--file reads the replay file a failed case wrote to its run directory (the last 50 datagrams, in
order, with protocol, seed, index, sub index, port, source kind and hex). --regen builds the same
datagrams again from (protocol, seed, index) with dvtest.fuzz, no file needed (only the datagrams of
the mutators, not the valid client traffic between them).

It sends to the host you name, never a default, and only to a loopback, private (RFC 1918),
link-local or documentation address unless --i-have-owner-approval is given. Normally run it
inside the same kind of network namespace as the case (a running DV-Router on 127.0.0.1), for
example from `unshare -rn` next to a started build; --link makes the "linked" entries come from a
live session of the fuzz client (dvtest.fuzz_run.make_client) of the entry's protocol, the others
from a socket that never linked.
"""
import argparse
import ipaddress
import sys

from dvtest import fuzz, fuzz_run


def allowed_host(host):
    """True for a loopback, private, link-local or documentation address (a literal IPv4 address)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return any(ip in ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


def regenerate(protocol, seed, start, count):
    """Replay entries for `count` datagrams from case `start` of (protocol, seed), as source kind
    fuzz_run.source_for says."""
    entries = []
    index = start
    while len(entries) < count:
        case = fuzz.generate(protocol, seed, index)
        for sub, data in enumerate(case.packets):
            entries.append(dict(protocol=protocol, seed=str(seed), index=index, sub=sub, port=case.port,
                                src=fuzz_run.source_for(index), hex=data.hex()))
        index += 1
    return entries[:count]


def describe(entries):
    lines = []
    for n, e in enumerate(entries):
        lines.append("%3d %s seed=%s index=%d.%d port=%d src=%s %d bytes %s" % (
            n, e["protocol"], e["seed"], e["index"], e["sub"], e["port"], e["src"], len(e["hex"]) // 2,
            e["hex"][:48] + ("..." if len(e["hex"]) > 48 else "")))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fuzz_replay", description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", help="replay file written by a failed fuzz case")
    ap.add_argument("--regen", metavar="PROTOCOL", help="rebuild datagrams from (protocol, seed, index)")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--from", dest="start", type=int, default=0, help="first case index for --regen")
    ap.add_argument("--count", type=int, default=50, help="datagrams for --regen")
    ap.add_argument("--host", help="destination address (required to send)")
    ap.add_argument("--bind", default="0.0.0.0", help="local address to send from")
    ap.add_argument("--gap-ms", type=float, default=1.0)
    ap.add_argument("--link", action="store_true", help="send the linked entries from a live client session")
    ap.add_argument("--module", default="B")
    ap.add_argument("--list", action="store_true", help="print the entries and send nothing")
    ap.add_argument("--i-have-owner-approval", action="store_true",
                    help="allow a destination that is not loopback / private / documentation")
    args = ap.parse_args(argv)
    if bool(args.file) == bool(args.regen):
        ap.error("give exactly one of --file and --regen")
    if args.file:
        with open(args.file) as handle:
            doc, entries = fuzz_run.read_replay(handle.read())
        print("replay file: scenario %s seed %s, %d entries, reason: %s" % (
            doc.get("scenario"), doc.get("seed"), len(entries), doc.get("reason") or "-"))
    else:
        if args.regen not in fuzz.PROTOCOLS:
            ap.error("unknown protocol %s (known: %s)" % (args.regen, ", ".join(fuzz.PROTOCOLS)))
        entries = regenerate(args.regen, args.seed, args.start, args.count)
    if args.list:
        print("\n".join(describe(entries)))
        return 0
    if not args.host:
        ap.error("--host is required to send (no default destination)")
    if not allowed_host(args.host) and not args.i_have_owner_approval:
        ap.error("%s is not a loopback / private / documentation address; --i-have-owner-approval if the owner "
                 "of that host agreed" % args.host)
    stranger = fuzz_run.Source(args.bind)
    linked, client = stranger, None
    if args.link:
        protocol = entries[0]["protocol"] if entries else ""
        kind = "m17" if protocol == "m17v3" else protocol
        if kind in fuzz_run.CLIENT_KINDS:
            client = fuzz_run.make_client(kind, args.bind, "fuzz", module=args.module)
            if fuzz_run.link(kind, client, args.host):
                linked = fuzz_run.ClientSource(client)
            else:
                print("the fuzz client could not link; the linked entries go out from the plain socket")
        else:
            print("no client session for %s; the linked entries go out from the plain socket" % kind)
    try:
        player = fuzz_run.Replayer(entries, args.host, linked, stranger, gap=args.gap_ms / 1000.0)
        player.batch(len(entries))
        print("sent %d datagrams to %s (%d refused by the socket)" % (player.sent, args.host, player.errors))
    finally:
        stranger.close()
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
