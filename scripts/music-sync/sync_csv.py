#!/usr/bin/env python3
"""Spotify Exportify CSV -> yt-dlp via YT Music search, runs inside container.

For each /sources/spotify/*.csv (Exportify export):
  - parse rows
  - for each row, ytmsearch5: "<Artist> - <Title>"; pick best (prefer
    "* - Topic" uploader, then duration closest to row's Track Duration (ms))
  - download chosen video as m4a with embedded metadata + thumbnail
  - write .m3u8 in CSV order

Caveat: when a track is already in the per-CSV --download-archive,
yt-dlp emits no after_move line, so the m3u8 misses that row. Acceptable on
first run; for re-runs after edits, delete the matching .archive file or
ship the per-source query->path cache (see plan "Out of scope").
"""
from __future__ import annotations

import csv
import glob
import json
import os
import pathlib
import subprocess
import sys

SRC = "/sources/spotify"
OUT = "/music"
M3U = "/music/playlists/_m3u"
COOKIES = "/config/cookies.txt"


def search_candidates(query: str) -> list[dict]:
    r = subprocess.run(
        [
            "yt-dlp",
            "--cookies", COOKIES,
            "--default-search", "ytmsearch5",
            "--flat-playlist", "--dump-json",
            query,
        ],
        capture_output=True, text=True,
    )
    out: list[dict] = []
    for line in r.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def pick(cands: list[dict], target_ms: int | None) -> dict | None:
    if not cands:
        return None

    def score(c: dict) -> tuple[int, int]:
        topic = 0 if (c.get("uploader") or "").endswith(" - Topic") else 1
        if target_ms and c.get("duration"):
            delta = abs(int(c["duration"]) * 1000 - target_ms)
        else:
            delta = 999_999
        return (topic, delta)

    return min(cands, key=score)


def download(video_id: str, archive: str) -> str | None:
    r = subprocess.run(
        [
            "yt-dlp", f"https://music.youtube.com/watch?v={video_id}",
            "--cookies", COOKIES,
            "--extract-audio", "--audio-format", "m4a", "--audio-quality", "0",
            "--embed-thumbnail", "--embed-metadata",
            "--output",
            OUT
            + "/%(album_artist,artist)s/%(album,playlist_title)s/"
            + "%(track_number,1)02d - %(title)s.%(ext)s",
            "--download-archive", archive, "--no-overwrites",
            "--print", "after_move:%(filepath)s",
        ],
        capture_output=True, text=True,
    )
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("/music/"):
            return line
    return None


def main() -> int:
    os.makedirs(M3U, exist_ok=True)
    os.makedirs("/config/logs", exist_ok=True)
    os.makedirs("/config/archives", exist_ok=True)

    for csv_file in sorted(glob.glob(f"{SRC}/*.csv")):
        name = pathlib.Path(csv_file).stem
        archive = f"/config/archives/{name}.archive"
        log_path = f"/config/logs/{name}.log"
        with open(log_path, "a") as log:
            print(f"[{name}] CSV", file=log, flush=True)
        files: list[str] = []

        with open(csv_file, newline="") as f, open(log_path, "a") as log:
            for row in csv.DictReader(f):
                artist = (row.get("Artist Name(s)") or row.get("Artist") or "")
                artist = artist.split(",")[0].strip()
                title = (row.get("Track Name") or row.get("Title") or "").strip()
                try:
                    dur_ms = int(row.get("Track Duration (ms)") or 0) or None
                except ValueError:
                    dur_ms = None
                if not (artist and title):
                    continue
                query = f"{artist} - {title}"
                cands = search_candidates(query)
                best = pick(cands, dur_ms)
                if not best:
                    print(f"  no match: {query}", file=log, flush=True)
                    continue
                path = download(best["id"], archive)
                if path:
                    files.append(path)
                    print(
                        f"  + {query} -> {best.get('uploader')} / {best['id']}",
                        file=log, flush=True,
                    )
                else:
                    print(
                        f"  ~ already-downloaded or failed: {query}",
                        file=log, flush=True,
                    )

        if files:
            out_path = os.path.join(M3U, f"{name}.m3u8")
            with open(out_path, "w") as f:
                f.write("#EXTM3U\n")
                for p in files:
                    f.write(os.path.relpath(p, M3U) + "\n")
            with open(log_path, "a") as log:
                print(
                    f"  wrote {out_path} with {len(files)} entries",
                    file=log, flush=True,
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
