<!-- SPDX-License-Identifier: MIT -->
# asl-lab vectors

IAX2 frames of the AllStarLink test lab (`docs/asl-lab.md`), as hex per frame type, for the
parser tests of DV-Router's future `dv-edge-allstar`. One file per scenario and capturing host,
made from the pcaps of run `20261103T024811Z` (Asterisk `2:22.10.1+asl3-3.10.8-1.deb13`):

    python3 -m dvtest.iax2_frames out/<run>/asl-lab/<scenario>/<host>.pcap \
        -o baselines/asl-lab/<scenario>-<host>.json --limit 2 --anon

`--limit 2` keeps two frames per type (and every ts16 wrap frame), `--anon` rewrites the `src` /
`dst` fields to 192.0.2.x. The `hex` of each frame is the datagram as captured: it still holds the
rig addresses (172.30.0.x) in `APPARENT_ADDR` (REGACK) and in the A records of the `dns` messages,
and Asterisk's own call-token strings. The `summary` counts are those of the whole capture.
The pcaps stay in `out/` (ignored). Node numbers 199901, 199902, 299901 are private test numbers,
the secret is the fixture `test-secret-not-real`.

| File | Frame types of interest |
|------|-------------------------|
| `link-dns-a.json` | NEW (empty and real call token), CALLTOKEN, ACCEPT, ANSWER, STOP_SOUND, ACK, TEXT (`!NEWKEY1!`, `T`, `L`, `!!DISCONNECT!!`), voice full + mini, RADIO_UNKEY, CNG, HANGUP, DNS SRV / A |
| `account-md5-a.json` | AUTHREQ (md5), AUTHREP (MD5_RESULT), NEW / CALLTOKEN for `acct` and `acct-ct` |
| `callee-e.json`, `callee-b.json` | mode C (the edge originates), `L T199901` |
| `loop-e.json` | a loop through three nodes |
| `legacy-key-e.json` | RADIO_KEY / RADIO_UNKEY both ways, `!NEWKEY!`, PING / PONG, LAGRQ / LAGRP, a call without call token |
| `minimal-new-e.json` | the smallest NEW (8 IEs + CALLTOKEN) from a node address, `!NEWKEY1!`, voice without RADIO_KEY, the wrong-address refusal (ACCEPT then HANGUP) |
| `registration-reg.json` | REGREQ (three per attempt), REGAUTH, REGACK, REGREJ |
| `long-call-a.json` | mini frame ts16 wrap (`ts16_wraps`), natural PING / PONG / LAGRQ / LAGRP |
| `dns-zone-reg.json` | the zone: SRV, A, TXT, NXDOMAIN |
