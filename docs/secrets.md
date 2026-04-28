# Secrets workflow

Every secret in this repo lives inside an Ansible Vault-encrypted file. The repo is designed to be safe to push to a public GitHub repo — *as long as the rules below are followed*.

There are two kinds of secrets, two storage shapes:

| File | Holds | Format |
|---|---|---|
| `servers/<host>/apps/<app>/.env` | per-app secrets (DB passwords, API tokens used by the app, API keys consumed by external tooling) | `KEY=VALUE` lines, vault-encrypted |
| `servers/<host>/vault.yml` | per-host secrets (TrueNAS API key, host-wide credentials) | YAML, vault-encrypted |
| `servers/group_vars/all/vault.yml` | secrets shared across all servers | YAML, vault-encrypted |

Per-app `.env` is by far the more common case in this repo — almost every app has one.

## The vault password

- Lives in `.vault-password` at the repo root.
- That file is **gitignored** and must never be committed.
- `ansible.cfg` points at it via `vault_password_file = .vault-password`, so every `ansible-playbook` and `ansible-vault` invocation finds it automatically.
- `bash scripts/bootstrap.sh` will create the file on first setup.

If you're moving to a new workstation, copy `.vault-password` over a secure channel (e.g. a password manager attachment) — don't email it, don't paste it in chat, don't put it in cloud storage unencrypted.

## Per-app `.env`

The compose body is the source of truth for an app's *shape*; the sibling `.env` is the source of truth for its *secrets*. Reference values from compose as `${VAR}`; put `VAR=...` lines in `.env`; encrypt the file.

```yaml
# servers/truenas/apps/foo/compose.yml
services:
  foo:
    environment:
      DB_PASSWORD: ${FOO_DB_PASSWORD}
      API_TOKEN:   ${FOO_API_TOKEN}
```

```
# servers/truenas/apps/foo/.env (vault-encrypted at rest)
FOO_DB_PASSWORD=...
FOO_API_TOKEN=...
```

### How the wiring happens at apply time

- **TrueNAS** (`scripts/truenas_reconcile.py`): the script reads `<app_dir>/.env`, decrypts in-memory using the password file (`--vault-password-file`, defaults to repo-root `.vault-password`), and substitutes the values into the compose body via `string.Template.safe_substitute`. `$VAR` and `${VAR}` are resolved; **`${VAR:-default}` is not supported**. The rendered body is what gets fingerprinted and shipped to `app.update`. The cleartext only exists in the script's process memory.
- **Docker VM** (`roles/docker_compose_sync/`): the rsync excludes `.env` so the encrypted blob never reaches the host. Immediately after, an `ansible.builtin.copy` writes the *decrypted* content (via `lookup('file', ...)`, which auto-decrypts vault) into `<dest>/<app>/.env` at mode `0600`. Docker Compose v2 auto-loads `.env` from `project_src` at deploy, so `${VAR}` references in `compose.yml` resolve natively — no Python substitution.

The TrueNAS fingerprint is `sha256(rendered_compose)`, so rotating a value in `.env` correctly triggers `~ to-update` on the next plan.

### Editing a secret

```bash
ansible-vault edit servers/truenas/apps/<app>/.env
```

This decrypts into `$EDITOR`, you save, it re-encrypts on close.

### Adding a new secret to an existing app

1. `ansible-vault edit servers/<host>/apps/<app>/.env` — add `NEW_KEY=<value>`.
2. Edit `compose.yml` — change the env value to `${NEW_KEY}`.
3. Re-plan; the app shows up under `~ to-update`.

### First-time encryption

When you've created a fresh `.env` that's still in cleartext:

```bash
ansible-vault encrypt servers/truenas/apps/<app>/.env
head -1 servers/truenas/apps/<app>/.env
# expected: $ANSIBLE_VAULT;1.1;AES256
```

The `.githooks/pre-commit` hook will also catch this for you (see below) — but encrypting before staging is faster than getting blocked at commit time.

### Tooling-only secrets

`.env` doubles as the catalog for credentials that *external tooling* needs to talk to an app's HTTP API — even when those credentials are generated inside the container and never appear in `compose.yml`. The arr stack is the canonical example: `servers/truenas/apps/arr/.env` carries `SONARR_API_KEY`, `RADARR_API_KEY`, `LIDARR_API_KEY`, `PROWLARR_API_KEY`, `SONARR_TV_API_KEY`, `BAZARR_API_KEY` — none referenced from the compose body, but the wire-up scripts (`scripts/wire_prowlarr_sonarrs.py`, `scripts/wire_jellyseerr_sonarrs.py`, `scripts/migrate_arr_settings.py`, etc.) and any one-off API call read them.

Workflow on a fresh app: deploy first → boot the app → grab the API key from its UI (Settings → General → Security → API Key) → `ansible-vault edit servers/truenas/apps/<app>/.env` and replace the `replace-me` placeholder. The `.env.example` should ship the placeholder so a clone can see what's expected.

## Per-host `vault.yml`

Used for credentials that aren't tied to a specific app — most importantly the TrueNAS API key.

Convention: every encrypted file is named `vault.yml`. The matching unencrypted `vars.yml` references vault values as `vault_<name>` so it's obvious where a value comes from.

```yaml
# vars.yml (committed in cleartext)
truenas_api_key: "{{ vault_truenas_api_key }}"
```

```yaml
# vault.yml (encrypted)
vault_truenas_api_key: "abc123..."
```

```bash
# Edit
ansible-vault edit servers/truenas/vault.yml

# First-time encrypt
ansible-vault encrypt servers/truenas/vault.yml
```

## Encrypting a single string (one-off)

For values you want to inline in a `vars.yml`:

```bash
ansible-vault encrypt_string 'my-secret-value' --name 'vault_thing'
```

Paste the output into the appropriate `vault.yml`.

## Rotating the vault password

```bash
ansible-vault rekey \
  servers/group_vars/all/vault.yml \
  servers/truenas/vault.yml \
  servers/docker-vm/vault.yml \
  $(find servers -name '.env' -type f)
# enter old password, then new
# update .vault-password to the new value
```

(The find expression catches every per-app `.env`.)

## What if `.vault-password` leaks?

1. Rotate every secret encrypted with that password (TrueNAS API key, every app's `.env`) at the source — the encrypted blobs may already be in someone else's clone.
2. `ansible-vault rekey` to a fresh password (using the `find` expression above to catch every per-app `.env`).
3. Force-push isn't enough — assume any value previously encrypted is compromised.

## Two enforcement points keep cleartext from leaving the workstation

1. **Pre-commit hook** — `.githooks/pre-commit`, activated per-clone by `scripts/bootstrap.sh` setting `core.hooksPath`. Any commit that stages a `.env` (basename match) without the `$ANSIBLE_VAULT;` header is rejected. `*.example` files are exempt — they document the dotenv format with placeholder values and are meant to be cleartext.
2. **Apply-time pre-task** — `playbooks/truenas_sync.yml` (pre-task) and `roles/docker_compose_sync/tasks/main.yml` (first task) both run `scripts/check_envs_encrypted.py` against the relevant apps tree. Any cleartext `.env` aborts the playbook before it talks to any server.

If either fires, the fix is the same:

```bash
ansible-vault encrypt servers/<host>/apps/<app>/.env
```

## What about secrets in `compose.yml` itself?

Don't put any. Replace with `${VAR}` and put the value in `.env`. There's no automated check for this — the heuristic `scripts/scan_compose_secrets.py` runs only on **imports** (warning the user when an imported compose has likely-secret values inline). For new composes you write yourself, the discipline is on you.
