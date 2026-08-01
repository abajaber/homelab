#!/usr/bin/env python3
"""Reconcile *arr instance settings from servers/truenas/apps/arr/settings.yml.

Recyclarr owns custom formats, quality definitions and quality profiles. This
owns the rest of the per-instance DB config that otherwise only exists as
clicks in a UI: tags, indexer/download-client tag routing, auto-tagging rules,
release profiles, series type and per-series tags.

Idempotent — every write is preceded by a read and a diff. Plan by default:

    set -a; eval "$(ansible-vault view servers/truenas/apps/arr/.env)"; set +a
    python scripts/arr_settings_sync.py
    python scripts/arr_settings_sync.py --apply

Library state (queue, history, blocklist, files) is never touched.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import urllib3

try:
    import requests
    import yaml
except ImportError:
    sys.stderr.write("requests/pyyaml not installed; activate the repo venv first\n")
    sys.exit(2)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = REPO / "servers" / "truenas" / "apps" / "arr" / "settings.yml"


def install_dns_overrides(overrides: dict[str, str]) -> None:
    """Pin hostnames to IPs for the rest of the process, like `curl --resolve`.
    The workstation has no split-DNS for *.bajaber.ca but Traefik routes by
    Host header on a known IP."""
    if not overrides:
        return
    real_getaddrinfo = socket.getaddrinfo

    def patched(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host in overrides:
            host = overrides[host]
        return real_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = patched  # type: ignore[assignment]


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
        r = self.s.get(self._url(path), timeout=60)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any) -> Any:
        r = self.s.post(self._url(path), data=json.dumps(body), timeout=60)
        if not r.ok:
            sys.stderr.write(f"    ! POST {path} -> {r.status_code} {r.text[:400]}\n")
            r.raise_for_status()
        return r.json() if r.text else None

    def put(self, path: str, body: Any) -> Any:
        r = self.s.put(self._url(path), data=json.dumps(body), timeout=60)
        if not r.ok:
            sys.stderr.write(f"    ! PUT {path} -> {r.status_code} {r.text[:400]}\n")
            r.raise_for_status()
        return r.json() if r.text else None


class Reconciler:
    """Collects a plan while walking the desired state; applies only if asked."""

    def __init__(self, client: ArrClient, apply: bool):
        self.c = client
        self.apply = apply
        self.changes: list[str] = []

    def note(self, msg: str) -> None:
        self.changes.append(msg)
        prefix = "  ~" if self.apply else "  +"
        print(f"{prefix} {msg}")

    # -- tags ---------------------------------------------------------------

    def sync_tags(self, labels: list[str]) -> dict[str, int]:
        """Ensure every label exists; return label -> id for the whole instance."""
        existing = {t["label"]: t["id"] for t in self.c.get("/tag")}
        for label in labels:
            if label in existing:
                continue
            self.note(f"tag '{label}': create")
            if self.apply:
                existing[label] = self.c.post("/tag", {"label": label})["id"]
            else:
                existing[label] = -1
        return existing

    def _resolve(self, labels: list[str], tag_ids: dict[str, int], where: str) -> list[int]:
        out = []
        for label in labels:
            if label not in tag_ids:
                sys.stderr.write(f"    ! {where}: unknown tag '{label}'\n")
                continue
            out.append(tag_ids[label])
        return sorted(out)

    # -- tag assignment on indexers / download clients ------------------------

    def sync_resource_tags(
        self, endpoint: str, desired: dict[str, list[str]], tag_ids: dict[str, int]
    ) -> None:
        by_name = {r["name"]: r for r in self.c.get(endpoint)}
        for name, labels in desired.items():
            res = by_name.get(name)
            if res is None:
                sys.stderr.write(f"    ! {endpoint}: no resource named '{name}'\n")
                continue
            want = self._resolve(labels, tag_ids, f"{endpoint}/{name}")
            if sorted(res.get("tags", [])) == want:
                continue
            self.note(f"{endpoint} '{name}': tags {sorted(res.get('tags', []))} -> {want}")
            if self.apply:
                res["tags"] = want
                self.c.put(f"{endpoint}/{res['id']}", res)

    # -- auto tagging --------------------------------------------------------

    def sync_auto_tagging(self, desired: list[dict], tag_ids: dict[str, int]) -> None:
        existing = {r["name"]: r for r in self.c.get("/autotagging")}
        for rule in desired:
            body = self._auto_tag_body(rule, tag_ids)
            current = existing.get(rule["name"])
            if current is None:
                self.note(f"autotagging '{rule['name']}': create")
                if self.apply:
                    self.c.post("/autotagging", body)
                continue
            if self._auto_tag_equal(current, body):
                continue
            self.note(f"autotagging '{rule['name']}': update")
            if self.apply:
                body["id"] = current["id"]
                self.c.put(f"/autotagging/{current['id']}", body)

    def _auto_tag_body(self, rule: dict, tag_ids: dict[str, int]) -> dict:
        specs = []
        for spec in rule.get("specifications", []):
            # `tag_value` is a label in the repo; the API wants a tag id, and
            # `fields` must be a LIST of {name, value} — a dict 400s.
            fields = [{"name": "value", "value": tag_ids[spec["tag_value"]]}]
            specs.append(
                {
                    "name": spec["name"],
                    "implementation": spec["implementation"],
                    "negate": bool(spec.get("negate", False)),
                    "required": bool(spec.get("required", False)),
                    "fields": fields,
                }
            )
        return {
            "name": rule["name"],
            "removeTagsAutomatically": bool(rule.get("remove_tags_automatically", False)),
            "tags": self._resolve(rule.get("tags", []), tag_ids, "autotagging"),
            "specifications": specs,
        }

    @staticmethod
    def _auto_tag_equal(current: dict, want: dict) -> bool:
        if sorted(current.get("tags", [])) != sorted(want["tags"]):
            return False
        if bool(current.get("removeTagsAutomatically")) != want["removeTagsAutomatically"]:
            return False

        def norm(specs: list[dict]) -> list[tuple]:
            out = []
            for s in specs:
                fields = s.get("fields", [])
                values = sorted(
                    (f["name"], f.get("value")) for f in fields if f.get("name") == "value"
                )
                out.append(
                    (s.get("implementation"), bool(s.get("negate")), bool(s.get("required")), tuple(values))
                )
            return sorted(out)

        return norm(current.get("specifications", [])) == norm(want["specifications"])

    # -- release profiles ----------------------------------------------------

    def sync_release_profiles(self, desired: list[dict], tag_ids: dict[str, int]) -> None:
        existing = {r["name"]: r for r in self.c.get("/releaseprofile")}
        for prof in desired:
            body = {
                "name": prof["name"],
                "enabled": bool(prof.get("enabled", True)),
                "required": prof.get("required", []),
                "ignored": prof.get("ignored", []),
                "indexerId": prof.get("indexer_id", 0),
                "tags": self._resolve(prof.get("tags", []), tag_ids, "releaseprofile"),
            }
            current = existing.get(prof["name"])
            if current is None:
                self.note(f"releaseprofile '{prof['name']}': create")
                if self.apply:
                    self.c.post("/releaseprofile", body)
                continue
            same = all(
                (sorted(current.get(k, [])) if isinstance(body[k], list) else current.get(k))
                == (sorted(body[k]) if isinstance(body[k], list) else body[k])
                for k in ("enabled", "required", "ignored", "indexerId", "tags")
            )
            if same:
                continue
            self.note(f"releaseprofile '{prof['name']}': update")
            if self.apply:
                body["id"] = current["id"]
                self.c.put(f"/releaseprofile/{current['id']}", body)

    # -- series-level ---------------------------------------------------------

    def sync_series(
        self, series_type: str | None, series_tags: dict[str, list[str]], tag_ids: dict[str, int]
    ) -> None:
        if not series_type and not series_tags:
            return
        for s in self.c.get("/series"):
            dirty = False
            if series_type and s.get("seriesType") != series_type:
                self.note(f"series '{s['title']}': seriesType {s.get('seriesType')} -> {series_type}")
                s["seriesType"] = series_type
                dirty = True
            want = series_tags.get(s["title"])
            if want:
                # In plan mode a not-yet-created tag resolves to the -1
                # placeholder, so report by label rather than by id.
                by_id = {v: k for k, v in tag_ids.items()}
                missing = [t for t in self._resolve(want, tag_ids, "series") if t not in s["tags"]]
                if missing:
                    labels = [by_id.get(t, str(t)) for t in missing]
                    self.note(f"series '{s['title']}': add tags {labels}")
                    s["tags"] = sorted(s["tags"] + missing)
                    dirty = True
            if dirty and self.apply:
                self.c.put(f"/series/{s['id']}", s)


def run_instance(name: str, spec: dict, verify: bool, apply: bool) -> int:
    key = os.environ.get(spec["api_key_env"], "")
    if not key or key == "replace-me":
        sys.stderr.write(f"! {name}: ${spec['api_key_env']} not set — skipping\n")
        return 0

    print(f"\n== {name} ({spec['base_url']})")
    client = ArrClient(spec["base_url"], key, spec.get("api_prefix", "/api/v3"), verify)
    r = Reconciler(client, apply)

    tag_ids = r.sync_tags(spec.get("tags", []))
    if spec.get("indexer_tags"):
        r.sync_resource_tags("/indexer", spec["indexer_tags"], tag_ids)
    if spec.get("download_client_tags"):
        r.sync_resource_tags("/downloadclient", spec["download_client_tags"], tag_ids)
    if spec.get("auto_tagging"):
        r.sync_auto_tagging(spec["auto_tagging"], tag_ids)
    if spec.get("release_profiles"):
        r.sync_release_profiles(spec["release_profiles"], tag_ids)
    r.sync_series(spec.get("enforce_series_type"), spec.get("series_tags", {}), tag_ids)

    if not r.changes:
        print("  = in sync")
    return len(r.changes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--settings", default=str(DEFAULT_SETTINGS))
    ap.add_argument("--apply", action="store_true", help="write changes (default: plan only)")
    ap.add_argument("--instance", action="append", help="limit to named instance(s)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.settings).read_text())
    install_dns_overrides(cfg.get("dns_overrides", {}))
    verify = bool(cfg.get("verify_tls", True))

    total = 0
    for name, spec in cfg["instances"].items():
        if args.instance and name not in args.instance:
            continue
        total += run_instance(name, spec, verify, args.apply)

    mode = "applied" if args.apply else "planned"
    print(f"\n{total} change(s) {mode}")
    if total and not args.apply:
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
