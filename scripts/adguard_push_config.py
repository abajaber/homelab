#!/usr/bin/env python3
"""Render and push AdGuard Home's config-as-code to TrueNAS.

TrueNAS ships only the compose body via app.update, never repo files, so
AdGuardHome.yaml has to be delivered out-of-band into the app's conf bind dir.
This script:

  1. decrypts servers/truenas/apps/adguard/.env (ansible-vault),
  2. renders AdGuardHome.yaml, substituting the two placeholders
     (__ADGUARD_PASSWORD_HASH__, __TRUENAS_TS_IP__) from the .env,
  3. writes it to /mnt/apps/adguard/conf/AdGuardHome.yaml via scripts/ot.py
     (open-terminal — /mnt/redsea/apps is mounted there as /mnt/apps).

After pushing, restart the `adguard` app so it reloads the config
(TrueNAS UI -> Apps -> adguard -> Restart, or redeploy via the JSON-RPC API).

Usage:
  python scripts/adguard_push_config.py            # render + push
  python scripts/adguard_push_config.py --dry-run  # print rendered yaml, no push
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "servers/truenas/apps/adguard"
ENV_FILE = APP_DIR / ".env"
TEMPLATE = APP_DIR / "AdGuardHome.yaml"
REMOTE_PATH = "/mnt/apps/adguard/conf/AdGuardHome.yaml"
OT = REPO_ROOT / "scripts/ot.py"

PLACEHOLDERS = {
    "__ADGUARD_PASSWORD_HASH__": "ADGUARD_PASSWORD_HASH",
    "__TRUENAS_TS_IP__": "TRUENAS_TS_IP",
}


def _ansible_vault() -> str:
    venv = REPO_ROOT / ".venv/bin/ansible-vault"
    if venv.exists():
        return str(venv)
    found = shutil.which("ansible-vault")
    if not found:
        sys.exit("ansible-vault not found — activate the venv (scripts/bootstrap.sh).")
    return found


def load_env() -> dict[str, str]:
    raw = ENV_FILE.read_text()
    if raw.startswith("$ANSIBLE_VAULT"):
        out = subprocess.run(
            [_ansible_vault(), "view", str(ENV_FILE)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if out.returncode != 0:
            sys.exit(f"ansible-vault view failed:\n{out.stderr}")
        raw = out.stdout
    env: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return env


def render(env: dict[str, str]) -> str:
    text = TEMPLATE.read_text()
    for token, key in PLACEHOLDERS.items():
        val = env.get(key, "")
        if not val or "replace" in val.lower() or val == "100.x.y.z":
            sys.exit(f"{key} is unset/placeholder in {ENV_FILE} — fill it before pushing.")
        text = text.replace(token, val)
    if "__" in text and any(p in text for p in PLACEHOLDERS):
        sys.exit("Unrendered placeholder remains — check the template.")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print rendered yaml, don't push")
    args = ap.parse_args()

    rendered = render(load_env())
    if args.dry_run:
        sys.stdout.write(rendered)
        return

    print(f"Pushing {len(rendered)} bytes -> {REMOTE_PATH}", file=sys.stderr)
    res = subprocess.run([str(OT), "write", REMOTE_PATH], input=rendered, text=True)
    if res.returncode != 0:
        sys.exit(res.returncode)
    print("Pushed. Now restart the `adguard` app so it reloads the config.", file=sys.stderr)


if __name__ == "__main__":
    main()
