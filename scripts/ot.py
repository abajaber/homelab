#!/usr/bin/env python3
"""Terse CLI over the open-terminal REST API.

Six verbs: ls, cat, write, replace, grep, exec. Each strips the JSON envelope
the API returns so output is cheap to send to an LLM. See the per-verb table
in the project's CLAUDE.md "Web terminal for direct on-box file ops" section.

Auth: looks for OPEN_TERMINAL_URL / OPEN_TERMINAL_API_KEY in env first, then
walks up from CWD looking for a `.open-terminal.env` file (two KEY=VALUE
lines) and uses that. Repo root has the gitignored copy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def load_auth() -> tuple[str, str]:
    url = os.environ.get("OPEN_TERMINAL_URL")
    key = os.environ.get("OPEN_TERMINAL_API_KEY")
    if not (url and key):
        for d in [Path.cwd(), *Path.cwd().parents]:
            f = d / ".open-terminal.env"
            if f.is_file():
                for line in f.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k == "OPEN_TERMINAL_URL" and not url:
                        url = v.strip()
                    elif k == "OPEN_TERMINAL_API_KEY" and not key:
                        key = v.strip()
                break
    if not (url and key):
        sys.exit(
            "ot: missing OPEN_TERMINAL_URL / OPEN_TERMINAL_API_KEY.\n"
            "    Either export them or create .open-terminal.env at repo root:\n"
            "      KEY=$(ansible-vault view servers/truenas/apps/open-terminal/.env "
            "| grep ^OPEN_TERMINAL_API_KEY | cut -d= -f2)\n"
            "      printf 'OPEN_TERMINAL_URL=https://open-terminal.bajaber.ca\\n"
            "OPEN_TERMINAL_API_KEY=%s\\n' \"$KEY\" > .open-terminal.env"
        )
    return url.rstrip("/"), key


def request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    url, key = load_auth()
    full = url + path
    if params:
        full += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, method=method, data=data)
    req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace").strip()
        sys.stderr.write(f"ot: HTTP {e.code} {method} {path}: {msg}\n")
        sys.exit(2)
    except urllib.error.URLError as e:
        sys.stderr.write(f"ot: cannot reach {full}: {e.reason}\n")
        sys.exit(2)


# ----- verbs --------------------------------------------------------------


def cmd_ls(args: argparse.Namespace) -> int:
    r = request("GET", "/files/list", params={"directory": args.directory})
    for e in r.get("entries", []):
        prefix = "d" if e.get("type") == "directory" else "f"
        print(f"{prefix} {e['name']}")
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    r = request("GET", "/files/read", params={"path": args.path})
    sys.stdout.write(r.get("content", ""))
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    content = sys.stdin.read()
    request("POST", "/files/write", body={"path": args.path, "content": content})
    return 0


def cmd_replace(args: argparse.Namespace) -> int:
    request(
        "POST",
        "/files/replace",
        body={
            "path": args.path,
            "replacements": [{"target": args.old, "replacement": args.new}],
        },
    )
    return 0


def cmd_grep(args: argparse.Namespace) -> int:
    r = request("GET", "/files/grep", params={"query": args.query, "path": args.path})
    for m in r.get("matches", []):
        print(f"{m['file']}:{m['line']}:{m['content']}")
    if r.get("truncated"):
        sys.stderr.write("ot: results truncated\n")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    cmd = " ".join(args.cmd)
    started = time.monotonic()
    r = request("POST", "/execute", body={"command": cmd, "timeout": args.timeout})
    pid = r["id"]
    while True:
        if r.get("status") != "running":
            break
        if time.monotonic() - started > args.timeout + 2:
            request("DELETE", f"/execute/{pid}")
            sys.stderr.write(f"ot: timeout after {args.timeout}s\n")
            return 124
        time.sleep(0.3)
        r = request("GET", f"/execute/{pid}/status")
    for chunk in r.get("output", []):
        stream = sys.stderr if chunk.get("type") == "stderr" else sys.stdout
        stream.write(chunk.get("data", ""))
    return r.get("exit_code") or 0


# ----- argparse -----------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="ot", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="verb", required=True)

    s = sub.add_parser("ls"); s.add_argument("directory"); s.set_defaults(fn=cmd_ls)
    s = sub.add_parser("cat"); s.add_argument("path"); s.set_defaults(fn=cmd_cat)
    s = sub.add_parser("write"); s.add_argument("path"); s.set_defaults(fn=cmd_write)
    s = sub.add_parser("replace")
    s.add_argument("path"); s.add_argument("old"); s.add_argument("new")
    s.set_defaults(fn=cmd_replace)
    s = sub.add_parser("grep")
    s.add_argument("query"); s.add_argument("path", nargs="?", default="/mnt/apps")
    s.set_defaults(fn=cmd_grep)
    s = sub.add_parser("exec")
    s.add_argument("--timeout", type=int, default=30)
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_exec)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
