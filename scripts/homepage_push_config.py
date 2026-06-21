#!/usr/bin/env python3
"""Push Homepage's config-as-code to TrueNAS.

TrueNAS ships only the compose body via app.update, never repo files, so the
YAML under servers/truenas/apps/homepage/config/ has to be delivered out-of-band
into the app's config bind dir (same pattern as adguard_push_config.py and the
Authentik blueprints). This script writes each *.yaml into
/mnt/apps/homepage/config/ via scripts/ot.py (open-terminal — /mnt/redsea/apps is
mounted there as /mnt/apps).

No templating: widget secrets are NOT baked into these files. They stay as
{{HOMEPAGE_VAR_*}} placeholders that Homepage resolves from its own container
environment (populated from the vault .env by the reconciler) at render time.

Homepage hot-reloads config on the next page load, so no restart is needed after
a push (a hard refresh in the browser is enough; restart the app only if a tile
seems stuck).

Usage:
  python scripts/homepage_push_config.py            # push all config files
  python scripts/homepage_push_config.py --dry-run  # list what would be pushed
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "servers/truenas/apps/homepage/config"
REMOTE_DIR = "/mnt/apps/homepage/config"
OT = REPO_ROOT / "scripts/ot.py"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="list files, don't push")
    args = ap.parse_args()

    files = sorted(CONFIG_DIR.glob("*.yaml"))
    if not files:
        sys.exit(f"No *.yaml found under {CONFIG_DIR}")

    for f in files:
        remote = f"{REMOTE_DIR}/{f.name}"
        if args.dry_run:
            print(f"would push {f.relative_to(REPO_ROOT)} -> {remote}")
            continue
        content = f.read_text()
        print(f"Pushing {len(content)} bytes -> {remote}", file=sys.stderr)
        res = subprocess.run([str(OT), "write", remote], input=content, text=True)
        if res.returncode != 0:
            sys.exit(res.returncode)

    if not args.dry_run:
        print("Pushed. Reload https://www.bajaber.ca (hard refresh) to pick up changes.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
