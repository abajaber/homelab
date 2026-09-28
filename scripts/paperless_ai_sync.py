#!/usr/bin/env python3
"""Reconcile the paperless AI pipeline state that compose.yml can't hold.

The compose body defines the containers. Three more pieces live in app state
and would otherwise be click-ops:

1. Paperless-ngx: the paperless-gpt trigger tags and the "AI pipeline"
   workflow that puts them on every new document.
2. paperless-gpt: the prompt templates under
   servers/truenas/apps/paperless-ngx/gpt-prompts/, posted to its
   /api/prompts endpoint, which validates and hot-reloads each one.
3. paperless-ai: its first-run setup, which writes /app/data/.env and creates
   the login user. Compose env overrides every value that file holds, so this
   runs once and never again.

paperless-gpt and paperless-ai have no public API route (Authentik gates both),
so those calls run inside the cluster through scripts/ot.py.

Idempotent. Plan by default:

    set -a; eval "$(ansible-vault view servers/truenas/apps/paperless-ngx/.env)"; set +a
    python scripts/paperless_ai_sync.py
    python scripts/paperless_ai_sync.py --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import urllib3

try:
    import requests
except ImportError:
    sys.stderr.write("requests not installed; activate the repo venv first\n")
    sys.exit(2)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = Path(__file__).resolve().parent.parent
APP_DIR = REPO / "servers" / "truenas" / "apps" / "paperless-ngx"
OT = REPO / "scripts" / "ot.py"

PAPERLESS_HOST = "paperless.bajaber.ca"
TRAEFIK_IP = "192.168.1.138"
GPT_URL = "http://paperless-gpt:8080"
AI_URL = "http://paperless-ai:3000"
AI_ENV_PATH = "/mnt/apps/paperless-ngx/ai-data/.env"

# Matching "none" keeps paperless' own auto-matcher from ever assigning these;
# only the workflow and paperless-gpt may.
TRIGGER_TAGS = [
    "paperless-gpt-ocr-auto",
    "paperless-gpt-auto",
    "paperless-gpt-ocr-complete",
    "paperless-gpt-manual",
]
WORKFLOW_NAME = "AI pipeline"
WORKFLOW_TAGS = ["paperless-gpt-ocr-auto", "paperless-gpt-auto"]
TRIGGER_DOCUMENT_ADDED = 2
ACTION_ASSIGNMENT = 1
MATCH_NONE = 0

AI_USERNAME = "abajaber"
AI_MODEL = "qwen3:4b-instruct-2507-q4_K_M"


def pin_dns() -> None:
    """The workstation has no split DNS for *.bajaber.ca; Traefik routes by Host."""
    real = socket.getaddrinfo

    def patched(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        return real(TRAEFIK_IP if host == PAPERLESS_HOST else host, *args, **kwargs)

    socket.getaddrinfo = patched  # type: ignore[assignment]


def in_cluster(method: str, url: str, body: Any = None, timeout: int = 60) -> Any:
    """curl a cluster-internal URL from the open-terminal container.

    The body travels base64-encoded because ot.py write escapes "!" and shell
    quoting mangles multi-line prompts.
    """
    cmd = f"curl -sS -m {timeout} -X {method} {shlex.quote(url)}"
    if body is not None:
        b64 = base64.b64encode(json.dumps(body).encode()).decode()
        cmd = f"echo {b64} | base64 -d | {cmd} -H 'Content-Type: application/json' --data-binary @-"
    res = subprocess.run(
        [str(OT), "exec", "--timeout", str(timeout + 10), cmd],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"{method} {url} failed: {res.stderr.strip() or res.stdout.strip()}")
    out = res.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


class Paperless:
    def __init__(self, token: str):
        self.base = f"https://{PAPERLESS_HOST}/api"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Token {token}"
        self.s.verify = False

    def all(self, path: str) -> list[dict]:
        out, url = [], f"{self.base}/{path}/?page_size=100"
        while url:
            r = self.s.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
            out += data["results"]
            url = data.get("next")
        return out

    def post(self, path: str, body: dict) -> dict:
        r = self.s.post(f"{self.base}/{path}/", json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"POST {path}: {r.status_code} {r.text}")
        return r.json()

    def patch(self, path: str, body: dict) -> dict:
        r = self.s.patch(f"{self.base}/{path}/", json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"PATCH {path}: {r.status_code} {r.text}")
        return r.json()


def sync_tags(p: Paperless, apply: bool) -> tuple[int, dict[str, int]]:
    changes = 0
    tags = {t["name"]: t for t in p.all("tags")}
    for name in TRIGGER_TAGS:
        tag = tags.get(name)
        if tag is None:
            print(f"+ tag {name}")
            changes += 1
            if apply:
                tags[name] = p.post("tags", {"name": name, "matching_algorithm": MATCH_NONE})
        elif tag["matching_algorithm"] != MATCH_NONE:
            print(f"~ tag {name}: matching_algorithm {tag['matching_algorithm']} -> none")
            changes += 1
            if apply:
                p.patch(f"tags/{tag['id']}", {"matching_algorithm": MATCH_NONE})
    return changes, {n: t["id"] for n, t in tags.items() if "id" in t}


def sync_workflow(p: Paperless, tag_ids: dict[str, int], apply: bool) -> int:
    want_tags = sorted(tag_ids.get(n, -1) for n in WORKFLOW_TAGS)
    existing = next((w for w in p.all("workflows") if w["name"] == WORKFLOW_NAME), None)
    if existing is None:
        print(f"+ workflow {WORKFLOW_NAME!r}: on document added, assign {', '.join(WORKFLOW_TAGS)}")
        if apply:
            p.post("workflows", {
                "name": WORKFLOW_NAME,
                "order": 0,
                "enabled": True,
                "triggers": [{"type": TRIGGER_DOCUMENT_ADDED, "sources": [1, 2, 3, 4],
                              "matching_algorithm": MATCH_NONE}],
                "actions": [{"type": ACTION_ASSIGNMENT, "assign_tags": want_tags}],
            })
        return 1

    triggers_ok = [t["type"] for t in existing["triggers"]] == [TRIGGER_DOCUMENT_ADDED]
    actions_ok = (
        len(existing["actions"]) == 1
        and sorted(existing["actions"][0].get("assign_tags") or []) == want_tags
    )
    if existing["enabled"] and triggers_ok and actions_ok:
        return 0
    # Rewriting nested triggers/actions in place is brittle across paperless
    # versions; report it and let a human fix or delete the workflow.
    print(f"! workflow {WORKFLOW_NAME!r} exists but differs; delete it in the UI and re-run")
    return 0


def sync_prompts(apply: bool) -> int:
    live = in_cluster("GET", f"{GPT_URL}/api/prompts")
    if not isinstance(live, dict):
        raise RuntimeError(f"paperless-gpt /api/prompts returned {live!r}")
    changes = 0
    for f in sorted((APP_DIR / "gpt-prompts").glob("*.tmpl")):
        content = f.read_text()
        if live.get(f.name) == content:
            continue
        print(f"~ prompt {f.name}")
        changes += 1
        if apply:
            res = in_cluster("POST", f"{GPT_URL}/api/prompts", {"filename": f.name, "content": content})
            if not (isinstance(res, dict) and "message" in res):
                raise RuntimeError(f"prompt {f.name}: {res}")
    return changes


def sync_ai_setup(token: str, apply: bool) -> int:
    res = subprocess.run([str(OT), "cat", AI_ENV_PATH], capture_output=True, text=True)
    if "PAPERLESS_API_URL=" in res.stdout:
        return 0
    print(f"+ paperless-ai first-run setup (user {AI_USERNAME}, model {AI_MODEL})")
    if not apply:
        return 1
    password = os.environ.get("PAPERLESS_AI_PASSWORD")
    if not password:
        sys.exit("PAPERLESS_AI_PASSWORD not set; load the paperless-ngx vault .env first")
    # Everything but the URL, token, and login is overridden by compose env, so
    # these are only here to pass the setup form's validation.
    res = in_cluster("POST", f"{AI_URL}/setup", {
        "paperlessUrl": "http://paperless:8000",
        "paperlessToken": token,
        "paperlessUsername": AI_USERNAME,
        "aiProvider": "ollama",
        "ollamaUrl": "http://ollama:11434",
        "ollamaModel": AI_MODEL,
        "username": AI_USERNAME,
        "password": password,
        "useExistingData": "yes",
        "activateTagging": True,
    }, timeout=120)
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError(f"paperless-ai setup: {res}")
    print("  paperless-ai saved its config and is restarting")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: plan only)")
    args = ap.parse_args()

    token = os.environ.get("PAPERLESS_API_TOKEN")
    if not token:
        sys.exit("PAPERLESS_API_TOKEN not set; load the paperless-ngx vault .env first")
    pin_dns()
    p = Paperless(token)

    total, tag_ids = sync_tags(p, args.apply)
    total += sync_workflow(p, tag_ids, args.apply)
    total += sync_prompts(args.apply)
    total += sync_ai_setup(token, args.apply)

    print(f"\n{total} change(s) {'applied' if args.apply else 'planned'}")
    if total and not args.apply:
        print("re-run with --apply to write")
    elif not total:
        print("= in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
