#!/usr/bin/env python3
"""ListenBrainz playlist -> Lidarr+slskd per-track import.

Pipeline: read a public ListenBrainz playlist, resolve each track via the
canonical MusicBrainz API (avoiding Lidarr's flaky api.lidarr.audio search
endpoint), register the underlying artist + release-group in Lidarr (so
Lidarr can match files on import without the artist's whole discography
being monitored), then for each track run a slskd search and queue the
best-matching file. After all downloads are queued, optionally wait for
slskd's queue to drain and trigger Lidarr's DownloadedAlbumsScan so files
get hardlinked into /music/<Artist>/<Album>/.

Idempotent. Per-track failures are isolated; one bad track doesn't kill the
batch.

Env: LIDARR_API_KEY and SLSKD_API_KEY (typical: `set -a; eval "$(ansible-vault
view servers/truenas/apps/music-tools/.env)"; set +a`).
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time

import requests
import urllib3

TRUENAS_IP = "192.168.1.138"
LOCAL_HOSTS = {"lidarr.bajaber.ca", "slskd.bajaber.ca"}
_orig = socket.getaddrinfo


def _ovr(host, *a, **kw):
    if host in LOCAL_HOSTS:
        return _orig(TRUENAS_IP, *a, **kw)
    return _orig(host, *a, **kw)


socket.getaddrinfo = _ovr
urllib3.disable_warnings()

LIDARR_URL = "https://lidarr.bajaber.ca"
SLSKD_URL = "https://slskd.bajaber.ca"
LB_API = "https://api.listenbrainz.org/1"
MB_API = "https://musicbrainz.org/ws/2"
USER_AGENT = "homelab-lb-to-pipeline/1.0 (rahman.bajaber@gmail.com)"

# How much we want each audio container, larger = more preferred
EXT_RANK = {".flac": 100, ".alac": 90, ".m4a": 60, ".mp3": 50, ".ogg": 40, ".aac": 30, ".wav": 20}


# ============================== ListenBrainz ==============================

def fetch_lb_playlist(uuid_or_url: str) -> dict:
    m = re.search(r"([a-f0-9-]{36})", uuid_or_url)
    if not m:
        sys.exit(f"can't extract playlist UUID from {uuid_or_url!r}")
    r = requests.get(f"{LB_API}/playlist/{m.group(1)}")
    r.raise_for_status()
    return r.json()["playlist"]


# =============================== MusicBrainz ===============================

def mb_get(path: str, params: dict | None = None) -> dict:
    time.sleep(1.05)  # MB anonymous rate limit: 1 req/sec
    p = {"fmt": "json"}
    if params:
        p.update(params)
    r = requests.get(
        f"{MB_API}{path}",
        params=p,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def resolve_track(track: dict) -> dict | None:
    """LB JSPF track -> dict with mb metadata, or {'error': ...} on failure."""
    rec_id = track.get("identifier")
    if isinstance(rec_id, list):
        rec_id = rec_id[0] if rec_id else None
    rec_mbid = rec_id.rsplit("/", 1)[-1] if rec_id else None
    title = track.get("title")
    creator = track.get("creator")
    if not rec_mbid:
        return {"error": "no recording mbid", "title": title, "creator": creator}

    try:
        rec = mb_get(
            f"/recording/{rec_mbid}",
            {"inc": "releases+artist-credits+release-groups"},
        )
    except Exception as e:
        return {"error": f"MB recording lookup: {e}", "title": title, "creator": creator}

    artists = rec.get("artist-credit") or []
    if not artists:
        return {"error": "no artist-credit", "title": title, "creator": creator}
    artist = artists[0]["artist"]

    releases = rec.get("releases") or []
    if not releases:
        return {
            "error": "recording has no releases",
            "title": title,
            "creator": creator,
            "artist_mbid": artist["id"],
            "artist_name": artist["name"],
        }

    def rscore(r):
        rg = r.get("release-group", {}) or {}
        sec = set(rg.get("secondary-types", []) or [])
        s = 0
        if r.get("status") == "Official": s += 10
        if rg.get("primary-type") == "Album": s += 5
        if "Compilation" in sec: s -= 3
        if "Live" in sec: s -= 2
        if "Remix" in sec: s -= 2
        return -s  # ascending sort -> best first

    releases.sort(key=rscore)
    rel = releases[0]
    rg = rel.get("release-group", {}) or {}

    return {
        "title": title,
        "track_title": rec.get("title", title),
        "creator": creator,
        "recording_mbid": rec_mbid,
        "artist_mbid": artist["id"],
        "artist_name": artist["name"],
        "release_mbid": rel["id"],
        "release_title": rel.get("title"),
        "release_group_mbid": rg.get("id"),
    }


# ================================== Lidarr ==================================

def lidarr_session(key: str) -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers["X-Api-Key"] = key
    return s


def lidarr_ensure_artist(s, artist_mbid, artist_name, root, qp_id, mp_id, existing_index):
    """Add artist to Lidarr if not present; return artist record or None."""
    if artist_mbid in existing_index:
        return existing_index[artist_mbid]

    for attempt in range(3):
        r = s.get(f"{LIDARR_URL}/api/v1/artist/lookup", params={"term": f"lidarr:{artist_mbid}"})
        if r.ok and r.json():
            break
        time.sleep(2 * (attempt + 1))
    else:
        print(f"      ! lidarr lookup failed for {artist_name} ({artist_mbid})")
        return None

    body = dict(r.json()[0])
    body["rootFolderPath"] = root
    body["qualityProfileId"] = qp_id
    body["metadataProfileId"] = mp_id
    body["monitored"] = True
    body["addOptions"] = {
        "monitor": "none",
        "albumsToMonitor": [],
        "monitored": True,
        "searchForMissingAlbums": False,
    }
    body["tags"] = []
    body.pop("id", None)
    rr = s.post(f"{LIDARR_URL}/api/v1/artist?forceSave=true", json=body)
    if rr.status_code >= 400:
        print(f"      ! lidarr add artist [{rr.status_code}]: {rr.text[:200]}")
        return None
    a = rr.json()
    existing_index[artist_mbid] = a
    return a


def lidarr_album_known(s, artist_id, release_group_mbid):
    """Make sure an album record exists in Lidarr for this release group.

    Lidarr fetches the artist's discography on add, but slow networks /
    skyhook hiccups can make it lag. Trigger a refresh and poll briefly.
    """
    def find():
        albums = s.get(f"{LIDARR_URL}/api/v1/album", params={"artistId": artist_id}).json()
        return next((a for a in albums if a.get("foreignAlbumId") == release_group_mbid), None)

    if (alb := find()):
        return alb
    s.post(f"{LIDARR_URL}/api/v1/command", json={"name": "RefreshArtist", "artistId": artist_id})
    for _ in range(20):
        time.sleep(3)
        if (alb := find()):
            return alb
    return None


def lidarr_recover_rejected(s, scan_path, basename_to_meta, existing_artists):
    """For each file Lidarr rejected during DownloadedAlbumsScan, look up the
    file's resolved recording_mbid (we have it from Phase 1), find the matching
    track on the right album in Lidarr, and force-import with explicit ids.
    Returns count of files recovered.
    """
    items = s.get(
        f"{LIDARR_URL}/api/v1/manualimport",
        params={"folder": scan_path, "filterExistingFiles": "true"},
    ).json()
    rejected = [c for c in items if c.get("rejections")]
    if not rejected:
        return 0
    print(f"  {len(rejected)} rejected file(s) — attempting recovery via recording_mbid")

    recovered = 0
    for cand in rejected:
        path = cand["path"]
        base = path.replace("\\", "/").rsplit("/", 1)[-1]
        meta = basename_to_meta.get(base)
        if not meta:
            # fallback: substring match track_title in basename
            blow = base.lower()
            for k, m in basename_to_meta.items():
                if m["track_title"].lower() in blow and m["artist_name"].lower() in path.lower():
                    meta = m
                    break
        if not meta:
            print(f"    ! recover: no meta for {base!r}")
            continue

        artist = next((a for a in existing_artists if a["foreignArtistId"] == meta["artist_mbid"]), None)
        if not artist:
            print(f"    ! recover: artist {meta['artist_name']!r} not in Lidarr")
            continue

        tracks = s.get(f"{LIDARR_URL}/api/v1/track", params={"artistId": artist["id"]}).json()
        track = next((t for t in tracks if t.get("foreignRecordingId") == meta["recording_mbid"]), None)
        if not track:
            print(f"    ! recover: no Lidarr track with recording_mbid {meta['recording_mbid']}")
            continue

        album = s.get(f"{LIDARR_URL}/api/v1/album/{track['albumId']}").json()
        releases = album.get("releases") or []
        rel_id = next((r["id"] for r in releases if r.get("monitored")), None) or (releases[0]["id"] if releases else None)
        if not rel_id:
            print(f"    ! recover: album has no releases")
            continue

        body = [{
            "path": path,
            "artistId": artist["id"],
            "albumId": track["albumId"],
            "albumReleaseId": rel_id,
            "trackIds": [track["id"]],
            "quality": cand["quality"],
            "disableReleaseSwitching": False,
        }]
        rr = s.post(f"{LIDARR_URL}/api/v1/command", json={"name": "ManualImport", "files": body, "importMode": "auto"})
        if rr.status_code >= 400:
            print(f"    ! recover ManualImport [{rr.status_code}]: {rr.text[:200]}")
            continue
        cid = rr.json()["id"]
        for _ in range(15):
            time.sleep(2)
            c = s.get(f"{LIDARR_URL}/api/v1/command/{cid}").json()
            if c["status"] in ("completed", "failed"):
                msg = (c.get("message") or "").lower()
                if "imported" in msg and "0 files" not in msg:
                    recovered += 1
                    print(f"    + recovered: {meta['artist_name']} - {meta['track_title']} -> {album['title']}")
                else:
                    print(f"    - recover failed: {meta['artist_name']} - {meta['track_title']}: {c.get('message','')}")
                break
    return recovered


# ================================== slskd ==================================

def slskd_session(key: str) -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers["X-API-Key"] = key
    return s


def slskd_search(s, query: str, wait_seconds: int = 30) -> list:
    r = s.post(f"{SLSKD_URL}/api/v0/searches", json={"searchText": query, "fileLimit": 200})
    r.raise_for_status()
    sid = r.json()["id"]
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        meta = s.get(f"{SLSKD_URL}/api/v0/searches/{sid}").json()
        state = meta.get("state", "")
        if "Completed" in state:
            break
        if meta.get("fileCount", 0) >= 30:
            break
    rr = s.get(f"{SLSKD_URL}/api/v0/searches/{sid}/responses")
    return rr.json() if rr.ok else []


def pick_file(responses: list, target_title: str) -> tuple[str | None, dict | None]:
    """Pick the best (username, file) across responses. Returns (None, None) if nothing matched."""
    target = target_title.lower()
    candidates = []
    for resp in responses:
        username = resp.get("username")
        if not username:
            continue
        slot = resp.get("hasFreeUploadSlot", False)
        speed = resp.get("uploadSpeed", 0) or 0
        for f in resp.get("files", []):
            fn = f.get("filename", "")
            base = fn.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if target not in base:
                continue
            ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
            er = EXT_RANK.get(ext, 0)
            if not er:
                continue
            size = f.get("size", 0) or 0
            if size < 1_500_000 or size > 300_000_000:
                continue
            score = er + (10 if slot else 0) + min(20, speed // 100_000)
            candidates.append((score, username, f))
    if not candidates:
        return None, None
    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


def slskd_download(s, username: str, file_obj: dict) -> tuple[bool, str]:
    body = [{"filename": file_obj["filename"], "size": file_obj["size"]}]
    r = s.post(f"{SLSKD_URL}/api/v0/transfers/downloads/{username}", json=body)
    if r.status_code >= 400:
        return False, r.text[:200]
    return True, ""


def slskd_active_count(s) -> int:
    r = s.get(f"{SLSKD_URL}/api/v0/transfers/downloads")
    if not r.ok:
        return -1
    active = 0
    for u in r.json():
        for d in u.get("directories", []):
            for f in d.get("files", []):
                state = f.get("state", "") or ""
                if "Completed" not in state and "Cancelled" not in state and "Errored" not in state:
                    active += 1
    return active


# ==================================== main ====================================

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist", help="LB playlist UUID or URL")
    ap.add_argument("--lidarr-key", default=os.environ.get("LIDARR_API_KEY"))
    ap.add_argument("--slskd-key", default=os.environ.get("SLSKD_API_KEY"))
    ap.add_argument("--root-folder", default="/data/media/music")
    ap.add_argument("--qp", type=int, default=5, help="Lidarr quality profile id")
    ap.add_argument("--mp", type=int, default=1, help="Lidarr metadata profile id")
    ap.add_argument("--limit", type=int, default=0, help="cap tracks (testing)")
    ap.add_argument("--dry-run", action="store_true", help="resolve + register in Lidarr, do not download")
    ap.add_argument("--no-import", action="store_true", help="don't trigger DownloadedAlbumsScan at the end")
    ap.add_argument("--wait-minutes", type=int, default=10, help="how long to wait for slskd queue to drain")
    args = ap.parse_args()
    if not args.lidarr_key:
        sys.exit("LIDARR_API_KEY (env or --lidarr-key) required")
    if not args.slskd_key:
        sys.exit("SLSKD_API_KEY (env or --slskd-key) required")

    pl = fetch_lb_playlist(args.playlist)
    tracks = pl.get("track", []) or []
    print(f"Playlist: {pl.get('title')!r} by {pl.get('creator')!r}  tracks={len(tracks)}")
    if args.limit:
        tracks = tracks[: args.limit]
        print(f"  --limit {args.limit}: processing first {len(tracks)} tracks")

    L = lidarr_session(args.lidarr_key)
    K = slskd_session(args.slskd_key)

    # ---- Phase 1: MB resolve ----
    print("\n=== Phase 1: resolve via MusicBrainz ===")
    resolved = []
    for i, t in enumerate(tracks, 1):
        m = resolve_track(t)
        if not m or "error" in m:
            err = (m or {}).get("error", "unknown")
            print(f"  [{i:>3}] ✗ {t.get('creator')} - {t.get('title')}: {err}")
            continue
        print(f"  [{i:>3}] ✓ {m['artist_name']} - {m['track_title']}  ({m['release_title']!r})")
        resolved.append(m)
    print(f"  resolved {len(resolved)}/{len(tracks)}")

    # ---- Phase 2: register artists+albums in Lidarr ----
    print("\n=== Phase 2: register artists + albums in Lidarr ===")
    existing_index = {a["foreignArtistId"]: a for a in L.get(f"{LIDARR_URL}/api/v1/artist").json()}
    seen_artists = set()
    for m in resolved:
        if m["artist_mbid"] in seen_artists:
            continue
        seen_artists.add(m["artist_mbid"])
        a = lidarr_ensure_artist(L, m["artist_mbid"], m["artist_name"], args.root_folder, args.qp, args.mp, existing_index)
        if a:
            tag = "added" if m["artist_mbid"] not in {x["foreignArtistId"] for x in existing_index.values() if x is not a} else "exists"
            print(f"  artist {tag}: {m['artist_name']}")

    seen_albums = set()
    for m in resolved:
        if not m.get("release_group_mbid") or m["release_group_mbid"] in seen_albums:
            continue
        seen_albums.add(m["release_group_mbid"])
        a = existing_index.get(m["artist_mbid"])
        if not a:
            continue
        alb = lidarr_album_known(L, a["id"], m["release_group_mbid"])
        if alb:
            print(f"  album known: {m['artist_name']} - {m['release_title']}")
        else:
            print(f"  ! album not in Lidarr DB (skyhook lag): {m['release_title']}")

    if args.dry_run:
        print("\nDRY RUN — skipping slskd downloads. Re-run without --dry-run to fetch.")
        return

    # ---- Phase 3: per-track slskd searches + downloads ----
    print("\n=== Phase 3: slskd per-track searches + downloads ===")
    queued = misses = errors = 0
    basename_to_meta: dict[str, dict] = {}
    for i, m in enumerate(resolved, 1):
        q = f"{m['artist_name']} {m['track_title']}"
        try:
            resp = slskd_search(K, q, wait_seconds=15)
        except Exception as e:
            print(f"  [{i:>3}] ✗ search error {q!r}: {e}")
            errors += 1
            continue
        username, f = pick_file(resp, m["track_title"])
        if not f:
            print(f"  [{i:>3}] - no hit: {q!r}")
            misses += 1
            continue
        ok, err = slskd_download(K, username, f)
        if ok:
            queued += 1
            base = f["filename"].replace("\\", "/").rsplit("/", 1)[-1]
            basename_to_meta[base] = m
            print(f"  [{i:>3}] ↓ {username}: {base} ({f['size']//1_000_000}MB)")
        else:
            errors += 1
            print(f"  [{i:>3}] ✗ download {q!r}: {err}")

    print(f"\n  queued={queued}  no-hit={misses}  errors={errors}  (of {len(resolved)} resolved)")

    if queued == 0:
        print("\nNothing queued; skipping import trigger.")
        return

    if args.no_import:
        print("\n--no-import: skipping DownloadedAlbumsScan; run later when downloads complete.")
        return

    # ---- Phase 4: wait for slskd, trigger Lidarr import ----
    print("\n=== Phase 4: wait for slskd queue + trigger Lidarr import ===")
    deadline = time.time() + args.wait_minutes * 60
    last_active = -1
    while time.time() < deadline:
        time.sleep(20)
        n = slskd_active_count(K)
        if n != last_active:
            print(f"  active transfers: {n}")
            last_active = n
        if n == 0:
            break
    if time.time() >= deadline:
        print(f"  ! still active after {args.wait_minutes}min — triggering import on whatever is done")

    scan_path = "/data/torrents/music"
    rr = L.post(
        f"{LIDARR_URL}/api/v1/command",
        json={"name": "DownloadedAlbumsScan", "path": scan_path},
    )
    if rr.ok:
        cid = rr.json().get("id")
        print(f"  triggered DownloadedAlbumsScan id={cid}")
        for _ in range(30):
            time.sleep(2)
            c = L.get(f"{LIDARR_URL}/api/v1/command/{cid}").json()
            if c["status"] in ("completed", "failed"):
                print(f"    auto-scan {c['status']}: {(c.get('message') or '')[:200]}")
                break
    else:
        print(f"  ! DownloadedAlbumsScan [{rr.status_code}]: {rr.text[:200]}")

    # ---- Phase 5: recover any rejected files via recording_mbid ----
    print("\n=== Phase 5: recovery — force-import remaining rejects via recording_mbid ===")
    arts = L.get(f"{LIDARR_URL}/api/v1/artist").json()
    fixed = lidarr_recover_rejected(L, scan_path, basename_to_meta, arts)
    print(f"  recovered {fixed} file(s)")

    print("\nDONE.")


if __name__ == "__main__":
    main()
