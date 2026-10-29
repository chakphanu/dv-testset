# .cache/

Downloaded sources and build inputs. Filled by `scripts/pre-fetch-*.sh`;
everything here except this file is gitignored and never committed.

| Path              | Filled by                  | Content                          |
|-------------------|----------------------------|----------------------------------|
| `xlxd/<name>/`    | `scripts/pre-fetch-xlxd.sh`   | xlxd source at a pinned ref   |
| `codec2/`         | `scripts/pre-fetch-codec2.sh` | codec2 source at a pinned ref |

Later phases add test vectors (`tv/`) and vendor libraries here the same way.
Safe to delete; re-run the pre-fetch scripts to restore.
