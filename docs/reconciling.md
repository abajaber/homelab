# Reconciling: how the source-of-truth model works

This repo treats itself as the desired state for every app it knows about. `apply` reconciles the server toward that state — including **deleting** apps that aren't in the repo. That's powerful and dangerous, so the design has two safety rails:

## Rail 1: only manage what you've claimed

The reconciler doesn't look at *every* app on a server. It only looks at apps that carry an explicit "I belong to this repo" mark:

- **TrueNAS**: every app this repo creates has `managed-by: homelab-repo` in its description. Apps without that mark — anything you installed via the TrueNAS UI, anything from before this repo existed — are **invisible** to the reconciler.
- **Docker VM**: managed apps live under `/opt/homelab/apps/`. Anything in `/var/lib/docker`, anything started by hand outside that path, anything in another compose project — invisible.

Result: the worst case for "I forgot to add an app to the repo" is that your `apply` won't manage it, not that it gets deleted.

## Rail 2: plan before apply

`plan.yml` runs the same diff logic but applies nothing. Always run it first:

```bash
ansible-playbook playbooks/plan.yml
```

Output looks like:

```
host: docker-vm  mode: plan
+ to-create: ['gitea']
~ to-update: []
- to-delete: ['old-thing']
= unchanged: ['jellyfin', 'paperless']
```

If anything in `- to-delete` surprises you, **stop**. Either the app should be added back to the repo, or you've discovered an actually-orphaned app you didn't know about.

`apply.yml` prompts for confirmation before doing anything. Skip the prompt with `-e confirm=auto` (e.g., from a cron job).

## What "managed" *doesn't* claim

- App data (volumes, datasets, bind mounts) is never considered repo state. Tear-downs preserve volumes unless `docker_vm_prune_volumes=true` is passed.
- Networks and external resources (Cloudflare tunnels, DNS, reverse proxy entries) are not in scope yet.

## Drift in CI (future)

Pass `-e fail_on_drift=true` to `docker_vm_sync.yml` and the playbook exits non-zero whenever there's drift — useful for a scheduled "did anything change behind our backs" alert.
