#!/usr/bin/env python3
"""Sonarr searches with a per-indexer API budget.

MODE=anime-upgrade: monitored episodes on ANIME_PROFILE whose file scores below the
  profile minimum, worst first.
MODE=missing: monitored missing episodes, most recently aired first.

Every search is one episode, because Sonarr cannot cancel a search once it has
started and an anime-type episode can cost dozens of queries per indexer.
Prowlarr history (Sonarr searches since the run started) gives the queries used per
indexer. An episode is only searched if the busiest indexer's usage plus the
costliest episode of the same series type seen so far (or INITIAL_ESTIMATE_*) fits
within BUDGET. Episodes searched within COOLDOWN_DAYS are skipped, so each night
moves on to the next ones (state in STATE_DIR).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

MODE = os.environ["MODE"]
SONARR = os.environ.get("SONARR_URL", "http://sonarr:8989/sonarr").rstrip("/")
PROWLARR = os.environ.get("PROWLARR_URL", "http://prowlarr:9696/prowlarr").rstrip("/")
SONARR_KEY = os.environ["SONARR_API_KEY"]
PROWLARR_KEY = os.environ["PROWLARR_API_KEY"]
BUDGET = int(os.environ.get("BUDGET", "300"))
INITIAL_ESTIMATE = {
    "anime": int(os.environ.get("INITIAL_ESTIMATE_ANIME", "60")),
    "standard": int(os.environ.get("INITIAL_ESTIMATE_STANDARD", "10")),
}
COOLDOWN_DAYS = float(os.environ.get("COOLDOWN_DAYS", "3"))
MAX_UNITS = int(os.environ.get("MAX_UNITS", "200"))
UNIT_TIMEOUT = int(os.environ.get("UNIT_TIMEOUT", "1800"))
PROFILE = os.environ.get("ANIME_PROFILE", "[Anime] Remux-1080p")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {MODE}: {msg}", flush=True)


def call(base, key, path, body=None):
    req = urllib.request.Request(
        f"{base}/api/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def sonarr(path, body=None):
    return call(SONARR, SONARR_KEY, f"v3/{path}", body)


def queries_since(start):
    """Sonarr search queries per Prowlarr indexer since start."""
    q = urllib.parse.urlencode({"date": start.strftime("%Y-%m-%dT%H:%M:%SZ")})
    hist = call(PROWLARR, PROWLARR_KEY, f"v1/history/since?{q}")
    return Counter(
        r["indexerId"] for r in hist
        if r.get("eventType") == "indexerQuery" and (r.get("data") or {}).get("source") == "Sonarr"
    )


def queued_episode_ids():
    ids, page = set(), 1
    while True:
        q = sonarr(f"queue?page={page}&pageSize=200")
        ids.update(r["episodeId"] for r in q["records"] if r.get("episodeId"))
        if page * 200 >= q["totalRecords"]:
            return ids
        page += 1


def kind(series):
    return "anime" if series.get("seriesType") == "anime" else "standard"


def anime_units(queued):
    """(episode id, label, series kind), worst score first."""
    profile = next((p for p in sonarr("qualityprofile") if p["name"] == PROFILE), None)
    if profile is None:
        log(f"profile {PROFILE!r} not found")
        return []
    min_score = profile.get("minFormatScore", 0)
    units = []
    for s in sonarr("series"):
        if s["qualityProfileId"] != profile["id"] or not s["monitored"]:
            continue
        files = {f["id"]: f for f in sonarr(f"episodefile?seriesId={s['id']}")}
        for e in sonarr(f"episode?seriesId={s['id']}"):
            f = files.get(e.get("episodeFileId"))
            if not f or not e["monitored"] or e["id"] in queued:
                continue
            score = f.get("customFormatScore", 0)
            if score < min_score:
                label = f"{s['title']} S{e['seasonNumber']:02d}E{e['episodeNumber']:02d} (score {score})"
                units.append((score, e["id"], label, kind(s)))
    units.sort(key=lambda u: u[0])
    return [u[1:] for u in units]


def missing_units(queued):
    """(episode id, label, series kind), most recently aired first."""
    units, page = [], 1
    while True:
        w = sonarr(f"wanted/missing?page={page}&pageSize=500&monitored=true&includeSeries=true")
        for e in w["records"]:
            if e["id"] in queued:
                continue
            s = e.get("series") or {}
            label = f"{s.get('title', e['seriesId'])} S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}"
            units.append((e.get("airDateUtc") or "", e["id"], label, kind(s)))
        if page * 500 >= w["totalRecords"]:
            break
        page += 1
    units.sort(key=lambda u: u[0], reverse=True)
    return [u[1:] for u in units]


def load_state():
    try:
        with open(os.path.join(STATE_DIR, f"{MODE}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{MODE}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(path + ".tmp", path)


def wait_for(cmd_id):
    deadline = time.time() + UNIT_TIMEOUT
    while time.time() < deadline:
        c = sonarr(f"command/{cmd_id}")
        if c["status"] in ("completed", "failed", "aborted", "cancelled", "orphaned"):
            return c["status"]
        time.sleep(5)
    return "timeout"


def main():
    start = datetime.now(timezone.utc)
    state = load_state()
    cutoff = (start - timedelta(days=COOLDOWN_DAYS)).isoformat()
    state = {k: v for k, v in state.items() if v > cutoff}
    queued = queued_episode_ids()
    units = anime_units(queued) if MODE == "anime-upgrade" else missing_units(queued)
    fresh = [u for u in units if str(u[0]) not in state]
    log(f"{len(units)} episode(s), {len(fresh)} outside cooldown; budget {BUDGET} queries per indexer")
    if DRY_RUN:
        for _, label, k in fresh[:20]:
            log(f"  would search {label} [{k}]")
        return 0

    max_cost = {}
    done = 0
    for ep_id, label, k in fresh[:MAX_UNITS]:
        before = queries_since(start)
        busiest = max(before.values(), default=0)
        estimate = max_cost.get(k, INITIAL_ESTIMATE[k])
        if busiest + estimate > BUDGET:
            log(f"stopping before {label}: busiest indexer at {busiest}, a {k} episode may cost {estimate}")
            break
        cmd = sonarr("command", {"name": "EpisodeSearch", "episodeIds": [ep_id]})
        status = wait_for(cmd["id"])
        after = queries_since(start)
        cost = max((after[i] - before.get(i, 0) for i in after), default=0)
        max_cost[k] = max(max_cost.get(k, 0), cost)
        done += 1
        state[str(ep_id)] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        log(f"searched {label} [{k}]: {status}, cost {cost} on busiest indexer")
    used = queries_since(start)
    log(f"done: {done} episode(s); queries per indexer {dict(sorted(used.items()))} (budget {BUDGET})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
