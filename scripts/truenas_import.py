#!/usr/bin/env python3
"""Import existing TrueNAS Custom Apps into the repo.

For each Custom App on the server that is not yet in the repo, write
`servers/truenas/apps/<name>/{app.yml,compose.yml}` mirroring its current
compose. Catalog apps are skipped with a warning — they don't have a compose
body the way custom apps do.

The repo file is the *clean* user compose: any existing `x-homelab` marker
on the live app is stripped before writing the repo file. This script never
writes to the server — adoption (stamping the marker so the next sync treats
the app as managed) happens during `truenas_sync.yml -e mode=apply`, which
recognizes "in repo + on server without marker" as an adoption case.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from truenas_client import TruenasClient, TruenasError  # noqa: E402
from truenas_reconcile import (  # noqa: E402
    read_marker,
    strip_marker,
)
from scan_compose_secrets import scan as scan_compose_secrets  # noqa: E402

import yaml


def write_app_files(app_dir: Path, name: str, compose: str) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {"name": name, "enabled": True}
    rendered = "---\n" + yaml.safe_dump(meta, default_flow_style=False, sort_keys=False)
    (app_dir / "app.yml").write_text(rendered)
    body = compose if compose.endswith("\n") else compose + "\n"
    (app_dir / "compose.yml").write_text(body)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apps-dir", required=True, type=Path)
    p.add_argument("--api-url", required=True, help="wss://host/api/<version>")
    p.add_argument("--api-user", default="admin", help="username the API key belongs to")
    p.add_argument("--managed-by", default="homelab-repo")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--host", default="truenas")
    args = p.parse_args()

    api_key = os.environ.get("TRUENAS_API_KEY")
    if not api_key:
        print("TRUENAS_API_KEY env var is required", file=sys.stderr)
        return 2

    existing_repo = (
        {p.name for p in args.apps_dir.iterdir() if p.is_dir()}
        if args.apps_dir.is_dir()
        else set()
    )

    to_write: list[tuple[str, str]] = []
    catalog_skipped: list[str] = []
    already_managed: list[str] = []
    already_in_repo: list[str] = []
    no_compose: list[str] = []

    try:
        with TruenasClient(
            args.api_url,
            api_key=api_key,
            username=args.api_user,
            verify_tls=not args.insecure,
        ) as client:
            for app in client.app_query():
                name = app.get("name") or app.get("id")
                if not name:
                    continue
                if not app.get("custom_app"):
                    catalog_skipped.append(name)
                    continue
                # app.config() returns the parsed compose dict directly.
                cfg = client.app_config(name)
                if not isinstance(cfg, dict) or not cfg:
                    no_compose.append(name)
                    continue
                if read_marker(cfg, args.managed_by) is not None:
                    already_managed.append(name)
                    continue
                if name in existing_repo:
                    already_in_repo.append(name)
                    continue
                to_write.append((name, strip_marker(cfg)))

            print(f"host: {args.host}")
            print(f"+ to-import:        {[n for n, _ in to_write]}")
            print(f"= already-managed:  {already_managed}")
            print(f"= already-in-repo:  {already_in_repo}  (run truenas_sync.yml -e mode=apply to adopt these)")
            print(f"! catalog-skipped:  {catalog_skipped}  (manage these in the TrueNAS UI)")
            if no_compose:
                print(f"! no-compose-found: {no_compose}")

            n_written = 0
            apps_with_secrets: list[tuple[str, list[tuple[str, str, str]]]] = []
            for name, compose in to_write:
                app_dir = args.apps_dir / name
                print(f"  writing {app_dir.relative_to(REPO_ROOT)} ...")
                write_app_files(app_dir, name, compose)
                n_written += 1
                findings = scan_compose_secrets(compose)
                if findings:
                    apps_with_secrets.append((name, findings))

            if apps_with_secrets:
                print()
                print("! likely secrets present in the imported composes:")
                for name, findings in apps_with_secrets:
                    print(f"  {name}:")
                    for svc, key, preview in findings:
                        print(f"    services.{svc}.environment.{key} = {preview}")
                print(
                    "\n  Extract these into `.env` next to compose.yml, replace each value\n"
                    "  with ${VAR_NAME} in compose.yml, then\n"
                    "    ansible-vault encrypt servers/<host>/apps/<app>/.env\n"
                    "  before committing."
                )

            print(f"changed={'true' if n_written > 0 else 'false'}  written={n_written}")
            return 0
    except TruenasError as e:
        print(f"truenas error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
