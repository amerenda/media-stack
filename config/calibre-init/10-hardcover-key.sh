#!/bin/bash
# Seeds the Hardcover calibre plugin's API key from HARDCOVER_API_KEY into its
# JSONConfig prefs file. Runs as a linuxserver custom-cont-init.d script on
# every container start, before the calibre process starts — idempotent,
# always overwrites with the current env var value.
#
# The plugin (RobBrazier/calibre-plugins, "Hardcover" Source) stores prefs at
# ~/.config/calibre/metadata_sources/Hardcover.json, keyed by Source.name via
# calibre's JSONConfig('metadata_sources/Hardcover'). Confirmed via
# `calibre-debug -c "from calibre.ebooks.metadata.sources.base import Source;
# print(Source(None).prefs.file_path)"` against this same image/version.
set -euo pipefail

PREFS_DIR="/config/.config/calibre/metadata_sources"
PREFS_FILE="${PREFS_DIR}/Hardcover.json"

if [[ -z "${HARDCOVER_API_KEY:-}" ]]; then
  echo "[hardcover-key] HARDCOVER_API_KEY not set, skipping"
  exit 0
fi

mkdir -p "$PREFS_DIR"

python3 - "$PREFS_FILE" "$HARDCOVER_API_KEY" <<'EOF'
import json
import sys

path, api_key = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
data["api_key"] = api_key
with open(path, "w") as f:
    json.dump(data, f)
EOF

chown 1000:1000 "$PREFS_FILE"
echo "[hardcover-key] wrote api_key to ${PREFS_FILE}"
