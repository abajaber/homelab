# homelab

Source of truth for the **applications** running on my homelab servers.

This repo does not provision the servers themselves — it manages the apps deployed on them, their definitions, and (with care) reconciles servers back to the state described here.

## Layout

```
homelab/
├── ansible.cfg
├── servers/                  # everything Ansible cares about lives here
│   ├── hosts.yml             # the inventory (hosts + connection info)
│   ├── group_vars/all/       # repo-wide constants (auto-loaded by Ansible)
│   ├── truenas/              # one folder per server
│   │   ├── vars.yml          # per-host config
│   │   ├── vault.yml         # per-host secrets (encrypted)
│   │   └── apps/             # docker-compose definitions per app
│   └── docker-vm/
│       ├── vars.yml
│       ├── vault.yml
│       └── apps/
├── playbooks/
├── roles/
├── scripts/
└── docs/
```

The host name in `servers/hosts.yml`, the folder name under `servers/`, and the value of `inventory_hostname` in playbooks are the **same string** — `truenas`, `docker-vm`, etc. To add a new server: drop a new folder under `servers/`, add a host to `servers/hosts.yml`, copy a playbook.

## Servers

| Folder | What it is | How apps are managed |
|---|---|---|
| `servers/truenas/` | TrueNAS Scale 24.10+ "Electric Eel" | Custom Apps (Docker) via the TrueNAS API |
| `servers/docker-vm/` | A VM running plain Docker | `docker compose` projects synced over SSH |

Each app under a server is a folder containing a `compose.yml` (the docker-compose body) and an `app.yml` (metadata). Add a folder → app gets created. Remove a folder → app gets removed (see [docs/reconciling.md](docs/reconciling.md) for safety rails).

## Commands you'll run 99% of the time

```bash
# Show what would change, change nothing
ansible-playbook playbooks/plan.yml

# Reconcile every server to repo state
ansible-playbook playbooks/apply.yml

# Scope to a single server
ansible-playbook playbooks/docker_vm_sync.yml -e mode=apply
ansible-playbook playbooks/truenas_sync.yml   -e mode=plan

# Pull existing apps off the servers into the repo (safe, re-runnable)
ansible-playbook playbooks/import.yml                  # show what would be imported
ansible-playbook playbooks/import.yml -e mode=apply    # write repo files
```

## First-time setup

```bash
bash scripts/bootstrap.sh         # venv, collections, vault password
source .venv/bin/activate
ansible-inventory --graph         # sanity-check inventory
```

Then edit `servers/hosts.yml` so `ansible_host` points at your real machines, and seed the encrypted vault files (see [docs/secrets.md](docs/secrets.md)).

## Adding an app

See [docs/adding-an-app.md](docs/adding-an-app.md).

## Public-repo safety

- All secrets live in `*vault.yml` files encrypted with Ansible Vault.
- `.vault-password` is gitignored — never commit it.
- See [docs/secrets.md](docs/secrets.md) for the full workflow.
