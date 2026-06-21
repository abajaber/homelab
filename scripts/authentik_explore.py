#!/usr/bin/env python3
"""Read-only dump of the live Authentik instance.

Authentik keeps all auth state (users, groups, applications, providers, flows,
outposts) in its postgres DB — nothing in this repo. This script queries the
`/api/v3/` REST API and prints a concise summary so we can see the current
state before deciding what to manage as code.

Read-only: only issues GET requests.

Auth: pass `--token` or set AUTHENTIK_API_TOKEN in the environment. Mint a token
in the UI (Directory -> Tokens, or a service-account token).

Follows the repo's wire-script conventions: DNS-overrides *.bajaber.ca to the
TrueNAS IP (so it works off-LAN / without public DNS) and skips TLS verify.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from typing import Any

import requests
import urllib3

TRUENAS_IP = "192.168.1.138"
HOSTS = {"auth.bajaber.ca"}
_orig = socket.getaddrinfo


def _ovr(host, *a, **kw):
    if host in HOSTS:
        return _orig(TRUENAS_IP, *a, **kw)
    return _orig(host, *a, **kw)


socket.getaddrinfo = _ovr
urllib3.disable_warnings()


def get_all(s: requests.Session, base: str, path: str) -> list[dict[str, Any]]:
    """GET every page of a paginated Authentik list endpoint."""
    out: list[dict[str, Any]] = []
    url = f"{base}/api/v3/{path}"
    params = {"page_size": 200}
    while url:
        r = s.get(url, params=params, timeout=30, verify=False)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("results", []))
        nxt = body.get("pagination", {}).get("next")
        # `next` is a page number in Authentik; fall back to None when 0/absent.
        if nxt:
            params["page"] = nxt
        else:
            url = None
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://auth.bajaber.ca")
    ap.add_argument(
        "--token",
        default=os.environ.get("AUTHENTIK_API_TOKEN"),
        help="Authentik API token (or set AUTHENTIK_API_TOKEN)",
    )
    args = ap.parse_args()

    if not args.token:
        sys.exit("No token: pass --token or set AUTHENTIK_API_TOKEN")

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {args.token}"
    base = args.url.rstrip("/")

    # Sanity check + identity.
    me = s.get(f"{base}/api/v3/core/users/me/", timeout=30, verify=False)
    if me.status_code == 403:
        sys.exit("403: token rejected (expired, wrong scope, or not an API token)")
    me.raise_for_status()
    ident = me.json().get("user", {})
    print(f"Authenticated as: {ident.get('username')} (pk={ident.get('pk')})\n")

    users = get_all(s, base, "core/users/")
    groups = get_all(s, base, "core/groups/")
    apps = get_all(s, base, "core/applications/")
    providers = get_all(s, base, "providers/all/")
    flows = get_all(s, base, "flows/instances/")
    outposts = get_all(s, base, "outposts/instances/")

    print(f"USERS ({len(users)}):")
    for u in sorted(users, key=lambda x: x.get("username", "")):
        active = "" if u.get("is_active") else " [inactive]"
        gs = ", ".join(u.get("groups_obj_names", []) or [])
        print(f"  - {u.get('username'):<24} {u.get('email', ''):<32} groups: {gs}{active}")

    print(f"\nGROUPS ({len(groups)}):")
    for g in sorted(groups, key=lambda x: x.get("name", "")):
        su = " [superuser]" if g.get("is_superuser") else ""
        print(f"  - {g.get('name'):<28} members: {g.get('num_pk', '?')}{su}")

    print(f"\nAPPLICATIONS ({len(apps)}):")
    for a in sorted(apps, key=lambda x: x.get("slug", "")):
        print(f"  - {a.get('slug'):<28} provider: {a.get('provider_obj', {}).get('name') if a.get('provider_obj') else a.get('provider')}")

    print(f"\nPROVIDERS ({len(providers)}):")
    for p in sorted(providers, key=lambda x: x.get("name", "")):
        print(f"  - {p.get('name'):<28} type: {p.get('component', '')}")

    print(f"\nFLOWS ({len(flows)}):")
    for f in sorted(flows, key=lambda x: x.get("slug", "")):
        print(f"  - {f.get('slug'):<32} {f.get('designation', '')}")

    print(f"\nOUTPOSTS ({len(outposts)}):")
    for o in outposts:
        print(f"  - {o.get('name'):<28} providers: {len(o.get('providers', []))}")


if __name__ == "__main__":
    main()
