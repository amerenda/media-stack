#!/usr/bin/env python3
"""
audio-defaults.py — Set default audio/subtitle MKV track flags based on original language.

Queries Radarr/Sonarr for each file's original language, then uses mkvpropedit to:
  - Set flag-default=1 on the audio track matching the original language
  - Set flag-default=1 on the first English subtitle when original lang != English
  - Clear flag-default from all other audio/subtitle tracks

Uses docker exec jellyfin for ffprobe (jellyfin-ffmpeg is in the container).
mkvpropedit runs on the host at /usr/bin/mkvpropedit.

Usage:
  ./audio-defaults.py                   # process entire library (movies + TV)
  ./audio-defaults.py --dry-run         # show what would change, no writes
  ./audio-defaults.py /path/to/file.mkv # process one file

Radarr/Sonarr post-import hook (configure as Custom Script connection):
  Set the script path, eventtype filter fires automatically.
"""

import json
import subprocess
import sys
import os
import logging
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
RADARR_URL     = "http://localhost:7878/radarr"
RADARR_API_KEY = "00b16b7522014aaaa0e4212947409ab9"
SONARR_URL     = "http://localhost:8989"
SONARR_API_KEY = "b44c33badadc408f91d6f100b34187f5"

MOVIES_DIR = "/mnt/storage/movies"
TV_DIR     = "/mnt/storage/tv"

# Jellyfin container name for ffprobe access
JELLYFIN_CONTAINER = "jellyfin"
# Path mapping: host path prefix → container path prefix
HOST_TO_CONTAINER = {
    "/mnt/storage/movies": "/movies",
    "/mnt/storage/tv":     "/tv",
}

# Map Radarr/Sonarr language name → MKV ISO 639-2/B codes
# mkvpropedit reads BCP47 tags; files may use 2- or 3-letter codes
LANG_TO_CODES = {
    "English":    ["eng", "en"],
    "Japanese":   ["jpn", "ja"],
    "French":     ["fre", "fra", "fr"],
    "German":     ["ger", "deu", "de"],
    "Spanish":    ["spa", "es"],
    "Italian":    ["ita", "it"],
    "Korean":     ["kor", "ko"],
    "Chinese":    ["chi", "zho", "zh"],
    "Mandarin":   ["chi", "zho", "zh"],
    "Cantonese":  ["chi", "zho", "zh"],
    "Portuguese": ["por", "pt"],
    "Russian":    ["rus", "ru"],
    "Arabic":     ["ara", "ar"],
    "Hindi":      ["hin", "hi"],
    "Thai":       ["tha", "th"],
    "Dutch":      ["dut", "nld", "nl"],
    "Polish":     ["pol", "pl"],
    "Swedish":    ["swe", "sv"],
    "Danish":     ["dan", "da"],
    "Norwegian":  ["nor", "nob", "nno", "no"],
    "Finnish":    ["fin", "fi"],
    "Czech":      ["cze", "ces", "cs"],
    "Hungarian":  ["hun", "hu"],
    "Romanian":   ["rum", "ron", "ro"],
    "Turkish":    ["tur", "tr"],
    "Greek":      ["gre", "ell", "el"],
    "Hebrew":     ["heb", "he"],
    "Ukrainian":  ["ukr", "uk"],
    "Vietnamese": ["vie", "vi"],
    "Indonesian": ["ind", "id"],
    "Persian":    ["per", "fas", "fa"],
    "Malay":      ["may", "msa", "ms"],
    "Tamil":      ["tam", "ta"],
    "Telugu":     ["tel", "te"],
    "Catalan":    ["cat", "ca"],
}
ENGLISH_CODES = set(LANG_TO_CODES["English"])


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── API helpers ───────────────────────────────────────────────────────────────

