#!/usr/bin/env python3
"""Sanity-check TrueNAS API auth.

Reads the API key from the vault (or TRUENAS_API_KEY env var), the username
from servers/truenas/vars.yml, and tries `auth.login_ex` + a handful of
read-only calls the reconciler depends on. Prints what works, what doesn't,
and (for the recursion-on-login bug) hints at where to look in the UI.

Run from the repo root:
    .venv/bin/python3 scripts/check_truenas_auth.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from truenas_client import TruenasClient, TruenasError  # noqa: E402
import yaml  # noqa: E402


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _api_key_from_vault() -> str | None:
    if v := os.environ.get("TRUENAS_API_KEY"):
        return v
    from ansible.parsing.vault import VaultLib, VaultSecret  # type: ignore

    pw_path = REPO_ROOT / ".vault-password"
    if not pw_path.is_file():
        return None
    secret = pw_path.read_bytes().strip()
    vault = VaultLib([(b"default", VaultSecret(secret))])
    raw = (REPO_ROOT / "servers/truenas/vault.yml").read_bytes()
    if not raw.startswith(b"$ANSIBLE_VAULT;"):
        # cleartext placeholder
        data = yaml.safe_load(raw.decode()) or {}
    else:
        data = yaml.safe_load(vault.decrypt(raw).decode("utf-8")) or {}
    return data.get("vault_truenas_api_key")


def main() -> int:
    vars_data = _load_yaml(REPO_ROOT / "servers/truenas/vars.yml")
    api_user = vars_data.get("truenas_api_user", "ansible")
    version = vars_data.get("truenas_api_version", "v25.10.2")
    host = "truenas.bajaber.ca"  # matches the inventory; override via TRUENAS_HOST
    host = os.environ.get("TRUENAS_HOST", host)
    url = f"wss://{host}:4443/api/{version}"

    api_key = _api_key_from_vault()
    if not api_key:
        print("could not read API key from vault (set TRUENAS_API_KEY or check .vault-password)", file=sys.stderr)
        return 2

    print(f"endpoint: {url}")
    print(f"username: {api_user}")
    print()

    try:
        with TruenasClient(url, api_key=api_key, username=api_user, verify_tls=False) as c:
            print("✓ auth.login_ex OK")
            for name, method, params in [
                ("app.query",          "app.query",          [[], {"select": ["name", "custom_app"]}]),
                ("pool.dataset.query", "pool.dataset.query", [[["name", "=", "redsea/apps"]], {"limit": 1}]),
                ("filesystem.stat",    "filesystem.stat",    ["/mnt/redsea/apps"]),
            ]:
                try:
                    res = c._call(method, params)
                    n = len(res) if isinstance(res, list) else "ok"
                    print(f"✓ {name}: {n}")
                except TruenasError as e:
                    print(f"✗ {name}: {e}")
        return 0
    except TruenasError as e:
        msg = str(e)
        print(f"✗ auth failed: {msg[:300]}")
        if "recursion" in msg.lower() or "RecursionError" in msg:
            print()
            print("This is a TrueNAS server-side bug — `roles_for_role` is recursing on the")
            print(f"`{api_user}` user's role tree (cyclic inclusion). Fix in the UI:")
            print(f"  Credentials → Local Users → {api_user} → Edit → clear Allowed Roles,")
            print("  save, then re-add a single non-cyclic role (e.g. FULL_ADMIN).")
            print(f"Or generate a new API key under a different user (e.g. root) and update")
            print("`servers/truenas/vars.yml` `truenas_api_user` + `servers/truenas/vault.yml`.")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
