# Crawler factory deployment

The crawler factory is a separate continuous CapRover worker. Its long-running
supervisor runs crawler-creation batches one after another while eligible
sources exist. Each batch and every Codex process remain sequential. The
supervisor drains before factory-relevant deployments, so automatic updates do
not interrupt active agents.

SHA comparison, webhook delivery, retry state, and duplicate suppression are
shared with the normal scraper runner through `deployment/caprover_updater.py`.
The factory image pins GitHub CLI 2.94.0 from GitHub's official release package,
verifies its architecture-specific SHA-256 checksum, and asserts the installed
version during the build. When upgrading it, update the version and official
package checksums together in `Dockerfile.crawler-factory`.

Each batch clones `master` into a temporary directory, atomically claims at
most five due sources from PostgreSQL, commits generated `main.py` or
`BLOCKED.md` results, and opens one pull request. The supervisor waits for the
required checks and then submits a guarded synchronous squash merge.
Codex runs with full filesystem access inside the dedicated worker container and
its disposable clone. The worker passes only an allow-listed child environment,
rejects changes outside the assigned crawler directory, resets failed attempts,
and validates generated crawlers before committing them. Avoiding a second,
nested Codex filesystem sandbox keeps the worker compatible with container
runtimes that do not permit nested sandbox namespaces.

Codex investigates and implements each crawler with targeted parser checks. For
a generated `main.py`, the worker performs one authoritative full scrape before
committing it. The live validation applies the same transformations and
deduplication as production without writing a CSV or uploading data. Invalid
records prevent publication; external availability failures and validation
timeouts are retained as inconclusive reports and retried later.

The required GitHub check is deliberately limited to deterministic repository
safety: generated-file scope, protection of existing crawlers, file size,
UTF-8, likely-secret detection, and Python syntax. Live availability, record
quality, pagination, and runtime duration are not merge gates.

## Runtime behavior

The defaults baked into `Dockerfile.crawler-factory` are:

- run continuously, one batch at a time;
- process at most five URLs per batch;
- give each Codex generation 60 minutes;
- after opening a pull request, poll it every minute, update its branch when
  `master` advances, and wait until it is merged or closed;
- poll for eligible work every five minutes when a batch claims fewer than five sources;
- back off for 15 minutes after a batch-level failure.

Set `CRAWLER_FACTORY_MODE=scheduled` to restore the former once-daily behavior.
In scheduled mode the supervisor stores `last_factory_attempt_date` in
`/var/lib/crawler-factory/service-state.json` before starting a batch and runs
at `06:00` in `Europe/Prague` by default.

The batch runs as a synchronous child process. When a batch opens a pull request,
it immediately returns the URL and exact `master` SHA cloned by the batch so the
supervisor can persist them before any merge attempt.
It will not start another batch while that pull request remains open. If
`master` advances at any point, the supervisor uses `gh pr update-branch` to
merge the new base into its own `crawler-factory/*` branch. Both required checks
then run again against the updated head. Once `test` and
`crawler-factory-validation` are complete and successful and GitHub reports the
PR clean and mergeable, the supervisor runs `gh pr merge --squash
--delete-branch --match-head-commit <head-sha>`. It never uses `--admin`, so the
repository ruleset remains authoritative. The supervisor refuses to modify
unexpected repositories or branch names.

Pending, missing, successful, or failed checks all retain the gate. Failed
branch updates preserve the PR and generated commits and retry with exponential
backoff capped by the normal 15-minute failure interval. A genuine merge
conflict therefore pauses the factory for manual resolution instead of
discarding or regenerating crawler work. The URL, synchronized base SHA, and
retry state survive container restarts. Synchronous merge failures use the same
bounded exponential backoff and retain the exact observed head SHA; a changed
head must pass fresh checks before another guarded merge. A merged pull request
is classified by the existing update check before work resumes; a pull request
closed without merging clears the gate and is left to the existing source
reconciliation flow.

