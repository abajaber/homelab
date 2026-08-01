# recyclarr

Syncs [TRaSH-Guides](https://trash-guides.info) custom formats, quality
definitions and quality profiles into the two Sonarr instances and Radarr.

No web UI. It runs `recyclarr sync` on a cron (`CRON_SCHEDULE`, default 04:00
local) and exits between runs.

## Why it exists

The custom formats in Sonarr were pushed by hand once and then rotted. As of
2026-07-31 the drift was severe — TRaSH restructured the anime tiers and the
local copy never followed:

| | local | upstream |
|---|---|---|
| Anime Web Tier 03 | SubsPlease, SubsPlus+, ZR | AnoZu, Dooky, Kitsune, SubsPlus+, ZR |
| Anime Web Tier 04 | BlueLobster, GST, Horrible\*, KAN3D2M, KiyoshiStar, Lia, NanDesuKa, Some-Stuffs, URANIME, VARYG, ZigZag, Erai-raws | Erai-Raws, ToonsHub, VARYG |
| Anime Web Tier 05 | *absent* | the 13 groups upstream moved out of Tier 04, plus SubsPlease |
| Anime BD Tier 02 | 44 groups | 19 — 14 missing, 39 stale |
| Anime BD Tier 07 | 18 groups | 34 — 30 missing |

Practical effect: releases from groups TRaSH has since promoted (`ToonsHub`,
`AnoZu`) scored 0, which is below the `min_format_score: 100` on
`Remux-1080p - Anime`, so nothing was grabbable on that profile.

First sync (2026-07-31) on the anime instance: 40 custom formats created, 5
updated, both profiles rescored (32 → 72 formats). `[AnoZu] Bleach S17E41
1080p DSNP WEB-DL` went from 0 (rejected) to 405 (top-scoring approved
release).

## Config delivery

Unlike the authentik blueprints, the config is **not** pushed out-of-band.
`recyclarr.yml` lives inline in `compose.yml` as a docker `config` targeted at
`/config/recyclarr.yml`. That works here (and not for blueprints) because
`/config` already exists as a bind mount, so docker has a parent directory to
mount into. It is therefore part of the compose body and covered by the
fingerprint — editing it shows up as `~ to-update` on the next plan.

## v8 schema — not the one most guides show

Recyclarr 8 dropped the `includes/` mechanism that every v7-era example uses.
`- template: sonarr-v4-custom-formats-anime` and friends no longer resolve —
the config-templates repo has no `includes/` directory at all, and
`includes.json` is an empty map. Instead:

- a quality profile is referenced by its TRaSH `trash_id`, and the custom
  formats it needs come along with it automatically
- `custom_format_groups.add[].select[]` opts extra CFs in
- `name:` overrides the guide's profile name

That last one is why nothing had to be migrated here: the guide calls the anime
profile `[Anime] Remux-1080p`, but `name:` renames it to the
`Remux-1080p - Anime` this fleet already had, so every series stayed pointed at
the profile it was already on.

## Profiles managed

**`sonarr` (anime)** — both entries are the same guide profile
(`20e0fc959f1f1704bed501f23bdae76f`, `[Anime] Remux-1080p`, min score 100):

- `Remux-1080p - Anime` — guide defaults
- `Remux-1080p - Anime - Dual Audio` — `min_format_score: 2000`, with
  `Anime Dual Audio` overridden to +2000 so a dual-audio release is mandatory

**`sonarr-tv`** — `WEB-1080p` (`72dae194fc92bf828f32cde7744e51a1`).

**`radarr`** — `HD Bluray + WEB` (`d1d67249d3890e49bc12e275d989a7e9`).

`delete_old_custom_formats` is `false` on every instance: recyclarr will update
and add, never remove. The first sync therefore left the old hand-made formats
in place under their v7 names (`Anime Web Tier 01 (Muxers)` etc.) alongside the
new ones (`Anime Web Tier 01`). They are harmless — `reset_unmatched_scores`
zeroed every one of them in both profiles — but they clutter the CF list. Flip
the flag if you want them gone; check what would go first.

## Deploy

```bash
ansible-playbook playbooks/truenas_sync.yml -e mode=apply
```

Run a sync immediately instead of waiting for the cron:

```bash
./scripts/ot.py exec --timeout 300 'docker exec recyclarr recyclarr sync'
```

Preview without writing (`--preview` prints the diff):

```bash
./scripts/ot.py exec --timeout 300 'docker exec recyclarr recyclarr sync --preview'
```

## Secrets

`.env` carries `SONARR_ANIME_API_KEY`, `SONARR_TV_API_KEY`, `RADARR_API_KEY` —
copies of the values in `servers/truenas/apps/arr/.env`. These *are* compose
env-var refs (substituted into the inline config), so rotating a key in the arr
vault means rotating it here too.
