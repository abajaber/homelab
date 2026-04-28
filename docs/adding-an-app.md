# Adding an app

The flow has the same shape on both server types — drop a folder, run `plan`, run `apply`. The compose body and a sibling `.env` are the entire source of truth.

## Shared conventions

- **No top-level `name:` in `compose.yml`** — the project name comes from the folder name (and `app.yml`'s `name:`, if you override it).
- **Reference secrets as `${VAR}`** in `compose.yml`; put `VAR=...` lines in a sibling `.env` and `ansible-vault encrypt` the file. The reconciler wires the two together at apply time. See [secrets.md](secrets.md) for the full workflow.
- **Don't paste a literal password** into `compose.yml`. The `.githooks/pre-commit` hook will block any cleartext `.env`, but it can't see secrets you accidentally inlined in compose.

A starter template lives at `servers/truenas/apps/_example/` — copy that directory and rename.

## On the Docker VM

1. Pick a name. It will become the directory name *and* the compose project name on the host (`/opt/homelab/apps/<name>/`).

2. Create the folder:

    ```
    servers/docker-vm/apps/<name>/
    ├── app.yml
    ├── compose.yml
    └── .env            # optional; vault-encrypt before committing
    ```

    `app.yml`:
    ```yaml
    ---
    name: <name>
    enabled: true
    ```

3. If the app needs secrets:

    ```bash
    cat > servers/docker-vm/apps/<name>/.env <<'EOF'
    DB_PASSWORD=<value>
    API_TOKEN=<value>
    EOF
    ansible-vault encrypt servers/docker-vm/apps/<name>/.env
    ```

    Reference them from `compose.yml` as `${DB_PASSWORD}` / `${API_TOKEN}`. The Docker VM role rsyncs the app dir minus `.env`, then writes a decrypted copy at `0600` next to the compose; Docker Compose's native loader picks it up.

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

1. Same idea, under `servers/truenas/apps/<name>/` with `app.yml`, `compose.yml`, and an optional `.env`.

2. **Bind everything stateful** to `/mnt/<truenas_dataset_root>/<name>/<purpose>` — the variable `truenas_dataset_root` lives in `servers/truenas/vars.yml` (currently `redsea/apps`). Use sub-folders like `data`, `config`, `db` per service. The reconciler creates the dataset on first apply and stamps `apps:apps 770`. **Existing paths are never touched** — hand-tuned ownership/perms survive across reconciles.

   Pick the right volume style for each mount:

    | Compose form | Where data lives | When to use |
    |---|---|---|
    | `/mnt/<root>/<name>/<sub>:/path` (explicit bind) | Inside the per-app dataset on the data pool. | **Default** for anything you'd want to back up, snapshot, or quota: databases, app config, user data, media. |
    | `mydata:/path` (named volume) | `/mnt/<apps-pool>/ix-apps/docker/volumes/<vol>/_data`. Docker manages it; reconciler ignores it. | Cache or scratch where the on-disk location doesn't matter (Redis cache, ML model cache, build artifacts). |
    | `tmpfs:` mount | RAM only. | Truly ephemeral state: `/tmp`, sockets, secrets that should evaporate on restart. |
    | `/mnt/.ix-apps/app_mounts/<app>/<x>:/path` | Directories TrueNAS provisions for catalog apps converted to Custom. | Almost never write by hand. Leave it alone if it shows up in imported compose and works. |

   **Rule of thumb**: if losing the data would matter, use an explicit bind to a dataset.

3. Optional override — list extra paths in `app.yml`'s `folders:` for things the compose doesn't bind-mount but you want pre-created:

    ```yaml
    ---
    name: myapp
    enabled: true
    folders:
      - extra-path        # relative → /mnt/<truenas_dataset_root>/myapp/extra-path
    ```

   Relative entries anchor under the per-app dataset; absolute entries are taken verbatim (refused if outside `/mnt/`). The reconciler refuses to touch anything outside `/mnt/`.

4. For finer dataset granularity (e.g. snapshot the DB separately from media, or set `recordsize=8K` for a Postgres volume), create those child datasets in the TrueNAS UI first — the auto-create is idempotent and skips existing datasets, and binds resolve through child mountpoints without any code change.

5. Plan, apply:

    ```bash
    ansible-playbook playbooks/truenas_sync.yml
    ansible-playbook playbooks/truenas_sync.yml -e mode=apply
    ```

6. Verify in the TrueNAS UI under **Apps** — the new app should be `Running`, and its compose body (visible from the app's UI page) should have an `x-homelab.fingerprint` block at the top.

## Forward auth (Authentik) for apps with an HTTP API

If the new app sits behind Authentik **and** something external still needs to talk to its API (Recyclarr, the wire-up scripts under `scripts/`, anything authenticating with `X-Api-Key`), use the two-router pattern: a UI router with the `authentik@docker` middleware, plus an `<name>-api` router that bypasses auth for `/api`. Full pattern + matching app-side config in [forward-auth.md](forward-auth.md).

## Importing existing apps

If you've already deployed something on a server (manually clicked through the TrueNAS UI, or `docker compose up`'d a project on the VM), use `import.yml` to bring those into the repo. It's safe to re-run regularly — apps already in the repo are skipped.

```bash
# Show what would be imported (no files written, no server changes)
ansible-playbook playbooks/import.yml

# Write servers/<host>/apps/<name>/{app.yml,compose.yml} for everything new
ansible-playbook playbooks/import.yml -e mode=apply
```

After import, **review with `git diff` before committing** — and pay attention to the warnings printed at the end of the run.

### The import secrets warning

After writing each new compose, the importer runs `scripts/scan_compose_secrets.py` against it. The script prints a warning enumerating env keys whose names match `*_(PASSWORD|SECRET|TOKEN|KEY|API_KEY|JWT)` (case-insensitive) when the value is a literal scalar (not already `${VAR}`). Example output:

```
! likely secrets in servers/truenas/apps/foo/compose.yml:
    foo.environment.DB_PASSWORD = abcd…ef
    foo.environment.API_TOKEN = ghij…kl
```

For each flagged key:
1. Create or edit `servers/<host>/apps/<app>/.env` and add `KEY=<value>`.
2. Replace the value in `compose.yml` with `${KEY}`.
3. `ansible-vault encrypt servers/<host>/apps/<app>/.env`.

Then re-plan; the first apply afterwards will adopt the app (see "Adoption" below).

### TrueNAS specifics

- **Custom Apps** (compose-based) round-trip cleanly.
- **Catalog apps** (Plex, Jellyfin, Nextcloud, etc. installed from the catalog) are **skipped with a warning**. Catalog apps don't have a compose body — they're parameterized by a `values` form. Repo support is a follow-up.
- Import is **read-only on the server**. Adoption happens automatically the next time you run `truenas_sync.yml -e mode=apply`: the reconciler classifies "in repo + on server without our marker" as `@ to-adopt` and re-pushes the stamped compose. No separate "adopt" gesture needed.

### Docker VM specifics

- Imported `compose.yml` is the *rendered* form (`docker compose config --no-interpolate`). Multi-file overrides are merged. Comments and YAML anchors are lost.
- Adoption happens automatically on the next sync — `community.docker.docker_compose_v2` reconciles by **project name**, so as long as the imported folder name matches the running project name, `apply.yml` will pick it up. The project will be redeployed from `/opt/homelab/apps/<name>/` (the canonical managed path), which may recreate containers if the original compose lived elsewhere on the host.

## Bringing an app onto TrueNAS from elsewhere (not yet on the box)

Two flows depending on where the app starts.

### A) You already have the compose somewhere else (gist, another host, scratch)

1. Drop `app.yml` + `compose.yml` under `servers/truenas/apps/<name>/`.
2. Rewrite the compose's persistent volumes to bind under `/mnt/<truenas_dataset_root>/<name>/...` — drop any absolute paths from the original host, kill any `bind:` to `/var/lib/docker/...`.
3. Pull every literal secret out of `compose.yml` into a sibling `.env`; replace each with `${VAR}` in compose. `ansible-vault encrypt servers/truenas/apps/<name>/.env`.
4. **Migrate the data first if there is any.** `rsync` the old volumes into `/mnt/<truenas_dataset_root>/<name>/<sub>/` on TrueNAS *before* applying — otherwise the new container starts on an empty dataset.
5. `ansible-playbook playbooks/truenas_sync.yml -e mode=apply`.

### B) The app is already running on TrueNAS (UI/Custom App), just not in this repo

Use the import flow above — `playbooks/truenas_import.yml` reads each Custom App's compose via `app.config(name)`, strips any `x-homelab` marker, and writes `servers/truenas/apps/<name>/{app.yml,compose.yml}`. Catalog apps are skipped with a warning. Re-run is safe (existing repo dirs are skipped). Then act on the secrets warning, encrypt the `.env`, and apply.

## Removing an app

Delete the folder under `servers/<name>/apps/`. Re-run `plan` to confirm `- <name>` shows up under `to-delete`. Then `apply`.

The Docker VM tear-down keeps volumes by default. To wipe them too:

```bash
ansible-playbook playbooks/docker_vm_sync.yml -e mode=apply -e docker_vm_prune_volumes=true
```

## Renaming an app

Rename = delete + create. The state transfer (volumes, datasets) is on you. If that matters, do the rename out-of-band on the server first, then update the repo to match.
