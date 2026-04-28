# Reconciling: how the source-of-truth model works

This repo treats itself as the desired state for every app it knows about. `apply` reconciles the server toward that state — including **deleting** apps that aren't in the repo. That's powerful and dangerous, so the design has three safety rails.

## Rail 1: only manage what you've claimed

The reconciler doesn't look at *every* app on a server. It only looks at apps carrying an explicit "I belong to this repo" marker:

- **TrueNAS**: every managed app has an `x-homelab` extension field at the top of its compose body. Top-level `x-` keys are reserved by the Compose spec, so Docker ignores them, but the reconciler sees them:

    ```yaml
    x-homelab:
      managed-by: homelab-repo
      fingerprint: <12-char sha256 of repo compose>
    services: ...
    ```

    This lives in the compose because TrueNAS 25.x dropped the writable description/notes field that older versions had — the compose body is the only round-trippable place for repo metadata. Apps without the marker are **invisible to deletion logic**: the reconciler never deletes anything it didn't stamp.

- **Docker VM**: managed apps live under `/opt/homelab/apps/`. Anything started by hand outside that path, anything in another compose project — invisible.

Result: the worst case for "I forgot to add an app to the repo" is that your `apply` won't manage it, not that it gets deleted.

## Rail 2: plan before apply

`plan.yml` runs the same diff logic but applies nothing. Always run it first:

```bash
ansible-playbook playbooks/plan.yml
```

Output looks like:

```
host: truenas  mode: plan
+ to-create: ['gitea']
~ to-update: ['paperless']
@ to-adopt:  ['old-immich']
- to-delete: ['old-thing']
= unchanged: ['jellyfin', 'authentik']
```

The buckets:

- `+ to-create` — in repo, not on server. Reconciler will create + stamp.
- `~ to-update` — in repo and on server, marker present, fingerprint differs. Reconciler will push the stamped repo compose.
- `@ to-adopt` — in repo and on server, but no marker. Reconciler will push the stamped repo compose so subsequent runs treat it as managed. Imports always land here on first apply.
- `- to-delete` — has our marker on the server, missing from the repo. Reconciler will delete.
- `= unchanged` — fingerprint matches; no-op.

If anything in `- to-delete` surprises you, **stop**. Either the app should be added back to the repo, or you've discovered an actually-orphaned app you didn't know about.

`apply.yml` prompts for confirmation before doing anything. Skip the prompt with `-e confirm=auto` (e.g., from a cron job).

## Rail 3: cleartext secrets can't reach the server

Both apply paths run `scripts/check_envs_encrypted.py` as a pre-task before talking to any server:

- `playbooks/truenas_sync.yml` — pre-task, walks `servers/truenas/apps/`.
- `roles/docker_compose_sync/tasks/main.yml` — first task, walks `servers/docker-vm/apps/`.

Any `.env` file that doesn't start with `$ANSIBLE_VAULT;` causes the playbook to fail before any network call. (`*.example` files are exempt.) The `.githooks/pre-commit` hook does the same check at commit time.

## What "fingerprint" actually fingerprints

For TrueNAS, `x-homelab.fingerprint` is `sha256(rendered_compose)` — the compose **after** `${VAR}` substitution from the per-app `.env`. That means:

- Editing `compose.yml` triggers `~ to-update` on next plan.
- Editing a value in `.env` *also* triggers `~ to-update`, because the rendered output is different. Drift detection works correctly across rotations.
- Re-planning when nothing changed is `= unchanged` — fingerprints are byte-stable.

For the Docker VM, drift is detected by directory diff against `/opt/homelab/apps/<name>/`.

## What "managed" *doesn't* claim

- App data (volumes, datasets, bind mounts) is never considered repo state. Tear-downs preserve volumes unless `docker_vm_prune_volumes=true` is passed.
- **Existing dataset perms and ownership.** The reconciler stamps `apps:apps 770` on freshly-created datasets and folders only. Anything that already exists on disk — including resources created by the TrueNAS UI, or hand-tuned for an app that needs a non-`apps` UID like Postgres (999) — is left alone. Permissions sidecars, manual `chown`s, and `chmod`s survive across reconciles.
- Docker networks declared `external: true` (currently `proxy` and `media-internal` on TrueNAS). These are managed by hand — the TrueNAS JSON-RPC API doesn't expose a `docker.network.*` method as of 2026-04-25.
- Reverse-proxy / DNS / TLS plumbing. The repo defines container-side ports; routing them is whatever Traefik or equivalent you have running already.

## Drift in CI (future)

Pass `-e fail_on_drift=true` to `docker_vm_sync.yml` and the playbook exits non-zero whenever there's drift — useful for a scheduled "did anything change behind our backs" alert.
