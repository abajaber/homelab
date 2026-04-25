# Secrets workflow

Every secret in this repo lives inside an Ansible Vault-encrypted YAML file. The repo is designed to be safe to push to a public GitHub repo — *as long as the rules below are followed*.

## The vault password

- Lives in `.vault-password` at the repo root.
- That file is **gitignored** (see `.gitignore`) and must never be committed.
- `ansible.cfg` points at it via `vault_password_file = .vault-password`, so every `ansible-playbook` and `ansible-vault` invocation finds it automatically.
- `bash scripts/bootstrap.sh` will create the file on first setup.

If you're moving to a new workstation, copy `.vault-password` over a secure channel (e.g. a password manager attachment) — don't email it, don't paste it in chat, don't put it in cloud storage unencrypted.

## Where secrets live

| File | Purpose |
|---|---|
| `servers/group_vars/all/vault.yml` | secrets shared across all servers |
| `servers/truenas/vault.yml` | TrueNAS API key, TrueNAS-only secrets |
| `servers/docker-vm/vault.yml` | per-app secrets for the Docker VM |

Convention: every encrypted file is named `vault.yml`. The matching unencrypted `vars.yml` references vault values as `vault_<name>` so it's obvious where a value comes from.

```yaml
# vars.yml (committed in cleartext)
truenas_api_key: "{{ vault_truenas_api_key }}"
```

```yaml
# vault.yml (encrypted)
vault_truenas_api_key: "abc123..."
```

## Editing an encrypted file

```bash
ansible-vault edit servers/truenas/vault.yml
```

This decrypts it into `$EDITOR`, you save, it re-encrypts on close.

## Encrypting a file in place (first time)

When you've added secrets to a fresh `vault.yml` that's still in cleartext:

```bash
ansible-vault encrypt servers/truenas/vault.yml
```

Verify before committing:

```bash
head -1 servers/truenas/vault.yml
# expected: $ANSIBLE_VAULT;1.1;AES256
```

## Encrypting a single string

For one-off values you want to inline in a `vars.yml`:

```bash
ansible-vault encrypt_string 'my-secret-value' --name 'vault_thing'
```

Paste the output into the appropriate `vault.yml`.

## Rotating the vault password

```bash
ansible-vault rekey servers/group_vars/all/vault.yml servers/truenas/vault.yml servers/docker-vm/vault.yml
# enter old password, then new
# update .vault-password to the new value
```

## What if `.vault-password` leaks?

1. Rotate every secret encrypted with that password (TrueNAS API key, app secrets, etc.) at the source — the encrypted blobs may already be in someone else's clone.
2. `ansible-vault rekey` to a fresh password.
3. Force-push isn't enough — assume any value previously encrypted is compromised.

## Pre-commit safety (optional, recommended later)

A pre-commit hook that fails on unencrypted private keys / API tokens is not yet in scope, but a starting `grep -E` set:

- `BEGIN ([A-Z]+ )?PRIVATE KEY`
- `xox[baprs]-` (Slack)
- `ghp_`, `github_pat_` (GitHub)
- `AKIA[0-9A-Z]{16}` (AWS)