Before starting another batch,
the supervisor compares the latest unclassified `master` SHA with the previous
one. Changes entirely below `crawlers/` are recorded without redeploying because
each batch clones current `master`. Any other change drains the service and
requests a CapRover deployment. Inconclusive comparisons also keep the service
drained until they can be retried safely. A successful deployment request for
the same commit is suppressed for 30 minutes; failed Git or webhook calls remain
retryable.

## CapRover deployment

### 1. Create a separate app

Create a new CapRover app such as `crawler-factory`. Keep it separate from the
existing crawler-runner app, run exactly one instance, and do not expose it as
an HTTP web application. The deployment webhook is managed by CapRover itself;
the factory container does not listen on a public port.

In the factory app's **Deployment** tab:

1. Configure the repository as
   `github.com/druskacik/classical_bot`.
2. Configure the branch as `master`.
3. Set the captain-definition path to
   `./captain-definition-crawler-factory`.
4. Configure repository authentication or a deploy key as usual.
5. Save the settings and copy the deployment webhook shown by CapRover.

Do **not** add this factory webhook to the GitHub repository's push webhooks.
An ordinary push could then replace the container while Codex is running. The
factory supervisor is the only component that should call this webhook
automatically.

### 2. Configure persistent directories

In **App Configs > Persistent Directories**, create three persistent volume
mappings:

| Path in app | Storage | Purpose |
|---|---|---|
| `/var/lib/crawler-factory` | CapRover-managed label `crawler-factory-state` | supervisor state, worker lock, and run reports |
| `/factory-home` | CapRover-managed label `crawler-factory-home` | isolated GitHub CLI home |
| `/codex-home` | host path `/captain/data/codex-auth-crawler-factory` | factory-only Codex authentication and configuration |

Keep the app at one instance. These directories survive image rebuilds,
deployments, and container replacement. The image already sets
`CODEX_HOME=/codex-home`; do not configure `CODEX_HOME` in CapRover.

Do not mount the normal `classical-bot` credential directory here. Each
concurrent service must have its own `auth.json` so one process cannot rotate a
refresh token while another process still holds the previous value.

### 3. Configure environment variables

Set these in **App Configs > Environmental Variables**:

| Variable | Required/default | Purpose |
|---|---|---|
| `GH_TOKEN` | required | GitHub contents and pull-request write access, without branch-protection bypass |
| `CRAWLER_FACTORY_DEPLOY_WEBHOOK` | required for auto-update | private webhook copied from this CapRover app |
| `CRAWLER_FACTORY_REPOSITORY` | repository URL | repository cloned by the worker and checked for updates |
| `CRAWLER_FACTORY_MODE` | `continuous` | `continuous` or rollback-compatible `scheduled` operation |
| `CRAWLER_FACTORY_SCHEDULE_TIME` | `06:00` | daily time used only in scheduled mode |
| `CRAWLER_FACTORY_TIMEZONE` | `Europe/Prague` | IANA timezone used in scheduled mode and timestamps |
| `CRAWLER_FACTORY_UPDATE_INTERVAL_MINUTES` | `5` | drained deployment/update retry interval |
| `CRAWLER_FACTORY_IDLE_INTERVAL_MINUTES` | `5` | delay after a batch claims fewer than five sources |
| `CRAWLER_FACTORY_FAILURE_BACKOFF_MINUTES` | `15` | delay after a batch-level failure |
| `CRAWLER_FACTORY_PR_POLL_INTERVAL_MINUTES` | `1` | interval while waiting for an open factory pull request |
| `CRAWLER_FACTORY_MAX_URLS` | `5` | per-batch URL limit |
| `CRAWLER_FACTORY_TIMEOUT_MINUTES` | `60` | per-URL Codex timeout |
| `CRAWLER_FACTORY_VALIDATION_TIMEOUT_MINUTES` | `15` | per-crawler full-scrape validation timeout |
| `DB_HOST` | required | PostgreSQL host containing the crawler source registry |
| `DB_NAME` | required | PostgreSQL database name |
| `DB_USER` | required | PostgreSQL user |
| `DB_PASS` | required | PostgreSQL password |
| `DB_PORT` | `5432` in normal deployments | PostgreSQL port |

