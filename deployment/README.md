# Deferred scraper deployment

The shared `CapRoverUpdater` compares the deployed image SHA with `master`,
invokes a private Captain Webhook for newer commits, persists successful
requests, and keeps failed checks retryable. The crawler factory and normal
scraper runner apply their own scheduling guards around this shared mechanism.

After the daily crawlers have returned, the scheduler writes a persistent
update-check request. The parent process checks
`master`; failed checks remain retryable every five minutes. A conclusive
no-update result consumes the request and disables checks until the next daily
pipeline finishes.

The continuous programme analyzer and optional potential-event classifier are
part of the same `classical-bot` app and do not perform their own update checks.
When a newer commit is found, the parent lets both current Codex runs finish and
prevents new work from starting. It invokes the deployment webhook only after
both workers are drained. If either does not finish within one hour, supervised
shutdown safely interrupts it before deployment.

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
| `SCRAPER_UPDATE_REQUEST_PATH` | `/var/lib/classical-bot/update-check-request.json` | Daily scheduler-to-parent update signal |
| `SCRAPER_UPDATE_RETRY_SECONDS` | `300` | Retry interval for failed checks and webhook requests |

If the webhook or deployed commit SHA is unavailable, scraping continues and
automatic deployment is disabled with a log message.

## Continuous programme analyzer

The default `classical-bot` Docker image supervises both the daily scraper
scheduler and the continuous programme analyzer. Configure the production
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
| `POTENTIAL_EVENT_CLASSIFIER_IDLE_SECONDS` | `300` | Wait after draining the eligible queue |
| `POTENTIAL_EVENT_CLASSIFIER_FAILURE_BACKOFF_SECONDS` | `900` | Wait after a fatal source run |
| `POTENTIAL_EVENT_CLASSIFIER_TURN_TIMEOUT_SECONDS` | `1800` | Codex deadline per bounded source page; process guards are derived from it |
