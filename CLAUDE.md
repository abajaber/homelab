# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Source of truth for **applications** running on a small homelab fleet — *not* for provisioning the hosts themselves. Run from a workstation; reaches each server via Ansible (over SSH for the Docker VM, over a TrueNAS-API WebSocket for TrueNAS). The repo is meant to be safely committable to a public GitHub: every secret lives in Ansible Vault.

Two server types are wired today: **TrueNAS Scale 25.x** (Docker apps via JSON-RPC) and a **Docker VM** running plain compose. A `servers/<host>/` folder is the unit of extension; new server types slot in beside the existing two.

## Common commands

Always operate inside the venv that `scripts/bootstrap.sh` creates:

```bash
bash scripts/bootstrap.sh             # creates .venv, installs deps, prompts for vault password
source .venv/bin/activate
```

Day-to-day:

```bash
# Plan / apply across every server (apply prompts before destructive actions)
ansible-playbook playbooks/plan.yml
ansible-playbook playbooks/apply.yml
ansible-playbook playbooks/apply.yml -e confirm=auto         # skip the prompt

# Scope to one server
ansible-playbook playbooks/truenas_sync.yml   -e mode=apply
ansible-playbook playbooks/docker_vm_sync.yml -e mode=apply

# Discover existing apps on the servers and write repo files for them
# (read-only on the server — adoption happens during the next sync apply)
ansible-playbook playbooks/import.yml
ansible-playbook playbooks/truenas_import.yml                # TrueNAS only

# Vault
ansible-vault edit servers/<host>/vault.yml
EDITOR='code --wait' ansible-vault edit servers/<host>/vault.yml   # via VS Code
```

The Python scripts under `scripts/` can be invoked directly for debugging — they take `--api-url`, `--api-user`, `--apps-dir`, etc., and read the API key from `TRUENAS_API_KEY`. Useful when iterating on JSON-RPC method shapes without going through Ansible.

## Architecture

### Layout convention

Everything Ansible cares about lives under `servers/`:

- `servers/hosts.yml` — inventory.
- `servers/<host>/vars.yml` + `vault.yml` — per-host config (loaded explicitly via `include_vars` in each playbook, not via group_vars magic).
- `servers/<host>/apps/<name>/{app.yml,compose.yml}` — one folder per app.

The host name in `servers/hosts.yml`, the folder name under `servers/`, and `inventory_hostname` in playbooks are **the same string** (`truenas`, `docker-vm`). Playbooks resolve per-host paths as `{{ playbook_dir }}/../servers/{{ inventory_hostname }}/...`.

### Reconcile model + safety rails

`apply` deletes apps not present in the repo, so each server type has a "managed" mark:

