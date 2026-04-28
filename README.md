# homelab

Source of truth for the **applications** running on my homelab servers.

This repo doesn't provision the servers themselves — it manages the apps deployed on them, their definitions, and (with care) reconciles servers back to the state described here. It is designed to be safely committable to a public GitHub: every secret lives inside an Ansible Vault.

## Layout

```
homelab/
├── ansible.cfg
├── requirements.yml             # ansible-galaxy collections
├── .githooks/                   # activated by bootstrap.sh; refuses to commit cleartext .env
├── servers/                     # everything Ansible cares about lives here
│   ├── hosts.yml                # the inventory (hosts + connection info)
│   ├── group_vars/all/          # repo-wide constants (auto-loaded by Ansible)
│   ├── truenas/                 # one folder per server
│   │   ├── vars.yml             # per-host config
│   │   ├── vault.yml            # per-host secrets (encrypted)
│   │   └── apps/<name>/
│   │       ├── app.yml          # name, enabled, optional folders override
│   │       ├── compose.yml      # docker-compose body; secrets as ${VAR}
│   │       └── .env             # per-app secrets, vault-encrypted at rest
│   └── docker-vm/
│       └── apps/<name>/{app.yml,compose.yml,.env}
├── playbooks/
├── roles/
├── scripts/                     # truenas client, reconciler, importer, wire-up tools
└── docs/
```

The host name in `servers/hosts.yml`, the folder name under `servers/`, and the value of `inventory_hostname` in playbooks are the **same string** — `truenas`, `docker-vm`, etc. To add a new server: drop a new folder under `servers/`, add a host to `servers/hosts.yml`, copy a playbook.

## Servers

| Folder | What it is | How apps are managed |
|---|---|---|
| `servers/truenas/` | TrueNAS Scale 25.x | Custom Apps (Docker) via JSON-RPC over WebSocket on `:4443` |
| `servers/docker-vm/` | A VM running plain Docker | `docker compose` projects synced over SSH |

Each app under a server is a folder containing `compose.yml` (the docker-compose body), `app.yml` (metadata), and an optional `.env` (secrets, vault-encrypted). Add a folder → app gets created. Remove a folder → app gets removed (see [docs/reconciling.md](docs/reconciling.md) for the safety rails that keep that from going wrong).

## Per-app secrets, in one paragraph

The compose body is the source of truth for an app's *shape*; a sibling `.env` is the source of truth for its *secrets*. Reference values from compose as `${VAR}`; put `VAR=...` lines in `.env`; encrypt the file with `ansible-vault encrypt servers/<host>/apps/<app>/.env`. The reconcilers wire the two together at apply time — TrueNAS substitutes in-memory; Docker VM writes a decrypted copy at `0600` next to the compose so Docker Compose's native `.env` loader picks it up. Cleartext `.env` files are blocked at commit (`.githooks/pre-commit`) **and** at apply (a pre-task hard-fails before the playbook talks to any server). Full workflow in [docs/secrets.md](docs/secrets.md).

## Commands you'll run 99% of the time

```bash
# Show what would change, change nothing
ansible-playbook playbooks/plan.yml

# Reconcile every server to repo state (prompts before destructive actions)
ansible-playbook playbooks/apply.yml
ansible-playbook playbooks/apply.yml -e confirm=auto      # skip the prompt

# Scope to a single server
ansible-playbook playbooks/docker_vm_sync.yml -e mode=apply
ansible-playbook playbooks/truenas_sync.yml   -e mode=plan

# Pull existing apps off the servers into the repo (read-only on the server)
ansible-playbook playbooks/import.yml                     # plan
ansible-playbook playbooks/import.yml -e mode=apply       # write repo files

# Edit a per-app secrets file
ansible-vault edit servers/truenas/apps/<app>/.env
```

## First-time setup

```bash
bash scripts/bootstrap.sh         # venv, collections, vault password, .githooks
source .venv/bin/activate
ansible-inventory --graph         # sanity-check inventory
```

Then edit `servers/hosts.yml` so `ansible_host` points at your real machines, and seed the encrypted vault files (see [docs/secrets.md](docs/secrets.md)).

## Documentation

| Doc | Read when… |
|---|---|
| [docs/adding-an-app.md](docs/adding-an-app.md) | adding a new app to either server, or bringing an existing one onto TrueNAS from elsewhere |
| [docs/reconciling.md](docs/reconciling.md) | you want to understand how `apply` decides what to create / update / delete / adopt |
| [docs/secrets.md](docs/secrets.md) | editing, rotating, or adding any secret — per-app `.env` or per-host `vault.yml` |
| [docs/forward-auth.md](docs/forward-auth.md) | adding an Authentik-gated app whose API still needs to be reachable for tooling (arr stack, qBittorrent, …) |
| [docs/open-terminal.md](docs/open-terminal.md) | poking at live container state on TrueNAS (logs, generated configs, sqlite DBs) without SSH |
| [CLAUDE.md](CLAUDE.md) | the deep reference — JSON-RPC method shapes, reconciler internals, Ansible quirks, future-extension notes |

## Public-repo safety

- Every secret lives in a vault-encrypted file (`servers/<host>/vault.yml` for per-host secrets, `servers/<host>/apps/<app>/.env` for per-app secrets).
- `.vault-password` is gitignored — never commit it.
- `.githooks/pre-commit` (activated by `bootstrap.sh`) refuses any commit staging a cleartext `.env`.
- Apply paths run `scripts/check_envs_encrypted.py` as a pre-task and abort before talking to any server if a cleartext `.env` is sitting in the tree.
