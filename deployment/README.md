# Deferred scraper deployment

The shared `CapRoverUpdater` compares the deployed image SHA with `master`,
invokes a private Captain Webhook for newer commits, persists successful
requests, and keeps failed checks retryable. The normal service checks every
five minutes and before each crawler or analyzer batch. When a newer commit is
found, all workers stop claiming work and finish their current batches. The
webhook runs after all three workers drain; after one hour, supervised shutdown
interrupts remaining child processes.

## CapRover configuration

1. Keep the app's existing repository deployment configured for the `master`
   branch and its current Dockerfile build.
2. Remove the scraper app's Captain Webhook from GitHub's push webhooks. A push
   must not invoke CapRover directly.
3. Copy the scraper app's private Captain Webhook into the app environment as
   `SCRAPER_DEPLOY_WEBHOOK`.
4. Add a persistent-directory mapping for `/var/lib/classical-bot`. It stores
   the last requested commit so restarts do not request the same deployment
   repeatedly.
5. Deploy this revision manually once. CapRover supplies
   `CAPROVER_GIT_COMMIT_SHA` during repository builds; the Dockerfile exposes it
   to the running process for comparison with `master`.

Optional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPER_REPOSITORY` | `https://github.com/druskacik/classical_bot.git` | Repository queried for `master` |
| `SCRAPER_DEPLOY_STATE_PATH` | `/var/lib/classical-bot/deployment-state.json` | Persistent updater state |
| `SCRAPER_UPDATE_RETRY_SECONDS` | `300` | Retry interval for failed checks and webhook requests |

If the webhook or deployed commit SHA is unavailable, scraping continues and
automatic deployment is disabled with a log message.

## Continuous crawler worker

Crawler entrypoints are discovered from `crawlers/*/*/main.py`. PostgreSQL
orders them by oldest attempt, records claims before launch, and retains attempt
history. A crawler is eligible only when it has never been attempted or its
last attempt began at least 24 hours ago. This applies to failures and timeouts
as well as successful runs. When nothing is eligible, the worker rechecks after
five minutes while remaining responsive to deployment draining.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CRAWLER_CONCURRENCY` | `5` | Concurrent crawler subprocesses |
| `CRAWLER_TIMEOUT_SECONDS` | `1800` | Hard deadline for one crawler |
| `CRAWLER_TERMINATE_GRACE_SECONDS` | `30` | SIGTERM grace before SIGKILL |
| `CRAWLER_LEASE_SECONDS` | timeout + 300 | Database claim lifetime |
| `CRAWLER_HISTORY_RETENTION_DAYS` | `90` | Completed attempt retention |

## Continuous programme analyzer

The default `classical-bot` Docker image supervises the crawler worker and the
continuous programme analyzer. Configure the production
database and trusted Codex authentication on that app without placing
credentials in the repository or image. Database migrations run once before
the combined runtime starts.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONCERT_PROGRAM_BATCH_SIZE` | `100` | Maximum concerts selected per child batch |
| `CONCERT_PROGRAM_CONCURRENCY` | `4` | Concurrent Codex group turns |
| `CONCERT_PROGRAM_IDLE_INTERVAL_SECONDS` | `300` | Wait after draining the queue |
| `CONCERT_PROGRAM_FAILURE_BACKOFF_SECONDS` | `900` | Wait after a fatal batch |
| `CONCERT_PROGRAM_STALL_TIMEOUT_SECONDS` | `2400` | Kill a child with no group progress |
| `CONCERT_PROGRAM_BATCH_TIMEOUT_SECONDS` | `72000` | Hard child-batch deadline |
| `CONCERT_PROGRAM_DEPLOY_DRAIN_TIMEOUT_SECONDS` | `3600` | Maximum wait for a batch boundary before deployment |

## Potential-event classifier

The classifier is opt-in and processes one source snapshot per child process.
Automatic runs select only current/future events; historical processing must be
started explicitly with the analyzer CLI.

| Variable | Default | Purpose |
| --- | --- | --- |
| `POTENTIAL_EVENT_CLASSIFIER_ENABLED` | `false` | Enable the continuous source classifier |
| `POTENTIAL_EVENT_CLASSIFIER_PROMOTION_ENABLED` | `false` | Promote validated classical decisions; false never inserts new concerts, but explicit negative decisions can quarantine exact existing matches |
| `POTENTIAL_EVENT_CLASSIFIER_IDLE_SECONDS` | `300` | Wait after draining the eligible queue |
| `POTENTIAL_EVENT_CLASSIFIER_FAILURE_BACKOFF_SECONDS` | `900` | Wait after a fatal source run |
| `POTENTIAL_EVENT_CLASSIFIER_TURN_TIMEOUT_SECONDS` | `1800` | Codex deadline per bounded source page; process guards are derived from it |

The programme analyzer records an independent occurrence-level inclusion
assessment. Explicit `nonclassical` and `not_event` results quarantine the row,
disable further programme attempts, and remove catalogue links. Quarantine is
currently a backend review state; public API filtering is intentionally deferred.
