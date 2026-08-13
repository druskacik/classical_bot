<!-- crawler-factory-metadata
{"url":"https://www.tpo.or.jp/","geographic_scope":"country","country_code":"JP","reason_code":"access_blocked","attempted_at":"2026-08-13","retry_after":"2026-09-12"}
-->

# Tokyo Philharmonic Orchestra crawler blocked

## Original URL

https://www.tpo.or.jp/

## Why implementation is currently blocked

The Tokyo Philharmonic Orchestra website publishes concrete current and past
concerts, but its origin did not return any HTTP response from the crawler
environment. Connections to the resolved host (`61.112.21.183`) timed out before
response headers were received. Consequently, the live first-party feed could
not be inspected or tested safely.

Search-engine indexing confirms that concert listings exist at `/concert/`, past
performances at `/concert/end.php`, and a calendar at `/calendar/`. It also shows
individual detail URLs under `/concert/`. Indexed snippets include orchestral
concerts, opera, ballet, family concerts, and touring performances. Those snippets
are not a stable or complete first-party interface and cannot substitute for live
pagination and detail-page validation.

## Approaches attempted

- Opened both the home page and `/concert/` with the Playwright MCP. Each
  navigation timed out after 60 seconds before `DOMContentLoaded`, so no network
  request list, API response, filter identifiers, or DOM snapshot was available.
- Retried the origin with direct HTTPS and HTTP requests using a browser user
  agent. DNS resolved successfully, but connections timed out without headers or
  a response body.
- Checked indexed first-party URLs to establish that current and archived
  concerts exist and that the organization is a Japan-based, classical-only
  source. This was useful for scope confirmation only; it did not expose a
  verifiable API or stable pagination contract.
- No first-party genre/category filter values could be tested. The visible source
  is the orchestra's own concert feed and appears classical-only, but neither its
  query behavior nor persistence across pagination/date ranges could be verified.

## What would unblock implementation

Restore network reachability from the crawler environment (or have the site
allowlist its egress address). Once reachable, inspect listing and calendar
network traffic for a structured endpoint, verify current and archive pagination,
inspect representative orchestra/opera/ballet/family/touring detail pages, and
then implement and test the parser against live responses.