CapRover supplies `CAPROVER_GIT_COMMIT_SHA` to the Docker build for a Git-based
deployment. `Dockerfile.crawler-factory` copies that build argument into the
running container, where the supervisor compares it with the current `master`
SHA. Do not configure it manually in CapRover's environmental variables,
because a manually entered value would become stale after deployment.
The supervisor normalizes and validates both commit SHAs and records each
successfully requested target SHA in its persistent service state. A container
restart therefore cannot request the same deployment repeatedly.

All numeric settings must be positive integers. An invalid mode, schedule,
timezone, or numeric setting stops startup with an explicit error. If either the
CapRover webhook or deployed commit SHA is unavailable, crawler creation still
runs but automatic updates are disabled with a warning.

### 4. Deploy and authenticate

Before starting the factory against an empty credential directory, keep the
service scaled to zero and authenticate its persistent Codex home from the
CapRover host. Create the host directory with restricted permissions:

```bash
sudo install -d -m 700 /captain/data/codex-auth-crawler-factory
```

Find the deployed factory image with `docker service inspect`, then use that
image in a temporary container whose only job is device authentication:

```bash
docker run --rm -it \
  -v /captain/data/codex-auth-crawler-factory:/codex-home \
  CRAWLER_FACTORY_IMAGE \
  codex login --device-auth
```

Verify the credentials with an authenticated request before scaling the
factory up:

```bash
docker run --rm \
  -v /captain/data/codex-auth-crawler-factory:/codex-home \
  CRAWLER_FACTORY_IMAGE \
  codex exec --json "Reply with exactly OK."
```

Replace `CRAWLER_FACTORY_IMAGE` with the exact deployed image name. The
temporary containers are removed after each command, while the credentials
remain in the host directory. Never print, commit, or repeatedly restore an
older copy of `auth.json`. For planned credential replacement, stop the factory
before reauthenticating. After an auth-triggered pause, leave the supervisor
running: it cannot claim sources and will verify the replacement automatically.

An explicit Codex authentication failure now stops further source claims. The
current attempt is recorded as abandoned and immediately retryable rather than
waiting seven days. Valid sources completed earlier in the batch are still
published. The persistent pause marker is
`/var/lib/crawler-factory/codex-auth-required.json`; while it exists the
supervisor may reconcile an already-open PR but will not start another Codex
batch.

Reauthenticate the mounted host directory with the device-login command above.
The paused supervisor detects the changed `auth.json`, performs one real Codex
smoke request, and resumes only after it succeeds. Do not delete the marker to
bypass the check. `codex_auth_required`, `codex_auth_pause_active`, and
`codex_auth_restored` are the structured incident lifecycle events.

If `TELEGRAM_ALERT_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` are configured, the
factory sends one direct Telegram notification when it creates the pause and
one after authentication is verified and processing resumes. Notification
delivery is best-effort and cannot bypass or delay the persistent pause. See
`deployment/alerting/README.md` for setup and a safe test command.

Inside the paused factory container, inspect or force the guarded smoke check
after device login with:

```bash
python -m automation.codex_auth status
python -m automation.codex_auth resume
```

Inspect the app logs after authentication. Startup should report continuous
batching and the idle interval. If eligible sources exist, the first batch
starts immediately.

## GitHub settings

Protect `master`, disallow force pushes and direct worker pushes, and require
the `test` and `crawler-factory-validation` checks. No approving review is
required for factory PRs. Repository auto-merge is not required.

The worker token must be able to push `crawler-factory/*` branches and create
and merge pull requests, but it must not bypass the `master` ruleset. The first
authenticated batch calls `gh auth setup-git`, pushes a
`crawler-factory/YYYY-MM-DD-<run-id>` branch, and creates a PR. The supervisor
submits the guarded squash merge with branch deletion after the required checks
pass.