def api_get(url: str) -> list | dict:
    result = subprocess.run(
        ["curl", "-sL", "--max-time", "30", url],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def build_radarr_map() -> dict[str, str]:
    """Returns {host_file_path: language_name}"""
    movies = api_get(f"{RADARR_URL}/api/v3/movie?apikey={RADARR_API_KEY}")
    out = {}
    for m in movies:
        mf = m.get("movieFile")
        if not mf:
            continue
        # Radarr reports container path (/movies/...); translate to host path
        container_path = mf.get("path", "")
        host_path = container_path.replace("/movies", MOVIES_DIR, 1)
        lang = m.get("originalLanguage", {}).get("name", "English")
        out[host_path] = lang
    log.info(f"Radarr: {len(out)} movie files indexed")
    return out


def build_sonarr_map() -> dict[str, str]:
    """Returns {host_file_path: language_name}"""
    series_list = api_get(f"{SONARR_URL}/api/v3/series?apikey={SONARR_API_KEY}")
    out = {}
    for series in series_list:
        sid = series["id"]
        lang = series.get("originalLanguage", {}).get("name", "English")
        ep_files = api_get(
            f"{SONARR_URL}/api/v3/episodefile?seriesId={sid}&apikey={SONARR_API_KEY}"
        )
        for ep in ep_files:
            # Sonarr gives the relative path; prepend series path
            rel = ep.get("relativePath", "")
            series_path = series.get("path", "").replace("/tv", TV_DIR, 1)
            host_path = str(Path(series_path) / rel)
            out[host_path] = lang
    log.info(f"Sonarr: {len(out)} episode files indexed")
    return out


# ── Track inspection ──────────────────────────────────────────────────────────

def host_to_container_path(host_path: str) -> str:
    for hprefix, cprefix in HOST_TO_CONTAINER.items():
        if host_path.startswith(hprefix):
            return cprefix + host_path[len(hprefix):]
    return host_path


def get_tracks(host_path: str) -> list[dict]:
    """
    Returns list of stream dicts with keys:
      index, codec_type, lang, default, forced, audio_n, sub_n
    audio_n / sub_n are 1-based track numbers within their type (for mkvpropedit).
    """
    container_path = host_to_container_path(host_path)
    result = subprocess.run(
        [
            "docker", "exec", JELLYFIN_CONTAINER,
            "/usr/lib/jellyfin-ffmpeg/ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            container_path,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout)
    tracks = []
    audio_n = sub_n = 0
    for s in data.get("streams", []):
        ct = s.get("codec_type")
        if ct not in ("audio", "subtitle"):
            continue
        tags = s.get("tags", {})
        lang = tags.get("language") or tags.get("LANGUAGE") or "und"
        disp = s.get("disposition", {})
        entry = {
            "index":      s["index"],
            "codec_type": ct,
            "lang":       lang.lower(),
            "codec":      s.get("codec_name", ""),
            "default":    bool(disp.get("default")),
            "forced":     bool(disp.get("forced")),
            "title":      tags.get("title") or tags.get("TITLE") or "",
            "audio_n":    None,
            "sub_n":      None,
        }
        if ct == "audio":
            audio_n += 1
            entry["audio_n"] = audio_n
        else:
            sub_n += 1
            entry["sub_n"] = sub_n
        tracks.append(entry)
    return tracks


# ── Decision logic ────────────────────────────────────────────────────────────

def pick_audio_track(tracks: list[dict], orig_lang: str) -> dict | None:
    """First audio track whose language matches original language codes."""
    codes = set(LANG_TO_CODES.get(orig_lang, [orig_lang.lower()[:3]]))
    audio = [t for t in tracks if t["codec_type"] == "audio"]
    for t in audio:
        if t["lang"] in codes or t["lang"][:2] in {c[:2] for c in codes}:
            return t
    # Fallback: first audio track if only one exists
    if len(audio) == 1:
        return audio[0]
    return None


def pick_subtitle_track(tracks: list[dict]) -> dict | None:
    """First English subtitle track (prefer non-forced, then forced, then any)."""
    subs = [t for t in tracks if t["codec_type"] == "subtitle"]
    # Prefer non-forced English subs
    for t in subs:
        if t["lang"] in ENGLISH_CODES and not t["forced"]:
            return t
    # Fall back to any English sub
    for t in subs:
        if t["lang"] in ENGLISH_CODES:
            return t
    return None


# ── mkvpropedit ──────────────────────────────────────────────────────────────

def apply_flags(host_path: str, tracks: list[dict],
                target_audio: dict | None, target_sub: dict | None,
                dry_run: bool) -> bool:
    """
    Build and run the mkvpropedit command.
    Returns True if any change was made (or would be made in dry-run).
    """
    args = ["mkvpropedit", host_path]
    changed = False

    audio_tracks = [t for t in tracks if t["codec_type"] == "audio"]
    sub_tracks   = [t for t in tracks if t["codec_type"] == "subtitle"]

    for t in audio_tracks:
        want_default = (target_audio is not None and t["audio_n"] == target_audio["audio_n"])
        if t["default"] != want_default:
            args += ["--edit", f"track:a{t['audio_n']}", "--set", f"flag-default={'1' if want_default else '0'}"]
            log.info(f"  audio a{t['audio_n']} lang={t['lang']} codec={t['codec']} default: {t['default']} → {want_default}")
            changed = True

    for t in sub_tracks:
        want_default = (target_sub is not None and t["sub_n"] == target_sub["sub_n"])
        if t["default"] != want_default:
            args += ["--edit", f"track:s{t['sub_n']}", "--set", f"flag-default={'1' if want_default else '0'}"]
            log.info(f"  sub   s{t['sub_n']} lang={t['lang']} default: {t['default']} → {want_default}")
            changed = True

    if not changed:
        return False

    if dry_run:
        log.info(f"  [DRY RUN] would run: {' '.join(args)}")
        return True

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"  mkvpropedit failed: {result.stderr.strip()}")
        return False
    return True


# ── Per-file processing ───────────────────────────────────────────────────────

def process_file(host_path: str, orig_lang: str, dry_run: bool) -> str:
    """Returns 'changed', 'skipped', or 'error'."""
    if not host_path.lower().endswith(".mkv"):
        return "skipped"

    log.info(f"{'[DRY] ' if dry_run else ''}Processing [{orig_lang}]: {host_path}")

    try:
        tracks = get_tracks(host_path)
    except Exception as e:
        log.error(f"  Failed to read tracks: {e}")
        return "error"

    if not tracks:
        log.warning(f"  No audio/subtitle tracks found, skipping")
        return "skipped"

    target_audio = pick_audio_track(tracks, orig_lang)
    target_sub   = None

    if orig_lang != "English" and target_audio is not None:
        target_sub = pick_subtitle_track(tracks)
        if target_sub is None:
            log.warning(f"  No English subtitle track found (Bazarr should handle this)")

    if target_audio is None:
        log.warning(f"  No {orig_lang} audio track found — skipping")
        return "skipped"

    changed = apply_flags(host_path, tracks, target_audio, target_sub, dry_run)
    return "changed" if changed else "skipped"


# ── Library scan ─────────────────────────────────────────────────────────────

def scan_library(path_lang_map: dict[str, str], dry_run: bool):
    stats = {"changed": 0, "skipped": 0, "error": 0, "unknown": 0}

    for host_path, lang in sorted(path_lang_map.items()):
        if not Path(host_path).exists():
            continue
        result = process_file(host_path, lang, dry_run)
        if result in stats:
            stats[result] += 1
        else:
            stats["unknown"] += 1

    log.info(
        f"\nDone — changed: {stats['changed']}, "
        f"already-correct: {stats['skipped']}, "
        f"errors: {stats['error']}"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    # Called as a single-file processor (or Radarr/Sonarr hook via env vars)
    radarr_path = os.environ.get("radarr_moviefile_path")
    sonarr_path = os.environ.get("sonarr_episodefile_path")
    event_type  = os.environ.get("radarr_eventtype") or os.environ.get("sonarr_eventtype", "")

    if event_type == "Test":
        log.info("Test event received — OK")
        return

    single_file = None
    orig_lang   = None

    if radarr_path:
        # Radarr post-import hook: look up this movie in Radarr
        host_path = radarr_path.replace("/movies", MOVIES_DIR, 1)
        movie_id  = os.environ.get("radarr_movie_id")
        movies    = api_get(f"{RADARR_URL}/api/v3/movie/{movie_id}?apikey={RADARR_API_KEY}")
        orig_lang = movies.get("originalLanguage", {}).get("name", "English")
        single_file = host_path

    elif sonarr_path:
        # Sonarr post-import hook
        host_path  = sonarr_path.replace("/tv", TV_DIR, 1)
        series_id  = os.environ.get("sonarr_series_id")
        series     = api_get(f"{SONARR_URL}/api/v3/series/{series_id}?apikey={SONARR_API_KEY}")
        orig_lang  = series.get("originalLanguage", {}).get("name", "English")
        single_file = host_path

    elif args:
        single_file = args[0]
        # Try to find it in Radarr or Sonarr maps
        radarr_map = build_radarr_map()
        sonarr_map = build_sonarr_map()
        full_map   = {**radarr_map, **sonarr_map}
        orig_lang  = full_map.get(single_file)
        if orig_lang is None:
            log.error(f"File not found in Radarr or Sonarr: {single_file}")
            sys.exit(1)

    if single_file:
        process_file(single_file, orig_lang, dry_run)
        return

    # Full library scan
    log.info("Building Radarr + Sonarr file index...")
    radarr_map = build_radarr_map()
    sonarr_map = build_sonarr_map()
    full_map   = {**radarr_map, **sonarr_map}
    log.info(f"Total: {len(full_map)} files to process")

    if dry_run:
        log.info("DRY RUN — no files will be modified\n")

    scan_library(full_map, dry_run)


if __name__ == "__main__":
    main()
