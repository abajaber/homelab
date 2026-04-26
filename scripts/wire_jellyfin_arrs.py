#!/usr/bin/env python3
"""Wire each *arr to Jellyfin's MediaBrowser notification.

Sonarr/Radarr's built-in 'Emby / Jellyfin' notifier (implementation
``MediaBrowser``) issues a path-scoped library refresh to Jellyfin on
import/upgrade/rename/delete. With this configured, the realtime watcher
in Jellyfin is no longer load-bearing — newly imported episodes/movies
appear immediately.

Idempotent: re-runnable; matches by notification ``name``.

Internal hostname is ``jellyfin:8096`` on the ``media-internal`` Docker
network (shared by jellyfin and the *arrs).

Path mapping: the *arrs see media at ``/data/media/...`` while Jellyfin
sees the same files at ``/media/...`` (Jellyfin only mounts the media
subtree, ro). ``mapFrom``/``mapTo`` translates so Jellyfin scans the right
folder instead of the entire library.
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


JELLYFIN_HOST = "jellyfin"
JELLYFIN_PORT = 8096
NOTIFICATION_NAME = "Jellyfin"

# Triggers per app type. Common: import + upgrade + rename + delete cleanup.
SONARR_TRIGGERS = {
    "onDownload": True,
    "onUpgrade": True,
    "onRename": True,
    "onSeriesDelete": True,
    "onEpisodeFileDelete": True,
    "onEpisodeFileDeleteForUpgrade": True,
}
RADARR_TRIGGERS = {
    "onDownload": True,
    "onUpgrade": True,
    "onRename": True,
    "onMovieDelete": True,
    "onMovieFileDelete": True,
    "onMovieFileDeleteForUpgrade": True,
}


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
    r = s.post(f"{s.base}{path}", json=body)
    if r.status_code >= 400:
        print(f"  POST {path} failed [{r.status_code}]: {r.text[:400]}", file=sys.stderr)
    r.raise_for_status()
    return r.json() if r.text else {}


def put(s, path, body) -> Any:
    r = s.put(f"{s.base}{path}", json=body)
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


def build_payload(schema: dict, jellyfin_api_key: str, triggers: dict[str, bool]) -> dict:
    body = dict(schema)
    body["name"] = NOTIFICATION_NAME
    body["fields"] = [dict(f) for f in schema["fields"]]
    set_field(body["fields"], "host", JELLYFIN_HOST)
    set_field(body["fields"], "port", JELLYFIN_PORT)
    set_field(body["fields"], "useSsl", False)
    set_field(body["fields"], "apiKey", jellyfin_api_key)
    set_field(body["fields"], "notify", False)        # Jellyfin doesn't support
    set_field(body["fields"], "updateLibrary", True)
    set_field(body["fields"], "mapFrom", "/data/media")
    set_field(body["fields"], "mapTo", "/media")
    body["tags"] = []
    body.pop("id", None)
    body.pop("message", None)
    for trig, val in triggers.items():
        body[trig] = val
    return body


def wire(name: str, base_url: str, app_key: str, jellyfin_api_key: str, triggers: dict[str, bool]) -> None:
    s = session(base_url, app_key)
    schema_list = get(s, "/notification/schema")
    schema = next((x for x in schema_list if x.get("implementation") == "MediaBrowser"), None)
    if schema is None:
        print(f"  {name}: no MediaBrowser schema; skipping", file=sys.stderr)
        return

    existing = get(s, "/notification")
    current = next(
        (n for n in existing
         if n.get("implementation") == "MediaBrowser" and n.get("name") == NOTIFICATION_NAME),
        None,
    )

    payload = build_payload(schema, jellyfin_api_key, triggers)
    if current is None:
        created = post(s, "/notification", payload)
        print(f"  {name}: created '{NOTIFICATION_NAME}' id={created.get('id')}")
    else:
        payload["id"] = current["id"]
        put(s, f"/notification/{current['id']}", payload)
        print(f"  {name}: updated '{NOTIFICATION_NAME}' id={current['id']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sonarr-anime-url", default="https://sonarr.bajaber.ca")
    ap.add_argument("--sonarr-anime-key", required=True)
    ap.add_argument("--sonarr-tv-url", default="https://sonarr-tv.bajaber.ca")
    ap.add_argument("--sonarr-tv-key", required=True)
    ap.add_argument("--radarr-url", default="https://radarr.bajaber.ca")
    ap.add_argument("--radarr-key", required=True)
    ap.add_argument("--jellyfin-api-key", required=True)
    args = ap.parse_args()

    wire("sonarr (anime)", args.sonarr_anime_url, args.sonarr_anime_key,
         args.jellyfin_api_key, SONARR_TRIGGERS)
    wire("sonarr-tv", args.sonarr_tv_url, args.sonarr_tv_key,
         args.jellyfin_api_key, SONARR_TRIGGERS)
    wire("radarr", args.radarr_url, args.radarr_key,
         args.jellyfin_api_key, RADARR_TRIGGERS)

    print("\nDONE. Test in each *arr UI: Settings -> Connect -> Jellyfin -> Test.")


if __name__ == "__main__":
    main()
