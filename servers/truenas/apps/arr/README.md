# arr

Sonarr (anime) + Sonarr (TV) + Radarr + Bazarr + Prowlarr.

`compose.yml` defines the containers. Everything *inside* each app — indexers,
quality profiles, custom formats, tags — lives in that app's SQLite DB under
`/mnt/redsea/apps/arr/<app>/config/`, which the compose reconciler cannot see.
Two things bring that DB state under version control:

| Owns | Source of truth | Runs |
|---|---|---|
| custom formats, quality definitions, quality profiles | `servers/truenas/apps/recyclarr/compose.yml` (inline config) | `recyclarr` container, daily 04:00 |
| tags, indexer/client tag routing, auto-tagging, release profiles, series type, which profile each series is on | `settings.yml` (this folder) | `scripts/arr_settings_sync.py`, on demand |

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

## Quality profile assignment

Recyclarr manages the *contents* of a quality profile but has no idea which
series are pointed at it, so a series added through the UI silently keeps
Sonarr's stock `Any` — `upgradeAllowed=false`, cutoff `SDTV`, every custom
format scored 0. It will accept an SDTV raw and never upgrade. `SAKAMOTO DAYS`
sat like that unnoticed; `Fallout` sat on a renamed stock profile that recyclarr
never touched, so its `Upscaled` / `Bad Dual Groups` / `BR-DISK (BTN)` blocks
were all at 0.

`default_quality_profile` closes that: every series is moved onto it unless
`series_profiles` overrides by name. Profiles are named rather than numbered
because the ids differ per instance. Quote both sides — several series titles
contain a colon.

```yaml
default_quality_profile: "Remux-1080p - Anime"
series_profiles:
  "The Eminence in Shadow": "Remux-1080p - Anime - Dual Audio"
```

This makes new series safe by default: whatever the UI picks on add, the next
`--apply` puts it on a managed profile.

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
5. **Alias-stripping release groups.** Bleach's Thousand-Year Blood War is
   numbered relative to its own cour (`S01E41`) while TVDB puts it at `S17E41`.
   Sonarr matches the *release title* at grab time but parses the *file* name at
   import. What decides success is whether the filename still carries something
   that disambiguates the series:

   ```
   ToonsHub  BLEACH.Thousand-Year.Blood.War.S01E42...-ToonsHub.mkv   alias kept -> S17E42  ok
   AnoZu     Bleach.2004.S17E41...-AnoZu.mkv                         TVDB numbering -> ok
   VARYG     BLEACH.S01E41.GOD.OF.THUNDER...-VARYG.mkv               alias dropped -> 2x21  FAILS
   ```

   The `S01Exx` form is **not** the discriminator — ToonsHub uses it and imports
   fine. The alias-stripping is, and nothing in the release title reveals it.

   **No rule guards this today, deliberately.** A release profile blocking
   VARYG was tried and reverted. TRaSH rates VARYG a good group (`Anime Web
   Tier 04`, +300) and it is right — the encodes are fine, the collision is
   specific to this one series. Blocking it also left ToonsHub as the sole
   source above `min_format_score`, and later evidence undercut the premise
   outright: VARYG's S17E27 releases on AnimeTosho *do* carry the alias
   (`BLEACH.Thousand.Year.Blood.War.S01E27...-VARYG.mkv`), so the stripping is
   per-release, not a group convention. One observed failure was not a rule.

   When it does recur the symptom is a queue item stuck at `importPending`
   with `Episode 2x21 was not found in the grabbed release`. It does not
   auto-fail, so Sonarr will not re-search past it — clear it with
   `DELETE /api/v3/queue/<id>?removeFromClient=true&blocklist=true`, which
   blocklists that release and triggers a fresh search.

   It really is unresolvable by configuration: scene slot `S01E41` is already
   bound to `S17E41`'s neighbour by XEM —

   ```
   id=480  S02E21  sceneSeason=1   sceneEpisode=41   <- slot already taken
   id=846  S17E41  sceneSeason=17  sceneEpisode=41
   ```

   — so no alternate title or scene mapping can make bare `Bleach S01E41` resolve
   to S17E41 without stealing the slot from S02E21. Forcing a per-torrent folder
   in qBittorrent does not help either: the folder is named after the *torrent*
   name, which for a single-file torrent is the filename, not the indexer's
   listing title.

Diagnosing #5: `GET /api/v3/parse?title=<release>` returns the episodes Sonarr
would map a title to. Compare that against the history entry's `droppedPath`,
which is what import actually parses — they differ whenever the torrent's inner
filename is less specific than its indexer listing title.

**Thin source pool.** Only two groups clear `min_format_score: 100` for Bleach —
ToonsHub (375) and VARYG (305). Judas, Lazier, ESPADAS and KiyoshiiSubs all
parse to `S17Exx` correctly but score 0, so the floor excludes them, not the
numbering. Dropping the floor to 0 on profile 8 would widen the pool without
admitting the LQ/VOSTFR/Raws groups (they sit at -10000), but that is a
quality-policy decision, so it has not been made. This is why blocking either of
the two viable groups is a bad trade.

6. **Dub-only releases that do not say "dub".** `Dubs Only` is scored -10000 but
   every one of its specifications matches on release *title*, and the primary
   one needs the literal token `dub`/`dubbed`. ToonsHub labels theirs
   `English Audio`, so S17E30 imported with `audioLanguages: eng` and no
   Japanese track, beating the dual-audio release 375 to 305.

   Sonarr had already parsed it as `languages: [English]`, so the fix is
   TRaSH's `Language: Not Original` (`ae575f95…`) — one negated
   `LanguageSpecification` on "Original" — scored -10000 on
   `Remux-1080p - Anime` in the recyclarr config. It is opt-in upstream
   (`[Optional] Language Profiles`), and listing it explicitly also marks it
   matched so `reset_unmatched_scores` leaves it alone.

   It self-heals: an offending file rescores below the floor, goes cutoff
   unmet, and the next search replaces it. The Dual Audio profile needs no
   equivalent — it requires `Anime Dual Audio` (+2000) to clear its floor,
   which a dub-only release never carries.

**Reading a search result list.** Interactive search shows everything the
indexers returned, including non-matches, each with its rejection. For S17E30
that was 111 results of which **58 were not that episode** — 34
`Episode wasn't requested`, 24 `Unknown Series`. Anime searches query by title
plus absolute number (`Bleach 396`), and indexers text-match loosely, so 29-
episode Bluray batches from the 2004 run come back scoring 700. High score does
not mean eligible; Sonarr checks the episodes a release actually contains
separately, and automatic search drops all of them silently.

## Secrets

`.env` is a credentials notebook, vault-encrypted, not referenced by
`compose.yml` — each *arr generates its own API key on first start. External
tooling (`arr_settings_sync.py`, `recyclarr`, the wire-up scripts) reads it.
Recyclarr keeps its own copy in `servers/truenas/apps/recyclarr/.env`; rotating
a key means rotating it in both.
