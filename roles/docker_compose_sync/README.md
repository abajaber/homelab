# role: docker_compose_sync

Reconciles `docker compose` projects on the Docker VM with the directories under `servers/docker-vm/apps/` in this repo.

## What "managed" means

A directory under `{{ docker_vm_apps_dest_dir }}` (default `/opt/homelab/apps/`) on the host is considered managed by this role. Anything outside that path is invisible to the role and will not be touched.

## Per-app layout

```
servers/docker-vm/apps/<name>/
├── app.yml        # metadata (see below)
└── compose.yml    # docker-compose body
```

`app.yml` fields:

| field | default | meaning |
|---|---|---|
| `name` | dirname | compose project name (almost always equals the dirname) |
| `enabled` | `true` | set to `false` to keep the dir but skip syncing |

Anything else in `app.yml` is currently ignored (room for future fields like env-rendering).

## How sync runs

1. Enumerate enabled apps in the repo → `desired_apps`.
2. Enumerate dirs under `docker_vm_apps_dest_dir` on the host → `existing_apps`.
3. Diff. Print a `+`/`~`/`-`/`=` report.
4. If `mode=apply`:
   - Rsync each desired app dir to the host (with `--delete`).
   - `docker compose up -d --remove-orphans` per project.
   - For orphan dirs: `docker compose down` (volumes preserved unless `docker_vm_prune_volumes=true`), then remove the directory.

## Variables

| var | default | meaning |
|---|---|---|
| `mode` | `plan` | `plan` or `apply` |
| `docker_vm_apps_src_dir` | `{{ playbook_dir }}/../{{ inventory_hostname }}/apps` | local source (resolves to `servers/docker-vm/apps/`) |
| `docker_vm_apps_dest_dir` | `/opt/homelab/apps` | remote dest |
| `docker_vm_prune_volumes` | `false` | wipe volumes on tear-down |
| `fail_on_drift` | `false` | exit non-zero from `plan` if drift exists (CI hook) |
