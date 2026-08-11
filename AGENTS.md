## Project Overview

Data scraping pipeline for classical music events. Crawlers scrape concert listings from cultural websites, store them in a PostgreSQL database, use the Codex SDK to classify broad-source events, and use Codex to extract concert programmes.

## Commands

```bash
# Run all crawlers + analyzers (scheduled loop)
uv run python main.py

# Run a single crawler
uv run python -m crawlers.sk.filharmonia_sk.main

# Run a single analyzer
uv run python -m analyzers.analyze_potential_events              # dry run
uv run python -m analyzers.analyze_potential_events --commit     # classify one source in shadow mode
uv run python -m analyzers.analyze_potential_events --commit --promote  # classify and promote
```

Package management uses `uv` (not pip) for local development. Exception:
crawler-factory Codex sessions run in an image where the project is installed
into system Python, so use `python` directly for crawler investigation and
testing.

## Architecture

### Production services

- **`classical-bot`** — Built from `Dockerfile`; starts `python main.py` and runs the scheduled crawlers and analyzers.
- **`classical-crawler-factory`** — Deployed through `captain-definition-crawler-factory`, which selects `Dockerfile.crawler-factory`; starts `python -m automation.run_crawler_factory_service` and creates, validates, and publishes crawler changes. It does not run the normal concert pipeline.

### Pipeline flow

1. **Crawlers** (`crawlers/<country_code>/<site>/main.py`) — Each crawler has a `main()` function that scrapes a specific website, saves results to `data/<site>.csv`, and uploads to the DB via `upload_concerts()`.
2. **Analyzer: classify events** (`analyzers/analyze_potential_events.py`) — Uses one persistent Codex thread per source to classify its unreviewed events in bounded pages. Committed runs store shadow decisions; promotion requires the explicit `--promote` flag. Automatic runs only consider current/future events; `--include-past` and `--reanalyze` are explicit backlog operations.
3. **Analyzer: extract concert programmes** (`analyzers/analyze_concert_programs.py`) — Uses the Codex SDK to inspect concert sources, independently assess event inclusion, resolve composers and works, and populate the concert programme tables. Explicit nonclassical/non-event results quarantine rather than delete rows.

### Two upload paths

- **Direct crawlers** (filharmonia, snd, sfk, etc.) — Sites known to only list classical music. They call `upload_concerts()` which inserts directly into `classical_concert`.
- **Broad crawlers** (ticketportal, goout, predpredaj, etc.) — General event sites. They call `upload_potential_concerts()` which inserts into `potential_event` for later AI classification.

### Key shared modules

- `crawlers/classical.py` — `upload_concerts()` / `upload_potential_concerts()` for DB insertion, `Concert` class
- `crawlers/extractors.py` — City extraction from postal codes (uses `data/cities_*.csv`), date/time parsing
- `crawlers/formaters.py` — `format_date()` converts `dd.mm.yyyy` → `yyyy-mm-dd`

### Database tables

- `classical_concert` — Confirmed classical music events
- `potential_event` — Unclassified events awaiting AI analysis
- `event_inclusion_assessment` — Append-only classifier/programme inclusion evidence
- `composer` — Composer registry
- `classical_concert_composer` — Many-to-many join table

## Adding a new crawler

Create `crawlers/<country_code>/<site_domain>/main.py` with a `main()` function. It will be auto-discovered by `main.py`. Use `upload_concerts()` for classical-only sources or `upload_potential_concerts()` for general sources. Set `CrawlerConfig.country_code` to an ISO 3166-1 alpha-2 code and save a CSV backup to `data/<site>.csv`.

## General notes

If I ask you to analyse the data, I mean to analyse the production database with the `agent_utils/search_db.py` script unless I tell you otherwise.

When production runtime behavior or failures require log evidence, use the `search-production-logs` skill to query VictoriaLogs. It expects the private base URL in `VICTORIALOGS_URL`.
