#!/bin/bash
# Entrypoint for the calibre-metadata-sync sidecar: runs the Hardcover
# ISBN-sync pass every 15 minutes. This container reuses the linuxserver
# calibre image but overrides the entrypoint to skip s6/Xvfb entirely —
# calibredb and fetch-ebook-metadata are headless CLI tools with no GUI
# dependency, confirmed via `which` against the same image.
set -euo pipefail

while true; do
  echo "[$(date -Is)] running hardcover-metadata-sync"
  python3 /sync-scripts/hardcover-metadata-sync.py || echo "[$(date -Is)] sync pass failed, will retry next interval"
  sleep 900
done