When that PR merges, `master` changes. The idle supervisor notices the new SHA
on its next check, calls the private CapRover webhook, and CapRover replaces the
container with an image built from the new commit.

## Operations and troubleshooting

### Inspect activity

Use the CapRover app logs for scheduling, batch exit status, update checks, and
deployment requests. Detailed generation reports remain under:

```text
/var/lib/crawler-factory/runs/<run-id>/
```

The persistent scheduler state and worker lock are:

```text
/var/lib/crawler-factory/service-state.json
/var/lib/crawler-factory/factory.lock
```

Source status, retry dates, URL aliases, crawler paths, runs, and attempts live
in PostgreSQL. `service-state.json` contains deployment suppression state, the
last classified `master` SHA, the pending factory pull request URL and
synchronized base SHA, branch-update retry state when needed, and the last
scheduled attempt date when scheduled mode is used.

### Apply the initial source seed

Apply migrations before deploying the DB-backed factory:

```bash
uv run alembic upgrade head
```

Then import the versioned legacy URL seed against production:

```bash
uv run python -m seeds.import_crawler_sources \
  seeds/crawler_sources/0001_legacy_builder_urls.csv \
  --prod
```

The importer is transactional and records the filename and SHA-256 checksum.
Reapplying the identical file is a no-op; changing an applied file is rejected,
so corrections and additional source batches must use a new numbered CSV.
Existing `main.py` and `BLOCKED.md` directories are reconciled without Codex
calls. The legacy Salvator URL is stored as an alias of
`farnostsalvator.cz` and owns `crawlers/cz/farnostsalvator_cz`.

Apply every seed in filename order. `0002_existing_crawlers.csv` registers the
historically hand-maintained Slovak crawlers and any other existing crawler
that was absent from the original builder list:

```bash
uv run python -m seeds.import_crawler_sources \
  seeds/crawler_sources/0002_existing_crawlers.csv \
  --prod
```

### Run a manual batch

Running the worker module directly inside the existing container does not
affect the supervisor's scheduling state:

```bash
docker exec -it <container-id> \
  python -m automation.run_crawler_factory \
    --max-urls 1
```

Use `--source-id ID` to target a due registry row. `--url URL` remains
available for manual discovery/debugging and idempotently ingests the URL
before selection; add `--country-code XX` when the country cannot be inferred.
Use `--validation-timeout-minutes N` to give an unusually slow crawler a larger
one-off full-scrape budget. A validation timeout never publishes an unverified
crawler; it retains an inconclusive report and returns the source to retry.
Inside the deployed container, the repository defaults to
`CRAWLER_FACTORY_REPOSITORY`. Pass `--repository` only to override it or when
running outside that configured environment.

The worker's file lock prevents this command from overlapping an active batch.

### Force an update

Use **Deploy Now** in CapRover or invoke the private deployment webhook
manually. A manually initiated deployment is outside the supervisor's idle
guard and can interrupt an active batch, so first confirm in the logs that no
batch is running.

### Recover from a failed update

If a CapRover build fails, the existing container remains on its old deployed
SHA. The supervisor retries a successfully accepted webhook request after 30
minutes. A rejected webhook or failed Git check is retried after the normal
five-minute interval. Fix the build on `master` or use CapRover's deployment
history to redeploy or roll back.

### Local image and one-shot rehearsal

Build the image locally with:

```bash
docker build -f Dockerfile.crawler-factory -t classical-crawler-factory .
```

For a one-shot local rehearsal without the supervisor or a pushed PR:

```bash
uv run python -m automation.run_crawler_factory \
  --repository /path/to/local/bare-repository.git \
  --url https://example.cz/ \
  --country-code CZ \
  --max-urls 1 \
  --no-push \
  --keep-workspace
```
