# Adding an app

The flow is the same shape on both server types — drop a folder, run `plan`, run `apply`.

## On the Docker VM

1. Pick a name. It will become the directory name *and* the compose project name on the host (`/opt/homelab/apps/<name>/`).
2. Create the folder and two files:

    ```
    servers/docker-vm/apps/<name>/
    ├── app.yml
    └── compose.yml
    ```

    `app.yml`:
    ```yaml
    ---
    name: <name>
    enabled: true
    ```

    `compose.yml`: a normal docker-compose body. **Don't** set a top-level `name:` — the role passes the project name explicitly.

3. If the app needs secrets, add them to the encrypted vault and reference them via env in compose:

    ```bash
    ansible-vault edit servers/docker-vm/vault.yml
    ```

    Wiring vault values into a `.env` file rendered at sync time isn't yet built — for now, use the standard compose `environment:` keys with values pulled from the host environment, or add a small `env.j2` template to the app dir and a render task. (Follow-up.)

4. Plan, then apply:

    ```bash
    ansible-playbook playbooks/docker_vm_sync.yml                       # plan
    ansible-playbook playbooks/docker_vm_sync.yml -e mode=apply          # apply
    ```

5. Verify on the host:

    ```bash
    ssh docker-vm 'docker compose -p <name> ps'
    ```

## On TrueNAS

1. Same idea, under `servers/truenas/apps/<name>/` with `app.yml` + `compose.yml`.
2. **Datasets and folders are inferred from the compose at apply time** — for every bind-mount source under `/mnt/<pool>/apps/<name>/`, the reconciler creates a ZFS dataset at `<pool>/apps/<name>` (if missing) and `mkdir`s each subfolder. You don't have to list anything in `app.yml` for this; the compose is the source of truth.

   Optional override: list extra paths in `app.yml`'s `folders:` for things the compose doesn't mention but you want pre-created:

   ```yaml
   ---
   name: myapp
   enabled: true
   folders:
     - extra-path   # in addition to whatever the compose mounts
   ```

   For finer dataset granularity (e.g. snapshot the DB separately from media), create those datasets in the TrueNAS UI first — the auto-create is idempotent and skips existing datasets.
3. Plan, apply:

    ```bash
    ansible-playbook playbooks/truenas_sync.yml
    ansible-playbook playbooks/truenas_sync.yml -e mode=apply
    ```

4. Verify in the TrueNAS UI under **Apps** — the new app should be in `Running` state and its description should contain `managed-by: homelab-repo`.

## Importing existing apps

If you've already deployed something on a server (manually clicked through the TrueNAS UI, or `docker compose up`'d a project on the VM), use `import.yml` to bring those into the repo. It's safe to re-run regularly — apps already in the repo are skipped.

```bash
# Show what would be imported (no files written, no server changes)
ansible-playbook playbooks/import.yml

# Write servers/<host>/apps/<name>/{app.yml,compose.yml} for everything new
ansible-playbook playbooks/import.yml -e mode=apply
```

After import, **re-run `plan.yml` before `apply.yml`** to see how the imported state compares to the repo.

### TrueNAS specifics

- **Custom Apps** (compose-based) round-trip cleanly.
- **Catalog apps** (Plex, Jellyfin, Nextcloud, etc. installed from the catalog) are **skipped with a warning**. Catalog apps don't have a compose body — they're parameterized by a `values` form. Repo support for those is a follow-up.
- Import is **read-only on the server**. It never writes anything to TrueNAS. Adoption happens automatically the next time you run `truenas_sync.yml -e mode=apply`: the reconciler sees apps that are in the repo but on the server without the `x-homelab` marker, classifies them as `@ to-adopt`, and updates the live app with the stamped compose. No separate "adopt" gesture needed — the same `apply` that creates new apps and updates existing managed ones also adopts unmarked apps that match repo entries.

### Docker VM specifics

- Imported `compose.yml` is the *rendered* form (`docker compose config --no-interpolate`). Multi-file overrides are merged. Comments and YAML anchors are lost.
- Adoption happens automatically on the next sync — `community.docker.docker_compose_v2` reconciles by **project name**, so as long as the imported folder name matches the running project name, `apply.yml` will pick it up. The project will be redeployed from `/opt/homelab/apps/<name>/` (the canonical managed path), which may recreate containers if the original compose lived elsewhere on the host.

## Removing an app

Delete the folder under `servers/<name>/apps/`. Re-run `plan` to confirm `- <name>` shows up under `to-delete`. Then `apply`.

The Docker VM tear-down keeps volumes by default. To wipe them too:

```bash
ansible-playbook playbooks/docker_vm_sync.yml -e mode=apply -e docker_vm_prune_volumes=true
```

## Renaming an app

Rename = delete + create. The state transfer (volumes, datasets) is on you. If that matters, do the rename out-of-band on the server first, then update the repo to match.
