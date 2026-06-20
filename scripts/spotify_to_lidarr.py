#!/usr/bin/env python3
"""Spotify playlist -> Lidarr artist import, bypassing Lidarr's flaky search.

Lidarr's import list / search flow goes through https://api.lidarr.audio
which intermittently returns 500s and aborts the whole batch. This script
sidesteps that by:

  1. Reading playlist tracks straight from the Spotify Web API.
  2. Resolving each unique artist to a MusicBrainz ID via the canonical
     MusicBrainz API (musicbrainz.org) — not the flaky proxy.
  3. Adding each artist to Lidarr by MBID (POST /artist), with per-artist
     error isolation so one bad lookup doesn't kill the run.

Idempotent: artists already in Lidarr are detected by foreignArtistId and
skipped.

Reads Spotify access token + playlist IDs from Lidarr's existing
SpotifyPlaylist import list config (after the user has authenticated in the
Lidarr UI). Lidarr's API key comes from $LIDARR_API_KEY in the environment
(typical pattern: `set -a; eval "$(ansible-vault view
servers/truenas/apps/music-tools/.env)"; set +a`).
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from typing import Any

import requests
import urllib3

TRUENAS_IP = "192.168.1.138"
LOCAL_HOSTS = {"lidarr.bajaber.ca"}
_orig = socket.getaddrinfo


def _ovr(host, *a, **kw):
    if host in LOCAL_HOSTS:
        return _orig(TRUENAS_IP, *a, **kw)
    return _orig(host, *a, **kw)


socket.getaddrinfo = _ovr
urllib3.disable_warnings()

LIDARR_URL = "https://lidarr.bajaber.ca"
SPOTIFY_API = "https://api.spotify.com/v1"
MB_API = "https://musicbrainz.org/ws/2"
USER_AGENT = "homelab-spotify-to-lidarr/1.0 (rahman.bajaber@gmail.com)"


def fetch_playlist_tracks(spotify_token: str, playlist_id: str) -> list[dict]:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {spotify_token}"
    items: list[dict] = []
    url = f"{SPOTIFY_API}/playlists/{playlist_id}/tracks?limit=100"
    while url:
        r = s.get(url)
        if r.status_code == 401:
            sys.exit(
                "Spotify access token expired. In Lidarr UI: Settings -> "
                "Import Lists -> Spotify Playlists -> click Authenticate, "
                "then re-run."
            )
        r.raise_for_status()
        d = r.json()
        items += d.get("items", [])
        url = d.get("next")
    return items


def unique_artists(tracks: list[dict]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for t in tracks:
        track = t.get("track")
        if not track:
            continue
        for a in track.get("artists", []):
            sid, name = a.get("id"), a.get("name")
            if sid and name and sid not in seen:
                seen[sid] = name
    return list(seen.items())


def mb_lookup(name: str) -> str | None:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"] = "application/json"
    time.sleep(1.05)  # MB rate-limit: 1 req/sec
    r = s.get(f"{MB_API}/artist/", params={"query": name, "fmt": "json", "limit": 5})
    if r.status_code >= 500:
        return None
    r.raise_for_status()
    arts = r.json().get("artists", [])
    if not arts:
        return None
    top = arts[0]
    score = top.get("score", 0)
    matched = top.get("name", "").lower() == name.lower()
    if score >= 95 or (score >= 90 and matched):
        return top.get("id")
    return None


def lidarr_lookup(s: requests.Session, mbid: str) -> dict | None:
    for attempt in range(3):
        r = s.get(f"{LIDARR_URL}/api/v1/artist/lookup", params={"term": f"lidarr:{mbid}"})
        if r.ok and r.json():
            return r.json()[0]
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        return None
    return None


def lidarr_add(
    s: requests.Session,
    details: dict,
    root: str,
    qp_id: int,
    mp_id: int,
) -> tuple[int, str]:
    body = dict(details)
    body["rootFolderPath"] = root
    body["qualityProfileId"] = qp_id
    body["metadataProfileId"] = mp_id
    body["monitored"] = True
    body["tags"] = []
    body["addOptions"] = {
        "monitor": "all",
        "albumsToMonitor": [],
        "monitored": True,
        "searchForMissingAlbums": False,
    }
    body.pop("id", None)
    r = s.post(f"{LIDARR_URL}/api/v1/artist?forceSave=true", json=body)
    return r.status_code, r.text[:300]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lidarr-key", default=os.environ.get("LIDARR_API_KEY"))
    ap.add_argument("--root-folder", default="/data/media/music")
    ap.add_argument("--quality-profile-id", type=int, default=5,
                    help="default 5 = AAC 320 (Bluetooth)")
    ap.add_argument("--metadata-profile-id", type=int, default=1,
                    help="default 1 = Standard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap unique artists (testing); 0 = all")
    args = ap.parse_args()

    if not args.lidarr_key:
        sys.exit("--lidarr-key (or LIDARR_API_KEY env) is required")

    l = requests.Session()
    l.verify = False
    l.headers["X-Api-Key"] = args.lidarr_key

    imports = l.get(f"{LIDARR_URL}/api/v1/importlist").json()
    sp = next((il for il in imports if il["implementation"] == "SpotifyPlaylist"), None)
    if not sp:
        sys.exit("No SpotifyPlaylist import list configured in Lidarr.")

    fmap = {f["name"]: f.get("value") for f in sp["fields"]}
    access_token = fmap.get("accessToken")
    playlist_ids = fmap.get("playlistIds") or []
    expires = fmap.get("expires", "")
    if not access_token or not playlist_ids:
        sys.exit("Spotify accessToken / playlistIds missing in Lidarr config.")
    print(f"Spotify token expires: {expires}")
    print(f"Playlists: {playlist_ids}")

    existing = {a["foreignArtistId"]: a for a in l.get(f"{LIDARR_URL}/api/v1/artist").json()}
    print(f"existing artists in Lidarr: {len(existing)}\n")

    all_tracks: list[dict] = []
    for pid in playlist_ids:
        ts = fetch_playlist_tracks(access_token, pid)
        print(f"  playlist {pid}: {len(ts)} tracks")
        all_tracks += ts

    artists = unique_artists(all_tracks)
    if args.limit:
        artists = artists[: args.limit]
    print(f"\nunique artists across playlists: {len(artists)}\n")

    added = skipped = unresolved = errored = 0
    for i, (sid, name) in enumerate(artists, 1):
        prefix = f"[{i:>4}/{len(artists)}]"

        try:
            mbid = mb_lookup(name)
        except Exception as e:
            print(f"{prefix} MB lookup error: {name!r}: {e}")
            errored += 1
            continue
        if not mbid:
            print(f"{prefix} no MB match: {name!r}")
            unresolved += 1
            continue
        if mbid in existing:
            print(f"{prefix} skip-exists: {name} ({mbid})")
            skipped += 1
            continue
        if args.dry_run:
            print(f"{prefix} DRY: would add {name} ({mbid})")
            added += 1
            continue

        details = lidarr_lookup(l, mbid)
        if not details:
            print(f"{prefix} lidarr lookup failed (api.lidarr.audio 500?): {name} ({mbid})")
            errored += 1
            continue
        code, txt = lidarr_add(
            l, details, args.root_folder, args.quality_profile_id, args.metadata_profile_id,
        )
        if code >= 400:
            print(f"{prefix} lidarr POST {code}: {name}: {txt}")
            errored += 1
            continue
        print(f"{prefix} + {name} ({mbid})")
        added += 1
        existing[mbid] = {"foreignArtistId": mbid, "artistName": name}

    print(
        f"\nDONE  added={added}  skipped={skipped}  "
        f"unresolved={unresolved}  errored={errored}  total={len(artists)}"
    )


if __name__ == "__main__":
    main()
