# ClassicalBot

Crawlers for classical music concerts websites.

## Production services

The repository deploys two independent CapRover services:

- `classical-bot` is the normal concert pipeline. It is built from the default
  `Dockerfile`, applies database migrations, and starts `python main.py`. The
  app supervises a continuous crawler worker and two independent Codex workers:
  programme extraction and, when enabled, source-by-source potential-event
  classification. Five crawler subprocesses run concurrently by default.
  Each crawler is attempted at most once per rolling 24-hour period.
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
Codex analyzer stops immediately without consuming attempts. The continuous
crawler worker continues. Both analyzers share the same persistent authentication
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
minutes; stalled batch processes are terminated without stopping crawlers.
Deployments wait for active crawler and analyzer batches to finish, with a
one-hour maximum drain period.

Potential-event classification is disabled by default until its migration and
credentials are deployed. Set `POTENTIAL_EVENT_CLASSIFIER_ENABLED=true` to run
it continuously. Each child run takes one source, snapshots its eligible
unclassified current/future events, and reuses one Codex thread across bounded
pages. Classical results become promotion candidates, nonclassical results are
retained as decisions, and genuinely ambiguous events remain `uncertain` for a
later retry.
Committed classification runs do not publish new concerts by default. They do
record every assessment, and an explicit `nonclassical` or `not_event` decision
quarantines an exact matching existing concert; quarantine is currently a
backend review state and does not hide the row from the public API. Set
`POTENTIAL_EVENT_CLASSIFIER_PROMOTION_ENABLED=true` only after reviewing the
stored inclusion assessments; the worker then passes the explicit `--promote`
flag and promotes the validated backlog without another model run.
Use `uv run python -m analyzers.analyze_potential_events --include-past` for a
read-only historical preview, adding `--commit` only for an intentional backlog
run and `--promote` only when publication is intentional. Use `--source NAME
--reanalyze --commit` to reassess a completed source. Musical theatre is judged
by musical substance rather than its label:
classically substantial works and concert/orchestral presentations such as
Bernstein's *West Side Story* are eligible; routine commercial productions
without a meaningful classical connection are not. The same inclusion guidance
is used by potential-event classification and programme extraction. Named
canonical repertoire is not required when classical performance practice is
clearly central; this includes substantial symphonic modern-song arrangements,
family opera or orchestral storytelling, and contemporary dance built around a
classical score or live classical forces. Vague branding or an incidental
orchestra mention remains insufficient, but an orchestra or chamber ensemble
explicitly billed as a principal or co-equal performer is direct evidence of
classical crossover even with jazz, cabaret, popular-song, vocal, or rhythm-section
elements. A curated Kurt Weill programme led by a string trio is therefore
eligible. One validated `classical` assessment is sufficient for promotion;
repeated model consensus is not required, while `uncertain` never promotes.

Programme responses are structurally and catalogue-validated before acceptance.
Unknown composer/work IDs and work/composer ownership mismatches are returned to
the same Codex thread for up to two complete correction turns. Database,
authentication, capacity, transport, and timeout failures remain technical
failures handled by the existing retry and supervision paths.

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
CRAWLER_CONCURRENCY=5
CRAWLER_TIMEOUT_SECONDS=1800
CRAWLER_HISTORY_RETENTION_DAYS=90
POTENTIAL_EVENT_CLASSIFIER_ENABLED=false
POTENTIAL_EVENT_CLASSIFIER_PROMOTION_ENABLED=false
```

# Run crawlers

```
uv run python -m crawlers.sk.filharmonia_sk.main
```

## Codex resumes:

musicbrainz:
codex resume 019fb970-7f0f-7081-bc57-9556b294591b
