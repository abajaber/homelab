#!/usr/bin/env python3
"""Add a YT Music playlist URL or Spotify Exportify CSV to the music-sync
sources folder on TrueNAS, then trigger an immediate sync.

Usage:
  ./scripts/add_playlist.py 'https://music.youtube.com/playlist?list=...'
  ./scripts/add_playlist.py ~/Downloads/spotify-playlist.csv
  ./scripts/add_playlist.py <input> --name custom-name
  ./scripts/add_playlist.py <input> --no-trigger

Detection:
  http(s) URL containing youtube.com / music.youtube.com
      -> /mnt/redsea/apps/music-tools/music-sync/sources/yt/<name>.url
  path ending in .csv
      -> /mnt/redsea/apps/music-tools/music-sync/sources/spotify/<name>.csv

The default --name comes from the URL's `list=` param or the CSV's basename.
After upload, restarts the music-sync container so its sleep-loop wakes
immediately. Pass --no-trigger to leave the next run for the 6h schedule.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parent.parent
OT = REPO / "scripts" / "ot.py"

YT_DEST = "/mnt/redsea/apps/music-tools/music-sync/sources/yt"
SP_DEST = "/mnt/redsea/apps/music-tools/music-sync/sources/spotify"


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "playlist"


def ot_write(path: str, content: str) -> None:
    subprocess.run(
        [str(OT), "write", path],
        input=content, text=True, check=True,
    )


def ot_mkdir(path: str) -> None:
    subprocess.run(
        [str(OT), "exec", "--timeout", "10", "mkdir", "-p", path],
        check=False,
    )


def restart() -> None:
    subprocess.run(
        [str(OT), "exec", "--timeout", "60", "docker", "restart", "music-sync"],
        check=False,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input", help="YT Music URL or Spotify Exportify CSV path")
    p.add_argument("--name", help="filename stem on the box (default: derived)")
    p.add_argument("--no-trigger", action="store_true",
                   help="skip docker restart of music-sync")
    a = p.parse_args()

    if a.input.startswith(("http://", "https://")):
        if "youtube.com" not in a.input:
            sys.exit("only YouTube / YT Music URLs are supported as URL input")
        list_id = urllib.parse.parse_qs(
            urllib.parse.urlparse(a.input).query
        ).get("list", [""])[0]
        name = safe_name(a.name or list_id or "yt-playlist")
        ot_mkdir(YT_DEST)
        ot_write(f"{YT_DEST}/{name}.url", a.input.strip() + "\n")
        print(f"wrote {YT_DEST}/{name}.url")
    else:
        path = pathlib.Path(a.input).expanduser()
        if not path.is_file() or path.suffix.lower() != ".csv":
            sys.exit("input must be a URL or a .csv file path")
        name = safe_name(a.name or path.stem)
        ot_mkdir(SP_DEST)
        ot_write(f"{SP_DEST}/{name}.csv", path.read_text())
        print(f"wrote {SP_DEST}/{name}.csv")

    if not a.no_trigger:
        restart()
        print("restarted music-sync — first sync starts now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
