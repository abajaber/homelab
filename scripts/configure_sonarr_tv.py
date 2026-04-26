#!/usr/bin/env python3
"""Configure the Sonarr-TV instance per TRaSH-Guides for standard TV.

Scope:
  - tags: vpn, direct
  - root folder: /data/media/tv
  - download clients: qBittorrent-vpn (gluetun:8080) + qBittorrent-direct
    (qbittorrent-direct:8080), each tagged + tv category
  - naming: TRaSH standard episode format
  - media management: hardlinks + clean import defaults
  - quality profile "UHD Bluray + WEB": 4K-default, Remux > Bluray > WEB-DL
    via the built-in quality cutoff (Bluray-2160p Remux). 1080p kept as
    fallback for shows with no 4K release.

Out of scope:
  - TRaSH custom-format JSON imports + tier scoring — left for Recyclarr.
  - Quality definition size limits — left for Recyclarr (or default).

Idempotent: every write is preceded by a GET-list and dedup by name/path.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from typing import Any

import requests
import urllib3

# DNS override for *.bajaber.ca (workstation can't resolve subdomains, only
# truenas.bajaber.ca → 192.168.1.138)
TRUENAS_IP = "192.168.1.138"
HOSTS = {
    "sonarr.bajaber.ca",
    "sonarr-tv.bajaber.ca",
    "radarr.bajaber.ca",
    "prowlarr.bajaber.ca",
    "jellyseerr.bajaber.ca",
    "qbit-direct.bajaber.ca",
    "qbit-vpn.bajaber.ca",
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
    s.base = f"{base.rstrip('/')}/api/v3"  # type: ignore[attr-defined]
    return s


def get(s, path) -> Any:
    r = s.get(f"{s.base}{path}")
    r.raise_for_status()
    return r.json()


def post(s, path, body) -> Any:
    sep = "&" if "?" in path else "?"
    r = s.post(f"{s.base}{path}{sep}forceSave=true", json=body)
    if r.status_code >= 400:
        print(f"  POST {path} failed [{r.status_code}]: {r.text[:300]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def put(s, path, body) -> Any:
    sep = "&" if "?" in path else "?"
    r = s.put(f"{s.base}{path}{sep}forceSave=true", json=body)
    if r.status_code >= 400:
        print(f"  PUT {path} failed [{r.status_code}]: {r.text[:300]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def ensure_tag(s, label: str) -> int:
    for t in get(s, "/tag"):
        if t["label"] == label:
            return t["id"]
    return post(s, "/tag", {"label": label})["id"]


def ensure_root_folder(s, path: str) -> int:
    for r in get(s, "/rootfolder"):
        if r["path"].rstrip("/") == path.rstrip("/"):
            return r["id"]
    return post(s, "/rootfolder", {"path": path})["id"]


def field(name: str, value: Any) -> dict:
    return {"name": name, "value": value}


def qbit_body(*, name: str, host: str, port: int, password: str, tag_ids: list[int], priority: int) -> dict:
    return {
        "enable": True,
        "protocol": "torrent",
        "priority": priority,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "name": name,
        "fields": [
            field("host", host),
            field("port", port),
            field("useSsl", False),
            field("urlBase", ""),
            field("username", "admin"),
            field("password", password),
            field("tvCategory", "tv"),
            field("recentTvPriority", 0),
            field("olderTvPriority", 0),
            field("initialState", 0),
            field("sequentialOrder", False),
            field("firstAndLast", False),
            field("contentLayout", 0),
        ],
        "implementationName": "qBittorrent",
        "implementation": "QBittorrent",
        "configContract": "QBittorrentSettings",
        "tags": tag_ids,
    }


def upsert_download_client(s, body: dict) -> tuple[str, int]:
    for c in get(s, "/downloadclient"):
        if c["name"].lower() == body["name"].lower():
            body["id"] = c["id"]
            put(s, f"/downloadclient/{c['id']}", body)
            return ("updated", c["id"])
    created = post(s, "/downloadclient", body)
    return ("created", created["id"])


def configure_naming(s) -> None:
    cur = get(s, "/config/naming")
    cur.update({
        "renameEpisodes": True,
        "replaceIllegalCharacters": True,
        "colonReplacementFormat": 4,  # smart replace
        "multiEpisodeStyle": 5,        # range
        "standardEpisodeFormat": (
            "{Series TitleYear} - S{season:00}E{episode:00} - "
            "{Episode CleanTitle} [{Custom Formats }{Quality Full}]"
            "{[MediaInfo VideoDynamicRangeType]}"
            "[{MediaInfo VideoBitDepth}bit]{[MediaInfo VideoCodec]}"
            "[{Mediainfo AudioCodec} { Mediainfo AudioChannels}]"
            "{MediaInfo AudioLanguages}{-Release Group}"
        ),
        "dailyEpisodeFormat": (
            "{Series TitleYear} - {Air-Date} - "
            "{Episode CleanTitle} [{Custom Formats }{Quality Full}]"
            "{[MediaInfo VideoDynamicRangeType]}"
            "[{MediaInfo VideoBitDepth}bit]{[MediaInfo VideoCodec]}"
            "[{Mediainfo AudioCodec} { Mediainfo AudioChannels}]"
            "{MediaInfo AudioLanguages}{-Release Group}"
        ),
        "animeEpisodeFormat": (
            "{Series TitleYear} - S{season:00}E{episode:00} - {absolute:000} - "
            "{Episode CleanTitle} [{Custom Formats }{Quality Full}]"
            "{[MediaInfo VideoDynamicRangeType]}"
            "[{MediaInfo VideoBitDepth}bit]{[MediaInfo VideoCodec]}"
            "[{Mediainfo AudioCodec} { Mediainfo AudioChannels}]"
            "{MediaInfo AudioLanguages}{-Release Group}"
        ),
        "seriesFolderFormat": "{Series TitleYear} {tvdb-{TvdbId}}",
        "seasonFolderFormat": "Season {season:00}",
        "specialsFolderFormat": "Specials",
    })
    put(s, "/config/naming", cur)


def configure_media_management(s) -> None:
    cur = get(s, "/config/mediamanagement")
    cur.update({
        "autoUnmonitorPreviouslyDownloadedEpisodes": False,
        "recycleBin": "",
        "recycleBinCleanupDays": 7,
        "downloadPropersAndRepacks": "preferAndUpgrade",
        "createEmptySeriesFolders": False,
        "deleteEmptyFolders": True,
        "fileDate": "none",
        "rescanAfterRefresh": "always",
        "setPermissionsLinux": False,
        "chmodFolder": "755",
        "chownGroup": "",
        "episodeTitleRequired": "always",
        "skipFreeSpaceCheckWhenImporting": False,
        "minimumFreeSpaceWhenImporting": 100,
        "copyUsingHardlinks": True,
        "useScriptImport": False,
        "importExtraFiles": True,
        "extraFileExtensions": "srt,nfo",
        "enableMediaInfo": True,
    })
    put(s, "/config/mediamanagement", cur)


def upsert_quality_profile(s, name: str) -> int:
    """Build TRaSH 'UHD Bluray + WEB' profile from /qualityprofile/schema.

    Items enabled (lowest-to-highest within profile):
      - WEBDL-1080p, Bluray-1080p (group: 'HD-1080p Fallback')
      - WEBDL-2160p, WEBRip-2160p
      - Bluray-2160p
      - Bluray-2160p Remux
    Cutoff: Bluray-2160p Remux. allowUpgrade=true.

    Sonarr's built-in quality ranking already scores Remux > Bluray > WEB-DL
    within the same resolution tier, so 'Remux preferred' falls out of the
    quality cutoff alone — no custom-format scoring required.
    """
    for p in get(s, "/qualityprofile"):
        if p["name"].lower() == name.lower():
            return p["id"]

    schema = get(s, "/qualityprofile/schema")
    items = schema["items"]

    enabled_quality_names = {
        "WEBDL-1080p",
        "Bluray-1080p",
        "WEBDL-2160p",
        "WEBRip-2160p",
        "Bluray-2160p",
        "Bluray-2160p Remux",
    }

    new_items: list[dict] = []
    cutoff_quality_id: int | None = None

    def walk_quality(q: dict) -> None:
        nonlocal cutoff_quality_id
        if q["name"] == "Bluray-2160p Remux":
            cutoff_quality_id = q["id"]

    for it in items:
        if it.get("quality"):
            q = it["quality"]
            allowed = q["name"] in enabled_quality_names
            walk_quality(q)
            new_items.append({"quality": q, "items": [], "allowed": allowed})
        else:
            # schema group — keep its children, mark group disallowed since we
            # don't currently care about Sonarr's default grouping.
            grp = dict(it)
            grp["allowed"] = False
            child_items = []
            for child in grp.get("items", []):
                cq = child.get("quality") or child
                if isinstance(cq, dict) and "id" in cq and "name" in cq:
                    walk_quality(cq)
                    child_items.append({
                        "quality": cq,
                        "items": [],
                        "allowed": cq["name"] in enabled_quality_names,
                    })
            grp["items"] = child_items
            new_items.append(grp)

    if cutoff_quality_id is None:
        raise SystemExit("could not find 'Bluray-2160p Remux' in quality schema")

    body = {
        "name": name,
        "upgradeAllowed": True,
        "cutoff": cutoff_quality_id,
        "items": new_items,
        "minFormatScore": 0,
        "cutoffFormatScore": 0,
        "minUpgradeFormatScore": 1,
        "formatItems": [],
        "language": schema.get("language", {"id": 1, "name": "English"}),
    }
    created = post(s, "/qualityprofile", body)
    return created["id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    args = ap.parse_args()

    qbit_vpn_pw = os.environ.get("QBIT_VPN_PASSWORD", "Mane6556")
    qbit_dir_pw = os.environ.get("QBIT_DIRECT_PASSWORD", "Mane6556")

    s = session(args.url, args.key)

    print("== Tags ==")
    vpn = ensure_tag(s, "vpn")
    direct = ensure_tag(s, "direct")
    print(f"  vpn={vpn} direct={direct}")

    print("== Root folder ==")
    rf = ensure_root_folder(s, "/data/media/tv")
    print(f"  /data/media/tv id={rf}")

    print("== Quality profile ==")
    qp = upsert_quality_profile(s, "UHD Bluray + WEB")
    print(f"  UHD Bluray + WEB id={qp}")

    print("== Naming ==")
    configure_naming(s)
    print("  applied")

    print("== Media management ==")
    configure_media_management(s)
    print("  applied")

    print("== Download clients ==")
    for spec in [
        {"name": "qBittorrent-vpn",    "host": "gluetun",            "port": 8080, "password": qbit_vpn_pw, "tag_ids": [vpn],    "priority": 1},
        {"name": "qBittorrent-direct", "host": "qbittorrent-direct", "port": 8080, "password": qbit_dir_pw, "tag_ids": [direct], "priority": 2},
    ]:
        action, cid = upsert_download_client(s, qbit_body(**spec))
        print(f"  {spec['name']}: {action} id={cid}")

    # Default Series Type is set per-series at add time. There's no host-level
    # "default series type" in Sonarr's config/host. Sonarr-TV's UI default
    # "Standard" is already correct unless the user changes it in Add Series.

    print("\nDONE. Sonarr-TV configured.")
    print("Next: trigger Prowlarr 'Sync App Indexers' to populate indexers.")


if __name__ == "__main__":
    main()
