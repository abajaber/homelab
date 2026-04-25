#!/usr/bin/env python3
"""Reconcile TrueNAS Scale Custom Apps with the definitions in this repo.

The TrueNAS 25.x JSON-RPC API doesn't expose a writable description/notes
field, so the `managed-by:` marker is embedded as a Compose extension field
(top-level `x-homelab`) on the compose body itself:

    x-homelab:
      managed-by: homelab-repo
      fingerprint: <12-char sha256 of the repo compose>
    services:
      ...

Top-level `x-` keys are reserved by the Compose spec for arbitrary metadata,
so Docker ignores them. Apps without that marker are considered "not ours"
and are left alone.

Auth uses the TRUENAS_API_KEY env var.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from truenas_client import TruenasClient, TruenasError  # noqa: E402
from dotenv import parse as parse_dotenv, substitute as substitute_env  # noqa: E402


EXT_KEY = "x-homelab"
VAULT_HEADER = b"$ANSIBLE_VAULT;"


def _read_env_for_app(app_dir: Path, vault_password_file: Path | None) -> dict[str, str]:
    """Return parsed env for `app_dir/.env`, or {} if no file.

    If the file is vault-encrypted, decrypt with `vault_password_file`. We use
    ansible's own VaultLib so the script doesn't shell out — it's already in
    the venv. Cleartext files are accepted (the apply-time check in the
    playbook is the gate that prevents shipping them; the script itself stays
    permissive so it's runnable standalone for debugging).
    """
    env_path = app_dir / ".env"
    if not env_path.is_file():
        return {}
    raw = env_path.read_bytes()
    if raw.startswith(VAULT_HEADER):
        if vault_password_file is None or not vault_password_file.is_file():
            raise SystemExit(
                f"{env_path} is vault-encrypted but no --vault-password-file was provided"
            )
        from ansible.parsing.vault import VaultLib, VaultSecret  # type: ignore

        secret = vault_password_file.read_bytes().strip()
        vault = VaultLib([(b"default", VaultSecret(secret))])
        try:
            text = vault.decrypt(raw).decode("utf-8")
        except Exception as e:  # ansible raises AnsibleError; we don't want to import it
            raise SystemExit(f"failed to decrypt {env_path}: {e}")
    else:
        text = raw.decode("utf-8")
    return parse_dotenv(text)


def stamp(compose_text: str, managed_by: str, fingerprint: str) -> str:
    """Return compose YAML with the x-homelab marker prepended."""
    data = yaml.safe_load(compose_text) or {}
    if not isinstance(data, dict):
        raise ValueError("compose root must be a mapping")
    out: dict = {EXT_KEY: {"managed-by": managed_by, "fingerprint": fingerprint}}
    for k, v in data.items():
        if k == EXT_KEY:
            continue
        out[k] = v
    return yaml.safe_dump(out, default_flow_style=False, sort_keys=False)


def _to_dict(compose) -> dict | None:
    """Accept a YAML string or an already-parsed dict and return a dict (or None)."""
    if isinstance(compose, dict):
        return compose
    if isinstance(compose, str):
        try:
            data = yaml.safe_load(compose) or {}
        except yaml.YAMLError:
            return None
        return data if isinstance(data, dict) else None
    return None


def read_marker(compose, managed_by: str) -> str | None:
    """Return the fingerprint embedded in the marker, or None if not ours.

    `compose` may be a YAML string OR a parsed dict — the TrueNAS API returns
    the latter directly from app.config().
    """
    data = _to_dict(compose)
    if data is None:
        return None
    ext = data.get(EXT_KEY) or {}
    if not isinstance(ext, dict):
        return None
    if ext.get("managed-by") != managed_by:
        return None
    fp = ext.get("fingerprint")
    return str(fp) if fp else None


def strip_marker(compose) -> str:
    """Return compose YAML with the x-homelab marker removed (for repo files).

    Accepts a string or a dict. Returns YAML text. If the input parses but has
    no marker, the YAML is canonicalized; if it doesn't parse, the original
    string is returned untouched.
    """
    if isinstance(compose, str):
        data = _to_dict(compose)
        if data is None:
            return compose
    elif isinstance(compose, dict):
        data = compose
    else:
        return ""
    out = {k: v for k, v in data.items() if k != EXT_KEY}
    return yaml.safe_dump(out, default_flow_style=False, sort_keys=False)


@dataclass
class DesiredApp:
    name: str
    compose: str          # raw YAML string, as committed in the repo
    folders: list[str]    # paths to ensure exist before create/update

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.compose.encode("utf-8")).hexdigest()[:12]

    def stamped_compose(self, managed_by: str) -> str:
        return stamp(self.compose, managed_by, self.fingerprint)


def load_desired(apps_dir: Path, vault_password_file: Path | None = None) -> list[DesiredApp]:
    out: list[DesiredApp] = []
    if not apps_dir.is_dir():
        return out
    for app_dir in sorted(p for p in apps_dir.iterdir() if p.is_dir()):
        app_yml = app_dir / "app.yml"
        compose_yml = app_dir / "compose.yml"
        if not (app_yml.is_file() and compose_yml.is_file()):
            continue
        meta = yaml.safe_load(app_yml.read_text()) or {}
        if meta.get("enabled", True) is False:
            continue
        name = meta.get("name", app_dir.name)
        compose = compose_yml.read_text()
        env = _read_env_for_app(app_dir, vault_password_file)
        if env:
            compose, unresolved = substitute_env(compose, env)
            if unresolved:
                print(
                    f"  ! warning: {name} has unresolved compose vars: {unresolved}",
                    file=sys.stderr,
                )
        folders = list(meta.get("folders") or [])
        out.append(DesiredApp(name=name, compose=compose, folders=folders))
    return out


def resolve_folder(raw: str, dataset_root: str, app_name: str) -> str:
    """Absolute path on the TrueNAS box for an app.yml `folders:` entry.

    Absolute paths are taken verbatim; relative ones are anchored under
    `/mnt/<dataset_root>/<app_name>/`.
    """
    if raw.startswith("/"):
        return raw.rstrip("/")
    return f"/mnt/{dataset_root}/{app_name}/{raw}".rstrip("/")


def discover_folders(compose, dataset_root: str, app_name: str) -> list[str]:
    """Reverse-engineer an app.yml `folders:` list from a compose dict.

    Walks every service's `volumes:` and pulls bind-mount source paths.
    Paths under `/mnt/<dataset_root>/<app_name>/` are returned relative;
    other host paths under `/mnt/` are returned absolute. Named volumes
    and tmpfs mounts are ignored.
    """
    data = _to_dict(compose)
    if data is None:
        return []
    base = f"/mnt/{dataset_root}/{app_name}"
    found: set[str] = set()
    services = data.get("services") or {}
    if not isinstance(services, dict):
        return []
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            src: str | None = None
            if isinstance(v, str):
                # short form: "src:dst[:opts]"; only host paths start with /
                head = v.split(":", 1)[0]
                if head.startswith("/"):
                    src = head
            elif isinstance(v, dict) and v.get("type") == "bind":
                s = v.get("source")
                if isinstance(s, str) and s.startswith("/"):
                    src = s
            if not src:
                continue
            src = src.rstrip("/")
            if src == base or src.startswith(f"{base}/"):
                rel = src[len(base):].lstrip("/")
                if rel:
                    found.add(rel)
            elif src.startswith("/mnt/"):
                found.add(src)
    return sorted(found)


def ensure_folder(client: TruenasClient, path: str) -> bool:
    """Ensure `path` exists on the box, creating parent dirs as needed.

    Returns True if anything was created. Refuses to touch paths outside
    /mnt/ as a safety rail. Newly-created folders get apps:apps 770; existing
    ones are left alone.
    """
    if not path.startswith("/mnt/"):
        raise ValueError(f"refusing to create {path}: must be under /mnt/")
    if client.fs_stat(path) is not None:
        return False
    parent = path.rsplit("/", 1)[0]
    if parent and parent != path:
        ensure_folder(client, parent)
    client.fs_mkdir(path)
    try:
        client.fs_setperm(path, user=APPS_USER, group=APPS_GROUP, mode=APPS_MODE)
    except TruenasError as e:
        print(f"  ! warning: could not setperm on {path}: {e}", file=sys.stderr)
    return True


# Default ownership applied to *newly-created* datasets and folders. Existing
# resources are left alone so any sidecar permissions fixup the user runs in
# their compose isn't clobbered by the next reconcile.
APPS_USER = "apps"
APPS_GROUP = "apps"
APPS_MODE = "770"


def ensure_dataset(client: TruenasClient, name: str) -> bool:
    """Idempotent dataset create. Returns True if anything was created.

    On fresh creation, the dataset's mountpoint is set to apps:apps 770 so
    Custom Apps can write to it without needing a permissions sidecar.
    """
    if client.dataset_query(name) is not None:
        return False
    client.dataset_create(name)
    try:
        client.fs_setperm(
            f"/mnt/{name}", user=APPS_USER, group=APPS_GROUP, mode=APPS_MODE
        )
    except TruenasError as e:
        print(f"  ! warning: could not setperm on {name}: {e}", file=sys.stderr)
    return True


def required_resources(
    app: "DesiredApp", dataset_root: str
) -> tuple[set[str], list[str]]:
    """Compute (datasets, folders) needed to deploy `app`.

    Folders come from two sources, deduped:
      1. bind mounts in the compose body that point under
         /mnt/<dataset_root>/<app.name>/...  (auto)
      2. extra entries in app.yml's `folders:` list (manual override)

    A dataset at `<dataset_root>/<app.name>` is required iff at least one
    folder is *relative* (i.e. lives under that base). Bind mounts that point
    elsewhere under /mnt/ are still mkdir'd as folders, but no dataset is
    auto-created at that location.
    """
    discovered = discover_folders(app.compose, dataset_root, app.name)
    seen: set[str] = set()
    merged: list[str] = []
    for raw in [*discovered, *app.folders]:
        if raw not in seen:
            seen.add(raw)
            merged.append(raw)

    datasets: set[str] = set()
    if any(not f.startswith("/") for f in merged):
        datasets.add(f"{dataset_root}/{app.name}")

    return datasets, merged


def ensure_app_resources(
    client: TruenasClient,
    app: "DesiredApp",
    dataset_root: str,
) -> tuple[list[str], list[str]]:
    """Create missing datasets, then missing folders. Returns (datasets, folders) created."""
    datasets, folders = required_resources(app, dataset_root)
    created_ds: list[str] = []
    for ds in sorted(datasets):
        if ensure_dataset(client, ds):
            created_ds.append(ds)
    created_f: list[str] = []
    for raw in folders:
        target = resolve_folder(raw, dataset_root, app.name)
        if ensure_folder(client, target):
            created_f.append(target)
    return created_ds, created_f


def fetch_apps(
    client: TruenasClient, managed_by: str
) -> dict[str, str | None]:
    """Return {name: fingerprint-or-None} for every Custom App on the server.

    `None` means the app exists but doesn't carry our marker — it's an
    adoption candidate (next sync will stamp it).
    """
    out: dict[str, str | None] = {}
    for app in client.app_query():
        if not app.get("custom_app"):
            continue
        name = app.get("name") or app.get("id")
        if not name:
            continue
        cfg = client.app_config(name)
        out[name] = read_marker(cfg, managed_by)
    return out


def diff(
    desired: list[DesiredApp],
    actual: dict[str, str | None],
) -> tuple[list[DesiredApp], list[DesiredApp], list[DesiredApp], list[str], list[str]]:
    """Return (to_create, to_update, to_adopt, to_delete, unchanged).

    - to_create: in repo, not on server.
    - to_update: in repo, on server with our marker, fingerprints differ.
    - to_adopt:  in repo, on server WITHOUT our marker. Push the repo compose
                 stamped with the marker so the next sync owns it.
    - to_delete: on server with our marker, not in repo.
    - unchanged: in repo, on server with our marker, same fingerprint.

    Apps on the server WITHOUT a marker that aren't in the repo are simply
    invisible (the reconciler never touches them).
    """
    desired_by_name = {a.name: a for a in desired}

    to_create: list[DesiredApp] = []
    to_update: list[DesiredApp] = []
    to_adopt: list[DesiredApp] = []
    unchanged: list[str] = []
    for name, want in desired_by_name.items():
        if name not in actual:
            to_create.append(want)
            continue
        actual_fp = actual[name]
        if actual_fp is None:
            to_adopt.append(want)
        elif actual_fp != want.fingerprint:
            to_update.append(want)
        else:
            unchanged.append(name)

    to_delete = [
        name for name, fp in actual.items()
        if name not in desired_by_name and fp is not None
    ]

    return to_create, to_update, to_adopt, to_delete, unchanged


def report(host: str, mode: str, to_create, to_update, to_adopt, to_delete, unchanged) -> None:
    print(f"host: {host}  mode: {mode}")
    print(f"+ to-create: {[a.name for a in to_create]}")
    print(f"~ to-update: {[a.name for a in to_update]}")
    print(f"@ to-adopt:  {[a.name for a in to_adopt]}")
    print(f"- to-delete: {to_delete}")
    print(f"= unchanged: {unchanged}")


def _canonical_yaml(d) -> str:
    """Same dump options as stamp() so live and desired diff cleanly."""
    if not isinstance(d, dict) or not d:
        return ""
    return yaml.safe_dump(d, default_flow_style=False, sort_keys=False)


def _print_unified_diff(name: str, current: str, desired: str, marker: str) -> None:
    print(f"  {marker} {name}:")
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        desired.splitlines(keepends=True),
        fromfile=f"live/{name}",
        tofile=f"repo/{name}",
        n=3,
    )
    any_lines = False
    for line in diff:
        any_lines = True
        print(f"    {line.rstrip()}")
    if not any_lines:
        print(f"    (no textual change — server side already matches stamped repo body)")


def show_diffs(
    client: TruenasClient,
    to_create,
    to_update,
    to_adopt,
    to_delete,
    managed_by: str,
) -> None:
    """Print per-app diffs for everything that would change in apply mode.

    Compares the *live* compose (as the server returns it) against the
    *stamped* repo compose (what apply would actually push). For adoption,
    that means the diff shows the x-homelab block being added at the top.
    """
    if not (to_create or to_update or to_adopt or to_delete):
        return
    print("--- diff ---")
    for app in to_create:
        print(f"  + {app.name}: would create with this compose:")
        for line in app.stamped_compose(managed_by).splitlines():
            print(f"    + {line}")
    for app in (*to_update, *to_adopt):
        cfg = client.app_config(app.name)
        current = _canonical_yaml(cfg)
        desired = app.stamped_compose(managed_by)
        marker = "~" if app in to_update else "@"
        _print_unified_diff(app.name, current, desired, marker)
    for name in to_delete:
        cfg = client.app_config(name)
        live = _canonical_yaml(cfg)
        print(f"  - {name}: would delete (current compose):")
        lines = live.splitlines()
        for line in lines[:40]:
            print(f"    - {line}")
        if len(lines) > 40:
            print(f"    - ... ({len(lines) - 40} more lines)")


def apply(
    client: TruenasClient,
    to_create,
    to_update,
    to_adopt,
    to_delete,
    managed_by: str,
    dataset_root: str,
) -> int:
    changes = 0
    for app in (*to_create, *to_update, *to_adopt):
        ds_created, fs_created = ensure_app_resources(client, app, dataset_root)
        for ds in ds_created:
            print(f"  + dataset {ds}")
            changes += 1
        for path in fs_created:
            print(f"  + folder {path}")
            changes += 1
    for app in to_create:
        print(f"  creating {app.name} ...")
        client.app_create(name=app.name, compose_yaml=app.stamped_compose(managed_by))
        changes += 1
    for app in to_update:
        print(f"  updating {app.name} ...")
        client.app_update(name=app.name, compose_yaml=app.stamped_compose(managed_by))
        changes += 1
    for app in to_adopt:
        print(f"  adopting {app.name} ...")
        client.app_update(name=app.name, compose_yaml=app.stamped_compose(managed_by))
        changes += 1
    for name in to_delete:
        print(f"  deleting {name} ...")
        client.app_delete(name=name)
        changes += 1
    return changes


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apps-dir", required=True, type=Path)
    p.add_argument("--api-url", required=True, help="wss://host/api/<version>")
    p.add_argument("--api-user", default="admin", help="username the API key belongs to")
    p.add_argument(
        "--dataset-root",
        default="tank/apps",
        help="ZFS path that anchors per-app folders, e.g. tank/apps -> /mnt/tank/apps/<name>/",
    )
    p.add_argument("--mode", choices=["plan", "apply"], default="plan")
    p.add_argument("--managed-by", default="homelab-repo")
    p.add_argument(
        "--show-diff",
        action="store_true",
        help="in plan mode, print a unified diff per app that would change",
    )
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    p.add_argument("--host", default="truenas")
    p.add_argument(
        "--vault-password-file",
        type=Path,
        default=REPO_ROOT / ".vault-password",
        help="ansible-vault password file used to decrypt per-app .env (default: .vault-password at repo root)",
    )
    args = p.parse_args()

    api_key = os.environ.get("TRUENAS_API_KEY")
    if not api_key:
        print("TRUENAS_API_KEY env var is required", file=sys.stderr)
        return 2

    desired = load_desired(args.apps_dir, args.vault_password_file)
    try:
        with TruenasClient(
            args.api_url,
            api_key=api_key,
            username=args.api_user,
            verify_tls=not args.insecure,
        ) as client:
            actual = fetch_apps(client, args.managed_by)
            to_create, to_update, to_adopt, to_delete, unchanged = diff(desired, actual)
            report(
                args.host, args.mode,
                to_create, to_update, to_adopt, to_delete, unchanged,
            )

            # Show datasets and folders that would be created, for visibility.
            if args.mode == "plan":
                pending_ds: list[str] = []
                pending_fs: list[str] = []
                for app in (*to_create, *to_update, *to_adopt):
                    datasets, folders = required_resources(app, args.dataset_root)
                    for ds in sorted(datasets):
                        if client.dataset_query(ds) is None:
                            pending_ds.append(ds)
                    for raw in folders:
                        path = resolve_folder(raw, args.dataset_root, app.name)
                        if client.fs_stat(path) is None:
                            pending_fs.append(path)
                if pending_ds:
                    print(f"+ datasets-to-create: {pending_ds}")
                if pending_fs:
                    print(f"+ folders-to-create:  {pending_fs}")

                if args.show_diff:
                    show_diffs(
                        client, to_create, to_update, to_adopt, to_delete, args.managed_by
                    )

            drift = bool(to_create or to_update or to_adopt or to_delete)
            if args.mode == "plan":
                print(f"changed={'true' if drift else 'false'}")
                return 0

            n = apply(
                client, to_create, to_update, to_adopt, to_delete,
                args.managed_by, args.dataset_root,
            )
            print(f"changed={'true' if n > 0 else 'false'}")
            return 0
    except TruenasError as e:
        print(f"truenas error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