- **TrueNAS**: every managed app has a `x-homelab` extension field at the top of its compose body (top-level `x-` keys are reserved by the Compose spec, so Docker ignores them):

  ```yaml
  x-homelab:
    managed-by: homelab-repo
    fingerprint: <12-char sha256 of repo compose>
  services: ...
  ```

  This lives in the compose because TrueNAS 25.x dropped the writable description/notes field that older versions had — the compose body is the only round-trippable place for repo metadata. Apps without the marker are invisible to **deletion** logic (the reconciler never deletes anything it didn't stamp).

  Adoption is implicit: when sync apply sees an app that's in the repo but exists on the server without the marker, it classifies it `@ to-adopt` and updates the live app with the stamped compose (rather than trying to create a duplicate). Import never writes to the server — only sync apply does.

- **Docker VM**: anything outside `/opt/homelab/apps/` on the host is invisible. The path itself is the marker.

Drift is detected by hashing the repo compose and comparing to `x-homelab.fingerprint` (TrueNAS) or by directory diff (Docker VM).

### TrueNAS client (`scripts/truenas_client.py` + `truenas_reconcile.py` + `truenas_import.py`)

TrueNAS Scale 25.x **dropped REST** in favor of JSON-RPC 2.0 over WebSocket. The endpoint is version-pinned:

```
wss://<host>:4443/api/<truenas_api_version>
```

We hit port **4443 directly** rather than going through Traefik. TrueNAS auto-revokes any API key that arrives over plain HTTP transport, and a Traefik-terminated HTTPS edge typically forwards as plain HTTP to the backend — that revoke triggers on first auth. Bypassing the proxy avoids it.

Auth uses `auth.login_ex` with `mechanism: "API_KEY_PLAIN"` plus both `username` and `api_key` (the legacy `auth.login_with_api_key` returns false in 25.x). The username the key belongs to is `truenas_api_user` in `servers/truenas/vars.yml`.

App methods used: `app.query`, `app.config(name)` (returns the parsed compose **dict** directly, *not* a wrapper with `custom_compose_config_string`), `app.create`, `app.update(name, {custom_compose_config_string: ...})`, `app.delete`. Storage methods: `pool.dataset.query` / `pool.dataset.create` for per-app datasets, `filesystem.stat` / `filesystem.mkdir` / `filesystem.setperm` for sub-folders. Newly-created datasets and folders are stamped with `apps:apps 770`; existing resources are never touched, so any sidecar permissions fixup or hand-tuned ownership is preserved across reconciles.

`scripts/truenas_reconcile.py` and `scripts/truenas_import.py` share helpers (`stamp`, `read_marker`, `strip_marker`) — `truenas_import.py` imports them. `read_marker` and `strip_marker` accept either a YAML string OR a parsed dict, since `app.query` and `app.config` return dicts and the repo files are strings.

**Catalog vs Custom apps**: catalog apps (Plex, Jellyfin, etc.) are parameterized by a `values` form, not a compose body. The reconciler/importer only handles `custom_app: true` items; catalog apps are skipped with a warning. Adding catalog support means a separate `catalog.yml` file format and branching in the reconciler.

### Docker VM (`roles/docker_compose_sync/`)

Pure Ansible — no Python script. `find` enumerates apps in repo and on host, computes the diff, and in apply mode rsyncs each app folder, runs `community.docker.docker_compose_v2`, and prunes orphan directories with `docker compose down`. Volumes preserved by default; `-e docker_vm_prune_volumes=true` to wipe them.

The import path (`playbooks/docker_vm_import.yml`) shells out to `docker compose ls --format json` on the host, then `docker compose -p <name> -f <files> config --no-interpolate` per project. The result is the *rendered* compose — overrides merged, comments and anchors lost. Adoption happens automatically the next sync because `docker_compose_v2` reconciles by **project name**.

### Per-app schema

`app.yml`:

| field | default | meaning |
|---|---|---|
| `name` | dirname | app/project name on the server |
| `enabled` | `true` | `false` keeps the dir but skips it |
| `folders` | `[]` | TrueNAS only — *additional* host paths to mkdir on top of what's auto-discovered from the compose. Rare; only used for paths the compose doesn't bind-mount. Relative entries anchor under `/mnt/<truenas_dataset_root>/<name>/`; absolute entries are verbatim. Refuses to touch anything outside `/mnt/`. |

`compose.yml` — a normal docker-compose body. **Don't** set top-level `name:`; the project name comes from the folder name / `app.yml`. Reference secrets as `${VAR}`; values come from a sibling `.env` (see below).

`.env` (optional) — per-app secrets file, ansible-vault encrypted at rest. Keep it next to `compose.yml`; it never shows up in cleartext in git.

### Per-app secrets via encrypted `.env`

The compose body is the source of truth for an app's *shape*; the sibling `.env` is the source of truth for its *secrets*. The reconcilers wire them together at apply time:

- **TrueNAS** (`scripts/truenas_reconcile.py`): when loading each app, the script reads `<app_dir>/.env`. If the file is ansible-vault encrypted (`$ANSIBLE_VAULT;` header), it's decrypted in-memory using the password file passed via `--vault-password-file` (defaults to repo-root `.vault-password`, set automatically by the role). The KEY=VALUE pairs are then substituted into the compose body via `string.Template.safe_substitute` — `$VAR` and `${VAR}` are resolved; `${VAR:-default}` is **not** supported. The rendered body is what gets fingerprinted and shipped to `app.update`. The cleartext only exists in the script's process memory.
- **Docker VM** (`roles/docker_compose_sync/`): the rsync excludes `.env` so the encrypted blob never reaches the host; immediately after the rsync, an `ansible.builtin.copy` writes the *decrypted* content (via `lookup('file', ...)`, which auto-decrypts vault) into `<dest>/<app>/.env` with mode `0600`. Docker Compose v2 auto-loads `.env` from `project_src` at deploy, so `${VAR}` references in `compose.yml` resolve natively — no Python substitution.

**Fingerprint** for TrueNAS apps is `sha256(rendered_compose)` — rotating a value in `.env` correctly triggers drift detection on the next plan.

**Two safety rails** prevent a cleartext `.env` from leaving the workstation:

1. `.githooks/pre-commit` (activated per-clone by `scripts/bootstrap.sh` setting `core.hooksPath`) refuses any commit that stages a `.env` without the `$ANSIBLE_VAULT;` header.
2. The `playbooks/truenas_sync.yml` and `roles/docker_compose_sync` apply paths run `scripts/check_envs_encrypted.py` as a pre-task and hard-fail on cleartext before talking to any server.

`*.example` files are exempt — they document the dotenv format with placeholder values and are meant to be cleartext.

**Editing a secret**:

```bash
ansible-vault edit servers/<host>/apps/<app>/.env
```

**Adding a new secret to an existing app**: edit the `.env` to add the key, then change the `compose.yml` env value to `${KEY}`. Re-plan; the app shows up under `~ to-update`.

**Tooling secrets that aren't compose env-vars**: `.env` is also the catalog for credentials that *external tooling* needs to talk to an app's HTTP API — even when those credentials are generated inside the container and never appear in `compose.yml`. The arr stack is the canonical example: `servers/truenas/apps/arr/.env` carries `SONARR_API_KEY`, `RADARR_API_KEY`, `LIDARR_API_KEY`, `PROWLARR_API_KEY`, `SONARR_TV_API_KEY`, `BAZARR_API_KEY` — none are referenced from the compose body, but the wire-up scripts (`scripts/wire_prowlarr_sonarrs.py`, `scripts/wire_jellyseerr_sonarrs.py`, `scripts/migrate_arr_settings.py`, etc.) and any one-off API call (e.g. flipping AuthenticationMethod) read them. Workflow on a fresh app: deploy first → boot the app → grab the API key from its UI (Settings → General → Security → API Key) → `ansible-vault edit servers/truenas/apps/<app>/.env` and replace the `replace-me` placeholder. The `.env.example` should ship the placeholder so a clone can see what's expected.

### Forward-auth pattern for arr / qBittorrent (TrueNAS)

Authentik forward auth gates every protected hostname via a Traefik middleware defined as Docker labels on `servers/truenas/apps/authentik/compose.yml` (`traefik.http.middlewares.authentik.forwardauth.*`, referenced from app routers as `authentik@docker`). Because `AUTHENTIK_LISTEN__HTTP=0.0.0.0:30140`, the middleware address is `http://authentik-server:30140/outpost.goauthentik.io/auth/traefik` — **not** the upstream-default `:9000`. Authentik's canonical external URL is pinned with `AUTHENTIK_EXTERNAL_HOST=https://auth.bajaber.ca` in the same compose; the embedded Outpost YAML config (UI: *Applications → Outposts → Edit*) must also have `authentik_host_browser: https://auth.bajaber.ca`, otherwise the outpost sends browsers to whatever URL Authentik was first reached on (typically `https://truenas.bajaber.ca:30141`). Outpost YAML overrides everything else.

Each gated app declares **two** Traefik routers: the UI router (`authentik@docker` middleware, `priority=10`) and an `<name>-api` router that matches `Host(...) && PathPrefix(\`/api\`)` for *arr/Bazarr or `/api/v2` for qBittorrent (no middleware, `priority=20`, `service=<name>` to share the backend). The bypass is required so external tooling — Recyclarr, the wire-up scripts, anything that authenticates via `X-Api-Key` — can still reach the API. Pair the bypass with `AuthenticationMethod=External` + `AuthenticationRequired=DisabledForLocalAddresses` in each *arr (PUT `/api/v3/config/host` on Sonarr/Radarr, `/api/v1/config/host` on Lidarr/Prowlarr), and on qBittorrent set `bypass_auth_subnet_whitelist_enabled=true` plus the standard private CIDRs (POST `/api/v2/app/setPreferences` after `/api/v2/auth/login`). Both *arr and qBittorrent apply these live with no container restart, so use the API; never edit `config.xml` / `qBittorrent.conf` while the container is up (qBittorrent rewrites its conf on shutdown and clobbers manual edits).

**Imports never write a `.env`**: `truenas_import.py` and `playbooks/docker_vm_import.yml` round-trip the rendered compose verbatim (current behavior), but after writing they call `scripts/scan_compose_secrets.py` which prints a heuristic warning enumerating env keys whose names match `*_(PASSWORD|SECRET|TOKEN|KEY|API_KEY)`. Extract those manually before commit.

### TrueNAS storage strategy: which volume style to pick

The compose body is the source of truth for storage. Three volume styles, each with a clear default use case:

| Volume style in compose | Where data lives on disk | When to use |
|---|---|---|
| **Explicit bind to dataset**: `/mnt/<truenas_dataset_root>/<name>/<sub>:/path` | Inside the per-app dataset on your data pool. Reconciler auto-creates the dataset and folder, stamps `apps:apps 770`. | Default for **anything you want to back up, snapshot, or quota**: databases, app config, user data, media. |
| **Docker named volume**: `mydata:/path` (with `volumes: { mydata: }`) | `/mnt/<apps-pool>/ix-apps/docker/volumes/<vol>/_data` — TrueNAS's Docker `data-root`. Reconciler ignores it; Docker manages it. | Cache or scratch where you don't care about the on-disk location and don't need ZFS-level features (Redis cache, ML model cache, build artifacts). |
| **`tmpfs`**: `tmpfs: - /tmp` (or `type: tmpfs`) | RAM only — never hits disk. | Truly ephemeral state: `/tmp`, sockets, secrets that should evaporate on container restart. |
| **TrueNAS ix-volume bind**: `/mnt/.ix-apps/app_mounts/<app>/<x>:/path` | `/mnt/<apps-pool>/.ix-apps/app_mounts/<app>/<x>` — directories TrueNAS provisions. | Almost never write this by hand. Shows up in *imported* compose for catalog apps that were converted to Custom Apps; leave it alone if it works. New apps should use explicit bind to dataset instead. |

**Rule of thumb**: if losing the data would matter, use explicit bind to a dataset. The "one dataset per app" granularity is a deliberate choice — it makes `zfs send <truenas_dataset_root>/<name>@snap` a complete app backup.

### Recommended structure for a new TrueNAS app

```
servers/truenas/apps/<name>/
├── app.yml          # name, enabled (folders is rarely needed)
├── compose.yml      # the actual definition; bind mounts under /mnt/<root>/<name>/...
└── .env             # ansible-vault encrypted at rest; secrets referenced from compose.yml as ${VAR}
```

Compose conventions:

- **No top-level `name:`** — project name comes from the folder.
- **Bind everything stateful** to `/mnt/<truenas_dataset_root>/<name>/<purpose>` — `data`, `config`, `db`, etc. The reconciler will create the dataset on first apply with `apps:apps 770` perms.
- **Use named volumes only for cache/scratch** where the on-disk location is genuinely unimportant.
- **Use `tmpfs`** for `/tmp` and similar.
- **No permissions sidecar needed** for a fresh app — the auto-stamp handles it. Only add a permissions service if a specific container needs a non-`apps` UID (e.g. official Postgres image expects 999) — and write that as either a sidecar or the container's `user:` field.
- **Ports/networks**: this repo has nothing reverse-proxy-aware yet; if Traefik is in front of the box, the compose just exposes ports and Traefik resolves them via its own config (typically labels on a different network).
- **Reference secrets as `${VAR}`** in `compose.yml`; put the values in a sibling `.env` and `ansible-vault encrypt` it. Never paste a literal password into `compose.yml`.

Then `plan` → `apply`:

```bash
ansible-playbook playbooks/truenas_sync.yml                  # plan
ansible-playbook playbooks/truenas_sync.yml -e mode=apply    # apply
```

### Bringing an app onto TrueNAS from elsewhere (not yet on the box)

Two flows:

**A) You already have the compose somewhere else (gist, another host, scratch)**

