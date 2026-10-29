# SPDX-License-Identifier: MIT
# Sourced by scripts/*.sh (bash). Loads settings and offers fetch_verified.
#
# dvt_load_env: read .env.example, then .env, then .local/rig.env (when
# present) and export every KEY=value; later files win, and a variable that
# was already set in the calling environment wins over all files. The files
# are parsed, never executed, and no value is printed.
#
# fetch_verified URL SHA256 DEST: download URL to DEST and check its sha256.
# A DEST that already matches is kept; a DEST that does not match is never
# overwritten (the call fails).

DVT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

dvt_load_env() {
  local -A caller=()
  local name file line key value
  while IFS= read -r name; do caller["$name"]=1; done < <(compgen -e)
  for file in "$DVT_ROOT/.env.example" "$DVT_ROOT/.env" "$DVT_ROOT/.local/rig.env"; do
    [ -f "$file" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      [ -z "${caller[$key]:-}" ] || continue
      # one level of surrounding quotes, as compose env files allow
      if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi
      export "$key=$value"
    done < "$file"
  done
}

# dvt_env_files: the --env-file arguments for docker compose, same order
dvt_env_files() {
  local file
  DVT_ENV_FILES=()
  for file in .env.example .env .local/rig.env; do
    [ -f "$DVT_ROOT/$file" ] && DVT_ENV_FILES+=(--env-file "$DVT_ROOT/$file")
  done
  return 0
}

fetch_verified() {
  local url="$1" sha="$2" dest="$3" tmp
  if [ -z "$url" ] || [ -z "$sha" ] || [ -z "$dest" ]; then
    echo "fetch_verified: URL, SHA256 and DEST are required" >&2
    return 2
  fi
  if [ -e "$dest" ]; then
    if printf '%s  %s\n' "$sha" "$dest" | sha256sum -c --status; then
      echo "fetch_verified: $dest already verified"
      return 0
    fi
    echo "fetch_verified: $dest exists with another sha256; not overwritten" >&2
    return 1
  fi
  mkdir -p "$(dirname "$dest")"
  tmp="$(mktemp "$dest.part.XXXXXX")"
  if ! curl -fsSL --retry 3 -o "$tmp" "$url"; then
    rm -f -- "$tmp"
    echo "fetch_verified: download failed: $url" >&2
    return 1
  fi
  if ! printf '%s  %s\n' "$sha" "$tmp" | sha256sum -c --status; then
    rm -f -- "$tmp"
    echo "fetch_verified: sha256 mismatch for $url" >&2
    return 1
  fi
  mv -- "$tmp" "$dest"
  echo "fetch_verified: $dest OK"
}
