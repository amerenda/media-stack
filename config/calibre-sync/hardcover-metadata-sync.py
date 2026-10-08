#!/usr/bin/env python3
"""
One-shot pass: find calibre library books with an ISBN identifier that
haven't been Hardcover-synced yet, fetch canonical metadata for each from
Hardcover (calibre's "Hardcover" Source plugin, restricted via
--allowed-plugin so no other source can override it), and write it back via
calibredb set_metadata.

Books without an ISBN identifier are skipped — this only handles the "one
source of truth" flow (LazyLibrarian sets the ISBN on import, this script
lets calibre resolve the rest from Hardcover). Books whose only identifier is
e.g. goodreads (no isbn) are a known gap, not silently worked around here.

Run in a loop by loop.sh in the calibre-metadata-sync sidecar service
(same calibre image, entrypoint overridden — no GUI/Xvfb needed since
calibredb/fetch-ebook-metadata are headless CLI tools).
"""
import json
import os
import subprocess
import sys
import tempfile

LIBRARY = "/books"
ALLOWED_PLUGIN = "Hardcover"
SYNCED_KEY = "hardcover_synced"
FETCH_TIMEOUT = 45


def calibredb(*args):
    return subprocess.run(
        ["calibredb", *args, "--with-library", LIBRARY],
        capture_output=True,
        text=True,
    )


def list_books():
    res = calibredb("list", "--fields", "identifiers", "--for-machine")
    if res.returncode != 0:
        print(f"calibredb list failed: {res.stderr.strip()}", file=sys.stderr)
        return []
    return json.loads(res.stdout)


def sync_one(book_id, isbn):
    fetch = subprocess.run(
        [
            "fetch-ebook-metadata",
            "--isbn", isbn,
            "--opf",
            "--allowed-plugin", ALLOWED_PLUGIN,
            "--timeout", str(FETCH_TIMEOUT),
        ],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0 or not fetch.stdout.strip():
        print(f"id={book_id} isbn={isbn}: no Hardcover match ({fetch.stderr.strip()[:200]})")
        return False

    with tempfile.NamedTemporaryFile(mode="w", suffix=".opf", delete=False) as tf:
        tf.write(fetch.stdout)
        opf_path = tf.name
    try:
        setres = calibredb("set_metadata", str(book_id), opf_path)
    finally:
        os.unlink(opf_path)

    if setres.returncode != 0:
        print(f"id={book_id} isbn={isbn}: set_metadata failed: {setres.stderr.strip()[:200]}")
        return False

    # Merge the synced marker into identifiers rather than overwrite —
    # --field identifiers:... replaces the whole identifiers field.
    cur = calibredb("list", "--search", f"id:{book_id}", "--fields", "identifiers", "--for-machine")
    ids = {}
    if cur.returncode == 0:
        try:
            ids = json.loads(cur.stdout)[0].get("identifiers") or {}
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
    ids[SYNCED_KEY] = "1"
    idstr = ",".join(f"{k}:{v}" for k, v in ids.items())
    calibredb("set_metadata", "--field", f"identifiers:{idstr}", str(book_id))
    print(f"id={book_id} isbn={isbn}: synced from Hardcover")
    return True


def main():
    books = list_books()
    candidates = [
        b for b in books
        if (b.get("identifiers") or {}).get("isbn")
        and SYNCED_KEY not in (b.get("identifiers") or {})
    ]
    print(f"{len(candidates)} book(s) pending Hardcover sync")
    for b in candidates:
        sync_one(b["id"], b["identifiers"]["isbn"])


if __name__ == "__main__":
    main()