1. Drop `app.yml` + `compose.yml` under `servers/truenas/apps/<name>/`.
2. Rewrite the compose's persistent volumes to bind under `/mnt/<truenas_dataset_root>/<name>/...` — drop any Docker `data-root`-style absolute paths from the original host, kill any `bind:` to `/var/lib/docker/...`, replace with the dataset paths.
3. Pull every literal secret out of `compose.yml` into a sibling `.env`; replace each with `${VAR}` in compose. `ansible-vault encrypt servers/truenas/apps/<name>/.env`.
4. Migrate the data manually first if there is any: `rsync` the old volumes into `/mnt/<truenas_dataset_root>/<name>/<sub>/` on TrueNAS *before* running apply, otherwise the new container starts with an empty dataset.
5. `ansible-playbook playbooks/truenas_sync.yml -e mode=apply`.

**B) The app is already running on TrueNAS (UI/Custom App), just not in this repo**

Use the import flow — `playbooks/truenas_import.yml` reads each Custom App's compose via `app.config(name)`, strips any `x-homelab` marker, and writes `servers/truenas/apps/<name>/{app.yml,compose.yml}`. Catalog apps are skipped with a warning. Re-run is safe (existing repo dirs are skipped).

After import, **review with `git diff`** before committing — the imported compose is the rendered form (TrueNAS may have inlined `configs:` blocks, expanded shorthand, etc.). The import also runs `scripts/scan_compose_secrets.py` over each new compose and prints a warning listing env keys that look like secrets (`*_PASSWORD|*_TOKEN|*_KEY|*_SECRET|*_API_KEY`). For each flagged key: extract the value into a `.env`, replace it with `${VAR}` in compose.yml, then `ansible-vault encrypt` the `.env`. The first apply afterwards will adopt the app (the reconciler classifies "in repo + on server without our marker" as `@ to-adopt` and re-pushes with the stamp).

