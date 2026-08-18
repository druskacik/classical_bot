# Source discovery

This package contains reproducible research workflows for finding new external
sources. It is separate from runtime crawlers, scheduled automation, and the
small reusable tools in `agent_utils`.

## Bachtrack

Discover ticket destinations and write deduplicated website origins:

```bash
uv run python -m source_discovery.bachtrack.discover \
  --all-categories \
  --resolve-ticket-targets \
  --output data/bachtrack_source_urls.csv \
  --listings-output data/bachtrack_source_listings.csv
```

Prepare normalized review batches:

```bash
uv run python -m source_discovery.bachtrack.prepare_review
```

Compile reviewed candidates into the numbered crawler-source seed:

```bash
uv run python -m source_discovery.bachtrack.compile_seed \
  --include-medium-confidence
```

The files under `data/bachtrack_*` are generated discovery and review evidence.
The finalized immutable output belongs under `seeds/crawler_sources/`.

## MusicBrainz

Download artists matching MusicBrainz's classical tag:

```bash
uv run python -m source_discovery.musicbrainz.download_classical_artists
```

Enrich a downloaded artist CSV with official-homepage and other URL relations:

```bash
uv run python -m source_discovery.musicbrainz.download_classical_artists \
  --enrich-urls-from data/musicbrainz_classical_artists.csv
```

Prepare a normalized, exact-once manifest for reviewing every active official
homepage relation:

```bash
uv run python -m source_discovery.musicbrainz.prepare_official_url_review \
  --shard-dir data/musicbrainz_official_url_review_shards \
  --shards 3
```

After filling the shard decision columns, validate their evidence fields and
merge them into the auditable review CSV:

```bash
uv run python -m source_discovery.musicbrainz.merge_official_url_reviews
```

Prepare a second-pass review of every `needs_deeper_review` row while preserving
the complete first-pass decision and evidence:

```bash
uv run python -m source_discovery.musicbrainz.focused_official_url_review prepare
```

After the focused shard reviews are complete, merge them into the version-2
audit CSV:

```bash
uv run python -m source_discovery.musicbrainz.focused_official_url_review merge
```

Compile all reviewed, current-event official sites into one numbered seed:

```bash
uv run python -m source_discovery.musicbrainz.compile_seed
```

The compiler includes people as well as ensembles and organizations, requires
event evidence to belong to the official site's host, consolidates shared
hosts, and skips hosts already present in earlier crawler-source seeds.

## ClassicalConcertMap

Discover organization homepages and compile a new crawler-source seed:

```bash
uv run python -m source_discovery.classicalconcertmap \
  --discovery-output data/classicalconcertmap_org_sources.csv \
  --seed-output seeds/crawler_sources/0004_classicalconcertmap_discovered_sources.csv
```

Compilation automatically applies the reviewed country and multi-country
corrections in `source_discovery/classicalconcertmap_overrides.csv`. Add verified
corrections there instead of editing the generated seed directly.
