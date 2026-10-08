#!/bin/sh
# Permanent Sonarr + Radarr queue watcher. See media-stack
# CLAUDE.md "Sonarr/Radarr stuck-import fix" for the three failure
# classes this resolves (orphaned queue entries, malware-warning
# backstop, ID-mismatch ImportBlocked items) and why each is safe to
# automate. No notifications are sent — every action is logged only,
# per Alex 2026-09-17 ("I do not want to be alerted, I want this to
# work"). ID-mismatch retries are capped so a chronically-mismatched
# title doesn't get blocklisted forever.
set -eu

STATE_FILE="${STATE_FILE:-/state/retry-counts.txt}"
LOG_FILE="${LOG_FILE:-/state/actions.log}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
touch "$STATE_FILE" "$LOG_FILE"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

get_count() {
  awk -v k="$1" '$1==k{print $2}' "$STATE_FILE"
}

set_count() {
  tmp="${STATE_FILE}.tmp"
  awk -v k="$1" -v v="$2" '$1==k{print k" "v; f=1; next} {print} END{if(!f) print k" "v}' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

clear_count() {
  tmp="${STATE_FILE}.tmp"
  awk -v k="$1" '$1!=k' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

# Args: app base_url api_key id title output_path status_messages item_key
handle_item() {
  app="$1"; base_url="$2"; api_key="$3"; id="$4"; title="$5"
  output_path="$6"; status_messages="$7"; item_key="$8"

  # Class 1: orphaned queue entry — pointer to a folder that no longer
  # exists on disk. Stale pointer, not a decision about content, safe
  # to remove without blocklisting.
  if [ -n "$output_path" ] && [ ! -e "$output_path" ]; then
    log "${app}: removing orphaned queue item ${id} (${title}) - missing ${output_path}"
    curl -sf -X DELETE -H "X-Api-Key: ${api_key}" \
      "${base_url}/api/v3/queue/${id}?removeFromClient=true&blocklist=false" >/dev/null || true
    clear_count "$item_key"
    return
  fi

  # Class 2: malware backstop, in case something slips past SABnzbd's
  # unwanted_extensions blacklist (Phase 2, 2026-09-07).
  case "$status_messages" in
    *"Found executable file"*)
      log "${app}: removing+blocklisting malware-flagged item ${id} (${title})"
      curl -sf -X DELETE -H "X-Api-Key: ${api_key}" \
        "${base_url}/api/v3/queue/${id}?removeFromClient=true&blocklist=true" >/dev/null || true
      clear_count "$item_key"
      return
      ;;
  esac

  # Class 3: ImportBlocked due to series/movie ID mismatch (the
  # dominant class found live 2026-09-17 - see CompletedDownloadService.cs
  # upstream). Same action as the Queue's own "X" button, automated and
  # capped to avoid looping forever on a chronically-mismatched title.
  case "$status_messages" in
    *"matched to series by ID"*|*"matched to movie by ID"*)
      count=$(get_count "$item_key")
      count=${count:-0}
      if [ "$count" -ge "$MAX_ATTEMPTS" ]; then
        log "${app}: ${title} (${item_key}) hit ${MAX_ATTEMPTS}-attempt cap - leaving as-is"
        return
      fi
      new_count=$((count + 1))
      set_count "$item_key" "$new_count"
      log "${app}: blocklisting ID-mismatched item ${id} (${title}), attempt ${new_count}/${MAX_ATTEMPTS}"
      curl -sf -X DELETE -H "X-Api-Key: ${api_key}" \
        "${base_url}/api/v3/queue/${id}?removeFromClient=true&blocklist=true" >/dev/null || true
      ;;
  esac
}

check_sonarr() {
  base_url="http://sonarr:8989/sonarr"
  curl -sf -H "X-Api-Key: ${SONARR_API_KEY}" \
    "${base_url}/api/v3/queue?pageSize=250&includeUnknownItems=true" \
    | jq -c '.records[]' | while IFS= read -r r; do
        id=$(echo "$r" | jq -r '.id')
        title=$(echo "$r" | jq -r '.title')
        output_path=$(echo "$r" | jq -r '.outputPath // empty')
        status_messages=$(echo "$r" | jq -r '[.statusMessages[]?.messages[]?] | join(" ; ")')
        key="sonarr:$(echo "$r" | jq -r '(.seriesId|tostring) + "-" + ((.episodeId // .title) | tostring)')"
        handle_item "sonarr" "$base_url" "$SONARR_API_KEY" "$id" "$title" "$output_path" "$status_messages" "$key"
      done
}

check_radarr() {
  base_url="http://radarr:7878/radarr"
  curl -sf -H "X-Api-Key: ${RADARR_API_KEY}" \
    "${base_url}/api/v3/queue?pageSize=250&includeUnknownItems=true" \
    | jq -c '.records[]' | while IFS= read -r r; do
        id=$(echo "$r" | jq -r '.id')
        title=$(echo "$r" | jq -r '.title')
        output_path=$(echo "$r" | jq -r '.outputPath // empty')
        status_messages=$(echo "$r" | jq -r '[.statusMessages[]?.messages[]?] | join(" ; ")')
        key="radarr:$(echo "$r" | jq -r '.movieId')"
        handle_item "radarr" "$base_url" "$RADARR_API_KEY" "$id" "$title" "$output_path" "$status_messages" "$key"
      done
}

log "queue-watcher starting, interval=${WATCH_INTERVAL_SECONDS:-900}s max_attempts=${MAX_ATTEMPTS}"

while true; do
  check_sonarr || log "sonarr: queue check failed, will retry next cycle"
  check_radarr || log "radarr: queue check failed, will retry next cycle"
  sleep "${WATCH_INTERVAL_SECONDS:-900}"
done
