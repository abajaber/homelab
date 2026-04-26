#!/usr/bin/env python3
"""Split Prowlarr's Sonarr application into anime-only + TV-only entries.

Existing Sonarr application (anime instance, http://sonarr:8989) is updated
so it only pulls anime category 5070 — the regular TV syncCategories list is
cleared. A new application 'Sonarr-TV' is created (or updated) pointing at
http://sonarr-tv:8989 with the regular TV categories.

Idempotent: re-runnable; matches by application `name`.
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
    "prowlarr.bajaber.ca",
    "sonarr.bajaber.ca",
    "sonarr-tv.bajaber.ca",
    "radarr.bajaber.ca",
}
_orig = socket.getaddrinfo


def _ovr(host, *a, **kw):
    if host in HOSTS:
        return _orig(TRUENAS_IP, *a, **kw)
    return _orig(host, *a, **kw)


socket.getaddrinfo = _ovr
urllib3.disable_warnings()

REGULAR_TV_CATEGORIES = [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090]
ANIME_CATEGORIES = [5070]


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


def post(s, path, body) -> Any:
    sep = "&" if "?" in path else "?"
    r = s.post(f"{s.base}{path}{sep}forceSave=true", json=body)
    if r.status_code >= 400:
        print(f"  POST {path} failed [{r.status_code}]: {r.text[:400]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def put(s, path, body) -> Any:
    sep = "&" if "?" in path else "?"
    r = s.put(f"{s.base}{path}{sep}forceSave=true", json=body)
    if r.status_code >= 400:
        print(f"  PUT {path} failed [{r.status_code}]: {r.text[:400]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def set_field(fields: list[dict], name: str, value: Any) -> None:
    for f in fields:
        if f["name"] == name:
            f["value"] = value
            return
    fields.append({"name": name, "value": value})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prowlarr-url", default="https://prowlarr.bajaber.ca")
    ap.add_argument("--prowlarr-key", required=True)
    ap.add_argument("--sonarr-tv-key", required=True)
    args = ap.parse_args()

    p = session(args.prowlarr_url, args.prowlarr_key)
    apps = get(p, "/applications")

    # 1. Anime Sonarr: clear TV categories, keep anime only.
    anime = next((a for a in apps if a["name"].lower() == "sonarr"), None)
    if anime is None:
        print("WARN: no existing 'Sonarr' application found in Prowlarr", file=sys.stderr)
    else:
        set_field(anime["fields"], "syncCategories", [])
        set_field(anime["fields"], "animeSyncCategories", ANIME_CATEGORIES)
        # Keep prowlarrUrl, baseUrl, apiKey untouched.
        put(p, f"/applications/{anime['id']}", anime)
        print(f"  updated 'Sonarr' (anime): syncCategories=[] animeSyncCategories={ANIME_CATEGORIES}")

    # 2. Sonarr-TV: regular TV categories, no anime.
    tv = next((a for a in apps if a["name"].lower() == "sonarr-tv"), None)
    schema_template = anime if anime else next(
        a for a in get(p, "/applications/schema") if a["implementation"] == "Sonarr"
    )
    body = dict(schema_template)
    body["name"] = "Sonarr-TV"
    body["syncLevel"] = "fullSync"
    body["fields"] = [dict(f) for f in schema_template["fields"]]
    set_field(body["fields"], "prowlarrUrl", "http://prowlarr:9696")
    set_field(body["fields"], "baseUrl", "http://sonarr-tv:8989")
    set_field(body["fields"], "apiKey", args.sonarr_tv_key)
    set_field(body["fields"], "syncCategories", REGULAR_TV_CATEGORIES)
    set_field(body["fields"], "animeSyncCategories", [])
    body["tags"] = []
    body.pop("id", None)

    if tv is None:
        created = post(p, "/applications", body)
        print(f"  created 'Sonarr-TV' id={created['id']} baseUrl=http://sonarr-tv:8989 syncCategories={REGULAR_TV_CATEGORIES}")
    else:
        body["id"] = tv["id"]
        put(p, f"/applications/{tv['id']}", body)
        print(f"  updated 'Sonarr-TV' id={tv['id']}")

    print("\nDONE. Trigger 'Sync App Indexers' in Prowlarr UI to populate Sonarr-TV indexers.")


if __name__ == "__main__":
    main()
