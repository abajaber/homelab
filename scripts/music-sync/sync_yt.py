#!/usr/bin/env python3
"""yt-dlp full-playlist downloader, runs inside the music-sync container.

For each /sources/yt/*.url file (one YT Music playlist URL per file):
  - download anything new with yt-dlp (--download-archive skips existing)
  - regenerate /music/playlists/_m3u/<name>.m3u8 from the .info.json sidecars
    yt-dlp wrote, ordered by playlist_index.

The 256 kbps M4A path requires /config/cookies.txt — a logged-in YT Music
Premium session export. Without it yt-dlp falls back to 128 kbps and never
warns; verify with ffprobe on the output.
"""
from __future__ import annotations

import glob
import json
import os
import pathlib
import subprocess
import sys

SRC = "/sources/yt"
OUT = "/music"
M3U = "/music/playlists/_m3u"
COOKIES = "/config/cookies.txt"


def build_m3u(name: str, playlist_id: str) -> int:
    """Walk every .info.json under /music, keep those tagged with our
    playlist_id, write an m3u8 ordered by playlist_index."""
    rows: list[tuple[int, str]] = []
    for ij in glob.glob(f"{OUT}/**/*.info.json", recursive=True):
        try:
            d = json.load(open(ij))
        except Exception:
            continue
        if d.get("playlist_id") != playlist_id:
            continue
        audio = ij[: -len(".info.json")] + ".m4a"
        if os.path.exists(audio):
            rows.append((d.get("playlist_index", 9999), audio))
    rows.sort()
    path = os.path.join(M3U, f"{name}.m3u8")
    with open(path, "w") as f:
        f.write("#EXTM3U\n")
        for _, audio in rows:
            f.write(os.path.relpath(audio, M3U) + "\n")
    return len(rows)


def main() -> int:
    os.makedirs(M3U, exist_ok=True)
    for url_file in sorted(glob.glob(f"{SRC}/*.url")):
        name = pathlib.Path(url_file).stem
        url = pathlib.Path(url_file).read_text().strip()
        if not url:
            continue
        print(f"[{name}] yt-dlp {url}", flush=True)
        subprocess.run(
            [
                "yt-dlp", url,
                "--cookies", COOKIES,
                "--extract-audio", "--audio-format", "m4a", "--audio-quality", "0",
                "--embed-thumbnail", "--embed-metadata",
                "--output",
                OUT
                + "/%(album_artist,artist)s/%(album,playlist_title)s/"
                + "%(track_number,playlist_index)02d - %(title)s.%(ext)s",
                "--download-archive", f"/config/archives/{name}.archive",
                "--no-overwrites", "--write-info-json", "--ignore-errors",
            ],
        )
        pid_proc = subprocess.run(
            [
                "yt-dlp", "--flat-playlist", "--print", "%(playlist_id)s",
                "--playlist-items", "1", "--cookies", COOKIES, url,
            ],
            capture_output=True, text=True,
        )
        playlist_id = (pid_proc.stdout.splitlines() or [""])[0].strip()
        if playlist_id:
            n = build_m3u(name, playlist_id)
            print(f"  wrote m3u8 with {n} entries", flush=True)
        else:
            print(f"  could not resolve playlist_id; skipping m3u8", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
