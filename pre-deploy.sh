#!/usr/bin/env bash
# Hydrates .env from BWS using the UUID -> name map in secrets.yaml.
# Komodo runs this from the repo root, then runs `docker compose up`.
set -euo pipefail
: "${BWS_ACCESS_TOKEN:?BWS_ACCESS_TOKEN required}"

umask 077
out=$(mktemp .env.XXXXXX)
while IFS=': ' read -r id name; do
  [[ $id =~ ^[0-9a-f-]{36}$ ]] || continue
  value=$(bws secret get "$id" --access-token "$BWS_ACCESS_TOKEN" | jq -er .value | tr -d '[:space:]') \
    || { rm -f "$out"; echo "pre-deploy: failed to fetch $name ($id)" >&2; exit 1; }
  key=${name//-/_}
  printf '%s=%s\n' "${key^^}" "$value" >> "$out"
done < secrets.yaml
mv "$out" .env
