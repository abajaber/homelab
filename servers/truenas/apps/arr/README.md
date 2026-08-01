# arr

Sonarr (anime) + Sonarr (TV) + Radarr + Bazarr + Prowlarr.

`compose.yml` defines the containers. Everything *inside* each app — indexers,
quality profiles, custom formats, tags — lives in that app's SQLite DB under
`/mnt/redsea/apps/arr/<app>/config/`, which the compose reconciler cannot see.
Two things bring that DB state under version control:

| Owns | Source of truth | Runs |
|---|---|---|
| custom formats, quality definitions, quality profiles | `servers/truenas/apps/recyclarr/compose.yml` (inline config) | `recyclarr` container, daily 04:00 |
| tags, indexer/client tag routing, auto-tagging, release profiles, series type | `settings.yml` (this folder) | `scripts/arr_settings_sync.py`, on demand |

Anything not in one of those two is still click-ops and will rot. It has
before.

## settings.yml

```bash
set -a; eval "$(ansible-vault view servers/truenas/apps/arr/.env)"; set +a
python scripts/arr_settings_sync.py                    # plan (default)
python scripts/arr_settings_sync.py --apply
python scripts/arr_settings_sync.py --instance radarr  # scope to one
```

Idempotent — every write is preceded by a read and a diff, and a clean run
prints `= in sync`. Library state (series, movies, queue, history, blocklist,
files) is never touched.

## The tag routing model

`vpn` and `direct` are not labels — they are the routing mechanism, and they
control two independent things:

- an **indexer** carrying a tag is only searched for series carrying that tag
- a **download client** carrying a tag is only eligible for series carrying it

A series with *no* tags can therefore only use untagged indexers, and — because
both clients are tagged — **has no eligible download client at all**. It can
never grab. RSS sync ignores tags entirely, so the Activity page still looks
busy while every search silently hits a single indexer.

The `vpn-default` auto-tagging rule closes this: `TagSpecification(direct)` with
`negate: true`, so anything not explicitly marked `direct` gets `vpn`.
`remove_tags_automatically: true` gives mutual exclusion — tag a series `direct`
and `vpn` is pulled back off, so nothing ends up with both. (Both clients sit at
`priority: 1`; a series with both tags would get a round-robin.)

## Anime gotchas

Five separate faults were found on 2026-07-31, each of which alone looks like
"Sonarr is broken". They are all covered by `settings.yml` + recyclarr now, but
they are worth recognising:

1. **`seriesType: standard` on an anime series.** Searches then use `S17E41`,
   which no anime group names. `enforce_series_type: anime` backfills; the
   `OnSeriesAdd` custom script (`sonarr_custom_scripts/enforce_anime_type.sh`)
   only covers newly added series.
2. **Untagged series** — see above.
3. **Stale custom formats.** TRaSH restructured the anime tiers; the hand-pushed
   copy was ~2 years behind, so `ToonsHub`/`AnoZu` scored 0.
4. **`min_format_score: 100` plus stale formats** = nothing clears the floor,
   and the UI just shows an empty interactive search.
5. **Cour-relative `SxxExx` numbering.** Some groups ship Bleach's Thousand-Year
   Blood War as `S01E41` even though TVDB puts it at `S17E41`. Sonarr matches
   the *release title* (which carries the TYBW alias) but imports the *file*
   name, which usually drops it — bare `Bleach S01E41` then scene-maps onto the
   real season 1 and import dies with `Episode 2x21 was not found in the grabbed
   release`. Not resolvable in general, so the `reject cour-relative SxxExx`
   release profile refuses them for tagged series.

Diagnosing #5: `GET /api/v3/parse?title=<release>` returns the episodes Sonarr
would map a title to. Compare that against the queue item's `outputPath`, which
is what import actually parses — they differ whenever the torrent's inner
filename is less specific than its release title.

## Secrets

`.env` is a credentials notebook, vault-encrypted, not referenced by
`compose.yml` — each *arr generates its own API key on first start. External
tooling (`arr_settings_sync.py`, `recyclarr`, the wire-up scripts) reads it.
Recyclarr keeps its own copy in `servers/truenas/apps/recyclarr/.env`; rotating
a key means rotating it in both.