### Web terminal for direct on-box file ops (`open-terminal`)

`servers/truenas/apps/open-terminal/` runs the [open-webui/open-terminal](https://github.com/open-webui/open-terminal) REST API on TrueNAS at `https://open-terminal.bajaber.ca`. The container runs as `apps:apps` (UID 568) with `/mnt/redsea/apps:/mnt/apps` bind-mounted RW, so any tool with the API key can read/write every per-app folder on the host with the same identity TrueNAS itself uses.

**When to reach for it:**

- **Troubleshooting a live container** — peek at generated state the repo doesn't track: `arr/<name>/config/config.xml` (API keys, DB rows), `qbittorrent-*/config/qBittorrent/qBittorrent.conf`, `paperless-ngx/redis/*`, sqlite DBs, app logs in `/mnt/apps/<app>/<sub>/logs/`.
- **Reading what's actually on disk** when the live state diverges from what compose suggests (e.g. confirming a permissions fix landed, checking that a sidecar wrote what it was supposed to, comparing `.env` substitution against what the container sees).
- **One-shot ops the reconciler doesn't cover** — chowning a path, creating a sibling folder for a hand-tuned bind, dropping a placeholder file before first deploy. Faster than SSHing into TrueNAS and remembering the dataset path.

**When NOT to use it:**

- **Don't edit `compose.yml`, `app.yml`, or `.env` files via this surface** — those live under `servers/<host>/apps/<app>/` in the repo and are pushed by the reconciler. Edits made directly on the box's bind-mounted folders aren't seen by the apps reconciler (it only reads the repo) and won't survive the next apply if the live compose drifts. The repo is still the source of truth for app *definitions*.
- **Don't use it as a backdoor for secrets** — secrets belong in the per-app vault-encrypted `.env`. The web terminal can read the live cleartext value (the container has it expanded), but writing a new secret here doesn't update the vault.

**Auth:**

The API key lives in `servers/truenas/apps/open-terminal/.env` (vault-encrypted). For convenience, `.open-terminal.env` at the repo root is a gitignored cleartext copy with the key + base URL. Read it with the `Read` tool or:

```bash
set -a; . ./.open-terminal.env; set +a   # OPEN_TERMINAL_URL + OPEN_TERMINAL_API_KEY in env
```

If `.open-terminal.env` doesn't exist yet, regenerate it from the vault:

```bash
KEY=$(ansible-vault view servers/truenas/apps/open-terminal/.env | grep ^OPEN_TERMINAL_API_KEY | cut -d= -f2)
printf 'OPEN_TERMINAL_URL=https://open-terminal.bajaber.ca\nOPEN_TERMINAL_API_KEY=%s\n' "$KEY" > .open-terminal.env
chmod 600 .open-terminal.env
```

**Use the `scripts/ot.py` wrapper for the common case** — it handles auth, strips the JSON envelope, and is meaningfully cheaper in tokens than raw curl:

```bash
./scripts/ot.py ls /mnt/apps                                              # one entry per line
./scripts/ot.py cat /mnt/apps/arr/sonarr/config/config.xml                # raw content to stdout
printf 'new content\n' | ./scripts/ot.py write /mnt/apps/x/y              # stdin -> file
./scripts/ot.py replace /mnt/apps/x/y 'old' 'new'                         # single in-place swap
./scripts/ot.py grep 'AuthenticationMethod' /mnt/apps/arr                 # server-side ripgrep
./scripts/ot.py exec --timeout 10 'ls -la /mnt/apps/open-terminal/home'   # auto-polls; exits with remote code
```

Six verbs, no flags except `--timeout` on `exec`. Walks up from CWD to find `.open-terminal.env`, so it works from any subdir of the repo.

**Drop to raw curl** for things the wrapper deliberately doesn't cover — listing or killing detached execs, multi-target replaces, line-bounded reads, glob, anything else under `${OPEN_TERMINAL_URL}/docs`. The full API (every endpoint takes `Authorization: Bearer ${OPEN_TERMINAL_API_KEY}`):

| Verb | Path | Body / params |
|---|---|---|
| GET | `/files/list?directory=…` | Returns `{dir, entries: [{name, type, size, modified}]}`. |
| GET | `/files/read?path=…&start_line=&end_line=` | Returns `{path, total_lines, content}`. |
| POST | `/files/write` | `{"path": "...", "content": "..."}` — overwrites. |
| POST | `/files/replace` | `{"path": "...", "replacements": [{"target", "replacement", "allow_multiple"?}]}`. |
| GET | `/files/grep?query=…&path=…&regex=&case_insensitive=&include=` | Returns `{matches: [{file, line, content}], truncated}`. |
| GET | `/files/glob?pattern=…&path=…` | Server-side glob. |
| POST | `/execute` | `{"command": "...", "timeout": N}` → `{id, status, output, exit_code, ...}`. |
| GET | `/execute` | List running processes. |
| GET | `/execute/{id}/status?offset=N` | Poll for incremental output + exit code. |
| DELETE | `/execute/{id}` | Kill a running process. |

```bash
. ./.open-terminal.env
H="Authorization: Bearer $OPEN_TERMINAL_API_KEY"
curl -sS -H "$H" "$OPEN_TERMINAL_URL/files/glob?pattern=*.xml&path=/mnt/apps/arr"
```

### Ansible quirks worth knowing

- Self-referential vars cause a recursive-template error in newer Ansible: never write `mode: "{{ mode | default('plan') }}"` in a `vars:` block. Inline `(mode | default('plan'))` directly in the task that uses it.
- Playbooks invoke the venv Python explicitly (`{{ playbook_dir }}/../.venv/bin/python3 scripts/...`) instead of relying on the script's shebang — running `.venv/bin/ansible-playbook` without activation doesn't propagate the venv to subprocesses.
- `stdout_callback = community.general.yaml` was removed in `community.general` 12+. Use `stdout_callback = default` + `result_format = yaml` in `ansible.cfg`.

### Secrets

Vault password file is `.vault-password` at the repo root, gitignored, and `ansible.cfg` references it via `vault_password_file = .vault-password`. Two kinds of secrets, two storage shapes:

- **Per-host** (e.g. TrueNAS API key): `servers/<host>/vault.yml`. Cleartext `vars.yml` references each secret as `{{ vault_<name> }}` so it's obvious where a value comes from. Files start as cleartext placeholders and get encrypted in place once real values are added (`ansible-vault encrypt servers/<host>/vault.yml`).
- **Per-app** (DB passwords, API tokens used by the app itself): `servers/<host>/apps/<app>/.env`, ansible-vault encrypted, KEY=VALUE format. Referenced from the app's `compose.yml` as `${VAR}`. See "Per-app secrets via encrypted `.env`" above. The reconciler substitutes values at apply time; the cleartext never lands on disk outside the app's running container.

### Reconciler extension hooks (future work)

The reconciler does *one* thing per app today: create a single ZFS dataset at `<dataset_root>/<app_name>` and `mkdir` any bind-mount paths inside it (or under `/mnt/`) referenced by the compose. That covers ~80% of cases but leaves several patterns to manual operations:

1. **Per-component child datasets** (e.g. immich's `redsea/apps/immich/database` carved off so postgres can have `recordsize=8K compression=zstd` and its own snapshot cadence). The reconciler today does not create child datasets — they exist on the box because someone ran `zfs create` by hand. The reconciler is idempotent so binds resolve through the child mountpoint without any code change, but the *creation* is invisible to the repo.

   To make this declarative, extend `app.yml` with a `datasets:` field:

   ```yaml
   ---
   name: immich
   datasets:
     database:
       recordsize: 8K
       compression: zstd
       atime: off
       owner: "999:999"      # postgres
       mode: "770"
     data:
       owner: "568:568"
       mode: "770"
   ```

   The reconciler (`scripts/truenas_reconcile.py:261-276` `ensure_dataset`) would need to (a) accept a properties dict and pass it through to `pool.dataset.create` on the JSON-RPC API, and (b) optionally apply non-default ownership via `fs_setperm` when `owner:` is given (today it always stamps `apps:apps 770`). Idempotency is already handled — `dataset_query` returns None for absent and a dict otherwise.

2. **Non-default ownership on the parent dataset.** Some apps need the parent itself owned by a non-`apps` UID. Today `APPS_USER/APPS_GROUP/APPS_MODE` are module constants (`scripts/truenas_reconcile.py:256-258`); could lift them per-app via `app.yml` (`owner:`, `mode:` at the top level). Existing apps default to `apps:apps 770` — backwards-compatible.

3. **Docker network management.** Networks declared `external: true` in per-app composes (currently `proxy` and `media-internal`) are managed by hand: someone runs `docker network create` once on the host. The repo has no record of which networks are required, so a fresh-install bootstrap has to be reverse-engineered from grepping composes. The TrueNAS JSON-RPC API does **not** expose `docker.network.*` methods today (verified 2026-04-25), so a workaround would be either (a) wrap a `chart_release.exec` or similar method to run `docker network create` from a long-lived container, or (b) accept the manual step and document a `servers/<host>/networks.yml` catalog so the human knows what to create. (b) is simpler and good enough.

4. **Pre-existing-resource adoption.** When binding a path to a dataset created outside the reconciler, ownership/perms aren't normalized — the reconciler's `ensure_folder`/`ensure_dataset` no-op when paths exist. Could add an optional `adopt: true` flag on `app.yml` `folders:` entries that forces a `fs_setperm` to `APPS_USER:APPS_GROUP:APPS_MODE` even on existing paths. Risky if misused (could clobber hand-tuned perms), so opt-in is the right shape.

5. **Catalog-app management.** `truenas_reconcile.py:340-341` skips apps where `custom_app: false`. Catalog apps (Plex, scrutiny, netbootxyz, tailscale) are configured by a `values` form, not a compose body. Adding support means a separate `catalog.yml` schema and a code path that calls `app.update` with `values` instead of `custom_compose_config_string`.

6. **SMB/NFS share creation.** TrueNAS exposes `sharing.smb.create` / `sharing.nfs.create` over JSON-RPC. Could declarative this via `shares:` in `app.yml`. Lowest priority — easy in TrueNAS UI for one-offs.

7. **ZFS snapshot policies.** Tied to (1) — `pool.snapshottask.create` exposes the same retention/cadence options the UI offers. A `snapshots:` block in `app.yml` would let the reconciler reconcile snapshot tasks the same way it reconciles apps.

Recommended order to tackle these when the need shows up: (1) → (2) → (4) → (3) → (7) → (6) → (5). Each is additive — no existing app needs to change when a new field becomes available.

## Out of scope

- Provisioning the servers themselves (OS install, pool layout, VM creation).
- App data backups — this repo manages app *definitions*, not app *data*. (Per-app dataset granularity is chosen so `zfs send` snapshots are clean, but the schedule/destination is yours.)
- Catalog-app management on TrueNAS (compose-only today; catalog apps are skipped on import with a warning).
- Reverse-proxy / DNS / TLS plumbing. The repo defines container-side ports; routing them is whatever Traefik or equivalent you have running already.
- Migrating data when you change a volume style (e.g. switching a named volume to a dataset bind). The reconciler updates definitions, not the bytes on disk; copy data out-of-band first.
