# homepage — the fleet dashboard (`https://www.bajaber.ca`)

[Homepage](https://gethomepage.dev) renders a tile for every app on the fleet,
with live status widgets pulled from each app's own API. It is **GitOps-native**:
no database, no click-to-configure UI — the dashboard is defined entirely by the
YAML in `config/` plus `homepage.*` Docker labels on each app's compose.

## How an app gets on the dashboard — `homepage.*` labels (the dynamic path)

Homepage mounts the Docker socket read-only and discovers every container that
carries `homepage.*` labels — exactly how Traefik already discovers `traefik.*`
labels in this repo. **So a new app self-registers on the dashboard the moment
you deploy it with `homepage.*` labels — no edit to this folder.**

Add the block right after the app's existing Traefik labels, on the same service.
List-style compose (`- key=value`):

```yaml
    labels:
      - traefik.enable=true
      # ... existing traefik labels ...
      - homepage.group=Media            # which dashboard group the tile lands in
      - homepage.name=Jellyfin
      - homepage.icon=jellyfin.png      # a dashboard-icons slug, or mdi-<name>
      - homepage.href=https://jellyfin.bajaber.ca   # where the tile links
      - homepage.description=Media server
      # optional live widget:
      - homepage.widget.type=jellyfin
      - homepage.widget.url=http://jellyfin:8096    # INTERNAL url (see rules)
      - homepage.widget.key={{HOMEPAGE_VAR_JELLYFIN_KEY}}
```

Map-style compose (`key: value`, e.g. authentik/immich/traefik) is the same keys
as a mapping; quote any value that starts with `{` so YAML doesn't read it as a
flow map: `homepage.widget.key: '{{HOMEPAGE_VAR_JELLYFIN_KEY}}'`.

### Rules that bite

- **`homepage.widget.url` must be the INTERNAL service URL** (`http://sonarr:8989`,
  the container name on the `proxy` network) — never the Authentik-gated public
  hostname, or the server-side widget call 302s to the login page and shows no
  data. Host-networked apps (AdGuard, Home Assistant) have no container DNS, so
  they live in `config/services.yaml` with a `http://192.168.1.138:<port>` URL
  instead of labels.
- **Widget credentials are `{{HOMEPAGE_VAR_*}}` placeholders**, resolved from
  *this* container's environment — NOT the labelled app's. So the real value goes
  in **`homepage/.env`** (this folder), not the app's `.env`. Homepage runs the
  substitution on label values too, so the cleartext key never sits in any
  committed compose.
- `homepage.group` just names a group; unknown groups appear at the end. Group
  order/columns live in `config/settings.yaml`.

## Secrets (`homepage/.env`, vault-encrypted)

Every `homepage.widget.*` credential is a `HOMEPAGE_VAR_*` entry in `.env`
(see `.env.example` for the full list and how to mint each). The reconciler
substitutes them into the compose `environment:` at apply, so the container
receives `HOMEPAGE_VAR_*` and resolves the `{{...}}` placeholders. The arr keys
and the Authentik token are reused from those apps' vault `.env` files (copied
here, since Homepage can't read another app's `.env`); the rest ship as
`replace-me` stubs — a stubbed widget just shows no data until you fill it; the
link tile still works. Add/rotate a secret with:

```bash
ansible-vault edit servers/truenas/apps/homepage/.env
```

## Config-as-code (`config/`)

`settings.yaml` (layout), `widgets.yaml` (info header), `docker.yaml` (socket),
and `services.yaml` (the few non-discoverable endpoints) are the source of truth.
TrueNAS ships only the compose body, so these are pushed out-of-band — they are
**not** drift-tracked, so re-push after editing:

```bash
python scripts/homepage_push_config.py            # push config/*.yaml to the box
python scripts/homepage_push_config.py --dry-run  # preview
```

## Deploy / change workflow

1. **Compose changed** (new `HOMEPAGE_VAR_*`, image bump, etc.) →
   `ansible-playbook playbooks/truenas_sync.yml -e mode=apply`.
2. **`config/*.yaml` changed** → `python scripts/homepage_push_config.py`.
3. **A `homepage.*` label changed on another app** → apply that app
   (`truenas_sync.yml -e mode=apply`); Homepage re-discovers on the next page load.
4. Reload `https://www.bajaber.ca` (hard refresh).

## Notes

- Gated behind `authentik@docker` like the other UIs — Homepage ships no auth of
  its own by design.
- `HOMEPAGE_ALLOWED_HOSTS=www.bajaber.ca` (in `compose.yml`) is **mandatory**
  behind Traefik; without it Homepage 4xx's on the proxied Host header.
- DNS: `www.bajaber.ca` is an AdGuard rewrite to the box (internal/tailnet only),
  added to `servers/truenas/apps/adguard/AdGuardHome.yaml` — re-push AdGuard
  config if that file changes (`scripts/adguard_push_config.py`).
- Uptime Kuma's widget needs a public status-page slug; it ships as a link tile
  until you create a status page and add `homepage.widget.type=uptimekuma` +
  `homepage.widget.url` + `homepage.widget.slug=<slug>`.
