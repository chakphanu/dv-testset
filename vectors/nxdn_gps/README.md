<!-- SPDX-License-Identifier: MIT -->
# vectors/nxdn_gps - NXDNGateway output per table row (DP14b)

`gateway-b4b041d.jsonl`: one JSON object per line (schema
`dvt-nxdn-gps-gateway/1`), 87 cases, written by

    python3 -m dvtest.oracle_nxdn gateway --out vectors/nxdn_gps/gateway-b4b041d.jsonl

from the image `dvt-nxdngw:local` (`rig/Dockerfile.nxdngw`, NXDNClients
`b4b041d`). Each record holds the encoded user data (hex), the exact line the
gateway printed (`gateway_lines`), the reduced values, the prediction and the
per-field comparison to the encoded input. Case names are
`LAYOUT.ROW.KIND`. Text only; the dsd-fme side is empty (no output yet).
Results and reading: `docs/nxdn-gps-cases.md`.
