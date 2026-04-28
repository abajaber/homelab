# Forward auth: Authentik + Traefik with API bypass

Most apps in `servers/truenas/apps/` sit behind Authentik forward auth at the Traefik edge. The pattern is non-obvious enough — and gets re-derived enough — to deserve its own page. The arr stack and qBittorrent are the canonical examples; new apps that need API access from external tooling follow the same shape.

## The problem

Authentik forward auth gates a hostname by sending every request through an outpost endpoint that demands a session cookie. That works fine for browser users, but breaks anything that authenticates differently — Recyclarr, the wire-up scripts under `scripts/`, anything sending `X-Api-Key`. Naïvely gating an *arr means the API stops working.

The fix: **two routers per app**. One for the UI (gated). One for `/api` paths (not gated). Both share the same backend service.

## Where the middleware is defined

The Authentik forward-auth middleware is defined as Docker labels on `servers/truenas/apps/authentik/compose.yml`, on the `authentik-server` service:

```yaml
labels:
  traefik.http.middlewares.authentik.forwardauth.address: http://authentik-server:30140/outpost.goauthentik.io/auth/traefik
  traefik.http.middlewares.authentik.forwardauth.trustForwardHeader: 'true'
  traefik.http.middlewares.authentik.forwardauth.authResponseHeaders: X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid
```

Other apps reference it as `authentik@docker`.

A few details that bite:

- **Port `30140`, not `9000`.** Authentik defaults to `9000`, but `AUTHENTIK_LISTEN__HTTP=0.0.0.0:30140` in the compose pins it to TrueNAS-friendly ranges. The middleware address has to match.
- **`AUTHENTIK_EXTERNAL_HOST=https://auth.bajaber.ca`** is set in the same compose. This is the canonical external URL.
- **The Outpost YAML overrides everything.** In the Authentik UI, go *Applications → Outposts → Edit* on the embedded outpost. Its YAML config must contain `authentik_host_browser: https://auth.bajaber.ca`. Without that, the outpost will redirect browsers to whatever URL Authentik was first reached on (typically `https://truenas.bajaber.ca:30141`), and the redirect loop is confusing to debug.

## The two-router pattern (per gated app)

Drop these labels on the service that backs the app. Snippet from the `sonarr` service in `servers/truenas/apps/arr/compose.yml`:

```yaml
labels:
  - traefik.enable=true
  - traefik.docker.network=proxy
  # Backend service
  - traefik.http.services.sonarr.loadbalancer.server.port=8989

  # UI router — gated by Authentik
  - traefik.http.routers.sonarr.rule=Host(`sonarr.bajaber.ca`)
  - traefik.http.routers.sonarr.entrypoints=websecure
  - traefik.http.routers.sonarr.tls=true
  - traefik.http.routers.sonarr.tls.certresolver=cloudflare
  - traefik.http.routers.sonarr.middlewares=authentik@docker
  - traefik.http.routers.sonarr.priority=10

  # API router — bypasses Authentik for /api
  - traefik.http.routers.sonarr-api.rule=Host(`sonarr.bajaber.ca`) && PathPrefix(`/api`)
  - traefik.http.routers.sonarr-api.entrypoints=websecure
  - traefik.http.routers.sonarr-api.tls=true
  - traefik.http.routers.sonarr-api.tls.certresolver=cloudflare
  - traefik.http.routers.sonarr-api.service=sonarr
  - traefik.http.routers.sonarr-api.priority=20
```

Things to copy verbatim:

- The `<name>-api` router has **no `middlewares=` line**. That's the bypass.
- `priority=20` on the API router beats `priority=10` on the UI router, so the longer match (`Host && PathPrefix`) wins on `/api/*` requests.
- `service=<name>` makes the API router share the backend declaration; you don't need a second `loadbalancer.server.port` line.

For **qBittorrent**, the path-prefix is `/api/v2` (qBittorrent's whole API namespace), not `/api`:

```yaml
- traefik.http.routers.qbit-api.rule=Host(`qbit.bajaber.ca`) && PathPrefix(`/api/v2`)
```

## Matching app-side config

The bypass alone isn't enough — each app also needs to be told *not* to demand its own session cookie when an authenticated-by-IP request comes in. Apply these via API; never edit `config.xml` / `qBittorrent.conf` while the container is up (qBittorrent rewrites its conf on shutdown and clobbers manual edits).

### *arr (Sonarr / Radarr / Lidarr / Prowlarr / Bazarr)

Set:

- `AuthenticationMethod=External`
- `AuthenticationRequired=DisabledForLocalAddresses`

Apply with:

- Sonarr / Radarr: `PUT /api/v3/config/host`
- Lidarr / Prowlarr: `PUT /api/v1/config/host`

Both apply live — no restart. The wire-up scripts under `scripts/` already do this; if you're adding a new *arr by hand, mirror what `scripts/migrate_arr_settings.py` does.

### qBittorrent

Set:

- `bypass_auth_subnet_whitelist_enabled=true`
- `bypass_auth_subnet_whitelist=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` (or whatever covers your Traefik network and the Docker bridge)

Apply with:

```
POST /api/v2/auth/login           (form: username, password)
POST /api/v2/app/setPreferences   (form: json={"bypass_auth_subnet_whitelist_enabled": true, ...})
```

Live — no restart.

## Where the API keys for the bypassed paths live

In the per-app `.env` (vault-encrypted). Example: `servers/truenas/apps/arr/.env` carries `SONARR_API_KEY`, `RADARR_API_KEY`, etc. — none referenced from the compose body, but read by every wire-up script that talks to the now-bypassed APIs. See `secrets.md`.

## Adding a new gated app

1. Add the UI router labels with `middlewares=authentik@docker` and `priority=10`.
2. Add the `<name>-api` router with the right `PathPrefix` for the app's API and `priority=20`, no middleware, `service=<name>`.
3. Apply on TrueNAS.
4. Boot the app, grab its API key from the UI, stash it in the per-app `.env` (vault-encrypted).
5. If the app has its own auth toggles (most do), flip them via API to "trust local addresses" — see the *arr / qBittorrent recipes above for the shape; adapt to whatever setting the new app exposes.
