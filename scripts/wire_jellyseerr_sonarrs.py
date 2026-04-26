#!/usr/bin/env python3
"""Wire Jellyseerr to use two Sonarr servers: anime (existing) + TV (new).

The existing 'Sonarr' entry is updated in-place to be the anime default
(isAnime=true, profile 8 = Remux-1080p - Anime, root /data/media/anime).
A new 'Sonarr-TV' entry is created (or updated) as the regular default
(isAnime=false, profile from the UHD Bluray + WEB id passed in,
root /data/media/tv).

Idempotent: matches by entry name.
"""
from __future__ import annotations

import argparse
import socket
import sys
from typing import Any

import requests
import urllib3

TRUENAS_IP = "192.168.1.138"
HOSTS = {
    "jellyseerr.bajaber.ca",
    "sonarr.bajaber.ca",
    "sonarr-tv.bajaber.ca",
}
_orig = socket.getaddrinfo


def _ovr(host, *a, **kw):
    if host in HOSTS:
        return _orig(TRUENAS_IP, *a, **kw)
    return _orig(host, *a, **kw)


socket.getaddrinfo = _ovr
urllib3.disable_warnings()


def session(base: str, key: str) -> requests.Session:
    s = requests.Session()
    s.headers["X-Api-Key"] = key
    s.verify = False
    s.base = f"{base.rstrip('/')}/api/v1"  # type: ignore[attr-defined]
    return s


def get(s, path) -> Any:
    r = s.get(f"{s.base}{path}")
    r.raise_for_status()
    return r.json()


def request(s, method, path, body=None) -> Any:
    r = s.request(method, f"{s.base}{path}", json=body)
    if r.status_code >= 400:
        print(f"  {method} {path} failed [{r.status_code}]: {r.text[:400]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jellyseerr-url", default="https://jellyseerr.bajaber.ca")
    ap.add_argument("--jellyseerr-key", required=True)
    ap.add_argument("--sonarr-anime-key", required=True)
    ap.add_argument("--sonarr-tv-key", required=True)
    ap.add_argument("--anime-profile-id", type=int, default=8,
                    help="Sonarr (anime) quality-profile id, default 8 = Remux-1080p - Anime")
    ap.add_argument("--anime-profile-name", default="Remux-1080p - Anime")
    ap.add_argument("--tv-profile-id", type=int, required=True,
                    help="Sonarr-TV quality-profile id (UHD Bluray + WEB)")
    ap.add_argument("--tv-profile-name", default="UHD Bluray + WEB")
    args = ap.parse_args()

    j = session(args.jellyseerr_url, args.jellyseerr_key)
    servers = get(j, "/settings/sonarr")

    # 1. Existing Sonarr → anime default.
    anime = next((srv for srv in servers if srv["name"].lower() == "sonarr"), None)
    if anime is None:
        raise SystemExit("no existing 'Sonarr' entry found in Jellyseerr")
    anime_id = anime["id"]
    body = {
        "name": "Sonarr",
        "hostname": "sonarr",
        "port": 8989,
        "useSsl": False,
        "apiKey": args.sonarr_anime_key,
        "baseUrl": "",
        "activeProfileId": args.anime_profile_id,
        "activeProfileName": args.anime_profile_name,
        "activeDirectory": "/data/media/anime",
        "is4k": False,
        "isDefault": True,
        "isAnime": True,
        "enableSeasonFolders": True,
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "tags": [],
        "externalUrl": "https://sonarr.bajaber.ca",
        # When isAnime=true these anime-specific overrides are unused but
        # Jellyseerr still expects the keys to be present.
        "animeProfileId": args.anime_profile_id,
        "animeRootFolder": "/data/media/anime",
        "animeTags": [],
        "animeLanguageProfileId": -2,
    }
    request(j, "PUT", f"/settings/sonarr/{anime_id}", body)
    print(f"  updated 'Sonarr' (id={anime_id}): isAnime=true, isDefault=true, /data/media/anime")

    # 2. Sonarr-TV → regular TV default.
    tv = next((srv for srv in servers if srv["name"].lower() == "sonarr-tv"), None)
    body = {
        "name": "Sonarr-TV",
        "hostname": "sonarr-tv",
        "port": 8989,
        "useSsl": False,
        "apiKey": args.sonarr_tv_key,
        "baseUrl": "",
        "activeProfileId": args.tv_profile_id,
        "activeProfileName": args.tv_profile_name,
        "activeDirectory": "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "isAnime": False,
        "enableSeasonFolders": True,
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "tags": [],
        "externalUrl": "https://sonarr-tv.bajaber.ca",
        "animeProfileId": args.tv_profile_id,
        "animeRootFolder": "/data/media/tv",
        "animeTags": [],
        "animeLanguageProfileId": -2,
    }
    if tv is None:
        created = request(j, "POST", "/settings/sonarr", body)
        print(f"  created 'Sonarr-TV' id={created['id']}")
    else:
        request(j, "PUT", f"/settings/sonarr/{tv['id']}", body)
        print(f"  updated 'Sonarr-TV' id={tv['id']}")

    print("\nDONE. TV requests now route to Sonarr-TV; anime requests stay on Sonarr.")


if __name__ == "__main__":
    main()
