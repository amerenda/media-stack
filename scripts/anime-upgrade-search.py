#!/usr/bin/env python3
"""Search for replacements of anime episodes whose file scores below the profile minimum.

Runs nightly from anime-upgrade-cron. Looks at monitored series on the anime
quality profile, finds monitored episodes whose file custom-format score is below
the profile's minimum score, and queues one Sonarr EpisodeSearch for the worst
MAX_EPISODES of them. Episodes already in the download queue are skipped.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989/sonarr").rstrip("/")
API_KEY = os.environ["SONARR_API_KEY"]
PROFILE = os.environ.get("ANIME_PROFILE", "[Anime] Remux-1080p")
MAX_EPISODES = int(os.environ.get("MAX_EPISODES", "10"))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} anime-upgrade-search: {msg}", flush=True)


def api(path, body=None):
    req = urllib.request.Request(
        f"{SONARR_URL}/api/v3/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main():
    profile = next((p for p in api("qualityprofile") if p["name"] == PROFILE), None)
    if profile is None:
        log(f"profile {PROFILE!r} not found; nothing to do")
        return 0
    min_score = profile.get("minFormatScore", 0)

    queued = set()
    page = 1
    while True:
        q = api(f"queue?page={page}&pageSize=200")
        queued.update(r["episodeId"] for r in q["records"] if r.get("episodeId"))
        if page * 200 >= q["totalRecords"]:
            break
        page += 1

    candidates = []
    for s in api("series"):
        if s["qualityProfileId"] != profile["id"] or not s["monitored"]:
            continue
        files = {f["id"]: f for f in api(f"episodefile?seriesId={s['id']}")}
        for e in api(f"episode?seriesId={s['id']}"):
            f = files.get(e.get("episodeFileId"))
            if not f or not e["monitored"] or e["id"] in queued:
                continue
            score = f.get("customFormatScore", 0)
            if score < min_score:
                candidates.append((score, s["title"], e["seasonNumber"], e["episodeNumber"], e["id"]))

    candidates.sort()
    picked = candidates[:MAX_EPISODES]
    log(f"{len(candidates)} episode(s) below min score {min_score}; searching {len(picked)}")
    for score, title, sn, en, _ in picked:
        log(f"  {title} S{sn:02d}E{en:02d} score {score}")
    if not picked:
        return 0
    if DRY_RUN:
        log("dry run: no search queued")
        return 0
    cmd = api("command", {"name": "EpisodeSearch", "episodeIds": [c[4] for c in picked]})
    log(f"queued EpisodeSearch command {cmd['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
