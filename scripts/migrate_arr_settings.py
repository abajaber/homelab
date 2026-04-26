#!/usr/bin/env python3
"""Migrate basic settings between *arr instances (Sonarr / Radarr / Prowlarr).

Idempotent: every write is preceded by a GET-list and dedup-by-name. Library
state (series/movies, queue, history, blocklist, calendar) is never touched.

See plan: ~/.claude/plans/crystalline-tumbling-biscuit.md
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any, Callable, Iterable

import urllib3

try:
    import requests
except ImportError:
    sys.stderr.write("requests not installed; activate the repo venv first\n")
    sys.exit(2)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def install_dns_overrides(overrides: dict[str, str]) -> None:
    """Pin specific hostnames to specific IPs for the rest of the process,
    similar to `curl --resolve`. Used when split-DNS isn't configured on the
    workstation but Traefik routes by Host header on a known IP."""
    if not overrides:
        return
    real_getaddrinfo = socket.getaddrinfo

    def patched(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host in overrides:
            host = overrides[host]
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = patched  # type: ignore[assignment]


# ---------- HTTP client ------------------------------------------------------


class ArrClient:
    def __init__(self, base_url: str, api_key: str, api_prefix: str, verify: bool):
        self.base = base_url.rstrip("/")
        self.prefix = api_prefix
        self.s = requests.Session()
        self.s.headers["X-Api-Key"] = api_key
        self.s.headers["Content-Type"] = "application/json"
        self.s.verify = verify

    def _url(self, path: str) -> str:
        return f"{self.base}{self.prefix}{path}"

    def get(self, path: str) -> Any:
        r = self.s.get(self._url(path), timeout=30)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any) -> Any:
        r = self.s.post(self._url(path), data=json.dumps(body), timeout=30)
        if not r.ok:
            sys.stderr.write(f"    ! POST {path} -> {r.status_code} {r.text[:400]}\n")
            r.raise_for_status()
        return r.json() if r.text else None

    def put(self, path: str, body: Any) -> Any:
        r = self.s.put(self._url(path), data=json.dumps(body), timeout=30)
        if not r.ok:
            sys.stderr.write(f"    ! PUT {path} -> {r.status_code} {r.text[:400]}\n")
            r.raise_for_status()
        return r.json() if r.text else None


# ---------- helpers ----------------------------------------------------------


def index_by(items: Iterable[dict], key: str) -> dict[Any, dict]:
    return {item[key]: item for item in items if item.get(key) is not None}


def deep_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def strip_id(item: dict) -> dict:
    out = dict(item)
    out.pop("id", None)
    return out


def field_value(item: dict, name: str, default: Any = None) -> Any:
    for f in item.get("fields", []) or []:
        if f.get("name") == name:
            return f.get("value", default)
    return default


def set_field(item: dict, name: str, value: Any) -> bool:
    """Set field by name; return True if the field exists in the schema."""
    for f in item.get("fields", []) or []:
        if f.get("name") == name:
            f["value"] = value
            return True
    return False


# ---------- generic POST-by-name migrate -------------------------------------


def migrate_collection(
    label: str,
    src: ArrClient,
    dst: ArrClient,
    path: str,
    match_key: str,
    dry_run: bool,
    transform: Callable[[dict], dict] | None = None,
    skip_predicate: Callable[[dict], bool] | None = None,
) -> dict[Any, dict]:
    """Copy items from src→dst at `path`, deduped by `match_key`. Returns dst index."""
    src_items = src.get(path)
    dst_items = dst.get(path)
    dst_by = index_by(dst_items, match_key)
    created = 0
    for src_item in src_items:
        if skip_predicate and skip_predicate(src_item):
            continue
        key = src_item.get(match_key)
        if key is None or key in dst_by:
            continue
        body = strip_id(src_item)
        if transform:
            body = transform(body)
        if dry_run:
            print(f"  [dry-run] POST {path}: {match_key}={key!r}")
            created += 1
            continue
        print(f"  POST {path}: {match_key}={key!r}")
        result = dst.post(path, body)
        if isinstance(result, dict) and "id" in result:
            dst_by[key] = result
        created += 1
    print(f"  {label}: created {created}, dst now has {len(dst_by) + (created if dry_run else 0)}")
    return dst_by


# ---------- Sonarr/Radarr migration ------------------------------------------


def migrate_qualitydefinitions(src: ArrClient, dst: ArrClient, dry_run: bool) -> None:
    src_defs = src.get("/qualitydefinition")
    dst_defs = dst.get("/qualitydefinition")
    dst_by_qname: dict[str, dict] = {
        d["quality"]["name"]: d for d in dst_defs if isinstance(d.get("quality"), dict)
    }
    updated = 0
    for src_def in src_defs:
        qname = src_def.get("quality", {}).get("name")
        if not qname or qname not in dst_by_qname:
            continue
        dst_def = dst_by_qname[qname]
        merged = dict(dst_def)
        changed = False
        for field in ("minSize", "maxSize", "preferredSize", "title"):
            if field in src_def and src_def[field] != merged.get(field):
                merged[field] = src_def[field]
                changed = True
        if not changed:
            continue
        if dry_run:
            print(f"  [dry-run] PUT /qualitydefinition/{merged['id']}: {qname}")
        else:
            print(f"  PUT /qualitydefinition/{merged['id']}: {qname}")
            dst.put(f"/qualitydefinition/{merged['id']}", merged)
        updated += 1
    print(f"  qualitydefinition: updated {updated}")


def migrate_qualityprofiles(src: ArrClient, dst: ArrClient, dry_run: bool) -> None:
    src_cf = src.get("/customformat")
    dst_cf = dst.get("/customformat")
    src_cf_by_id = {c["id"]: c["name"] for c in src_cf}
    dst_cf_by_name = {c["name"]: c["id"] for c in dst_cf}
    src_profs = src.get("/qualityprofile")
    dst_profs = dst.get("/qualityprofile")
    dst_by_name = index_by(dst_profs, "name")
    created = 0
    for prof in src_profs:
        name = prof["name"]
        if name in dst_by_name:
            continue
        body = strip_id(deep_copy(prof))
        new_items = []
        for fi in body.get("formatItems", []) or []:
            src_id = fi.get("format")
            cf_name = src_cf_by_id.get(src_id)
            if cf_name and cf_name in dst_cf_by_name:
                fi["format"] = dst_cf_by_name[cf_name]
                new_items.append(fi)
            elif cf_name:
                print(f"    ! profile {name!r}: dropping unknown custom format {cf_name!r}")
        body["formatItems"] = new_items
        if dry_run:
            print(f"  [dry-run] POST /qualityprofile: {name}")
        else:
            print(f"  POST /qualityprofile: {name}")
            dst.post("/qualityprofile", body)
        created += 1
    print(f"  qualityprofile: created {created}")


def install_arr_download_clients(
    dst: ArrClient, app: str, vpn_pass: str | None, direct_pass: str | None, dry_run: bool
) -> None:
    schemas = dst.get("/downloadclient/schema")
    qbit_schema = next((s for s in schemas if s.get("implementation") == "QBittorrent"), None)
    if not qbit_schema:
        sys.stderr.write("    ! no QBittorrent schema on dst — skipping download clients\n")
        return
    existing = dst.get("/downloadclient")
    existing_names = {c["name"] for c in existing}
    cat_field = "tvCategory" if app == "sonarr" else "movieCategory"
    cat_value = {"sonarr": "tv-sonarr", "radarr": "movies-radarr"}.get(app, "")

    def build(name: str, host: str, pw: str | None) -> dict:
        body = deep_copy(qbit_schema)
        body["name"] = name
        body["enable"] = True
        body["priority"] = 1
        body["removeCompletedDownloads"] = True
        body["removeFailedDownloads"] = True
        body["tags"] = []
        set_field(body, "host", host)
        set_field(body, "port", 8080)
        set_field(body, "useSsl", False)
        set_field(body, "urlBase", "")
        set_field(body, "username", "admin")
        set_field(body, "password", pw or "")
        set_field(body, cat_field, cat_value)
        body.pop("id", None)
        return body

    for name, host, pw in (
        ("qbittorrent-vpn", "gluetun", vpn_pass),
        ("qbittorrent-direct", "qbittorrent-direct", direct_pass),
    ):
        if name in existing_names:
            print(f"  download client {name!r} already present, skipping")
            continue
        if not pw:
            print(f"    ! no password for {name!r}; creating with empty password (set later)")
        body = build(name, host, pw)
        if dry_run:
            print(f"  [dry-run] POST /downloadclient: {name}")
            continue
        # forceSave=true skips Sonarr/Radarr's connection-test validation. The
        # entry is persisted; the user fixes the password in the UI later.
        print(f"  POST /downloadclient?forceSave=true: {name}")
        try:
            dst.post("/downloadclient?forceSave=true", body)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(f"    ! download client {name!r} failed validation — skipping (fix in UI)")
                continue
            raise


def migrate_arr(args: argparse.Namespace, app: str) -> None:
    src = ArrClient(args.src_url, args.src_key, "/api/v3", verify=not args.insecure)
    dst = ArrClient(args.dst_url, args.dst_key, "/api/v3", verify=not args.insecure)

    print(f"\n=== {app} ===")
    print(f"src: {args.src_url}  ->  dst: {args.dst_url}")

    print("[1/12] tag")
    migrate_collection("tag", src, dst, "/tag", "label", args.dry_run)

    print("[2/12] customformat")
    migrate_collection("customformat", src, dst, "/customformat", "name", args.dry_run,
                       transform=strip_id)

    print("[3/12] qualitydefinition")
    migrate_qualitydefinitions(src, dst, args.dry_run)

    print("[4/12] qualityprofile")
    migrate_qualityprofiles(src, dst, args.dry_run)

    print("[5/12] rootfolder")
    src_roots = src.get("/rootfolder")
    dst_roots = dst.get("/rootfolder")
    dst_paths = {r["path"] for r in dst_roots}
    for r in src_roots:
        path = r.get("path")
        if not path or path in dst_paths:
            continue
        body = strip_id(r)
        if args.dry_run:
            print(f"  [dry-run] POST /rootfolder: path={path!r}")
            continue
        try:
            print(f"  POST /rootfolder: path={path!r}")
            dst.post("/rootfolder", body)
        except requests.HTTPError as e:
            # Path layout differs between src and dst; skip the missing one
            # and let the user pick the right root folder in the UI later.
            if e.response is not None and e.response.status_code == 400:
                print(f"    ! root folder {path!r} doesn't exist on dst — skipping (set manually)")
                continue
            raise

    print("[6/12] downloadclient (rewrite)")
    install_arr_download_clients(dst, app, args.qbit_vpn_pass, args.qbit_direct_pass, args.dry_run)

    print("[7/12] notification")
    migrate_collection("notification", src, dst, "/notification", "name", args.dry_run)

    print("[8/12] delayprofile")
    src_dp = src.get("/delayprofile")
    dst_dp = dst.get("/delayprofile")
    dst_dp_by_id = index_by(dst_dp, "id")
    # id=1 is the default; PUT it. Others go by tags signature (POST).
    for sp in src_dp:
        if sp.get("id") == 1 and 1 in dst_dp_by_id:
            merged = dict(dst_dp_by_id[1])
            for k, v in sp.items():
                if k != "id":
                    merged[k] = v
            if args.dry_run:
                print(f"  [dry-run] PUT /delayprofile/1 (default)")
            else:
                print(f"  PUT /delayprofile/1 (default)")
                dst.put("/delayprofile/1", merged)
        else:
            body = strip_id(sp)
            if args.dry_run:
                print(f"  [dry-run] POST /delayprofile (tags={sp.get('tags')})")
            else:
                print(f"  POST /delayprofile (tags={sp.get('tags')})")
                dst.post("/delayprofile", body)

    print("[9/12] importlist")
    migrate_collection("importlist", src, dst, "/importlist", "name", args.dry_run)

    print("[10/12] metadata")
    src_md = src.get("/metadata")
    dst_md = index_by(dst.get("/metadata"), "name")
    for sm in src_md:
        if sm.get("name") not in dst_md:
            continue
        target = dict(dst_md[sm["name"]])
        for k in ("enable", "fields"):
            if k in sm:
                target[k] = sm[k]
        if args.dry_run:
            print(f"  [dry-run] PUT /metadata/{target['id']}: {sm['name']}")
        else:
            print(f"  PUT /metadata/{target['id']}: {sm['name']}")
            dst.put(f"/metadata/{target['id']}", target)

    if app == "sonarr":
        print("[11/12] releaseprofile")
        migrate_collection("releaseprofile", src, dst, "/releaseprofile", "name", args.dry_run)
    else:
        print("[11/12] releaseprofile — skipped (not sonarr)")

    print("[12/12] config (mediamanagement, naming" + (", host" if args.include_host_config else "") + ")")
    for cfg in ("mediamanagement", "naming"):
        src_cfg = src.get(f"/config/{cfg}")
        dst_cfg = dst.get(f"/config/{cfg}")
        merged = dict(dst_cfg)
        for k, v in src_cfg.items():
            if k in ("id",):
                continue
            merged[k] = v
        if args.dry_run:
            print(f"  [dry-run] PUT /config/{cfg}")
        else:
            print(f"  PUT /config/{cfg}")
            dst.put(f"/config/{cfg}", merged)
    if args.include_host_config:
        src_host = src.get("/config/host")
        dst_host = dst.get("/config/host")
        merged = dict(dst_host)
        for k, v in src_host.items():
            if k in ("id", "apiKey", "instanceName", "urlBase"):
                continue
            merged[k] = v
        if args.dry_run:
            print(f"  [dry-run] PUT /config/host")
        else:
            print(f"  PUT /config/host")
            dst.put("/config/host", merged)

    print("\nindexer migration: SKIPPED (Prowlarr will repopulate)\n")


# ---------- Prowlarr migration -----------------------------------------------


def install_prowlarr_download_clients(
    dst: ArrClient, vpn_pass: str | None, direct_pass: str | None, dry_run: bool
) -> None:
    schemas = dst.get("/downloadclient/schema")
    qbit_schema = next((s for s in schemas if s.get("implementation") == "QBittorrent"), None)
    if not qbit_schema:
        sys.stderr.write("    ! no QBittorrent schema on prowlarr dst — skipping\n")
        return
    existing = dst.get("/downloadclient")
    existing_names = {c["name"] for c in existing}

    def build(name: str, host: str, pw: str | None) -> dict:
        body = deep_copy(qbit_schema)
        body["name"] = name
        body["enable"] = True
        body["priority"] = 1
        body["tags"] = []
        set_field(body, "host", host)
        set_field(body, "port", 8080)
        set_field(body, "useSsl", False)
        set_field(body, "urlBase", "")
        set_field(body, "username", "admin")
        set_field(body, "password", pw or "")
        body.pop("id", None)
        return body

    for name, host, pw in (
        ("qbittorrent-vpn", "gluetun", vpn_pass),
        ("qbittorrent-direct", "qbittorrent-direct", direct_pass),
    ):
        if name in existing_names:
            print(f"  download client {name!r} already present, skipping")
            continue
        body = build(name, host, pw)
        if dry_run:
            print(f"  [dry-run] POST /downloadclient: {name}")
            continue
        print(f"  POST /downloadclient?forceSave=true: {name}")
        try:
            dst.post("/downloadclient?forceSave=true", body)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(f"    ! download client {name!r} failed validation — skipping (fix in UI)")
                continue
            raise


def migrate_prowlarr_indexers(src: ArrClient, dst: ArrClient, dry_run: bool) -> None:
    src_proxies = src.get("/indexerproxy")
    dst_proxies = dst.get("/indexerproxy")
    src_proxy_by_id = {p["id"]: p["name"] for p in src_proxies}
    dst_proxy_by_name = {p["name"]: p["id"] for p in dst_proxies}

    src_indexers = src.get("/indexer")
    dst_indexers = dst.get("/indexer")
    dst_by_name = index_by(dst_indexers, "name")

    created = 0
    for idx in src_indexers:
        name = idx.get("name")
        if not name or name in dst_by_name:
            continue
        body = strip_id(deep_copy(idx))
        # Remap indexerProxyId field (Prowlarr stores it inside fields[]).
        for f in body.get("fields", []) or []:
            if f.get("name") == "indexerProxyId":
                src_pid = f.get("value")
                proxy_name = src_proxy_by_id.get(src_pid)
                if proxy_name and proxy_name in dst_proxy_by_name:
                    f["value"] = dst_proxy_by_name[proxy_name]
                else:
                    f["value"] = 0
        if dry_run:
            print(f"  [dry-run] POST /indexer: {name}")
            created += 1
            continue
        print(f"  POST /indexer?forceSave=true: {name}")
        try:
            dst.post("/indexer?forceSave=true", body)
            created += 1
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 404, 500):
                msg = (e.response.text or "")[:160].replace("\n", " ")
                print(f"    ! indexer {name!r} failed ({e.response.status_code}): {msg!r} — skipping")
                continue
            raise
    print(f"  indexer: created {created}, dst already had {len(dst_by_name)}")


def install_prowlarr_applications(
    src: ArrClient,
    dst: ArrClient,
    sonarr_key: str | None,
    radarr_key: str | None,
    dry_run: bool,
) -> None:
    schemas = dst.get("/applications/schema")
    by_impl = {s.get("implementation"): s for s in schemas}
    src_apps = src.get("/applications")
    src_by_impl = {a.get("implementation"): a for a in src_apps}
    existing = dst.get("/applications")
    existing_names = {a["name"] for a in existing}

    targets = (
        ("Sonarr", "sonarr", "https://sonarr.bajaber.ca", "http://sonarr:8989", sonarr_key),
        ("Radarr", "radarr", "https://radarr.bajaber.ca", "http://radarr:7878", radarr_key),
    )
    for impl, name, public_url, internal_url, api_key in targets:
        if name in existing_names:
            print(f"  application {name!r} already present, skipping")
            continue
        if not api_key:
            print(f"    ! no API key for {name!r} — skipping (pass --{name}-key)")
            continue
        schema = by_impl.get(impl)
        if not schema:
            print(f"    ! no schema for {impl} on dst, skipping")
            continue
        src_app = src_by_impl.get(impl)
        body = deep_copy(schema)
        body["name"] = name
        body["syncLevel"] = (src_app or {}).get("syncLevel", "fullSync")
        body["tags"] = (src_app or {}).get("tags", [])
        set_field(body, "prowlarrUrl", "http://prowlarr:9696")
        set_field(body, "baseUrl", internal_url)
        set_field(body, "apiKey", api_key)
        if src_app:
            for fname in ("syncCategories", "animeSyncCategories", "syncRejectBlocklistedTorrentHashesWhileGrabbing"):
                src_val = field_value(src_app, fname)
                if src_val is not None:
                    set_field(body, fname, src_val)
        body.pop("id", None)
        if dry_run:
            print(f"  [dry-run] POST /applications: {name} (baseUrl={internal_url})")
            continue
        print(f"  POST /applications?forceSave=true: {name} (baseUrl={internal_url})")
        try:
            dst.post("/applications?forceSave=true", body)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                print(f"    ! application {name!r} test failed — saved anyway, review in UI")
                continue
            raise


def migrate_prowlarr(args: argparse.Namespace) -> None:
    src = ArrClient(args.src_url, args.src_key, "/api/v1", verify=not args.insecure)
    dst = ArrClient(args.dst_url, args.dst_key, "/api/v1", verify=not args.insecure)

    print(f"\n=== prowlarr ===")
    print(f"src: {args.src_url}  ->  dst: {args.dst_url}")

    print("[1/6] tag")
    migrate_collection("tag", src, dst, "/tag", "label", args.dry_run)

    print("[2/6] indexerproxy")
    migrate_collection("indexerproxy", src, dst, "/indexerproxy", "name", args.dry_run)

    print("[3/6] indexer")
    migrate_prowlarr_indexers(src, dst, args.dry_run)

    print("[4/6] downloadclient (rewrite)")
    install_prowlarr_download_clients(dst, args.qbit_vpn_pass, args.qbit_direct_pass, args.dry_run)

    print("[5/6] notification")
    migrate_collection("notification", src, dst, "/notification", "name", args.dry_run)

    print("[6/6] applications (rewrite)")
    install_prowlarr_applications(src, dst, args.sonarr_key, args.radarr_key, args.dry_run)


# ---------- entry point ------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--app", required=True, choices=("sonarr", "radarr", "prowlarr"))
    p.add_argument("--src-url", required=True)
    p.add_argument("--src-key", default=None,
                   help="src API key (fallback env: SRC_<APP>_API_KEY, e.g. SRC_SONARR_API_KEY)")
    p.add_argument("--dst-url", required=True)
    p.add_argument("--dst-key", default=None,
                   help="dst API key (fallback env: <APP>_API_KEY, matches .env at "
                        "servers/truenas/apps/arr/.env)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--insecure", action="store_true",
                   help="skip TLS verification (e.g. self-signed certs)")
    p.add_argument("--resolve", action="append", default=[],
                   metavar="HOST:IP",
                   help="pin DNS for HOST to IP (curl-style, repeatable). "
                        "Useful when *.bajaber.ca isn't in workstation DNS "
                        "but Traefik on the IP routes by Host header.")
    p.add_argument("--include-host-config", action="store_true",
                   help="sonarr/radarr only: also migrate /config/host (UI prefs, security)")
    p.add_argument("--qbit-vpn-pass", default=os.environ.get("QBIT_VPN_PASSWORD"),
                   help="password for qbittorrent-vpn admin user (env: QBIT_VPN_PASSWORD)")
    p.add_argument("--qbit-direct-pass", default=os.environ.get("QBIT_DIRECT_PASSWORD"),
                   help="password for qbittorrent-direct admin user (env: QBIT_DIRECT_PASSWORD)")
    # Prowlarr-only — picked up from the same .env that pins keys into the containers.
    p.add_argument("--sonarr-key", default=os.environ.get("SONARR_API_KEY"),
                   help="prowlarr only: dst sonarr API key (fallback env: SONARR_API_KEY)")
    p.add_argument("--radarr-key", default=os.environ.get("RADARR_API_KEY"),
                   help="prowlarr only: dst radarr API key (fallback env: RADARR_API_KEY)")
    args = p.parse_args()

    app_upper = args.app.upper()
    if not args.src_key:
        args.src_key = os.environ.get(f"SRC_{app_upper}_API_KEY")
    if not args.dst_key:
        args.dst_key = os.environ.get(f"{app_upper}_API_KEY")
    missing = [name for name, val in (("--src-key", args.src_key),
                                       ("--dst-key", args.dst_key)) if not val]
    if missing:
        p.error(f"missing required: {', '.join(missing)} (or matching env var)")

    overrides: dict[str, str] = {}
    for entry in args.resolve:
        if ":" not in entry:
            p.error(f"--resolve expects HOST:IP, got {entry!r}")
        host, ip = entry.split(":", 1)
        overrides[host.strip()] = ip.strip()
    install_dns_overrides(overrides)

    try:
        if args.app == "prowlarr":
            migrate_prowlarr(args)
        else:
            migrate_arr(args, args.app)
    except requests.HTTPError as exc:
        sys.stderr.write(f"\nHTTP error: {exc}\n")
        return 1
    except requests.RequestException as exc:
        sys.stderr.write(f"\nNetwork error: {exc}\n")
        return 1
    print("\ndone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
