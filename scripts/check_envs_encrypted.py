#!/usr/bin/env python3
"""Refuse to proceed if any tracked `.env` is committed in cleartext.

Used in two places:
  - `.githooks/pre-commit` — runs against staged `*.env` paths so a fumbled
    `git commit` of an unencrypted secrets file never reaches GitHub.
  - apply-time pre-task in the playbooks — runs against the whole apps tree
    so a local edit can't push secrets to a server in cleartext either.

A vault-encrypted file starts with `$ANSIBLE_VAULT;<version>;<cipher>`.
Anything else is treated as cleartext and reported.

`*.example` files are deliberately ignored — they document the dotenv format
with placeholder values and are meant to be cleartext.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


VAULT_HEADER = "$ANSIBLE_VAULT;"


def is_vault_encrypted(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(len(VAULT_HEADER))
    except OSError:
        return False
    try:
        return head.decode("ascii") == VAULT_HEADER
    except UnicodeDecodeError:
        return False


def find_env_files(root: Path) -> list[Path]:
    """All `.env` files under `root`, excluding `.env.example` siblings."""
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.name == ".env" else []
    return sorted(p for p in root.rglob(".env") if p.is_file())


def check(paths: list[Path]) -> list[Path]:
    return [p for p in paths if not is_vault_encrypted(p)]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories to check; directories are walked for `.env` files",
    )
    args = p.parse_args()

    if not args.paths:
        print("usage: check_envs_encrypted.py <path> [<path> ...]", file=sys.stderr)
        return 2

    targets: list[Path] = []
    for p_arg in args.paths:
        if p_arg.is_dir():
            targets.extend(find_env_files(p_arg))
        elif p_arg.is_file():
            if p_arg.name == ".env":
                targets.append(p_arg)
        # Silently skip non-existent paths — a `git diff --cached --name-only`
        # may list deleted files, and a hook shouldn't blow up on those.

    offenders = check(targets)
    if offenders:
        print("error: the following .env files are NOT vault-encrypted:", file=sys.stderr)
        for o in offenders:
            print(f"  {o}", file=sys.stderr)
        print(
            "\nencrypt them with:  ansible-vault encrypt <path>",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
