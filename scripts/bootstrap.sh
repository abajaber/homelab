#!/usr/bin/env bash
set -euo pipefail

# Sets up everything needed to run plan/apply playbooks from this workstation.
# Idempotent: safe to re-run.

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

echo "==> Creating Python venv at .venv"
if [[ -d .venv && ! -f .venv/bin/activate ]]; then
  echo "    .venv exists but is incomplete (no bin/activate); removing and recreating"
  rm -rf .venv
fi
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing Python deps"
pip install --quiet --upgrade pip
pip install --quiet "ansible>=9.0.0" "requests>=2.31.0" "PyYAML>=6.0" "websocket-client>=1.7.0"

echo "==> Installing Ansible collections"
ansible-galaxy collection install -r requirements.yml --upgrade >/dev/null

echo "==> Git hooks"
git config core.hooksPath .githooks
echo "    core.hooksPath -> .githooks (pre-commit blocks cleartext .env)"

echo "==> Vault password file"
if [[ ! -f .vault-password ]]; then
  read -r -s -p "Set Ansible Vault password (will be saved to .vault-password, gitignored): " pw
  echo
  printf '%s\n' "$pw" > .vault-password
  chmod 600 .vault-password
  echo "    wrote .vault-password (mode 600, gitignored)"
else
  echo "    .vault-password already exists, leaving it alone"
fi

echo
echo "Done. Activate the venv with:  source .venv/bin/activate"
echo "Then try:                      ansible-inventory --graph"
echo "Edit secrets with:             ansible-vault edit servers/<host>/apps/<app>/.env"
