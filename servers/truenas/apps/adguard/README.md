# adguard — network-wide DNS for the tailnet

AdGuard Home as the tailnet's DNS resolver + ad-blocker. Host-networked so it
binds the box's **Tailscale** interface (the `tailscale` app runs
`host_network: true`); tailnet clients point Magic DNS at this box and resolve
every `*.bajaber.ca` app to it. DNS records, upstreams, blocklists and the admin
login are config-as-code in [`AdGuardHome.yaml`](./AdGuardHome.yaml).

## Files

| file | what |
|---|---|
| `compose.yml` | container shape (host net, `adguard/adguardhome` pinned, two binds) |
| `AdGuardHome.yaml` | **source of truth** for DNS config. A template — `__ADGUARD_PASSWORD_HASH__` and `__TRUENAS_TS_IP__` are rendered from `.env` at push time |
| `.env` | vault-encrypted. Admin user/pw + bcrypt hash + the box's Tailscale IP. Not compose env-vars — consumed only by the push script |
| `.env.example` | placeholder template |

## One-time setup

1. **Get the box's Tailscale IPv4** (`truenas-scale` node) from the Tailscale
   admin console or `tailscale ip -4` on the box, and put it in `.env`:
   ```bash
   ansible-vault edit servers/truenas/apps/adguard/.env   # set TRUENAS_TS_IP=100.x.y.z
   ```

2. **Create the app** (makes the `redsea/apps/adguard` dataset + `work`/`conf`
   bind dirs and starts the container in setup-wizard mode):
   ```bash
   ansible-playbook playbooks/truenas_sync.yml -e mode=apply
   ```

3. **Push the config** (renders the hash + Tailscale IP, ships the yaml to
   `/mnt/redsea/apps/adguard/conf/` via open-terminal):
   ```bash
   python scripts/adguard_push_config.py
   ```

4. **Restart the `adguard` app** so it reloads the seeded config (TrueNAS UI →
   Apps → adguard → Restart).

5. **Front the UI/API with Traefik** — push the dynamic config (backup first;
   Traefik hot-reloads, no restart):
   ```bash
   ./scripts/ot.py cat /mnt/apps/traefik/file-provider.yml > /tmp/file-provider.bak-adguard
   ./scripts/ot.py write /mnt/apps/traefik/file-provider.yml < servers/truenas/apps/traefik/file-provider.yml
   ```
   Now log in at **`https://adguard.bajaber.ca`** (Authentik SSO, then the
   AdGuard admin login from `.env`). The `/control` API bypasses Authentik and
   is protected by AdGuard's Basic Auth. Direct `http://<ip>:3000` still works.

5. **Point the tailnet at it** — Tailscale admin console → **DNS**:
   - Add a global **Nameserver** = the box's Tailscale IP.
   - Enable **Override local DNS**.
   - Keep **MagicDNS** on (`*.ts.net` still resolves; everything else goes to
     AdGuard, which answers `*.bajaber.ca` locally and forwards the rest).

## Changing DNS / settings

Edit `AdGuardHome.yaml` in the repo, re-run `scripts/adguard_push_config.py`,
restart the app. **Don't** click-edit managed settings in the UI — AdGuard
rewrites the live file and you'll drift from the repo. Adding an app? Add a
`rewrites:` line and re-push. `truenas.bajaber.ca` and `netbird.bajaber.ca` are
deliberately left out (they resolve via upstream).
