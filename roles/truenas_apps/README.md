# role: truenas_apps

Reconciles TrueNAS Scale Custom Apps (25.x line, Docker-based) with the definitions under `servers/truenas/apps/`.

The TrueNAS API switched from REST (`/api/v2.0/*`) to JSON-RPC over WebSocket (`wss://<host>/api/v25.x.x`) in the 25.x line. The client (`scripts/truenas_client.py`) speaks JSON-RPC and is version-pinned via `truenas_api_version` in `servers/truenas/vars.yml`.

## What "managed" means

Every app this role creates has a marker injected at the top of its compose body as a Compose extension field:

```yaml
x-homelab:
  managed-by: homelab-repo
  fingerprint: <12-char sha256 of the repo compose>
services:
  ...
```

Top-level `x-` keys are reserved by the Compose spec for arbitrary metadata, so Docker ignores them.

When the role runs, it queries `app.config(name)` for each Custom App, parses the YAML, and reads `x-homelab.managed-by` / `x-homelab.fingerprint`. Apps without that marker are considered "not ours" and are left alone — apps you installed manually through the TrueNAS UI are safe.

The marker lives in the compose because TrueNAS 25.x dropped the writable description/notes field that older versions exposed; the compose body is the only round-trippable place for repo metadata.

## Per-app layout

```
servers/truenas/apps/<name>/
├── app.yml        # name (defaults to dirname), enabled flag
└── compose.yml    # docker-compose body, embedded as a Custom App
```

`app.yml` fields:

| field | default | meaning |
|---|---|---|
| `name` | dirname | the TrueNAS app name |
| `enabled` | `true` | set to `false` to keep the dir but skip syncing |
| `folders` | `[]` | extra host paths to ensure exist on top of what's auto-discovered from the compose body — relative entries anchor under `/mnt/<truenas_dataset_root>/<name>/`, absolute entries are verbatim. Refuses to touch anything outside `/mnt/`. |

## Auto-creation of datasets and folders

Before each `app.create` / `app.update`, the reconciler:

1. Walks every bind-mount source path in the compose body (`services.<svc>.volumes`).
2. For any path under `/mnt/<truenas_dataset_root>/<name>/...`:
   - Ensures a ZFS dataset exists at `<truenas_dataset_root>/<name>` (`pool.dataset.create` if missing).
   - Ensures each subfolder exists (`filesystem.mkdir`, parents created as needed).
3. For any host path under `/mnt/` *outside* that base: only the folder is created — no dataset auto-creation, since TrueNAS or you own those locations.

Both operations are idempotent. Existing datasets and folders are left alone.

### One dataset per app

Granularity is intentionally one dataset per app: `<truenas_dataset_root>/<app_name>`. Backing up an app is then a single `zfs send` of that dataset (or one snapshot policy entry). All sub-paths — `data`, `config`, `database`, `postgres/pgdata`, etc. — are folders inside the per-app dataset, not separate datasets.

If you import an app whose original layout used per-subfolder datasets (TrueNAS catalog apps sometimes do this), those existing sub-datasets are left intact: `dataset_query` finds them, `ensure_dataset` skips them, `mkdir` sees them as existing mountpoints and skips them. But if you tear the app down and rebuild from this repo, you'll get the simpler one-dataset shape.

### Permissions on auto-created resources

Newly-created datasets and folders are stamped with `apps:apps 770` via `filesystem.setperm` so Custom Apps can write to them out of the box. **Existing** resources are never touched — if you've manually customized perms or your compose has a permissions sidecar that adjusts them, that work isn't clobbered.

Catalog apps imported as Custom Apps usually carry a `permissions` service in their compose that fixes per-mount ownership at deploy time (e.g. `999:999` for postgres). That pattern still works alongside our default; their sidecar runs after our setperm and the per-mount tweaks take precedence.

`plan` mode reports `+ datasets-to-create:` and `+ folders-to-create:` so you see what would happen before applying.

The `folders:` field in `app.yml` is purely additive on top of what's auto-discovered. Use it for paths that the compose doesn't mention but that you want pre-created (rare).

## How sync runs

Almost all the work happens in `scripts/truenas_reconcile.py` so the logic is testable on its own:

1. Walk `servers/truenas/apps/*/` → desired apps.
2. Connect to `wss://<host>/api/<version>`, authenticate with the API key.
3. `app.query` → list of all apps; for each `custom_app` call `app.config(name)` to read the stored compose; filter to those whose compose carries the marker comment.
4. Compare each desired app's compose `fingerprint` against the one in the marker on the live app.
5. Print a `+`/`~`/`-`/`=` report.
6. If `mode=apply`: call `app.create` / `app.update` / `app.delete` to reconcile, prepending the marker comment on every push.

## Variables

| var | default | meaning |
|---|---|---|
| `mode` | `plan` | `plan` or `apply` |
| `truenas_apps_dir` | `{{ playbook_dir }}/../servers/{{ inventory_hostname }}/apps` | local source (resolves to `servers/truenas/apps/`) |
| `truenas_api_version` | `v25.10.2` | bump when you upgrade TrueNAS |
| `truenas_api_url` | `wss://{{ ansible_host }}/api/{{ truenas_api_version }}` | full WS URL |
| `truenas_api_key` | (vault) | TrueNAS API key |
| `homelab_managed_by` | `homelab-repo` | the marker string |

## Caveats

- The JSON-RPC method shapes have shifted across TrueNAS releases. The client lives at `scripts/truenas_client.py` and is intentionally short — adjust the calls there if a method signature changes.
- Datasets that the compose expects to bind-mount must already exist. Creating them automatically (via `pool.dataset.create`) is a follow-up.
- Manual edits made through the TrueNAS UI to a managed app's compose are detected on the next `plan` only if they don't preserve the marker comment exactly; if they do, drift is invisible until you next change the repo file.
