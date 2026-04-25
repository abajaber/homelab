#!/usr/bin/env python3
"""Warn about likely-secret env values in a compose body.

Imports (TrueNAS and Docker VM) write the *rendered* compose verbatim — any
inline secret on the live server lands in the repo cleartext. This script
scans an imported compose for env keys that look like secrets and prints a
loud warning telling the user to extract them into `.env` before commit.

Heuristic: any key in a service's `environment:` block whose name matches
`*_(PASSWORD|SECRET|TOKEN|KEY|API_KEY)` (case-insensitive), where the value
is a literal scalar (not already a `${VAR}` reference). False positives are
fine — the user just leaves them as-is.

Always exits 0. The warning is informational, not blocking.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

_SECRET_NAME = re.compile(
    r"(?i)(?:^|_)(password|passwd|pass|secret|token|key|seed|api_key|apikey|jwt)$"
)


def scan(compose_text: str) -> list[tuple[str, str, str]]:
    """Return [(service, key, value-preview)] for likely secrets."""
    try:
        data = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    services = data.get("services") or {}
    if not isinstance(services, dict):
        return []
    findings: list[tuple[str, str, str]] = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        items: list[tuple[str, object]] = []
        if isinstance(env, dict):
            items = list(env.items())
        elif isinstance(env, list):
            for entry in env:
                if isinstance(entry, str) and "=" in entry:
                    k, v = entry.split("=", 1)
                    items.append((k, v))
        for k, v in items:
            if not isinstance(k, str) or not _SECRET_NAME.search(k):
                continue
            if not isinstance(v, (str, int, float)):
                continue
            sval = str(v)
            # Already a placeholder — skip.
            if "${" in sval or sval.startswith("$") and not sval.startswith("$$"):
                continue
            preview = sval if len(sval) <= 8 else sval[:4] + "…" + sval[-2:]
            findings.append((svc_name, k, preview))
    return findings


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: scan_compose_secrets.py <compose.yml> [<compose.yml> ...]", file=sys.stderr)
        return 2
    any_findings = False
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        findings = scan(path.read_text())
        if not findings:
            continue
        any_findings = True
        print(f"! likely secrets in {path}:")
        for svc, key, preview in findings:
            print(f"    {svc}.environment.{key} = {preview}")
    if any_findings:
        print(
            "\n  Extract these into a `.env` next to the compose, replace each value with",
            "\n  ${VAR_NAME} in compose.yml, then `ansible-vault encrypt <path>/.env`",
            "\n  before committing.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
