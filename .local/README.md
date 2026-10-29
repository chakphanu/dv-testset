# .local/

Machine-local settings and secrets. Everything here except this file is
gitignored and never committed.

| File        | Written by              | Content                                        |
|-------------|-------------------------|------------------------------------------------|
| `rig.env`   | `scripts/gen-local.sh`  | generated rig secrets (e.g. IAX), mode 600     |
| `pii-words` | you                     | extra words for `scripts/pii-check.sh`, one per line (personal callsigns, host names) |
