# ClassicalBot

Crawlers for classical music concerts websites.

## Production services

The repository deploys two independent CapRover services:

- `classical-bot` is the normal concert pipeline. It is built from the default
  `Dockerfile`, applies database migrations, and starts `python main.py`. The
  app supervises the daily crawler scheduler and two independent Codex workers:
  continuous programme extraction and, when enabled, source-by-source
  potential-event classification. Scraping does not wait for either analyzer.
  Its persistent runtime state is stored under
  `/var/lib/classical-bot`.
- `classical-crawler-factory` creates and validates crawler changes with Codex.
  Its CapRover deployment uses `captain-definition-crawler-factory`, which
  selects `Dockerfile.crawler-factory`, and starts
  `python -m automation.run_crawler_factory_service`. It keeps its scheduler,
  worker, GitHub CLI, and Codex state separate from the normal pipeline.

The factory can publish crawler changes, but it does not run the production
concert-scraping pipeline. See `automation/README.md` for its deployment and
runtime details.

## Codex authentication in production

Both production images set `CODEX_HOME=/codex-home`; do not duplicate that
variable in CapRover. Keep a separate persistent credential directory for each
service so concurrent Codex processes never read or rotate the same
`auth.json`:

| Service | Path in app | Path on host |
|---|---|---|
| `classical-bot` | `/codex-home` | `/captain/data/codex-auth-classical-bot` |
| `classical-crawler-factory` | `/codex-home` | `/captain/data/codex-auth-crawler-factory` |

Authenticate a new `classical-bot` directory from the CapRover host with a
temporary container built from the deployed app image:

```bash
docker run --rm -it \
  -v /captain/data/codex-auth-classical-bot:/codex-home \
  CLASSICAL_BOT_IMAGE \
  codex login --device-auth
```

Verify it with a real authenticated request, not only `codex login status`:

```bash
docker run --rm \
  -v /captain/data/codex-auth-classical-bot:/codex-home \
  CLASSICAL_BOT_IMAGE \
  codex exec --json "Reply with exactly OK."
```

Replace `CLASSICAL_BOT_IMAGE` with the deployed image reported by
`docker service inspect`. Never copy refreshed credentials back from an older
seed, mount one credential directory into multiple running services, print
`auth.json`, or store it in the repository or logs. See
`automation/README.md` for the factory-specific authentication procedure.

If a Codex request reports revoked or missing authentication, the affected
Codex analyzer stops immediately without consuming attempts. The daily crawler
scheduler continues. Both analyzers share the same persistent authentication
pause, which survives restarts
at `/var/lib/classical-bot/codex-auth-required.json` and emits
`codex_auth_pause_active` once per minute for alerting.

Reauthenticate the same host directory with the temporary-container command
above. The running service notices the changed `auth.json`, performs one real
bounded Codex smoke request, removes the pause only after success, and emits
`codex_auth_restored`. Do not delete the pause marker to bypass verification.
Set `TELEGRAM_ALERT_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` on the application
to receive the initial pause and verified-recovery notifications directly in
Telegram. Setup and testing are documented in `deployment/alerting/README.md`;
no separate alerting service is required.

Inside the running app container, inspect or force the guarded smoke check with:

```bash
python -m automation.codex_auth status
python -m automation.codex_auth resume
```

The in-app programme analyzer defaults to batches of 100 concerts with
concurrency 4. It immediately continues after a full batch and waits five
minutes after draining the eligible queue. Fatal batches back off for fifteen
minutes; stalled batch processes are terminated without stopping the daily
scraper scheduler. Deployments normally wait for the active analyzer batch to
finish, with a one-hour maximum drain period.

Potential-event classification is disabled by default until its migration and
credentials are deployed. Set `POTENTIAL_EVENT_CLASSIFIER_ENABLED=true` to run
it continuously. Each child run takes one source, snapshots its eligible
unclassified current/future events, and reuses one Codex thread across bounded
pages. Classical results are promoted, nonclassical results are retained as
decisions, and genuinely ambiguous events remain `uncertain` for a later retry.
Use `uv run python -m analyzers.analyze_potential_events --include-past` for a
read-only historical preview, adding `--commit` only for an intentional backlog
run. Musical theatre is judged by musical substance rather than its label:
classically substantial works and concert/orchestral presentations such as
Bernstein's *West Side Story* are eligible; routine commercial productions
without a meaningful classical connection are not.

Structure:

`crawlers/{country_code}/` - crawlers for given country
`crawlers/{country_code}/{url}.py` - crawler for given url

## Env:

```
API_URL=
DB_HOST=
DB_NAME=classical_sk
DB_USER=
DB_PASS=
DB_PORT=5432
HTTP_PROXY=
HTTPS_PROXY=
PYTHONUNBUFFERED=1
RUN_JOBS_ON_STARTUP=false
POTENTIAL_EVENT_CLASSIFIER_ENABLED=false
```

# Run crawlers

```
uv run python -m crawlers.sk.filharmonia_sk.main
```

## Codex resumes:

musicbrainz:
codex resume 019fb970-7f0f-7081-bc57-9556b294591b
